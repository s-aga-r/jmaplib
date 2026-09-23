"""The event source and PushSubscription lifecycle, end to end.

The fake serves a real ``text/event-stream``, so these exercise the whole path:
URL template expansion, the streaming read, incremental parsing, and the
``Last-Event-ID`` that turns a reconnect into a resumption.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN
from jmap.capabilities.push import VAPID, VAPID_URN, WEBSOCKET, WEBSOCKET_URN
from jmap.capabilities.registry import Registry
from jmap.client import JMAPClient
from jmap.core.errors import (
    AuthenticationError,
    CapabilityFieldError,
    RequestError,
    TransportError,
)
from jmap.models.push import PushSubscription, PushVerification, StateChange
from jmap.push import (
    AsyncEventSourceClient,
    EventSourceClient,
    PendingVerification,
    Ping,
    UnknownPushTypeError,
    needs_recreating,
    new_subscription,
    verification_update,
)
from jmap.push.listener import (
    DEFAULT_RECONNECT_SECONDS,
    ERROR_BODY_LIMIT,
    MIN_RECONNECT_SECONDS,
)
from jmap.testing import FakeJMAPServer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"

#: One read of an error body in the tests below that stream one without end.
ERROR_CHUNK = 16 * 1024


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, MAIL, WEBSOCKET, VAPID):
        reg.register(spec)
    return reg


def server(**capabilities: Any) -> FakeJMAPServer:
    return FakeJMAPServer(
        capabilities={CORE_URN: {}, MAIL_URN: {}, **capabilities},
        primary_accounts={CORE_URN: "a", MAIL_URN: "a"},
    )


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
        registry=registry(),
    )


class TestEventSource:
    def test_a_queued_change_arrives_as_a_state_change(self):
        fake = server()
        fake.push("a", {"Email": "e2"}, event_id="1")
        with connect(fake) as client:
            events = list(EventSourceClient(client).events())
        assert isinstance(events[0], StateChange)
        assert events[0].states_for("a") == {"Email": "e2"}

    def test_several_changes_arrive_in_order(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1")
        fake.push("a", {"Email": "e2"}, event_id="2")
        with connect(fake) as client:
            events = list(EventSourceClient(client).events())
        assert [e.states_for("a")["Email"] for e in events if isinstance(e, StateChange)] == [
            "e1",
            "e2",
        ]

    def test_the_types_filter_reaches_the_url(self):
        fake = server()
        with connect(fake) as client:
            list(EventSourceClient(client, types=("Email",)).events())
        assert fake.event_source_requests

    def test_an_unpushable_type_is_refused_before_connecting(self):
        # The server would simply never send those notifications, which is
        # indistinguishable from nothing having changed.
        fake = server()
        with connect(fake) as client, pytest.raises(UnknownPushTypeError, match="Widget"):
            EventSourceClient(client, types=("Widget",))

    def test_the_accept_header_asks_for_an_event_stream(self):
        fake = server()
        with connect(fake) as client:
            list(EventSourceClient(client).events())
        assert fake.event_source_requests[0]["accept"] == "text/event-stream"


class TestResumption:
    def test_the_cursor_advances_with_the_events(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="7")
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            assert source.last_event_id == "7"

    def test_a_reconnect_sends_the_cursor_back(self):
        # Without it the server cannot replay what was missed, and the client
        # silently restarts from *now* - a gap it can never detect afterwards.
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="7")
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            list(source.events())
        assert "last-event-id" not in fake.event_source_requests[0]
        assert fake.event_source_requests[1]["last-event-id"] == "7"

    def test_a_ping_does_not_move_the_cursor(self):
        # RFC 8620 §7.3 forbids a ping from setting an event id. Resuming from one
        # would skip every change that arrived before it.
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="7")
        fake.push_ping(300)
        with connect(fake) as client:
            source = EventSourceClient(client)
            events = list(source.events())
            assert source.last_event_id == "7"
        assert any(isinstance(event, Ping) for event in events)

    def test_the_ping_carries_the_interval_the_server_settled_on(self):
        fake = server()
        fake.push_ping(45)
        with connect(fake) as client:
            events = list(EventSourceClient(client).events())
        assert events == [Ping(interval=45)]

    def test_a_state_event_without_an_id_leaves_the_cursor_empty(self):
        # RFC 8620 §7.3 only *SHOULD*s an event id, so a server may send none -
        # and then there is nothing to resume from, which is worth knowing rather
        # than papering over with a fabricated cursor.
        fake = server()
        fake.push("a", {"Email": "e1"})
        with connect(fake) as client:
            source = EventSourceClient(client)
            events = list(source.events())
            assert source.last_event_id == ""
        assert len(events) == 1

    def test_a_drop_mid_event_resumes_from_the_last_complete_one(self):
        # Event 5's id line arrived, its blank line did not. It was never
        # delivered, so the reconnect must ask for everything after 4.
        fake = server()
        fake.push("a", {"Email": "e4"}, event_id="4")
        fake.push_events.append('id: 5\nevent: state\ndata: {"@type": "StateChange"}\n')
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            list(source.events())
        assert fake.event_source_requests[1]["last-event-id"] == "4"

    def test_the_cursor_survives_a_connection_that_yielded_nothing(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="9")
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            list(source.events())  # empty stream
            assert source.last_event_id == "9"


class TestCloseAfterState:
    def test_the_connection_ending_after_a_state_event_is_success(self):
        # Some proxies buffer a stream until it completes, holding every
        # notification back. Ending the response is the server doing as asked.
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1")
        with connect(fake) as client:
            source = EventSourceClient(client, close_after_state=True)
            events = list(source.events())
        assert len(events) == 1
        assert fake.event_source_requests[0]

    def test_it_reaches_the_url(self):
        fake = server()
        with connect(fake) as client:
            list(EventSourceClient(client, close_after_state=True).events())
        assert fake.event_source_requests


class TestFailures:
    def test_a_401_raises_the_same_error_the_api_path_does(self):
        # A caller with one `except AuthenticationError` should not need a second.
        fake = server()

        def unauthorised(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(401, headers={"WWW-Authenticate": "Basic realm=x"})
            return None

        with connect(fake) as client:
            fake.intercept = unauthorised
            with pytest.raises(AuthenticationError):
                list(EventSourceClient(client).events())

    def test_a_problem_response_is_raised_as_one(self):
        fake = server()

        def refused(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(
                    429,
                    json={"type": "urn:ietf:params:jmap:error:limit", "status": 429},
                    headers={"Content-Type": "application/problem+json"},
                )
            return None

        with connect(fake) as client:
            fake.intercept = refused
            with pytest.raises(RequestError):
                list(EventSourceClient(client).events())

    def test_a_transport_failure_is_wrapped(self):
        fake = server()

        def broken(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                raise httpx.ConnectError("no route to host")
            return None

        with connect(fake) as client:
            fake.intercept = broken
            with pytest.raises(TransportError, match="no route"):
                list(EventSourceClient(client).events())

    def test_an_endless_error_body_is_not_read_to_the_end(self):
        # Only a hostile server answers the event source with an error and then
        # keeps streaming; the problem parser needs a fraction of the limit, and
        # reading on would be an allocation the size the server chose.
        fake = server()
        pulled = {"chunks": 0}

        def endless() -> Iterator[bytes]:
            while True:
                pulled["chunks"] += 1
                yield b"x" * ERROR_CHUNK

        def refused(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(500, content=endless())
            return None

        with connect(fake) as client:
            fake.intercept = refused
            with pytest.raises(RequestError):
                list(EventSourceClient(client).events())
        assert pulled["chunks"] <= ERROR_BODY_LIMIT // ERROR_CHUNK + 1


class TestSubscriptionLifecycle:
    def test_create_verify_and_renew(self):
        """The three-step dance, in the order RFC 8620 §7.2 describes it."""
        fake = server()
        fake.respond(
            "PushSubscription/set",
            {"created": {"4f29": {"id": "P43", "keys": None, "expires": "2018-07-13T02:14:29Z"}}},
        )
        with connect(fake) as client:
            with client.batch() as batch:
                created = batch.core.push_subscription.set(
                    create={
                        "4f29": new_subscription(
                            "a889-ffea-910", "https://example.com/push/?device=X8980fc"
                        )
                    }
                )
            subscription_id = created.result.created_id("4f29")
            assert subscription_id == "P43"

            # The server pushed a PushVerification to the URL out of band.
            pending = PendingVerification()
            pending.record(PushVerification(pushSubscriptionId="P43", verificationCode="da1f097b"))
            code = pending.claim(subscription_id)
            assert code is not None

            with client.batch() as batch:
                batch.core.push_subscription.set(
                    update={subscription_id: verification_update(code)}
                )
        sent = fake.requests[-1]["methodCalls"][0][1]
        assert sent["update"]["P43"] == {"verificationCode": "da1f097b"}

    def test_the_verification_can_arrive_before_the_create_response(self):
        # §7.2.3's race. A client that only looks for the code after the create
        # returns misses it and waits forever for a second one.
        pending = PendingVerification()
        pending.record(PushVerification(pushSubscriptionId="P43", verificationCode="early"))
        fake = server()
        fake.respond("PushSubscription/set", {"created": {"c": {"id": "P43"}}})
        with connect(fake) as client, client.batch() as batch:
            created = batch.core.push_subscription.set(
                create={"c": new_subscription("dev", "https://push.example.com/x")}
            )
        assert pending.claim(str(created.result.created_id("c"))) == "early"

    def test_the_subscription_is_not_account_scoped(self):
        # §7.2.1: no accountId in, no accountId out. Sending one would be wrong
        # rather than merely redundant.
        fake = server()
        fake.respond("PushSubscription/get", {"list": [], "notFound": []})
        with connect(fake) as client, client.batch() as batch:
            batch.core.push_subscription.get(ids=None)
        assert "accountId" not in fake.requests[0]["methodCalls"][0][1]

    def test_url_and_keys_cannot_be_requested(self):
        # Asking earns `forbidden` for the whole call, which is a confusing way to
        # learn about a typo.
        fake = server()
        refused = pytest.raises(CapabilityFieldError, match="never returned")
        with connect(fake) as client, client.batch() as batch, refused:
            batch.core.push_subscription.get(ids=None, properties=["url"])

    def test_subscriptions_parse_as_typed_objects(self):
        fake = server()
        fake.respond(
            "PushSubscription/get",
            {
                "list": [
                    {
                        "id": "e50b2c1d",
                        "deviceClientId": "b37ff8001ca0",
                        "expires": "2018-07-31T00:13:21Z",
                        "types": ["Todo"],
                    }
                ],
                "notFound": [],
            },
        )
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.core.push_subscription.get(ids=None)
        subscription = fetched.result.items[0]
        assert isinstance(subscription, PushSubscription)
        assert subscription.device_client_id == "b37ff8001ca0"


class TestVapidRotation:
    def test_a_rotated_key_means_the_subscription_must_be_rebuilt(self):
        # RFC 9749 §5: the server destroys subscriptions tied to the old key and
        # notifications simply stop. Nothing raises, so the client has to ask.
        fake = server(**{VAPID_URN: {"applicationServerKey": "new-key"}})
        with connect(fake) as client:
            assert needs_recreating(client.session, "old-key") is True
            assert needs_recreating(client.session, "new-key") is False

    def test_a_server_without_vapid_never_reports_a_rotation(self):
        fake = server()
        with connect(fake) as client:
            assert needs_recreating(client.session, "old-key") is False


class TestWebSocketDiscovery:
    def test_the_endpoint_is_read_from_the_session(self):
        fake = server(
            **{WEBSOCKET_URN: {"url": "wss://jmap.example.com/jmap/ws/", "supportsPush": True}}
        )
        with connect(fake) as client:
            from jmap.capabilities.push import WebSocketCapability

            capability = WebSocketCapability.of(client.session.capability_value(WEBSOCKET_URN))
        assert capability.url == "wss://jmap.example.com/jmap/ws/"
        assert capability.supports_push is True
        assert capability.is_secure is True

    def test_a_server_without_the_capability_has_no_endpoint(self):
        fake = server()
        with connect(fake) as client:
            from jmap.capabilities.push import WebSocketCapability

            capability = WebSocketCapability.of(client.session.capability_value(WEBSOCKET_URN))
        assert capability.url is None


class TestAsyncEventSource:
    """The async twin must behave identically - it is the same parser underneath.

    Written async-native rather than driven through a portal, because the portal
    approach systematically hides exactly the cancellation and streaming bugs
    these are here to catch.
    """

    async def aconnect(self, fake: FakeJMAPServer) -> AsyncJMAPClient:
        return await AsyncJMAPClient.connect(
            WELL_KNOWN,
            auth=BasicAuth("alice@example.com", "pw"),
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(fake.route), follow_redirects=True
            ),
            registry=registry(),
        )

    @pytest.mark.asyncio
    async def test_a_queued_change_arrives(self):
        fake = server()
        fake.push("a", {"Email": "e2"}, event_id="1")
        async with await self.aconnect(fake) as client:
            events = [event async for event in AsyncEventSourceClient(client).events()]
        assert isinstance(events[0], StateChange)
        assert events[0].states_for("a") == {"Email": "e2"}

    @pytest.mark.asyncio
    async def test_the_cursor_resumes_on_reconnect(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="7")
        async with await self.aconnect(fake) as client:
            source = AsyncEventSourceClient(client)
            [event async for event in source.events()]
            assert source.last_event_id == "7"
            [event async for event in source.events()]
        assert fake.event_source_requests[1]["last-event-id"] == "7"

    @pytest.mark.asyncio
    async def test_a_ping_does_not_move_the_cursor(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="7")
        fake.push_ping(300)
        async with await self.aconnect(fake) as client:
            source = AsyncEventSourceClient(client)
            events = [event async for event in source.events()]
            assert source.last_event_id == "7"
        assert any(isinstance(event, Ping) for event in events)

    @pytest.mark.asyncio
    async def test_a_401_raises_the_same_error(self):
        fake = server()

        def unauthorised(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(401)
            return None

        async with await self.aconnect(fake) as client:
            fake.intercept = unauthorised
            with pytest.raises(AuthenticationError):
                [event async for event in AsyncEventSourceClient(client).events()]

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_wrapped(self):
        fake = server()

        def broken(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                raise httpx.ConnectError("no route to host")
            return None

        async with await self.aconnect(fake) as client:
            fake.intercept = broken
            with pytest.raises(TransportError, match="no route"):
                [event async for event in AsyncEventSourceClient(client).events()]

    @pytest.mark.asyncio
    async def test_a_problem_response_is_raised_as_one(self):
        fake = server()

        def refused(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(
                    503,
                    json={"type": "about:blank", "status": 503},
                    headers={"Content-Type": "application/problem+json"},
                )
            return None

        async with await self.aconnect(fake) as client:
            fake.intercept = refused
            with pytest.raises(RequestError):
                [event async for event in AsyncEventSourceClient(client).events()]

    @pytest.mark.asyncio
    async def test_an_endless_error_body_is_not_read_to_the_end(self):
        fake = server()
        pulled = {"chunks": 0}

        async def endless() -> AsyncIterator[bytes]:
            while True:
                pulled["chunks"] += 1
                yield b"x" * ERROR_CHUNK

        def refused(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(500, content=endless())
            return None

        async with await self.aconnect(fake) as client:
            fake.intercept = refused
            with pytest.raises(RequestError):
                [event async for event in AsyncEventSourceClient(client).events()]
        assert pulled["chunks"] <= ERROR_BODY_LIMIT // ERROR_CHUNK + 1


class TestReconnectLoop:
    """``listen()`` is the loop most callers actually want.

    These use a server-sent ``retry:`` so the delay is 100 ms rather than the
    3 s default - which is itself the behaviour being asserted, since ignoring
    ``retry:`` means hammering a server that asked to be dialled less often.
    """

    def test_it_reconnects_and_resumes(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1", retry=100)
        with connect(fake) as client:
            source = EventSourceClient(client)
            stream = source.listen()
            first = next(stream)
            # Queued only now, so it can only arrive over a second connection.
            fake.push("a", {"Email": "e2"}, event_id="2")
            second = next(stream)
            stream.close()
        assert isinstance(first, StateChange)
        assert isinstance(second, StateChange)
        assert second.states_for("a") == {"Email": "e2"}
        assert fake.event_source_requests[1]["last-event-id"] == "1"

    def test_the_servers_retry_hint_is_honoured(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1", retry=250)
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            assert source._listener.delay() == pytest.approx(0.25)

    def test_a_zero_retry_is_floored_rather_than_spinning(self):
        # RFC 8620 §7.3 puts no floor on retry:, and honouring a literal zero
        # would turn the loop into a busy wait against the server.
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1", retry=0)
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            assert source._listener.delay() == MIN_RECONNECT_SECONDS

    def test_the_default_delay_applies_when_the_server_offers_none(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1")
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            assert source._listener.delay() == DEFAULT_RECONNECT_SECONDS

    def test_the_listener_repr_shows_the_cursor(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="9")
        with connect(fake) as client:
            source = EventSourceClient(client)
            list(source.events())
            assert "'9'" in repr(source._listener)

    @pytest.mark.asyncio
    async def test_the_async_loop_reconnects_too(self):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1", retry=100)
        async with await TestAsyncEventSource().aconnect(fake) as client:
            source = AsyncEventSourceClient(client)
            stream = source.listen()
            first = await anext(stream)
            fake.push("a", {"Email": "e2"}, event_id="2")
            second = await anext(stream)
            await stream.aclose()
        assert isinstance(first, StateChange)
        assert isinstance(second, StateChange)
        assert second.states_for("a") == {"Email": "e2"}
        assert fake.event_source_requests[1]["last-event-id"] == "1"


class TestListenReconnects:
    def test_a_mid_stream_drop_redials_instead_of_escaping(self, monkeypatch):
        # The ping deadline and NAT timeouts surface as transport errors, and
        # they are precisely what reconnection exists for. listen() used to let
        # them escape, so the mechanism built to detect a dead connection
        # killed the listener instead of recovering it.
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1")
        drops = {"remaining": 1}

        def flaky(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path and drops["remaining"]:
                drops["remaining"] -= 1
                raise httpx.ReadError("connection reset mid-stream")
            return None

        naps: list[float] = []
        monkeypatch.setattr("jmap.push.listener.time.sleep", naps.append)
        with connect(fake) as client:
            fake.intercept = flaky
            stream = EventSourceClient(client).listen()
            event = next(stream)
            stream.close()
        assert isinstance(event, StateChange)
        assert naps, "the redial should have waited out the reconnect delay"

    @pytest.mark.asyncio
    async def test_the_async_loop_redials_after_a_drop_too(self, monkeypatch):
        fake = server()
        fake.push("a", {"Email": "e1"}, event_id="1")
        drops = {"remaining": 1}

        def flaky(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path and drops["remaining"]:
                drops["remaining"] -= 1
                raise httpx.ReadError("connection reset mid-stream")
            return None

        naps: list[float] = []

        async def nap(seconds: float) -> None:
            naps.append(seconds)

        monkeypatch.setattr("jmap.push.listener.anyio.sleep", nap)
        async with await TestAsyncEventSource().aconnect(fake) as client:
            fake.intercept = flaky
            stream = AsyncEventSourceClient(client).listen()
            event = await anext(stream)
            await stream.aclose()
        assert isinstance(event, StateChange)
        assert naps, "the redial should have waited out the reconnect delay"

    def test_an_authentication_failure_still_escapes(self, monkeypatch):
        # Retrying a 401 loops on an answer that will not change.
        fake = server()

        def unauthorised(request: httpx.Request) -> httpx.Response | None:
            if "/jmap/eventsource/" in request.url.path:
                return httpx.Response(401, headers={"WWW-Authenticate": "Basic realm=x"})
            return None

        monkeypatch.setattr("jmap.push.listener.time.sleep", lambda _s: None)
        with connect(fake) as client:
            fake.intercept = unauthorised
            with pytest.raises(AuthenticationError):
                next(EventSourceClient(client).listen())

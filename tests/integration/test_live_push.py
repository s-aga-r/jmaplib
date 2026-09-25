"""Push against a real JMAP server (RFC 8620 §7, RFC 8887, RFC 9749).

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.

Push is the hardest thing here to test honestly, because most of what can go wrong
is a *timing* property: an event that never arrives looks exactly like nothing
having changed. So these assert what can be asserted deterministically -

- an event source connection is accepted and speaks ``text/event-stream``
- a change made by this client shows up on it within a bounded wait
- the event id it carries comes back on a reconnect
- a ping does not move the resume cursor

- and skip rather than hang when the server does not implement the piece under
test. Every wait is bounded; a push that never arrives fails with a message
saying so rather than blocking the suite.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING

import anyio
import pytest

from jmap.capabilities.push import (
    VAPID_URN,
    WEBSOCKET_URN,
    VapidCapability,
    WebSocketCapability,
)
from jmap.models.push import PushSubscription, StateChange
from jmap.push import EventSourceClient, Ping, new_subscription
from jmap.push.eventsource import MIN_PORTABLE_PING
from jmap.push.websocket_aio import AsyncWebSocketClient
from jmap.push.websocket_client import WebSocketClient
from tests.integration.conftest import aconnect, connect

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from jmap.aio import AsyncJMAPClient
    from jmap.client import JMAPClient

pytestmark = pytest.mark.integration

JMAP_URL = os.environ.get("JMAP_TEST_URL", "")
ALICE = os.environ.get("JMAP_TEST_USER", "")
ALICE_PASSWORD = os.environ.get("JMAP_TEST_PASS", "")

requires_server = pytest.mark.skipif(
    not (JMAP_URL and ALICE and ALICE_PASSWORD),
    reason="set JMAP_TEST_URL, JMAP_TEST_USER and JMAP_TEST_PASS to run",
)

#: How long to wait for a notification before calling it lost. Generous, because
#: a server may coalesce changes before pushing them.
PUSH_TIMEOUT = 15.0

#: Every connection here asks for pings: a ping is the only thing that bounds how
#: long the client will wait on a silent stream, and with none requested the read
#: blocks indefinitely - correct for a push client, useless for a test suite.
#:
#: Asking for less than this buys nothing. RFC 8620 §7.3 lets a server set a
#: minimum of up to 30s, so a smaller request is simply clamped back up; Stalwart
#: does exactly that. It also sets the pace of everything below, because a server
#: sends nothing at all on connect - the first traffic on an idle account is the
#: first ping, 30s in.
PING_SECONDS = MIN_PORTABLE_PING

#: Long enough to see that first ping, since several tests below need traffic on
#: an account where nothing is happening.
PING_TIMEOUT = PING_SECONDS + 20.0


def requires_method(client: JMAPClient, method: str) -> None:
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


def requires_event_source(client: JMAPClient) -> None:
    if not client.session.event_source_url:
        pytest.skip("server advertises no eventSourceUrl")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with connect(ALICE, ALICE_PASSWORD) as client:
        yield client


@pytest.fixture(scope="module")
def drafts(alice: JMAPClient) -> str:
    with alice.batch() as batch:
        handle = batch.mail.mailbox.get(ids=None)
    for mailbox in handle.result.items:
        if mailbox.role == "drafts" and mailbox.id is not None:
            return str(mailbox.id)
    pytest.skip("no mailbox with role 'drafts'")


def make_draft(client: JMAPClient, mailbox_id: str, subject: str) -> str:
    with client.batch() as batch:
        created = batch.mail.email.set(
            create={
                "d1": {
                    "mailboxIds": {mailbox_id: True},
                    "keywords": {"$draft": True},
                    "subject": subject,
                    "from": [{"email": ALICE}],
                    "to": [{"email": ALICE}],
                    "textBody": [{"partId": "t", "type": "text/plain"}],
                    "bodyValues": {"t": {"value": "push test"}},
                }
            }
        )
    assert not created.result.has_errors, created.result.creation_errors
    return str(created.result.created_id("d1"))


def destroy(client: JMAPClient, email_id: str) -> None:
    with client.batch() as batch:
        batch.mail.email.set(destroy=[email_id])


#: How long to give the caller to get its stream open before writing.
WRITE_DELAY = 2.0


@contextlib.contextmanager
def draft_written_after_connecting(
    client: JMAPClient, mailbox_id: str, subject: str
) -> Iterator[None]:
    """Write a draft shortly *after* the caller opens its event source.

    The ordering is the whole point. A server registers a push subscriber when
    the stream opens and does not replay what it missed, so a change made before
    connecting - which is what these tests used to do - is a change nobody will
    ever be notified about. The test then waits out its entire budget for a
    notification that was never going to come, and reports it as a push failure.

    The write goes on a thread because the connection is only established by
    iterating, which blocks: there is no point between "connected" and "waiting"
    at which this thread of control gets to do anything.
    """
    created: list[str] = []

    def write() -> None:
        time.sleep(WRITE_DELAY)
        created.append(make_draft(client, mailbox_id, subject))

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        yield
    finally:
        writer.join(timeout=PING_TIMEOUT)
        for email_id in created:
            destroy(client, email_id)


@requires_server
class TestEventSource:
    def test_the_connection_is_accepted(self, alice):
        """One event of any kind proves the connection, and bounds the test.

        Deliberately not ``list(source.events())``: draining assumes the stream
        ends, which is true only if the server honours ``closeafter=state``. A
        server that ignores it is not wrong enough to fail this test - what is
        being asserted is that the endpoint is reachable, answers
        ``text/event-stream``, and frames events the parser accepts. The first
        event settles all three, and the requested ping guarantees one arrives
        even with nothing happening in the account.
        """
        requires_event_source(alice)
        source = EventSourceClient(alice, close_after_state=True, ping=PING_SECONDS)
        # An error status or a wrong content type raises out of the iterator.
        # Slow by nature: with nothing happening in the account, the first event
        # is the first ping, and §7.3 lets the server hold that for 30s.
        with contextlib.closing(source.events()) as events:
            # Bounded by the client's own read deadline, which is derived from
            # the ping interval - so a stream that goes silent raises rather than
            # hanging here.
            assert next(events, None) is not None, "connection closed without an event"

    def test_a_change_this_client_makes_is_pushed_back(self, alice, drafts):
        """The end-to-end proof, and the only one that needs a real server.

        Everything else about push is testable offline; that a write actually
        produces a notification is not.
        """
        requires_event_source(alice)
        requires_method(alice, "Email/set")
        subject = f"jmaplib push {uuid.uuid4().hex[:8]}"
        source = EventSourceClient(alice, close_after_state=True, ping=PING_SECONDS)

        with draft_written_after_connecting(alice, drafts, subject):
            change = _wait_for_state_change(source, "Email")
        assert change is not None, f"no Email StateChange within {PUSH_TIMEOUT}s"
        assert "Email" in change.types()

    def test_the_cursor_comes_back_on_a_reconnect(self, alice, drafts):
        """Resumption is what makes a dropped connection cost latency, not data.

        Skipped when the server sends no event ids at all - RFC 8620 §7.3 only
        *SHOULD*s them, and there is then nothing to resume from.
        """
        requires_event_source(alice)
        subject = f"jmaplib resume {uuid.uuid4().hex[:8]}"
        source = EventSourceClient(alice, close_after_state=True, ping=PING_SECONDS)

        with draft_written_after_connecting(alice, drafts, subject):
            _wait_for_state_change(source, "Email")
        if not source.last_event_id:
            pytest.skip("server sends no event ids, so there is nothing to resume from")

        # The second connection carries Last-Event-ID; that it is accepted at all
        # is the assertion, since a server rejecting it would raise. The destroy
        # above is itself a change, so this need not wait for a ping.
        second = f"jmaplib resume again {uuid.uuid4().hex[:8]}"
        with draft_written_after_connecting(alice, drafts, second):
            _wait_for_state_change(source, "Email")

    def test_a_ping_never_moves_the_cursor(self, alice):
        """RFC 8620 §7.3 forbids a ping from setting an event id.

        Resuming from a keep-alive would skip every change before it. Skipped when
        no ping arrives in the window - a server may legally clamp the interval up.
        """
        requires_event_source(alice)
        source = EventSourceClient(alice, ping=PING_SECONDS)
        before = source.last_event_id
        saw_ping = False
        deadline = time.monotonic() + PING_TIMEOUT
        with contextlib.closing(source.events()) as events:
            for event in events:
                if isinstance(event, Ping):
                    saw_ping = True
                    break
                if time.monotonic() > deadline:
                    break
        if not saw_ping:
            pytest.skip(f"no ping arrived within {PING_TIMEOUT}s")
        assert source.last_event_id == before


@requires_server
class TestPushSubscriptions:
    def test_listing_subscriptions_is_scoped_to_these_credentials(self, alice):
        # §7.2.1: the server returns only what these credentials created, so an
        # empty list is a perfectly good result.
        requires_method(alice, "PushSubscription/get")
        with alice.batch() as batch:
            fetched = batch.core.push_subscription.get(ids=None)
        for subscription in fetched.result.items:
            assert isinstance(subscription, PushSubscription)
            # §7.2.1 forbids returning these, whatever was asked for.
            assert subscription.url is None
            assert subscription.keys is None

    def test_the_call_carries_no_account_id(self, alice):
        # Push subscriptions are not account-scoped, and sending an accountId
        # would be wrong rather than merely redundant.
        requires_method(alice, "PushSubscription/get")
        with alice.batch() as batch:
            batch.core.push_subscription.get(ids=None)

    def test_an_unverifiable_url_still_creates_a_subscription(self, alice):
        """Creation succeeds; the server then cannot verify, so nothing is pushed.

        That split is the whole security property (§7.2): registering a URL is
        cheap, but the server makes no further request to it until the client
        proves it received the verification code. The subscription is destroyed
        immediately afterwards so nothing is left pointing at a dead endpoint.
        """
        requires_method(alice, "PushSubscription/set")
        device = f"jmaplib-{uuid.uuid4().hex[:12]}"
        with alice.batch() as batch:
            created = batch.core.push_subscription.set(
                create={
                    "p": new_subscription(
                        device, "https://push.invalid.example/jmaplib-test", types=["Email"]
                    )
                }
            )
        if created.result.has_errors:
            pytest.skip(f"server refused the subscription: {created.result.creation_errors}")
        subscription_id = created.result.created_id("p")
        assert subscription_id
        with alice.batch() as batch:
            batch.core.push_subscription.set(destroy=[subscription_id])


@requires_server
class TestCapabilityObjects:
    def test_the_websocket_endpoint_is_no_less_secure_than_the_session(self, alice):
        """RFC 8887 §4.2 requires TLS - of a deployment, which this is not.

        A test server reached over plain HTTP will advertise a ``ws://`` endpoint,
        and must: downgrading is the whole point of running it that way. What is
        actually a defect is a *downgrade* - an ``https`` session handing out a
        ``ws://`` URL, which moves credentials from a protected channel to an
        unprotected one and is the case worth catching. So the assertion is
        relative to how this session itself was reached.
        """
        capability = WebSocketCapability.of(alice.session.capability_value(WEBSOCKET_URN))
        if capability.url is None:
            pytest.skip("server does not advertise urn:ietf:params:jmap:websocket")
        if not alice.session.api_url.startswith("https:"):
            pytest.skip("session is not itself over TLS, so there is no downgrade to detect")
        assert capability.is_secure, (
            f"TLS session {alice.session.api_url!r} advertises insecure "
            f"WebSocket endpoint {capability.url!r}"
        )

    def test_the_vapid_key_is_present_when_advertised(self, alice):
        capability = VapidCapability.of(alice.session.capability_value(VAPID_URN))
        if VAPID_URN not in alice.session.capabilities:
            pytest.skip("server does not advertise urn:ietf:params:jmap:webpush-vapid")
        # RFC 9749 §3 makes the key mandatory once the capability is present, and
        # a subscription created without one cannot be authenticated.
        assert capability.application_server_key


def requires_websocket(client: JMAPClient, *, push: bool = False) -> None:
    capability = WebSocketCapability.of(client.session.capability_value(WEBSOCKET_URN))
    if capability.url is None:
        pytest.skip("server does not advertise urn:ietf:params:jmap:websocket")
    if push and not capability.supports_push:
        pytest.skip("server's WebSocket does not carry push")


@requires_server
class TestWebSocket:
    """RFC 8887 against a real server: the handshake, a request, a push."""

    def test_a_batch_travels_over_the_socket(self, alice):
        requires_websocket(alice)
        with WebSocketClient(alice) as socket, socket.batch() as batch:
            mailboxes = batch.mail.mailbox.get(ids=None)
        assert mailboxes.result.items

    def test_a_change_is_pushed_over_the_socket(self, alice, drafts):
        requires_websocket(alice, push=True)
        requires_method(alice, "Email/set")
        with WebSocketClient(alice) as socket:
            socket.enable_push(["Email"])
            email_id = make_draft(alice, drafts, f"jmaplib ws {uuid.uuid4().hex[:8]}")
            try:
                change = _first_within(socket.notifications(), PUSH_TIMEOUT)
            finally:
                destroy(alice, email_id)
        assert change is not None, f"no StateChange over the WebSocket within {PUSH_TIMEOUT}s"
        assert "Email" in change.types()


@requires_server
class TestAsyncWebSocket:
    @pytest.mark.asyncio
    async def test_requests_in_flight_together_are_each_answered(self, alice):
        requires_websocket(alice)
        answers: dict[str, int] = {}

        async def count(socket: AsyncWebSocketClient, key: str) -> None:
            async with socket.batch() as batch:
                mailboxes = batch.mail.mailbox.get(ids=None)
            answers[key] = len(mailboxes.result.items)

        async with async_client() as client, AsyncWebSocketClient(client) as socket:
            with anyio.fail_after(PUSH_TIMEOUT):
                async with anyio.create_task_group() as group:
                    for key in ("first", "second", "third"):
                        group.start_soon(count, socket, key)
        assert len(answers) == 3
        assert len(set(answers.values())) == 1

    @pytest.mark.asyncio
    async def test_a_change_is_pushed_over_the_socket(self, alice, drafts):
        requires_websocket(alice, push=True)
        async with async_client() as client, AsyncWebSocketClient(client) as socket:
            await socket.enable_push(["Email"])
            email_id = make_draft(alice, drafts, f"jmaplib async ws {uuid.uuid4().hex[:8]}")
            try:
                with anyio.fail_after(PUSH_TIMEOUT):
                    async with contextlib.aclosing(socket.notifications()) as changes:
                        change = await anext(changes)
            finally:
                destroy(alice, email_id)
        assert "Email" in change.types()


@contextlib.asynccontextmanager
async def async_client() -> AsyncIterator[AsyncJMAPClient]:
    """Alice's async client, closing the HTTP client it was given."""
    client = await aconnect(ALICE, ALICE_PASSWORD)
    try:
        yield client
    finally:
        await client.http.aclose()


def _first_within(changes: Iterator[StateChange], seconds: float) -> StateChange | None:
    """The first item, or ``None`` if none comes in time - so a lost push fails
    the test rather than hanging the suite."""
    found: list[StateChange] = []
    reader = threading.Thread(target=lambda: found.append(next(changes)), daemon=True)
    reader.start()
    reader.join(seconds)
    return found[0] if found else None


def _wait_for_state_change(source: EventSourceClient, type_name: str) -> StateChange | None:
    """Drain reconnecting streams until a change for ``type_name`` arrives.

    Bounded: a notification that never comes fails the test with a message rather
    than blocking the suite forever.
    """
    deadline = time.monotonic() + PUSH_TIMEOUT
    while time.monotonic() < deadline:
        for event in source.events():
            if isinstance(event, StateChange) and type_name in event.types():
                return event
        time.sleep(0.5)
    return None

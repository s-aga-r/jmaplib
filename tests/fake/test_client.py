"""The sync and async clients, end to end against the fake server.

The same behaviours are asserted for both shells. They are meant to be mirrors of
each other, so anything true of one and not the other is a bug in whichever
drifted.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth
from jmap.batch import NoAccountError, ReadOnlyAccountError
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.registry import Registry, UnsupportedMethodError
from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.client import JMAPClient
from jmap.core.errors import (
    AuthenticationError,
    CapabilityNotSupportedError,
    RequestError,
    TransportError,
)
from jmap.core.ids import Id
from jmap.core.retry import RetryPolicy
from jmap.testing import FakeJMAPServer, ServerQuirks

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"
MAIL_URN = "urn:ietf:params:jmap:mail"

MAIL = CapabilitySpec(
    urn=MAIL_URN,
    attr="mail",
    data_types=(DataTypeSpec(name="Email"), DataTypeSpec(name="Mailbox")),
    methods=(
        MethodSpec(name="Email/get", kind=MethodKind.GET),
        MethodSpec(name="Email/query", kind=MethodKind.QUERY),
        MethodSpec(name="Email/set", kind=MethodKind.SET, mutating=True),
    ),
)


def mail_registry() -> Registry:
    registry = Registry()
    registry.register(CORE)
    registry.register(MAIL)
    return registry


def mail_server(**kwargs: Any) -> FakeJMAPServer:
    kwargs.setdefault("capabilities", {CORE_URN: {"maxCallsInRequest": 16}, MAIL_URN: {}})
    kwargs.setdefault("primary_accounts", {CORE_URN: "a", MAIL_URN: "a"})
    return FakeJMAPServer(**kwargs)


def connect(server: FakeJMAPServer, **kwargs: Any) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**server.client_kwargs()),
        registry=mail_registry(),
        **kwargs,
    )


async def connect_async(server: FakeJMAPServer, **kwargs: Any) -> AsyncJMAPClient:
    return await AsyncJMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.AsyncClient(**server.client_kwargs()),
        registry=mail_registry(),
        **kwargs,
    )


class TestConnect:
    def test_follows_the_well_known_redirect(self):
        # Stalwart answers 307, Fastmail 302. What matters is landing on the
        # session document, not the number.
        with connect(mail_server()) as client:
            assert client.session.username == "alice@example.com"
            assert client.session.api_url.endswith("/jmap/")

    def test_resolves_capabilities_from_the_session(self):
        with connect(mail_server()) as client:
            assert client.capabilities.supports("Email/get")
            assert client.capabilities.limits.max_calls_in_request == 16

    def test_default_account_comes_from_primary_accounts(self):
        with connect(mail_server()) as client:
            assert client.default_account == Id("a")

    def test_unknown_capabilities_are_surfaced(self):
        server = mail_server()
        server.capabilities["urn:vendor:private"] = {}
        with connect(server) as client:
            assert "urn:vendor:private" in client.capabilities.unknown_urns

    def test_a_401_on_the_session_raises_with_the_challenges(self):
        server = mail_server()
        server.intercept = lambda _request: httpx.Response(
            401, headers=[("www-authenticate", 'Basic realm="x"')], json={}
        )
        with pytest.raises(AuthenticationError) as excinfo:
            connect(server)
        assert 'Basic realm="x"' in excinfo.value.challenges

    def test_repr_names_the_user(self):
        with connect(mail_server()) as client:
            assert "alice@example.com" in repr(client)


class TestCalling:
    def test_echo_round_trips(self):
        with connect(mail_server()) as client:
            assert client.echo(hello="world") == {"hello": "world"}

    def test_using_is_derived_from_the_calls_made(self):
        server = mail_server()
        server.respond("Email/get", {"list": [], "state": "s"})
        with connect(server) as client:
            client.call("Email/get", {"ids": ["m1"]})
        assert set(server.requests[-1]["using"]) == {CORE_URN, MAIL_URN}

    def test_account_id_is_injected(self):
        server = mail_server()
        server.respond("Email/get", {"list": [], "state": "s"})
        with connect(server) as client:
            client.call("Email/get", {"ids": ["m1"]})
        assert server.requests[-1]["methodCalls"][0][1]["accountId"] == "a"

    def test_core_echo_gets_no_account_id(self):
        # Core/echo is not account-scoped; sending one would be wrong.
        server = mail_server()
        with connect(server) as client:
            client.echo(hi=1)
        assert "accountId" not in last_call_arguments(server)

    def test_an_unsupported_method_never_reaches_the_wire(self):
        server = mail_server()
        with connect(server) as client, pytest.raises(UnsupportedMethodError):
            client.call("Calendar/get")
        assert not server.requests

    def test_a_method_error_surfaces_when_read(self):
        server = mail_server()
        with connect(server) as client:
            with client.batch() as batch:
                handle = batch.add("Email/get", {"ids": ["m1"]})
            with pytest.raises(Exception, match="unknownMethod"):
                _ = handle.result


def last_call_arguments(fake: FakeJMAPServer) -> dict[str, Any]:
    """The arguments of the last method call the server received."""
    arguments: dict[str, Any] = fake.requests[-1]["methodCalls"][0][1]
    return arguments


class TestBatching:
    def test_one_request_carries_every_queued_call(self):
        server = mail_server()
        server.respond("Email/query", {"ids": ["m1", "m2"], "queryState": "q"})
        server.respond("Email/get", {"list": [{"id": "m1"}], "state": "s"})

        with connect(server) as client:
            with client.batch() as batch:
                query = batch.add("Email/query", {})
                get = batch.add("Email/get", {"ids": query.ref_ids()})

            assert query.result.ids == ["m1", "m2"]
            assert [item.id for item in get.result.items] == ["m1"]

        assert len(server.requests) == 1
        assert len(server.requests[0]["methodCalls"]) == 2

    def test_back_references_are_resolved_by_the_server(self):
        server = mail_server()
        server.respond("Email/query", {"ids": ["m1", "m2"], "queryState": "q"})
        seen: dict[str, Any] = {}

        def capture(arguments: dict[str, Any], _server: FakeJMAPServer) -> dict[str, Any]:
            seen.update(arguments)
            return {"list": [], "state": "s"}

        server.handle("Email/get", capture)

        with connect(server) as client, client.batch() as batch:
            query = batch.add("Email/query", {})
            batch.add("Email/get", {"ids": query.ref_ids()})

        # The reference arrived as `#ids` and the fake resolved it with the
        # library's own pointer evaluator.
        assert seen["ids"] == ["m1", "m2"]

    def test_a_raising_block_sends_nothing(self):
        server = mail_server()
        # The raises block needs several statements: queue a call, then fail.
        with (  # noqa: PT012
            connect(server) as client,
            pytest.raises(RuntimeError),
            client.batch() as batch,
        ):
            batch.add("Email/get", {"ids": ["m1"]})
            raise RuntimeError("changed my mind")
        assert not server.requests

    def test_a_batch_is_split_to_respect_max_calls_in_request(self):
        server = mail_server(quirks=ServerQuirks(max_calls_in_request=2))
        server.capabilities[CORE_URN] = {"maxCallsInRequest": 2}
        server.respond("Email/get", {"list": [], "state": "s"})

        with connect(server) as client, client.batch() as batch:
            for _ in range(5):
                batch.add("Email/get", {"ids": ["m1"]})

        assert [len(r["methodCalls"]) for r in server.requests] == [2, 2, 1]


class TestLocalGates:
    def test_a_read_only_account_refuses_mutations(self):
        server = mail_server(
            accounts={"a": {"name": "alice", "isPersonal": True, "isReadOnly": True}}
        )
        with connect(server) as client, pytest.raises(ReadOnlyAccountError):
            client.call("Email/set", {"create": {}})
        assert not server.requests

    def test_a_missing_account_is_named_not_guessed(self):
        server = mail_server(primary_accounts={})
        with connect(server) as client, pytest.raises(NoAccountError, match="primaryAccounts"):
            client.call("Email/get", {"ids": ["m1"]})

    def test_forbidden_properties_are_rejected_locally(self):
        # PushSubscription's url and keys always earn `forbidden`.
        server = mail_server()
        with connect(server) as client, pytest.raises(Exception, match="never returned"):
            client.call("PushSubscription/get", {"properties": ["url"]})
        assert not server.requests

    def test_an_unadvertised_extra_using_raises_before_sending(self):
        server = mail_server()
        calendars = frozenset({"urn:ietf:params:jmap:calendars"})
        with (
            connect(server) as client,
            pytest.raises(CapabilityNotSupportedError),
            client.batch(extra_using=calendars) as batch,
        ):
            batch.add("Email/get", {"ids": ["m1"]})
        assert not server.requests


class TestFailures:
    def test_a_request_level_problem_becomes_a_typed_error(self):
        server = mail_server(
            quirks=ServerQuirks(
                scripted_failures=[
                    httpx.Response(
                        400,
                        json={"type": "urn:ietf:params:jmap:error:notRequest", "status": 400},
                        headers={"Content-Type": "application/problem+json"},
                    )
                ]
            )
        )
        with connect(server) as client, pytest.raises(RequestError) as excinfo:
            client.echo()
        assert excinfo.value.type.endswith("notRequest")

    def test_a_rate_limit_is_retried(self):
        server = mail_server(
            quirks=ServerQuirks(
                scripted_failures=[
                    httpx.Response(429, headers={"Retry-After": "0"}, json={"status": 429})
                ]
            )
        )
        with connect(server, retry_policy=RetryPolicy(max_attempts=3, initial_backoff=0)) as c:
            assert c.echo(hi=1) == {"hi": 1}

    def test_an_unguarded_mutation_is_not_retried_after_a_5xx(self):
        # This is the case that would otherwise create duplicate drafts.
        server = mail_server(
            quirks=ServerQuirks(scripted_failures=[httpx.Response(500, json={"status": 500})])
        )
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with connect(server, retry_policy=policy) as client, pytest.raises(RequestError):
            client.call("Email/set", {"create": {"d1": {}}})

    def test_session_state_change_is_flagged_not_acted_on(self):
        server = mail_server()
        with connect(server) as client:
            assert not client.session_stale
            server.session_state = "session-1"
            client.echo()
            assert client.session_stale


class TestAsyncMirror:
    """Whatever the sync shell does, the async one must do identically."""

    @pytest.mark.asyncio
    async def test_connect_and_echo(self):
        async with await connect_async(mail_server()) as client:
            assert client.session.username == "alice@example.com"
            assert await client.echo(hello="world") == {"hello": "world"}

    @pytest.mark.asyncio
    async def test_batching_with_back_references(self):
        server = mail_server()
        server.respond("Email/query", {"ids": ["m1"], "queryState": "q"})
        server.respond("Email/get", {"list": [{"id": "m1"}], "state": "s"})

        async with await connect_async(server) as client:
            async with client.batch() as batch:
                query = batch.add("Email/query", {})
                get = batch.add("Email/get", {"ids": query.ref_ids()})

            assert query.result.ids == ["m1"]
            assert [item.id for item in get.result.items] == ["m1"]
        assert len(server.requests) == 1

    @pytest.mark.asyncio
    async def test_local_gates_apply_equally(self):
        server = mail_server(
            accounts={"a": {"name": "alice", "isPersonal": True, "isReadOnly": True}}
        )
        async with await connect_async(server) as client:
            with pytest.raises(ReadOnlyAccountError):
                await client.call("Email/set", {"create": {}})
        assert not server.requests

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self):
        server = mail_server(
            quirks=ServerQuirks(
                scripted_failures=[
                    httpx.Response(429, headers={"Retry-After": "0"}, json={"status": 429})
                ]
            )
        )
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        async with await connect_async(server, retry_policy=policy) as client:
            assert await client.echo(hi=1) == {"hi": 1}

    @pytest.mark.asyncio
    async def test_a_raising_block_sends_nothing(self):
        server = mail_server()
        async with await connect_async(server) as client:
            with pytest.raises(RuntimeError):  # noqa: PT012 - queue, then fail
                async with client.batch() as batch:
                    batch.add("Email/get", {"ids": ["m1"]})
                    raise RuntimeError("changed my mind")
        assert not server.requests

    @pytest.mark.asyncio
    async def test_repr(self):
        async with await connect_async(mail_server()) as client:
            assert "alice@example.com" in repr(client)


class TestAddressDiscovery:
    """Connecting from an email address rather than a URL (RFC 8620 §2.2)."""

    def test_the_well_known_url_is_tried(self):
        fake = FakeJMAPServer(base_url="https://example.com")
        with JMAPClient.discover(
            "alice@example.com",
            auth=BasicAuth("alice", "pw"),
            use_srv=False,
            http=httpx.Client(**fake.client_kwargs()),
        ) as client:
            assert client.session.username == "alice@example.com"

    def test_a_bare_domain_works_too(self):
        # What a user types when they know their provider but not their address.
        fake = FakeJMAPServer(base_url="https://example.com")
        with JMAPClient.discover(
            "example.com",
            auth=BasicAuth("alice", "pw"),
            use_srv=False,
            http=httpx.Client(**fake.client_kwargs()),
        ) as client:
            assert client.session.api_url

    def test_the_last_failure_is_what_surfaces(self):
        # The well-known URL is tried last, so its error is the one a user can
        # most easily go and check by hand.
        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nothing listening")

        with pytest.raises(TransportError, match="nothing listening"):
            JMAPClient.discover(
                "alice@example.com",
                auth=BasicAuth("alice", "pw"),
                use_srv=False,
                http=httpx.Client(transport=httpx.MockTransport(refuse)),
            )

    def test_a_server_error_is_also_a_reason_to_move_on(self):
        def broken(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, content=b"bad gateway")

        with pytest.raises(RequestError):
            JMAPClient.discover(
                "alice@example.com",
                auth=BasicAuth("alice", "pw"),
                use_srv=False,
                http=httpx.Client(transport=httpx.MockTransport(broken)),
            )

"""Edge paths of the batch layer, both shells, and the fake server itself.

Split from ``test_client.py`` to keep the happy-path story there readable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth
from jmap.batch import Batch, all_mutations_guarded, guarded_call_ids, is_mutating
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.registry import ActiveCapabilities, Registry
from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.client import JMAPClient
from jmap.core.errors import (
    AuthenticationError,
    BatchTooLargeError,
    CapabilityFieldError,
    MethodError,
    RequestError,
    TransportError,
)
from jmap.core.ids import CreationRef, Id
from jmap.core.retry import RetryPolicy
from jmap.core.session import InsecureEndpointError, Session
from jmap.testing import FakeJMAPServer, ServerQuirks

MAIL_URN = "urn:ietf:params:jmap:mail"
WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"

#: A stand-in for one API request, used to drive transport failures.
RequestHandler = Callable[[httpx.Request], httpx.Response]

MAIL = CapabilitySpec(
    urn=MAIL_URN,
    attr="mail",
    data_types=(DataTypeSpec(name="Email"),),
    methods=(
        MethodSpec(name="Email/get", kind=MethodKind.GET),
        MethodSpec(name="Email/query", kind=MethodKind.QUERY),
        MethodSpec(name="Email/set", kind=MethodKind.SET, mutating=True),
    ),
)


def registry() -> Registry:
    reg = Registry()
    reg.register(CORE)
    reg.register(MAIL)
    return reg


#: A draft-tracking capability, for the experimental opt-in.
DRAFT_URN = "urn:example:draft"
DRAFT = CapabilitySpec(
    urn=DRAFT_URN,
    attr="draft",
    experimental=True,
    data_types=(DataTypeSpec(name="Draft"),),
    methods=(MethodSpec(name="Draft/get", kind=MethodKind.GET),),
)


def draft_registry() -> Registry:
    reg = registry()
    reg.register(DRAFT)
    return reg


def capabilities(**session_overrides: Any) -> ActiveCapabilities:
    document: dict[str, Any] = {
        "capabilities": {
            CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInSet": 500},
            MAIL_URN: {},
        },
        "accounts": {"a": {"name": "alice", "isPersonal": True, "isReadOnly": False}},
        "primaryAccounts": {CORE_URN: "a", MAIL_URN: "a"},
        "state": "s0",
    }
    document.update(session_overrides)
    return registry().resolve(Session.from_wire(document), Id("a"))


def server(**kwargs: Any) -> FakeJMAPServer:
    kwargs.setdefault("capabilities", {CORE_URN: {"maxCallsInRequest": 16}, MAIL_URN: {}})
    kwargs.setdefault("primary_accounts", {CORE_URN: "a", MAIL_URN: "a"})
    return FakeJMAPServer(**kwargs)


def connect(fake: FakeJMAPServer, **kwargs: Any) -> JMAPClient:
    kwargs.setdefault("http", httpx.Client(**fake.client_kwargs()))
    kwargs.setdefault("registry", registry())
    return JMAPClient.connect(WELL_KNOWN, auth=BasicAuth("alice@example.com", "pw"), **kwargs)


class TestBatchMechanics:
    def test_empty_batch_plans_nothing(self):
        assert Batch(capabilities()).plan() == []

    def test_len_and_repr(self):
        batch = Batch(capabilities())
        batch.add("Email/get", {"ids": ["m1"]})
        assert len(batch) == 1
        assert "Email/get" in repr(batch)

    def test_explicit_account_id_wins(self):
        batch = Batch(capabilities())
        handle = batch.add("Email/get", {"ids": ["m1"]}, account_id=Id("other"))
        assert handle.call.arguments["accountId"] == "other"

    def test_account_id_already_in_arguments_is_left_alone(self):
        batch = Batch(capabilities())
        handle = batch.add("Email/get", {"ids": ["m1"], "accountId": "explicit"})
        assert handle.call.arguments["accountId"] == "explicit"

    def test_account_id_can_be_attached_to_a_non_scoped_method(self):
        batch = Batch(capabilities())
        handle = batch.add("Core/echo", {}, account_id=Id("a"))
        assert handle.call.arguments["accountId"] == "a"

    def test_non_account_scoped_methods_get_none_by_default(self):
        batch = Batch(capabilities())
        assert "accountId" not in batch.add("Core/echo", {}).call.arguments

    def test_pending_reports_unresolved_handles(self):
        batch = Batch(capabilities())
        batch.add("Email/get", {"ids": ["m1"]})
        assert len(batch.pending()) == 1

    def test_created_ids_start_from_the_seed(self):
        batch = Batch(capabilities(), created_ids={"draft": Id("M1")})
        assert batch.created_ids == {"draft": Id("M1")}

    def test_properties_on_an_unmodelled_type_are_not_gated(self):
        batch = Batch(capabilities())
        handle = batch.add("Core/echo", {"properties": ["anything"]})
        assert handle.call.arguments["properties"] == ["anything"]

    def test_allowed_properties_pass_the_check(self):
        # A modelled type with nothing on its never-request list.
        batch = Batch(capabilities())
        handle = batch.add("Email/get", {"properties": ["subject", "from"]})
        assert handle.call.arguments["properties"] == ["subject", "from"]

    def test_empty_properties_skip_the_check(self):
        batch = Batch(capabilities())
        assert batch.add("Email/get", {"properties": []}).call.arguments["properties"] == []

    def test_oversized_set_raises_rather_than_splitting(self):
        # Splitting would break the single ifInState that makes /set atomic.
        active = capabilities(
            capabilities={CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInSet": 2}, MAIL_URN: {}}
        )
        batch = Batch(active)
        batch.add("Email/set", {"create": {"a": {}, "b": {}, "c": {}}})
        with pytest.raises(CapabilityFieldError, match="maxObjectsInSet"):
            batch.plan()

    def test_set_size_counts_create_update_and_destroy_together(self):
        active = capabilities(
            capabilities={CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInSet": 2}, MAIL_URN: {}}
        )
        batch = Batch(active)
        batch.add("Email/set", {"create": {"a": {}}, "update": {"b": {}}, "destroy": ["c"]})
        with pytest.raises(CapabilityFieldError):
            batch.plan()

    def test_a_set_within_the_limit_plans_fine(self):
        batch = Batch(capabilities())
        batch.add("Email/set", {"create": {"a": {}}})
        assert len(batch.plan()) == 1

    def test_an_unsplittable_reference_chain_is_named(self):
        active = capabilities(capabilities={CORE_URN: {"maxCallsInRequest": 1}, MAIL_URN: {}})
        batch = Batch(active)
        query = batch.add("Email/query", {})
        batch.add("Email/get", {"ids": query.ref_ids()})
        with pytest.raises(BatchTooLargeError):
            batch.plan()


class TestMutationHelpers:
    def test_read_only_batch_is_not_mutating(self):
        active = capabilities()
        batch = Batch(active)
        batch.add("Email/get", {"ids": ["m1"]})
        assert not is_mutating(batch, active)
        assert all_mutations_guarded(batch, active)
        assert guarded_call_ids(batch, active) == []

    def test_an_unguarded_set_is_mutating_and_unguarded(self):
        active = capabilities()
        batch = Batch(active)
        batch.add("Email/set", {"create": {"d": {}}})
        assert is_mutating(batch, active)
        assert not all_mutations_guarded(batch, active)

    def test_a_guarded_set_is_recognised(self):
        active = capabilities()
        batch = Batch(active)
        batch.add("Email/set", {"create": {"d": {}}, "ifInState": "s1"})
        assert is_mutating(batch, active)
        assert all_mutations_guarded(batch, active)
        assert guarded_call_ids(batch, active) == ["c1"]

    def test_a_back_referenced_guard_does_not_count(self):
        # A reference resolves afresh on every attempt, against whatever state
        # the server is in *then* - so it can never fail a repeat.
        active = capabilities()
        batch = Batch(active)
        got = batch.add("Email/get", {"ids": []})
        batch.add("Email/set", {"create": {"d": {}}, "ifInState": got.ref("/state")})
        assert not all_mutations_guarded(batch, active)
        assert guarded_call_ids(batch, active) == []


class TestClientLifecycle:
    def test_a_supplied_http_client_is_not_closed(self):
        fake = server()
        http = httpx.Client(**fake.client_kwargs())
        client = connect(fake, http=http)
        client.close()
        assert not http.is_closed
        http.close()

    def test_refresh_session_re_resolves_capabilities(self):
        fake = server()
        with connect(fake) as client:
            assert client.capabilities.supports("Email/get")
            fake.capabilities.pop(MAIL_URN)
            fake.session_state = "session-1"
            client.refresh_session()
            assert not client.capabilities.supports("Email/get")
            assert not client.session_stale

    def test_refresh_session_keeps_the_experimental_opt_in(self):
        # Re-resolving without it quietly moved every draft-tracking capability
        # into unknown_urns, so `client.calendars` vanished after a refresh.
        fake = server(capabilities={CORE_URN: {}, MAIL_URN: {}, DRAFT_URN: {}})
        with connect(fake, registry=draft_registry(), experimental=True) as client:
            assert client.capabilities.supports("Draft/get")
            client.refresh_session()
            assert client.capabilities.supports("Draft/get")

    def test_a_guarded_mutation_is_retried_after_a_5xx(self):
        # ifInState makes the retry safe: a second landing fails stateMismatch.
        fake = server(
            quirks=ServerQuirks(scripted_failures=[httpx.Response(500, json={"status": 500})])
        )
        fake.respond("Email/set", {"created": {}, "newState": "s2"})
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with connect(fake, retry_policy=policy) as client:
            result = client.call("Email/set", {"create": {"d": {}}, "ifInState": "s1"})
        assert result.new_state == "s2"

    def test_a_connection_failure_becomes_a_transport_error(self):
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        http = httpx.Client(transport=httpx.MockTransport(explode))
        with pytest.raises(TransportError, match="refused"):
            JMAPClient.connect(WELL_KNOWN, auth=BasicAuth("u", "p"), http=http)


class TestTransportFailureClassification:
    """Which transport failures may re-send a request.

    The line that matters: a ConnectError provably never sent the request, while
    a connection that died mid-exchange (reset, server hung up unanswered) is
    the same silence as a timeout - the server may have done the work. Getting
    that wrong re-sends unguarded mutations, which for EmailSubmission/set is
    mail sent twice.
    """

    def _flaky_http(
        self, fake: FakeJMAPServer, exc: type[Exception], failures: int
    ) -> tuple[httpx.Client, dict[str, int]]:
        """A transport that raises ``exc`` for the first ``failures`` API POSTs."""
        seen = {"posts": 0}

        def route(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                seen["posts"] += 1
                if seen["posts"] <= failures:
                    raise exc("mid-exchange loss")
            return fake.route(request)

        kwargs = {**fake.client_kwargs(), "transport": httpx.MockTransport(route)}
        return httpx.Client(**kwargs), seen

    def test_an_interrupted_unguarded_mutation_is_not_retried(self):
        # The server may have applied the set before the connection died;
        # re-sending it without ifInState could duplicate the work.
        fake = server()
        fake.respond("Email/set", {"created": {}, "newState": "s2"})
        http, seen = self._flaky_http(fake, httpx.RemoteProtocolError, failures=99)
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with (
            connect(fake, http=http, retry_policy=policy) as client,
            pytest.raises(TransportError, match="mid-exchange"),
        ):
            client.call("Email/set", {"create": {"d": {}}})
        assert seen["posts"] == 1

    def test_an_interrupted_read_is_retried(self):
        # A read that cannot have changed anything is always safe to re-send.
        fake = server()
        fake.respond("Email/get", {"list": [], "state": "s1"})
        http, seen = self._flaky_http(fake, httpx.RemoteProtocolError, failures=1)
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with connect(fake, http=http, retry_policy=policy) as client:
            result = client.call("Email/get", {"ids": []})
        assert result.state == "s1"
        assert seen["posts"] == 2

    def test_a_connect_failure_still_retries_an_unguarded_mutation(self):
        # ConnectError proves the request never went out, so nothing can have
        # run - the one transport failure where an unguarded retry is safe.
        fake = server()
        fake.respond("Email/set", {"created": {}, "newState": "s2"})
        http, seen = self._flaky_http(fake, httpx.ConnectError, failures=1)
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with connect(fake, http=http, retry_policy=policy) as client:
            result = client.call("Email/set", {"create": {"d": {}}})
        assert result.new_state == "s2"
        assert seen["posts"] == 2

    def test_a_back_referenced_guard_does_not_make_a_retry_safe(self):
        # The guard points at a /get in the same request, and a retry re-runs
        # that /get after the first attempt already landed - so the guard matches
        # the new state, and the write happens twice.
        fake = server()
        store = {"state": 0, "created": 0}

        def email_get(_arguments: dict[str, Any], _srv: FakeJMAPServer) -> dict[str, Any]:
            return {"list": [], "state": str(store["state"])}

        def email_set(arguments: dict[str, Any], _srv: FakeJMAPServer) -> dict[str, Any]:
            if arguments.get("ifInState") != str(store["state"]):
                return {"notCreated": {"d": {"type": "stateMismatch"}}}
            store["created"] += 1
            store["state"] += 1
            return {"created": {"d": {"id": f"M{store['created']}"}}}

        fake.handle("Email/get", email_get)
        fake.handle("Email/set", email_set)
        posts = {"n": 0}

        def route(request: httpx.Request) -> httpx.Response:
            response = fake.route(request)  # the server applies it...
            if request.method == "POST":
                posts["n"] += 1
                if posts["n"] == 1:
                    raise httpx.ReadTimeout("...and the answer never arrives")
            return response

        http = httpx.Client(**{**fake.client_kwargs(), "transport": httpx.MockTransport(route)})
        policy = RetryPolicy(max_attempts=3, initial_backoff=0)
        with connect(fake, http=http, retry_policy=policy) as client:
            batch = Batch(client.capabilities, default_account=client.default_account)
            got = batch.add("Email/get", {"ids": []})
            batch.add("Email/set", {"create": {"d": {}}, "ifInState": got.ref("/state")})
            with pytest.raises(TransportError):
                client.execute(batch)
        assert posts["n"] == 1
        assert store["created"] == 1

    def test_connect_closes_a_client_it_created_when_the_session_fails(self):
        # Only reachable when connect() owns the client, so it cannot leak.
        with pytest.raises((TransportError, httpx.HTTPError)):
            JMAPClient.connect("http://127.0.0.1:1/.well-known/jmap", auth=BasicAuth("u", "p"))


class TestFakeServer:
    def test_unknown_paths_are_404(self):
        fake = server()
        with httpx.Client(**fake.client_kwargs()) as http:
            assert http.get("https://jmap.example.com/nope").status_code == 404

    def test_a_non_object_request_body_is_rejected(self):
        fake = server()
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post("https://jmap.example.com/jmap", content=b'"not an object"')
        assert response.status_code == 400

    def test_an_unadvertised_using_urn_is_unknown_capability_by_default(self):
        fake = server()
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post(
                "https://jmap.example.com/jmap",
                json={"using": ["urn:vendor:x"], "methodCalls": []},
            )
        assert response.json()["type"].endswith("unknownCapability")

    def test_the_stalwart_quirk_turns_it_into_not_request(self):
        # One unknown URN destroying the whole batch is why the client refuses
        # to send an unadvertised capability at all.
        fake = server(quirks=ServerQuirks(unknown_using_is_not_request=True))
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post(
                "https://jmap.example.com/jmap",
                json={"using": ["urn:vendor:x"], "methodCalls": []},
            )
        assert response.json()["type"].endswith("notRequest")

    def test_account_level_capabilities_count_as_advertised(self):
        fake = server(
            accounts={"a": {"name": "alice", "accountCapabilities": {"urn:vendor:x": {}}}}
        )
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post(
                "https://jmap.example.com/jmap",
                json={"using": ["urn:vendor:x"], "methodCalls": []},
            )
        assert response.status_code == 200

    def test_a_dangling_back_reference_becomes_invalid_result_reference(self):
        fake = server()
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post(
                "https://jmap.example.com/jmap",
                json={
                    "using": [CORE_URN],
                    "methodCalls": [
                        [
                            "Core/echo",
                            {"#x": {"resultOf": "nope", "name": "Core/echo", "path": "/x"}},
                            "c0",
                        ]
                    ],
                },
            )
        assert response.json()["methodResponses"][0][1]["type"] == "invalidResultReference"

    def test_created_ids_are_returned_when_present(self):
        fake = server()
        fake.created_ids["draft"] = "M1"
        with connect(fake) as client:
            client.echo()
        assert fake.requests

    def test_max_calls_quirk_yields_a_request_level_limit(self):
        fake = server(quirks=ServerQuirks(max_calls_in_request=1))
        with httpx.Client(**fake.client_kwargs()) as http:
            response = http.post(
                "https://jmap.example.com/jmap",
                json={
                    "using": [CORE_URN],
                    "methodCalls": [["Core/echo", {}, "c0"], ["Core/echo", {}, "c1"]],
                },
            )
        assert response.json()["limit"] == "maxCallsInRequest"


class TestAsyncEdges:
    @pytest.mark.asyncio
    async def test_a_supplied_client_is_not_closed(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        client = await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        )
        await client.aclose()
        assert not http.is_closed
        await http.aclose()

    @pytest.mark.asyncio
    async def test_refresh_session(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            fake.capabilities.pop(MAIL_URN)
            await client.refresh_session()
            assert not client.capabilities.supports("Email/get")

    @pytest.mark.asyncio
    async def test_refresh_session_keeps_the_experimental_opt_in(self):
        fake = server(capabilities={CORE_URN: {}, MAIL_URN: {}, DRAFT_URN: {}})
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN,
            auth=BasicAuth("u", "p"),
            http=http,
            registry=draft_registry(),
            experimental=True,
        ) as client:
            await client.refresh_session()
            assert client.capabilities.supports("Draft/get")

    @pytest.mark.asyncio
    async def test_a_connection_failure_becomes_a_transport_error(self):
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        http = httpx.AsyncClient(transport=httpx.MockTransport(explode))
        with pytest.raises(TransportError, match="refused"):
            await AsyncJMAPClient.connect(WELL_KNOWN, auth=BasicAuth("u", "p"), http=http)
        await http.aclose()

    @pytest.mark.asyncio
    async def test_a_request_problem_raises(self):
        fake = server(
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
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            with pytest.raises(Exception, match="notRequest"):
                await client.echo()

    @pytest.mark.asyncio
    async def test_session_state_change_is_flagged(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            fake.session_state = "session-9"
            await client.echo()
            assert client.session_stale


class TestTransportFailures:
    """The `_post` retry loop, driven through real httpx exceptions."""

    def _client(self, handler: RequestHandler, **kwargs: Any) -> JMAPClient:
        fake = server()
        # Only the API POST is taken over; the session fetch still goes to the
        # real router, since "/.well-known/jmap" also ends with "/jmap".
        fake.intercept = lambda request: handler(request) if request.method == "POST" else None
        http = httpx.Client(**fake.client_kwargs())
        kwargs.setdefault("retry_policy", RetryPolicy(max_attempts=2, initial_backoff=0))
        return connect(fake, http=http, **kwargs)

    def test_a_timeout_on_a_read_only_call_is_retried(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("too slow")
            return httpx.Response(200, json={"methodResponses": [["Core/echo", {"ok": 1}, "c1"]]})

        with self._client(handler) as client:
            assert client.echo() == {"ok": 1}
        assert calls["n"] == 2

    def test_a_timeout_on_an_unguarded_mutation_is_not_retried(self):
        # The request went out; the server may have applied it.
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow")

        with self._client(handler) as client, pytest.raises(TransportError):
            client.call("Email/set", {"create": {"d": {}}})
        assert calls["n"] == 1

    def test_a_connect_error_is_retried_then_surfaced(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        with self._client(handler) as client, pytest.raises(TransportError, match="refused"):
            client.echo()
        assert calls["n"] == 2

    def test_a_401_from_the_api_raises_with_its_challenges(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers=[("www-authenticate", 'Bearer realm="x"')], json={"status": 401}
            )

        with self._client(handler) as client, pytest.raises(AuthenticationError) as excinfo:
            client.echo()
        assert 'Bearer realm="x"' in excinfo.value.challenges

    def test_a_failing_session_fetch_raises_the_problem(self):
        def route(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"status": 500})

        http = httpx.Client(transport=httpx.MockTransport(route), follow_redirects=True)
        with pytest.raises(RequestError):
            JMAPClient.connect(WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry())

    def test_a_retry_after_within_the_policy_is_waited_out(self, monkeypatch):
        naps: list[float] = []
        monkeypatch.setattr("jmap.client.time.sleep", naps.append)
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, headers={"Retry-After": "7"})
            return httpx.Response(200, json={"methodResponses": [["Core/echo", {"ok": 1}, "c1"]]})

        with self._client(handler) as client:
            assert client.echo() == {"ok": 1}
        assert naps == [7.0]

    def test_a_retry_after_past_the_policy_fails_at_once(self, monkeypatch):
        # One 503 asking for a year used to park the call for a year. Now the
        # error comes straight back, carrying the hint so the caller can
        # reschedule instead of blocking.
        naps: list[float] = []
        monkeypatch.setattr("jmap.client.time.sleep", naps.append)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, headers={"Retry-After": "31536000"})

        with self._client(handler) as client, pytest.raises(RequestError) as excinfo:
            client.echo()
        assert naps == []
        assert excinfo.value.retry_after == 31536000.0


class TestAccountFallback:
    def test_primary_accounts_supplies_the_account_when_no_default_is_set(self):
        # The batch falls back to primaryAccounts for the owning capability.
        unbound = registry().resolve(
            Session.from_wire(
                {
                    "capabilities": {CORE_URN: {}, MAIL_URN: {}},
                    "accounts": {"a": {"name": "alice"}},
                    "primaryAccounts": {MAIL_URN: "a"},
                }
            )
        )
        batch = Batch(unbound)
        handle = batch.add("Email/get", {"ids": ["m1"]})
        assert handle.call.arguments["accountId"] == "a"


class TestAsyncTransportFailures:
    """The async `_post` loop and session fetch, mirroring the sync tests."""

    def _http(self, handler: RequestHandler) -> tuple[httpx.AsyncClient, FakeJMAPServer]:
        fake = server()
        fake.intercept = lambda request: handler(request) if request.method == "POST" else None
        return httpx.AsyncClient(**fake.client_kwargs()), fake

    async def _connect(self, handler: RequestHandler, **kwargs: Any) -> AsyncJMAPClient:
        http, _fake = self._http(handler)
        kwargs.setdefault("retry_policy", RetryPolicy(max_attempts=2, initial_backoff=0))
        return await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry(), **kwargs
        )

    @pytest.mark.asyncio
    async def test_a_timeout_on_a_read_only_call_is_retried(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("too slow")
            return httpx.Response(200, json={"methodResponses": [["Core/echo", {"ok": 1}, "c1"]]})

        async with await self._connect(handler) as client:
            assert await client.echo() == {"ok": 1}
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_timeout_on_an_unguarded_mutation_is_not_retried(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow")

        async with await self._connect(handler) as client:
            with pytest.raises(TransportError):
                await client.call("Email/set", {"create": {"d": {}}})
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_an_interrupted_unguarded_mutation_is_not_retried(self):
        # A connection that died after the request went out is the same silence
        # as a timeout: the server may have applied the set.
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.RemoteProtocolError("server hung up mid-exchange")

        async with await self._connect(handler) as client:
            with pytest.raises(TransportError, match="hung up"):
                await client.call("Email/set", {"create": {"d": {}}})
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_a_connect_error_is_retried_then_surfaced(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        async with await self._connect(handler) as client:
            with pytest.raises(TransportError, match="refused"):
                await client.echo()
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_retry_after_past_the_policy_fails_at_once(self, monkeypatch):
        naps: list[float] = []

        async def nap(seconds: float) -> None:
            naps.append(seconds)

        monkeypatch.setattr("jmap.aio.anyio.sleep", nap)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "86400"})

        async with await self._connect(handler) as client:
            with pytest.raises(RequestError) as excinfo:
                await client.echo()
        assert naps == []
        assert excinfo.value.retry_after == 86400.0

    @pytest.mark.asyncio
    async def test_a_401_from_the_api_raises_with_its_challenges(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers=[("www-authenticate", 'Bearer realm="x"')], json={"status": 401}
            )

        async with await self._connect(handler) as client:
            with pytest.raises(AuthenticationError) as excinfo:
                await client.echo()
        assert 'Bearer realm="x"' in excinfo.value.challenges

    @pytest.mark.asyncio
    async def test_a_401_on_the_session_raises(self):
        def route(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, headers=[("www-authenticate", 'Basic realm="x"')], json={})

        http = httpx.AsyncClient(transport=httpx.MockTransport(route), follow_redirects=True)
        with pytest.raises(AuthenticationError):
            await AsyncJMAPClient.connect(WELL_KNOWN, auth=BasicAuth("u", "p"), http=http)
        await http.aclose()

    @pytest.mark.asyncio
    async def test_a_failing_session_fetch_raises_the_problem(self):
        def route(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"status": 500})

        http = httpx.AsyncClient(transport=httpx.MockTransport(route), follow_redirects=True)
        with pytest.raises(RequestError):
            await AsyncJMAPClient.connect(WELL_KNOWN, auth=BasicAuth("u", "p"), http=http)
        await http.aclose()


class TestOwnedHttpClients:
    """A client that created its own transport must close it."""

    def test_sync(self):
        fake = server()
        http = httpx.Client(**fake.client_kwargs())
        client = connect(fake, http=http)
        # Rebuild it as if connect() had created the transport itself.
        owned = JMAPClient(
            client.session,
            client.capabilities,
            http,
            registry=registry(),
            owns_http=True,
        )
        owned.close()
        assert http.is_closed

    @pytest.mark.asyncio
    async def test_async(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        client = await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        )
        owned = AsyncJMAPClient(
            client.session,
            client.capabilities,
            http,
            registry=registry(),
            owns_http=True,
        )
        await owned.aclose()
        assert http.is_closed

    @pytest.mark.asyncio
    async def test_a_failed_connect_closes_the_client_it_created(self):
        # Only reachable when connect() owns the transport, so it cannot leak.
        with pytest.raises((TransportError, httpx.HTTPError)):
            await AsyncJMAPClient.connect(
                "http://127.0.0.1:1/.well-known/jmap", auth=BasicAuth("u", "p")
            )


class TestSessionRedirectDowngrade:
    """An https session fetch redirected onto http is refused, in both shells.

    The 1.1.0 endpoint check judges the document by the channel it arrived
    over, so without this the redirect itself was the way around it: the
    cleartext leg served an http ``apiUrl``, which an "http session" may name,
    and the next call carried the credentials there in the clear.
    """

    CLEARTEXT = "http://jmap.example.com"

    def _downgrading(self) -> FakeJMAPServer:
        fake = server()

        def downgrade(request: httpx.Request) -> httpx.Response | None:
            if request.url.scheme == "https" and request.url.path == "/.well-known/jmap":
                return httpx.Response(307, headers={"Location": f"{self.CLEARTEXT}/jmap/session"})
            if request.url.scheme == "http" and request.url.path == "/jmap/session":
                document = {**fake.session_document, "apiUrl": f"{self.CLEARTEXT}/jmap/"}
                return httpx.Response(200, json=document)
            return None

        fake.intercept = downgrade
        return fake

    def test_the_sync_client_refuses_it(self):
        with pytest.raises(InsecureEndpointError, match="redirected"):
            connect(self._downgrading())

    @pytest.mark.asyncio
    async def test_the_async_client_refuses_it(self):
        fake = self._downgrading()
        http = httpx.AsyncClient(**fake.client_kwargs())
        with pytest.raises(InsecureEndpointError, match="redirected"):
            await AsyncJMAPClient.connect(
                WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
            )
        await http.aclose()


class TestSplitBatchesCarryCreationIds:
    """RFC 8620 §3.3: ``createdIds`` is how ``#creationId`` crosses requests.

    A batch split under ``maxCallsInRequest`` used to plan every request up
    front with only the caller's seed, so the ids the server assigned while
    answering one request never reached the next - and a creation reference
    there resolved to nothing.
    """

    def _fake(self) -> FakeJMAPServer:
        fake = server(capabilities={CORE_URN: {"maxCallsInRequest": 1}, MAIL_URN: {}})

        def email_set(arguments: dict[str, Any], srv: FakeJMAPServer) -> dict[str, Any]:
            created = {}
            for creation_id in arguments.get("create") or {}:
                srv.created_ids[creation_id] = f"M-{creation_id}"
                created[creation_id] = {"id": f"M-{creation_id}"}
            return {"created": created, "newState": "s2"}

        fake.handle("Email/set", email_set)
        return fake

    def test_a_later_request_carries_the_ids_an_earlier_one_created(self):
        fake = self._fake()
        with connect(fake) as client, client.batch() as batch:
            batch.add("Email/set", {"create": {"draft": {}}})
            batch.add("Email/set", {"create": {"reply": {"inReplyTo": CreationRef("draft")}}})
        assert len(fake.requests) == 2
        assert "createdIds" not in fake.requests[0]
        assert fake.requests[1]["createdIds"] == {"draft": "M-draft"}

    @pytest.mark.asyncio
    async def test_the_async_client_carries_them_too(self):
        fake = self._fake()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with (
            await AsyncJMAPClient.connect(
                WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
            ) as client,
            client.batch() as batch,
        ):
            batch.add("Email/set", {"create": {"draft": {}}})
            batch.add("Email/set", {"create": {"reply": {"inReplyTo": CreationRef("draft")}}})
        assert fake.requests[1]["createdIds"] == {"draft": "M-draft"}


class TestConformantNullResultMaps:
    def test_a_set_answered_with_nulls_is_a_success_not_a_malformed_result(self):
        # RFC 8620 §5.3 answers an update-only /set with null for every empty
        # category. The write has happened; reporting it as malformedResult
        # invites the caller to make it again.
        fake = server()
        fake.respond(
            "Email/set",
            {
                "accountId": "a",
                "oldState": "s1",
                "newState": "s2",
                "created": None,
                "updated": {"m1": None},
                "destroyed": None,
                "notCreated": None,
                "notUpdated": None,
                "notDestroyed": None,
            },
        )
        with connect(fake) as client:
            result = client.call("Email/set", {"update": {"m1": {"keywords/$seen": True}}})
        assert result.new_state == "s2"
        assert result.updated == {"m1": None}
        assert not result.has_errors


def answering(*, skip: set[int]) -> Callable[[httpx.Request], httpx.Response | None]:
    """An API that echoes every call back, except the first call of each request
    whose number (counting from 1) is in ``skip``."""
    seen = {"requests": 0}

    def intercept(request: httpx.Request) -> httpx.Response | None:
        if request.method != "POST":
            return None
        seen["requests"] += 1
        calls = json.loads(request.content)["methodCalls"]
        if seen["requests"] in skip:
            calls = calls[1:]
        answers = [["Core/echo", arguments, call_id] for _name, arguments, call_id in calls]
        return httpx.Response(200, json={"methodResponses": answers, "sessionState": "s0"})

    return intercept


class TestUnansweredCalls:
    """RFC 8620 §3.4 has the server answer every method call. Some do not."""

    def test_a_call_left_unanswered_fails_as_a_method_error(self):
        # It read as a RuntimeError saying the batch had not run yet - after the
        # batch had run and its context had exited.
        fake = server()
        with connect(fake) as client:
            fake.intercept = answering(skip={1})
            with client.batch() as batch:
                dropped = batch.add("Core/echo", {"n": 1})
                answered = batch.add("Core/echo", {"n": 2})
        assert answered.result == {"n": 2}
        with pytest.raises(MethodError) as excinfo:
            _ = dropped.result
        assert excinfo.value.type == "missingResponse"
        assert excinfo.value.method_call_id == dropped.call_id

    def test_only_the_request_that_went_unanswered_is_blamed(self):
        # Split one call per request: the first comes back empty, the second
        # answered - and the second's handle is not failed for the first's gap.
        fake = server(capabilities={CORE_URN: {"maxCallsInRequest": 1}, MAIL_URN: {}})
        with connect(fake) as client:
            fake.intercept = answering(skip={1})
            with client.batch() as batch:
                dropped = batch.add("Core/echo", {"n": 1})
                answered = batch.add("Core/echo", {"n": 2})
        assert dropped.error is not None
        assert answered.result == {"n": 2}

    @pytest.mark.asyncio
    async def test_the_async_client_fails_it_the_same_way(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            fake.intercept = answering(skip={1})
            async with client.batch() as batch:
                dropped = batch.add("Core/echo", {"n": 1})
        with pytest.raises(MethodError, match="missingResponse"):
            _ = dropped.result


class TestSetSizeGate:
    def test_a_result_ref_destroy_does_not_crash_planning(self):
        # The canonical query-then-destroy pattern: `destroy` is a ResultRef
        # naming ids that do not exist yet, so it cannot be counted - and
        # len() on it used to crash plan() on a legitimate batch.
        active = capabilities()
        batch = Batch(active)
        found = batch.add("Email/query", {"filter": {"inMailbox": "m1"}})
        batch.add("Email/set", {"destroy": found.ref_ids()})
        assert len(batch.plan()) == 1

    def test_literal_containers_are_still_counted(self):
        active = capabilities()
        batch = Batch(active)
        batch.add("Email/set", {"update": {f"e{i}": {} for i in range(501)}})
        with pytest.raises(CapabilityFieldError):
            batch.plan()

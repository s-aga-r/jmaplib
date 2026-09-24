"""Credential presentation and the 401 refresh path.

Driven through a real ``httpx.MockTransport`` rather than by calling the flow
generators by hand, because the thing worth testing is what httpx does with our
auth objects - including that it stops when we stop yielding.
"""

from __future__ import annotations

import base64
import threading

import httpx
import pytest

from jmap.auth import (
    AppPasswordAuth,
    BasicAuth,
    BearerAuth,
    CallableAuth,
    InsufficientScopeError,
    JMAPAuth,
    OAuth2Auth,
    OAuth2Token,
)
from jmap.auth.challenge import parse_challenges
from jmap.core.errors import AuthenticationError

STALWART_CHALLENGES = [
    ('Bearer realm="Stalwart Server", resource_metadata="/.well-known/oauth-protected-resource"'),
    'Basic realm="Stalwart Server"',
]


def recording_transport(*responses: httpx.Response) -> tuple[httpx.MockTransport, list[str]]:
    """A transport replaying ``responses`` and recording each Authorization header."""
    seen: list[str] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return queue.pop(0) if queue else httpx.Response(200, json={})

    return httpx.MockTransport(handler), seen


def unauthorized(*, challenges: list[str] | None = None) -> httpx.Response:
    headers = [("www-authenticate", value) for value in (challenges or STALWART_CHALLENGES)]
    return httpx.Response(401, headers=headers, json={"status": 401})


class TestStaticCredentials:
    def test_basic_encodes_username_and_password(self):
        transport, seen = recording_transport()
        with httpx.Client(transport=transport) as client:
            client.get("https://x/jmap/session", auth=BasicAuth("alice@example.com", "pw"))
        expected = base64.b64encode(b"alice@example.com:pw").decode()
        assert seen == [f"Basic {expected}"]

    def test_basic_handles_non_ascii(self):
        transport, seen = recording_transport()
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=BasicAuth("üser", "pä55"))
        decoded = base64.b64decode(seen[0].removeprefix("Basic ")).decode()
        assert decoded == "üser:pä55"

    def test_app_password_is_basic(self):
        assert AppPasswordAuth is BasicAuth

    def test_bearer_sets_the_scheme(self):
        transport, seen = recording_transport()
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=BearerAuth("tok123"))
        assert seen == ["Bearer tok123"]

    def test_callable_is_consulted_per_request(self):
        values = iter(["Bearer one", "Bearer two"])
        transport, seen = recording_transport(httpx.Response(200), httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            auth = CallableAuth(lambda: next(values))
            client.get("https://x/", auth=auth)
            client.get("https://x/", auth=auth)
        assert seen == ["Bearer one", "Bearer two"]

    @pytest.mark.parametrize("auth", [BasicAuth("u", "p"), BearerAuth("t")])
    def test_static_credentials_never_retry_a_401(self, auth):
        # A retry would send byte-identical credentials, so it cannot succeed -
        # it can only look like brute force. Stalwart fail2bans that.
        transport, seen = recording_transport(unauthorized(), httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            response = client.get("https://x/", auth=auth)
        assert response.status_code == 401
        assert len(seen) == 1

    def test_base_class_demands_an_implementation(self):
        with pytest.raises(NotImplementedError):
            JMAPAuth().apply(httpx.Request("GET", "https://x/"))


class TestOAuth2Token:
    def test_expiry_window(self):
        token = OAuth2Token("a", expires_at=100.0)
        assert token.expires_within(10, now=95.0)
        assert not token.expires_within(10, now=80.0)

    def test_a_token_with_no_expiry_never_looks_stale(self):
        assert not OAuth2Token("a").expires_within(10, now=1e9)

    def test_repr_does_not_leak_the_token(self):
        text = repr(OAuth2Token("super-secret", scope="mail"))
        assert "super-secret" not in text
        assert "mail" in text


class TestOAuth2Refresh:
    def test_refreshes_once_and_retries(self):
        transport, seen = recording_transport(unauthorized(), httpx.Response(200, json={}))
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client:
            response = client.get("https://x/", auth=auth)

        assert response.status_code == 200
        assert seen == ["Bearer old", "Bearer new"]
        assert auth.generation == 1

    def test_gives_up_after_a_single_retry(self):
        transport, seen = recording_transport(unauthorized(), unauthorized())
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client:
            response = client.get("https://x/", auth=auth)
        assert response.status_code == 401
        assert len(seen) == 2

    def test_no_refresh_callable_means_no_retry(self):
        transport, seen = recording_transport(unauthorized(), httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=OAuth2Auth(OAuth2Token("old")))
        assert len(seen) == 1

    def test_a_basic_only_challenge_is_not_worth_refreshing(self):
        # This server will not take a bearer token at all.
        transport, seen = recording_transport(
            unauthorized(challenges=['Basic realm="x"']), httpx.Response(200)
        )
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
        assert len(seen) == 1
        assert auth.generation == 0

    def test_a_401_with_no_challenge_at_all(self):
        transport, seen = recording_transport(httpx.Response(401), httpx.Response(200))
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
        assert len(seen) == 1

    def test_non_401_responses_pass_through(self):
        transport, seen = recording_transport(httpx.Response(403))
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client:
            assert client.get("https://x/", auth=auth).status_code == 403
        assert len(seen) == 1

    def test_insufficient_scope_short_circuits(self):
        # Refreshing cannot widen a grant, so retrying just wastes a round trip.
        transport, _ = recording_transport(
            unauthorized(challenges=['Bearer error="insufficient_scope"'])
        )
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        with httpx.Client(transport=transport) as client, pytest.raises(InsufficientScopeError):
            client.get("https://x/", auth=auth)

    def test_token_is_persisted_before_the_old_one_is_dropped(self):
        # A server that rotates refresh tokens invalidates the old one as soon as
        # the refresh succeeds; saving afterwards would strand the grant on crash.
        order: list[str] = []

        class Store:
            def save(self, token: OAuth2Token) -> None:
                order.append(f"save:{token.access_token}")

        def refresh(_old: OAuth2Token) -> OAuth2Token:
            order.append("refresh")
            return OAuth2Token("new")

        auth = OAuth2Auth(OAuth2Token("old"), refresh=refresh, store=Store())
        transport, _ = recording_transport(unauthorized(), httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)

        assert order == ["refresh", "save:new"]
        assert auth.token.access_token == "new"

    def test_a_token_that_could_not_be_saved_is_still_the_one_used(self):
        # Against a server that rotates refresh tokens, the old one died the
        # moment this refresh succeeded. Dropping the new token because the
        # store failed kept the old one, and the next refresh replayed it -
        # which Fastmail answers by revoking the whole grant.
        presented: list[str | None] = []

        def refresh(current: OAuth2Token) -> OAuth2Token:
            presented.append(current.refresh_token)
            n = len(presented) + 1
            return OAuth2Token(f"AT-{n}", refresh_token=f"RT-{n}")

        class LockedOnce:
            saves = 0

            def save(self, token: OAuth2Token) -> None:
                self.saves += 1
                if self.saves == 1:
                    raise OSError("keyring locked")

        auth = OAuth2Auth(
            OAuth2Token("AT-1", refresh_token="RT-1"), refresh=refresh, store=LockedOnce()
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Authorization"] == "Bearer AT-2":
                return httpx.Response(200, json={})
            return unauthorized()

        with httpx.Client(transport=httpx.MockTransport(handler), auth=auth) as client:
            # The failure still reaches the caller: the token is only in memory.
            with pytest.raises(OSError, match="keyring locked"):
                client.get("https://x/")
            assert client.get("https://x/").status_code == 200
        assert presented == ["RT-1"]
        assert auth.token.refresh_token == "RT-2"
        assert auth.generation == 1

    def test_a_refresh_that_reenters_its_own_credential_fails_rather_than_hangs(self):
        # A refresh callable sending its request through a client carrying this
        # same credential, and refused with a Bearer 401, landed back in the
        # refresh in progress - waiting on the lock its own thread held.
        reentered = {"yet": False}
        clients: list[httpx.Client] = []

        def refresh(_old: OAuth2Token) -> OAuth2Token:
            if not reentered["yet"]:
                reentered["yet"] = True
                clients[0].get("https://as.example/token")
            return OAuth2Token("new")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Authorization"] == "Bearer new":
                return httpx.Response(200, json={})
            return unauthorized()

        auth = OAuth2Auth(OAuth2Token("old"), refresh=refresh)
        clients.append(httpx.Client(transport=httpx.MockTransport(handler), auth=auth))
        errors: list[BaseException] = []

        def first() -> None:
            try:
                clients[0].get("https://x/")
            except BaseException as exc:  # surfaced via `errors`, not swallowed
                errors.append(exc)

        worker = threading.Thread(target=first, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        [error] = errors
        assert isinstance(error, AuthenticationError)
        assert "refresh" in str(error)
        # Nothing is left held: the next request refreshes and gets through.
        assert clients[0].get("https://x/").status_code == 200
        clients[0].close()

    def test_concurrent_401s_refresh_exactly_once(self):
        # Two threads race a 401. Fastmail revokes the whole grant if a rotated
        # refresh token is replayed, so a second refresh is not merely wasteful.
        refreshes: list[int] = []
        barrier = threading.Barrier(2)

        def refresh(_old: OAuth2Token) -> OAuth2Token:
            refreshes.append(1)
            return OAuth2Token(f"new{len(refreshes)}")

        auth = OAuth2Auth(OAuth2Token("old"), refresh=refresh)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Authorization"] == "Bearer old":
                barrier.wait(timeout=5)  # ensure both see the stale token
                return unauthorized()
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with httpx.Client(transport=transport) as client:
                    client.get("https://x/", auth=auth)
            except BaseException as exc:  # surfaced via `errors`, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert len(refreshes) == 1
        assert auth.generation == 1


SCOPE_CHALLENGE = 'Bearer error="insufficient_scope", scope="urn:ietf:params:jmap:mail"'


def scope_refusal(status: int) -> httpx.Response:
    return httpx.Response(status, headers={"WWW-Authenticate": SCOPE_CHALLENGE})


class TestInsufficientScope:
    """RFC 6750 §3.1: a valid token that lacks a scope - most often a 403."""

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.parametrize(
        "make",
        [
            lambda: BearerAuth("t"),
            lambda: CallableAuth(lambda: "Bearer t"),
            lambda: OAuth2Auth(OAuth2Token("t")),
            lambda: OAuth2Auth(OAuth2Token("t"), refresh=lambda _t: OAuth2Token("n")),
        ],
    )
    def test_it_is_raised_for_either_status_and_any_credential(self, status, make):
        # Only a 401 was checked, and only when a refresh callable was set: the
        # 403 RFC 6750 recommends came back as a generic RequestError.
        transport, seen = recording_transport(scope_refusal(status))
        with httpx.Client(transport=transport) as client, pytest.raises(InsufficientScopeError):
            client.get("https://x/", auth=make())
        assert len(seen) == 1

    def test_it_carries_the_challenge_as_the_server_sent_it(self):
        # A repr of the parsed challenge could not be parsed back, so the scope
        # the server named was lost.
        transport, _ = recording_transport(scope_refusal(403))
        with (
            httpx.Client(transport=transport) as client,
            pytest.raises(InsufficientScopeError) as excinfo,
        ):
            client.get("https://x/", auth=BearerAuth("t"))
        assert excinfo.value.challenges == (SCOPE_CHALLENGE,)
        [challenge] = parse_challenges(excinfo.value.challenges)
        assert challenge.params["scope"] == "urn:ietf:params:jmap:mail"

    @pytest.mark.asyncio
    async def test_the_async_flow_raises_it_too(self):
        transport, _ = recording_transport(scope_refusal(403))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(InsufficientScopeError):
                await client.get("https://x/", auth=OAuth2Auth(OAuth2Token("t")))

    def test_an_ordinary_403_is_left_alone(self):
        transport, _ = recording_transport(httpx.Response(403))
        with httpx.Client(transport=transport) as client:
            assert client.get("https://x/", auth=BearerAuth("t")).status_code == 403


class TestProactiveRefresh:
    """docs/auth.md promises a refresh near expiry; nothing read expires_at."""

    def clock(self, monkeypatch: pytest.MonkeyPatch, start: float) -> dict[str, float]:
        now = {"t": start}
        monkeypatch.setattr("jmap.auth.credentials.time.time", lambda: now["t"])
        return now

    def test_a_token_about_to_expire_is_refreshed_before_it_is_sent(self, monkeypatch):
        # Sent anyway, it drew a 401 - and one without a Bearer challenge never
        # led to a refresh at all.
        now = self.clock(monkeypatch, 900.0)
        auth = OAuth2Auth(
            OAuth2Token("old", expires_at=1010.0),
            refresh=lambda _t: OAuth2Token("new", expires_at=4600.0),
        )
        now["t"] = 1000.0
        transport, seen = recording_transport(httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
        assert seen == ["Bearer new"]

    def test_a_token_with_time_to_spare_is_left_alone(self, monkeypatch):
        self.clock(monkeypatch, 1000.0)
        refreshed: list[int] = []

        def refresh(_old: OAuth2Token) -> OAuth2Token:
            refreshed.append(1)
            return OAuth2Token("new")

        auth = OAuth2Auth(OAuth2Token("old", expires_at=4600.0), refresh=refresh)
        transport, seen = recording_transport(httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
        assert seen == ["Bearer old"]
        assert refreshed == []

    def test_an_expired_token_is_refreshed_whatever_its_lifetime(self, monkeypatch):
        self.clock(monkeypatch, 1000.0)
        auth = OAuth2Auth(
            OAuth2Token("old", expires_at=900.0), refresh=lambda _t: OAuth2Token("new")
        )
        transport, seen = recording_transport(httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
        assert seen == ["Bearer new"]

    def test_a_short_lived_token_is_not_refreshed_on_every_request(self, monkeypatch):
        # Thirty seconds ahead of a token that only lives twenty is always:
        # the lead shrinks to half the token's lifetime instead.
        now = self.clock(monkeypatch, 1000.0)
        refreshed: list[int] = []

        def refresh(_old: OAuth2Token) -> OAuth2Token:
            refreshed.append(1)
            return OAuth2Token(f"t{len(refreshed)}", expires_at=now["t"] + 20)

        auth = OAuth2Auth(OAuth2Token("t0", expires_at=1020.0), refresh=refresh)
        transport, seen = recording_transport()
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=auth)
            now["t"] = 1011.0
            client.get("https://x/", auth=auth)
            client.get("https://x/", auth=auth)
        assert seen == ["Bearer t0", "Bearer t1", "Bearer t1"]
        assert refreshed == [1]

    def test_nothing_is_refreshed_without_a_deadline_or_a_callable(self, monkeypatch):
        self.clock(monkeypatch, 1000.0)
        transport, seen = recording_transport()
        with httpx.Client(transport=transport) as client:
            client.get("https://x/", auth=OAuth2Auth(OAuth2Token("a")))
            client.get("https://x/", auth=OAuth2Auth(OAuth2Token("b", expires_at=900.0)))
        assert seen == ["Bearer a", "Bearer b"]

    @pytest.mark.asyncio
    async def test_the_async_flow_refreshes_ahead_too(self, monkeypatch):
        now = self.clock(monkeypatch, 900.0)
        auth = OAuth2Auth(
            OAuth2Token("old", expires_at=1005.0), refresh=lambda _t: OAuth2Token("new")
        )
        now["t"] = 1000.0
        transport, seen = recording_transport(httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://x/", auth=auth)
        assert seen == ["Bearer new"]


class TestOAuth2RefreshAsync:
    """The async flow is a separate code path in httpx and must behave the same."""

    @pytest.mark.asyncio
    async def test_refreshes_once_and_retries(self):
        transport, seen = recording_transport(unauthorized(), httpx.Response(200, json={}))
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://x/", auth=auth)

        assert response.status_code == 200
        assert seen == ["Bearer old", "Bearer new"]

    @pytest.mark.asyncio
    async def test_no_retry_without_a_bearer_challenge(self):
        transport, seen = recording_transport(
            unauthorized(challenges=['Basic realm="x"']), httpx.Response(200)
        )
        auth = OAuth2Auth(OAuth2Token("old"), refresh=lambda _t: OAuth2Token("new"))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://x/", auth=auth)
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_static_credentials_do_not_retry(self):
        transport, seen = recording_transport(unauthorized(), httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://x/", auth=BasicAuth("u", "p"))
        assert len(seen) == 1

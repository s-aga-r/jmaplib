"""The I/O half of OAuth acquisition.

The loopback listener is exercised against a real socket - it is the one piece
that cannot be faked usefully, since the thing being tested is that a browser
redirect to an ephemeral port actually lands. Everything HTTP goes through
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from jmap.auth.acquire import (
    LOOPBACK_HOST,
    LoopbackReceiver,
    OAuthClient,
    RedirectTimeoutError,
    loopback_receiver,
    protected_resource_url,
)
from jmap.auth.credentials import BasicAuth, OAuth2Auth, OAuth2Token
from jmap.auth.flows import DeviceAuthorization, OAuthError
from jmap.auth.metadata import (
    AuthorizationServerMetadata,
    DiscoveryError,
    IssuerMismatchError,
    ResourceMismatchError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"

ISSUER = "https://auth.example.com"

SERVER_METADATA: dict[str, Any] = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "registration_endpoint": f"{ISSUER}/register",
    "device_authorization_endpoint": f"{ISSUER}/device",
    "code_challenge_methods_supported": ["S256"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
}


def metadata(**overrides: Any) -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata.model_validate({**SERVER_METADATA, **overrides})


class Router:
    """A tiny programmable JSON endpoint set."""

    def __init__(self) -> None:
        self.routes: dict[str, list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, path: str, *responses: httpx.Response) -> None:
        self.routes.setdefault(path, []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return httpx.Response(404, json={"error": "not_found"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


def oauth(router: Router, **kwargs: Any) -> OAuthClient:
    return OAuthClient(metadata(), client_id="client-1", http=router.client(), **kwargs)


class TestLoopbackReceiver:
    def test_it_binds_to_the_literal_loopback_address(self):
        # RFC 8252 §8.3: not `localhost`, which can resolve elsewhere and can be
        # reachable from off the machine.
        with loopback_receiver() as receiver:
            assert receiver.redirect_uri.startswith(f"http://{LOOPBACK_HOST}:")
            assert receiver.port > 0

    def test_it_captures_a_redirect(self):
        with loopback_receiver() as receiver:
            _get(f"{receiver.redirect_uri}?code=abc&state=xyz")
            assert receiver.wait(timeout=5).endswith("?code=abc&state=xyz")

    def test_it_answers_the_browser_with_something_readable(self):
        with loopback_receiver() as receiver:
            response = _get(f"{receiver.redirect_uri}?code=abc")
            assert response.status_code == 200
            assert b"close this window" in response.content

    def test_a_second_request_is_not_part_of_the_flow(self):
        # The listener is one-shot; anything after the redirect is something else
        # entirely, and must not overwrite the captured URL.
        with loopback_receiver() as receiver:
            _get(f"{receiver.redirect_uri}?code=first")
            _get(f"{receiver.redirect_uri}?code=second")
            assert "first" in receiver.wait(timeout=5)

    def test_a_redirect_with_no_query_still_completes(self):
        with loopback_receiver() as receiver:
            _get(receiver.redirect_uri)
            assert receiver.wait(timeout=5) == receiver.redirect_uri

    def test_waiting_gives_up(self):
        with loopback_receiver() as receiver, pytest.raises(RedirectTimeoutError, match=r"0\.05s"):
            receiver.wait(timeout=0.05)

    def test_the_socket_is_closed_on_exit(self):
        receiver = LoopbackReceiver()
        with receiver:
            port = receiver.port
        # Rebinding the same port proves the listener really let go of it.
        import socket

        probe = socket.socket()
        try:
            probe.bind((LOOPBACK_HOST, port))
        finally:
            probe.close()

    def test_a_stray_request_cannot_occupy_the_capture_slot(self):
        # Anything can reach a loopback port - a web page port-scanning
        # 127.0.0.1, another local process. A request for the wrong path must
        # not poison the one-shot slot and abort the genuine redirect.
        with loopback_receiver() as receiver:
            base = receiver.redirect_uri.rsplit("/callback", 1)[0]
            _get(f"{base}/favicon.ico")
            _get(f"{receiver.redirect_uri}?code=genuine&state=st")
            assert "genuine" in receiver.wait(timeout=5)

    def test_once_the_state_is_known_nothing_else_takes_the_slot(self):
        # A stray request for the registered path itself took the one-shot slot,
        # and the genuine redirect behind it was turned away: the flow failed on
        # a state mismatch it had no part in.
        with loopback_receiver() as receiver:
            receiver.expect_state("REAL")
            assert b"failed" in _get(receiver.redirect_uri).content
            _get(f"{receiver.redirect_uri}?code=stray&state=WRONG")
            _get(f"{receiver.redirect_uri}?code=genuine&state=REAL")
            assert receiver.wait(timeout=5).endswith("?code=genuine&state=REAL")

    def test_a_capture_made_before_the_state_was_known_is_let_go(self):
        with loopback_receiver() as receiver:
            _get(f"{receiver.redirect_uri}?code=early&state=OLD")
            receiver.expect_state("REAL")
            with pytest.raises(RedirectTimeoutError):
                receiver.wait(timeout=0.05)
            _get(f"{receiver.redirect_uri}?code=genuine&state=REAL")
            assert "genuine" in receiver.wait(timeout=5)

    def test_a_capture_carrying_the_state_is_kept(self):
        with loopback_receiver() as receiver:
            _get(f"{receiver.redirect_uri}?code=quick&state=REAL")
            receiver.expect_state("REAL")
            assert "quick" in receiver.wait(timeout=5)

    def test_an_idle_connection_does_not_block_the_redirect(self):
        # Anything local can connect and send nothing. One listener thread sat
        # reading that connection, and the redirect was never answered.
        with loopback_receiver() as receiver:
            idle = socket.create_connection((LOOPBACK_HOST, receiver.port))
            try:
                _get(f"{receiver.redirect_uri}?code=abc&state=xyz")
                assert receiver.wait(timeout=5).endswith("?code=abc&state=xyz")
            finally:
                idle.close()

    def test_closing_does_not_wait_for_an_idle_connection(self):
        receiver = LoopbackReceiver()
        receiver.__enter__()
        idle = socket.create_connection((LOOPBACK_HOST, receiver.port))
        try:
            time.sleep(0.2)  # accepted, and its handler reading
            closer = threading.Thread(target=receiver.__exit__, daemon=True)
            closer.start()
            closer.join(timeout=3)
            assert not closer.is_alive()
        finally:
            idle.close()

    def test_an_idle_connection_is_eventually_dropped(self, monkeypatch):
        # A thread each keeps idle connections from blocking the redirect; a
        # deadline keeps a pile of them from outliving the flow.
        from jmap.auth.acquire import _RedirectHandler

        assert _RedirectHandler.timeout is not None
        monkeypatch.setattr(_RedirectHandler, "timeout", 0.2)
        with loopback_receiver() as receiver:
            idle = socket.create_connection((LOOPBACK_HOST, receiver.port))
            idle.settimeout(5)
            try:
                assert idle.recv(1) == b""
            finally:
                idle.close()


class TestDiscovery:
    def test_the_chain_runs_from_a_challenge_pointer(self):
        router = Router()
        router.add(
            "/.well-known/oauth-protected-resource",
            httpx.Response(
                200,
                json={"resource": "https://jmap.example.com", "authorization_servers": [ISSUER]},
            ),
        )
        router.add(
            "/.well-known/oauth-authorization-server", httpx.Response(200, json=SERVER_METADATA)
        )
        found = OAuthClient.discover(
            "https://jmap.example.com/.well-known/oauth-protected-resource", http=router.client()
        )
        assert found.token_endpoint == f"{ISSUER}/token"

    def test_a_caller_can_override_which_issuer_to_use(self):
        # A resource listing several refuses to choose; naming one is the way out.
        router = Router()
        router.add(
            "/.well-known/oauth-protected-resource",
            httpx.Response(
                200,
                json={
                    "resource": "https://jmap.example.com",
                    "authorization_servers": [ISSUER, "https://other.example"],
                },
            ),
        )
        router.add(
            "/.well-known/oauth-authorization-server", httpx.Response(200, json=SERVER_METADATA)
        )
        found = OAuthClient.discover(
            "https://jmap.example.com/.well-known/oauth-protected-resource",
            http=router.client(),
            issuer=ISSUER,
        )
        assert found.issuer == ISSUER

    def test_it_falls_back_to_the_openid_convention(self):
        # Two well-known conventions exist and plenty of deployments answer only
        # one; trying both costs a round trip and saves the caller from knowing.
        router = Router()
        router.add("/.well-known/openid-configuration", httpx.Response(200, json=SERVER_METADATA))
        found = OAuthClient.discover_from_issuer(ISSUER, http=router.client())
        assert found.issuer == ISSUER
        assert [str(r.url.path) for r in router.requests] == [
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ]

    def test_a_cleartext_resource_metadata_url_is_refused(self):
        # RFC 9728 mandates TLS; the RFC 8414 issuer check downstream cannot
        # help against a MITM who serves self-consistent metadata for the
        # http:// URL it injected into the 401.
        router = Router()
        with pytest.raises(DiscoveryError, match="must be https"):
            OAuthClient.discover(
                "http://jmap.example.com/.well-known/oauth-protected-resource",
                http=router.client(),
            )
        assert router.requests == []

    def test_a_cleartext_issuer_is_refused(self):
        router = Router()
        with pytest.raises(DiscoveryError, match="must be https"):
            OAuthClient.discover_from_issuer("http://auth.example.com", http=router.client())
        assert router.requests == []

    def test_a_loopback_development_server_may_stay_cleartext(self):
        # RFC 8252's development posture: on the caller's own machine there is
        # nowhere for cleartext to leak to.
        issuer = "http://127.0.0.1:8080"
        router = Router()
        router.add(
            "/.well-known/oauth-authorization-server",
            httpx.Response(200, json={**SERVER_METADATA, "issuer": issuer}),
        )
        found = OAuthClient.discover_from_issuer(issuer, http=router.client())
        assert found.issuer == issuer

    def test_a_mismatched_issuer_is_refused(self):
        # RFC 8414 §3.3. Without this any host answering the path could nominate
        # whichever token endpoint it liked.
        router = Router()
        router.add(
            "/.well-known/oauth-authorization-server",
            httpx.Response(200, json={**SERVER_METADATA, "issuer": "https://evil.example"}),
        )
        router.add(
            "/.well-known/openid-configuration",
            httpx.Response(200, json={**SERVER_METADATA, "issuer": "https://evil.example"}),
        )
        with pytest.raises(IssuerMismatchError):
            OAuthClient.discover_from_issuer(ISSUER, http=router.client())

    def test_an_unreachable_document_is_a_discovery_error(self):
        router = Router()
        with pytest.raises(DiscoveryError):
            OAuthClient.discover_from_issuer(ISSUER, http=router.client())

    def test_a_non_json_document_is_a_discovery_error(self):
        router = Router()
        router.add(
            "/.well-known/oauth-authorization-server", httpx.Response(200, content=b"<html>")
        )
        router.add("/.well-known/openid-configuration", httpx.Response(200, content=b"<html>"))
        with pytest.raises(DiscoveryError, match="did not return JSON"):
            OAuthClient.discover_from_issuer(ISSUER, http=router.client())

    def test_a_transport_failure_is_a_discovery_error(self):
        def broken(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        client = httpx.Client(transport=httpx.MockTransport(broken))
        with pytest.raises(DiscoveryError, match="could not fetch"):
            OAuthClient.discover_from_issuer(ISSUER, http=client)

    def test_the_protected_resource_url_is_built_by_insertion(self):
        assert protected_resource_url("https://jmap.example.com/t") == (
            "https://jmap.example.com/.well-known/oauth-protected-resource/t"
        )

    def test_the_full_chain_closes_a_client_it_opened(self):
        # No `http=`, so it made its own. A leaked connection pool per discovery
        # is the kind of thing nothing notices until a long-running process dies.
        with pytest.raises(DiscoveryError):
            OAuthClient.discover("https://127.0.0.1:1/nowhere", timeout=0.05)

    def test_discovery_closes_a_client_it_opened(self):
        # No `http=`, so it made its own; a leaked connection pool per discovery
        # is the kind of thing nothing notices until a long-running process dies.
        with pytest.raises(DiscoveryError):
            OAuthClient.discover_from_issuer("https://127.0.0.1:1/nowhere", timeout=0.05)


PRM_URL = "https://jmap.example.com/.well-known/oauth-protected-resource"


def prm_router(
    document: dict[str, Any], *, path: str = "/.well-known/oauth-protected-resource"
) -> Router:
    """A resource answering at ``path``, and the authorization server it names."""
    router = Router()
    router.add(path, httpx.Response(200, json={"authorization_servers": [ISSUER], **document}))
    router.add("/.well-known/oauth-authorization-server", httpx.Response(200, json=SERVER_METADATA))
    return router


class TestDiscoveryRedirects:
    def test_a_redirect_to_cleartext_is_refused_before_it_is_fetched(self):
        # One cleartext hop, and whoever is on the path serves self-consistent
        # documents naming its own endpoints - the issuer check included.
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            if request.url.scheme == "https":
                downgraded = str(request.url.copy_with(scheme="http"))
                return httpx.Response(302, headers={"Location": downgraded})
            return httpx.Response(
                200,
                json={**SERVER_METADATA, "token_endpoint": "https://tokens.attacker.example/t"},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        with pytest.raises(DiscoveryError, match="redirect in the discovery chain must be https"):
            OAuthClient.discover_from_issuer(ISSUER, http=client)
        assert fetched
        assert all(url.startswith("https://") for url in fetched)

    def test_an_https_redirect_is_followed_without_credentials(self):
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.headers.get("Authorization")))
            if request.url.path == "/.well-known/oauth-authorization-server":
                return httpx.Response(301, headers={"Location": "/moved/metadata"})
            if request.url.path == "/moved/metadata":
                return httpx.Response(200, json=SERVER_METADATA)
            return httpx.Response(404)

        client = httpx.Client(
            transport=httpx.MockTransport(handler), auth=BasicAuth("alice@example.com", "pw")
        )
        assert OAuthClient.discover_from_issuer(ISSUER, http=client).issuer == ISSUER
        assert seen == [
            ("/.well-known/oauth-authorization-server", None),
            ("/moved/metadata", None),
        ]

    def test_a_redirect_loop_is_abandoned(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": str(request.url)})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(DiscoveryError, match="redirected more than"):
            OAuthClient.discover_from_issuer(ISSUER, http=client)


class TestResourceValidation:
    """RFC 9728 §3.3: metadata about another resource MUST NOT be used."""

    def test_metadata_naming_another_resource_is_refused(self):
        router = prm_router({"resource": "https://some-other-resource.example/"})
        with pytest.raises(ResourceMismatchError, match="some-other-resource"):
            OAuthClient.discover(PRM_URL, http=router.client())
        assert [r.url.path for r in router.requests] == ["/.well-known/oauth-protected-resource"]

    def test_metadata_naming_no_resource_is_refused(self):
        # RFC 9728 §2 makes it REQUIRED; without it nothing ties the document to
        # the server being signed in to.
        with pytest.raises(DiscoveryError, match="names no resource"):
            OAuthClient.discover(PRM_URL, http=prm_router({}).client())

    @pytest.mark.parametrize("resource", ["https://jmap.example.com", "https://jmap.example.com/"])
    def test_a_trailing_slash_names_the_same_resource(self, resource):
        # RFC 9728 §3.1 drops it before inserting the well-known segment, so
        # the URL cannot tell the two spellings apart and neither may the check.
        found = OAuthClient.discover(PRM_URL, http=prm_router({"resource": resource}).client())
        assert found.issuer == ISSUER

    def test_a_resource_with_a_path_is_published_under_it(self):
        tenant = "/.well-known/oauth-protected-resource/tenant"
        router = prm_router({"resource": "https://jmap.example.com/tenant"}, path=tenant)
        assert OAuthClient.discover(f"{PRM_URL}/tenant", http=router.client()).issuer == ISSUER
        # The host's own metadata, served at a tenant's path, speaks for the host.
        router = prm_router({"resource": "https://jmap.example.com"}, path=tenant)
        with pytest.raises(ResourceMismatchError):
            OAuthClient.discover(f"{PRM_URL}/tenant", http=router.client())

    def test_stalwarts_relative_pointer_resolves_against_the_requested_url(self):
        # Stalwart's challenge says resource_metadata="/.well-known/...", which
        # could not be fetched at all, and names its origin as the resource.
        router = Router()
        for path, fixture in (
            ("/.well-known/oauth-protected-resource", "stalwart-0.16.17-prm.json"),
            ("/.well-known/oauth-authorization-server", "stalwart-0.16.17-as-metadata.json"),
        ):
            document = json.loads((FIXTURES / fixture).read_text())
            router.add(path, httpx.Response(200, json=document))
        found = OAuthClient.discover(
            "/.well-known/oauth-protected-resource",
            resource="https://localhost/.well-known/jmap",
            http=router.client(),
        )
        assert found.issuer == "https://localhost"
        assert (
            str(router.requests[0].url) == "https://localhost/.well-known/oauth-protected-resource"
        )

    def test_a_relative_pointer_needs_the_requested_url(self):
        router = Router()
        with pytest.raises(DiscoveryError, match="relative"):
            OAuthClient.discover("/.well-known/oauth-protected-resource", http=router.client())
        assert router.requests == []

    def test_the_resource_must_cover_the_url_requested(self):
        # Correctly published, and still about some other resource on the host.
        other = "/.well-known/oauth-protected-resource/other"
        router = prm_router({"resource": "https://jmap.example.com/other"}, path=other)
        with pytest.raises(ResourceMismatchError, match="jmap/session"):
            OAuthClient.discover(
                f"{PRM_URL}/other",
                resource="https://jmap.example.com/jmap/session",
                http=router.client(),
            )

    def test_another_origins_metadata_is_refused(self):
        # A pointer aimed at another host, whose own document is in order.
        router = prm_router({"resource": "https://evil.example"})
        with pytest.raises(ResourceMismatchError):
            OAuthClient.discover(
                "https://evil.example/.well-known/oauth-protected-resource",
                resource="https://jmap.example.com/jmap",
                http=router.client(),
            )

    def test_a_pointer_elsewhere_is_checked_against_the_requested_url(self):
        router = prm_router({"resource": "https://jmap.example.com"}, path="/prm.json")
        found = OAuthClient.discover(
            "https://jmap.example.com/prm.json",
            resource="https://jmap.example.com/jmap",
            http=router.client(),
        )
        assert found.issuer == ISSUER


class TestBorrowedClient:
    """``http=`` may be the very client that authenticates to the JMAP server."""

    def test_its_credential_reaches_no_oauth_host(self):
        # These hosts are whichever the server's documents name, and the shared
        # client's Basic password went to every one of them.
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            answers: dict[str, Any] = {
                "/.well-known/oauth-protected-resource": {
                    "resource": "https://jmap.example.com",
                    "authorization_servers": [ISSUER],
                },
                "/.well-known/oauth-authorization-server": SERVER_METADATA,
                "/register": {"client_id": "assigned"},
                "/device": {
                    "device_code": "dc",
                    "user_code": "u",
                    "verification_uri": f"{ISSUER}/device",
                    "interval": 0,
                },
            }
            return httpx.Response(200, json=answers.get(request.url.path, {"access_token": "at"}))

        shared = httpx.Client(
            transport=httpx.MockTransport(handler), auth=BasicAuth("alice@example.com", "hunter2")
        )
        found = OAuthClient.discover(PRM_URL, http=shared)
        with OAuthClient(found, client_id="c", http=shared) as client:
            client.register("jmaplib test")
            client.refresh("rt")
            client.poll_device_flow(client.begin_device_flow())
            client.exchange_code("code", redirect_uri="http://127.0.0.1:9/cb", verifier="v" * 43)
        assert seen == [None] * 7

    def test_refreshing_through_it_does_not_deadlock(self):
        # docs/auth.md's wiring on one shared client. The refresh POST carried
        # the stale bearer; a token endpoint answering that with a Bearer 401
        # re-entered the refresh in progress, whose lock this very thread held.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                if "Authorization" in request.headers:
                    return httpx.Response(
                        401,
                        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                        json={"error": "invalid_client"},
                    )
                return httpx.Response(200, json={"access_token": "NEW", "refresh_token": "RT-2"})
            if request.headers.get("Authorization") == "Bearer NEW":
                return httpx.Response(200, json={})
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})

        shared = httpx.Client(transport=httpx.MockTransport(handler))
        client = OAuthClient(metadata(), client_id="c", http=shared)
        shared.auth = OAuth2Auth(
            OAuth2Token("STALE", refresh_token="RT-1"),
            refresh=lambda current: client.refresh(current.refresh_token or ""),
        )
        statuses: list[int] = []
        worker = threading.Thread(
            target=lambda: statuses.append(shared.get("https://jmap.example.com/api").status_code),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert statuses == [200]


class TestAuthorizationCode:
    def test_the_url_carries_the_pkce_challenge(self):
        router = Router()
        with oauth(router) as client:
            url, pkce, state = client.authorization_url("http://127.0.0.1:9/callback")
        assert "code_challenge_method=S256" in url
        assert pkce.challenge in url
        assert state in url
        # The verifier never leaves this process.
        assert pkce.verifier not in url

    def test_a_server_without_s256_is_refused_before_anything_is_sent(self):
        router = Router()
        client = OAuthClient(
            metadata(code_challenge_methods_supported=["plain"]),
            client_id="c",
            http=router.client(),
        )
        with pytest.raises(DiscoveryError, match="S256"):
            client.authorization_url("http://127.0.0.1:9/callback")

    def test_a_code_is_exchanged_for_a_token(self):
        router = Router()
        router.add(
            "/token",
            httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            ),
        )
        with oauth(router) as client:
            token = client.exchange_code(
                "the-code", redirect_uri="http://127.0.0.1:9/cb", verifier="v" * 43
            )
        assert token.access_token == "at"
        assert token.refresh_token == "rt"
        assert token.expires_at is not None
        sent = dict(httpx.QueryParams(router.requests[-1].content.decode()))
        assert sent["code_verifier"] == "v" * 43
        assert sent["grant_type"] == "authorization_code"

    def test_a_token_endpoint_returning_html_is_an_error(self):
        # A proxy or captive portal in the way, which otherwise surfaces as a
        # confusing JSON decode failure rather than "that was not JSON".
        router = Router()
        router.add("/token", httpx.Response(200, content=b"<html>login</html>"))
        with oauth(router) as client, pytest.raises(DiscoveryError, match="did not return JSON"):
            client.exchange_code("c", redirect_uri="http://127.0.0.1:9/cb", verifier="v" * 43)

    def test_a_token_error_is_raised(self):
        router = Router()
        router.add("/token", httpx.Response(400, json={"error": "invalid_grant"}))
        with oauth(router) as client, pytest.raises(OAuthError, match="invalid_grant"):
            client.exchange_code("bad", redirect_uri="http://127.0.0.1:9/cb", verifier="v" * 43)

    def test_the_whole_flow_runs_end_to_end(self, monkeypatch, capsys):
        """Listener, browser hand-off, redirect, and exchange, in one call."""
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "at"}))
        opened: list[str] = []

        def fake_open(url: str) -> bool:
            opened.append(url)
            # Play the browser: follow the redirect the authorize URL implies.
            params = httpx.QueryParams(url.split("?", 1)[1])
            threading.Thread(
                target=_get,
                args=(f"{params['redirect_uri']}?code=abc&state={params['state']}",),
                daemon=True,
            ).start()
            return True

        monkeypatch.setattr("webbrowser.open", fake_open)
        with oauth(router) as client:
            token = client.authorize(scope="jmap", open_browser=True, timeout=5)
        assert token.access_token == "at"
        assert len(opened) == 1
        assert "scope=jmap" in opened[0]

    def test_without_a_browser_the_url_is_printed(self, capsys):
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "at"}))
        with oauth(router) as client, pytest.raises(RedirectTimeoutError):
            client.authorize(timeout=0.05)
        assert "Open this URL" in capsys.readouterr().out

    def test_a_forged_redirect_never_reaches_the_token_endpoint(self, monkeypatch):
        # The state is checked before the code is looked at, so a redirect from
        # someone else's flow costs nothing - and no longer ends this one: the
        # genuine redirect behind it still completes the flow.
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "at"}))

        def fake_open(url: str) -> bool:
            params = httpx.QueryParams(url.split("?", 1)[1])

            def browse() -> None:
                _get(f"{params['redirect_uri']}?code=forged&state=not-ours")
                _get(f"{params['redirect_uri']}?code=genuine&state={params['state']}")

            threading.Thread(target=browse, daemon=True).start()
            return True

        monkeypatch.setattr("webbrowser.open", fake_open)
        with oauth(router) as client:
            assert client.authorize(open_browser=True, timeout=5).access_token == "at"
        [exchange] = [r for r in router.requests if r.url.path == "/token"]
        assert dict(httpx.QueryParams(exchange.content.decode()))["code"] == "genuine"

    def test_only_forged_redirects_time_the_flow_out(self, monkeypatch):
        router = Router()

        def fake_open(url: str) -> bool:
            params = httpx.QueryParams(url.split("?", 1)[1])
            _get(f"{params['redirect_uri']}?code=forged&state=not-ours")
            return True

        monkeypatch.setattr("webbrowser.open", fake_open)
        with oauth(router) as client, pytest.raises(RedirectTimeoutError):
            client.authorize(open_browser=True, timeout=0.2)
        assert router.requests == []

    def test_an_idle_connection_does_not_hold_up_the_flow(self, monkeypatch):
        # Anything local can open a connection to the listener and send nothing.
        # A single-threaded listener then never read the redirect behind it,
        # and authorize() hung for as long as that connection stayed open.
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "at"}))
        idle: list[socket.socket] = []

        def fake_open(url: str) -> bool:
            params = httpx.QueryParams(url.split("?", 1)[1])
            port = httpx.URL(params["redirect_uri"]).port
            idle.append(socket.create_connection((LOOPBACK_HOST, port)))
            threading.Thread(
                target=_get,
                args=(f"{params['redirect_uri']}?code=abc&state={params['state']}",),
                daemon=True,
            ).start()
            return True

        monkeypatch.setattr("webbrowser.open", fake_open)
        tokens: list[str] = []
        with oauth(router) as client:
            # On a thread, so a regression fails here rather than hanging: the
            # idle connection is what kept the flow from ever returning.
            worker = threading.Thread(
                target=lambda: tokens.append(
                    client.authorize(open_browser=True, timeout=5).access_token
                ),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=4)
            held_up = worker.is_alive()
            for connection in idle:
                connection.close()
            worker.join(timeout=10)
        assert not held_up
        assert tokens == ["at"]


class TestRegistration:
    def test_a_client_registers_as_public(self):
        # A desktop client cannot keep a secret; asking for one produces a
        # credential that ships in the binary and fools nobody.
        router = Router()
        router.add("/register", httpx.Response(201, json={"client_id": "assigned"}))
        with oauth(router) as client:
            registered = client.register("jmaplib test", redirect_uris=["http://127.0.0.1:9/cb"])
        assert registered.client_id == "assigned"
        assert client.client_id == "assigned"
        body = router.requests[-1].content.decode()
        assert '"token_endpoint_auth_method":"none"' in body.replace(" ", "")

    def test_a_registration_error_is_raised(self):
        router = Router()
        router.add("/register", httpx.Response(400, json={"error": "invalid_redirect_uri"}))
        with oauth(router) as client, pytest.raises(OAuthError, match="invalid_redirect_uri"):
            client.register("jmaplib test")

    def test_a_missing_endpoint_says_which_one(self):
        router = Router()
        client = OAuthClient(
            metadata(registration_endpoint=None), client_id="c", http=router.client()
        )
        with pytest.raises(DiscoveryError, match="registration_endpoint"):
            client.register("jmaplib test")


class TestDeviceFlow:
    def test_it_begins_with_a_user_code(self):
        router = Router()
        router.add(
            "/device",
            httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "WDJB-MJHT",
                    "verification_uri": "https://auth.example.com/device",
                    "interval": 1,
                },
            ),
        )
        with oauth(router) as client:
            authorization = client.begin_device_flow(scope="jmap")
        assert authorization.user_code == "WDJB-MJHT"
        assert authorization.interval == 1
        # The device code is a credential and must not be in the repr.
        assert "dc" not in repr(authorization)

    def test_polling_survives_authorization_pending(self, monkeypatch):
        # The normal state of a flow the user has not finished yet. Treating it as
        # a failure abandons a flow that is still in progress.
        monkeypatch.setattr("time.sleep", lambda _s: None)
        router = Router()
        router.add(
            "/token",
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(200, json={"access_token": "at"}),
        )
        with oauth(router) as client:
            token = client.poll_device_flow(
                DeviceAuthorization(
                    device_code="dc", user_code="u", verification_uri="v", interval=0.01
                )
            )
        assert token.access_token == "at"

    def test_slow_down_raises_the_interval_permanently(self, monkeypatch):
        # Not a one-off retry: the increase applies to every later poll, which is
        # the difference between backing off and being rate-limited.
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        router = Router()
        router.add(
            "/token",
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "at"}),
        )
        with oauth(router) as client:
            client.poll_device_flow(
                DeviceAuthorization(
                    device_code="dc", user_code="u", verification_uri="v", interval=5
                )
            )
        assert slept == [10.0, 15.0]

    def test_a_denial_stops_the_loop(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        router = Router()
        router.add("/token", httpx.Response(400, json={"error": "access_denied"}))
        with oauth(router) as client, pytest.raises(OAuthError, match="access_denied"):
            client.poll_device_flow(
                DeviceAuthorization(
                    device_code="dc", user_code="u", verification_uri="v", interval=0
                )
            )

    def test_an_expiring_code_stops_before_the_next_poll(self, monkeypatch):
        # Polling past the expiry just burns requests against a code that can
        # never be granted.
        monkeypatch.setattr("time.sleep", lambda _s: None)
        router = Router()
        router.add("/token", httpx.Response(400, json={"error": "authorization_pending"}))
        with oauth(router) as client, pytest.raises(OAuthError, match="expired_token"):
            client.poll_device_flow(
                DeviceAuthorization(
                    device_code="dc",
                    user_code="u",
                    verification_uri="v",
                    interval=10,
                    expires_in=1,
                )
            )

    def test_a_non_json_failure_is_still_a_failure(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        router = Router()
        router.add("/token", httpx.Response(500, content=b"gateway exploded"))
        with oauth(router) as client, pytest.raises(OAuthError, match="invalid_response"):
            client.poll_device_flow(
                DeviceAuthorization(
                    device_code="dc", user_code="u", verification_uri="v", interval=0
                )
            )

    def test_the_convenience_wrapper_prints_both_forms(self, monkeypatch, capsys):
        # Someone reading this aloud needs the short URI and the code separately;
        # the complete URI is for whoever can click it.
        monkeypatch.setattr("time.sleep", lambda _s: None)
        router = Router()
        router.add(
            "/device",
            httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "WDJB-MJHT",
                    "verification_uri": "https://auth.example.com/device",
                    "verification_uri_complete": "https://auth.example.com/device?code=WDJB-MJHT",
                    "interval": 0,
                },
            ),
        )
        router.add("/token", httpx.Response(200, json={"access_token": "at"}))
        with oauth(router) as client:
            token = client.device_flow()
        printed = capsys.readouterr().out
        assert token.access_token == "at"
        assert "WDJB-MJHT" in printed
        assert "https://auth.example.com/device" in printed


class TestRefresh:
    def test_a_refresh_token_is_exchanged(self):
        router = Router()
        router.add(
            "/token", httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated"})
        )
        with oauth(router) as client:
            token = client.refresh("old", scope="jmap")
        assert token.access_token == "new"
        assert token.refresh_token == "rotated"
        sent = dict(httpx.QueryParams(router.requests[-1].content.decode()))
        assert sent["grant_type"] == "refresh_token"
        assert sent["scope"] == "jmap"

    def test_a_client_secret_is_included_when_there_is_one(self):
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "new"}))
        with OAuthClient(
            metadata(), client_id="c", client_secret="s", http=router.client()
        ) as client:
            client.refresh("old")
        sent = dict(httpx.QueryParams(router.requests[-1].content.decode()))
        assert sent["client_secret"] == "s"

    def test_a_refresh_token_that_is_not_rotated_is_kept(self):
        # RFC 6749 §6: the server MAY issue a new refresh token. When it does
        # not, the one presented is still the grant - returning none made the
        # next refresh present nothing and fail with invalid_grant, stranding the
        # grant an hour in on every server that does not rotate.
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "new"}))
        with oauth(router) as client:
            token = client.refresh("RT-1")
        assert token.access_token == "new"
        assert token.refresh_token == "RT-1"

    def test_the_documented_wiring_survives_a_second_refresh(self):
        # docs/auth.md feeds each token's refresh_token into the next refresh.
        router = Router()
        router.add(
            "/token",
            httpx.Response(200, json={"access_token": "AT-2"}),
            httpx.Response(200, json={"access_token": "AT-3"}),
        )
        with oauth(router) as client:
            first = client.refresh("RT-1")
            second = client.refresh(first.refresh_token or "")
        presented = [
            dict(httpx.QueryParams(request.content.decode()))["refresh_token"]
            for request in router.requests
        ]
        assert presented == ["RT-1", "RT-1"]
        assert second.access_token == "AT-3"

    def test_no_refresh_token_is_invented_from_nothing(self):
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "new"}))
        with oauth(router) as client:
            assert client.refresh("").refresh_token is None


class TestTokenEndpointHardening:
    def test_a_cleartext_token_endpoint_is_refused(self):
        # The POST body carries the code, verifier, refresh token and client
        # secret; https is not negotiable outside loopback development.
        router = Router()
        with (
            OAuthClient(
                metadata(token_endpoint="http://auth.example.com/token"),
                client_id="c",
                http=router.client(),
            ) as client,
            pytest.raises(DiscoveryError, match="token endpoint must be https"),
        ):
            client.refresh("old")
        assert router.requests == []

    def test_a_loopback_token_endpoint_stays_usable(self):
        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "new"}))
        with OAuthClient(
            metadata(token_endpoint="http://127.0.0.1:8080/token"),
            client_id="c",
            http=router.client(),
        ) as client:
            assert client.refresh("old").access_token == "new"

    def test_a_token_endpoint_redirect_is_not_followed(self):
        # A 307 would re-send the whole form body - code, verifier, refresh
        # token, client secret - to wherever Location points.
        router = Router()
        router.add(
            "/token",
            httpx.Response(307, headers={"Location": "https://elsewhere.example/collect"}),
        )
        with oauth(router) as client, pytest.raises(DiscoveryError):
            client.refresh("old")
        assert [request.url.path for request in router.requests] == ["/token"]

    def test_the_device_poll_gets_the_same_protection(self):
        # The poll posts the device_code, which redeems the grant the moment the
        # user approves it: the same kind of secret, in the same kind of body,
        # that the other token requests refuse to send in cleartext or re-post
        # to wherever a redirect points.
        authorization = DeviceAuthorization(
            device_code="dc", user_code="u", verification_uri="v", interval=0
        )
        router = Router()
        with (
            OAuthClient(
                metadata(token_endpoint="http://auth.example.com/token"),
                client_id="c",
                http=router.client(),
            ) as client,
            pytest.raises(DiscoveryError, match="token endpoint must be https"),
        ):
            client.poll_device_flow(authorization)
        assert router.requests == []
        router = Router()
        router.add(
            "/token",
            httpx.Response(307, headers={"Location": "https://elsewhere.example/collect"}),
        )
        with oauth(router) as client, pytest.raises(OAuthError):
            client.poll_device_flow(authorization)
        assert [request.url.path for request in router.requests] == ["/token"]

    def test_token_deadlines_are_wall_clock(self):
        # TokenStore persists expires_at; a monotonic deadline is meaningless in
        # any other process.
        import time

        router = Router()
        router.add("/token", httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        with oauth(router) as client:
            token = client.refresh("old")
        assert token.expires_at is not None
        assert abs(token.expires_at - (time.time() + 3600)) < 60


class TestLifecycle:
    def test_the_repr_names_the_issuer_and_client(self):
        router = Router()
        with oauth(router) as client:
            assert "auth.example.com" in repr(client)
            assert "client-1" in repr(client)

    def test_a_borrowed_http_client_is_not_closed(self):
        # The caller may be reusing it for the JMAP connection itself.
        borrowed = Router().client()
        with OAuthClient(metadata(), client_id="c", http=borrowed):
            pass
        assert not borrowed.is_closed

    def test_an_owned_http_client_is_closed(self):
        client = OAuthClient(metadata(), client_id="c")
        client.close()
        assert client._http.is_closed


def _get(url: str) -> httpx.Response:
    with httpx.Client(timeout=5) as client:
        return client.get(url)

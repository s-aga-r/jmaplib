"""Acquiring an OAuth token (RFC 6749, RFC 8252, RFC 8628, RFC 9728).

The thin I/O half of :mod:`jmap.auth.flows`. Everything decidable is decided
there; what happens here is HTTP requests, a browser hand-off, and one short-lived
loopback listener.

The chain a caller normally wants is one call: a 401 from the JMAP server carries a
``resource_metadata`` pointer, that document names an authorization server, its
metadata names the endpoints, and the flow runs against those. Nothing needs to be
configured in advance, which matters because the alternative is asking users to
paste endpoint URLs they have no way to verify.

**The redirect listener binds to 127.0.0.1, never ``localhost``.** RFC 8252 §8.3
is explicit: the literal address avoids a name that can resolve elsewhere, and it
means nothing outside this machine can reach the port. The port is ephemeral
because §7.3 requires the authorization server to accept any port on a loopback
redirect, which in turn means a client never has to reserve one.

**The listener answers exactly one request and stops.** It exists for the seconds
between opening a browser and the redirect arriving; leaving it up afterwards is a
socket accepting unauthenticated requests for no reason.
"""

from __future__ import annotations

import http.server
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlsplit

import httpx

from jmap.auth.credentials import OAuth2Token
from jmap.auth.flows import (
    AuthorizationRequest,
    DeviceAuthorization,
    OAuthError,
    PollOutcome,
    RegisteredClient,
    classify_poll,
    code_exchange_body,
    device_authorization_body,
    device_token_body,
    parse_redirect,
    parse_token_response,
    refresh_body,
    registration_body,
)
from jmap.auth.metadata import (
    AUTHORIZATION_SERVER_SUFFIX,
    PROTECTED_RESOURCE_SUFFIX,
    AuthorizationServerMetadata,
    DiscoveryError,
    ProtectedResourceMetadata,
    openid_url,
    well_known_url,
)
from jmap.auth.pkce import PKCEPair, new_state
from jmap.core.ijson import loads

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: RFC 8252 §8.3. The literal address, not ``localhost``.
LOOPBACK_HOST: Final = "127.0.0.1"

#: Hosts allowed to speak plain ``http`` in the discovery/token chain. RFC 8414
#: §3.1 and RFC 6749 §3.2 mandate TLS for everything else - every credential in
#: the flow travels through these exchanges - but a development server on the
#: caller's own machine has nowhere for cleartext to leak to.
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

#: C0 and C1 control characters. Terminal escape sequences live here, and the
#: URLs this module prints are the one line the user is told to trust.
_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _require_https(url: str, *, purpose: str) -> str:
    """Refuse to carry credentials to a non-TLS endpoint.

    Applied to every URL the discovery chain fetches or POSTs secrets to. The
    RFC 8414 §3.3 issuer check downstream cannot help against a cleartext
    endpoint: a network attacker serves self-consistent metadata for the
    ``http://`` issuer it injected, and the code, verifier and refresh token
    then travel in the clear to an endpoint it controls.
    """
    split = urlsplit(url)
    if split.scheme == "https":
        return url
    if split.scheme == "http" and (split.hostname or "").lower() in _LOOPBACK_HOSTS:
        return url
    raise DiscoveryError(
        f"{purpose} must be https (or http on loopback for development), got {url!r}; "
        f"OAuth credentials travel through this exchange and cleartext hands them "
        f"to the network"
    )


def _printable(text: str) -> str:
    """Server-supplied text made safe to print to a terminal.

    Escape sequences could overwrite the displayed URL with a legitimate-looking
    one while the copyable target stays the attacker's.
    """
    return _CONTROL_CHARS.sub("�", text)


#: How long to wait for the browser to come back before giving up.
DEFAULT_REDIRECT_TIMEOUT: Final = 300.0

#: What the browser shows once the redirect lands. Plain text on purpose: this
#: page is served by a process the user did not knowingly start, so it says what
#: happened and nothing else.
_DONE_PAGE: Final = b"Authorisation complete. You can close this window."
_FAILED_PAGE: Final = b"Authorisation failed. You can close this window."


class RedirectTimeoutError(OAuthError):
    """The browser never came back."""

    def __init__(self, seconds: float) -> None:
        super().__init__(
            "redirect_timeout",
            description=f"no redirect arrived within {seconds:g}s; the flow was abandoned",
        )


class _RedirectServer(http.server.HTTPServer):
    """An HTTPServer carrying the slot its handler writes the redirect into.

    Subclassed rather than attribute-assigned so the handler's back-reference has
    a type: ``BaseHTTPRequestHandler.server`` is typed as the base class, and an
    ad-hoc attribute on it is invisible to a type checker.
    """

    holder: _Redirect
    #: The redirect path registered with the authorization server. Only requests
    #: for it may occupy the capture slot.
    expected_path: str


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Captures one redirect and says nothing useful to anything else."""

    captured: str | None = None
    server_version = "jmaplib"
    sys_version = ""

    def do_GET(self) -> None:
        server = cast("_RedirectServer", self.server)
        holder = server.holder
        # Only the registered path may occupy the one capture slot. Anything can
        # reach a loopback port - a web page port-scanning 127.0.0.1, another
        # local process - and a stray request winning the slot would abort the
        # flow for the genuine redirect arriving a moment later.
        if holder.url is None and self.path.split("?", 1)[0] == server.expected_path:
            holder.url = self.path
            holder.arrived.set()
            body = _DONE_PAGE
        else:
            # A second request on a one-shot listener is not part of the flow.
            body = _FAILED_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr logging.

        The default writes the request line, which for an OAuth redirect is the
        authorization code - straight to whatever collects this process's stderr.
        """


@dataclass(slots=True)
class _Redirect:
    url: str | None = None
    arrived: threading.Event = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.arrived = threading.Event()


class LoopbackReceiver:
    """A one-shot listener on an ephemeral loopback port (RFC 8252 §7.3).

    Used as a context manager so the socket is closed even when the flow raises,
    which matters more than usual: the alternative is an open port on the user's
    machine outliving the operation that needed it.
    """

    __slots__ = ("_holder", "_httpd", "_path", "_thread")

    def __init__(self, path: str = "/callback") -> None:
        self._path = path
        self._holder = _Redirect()
        self._httpd = _RedirectServer((LOOPBACK_HOST, 0), _RedirectHandler)
        self._httpd.holder = self._holder
        self._httpd.expected_path = path
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}{self._path}"

    def __enter__(self) -> LoopbackReceiver:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def wait(self, timeout: float = DEFAULT_REDIRECT_TIMEOUT) -> str:
        """Block until the redirect lands, returning its full URL."""
        if not self._holder.arrived.wait(timeout):
            raise RedirectTimeoutError(timeout)
        return f"{self.redirect_uri}{_query_of(self._holder.url or '')}"


def _query_of(path: str) -> str:
    split = path.split("?", 1)
    return f"?{split[1]}" if len(split) == 2 else ""


def loopback_receiver(path: str = "/callback") -> LoopbackReceiver:
    """Open a redirect listener for the duration of a flow.

    The receiver is itself a context manager, so this is a factory rather than a
    wrapper - ``with loopback_receiver() as receiver:`` reads the same either way.
    """
    return LoopbackReceiver(path)


class OAuthClient:
    """Runs the OAuth flows against one authorization server.

    Holds no credentials of its own. It is given (or discovers) the endpoints and
    hands back an :class:`~jmap.auth.credentials.OAuth2Token`, which is what the
    JMAP client actually authenticates with.
    """

    __slots__ = ("_http", "_owns_http", "client_id", "client_secret", "metadata")

    metadata: AuthorizationServerMetadata
    client_id: str
    #: ``None`` for a public client, which is what a desktop or CLI application
    #: is: it cannot keep a secret, and one it ships is not a secret.
    client_secret: str | None

    def __init__(
        self,
        metadata: AuthorizationServerMetadata,
        *,
        client_id: str,
        client_secret: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.metadata = metadata
        self.client_id = client_id
        self.client_secret = client_secret
        self._owns_http = http is None
        self._http = http or httpx.Client(follow_redirects=True, timeout=timeout)

    # -- discovery ---------------------------------------------------------- #
    @staticmethod
    def discover(
        resource_metadata_url: str,
        *,
        http: httpx.Client | None = None,
        issuer: str | None = None,
        timeout: float = 30.0,
    ) -> AuthorizationServerMetadata:
        """Walk RFC 9728 -> RFC 8414 from a challenge's ``resource_metadata``.

        ``issuer`` overrides the one the resource names, for the case where a
        resource lists several and the caller has chosen.
        """
        client = http or httpx.Client(follow_redirects=True, timeout=timeout)
        try:
            _require_https(resource_metadata_url, purpose="the resource metadata URL")
            resource = ProtectedResourceMetadata.of(_fetch_json(client, resource_metadata_url))
            chosen = issuer or resource.issuer()
            return _fetch_server_metadata(client, chosen)
        finally:
            if http is None:
                client.close()

    @staticmethod
    def discover_from_issuer(
        issuer: str, *, http: httpx.Client | None = None, timeout: float = 30.0
    ) -> AuthorizationServerMetadata:
        """Fetch RFC 8414 metadata for a known issuer."""
        client = http or httpx.Client(follow_redirects=True, timeout=timeout)
        try:
            return _fetch_server_metadata(client, issuer)
        finally:
            if http is None:
                client.close()

    def register(
        self,
        client_name: str,
        *,
        redirect_uris: Sequence[str] = (),
        scope: str | None = None,
        grant_types: Sequence[str] = ("authorization_code", "refresh_token"),
    ) -> RegisteredClient:
        """Register this client dynamically (RFC 7591).

        Registering as a *public* client: a desktop or CLI application cannot keep
        a secret, and asking for one produces a credential that ships in the
        binary and fools nobody.
        """
        endpoint = _require_https(
            self.metadata.require("registration_endpoint"), purpose="the registration endpoint"
        )
        response = self._http.post(
            endpoint,
            follow_redirects=False,
            json=registration_body(
                client_name=client_name,
                redirect_uris=redirect_uris,
                grant_types=grant_types,
                scope=scope,
            ),
            headers={"Accept": "application/json"},
        )
        registered = RegisteredClient.of(_json_body(response))
        self.client_id = registered.client_id
        self.client_secret = registered.client_secret
        return registered

    # -- authorization code ------------------------------------------------- #
    def authorization_url(
        self,
        redirect_uri: str,
        *,
        scope: str | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> tuple[str, PKCEPair, str]:
        """Build the URL to open, plus the two secrets needed to finish the flow.

        Returned rather than stored so a caller can run several flows at once, and
        so the verifier's lifetime is visible in the calling code rather than
        hidden in this object.
        """
        self.metadata.check_pkce()
        pkce = PKCEPair.generate()
        state = new_state()
        request = AuthorizationRequest(
            authorization_endpoint=self.metadata.require("authorization_endpoint"),
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            pkce=pkce,
            state=state,
            scope=scope,
            extra=dict(extra or {}),
        )
        return request.url(), pkce, state

    def exchange_code(self, code: str, *, redirect_uri: str, verifier: str) -> OAuth2Token:
        """Redeem an authorization code (RFC 6749 §4.1.3)."""
        return self._token_request(
            code_exchange_body(
                code=code,
                client_id=self.client_id,
                redirect_uri=redirect_uri,
                verifier=verifier,
                client_secret=self.client_secret,
            )
        )

    def authorize(
        self,
        *,
        scope: str | None = None,
        open_browser: bool = False,
        timeout: float = DEFAULT_REDIRECT_TIMEOUT,
        extra: Mapping[str, str] | None = None,
    ) -> OAuth2Token:
        """Run the whole authorization-code flow, listener and all.

        ``open_browser`` is off by default: launching a browser is a visible side
        effect on the user's machine, and a caller embedding this in a TUI or a
        headless context wants the URL rather than a window. The URL is printed
        through the returned listener either way.
        """
        with loopback_receiver() as receiver:
            url, pkce, state = self.authorization_url(
                receiver.redirect_uri, scope=scope, extra=extra
            )
            if open_browser:
                import webbrowser

                webbrowser.open(url)
            else:
                print(f"Open this URL to authorise:\n{_printable(url)}")
            redirect = receiver.wait(timeout)
            code = parse_redirect(redirect, expected_state=state)
            return self.exchange_code(
                code, redirect_uri=receiver.redirect_uri, verifier=pkce.verifier
            )

    # -- device flow -------------------------------------------------------- #
    def begin_device_flow(self, *, scope: str | None = None) -> DeviceAuthorization:
        """Ask for a user code (RFC 8628 §3.1)."""
        endpoint = _require_https(
            self.metadata.require("device_authorization_endpoint"),
            purpose="the device authorization endpoint",
        )
        response = self._http.post(
            endpoint,
            follow_redirects=False,
            data=device_authorization_body(client_id=self.client_id, scope=scope),
            headers={"Accept": "application/json"},
        )
        return DeviceAuthorization.of(_json_body(response))

    def poll_device_flow(
        self, authorization: DeviceAuthorization, *, deadline: float | None = None
    ) -> OAuth2Token:
        """Poll until the user finishes, or the code expires (RFC 8628 §3.4-3.5).

        The interval is the server's, and a ``slow_down`` raises it permanently
        rather than for one poll - which is the whole difference between backing
        off and being rate-limited.
        """
        endpoint = self.metadata.require("token_endpoint")
        interval = authorization.interval
        if deadline is None and authorization.expires_in is not None:
            deadline = time.monotonic() + authorization.expires_in
        body = device_token_body(device_code=authorization.device_code, client_id=self.client_id)
        while True:
            response = self._http.post(endpoint, data=body, headers={"Accept": "application/json"})
            result = classify_poll(
                response.status_code, _json_body(response, strict=False), interval=interval
            )
            if result.outcome is PollOutcome.GRANTED:
                return _as_token(_json_body(response))
            if result.error is not None:
                raise result.error
            interval = result.interval
            if deadline is not None and time.monotonic() + interval > deadline:
                raise OAuthError(
                    "expired_token", description="the device code expired before authorisation"
                )
            time.sleep(interval)

    def device_flow(self, *, scope: str | None = None) -> OAuth2Token:
        """Begin a device flow, print the instructions, and poll to completion."""
        authorization = self.begin_device_flow(scope=scope)
        target = authorization.verification_uri_complete or authorization.verification_uri
        # Both are shown: the complete URI is convenient, but a user reading this
        # to someone else needs the short one and the code separately (§3.3.1).
        print(
            f"Go to {_printable(authorization.verification_uri)} and enter the code "
            f"{_printable(authorization.user_code)}\n  (or open {_printable(target)} directly)"
        )
        return self.poll_device_flow(authorization)

    # -- refresh ------------------------------------------------------------ #
    def refresh(self, refresh_token: str, *, scope: str | None = None) -> OAuth2Token:
        """Exchange a refresh token for a new access token (RFC 6749 §6)."""
        return self._token_request(
            refresh_body(
                refresh_token=refresh_token,
                client_id=self.client_id,
                client_secret=self.client_secret,
                scope=scope,
            )
        )

    # -- plumbing ----------------------------------------------------------- #
    def _token_request(self, body: Mapping[str, str]) -> OAuth2Token:
        endpoint = _require_https(
            self.metadata.require("token_endpoint"), purpose="the token endpoint"
        )
        # No redirects: a 307/308 would re-send the form body - code, verifier,
        # refresh token, client secret - to wherever the Location header points.
        response = self._http.post(
            endpoint,
            follow_redirects=False,
            data=dict(body),
            headers={"Accept": "application/json"},
        )
        return _as_token(_json_body(response))

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> OAuthClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"OAuthClient(issuer={self.metadata.issuer!r}, client_id={self.client_id!r})"


def _as_token(document: Any) -> OAuth2Token:
    # Wall clock, not monotonic: TokenStore persists this deadline, and a
    # monotonic value is meaningless in any other process.
    fields = parse_token_response(document, now=time.time())
    return OAuth2Token(
        fields["access_token"],
        refresh_token=fields["refresh_token"],
        expires_at=fields["expires_at"],
        scope=fields["scope"],
    )


def _fetch_json(client: httpx.Client, url: str) -> Any:
    try:
        response = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"could not fetch {url}: {exc}") from exc
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise DiscoveryError(f"{url} answered HTTP {response.status_code}")
    return _decode(response.content, url)


def _fetch_server_metadata(client: httpx.Client, issuer: str) -> AuthorizationServerMetadata:
    """Try RFC 8414's path, then OpenID Connect's, before giving up.

    Two conventions exist and plenty of deployments answer only one. Trying both
    costs a round trip on servers that follow the older convention and saves the
    caller from having to know which they are talking to.
    """
    _require_https(issuer, purpose="the issuer")
    attempts = (
        well_known_url(issuer, AUTHORIZATION_SERVER_SUFFIX),
        openid_url(issuer),
    )
    last: DiscoveryError | None = None
    for url in attempts:
        try:
            return AuthorizationServerMetadata.of(_fetch_json(client, url), expected_issuer=issuer)
        except DiscoveryError as exc:
            last = exc
    raise last if last is not None else DiscoveryError(f"no metadata found for {issuer}")


def _json_body(response: httpx.Response, *, strict: bool = True) -> Any:
    try:
        return _decode(response.content, str(response.url))
    except DiscoveryError:
        if strict:
            raise
        return None


def _decode(content: bytes, url: str) -> Any:
    try:
        return loads(content)
    except ValueError as exc:
        raise DiscoveryError(f"{url} did not return JSON") from exc


def protected_resource_url(resource: str) -> str:
    """The RFC 9728 well-known URL for a resource, when no challenge named one."""
    return well_known_url(resource, PROTECTED_RESOURCE_SUFFIX)

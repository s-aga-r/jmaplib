"""Credential presentation for JMAP over HTTP.

RFC 8620 §8.2 deliberately defines no authentication scheme: the Session resource
is simply an authenticated endpoint, and which credential works is a property of
the deployment. So this module presents credentials and understands challenges;
it does not *acquire* them. Interactive OAuth flows land later, behind an extra.

Two decisions here are correctness, not style:

**Basic and Bearer never retry a 401.** The credential would be byte-identical
the second time, so a retry cannot succeed - it can only look like a brute-force
attempt. Stalwart fail2bans repeated failures, so a well-meaning retry loop gets
the client's IP banned.

**Refresh is single-flight, and persists before it discards.** Fastmail rotates
the refresh token on every use and revokes the entire grant if an old one is
replayed, so two concurrent 401s must produce exactly one refresh, and the new
token must reach the store *before* the old one is dropped. A generation counter
does the first part; ordering inside :meth:`OAuth2Auth._apply_token` does the
second.
"""

from __future__ import annotations

import base64
import threading
from typing import TYPE_CHECKING, Protocol

import httpx

from jmap.auth.challenge import find_challenge, parse_challenges
from jmap.core.errors import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from jmap.auth.challenge import Challenge


class InsufficientScopeError(AuthenticationError):
    """The token is valid but not authorised for what was asked (RFC 6750 §3.1).

    Distinct from a plain 401 because refreshing cannot help: the grant itself is
    too narrow, and retrying just spends a round trip to be told so again.
    """


def _challenges_of(response: httpx.Response) -> tuple[Challenge, ...]:
    return parse_challenges(response.headers.get_list("www-authenticate"))


class JMAPAuth(httpx.Auth):
    """Base for every credential: applies a header, and does not retry.

    Subclasses that *can* usefully retry override the flow methods rather than
    flipping a flag, so the retry logic sits next to the refresh that justifies
    it.
    """

    def apply(self, request: httpx.Request) -> None:
        """Attach this credential to ``request``."""
        raise NotImplementedError

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        self.apply(request)
        yield request


class BasicAuth(JMAPAuth):
    """HTTP Basic (RFC 7617) - username and password, or an app password.

    The workhorse for self-hosted servers. Stalwart requires the username to be a
    full email address since v0.16; bare names only work through a
    backwards-compatibility shim that appends the default domain.
    """

    __slots__ = ("_header",)

    def __init__(self, username: str, password: str) -> None:
        raw = f"{username}:{password}".encode()
        self._header = f"Basic {base64.b64encode(raw).decode('ascii')}"

    def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = self._header


#: Fastmail and Stalwart both issue app passwords, which are presented exactly as
#: Basic credentials. Named separately because the operational advice differs.
AppPasswordAuth = BasicAuth


class BearerAuth(JMAPAuth):
    """A static bearer token (RFC 6750): an API token or a long-lived access token."""

    __slots__ = ("_header",)

    def __init__(self, token: str) -> None:
        self._header = f"Bearer {token}"

    def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = self._header


class CallableAuth(JMAPAuth):
    """Delegates the header value to a callable, consulted on every request.

    The escape hatch for a scheme this library does not model - a signed request,
    a token from an ambient credential provider, a vendor scheme. Return the
    complete header value, including the scheme.
    """

    __slots__ = ("_supplier",)

    def __init__(self, supplier: Callable[[], str]) -> None:
        self._supplier = supplier

    def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = self._supplier()


class TokenStore(Protocol):
    """Where a refreshed token is persisted.

    ``save`` must durably store the token before returning. It is called *before*
    the previous refresh token is discarded, because a crash in between, against
    a server that rotates refresh tokens, loses the grant permanently.
    """

    def save(self, token: OAuth2Token) -> None: ...


class OAuth2Token:
    """An access token and what is needed to renew it."""

    __slots__ = ("access_token", "expires_at", "refresh_token", "scope")

    access_token: str
    refresh_token: str | None
    #: Monotonic-clock deadline, or ``None`` when the server did not say.
    expires_at: float | None
    scope: str | None

    def __init__(
        self,
        access_token: str,
        *,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        scope: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.scope = scope

    def expires_within(self, seconds: float, *, now: float) -> bool:
        """Whether the token expires inside ``seconds``.

        Drives proactive renewal for SSE and WebSocket, which authenticate only
        at the handshake: once connected, an expiring token is not noticed until
        the server drops the stream.
        """
        return self.expires_at is not None and self.expires_at - now <= seconds

    def __repr__(self) -> str:
        # Never render the token itself; these objects end up in logs.
        return f"OAuth2Token(expires_at={self.expires_at!r}, scope={self.scope!r})"


class OAuth2Auth(JMAPAuth):
    """A bearer token that can be renewed once, in response to a 401.

    ``refresh`` is supplied by the caller: this class owns *when* to renew and
    the concurrency around it, not the wire format of the grant.
    """

    def __init__(
        self,
        token: OAuth2Token,
        *,
        refresh: Callable[[OAuth2Token], OAuth2Token] | None = None,
        store: TokenStore | None = None,
    ) -> None:
        self._token = token
        self._refresh = refresh
        self._store = store
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def token(self) -> OAuth2Token:
        return self._token

    @property
    def generation(self) -> int:
        """Increments on every successful refresh. Exposed for tests and for
        callers coordinating a reconnect."""
        return self._generation

    def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = f"Bearer {self._token.access_token}"

    def _should_retry(self, response: httpx.Response) -> bool:
        """Whether a 401 is worth one refresh-and-retry.

        Gated on an actual Bearer challenge rather than the bare status code: a
        401 offering only Basic means this server will not take our token at all,
        and refreshing is wasted work.
        """
        if response.status_code != 401 or self._refresh is None:
            return False
        bearer = find_challenge(_challenges_of(response), "bearer")
        if bearer is None:
            return False
        if bearer.error == "insufficient_scope":
            raise InsufficientScopeError(
                "the access token lacks the scope this request needs; "
                "re-authorise with a wider scope rather than refreshing",
                challenges=tuple(str(c) for c in _challenges_of(response)),
            )
        return True

    def _apply_token(self, token: OAuth2Token) -> None:
        # Persist first. A server that rotates refresh tokens invalidates the old
        # one the moment this succeeds, so a crash after swapping but before
        # saving would strand the grant.
        if self._store is not None:
            self._store.save(token)
        self._token = token
        self._generation += 1

    def _refresh_once(self, seen_generation: int) -> bool:
        """Refresh unless another caller already did. Returns whether we hold a
        newer token than the one that produced the 401."""
        refresh = self._refresh
        if refresh is None:  # pragma: no cover - _should_retry already ruled this out
            return False
        with self._lock:
            if self._generation != seen_generation:
                # Someone else refreshed while we waited for the lock; their
                # token is already newer than the one that just failed.
                return True
            self._apply_token(refresh(self._token))
            return True

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        seen = self._generation
        self.apply(request)
        response = yield request
        if self._should_retry(response):
            self._refresh_once(seen)
            self.apply(request)
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        seen = self._generation
        self.apply(request)
        response = yield request
        if self._should_retry(response):
            # The refresh callable is synchronous by contract. Running it inline
            # blocks the event loop briefly, which is the right trade: a refresh
            # is one short request, and threading it out would need the caller to
            # supply a second, async version of the same function.
            self._refresh_once(seen)
            self.apply(request)
            yield request

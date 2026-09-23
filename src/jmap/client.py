"""The synchronous JMAP client.

A thin shell over the I/O-free kernel: fetch the Session, resolve capabilities
against it, then post batches and route the answers back. Everything interesting
- what ``using`` should contain, how to split a batch, whether a failure may be
retried - is decided elsewhere and merely *called* from here, which is what keeps
this and :mod:`jmap.aio` from drifting apart.

Discovery starts wherever the caller points it and follows redirects, because
``/.well-known/jmap`` is a redirect on every real server (Stalwart answers 307,
Fastmail 302). The status code is never asserted; what matters is landing on the
session document with the ``Authorization`` header intact.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Self

import httpx

from jmap._shell import (
    NO_BLOB_ACCOUNT,
    as_json_object,
    failure_of,
    problem_of,
    request_headers,
    retry_pause,
    session_is_stale,
)
from jmap.api.namespace import Namespaces
from jmap.batch import Batch, NoAccountError, all_mutations_guarded, is_mutating
from jmap.blobs import (
    UploadResult,
    check_upload_size,
    download_url,
    parse_upload,
    upload_headers,
    upload_url,
)
from jmap.capabilities.core import CORE_URN
from jmap.core.errors import AuthenticationError, RequestError, TransportError
from jmap.core.ijson import dumps, loads
from jmap.core.response import Response
from jmap.core.retry import Failure, RetryPolicy, Safety, classify, should_retry
from jmap.core.session import Session, check_session_redirects
from jmap.defaults import default_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType

    from jmap.capabilities.registry import ActiveCapabilities, Registry
    from jmap.core.ids import Id
    from jmap.core.invocation import Handle
    from jmap.core.request import Request
    from jmap.discovery import SRVTarget


class BatchContext:
    """A batch that executes when its ``with`` block exits.

    Handles are readable afterwards. Executing on exit rather than on demand is
    what makes back-references natural: everything queued in the block travels in
    one request, so a later call can point at an earlier one's results.
    """

    __slots__ = ("_batch", "_client", "_extra_using", "_namespaces")

    def __init__(self, client: JMAPClient, batch: Batch, extra_using: frozenset[str]) -> None:
        self._client = client
        self._batch = batch
        self._extra_using = extra_using
        self._namespaces = Namespaces(batch, client.capabilities)

    def __getattr__(self, name: str) -> Any:
        """Capability namespaces: ``batch.mail.email.get(...)``."""
        return getattr(self._namespaces, name)

    def add(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        account_id: Id | None = None,
    ) -> Handle[Any]:
        return self._batch.add(name, arguments, account_id=account_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Do not send a batch assembled by a block that raised: the caller's
        # intent is unknown past the point it failed.
        if exc_type is None:
            self._client.execute(self._batch, extra_using=self._extra_using)


class JMAPClient:
    """A synchronous JMAP client bound to one session."""

    session: Session
    #: Where the session was fetched from, so it can be refetched.
    session_url: str
    capabilities: ActiveCapabilities
    registry: Registry
    retry_policy: RetryPolicy
    default_account: Id | None
    #: Set when a response reports a different ``sessionState``. The session is
    #: not refetched automatically - that would turn one stale read into a
    #: surprise round trip in the middle of someone's batch.
    session_stale: bool
    #: Whether draft-tracking capabilities were opted into. Kept so that
    #: :meth:`refresh_session` resolves the same way :meth:`connect` did.
    experimental: bool

    def __init__(
        self,
        session: Session,
        capabilities: ActiveCapabilities,
        http: httpx.Client,
        *,
        registry: Registry,
        retry_policy: RetryPolicy | None = None,
        default_account: Id | None = None,
        owns_http: bool = False,
        session_url: str = "",
        experimental: bool = False,
    ) -> None:
        self.session = session
        self.session_url = session_url or session.api_url
        self.capabilities = capabilities
        self.registry = registry
        self.retry_policy = retry_policy or RetryPolicy()
        self.default_account = default_account
        self._http = http
        self._owns_http = owns_http
        self.session_stale = False
        self.experimental = experimental

    # -- construction ------------------------------------------------------- #
    @classmethod
    def connect(
        cls,
        url: str,
        *,
        auth: httpx.Auth,
        registry: Registry | None = None,
        account_id: Id | None = None,
        experimental: bool = False,
        retry_policy: RetryPolicy | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> Self:
        """Fetch the session from ``url`` and resolve what this server supports.

        ``url`` may be the session endpoint or anything that redirects to it,
        which is the normal case: ``https://example.com/.well-known/jmap``.
        """
        owns_http = http is None
        client = http or httpx.Client(follow_redirects=True, timeout=timeout)
        try:
            session = _fetch_session(client, url, auth=auth)
        except BaseException:
            if owns_http:
                client.close()
            raise

        client.auth = auth
        registry = registry or default_registry()
        account = account_id or _primary_account(session)
        return cls(
            session,
            registry.resolve(session, account, experimental=experimental),
            client,
            registry=registry,
            retry_policy=retry_policy,
            default_account=account,
            owns_http=owns_http,
            session_url=url,
            experimental=experimental,
        )

    @classmethod
    def discover(
        cls,
        address: str,
        *,
        auth: httpx.Auth,
        use_srv: bool = True,
        confirm_srv_target: Callable[[SRVTarget], bool] | None = None,
        **kwargs: Any,
    ) -> Self:
        """Connect using only an email address or domain (RFC 8620 §2.2).

        Tries each candidate in preference order - SRV targets first, then
        ``https://<domain>/.well-known/jmap`` - and returns the first that yields
        a session. The well-known guess alone is not enough: Fastmail answers 404
        there, so a client that only tries it cannot reach one of the largest JMAP
        deployments in existence.

        An SRV target outside the address's domain is tried only when
        ``confirm_srv_target`` returns true for it. The first candidate receives
        the credentials, a forged DNS answer can name any host, and TLS vouches
        for that host rather than the domain - so RFC 6186 §6 has the client ask
        first. When nothing else answers and such a target went untried,
        :class:`~jmap.discovery.UnconfirmedSRVTargetError` names it, so the
        question can be put to the user and the call made again with the answer.

        Otherwise the error raised on total failure is the *last* one, which is
        the well-known URL's - the one a user can most easily check by hand.

        A candidate that answers with something other than a session - a parked
        domain's page, a portal, JSON of the wrong shape - is moved past like
        one that cannot be reached: it says this is not the server, not that
        there is none. A 401 or a downgrading session still stops the search,
        because trying the next candidate would hand it the same credentials.
        """
        from jmap.discovery import UnconfirmedSRVTargetError, candidate_urls, domain_of

        untried: list[SRVTarget] = []

        def confirm(target: SRVTarget) -> bool:
            if confirm_srv_target is not None and confirm_srv_target(target):
                return True
            untried.append(target)
            return False

        failure: BaseException | None = None
        for url in candidate_urls(address, use_srv=use_srv, confirm_srv_target=confirm):
            try:
                return cls.connect(url, auth=auth, **kwargs)
            except (TransportError, RequestError, ValueError) as exc:
                failure = exc
        if untried:
            raise UnconfirmedSRVTargetError(domain_of(address), tuple(untried)) from failure
        raise (
            failure
            if failure is not None
            else TransportError(f"no JMAP server could be found for {address!r}")
        )

    def refresh_session(self) -> None:
        """Refetch the session and re-resolve capabilities.

        Called by the application when :attr:`session_stale` is set. A server may
        gain or lose a capability at any time, so the resolution is redone rather
        than patched - with the same ``experimental`` opt-in :meth:`connect` had,
        or the draft-tracking capabilities would vanish on the first refresh.
        """
        self.session = _fetch_session(self._http, self.session_url, auth=None)
        self.capabilities = self.registry.resolve(
            self.session, self.default_account, experimental=self.experimental
        )
        self.session_stale = False

    @property
    def http(self) -> httpx.Client:
        """The underlying HTTP client.

        Exposed so the push shells can hold a streaming connection open on the
        same authenticated client rather than opening a second one - the event
        source needs exactly the credentials this client already carries.
        """
        return self._http

    # -- calling ------------------------------------------------------------ #
    def batch(self, *, extra_using: frozenset[str] = frozenset()) -> BatchContext:
        """Open a batch. Calls queued inside it travel in one request."""
        return BatchContext(
            self,
            Batch(self.capabilities, default_account=self.default_account),
            extra_using,
        )

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        account_id: Id | None = None,
    ) -> Any:
        """Make a single call and return its parsed result."""
        batch = Batch(self.capabilities, default_account=self.default_account)
        handle = batch.add(name, arguments, account_id=account_id)
        self.execute(batch)
        return handle.result

    def echo(self, **arguments: Any) -> Any:
        """``Core/echo`` - the cheapest proof that auth and routing both work."""
        return self.call("Core/echo", arguments)

    def execute(self, batch: Batch, *, extra_using: frozenset[str] = frozenset()) -> None:
        """Send ``batch``, possibly as several requests, and resolve its handles."""
        for request in batch.requests(extra_using=extra_using):
            response = self._post(request, batch)
            batch.absorb(response)
            if session_is_stale(self.session, response.session_state):
                self.session_stale = True

    # -- blobs -------------------------------------------------------------- #
    def upload(
        self,
        content: bytes,
        *,
        content_type: str | None = None,
        account_id: Id | None = None,
    ) -> UploadResult:
        """Upload a blob and return its id (RFC 8620 §6.1).

        Blobs move over plain HTTP rather than as method calls, so this is not
        batchable and takes no ``using``.
        """
        account = account_id or self.default_account or self.session.implied_account()
        if account is None:
            raise NoAccountError("upload", CORE_URN, reason=NO_BLOB_ACCOUNT)
        check_upload_size(len(content), self.capabilities.limits)
        response = self._http.post(
            upload_url(self.session, account),
            content=content,
            headers=upload_headers(content_type),
        )
        problem = problem_of(response.status_code, response.headers, response.content)
        if problem is not None:
            raise problem
        return parse_upload(as_json_object(loads(response.content), "the upload endpoint"))

    def download(
        self,
        blob_id: str,
        *,
        name: str = "download",
        content_type: str = "application/octet-stream",
        account_id: Id | None = None,
    ) -> bytes:
        """Fetch a blob's bytes (RFC 8620 §6.2).

        ``name`` and ``content_type`` only shape the response headers; the blob
        is addressed by ``blob_id`` alone.
        """
        account = account_id or self.default_account or self.session.implied_account()
        if account is None:
            raise NoAccountError("download", CORE_URN, reason=NO_BLOB_ACCOUNT)
        response = self._http.get(
            download_url(self.session, account, blob_id, name=name, content_type=content_type)
        )
        problem = problem_of(response.status_code, response.headers, response.content)
        if problem is not None:
            raise problem
        return response.content

    # -- transport ---------------------------------------------------------- #
    def _post(self, request: Request, batch: Batch) -> Response:
        mutating = is_mutating(batch, self.capabilities)
        guarded = all_mutations_guarded(batch, self.capabilities)
        body = dumps(request.to_wire()).encode()
        attempt = 0

        safety: Safety
        delay: float
        error: BaseException

        while True:
            attempt += 1
            try:
                response = self._http.post(
                    self.session.api_url, content=body, headers=request_headers()
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # Only these prove the request was never sent. The catch-all
                # below must not be widened into this branch: ReadError and
                # RemoteProtocolError arrive *after* the bytes went out, and
                # classifying them as never-applied re-sends unguarded
                # mutations the server may have already run.
                safety, error = classify(Failure.CONNECT), exc
                delay = self.retry_policy.backoff(attempt)
            except httpx.TimeoutException as exc:
                # A timeout is not a connection failure: the request went out, so
                # the server may have applied it and simply answered too slowly.
                safety, error = classify(Failure.TIMEOUT), exc
                delay = self.retry_policy.backoff(attempt)
            except httpx.HTTPError as exc:
                safety, error = classify(Failure.INTERRUPTED), exc
                delay = self.retry_policy.backoff(attempt)
            else:
                problem = problem_of(response.status_code, response.headers, response.content)
                if problem is None:
                    return _parse_response(response.content)
                if response.status_code == 401:
                    raise AuthenticationError(
                        "the server rejected these credentials",
                        challenges=tuple(response.headers.get_list("www-authenticate")),
                    )
                pause = retry_pause(
                    problem,
                    response.headers,
                    policy=self.retry_policy,
                    attempt=attempt,
                    now=time.time(),
                )
                if pause is None:
                    # The server asked for a longer wait than the policy takes.
                    raise problem
                safety, delay, error = failure_of(response.status_code, problem), pause, problem

            if not should_retry(
                safety,
                policy=self.retry_policy,
                attempt=attempt,
                mutating=mutating,
                all_mutations_guarded=guarded,
            ):
                raise _as_error(error)
            time.sleep(delay)

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"JMAPClient(username={self.session.username!r}, account={self.default_account!r})"


def _as_error(error: BaseException) -> BaseException:
    if isinstance(error, httpx.HTTPError):
        return TransportError(str(error) or type(error).__name__)
    return error


def _parse_response(body: bytes) -> Response:
    return Response.from_wire(as_json_object(loads(body), "the API"))


def _fetch_session(http: httpx.Client, url: str, *, auth: httpx.Auth | None) -> Session:
    """GET the session document, following whatever redirects stand in the way."""
    kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
    if auth is not None:
        kwargs["auth"] = auth
    try:
        response = http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise _as_error(exc) from exc
    # Before anything in the answer is trusted: a hop onto a weaker channel
    # hands the choice of apiUrl to whoever answered it.
    check_session_redirects(url, [*(str(hop.url) for hop in response.history), str(response.url)])
    if response.status_code == 401:
        raise AuthenticationError(
            "the server rejected these credentials",
            challenges=tuple(response.headers.get_list("www-authenticate")),
        )
    problem = problem_of(response.status_code, response.headers, response.content)
    if problem is not None:
        raise problem
    parsed = as_json_object(loads(response.content), "the session endpoint")
    # `response.url` is the post-redirect URL, which is what relative endpoint
    # URLs in the document must resolve against.
    return Session.from_wire(parsed, base_url=str(response.url))


def _primary_account(session: Session) -> Id | None:
    """The account to use when the caller names none.

    Prefers the core capability's primary account, which every server sets, and
    otherwise leaves it unset so the batch layer can raise a precise error rather
    than guessing.
    """
    from jmap.capabilities.core import CORE_URN

    return session.primary_account_for(CORE_URN)

"""The asynchronous JMAP client.

Deliberately a mirror of :mod:`jmap.client`: the two differ only in where the
``await`` goes and which httpx class they hold. Every decision either makes -
what ``using`` needs, how to split a batch, whether a failure may be retried - is
taken by the same I/O-free code, so the twins cannot disagree about protocol
behaviour, only about concurrency.

That similarity is load-bearing and worth preserving. If these files start to
drift, the fix is to move the difference down into the kernel, not to special-case
it here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Self

import anyio
import httpx

from jmap._shell import (
    NO_BLOB_ACCOUNT,
    as_json_object,
    failure_of,
    problem_of,
    request_headers,
    retry_delay,
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
from jmap.core.errors import AuthenticationError, TransportError
from jmap.core.ijson import dumps, loads
from jmap.core.response import Response
from jmap.core.retry import Failure, RetryPolicy, Safety, classify, should_retry
from jmap.core.session import Session
from jmap.defaults import default_registry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from jmap.capabilities.registry import ActiveCapabilities, Registry
    from jmap.core.ids import Id
    from jmap.core.invocation import Handle
    from jmap.core.request import Request


class AsyncBatchContext:
    """A batch that executes when its ``async with`` block exits."""

    __slots__ = ("_batch", "_client", "_extra_using", "_namespaces")

    def __init__(self, client: AsyncJMAPClient, batch: Batch, extra_using: frozenset[str]) -> None:
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # A block that raised has an unknown intent past the failure point.
        if exc_type is None:
            await self._client.execute(self._batch, extra_using=self._extra_using)


class AsyncJMAPClient:
    """An asynchronous JMAP client bound to one session."""

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

    def __init__(
        self,
        session: Session,
        capabilities: ActiveCapabilities,
        http: httpx.AsyncClient,
        *,
        registry: Registry,
        retry_policy: RetryPolicy | None = None,
        default_account: Id | None = None,
        owns_http: bool = False,
        session_url: str = "",
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

    # -- construction ------------------------------------------------------- #
    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        auth: httpx.Auth,
        registry: Registry | None = None,
        account_id: Id | None = None,
        experimental: bool = False,
        retry_policy: RetryPolicy | None = None,
        http: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> Self:
        """Fetch the session from ``url`` and resolve what this server supports."""
        owns_http = http is None
        client = http or httpx.AsyncClient(follow_redirects=True, timeout=timeout)
        try:
            session = await _fetch_session(client, url, auth=auth)
        except BaseException:
            if owns_http:
                await client.aclose()
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
        )

    async def refresh_session(self) -> None:
        """Refetch the session and re-resolve capabilities."""
        self.session = await _fetch_session(self._http, self.session_url, auth=None)
        self.capabilities = self.registry.resolve(self.session, self.default_account)
        self.session_stale = False

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying HTTP client.

        Exposed so the push shells can hold a streaming connection open on the
        same authenticated client rather than opening a second one - the event
        source needs exactly the credentials this client already carries.
        """
        return self._http

    # -- calling ------------------------------------------------------------ #
    def batch(self, *, extra_using: frozenset[str] = frozenset()) -> AsyncBatchContext:
        """Open a batch. Calls queued inside it travel in one request."""
        return AsyncBatchContext(
            self,
            Batch(self.capabilities, default_account=self.default_account),
            extra_using,
        )

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        account_id: Id | None = None,
    ) -> Any:
        """Make a single call and return its parsed result."""
        batch = Batch(self.capabilities, default_account=self.default_account)
        handle = batch.add(name, arguments, account_id=account_id)
        await self.execute(batch)
        return handle.result

    async def echo(self, **arguments: Any) -> Any:
        """``Core/echo`` - the cheapest proof that auth and routing both work."""
        return await self.call("Core/echo", arguments)

    async def execute(self, batch: Batch, *, extra_using: frozenset[str] = frozenset()) -> None:
        """Send ``batch``, possibly as several requests, and resolve its handles."""
        for request in batch.plan(extra_using=extra_using):
            response = await self._post(request, batch)
            batch.absorb(response)
            if session_is_stale(self.session, response.session_state):
                self.session_stale = True

    # -- blobs -------------------------------------------------------------- #
    async def upload(
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
        response = await self._http.post(
            upload_url(self.session, account),
            content=content,
            headers=upload_headers(content_type),
        )
        problem = problem_of(response.status_code, response.headers, response.content)
        if problem is not None:
            raise problem
        return parse_upload(as_json_object(loads(response.content), "the upload endpoint"))

    async def download(
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
        response = await self._http.get(
            download_url(self.session, account, blob_id, name=name, content_type=content_type)
        )
        problem = problem_of(response.status_code, response.headers, response.content)
        if problem is not None:
            raise problem
        return response.content

    # -- transport ---------------------------------------------------------- #
    async def _post(self, request: Request, batch: Batch) -> Response:
        mutating = is_mutating(batch, self.capabilities)
        guarded = all_mutations_guarded(batch, self.capabilities)
        body = dumps(request.to_wire()).encode()
        attempt = 0

        safety: Safety
        delay: float | None
        error: BaseException

        while True:
            attempt += 1
            try:
                response = await self._http.post(
                    self.session.api_url, content=body, headers=request_headers()
                )
            except httpx.TimeoutException as exc:
                safety, delay, error = classify(Failure.TIMEOUT), None, exc
            except httpx.HTTPError as exc:
                safety, delay, error = classify(Failure.CONNECT), None, exc
            else:
                problem = problem_of(response.status_code, response.headers, response.content)
                if problem is None:
                    return Response.from_wire(as_json_object(loads(response.content), "the API"))
                if response.status_code == 401:
                    raise AuthenticationError(
                        "the server rejected these credentials",
                        challenges=tuple(response.headers.get_list("www-authenticate")),
                    )
                safety = failure_of(response.status_code, problem)
                delay = retry_delay(
                    response.headers,
                    policy_delay=self.retry_policy.backoff(attempt),
                    now=time.time(),
                )
                error = problem

            if not should_retry(
                safety,
                policy=self.retry_policy,
                attempt=attempt,
                mutating=mutating,
                all_mutations_guarded=guarded,
            ):
                raise _as_error(error)
            # anyio rather than asyncio.sleep so the client works under trio too.
            await anyio.sleep(delay if delay is not None else self.retry_policy.backoff(attempt))

    # -- lifecycle ---------------------------------------------------------- #
    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return (
            f"AsyncJMAPClient(username={self.session.username!r}, account={self.default_account!r})"
        )


def _as_error(error: BaseException) -> BaseException:
    if isinstance(error, httpx.HTTPError):
        return TransportError(str(error) or type(error).__name__)
    return error


async def _fetch_session(http: httpx.AsyncClient, url: str, *, auth: httpx.Auth | None) -> Session:
    kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
    if auth is not None:
        kwargs["auth"] = auth
    try:
        response = await http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise _as_error(exc) from exc
    if response.status_code == 401:
        raise AuthenticationError(
            "the server rejected these credentials",
            challenges=tuple(response.headers.get_list("www-authenticate")),
        )
    problem = problem_of(response.status_code, response.headers, response.content)
    if problem is not None:
        raise problem
    parsed = as_json_object(loads(response.content), "the session endpoint")
    return Session.from_wire(parsed, base_url=str(response.url))


def _primary_account(session: Session) -> Id | None:
    from jmap.capabilities.core import CORE_URN

    return session.primary_account_for(CORE_URN)

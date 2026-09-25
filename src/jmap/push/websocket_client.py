"""Holding a JMAP WebSocket open: requests, answers and push on one connection.

:mod:`jmap.push.websocket` frames and classifies the messages (RFC 8887); this
owns the socket. A :class:`WebSocketClient` opens one on a connected client's
session and credentials, sends batches over it with the same builders the HTTP
client offers, and yields the state changes the server pushes. It needs
``jmaplib[ws]``.

Three things differ from the HTTP path:

**A request that loses its socket is not re-sent.** The HTTP client retries only
what provably never ran. A socket that drops mid-request gives no such proof,
so the error goes back to the caller, who knows whether the batch could have
changed anything.

**The close code matters.** RFC 8887 §4.1 has the server close with 1008 when
the credentials that opened the socket expire. Redialling with the same ones
earns the same close forever, so it is raised as an
:class:`~jmap.core.errors.AuthenticationError` rather than a transport failure.

**The endpoint is checked.** The session parse checks its own endpoints but not
the WebSocket URL, which lives in a capability object. It must be no weaker
than the API: ``wss``, or ``ws`` only when the API is itself plain http, or on
loopback.

The sync client reads the socket only while it waits: a request reads until its
own answer arrives and keeps any state change it passes for
:meth:`WebSocketClient.notifications`. Use it from one thread at a time.
"""

from __future__ import annotations

import queue
from collections import deque
from collections.abc import Sequence
from contextlib import ExitStack
from typing import TYPE_CHECKING, Final, Self

from jmap._shell import refusal_of, session_is_stale
from jmap.batch import Batch
from jmap.capabilities.push import WEBSOCKET_URN, WebSocketCapability
from jmap.client import BatchContext
from jmap.core.errors import AuthenticationError, JMAPError, TransportError
from jmap.core.session import check_endpoint
from jmap.models.arguments import checked
from jmap.models.push import StateChange
from jmap.push.eventsource import UnknownPushTypeError
from jmap.push.websocket import (
    CLOSE_INVALID_PAYLOAD,
    SUBPROTOCOL,
    RequestErrorMessage,
    SubprotocolError,
    WebSocketProtocol,
    WebSocketProtocolError,
    should_reauthenticate,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import TracebackType

    import httpx
    from httpx_ws import WebSocketSession

    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.client import JMAPClient
    from jmap.core.request import Request
    from jmap.core.response import Response
    from jmap.core.session import Session
    from jmap.push.websocket import Incoming

#: The largest message accepted from the server. RFC 8887 bounds none, and a
#: JMAP response is routinely far past httpx-ws's 64 KiB default - a /get of a
#: hundred emails, a Blob/get carrying data.
DEFAULT_MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024

#: RFC 6455 §7.4.1: a normal close, which ends a notification stream quietly.
CLOSE_NORMAL: Final = 1000


class WebSocketUnavailableError(JMAPError):
    """The server offers no WebSocket, or none this client can use as asked."""


def websocket_url(session: Session) -> str:
    """The advertised WebSocket endpoint, once it is known to be safe to use."""
    capability = WebSocketCapability.of(session.capability_value(WEBSOCKET_URN))
    if not capability.url:
        raise WebSocketUnavailableError(f"the server does not advertise {WEBSOCKET_URN}")
    check_endpoint("WebSocket url", capability.url, base_url=session.api_url)
    return capability.url


def check_push(session: Session, capabilities: ActiveCapabilities, types: Sequence[str]) -> None:
    """Refuse push the socket cannot deliver, before asking for it."""
    if not WebSocketCapability.of(session.capability_value(WEBSOCKET_URN)).supports_push:
        raise WebSocketUnavailableError("this server's WebSocket does not carry push")
    known = capabilities.push_types()
    unknown = tuple(name for name in types if name not in known)
    if unknown:
        raise UnknownPushTypeError(unknown, known)


def refused(response: httpx.Response) -> JMAPError:
    """What a handshake the server did not accept amounts to.

    The body was never read - httpx-ws closes the stream as it raises - so a
    problem document cannot be parsed; the status says enough.
    """
    return refusal_of(
        response.status_code,
        response.headers,
        b"",
        challenges=response.headers.get_list("www-authenticate"),
    ) or TransportError(
        f"the WebSocket handshake was answered with HTTP {response.status_code}, not 101"
    )


def closed(code: int, reason: str | None) -> JMAPError:
    """The error a server's close amounts to - see the module docstring."""
    detail = f"{code}: {reason}" if reason else str(code)
    if should_reauthenticate(code):
        return AuthenticationError(
            f"the server closed the WebSocket ({detail}) because the credentials that "
            f"opened it expired; reconnect with new ones rather than the same"
        )
    return TransportError(f"the server closed the WebSocket ({detail})")


class WebSocketClient:
    """JMAP over one WebSocket (RFC 8887): batches, answers and push together.

    Use it as a context manager on a connected :class:`~jmap.client.JMAPClient`,
    whose session names the endpoint and whose HTTP client carries the
    credentials. Pass ``push_state`` from an earlier connection to resume push
    from where it stopped.
    """

    __slots__ = ("_client", "_max_message_bytes", "_pending", "_protocol", "_socket", "_stack")

    def __init__(
        self,
        client: JMAPClient,
        *,
        push_state: str | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        websocket_url(client.session)
        self._client = client
        self._protocol = WebSocketProtocol(push_state=push_state)
        self._max_message_bytes = max_message_bytes
        self._pending: deque[StateChange] = deque()
        self._stack: ExitStack | None = None
        self._socket: WebSocketSession | None = None

    @property
    def capabilities(self) -> ActiveCapabilities:
        return self._client.capabilities

    @property
    def push_state(self) -> str | None:
        """The latest ``pushState``: keep it to resume push on a new connection."""
        return self._protocol.push_state

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:
        """Dial the endpoint and agree the ``jmap`` subprotocol."""
        from httpx_ws import HTTPXWSException, WebSocketUpgradeError, connect_ws

        stack = ExitStack()
        try:
            socket: WebSocketSession = stack.enter_context(
                connect_ws(
                    websocket_url(self._client.session),
                    self._client.http,
                    subprotocols=[SUBPROTOCOL],
                    max_message_size_bytes=self._max_message_bytes,
                )
            )
        except WebSocketUpgradeError as exc:
            raise refused(exc.response) from exc
        except HTTPXWSException as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        if socket.subprotocol != SUBPROTOCOL:
            stack.close()
            raise SubprotocolError(socket.subprotocol)
        self._stack, self._socket = stack, socket

    def close(self) -> None:
        stack, self._stack, self._socket = self._stack, None, None
        if stack is not None:
            stack.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- requests ----------------------------------------------------------- #
    def batch(self, *, extra_using: frozenset[str] = frozenset()) -> BatchContext:
        """Open a batch; its calls travel as one request over the socket."""
        batch = Batch(self.capabilities, default_account=self._client.default_account)
        return BatchContext(self, batch, extra_using)

    def execute(self, batch: Batch, *, extra_using: frozenset[str] = frozenset()) -> None:
        """Send ``batch``, possibly as several requests, and resolve its handles."""
        for request in batch.requests(extra_using=extra_using):
            response = self._exchange(request)
            batch.absorb(response)
            if session_is_stale(self._client.session, response.session_state):
                self._client.session_stale = True

    def _exchange(self, request: Request) -> Response:
        request_id = self._protocol.next_request_id()
        self._send(self._protocol.encode_request(request, request_id))
        while True:
            message = self._receive(timeout=self._client.http.timeout.read)
            if isinstance(message, StateChange):
                self._pending.append(message)
                continue
            self._protocol.settle(message.request_id)
            # One request is in flight, so an answer naming no id is its answer.
            if message.request_id not in (request_id, None):
                raise self._reject(
                    WebSocketProtocolError(f"an answer to {message.request_id!r}, not in flight")
                )
            if isinstance(message, RequestErrorMessage):
                raise message.error
            return message.response

    # -- push --------------------------------------------------------------- #
    @checked
    def enable_push(self, types: Sequence[str] | None = None, *, resume: bool = True) -> None:
        """Ask for state changes on this socket - of ``types``, or of every type.

        ``resume`` sends the last ``pushState`` back, so the server replays what
        changed while no socket was open.
        """
        check_push(self._client.session, self.capabilities, types or ())
        self._send(self._protocol.encode_push_enable(list(types) if types else None, resume=resume))

    def disable_push(self) -> None:
        self._send(self._protocol.encode_push_disable())

    def notifications(self) -> Generator[StateChange, None, None]:
        """Yield state changes as they arrive, ending when the server closes normally.

        Changes that arrived while a request waited for its answer come first.
        """
        while True:
            while self._pending:
                yield self._pending.popleft()
            try:
                message = self._receive(timeout=None)
            except _NormalCloseError:
                return
            if not isinstance(message, StateChange):
                raise self._reject(
                    WebSocketProtocolError("an answer arrived with no request in flight")
                )
            yield message

    # -- the socket --------------------------------------------------------- #
    def _require(self) -> WebSocketSession:
        if self._socket is None:
            raise RuntimeError("the WebSocket is not open; use it as a context manager")
        return self._socket

    def _send(self, text: str) -> None:
        from httpx_ws import HTTPXWSException

        try:
            self._require().send_text(text)
        except HTTPXWSException as exc:
            self.close()
            raise TransportError(str(exc) or type(exc).__name__) from exc

    def _receive(self, *, timeout: float | None) -> Incoming:
        from httpx_ws import HTTPXWSException, WebSocketDisconnect, WebSocketInvalidTypeReceived

        socket = self._require()
        try:
            text = socket.receive_text(timeout)
        except queue.Empty as exc:
            raise TransportError(f"no answer from the WebSocket within {timeout} seconds") from exc
        except WebSocketDisconnect as exc:
            self.close()
            if exc.code == CLOSE_NORMAL:
                raise _NormalCloseError from exc
            raise closed(exc.code, exc.reason) from exc
        except WebSocketInvalidTypeReceived as exc:
            raise self._reject(
                WebSocketProtocolError("a JMAP WebSocket carries text frames only")
            ) from exc
        except HTTPXWSException as exc:
            self.close()
            raise TransportError(str(exc) or type(exc).__name__) from exc
        try:
            return self._protocol.decode(text)
        except WebSocketProtocolError as exc:
            raise self._reject(exc) from None

    def _reject(self, error: WebSocketProtocolError) -> WebSocketProtocolError:
        """Close as RFC 8887 §4.3.1 allows for a frame the subprotocol forbids."""
        self._require().close(CLOSE_INVALID_PAYLOAD, str(error)[:120])
        self.close()
        return error

    def __repr__(self) -> str:
        state = "open" if self._socket is not None else "closed"
        return f"WebSocketClient({state}, push_state={self.push_state!r})"


class _NormalCloseError(TransportError):
    """The server closed with 1000. A notification stream ends; a request fails."""

    def __init__(self) -> None:
        super().__init__("the server closed the WebSocket (1000)")

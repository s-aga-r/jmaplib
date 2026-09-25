"""The async twin of :mod:`jmap.push.websocket_client`.

Unlike the sync client, this one reads in a background task, so several
requests may be in flight on the socket at once. RFC 8887 §4.3.2 lets the server
answer them in any order; the reader matches each answer to its request by id
and queues state changes for :meth:`AsyncWebSocketClient.notifications`.

Open and close it in the same task - it is an ``async with`` block - because the
reader lives in a task group tied to the task that opened it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Self

import anyio

from jmap._shell import session_is_stale
from jmap.aio import AsyncBatchContext
from jmap.batch import Batch
from jmap.core.errors import JMAPError, RequestError, TransportError
from jmap.models.arguments import checked
from jmap.models.push import StateChange
from jmap.push.websocket import (
    CLOSE_INVALID_PAYLOAD,
    SUBPROTOCOL,
    RequestErrorMessage,
    SubprotocolError,
    WebSocketProtocol,
    WebSocketProtocolError,
)
from jmap.push.websocket_client import (
    CLOSE_NORMAL,
    DEFAULT_MAX_MESSAGE_BYTES,
    check_push,
    closed,
    refused,
    websocket_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import TracebackType

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from httpx_ws import AsyncWebSocketSession

    from jmap.aio import AsyncJMAPClient
    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.core.request import Request
    from jmap.core.response import Response
    from jmap.push.websocket import Incoming, ResponseMessage


class AsyncWebSocketClient:
    """JMAP over one WebSocket, asynchronously; see :class:`WebSocketClient`."""

    __slots__ = (
        "_answers",
        "_changes",
        "_client",
        "_failure",
        "_max_message_bytes",
        "_protocol",
        "_socket",
        "_stack",
        "_waiting",
    )

    def __init__(
        self,
        client: AsyncJMAPClient,
        *,
        push_state: str | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        websocket_url(client.session)
        self._client = client
        self._protocol = WebSocketProtocol(push_state=push_state)
        self._max_message_bytes = max_message_bytes
        self._stack: AsyncExitStack | None = None
        self._socket: AsyncWebSocketSession | None = None
        #: Request id -> the event its caller waits on, and then its answer.
        self._waiting: dict[str, anyio.Event] = {}
        self._answers: dict[str, Response | RequestError] = {}
        self._changes: MemoryObjectReceiveStream[StateChange] | None = None
        #: Why the reader stopped; ``None`` while it runs or after a normal close.
        self._failure: JMAPError | None = None

    @property
    def capabilities(self) -> ActiveCapabilities:
        return self._client.capabilities

    @property
    def push_state(self) -> str | None:
        """The latest ``pushState``: keep it to resume push on a new connection."""
        return self._protocol.push_state

    # -- lifecycle ---------------------------------------------------------- #
    async def open(self) -> None:
        """Dial the endpoint, agree the ``jmap`` subprotocol and start reading."""
        from httpx_ws import HTTPXWSException, WebSocketUpgradeError, aconnect_ws

        stack = AsyncExitStack()
        try:
            try:
                socket: AsyncWebSocketSession = await stack.enter_async_context(
                    aconnect_ws(
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
                raise SubprotocolError(socket.subprotocol)
            sender, self._changes = anyio.create_memory_object_stream[StateChange](math.inf)
            group = await stack.enter_async_context(anyio.create_task_group())
            # Last in, first out: the reader is cancelled before the socket closes.
            stack.callback(group.cancel_scope.cancel)
            group.start_soon(self._read, socket, sender)
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._socket, self._failure = stack, socket, None

    async def close(self) -> None:
        stack, self._stack, self._socket = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- requests ----------------------------------------------------------- #
    def batch(self, *, extra_using: frozenset[str] = frozenset()) -> AsyncBatchContext:
        """Open a batch; its calls travel as one request over the socket."""
        batch = Batch(self.capabilities, default_account=self._client.default_account)
        return AsyncBatchContext(self, batch, extra_using)

    async def execute(self, batch: Batch, *, extra_using: frozenset[str] = frozenset()) -> None:
        """Send ``batch``, possibly as several requests, and resolve its handles."""
        for request in batch.requests(extra_using=extra_using):
            response = await self._exchange(request)
            batch.absorb(response)
            if session_is_stale(self._client.session, response.session_state):
                self._client.session_stale = True

    async def _exchange(self, request: Request) -> Response:
        request_id = self._protocol.next_request_id()
        arrived = self._waiting[request_id] = anyio.Event()
        try:
            await self._send(self._protocol.encode_request(request, request_id))
            with anyio.fail_after(self._client.http.timeout.read):
                await arrived.wait()
        except TimeoutError as exc:
            raise TransportError("no answer from the WebSocket in time") from exc
        finally:
            self._waiting.pop(request_id, None)
        answer = self._answers.pop(request_id, None)
        if answer is None:
            raise self._failure or TransportError("the WebSocket closed before answering")
        if isinstance(answer, RequestError):
            raise answer
        return answer

    # -- push --------------------------------------------------------------- #
    @checked
    async def enable_push(self, types: Sequence[str] | None = None, *, resume: bool = True) -> None:
        """Ask for state changes on this socket; see :meth:`WebSocketClient.enable_push`."""
        check_push(self._client.session, self.capabilities, types or ())
        await self._send(
            self._protocol.encode_push_enable(list(types) if types else None, resume=resume)
        )

    async def disable_push(self) -> None:
        await self._send(self._protocol.encode_push_disable())

    async def notifications(self) -> AsyncGenerator[StateChange, None]:
        """Yield state changes as they arrive, ending when the server closes normally."""
        if self._changes is None:
            raise RuntimeError("the WebSocket is not open; use it with `async with`")
        async for change in self._changes:
            yield change
        if self._failure is not None:
            raise self._failure

    # -- the socket --------------------------------------------------------- #
    async def _send(self, text: str) -> None:
        from httpx_ws import HTTPXWSException

        if self._stack is None:
            raise RuntimeError("the WebSocket is not open; use it with `async with`")
        if self._socket is None:
            raise self._failure or TransportError("the server closed the WebSocket")
        try:
            await self._socket.send_text(text)
        except HTTPXWSException as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc

    async def _read(
        self, socket: AsyncWebSocketSession, changes: MemoryObjectSendStream[StateChange]
    ) -> None:
        """Route every message until the socket ends, then wake whoever still waits.

        It never raises: an error escaping the task group would reach the caller
        of ``close()`` wrapped in an ExceptionGroup, far from where it happened.
        """
        try:
            while True:
                message = await self._receive(socket)
                if message is None:
                    return
                if isinstance(message, StateChange):
                    changes.send_nowait(message)
                    continue
                self._protocol.settle(message.request_id)
                self._deliver(message)
        finally:
            changes.close()
            self._socket = None
            for event in self._waiting.values():
                event.set()

    async def _receive(self, socket: AsyncWebSocketSession) -> Incoming | None:
        """The next message, or ``None`` once the socket has ended for good."""
        from httpx_ws import HTTPXWSException, WebSocketDisconnect, WebSocketInvalidTypeReceived

        try:
            text = await socket.receive_text()
            return self._protocol.decode(text)
        except WebSocketDisconnect as exc:
            if exc.code != CLOSE_NORMAL:
                self._failure = closed(exc.code, exc.reason)
        except (WebSocketInvalidTypeReceived, WebSocketProtocolError) as exc:
            self._failure = (
                exc
                if isinstance(exc, WebSocketProtocolError)
                else WebSocketProtocolError("a JMAP WebSocket carries text frames only")
            )
            await socket.close(CLOSE_INVALID_PAYLOAD, str(self._failure)[:120])
        except HTTPXWSException as exc:
            self._failure = TransportError(str(exc) or type(exc).__name__)
        return None

    def _deliver(self, message: ResponseMessage | RequestErrorMessage) -> None:
        """Hand an answer to the request it belongs to.

        An answer naming no id - a request the server could not parse far enough
        to read one from - can only be placed when a single request waits. One
        for a request nobody waits on any more, after a timeout, is dropped.
        """
        request_id = message.request_id
        if request_id is None and len(self._waiting) == 1:
            request_id = next(iter(self._waiting))
        event = self._waiting.get(request_id) if request_id is not None else None
        if request_id is None or event is None:
            return
        self._answers[request_id] = (
            message.error if isinstance(message, RequestErrorMessage) else message.response
        )
        event.set()

    def __repr__(self) -> str:
        state = "open" if self._socket is not None else "closed"
        return f"AsyncWebSocketClient({state}, push_state={self.push_state!r})"

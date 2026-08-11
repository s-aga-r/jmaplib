"""Holding an event source connection open, and reconnecting when it drops.

Twin shells over :mod:`jmap.push.eventsource`, which does the parsing. What lives
here is the connection lifecycle and four decisions inside it:

**Reconnection resumes, it does not restart.** ``Last-Event-ID`` goes back on
every reconnect so the server can replay what was missed (RFC 8620 §7.3). Drop it
and the client silently starts from *now*, losing everything that happened while
it was away - a gap it has no way to detect afterwards.

**The delay comes from the server when it offers one.** A ``retry:`` field is the
server asking to be dialled less often; ignoring it turns a server under load into
a server under load being hammered.

**``closeafter=state`` ending the response is success.** The stream stops after one
state event because that is what was asked for - some proxies buffer a stream
until it completes, and would otherwise hold every notification back indefinitely.
The loop reconnects rather than treating it as a failure.

**The read timeout comes from ``ping``, not from the HTTP client.** Every other
request in this library wants a read deadline; an event source is idle by design,
so inheriting one caps how long a *healthy* stream may wait. See
:attr:`PushListener.read_timeout`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import anyio
import httpx

from jmap._shell import problem_of
from jmap.core.errors import AuthenticationError, TransportError
from jmap.push.eventsource import (
    CLOSE_AFTER_NO,
    CLOSE_AFTER_STATE,
    EventStream,
    event_source_url,
    stream_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from jmap.aio import AsyncJMAPClient
    from jmap.client import JMAPClient
    from jmap.models.push import StateChange
    from jmap.push.eventsource import Ping

#: RFC 8620 §7.3 puts no floor on ``retry:``. A server asking for zero would spin
#: the loop, so a reconnect never happens faster than this.
MIN_RECONNECT_SECONDS = 0.1

#: Used when the server has never sent a ``retry:``.
DEFAULT_RECONNECT_SECONDS = 3.0

#: Added to the ping interval before a silent connection is declared dead. A
#: server promising a ping "every n seconds" is not promising a stopwatch, and
#: hanging up on it one second late costs a reconnect for nothing.
PING_TIMEOUT_SLACK = 10.0


class PushListener:
    """I/O-free connection state, shared by both shells.

    The resume cursor is the only thing that has to survive a drop, so it lives
    here rather than inside any one connection.
    """

    __slots__ = ("_close_after_state", "_last_event_id", "_ping", "_retry", "_url")

    def __init__(self, url: str, *, close_after_state: bool, ping: int = 0) -> None:
        self._url = url
        self._close_after_state = close_after_state
        self._ping = ping
        self._last_event_id = ""
        self._retry: int | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def read_timeout(self) -> float | None:
        """How long this connection may stay silent before it is called dead.

        The HTTP client's own read timeout is exactly wrong for an event source
        and dangerously plausible-looking: it caps how long a *perfectly healthy*
        connection may wait for the next change. httpx defaults it to five
        seconds, so an unconfigured client cannot hold a stream open past five
        idle seconds - which is not a push client at all, and fails as
        ``TransportError: timed out`` that reads like a server fault.

        ``ping`` is what makes any finite answer defensible. RFC 8620 §7.3 has
        the client name an interval and the server send an empty event on it, so
        silence beyond that interval really is evidence of a dead connection.
        Without a ping there is no such promise and no sound basis for a
        deadline, so there is none: waiting indefinitely is what "tell me when
        something changes" means. A caller who wants to bound that instead should
        ask for pings, which is the mechanism the protocol provides for it.
        """
        if self._ping <= 0:
            return None
        return self._ping + PING_TIMEOUT_SLACK

    @property
    def last_event_id(self) -> str:
        return self._last_event_id

    @property
    def headers(self) -> dict[str, str]:
        return stream_headers(self._last_event_id)

    def new_stream(self) -> EventStream:
        return EventStream.open(
            close_after_state=self._close_after_state,
            last_event_id=self._last_event_id,
        )

    def absorb(self, stream: EventStream) -> None:
        """Carry a finished connection's cursor and retry hint into the next one."""
        self._last_event_id = stream.last_event_id
        if stream.retry is not None:
            self._retry = stream.retry

    def delay(self) -> float:
        """How long to wait before redialling."""
        if self._retry is None:
            return DEFAULT_RECONNECT_SECONDS
        return max(MIN_RECONNECT_SECONDS, self._retry / 1000)

    def __repr__(self) -> str:
        return f"PushListener(last_event_id={self._last_event_id!r})"


def _listener(
    client: JMAPClient | AsyncJMAPClient,
    *,
    types: tuple[str, ...] | None,
    close_after_state: bool,
    ping: int,
) -> PushListener:
    url = event_source_url(
        client.session,
        types=types,
        close_after=CLOSE_AFTER_STATE if close_after_state else CLOSE_AFTER_NO,
        ping=ping,
        push_types=client.capabilities.push_types(),
    )
    return PushListener(url, close_after_state=close_after_state, ping=ping)


def stream_timeout(base: httpx.Timeout, read: float | None) -> httpx.Timeout:
    """The client's timeout with its read deadline replaced.

    Only the read leg changes: connecting to the event source should fail as
    quickly as connecting to anything else, and it is the *waiting* that an
    event source does differently. ``httpx.Timeout`` refuses to take an existing
    instance alongside overrides, so the legs are copied across by hand.
    """
    return httpx.Timeout(connect=base.connect, read=read, write=base.write, pool=base.pool)


def _check(response: httpx.Response, body: bytes) -> None:
    """Turn a failed connection attempt into the errors the API path raises.

    A 401 here means the same thing it means anywhere else, and a caller that has
    one ``except AuthenticationError`` should not need a second one for push.
    """
    if response.status_code == 401:
        raise AuthenticationError(
            "the server rejected these credentials",
            challenges=tuple(response.headers.get_list("www-authenticate")),
        )
    problem = problem_of(response.status_code, response.headers, body)
    if problem is None:  # pragma: no cover - problem_of only answers None for a 2xx
        return
    raise problem


class EventSourceClient:
    """A synchronous JMAP event source connection (RFC 8620 §7.3)."""

    __slots__ = ("_client", "_listener")

    def __init__(
        self,
        client: JMAPClient,
        *,
        types: tuple[str, ...] | None = None,
        close_after_state: bool = False,
        ping: int = 0,
    ) -> None:
        self._client = client
        self._listener = _listener(
            client, types=types, close_after_state=close_after_state, ping=ping
        )

    @property
    def last_event_id(self) -> str:
        """The resume cursor. Survives reconnects; unmoved by pings."""
        return self._listener.last_event_id

    def events(self) -> Generator[StateChange | Ping, None, None]:
        """Yield events from a *single* connection, ending when it does.

        Use :meth:`listen` unless you want to handle reconnection yourself.

        Typed as a generator rather than an iterator for the same reason
        :meth:`listen` is: a caller who stops early - after the first event, or
        on a match - needs ``close()`` to hang up. Without it the connection
        stays open until the object is collected, which on a long-lived stream
        means holding a socket for no reason.
        """
        stream = self._listener.new_stream()
        try:
            with self._client.http.stream(
                "GET",
                self._listener.url,
                headers=self._listener.headers,
                timeout=stream_timeout(self._client.http.timeout, self._listener.read_timeout),
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    _check(response, response.read())
                for chunk in response.iter_bytes():
                    yield from stream.feed(chunk)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        finally:
            # Even a connection that died mid-event has usually advanced the
            # cursor, and discarding it would replay everything already seen.
            self._listener.absorb(stream)

    def listen(self) -> Generator[StateChange | Ping, None, None]:
        """Yield events indefinitely, reconnecting whenever the stream ends.

        Resumes from ``Last-Event-ID`` each time, so a drop costs latency rather
        than data. Typed as a generator rather than an iterator because a caller
        that stops listening needs ``close()`` to end the connection - an
        infinite iterator with no way to stop it is a leak.
        """
        while True:
            yield from self.events()
            time.sleep(self._listener.delay())


class AsyncEventSourceClient:
    """The async twin of :class:`EventSourceClient`."""

    __slots__ = ("_client", "_listener")

    def __init__(
        self,
        client: AsyncJMAPClient,
        *,
        types: tuple[str, ...] | None = None,
        close_after_state: bool = False,
        ping: int = 0,
    ) -> None:
        self._client = client
        self._listener = _listener(
            client, types=types, close_after_state=close_after_state, ping=ping
        )

    @property
    def last_event_id(self) -> str:
        return self._listener.last_event_id

    async def events(self) -> AsyncGenerator[StateChange | Ping, None]:
        """Yield events from a single connection, ending when it does.

        A generator rather than an iterator so a caller who stops early can
        ``aclose()`` the connection; see :meth:`EventSourceClient.events`.
        """
        stream = self._listener.new_stream()
        try:
            async with self._client.http.stream(
                "GET",
                self._listener.url,
                headers=self._listener.headers,
                timeout=stream_timeout(self._client.http.timeout, self._listener.read_timeout),
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    _check(response, await response.aread())
                async for chunk in response.aiter_bytes():
                    for event in stream.feed(chunk):
                        yield event
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        finally:
            self._listener.absorb(stream)

    async def listen(self) -> AsyncGenerator[StateChange | Ping, None]:
        """Yield events indefinitely, reconnecting whenever the stream ends."""
        while True:
            async for event in self.events():
                yield event
            # anyio rather than asyncio.sleep, so this works under trio too.
            await anyio.sleep(self._listener.delay())

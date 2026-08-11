"""Parsing ``text/event-stream`` (the HTML server-sent events format).

I/O-free on purpose. Everything hard about server-sent events is in the parsing -
incremental decoding across chunk boundaries, three legal line terminators, the
resumption cursor - and none of it needs a socket to test.

The format is small but has four rules that are easy to get subtly wrong, and each
one is silent when you do:

**Lines end with LF, CR, or CRFL, and a chunk may split any of them.** A CR
arriving at the end of one read and an LF at the start of the next is *one*
terminator, not two, so the parser cannot decide what a trailing CR means until it
sees the next byte.

**``data`` accumulates.** Several ``data:`` lines in one event are joined with
newlines, and a single trailing newline is stripped at dispatch. A parser that
keeps only the last line silently truncates every multi-line payload.

**An event with no data is not dispatched at all.** That is what makes a bare
``id:`` line a legal way to move the cursor without delivering anything.

**The last event id persists across events.** It is *not* reset between them, so
an event that carries no ``id:`` inherits the previous one. This is what makes the
JMAP ping rule work: RFC 8620 §7.3 says a ping MUST NOT set a new event id, so
pings leave the resumption cursor exactly where the last real event left it. A
client that instead tracked "the last thing I received" would resume from a ping
and lose every change that arrived before it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The event type assumed when a stream sends ``data:`` with no ``event:`` line.
DEFAULT_EVENT_TYPE = "message"

#: A NUL in an ``id`` field means the field is ignored entirely (HTML spec).
_NUL = "\x00"


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One dispatched event."""

    #: ``event:``, or ``message`` when the stream sent none.
    type: str = DEFAULT_EVENT_TYPE
    #: The joined ``data:`` lines, with one trailing newline removed.
    data: str = ""
    #: The event id *in force* when this was dispatched - which may have been set
    #: by an earlier event, since the id persists until replaced.
    last_event_id: str = ""
    #: ``retry:``, in milliseconds, if this event carried one.
    retry: int | None = None


@dataclass(slots=True)
class SSEParser:
    """Incrementally turns ``text/event-stream`` bytes into events.

    Feed it whatever arrives; it holds partial lines until they are complete.
    """

    #: Survives across events (see the module docstring), and is what a reconnect
    #: sends back as ``Last-Event-ID``.
    last_event_id: str = ""
    #: The server's requested reconnection delay, in milliseconds.
    retry: int | None = None

    _buffer: str = ""
    _data: list[str] = field(default_factory=lambda: [])
    _event_type: str = ""
    _pending_cr: bool = False

    def feed(self, chunk: str) -> Iterator[ServerSentEvent]:
        """Consume a chunk of the stream, yielding whatever events complete."""
        # A CR held over from the previous chunk: if this one starts with LF the
        # two are a single terminator, otherwise the CR already ended its line.
        if self._pending_cr:
            self._pending_cr = False
            if chunk.startswith("\n"):
                chunk = chunk[1:]
        self._buffer += chunk

        while True:
            line, terminator, rest = _split_line(self._buffer)
            if terminator is None:
                break
            if terminator == "\r" and not rest:
                # Cannot tell yet whether the next chunk starts with LF, which
                # would make this one CRLF rather than a bare CR.
                self._pending_cr = True
            self._buffer = rest
            event = self._consume(line)
            if event is not None:
                yield event

    def feed_bytes(self, chunk: bytes) -> Iterator[ServerSentEvent]:
        """Consume raw bytes. The format is always UTF-8."""
        return self.feed(chunk.decode())

    def _consume(self, line: str) -> ServerSentEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            # A comment. Servers use these as keep-alives; they dispatch nothing.
            return None
        name, _, value = line.partition(":")
        # Exactly one leading space is part of the framing, not the value.
        self._field(name, value.removeprefix(" "))
        return None

    def _field(self, name: str, value: str) -> None:
        if name == "event":
            self._event_type = value
        elif name == "data":
            self._data.append(value)
        elif name == "id" and _NUL not in value:
            self._last_id_is(value)
        elif name == "retry" and value.isdigit():
            self.retry = int(value)
        # Any other field name is ignored, per the spec - which is what lets the
        # format be extended without breaking existing parsers.

    def _last_id_is(self, value: str) -> None:
        self.last_event_id = value

    def _dispatch(self) -> ServerSentEvent | None:
        """Finish the current event, if there is one."""
        if not self._data:
            # No data means nothing is delivered, but any id: line has already
            # moved the cursor - which is how a stream sends a bare checkpoint.
            self._event_type = ""
            return None
        event = ServerSentEvent(
            type=self._event_type or DEFAULT_EVENT_TYPE,
            # Lines are joined with newlines and one trailing newline is dropped;
            # keeping it would append a phantom blank line to every payload.
            data="\n".join(self._data),
            last_event_id=self.last_event_id,
            retry=self.retry,
        )
        self._data = []
        self._event_type = ""
        return event


def _split_line(buffer: str) -> tuple[str, str | None, str]:
    """Split off one complete line, returning ``(line, terminator, rest)``.

    ``terminator`` is ``None`` when the buffer holds no complete line yet.
    """
    index = _first_terminator(buffer)
    if index is None:
        return "", None, buffer
    if buffer.startswith("\r\n", index):
        return buffer[:index], "\r\n", buffer[index + 2 :]
    return buffer[:index], buffer[index], buffer[index + 1 :]


def _first_terminator(buffer: str) -> int | None:
    carriage = buffer.find("\r")
    newline = buffer.find("\n")
    if carriage == -1 and newline == -1:
        return None
    if carriage == -1:
        return newline
    if newline == -1:
        return carriage
    return min(carriage, newline)

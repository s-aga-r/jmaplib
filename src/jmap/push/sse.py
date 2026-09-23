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

import codecs
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from jmap.core.errors import JMAPError

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The event type assumed when a stream sends ``data:`` with no ``event:`` line.
DEFAULT_EVENT_TYPE = "message"

#: A NUL in an ``id`` field means the field is ignored entirely (HTML spec).
_NUL = "\x00"

#: The most characters one event (its pending line included) may accumulate.
#: A JMAP StateChange is a few hundred bytes; this is three orders of magnitude
#: of headroom. Without a bound, a hostile server streams one endless ``data:``
#: line - or endless terminated ones with no blank line - into a connection
#: designed to stay open for days, and the client grows until the OS kills it.
MAX_EVENT_CHARS = 1 << 22

#: ``retry:`` values longer than this are nonsense, not pacing - and a huge
#: digit string would trip CPython's int-conversion limit besides.
_MAX_RETRY_DIGITS = 15


class EventOverflowError(JMAPError):
    """The stream accumulated more than :data:`MAX_EVENT_CHARS` for one event."""

    def __init__(self, buffered: int) -> None:
        self.buffered = buffered
        super().__init__(
            f"the event source buffered {buffered} characters without completing an "
            f"event (limit {MAX_EVENT_CHARS}); a conforming JMAP stream never comes "
            f"close, so this connection is broken or hostile"
        )


def _new_decoder() -> codecs.IncrementalDecoder:
    # Incremental because the transport chunks at arbitrary byte boundaries: a
    # multi-byte character split across two reads must decode as one character,
    # not crash. `replace` because the WHATWG stream spec decodes with U+FFFD
    # substitution - strict decoding turns one bad byte into a dead listener.
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


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
    _data_chars: int = 0
    _event_type: str = ""
    _pending_cr: bool = False
    _at_stream_start: bool = True
    _decoder: codecs.IncrementalDecoder = field(default_factory=_new_decoder)

    def feed(self, chunk: str) -> Iterator[ServerSentEvent]:
        """Consume a chunk of the stream, yielding whatever events complete."""
        # A CR held over from the previous chunk: if this one starts with LF the
        # two are a single terminator, otherwise the CR already ended its line.
        if self._pending_cr:
            self._pending_cr = False
            if chunk.startswith("\n"):
                chunk = chunk[1:]
        if self._at_stream_start:
            # One leading U+FEFF is a byte-order mark, not the first field name.
            self._at_stream_start = False
            chunk = chunk.removeprefix("\ufeff")
        buffer = self._buffer + chunk
        if len(buffer) + self._data_chars > MAX_EVENT_CHARS:
            raise EventOverflowError(len(buffer) + self._data_chars)

        # Scanned by offset rather than re-sliced per line: slicing copied the
        # whole remaining buffer once per extracted line, which for a large
        # multi-chunk event is quadratic. The buffer is compacted once per
        # completed event (so an abandoned generator never replays one) and
        # once when the chunk is exhausted.
        position = 0
        length = len(buffer)
        while position < length:
            index = _next_terminator(buffer, position)
            if index is None:
                break
            line = buffer[position:index]
            if buffer.startswith("\r\n", index):
                position = index + 2
            else:
                if buffer[index] == "\r" and index + 1 == length:
                    # Cannot tell yet whether the next chunk starts with LF,
                    # which would make this one CRLF rather than a bare CR.
                    self._pending_cr = True
                position = index + 1
            event = self._consume(line)
            if event is not None:
                self._buffer = buffer[position:]
                yield event
        self._buffer = buffer[position:]

    def feed_bytes(self, chunk: bytes) -> Iterator[ServerSentEvent]:
        """Consume raw bytes. The format is always UTF-8, decoded incrementally
        so a character split across transport chunks survives."""
        return self.feed(self._decoder.decode(chunk))

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
            # The value plus the newline that joins it to the next, which is what
            # the spec's data buffer holds. Counting the value alone let endless
            # bare `data` lines grow this list at a tally of zero.
            self._data_chars += len(value) + 1
        elif name == "id" and _NUL not in value:
            self._last_id_is(value)
        elif name == "retry" and _is_ascii_digits(value):
            # isascii() matters: '²'.isdigit() is true but int('²') raises, and
            # a Unicode digit crashing the parser kills the whole listener.
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
        self._data_chars = 0
        self._event_type = ""
        return event


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and len(value) <= _MAX_RETRY_DIGITS and value.isascii() and value.isdigit()


#: A line ends at CR or LF, whichever comes first. One pattern rather than a
#: search for each: a search for CR alone runs to the end of the buffer on every
#: line of a stream that never sends one - any real server's - which made
#: splitting a large chunk quadratic.
_LINE_ENDING: Final = re.compile(r"[\r\n]")


def _next_terminator(buffer: str, start: int) -> int | None:
    """The index of the first ``\\r`` or ``\\n`` at or after ``start``."""
    match = _LINE_ENDING.search(buffer, start)
    return match.start() if match is not None else None

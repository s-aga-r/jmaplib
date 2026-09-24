"""The JMAP event source (RFC 8620 §7.3).

A long-running authenticated GET that never finishes: the server appends a
``state`` event whenever something changes. It needs no third party and no
registered URL, which makes it the right choice anywhere a process can hold a
connection open - and the wrong one on a phone, which is what PushSubscription is
for.

Everything in this module except :class:`EventSourceClient` and its async twin is
I/O-free, so the interesting rules are testable without a socket:

**``ping`` is not a cursor.** RFC 8620 §7.3 says a ping event MUST NOT set a new
event id. It says the connection is alive, nothing more. Resuming from a ping -
which is what happens if a client tracks "the last event I saw" rather than the
last event *id* - silently skips every change that arrived before it.

**``closeafter=state`` ends the response.** Not an error and not a lost
connection: the server is doing what was asked, because some proxies buffer a
stream until it completes and would otherwise hold every notification back
indefinitely. The client reconnects.

**The types list is validated against the session.** A type the server does not
know is not an error it reports - it simply never pushes it, and the client waits
forever for notifications that were never going to come.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Self

from pydantic import ValidationError

from jmap.core.errors import JMAPError
from jmap.core.ijson import loads
from jmap.core.narrow import as_object, is_object
from jmap.core.session import Session
from jmap.core.uritemplate import expand
from jmap.models.arguments import UnsignedInt, checked
from jmap.models.base import validation_summary
from jmap.models.push import StateChange
from jmap.push.sse import DEFAULT_EVENT_TYPE, SSEParser

if TYPE_CHECKING:
    from collections.abc import Iterator

#: RFC 8620 §7.3 event names.
EVENT_STATE = "state"
EVENT_PING = "ping"

#: ``closeafter`` values.
CLOSE_AFTER_STATE: Final = "state"
CLOSE_AFTER_NO: Final = "no"

#: The wildcard accepted by the ``types`` template variable.
ALL_TYPES = "*"

#: RFC 8620 §7.3 bounds what a server may clamp a ping interval to: a minimum no
#: higher than 30 and a maximum no lower than 300. Anything inside that range is
#: therefore honoured verbatim by every conformant server.
MIN_PORTABLE_PING = 30
MAX_PORTABLE_PING = 300

#: An id that can go back as the ``Last-Event-ID`` header: an HTTP field value
#: (RFC 9110 §5.5) - visible characters, with spaces or tabs only between them -
#: and ASCII, since httpx will not encode anything else.
_RESUMABLE_ID: Final = re.compile(r"\A(?:[\x21-\x7e]+(?:[ \t]+[\x21-\x7e]+)*)?\Z")


class EventSourceError(JMAPError):
    """The event source stream could not be used as asked."""


class UnknownPushTypeError(EventSourceError):
    """A requested type is not one this server can push.

    Raised locally because the failure mode otherwise is silence: the server
    simply never sends notifications for a type it does not have, and the client
    cannot tell that apart from nothing having changed.
    """

    def __init__(self, unknown: tuple[str, ...], known: frozenset[str]) -> None:
        self.unknown = unknown
        self.known = known
        super().__init__(
            f"this server cannot push {', '.join(unknown)}; it offers "
            f"{', '.join(sorted(known)) or 'no types at all'}"
        )


@dataclass(frozen=True, slots=True)
class Ping:
    """A keep-alive (RFC 8620 §7.3).

    Carries the interval the server actually settled on, which may not be the one
    requested. It deliberately does *not* carry an event id: a ping is not a point
    in the change stream and must never be resumed from.
    """

    interval: int | None = None


@checked
def event_source_url(
    session: Session,
    *,
    types: Sequence[str] | None = None,
    close_after: Literal["state", "no"] = CLOSE_AFTER_NO,
    ping: UnsignedInt = 0,
    push_types: frozenset[str] | None = None,
) -> str:
    """Expand the session's ``eventSourceUrl`` template (RFC 8620 §7.3).

    ``types=None`` means every type, sent as the literal ``*``. Passing an
    explicit list is checked against ``push_types`` when one is given - see
    :class:`UnknownPushTypeError` for why silence is the alternative.

    The arguments are checked first, so a type name passed on its own - which
    joined into ``E,m,a,i,l`` - or a negative ping fails here.
    """
    if types is None:
        selector = ALL_TYPES
    else:
        if push_types is not None:
            unknown = tuple(name for name in types if name not in push_types)
            if unknown:
                raise UnknownPushTypeError(unknown, push_types)
        selector = ",".join(types)
    return expand(
        session.event_source_url,
        types=selector,
        closeafter=close_after,
        ping=str(ping),
    )


def parse_event(event_type: str, data: str) -> StateChange | Ping | None:
    """Interpret one server-sent event as JMAP.

    Returns ``None`` for anything unrecognised rather than raising: RFC 8620 §7.3
    names two event types and a stream is free to carry others, and a client that
    dies on an unknown event cannot be extended without breaking it.
    """
    if event_type == EVENT_STATE:
        try:
            return StateChange.model_validate(_json_object(data))
        except ValidationError as exc:
            raise EventSourceError(
                f"state event is not a StateChange: {validation_summary(exc)}"
            ) from exc
    if event_type == EVENT_PING:
        payload = _json_object(data)
        interval = payload.get("interval")
        # bool is an int subclass; `interval: true` must not become Ping(True).
        genuine = isinstance(interval, int) and not isinstance(interval, bool)
        return Ping(interval=interval if genuine else None)
    return None


def _json_object(data: str) -> dict[str, Any]:
    try:
        # The str goes straight in: encoding it first pays a full UTF-8 pass
        # per event for bytes that came from a decode moments earlier.
        decoded = loads(data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise EventSourceError(f"event payload was not JSON: {data[:80]!r}") from exc
    if not is_object(decoded):
        raise EventSourceError(f"event payload was not a JSON object: {data[:80]!r}")
    return as_object(decoded)


@dataclass(slots=True)
class EventStream:
    """Turns a byte stream into JMAP push events, tracking the resume cursor.

    Holds no connection. Feed it whatever the transport read; it yields
    :class:`~jmap.models.push.StateChange` and :class:`Ping` values and keeps
    :attr:`last_event_id` current so a reconnect can resume.
    """

    parser: SSEParser
    #: Set once a ``state`` event arrives on a ``closeafter=state`` connection, so
    #: a caller can tell "the server finished as instructed" from "the connection
    #: dropped".
    closed_after_state: bool = False
    #: Whether the server was asked to end the response after a state event.
    close_after_state: bool = False
    _cursor: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self._track()

    @classmethod
    def open(cls, *, close_after_state: bool = False, last_event_id: str = "") -> Self:
        return cls(
            parser=SSEParser(last_event_id=last_event_id),
            close_after_state=close_after_state,
        )

    @property
    def last_event_id(self) -> str:
        """What a reconnect should send as ``Last-Event-ID``.

        Unmoved by pings, which is the point - see the module docstring. Also
        unmoved by an id that cannot be sent as that header at all: adopting one
        left every later reconnect failing while its request was built, for
        good. The last id that can be sent is resumed from instead, which costs
        a short replay of events already seen.
        """
        return self._cursor

    def _track(self) -> None:
        candidate = self.parser.last_event_id
        if _RESUMABLE_ID.match(candidate):
            self._cursor = candidate

    @property
    def retry(self) -> int | None:
        """The server's requested reconnection delay, in milliseconds."""
        return self.parser.retry

    def feed(self, chunk: bytes) -> Iterator[StateChange | Ping]:
        """Consume transport bytes, yielding whatever events they complete."""
        for event in self.parser.feed_bytes(chunk):
            self._track()
            parsed = parse_event(event.type or DEFAULT_EVENT_TYPE, event.data)
            if parsed is None:
                continue
            if isinstance(parsed, StateChange) and self.close_after_state:
                self.closed_after_state = True
            yield parsed
        # A bare `id:` checkpoint moves the parser's cursor without yielding.
        self._track()


def resume_headers(last_event_id: str) -> dict[str, str]:
    """The header that makes a reconnect a resumption rather than a restart.

    Without it the server has no way to know what the client already saw, and
    RFC 8620 §7.3's "SHOULD send these changes immediately on connection" cannot
    happen - the client silently starts from now and loses the gap.
    """
    return {"Last-Event-ID": last_event_id} if last_event_id else {}


def stream_headers(last_event_id: str = "") -> dict[str, str]:
    """Request headers for an event source connection."""
    return {"Accept": "text/event-stream", **resume_headers(last_event_id)}

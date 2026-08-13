"""JMAP over WebSocket (RFC 8887).

One socket carries requests, responses, request-level errors and push
notifications, all as text frames, all distinguished only by an ``@type`` tag. That
makes the protocol layer here mostly a demultiplexer - and it is I/O-free, so
every rule below is testable without a socket.

Three things differ from the HTTP binding and each one is a correctness issue
rather than a detail:

**Responses may arrive out of order.** §4.3.2 says so explicitly, which is why a
request carries a client-chosen ``id`` and the response echoes it back as
``requestId``. Assuming FIFO works right up until the server answers a cheap call
before an expensive one, and then silently pairs every response with the wrong
request.

**A request-level error is a message, not a status code.** There is no HTTP status
to key off, so ``RequestError`` arrives on the same socket as everything else and
carries the ``requestId`` of whatever failed - or ``null`` when the server could
not parse far enough to tell.

**``pushState`` is the cheap reconnect.** §4.3.5.1's token encodes the whole
visible server state, so sending the last one back on reconnect gets everything
missed in a single exchange instead of a ``/changes`` call per type. Servers need
not support it; when absent, the per-type walk is the only option.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jmap.core.errors import JMAPError, RequestError
from jmap.core.ijson import dumps, loads
from jmap.core.narrow import as_object, is_object
from jmap.core.response import Response
from jmap.models.push import (
    TYPE_PUSH_DISABLE,
    TYPE_PUSH_ENABLE,
    TYPE_REQUEST,
    TYPE_REQUEST_ERROR,
    TYPE_RESPONSE,
    TYPE_STATE_CHANGE,
    StateChange,
    WebSocketPushDisable,
    WebSocketPushEnable,
)

if TYPE_CHECKING:
    from jmap.core.request import Request

#: The subprotocol name that must appear in ``Sec-WebSocket-Protocol`` on both
#: sides. A handshake the server answers without it is not a JMAP connection.
SUBPROTOCOL = "jmap"

# RFC 6455 §7.4.1 close codes, with the meanings RFC 8887 §4.1 and §4.3.1 give
# them. The distinction that matters: 1008 is about *credentials*, so redialling
# with the same ones loops forever.
CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_INVALID_PAYLOAD = 1007
CLOSE_POLICY_VIOLATION = 1008


class WebSocketProtocolError(JMAPError):
    """A frame arrived that the JMAP subprotocol does not allow."""


class SubprotocolError(JMAPError):
    """The server did not agree to the ``jmap`` subprotocol.

    Raised rather than proceeding: without the agreement the peer is speaking
    something else, and the first frame would be misparsed rather than rejected.
    """

    def __init__(self, negotiated: str | None) -> None:
        self.negotiated = negotiated
        super().__init__(
            f"server negotiated {negotiated!r} rather than the {SUBPROTOCOL!r} "
            f"subprotocol required by RFC 8887 §4.2"
        )


def should_reauthenticate(close_code: int | None) -> bool:
    """Whether a close means "get new credentials" rather than "try again".

    RFC 8887 §4.1: the server may close with 1008 when the credentials that
    authenticated the handshake expire. Reconnecting with the same ones produces
    the same close, forever - so this is the one code that must not be retried
    blindly.
    """
    return close_code == CLOSE_POLICY_VIOLATION


@dataclass(frozen=True, slots=True)
class ResponseMessage:
    """A JMAP Response, tagged with the request it answers."""

    response: Response
    #: ``None`` when the request carried no id, which is legal but leaves the
    #: client unable to correlate anything.
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RequestErrorMessage:
    """A request-level failure, delivered as a message rather than a status."""

    error: RequestError
    #: ``None`` when the server could not parse the request far enough to find
    #: its id - a malformed frame, typically.
    request_id: str | None = None


#: Everything a client may receive on a JMAP WebSocket (§4.3).
Incoming = ResponseMessage | RequestErrorMessage | StateChange


@dataclass(slots=True)
class WebSocketProtocol:
    """Frames outgoing messages and classifies incoming ones. No I/O.

    Holds the two pieces of state a connection needs: the request-id counter used
    for correlation, and the latest ``pushState`` for resuming after a drop.
    """

    #: The most recent ``pushState`` seen, or ``None`` if the server sends none.
    push_state: str | None = None
    _counter: int = 0
    #: Request ids sent but not yet answered.
    outstanding: set[str] = field(default_factory=lambda: set())

    def next_request_id(self) -> str:
        self._counter += 1
        return f"r{self._counter}"

    def encode_request(self, request: Request, request_id: str | None = None) -> str:
        """Frame a JMAP request, returning the text to send and recording its id."""
        identifier = request_id or self.next_request_id()
        body = request.to_wire()
        body["@type"] = TYPE_REQUEST
        body["id"] = identifier
        self.outstanding.add(identifier)
        return dumps(body)

    def encode_push_enable(
        self, data_types: list[str] | None = None, *, resume: bool = True
    ) -> str:
        """Frame a ``WebSocketPushEnable``.

        ``resume`` sends the last ``pushState`` back, which asks the server for
        everything that changed while the socket was down. It is the difference
        between a reconnect costing one exchange and costing a ``/changes`` call
        per type.
        """
        body: dict[str, Any] = {"dataTypes": data_types}
        if resume and self.push_state is not None:
            body["pushState"] = self.push_state
        return dumps(WebSocketPushEnable.model_validate(body).to_wire())

    def encode_push_disable(self) -> str:
        return dumps(WebSocketPushDisable().to_wire())

    def decode(self, text: str) -> Incoming:
        """Classify one received text frame.

        Raises :class:`WebSocketProtocolError` for anything the subprotocol does
        not allow, which §4.3.1 lets a client answer with a 1007 close.
        """
        decoded = loads(text)
        if not is_object(decoded):
            raise WebSocketProtocolError("a JMAP WebSocket message must be a JSON object")
        body = as_object(decoded)
        tag = body.get("@type")

        if tag == TYPE_RESPONSE:
            return ResponseMessage(response=Response.from_wire(body), request_id=_identifier(body))
        if tag == TYPE_STATE_CHANGE:
            change = StateChange.model_validate(body)
            if change.push_state is not None:
                self.push_state = change.push_state
            return change
        if tag == TYPE_REQUEST_ERROR:
            return RequestErrorMessage(
                error=RequestError.from_problem(body), request_id=_identifier(body)
            )
        raise WebSocketProtocolError(
            f"unexpected @type {tag!r}; a server may only send {TYPE_RESPONSE}, "
            f"{TYPE_STATE_CHANGE} or {TYPE_REQUEST_ERROR}"
        )

    def settle(self, request_id: str | None) -> None:
        """Mark a request answered, whether it succeeded or failed."""
        if request_id is not None:
            self.outstanding.discard(request_id)

    def __repr__(self) -> str:
        return (
            f"WebSocketProtocol(push_state={self.push_state!r}, "
            f"outstanding={len(self.outstanding)})"
        )


def _identifier(body: dict[str, Any]) -> str | None:
    value = body.get("requestId")
    return str(value) if isinstance(value, str) else None


#: Frames a client may send, for documentation and for the fake server to accept.
OUTGOING_TYPES = frozenset({TYPE_REQUEST, TYPE_PUSH_ENABLE, TYPE_PUSH_DISABLE})

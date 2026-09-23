"""The four JMAP error families (RFC 8620 §3.6).

JMAP fails at four distinct levels, and conflating them is the single most common
source of bad error handling in JMAP clients:

1. **Transport** - the HTTP request never produced a JMAP response at all.
2. **Request-level** - RFC 7807 problem+json; the *whole* request was rejected and
   no method ran. Fatal to every call in the batch.
3. **Method-level** - an ``error`` invocation replaced one method's response. The
   other calls in the batch still ran.
4. **Set-level** - a ``SetError`` against one object inside an otherwise
   successful ``/set``. Nothing else is affected.

Only (2) is fatal to a batch; (3) and (4) are per-call and per-object, and a
client that raises on them loses the results of every sibling call.
"""

from __future__ import annotations

from typing import Any, Final

from jmap.core.ids import Id


class JMAPError(Exception):
    """Base class for every error this library raises."""


# --------------------------------------------------------------------------- #
# 1. Transport
# --------------------------------------------------------------------------- #
class TransportError(JMAPError):
    """The request did not yield a JMAP response (connection, TLS, timeout)."""


class AuthenticationError(JMAPError):
    """The server rejected our credentials (HTTP 401)."""

    def __init__(self, message: str, *, challenges: tuple[str, ...] = ()) -> None:
        self.challenges = challenges
        super().__init__(message)


# --------------------------------------------------------------------------- #
# 2. Request-level (RFC 8620 §3.6.1) - fatal to the whole batch
# --------------------------------------------------------------------------- #
#: Request-level problem types, as registered by RFC 8620 §3.6.1.
URN_UNKNOWN_CAPABILITY: Final = "urn:ietf:params:jmap:error:unknownCapability"
URN_NOT_JSON: Final = "urn:ietf:params:jmap:error:notJSON"
URN_NOT_REQUEST: Final = "urn:ietf:params:jmap:error:notRequest"
URN_LIMIT: Final = "urn:ietf:params:jmap:error:limit"


def _request_message(type_: str, status: int | None, title: str | None, detail: str | None) -> str:
    """Phrase a problem document as one line without discarding its identity.

    ``detail`` is the most specific field and so leads, but it is free prose
    chosen by the server and some servers fill it with something useless - one
    echoes the request back verbatim. Reporting it alone leaves a reader holding
    an unattributed blob of text with no status and no problem type, unable to
    tell a 400 from a 503. The type and status therefore always follow.
    """
    head = detail or title or type_
    context = [part for part in (None if head == type_ else type_,) if part]
    if status is not None:
        context.append(f"HTTP {status}")
    return f"{head} [{'; '.join(context)}]" if context else head


class RequestError(JMAPError):
    """An RFC 7807 problem response. No method in the request executed.

    ``limit`` is populated only for :data:`URN_LIMIT`, where RFC 8620 requires the
    server to name which limit was exceeded. ``retry_after`` is the server's
    ``Retry-After``, in seconds, when it sent one: the error reaches the caller
    without a retry when that is longer than the retry policy will wait, and
    this is what to reschedule by.
    """

    def __init__(
        self,
        type_: str,
        *,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        limit: str | None = None,
        raw: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.type: str = type_
        self.status: int | None = status
        self.title: str | None = title
        self.detail: str | None = detail
        self.limit: str | None = limit
        self.raw: dict[str, Any] = raw or {}
        self.retry_after: float | None = retry_after
        super().__init__(_request_message(type_, status, title, detail))

    @classmethod
    def from_problem(cls, body: dict[str, Any], status: int | None = None) -> RequestError:
        """Build from an RFC 7807 ``application/problem+json`` body."""
        return cls(
            str(body.get("type", "about:blank")),
            status=status if status is not None else body.get("status"),
            title=body.get("title"),
            detail=body.get("detail"),
            limit=body.get("limit"),
            raw=body,
        )


# --------------------------------------------------------------------------- #
# 3. Method-level (RFC 8620 §3.6.2)
# --------------------------------------------------------------------------- #
class MethodError(JMAPError):
    """One invocation was replaced by an ``error`` response.

    Sibling calls in the same request are unaffected, so this is raised only when
    the caller reads *this* call's result.
    """

    def __init__(self, type_: str, method_call_id: str, arguments: dict[str, Any]) -> None:
        self.type = type_
        self.method_call_id = method_call_id
        self.arguments = arguments
        super().__init__(f"{type_} (call {method_call_id})")


class ServerPartialFailError(MethodError):
    """RFC 8620 §3.6.2: the only method error after which server state may have
    changed. Never retry a request that produced one."""


# --------------------------------------------------------------------------- #
# 4. Set-level (RFC 8620 §5.3)
# --------------------------------------------------------------------------- #
class SetError:
    """A per-object failure inside a ``/set``. A value, not an exception.

    ``/set`` routinely half-succeeds, and raising would discard the objects that
    *did* change. Callers inspect ``not_created`` / ``not_updated`` /
    ``not_destroyed`` and decide.
    """

    __slots__ = ("description", "existing_id", "properties", "raw", "type")

    type: str
    description: str | None
    #: Set for ``invalidProperties``: which properties were at fault.
    properties: tuple[str, ...] | None
    #: Set for ``alreadyExists``: the id of the object that already exists.
    existing_id: Id | None
    raw: dict[str, Any]

    def __init__(
        self,
        type_: str,
        *,
        description: str | None = None,
        properties: tuple[str, ...] | None = None,
        existing_id: Id | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.type = type_
        self.description = description
        self.properties = properties
        self.existing_id = existing_id
        self.raw = raw or {}

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> SetError:
        """Build from one entry of a ``notCreated``/``notUpdated``/``notDestroyed`` map."""
        props = body.get("properties")
        existing = body.get("existingId")
        return cls(
            str(body.get("type", "unknown")),
            description=body.get("description"),
            properties=tuple(props) if props is not None else None,
            existing_id=Id(existing) if existing is not None else None,
            raw=body,
        )

    def __repr__(self) -> str:
        return f"SetError({self.type!r}, description={self.description!r})"


class SetFailedError(JMAPError):
    """Wraps a :class:`SetError` for callers that opted into raising."""

    def __init__(self, error: SetError, *, key: str | None = None) -> None:
        self.error: SetError = error
        self.key = key
        super().__init__(f"{error.type}{f' for {key!r}' if key else ''}")


# --------------------------------------------------------------------------- #
# Client-side guards - raised before anything reaches the wire
# --------------------------------------------------------------------------- #
class CapabilityNotSupportedError(JMAPError):
    """A call needs a capability the server does not advertise.

    Raised locally rather than sent, because an unadvertised URN in ``using``
    makes some servers (Stalwart) reject the *entire* request with ``notRequest``,
    destroying every unrelated call batched alongside it.
    """

    def __init__(
        self,
        urn: str,
        *,
        advertised: frozenset[str] = frozenset(),
        message: str | None = None,
    ) -> None:
        self.urn = urn
        self.advertised = advertised
        # Subclasses pass their own wording rather than reassigning `args`, which
        # reads as an override of BaseException.args to a strict type checker.
        super().__init__(message or f"server does not advertise {urn}")


class CapabilityFieldError(JMAPError):
    """A request violates a limit or option the capability object advertises."""

    def __init__(self, urn: str, field: str, advertised: object, requested: object) -> None:
        self.urn = urn
        self.field = field
        self.advertised = advertised
        self.requested = requested
        super().__init__(
            f"{urn} advertises {field}={advertised!r}, but {requested!r} was requested"
        )


class BatchTooLargeError(JMAPError):
    """A batch cannot be split under ``maxCallsInRequest`` without cutting a
    back-reference edge, so it can never be sent."""

    def __init__(self, call_ids: tuple[str, ...], limit: int) -> None:
        self.call_ids = call_ids
        self.limit = limit
        super().__init__(
            f"{len(call_ids)} interdependent calls exceed maxCallsInRequest={limit}: "
            f"{', '.join(call_ids)}"
        )

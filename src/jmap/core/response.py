"""Parsing a JMAP Response and routing it back to the calls that asked (RFC 8620 §3.4).

A Response is ``methodResponses``, an optional ``createdIds`` and a
``sessionState``. Routing looks like it should be a dict lookup by
``methodCallId``, and mostly is, except for two details that make it not:

**One call id can carry several responses.** A server may emit *implicit*
responses under the same id - RFC 8621 §5.3 has ``EmailSubmission/set`` with
``onSuccessUpdateEmail`` emitting an extra ``Email/set``, and RFC 8620 §5.4 has
``Foo/copy`` with ``onSuccessDestroyOriginal`` emitting a ``Foo/destroy``. They
must be handed back separately, because merging them into the result would
overwrite fields with a different method's answer.

**A failed call is renamed, not annotated.** Its response name becomes ``error``
(§3.6.2), which is also why a back-reference that expected a specific method name
fails cleanly instead of reading fields off an error payload.

``sessionState`` changing means the session is stale and must be refetched
(§3.4); it is surfaced here rather than acted on, because deciding to refetch is
the client's job and this layer does no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.core.errors import JMAPError, MethodError, ServerPartialFailError
from jmap.core.ids import Id
from jmap.core.invocation import ParsedInvocation
from jmap.core.narrow import as_list, as_object, is_list, is_object

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jmap.core.invocation import Handle

#: RFC 8620 §3.6.2. The one method error after which server state may have
#: changed, so a request that produced it must never be blindly retried.
SERVER_PARTIAL_FAIL = "serverPartialFail"

ERROR_RESPONSE_NAME = "error"


class Response:
    """A parsed JMAP Response object."""

    __slots__ = ("created_ids", "method_responses", "session_state")

    method_responses: tuple[ParsedInvocation, ...]
    #: Creation id -> server-assigned id. Threaded into the next request so
    #: ``#`` references keep working across a split batch (RFC 8620 §3.3).
    created_ids: dict[str, Id]
    session_state: str

    def __init__(
        self,
        method_responses: Sequence[ParsedInvocation],
        *,
        created_ids: Mapping[str, Id] | None = None,
        session_state: str = "",
    ) -> None:
        self.method_responses = tuple(method_responses)
        self.created_ids = dict(created_ids or {})
        self.session_state = session_state

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> Response:
        # `x or default` would be wrong here: an empty value of the *wrong* type
        # is falsy, so `{}` for methodResponses would silently become a valid
        # empty list instead of the malformed response it is.
        raw_responses: Any = data.get("methodResponses")
        if raw_responses is None:
            raw_responses = []
        if not is_list(raw_responses):
            raise MalformedResponseError("methodResponses must be an array")

        created: Any = data.get("createdIds")
        if created is None:
            created = {}
        if not is_object(created):
            raise MalformedResponseError("createdIds must be an object")
        triples = as_list(raw_responses)
        pairs = as_object(created)
        created_ids: dict[str, Id] = {}
        for key, value in pairs.items():
            # Strings only: these values are echoed into the *next* request's
            # createdIds and handed to application code as Ids, so reifying a
            # non-string via str() would put a Python repr on the wire.
            if not isinstance(value, str):
                raise MalformedResponseError(
                    f"createdIds[{key!r}] must be a string id, got {type(value).__name__}"
                )
            created_ids[str(key)] = Id(value)
        try:
            invocations = [ParsedInvocation.from_wire(triple) for triple in triples]
        except ValueError as exc:
            raise MalformedResponseError(str(exc)) from exc
        return cls(
            invocations,
            created_ids=created_ids,
            session_state=str(data.get("sessionState", "")),
        )

    def __repr__(self) -> str:
        names = [invocation.name for invocation in self.method_responses]
        return f"Response({names}, sessionState={self.session_state!r})"


class MalformedResponseError(JMAPError, ValueError):
    """The server's answer parsed as JSON but is not the shape JMAP requires.

    A Response without its ``methodResponses`` array, an upload result with a
    string for its size, a blob whose base64 does not decode.
    """


def _to_method_error(invocation: ParsedInvocation) -> MethodError:
    error_type = str(invocation.arguments.get("type", "unknownError"))
    cls = ServerPartialFailError if error_type == SERVER_PARTIAL_FAIL else MethodError
    return cls(error_type, invocation.method_call_id, invocation.arguments)


def dispatch(response: Response, handles: Sequence[Handle[Any]]) -> None:
    """Resolve each handle from ``response``, in place.

    A handle with no response at all is left unresolved rather than failed: that
    happens when a batch was split across requests and this is only the first
    one, and treating it as an error would break splitting entirely.
    """
    by_call_id: dict[str, list[ParsedInvocation]] = {}
    for invocation in response.method_responses:
        by_call_id.setdefault(invocation.method_call_id, []).append(invocation)

    for handle in handles:
        invocations = by_call_id.get(handle.call_id)
        if not invocations:
            continue

        primary, extra = _partition(handle.call.name, invocations)

        if primary.name == ERROR_RESPONSE_NAME:
            handle.fail(_to_method_error(primary))
        else:
            # Parse failures are contained per handle, like method errors: one
            # malformed response must not abort dispatch mid-loop and leave the
            # sibling handles unresolved. The whole point of Handle is that a
            # call's failure surfaces when *its* result is read.
            try:
                parsed = handle.call.parse(primary.arguments)
            except (ValueError, TypeError, KeyError) as exc:
                handle.fail(
                    MethodError(
                        "malformedResult",
                        handle.call_id,
                        {"description": f"the {primary.name} response did not parse: {exc}"},
                    )
                )
            else:
                handle.fulfil(parsed, tuple(extra))


def _partition(
    expected: str, invocations: Sequence[ParsedInvocation]
) -> tuple[ParsedInvocation, list[ParsedInvocation]]:
    """Pick the response that answers the call, and set the rest aside.

    Matching on the method name rather than taking the first is what keeps an
    implicit response from being mistaken for the answer: the server is free to
    order them as it likes, and ``EmailSubmission/set`` returning an ``Email/set``
    alongside it must not have the two swapped.
    """
    for index, invocation in enumerate(invocations):
        if invocation.name in (expected, ERROR_RESPONSE_NAME):
            rest = [item for position, item in enumerate(invocations) if position != index]
            return invocation, rest
    # Nothing matched the name we called. Trust position over the name, because
    # a response we cannot classify is still better than none.
    return invocations[0], list(invocations[1:])

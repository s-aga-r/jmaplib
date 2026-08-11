"""Request assembly: ``using`` derivation, createdIds threading, and splitting
a batch under ``maxCallsInRequest`` without cutting a back-reference (RFC 8620
§3.3, §3.7, §5.3).

Three things happen here, and all three are correctness-critical:

**`using` is derived, then hard-intersected.** RFC 8620 §1.8 says the server must
behave as if it implements nothing the client did not list, so an under-declared
``using`` degrades silently. Over-declaring is worse: an unadvertised URN makes
some servers (Stalwart) reject the *entire* request with ``notRequest``,
destroying every unrelated call batched alongside it. So the set is computed from
the calls, then intersected with what the session advertises, and anything
missing raises locally.

**Splitting must not cut a reference edge.** A back-reference can only target a
call in the *same* request, so calls joined by references form a connected
component that must travel together. If one such component alone exceeds
``maxCallsInRequest`` the batch is unsendable, and saying so by name beats a
server-side ``invalidResultReference``.

**Creation ids cross requests only via `createdIds`.** ``#foo`` is scoped to one
request. RFC 8620 §3.3's ``createdIds`` map is the mechanism for carrying those
assignments into the next one, so a split batch keeps working.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from jmap.core.errors import BatchTooLargeError

if TYPE_CHECKING:
    from jmap.core.ids import Id
    from jmap.core.invocation import Handle, MethodCall

#: Resolves the ``using`` set for a batch. Supplied by the capability registry in
#: the layer above; the kernel stays ignorant of which URN owns which method.
UsingResolver = Callable[[Sequence[str], frozenset[str]], frozenset[str]]

CORE_URN = "urn:ietf:params:jmap:core"


class InvalidReferenceError(ValueError):
    """A back-reference targets a call that cannot satisfy it."""

    def __init__(self, source_id: str, argument: str, reason: str) -> None:
        self.source_id = source_id
        self.argument = argument
        super().__init__(f"call {source_id!r} argument {argument!r}: {reason}")


class Request:
    """One serialisable JMAP Request object."""

    __slots__ = ("created_ids", "method_calls", "using")

    using: frozenset[str]
    method_calls: list[tuple[str, MethodCall[Any]]]
    created_ids: dict[str, Id] | None

    def __init__(
        self,
        using: frozenset[str],
        method_calls: Sequence[tuple[str, MethodCall[Any]]],
        created_ids: Mapping[str, Id] | None = None,
    ) -> None:
        self.using = using
        self.method_calls = list(method_calls)
        self.created_ids = dict(created_ids) if created_ids else None

    def to_wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            # Sorted for deterministic golden-wire tests; order is not significant.
            "using": sorted(self.using),
            "methodCalls": [
                [call.name, call.to_wire_arguments(), call_id]
                for call_id, call in self.method_calls
            ],
        }
        if self.created_ids:
            body["createdIds"] = dict(self.created_ids)
        return body

    def __repr__(self) -> str:
        names = [call.name for _, call in self.method_calls]
        return f"Request({names}, using={sorted(self.using)})"


def validate_references(calls: Sequence[tuple[str, MethodCall[Any]]]) -> None:
    """Check every back-reference targets an *earlier* call of the expected name.

    Both halves matter. Targeting a later call is simply invalid. Targeting a call
    whose method name differs means the reference will fail at run time, because
    the server matches ``ResultReference.name`` against the response name - and an
    errored call responds as ``error``, so the mismatch is exactly how JMAP stops
    you reading fields off an error payload.
    """
    seen: dict[str, str] = {}
    for call_id, call in calls:
        _, refs = call.split_arguments()
        for argument, ref in refs.items():
            if ref.result_of not in seen:
                reason = f"references {ref.result_of!r}, which is not an earlier call in this batch"
                raise InvalidReferenceError(call_id, argument, reason)
            target_name = seen[ref.result_of]
            if ref.name != target_name:
                raise InvalidReferenceError(
                    call_id,
                    argument,
                    f"expects {ref.name!r} but call {ref.result_of!r} is {target_name!r}",
                )
        seen[call_id] = call.name


def _reference_components(
    calls: Sequence[tuple[str, MethodCall[Any]]],
) -> list[list[int]]:
    """Group call indices into connected components joined by references.

    Union-find over the reference edges. Components are returned ordered by their
    earliest member so that packing them preserves the caller's original call
    order, which JMAP executes in sequence and which can be observable when two
    calls touch the same objects.
    """
    index_of = {call_id: i for i, (call_id, _) in enumerate(calls)}
    parent = list(range(len(calls)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Keep the lower index as the root so component ordering stays stable.
            parent[max(ra, rb)] = min(ra, rb)

    for i, (_, call) in enumerate(calls):
        _, refs = call.split_arguments()
        for ref in refs.values():
            union(i, index_of[ref.result_of])

    groups: dict[int, list[int]] = {}
    for i in range(len(calls)):
        groups.setdefault(find(i), []).append(i)
    return [groups[root] for root in sorted(groups)]


def plan_requests(
    calls: Sequence[tuple[str, MethodCall[Any]]],
    *,
    using: frozenset[str],
    max_calls_in_request: int,
    created_ids: Mapping[str, Id] | None = None,
) -> list[Request]:
    """Split ``calls`` into requests that respect ``maxCallsInRequest``.

    Components are packed greedily in order. Only the first request carries the
    incoming ``createdIds``; the caller threads each response's ``createdIds``
    into the next request, because the server assigns them as it goes.
    """
    if max_calls_in_request < 1:
        raise ValueError(f"maxCallsInRequest must be >= 1, got {max_calls_in_request}")
    if not calls:
        return []

    validate_references(calls)
    components = _reference_components(calls)

    for component in components:
        if len(component) > max_calls_in_request:
            raise BatchTooLargeError(tuple(calls[i][0] for i in component), max_calls_in_request)

    batches: list[list[int]] = []
    current: list[int] = []
    for component in components:
        if current and len(current) + len(component) > max_calls_in_request:
            batches.append(current)
            current = []
        current.extend(component)
    # No `if current` guard: every component is non-empty and every iteration
    # extends `current`, so after the loop it always holds the final batch.
    batches.append(current)

    return [
        Request(
            using,
            [calls[i] for i in sorted(batch)],
            created_ids if position == 0 else None,
        )
        for position, batch in enumerate(batches)
    ]


def derive_using(
    calls: Iterable[tuple[str, MethodCall[Any]]],
    *,
    resolver: UsingResolver,
    extra: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Compute the ``using`` set for a batch.

    ``urn:ietf:params:jmap:core`` is always included: RFC 8620 requires it, and
    omitting it makes ``Core/echo`` - the one call every connectivity check uses -
    fail for a reason nobody guesses quickly.
    """
    method_names = [call.name for _, call in calls]
    return resolver(method_names, extra) | {CORE_URN}


def handles_to_calls(handles: Sequence[Handle[Any]]) -> list[tuple[str, MethodCall[Any]]]:
    """Adapt queued handles into the ``(call_id, call)`` pairs the planner takes."""
    return [(handle.call_id, handle.call) for handle in handles]

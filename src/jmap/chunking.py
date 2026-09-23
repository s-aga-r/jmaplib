"""Splitting an oversized ``Foo/get`` across several calls (RFC 8620 §5.1).

``maxObjectsInGet`` caps how many ids one call may name, and a client holding a
few thousand ids from a ``/query`` will exceed it routinely. Unlike ``/set``,
``/get`` *is* safe to split: it changes nothing, and the pieces recombine
cleanly.

The one thing that does not recombine cleanly is the ``state`` string. Each chunk
reports the state it was answered from, and if something changed between the
first chunk and the last, the merged result is a mix of two points in time. That
is not detectable afterwards, so it is checked here: mismatched states raise
rather than quietly handing back a torn read.

A chunked call is still *one* handle to the caller. Splitting is a transport
detail, and leaking it would mean every caller who might exceed a limit has to
write the merge themselves. The one place it cannot be hidden is a
back-reference: that names a single call, and the chunks are several.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jmap.core.errors import JMAPError
from jmap.core.invocation import Handle, MethodCall

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jmap.core.ids import Id
    from jmap.core.invocation import ResultRef

#: Paths every chunk answers alike: the account never differs, and the state may
#: not - :func:`merge_get_results` refuses a torn read. A reference to one of
#: these can let the first chunk speak for the rest.
_CHUNK_INVARIANT_PATHS: Final = frozenset({"/accountId", "/state"})


class ChunkedReferenceError(JMAPError):
    """A back-reference was asked of a ``/get`` that was split into chunks.

    A back-reference names one call, and each chunk is its own call, so it could
    only ever see the first chunk's share of the result. The server resolves it
    without complaint, which made this a silent truncation: ``ref_list`` over a
    250-id ``Email/get`` split at 100 fed 100 thread ids to the next call.
    """

    def __init__(self, method: str, chunks: int, path: str) -> None:
        self.method = method
        self.chunks = chunks
        self.path = path
        super().__init__(
            f"{method} named more ids than maxObjectsInGet and was split into {chunks} "
            f"calls, so a back-reference to {path!r} would see only the first of them; "
            f"name fewer ids per call, or read the result and pass the ids along in a "
            f"second batch"
        )


class TornReadError(JMAPError):
    """A chunked ``/get`` spanned a change to the underlying data.

    The chunks were answered from different states, so merging them would produce
    a result that never existed on the server. Re-running the read is the fix;
    doing so from a single ``/query`` state usually avoids a repeat.
    """

    method: str
    states: tuple[str, ...]

    def __init__(self, method: str, states: Sequence[str]) -> None:
        self.method = method
        self.states = tuple(states)
        super().__init__(
            f"{method} was split across chunks answered from different states "
            f"({', '.join(repr(state) for state in self.states)}); the data changed "
            f"mid-read, so the merged result would be inconsistent"
        )


class ChunkedHandle(Handle[Any]):
    """One handle standing in for several ``/get`` calls.

    Presents the same surface as an ordinary handle, except as a back-reference
    source: only a path every chunk answers alike (``/state``, ``/accountId``)
    can be referenced, through the first chunk. Anything else raises
    :class:`ChunkedReferenceError` rather than quietly covering one chunk.
    """

    __slots__ = ("_chunks",)

    def __init__(self, chunks: Sequence[Handle[Any]]) -> None:
        first = chunks[0]
        super().__init__(first.call_id, first.call)
        self._chunks = tuple(chunks)

    @property
    def chunks(self) -> tuple[Handle[Any], ...]:
        return self._chunks

    def _refuse(self, path: str) -> ChunkedReferenceError:
        return ChunkedReferenceError(self.call.name, len(self._chunks), path)

    def ref(self, path: str) -> ResultRef[Any]:
        if path not in _CHUNK_INVARIANT_PATHS:
            raise self._refuse(path)
        return super().ref(path)

    def ref_ids(self) -> ResultRef[list[Id]]:
        raise self._refuse("/ids")

    def ref_list(self, prop: str) -> ResultRef[list[Id]]:
        raise self._refuse(f"/list/*/{prop}")

    def ref_created(self, creation_key: str, prop: str = "id") -> ResultRef[Any]:
        raise self._refuse(f"/created/{creation_key}/{prop}")

    def ref_updated(self) -> ResultRef[list[Id]]:
        raise self._refuse("/updated")

    def ref_updated_properties(self) -> ResultRef[list[str] | None]:
        raise self._refuse("/updatedProperties")

    @property
    def is_resolved(self) -> bool:
        return all(chunk.is_resolved for chunk in self._chunks)

    @property
    def error(self) -> Any:
        return next((chunk.error for chunk in self._chunks if chunk.error is not None), None)

    @property
    def result(self) -> Any:
        """The merged result of every chunk.

        Reading it raises whatever the first failing chunk raised: a partial
        answer to a request the caller made as a whole would be worse than an
        error.
        """
        results = [chunk.result for chunk in self._chunks]
        if len(results) == 1:
            return results[0]
        return merge_get_results(self.call.name, results)


def merge_get_results(method: str, results: Sequence[Any]) -> Any:
    """Combine the chunks of one ``/get`` back into a single response.

    Works on the parsed responses rather than the raw payloads so that a typed
    ``GetResponse[Email]`` stays typed after merging.
    """
    merged = results[0].model_copy(deep=False)

    states = [result.state for result in results if getattr(result, "state", None) is not None]
    if len(set(states)) > 1:
        raise TornReadError(method, states)

    items: list[Any] = []
    not_found: list[str] = []
    for result in results:
        items.extend(result.items)
        not_found.extend(result.not_found)
    merged.items = items
    merged.not_found = not_found
    return merged


def chunk_get_call(call: MethodCall[Any], ids: Sequence[Any], size: int) -> list[MethodCall[Any]]:
    """Split one ``/get`` into calls of at most ``size`` ids each."""
    return [
        MethodCall(
            call.name, {**call.arguments, "ids": list(ids[start : start + size])}, call.parse
        )
        for start in range(0, len(ids), size)
    ]

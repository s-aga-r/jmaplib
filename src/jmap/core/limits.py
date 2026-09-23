"""Server-advertised limits from the core capability, and the policy for obeying them
(RFC 8620 §2).

A limit is a boundary the *client* has to enforce, because breaching one is not a
per-call failure: the server answers with a request-level
``urn:ietf:params:jmap:error:limit`` problem response, and that fails the whole
request. One oversized ``/get`` therefore destroys every unrelated method call
batched alongside it, which is why the planner asks these questions before the
request is built rather than reacting to the error afterwards.

Two things here are easy to get wrong.

**The wire names.** The core capability spells them ``maxSizeRequest`` and
``maxConcurrentRequests`` - *not* ``maxSizeRequestObject`` or any other
plausible-looking variant. Misspelling one is silent: the field simply reads as
absent and the client keeps a fallback instead of the server's real limit. Every
name below is verified against
``tests/fixtures/stalwart-0.16.17-session-bootstrap.json``, a real Stalwart
v0.16.17 capture, and the test suite asserts the set of names we read is exactly
the set that capture contains.

**What may be split.** Fitting under a limit by cutting work into chunks is safe
for ``/get`` and unsafe for ``/set``; see :class:`DefaultLimitPolicy`, where that
decision lives.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, TypeVar, cast, runtime_checkable

from jmap.core.errors import CapabilityFieldError, JMAPError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

T = TypeVar("T")

#: The capability whose object carries every limit in this module.
URN_CORE: Final = "urn:ietf:params:jmap:core"

#: Not a limit, so it has no :class:`LimitKey`; named once and shared between
#: parsing and :func:`check_collation` so the two cannot drift.
FIELD_COLLATION_ALGORITHMS: Final = "collationAlgorithms"

# RFC 8620 §2 gives a "suggested minimum" for each limit. Those double as the
# fallback for an unadvertised field: a server that omits one has promised
# nothing, and the suggested minimum is what a conformant server would offer.
MIN_SIZE_UPLOAD: Final = 50_000_000
MIN_CONCURRENT_UPLOAD: Final = 4
MIN_SIZE_REQUEST: Final = 10_000_000
MIN_CONCURRENT_REQUESTS: Final = 4
MIN_CALLS_IN_REQUEST: Final = 16
MIN_OBJECTS_IN_GET: Final = 500
MIN_OBJECTS_IN_SET: Final = 500

#: Collations registered in the RFC 4790 registry that a JMAP server plausibly
#: offers. Used only by :meth:`Limits.permissive`; never assumed of a real server.
KNOWN_COLLATIONS: Final = (
    "i;octet",
    "i;ascii-numeric",
    "i;ascii-casemap",
    "i;unicode-casemap",
)

#: Stands in for "no practical bound" in :meth:`Limits.permissive`. A real number
#: rather than ``None`` keeps every field an ``int``, so arithmetic on limits needs
#: no special case.
_UNBOUNDED: Final = sys.maxsize


class InvalidChunkSizeError(JMAPError, ValueError):
    """A chunk size was not a positive integer."""

    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(f"chunk size must be at least 1, got {size}")


def _uint(source: Mapping[str, Any], key: str, fallback: int) -> int:
    """Read an ``UnsignedInt`` limit, falling back when it is absent or unusable.

    A malformed field is treated as absent rather than fatal: one bad value in the
    capability object should not make an otherwise working session unusable. Zero
    and negatives are rejected along with wrong types because a limit of zero
    would yield a chunk size of zero, and a planner dividing work into
    zero-sized chunks never terminates.
    """
    value = source.get(key)
    # bool is an int subclass, and ``True`` would otherwise read as a limit of 1.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return fallback
    return value


def _collations(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    entries = cast("list[object]", value)
    # Non-string entries cannot name a collation, and keeping them would let a
    # membership test succeed on something that can never go on the wire.
    return tuple(item for item in entries if isinstance(item, str))


@dataclass(frozen=True, slots=True)
class Limits:
    """The limits advertised in the ``urn:ietf:params:jmap:core`` capability object.

    Field defaults are the RFC 8620 §2 suggested minimums, so a bare ``Limits()``
    models the least capable conformant server.
    """

    #: Largest single upload the server accepts, in octets.
    max_size_upload: int = MIN_SIZE_UPLOAD
    #: Concurrent requests permitted against the upload endpoint.
    max_concurrent_upload: int = MIN_CONCURRENT_UPLOAD
    #: Largest single request body the API endpoint accepts, in octets.
    max_size_request: int = MIN_SIZE_REQUEST
    #: Concurrent requests permitted against the API endpoint.
    max_concurrent_requests: int = MIN_CONCURRENT_REQUESTS
    #: Method calls permitted in one request.
    max_calls_in_request: int = MIN_CALLS_IN_REQUEST
    #: Objects requestable in one ``/get``.
    max_objects_in_get: int = MIN_OBJECTS_IN_GET
    #: Objects touchable in one ``/set``. RFC 8620 §2 counts creates, updates and
    #: destroys against a *single* budget, so 7 creates plus 6 destroys is 13.
    max_objects_in_set: int = MIN_OBJECTS_IN_SET
    #: Collation identifiers (RFC 4790) the server supports for sorting.
    collation_algorithms: tuple[str, ...] = ()

    @classmethod
    def from_capability(cls, value: Mapping[str, Any]) -> Self:
        """Parse a core capability object, tolerating missing or malformed fields."""
        return cls(
            max_size_upload=_uint(value, "maxSizeUpload", MIN_SIZE_UPLOAD),
            max_concurrent_upload=_uint(value, "maxConcurrentUpload", MIN_CONCURRENT_UPLOAD),
            max_size_request=_uint(value, "maxSizeRequest", MIN_SIZE_REQUEST),
            max_concurrent_requests=_uint(value, "maxConcurrentRequests", MIN_CONCURRENT_REQUESTS),
            max_calls_in_request=_uint(value, "maxCallsInRequest", MIN_CALLS_IN_REQUEST),
            max_objects_in_get=_uint(value, "maxObjectsInGet", MIN_OBJECTS_IN_GET),
            max_objects_in_set=_uint(value, "maxObjectsInSet", MIN_OBJECTS_IN_SET),
            # Unlike the numeric limits this has no RFC-suggested value, and
            # inventing one would let a client send a `sort` the server rejects
            # with `unsupportedSort`. Silence means "advertised nothing".
            collation_algorithms=_collations(value.get(FIELD_COLLATION_ALGORITHMS)),
        )

    @classmethod
    def permissive(cls) -> Self:
        """Limits that never bind - for tests that are not about limits."""
        return cls(
            max_size_upload=_UNBOUNDED,
            max_concurrent_upload=_UNBOUNDED,
            max_size_request=_UNBOUNDED,
            max_concurrent_requests=_UNBOUNDED,
            max_calls_in_request=_UNBOUNDED,
            max_objects_in_get=_UNBOUNDED,
            max_objects_in_set=_UNBOUNDED,
            collation_algorithms=KNOWN_COLLATIONS,
        )


class LimitKey(StrEnum):
    """The limits a batch can be planned against.

    The values are the capability field names verbatim, which is also what the
    server names in the ``limit`` member of a ``urn:ietf:params:jmap:error:limit``
    problem response (RFC 8620 §3.6.1) - so a server-reported breach maps straight
    back onto a key.
    """

    GET_OBJECTS = "maxObjectsInGet"
    SET_OBJECTS = "maxObjectsInSet"
    CALLS_IN_REQUEST = "maxCallsInRequest"
    UPLOAD_SIZE = "maxSizeUpload"
    REQUEST_SIZE = "maxSizeRequest"


@runtime_checkable
class LimitPolicy(Protocol):
    """How the batch planner decides what to do about a limit."""

    def chunk_size(self, key: LimitKey, limits: Limits) -> int:
        """Return the ceiling for ``key``: items per chunk, or octets for a size limit."""
        raise NotImplementedError

    def may_chunk(self, key: LimitKey) -> bool:
        """Return whether work may be split by the client to fit under ``key``."""
        raise NotImplementedError


#: The advertised value that bounds each key, as one table: a key can only ever
#: read the field it is paired with here, and a key with no entry raises
#: ``KeyError`` from :meth:`DefaultLimitPolicy.chunk_size` instead of quietly
#: reporting some other limit's ceiling.
_CEILINGS: Final[Mapping[LimitKey, Callable[[Limits], int]]] = {
    LimitKey.GET_OBJECTS: lambda limits: limits.max_objects_in_get,
    LimitKey.SET_OBJECTS: lambda limits: limits.max_objects_in_set,
    LimitKey.CALLS_IN_REQUEST: lambda limits: limits.max_calls_in_request,
    LimitKey.UPLOAD_SIZE: lambda limits: limits.max_size_upload,
    LimitKey.REQUEST_SIZE: lambda limits: limits.max_size_request,
}


class DefaultLimitPolicy:
    """Chunk ``/get``, never chunk ``/set``.

    This is a correctness decision, not a tuning knob:

    * A ``/get`` over more than ``maxObjectsInGet`` ids **may** be split. The
      results concatenate cleanly, and the ``state`` string returned by each chunk
      is compared across chunks so a store that changed mid-sequence is detected
      and the caller can retry rather than assemble a torn snapshot.
    * A ``/set`` **must not** be split. ``ifInState`` makes one ``/set`` an atomic
      conditional write; two ``/set`` calls are two independent writes, and the
      second can fail (or race another client) after the first has already been
      committed. Splitting therefore converts an all-or-nothing operation into a
      partially applied one, which no amount of retrying repairs. Over-large sets
      are the caller's problem to reduce, so :meth:`may_chunk` returns ``False``
      for :attr:`LimitKey.SET_OBJECTS`.

    The byte limits are equally unsplittable here: dividing a request body or a
    blob by octets needs a server-side reassembly facility this policy cannot
    assume, so those report ``False`` too.
    """

    __slots__ = ()

    #: Only these may be divided by the client; every other key is all-or-nothing.
    _CHUNKABLE: Final = frozenset({LimitKey.GET_OBJECTS, LimitKey.CALLS_IN_REQUEST})

    def chunk_size(self, key: LimitKey, limits: Limits) -> int:
        """Return the ceiling for ``key``.

        Defined even where :meth:`may_chunk` is ``False``: the ceiling is what
        tells a caller its indivisible unit of work is too big to send at all.
        """
        return _CEILINGS[key](limits)

    def may_chunk(self, key: LimitKey) -> bool:
        return key in self._CHUNKABLE


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Yield consecutive slices of ``items`` of at most ``size`` elements.

    Validation happens eagerly rather than inside the generator body, because a
    generator would defer :class:`InvalidChunkSizeError` until the first
    iteration - by which point the traceback no longer points at the caller that
    computed the bad size.
    """
    if size < 1:
        raise InvalidChunkSizeError(size)
    return _chunked(items, size)


def _chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def check_upload_size(size: int, limits: Limits) -> None:
    """Reject an over-large upload before a single octet is streamed.

    The server's answer to an oversized upload arrives only after it has read the
    body, so a 50 MB attachment costs a full upload to learn it was never going to
    be accepted. This check costs microseconds.
    """
    if size > limits.max_size_upload:
        raise CapabilityFieldError(
            URN_CORE, LimitKey.UPLOAD_SIZE.value, limits.max_size_upload, size
        )


def check_collation(collation: str, limits: Limits) -> None:
    """Reject a collation the server did not advertise.

    Matching is exact: the identifiers are registry names (RFC 4790) that servers
    and this library both write in their registered form, and guessing at variants
    would only trade a clear local error for a remote ``unsupportedSort``.
    """
    if collation not in limits.collation_algorithms:
        raise CapabilityFieldError(
            URN_CORE, FIELD_COLLATION_ALGORITHMS, limits.collation_algorithms, collation
        )

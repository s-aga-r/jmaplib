"""Keeping a cached query result up to date (RFC 8620 §5.6).

``Foo/queryChanges`` hands back a delta rather than a new list, and the client
splices it into what it already has. The spec spells the algorithm out, and three
details in it are easy to miss:

**The cached list is sparse.** A client that ran a query and fetched only the
first screenful holds ``["id1", "id2", null, null, "id3", ...]`` - the nulls are
positions it knows exist but has never downloaded. Splicing has to preserve them,
because they are what keeps the *indices* meaningful.

**Removals come first, then insertions, lowest index first.** The indices in
``added`` describe the list *after* the removals have been applied, so doing it in
either other order silently puts items in the wrong places.

**An id may appear in both arrays.** When the sort or filter uses a mutable
property, the server reports a moved item as removed *and* re-added, and §5.6
requires the client to reinsert it. Treating ``removed`` as a delete-set and
skipping the re-add loses the item.

A removed id the client does not have is simply ignored - the RFC's own worked
example includes one.

:func:`splice` follows the RFC to the letter, trailing nulls and all.
:class:`QueryView` does not store those: a run of nulls out to ``total`` says
only "unknown", and materialising it made a view's size the server's choice -
bounded, it refused any query past a million results, including one the client
holds fifty rows of. The view keeps ``total`` and implies the tail.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from jmap.core.errors import JMAPError
from jmap.models.arguments import checked

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jmap.models.responses import AddedItem, QueryChangesResponse, QueryResponse

#: A cached result is sparse: ``None`` marks a position the client knows exists
#: but has never fetched.
SparseIds = list[str | None]

#: The most positions a sparse view will materialise. ``position``, ``total``
#: and each added item's ``index`` are server-chosen integers, and the view
#: allocates a slot per position - so without a bound, a fifty-byte response
#: claiming ``"total": 2**45`` is a multi-terabyte allocation. A million rows
#: is far past any list a client usefully caches.
MAX_VIEW_LENGTH: Final = 1_000_000


class ViewTooLargeError(JMAPError):
    """A query response named a position beyond what a view will materialise."""

    def __init__(self, what: str, value: int) -> None:
        self.what = what
        self.value = value
        super().__init__(
            f"{what} of {value} is outside [0, {MAX_VIEW_LENGTH}]; a sparse view "
            f"allocates a slot per position, so honouring it would be an "
            f"allocation the size the server chose"
        )


def _checked_position(value: int, *, what: str) -> int:
    if value < 0 or value > MAX_VIEW_LENGTH:
        raise ViewTooLargeError(what, value)
    return value


def _checked_total(value: int) -> int:
    """A total a view can hold: any size, since it only ever truncates, but not
    negative - ``del ids[-3:]`` would quietly drop the last three rows."""
    if value < 0:
        raise ViewTooLargeError("total", value)
    return value


class StaleQueryViewError(JMAPError):
    """A delta was applied to a view it was not computed against.

    ``oldQueryState`` must match the view's current state. Applying a delta out of
    order corrupts the list in a way nothing downstream can detect, so it is
    refused rather than attempted.
    """

    def __init__(self, expected: str, received: str) -> None:
        self.expected = expected
        self.received = received
        super().__init__(
            f"this delta starts from query state {received!r} but the view is at "
            f"{expected!r}; re-run the query rather than splicing out of order"
        )


class UncacheableQueryError(JMAPError):
    """The server said it cannot compute deltas for this query.

    ``canCalculateChanges: false`` means ``Foo/queryChanges`` will never work for
    this filter and sort. Re-running ``Foo/query`` is the only way to refresh.
    """

    def __init__(self, type_name: str) -> None:
        self.type_name = type_name
        super().__init__(
            f"the server cannot calculate changes for this {type_name} query; "
            f"refresh by re-running the query instead"
        )


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """What makes two queries *the same* query.

    Used as the cache key, and as the thing ``queryChanges`` must echo back
    unchanged: RFC 8620 §5.6 takes ``filter`` and ``sort`` as arguments precisely
    so the server can confirm the delta is against the query the client means.
    Changing either produces a different result set, so it must produce a
    different view.
    """

    type_name: str
    account_id: str
    #: Serialised for hashing, since a filter is a nested mapping.
    filter_key: str = ""
    sort_key: str = ""
    #: ``collapseThreads`` changes which emails appear at all (RFC 8621 §4.4), so
    #: two views differing only in it are not interchangeable.
    collapse_threads: bool = False

    @classmethod
    @checked
    def build(
        cls,
        type_name: str,
        account_id: str,
        *,
        filter: Mapping[str, Any] | None = None,  # `filter` mirrors the wire name
        sort: Sequence[Mapping[str, Any]] | None = None,
        collapse_threads: bool = False,
    ) -> QuerySpec:
        """The spec for one query, from the arguments it is sent with.

        They are checked first: a sort given as a string was keyed by its
        ``repr()``, making a spec - and a state key - no real query matches.
        """
        return cls(
            type_name=type_name,
            account_id=account_id,
            filter_key=_stable_key(filter),
            sort_key=_stable_key(sort),
            collapse_threads=collapse_threads,
        )


def _stable_key(value: Any) -> str:
    """A deterministic string for a filter or sort, for use as a cache key.

    Dict ordering must not affect identity: ``{"a": 1, "b": 2}`` and
    ``{"b": 2, "a": 1}`` are the same filter and have to hash the same. Any
    mapping and any sequence but a string is read by its contents - a tuple sort
    or a read-only mapping went through ``repr()``, making the same query a
    different key - while dicts and lists keep exactly the keys they always
    had, which persisted cursors are filed under.
    """
    if value is None:
        return ""
    if isinstance(value, Mapping):
        # mypy narrows the Any to Mapping[Any, Any] and calls the cast redundant;
        # pyright narrows to Mapping[Unknown, Unknown] and requires it.
        mapping = cast("Mapping[Any, Any]", value)  # type: ignore[redundant-cast]
        items = sorted((str(key), _stable_key(item)) for key, item in mapping.items())
        return "{" + ",".join(f"{key}:{item}" for key, item in items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence = cast("Sequence[Any]", value)  # type: ignore[redundant-cast]
        return "[" + ",".join(_stable_key(item) for item in sequence) + "]"
    return repr(value)


def splice(
    ids: Sequence[str | None],
    *,
    removed: Iterable[str] = (),
    added: Iterable[AddedItem] = (),
    total: int | None = None,
) -> SparseIds:
    """Apply one ``queryChanges`` delta to a cached list (RFC 8620 §5.6).

    Follows the spec's own worked example exactly: splice out every removed id
    the list actually holds, splice in each added item one by one from the lowest
    index, then truncate or extend to the new total.
    """
    doomed = set(removed)
    # Splicing out shifts everything after it down, which a filtered rebuild
    # does in one pass. Ids we never cached are simply absent - the RFC's example
    # includes one such id.
    survivors: SparseIds = [item for item in ids if item is None or item not in doomed]

    # A single merge pass rather than insert() per added item: each insert
    # shifts the whole tail, so a large delta into a large view was O(k*n) -
    # roughly 10^9 element moves for 10k additions into a 100k view, with both
    # numbers under server control. The indices are checked first because they
    # size the result. (Out-of-spec duplicate indices land in delta order.)
    additions = sorted(
        ((_checked_position(item.index, what="an added item's index"), item.id) for item in added),
        key=lambda pair: pair[0],
    )
    result: SparseIds = []
    consumed = 0
    for index, added_id in additions:
        take = index - len(result)
        if take > 0:
            result.extend(survivors[consumed : consumed + take])
            consumed += take
        if index > len(result):
            # The sparse cache does not reach this far yet; pad so the id lands
            # at the index the server actually gave, not appended at the end.
            result.extend([None] * (index - len(result)))
        result.append(added_id)
    result.extend(survivors[consumed:])

    if total is not None:
        _checked_position(total, what="total")
        if total < len(result):
            del result[total:]
        else:
            result.extend([None] * (total - len(result)))
    return result


@dataclass(slots=True)
class QueryView:
    """A cached, sparse view of one query's results.

    Holds the ids and the ``queryState`` they correspond to, so a delta can be
    checked against the state it was computed from before being applied.

    ``ids`` runs as far as the last position the view knows anything about;
    positions past it, out to ``total``, are unknown and implied rather than
    stored - see the module docstring. ``len(view)`` counts them.
    """

    spec: QuerySpec
    ids: SparseIds = field(default_factory=lambda: [])
    query_state: str = ""
    total: int | None = None
    #: ``False`` means ``queryChanges`` is unavailable for this query and the only
    #: refresh is a fresh ``Foo/query``.
    can_calculate_changes: bool = False

    @classmethod
    def from_query(cls, spec: QuerySpec, response: QueryResponse) -> QueryView:
        """Seed a view from a ``Foo/query`` response.

        ``position`` matters: a query answered from part-way down the results
        describes a list whose earlier positions the client has not seen, and the
        view has to record them as unfetched rather than pretend the results start
        at zero.
        """
        position = _checked_position(response.position, what="position")
        ids: SparseIds = [None] * position + list(response.ids)
        if response.total is not None:
            _checked_total(response.total)
        return cls(
            spec=spec,
            ids=ids,
            query_state=response.query_state or "",
            total=response.total,
            can_calculate_changes=response.can_calculate_changes,
        )

    @property
    def known_ids(self) -> list[str]:
        """Just the ids actually cached, with the unfetched gaps dropped."""
        return [item for item in self.ids if item is not None]

    @property
    def up_to_id(self) -> str | None:
        """The highest-index id held, for ``queryChanges``'s ``upToId``.

        Letting the server skip changes past this point is a large saving on a
        long result set - but only when the sort and filter are on immutable
        properties. RFC 8620 §5.6 has the server ignore it otherwise, so passing
        it is always safe.
        """
        for item in reversed(self.ids):
            if item is not None:
                return item
        return None

    def apply(self, response: QueryChangesResponse) -> None:
        """Splice a delta into this view, in place."""
        if not self.can_calculate_changes:
            raise UncacheableQueryError(self.spec.type_name)
        old_state = response.old_query_state or ""
        if old_state != self.query_state:
            raise StaleQueryViewError(self.query_state, old_state)

        # No total passed down: splice() would pad out to it, and the view
        # implies that tail instead. A shrinking total still truncates.
        ids = splice(self.ids, removed=response.removed, added=response.added)
        if response.total is not None:
            del ids[_checked_total(response.total) :]
        # A delta that reports no total leaves it unknown. The old one counted
        # positions before this delta added or removed any, and kept, it had
        # len() report rows that were gone.
        self.total = response.total
        self.ids = ids
        self.query_state = response.new_query_state or ""

    def reset(self, response: QueryResponse) -> None:
        """Discard the cache and re-seed from a fresh query.

        The recovery path for ``cannotCalculateChanges`` and for a stale view.
        """
        refreshed = QueryView.from_query(self.spec, response)
        self.ids = refreshed.ids
        self.query_state = refreshed.query_state
        self.total = refreshed.total
        self.can_calculate_changes = refreshed.can_calculate_changes

    def __len__(self) -> int:
        """Every position the view knows of, including the unstored tail."""
        return max(len(self.ids), self.total or 0)

    def __repr__(self) -> str:
        return (
            f"QueryView({self.spec.type_name!r}, cached={len(self.known_ids)}/"
            f"{len(self)}, state={self.query_state!r})"
        )

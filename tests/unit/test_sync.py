"""The sync engine: cursors, change following, and query splicing."""

from __future__ import annotations

from typing import ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from jmap.models.responses import AddedItem, ChangesResponse, QueryChangesResponse, QueryResponse
from jmap.sync import (
    ChangeSet,
    InMemoryStateStore,
    QuerySpec,
    QueryView,
    StaleQueryViewError,
    StateStore,
    UncacheableQueryError,
    ViewTooLargeError,
    query_key,
    splice,
    type_key,
)


def added(*pairs: tuple[str, int]) -> list[AddedItem]:
    return [AddedItem.model_validate({"id": name, "index": index}) for name, index in pairs]


class TestSpliceRfcExample:
    """RFC 8620 §5.6 works the algorithm through by hand; this is that example."""

    CACHED: ClassVar[list[str | None]] = [
        "id1",
        "id2",
        None,
        None,
        "id3",
        "id4",
        None,
        None,
        None,
    ]

    def test_splicing_out_removed_ids(self):
        # "id31" is not in the cache and is simply ignored - the RFC's own
        # example includes such an id.
        result = splice(self.CACHED, removed=["id2", "id31"])
        assert result == ["id1", None, None, "id3", "id4", None, None, None]

    def test_splicing_in_added_ids(self):
        after_removal = ["id1", None, None, "id3", "id4", None, None, None]
        result = splice(after_removal, added=added(("id5", 0)))
        assert result == ["id5", "id1", None, None, "id3", "id4", None, None, None]

    def test_the_whole_worked_example_in_one_pass(self):
        result = splice(self.CACHED, removed=["id2", "id31"], added=added(("id5", 0)))
        assert result == ["id5", "id1", None, None, "id3", "id4", None, None, None]


class TestSpliceOrdering:
    def test_removals_are_applied_before_insertions(self):
        # The indices in `added` describe the list *after* the removals, so doing
        # it the other way round puts the item in the wrong place.
        result = splice(["a", "b", "c"], removed=["a"], added=added(("x", 1)))
        assert result == ["b", "x", "c"]

    def test_insertions_are_applied_lowest_index_first(self):
        result = splice(["a"], added=added(("x", 0), ("y", 1)))
        assert result == ["x", "y", "a"]

    def test_unsorted_added_is_sorted_defensively(self):
        # The server MUST send these index-ascending; a client that trusts it
        # blindly corrupts the list if one does not.
        result = splice(["a"], added=added(("y", 1), ("x", 0)))
        assert result == ["x", "y", "a"]

    def test_an_id_in_both_arrays_is_moved_not_dropped(self):
        # A mutable sort or filter property makes the server report a moved item
        # as removed *and* re-added (§5.6). Treating removed as a delete-set and
        # skipping the re-add loses it.
        result = splice(["a", "b", "c"], removed=["a"], added=added(("a", 2)))
        assert result == ["b", "c", "a"]


class TestSpliceSparseness:
    def test_gaps_are_preserved(self):
        # The nulls are what keep the indices meaningful.
        assert splice([None, "a", None], removed=["a"]) == [None, None]

    def test_inserting_past_the_end_pads_rather_than_appends(self):
        # A sparse cache may not reach that far yet; the id must land at the
        # index the server gave.
        result = splice(["a"], added=added(("z", 4)))
        assert result == ["a", None, None, None, "z"]

    def test_total_truncates(self):
        assert splice(["a", "b", "c"], total=2) == ["a", "b"]

    def test_total_extends_with_gaps(self):
        assert splice(["a"], total=3) == ["a", None, None]

    def test_total_of_zero_empties_the_list(self):
        assert splice(["a", "b"], total=0) == []

    def test_no_delta_is_a_no_op(self):
        assert splice(["a", None, "b"]) == ["a", None, "b"]

    def test_the_input_is_not_mutated(self):
        original = ["a", "b"]
        splice(original, removed=["a"])
        assert original == ["a", "b"]


class TestQuerySpec:
    def test_filter_key_ignores_dict_ordering(self):
        # {"a": 1, "b": 2} and {"b": 2, "a": 1} are the same filter.
        first = QuerySpec.build("Email", "a", filter={"a": 1, "b": 2})
        second = QuerySpec.build("Email", "a", filter={"b": 2, "a": 1})
        assert first == second
        assert hash(first) == hash(second)

    def test_a_different_filter_is_a_different_query(self):
        assert QuerySpec.build("Email", "a", filter={"x": 1}) != QuerySpec.build(
            "Email", "a", filter={"x": 2}
        )

    def test_sort_order_is_significant(self):
        # Unlike a filter's keys, a sort is a sequence and its order matters.
        first = QuerySpec.build("Email", "a", sort=[{"property": "x"}, {"property": "y"}])
        second = QuerySpec.build("Email", "a", sort=[{"property": "y"}, {"property": "x"}])
        assert first != second

    def test_collapse_threads_distinguishes_two_views(self):
        # It changes which emails appear at all (RFC 8621 §4.4).
        assert QuerySpec.build("Email", "a", collapse_threads=True) != QuerySpec.build(
            "Email", "a", collapse_threads=False
        )

    def test_nested_filters_are_handled(self):
        spec = QuerySpec.build(
            "Email", "a", filter={"operator": "AND", "conditions": [{"x": 1}, {"y": [1, 2]}]}
        )
        assert spec.filter_key

    def test_no_filter_or_sort(self):
        spec = QuerySpec.build("Email", "a")
        assert spec.filter_key == ""
        assert spec.sort_key == ""


class TestQueryViewSeeding:
    def test_from_a_simple_query(self):
        view = QueryView.from_query(
            QuerySpec.build("Email", "a"),
            QueryResponse.model_validate(
                {"queryState": "q1", "canCalculateChanges": True, "ids": ["m1", "m2"]}
            ),
        )
        assert view.ids == ["m1", "m2"]
        assert view.query_state == "q1"
        assert view.can_calculate_changes

    def test_a_query_answered_part_way_down_records_the_gap(self):
        # The client has not seen positions 0-4, and pretending the results start
        # at zero would make every later index wrong.
        view = QueryView.from_query(
            QuerySpec.build("Email", "a"),
            QueryResponse.model_validate({"position": 5, "ids": ["m6"], "queryState": "q"}),
        )
        assert view.ids == [None, None, None, None, None, "m6"]

    def test_the_unfetched_tail_is_implied_rather_than_allocated(self):
        # RFC 8620 §5.6 pictures the cache padded with nulls out to the total.
        # Those trailing slots say nothing but "unknown", so the total records
        # them instead: len() still reports the full length.
        view = QueryView.from_query(
            QuerySpec.build("Email", "a"),
            QueryResponse.model_validate({"ids": ["m1"], "total": 4, "queryState": "q"}),
        )
        assert view.ids == ["m1"]
        assert view.total == 4
        assert len(view) == 4

    def test_a_query_with_millions_of_results_can_be_cached(self):
        # The first screenful of a 1.2M-row query, with calculateTotal - the
        # sync guide's own flow - used to be refused outright, because the tail
        # was allocated; so did every delta once a total crossed a million.
        response = QueryResponse.model_validate(
            {
                "ids": ["m1", "m2"],
                "total": 1_200_000,
                "queryState": "q",
                "canCalculateChanges": True,
            }
        )
        view = QueryView.from_query(QuerySpec.build("Email", "a"), response)
        view.apply(
            QueryChangesResponse.model_validate(
                {
                    "oldQueryState": "q",
                    "newQueryState": "q2",
                    "added": [{"id": "m0", "index": 0}],
                    "total": 1_200_001,
                }
            )
        )
        assert view.known_ids == ["m0", "m1", "m2"]
        assert len(view) == 1_200_001

    def test_known_ids_drops_the_gaps(self):
        view = QueryView(QuerySpec.build("Email", "a"), ids=["m1", None, "m2"])
        assert view.known_ids == ["m1", "m2"]
        assert len(view) == 3

    def test_up_to_id_is_the_highest_cached_id(self):
        view = QueryView(QuerySpec.build("Email", "a"), ids=["m1", "m2", None, None])
        assert view.up_to_id == "m2"

    def test_up_to_id_of_an_empty_view(self):
        assert QueryView(QuerySpec.build("Email", "a")).up_to_id is None

    def test_repr_shows_how_much_is_cached(self):
        view = QueryView(QuerySpec.build("Email", "a"), ids=["m1", None])
        assert "1/2" in repr(view)


class TestQueryViewApply:
    def _view(self) -> QueryView:
        return QueryView(
            QuerySpec.build("Email", "a"),
            ids=["m1", "m2", "m3"],
            query_state="q1",
            can_calculate_changes=True,
        )

    def test_a_delta_is_spliced_in(self):
        view = self._view()
        view.apply(
            QueryChangesResponse.model_validate(
                {
                    "oldQueryState": "q1",
                    "newQueryState": "q2",
                    "removed": ["m2"],
                    "added": [{"id": "m0", "index": 0}],
                }
            )
        )
        assert view.ids == ["m0", "m1", "m3"]
        assert view.query_state == "q2"

    def test_total_is_updated_when_reported(self):
        view = self._view()
        view.apply(
            QueryChangesResponse.model_validate(
                {"oldQueryState": "q1", "newQueryState": "q2", "total": 2}
            )
        )
        assert view.total == 2
        assert len(view.ids) == 2

    def test_a_delta_without_a_total_leaves_none_rather_than_a_stale_one(self):
        # The old total described the list before the delta; kept, it made
        # len() report positions that had just been removed.
        view = QueryView(
            QuerySpec.build("Email", "a"),
            ids=["m1", "m2", "m3"],
            query_state="q1",
            total=3,
            can_calculate_changes=True,
        )
        view.apply(
            QueryChangesResponse.model_validate(
                {"oldQueryState": "q1", "newQueryState": "q2", "removed": ["m1", "m2"]}
            )
        )
        assert view.total is None
        assert len(view) == 1

    def test_a_delta_from_another_state_is_refused(self):
        # Applying out of order corrupts the list undetectably.
        view = self._view()
        with pytest.raises(StaleQueryViewError, match="re-run the query"):
            view.apply(
                QueryChangesResponse.model_validate({"oldQueryState": "q9", "newQueryState": "q10"})
            )

    def test_a_query_that_cannot_be_deltaed_refuses_up_front(self):
        view = self._view()
        view.can_calculate_changes = False
        with pytest.raises(UncacheableQueryError, match="cannot calculate changes"):
            view.apply(
                QueryChangesResponse.model_validate({"oldQueryState": "q1", "newQueryState": "q2"})
            )

    def test_reset_reseeds_from_a_fresh_query(self):
        # The recovery path for a stale view or cannotCalculateChanges.
        view = self._view()
        view.reset(
            QueryResponse.model_validate(
                {"ids": ["z1"], "queryState": "q9", "canCalculateChanges": True, "total": 1}
            )
        )
        assert view.ids == ["z1"]
        assert view.query_state == "q9"
        assert view.total == 1


class TestStateStore:
    def test_in_memory_round_trip(self):
        store = InMemoryStateStore()
        assert store.get("k") is None
        store.set("k", "s1")
        assert store.get("k") == "s1"
        store.delete("k")
        assert store.get("k") is None

    def test_deleting_an_unknown_key_is_not_an_error(self):
        InMemoryStateStore().delete("nope")

    def test_seeding_and_snapshotting(self):
        store = InMemoryStateStore({"a/Email": "s1"})
        assert "a/Email" in store
        assert len(store) == 1
        assert store.snapshot() == {"a/Email": "s1"}

    def test_the_snapshot_is_a_copy(self):
        store = InMemoryStateStore({"k": "v"})
        store.snapshot()["k"] = "tampered"
        assert store.get("k") == "v"

    def test_it_satisfies_the_protocol(self):
        assert isinstance(InMemoryStateStore(), StateStore)

    def test_repr(self):
        assert "1 keys" in repr(InMemoryStateStore({"k": "v"}))


class TestStateKeys:
    def test_type_key(self):
        assert type_key("a", "Email") == "a/Email"

    def test_query_keys_differ_by_filter(self):
        first = query_key(QuerySpec.build("Email", "a", filter={"x": 1}))
        second = query_key(QuerySpec.build("Email", "a", filter={"x": 2}))
        assert first != second

    def test_query_keys_are_stable_for_the_same_query(self):
        spec = QuerySpec.build("Email", "a", filter={"x": 1})
        assert query_key(spec) == query_key(spec)

    def test_query_keys_are_scoped_by_account_and_type(self):
        key = query_key(QuerySpec.build("Email", "acct1"))
        assert key.startswith("acct1/Email/query/")


class TestChangeSet:
    def test_absorbing_pages_accumulates(self):
        changes = ChangeSet("Email")
        changes.absorb(
            ChangesResponse.model_validate(
                {"newState": "s1", "created": ["m1"], "hasMoreChanges": True}
            )
        )
        changes.absorb(
            ChangesResponse.model_validate(
                {"newState": "s2", "updated": ["m1"], "destroyed": ["m0"]}
            )
        )
        assert changes.created == ["m1"]
        assert changes.updated == ["m1"]
        assert changes.destroyed == ["m0"]
        assert changes.new_state == "s2"
        assert changes.pages == 2
        assert len(changes) == 3

    def test_touched_deduplicates_across_created_and_updated(self):
        # A record created and then updated in one window needs fetching once.
        changes = ChangeSet("Email", created=["m1"], updated=["m1", "m2"])
        assert changes.touched == ["m1", "m2"]

    def test_the_categories_are_not_merged(self):
        # The server reported both, and a caller reconciling a cache needs to see
        # both.
        changes = ChangeSet("Email", created=["m1"], updated=["m1"])
        assert changes.created == ["m1"]
        assert changes.updated == ["m1"]

    def test_emptiness(self):
        assert ChangeSet("Email").is_empty
        assert not ChangeSet("Email", created=["m1"]).is_empty

    def test_repr(self):
        assert "created=1" in repr(ChangeSet("Email", created=["m1"]))


class TestHostileQueryResponses:
    @pytest.mark.parametrize("item", [{"id": "m1"}, {"index": 0}, {}])
    def test_an_added_item_needs_both_its_id_and_its_index(self, item):
        # RFC 8620 §5.6 requires both. A missing index read as 0 and a missing
        # id as a gap, so a malformed delta rewrote the view without a word.
        with pytest.raises(ValidationError):
            AddedItem.model_validate(item)

    def test_an_absurd_position_is_refused(self):
        # position/total/index size real allocations: [None] * 2**40 from a
        # fifty-byte response is a multi-terabyte list.
        response = QueryResponse.model_validate({"position": 2**40, "ids": []})
        with pytest.raises(ViewTooLargeError):
            QueryView.from_query(QuerySpec("Email", "a"), response)

    def test_an_absurd_total_allocates_nothing(self):
        # A view's total only ever truncates its list; it never sizes one, so a
        # hostile value has nothing to allocate.
        response = QueryResponse.model_validate({"position": 0, "ids": [], "total": 2**45})
        view = QueryView.from_query(QuerySpec("Email", "a"), response)
        assert view.ids == []
        assert view.total == 2**45

    def test_a_negative_total_is_refused_by_the_view(self):
        with pytest.raises(ViewTooLargeError):
            QueryView.from_query(
                QuerySpec("Email", "a"), QueryResponse.model_validate({"ids": [], "total": -1})
            )
        view = QueryView(
            QuerySpec("Email", "a"), ids=["m1"], query_state="q", can_calculate_changes=True
        )
        with pytest.raises(ViewTooLargeError):
            view.apply(
                QueryChangesResponse.model_validate(
                    {"oldQueryState": "q", "newQueryState": "q2", "total": -3}
                )
            )
        assert view.ids == ["m1"]

    def test_an_absurd_added_index_is_refused(self):
        with pytest.raises(ViewTooLargeError):
            splice(["a"], added=added(("x", 2**40)))

    def test_a_negative_added_index_is_refused(self):
        # Python's insert() would have accepted it and silently corrupted the
        # view via negative-index semantics.
        with pytest.raises(ViewTooLargeError):
            splice(["a", "b", "c"], added=added(("x", -2)))

    def test_a_negative_total_is_refused(self):
        with pytest.raises(ViewTooLargeError):
            splice(["a"], total=-5)


class TestSpliceMergeEquivalence:
    """The single merge pass must reproduce the spec's insert-per-item algorithm."""

    @staticmethod
    def _reference(
        ids: list[str | None],
        removed: list[str] | tuple[str, ...] = (),
        added: list[AddedItem] | tuple[AddedItem, ...] = (),
        total: int | None = None,
    ) -> list[str | None]:
        # RFC 8620 §5.6's own algorithm, executed literally.
        doomed = set(removed)
        result = [item for item in ids if item is None or item not in doomed]
        for item in sorted(added, key=lambda entry: entry.index or 0):
            index = item.index or 0
            if index > len(result):
                result.extend([None] * (index - len(result)))
            result.insert(index, item.id)
        if total is not None:
            if total < len(result):
                del result[total:]
            else:
                result.extend([None] * (total - len(result)))
        return result

    @given(
        ids=st.lists(st.one_of(st.none(), st.text("ab", min_size=1, max_size=2)), max_size=30),
        removals=st.lists(st.text("ab", min_size=1, max_size=2), max_size=10),
        add_pairs=st.lists(
            st.tuples(st.text("xyz", min_size=1, max_size=2), st.integers(0, 40)),
            max_size=10,
            unique_by=lambda pair: pair[1],
        ),
        total=st.one_of(st.none(), st.integers(0, 60)),
    )
    def test_matches_the_reference_algorithm(self, ids, removals, add_pairs, total):
        additions = added(*add_pairs)
        assert splice(ids, removed=removals, added=additions, total=total) == self._reference(
            ids, removed=removals, added=additions, total=total
        )

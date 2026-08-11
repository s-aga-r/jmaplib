"""Auto-chunking an oversized ``/get``, and the JSON narrowing helpers."""

from __future__ import annotations

from typing import Any

import pytest

from jmap.batch import Batch
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN
from jmap.capabilities.parsing import parser_for
from jmap.capabilities.registry import ActiveCapabilities, Registry
from jmap.capabilities.spec import DataTypeSpec, MethodKind, MethodSpec
from jmap.chunking import ChunkedHandle, TornReadError, chunk_get_call, merge_get_results
from jmap.core.errors import MethodError
from jmap.core.ids import Id
from jmap.core.invocation import Handle, MethodCall
from jmap.core.narrow import as_list, as_list_of, as_object, is_list, is_object
from jmap.core.session import Session
from jmap.models.base import JMAPObject
from jmap.models.mail.objects import Email
from jmap.models.responses import ChangesResponse, GetResponse, QueryResponse, SetResponse


def capabilities(max_objects_in_get: int = 100) -> ActiveCapabilities:
    registry = Registry()
    for spec in (CORE, MAIL):
        registry.register(spec)
    session = Session.from_wire(
        {
            "capabilities": {
                CORE_URN: {"maxObjectsInGet": max_objects_in_get, "maxCallsInRequest": 64},
                MAIL_URN: {},
            },
            "accounts": {"a": {"name": "alice"}},
            "primaryAccounts": {CORE_URN: "a", MAIL_URN: "a"},
        }
    )
    return registry.resolve(session, Id("a"))


def get_response(
    state: str, ids: list[str], not_found: list[str] | None = None
) -> GetResponse[Email]:
    return GetResponse[Email].model_validate(
        {"state": state, "list": [{"id": i} for i in ids], "notFound": not_found or []}
    )


class TestNarrowing:
    def test_type_guards(self):
        assert is_object({"a": 1})
        assert not is_object([1])
        assert is_list([1])
        assert not is_list({"a": 1})

    def test_as_object_and_as_list(self):
        assert as_object({"a": 1}) == {"a": 1}
        assert as_list([1, 2]) == [1, 2]

    def test_as_list_of_wraps_a_single_item(self):
        # bodyStructure holds one part; textBody holds many. Callers want both
        # treated the same way.
        assert as_list_of({"partId": "t"}) == [{"partId": "t"}]
        assert as_list_of([{"partId": "t"}]) == [{"partId": "t"}]


class TestSplitting:
    def test_a_small_get_is_not_chunked(self):
        batch = Batch(capabilities())
        handle = batch.add("Email/get", {"ids": [f"m{i}" for i in range(10)]})
        assert not isinstance(handle, ChunkedHandle)
        assert len(batch) == 1

    def test_an_oversized_get_is_split(self):
        batch = Batch(capabilities(max_objects_in_get=100))
        handle = batch.add("Email/get", {"ids": [f"m{i}" for i in range(250)]})
        assert isinstance(handle, ChunkedHandle)
        sizes = [len(chunk.call.arguments["ids"]) for chunk in handle.chunks]
        assert sizes == [100, 100, 50]
        assert len(batch) == 3

    def test_an_exact_multiple_splits_evenly(self):
        batch = Batch(capabilities(max_objects_in_get=100))
        handle = batch.add("Email/get", {"ids": [f"m{i}" for i in range(200)]})
        assert isinstance(handle, ChunkedHandle)
        assert [len(chunk.call.arguments["ids"]) for chunk in handle.chunks] == [100, 100]

    def test_exactly_the_limit_is_not_split(self):
        batch = Batch(capabilities(max_objects_in_get=100))
        handle = batch.add("Email/get", {"ids": [f"m{i}" for i in range(100)]})
        assert not isinstance(handle, ChunkedHandle)

    def test_chunks_keep_the_other_arguments(self):
        batch = Batch(capabilities(max_objects_in_get=2))
        handle = batch.add("Email/get", {"ids": ["a", "b", "c"], "properties": ["subject"]})
        assert isinstance(handle, ChunkedHandle)
        for chunk in handle.chunks:
            assert chunk.call.arguments["properties"] == ["subject"]
            assert chunk.call.arguments["accountId"] == "a"

    def test_chunks_get_distinct_call_ids(self):
        batch = Batch(capabilities(max_objects_in_get=1))
        handle = batch.add("Email/get", {"ids": ["a", "b", "c"]})
        assert isinstance(handle, ChunkedHandle)
        call_ids = [chunk.call_id for chunk in handle.chunks]
        assert len(set(call_ids)) == 3

    def test_ids_null_is_never_chunked(self):
        # `ids: null` means every record; the count is the server's problem.
        batch = Batch(capabilities(max_objects_in_get=1))
        assert not isinstance(batch.add("Email/get", {"ids": None}), ChunkedHandle)

    def test_a_back_reference_is_never_chunked(self):
        # A ResultRef has no length here - the server resolves it.
        batch = Batch(capabilities(max_objects_in_get=1))
        query = batch.add("Email/query", {})
        handle = batch.add("Email/get", {"ids": query.ref_ids()})
        assert not isinstance(handle, ChunkedHandle)

    def test_a_set_is_never_chunked(self):
        # Splitting a /set would break the single ifInState that makes it atomic.
        batch = Batch(capabilities(max_objects_in_get=1))
        handle = batch.add("Email/set", {"create": {"a": {}, "b": {}, "c": {}}})
        assert not isinstance(handle, ChunkedHandle)

    def test_chunk_get_call_helper(self):
        call = MethodCall("Email/get", {"ids": ["a", "b", "c"], "x": 1}, parse=dict)
        chunks = chunk_get_call(call, ["a", "b", "c"], 2)
        assert [chunk.arguments["ids"] for chunk in chunks] == [["a", "b"], ["c"]]
        assert all(chunk.arguments["x"] == 1 for chunk in chunks)


class TestMerging:
    def test_items_and_not_found_are_concatenated(self):
        merged = merge_get_results(
            "Email/get",
            [get_response("s1", ["m1", "m2"], ["x1"]), get_response("s1", ["m3"], ["x2"])],
        )
        assert [item.id for item in merged.items] == ["m1", "m2", "m3"]
        assert merged.not_found == ["x1", "x2"]

    def test_the_merged_result_keeps_its_type(self):
        merged = merge_get_results(
            "Email/get", [get_response("s1", ["m1"]), get_response("s1", [])]
        )
        assert isinstance(merged, GetResponse)
        assert isinstance(merged.items[0], Email)

    def test_a_state_change_mid_read_is_refused(self):
        # The chunks describe two different points in time; merging them would
        # produce a result that never existed on the server.
        with pytest.raises(TornReadError, match="changed mid-read") as excinfo:
            merge_get_results("Email/get", [get_response("s1", ["m1"]), get_response("s2", ["m2"])])
        assert excinfo.value.states == ("s1", "s2")

    def test_absent_states_do_not_trigger_a_false_positive(self):
        first = GetResponse[Email].model_validate({"list": []})
        second = GetResponse[Email].model_validate({"list": []})
        merge_get_results("Email/get", [first, second])


class TestChunkedHandle:
    def _handle(self, responses: list[GetResponse[Email]]) -> ChunkedHandle:
        chunks: list[Handle[GetResponse[Email]]] = []
        for index, response in enumerate(responses):
            call: MethodCall[GetResponse[Email]] = MethodCall(
                "Email/get", {}, parse=lambda args: GetResponse[Email].model_validate(dict(args))
            )
            handle: Handle[GetResponse[Email]] = Handle(f"c{index}", call)
            handle.fulfil(response)
            chunks.append(handle)
        return ChunkedHandle(chunks)

    def test_result_merges_every_chunk(self):
        handle = self._handle([get_response("s", ["m1"]), get_response("s", ["m2"])])
        assert [item.id for item in handle.result.items] == ["m1", "m2"]

    def test_a_single_chunk_is_returned_unmerged(self):
        handle = self._handle([get_response("s", ["m1"])])
        assert [item.id for item in handle.result.items] == ["m1"]

    def test_it_is_resolved_only_when_every_chunk_is(self):
        resolved: Handle[Any] = Handle("c0", MethodCall("Email/get", {}, parse=dict))
        resolved.fulfil(get_response("s", ["m1"]))
        pending: Handle[Any] = Handle("c1", MethodCall("Email/get", {}, parse=dict))
        handle = ChunkedHandle([resolved, pending])
        assert not handle.is_resolved

    def test_it_borrows_the_first_chunks_call_id_for_references(self):
        # A back-reference can only name a call the server has already answered,
        # which is the first chunk.
        handle = self._handle([get_response("s", ["m1"]), get_response("s", ["m2"])])
        assert handle.ref_ids().result_of == handle.chunks[0].call_id

    def test_a_failed_chunk_surfaces_as_the_handles_error(self):
        ok: Handle[Any] = Handle("c0", MethodCall("Email/get", {}, parse=dict))
        ok.fulfil(get_response("s", ["m1"]))
        bad: Handle[Any] = Handle("c1", MethodCall("Email/get", {}, parse=dict))
        bad.fail(MethodError("requestTooLarge", "c1", {}))

        handle = ChunkedHandle([ok, bad])
        assert handle.error is not None
        with pytest.raises(MethodError, match="requestTooLarge"):
            _ = handle.result


class TestParserSelection:
    """The typed annotations on the entity builders have to be true."""

    def test_get_yields_a_typed_get_response(self):
        method = MAIL.method("Email/get")
        data_type = MAIL.data_type("Email")
        assert method is not None
        assert data_type is not None
        parse = parser_for(method, data_type)
        result = parse({"state": "s", "list": [{"id": "m1", "subject": "Hi"}]})
        assert isinstance(result, GetResponse)
        assert result.items[0].subject == "Hi"

    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            ("Email/changes", ChangesResponse),
            ("Email/query", QueryResponse),
            ("Email/set", SetResponse),
        ],
    )
    def test_each_shape_maps_to_its_response(self, method_name, expected):
        method = MAIL.method(method_name)
        data_type = MAIL.data_type("Email")
        assert method is not None
        assert data_type is not None
        assert isinstance(parser_for(method, data_type)({}), expected)

    def test_custom_methods_keep_their_own_parser(self):
        # Email/parse and friends follow none of the six shapes.
        method = MAIL.method("Email/parse")
        data_type = MAIL.data_type("Email")
        assert method is not None
        parse = parser_for(method, data_type)
        assert parse({"parsed": {}}) == {"parsed": {}}

    def test_an_unknown_data_type_falls_back(self):
        method = MAIL.method("Email/get")
        assert method is not None
        assert parser_for(method, None)({"list": []}) == {"list": []}

    def test_an_unmodelled_type_still_gets_the_response_shape(self):
        # JMAPObject is the fallback model, so this is the shape an
        # advertised-but-unmodelled capability produces.
        method = MethodSpec("Vendor/get", MethodKind.GET)
        parse = parser_for(method, DataTypeSpec(name="Vendor"))
        result = parse({"state": "s", "list": [{"id": "v1", "someProp": 1}]})
        assert isinstance(result, GetResponse)
        assert isinstance(result.items[0], JMAPObject)
        assert result.items[0].some_prop == 1

    def test_response_classes_are_memoised(self):
        method = MAIL.method("Email/get")
        data_type = MAIL.data_type("Email")
        assert method is not None
        assert data_type is not None
        first = parser_for(method, data_type)({"list": []})
        second = parser_for(method, data_type)({"list": []})
        assert type(first) is type(second)

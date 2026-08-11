"""Method calls, back-references and handles (RFC 8620 §3.2, §3.7)."""

from __future__ import annotations

from typing import Any

import pytest

from jmap.core.errors import MethodError
from jmap.core.invocation import Handle, MethodCall, ParsedInvocation, ResultRef


def call(name: str = "Email/get", **arguments: Any) -> MethodCall[dict[str, Any]]:
    return MethodCall(name, arguments, parse=dict)


class TestResultRef:
    def test_wire_shape(self):
        ref: ResultRef[Any] = ResultRef("c0", "Email/query", "/ids")
        assert ref.to_wire() == {"resultOf": "c0", "name": "Email/query", "path": "/ids"}

    def test_equality_and_hashing(self):
        a: ResultRef[Any] = ResultRef("c0", "Email/query", "/ids")
        assert a == ResultRef("c0", "Email/query", "/ids")
        assert a != ResultRef("c1", "Email/query", "/ids")
        assert a != "not a ref"
        assert len({a, ResultRef("c0", "Email/query", "/ids")}) == 1


class TestMethodCall:
    def test_split_separates_refs_from_plain_arguments(self):
        ref: ResultRef[Any] = ResultRef("c0", "Email/query", "/ids")
        plain, refs = call(ids=ref, properties=["subject"]).split_arguments()
        assert plain == {"properties": ["subject"]}
        assert refs == {"ids": ref}

    def test_wire_arguments_rename_references(self):
        args = call(ids=ResultRef("c0", "Email/query", "/ids")).to_wire_arguments()
        assert "ids" not in args
        assert args["#ids"]["resultOf"] == "c0"

    def test_arguments_are_copied_not_aliased(self):
        original = {"a": 1}
        method = MethodCall("Core/echo", original, parse=dict)
        original["a"] = 2
        assert method.arguments == {"a": 1}

    def test_no_references_leaves_arguments_untouched(self):
        assert call(ids=["m1"]).to_wire_arguments() == {"ids": ["m1"]}


class TestParsedInvocation:
    def test_from_wire(self):
        parsed = ParsedInvocation.from_wire(["Email/get", {"list": []}, "c0"])
        assert (parsed.name, parsed.method_call_id) == ("Email/get", "c0")

    @pytest.mark.parametrize("triple", [[], ["a"], ["a", {}], ["a", {}, "c", "extra"]])
    def test_wrong_arity_rejected(self, triple):
        with pytest.raises(ValueError, match="3 elements"):
            ParsedInvocation.from_wire(triple)


class TestHandle:
    def test_reference_helpers_build_the_right_paths(self):
        handle = Handle("c0", call("Email/query"))
        assert handle.ref_ids().path == "/ids"
        assert handle.ref_list("threadId").path == "/list/*/threadId"
        assert handle.ref_created("d1").path == "/created/d1/id"
        assert handle.ref_created("d1", "blobId").path == "/created/d1/blobId"
        assert handle.ref_updated().path == "/updated"
        assert handle.ref_updated_properties().path == "/updatedProperties"
        assert handle.ref("/custom/path").path == "/custom/path"

    def test_references_carry_the_target_call_name(self):
        handle = Handle("c0", call("Email/query"))
        ref = handle.ref_ids()
        assert (ref.result_of, ref.name) == ("c0", "Email/query")

    def test_reading_before_execution_is_an_error(self):
        handle = Handle("c0", call())
        with pytest.raises(RuntimeError, match="has not been executed"):
            _ = handle.result

    def test_result_after_fulfilment(self):
        handle: Handle[dict[str, Any]] = Handle("c0", call())
        handle._fulfil({"list": [{"id": "m1"}]})
        assert handle.result == {"list": [{"id": "m1"}]}
        assert handle.error is None
        assert handle.is_resolved

    def test_method_error_raises_only_when_the_result_is_read(self):
        # Deferring the raise is what stops one failed call in a batch from
        # discarding its siblings' results.
        handle = Handle("c0", call())
        error = MethodError("unknownMethod", "c0", {})
        handle._fail(error)

        assert handle.error is error
        with pytest.raises(MethodError, match="unknownMethod"):
            _ = handle.result

    def test_implicit_responses_are_kept_separate_from_the_result(self):
        # RFC 8621 §7.5: an EmailSubmission/set may emit an extra Email/set
        # sharing the same call id. Merging it would corrupt the result.
        handle: Handle[dict[str, Any]] = Handle("c0", call("EmailSubmission/set"))
        extra = ParsedInvocation("Email/set", {"updated": {"m1": None}}, "c0")
        handle._fulfil({"created": {"s1": {"id": "s1"}}}, extra=(extra,))

        assert handle.result == {"created": {"s1": {"id": "s1"}}}
        assert len(handle.extra) == 1
        assert handle.extra[0].name == "Email/set"

    def test_repr_reflects_state(self):
        handle = Handle("c0", call("Email/get"))
        assert "pending" in repr(handle)
        handle._fulfil({})
        assert "ok" in repr(handle)

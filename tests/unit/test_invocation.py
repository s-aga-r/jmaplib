"""Method calls, back-references and handles (RFC 8620 §3.2, §3.7)."""

from __future__ import annotations

from typing import Any

import pytest

from jmap.core.errors import MethodError
from jmap.core.ids import CreationRef
from jmap.core.invocation import (
    Handle,
    MethodCall,
    NestedResultRefError,
    ParsedInvocation,
    ResultRef,
)


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


class TestCreationReferences:
    """RFC 8620 §5.3 - the ``#id`` form, and the only way to point at an object
    created earlier in the same request from *inside* another object."""

    def test_a_creation_ref_becomes_a_hash_string(self):
        args = call(
            "EmailSubmission/set", create={"s1": {"emailId": CreationRef("d1")}}
        ).to_wire_arguments()
        assert args["create"]["s1"]["emailId"] == "#d1"

    def test_it_is_converted_at_any_depth(self):
        method = call("Email/set", create={"e1": {"mailboxIds": [CreationRef("m1")]}})
        args = method.to_wire_arguments()
        assert args["create"]["e1"]["mailboxIds"] == ["#m1"]

    def test_a_top_level_creation_ref_is_converted_too(self):
        assert call("Email/set", destroy=CreationRef("d1")).to_wire_arguments() == {
            "destroy": "#d1"
        }

    def test_the_result_is_json_serialisable(self):
        # The regression this exists for: CreationRef documented itself as
        # serialising to "#id" while nothing ever called __str__, so it reached
        # json.dumps as an object and died there.
        from jmap.core.ijson import dumps

        wire = call("Email/set", create={"e1": {"x": CreationRef("d1")}}).to_wire_arguments()
        assert "#d1" in dumps(wire)


class Wired:
    """Anything with a ``to_wire()``, as a typed model has."""

    def __init__(self, wire: Any) -> None:
        self.wire = wire

    def to_wire(self) -> Any:
        return self.wire


class TestObjectsThatSerialiseThemselves:
    """A typed model goes wherever its wire object can: every object with a
    ``to_wire()`` is replaced by what it returns."""

    def test_an_object_is_replaced_by_its_wire_form(self):
        args = call("Mailbox/set", create={"k": Wired({"name": "R"})}).to_wire_arguments()
        assert args == {"create": {"k": {"name": "R"}}}

    def test_a_top_level_argument_is_converted_too(self):
        assert call("Mailbox/set", create=Wired({"k": {}})).to_wire_arguments() == {
            "create": {"k": {}}
        }

    def test_the_wire_form_is_converted_in_turn(self):
        wired = Wired({"parentId": CreationRef("p"), "tags": (Wired("t"),)})
        args = call("Mailbox/set", create={"k": wired}).to_wire_arguments()
        assert args["create"]["k"] == {"parentId": "#p", "tags": ["t"]}

    def test_a_back_reference_it_hides_is_still_refused(self):
        ref: ResultRef[Any] = ResultRef("c0", "Email/query", "/ids")
        method = call("Mailbox/set", create={"k": Wired({"parentId": ref})})
        with pytest.raises(NestedResultRefError) as excinfo:
            method.to_wire_arguments()
        assert excinfo.value.path == "/create/k/parentId"

    def test_a_real_model_serialises_only_the_fields_it_was_given(self):
        from jmap.core.ijson import dumps
        from jmap.models.mail.objects import Mailbox

        args = call("Mailbox/set", create={"k": Mailbox(name="R", parent_id=None)})
        assert dumps(args.to_wire_arguments()) == '{"create":{"k":{"name":"R","parentId":null}}}'

    @pytest.mark.parametrize("value", [object(), {1, 2}])
    def test_anything_else_passes_through_for_json_to_judge(self, value):
        # Unchanged, as before: serialisation is where a value JSON cannot
        # carry is refused.
        assert call(x=[value]).to_wire_arguments()["x"][0] is value

    def test_a_to_wire_that_is_not_a_method_is_not_called(self):
        class Named:
            to_wire = "a property of the object, not a method"

        value = Named()
        assert call(x=value).to_wire_arguments() == {"x": value}


class TestNestedBackReferences:
    """§3.7 renames the argument to carry a reference, so there is nowhere to put
    one that is not a whole top-level argument."""

    def test_a_nested_ref_is_refused_locally(self):
        ref: ResultRef[Any] = ResultRef("c0", "Email/set", "/created/d1/id")
        method = call("EmailSubmission/set", create={"s1": {"emailId": ref}})
        with pytest.raises(NestedResultRefError) as excinfo:
            method.to_wire_arguments()
        assert excinfo.value.path == "/create/s1/emailId"

    def test_the_error_names_the_alternative_that_works(self):
        method = call("Email/set", create={"e1": {"x": ResultRef("c0", "Email/query", "/ids")}})
        with pytest.raises(NestedResultRefError, match="CreationRef"):
            method.to_wire_arguments()

    def test_a_ref_inside_a_list_is_caught_with_its_index(self):
        method = call("Email/set", update={"m1": {"tags": [ResultRef("c0", "X/get", "/a")]}})
        with pytest.raises(NestedResultRefError) as excinfo:
            method.to_wire_arguments()
        assert excinfo.value.path == "/update/m1/tags/0"

    def test_a_top_level_ref_is_still_perfectly_legal(self):
        assert "#ids" in call(ids=ResultRef("c0", "Email/query", "/ids")).to_wire_arguments()


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
        handle.fulfil({"list": [{"id": "m1"}]})
        assert handle.result == {"list": [{"id": "m1"}]}
        assert handle.error is None
        assert handle.is_resolved

    def test_method_error_raises_only_when_the_result_is_read(self):
        # Deferring the raise is what stops one failed call in a batch from
        # discarding its siblings' results.
        handle = Handle("c0", call())
        error = MethodError("unknownMethod", "c0", {})
        handle.fail(error)

        assert handle.error is error
        with pytest.raises(MethodError, match="unknownMethod"):
            _ = handle.result

    def test_implicit_responses_are_kept_separate_from_the_result(self):
        # RFC 8621 §7.5: an EmailSubmission/set may emit an extra Email/set
        # sharing the same call id. Merging it would corrupt the result.
        handle: Handle[dict[str, Any]] = Handle("c0", call("EmailSubmission/set"))
        extra = ParsedInvocation("Email/set", {"updated": {"m1": None}}, "c0")
        handle.fulfil({"created": {"s1": {"id": "s1"}}}, extra=(extra,))

        assert handle.result == {"created": {"s1": {"id": "s1"}}}
        assert len(handle.extra) == 1
        assert handle.extra[0].name == "Email/set"

    def test_repr_reflects_state(self):
        handle = Handle("c0", call("Email/get"))
        assert "pending" in repr(handle)
        handle.fulfil({})
        assert "ok" in repr(handle)

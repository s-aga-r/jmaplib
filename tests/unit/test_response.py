"""Response parsing and routing back to handles (RFC 8620 §3.4, §3.6.2)."""

from __future__ import annotations

from typing import Any

import pytest

from jmap.core.errors import MethodError, ServerPartialFailError
from jmap.core.ids import Id
from jmap.core.invocation import Handle, MethodCall
from jmap.core.response import (
    MalformedResponseError,
    Response,
    dispatch,
)


def call(name: str = "Email/get") -> MethodCall[dict[str, Any]]:
    return MethodCall(name, {}, parse=dict)


def handle(call_id: str = "c0", name: str = "Email/get") -> Handle[dict[str, Any]]:
    return Handle(call_id, call(name))


class TestParsing:
    def test_full_response(self):
        response = Response.from_wire(
            {
                "methodResponses": [["Email/get", {"list": []}, "c0"]],
                "createdIds": {"draft": "M1"},
                "sessionState": "abc",
            }
        )
        assert len(response.method_responses) == 1
        assert response.created_ids == {"draft": Id("M1")}
        assert response.session_state == "abc"

    def test_absent_optional_members(self):
        response = Response.from_wire({"methodResponses": []})
        assert response.created_ids == {}
        assert response.session_state == ""

    def test_completely_empty_body(self):
        assert Response.from_wire({}).method_responses == ()

    @pytest.mark.parametrize("bad", [{"methodResponses": {}}, {"methodResponses": "x"}])
    def test_method_responses_must_be_an_array(self, bad):
        with pytest.raises(MalformedResponseError, match="methodResponses"):
            Response.from_wire(bad)

    def test_created_ids_must_be_an_object(self):
        with pytest.raises(MalformedResponseError, match="createdIds"):
            Response.from_wire({"methodResponses": [], "createdIds": []})

    def test_repr_lists_method_names(self):
        response = Response.from_wire(
            {"methodResponses": [["Email/get", {}, "c0"]], "sessionState": "s"}
        )
        assert "Email/get" in repr(response)


class TestDispatch:
    def test_result_reaches_its_handle(self):
        target = handle()
        dispatch(
            Response.from_wire({"methodResponses": [["Email/get", {"list": [1]}, "c0"]]}),
            [target],
        )
        assert target.result == {"list": [1]}

    def test_handles_are_matched_by_call_id_not_position(self):
        first, second = handle("c0"), handle("c1")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["Email/get", {"who": "second"}, "c1"],
                    ["Email/get", {"who": "first"}, "c0"],
                ]
            }
        )
        dispatch(response, [first, second])
        assert first.result == {"who": "first"}
        assert second.result == {"who": "second"}

    def test_an_error_response_fails_only_its_own_call(self):
        good, bad = handle("c0"), handle("c1")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["Email/get", {"list": []}, "c0"],
                    ["error", {"type": "unknownMethod"}, "c1"],
                ]
            }
        )
        dispatch(response, [good, bad])

        assert good.result == {"list": []}
        assert isinstance(bad.error, MethodError)
        with pytest.raises(MethodError, match="unknownMethod"):
            _ = bad.result

    def test_server_partial_fail_gets_its_own_type(self):
        # The one error after which server state may have changed, so retry
        # logic has to distinguish it.
        target = handle()
        dispatch(
            Response.from_wire(
                {"methodResponses": [["error", {"type": "serverPartialFail"}, "c0"]]}
            ),
            [target],
        )
        assert isinstance(target.error, ServerPartialFailError)

    def test_error_with_no_type(self):
        target = handle()
        dispatch(Response.from_wire({"methodResponses": [["error", {}, "c0"]]}), [target])
        assert target.error is not None
        assert target.error.type == "unknownError"

    def test_a_handle_with_no_response_is_left_pending(self):
        # Happens when a batch is split: this request only answers some of it.
        target = handle()
        dispatch(Response.from_wire({"methodResponses": []}), [target])
        assert not target.is_resolved

    def test_unknown_call_ids_are_ignored(self):
        target = handle("c0")
        dispatch(
            Response.from_wire({"methodResponses": [["Email/get", {}, "stranger"]]}),
            [target],
        )
        assert not target.is_resolved


class TestImplicitResponses:
    """One call id, several responses (RFC 8621 §5.3, RFC 8620 §5.4)."""

    def test_implicit_response_is_kept_beside_the_result(self):
        target = handle("c0", "EmailSubmission/set")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["EmailSubmission/set", {"created": {"s1": {}}}, "c0"],
                    ["Email/set", {"updated": {"m1": None}}, "c0"],
                ]
            }
        )
        dispatch(response, [target])

        assert target.result == {"created": {"s1": {}}}
        assert [i.name for i in target.extra] == ["Email/set"]

    def test_the_answer_is_found_by_name_not_by_position(self):
        # The server may order them either way; taking the first would hand the
        # caller an Email/set response as if it were the submission's.
        target = handle("c0", "EmailSubmission/set")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["Email/set", {"updated": {"m1": None}}, "c0"],
                    ["EmailSubmission/set", {"created": {"s1": {}}}, "c0"],
                ]
            }
        )
        dispatch(response, [target])

        assert target.result == {"created": {"s1": {}}}
        assert [i.name for i in target.extra] == ["Email/set"]

    def test_an_error_wins_over_an_implicit_response(self):
        target = handle("c0", "EmailSubmission/set")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["Email/set", {}, "c0"],
                    ["error", {"type": "forbidden"}, "c0"],
                ]
            }
        )
        dispatch(response, [target])
        assert target.error is not None
        assert target.error.type == "forbidden"

    def test_unrecognisable_names_fall_back_to_position(self):
        # A response we cannot classify is still better than none.
        target = handle("c0", "Email/get")
        response = Response.from_wire(
            {"methodResponses": [["Vendor/weird", {"a": 1}, "c0"], ["Other/thing", {}, "c0"]]}
        )
        dispatch(response, [target])
        assert target.result == {"a": 1}
        assert len(target.extra) == 1


class TestHostileResponses:
    def test_a_non_string_created_id_is_malformed(self):
        # These values are echoed into the next request's createdIds and handed
        # to application code as Ids; reifying {'nested': 'obj'} via str() would
        # put a Python repr on the wire.
        for hostile in (123, {"nested": "obj"}, None, ["x"]):
            with pytest.raises(MalformedResponseError, match="createdIds"):
                Response.from_wire({"methodResponses": [], "createdIds": {"d": hostile}})

    def test_a_non_object_arguments_slot_is_rejected(self):
        # As a MalformedResponseError: a plain ValueError got past the
        # `except JMAPError` the docs promise, out of every batch it hit.
        with pytest.raises(MalformedResponseError, match="arguments"):
            Response.from_wire({"methodResponses": [["Email/get", "not-an-object", "c0"]]})

    def test_a_three_key_dict_is_not_an_invocation(self):
        # Any length-3 Sized unpacks by iteration - a 3-key dict yields its
        # keys - and the failure then surfaced far from the malformed response.
        with pytest.raises(MalformedResponseError, match="3 elements"):
            Response.from_wire({"methodResponses": [{"a": 1, "b": 2, "c": 3}]})

    def test_a_parse_failure_is_contained_to_its_own_handle(self):
        # One malformed method response must not abort dispatch mid-loop and
        # leave the sibling handles unresolved.
        def explode(_arguments):
            raise ValueError("garbage shape")

        broken = Handle("c0", MethodCall("Email/get", {}, parse=explode))
        sibling = handle("c1")
        response = Response.from_wire(
            {
                "methodResponses": [
                    ["Email/get", {"bad": True}, "c0"],
                    ["Email/get", {"list": []}, "c1"],
                ]
            }
        )
        dispatch(response, [broken, sibling])
        assert broken.error is not None
        assert broken.error.type == "malformedResult"
        assert sibling.result == {"list": []}

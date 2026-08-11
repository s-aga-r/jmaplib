"""The debugging surface: ``__repr__`` output and the handle/call adapter.

Reprs are API. When a batch misbehaves the first thing anyone does is print it,
and a repr that omits the call id or the method name wastes the session.
"""

from __future__ import annotations

from typing import Any

from jmap.core.invocation import Handle, MethodCall, ParsedInvocation, ResultRef
from jmap.core.request import Request, handles_to_calls
from jmap.core.session import Session


def call(name: str = "Email/get", **arguments: Any) -> MethodCall[dict[str, Any]]:
    return MethodCall(name, arguments, parse=dict)


class TestReprs:
    def test_result_ref_shows_all_three_fields(self):
        text = repr(ResultRef("c0", "Email/query", "/ids"))
        assert "c0" in text
        assert "Email/query" in text
        assert "/ids" in text

    def test_method_call_shows_name_and_argument_names(self):
        text = repr(call("Email/get", ids=["m1"], properties=["subject"]))
        assert "Email/get" in text
        assert "ids" in text
        assert "properties" in text

    def test_parsed_invocation_shows_name_and_id(self):
        text = repr(ParsedInvocation("Email/get", {}, "c7"))
        assert "Email/get" in text
        assert "c7" in text

    def test_handle_repr_tracks_state_transitions(self):
        handle: Handle[dict[str, Any]] = Handle("c0", call("Email/query"))
        assert "pending" in repr(handle)
        handle._fulfil({})
        assert "ok" in repr(handle)

    def test_request_repr_lists_method_names_and_using(self):
        request = Request(
            frozenset({"urn:ietf:params:jmap:core"}),
            [("c0", call("Email/query")), ("c1", call("Email/get"))],
        )
        text = repr(request)
        assert "Email/query" in text
        assert "Email/get" in text
        assert "urn:ietf:params:jmap:core" in text

    def test_session_repr_summarises_rather_than_dumps(self):
        session = Session.from_wire(
            {
                "username": "alice@example.com",
                "capabilities": {"urn:ietf:params:jmap:core": {}},
                "accounts": {"a": {"name": "alice"}},
                "state": "s1",
            }
        )
        text = repr(session)
        assert "alice@example.com" in text
        assert "accounts=1" in text
        assert "capabilities=1" in text


class TestHandlesToCalls:
    def test_adapts_handles_preserving_order(self):
        handles = [Handle(f"c{i}", call(f"Email/get{i}")) for i in range(3)]
        pairs = handles_to_calls(handles)
        assert [call_id for call_id, _ in pairs] == ["c0", "c1", "c2"]
        assert [c.name for _, c in pairs] == ["Email/get0", "Email/get1", "Email/get2"]

    def test_empty_input(self):
        assert handles_to_calls([]) == []

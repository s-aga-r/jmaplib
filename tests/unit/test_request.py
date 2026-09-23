"""Request assembly: reference validation, DAG-safe splitting, createdIds."""

from __future__ import annotations

from typing import Any

import pytest

from jmap.core.errors import BatchTooLargeError
from jmap.core.ids import Id
from jmap.core.invocation import MethodCall, ResultRef
from jmap.core.request import (
    CORE_URN,
    InvalidReferenceError,
    Request,
    derive_using,
    plan_requests,
    validate_references,
)

USING = frozenset({CORE_URN, "urn:ietf:params:jmap:mail"})


def call(name: str, **arguments: Any) -> MethodCall[dict[str, Any]]:
    return MethodCall(name, arguments, parse=dict)


def pairs(*specs: tuple[str, MethodCall[Any]]) -> list[tuple[str, MethodCall[Any]]]:
    return list(specs)


class TestSerialisation:
    def test_back_reference_renames_the_argument(self):
        query = call("Email/query", filter={"inMailbox": "m1"})
        get = call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))
        request = Request(USING, pairs(("c0", query), ("c1", get)))

        wire = request.to_wire()
        get_args = wire["methodCalls"][1][1]

        assert "ids" not in get_args
        assert get_args["#ids"] == {
            "resultOf": "c0",
            "name": "Email/query",
            "path": "/ids",
        }

    def test_using_is_sorted_for_deterministic_wire_output(self):
        request = Request(USING, pairs(("c0", call("Core/echo"))))
        assert request.to_wire()["using"] == sorted(USING)

    def test_created_ids_omitted_when_absent(self):
        assert "createdIds" not in Request(USING, pairs(("c0", call("Core/echo")))).to_wire()

    def test_created_ids_included_when_present(self):
        request = Request(USING, pairs(("c0", call("Core/echo"))), {"draft": Id("M1")})
        assert request.to_wire()["createdIds"] == {"draft": "M1"}

    def test_invocation_triple_shape(self):
        wire = Request(USING, pairs(("c0", call("Core/echo", hi=1)))).to_wire()
        assert wire["methodCalls"] == [["Core/echo", {"hi": 1}, "c0"]]


class TestReferenceValidation:
    def test_forward_reference_is_rejected(self):
        calls = pairs(
            ("c0", call("Email/get", ids=ResultRef("c1", "Email/query", "/ids"))),
            ("c1", call("Email/query")),
        )
        with pytest.raises(InvalidReferenceError, match="not an earlier call"):
            validate_references(calls)

    def test_self_reference_is_rejected(self):
        calls = pairs(("c0", call("Email/get", ids=ResultRef("c0", "Email/get", "/ids"))))
        with pytest.raises(InvalidReferenceError, match="not an earlier call"):
            validate_references(calls)

    def test_unknown_target_is_rejected(self):
        calls = pairs(("c0", call("Email/get", ids=ResultRef("nope", "Email/query", "/ids"))))
        with pytest.raises(InvalidReferenceError, match="not an earlier call"):
            validate_references(calls)

    def test_method_name_mismatch_is_rejected(self):
        # The server matches on name; a mismatch is how JMAP stops you reading
        # fields off an `error` response.
        calls = pairs(
            ("c0", call("Mailbox/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
        )
        with pytest.raises(InvalidReferenceError, match="expects 'Email/query'"):
            validate_references(calls)

    def test_valid_chain_passes(self):
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
        )
        validate_references(calls)


class TestSplitting:
    def test_no_split_when_within_limit(self):
        calls = pairs(*[(f"c{i}", call("Core/echo")) for i in range(5)])
        assert len(plan_requests(calls, using=USING, max_calls_in_request=16)) == 1

    def test_independent_calls_split_freely(self):
        calls = pairs(*[(f"c{i}", call("Core/echo")) for i in range(5)])
        plans = plan_requests(calls, using=USING, max_calls_in_request=2)
        assert [len(p.method_calls) for p in plans] == [2, 2, 1]

    def test_referenced_pair_is_never_cut(self):
        # 4 calls, limit 3, where c2->c3 are joined. A naive slice would cut them.
        calls = pairs(
            ("c0", call("Core/echo")),
            ("c1", call("Core/echo")),
            ("c2", call("Email/query")),
            ("c3", call("Email/get", ids=ResultRef("c2", "Email/query", "/ids"))),
        )
        plans = plan_requests(calls, using=USING, max_calls_in_request=3)

        joined = [[cid for cid, _ in p.method_calls] for p in plans]
        for batch in joined:
            assert ("c2" in batch) == ("c3" in batch), f"reference cut across {joined}"

    def test_component_larger_than_limit_is_unsendable(self):
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
            ("c2", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
        )
        with pytest.raises(BatchTooLargeError) as excinfo:
            plan_requests(calls, using=USING, max_calls_in_request=2)
        assert set(excinfo.value.call_ids) == {"c0", "c1", "c2"}
        assert excinfo.value.limit == 2

    def test_transitive_chain_stays_together(self):
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
            ("c2", call("Thread/get", ids=ResultRef("c1", "Email/get", "/list/*/threadId"))),
        )
        plans = plan_requests(calls, using=USING, max_calls_in_request=3)
        assert len(plans) == 1

    def test_call_order_is_preserved_across_the_split(self):
        calls = pairs(*[(f"c{i}", call("Core/echo")) for i in range(6)])
        plans = plan_requests(calls, using=USING, max_calls_in_request=2)
        flattened = [cid for p in plans for cid, _ in p.method_calls]
        assert flattened == [f"c{i}" for i in range(6)]

    def test_interleaved_references_are_refused_rather_than_reordered(self):
        # c3 references c0 across c1 and c2. Packing the pair together meant
        # sending c3 in the first request and c1 (a destroy) in the second - so
        # a read queued after a write ran before it. JMAP executes calls in
        # order, so that silently changes what the read sees.
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/set", destroy=["m9"])),
            ("c2", call("Email/set", create={"d": {}})),
            ("c3", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
        )
        with pytest.raises(BatchTooLargeError) as excinfo:
            plan_requests(calls, using=USING, max_calls_in_request=2)
        assert excinfo.value.call_ids == ("c0", "c1", "c2", "c3")

    def test_a_split_only_ever_cuts_between_calls_in_order(self):
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
            ("c2", call("Core/echo")),
            ("c3", call("Mailbox/query")),
            ("c4", call("Mailbox/get", ids=ResultRef("c3", "Mailbox/query", "/ids"))),
        )
        plans = plan_requests(calls, using=USING, max_calls_in_request=3)
        assert [[cid for cid, _ in p.method_calls] for p in plans] == [
            ["c0", "c1", "c2"],
            ["c3", "c4"],
        ]

    def test_created_ids_only_on_the_first_request(self):
        calls = pairs(*[(f"c{i}", call("Core/echo")) for i in range(4)])
        plans = plan_requests(
            calls, using=USING, max_calls_in_request=2, created_ids={"a": Id("A1")}
        )
        assert plans[0].created_ids == {"a": Id("A1")}
        assert all(p.created_ids is None for p in plans[1:])

    def test_empty_batch_plans_nothing(self):
        assert plan_requests([], using=USING, max_calls_in_request=16) == []

    @pytest.mark.parametrize("limit", [0, -1])
    def test_nonsense_limit_rejected(self, limit):
        calls = pairs(("c0", call("Core/echo")))
        with pytest.raises(ValueError, match="must be >= 1"):
            plan_requests(calls, using=USING, max_calls_in_request=limit)

    def test_stalwart_limit_of_16(self):
        # Stalwart v0.16 advertises maxCallsInRequest=16, so the splitter runs
        # constantly against the primary CI target.
        calls = pairs(*[(f"c{i}", call("Core/echo")) for i in range(40)])
        plans = plan_requests(calls, using=USING, max_calls_in_request=16)
        assert [len(p.method_calls) for p in plans] == [16, 16, 8]


class TestDeriveUsing:
    def test_core_is_always_included(self):
        calls = pairs(("c0", call("Email/get")))
        result = derive_using(calls, resolver=lambda _names, _extra: frozenset())
        assert CORE_URN in result

    def test_resolver_output_and_extras_are_unioned(self):
        calls = pairs(("c0", call("Email/get")))

        def resolver(names, extra):
            assert names == ["Email/get"]
            return frozenset({"urn:ietf:params:jmap:mail"}) | extra

        result = derive_using(
            calls, resolver=resolver, extra=frozenset({"urn:ietf:params:jmap:smimeverify"})
        )
        assert result == {
            CORE_URN,
            "urn:ietf:params:jmap:mail",
            "urn:ietf:params:jmap:smimeverify",
        }


class TestSeveralReferences:
    """Calls whose references overlap, which the cut points must all respect."""

    def test_two_calls_sharing_a_target(self):
        # c2 references both c0 and c1, and c1 already references c0. All three
        # must land together.
        calls = pairs(
            ("c0", call("Email/query")),
            ("c1", call("Email/get", ids=ResultRef("c0", "Email/query", "/ids"))),
            (
                "c2",
                call(
                    "Thread/get",
                    ids=ResultRef("c1", "Email/get", "/list/*/threadId"),
                    other=ResultRef("c0", "Email/query", "/ids"),
                ),
            ),
        )
        plans = plan_requests(calls, using=USING, max_calls_in_request=3)
        assert len(plans) == 1
        assert [cid for cid, _ in plans[0].method_calls] == ["c0", "c1", "c2"]

    def test_two_references_to_the_same_call_from_one_call(self):
        calls = pairs(
            ("c0", call("Email/query")),
            (
                "c1",
                call(
                    "Email/get",
                    ids=ResultRef("c0", "Email/query", "/ids"),
                    properties=ResultRef("c0", "Email/query", "/queryState"),
                ),
            ),
        )
        plans = plan_requests(calls, using=USING, max_calls_in_request=2)
        assert len(plans) == 1

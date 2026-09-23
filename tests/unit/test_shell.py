"""Decisions shared by both client shells.

These are the parts that must behave identically in sync and async, so they live
in one place and are tested once.
"""

from __future__ import annotations

import pytest

from jmap._shell import (
    JMAP_CONTENT_TYPE,
    as_json_object,
    failure_of,
    merged_created_ids,
    problem_of,
    request_headers,
    retry_pause,
    session_is_stale,
)
from jmap.capabilities.spec import CapabilitySpec
from jmap.core.errors import RequestError, TransportError
from jmap.core.retry import RetryPolicy, Safety
from jmap.core.session import Session
from jmap.defaults import default_registry

PROBLEM = {"Content-Type": "application/problem+json"}


class TestHeaders:
    def test_content_type_is_json(self):
        headers = request_headers()
        assert headers["Content-Type"] == JMAP_CONTENT_TYPE
        assert headers["Accept"] == JMAP_CONTENT_TYPE

    def test_accept_language_is_opt_in(self):
        # RFC 8620 §3.8: the server SHOULD localise error text to it, but sending
        # one unasked would silently change what the server returns.
        assert "Accept-Language" not in request_headers()
        assert request_headers(accept_language="fr")["Accept-Language"] == "fr"

    def test_empty_accept_language_is_ignored(self):
        assert "Accept-Language" not in request_headers(accept_language="")


class TestProblemOf:
    def test_success_is_not_a_problem(self):
        assert problem_of(200, {}, b"{}") is None

    def test_parses_rfc7807(self):
        body = (
            b'{"type":"urn:ietf:params:jmap:error:limit","status":400,"limit":"maxCallsInRequest"}'
        )
        problem = problem_of(400, PROBLEM, body)
        assert problem is not None
        assert problem.limit == "maxCallsInRequest"

    def test_malformed_problem_body_still_yields_an_error(self):
        # A broken body must not become a JSON decode traceback.
        problem = problem_of(400, PROBLEM, b"not json at all")
        assert problem is not None
        assert problem.status == 400

    def test_problem_body_that_is_not_an_object(self):
        problem = problem_of(400, PROBLEM, b'"a string"')
        assert problem is not None
        assert problem.type == "about:blank"

    def test_non_problem_error_bodies_become_about_blank(self):
        # An intermediary returning HTML is the common case here.
        problem = problem_of(502, {"Content-Type": "text/html"}, b"<html>oops</html>")
        assert problem is not None
        assert problem.type == "about:blank"
        assert problem.status == 502
        assert problem.detail is not None
        assert "oops" in problem.detail

    def test_long_bodies_are_truncated(self):
        problem = problem_of(500, {}, b"x" * 5000)
        assert problem is not None
        assert problem.detail is not None
        assert len(problem.detail) <= 512

    def test_empty_body_has_no_detail(self):
        problem = problem_of(500, {}, b"")
        assert problem is not None
        assert problem.detail is None

    def test_lowercase_content_type_header(self):
        problem = problem_of(400, {"content-type": "application/problem+json"}, b'{"type":"x"}')
        assert problem is not None
        assert problem.type == "x"


class TestAsJsonObject:
    def test_passes_an_object_through(self):
        assert as_json_object({"a": 1}, "the API") == {"a": 1}

    @pytest.mark.parametrize("value", [[], "text", 1, None, True])
    def test_names_the_endpoint_that_misbehaved(self, value):
        with pytest.raises(TransportError, match="the session endpoint"):
            as_json_object(value, "the session endpoint")


class TestFailureOf:
    def test_uses_the_problem_type_when_present(self):
        problem = RequestError("urn:ietf:params:jmap:error:limit", status=400)
        assert failure_of(400, problem) is Safety.NEVER_APPLIED

    def test_falls_back_to_the_status(self):
        assert failure_of(503, None) is Safety.NEVER_APPLIED
        assert failure_of(400, None) is Safety.FUTILE


class TestRetryPause:
    @staticmethod
    def pause(headers: dict[str, str], policy: RetryPolicy) -> tuple[float | None, RequestError]:
        problem = RequestError("about:blank", status=503)
        return retry_pause(problem, headers, policy=policy, attempt=1, now=0.0), problem

    def test_the_server_hint_wins_and_is_recorded(self):
        delay, problem = self.pause({"retry-after": "7"}, RetryPolicy(initial_backoff=1.0))
        assert delay == 7.0
        assert problem.retry_after == 7.0

    def test_falls_back_to_the_policy(self):
        delay, problem = self.pause({}, RetryPolicy(initial_backoff=1.5))
        assert delay == 1.5
        assert problem.retry_after is None

    def test_an_unparseable_hint_falls_back(self):
        assert self.pause({"retry-after": "soon"}, RetryPolicy(initial_backoff=2.0))[0] == 2.0

    def test_a_hint_past_the_policy_stops_the_retry_but_is_kept(self):
        delay, problem = self.pause({"retry-after": "3600"}, RetryPolicy(max_retry_after=60.0))
        assert delay is None
        assert problem.retry_after == 3600.0


class TestSessionIsStale:
    def _session(self, state: str) -> Session:
        return Session.from_wire({"state": state})

    def test_a_changed_state_is_stale(self):
        assert session_is_stale(self._session("s1"), "s2")

    def test_an_unchanged_state_is_fresh(self):
        assert not session_is_stale(self._session("s1"), "s1")

    def test_a_server_that_omits_the_state_does_not_cause_a_refetch_loop(self):
        assert not session_is_stale(self._session("s1"), "")

    def test_an_unknown_local_state_is_treated_as_fresh(self):
        assert not session_is_stale(self._session(""), "s2")


class TestMergedCreatedIds:
    def test_incoming_wins(self):
        assert merged_created_ids({"a": "1"}, {"a": "2", "b": "3"}) == {"a": "2", "b": "3"}

    def test_neither_input_is_mutated(self):
        existing = {"a": "1"}
        merged_created_ids(existing, {"b": "2"})
        assert existing == {"a": "1"}


class TestDefaultRegistry:
    def test_knows_the_core_capability(self):
        assert "urn:ietf:params:jmap:core" in default_registry()

    def test_each_call_is_independent(self):
        # A registry is mutable; sharing one would let an application registering
        # a private capability change what every other client can speak.
        first = default_registry()
        first.register(CapabilitySpec(urn="urn:vendor:private"))
        assert "urn:vendor:private" not in default_registry()

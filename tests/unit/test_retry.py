"""Retry safety (RFC 8620 §3.6.2, RFC 9110 §10.2.3).

JMAP has no idempotency key, so the question is never "did it fail?" but "could
it have applied?". Getting this wrong duplicates drafts and double-sends mail.
"""

from __future__ import annotations

import email.utils

import pytest

from jmap.core.errors import URN_LIMIT, URN_UNKNOWN_CAPABILITY
from jmap.core.retry import (
    Failure,
    RetryPolicy,
    Safety,
    classify,
    parse_retry_after,
    should_retry,
)


class TestClassify:
    def test_connect_failure_never_reached_the_server(self):
        assert classify(Failure.CONNECT) is Safety.NEVER_APPLIED

    def test_timeout_might_have_applied(self):
        # The bytes went out; silence is indistinguishable from a slow success.
        assert classify(Failure.TIMEOUT) is Safety.MAYBE_APPLIED

    def test_an_interrupted_exchange_might_have_applied(self):
        # A reset while reading the response, or a server that hung up without
        # answering, is the same silence as a timeout - the work may be done.
        assert classify(Failure.INTERRUPTED) is Safety.MAYBE_APPLIED

    @pytest.mark.parametrize("status", [429, 503])
    def test_backpressure_means_nothing_ran(self, status):
        assert classify(Failure.STATUS, status=status) is Safety.NEVER_APPLIED

    def test_request_level_limit_rejects_the_whole_request(self):
        # RFC 8620 §3.6.1: no method in the request executed.
        safety = classify(Failure.STATUS, status=400, problem_type=URN_LIMIT)
        assert safety is Safety.NEVER_APPLIED

    def test_other_request_level_problems_are_futile(self):
        safety = classify(Failure.STATUS, status=400, problem_type=URN_UNKNOWN_CAPABILITY)
        assert safety is Safety.FUTILE

    @pytest.mark.parametrize("status", [500, 502, 504])
    def test_server_errors_might_have_applied(self, status):
        # A 500 can be raised while writing the response to completed work.
        assert classify(Failure.STATUS, status=status) is Safety.MAYBE_APPLIED

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
    def test_client_errors_are_futile(self, status):
        assert classify(Failure.STATUS, status=status) is Safety.FUTILE

    def test_success_status_is_futile_to_retry(self):
        assert classify(Failure.STATUS, status=200) is Safety.FUTILE


class TestShouldRetry:
    POLICY = RetryPolicy(max_attempts=3)

    def test_never_applied_is_always_retryable(self):
        assert should_retry(Safety.NEVER_APPLIED, policy=self.POLICY, attempt=1, mutating=True)

    def test_futile_is_never_retryable(self):
        assert not should_retry(Safety.FUTILE, policy=self.POLICY, attempt=1, mutating=False)

    def test_maybe_applied_is_safe_for_a_read_only_batch(self):
        assert should_retry(Safety.MAYBE_APPLIED, policy=self.POLICY, attempt=1, mutating=False)

    def test_maybe_applied_is_refused_for_an_unguarded_mutation(self):
        # This is the case that creates duplicate drafts.
        assert not should_retry(Safety.MAYBE_APPLIED, policy=self.POLICY, attempt=1, mutating=True)

    def test_ifinstate_makes_a_mutating_retry_safe(self):
        # RFC 8620 §5.3: a guarded /set fails with stateMismatch if the first
        # attempt landed, so the retry cannot duplicate.
        assert should_retry(
            Safety.MAYBE_APPLIED,
            policy=self.POLICY,
            attempt=1,
            mutating=True,
            all_mutations_guarded=True,
        )

    def test_attempts_are_capped(self):
        assert not should_retry(Safety.NEVER_APPLIED, policy=self.POLICY, attempt=3, mutating=False)

    def test_a_single_attempt_policy_never_retries(self):
        assert not should_retry(
            Safety.NEVER_APPLIED, policy=RetryPolicy(max_attempts=1), attempt=1, mutating=False
        )


class TestBackoff:
    def test_grows_exponentially(self):
        policy = RetryPolicy(initial_backoff=0.5, multiplier=2.0)
        assert [policy.backoff(n) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]

    def test_is_capped(self):
        policy = RetryPolicy(initial_backoff=1.0, multiplier=10.0, max_backoff=5.0)
        assert policy.backoff(5) == 5.0

    def test_retry_after_overrides_the_curve(self):
        # The server is telling us when it will be ready; ignoring it is how a
        # thundering herd forms.
        policy = RetryPolicy(initial_backoff=0.5, max_backoff=5.0)
        assert policy.backoff(1, retry_after=90.0) == 90.0

    def test_negative_retry_after_is_clamped(self):
        assert RetryPolicy().backoff(1, retry_after=-5.0) == 0.0


class TestPause:
    def test_no_hint_follows_the_curve(self):
        assert RetryPolicy(initial_backoff=0.5).pause(2) == 1.0

    def test_a_hint_within_the_ceiling_wins_over_the_curve(self):
        assert RetryPolicy(max_backoff=5.0).pause(1, retry_after=90.0) == 90.0

    def test_a_hint_past_the_ceiling_is_not_waited_out(self):
        # A server may name any delay. One 503 asking for a year parked call()
        # for a year, and past about 290 years time.sleep itself overflowed.
        # Past the ceiling nothing is waited for: the caller gets the error now.
        policy = RetryPolicy(max_retry_after=120.0)
        assert policy.pause(1, retry_after=120.0) == 120.0
        assert policy.pause(1, retry_after=120.5) is None

    def test_a_negative_hint_is_clamped(self):
        assert RetryPolicy().pause(1, retry_after=-5.0) == 0.0


class TestParseRetryAfter:
    def test_seconds_form(self):
        assert parse_retry_after({"retry-after": "120"}) == 120.0

    def test_header_name_is_case_insensitive(self):
        assert parse_retry_after({"Retry-After": "5"}) == 5.0

    def test_absent_header(self):
        assert parse_retry_after({}) is None

    def test_empty_header(self):
        assert parse_retry_after({"retry-after": ""}) is None

    def test_whitespace_is_tolerated(self):
        assert parse_retry_after({"retry-after": "  30  "}) == 30.0

    def test_negative_seconds_are_clamped(self):
        assert parse_retry_after({"retry-after": "-1"}) == 0.0

    def test_http_date_form_needs_a_clock(self):
        when = email.utils.formatdate(1_000_060.0, usegmt=True)
        assert parse_retry_after({"retry-after": when}, now=1_000_000.0) == pytest.approx(60.0)

    def test_a_date_already_past_is_clamped(self):
        when = email.utils.formatdate(1_000_000.0, usegmt=True)
        assert parse_retry_after({"retry-after": when}, now=1_000_060.0) == 0.0

    def test_date_form_is_ignored_without_a_clock(self):
        # Guessing the wall clock produces a nonsense delay.
        when = email.utils.formatdate(1_000_060.0, usegmt=True)
        assert parse_retry_after({"retry-after": when}) is None

    @pytest.mark.parametrize("value", ["not a date", "Mon, 99 Xxx 9999", "12x"])
    def test_unparseable_values_cost_the_hint_not_the_retry(self, value):
        # A server sending nonsense should not crash the retry decision; the
        # caller just falls back to its own backoff curve.
        assert parse_retry_after({"retry-after": value}, now=1_000_000.0) is None

    def test_unparseable_value_without_a_clock(self):
        assert parse_retry_after({"retry-after": "not a date"}) is None

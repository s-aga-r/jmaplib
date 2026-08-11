"""WWW-Authenticate parsing (RFC 9110 §11.6.1).

Anchored on what a live Stalwart v0.16.17 actually sends, since RFC 8620 defines
no auth scheme and the challenge is the only place a client learns what will be
accepted.
"""

from __future__ import annotations

import pytest

from jmap.auth.challenge import Challenge, find_challenge, parse_challenges

#: Measured from Stalwart v0.16.17 - two separate header lines.
STALWART_HEADERS = [
    'Bearer realm="Stalwart Server", resource_metadata="/.well-known/oauth-protected-resource"',
    'Basic realm="Stalwart Server"',
]


class TestStalwart:
    def test_both_schemes_are_found(self):
        challenges = parse_challenges(STALWART_HEADERS)
        assert [c.scheme for c in challenges] == ["bearer", "basic"]

    def test_bearer_carries_the_rfc9728_metadata_pointer(self):
        bearer = find_challenge(parse_challenges(STALWART_HEADERS), "Bearer")
        assert bearer is not None
        assert bearer.realm == "Stalwart Server"
        assert bearer.resource_metadata == "/.well-known/oauth-protected-resource"

    def test_basic_has_only_a_realm(self):
        basic = find_challenge(parse_challenges(STALWART_HEADERS), "basic")
        assert basic is not None
        assert basic.realm == "Stalwart Server"
        assert basic.resource_metadata is None


class TestAmbiguousCommas:
    """Challenges and auth-params share a separator; this is the hard part."""

    def test_two_challenges_in_one_header(self):
        challenges = parse_challenges(['Bearer realm="x", Basic realm="y"'])
        assert [(c.scheme, c.realm) for c in challenges] == [("bearer", "x"), ("basic", "y")]

    def test_multiple_params_stay_on_one_challenge(self):
        challenges = parse_challenges(['Bearer realm="x", error="invalid_token"'])
        assert len(challenges) == 1
        assert challenges[0].error == "invalid_token"

    def test_bare_scheme_with_no_params(self):
        assert parse_challenges(["Negotiate"]) == (Challenge("negotiate"),)

    def test_bare_scheme_followed_by_a_full_challenge(self):
        challenges = parse_challenges(['Negotiate, Basic realm="x"'])
        assert [c.scheme for c in challenges] == ["negotiate", "basic"]

    def test_comma_inside_a_quoted_value_does_not_split(self):
        challenges = parse_challenges(['Bearer realm="a,b", error="x"'])
        assert len(challenges) == 1
        assert challenges[0].realm == "a,b"

    def test_escaped_quote_inside_a_value(self):
        challenges = parse_challenges([r'Bearer realm="say \"hi\""'])
        assert challenges[0].realm == 'say "hi"'

    def test_escaped_backslash(self):
        assert parse_challenges([r'Bearer realm="a\\b"'])[0].realm == "a\\b"


class TestNormalisation:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
    def test_scheme_is_case_insensitive(self, scheme):
        assert parse_challenges([f'{scheme} realm="x"'])[0].scheme == "bearer"

    def test_param_names_are_case_insensitive(self):
        assert parse_challenges(['Bearer REALM="x"'])[0].realm == "x"

    def test_unquoted_values_are_accepted(self):
        assert parse_challenges(["Bearer realm=x"])[0].realm == "x"

    def test_whitespace_around_equals(self):
        assert parse_challenges(['Bearer realm  =  "x"'])[0].realm == "x"

    def test_empty_value(self):
        assert parse_challenges(['Bearer realm=""'])[0].realm == ""


class TestRobustness:
    """A malformed challenge is the server's bug; it must not become a crash."""

    def test_empty_input(self):
        assert parse_challenges([]) == ()

    def test_empty_header_value(self):
        assert parse_challenges([""]) == ()

    def test_whitespace_only(self):
        assert parse_challenges(["   "]) == ()

    def test_leading_params_with_no_scheme_are_ignored(self):
        assert parse_challenges(['realm="x"']) == ()

    def test_unparseable_fragment_is_skipped(self):
        challenges = parse_challenges(['Bearer realm="x", !!!not valid!!!'])
        assert challenges[0].scheme == "bearer"

    def test_scheme_with_an_unparseable_first_param(self):
        # The scheme is still recorded; only the bad parameter is dropped.
        challenges = parse_challenges(['Bearer "quoted-not-a-param"'])
        assert len(challenges) == 1
        assert challenges[0].scheme == "bearer"
        assert challenges[0].params == {}

    def test_trailing_comma(self):
        assert parse_challenges(['Bearer realm="x",'])[0].realm == "x"


class TestFindChallenge:
    def test_missing_scheme_returns_none(self):
        assert find_challenge(parse_challenges(STALWART_HEADERS), "Digest") is None

    def test_first_match_wins(self):
        challenges = parse_challenges(['Bearer realm="a", Bearer realm="b"'])
        found = find_challenge(challenges, "bearer")
        assert found is not None
        assert found.realm == "a"

    def test_no_error_param_reads_as_none(self):
        assert parse_challenges(['Basic realm="x"'])[0].error is None

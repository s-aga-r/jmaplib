"""Session parsing and capability resolution (RFC 8620 §2).

Anchored on a real capture from Stalwart v0.16.17 so the tests fail if a real
server's shape drifts from our model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jmap.core.ids import Id
from jmap.core.session import (
    CORE_URN,
    Account,
    InsecureEndpointError,
    Session,
    check_session_redirects,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stalwart-0.16.17-session-bootstrap.json"


@pytest.fixture
def stalwart_session() -> Session:
    data = json.loads(FIXTURE.read_text())
    return Session.from_wire(data, base_url="http://localhost:8080/jmap/session")


class TestStalwartCapture:
    """Against the real thing, not a hand-written approximation."""

    def test_advertises_the_expected_capability_set(self, stalwart_session):
        urns = stalwart_session.advertised_for()
        assert CORE_URN in urns
        for expected in [
            "urn:ietf:params:jmap:mail",
            "urn:ietf:params:jmap:submission",
            "urn:ietf:params:jmap:calendars",
            "urn:ietf:params:jmap:contacts",
            "urn:ietf:params:jmap:filenode",
            "urn:ietf:params:jmap:sieve",
            "urn:ietf:params:jmap:blob",
            "urn:ietf:params:jmap:quota",
            "urn:ietf:params:jmap:websocket",
        ]:
            assert expected in urns

    def test_does_not_advertise_mdn_or_smime(self, stalwart_session):
        # Stalwart implements neither, so those capabilities can only ever be
        # fixture-verified. If this starts failing, that changed.
        urns = stalwart_session.advertised_for()
        assert "urn:ietf:params:jmap:mdn" not in urns
        assert "urn:ietf:params:jmap:smimeverify" not in urns

    def test_limits_parse_from_the_real_core_capability(self, stalwart_session):
        limits = stalwart_session.limits
        assert limits.max_calls_in_request == 16
        assert limits.max_objects_in_get == 500
        assert limits.max_objects_in_set == 500
        assert limits.max_size_upload == 50_000_000
        assert limits.max_concurrent_requests == 4

    def test_sieve_capability_value_is_not_empty(self, stalwart_session):
        # Stalwart puts {"implementation": "..."} here; a model assuming every
        # capability value is {} would break on it.
        value = stalwart_session.capability_value("urn:ietf:params:jmap:sieve")
        assert value.get("implementation")

    def test_unauthenticated_session_has_no_accounts(self, stalwart_session):
        # The capture is unauthenticated: 200 with capabilities but no accounts.
        # This is exactly why it is useless as a CI readiness probe.
        assert stalwart_session.accounts == {}
        assert stalwart_session.username == ""


class TestUrlResolution:
    def test_relative_urls_resolve_against_the_post_redirect_base(self):
        session = Session.from_wire(
            {"apiUrl": "/jmap/", "uploadUrl": "/jmap/upload/{accountId}/"},
            base_url="https://mail.example.com/jmap/session",
        )
        assert session.api_url == "https://mail.example.com/jmap/"
        assert session.upload_url == "https://mail.example.com/jmap/upload/{accountId}/"

    def test_absolute_urls_are_left_alone(self):
        session = Session.from_wire(
            {"apiUrl": "https://other.example.net/api"},
            base_url="https://mail.example.com/jmap/session",
        )
        assert session.api_url == "https://other.example.net/api"

    def test_no_base_url_leaves_values_untouched(self):
        assert Session.from_wire({"apiUrl": "/jmap/"}).api_url == "/jmap/"


class TestEndpointDowngradeRefusal:
    """The endpoint URLs are where credentials go; a session fetched over https
    must not steer them onto cleartext."""

    BASE = "https://mail.example.com/jmap/session"

    def test_an_http_endpoint_in_an_https_session_is_refused(self):
        # The classic MITM shape: the document itself came over TLS, but points
        # the authenticated traffic at a host the attacker can read.
        with pytest.raises(InsecureEndpointError, match="apiUrl"):
            Session.from_wire({"apiUrl": "http://harvest.example.net/api"}, base_url=self.BASE)

    def test_every_endpoint_field_is_covered(self):
        for field in ("apiUrl", "downloadUrl", "uploadUrl", "eventSourceUrl"):
            with pytest.raises(InsecureEndpointError, match=field):
                Session.from_wire({field: "http://harvest.example.net/x"}, base_url=self.BASE)

    def test_a_cross_host_https_endpoint_stays_legal(self):
        # Real providers serve upload/download from separate hosts.
        session = Session.from_wire(
            {"downloadUrl": "https://content.example.net/blob/{blobId}"}, base_url=self.BASE
        )
        assert session.download_url.startswith("https://content.example.net/")

    def test_an_http_session_may_keep_http_endpoints(self):
        # A caller who connected over http has already accepted cleartext -
        # this is what keeps an internal-network deployment working.
        session = Session.from_wire(
            {"apiUrl": "http://mail.internal:8080/jmap/"},
            base_url="http://mail.internal:8080/jmap/session",
        )
        assert session.api_url == "http://mail.internal:8080/jmap/"

    def test_loopback_is_always_allowed(self):
        session = Session.from_wire({"apiUrl": "http://127.0.0.1:8080/jmap/"}, base_url=self.BASE)
        assert session.api_url == "http://127.0.0.1:8080/jmap/"

    def test_a_foreign_scheme_is_refused_outright(self):
        with pytest.raises(InsecureEndpointError):
            Session.from_wire({"apiUrl": "ftp://mail.example.com/api"}, base_url=self.BASE)

    def test_offline_parsing_is_not_second_guessed(self):
        # No base_url means no fetch context - a cached document is the
        # caller's own input, not a network response.
        session = Session.from_wire({"apiUrl": "http://mail.internal:8080/jmap/"})
        assert session.api_url == "http://mail.internal:8080/jmap/"


class TestRedirectDowngradeRefusal:
    """Endpoints are judged by the channel the document came over - which is
    only sound if that is the channel the caller asked for."""

    WELL_KNOWN = "https://mail.example.com/.well-known/jmap"

    def test_a_redirect_onto_cleartext_is_refused(self):
        # An https fetch redirected to http becomes an "http session", whose http
        # endpoints then pass - so whoever answers the cleartext leg chooses
        # where the credentials go next.
        hops = [self.WELL_KNOWN, "http://mail.example.com/jmap/session"]
        with pytest.raises(InsecureEndpointError, match="redirected"):
            check_session_redirects(self.WELL_KNOWN, hops)

    def test_a_downgrade_anywhere_in_the_chain_counts(self):
        hops = [self.WELL_KNOWN, "https://cdn.example.net/x", "http://mail.example.com/s"]
        with pytest.raises(InsecureEndpointError):
            check_session_redirects(self.WELL_KNOWN, hops)

    def test_a_cross_host_https_redirect_stays_legal(self):
        # Fastmail's well-known URL redirects to a different host.
        check_session_redirects(self.WELL_KNOWN, [self.WELL_KNOWN, "https://api.example.net/s"])

    def test_an_http_fetch_may_stay_on_http(self):
        # The caller asked for cleartext; this is how CI reaches its container.
        check_session_redirects(
            "http://localhost:8080/.well-known/jmap", ["http://localhost:8080/jmap/session"]
        )

    def test_loopback_is_allowed_as_for_endpoints(self):
        check_session_redirects(self.WELL_KNOWN, ["http://127.0.0.1:8080/jmap/session"])


class TestHostileSessionShapes:
    def test_a_non_object_accounts_map_is_a_value_error(self):
        # The entry point for a network-fetched document must fail as a
        # catchable ValueError, never an AttributeError from a comprehension.
        for hostile in ("accounts", "primaryAccounts", "capabilities"):
            with pytest.raises(ValueError, match=hostile):
                Session.from_wire({hostile: "not-an-object"})

    def test_a_non_object_account_entry_is_a_value_error(self):
        with pytest.raises(ValueError, match="a1"):
            Session.from_wire({"accounts": {"a1": "not-an-object"}})

    def test_a_non_string_primary_account_is_a_value_error(self):
        with pytest.raises(ValueError, match="urn:ietf:params:jmap:mail"):
            Session.from_wire({"primaryAccounts": {"urn:ietf:params:jmap:mail": 7}})


class TestCapabilityResolution:
    @pytest.fixture
    def session(self) -> Session:
        return Session.from_wire(
            {
                "capabilities": {CORE_URN: {"maxCallsInRequest": 16}, "urn:x:mail": {}},
                "accounts": {
                    "a": {
                        "name": "alice@example.com",
                        "isPersonal": True,
                        "isReadOnly": False,
                        "accountCapabilities": {
                            "urn:x:mail": {"maxMailboxDepth": 10},
                            "urn:vendor:only": {},
                        },
                    },
                    "b": {
                        "name": "shared",
                        "isPersonal": False,
                        "isReadOnly": True,
                        "accountCapabilities": {"urn:x:calendars": {}},
                    },
                },
                "primaryAccounts": {"urn:x:mail": "a"},
                "username": "alice@example.com",
                "state": "s1",
            }
        )

    def test_account_capabilities_are_unioned_not_subset_checked(self, session):
        # urn:vendor:only appears ONLY at account level, exactly as Stalwart does
        # with urn:stalwart:jmap.
        urns = session.advertised_for(Id("a"))
        assert "urn:vendor:only" in urns
        assert "urn:vendor:only" not in session.advertised_for()

    def test_capabilities_differ_per_account(self, session):
        assert "urn:x:calendars" in session.advertised_for(Id("b"))
        assert "urn:x:calendars" not in session.advertised_for(Id("a"))

    def test_account_level_capability_value_wins(self, session):
        value = session.capability_value("urn:x:mail", Id("a"))
        assert value == {"maxMailboxDepth": 10}

    def test_session_level_value_used_without_an_account(self, session):
        assert session.capability_value(CORE_URN) == {"maxCallsInRequest": 16}

    def test_unknown_capability_yields_empty_mapping(self, session):
        assert session.capability_value("urn:nope") == {}

    def test_primary_account_lookup(self, session):
        assert session.primary_account_for("urn:x:mail") == Id("a")

    def test_primary_account_returns_none_rather_than_guessing(self, session):
        # Guessing "the first account" is how Email/* ends up aimed at a
        # calendar-only shared account.
        assert session.primary_account_for("urn:x:calendars") is None

    def test_accounts_with_capability(self, session):
        assert session.accounts_with("urn:x:calendars") == (Id("b"),)

    def test_accounts_disagreeing_about_the_primary_imply_nothing(self, session):
        # This fixture puts mail's primary on "a" and has a second account "b"
        # that nothing points at - but only one URN is mapped, so there is a
        # unanimous answer. Break the unanimity and there is none.
        divided = Session.from_wire(
            {
                "accounts": {"a": {"name": "a"}, "b": {"name": "b"}},
                "primaryAccounts": {"urn:x:mail": "a", "urn:x:calendars": "b"},
                "username": "alice@example.com",
                "state": "s1",
            }
        )
        assert divided.implied_account() is None

    def test_a_unanimous_primary_settles_it(self):
        # The real-server case: a personal account alongside a shared team one.
        # Every primaryAccounts entry names the personal account, so the server
        # has already said which is the user's own - nothing is being guessed,
        # and nothing points at the shared account at all.
        session = Session.from_wire(
            {
                "accounts": {
                    "bw": {"name": "a1@example.com", "isPersonal": True},
                    "bh": {"name": "team@example.com", "isPersonal": False},
                },
                "primaryAccounts": {"urn:x:mail": "bw", "urn:x:contacts": "bw"},
                "username": "a1@example.com",
                "state": "s1",
            }
        )
        assert session.implied_account() == Id("bw")

    def test_one_account_is_its_own_answer(self):
        # Blobs are account-scoped but capability-less, so primaryAccounts can
        # never name their account - which left every upload against a perfectly
        # ordinary single-account server failing with "no accountId".
        session = Session.from_wire(
            {
                "accounts": {"a": {"name": "alice@example.com"}},
                "username": "alice@example.com",
                "state": "s1",
            }
        )
        assert session.implied_account() == Id("a")

    def test_a_primary_naming_an_unlisted_account_is_not_trusted(self):
        # The id would be unusable, so a malformed session gets no answer rather
        # than one that fails later at the upload endpoint.
        session = Session.from_wire(
            {
                "accounts": {"a": {"name": "a"}, "b": {"name": "b"}},
                "primaryAccounts": {"urn:x:mail": "ghost"},
                "username": "alice@example.com",
                "state": "s1",
            }
        )
        assert session.implied_account() is None

    def test_no_accounts_at_all_answers_none(self):
        assert Session.from_wire({"accounts": {}}).implied_account() is None

    def test_read_only_flag(self, session):
        assert session.is_read_only(Id("b"))
        assert not session.is_read_only(Id("a"))
        assert not session.is_read_only(Id("missing"))

    def test_unknown_account_id_yields_session_capabilities_only(self, session):
        assert session.advertised_for(Id("zzz")) == frozenset(session.capabilities)


class TestAccount:
    def test_from_wire_defaults(self):
        account = Account.from_wire("a", {})
        assert account.id == "a"
        assert account.name == ""
        assert not account.is_personal
        assert not account.is_read_only
        assert account.account_capabilities == {}

    def test_repr_mentions_read_only(self):
        account = Account.from_wire("a", {"name": "x", "isReadOnly": True})
        assert "read_only=True" in repr(account)


class TestCapabilityValueFallthrough:
    """The account-level lookup must fall back rather than shadow."""

    @pytest.fixture
    def session(self) -> Session:
        return Session.from_wire(
            {
                "capabilities": {"urn:x:mail": {"fromSession": True}},
                "accounts": {
                    "a": {"name": "alice", "accountCapabilities": {"urn:x:other": {}}},
                    # A capability whose value is not an object at all: out of
                    # spec, but a client must not hand the caller a non-mapping.
                    "b": {"name": "bob", "accountCapabilities": {"urn:x:mail": "nonsense"}},
                },
            }
        )

    def test_account_lacking_the_urn_falls_back_to_session_level(self, session):
        assert session.capability_value("urn:x:mail", Id("a")) == {"fromSession": True}

    def test_unknown_account_falls_back_to_session_level(self, session):
        assert session.capability_value("urn:x:mail", Id("zzz")) == {"fromSession": True}

    def test_non_mapping_account_value_falls_back(self, session):
        assert session.capability_value("urn:x:mail", Id("b")) == {"fromSession": True}

    def test_non_mapping_session_value_reads_as_absent(self):
        session = Session.from_wire({"capabilities": {"urn:x:mail": "nonsense"}})
        assert session.capability_value("urn:x:mail") == {}


class TestAccountOnlyCapabilities:
    """``account_capability_value`` deliberately does *not* fall back.

    Some capabilities are defined to appear only per account - RFC 9670's
    ``:principals:owner`` is the case - and reading the session-level map for one
    of those turns "this account has none" into a confident wrong answer.
    """

    def session(self, **account_capabilities: object) -> Session:
        return Session.from_wire(
            {
                "capabilities": {"urn:x": {"from": "session"}},
                "accounts": {"a": {"name": "a", "accountCapabilities": account_capabilities}},
                "primaryAccounts": {},
                "username": "alice",
                "apiUrl": "https://jmap.example.com/jmap/",
                "state": "s0",
            }
        )

    def test_the_account_value_is_returned(self):
        session = self.session(**{"urn:x": {"from": "account"}})
        assert session.account_capability_value("urn:x", Id("a")) == {"from": "account"}

    def test_the_session_value_is_never_consulted(self):
        assert self.session().account_capability_value("urn:x", Id("a")) == {}

    def test_no_account_means_nothing_to_read(self):
        assert self.session().account_capability_value("urn:x", None) == {}

    def test_an_unknown_account_reads_as_empty(self):
        assert self.session().account_capability_value("urn:x", Id("nope")) == {}

    def test_a_non_mapping_value_is_treated_as_absent(self):
        # Out of spec but survivable; handing back a string the caller will index
        # is worse than saying the capability carries nothing.
        session = self.session(**{"urn:x": "not an object"})
        assert session.account_capability_value("urn:x", Id("a")) == {}

    def test_the_fallback_lookup_still_falls_back(self):
        # `capability_value` keeps its RFC 8620 §2 behaviour - this is a second
        # method, not a change to the first.
        assert self.session().capability_value("urn:x", Id("a")) == {"from": "session"}


class TestWhichAccountsCapabilityValue:
    """The regression this exists for: a real Stalwart leaves most capability
    objects *empty* at session level and puts every actual limit under
    accountCapabilities. Reading the session-level copy when no account was named
    therefore reports no limits at all - and for supportedDigestAlgorithms that
    does not read as "unknown", it reads as "supports nothing"."""

    @pytest.fixture
    def session(self) -> Session:
        return Session.from_wire(
            {
                "capabilities": {"urn:x:blob": {}},
                "accounts": {
                    "bw": {
                        "name": "a1@example.com",
                        "accountCapabilities": {
                            "urn:x:blob": {"supportedDigestAlgorithms": ["sha-256"]}
                        },
                    },
                    "bh": {"name": "team@example.com", "accountCapabilities": {}},
                },
                "primaryAccounts": {"urn:x:blob": "bw"},
                "username": "a1@example.com",
                "state": "s1",
            }
        )

    def test_an_unnamed_account_resolves_to_the_capability_primary(self, session):
        assert session.capability_account("urn:x:blob") == Id("bw")

    def test_and_that_is_where_the_real_value_lives(self, session):
        account = session.capability_account("urn:x:blob")
        value = session.capability_value("urn:x:blob", account)
        assert value["supportedDigestAlgorithms"] == ["sha-256"]

    def test_reading_it_unscoped_is_what_used_to_lose_the_limits(self, session):
        # Kept as documentation of the failure mode, not as desired behaviour.
        assert session.capability_value("urn:x:blob") == {}

    def test_a_named_account_is_never_second_guessed(self, session):
        assert session.capability_account("urn:x:blob", Id("bh")) == Id("bh")

    def test_a_capability_with_no_primary_falls_back_to_what_is_implied(self, session):
        # Nothing is primary for this one, but every primaryAccounts entry names
        # bw, so the session still implies an account.
        assert session.capability_account("urn:x:other") == Id("bw")

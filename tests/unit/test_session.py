"""Session parsing and capability resolution (RFC 8620 §2).

Anchored on a real capture from Stalwart v0.16.17 so the tests fail if a real
server's shape drifts from our model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jmap.core.ids import Id
from jmap.core.session import CORE_URN, Account, Session

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

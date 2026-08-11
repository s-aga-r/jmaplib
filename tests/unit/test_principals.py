"""RFC 9670 Principals and ShareNotifications, the two ``:principals`` URNs, and
the ``shareWith`` patch builders.

Sharing is the one area where a plausible-looking client silently destroys data:
assigning ``shareWith`` revokes everyone absent from the new value, ``null`` and
``{}`` are not interchangeable, and the account holding the Principals is not the
account holding the shared data. These tests pin each of those, plus the two
places RFC 9670 departs from the surrounding specs - a ``ShareNotification.id``
typed ``String`` rather than ``Id``, and nullable rights maps where ``null``
carries meaning an empty dict does not.
"""

from __future__ import annotations

import pytest

from jmap.capabilities.principals import (
    PRINCIPALS,
    PRINCIPALS_OWNER,
    PRINCIPALS_OWNER_URN,
    PRINCIPALS_URN,
    PrincipalsCapability,
    PrincipalsOwnerCapability,
)
from jmap.core.session import Session
from jmap.models.principals import (
    PRINCIPAL_GROUP,
    PRINCIPAL_INDIVIDUAL,
    PRINCIPAL_LOCATION,
    PRINCIPAL_OTHER,
    PRINCIPAL_RESOURCE,
    PRINCIPAL_TYPES,
    SHARE_CREATED,
    SHARE_DESTROYED,
    SHARE_UPDATED,
    Entity,
    Principal,
    ShareNotification,
)
from jmap.sharing import (
    OwnerInShareMapError,
    check_share_map,
    grant,
    held,
    may,
    me,
    owner_of,
    principal_account,
    replace,
    revoke,
    subscribe,
    targets,
    unshare,
)

CALENDARS_URN = "urn:ietf:params:jmap:calendars"

#: The two accounts of the §1.5.2 arrangement: the shared data lives in one, the
#: Principals that name its viewers live in the other.
DATA_ACCOUNT = "data-account"
PRINCIPAL_ACCOUNT = "principal-account"


def session(**accounts: object) -> Session:
    """A Session whose accounts carry the given ``accountCapabilities``."""
    return Session.from_wire(
        {
            "capabilities": {"urn:ietf:params:jmap:core": {}},
            "accounts": {
                account_id: {"name": account_id, "accountCapabilities": capabilities}
                for account_id, capabilities in accounts.items()
            },
            "primaryAccounts": {},
            "username": "alice",
            "apiUrl": "https://jmap.example.com/jmap/",
            "state": "s0",
        }
    )


def shared_session() -> Session:
    """A data account owned by a Principal that lives in a different account."""
    return session(
        **{
            DATA_ACCOUNT: {
                PRINCIPALS_OWNER_URN: {
                    "accountIdForPrincipal": PRINCIPAL_ACCOUNT,
                    "principalId": "p-alice",
                }
            },
            PRINCIPAL_ACCOUNT: {PRINCIPALS_URN: {"currentUserPrincipalId": "p-alice"}},
        }
    )


class TestPrincipalModel:
    def test_every_property_parses_from_the_wire(self):
        principal = Principal.from_wire(
            {
                "id": "p-alice",
                "type": "individual",
                "name": "Alice Example",
                "description": "Engineering",
                "email": "alice@example.com",
                "timeZone": "Europe/London",
                "capabilities": {CALENDARS_URN: {"mayShareWith": True}},
                "accounts": {"a1": {"name": "Alice", "isPersonal": True}},
            }
        )
        assert principal.time_zone == "Europe/London"
        assert principal.email == "alice@example.com"
        assert principal.accounts == {"a1": {"name": "Alice", "isPersonal": True}}

    def test_the_closed_type_set_is_declared(self):
        assert sorted(PRINCIPAL_TYPES) == ["group", "individual", "location", "other", "resource"]
        assert (
            PRINCIPAL_INDIVIDUAL,
            PRINCIPAL_GROUP,
            PRINCIPAL_RESOURCE,
            PRINCIPAL_LOCATION,
            PRINCIPAL_OTHER,
        ) == ("individual", "group", "resource", "location", "other")

    def test_an_unregistered_type_does_not_fail_the_parse(self):
        # §2 closes the set, but a nonconformant value should cost one comparison
        # rather than the whole /get it arrived in.
        principal = Principal.from_wire({"id": "p1", "type": "committee"})
        assert principal.type == "committee"
        assert principal.type not in PRINCIPAL_TYPES

    def test_no_visible_accounts_is_null_and_not_an_empty_map(self):
        # §1.4 distinguishes "no accounts you can reach" from "an account holding
        # no records"; collapsing null to {} makes the two indistinguishable.
        assert Principal.from_wire({"id": "p1"}).accounts is None
        assert Principal.from_wire({"id": "p1", "accounts": {}}).accounts == {}

    def test_a_capability_object_is_returned_by_urn(self):
        principal = Principal(capabilities={CALENDARS_URN: {"mayShareWith": True}})
        assert principal.capability(CALENDARS_URN) == {"mayShareWith": True}

    def test_an_unadvertised_capability_reads_as_empty(self):
        assert Principal().capability(CALENDARS_URN) == {}

    def test_may_share_with_reads_the_flag(self):
        principal = Principal(capabilities={CALENDARS_URN: {"mayShareWith": True}})
        assert principal.may_share_with(CALENDARS_URN) is True

    def test_may_share_with_is_false_when_the_flag_is_absent(self):
        # RFC 9670 states no default, so assuming one offers the user a share
        # target the server will reject.
        principal = Principal(capabilities={CALENDARS_URN: {}})
        assert principal.may_share_with(CALENDARS_URN) is False
        assert Principal().may_share_with(CALENDARS_URN) is False

    def test_may_share_with_ignores_whether_any_account_is_visible(self):
        # A legal share target need not expose any account to the requesting user,
        # so filtering on `accounts` instead would hide most of the directory.
        principal = Principal(capabilities={CALENDARS_URN: {"mayShareWith": True}}, accounts=None)
        assert principal.may_share_with(CALENDARS_URN) is True


class TestEntity:
    def test_the_actor_parses(self):
        actor = Entity.from_wire(
            {"name": "Bob", "email": "bob@example.com", "principalId": "p-bob"}
        )
        assert actor.principal_id == "p-bob"
        assert actor.name == "Bob"

    def test_an_actor_with_no_visible_principal_has_none(self):
        assert Entity.from_wire({"name": "Bob"}).principal_id is None


class TestShareNotificationModel:
    def test_a_notification_parses_from_the_wire(self):
        notification = ShareNotification.from_wire(
            {
                "id": "n1",
                "created": "2026-01-01T00:00:00Z",
                "changedBy": {"name": "Bob", "email": "bob@example.com", "principalId": "p-bob"},
                "objectType": "Calendar",
                "objectAccountId": "a2",
                "objectId": "c1",
                "oldRights": {"mayReadItems": True},
                "newRights": {"mayReadItems": True, "mayWriteAll": True},
                "name": "Team calendar",
            }
        )
        assert notification.object_type == "Calendar"
        assert notification.object_account_id == "a2"
        assert notification.changed_by is not None
        assert notification.changed_by.principal_id == "p-bob"

    def test_the_id_is_a_plain_string_not_a_jmap_id(self):
        # §3 types it String, alone among this document's identifiers. Applying
        # the Id charset (RFC 8620 §1.2) would reject values a conformant server
        # is entitled to send.
        notification = ShareNotification.from_wire({"id": "not/an*id "})
        assert notification.id == "not/an*id "

    def test_the_object_name_survives_losing_access(self):
        # §3 keeps it so a user who can no longer fetch the object can still be
        # told what they lost.
        notification = ShareNotification.from_wire({"id": "n1", "name": "Team calendar"})
        assert notification.name == "Team calendar"

    def test_the_change_kinds_are_named(self):
        assert (SHARE_CREATED, SHARE_UPDATED, SHARE_DESTROYED) == (
            "created",
            "updated",
            "destroyed",
        )


class TestShareNotificationRights:
    def test_null_old_rights_means_newly_granted(self):
        notification = ShareNotification(oldRights=None, newRights={"mayReadItems": True})
        assert notification.is_grant is True
        assert notification.is_revocation is False

    def test_null_new_rights_means_fully_revoked(self):
        notification = ShareNotification(oldRights={"mayReadItems": True}, newRights=None)
        assert notification.is_revocation is True
        assert notification.is_grant is False

    def test_null_on_both_sides_is_a_revocation_and_not_a_grant(self):
        # Nothing before and nothing after. Reading only `oldRights is None` would
        # call this a grant and show the user access they do not have.
        notification = ShareNotification(oldRights=None, newRights=None)
        assert notification.is_revocation is True
        assert notification.is_grant is False

    def test_a_changed_grant_is_neither_a_grant_nor_a_revocation(self):
        notification = ShareNotification(
            oldRights={"mayReadItems": True}, newRights={"mayWriteAll": True}
        )
        assert notification.is_grant is False
        assert notification.is_revocation is False

    def test_gained_reports_only_what_was_added(self):
        notification = ShareNotification(
            oldRights={"mayReadItems": True},
            newRights={"mayReadItems": True, "mayWriteAll": True},
        )
        assert notification.gained() == {"mayWriteAll"}
        assert notification.lost() == set()

    def test_lost_reports_only_what_was_removed(self):
        notification = ShareNotification(
            oldRights={"mayReadItems": True, "mayWriteAll": True},
            newRights={"mayReadItems": True},
        )
        assert notification.lost() == {"mayWriteAll"}
        assert notification.gained() == set()

    def test_a_new_grant_counts_everything_as_gained(self):
        notification = ShareNotification(oldRights=None, newRights={"mayReadItems": True})
        assert notification.gained() == {"mayReadItems"}
        assert notification.lost() == set()

    def test_a_revocation_counts_everything_as_lost(self):
        notification = ShareNotification(oldRights={"mayReadItems": True}, newRights=None)
        assert notification.lost() == {"mayReadItems"}
        assert notification.gained() == set()

    def test_a_right_turned_off_is_lost_rather_than_merely_present(self):
        # The server may keep the key and set it false. Diffing key sets instead
        # of values would report no change at all.
        notification = ShareNotification(
            oldRights={"mayReadItems": True}, newRights={"mayReadItems": False}
        )
        assert notification.lost() == {"mayReadItems"}
        assert notification.gained() == set()

    def test_a_right_turned_on_is_gained(self):
        notification = ShareNotification(
            oldRights={"mayReadItems": False}, newRights={"mayReadItems": True}
        )
        assert notification.gained() == {"mayReadItems"}
        assert notification.lost() == set()

    def test_an_unchanged_right_is_neither_gained_nor_lost(self):
        notification = ShareNotification(
            oldRights={"mayReadItems": True}, newRights={"mayReadItems": True}
        )
        assert notification.gained() == set()
        assert notification.lost() == set()


class TestPrincipalsCapabilityObject:
    def test_the_current_user_principal_is_read(self):
        capability = PrincipalsCapability.of({"currentUserPrincipalId": "p-alice"})
        assert capability.current_user_principal_id == "p-alice"

    def test_having_no_principal_in_an_account_is_a_legal_answer(self):
        # §1.5.1: null means the user has none here. Not an error, and not
        # "unknown" - so nothing should retry or raise on it.
        explicit_null = PrincipalsCapability.of({"currentUserPrincipalId": None})
        assert explicit_null.current_user_principal_id is None
        assert PrincipalsCapability.of({}).current_user_principal_id is None

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        # One out-of-spec capability value must not cost the client every other
        # capability in the same session document.
        assert PrincipalsCapability.of(7).current_user_principal_id is None
        assert PrincipalsCapability.of("nonsense").current_user_principal_id is None


class TestPrincipalsOwnerCapabilityObject:
    def test_both_fields_are_read(self):
        capability = PrincipalsOwnerCapability.of(
            {"accountIdForPrincipal": PRINCIPAL_ACCOUNT, "principalId": "p-alice"}
        )
        assert capability.account_id_for_principal == PRINCIPAL_ACCOUNT
        assert capability.principal_id == "p-alice"

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        assert PrincipalsOwnerCapability.of(7).principal_id is None
        assert PrincipalsOwnerCapability.of("nonsense").account_id_for_principal is None


class TestCapabilitySpecs:
    def test_the_principals_urn_declares_its_methods_and_types(self):
        assert PRINCIPALS.urn == PRINCIPALS_URN
        assert PRINCIPALS.attr == "principals"
        assert PRINCIPALS.method("Principal/get") is not None
        assert PRINCIPALS.method("ShareNotification/set") is not None
        principal_type = PRINCIPALS.data_type("Principal")
        assert principal_type is not None
        assert principal_type.model is Principal
        notification_type = PRINCIPALS.data_type("ShareNotification")
        assert notification_type is not None
        assert notification_type.model is ShareNotification

    def test_the_per_account_value_is_where_the_current_principal_lives(self):
        # §1.5.1: the session-level object is empty, so it has no model at all.
        assert PRINCIPALS.account_value is PrincipalsCapability
        assert PRINCIPALS.session_value is None

    def test_the_owner_urn_declares_nothing_that_using_derivation_could_reach(self):
        # §1.5.2: it never appears at session level, and putting it in `using`
        # sends a URN the server never advertised there - which Stalwart answers
        # by rejecting the entire request. No methods, no types, no attr is what
        # keeps derivation from ever finding it.
        assert PRINCIPALS_OWNER.urn == PRINCIPALS_OWNER_URN
        assert PRINCIPALS_OWNER.methods == ()
        assert PRINCIPALS_OWNER.data_types == ()
        assert PRINCIPALS_OWNER.attr is None

    def test_the_owner_urn_still_parses_its_account_value(self):
        assert PRINCIPALS_OWNER.account_value is PrincipalsOwnerCapability


class TestSessionLookups:
    def test_the_principal_account_is_not_the_account_the_data_lives_in(self):
        # The whole point of accountIdForPrincipal. Addressing Principal/* at the
        # data account earns accountNotFound, or worse, succeeds against the wrong
        # account and shows a share picker full of strangers.
        assert principal_account(shared_session(), DATA_ACCOUNT) == PRINCIPAL_ACCOUNT
        assert PRINCIPAL_ACCOUNT != DATA_ACCOUNT

    def test_an_account_no_principal_owns_has_no_principal_account(self):
        # The account holding the Principals themselves is the usual case, and its
        # missing :owner key is not an error.
        assert principal_account(shared_session(), PRINCIPAL_ACCOUNT) is None

    def test_an_unknown_account_has_no_principal_account(self):
        assert principal_account(shared_session(), "nowhere") is None

    def test_the_owning_principal_is_read(self):
        assert owner_of(shared_session(), DATA_ACCOUNT) == "p-alice"

    def test_an_account_with_no_owner_key_reports_no_owner(self):
        assert owner_of(shared_session(), PRINCIPAL_ACCOUNT) is None

    def test_my_own_principal_is_read_from_the_principal_account(self):
        assert me(shared_session(), PRINCIPAL_ACCOUNT) == "p-alice"

    def test_an_account_not_advertising_principals_reports_no_principal(self):
        assert me(shared_session(), DATA_ACCOUNT) is None

    def test_an_account_that_declares_no_principal_for_me_reports_none(self):
        without_me = session(**{PRINCIPAL_ACCOUNT: {PRINCIPALS_URN: {}}})
        assert me(without_me, PRINCIPAL_ACCOUNT) is None


class TestOwnerRule:
    def test_the_owner_must_not_appear_in_share_with(self):
        # §4: the owner's rights are implicit. A client that round-trips
        # "everyone with access" back into an update includes them and corrupts
        # it - the update is invalid, not merely redundant.
        with pytest.raises(OwnerInShareMapError):
            replace({"p-alice": {"mayReadItems": True}}, owner_principal_id="p-alice")

    def test_the_error_names_the_offending_principal(self):
        with pytest.raises(OwnerInShareMapError) as excinfo:
            replace({"p-alice": {}}, owner_principal_id="p-alice")
        assert excinfo.value.principal_id == "p-alice"
        assert "RFC 9670" in str(excinfo.value)

    def test_a_map_of_other_principals_is_accepted(self):
        patch = replace({"p-bob": {"mayReadItems": True}}, owner_principal_id="p-alice")
        assert patch == {"shareWith": {"p-bob": {"mayReadItems": True}}}

    def test_an_unknown_owner_cannot_be_violated(self):
        # `principal_account`/`owner_of` legitimately return None, and refusing to
        # build a patch on that basis would break sharing on every server that
        # does not advertise :owner.
        assert replace({"p-alice": {}}) == {"shareWith": {"p-alice": {}}}

    def test_the_check_is_usable_on_a_map_the_caller_built_itself(self):
        check_share_map({"p-bob": {"mayReadItems": True}}, "p-alice")
        check_share_map({"p-bob": {"mayReadItems": True}}, None)


class TestPatchBuilders:
    def test_granting_is_a_pointer_patch_and_not_a_whole_map_assignment(self):
        # Assigning `shareWith` revokes everyone absent from the new value, so
        # "add Bob" written as a map assignment means "make Bob the only person
        # with access". The pointer form leaves every other entry alone.
        assert grant("p-bob", {"mayReadItems": True}) == {"shareWith/p-bob": {"mayReadItems": True}}

    def test_the_callers_rights_mapping_is_copied(self):
        # Callers reuse a rights template across several grants; sharing the dict
        # would let a later edit rewrite an already-built patch.
        rights = {"mayReadItems": True}
        patch = grant("p-bob", rights)
        rights["mayReadItems"] = False
        assert patch == {"shareWith/p-bob": {"mayReadItems": True}}

    def test_revoking_nulls_one_entry(self):
        # null at a pointer removes the key (RFC 8620 §5.3), which is what makes
        # this expressible without reading the map first - and a read-modify-write
        # is a race that drops any share added in between.
        assert revoke("p-bob") == {"shareWith/p-bob": None}

    def test_unsharing_uses_null_rather_than_an_empty_object(self):
        # §4 defines null as "shared with nobody"; {} is not stated to mean the
        # same thing, so a server is free to treat it differently.
        assert unshare() == {"shareWith": None}

    def test_replacing_copies_every_rights_map(self):
        rights = {"mayReadItems": True}
        patch = replace({"p-bob": rights})
        rights["mayReadItems"] = False
        assert patch == {"shareWith": {"p-bob": {"mayReadItems": True}}}

    def test_replacing_with_an_empty_map_is_a_deliberate_empty_map(self):
        # Distinct from `unshare()`. Both remove everyone; only this one asserts a
        # value the RFC does not define, which is why it is not the default.
        assert replace({}) == {"shareWith": {}}

    def test_subscribing_is_a_single_property_patch(self):
        assert subscribe() == {"isSubscribed": True}

    def test_unsubscribing_is_expressible(self):
        # §4 leaves the initial value implementation-dependent when someone shares
        # with you, so a client has to set it either way rather than assume.
        assert subscribe(subscribed=False) == {"isSubscribed": False}


class TestRights:
    def test_only_the_true_rights_are_held(self):
        assert held({"mayReadItems": True, "mayWriteAll": False}) == {"mayReadItems"}

    def test_no_rights_at_all_holds_nothing(self):
        assert held(None) == set()
        assert held({}) == set()

    def test_a_missing_right_is_not_held(self):
        # RFC 9670 gives no default, so assuming one means the client offers an
        # action the server will refuse.
        assert may({"mayReadItems": True}, "mayWriteAll") is False
        assert may(None, "mayReadItems") is False

    def test_an_explicitly_false_right_is_not_held(self):
        assert may({"mayReadItems": False}, "mayReadItems") is False

    def test_every_named_right_must_be_held(self):
        rights = {"mayReadItems": True, "mayWriteAll": True}
        assert may(rights, "mayReadItems", "mayWriteAll") is True
        assert may(rights, "mayReadItems", "mayAdmin") is False

    def test_asking_about_no_rights_at_all_is_permitted(self):
        # An action gated on an empty list of rights is ungated, which is the
        # right answer for a data type that declares none.
        assert may({}) is True


class TestTargets:
    def test_only_principals_that_may_be_shared_with_are_returned(self):
        principals = [
            Principal(id="p-bob", capabilities={CALENDARS_URN: {"mayShareWith": True}}),
            Principal(id="p-eve", capabilities={CALENDARS_URN: {"mayShareWith": False}}),
        ]
        assert [p.id for p in targets(principals, CALENDARS_URN)] == ["p-bob"]

    def test_a_target_with_no_visible_accounts_still_qualifies(self):
        # Filtering on `accounts` instead would drop every Principal whose
        # accounts the requesting user cannot see - which is most of a directory.
        principals = [
            Principal(
                id="p-bob", capabilities={CALENDARS_URN: {"mayShareWith": True}}, accounts=None
            )
        ]
        assert [p.id for p in targets(principals, CALENDARS_URN)] == ["p-bob"]

    def test_nothing_qualifies_for_a_capability_none_of_them_advertise(self):
        principals = [Principal(id="p-bob", capabilities={CALENDARS_URN: {"mayShareWith": True}})]
        assert targets(principals, "urn:ietf:params:jmap:mail") == []


class TestAccountOnlyLookups:
    """``:principals:owner`` is an accountCapabilities-only key (§1.5.2).

    Falling back to the session-level map would give every unowned account the
    same bogus owner - and then aim ``Principal/*`` at the wrong account, which is
    the one failure mode worse than an error.
    """

    def owned_session(self) -> Session:
        return Session.from_wire(
            {
                # A non-conformant server publishing the key where it never belongs.
                "capabilities": {
                    "urn:ietf:params:jmap:core": {},
                    PRINCIPALS_OWNER_URN: {
                        "accountIdForPrincipal": "WRONG",
                        "principalId": "p-wrong",
                    },
                },
                "accounts": {"a": {"name": "a", "accountCapabilities": {}}},
                "primaryAccounts": {},
                "username": "alice",
                "apiUrl": "https://jmap.example.com/jmap/",
                "state": "s0",
            }
        )

    def test_a_session_level_owner_is_not_read(self):
        assert owner_of(self.owned_session(), "a") is None

    def test_a_session_level_principal_account_is_not_read(self):
        assert principal_account(self.owned_session(), "a") is None

    def test_the_account_level_value_is_read(self):
        session = Session.from_wire(
            {
                "capabilities": {"urn:ietf:params:jmap:core": {}},
                "accounts": {
                    "a": {
                        "name": "a",
                        "accountCapabilities": {
                            PRINCIPALS_OWNER_URN: {
                                "accountIdForPrincipal": "p-account",
                                "principalId": "p1",
                            }
                        },
                    }
                },
                "primaryAccounts": {},
                "username": "alice",
                "apiUrl": "https://jmap.example.com/jmap/",
                "state": "s0",
            }
        )
        assert principal_account(session, "a") == "p-account"
        assert owner_of(session, "a") == "p1"

    def test_an_unknown_account_has_no_owner(self):
        assert owner_of(self.owned_session(), "nope") is None


class TestGrantGuards:
    def test_granting_to_the_owner_is_refused_when_the_owner_is_known(self):
        # `grant` is the recommended path, so the owner check has to live here
        # too - not only on `replace`.
        with pytest.raises(OwnerInShareMapError):
            grant("p-owner", {"mayRead": True}, owner_principal_id="p-owner")

    def test_granting_to_anyone_else_is_fine(self):
        assert grant("p2", {"mayRead": True}, owner_principal_id="p-owner") == {
            "shareWith/p2": {"mayRead": True}
        }

    def test_the_check_is_skipped_when_the_owner_is_unknown(self):
        assert grant("p-owner", {"mayRead": True}) == {"shareWith/p-owner": {"mayRead": True}}

    def test_pointer_separators_are_escaped(self):
        # A JMAP Id cannot contain these, but `Principal.id` is a plain string
        # here and an unescaped `/` addresses a different, nested path.
        assert grant("a/b", {}) == {"shareWith/a~1b": {}}
        assert revoke("a~b") == {"shareWith/a~0b": None}

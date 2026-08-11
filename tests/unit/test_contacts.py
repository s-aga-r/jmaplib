"""RFC 9610 contacts: the JSContact-backed models, and the three capabilities.

Two things here are counter-intuitive enough to be worth pinning down. A
``ContactCard`` carries *two* identifiers - JMAP's ``id`` and JSContact's ``uid``
- and only the second one names the same person across systems, so a client that
deduplicates on ``id`` deduplicates nothing. And the legacy ``Contact`` /
``ContactGroup`` methods are gated by two *vendor* URNs rather than by
``urn:ietf:params:jmap:contacts``, which makes them separate capabilities with a
method inventory that is deliberately not the standard six.
"""

from __future__ import annotations

from jmap.capabilities.contacts import (
    CONTACTS,
    CONTACTS_URN,
    CYRUS_CONTACTS,
    CYRUS_CONTACTS_URN,
    FASTMAIL_CONTACTS,
    FASTMAIL_CONTACTS_URN,
    LEGACY_CONTACT_URNS,
    ContactsCapability,
    looks_like_rfc9610,
)
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPObject
from jmap.models.contacts import (
    ADDRESS_BOOK_HAS_CONTENTS,
    KIND_APPLICATION,
    KIND_DEVICE,
    KIND_GROUP,
    KIND_INDIVIDUAL,
    KIND_LOCATION,
    KIND_ORG,
    OPTIONAL_SORTS,
    REQUIRED_SORTS,
    AddressBook,
    AddressBookRights,
    ContactCard,
    Media,
)


class TestAddressBook:
    def test_an_address_book_parses_from_the_wire(self):
        book = AddressBook.from_wire(
            {
                "id": "ab1",
                "name": "Personal",
                "description": "Friends and family",
                "sortOrder": 2,
                "isDefault": True,
                "isSubscribed": True,
                "myRights": {"mayRead": True, "mayWrite": True},
            }
        )
        assert book.name == "Personal"
        assert book.is_default is True
        assert book.my_rights == AddressBookRights(may_read=True, may_write=True)

    def test_share_with_parses_into_rights_objects(self):
        book = AddressBook.from_wire({"shareWith": {"u2": {"mayRead": True}}})
        assert book.share_with is not None
        assert book.share_with["u2"].may_read is True
        assert book.share_with["u2"].may_write is False

    def test_rights_default_to_denying_everything(self):
        # A server that omits myRights has told us nothing, and "nothing" must not
        # read as permission to write.
        rights = AddressBookRights()
        assert rights.may_read is False
        assert rights.may_write is False
        assert rights.may_share is False
        assert rights.may_delete is False

    def test_a_book_shared_with_nobody_looks_like_a_server_without_sharing(self):
        # Both are None, so a client cannot use this field to decide whether
        # RFC 9670 is available - only the advertised URN says that.
        assert AddressBook.from_wire({"id": "ab1"}).share_with is None

    def test_an_unsubscribed_book_is_distinguishable_from_an_unfetched_one(self):
        # isSubscribed defaults to true for a book you made yourself, so reading
        # an absent value as false hides address books the user just created.
        assert AddressBook.from_wire({"isSubscribed": False}).is_subscribed is False
        assert AddressBook.from_wire({"id": "ab1"}).is_subscribed is None


class TestCardIdentity:
    # RFC 9610 §3 allows the two to differ and requires uid to be unique per
    # account: id addresses the record, uid addresses the person.
    def test_a_cards_uid_is_not_its_id(self):
        card = ContactCard.from_wire({"id": "K1", "uid": "urn:uuid:6b0e-4c1f"})
        assert card.id == "K1"
        assert card.uid == "urn:uuid:6b0e-4c1f"
        assert card.uid != card.id

    def test_the_uid_is_read_out_of_the_jscontact_half(self):
        card = ContactCard.from_wire({"id": "K1", "uid": "urn:uuid:6b0e-4c1f"})
        assert card.jscontact("uid") == "urn:uuid:6b0e-4c1f"

    def test_a_card_whose_uid_was_not_fetched_reports_none(self):
        assert ContactCard.from_wire({"id": "K1"}).uid is None

    def test_a_card_with_no_jscontact_half_at_all_reports_none(self):
        assert ContactCard().uid is None

    # extra is not type-checked by anything, so a server sending a number here
    # would otherwise hand back an int from a property annotated `str | None`.
    def test_a_non_string_uid_reads_as_absent(self):
        assert ContactCard.from_wire({"uid": 7}).uid is None
        assert ContactCard.from_wire({"uid": None}).uid is None


class TestCardKind:
    def test_the_kind_is_read_out_of_the_jscontact_half(self):
        assert ContactCard.from_wire({"kind": "org"}).kind == KIND_ORG

    def test_a_group_card_is_a_group(self):
        assert ContactCard.from_wire({"kind": "group"}).is_group is True

    def test_every_other_kind_is_not_a_group(self):
        for kind in (KIND_INDIVIDUAL, KIND_ORG, KIND_LOCATION, KIND_DEVICE, KIND_APPLICATION):
            assert ContactCard.from_wire({"kind": kind}).is_group is False

    def test_an_absent_kind_is_not_a_group(self):
        assert ContactCard().kind is None
        assert ContactCard().is_group is False

    def test_a_non_string_kind_reads_as_absent(self):
        assert ContactCard.from_wire({"kind": 3}).kind is None
        assert ContactCard.from_wire({"kind": 3}).is_group is False


class TestAddressBookMembership:
    def test_the_set_as_map_reads_as_a_plain_list(self):
        card = ContactCard.from_wire({"addressBookIds": {"ab1": True, "ab2": True}})
        assert sorted(card.address_books) == ["ab1", "ab2"]

    # addressBookIds is a set-as-map, so `list(keys())` would put the card back
    # into a book it was just taken out of.
    def test_a_false_entry_is_not_a_book_the_card_is_in(self):
        card = ContactCard.from_wire({"addressBookIds": {"ab1": True, "ab2": False}})
        assert card.address_books == ["ab1"]

    def test_an_absent_map_reads_as_no_address_books(self):
        assert ContactCard().address_books == []

    # Parseable but not sendable: §3 requires a card to be in at least one book at
    # all times, so an empty map is a malformed patch rather than a delete.
    def test_an_empty_map_reads_as_no_address_books(self):
        assert ContactCard.from_wire({"addressBookIds": {}}).address_books == []

    def test_the_raw_map_stays_available(self):
        card = ContactCard.from_wire({"addressBookIds": {"ab1": True, "ab2": False}})
        assert card.address_book_ids == {"ab1": True, "ab2": False}


class TestGroupMembers:
    def test_members_are_read_from_the_set_as_map(self):
        card = ContactCard.from_wire({"kind": "group", "members": {"urn:uuid:1": True}})
        assert card.members() == ["urn:uuid:1"]

    # The values are uids, so resolving them needs a uid filter on
    # ContactCard/query; a ContactCard/get with them as ids answers notFound.
    def test_the_members_name_uids_rather_than_jmap_ids(self):
        member = ContactCard.from_wire({"id": "K7", "uid": "urn:uuid:member"})
        group = ContactCard.from_wire({"kind": "group", "members": {"urn:uuid:member": True}})
        assert group.members() == [member.uid]
        assert member.id not in group.members()

    def test_a_false_member_is_not_a_member(self):
        card = ContactCard.from_wire({"members": {"u1": True, "u2": False}})
        assert card.members() == ["u1"]

    def test_a_card_with_no_members_property_has_none(self):
        assert ContactCard().members() == []
        assert ContactCard.from_wire({"kind": "group"}).members() == []

    def test_an_empty_members_map_reads_as_no_members(self):
        assert ContactCard.from_wire({"members": {}}).members() == []

    # An array is the shape a JSContact-naive server sends, and walking it as a
    # mapping would raise from inside the accessor instead of degrading.
    def test_a_members_property_that_is_not_an_object_reads_as_empty(self):
        assert ContactCard.from_wire({"members": ["u1", "u2"]}).members() == []
        assert ContactCard.from_wire({"members": None}).members() == []
        assert ContactCard.from_wire({"members": "u1"}).members() == []

    def test_members_are_reported_whatever_the_kind_claims(self):
        # A group is identified by `kind`, but the accessor does not gate on it:
        # a mislabelled card should still surrender what it contains.
        assert ContactCard.from_wire({"members": {"u1": True}}).is_group is False
        assert ContactCard.from_wire({"members": {"u1": True}}).members() == ["u1"]


class TestJSContactBody:
    def test_a_jscontact_property_is_read_by_its_exact_wire_name(self):
        card = ContactCard.from_wire({"id": "K1", "name": {"full": "Alice Smith"}})
        assert card.jscontact("name") == {"full": "Alice Smith"}

    def test_an_unfetched_property_is_none(self):
        assert ContactCard.from_wire({"id": "K1"}).jscontact("organizations") is None

    def test_a_card_with_no_extras_at_all_answers_none(self):
        assert ContactCard().jscontact("name") is None

    # The value of this type to a client is mostly in handing the card back
    # unchanged, so a read-modify-write must not strip what is not modelled.
    def test_the_jscontact_body_round_trips_losslessly(self):
        wire = {
            "id": "K1",
            "addressBookIds": {"ab1": True},
            "@type": "Card",
            "version": "1.0",
            "uid": "urn:uuid:6b0e-4c1f",
            "kind": "individual",
            "name": {"full": "Alice Smith"},
            "emails": {"e1": {"address": "alice@example.com", "contexts": {"work": True}}},
        }
        assert ContactCard.from_wire(wire).to_wire() == wire

    def test_an_untouched_card_sends_nothing(self):
        assert ContactCard().to_wire() == {}


class TestMedia:
    def test_a_photo_parses_from_the_wire(self):
        media = Media.from_wire(
            {"@type": "Media", "kind": "photo", "blobId": "G1", "mediaType": "image/jpeg"}
        )
        assert media.at_type == "Media"
        assert media.kind == "photo"
        assert media.media_type == "image/jpeg"

    # `@type` is not a Python identifier, so the field is aliased - and the alias
    # has to survive serialisation or the object stops being valid JSContact.
    def test_the_type_discriminator_keeps_its_wire_spelling(self):
        assert Media.from_wire({"@type": "Media"}).to_wire() == {"@type": "Media"}

    def test_a_blob_backed_photo_is_recognised(self):
        media = Media.from_wire({"blobId": "G1", "mediaType": "image/jpeg"})
        assert media.is_blob_backed is True
        assert media.uri is None

    def test_a_uri_photo_is_not_blob_backed(self):
        assert Media.from_wire({"uri": "https://example.com/a.jpg"}).is_blob_backed is False

    def test_a_media_object_carrying_neither_is_not_blob_backed(self):
        assert Media().is_blob_backed is False


class TestContactsConstants:
    def test_the_destroy_error_type_is_named(self):
        assert ADDRESS_BOOK_HAS_CONTENTS == "addressBookHasContents"

    def test_the_kind_values_are_the_jscontact_spellings(self):
        assert (KIND_INDIVIDUAL, KIND_GROUP, KIND_ORG) == ("individual", "group", "org")
        assert (KIND_LOCATION, KIND_DEVICE, KIND_APPLICATION) == (
            "location",
            "device",
            "application",
        )

    # §3.3.2 splits them: sorting by name is only a SHOULD, so a client that
    # assumes it earns unsupportedSort from an otherwise conformant server.
    def test_only_the_timestamps_are_required_sorts(self):
        assert set(REQUIRED_SORTS) == {"created", "updated"}
        assert set(OPTIONAL_SORTS) == {"name/given", "name/surname", "name/surname2"}
        assert REQUIRED_SORTS.isdisjoint(OPTIONAL_SORTS)


class TestContactsCapabilityObject:
    def test_the_advertised_fields_are_read(self):
        capability = ContactsCapability.of(
            {"maxAddressBooksPerCard": 1, "mayCreateAddressBook": True}
        )
        assert capability.max_address_books_per_card == 1
        assert capability.may_create_address_book is True

    def test_an_absent_object_grants_no_permission_and_states_no_limit(self):
        capability = ContactsCapability.of({})
        assert capability.may_create_address_book is False
        assert capability.max_address_books_per_card is None

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        # Both halves matter: a bad field value raises ValidationError, while a
        # non-mapping raises from dict() before pydantic ever sees it.
        bad_field = ContactsCapability.of({"maxAddressBooksPerCard": "lots"})
        assert bad_field.max_address_books_per_card is None
        assert ContactsCapability.of(7).may_create_address_book is False
        assert ContactsCapability.of("not an object").may_create_address_book is False
        assert ContactsCapability.of(None).max_address_books_per_card is None

    def test_one_bad_field_costs_the_whole_object(self):
        # Degrading is all or nothing: a null mayCreateAddressBook discards the
        # sibling limit too, so neither field can be read as authoritative once
        # any part of the object is malformed.
        capability = ContactsCapability.of(
            {"mayCreateAddressBook": None, "maxAddressBooksPerCard": 5}
        )
        assert capability.max_address_books_per_card is None
        assert capability.may_create_address_book is False

    def test_an_unmodelled_field_survives(self):
        capability = ContactsCapability.of({"mayCreateAddressBook": True, "someNewLimit": 3})
        assert capability.may_create_address_book is True
        assert capability.to_wire()["someNewLimit"] == 3


class TestRFC9610Heuristic:
    def test_either_required_field_is_evidence_enough(self):
        assert looks_like_rfc9610({"mayCreateAddressBook": True}) is True
        assert looks_like_rfc9610({"maxAddressBooksPerCard": 1}) is True

    # §1.4.1 makes both MUST-contain, so presence is the signal: a false or null
    # value is still an RFC 9610 server declaring itself.
    def test_presence_is_what_counts_not_the_value(self):
        assert looks_like_rfc9610({"mayCreateAddressBook": False}) is True
        assert looks_like_rfc9610({"maxAddressBooksPerCard": None}) is True

    # And is not evidence of the opposite either: a real Fastmail capture shows
    # the IETF URN advertised as a bare {} beside a legacy-only account, which is
    # why the vendor URNs rather than this are what gate the legacy methods.
    def test_a_bare_object_is_not_evidence(self):
        assert looks_like_rfc9610({}) is False

    def test_a_non_object_is_not_evidence(self):
        assert looks_like_rfc9610(None) is False
        assert looks_like_rfc9610("urn:ietf:params:jmap:contacts") is False
        assert looks_like_rfc9610([("mayCreateAddressBook", True)]) is False

    # Fastmail advertises one on *calendars*; a lookup that expects it here reads
    # as absent on every server and silently picks the legacy model.
    def test_there_is_no_is_rfc_flag_to_look_for(self):
        assert looks_like_rfc9610({"isRFC": True}) is False


class TestContactsSpec:
    def test_the_urn_is_registered_under_its_attribute(self):
        assert CONTACTS_URN == "urn:ietf:params:jmap:contacts"
        assert CONTACTS.urn == CONTACTS_URN
        assert CONTACTS.attr == "contacts"
        assert CONTACTS.reference == "RFC 9610"

    def test_it_is_not_experimental(self):
        # RFC 9610 is published, unlike calendars and files, so it resolves
        # without the caller opting in.
        assert CONTACTS.experimental is False

    def test_it_needs_no_other_capability(self):
        assert CONTACTS.requires == frozenset()

    def test_it_owns_the_rfc_data_types_bound_to_their_models(self):
        for name, model in (("AddressBook", AddressBook), ("ContactCard", ContactCard)):
            data_type = CONTACTS.data_type(name)
            assert data_type is not None
            assert data_type.model is model

    def test_it_owns_neither_legacy_type(self):
        assert CONTACTS.data_type("Contact") is None
        assert CONTACTS.data_type("ContactGroup") is None

    def test_only_the_address_book_is_shareable(self):
        book = CONTACTS.data_type("AddressBook")
        card = CONTACTS.data_type("ContactCard")
        assert book is not None
        assert card is not None
        assert book.shareable is True
        assert card.shareable is False

    def test_the_account_capability_model_is_declared(self):
        assert CONTACTS.account_value is ContactsCapability
        assert CONTACTS.session_value is None

    # RFC 9610 defines none, so declaring one would put a method on the façade
    # that no conformant server implements.
    def test_there_is_no_address_book_query(self):
        assert CONTACTS.method("AddressBook/query") is None
        assert CONTACTS.method("AddressBook/queryChanges") is None
        assert CONTACTS.method("AddressBook/get") is not None

    def test_the_full_method_set_is_declared(self):
        assert {method.name for method in CONTACTS.methods} == {
            "AddressBook/get",
            "AddressBook/changes",
            "AddressBook/set",
            "ContactCard/get",
            "ContactCard/changes",
            "ContactCard/query",
            "ContactCard/queryChanges",
            "ContactCard/set",
            "ContactCard/copy",
        }

    def test_the_writing_methods_are_marked_mutating(self):
        for name in ("AddressBook/set", "ContactCard/set", "ContactCard/copy"):
            method = CONTACTS.method(name)
            assert method is not None
            assert method.mutating is True

    def test_the_reading_methods_are_not(self):
        for name in ("AddressBook/get", "ContactCard/query"):
            method = CONTACTS.method(name)
            assert method is not None
            assert method.mutating is False

    # Without onDestroyRemoveContents a non-empty book answers
    # addressBookHasContents rather than being destroyed.
    def test_address_book_destroy_declares_its_escape_hatch(self):
        method = CONTACTS.method("AddressBook/set")
        assert method is not None
        assert set(method.extra_args) == {"onDestroyRemoveContents", "onSuccessSetIsDefault"}

    def test_card_set_takes_no_arguments_beyond_the_standard_shape(self):
        method = CONTACTS.method("ContactCard/set")
        assert method is not None
        assert dict(method.extra_args) == {}

    def test_the_bulk_methods_declare_the_limit_that_chunks_them(self):
        for name, key in (
            ("AddressBook/get", LimitKey.GET_OBJECTS),
            ("ContactCard/get", LimitKey.GET_OBJECTS),
            ("AddressBook/set", LimitKey.SET_OBJECTS),
            ("ContactCard/set", LimitKey.SET_OBJECTS),
        ):
            method = CONTACTS.method(name)
            assert method is not None
            assert method.chunk_by is key


class TestLegacyContactsSpecs:
    # Opaque strings matched byte for byte against the session: not http, not
    # without the www, not singular, no trailing path.
    def test_the_vendor_urns_are_spelled_exactly(self):
        assert FASTMAIL_CONTACTS_URN == "https://www.fastmail.com/dev/contacts"
        assert CYRUS_CONTACTS_URN == "https://cyrusimap.org/ns/jmap/contacts"

    # The point of the whole module. Calling Contact/get with only the IETF URN in
    # `using` earns unknownMethod, and the error names the method rather than the
    # capability that was missing.
    def test_the_legacy_methods_hang_off_the_vendor_urns_not_the_ietf_one(self):
        assert FASTMAIL_CONTACTS.urn == FASTMAIL_CONTACTS_URN
        assert CYRUS_CONTACTS.urn == CYRUS_CONTACTS_URN
        assert CONTACTS.method("Contact/get") is None
        assert CONTACTS.method("ContactGroup/get") is None
        assert FASTMAIL_CONTACTS.method("ContactCard/get") is None
        assert FASTMAIL_CONTACTS.method("AddressBook/get") is None

    # A server may advertise several at once - Cyrus 3.10 exposes both models
    # concurrently - so they cannot be two flavours of one registration.
    def test_the_three_urns_are_three_separate_capabilities(self):
        assert len({CONTACTS.urn, FASTMAIL_CONTACTS.urn, CYRUS_CONTACTS.urn}) == 3
        assert set(LEGACY_CONTACT_URNS) == {FASTMAIL_CONTACTS_URN, CYRUS_CONTACTS_URN}
        assert CONTACTS_URN not in LEGACY_CONTACT_URNS

    def test_each_vendor_gets_its_own_client_attribute(self):
        assert FASTMAIL_CONTACTS.attr == "fastmail_contacts"
        assert CYRUS_CONTACTS.attr == "cyrus_contacts"
        assert FASTMAIL_CONTACTS.reference == "Fastmail vendor extension"
        assert CYRUS_CONTACTS.reference == "Cyrus vendor extension"

    def test_each_vendor_owns_the_pre_rfc_data_types(self):
        for spec in (FASTMAIL_CONTACTS, CYRUS_CONTACTS):
            assert {data_type.name for data_type in spec.data_types} == {"Contact", "ContactGroup"}

    # Their vocabulary was never standardised and the two vendors diverged, so the
    # default model keeps every key addressable exactly as the server spelled it.
    def test_the_legacy_types_resolve_to_the_lossless_fallback(self):
        for spec in (FASTMAIL_CONTACTS, CYRUS_CONTACTS):
            for name in ("Contact", "ContactGroup"):
                data_type = spec.data_type(name)
                assert data_type is not None
                assert data_type.model is JMAPObject

    # Not an oversight: neither vendor implements it, so offering it would put a
    # .query_changes on the façade that can only answer unknownMethod.
    def test_a_legacy_contact_has_no_query_changes(self):
        assert FASTMAIL_CONTACTS.method("Contact/queryChanges") is None
        assert CYRUS_CONTACTS.method("Contact/queryChanges") is None
        assert FASTMAIL_CONTACTS.method("Contact/query") is not None

    # The standard six would never have given it one, which is exactly why the
    # inventory is declared by hand rather than generated per data type.
    def test_a_legacy_contact_group_does_have_a_query(self):
        assert FASTMAIL_CONTACTS.method("ContactGroup/query") is not None
        assert CYRUS_CONTACTS.method("ContactGroup/query") is not None

    def test_only_contacts_can_be_copied(self):
        assert FASTMAIL_CONTACTS.method("Contact/copy") is not None
        assert FASTMAIL_CONTACTS.method("ContactGroup/copy") is None

    def test_the_full_legacy_inventory_is_declared(self):
        assert {method.name for method in FASTMAIL_CONTACTS.methods} == {
            "Contact/get",
            "Contact/changes",
            "Contact/query",
            "Contact/set",
            "Contact/copy",
            "ContactGroup/get",
            "ContactGroup/changes",
            "ContactGroup/query",
            "ContactGroup/set",
        }

    def test_both_vendors_ship_the_same_inventory(self):
        assert {method.name for method in FASTMAIL_CONTACTS.methods} == {
            method.name for method in CYRUS_CONTACTS.methods
        }

    def test_the_legacy_writing_methods_are_marked_mutating(self):
        for name in ("Contact/set", "Contact/copy", "ContactGroup/set"):
            method = FASTMAIL_CONTACTS.method(name)
            assert method is not None
            assert method.mutating is True

    def test_the_legacy_capabilities_advertise_no_typed_value_object(self):
        # Neither vendor documents one, so modelling fields for it would be
        # invention rather than description.
        assert FASTMAIL_CONTACTS.account_value is None
        assert CYRUS_CONTACTS.account_value is None
        assert FASTMAIL_CONTACTS.session_value is None
        assert CYRUS_CONTACTS.session_value is None

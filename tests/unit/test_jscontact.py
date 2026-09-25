"""JSContact (RFC 9553): the Card's objects, typed, lossless and forgiving.

The fixture is a card Stalwart 0.16.17 returned for a ContactCard/set create,
so it is what a real server sends rather than what the RFC hopes for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from jmap.models.contacts import ContactCard, Media
from jmap.models.jscontact import (
    Address,
    Anniversary,
    Calendar,
    Card,
    CryptoKey,
    Directory,
    EmailAddress,
    Name,
    NameComponent,
    PartialDate,
    Timestamp,
)
from jmap.models.jsobject import JSObject

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stalwart-0.16.17-contact-card.json"


@pytest.fixture
def wire() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


@pytest.fixture
def card(wire: dict[str, Any]) -> ContactCard:
    return ContactCard.from_wire(wire)


class TestAStalwartCard:
    def test_it_round_trips_exactly(self, card, wire):
        assert card.to_wire() == wire

    def test_nothing_it_sent_is_left_unmodelled(self, card):
        # Every property Stalwart returned is one RFC 9553 or RFC 9610 defines.
        assert card.model_extra == {}

    def test_the_name_reads_as_parts_and_in_full(self, card):
        assert card.name is not None
        assert card.name.full == "Ada Lovelace"
        assert card.name.components is not None
        assert [(part.kind, part.value) for part in card.name.components] == [
            ("given", "Ada"),
            ("surname", "Lovelace"),
        ]

    def test_contact_points_are_keyed_by_their_card_local_ids(self, card):
        assert card.emails is not None
        email = card.emails["e1"]
        assert (email.address, email.contexts, email.pref, email.label) == (
            "ada@example.com",
            {"work": True},
            1,
            "main",
        )
        assert card.phones is not None
        assert card.phones["p1"].features == {"voice": True, "mobile": True}
        assert card.online_services is not None
        assert card.online_services["s1"].user == "@ada@example.social"
        assert card.preferred_languages is not None
        assert card.preferred_languages["l1"].language == "en"

    def test_a_title_points_at_an_organization_on_the_same_card(self, card):
        assert card.titles is not None
        assert card.organizations is not None
        title = card.titles["t1"]
        assert title.kind == "role"
        assert title.organization_id is not None
        organization = card.organizations[title.organization_id]
        assert organization.units is not None
        assert organization.units[0].name == "Engines"

    def test_an_anniversary_date_is_partial_or_a_timestamp(self, card):
        assert card.anniversaries is not None
        birth = card.anniversaries["b1"].date
        death = card.anniversaries["d1"].date
        assert isinstance(birth, PartialDate)
        assert (birth.year, birth.month, birth.day) == (1815, 12, 10)
        assert isinstance(death, Timestamp)
        assert death.utc == "1852-11-27T12:00:00Z"

    def test_the_rest_reads_through(self, card):
        assert card.addresses is not None
        address = card.addresses["a1"]
        assert address.country_code == "GB"
        assert address.components is not None
        assert address.components[-1].kind == "locality"
        assert card.speak_to_as is not None
        assert card.speak_to_as.pronouns is not None
        assert card.speak_to_as.pronouns["pr1"].pronouns == "she/her"
        assert card.related_to is not None
        assert card.related_to["urn:uuid:babbage"].relation == {"colleague": True}
        assert card.notes is not None
        author = card.notes["n1"].author
        assert author is not None
        assert author.name == "Charles"
        assert card.personal_info is not None
        assert card.personal_info["pi1"].level == "high"
        assert card.links is not None
        assert card.links["k1"].kind == "contact"
        assert card.keywords == {"vip": True}
        assert card.nicknames is not None
        assert card.nicknames["n1"].name == "Countess"


class TestTheRestOfRfc9553:
    """Properties Stalwart's card did not carry, from the RFC's own figures."""

    WIRE: ClassVar[dict[str, Any]] = {
        "@type": "Card",
        "version": "1.0",
        "uid": "urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
        "kind": "group",
        "members": {"urn:uuid:03a0e51f-d1aa-4385-8a53-e29025acd8af": True},
        "language": "de-AT",
        "prodId": "ACME Contacts App version 1.23.5",
        "created": "2022-09-30T14:35:10Z",
        "updated": "2021-10-31T22:27:10Z",
        "name": {
            "components": [{"kind": "surname", "value": "Doe", "phonetic": "/doʊ/"}],
            "sortAs": {"surname": "Doe"},
            "defaultSeparator": " ",
            "phoneticSystem": "ipa",
        },
        "calendars": {
            "calendar-1": {
                "@type": "Calendar",
                "kind": "calendar",
                "uri": "webcal://calendar.example.com/calendar.ics",
            }
        },
        "schedulingAddresses": {"sched1": {"uri": "mailto:janedoe@example.com"}},
        "cryptoKeys": {"mykey1": {"uri": "https://www.example.com/keys/jdoe.cer"}},
        "directories": {
            "dir1": {"kind": "entry", "uri": "https://dir.example.com/addrbook/jdoe", "listAs": 1}
        },
        "media": {
            "res45": {
                "kind": "sound",
                "uri": "CID:JOHNQ.part8.19960229T080000.xyzMail@example.com",
                "mediaType": "audio/mp3",
            }
        },
        "localizations": {"uk-UA": {"name/full": "Іван Іванович"}},
        "anniversaries": {
            "k8": {
                "kind": "birth",
                "date": {"year": 1953, "month": 4, "day": 15},
                "place": {"full": "Los Angeles"},
            }
        },
    }

    def test_it_round_trips_exactly(self):
        assert Card.from_wire(self.WIRE).to_wire() == self.WIRE

    def test_every_property_is_modelled(self):
        card = Card.from_wire(self.WIRE)
        assert card.model_extra == {}
        assert card.member_uids == {"urn:uuid:03a0e51f-d1aa-4385-8a53-e29025acd8af": True}
        assert card.prod_id == "ACME Contacts App version 1.23.5"
        assert card.name is not None
        assert card.name.sort_as == {"surname": "Doe"}
        assert card.calendars is not None
        assert isinstance(card.calendars["calendar-1"], Calendar)
        assert card.scheduling_addresses is not None
        assert card.scheduling_addresses["sched1"].uri == "mailto:janedoe@example.com"
        assert card.crypto_keys is not None
        assert isinstance(card.crypto_keys["mykey1"], CryptoKey)
        assert card.directories is not None
        directory = card.directories["dir1"]
        assert isinstance(directory, Directory)
        assert directory.list_as == 1
        assert card.media_resources is not None
        assert card.media_resources["res45"].media_type == "audio/mp3"
        assert card.localizations == {"uk-UA": {"name/full": "Іван Іванович"}}
        assert card.anniversaries is not None
        place = card.anniversaries["k8"].place
        assert isinstance(place, Address)
        assert place.full == "Los Angeles"


class TestBuildingOne:
    def test_it_sends_what_it_was_given_by_wire_name(self):
        card = ContactCard(
            address_book_ids={"b1": True},
            name=Name(components=[NameComponent(kind="given", value="Ada")], full="Ada"),
            emails={"e1": EmailAddress(address="ada@example.com")},
            members={"urn:uuid:1": True},
        )
        assert card.to_wire() == {
            "addressBookIds": {"b1": True},
            "name": {"components": [{"kind": "given", "value": "Ada"}], "full": "Ada"},
            "emails": {"e1": {"address": "ada@example.com"}},
            "members": {"urn:uuid:1": True},
        }

    def test_type_and_version_are_left_to_the_server(self):
        # Both are mandatory in RFC 9553, and a server fills them in; an
        # untouched card still sends nothing at all.
        assert ContactCard().to_wire() == {}

    def test_a_timestamp_always_says_what_it_is(self):
        # Dates default to PartialDate, so without its @type a Timestamp would
        # be read as a partial date with no parts.
        birth = Anniversary(kind="birth", date=PartialDate(year=1815))
        death = Anniversary(kind="death", date=Timestamp(utc="1852-11-27T12:00:00Z"))
        assert birth.to_wire() == {"kind": "birth", "date": {"year": 1815}}
        assert death.to_wire() == {
            "kind": "death",
            "date": {"@type": "Timestamp", "utc": "1852-11-27T12:00:00Z"},
        }

    def test_a_date_of_another_type_is_refused(self):
        with pytest.raises(ValidationError, match="does not match any of the expected tags"):
            Anniversary(kind="birth", date=Name(full="not a date"))

    def test_a_misfit_named_in_python_still_raises(self):
        # `member_uids` is no wire name, so there is nothing to keep it as.
        with pytest.raises(ValidationError, match="member_uids"):
            ContactCard(member_uids=["u1"])


class TestForgiveness:
    def test_a_misfit_value_is_kept_as_it_came(self):
        wire = {"id": "c1", "emails": {"e1": {"address": 42, "label": "old"}}}
        card = ContactCard.from_wire(wire)
        assert card.emails is not None
        assert card.emails["e1"].address is None
        assert card.emails["e1"].label == "old"
        assert card.to_wire() == wire

    def test_it_costs_only_its_own_property(self):
        card = ContactCard.from_wire({"id": "c1", "name": "Ada", "kind": "individual"})
        assert card.name is None
        assert card.kind == "individual"
        assert card.jscontact("name") == "Ada"

    def test_an_unknown_date_type_reads_as_a_partial_date(self):
        anniversary = Anniversary.from_wire({"date": {"@type": "Era", "year": 5}})
        assert isinstance(anniversary.date, PartialDate)
        assert anniversary.to_wire() == {"date": {"@type": "Era", "year": 5}}

    def test_a_vendor_property_rides_along(self):
        wire = {"example.com:mascot": {"name": "Tux"}, "kind": "org"}
        card = Card.from_wire(wire)
        assert card.jscontact("example.com:mascot") == {"name": "Tux"}
        assert card.to_wire() == wire


class TestReadingByWireName:
    def test_a_modelled_property_answers_in_its_wire_form(self, card):
        assert card.jscontact("speakToAs") == {
            "grammaticalGender": "feminine",
            "pronouns": {"pr1": {"pronouns": "she/her"}},
        }

    def test_an_unset_property_is_none(self):
        assert ContactCard(kind="org").jscontact("name") is None

    def test_a_property_set_to_null_is_none(self):
        assert ContactCard.from_wire({"name": None}).jscontact("name") is None


class TestMediaIsAResource:
    def test_it_keeps_the_common_resource_properties(self):
        media = Media.from_wire({"kind": "logo", "uri": "https://x/l.png", "pref": 1})
        assert (media.pref, media.is_blob_backed) == (1, False)

    def test_every_object_is_forgiving(self):
        assert issubclass(Media, JSObject)
        assert issubclass(ContactCard, Card)

"""Model foundations: camelCase aliasing, lossless fallback, tri-state UNSET."""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jmap.models.base import (
    UNSET,
    JMAPModel,
    JMAPObject,
    UnsetType,
    omit_unset,
)
from jmap.models.responses import GetResponse


class SampleMailbox(JMAPModel):
    """Stands in for a real spec object; field names cover the aliasing cases."""

    id: str | None = None
    name: str | None = None
    parent_id: str | None = None
    total_emails: int = 0
    is_subscribed: bool = False


class SampleEvent(JMAPModel):
    created_at: datetime | None = None


class PlainModel(BaseModel):
    """A non-JMAPModel target, to prove ``as_`` accepts any pydantic model."""

    id: str


# --------------------------------------------------------------------------- #
# JMAPModel
# --------------------------------------------------------------------------- #
class TestAliases:
    @pytest.mark.parametrize(
        ("field", "wire"),
        [
            ("id", "id"),
            ("name", "name"),
            ("parent_id", "parentId"),
            ("total_emails", "totalEmails"),
            ("is_subscribed", "isSubscribed"),
        ],
    )
    def test_alias_is_camel_case(self, field, wire):
        assert SampleMailbox.model_fields[field].alias == wire

    def test_validates_by_alias(self):
        mailbox = SampleMailbox.from_wire({"parentId": "P1", "totalEmails": 3})
        assert mailbox.parent_id == "P1"
        assert mailbox.total_emails == 3

    def test_validates_by_name(self):
        mailbox = SampleMailbox.model_validate({"parent_id": "P1"})
        assert mailbox.parent_id == "P1"

    def test_serialises_by_alias(self):
        assert SampleMailbox(parent_id="P1").to_wire() == {"parentId": "P1"}

    def test_from_wire_accepts_any_mapping(self):
        mailbox = SampleMailbox.from_wire(MappingProxyType({"parentId": "P1"}))
        assert mailbox.parent_id == "P1"

    def test_to_wire_is_json_mode(self):
        moment = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
        wire = SampleEvent(created_at=moment).to_wire()
        assert isinstance(wire["createdAt"], str)


class TestTriState:
    """``None`` must mean literal ``null``, never "not supplied" (RFC 8620 §5.1)."""

    def test_untouched_fields_are_absent(self):
        assert SampleMailbox(name="Inbox").to_wire() == {"name": "Inbox"}

    def test_explicit_none_survives_as_null(self):
        # A Mailbox at the top level is `parentId: null`, not a missing key.
        assert SampleMailbox(name="Inbox", parent_id=None).to_wire() == {
            "name": "Inbox",
            "parentId": None,
        }

    def test_defaulted_field_is_not_serialised(self):
        # total_emails defaults to 0; sending 0 would be a spurious write.
        assert "totalEmails" not in SampleMailbox(name="Inbox").to_wire()

    def test_none_from_the_wire_round_trips(self):
        mailbox = SampleMailbox.from_wire({"name": "Inbox", "parentId": None})
        assert mailbox.parent_id is None
        assert mailbox.to_wire() == {"name": "Inbox", "parentId": None}


class TestExtraFields:
    """Canary tests. If pydantic ever stops tracking extras in ``model_fields_set``,
    ``exclude_unset=True`` would silently strip every unmodelled property from
    every write. These must fail loudly on such an upgrade."""

    def test_extras_land_in_model_fields_set(self):
        mailbox = SampleMailbox.from_wire({"name": "Inbox", "myserver:quota": 42})
        assert "myserver:quota" in mailbox.model_fields_set

    def test_extras_survive_exclude_unset(self):
        payload = {"name": "Inbox", "myserver:quota": 42, "futureProperty": "x"}
        assert SampleMailbox.from_wire(payload).to_wire() == payload

    def test_extras_passed_to_the_constructor_survive(self):
        mailbox = SampleMailbox(name="Inbox", **{"futureProperty": "x"})
        assert mailbox.to_wire() == {"name": "Inbox", "futureProperty": "x"}

    def test_extras_keep_their_raw_key(self):
        # No alias generation is applied to unknown keys: a server property named
        # `some_thing` must go back out as `some_thing`.
        mailbox = SampleMailbox.from_wire({"some_thing": 1})
        assert mailbox.to_wire() == {"some_thing": 1}

    def test_extras_are_kept_verbatim(self):
        mailbox = SampleMailbox.from_wire({"name": "Inbox", "futureProperty": "x"})
        assert mailbox.model_extra == {"futureProperty": "x"}


# --------------------------------------------------------------------------- #
# JMAPObject
# --------------------------------------------------------------------------- #
WIRE = {
    "id": "M1",
    "mailboxIds": {"MB1": True},
    "header:Subject": "hi",
    "digest:sha-256": "abc",
    "data:asBase64": "aGk=",
    "keys": ["a"],
}


@pytest.fixture
def obj():
    return JMAPObject(dict(WIRE))


class TestJMAPObjectMapping:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("id", "M1"),
            ("mailboxIds", {"MB1": True}),
            # The reason this type exists: colon-bearing keys stay addressable.
            ("header:Subject", "hi"),
            ("digest:sha-256", "abc"),
            ("data:asBase64", "aGk="),
        ],
    )
    def test_raw_keys_are_exposed_unchanged(self, obj, key, expected):
        assert obj[key] == expected

    def test_missing_key_raises_key_error(self, obj):
        with pytest.raises(KeyError):
            obj["nope"]

    def test_len_and_iter(self, obj):
        assert len(obj) == len(WIRE)
        assert list(obj) == list(WIRE)
        assert set(obj.keys()) == set(WIRE)

    def test_mapping_helpers_come_from_the_abc(self, obj):
        assert obj.get("id") == "M1"
        assert obj.get("nope", "fallback") == "fallback"
        assert "header:Subject" in obj
        assert dict(obj.items()) == WIRE

    def test_empty_object_is_falsey(self):
        assert not JMAPObject({})

    def test_raw_dict_is_adopted_not_copied(self):
        payload = dict(WIRE)
        assert JMAPObject(payload).raw is payload

    def test_non_dict_mapping_is_copied(self):
        proxy = MappingProxyType(dict(WIRE))
        wrapped = JMAPObject(proxy)
        assert wrapped.raw == WIRE
        assert isinstance(wrapped.raw, dict)

    def test_to_wire_returns_the_raw_dict(self, obj):
        assert obj.to_wire() is obj.raw


class TestJMAPObjectAttributes:
    @pytest.mark.parametrize(
        ("attribute", "expected"),
        [
            ("id", "M1"),
            ("mailbox_ids", {"MB1": True}),
            ("mailboxIds", {"MB1": True}),
        ],
    )
    def test_snake_case_finds_the_camel_case_key(self, obj, attribute, expected):
        assert getattr(obj, attribute) == expected

    def test_missing_attribute_raises_attribute_error(self, obj):
        with pytest.raises(AttributeError, match="no property 'subject'"):
            _ = obj.subject

    def test_getattr_default_and_hasattr(self, obj):
        assert getattr(obj, "subject", None) is None
        assert not hasattr(obj, "subject")
        assert hasattr(obj, "mailbox_ids")

    def test_private_names_never_reach_the_payload(self):
        # Otherwise copy/pickle probes would be answered from wire data.
        wrapped = JMAPObject({"_secret": 1, "__wat__": 2})
        with pytest.raises(AttributeError):
            _ = wrapped._secret
        with pytest.raises(AttributeError):
            _ = wrapped.__wat__
        assert wrapped["_secret"] == 1

    def test_real_methods_shadow_wire_keys(self, obj):
        # `keys` is a Mapping method, so item access is the only way to the value.
        assert callable(obj.keys)
        assert obj["keys"] == ["a"]

    def test_nested_values_are_returned_raw(self, obj):
        assert isinstance(obj.mailbox_ids, dict)


class TestJMAPObjectConversion:
    def test_as_validates_into_a_model(self):
        mailbox = JMAPObject({"id": "MB1", "parentId": None, "name": "Inbox"}).as_(SampleMailbox)
        assert isinstance(mailbox, SampleMailbox)
        assert mailbox.parent_id is None
        assert mailbox.name == "Inbox"

    def test_as_accepts_a_plain_pydantic_model(self):
        assert JMAPObject({"id": "M1"}).as_(PlainModel).id == "M1"

    def test_as_propagates_validation_errors(self):
        with pytest.raises(ValueError, match="totalEmails"):
            JMAPObject({"totalEmails": "not an int"}).as_(SampleMailbox)

    def test_round_trip_through_a_model_is_lossless(self, obj):
        # Unknown keys, colons and all, survive JMAPObject -> model -> wire.
        assert obj.as_(SampleMailbox).to_wire() == WIRE


class TestJMAPObjectProtocol:
    def test_equal_to_the_plain_dict(self, obj):
        assert obj == WIRE
        assert obj == JMAPObject(dict(WIRE))
        assert obj == MappingProxyType(dict(WIRE))

    def test_unequal(self, obj):
        assert obj != {"id": "M2"}
        assert obj != JMAPObject({"id": "M2"})

    def test_comparison_with_a_non_mapping_is_not_equal(self, obj):
        assert obj != "M1"
        assert obj is not None

    def test_repr_is_readable_when_short(self):
        assert repr(JMAPObject({"id": "M1"})) == "JMAPObject({'id': 'M1'})"

    def test_repr_truncates_long_payloads(self):
        wrapped = JMAPObject({"body": "x" * 5000})
        text = repr(wrapped)
        assert len(text) < 200
        assert text.endswith("...)")


# --------------------------------------------------------------------------- #
# UNSET
# --------------------------------------------------------------------------- #
class TestUnset:
    def test_is_falsey_but_not_none(self):
        # Falsey so `if arg:` reads naturally, yet distinct from every JSON value.
        assert not UNSET
        assert UNSET.__bool__() is False
        assert UNSET is not None

    def test_repr(self):
        assert repr(UNSET) == "UNSET"

    def test_is_a_singleton(self):
        assert UnsetType() is UNSET

    @pytest.mark.parametrize(
        "clone", [copy.copy, copy.deepcopy, lambda v: pickle.loads(pickle.dumps(v))]
    )
    def test_survives_copying(self, clone):
        # A copy that is not UNSET would be serialised as a value.
        assert clone(UNSET) is UNSET

    def test_survives_nested_deepcopy(self):
        payload: dict[str, Any] = {"ids": UNSET, "nested": [UNSET]}
        clone = copy.deepcopy(payload)
        assert clone["ids"] is UNSET
        assert clone["nested"][0] is UNSET


class TestOmitUnset:
    def test_drops_only_unset(self):
        assert omit_unset(ids=UNSET, properties=["id"]) == {"properties": ["id"]}

    @pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
    def test_keeps_every_other_falsey_value(self, value):
        # `ids=None` means "all records" (RFC 8620 §5.1) and must reach the wire.
        assert omit_unset(ids=value) == {"ids": value}

    def test_empty_call(self):
        assert omit_unset() == {}

    def test_all_unset(self):
        assert omit_unset(a=UNSET, b=UNSET) == {}


class TestJMAPObjectAsAPydanticField:
    """The fallback model has to work inside a typed response.

    ``GetResponse[JMAPObject]`` is exactly the shape an advertised-but-unmodelled
    capability produces, so without a pydantic schema the fallback would fail at
    the one moment it exists for.
    """

    def test_a_mapping_is_wrapped(self):
        response = GetResponse[JMAPObject].model_validate(
            {"list": [{"id": "v1", "someProp": 1, "header:X": "raw"}]}
        )
        item = response.items[0]
        assert isinstance(item, JMAPObject)
        assert item.some_prop == 1
        assert item["header:X"] == "raw"

    def test_an_existing_instance_passes_through_unchanged(self):
        existing = JMAPObject({"id": "v1"})
        response = GetResponse[JMAPObject].model_validate({"list": [existing]})
        assert response.items[0] is existing

    @pytest.mark.parametrize("value", ["text", 5, None, [1, 2]])
    def test_a_non_object_is_rejected(self, value):
        with pytest.raises(ValidationError):
            GetResponse[JMAPObject].model_validate({"list": [value]})

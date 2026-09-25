"""The forgiving base of every JSContact and JSCalendar object."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import Field, ValidationError

from jmap.models.jsobject import JSObject, type_tag, wire_property


class Part(JSObject):
    value: str | None = None


class Whole(JSObject):
    member_uids: dict[str, bool] | None = Field(default=None, alias="members")
    part: Part | None = None
    count: int | None = None


class Stamped(JSObject):
    REQUIRED_TYPE: ClassVar[str] = "Stamped"


class TestSettingAside:
    def test_a_misfit_is_kept_under_its_wire_name(self):
        whole = Whole.from_wire({"members": ["u1"], "count": 2})
        assert whole.member_uids is None
        assert whole.count == 2
        assert whole.model_extra == {"members": ["u1"]}
        assert whole.to_wire() == {"members": ["u1"], "count": 2}

    def test_a_nested_misfit_costs_only_the_nested_property(self):
        whole = Whole.from_wire({"part": {"value": 7}})
        assert whole.part is not None
        assert whole.part.model_extra == {"value": 7}
        assert whole.to_wire() == {"part": {"value": 7}}

    def test_a_non_object_is_the_parents_to_set_aside(self):
        whole = Whole.from_wire({"part": ["not", "an", "object"]})
        assert whole.part is None
        assert whole.model_extra == {"part": ["not", "an", "object"]}

    @pytest.mark.parametrize(
        "value",
        [
            {"nested": [1, 2.5, True, None, "s"]},
            [{"a": {"b": []}}],
        ],
    )
    def test_any_plain_json_can_be_set_aside(self, value):
        assert Whole.from_wire({"count": value}).model_extra == {"count": value}


class TestWhatStillRaises:
    def test_a_python_name_that_is_no_wire_name(self):
        with pytest.raises(ValidationError, match="member_uids"):
            Whole(member_uids=["u1"])

    def test_every_error_when_any_part_cannot_be_set_aside(self):
        with pytest.raises(ValidationError) as excinfo:
            Whole(member_uids=["u1"], count="many")
        assert {problem["loc"][0] for problem in excinfo.value.errors()} == {
            "member_uids",
            "count",
        }

    @pytest.mark.parametrize(
        "value",
        [Part(value="x"), [Part()], {"k": Part()}, {1: "non-string key"}, object()],
    )
    def test_anything_that_could_not_have_come_off_the_wire(self, value):
        with pytest.raises(ValidationError):
            Whole(count=value)

    def test_a_value_that_is_not_an_object_at_all(self):
        with pytest.raises(ValidationError):
            Whole.model_validate(["not", "an", "object"])


class TestRequiredType:
    def test_it_is_filled_in_when_built_in_python(self):
        assert Stamped().to_wire() == {"@type": "Stamped"}

    def test_one_already_given_is_kept(self):
        assert Stamped.from_wire({"@type": "Other"}).at_type == "Other"

    def test_other_objects_invent_none(self):
        assert Part().to_wire() == {}


class TestReadingByWireName:
    whole = Whole.from_wire({"members": {"u1": True}, "count": "two", "x-vendor": 1})

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("members", {"u1": True}),
            ("count", "two"),
            ("x-vendor", 1),
            ("part", None),
            ("member_uids", None),
            ("nothing", None),
        ],
    )
    def test_each_property_answers_as_the_wire_has_it(self, name: str, expected: Any) -> None:
        assert wire_property(self.whole, name) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [({"@type": "Card"}, "Card"), ({}, None), ("Card", None), (None, None)],
)
def test_type_tag_reads_only_a_wire_object(value: Any, expected: Any) -> None:
    assert type_tag(value) == expected

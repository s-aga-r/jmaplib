"""Checking callers' arguments against the annotations that describe them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from jmap.core.ids import CreationRef
from jmap.core.ijson import MAX_SAFE_INT, MIN_SAFE_INT
from jmap.core.invocation import ResultRef
from jmap.models.arguments import Int, UnsignedInt, UTCDate, checked
from jmap.models.base import UNSET, Unset

REF: ResultRef[Any] = ResultRef("c0", "Email/query", "/ids")


@checked
def plain(name: str, *, count: UnsignedInt = 0, when: UTCDate | None = None) -> str:
    return f"{name}:{count}:{when}"


@checked
def optional(value: str | Unset = UNSET) -> object:
    # `str | Unset` evaluates to a types.UnionType, not a typing.Union.
    return value


@checked
def referable(value: Sequence[str] | ResultRef[Any] | None) -> object:
    return value


@checked
def by_id(value: str | CreationRef) -> object:
    return value


@checked
def signed(value: Int) -> int:
    return value


class Shared:
    """A method one class implements for many subjects, as the builders are."""

    def __init__(self, subject: str) -> None:
        self.subject = subject

    @checked(subject=lambda self: self.subject)
    def act(self, *, count: int) -> int:
        return count


def problems(excinfo: pytest.ExceptionInfo[ValidationError]) -> list[tuple[Any, ...]]:
    return [(error["loc"], error["type"]) for error in excinfo.value.errors()]


class TestValidation:
    def test_valid_arguments_reach_the_function(self):
        assert plain("a", count=2, when="2026-09-24T10:00:00Z") == "a:2:2026-09-24T10:00:00Z"
        # And a second time, once the validators exist.
        assert plain("b") == "b:0:None"

    def test_strict_nothing_is_coerced(self):
        with pytest.raises(ValidationError) as excinfo:
            plain(5, count="2")  # type: ignore[arg-type]
        assert problems(excinfo) == [(("name",), "string_type"), (("count",), "int_type")]

    def test_a_bool_is_not_a_count(self):
        with pytest.raises(ValidationError):
            plain("a", count=True)

    def test_the_error_is_titled_with_the_function(self):
        with pytest.raises(ValidationError, match="1 validation error for plain"):
            plain(5)  # type: ignore[arg-type]

    def test_a_shared_method_is_titled_with_its_subject(self):
        with pytest.raises(ValidationError, match=r"validation error for Mailbox\.act"):
            Shared("Mailbox").act(count="1")  # type: ignore[arg-type]
        assert Shared("Mailbox").act(count=1) == 1

    def test_a_validation_error_is_a_value_error(self):
        with pytest.raises(ValueError, match="string_type"):
            plain(5)  # type: ignore[arg-type]


class TestCallingWrongly:
    """Python's own TypeError, named after what was being called."""

    def test_a_missing_argument(self):
        with pytest.raises(TypeError, match=r"^plain\(\): missing a required argument: 'name'"):
            plain()  # type: ignore[call-arg]

    def test_a_surplus_positional(self):
        with pytest.raises(TypeError, match=r"^plain\(\): too many positional arguments"):
            plain("a", 1)  # type: ignore[call-arg]

    def test_an_unknown_keyword(self):
        with pytest.raises(TypeError, match="unexpected keyword argument 'colour'"):
            plain("a", colour="red")  # type: ignore[call-arg]

    def test_a_shared_method_names_its_subject(self):
        with pytest.raises(TypeError, match=r"^Email\.act\(\): "):
            Shared("Email").act()  # type: ignore[call-arg]

    def test_without_a_subject_it_names_the_method(self):
        # Called on the class there is no instance to ask, and the TypeError
        # must not become an IndexError on the way out.
        with pytest.raises(TypeError, match=r"^Shared\.act\(\): missing a required argument"):
            Shared.act(count=1)  # type: ignore[call-arg]


class TestForwarding:
    """UNSET, a ResultRef and a CreationRef go through untouched wherever the
    annotation names them, and only there."""

    def test_unset_goes_through(self):
        assert optional(UNSET) is UNSET

    def test_a_forwarded_type_is_left_out_of_the_error(self):
        # One line saying what was wrong, not one per member of the union.
        with pytest.raises(ValidationError) as excinfo:
            optional(5)  # type: ignore[arg-type]
        assert problems(excinfo) == [(("value",), "string_type")]

    def test_a_reference_goes_through(self):
        assert referable(REF) is REF

    def test_the_rest_of_the_union_is_still_checked(self):
        assert referable(None) is None
        with pytest.raises(ValidationError) as excinfo:
            referable("abc")
        assert problems(excinfo) == [(("value",), "sequence_str")]

    def test_a_creation_ref_goes_through(self):
        ref = CreationRef("k")
        assert by_id(ref) is ref

    def test_a_type_the_annotation_does_not_name_is_checked(self):
        with pytest.raises(ValidationError):
            by_id(REF)  # type: ignore[arg-type]


class TestTypes:
    @pytest.mark.parametrize("value", [MIN_SAFE_INT, -1, 0, MAX_SAFE_INT])
    def test_an_int_is_any_integer_json_carries_exactly(self, value):
        assert signed(value) == value

    @pytest.mark.parametrize("value", [MIN_SAFE_INT - 1, MAX_SAFE_INT + 1])
    def test_past_that_it_is_not(self, value):
        with pytest.raises(ValidationError):
            signed(value)

    def test_an_unsigned_int_is_not_negative(self):
        with pytest.raises(ValidationError) as excinfo:
            plain("a", count=-1)
        assert problems(excinfo) == [(("count",), "greater_than_equal")]

    @pytest.mark.parametrize(
        "when", ["2026-09-24T10:00:00+00:00", "2026-09-24T10:00:00", "2026-09-24", "soon"]
    )
    def test_a_utc_date_ends_in_z(self, when):
        with pytest.raises(ValidationError, match="invalid date"):
            plain("a", when=when)

    def test_a_utc_date_is_sent_as_spelled(self):
        assert plain("a", when="2026-09-24T10:00:00.5Z").endswith("2026-09-24T10:00:00.5Z")

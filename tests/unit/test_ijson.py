"""I-JSON constraints (RFC 7493) and the JMAP date types (RFC 8620 §1.1, §1.3, §1.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jmap.core.ijson import (
    MAX_SAFE_INT,
    MIN_SAFE_INT,
    DuplicateKeyError,
    IJSONError,
    IntegerRangeError,
    InvalidDateError,
    InvalidStringError,
    check_int,
    check_string,
    dumps,
    format_local_date,
    format_utc_date,
    loads,
    parse_local_date,
    parse_utc_date,
)

PLUS_8 = timezone(timedelta(hours=8))


class TestErrorHierarchy:
    """One ``except IJSONError`` must catch everything this module rejects."""

    @pytest.mark.parametrize(
        "error",
        [
            IntegerRangeError(1),
            DuplicateKeyError("a"),
            InvalidStringError("bad"),
            InvalidDateError("x", "bad"),
        ],
    )
    def test_all_are_ijson_value_errors(self, error):
        assert isinstance(error, IJSONError)
        assert isinstance(error, ValueError)

    def test_range_error_carries_its_operands(self):
        error = IntegerRangeError(2**53, unsigned=True, field="/a")
        assert (error.value, error.unsigned, error.field) == (2**53, True, "/a")
        assert "UnsignedInt" in str(error)
        assert str(error).startswith("/a: ")

    def test_signed_range_error_names_the_signed_bound(self):
        assert f"[{MIN_SAFE_INT}, {MAX_SAFE_INT}]" in str(IntegerRangeError(-(2**53)))


class TestCheckInt:
    @pytest.mark.parametrize(
        ("value", "unsigned"),
        [
            (0, False),
            (0, True),
            (MAX_SAFE_INT, False),
            (MAX_SAFE_INT, True),
            (MIN_SAFE_INT, False),
            (-1, False),
        ],
    )
    def test_accepts_and_returns_value(self, value, unsigned):
        assert check_int(value, unsigned=unsigned) == value

    @pytest.mark.parametrize(
        ("value", "unsigned"),
        [
            (MAX_SAFE_INT + 1, False),
            (MAX_SAFE_INT + 1, True),
            (MIN_SAFE_INT - 1, False),
            (2**64, False),
            (-1, True),
            (MIN_SAFE_INT, True),
        ],
    )
    def test_rejects_out_of_range(self, value, unsigned):
        with pytest.raises(IntegerRangeError):
            check_int(value, unsigned=unsigned)

    def test_field_is_quoted_in_the_message(self):
        with pytest.raises(IntegerRangeError, match=r"/list/0/size: "):
            check_int(2**53, field="/list/0/size")

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_an_int(self, value):
        # bool is an int subclass and would pass the range check, but it
        # serialises as true/false and can never be a JSON number.
        with pytest.raises(TypeError, match="bool is not a JMAP Int"):
            check_int(value)


class TestCheckString:
    @pytest.mark.parametrize("value", ["", "plain", "é", "😀", "\U0010ffff", "a b"])
    def test_accepts_valid_unicode(self, value):
        assert check_string(value) == value

    @pytest.mark.parametrize(
        "value",
        # ruff PT014 mis-reads lone-surrogate escapes as duplicates; all four differ.
        ["\ud800", "\udfff", "ok\ud83d", "\ude00trailing"],  # noqa: PT014
    )
    def test_rejects_unpaired_surrogates(self, value):
        with pytest.raises(InvalidStringError, match="unpaired surrogate"):
            check_string(value)

    def test_message_locates_the_code_point(self):
        with pytest.raises(InvalidStringError, match=r"U\+D800 at index 2"):
            check_string("ab\ud800", field="/name")


class TestLoadsDuplicateKeys:
    """RFC 7493 §2.3. ``json.loads`` keeps the last value and says nothing."""

    def test_duplicate_at_top_level_names_the_key(self):
        with pytest.raises(DuplicateKeyError, match="'accountId'") as excinfo:
            loads('{"accountId": "a", "accountId": "b"}')
        assert excinfo.value.key == "accountId"

    def test_duplicate_nested_in_an_array(self):
        with pytest.raises(DuplicateKeyError, match="'id'"):
            loads('{"list": [{"id": "a", "id": "b"}]}')

    def test_stdlib_would_have_swallowed_it(self):
        # Pinning the behaviour this module exists to prevent.
        assert json.loads('{"a": 1, "a": 2}') == {"a": 2}

    def test_same_key_in_sibling_objects_is_fine(self):
        assert loads('[{"id": "a"}, {"id": "b"}]') == [{"id": "a"}, {"id": "b"}]

    def test_repeated_key_after_a_nested_object(self):
        with pytest.raises(DuplicateKeyError, match="'a'"):
            loads('{"a": {"b": 1}, "a": 2}')


class TestLoadsIntegers:
    def test_accepts_the_boundary(self):
        assert loads(f'{{"size": {MAX_SAFE_INT}}}') == {"size": MAX_SAFE_INT}

    @pytest.mark.parametrize("literal", ["9007199254740992", "-9007199254740992", "1" * 30])
    def test_rejects_beyond_the_boundary(self, literal):
        with pytest.raises(IntegerRangeError):
            loads(f'{{"size": {literal}}}')

    def test_error_names_a_json_pointer_to_the_member(self):
        with pytest.raises(IntegerRangeError) as excinfo:
            loads('{"a": {"b": [1, 9007199254740992]}}')
        assert excinfo.value.field == "/a/b/1"

    def test_pointer_escapes_slashes_in_keys(self):
        with pytest.raises(IntegerRangeError) as excinfo:
            loads('{"a/b": 9007199254740992}')
        assert excinfo.value.field == "/a~1b"

    def test_top_level_integer_has_no_field(self):
        with pytest.raises(IntegerRangeError) as excinfo:
            loads("9007199254740992")
        assert excinfo.value.field is None

    def test_booleans_are_not_range_checked(self):
        assert loads('{"isUnread": true, "isSeen": false}') == {"isUnread": True, "isSeen": False}

    def test_floats_pass_through(self):
        # Only the Int types are bounded; a float carries its own IEEE-754 limits.
        assert loads('{"weight": 1e308}') == {"weight": 1e308}


class TestLoadsStrings:
    def test_surrogate_pair_is_decoded_to_one_character(self):
        # The stdlib combines a *paired* escape, so this is valid I-JSON and must
        # survive the surrogate check that rejects a lone half.
        assert loads(r'"\uD83D\uDE00"') == "\U0001f600"

    def test_unpaired_surrogate_escape_is_rejected(self):
        with pytest.raises(InvalidStringError, match="unpaired surrogate"):
            loads(r'{"name": "\ud800"}')

    def test_unpaired_surrogate_in_a_key_is_rejected(self):
        with pytest.raises(InvalidStringError) as excinfo:
            loads(r'{"\udc00": 1}')
        assert excinfo.value.field == "/\udc00"

    def test_null_and_nested_containers_survive(self):
        assert loads('{"a": null, "b": [[]], "c": {}}') == {"a": None, "b": [[]], "c": {}}


class TestLoadsEncoding:
    def test_accepts_utf8_bytes(self):
        assert loads('{"subject": "héllo"}'.encode()) == {"subject": "héllo"}

    def test_rejects_invalid_utf8(self):
        with pytest.raises(InvalidStringError, match="not valid UTF-8"):
            loads(b'{"a": "\xff"}')

    def test_rejects_utf16_which_detect_encoding_would_have_accepted(self):
        # json.loads sniffs UTF-16/32 from a byte string; RFC 7493 §2.1 is UTF-8 only.
        with pytest.raises(InvalidStringError, match="not valid UTF-8"):
            loads('{"a": 1}'.encode("utf-16"))

    def test_malformed_json_still_raises_the_stdlib_error(self):
        with pytest.raises(json.JSONDecodeError):
            loads("{oops")


class TestDumps:
    def test_is_compact(self):
        assert dumps({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'

    @pytest.mark.parametrize("value", ["é", "😀", "日本語"])
    def test_non_ascii_is_not_escaped(self, value):
        assert dumps(value) == f'"{value}"'

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_nan_and_infinity(self, value):
        # NaN/Infinity are Python extensions; no conforming parser accepts them.
        with pytest.raises(ValueError, match="not JSON compliant"):
            dumps(value)

    def test_rejects_out_of_range_int(self):
        with pytest.raises(IntegerRangeError) as excinfo:
            dumps({"filter": {"before": 2**53}})
        assert excinfo.value.field == "/filter/before"

    def test_rejects_unpaired_surrogate_before_it_reaches_the_socket(self):
        # With ensure_ascii=False this would otherwise fail at UTF-8 encode time,
        # far away from the field that caused it.
        with pytest.raises(InvalidStringError, match="unpaired surrogate"):
            dumps({"subject": "\ud800"})

    @pytest.mark.parametrize("key", [1, None, True, (1, 2)])
    def test_rejects_non_string_object_keys(self, key):
        with pytest.raises(InvalidStringError, match="not a string"):
            dumps({key: "x"})

    def test_booleans_and_null_serialise_normally(self):
        assert dumps({"a": True, "b": False, "c": None}) == '{"a":true,"b":false,"c":null}'

    def test_tuples_are_validated_like_arrays(self):
        assert dumps({"ids": ("a", "b")}) == '{"ids":["a","b"]}'
        with pytest.raises(IntegerRangeError) as excinfo:
            dumps({"ids": (1, 2**53)})
        assert excinfo.value.field == "/ids/1"

    def test_roundtrips_a_realistic_request(self):
        request = {
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [["Email/query", {"accountId": "u1", "limit": 50}, "c0"]],
        }
        assert loads(dumps(request)) == request


class TestParseUTCDate:
    def test_rfc8620_example(self):
        assert parse_utc_date("2014-10-30T06:12:00Z") == datetime(2014, 10, 30, 6, 12, tzinfo=UTC)

    def test_result_is_aware(self):
        assert parse_utc_date("2014-10-30T06:12:00Z").utcoffset() == timedelta(0)

    def test_equals_the_same_instant_in_another_offset(self):
        # RFC 8620 §1.4 gives both forms; only the Z form is a UTCDate.
        assert parse_utc_date("2014-10-30T06:12:00Z") == datetime(
            2014, 10, 30, 14, 12, tzinfo=PLUS_8
        )

    @pytest.mark.parametrize(
        ("value", "microsecond"),
        [
            ("2014-10-30T06:12:00.5Z", 500000),
            ("2014-10-30T06:12:00.123Z", 123000),
            ("2014-10-30T06:12:00.123456Z", 123456),
            ("2014-10-30T06:12:00.000Z", 0),
            # Sub-microsecond precision is truncated, not rejected: nanoseconds
            # are legal RFC 3339 but unrepresentable as a datetime.
            ("2014-10-30T06:12:00.123456789Z", 123456),
        ],
    )
    def test_fractional_seconds(self, value, microsecond):
        assert parse_utc_date(value).microsecond == microsecond

    def test_rejects_zero_offset_spelled_out(self):
        # Same instant, but round-tripping "+00:00" back to a server is exactly
        # what RFC 8620 §1.4 forbids.
        with pytest.raises(InvalidDateError, match="must end with 'Z'"):
            parse_utc_date("2014-10-30T06:12:00+00:00")

    def test_rejects_a_non_utc_offset(self):
        with pytest.raises(InvalidDateError, match="convert the offset to UTC first"):
            parse_utc_date("2014-10-30T14:12:00+08:00")

    def test_rejects_a_negative_offset(self):
        with pytest.raises(InvalidDateError, match="convert the offset to UTC first"):
            parse_utc_date("2014-10-29T22:12:00-08:00")

    def test_rejects_a_missing_offset(self):
        with pytest.raises(InvalidDateError, match="missing time offset"):
            parse_utc_date("2014-10-30T06:12:00")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "2014-10-30",
            "2014-10-30t06:12:00Z",  # RFC 8620 §1.4 requires uppercase letters
            "2014-10-30T06:12:00z",
            "2014-10-30T06:12Z",  # seconds are mandatory in RFC 3339
            "2014-10-30T06:12:00.Z",
            "14-10-30T06:12:00Z",
            "2014-10-30T06:12:00Z ",
            " 2014-10-30T06:12:00Z",
            "2014-10-30T06:12:00ZZ",
        ],
    )
    def test_rejects_malformed(self, value):
        with pytest.raises(InvalidDateError, match="not an RFC 3339 date-time"):
            parse_utc_date(value)

    @pytest.mark.parametrize(
        "value",
        [
            "2014-13-30T06:12:00Z",
            "2014-02-30T06:12:00Z",
            "2014-10-30T25:12:00Z",
            "2014-10-30T06:60:00Z",
            "0000-01-01T00:00:00Z",
            # RFC 3339 allows a leap second; datetime cannot represent one.
            "2016-12-31T23:59:60Z",
        ],
    )
    def test_rejects_well_formed_but_unrepresentable(self, value):
        with pytest.raises(InvalidDateError) as excinfo:
            parse_utc_date(value)
        assert excinfo.value.value == value


class TestFormatUTCDate:
    def test_rfc8620_example(self):
        assert format_utc_date(datetime(2014, 10, 30, 6, 12, tzinfo=UTC)) == "2014-10-30T06:12:00Z"

    def test_converts_a_non_utc_offset(self):
        assert (
            format_utc_date(datetime(2014, 10, 30, 14, 12, tzinfo=PLUS_8)) == "2014-10-30T06:12:00Z"
        )

    def test_omits_a_zero_time_secfrac(self):
        # RFC 8620 §1.4 requires the normalised form.
        assert "." not in format_utc_date(datetime(2014, 10, 30, 6, 12, 0, 0, tzinfo=UTC))

    def test_emits_six_digits_when_non_zero(self):
        moment = datetime(2014, 10, 30, 6, 12, 0, 123456, tzinfo=UTC)
        assert format_utc_date(moment) == "2014-10-30T06:12:00.123456Z"

    def test_pads_a_sub_millisecond_fraction(self):
        moment = datetime(2014, 10, 30, 6, 12, 0, 7, tzinfo=UTC)
        assert format_utc_date(moment) == "2014-10-30T06:12:00.000007Z"

    def test_pads_a_year_below_1000(self):
        # strftime('%Y') is platform-dependent here; RFC 3339 wants four digits.
        assert format_utc_date(datetime(1, 2, 3, 4, 5, 6, tzinfo=UTC)) == "0001-02-03T04:05:06Z"

    def test_rejects_a_naive_datetime(self):
        with pytest.raises(InvalidDateError, match="naive datetime"):
            format_utc_date(datetime(2014, 10, 30, 6, 12))

    @pytest.mark.parametrize(
        "value",
        [
            "2014-10-30T06:12:00Z",
            "2014-10-30T06:12:00.123456Z",
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59.999999Z",
        ],
    )
    def test_roundtrip(self, value):
        assert format_utc_date(parse_utc_date(value)) == value


class TestLocalDate:
    """RFC 8984 §1.4.4: a wall-clock reading, deliberately not an instant."""

    def test_parses_to_a_naive_datetime(self):
        parsed = parse_local_date("2014-10-30T14:12:00")
        assert parsed == datetime(2014, 10, 30, 14, 12)
        assert parsed.tzinfo is None

    def test_parses_fractional_seconds(self):
        assert parse_local_date("2014-10-30T14:12:00.25").microsecond == 250000

    @pytest.mark.parametrize(
        "value",
        ["2014-10-30T14:12:00Z", "2014-10-30T14:12:00+08:00", "2014-10-30T14:12:00-05:00"],
    )
    def test_rejects_any_offset(self, value):
        with pytest.raises(InvalidDateError, match="must not carry a time zone or offset"):
            parse_local_date(value)

    @pytest.mark.parametrize("value", ["", "2014-10-30", "2014-10-30 14:12:00", "nonsense"])
    def test_rejects_malformed(self, value):
        with pytest.raises(InvalidDateError, match="not a date-time"):
            parse_local_date(value)

    def test_rejects_unrepresentable(self):
        with pytest.raises(InvalidDateError):
            parse_local_date("2014-10-32T14:12:00")

    def test_formats_a_naive_datetime(self):
        assert format_local_date(datetime(2014, 10, 30, 14, 12)) == "2014-10-30T14:12:00"

    def test_rejects_an_aware_datetime(self):
        # Dropping the offset silently would turn an instant into somebody's
        # local wall clock, which is the bug the naive/aware split prevents.
        with pytest.raises(InvalidDateError, match="is an instant, not a LocalDate"):
            format_local_date(datetime(2014, 10, 30, 14, 12, tzinfo=PLUS_8))

    @pytest.mark.parametrize(
        "value", ["2014-10-30T14:12:00", "2014-10-30T14:12:00.500000", "0001-01-01T00:00:00"]
    )
    def test_roundtrip(self, value):
        assert format_local_date(parse_local_date(value)) == value

    def test_a_local_date_is_never_equal_to_a_utc_date(self):
        # Comparing naive to aware raises rather than lying, which is the point.
        with pytest.raises(TypeError):
            _ = parse_local_date("2014-10-30T06:12:00") < parse_utc_date("2014-10-30T06:12:00Z")

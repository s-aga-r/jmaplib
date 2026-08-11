"""Id charset and creation-reference handling (RFC 8620 §1.2, §5.3)."""

from __future__ import annotations

import pytest

from jmap.core.ids import (
    MAX_ID_OCTETS,
    CreationRef,
    InvalidIdError,
    is_valid_id,
    parse_id,
)


class TestValidIds:
    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "M1",
            "abc123",
            "with-hyphen",
            "with_underscore",
            "A" * MAX_ID_OCTETS,
            "0",
            # Stalwart serialises ids as Crockford base32, e.g. account id "a".
            "aBcDeF-_09",
        ],
    )
    def test_accepted(self, value):
        assert is_valid_id(value)
        assert parse_id(value) == value


class TestInvalidIds:
    def test_empty(self):
        with pytest.raises(InvalidIdError, match="at least 1 octet"):
            parse_id("")

    def test_too_long(self):
        with pytest.raises(InvalidIdError, match="exceeds 255 octets"):
            parse_id("A" * (MAX_ID_OCTETS + 1))

    def test_creation_prefix_is_reserved(self):
        # The common real bug: a creation reference leaking into an id slot.
        with pytest.raises(InvalidIdError, match="reserved for creation references"):
            parse_id("#draft")

    @pytest.mark.parametrize(
        "value",
        ["has space", "has/slash", "has.dot", "has+plus", "has=pad", "héllo", "a\nb"],
    )
    def test_charset_violations(self, value):
        assert not is_valid_id(value)
        with pytest.raises(InvalidIdError):
            parse_id(value)

    def test_multibyte_counts_octets_not_characters(self):
        # 128 two-octet characters is 256 octets. It fails the charset check first,
        # but the octet accounting must not be character-based either.
        assert not is_valid_id("é" * 128)

    def test_error_carries_value_and_reason(self):
        with pytest.raises(InvalidIdError) as excinfo:
            parse_id("#x")
        assert excinfo.value.value == "#x"
        assert "reserved" in excinfo.value.reason


class TestCreationRef:
    def test_serialises_with_hash_prefix(self):
        assert str(CreationRef("draft")) == "#draft"

    def test_validates_the_creation_id(self):
        # The server echoes creation ids back in `createdIds`, where the Id
        # charset applies, so an invalid one fails here rather than there.
        with pytest.raises(InvalidIdError):
            CreationRef("not valid")

    def test_rejects_double_prefixing(self):
        with pytest.raises(InvalidIdError, match="reserved"):
            CreationRef("#draft")

    def test_equality_and_hashing(self):
        assert CreationRef("a") == CreationRef("a")
        assert CreationRef("a") != CreationRef("b")
        assert CreationRef("a") != "a"
        assert len({CreationRef("a"), CreationRef("a"), CreationRef("b")}) == 2

    def test_repr(self):
        assert repr(CreationRef("draft")) == "CreationRef('draft')"

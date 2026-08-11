"""The ``map_properties`` escape hatch for all-digit ids.

The array-index rule (RFC 8620 §5.3) has to be enforced from the shape of a token
alone, because the target document is not in hand. That misfires on a map keyed by
an id: ``mailboxIds/123`` is indistinguishable from an array index. RFC 8620 §1.2
asks servers to avoid all-digit ids, but only as a SHOULD and for unrelated
reasons, so this has to be survivable.
"""

from __future__ import annotations

import pytest

from jmap.core.ids import Id
from jmap.core.patch import (
    JSCALENDAR_20,
    InvalidPatchError,
    PatchBuilder,
    keyword_patch,
    mailbox_patch,
    validate_patch,
)


class TestGuardStaysOnByDefault:
    @pytest.mark.parametrize("key", ["mailboxIds/123", "links/0", "alerts/-"])
    def test_digit_tokens_are_rejected_without_opt_in(self, key):
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            PatchBuilder().set(key, True)

    def test_the_error_points_at_the_way_out(self):
        # A guard the caller cannot discover how to satisfy is a bad guard.
        with pytest.raises(InvalidPatchError) as excinfo:
            PatchBuilder().set("mailboxIds/123", True)
        assert "map_properties" in str(excinfo.value)


class TestOptIn:
    def test_named_parent_allows_a_digit_key(self):
        builder = PatchBuilder(map_properties={"mailboxIds"})
        assert builder.set("mailboxIds/123", True).build() == {"mailboxIds/123": True}

    def test_unnamed_parents_are_still_guarded(self):
        builder = PatchBuilder(map_properties={"mailboxIds"})
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            builder.set("links/0/href", "x")

    def test_only_the_immediate_parent_counts(self):
        # "keywords" is declared, but the digit here sits under "other".
        builder = PatchBuilder(map_properties={"keywords"})
        with pytest.raises(InvalidPatchError):
            builder.set("keywords/other/0", True)

    def test_validate_patch_accepts_the_same_option(self):
        validate_patch({"mailboxIds/123": True}, map_properties={"mailboxIds"})
        with pytest.raises(InvalidPatchError):
            validate_patch({"mailboxIds/123": True})

    def test_a_leading_digit_token_has_no_parent_to_exempt(self):
        with pytest.raises(InvalidPatchError):
            PatchBuilder(map_properties={"mailboxIds"}).set("0", True)

    def test_jscalendar_20_still_allows_arrays_outright(self):
        assert JSCALENDAR_20.allow_array_index
        assert PatchBuilder(JSCALENDAR_20).set("links/0/href", "x").build() == {"links/0/href": "x"}


class TestHelpersNeedNoOptIn:
    """The ergonomic path must work for any id the server hands out."""

    @pytest.mark.parametrize("mailbox_id", ["abc", "123", "0", "9-_"])
    def test_mailbox_patch_survives_numeric_ids(self, mailbox_id):
        assert mailbox_patch(add=[Id(mailbox_id)]) == {f"mailboxIds/{mailbox_id}": True}

    def test_mailbox_patch_removal(self):
        assert mailbox_patch(remove=[Id("123")]) == {"mailboxIds/123": None}

    def test_keyword_patch_is_unaffected(self):
        assert keyword_patch(add=["$seen"]) == {"keywords/$seen": True}

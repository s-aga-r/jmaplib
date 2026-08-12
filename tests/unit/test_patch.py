"""PatchObject rules for the three dialects (RFC 8620 §5.3, RFC 8984 §3.3)."""

from __future__ import annotations

import dataclasses

import pytest

from jmap.core.ids import Id, InvalidIdError
from jmap.core.patch import (
    IANA_KEYWORDS,
    INVALID_PATCH,
    JMAP,
    JSCALENDAR_10,
    JSCALENDAR_20,
    DialectRules,
    InvalidKeywordError,
    InvalidPatchError,
    PatchBuilder,
    is_valid_keyword,
    keyword_patch,
    mailbox_patch,
    parse_keyword,
    patch_key,
    validate_patch,
)

#: A dialect no spec defines, used to exercise the flags independently.
PERMISSIVE = DialectRules("Permissive", allow_array_index=True, allow_whole_object=True)


class TestDialects:
    @pytest.mark.parametrize(
        ("dialect", "name", "arrays"),
        [
            (JMAP, "JMAP", False),
            (JSCALENDAR_10, "JSCalendar 1.0", False),
            (JSCALENDAR_20, "JSCalendar 2.0", True),
        ],
    )
    def test_shipped_dialects(self, dialect, name, arrays):
        assert dialect.name == name
        assert dialect.allow_array_index is arrays
        # No spec permits replacing the whole object from inside an update.
        assert dialect.allow_whole_object is False

    def test_dialects_are_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            JMAP.allow_array_index = True  # type: ignore[misc]

    def test_dialects_compare_by_value(self):
        assert DialectRules("JMAP") == JMAP
        # Same rules, different spec: still distinguishable.
        assert JMAP != JSCALENDAR_10

    def test_invalid_patch_is_the_set_error_type(self):
        assert INVALID_PATCH == "invalidPatch"


class TestValidatePatch:
    @pytest.mark.parametrize(
        "patch",
        [
            {},
            {"keywords/$seen": True},
            {"mailboxIds/MB23cfa8094c0f41e6": True, "mailboxIds/MB674cc24095db49ce": None},
            # Siblings that share a prefix but not a pointer.
            {"a/b": 1, "a/c": 2},
            # "a/bc" is not under "a/b" - only whole tokens count.
            {"a/b": 1, "a/bc": 2},
            {"a/b~1c": 1, "a/b": 2},
            # An escaped token may itself contain a slash.
            {"keywords/a~1b": True},
            # Leading zeros are not RFC 6901 array indices, so this is a property.
            {"stuff/01": 1},
            # A trailing slash addresses the "" property, which is legal JSON.
            {"a/": 1},
        ],
    )
    def test_accepts_wellformed_patches(self, patch):
        validate_patch(patch)  # must not raise

    def test_rejects_leading_slash(self):
        # The server prepends one, so this would address the "" property.
        with pytest.raises(InvalidPatchError, match="leading '/' is implicit") as excinfo:
            validate_patch({"/mailboxIds/abc": True})
        assert excinfo.value.keys == ("/mailboxIds/abc",)

    def test_rejects_whole_object_replacement(self):
        with pytest.raises(InvalidPatchError, match="no whole-object replacement"):
            validate_patch({"": {"subject": "hi"}})

    def test_whole_object_allowed_only_by_an_opt_in_dialect(self):
        validate_patch({"": {"subject": "hi"}}, PERMISSIVE)  # must not raise

    def test_whole_object_is_a_prefix_of_every_other_key(self):
        with pytest.raises(InvalidPatchError, match="prefix") as excinfo:
            validate_patch({"": {}, "a": 1}, PERMISSIVE)
        assert excinfo.value.keys == ("", "a")

    @pytest.mark.parametrize("dialect", [JMAP, JSCALENDAR_10])
    @pytest.mark.parametrize("token", ["0", "1", "10", "-"])
    def test_array_pointers_rejected_by_jmap_and_jscalendar_10(self, dialect, token):
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            validate_patch({f"alerts/{token}/offset": "PT0S"}, dialect)

    @pytest.mark.parametrize("token", ["0", "1", "10", "-"])
    def test_array_pointers_accepted_by_jscalendar_20(self, token):
        validate_patch({f"links/{token}/href": "x"}, JSCALENDAR_20)  # must not raise

    def test_array_rule_applies_to_the_final_token_too(self):
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            validate_patch({"alerts/1": None})

    @pytest.mark.parametrize(
        ("patch", "expected"),
        [
            ({"a/b": 1, "a/b/c": 2}, ("a/b", "a/b/c")),
            ({"a/b/c": 1, "a/b": 2}, ("a/b", "a/b/c")),
            ({"a": 1, "a/b/c/d": 2}, ("a", "a/b/c/d")),
        ],
    )
    def test_rejects_prefix_overlap(self, patch, expected):
        with pytest.raises(InvalidPatchError, match="prefix") as excinfo:
            validate_patch(patch)
        assert sorted(excinfo.value.keys) == sorted(expected)

    def test_rfc8620_alerts_example_fails_on_the_array_rule_first(self):
        # RFC 8620 §5.3 gives "alerts/1/offset" and "alerts" as the illegal pair;
        # under JMAP it is doubly illegal, the index being reason enough.
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            validate_patch({"alerts/1/offset": "PT0S", "alerts": None})

    def test_rfc8620_alerts_example_is_still_an_overlap_where_indexing_is_legal(self):
        with pytest.raises(InvalidPatchError, match="prefix") as excinfo:
            validate_patch({"alerts/1/offset": "PT0S", "alerts": None}, JSCALENDAR_20)
        assert excinfo.value.keys == ("alerts", "alerts/1/offset")

    def test_rejects_two_keys_that_decode_to_the_same_pointer(self):
        # "~" and "~0" both unescape to the single token "~".
        with pytest.raises(InvalidPatchError, match="address the same property") as excinfo:
            validate_patch({"~": 1, "~0": 2})
        assert excinfo.value.keys == ("~", "~0")

    def test_parent_existence_is_not_checked(self):
        # The client cannot know whether "locations" exists; the server decides.
        validate_patch({"locations/x/name": "Home"})  # must not raise

    def test_error_message_names_every_offending_key(self):
        with pytest.raises(InvalidPatchError) as excinfo:
            validate_patch({"a/b": 1, "a/b/c": 2})
        assert "'a/b', 'a/b/c'" in str(excinfo.value)

    def test_error_is_a_valueerror(self):
        assert issubclass(InvalidPatchError, ValueError)


class TestPatchKey:
    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (("mailboxIds", "abc"), "mailboxIds/abc"),
            (("keywords", "a/b"), "keywords/a~1b"),
            (("keywords", "a~b"), "keywords/a~0b"),
            (("keywords", "~1"), "keywords/~01"),
            (("a",), "a"),
            ((), ""),
        ],
    )
    def test_builds_and_escapes(self, tokens, expected):
        assert patch_key(*tokens) == expected

    def test_never_emits_a_leading_slash(self):
        assert not patch_key("a", "b").startswith("/")


class TestPatchBuilder:
    def test_set_and_remove(self):
        patch = PatchBuilder().set("keywords/$seen", True).remove("mailboxIds/MB1").build()
        assert patch == {"keywords/$seen": True, "mailboxIds/MB1": None}

    def test_remove_is_exactly_set_to_none(self):
        assert PatchBuilder().set("a", None).build() == PatchBuilder().remove("a").build()

    def test_methods_chain(self):
        builder = PatchBuilder()
        assert builder.set("a", 1) is builder
        assert builder.remove("b") is builder
        assert builder.merge({"c": 1}) is builder

    def test_empty_builder_is_falsy_and_builds_an_empty_patch(self):
        builder = PatchBuilder()
        assert not builder
        assert len(builder) == 0
        assert builder.build() == {}

    def test_len_counts_edits_not_calls(self):
        builder = PatchBuilder().set("a", 1).set("a", 2)
        assert len(builder) == 1
        assert builder.build() == {"a": 2}

    def test_build_returns_a_copy(self):
        builder = PatchBuilder().set("a", 1)
        patch = builder.build()
        patch["b"] = 2
        assert builder.build() == {"a": 1}

    def test_validation_is_eager_on_set(self):
        with pytest.raises(InvalidPatchError, match="leading '/' is implicit"):
            PatchBuilder().set("/a", 1)

    def test_validation_is_eager_on_remove(self):
        with pytest.raises(InvalidPatchError, match="references inside an array"):
            PatchBuilder().remove("alerts/1")

    def test_overlap_is_caught_by_the_call_that_creates_it(self):
        builder = PatchBuilder().set("a/b", 1)
        with pytest.raises(InvalidPatchError, match="prefix"):
            builder.set("a/b/c", 2)
        # The rejected edit did not land.
        assert builder.build() == {"a/b": 1}

    def test_two_spellings_of_one_pointer_collide(self):
        # "~" and "~0" are different keys that decode to the same single token,
        # so the server would see one property addressed twice.
        builder = PatchBuilder().set("~", 1)
        with pytest.raises(InvalidPatchError, match="same property"):
            builder.set("~0", 2)
        assert builder.build() == {"~": 1}

    def test_overlap_is_caught_when_the_new_key_is_the_shorter_one(self):
        # The mirror of the case above it: here the arriving key sits *above* one
        # already held, which no amount of looking at the new key alone reveals.
        builder = PatchBuilder().set("a/b", 1)
        with pytest.raises(InvalidPatchError, match="prefix"):
            builder.set("a", 2)
        assert builder.build() == {"a/b": 1}

    def test_overlap_is_caught_across_many_keys(self):
        # Guards the bookkeeping that replaced the per-insert sweep: the conflict
        # is with the first key added, long after it stopped being the latest.
        builder = PatchBuilder().set("a/b", 1)
        for index in range(20):
            builder.set(f"x{index}", index)
        with pytest.raises(InvalidPatchError, match="prefix"):
            builder.set("a", 2)

    def test_merge_folds_in_a_helper_patch(self):
        patch = (
            PatchBuilder()
            .merge(keyword_patch(add=["$seen"]))
            .merge(mailbox_patch(add=[Id("MB1")], remove=[Id("MB2")]))
            .build()
        )
        assert patch == {"keywords/$seen": True, "mailboxIds/MB1": True, "mailboxIds/MB2": None}

    def test_merge_validates_the_incoming_keys(self):
        with pytest.raises(InvalidPatchError, match="prefix"):
            PatchBuilder().set("keywords", {}).merge({"keywords/$seen": True})

    def test_dialect_is_honoured(self):
        assert PatchBuilder(JSCALENDAR_20).set("links/0/href", "x").build() == {"links/0/href": "x"}
        with pytest.raises(InvalidPatchError, match=r"JSCalendar 1\.0"):
            PatchBuilder(JSCALENDAR_10).set("links/0/href", "x")

    def test_repr_shows_dialect_and_edits(self):
        assert repr(PatchBuilder().set("a", 1)) == "PatchBuilder('JMAP', {'a': 1})"


class TestParseKeyword:
    @pytest.mark.parametrize("keyword", sorted(IANA_KEYWORDS))
    def test_registered_keywords_round_trip(self, keyword):
        assert parse_keyword(keyword) == keyword

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("$Seen", "$seen"),
            ("$SEEN", "$seen"),
            ("Todo", "todo"),
            ("MixedCase", "mixedcase"),
        ],
    )
    def test_lowercases(self, value, expected):
        assert parse_keyword(value) == expected

    @pytest.mark.parametrize("keyword", ["todo", "a/b", "a~b", "x" * 255, "!", "[", "^", "|", "~"])
    def test_accepts_the_full_charset(self, keyword):
        assert parse_keyword(keyword) == keyword

    @pytest.mark.parametrize("char", ["(", ")", "{", "]", "%", "*", '"', "\\", " ", "\x7f", "é"])
    def test_rejects_characters_outside_the_charset(self, char):
        with pytest.raises(InvalidKeywordError, match="outside the keyword charset"):
            parse_keyword(f"a{char}b")

    def test_rejects_empty(self):
        with pytest.raises(InvalidKeywordError, match="at least 1 character"):
            parse_keyword("")

    def test_rejects_over_255_octets(self):
        with pytest.raises(InvalidKeywordError, match="exceeds 255 octets"):
            parse_keyword("x" * 256)

    @pytest.mark.parametrize("keyword", ["$custom", "$Unregistered", "$"])
    def test_rejects_unregistered_dollar_keywords(self, keyword):
        with pytest.raises(InvalidKeywordError, match="reserved for IANA"):
            parse_keyword(keyword)

    def test_dollar_is_allowed_away_from_the_start(self):
        assert parse_keyword("a$b") == "a$b"

    def test_error_carries_the_original_spelling(self):
        with pytest.raises(InvalidKeywordError) as excinfo:
            parse_keyword("$Custom")
        assert excinfo.value.keyword == "$Custom"
        assert issubclass(InvalidKeywordError, ValueError)

    @pytest.mark.parametrize(
        ("value", "valid"),
        [("$seen", True), ("todo", True), ("$custom", False), ("", False), ("a b", False)],
    )
    def test_is_valid_keyword(self, value, valid):
        assert is_valid_keyword(value) is valid


class TestMailboxPatch:
    def test_add_and_remove(self):
        assert mailbox_patch(add=[Id("MB1")], remove=[Id("MB2")]) == {
            "mailboxIds/MB1": True,
            "mailboxIds/MB2": None,
        }

    def test_defaults_to_an_empty_patch(self):
        assert mailbox_patch() == {}

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"add": [Id("a"), Id("b")]}, {"mailboxIds/a": True, "mailboxIds/b": True}),
            ({"remove": [Id("a")]}, {"mailboxIds/a": None}),
        ],
    )
    def test_one_sided_patches(self, kwargs, expected):
        assert mailbox_patch(**kwargs) == expected

    def test_rejects_a_mailbox_on_both_sides(self):
        with pytest.raises(InvalidPatchError, match="both added and removed") as excinfo:
            mailbox_patch(add=[Id("MB1")], remove=[Id("MB1")])
        assert excinfo.value.keys == ("mailboxIds/MB1",)

    def test_rejects_a_malformed_id(self):
        with pytest.raises(InvalidIdError):
            mailbox_patch(add=[Id("not a valid id")])

    def test_rejects_a_creation_reference(self):
        # A creation id belongs in the create map, not in a literal id slot.
        with pytest.raises(InvalidIdError, match="creation references"):
            mailbox_patch(add=[Id("#draftbox")])

    def test_result_validates(self):
        validate_patch(mailbox_patch(add=[Id("MB1")], remove=[Id("MB2")]))  # must not raise


class TestKeywordPatch:
    def test_add_and_remove_lowercased(self):
        assert keyword_patch(add=["$Seen"], remove=["$Flagged"]) == {
            "keywords/$seen": True,
            "keywords/$flagged": None,
        }

    def test_defaults_to_an_empty_patch(self):
        assert keyword_patch() == {}

    def test_values_are_true_exactly(self):
        assert keyword_patch(add=["$seen"])["keywords/$seen"] is True

    def test_escapes_a_keyword_containing_a_slash(self):
        assert keyword_patch(add=["a/b"]) == {"keywords/a~1b": True}

    def test_escaped_keyword_survives_validation_as_one_token(self):
        validate_patch(keyword_patch(add=["a/b"], remove=["a~b"]))  # must not raise

    def test_case_folding_makes_a_conflict_visible(self):
        with pytest.raises(InvalidPatchError, match="both added and removed") as excinfo:
            keyword_patch(add=["$Seen"], remove=["$seen"])
        assert excinfo.value.keys == ("keywords/$seen",)

    def test_rejects_an_unregistered_dollar_keyword(self):
        with pytest.raises(InvalidKeywordError, match="reserved for IANA"):
            keyword_patch(add=["$custom"])

    def test_duplicate_spellings_collapse_to_one_key(self):
        assert keyword_patch(add=["$Seen", "$seen"]) == {"keywords/$seen": True}

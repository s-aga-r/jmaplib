"""RFC 6901 pointer evaluation plus JMAP's ``*`` extension (RFC 8620 §3.7)."""

from __future__ import annotations

import pytest

from jmap.core.pointer import (
    PointerError,
    build_pointer,
    escape_token,
    resolve,
    resolve_or_default,
    split_pointer,
    unescape_token,
)

# The document from RFC 6901 §5, verbatim.
RFC6901_DOC = {
    "foo": ["bar", "baz"],
    "": 0,
    "a/b": 1,
    "c%d": 2,
    "e^f": 3,
    "g|h": 4,
    "i\\j": 5,
    'k"l': 6,
    " ": 7,
    "m~n": 8,
}


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("", RFC6901_DOC),
        ("/foo", ["bar", "baz"]),
        ("/foo/0", "bar"),
        ("/", 0),
        ("/a~1b", 1),
        ("/c%d", 2),
        ("/e^f", 3),
        ("/g|h", 4),
        ("/i\\j", 5),
        ('/k"l', 6),
        ("/ ", 7),
        ("/m~0n", 8),
    ],
)
def test_rfc6901_examples(pointer, expected):
    assert resolve(RFC6901_DOC, pointer) == expected


class TestEscaping:
    def test_roundtrip(self):
        for raw in ["a/b", "m~n", "~01", "~", "/", "plain"]:
            assert unescape_token(escape_token(raw)) == raw

    def test_tilde_one_decodes_before_tilde_zero(self):
        # If ~0 were substituted first, "~01" would wrongly become "/".
        assert unescape_token("~01") == "~1"

    def test_build_pointer_escapes_but_passes_wildcard_through(self):
        assert build_pointer("list", "*", "a/b") == "/list/*/a~1b"


class TestWildcard:
    """The flattening rule is the whole reason this module exists."""

    def test_flattens_one_level_when_remainder_is_an_array(self):
        doc = {"list": [{"ids": ["a", "b"]}, {"ids": ["c"]}]}
        assert resolve(doc, "/list/*/ids") == ["a", "b", "c"]

    def test_collects_scalars_without_flattening(self):
        doc = {"list": [{"id": "a"}, {"id": "b"}]}
        assert resolve(doc, "/list/*/id") == ["a", "b"]

    def test_mixed_scalar_and_array_results(self):
        doc = {"list": [{"x": ["a", "b"]}, {"x": "c"}]}
        assert resolve(doc, "/list/*/x") == ["a", "b", "c"]

    def test_empty_array_yields_empty_list(self):
        assert resolve({"list": []}, "/list/*/id") == []

    def test_trailing_wildcard_is_identity(self):
        assert resolve({"list": [1, 2]}, "/list/*") == [1, 2]

    def test_nested_wildcards_flatten_at_each_level(self):
        doc = {"a": [{"b": [{"c": ["x"]}, {"c": ["y"]}]}, {"b": [{"c": ["z"]}]}]}
        assert resolve(doc, "/a/*/b/*/c") == ["x", "y", "z"]

    def test_wildcard_on_non_array_is_an_error(self):
        with pytest.raises(PointerError, match="not an array"):
            resolve({"list": {"id": "a"}}, "/list/*/id")

    def test_realistic_email_query_backreference(self):
        # The shape an Email/get takes when chained off an Email/query.
        response = {"accountId": "a", "ids": ["m1", "m2"], "queryState": "s"}
        assert resolve(response, "/ids") == ["m1", "m2"]

    def test_realistic_thread_id_fanout(self):
        response = {"list": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]}
        assert resolve(response, "/list/*/threadId") == ["t1", "t2"]


class TestErrors:
    def test_missing_property(self):
        with pytest.raises(PointerError, match="no property"):
            resolve({"a": 1}, "/b")

    def test_index_out_of_range(self):
        with pytest.raises(PointerError, match="out of range"):
            resolve({"a": [1]}, "/a/5")

    def test_dash_is_rejected_when_reading(self):
        with pytest.raises(PointerError, match="does not reference an existing element"):
            resolve({"a": [1]}, "/a/-")

    @pytest.mark.parametrize("index", ["01", "x", "-1", "1.0"])
    def test_invalid_array_indices(self, index):
        with pytest.raises(PointerError, match="not a valid array index"):
            resolve({"a": [1, 2]}, f"/a/{index}")

    def test_traversing_into_scalar(self):
        with pytest.raises(PointerError, match="cannot traverse"):
            resolve({"a": 1}, "/a/b")

    def test_missing_leading_slash(self):
        with pytest.raises(PointerError, match="start with '/'"):
            resolve({"a": 1}, "a")

    def test_resolve_or_default_swallows(self):
        assert resolve_or_default({"a": 1}, "/nope", "fallback") == "fallback"


class TestSplitPointer:
    def test_patch_dialect_tolerates_missing_leading_slash(self):
        # PatchObject keys omit the leading '/' (RFC 8620 §5.3).
        assert split_pointer("mailboxIds/abc", leading_slash_required=False) == [
            "mailboxIds",
            "abc",
        ]

    def test_empty_pointer_is_the_whole_document(self):
        assert split_pointer("") == []

    def test_trailing_slash_yields_empty_final_token(self):
        # "/foo/" addresses the "" property of foo, which is legal in RFC 6901.
        assert split_pointer("/foo/") == ["foo", ""]


class TestHostileTokens:
    def test_a_unicode_digit_is_not_an_index(self):
        # '²'.isdigit() is true but int('²') raises a raw ValueError, and
        # RFC 6901 indices are ASCII digits only - '٣' must not read index 3.
        for token in ("²", "٣"):
            with pytest.raises(PointerError):
                resolve(["a", "b", "c", "d"], f"/{token}")

    def test_an_absurdly_long_index_is_a_pointer_error(self):
        # A 5000-digit token would otherwise trip CPython's int-conversion
        # limit with a stdlib ValueError instead of a PointerError.
        with pytest.raises(PointerError):
            resolve(["a"], "/" + "9" * 5000)

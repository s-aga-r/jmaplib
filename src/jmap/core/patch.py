"""``PatchObject`` construction and validation (RFC 8620 §5.3, RFC 8984 §3.3).

A PatchObject reads like JSON Merge Patch but is not one: every key is a JSON
Pointer whose leading ``/`` is *omitted*, because the server prepends one before
evaluating. So the key is ``"mailboxIds/abc"``. Writing ``"/mailboxIds/abc"`` -
the spelling a ``ResultReference.path`` requires two sections earlier - makes the
server evaluate ``"//mailboxIds/abc"``, which addresses the ``""`` property and
fails; the key still looks right in the log, so this module rejects it locally.

The remaining rules are all checked by the server *before* it applies anything,
so one bad key loses the entire update with an ``invalidPatch`` ``SetError``:

* a ``null`` value removes the addressed property, any other value sets it;
* a pointer must not reference inside an array (replace the array whole instead);
* no key may be a prefix of another (``"alerts"`` and ``"alerts/1/offset"``).

Two rules cannot be checked here. "Every parent object must already exist" is a
fact about server state the client does not have, so a patch that addresses two
levels down is accepted here and may still be rejected on arrival. And
"references inside an array" is decided from the *shape of a token*, since the
document is not in hand: a token that matches the RFC 6901 array-index grammar
(``0`` or ``[1-9][0-9]*``) or the append token ``-`` is presumed to index an
array. RFC 8620 §1.2 asks servers to avoid ids consisting only of digits for this
exact ambiguity, but only as a SHOULD - so if one does it anyway, name the parent
property in ``map_properties`` (``PatchBuilder(map_properties={"mailboxIds"})``).
That keeps the array rule enforced everywhere else, which
``DialectRules(allow_array_index=True)`` would not.

The three dialects differ in exactly one behaviour, so they are data rather than
subclasses: JSCalendar 2.0 (draft-ietf-calext-jscalendarbis-18) permits pointers
inside arrays, while RFC 8620 and JSCalendar 1.0 (RFC 8984 §3.3) forbid them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self

from jmap.core.ids import parse_id
from jmap.core.pointer import escape_token, split_pointer

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence

    from jmap.core.ids import Id

#: The ``SetError`` type a server returns for a patch that breaks these rules.
INVALID_PATCH: Final = "invalidPatch"

#: RFC 6901 §4 array indices, plus ``-`` ("the element after the last"). Nothing
#: else can address an array element, so nothing else needs rejecting.
_ARRAY_TOKEN_RE: Final = re.compile(r"\A(?:0|[1-9][0-9]*|-)\Z")


@dataclass(frozen=True, slots=True)
class DialectRules:
    """How one specification's PatchObject differs from the others.

    Kept as a value rather than a class hierarchy: the specs disagree about a
    single boolean, and a subclass per spec would turn that one difference into
    three code paths that have to be kept in step.
    """

    name: str
    #: JSCalendar 2.0 only. RFC 8620 and RFC 8984 require an array to be replaced
    #: in its entirety instead of patched element-wise.
    allow_array_index: bool = False
    #: Whether the empty key (replace the whole object) is meaningful. No shipped
    #: dialect allows it: in a ``/set`` update the object being replaced is
    #: addressed by its id already, and ``""`` would silently drop every property
    #: the client did not name.
    allow_whole_object: bool = False


#: RFC 8620 §5.3 - the rules for a ``Foo/set`` ``update`` patch.
JMAP: Final = DialectRules("JMAP")

#: RFC 8984 §3.3 - JSCalendar 1.0, as used for ``recurrenceOverrides`` values,
#: where the outer key is a LocalDateTime and the patch applies within that
#: override. Identical rules to :data:`JMAP`; the distinction is documentary.
JSCALENDAR_10: Final = DialectRules("JSCalendar 1.0")

#: draft-ietf-calext-jscalendarbis-18 - JSCalendar 2.0, which relaxed the
#: no-arrays rule so that a single element of e.g. ``links`` can be patched.
JSCALENDAR_20: Final = DialectRules("JSCalendar 2.0", allow_array_index=True)


class InvalidPatchError(ValueError):
    """A patch the server would reject with :data:`INVALID_PATCH`.

    ``keys`` holds every key implicated: one for a malformed pointer, two for an
    overlap, since neither of the pair is at fault on its own.
    """

    def __init__(self, keys: str | Iterable[str], reason: str) -> None:
        self.keys: tuple[str, ...] = (keys,) if isinstance(keys, str) else tuple(keys)
        self.reason = reason
        listed = ", ".join(repr(key) for key in self.keys)
        super().__init__(f"invalid patch key {listed}: {reason}")


class InvalidKeywordError(ValueError):
    """A string was used as an IMAP keyword but cannot be one (RFC 8621 §4.1.1)."""

    def __init__(self, keyword: str, reason: str) -> None:
        self.keyword = keyword
        self.reason = reason
        super().__init__(f"invalid keyword {keyword!r}: {reason}")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _key_tokens(
    key: str,
    dialect: DialectRules,
    map_properties: Collection[str] = (),
) -> tuple[str, ...]:
    """Decode one patch key, enforcing every rule that reads a key in isolation.

    ``map_properties`` names properties whose children are *map keys*, not array
    indices. Without it the array-index heuristic has a real false positive: an
    id may legally consist only of digits, so ``mailboxIds/123`` looks exactly
    like an array index. RFC 8620 §1.2 asks servers to avoid all-digit ids, but
    only as a SHOULD, and for unrelated reasons (glob completion, spreadsheets) -
    so a conforming server may still hand one out.
    """
    if key.startswith("/"):
        raise InvalidPatchError(key, "the leading '/' is implicit and must be omitted")
    if not key and not dialect.allow_whole_object:
        raise InvalidPatchError(key, f"{dialect.name} has no whole-object replacement")
    tokens = split_pointer(key, leading_slash_required=False)
    if not dialect.allow_array_index:
        for position, token in enumerate(tokens):
            if not _ARRAY_TOKEN_RE.match(token):
                continue
            if position and tokens[position - 1] in map_properties:
                continue
            raise InvalidPatchError(
                key,
                f"{token!r} references inside an array, forbidden by {dialect.name}. "
                f"If {token!r} is a map key rather than an array index, name its parent "
                f"in map_properties (or use mailbox_patch/keyword_patch).",
            )
    return tuple(tokens)


def _check_overlap(tokens_by_key: Mapping[str, tuple[str, ...]]) -> None:
    """Reject keys where one pointer is a prefix of another, or equals it.

    Comparing tokens rather than raw strings is what makes this correct:
    ``"a/b"`` is a prefix of ``"a/b/c"`` but not of ``"a/bc"``, and a
    ``str.startswith`` test cannot tell those apart. Distinct keys can also
    decode to the *same* pointer (``"~"`` and ``"~0"``), which the server sees as
    the same forbidden self-overlap.
    """
    owners: dict[tuple[str, ...], str] = {}
    for key, tokens in tokens_by_key.items():
        owner = owners.setdefault(tokens, key)
        if owner != key:
            raise InvalidPatchError((owner, key), "both keys address the same property")
    for key, tokens in tokens_by_key.items():
        for length in range(len(tokens)):
            # `is not None` and not truthiness: the whole-object key is "", which
            # is both a legitimate owner and falsy.
            shorter = owners.get(tokens[:length])
            if shorter is not None:
                raise InvalidPatchError((shorter, key), "one key is a prefix of the other")


def validate_patch(
    patch: Mapping[str, Any],
    dialect: DialectRules = JMAP,
    *,
    map_properties: Collection[str] = (),
) -> None:
    """Check ``patch`` against ``dialect``, raising :class:`InvalidPatchError`.

    Passing means the patch is well-formed, not that it will apply: whether the
    parent objects exist, and whether the properties are mutable, are known only
    to the server.
    """
    _check_overlap({key: _key_tokens(key, dialect, map_properties) for key in patch})


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def patch_key(*tokens: str) -> str:
    """Build a patch key from raw (unescaped) tokens - no leading ``/``.

    The escaping is not theoretical: a keyword may legally contain ``/`` and
    ``~``, so ``patch_key("keywords", "a/b")`` must yield ``"keywords/a~1b"`` for
    the server to see one property named ``a/b`` instead of two tokens.
    """
    return "/".join(escape_token(token) for token in tokens)


class PatchBuilder:
    """Accumulates edits and emits a PatchObject.

    Every edit is validated as it is added, so the traceback names the call that
    introduced the bad key. :meth:`build` re-validates the whole map anyway,
    which is what catches an overlap created by :meth:`merge`.
    """

    __slots__ = ("_dialect", "_edits", "_map_properties", "_tokens")

    def __init__(
        self,
        dialect: DialectRules = JMAP,
        *,
        map_properties: Collection[str] = (),
    ) -> None:
        self._dialect = dialect
        #: Properties whose children are map keys rather than array indices, so
        #: that an all-digit id (``mailboxIds/123``) is not mistaken for one.
        self._map_properties = frozenset(map_properties)
        self._edits: dict[str, Any] = {}
        self._tokens: dict[str, tuple[str, ...]] = {}

    def set(self, path: str, value: Any) -> Self:
        """Set the property at ``path`` to ``value``.

        ``value=None`` does not mean "leave unchanged": RFC 8620 §5.3 gives null
        the meaning "remove this property", so it is exactly :meth:`remove`.
        """
        self._add(path, value)
        return self

    def remove(self, path: str) -> Self:
        """Remove the property at ``path`` by patching it to null."""
        self._add(path, None)
        return self

    def merge(self, patch: Mapping[str, Any]) -> Self:
        """Fold in an existing patch, e.g. one from :func:`keyword_patch`."""
        for path, value in patch.items():
            self._add(path, value)
        return self

    def build(self) -> dict[str, Any]:
        """Return the PatchObject as a plain dict.

        A copy, so that continuing to use the builder cannot mutate a patch that
        has already been handed to a request.
        """
        validate_patch(self._edits, self._dialect, map_properties=self._map_properties)
        return dict(self._edits)

    def _add(self, path: str, value: Any) -> None:
        tokens = _key_tokens(path, self._dialect, self._map_properties)
        # Re-keying an existing path is an overwrite, not an overlap, so the
        # candidate map must replace that entry rather than add a second one.
        _check_overlap({**self._tokens, path: tokens})
        self._tokens[path] = tokens
        self._edits[path] = value

    def __len__(self) -> int:
        # Also gives truthiness: `if builder:` is how a caller skips sending an
        # update that would patch nothing.
        return len(self._edits)

    def __repr__(self) -> str:
        return f"PatchBuilder({self._dialect.name!r}, {self._edits!r})"


# --------------------------------------------------------------------------- #
# Email conveniences (RFC 8621 §4.1.1)
# --------------------------------------------------------------------------- #
MAILBOX_IDS: Final = "mailboxIds"
KEYWORDS: Final = "keywords"
MAX_KEYWORD_OCTETS: Final = 255

#: RFC 8621 §4.1.1 reserves a leading ``$`` for the IANA "IMAP and JMAP Keywords"
#: registry; a client must not invent one.
IANA_PREFIX: Final = "$"
IANA_KEYWORDS: Final = frozenset(
    {
        "$draft",
        "$seen",
        "$flagged",
        "$answered",
        "$forwarded",
        "$phishing",
        "$junk",
        "$notjunk",
    }
)

#: The IMAP atom-specials a keyword may not contain.
_KEYWORD_EXCLUDED: Final = frozenset('(){]%*"\\')
#: Printable US-ASCII (%x21-%x7e) minus those, per RFC 8621 §4.1.1.
_KEYWORD_ALLOWED: Final = frozenset(
    chr(code) for code in range(0x21, 0x7F) if chr(code) not in _KEYWORD_EXCLUDED
)


def parse_keyword(value: str) -> str:
    """Validate ``value`` as a keyword and return it lowercased.

    This normalises rather than merely checking, because keywords are
    case-insensitive but the patch key is not: ``"$Seen"`` and ``"$seen"`` would
    otherwise become two entries addressing one property, which is the
    prefix/duplicate violation in :func:`validate_patch`.
    """
    keyword = value.lower()
    if not keyword:
        raise InvalidKeywordError(value, "must be at least 1 character")
    outside = sorted(set(keyword) - _KEYWORD_ALLOWED)
    if outside:
        raise InvalidKeywordError(
            value, f"contains {''.join(outside)!r}, outside the keyword charset"
        )
    # Checked after the charset, where every character is one octet.
    if len(keyword) > MAX_KEYWORD_OCTETS:
        raise InvalidKeywordError(value, f"exceeds {MAX_KEYWORD_OCTETS} octets")
    if keyword.startswith(IANA_PREFIX) and keyword not in IANA_KEYWORDS:
        raise InvalidKeywordError(value, "leading '$' is reserved for IANA-registered keywords")
    return keyword


def is_valid_keyword(value: str) -> bool:
    """Return whether ``value`` is a well-formed keyword, ignoring case."""
    try:
        parse_keyword(value)
    except InvalidKeywordError:
        return False
    return True


def _conflicts(added: Sequence[str], removed: Sequence[str], prefix: str, noun: str) -> None:
    """Reject a value present on both sides, which a dict would silently collapse."""
    both = sorted(set(added) & set(removed))
    if both:
        raise InvalidPatchError(
            [patch_key(prefix, value) for value in both],
            f"the same {noun} is both added and removed",
        )


def mailbox_patch(add: Iterable[Id] = (), remove: Iterable[Id] = ()) -> dict[str, Any]:
    """Patch an Email's ``mailboxIds`` (RFC 8621 §4.1).

    Moving a message is one patch, not two updates: ``{"mailboxIds/new": True,
    "mailboxIds/old": None}`` is atomic, whereas replacing the whole
    ``mailboxIds`` object would clobber filing done concurrently by another
    client between the read and the write.
    """
    added = [parse_id(mailbox_id) for mailbox_id in add]
    removed = [parse_id(mailbox_id) for mailbox_id in remove]
    _conflicts(added, removed, MAILBOX_IDS, "mailbox")
    patch: dict[str, Any] = {patch_key(MAILBOX_IDS, mailbox_id): True for mailbox_id in added}
    patch.update({patch_key(MAILBOX_IDS, mailbox_id): None for mailbox_id in removed})
    return patch


def keyword_patch(add: Iterable[str] = (), remove: Iterable[str] = ()) -> dict[str, Any]:
    """Patch an Email's ``keywords`` (RFC 8621 §4.1.1).

    Values are ``True`` exactly, never ``1`` or the keyword itself: RFC 8621
    requires the value of a set keyword to be ``true``.
    """
    added = [parse_keyword(keyword) for keyword in add]
    removed = [parse_keyword(keyword) for keyword in remove]
    _conflicts(added, removed, KEYWORDS, "keyword")
    patch: dict[str, Any] = {patch_key(KEYWORDS, keyword): True for keyword in added}
    patch.update({patch_key(KEYWORDS, keyword): None for keyword in removed})
    return patch

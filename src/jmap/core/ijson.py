"""I-JSON (RFC 7493) constraints that JMAP inherits (RFC 8620 §1.1, §1.3, §1.4).

RFC 8620 §1.1 requires every JMAP payload to be I-JSON, a profile of JSON that
Python's :mod:`json` does not implement. Three of its constraints fail *silently*
under the stdlib, which is the reason this module exists instead of calling
``json.loads``/``json.dumps`` directly:

* **Integer range.** ``Int`` is bounded to ±(2**53 - 1) (RFC 8620 §1.3) because
  the peer may be JavaScript, where a larger integer loses its low-order bits on
  parse. Python serialises arbitrary-precision ints happily, so the corruption
  happens on the *far* side of the wire and returns as a wrong id or a wrong size.
* **Duplicate object keys.** ``json.loads`` keeps the last of a repeated member
  and says nothing. RFC 7493 §2.3 forbids duplicates outright, and a server that
  emits them is misbehaving in a way worth surfacing at the parse, not later.
* **Unpaired surrogates.** ``json.loads`` decodes ``"\\uD800"`` into a lone
  surrogate code point, which is not valid Unicode. With ``ensure_ascii=False``
  it survives ``json.dumps`` untouched and only explodes when the HTTP layer
  encodes the body to UTF-8 - a ``UnicodeEncodeError`` arbitrarily far from the
  field that caused it. A *paired* ``"\\uD83D\\uDE00"`` is combined into U+1F600
  by the stdlib decoder and is perfectly legal, so only unpaired ones are rejected.

The date helpers cover RFC 8620 §1.4, whose normalised form is narrower than
RFC 3339: a ``UTCDate`` must carry the literal ``Z`` suffix, so ``+00:00`` - which
every generic RFC 3339 parser accepts - must never be echoed back to a server
unchanged. ``LocalDate`` (RFC 8984 §1.4.4, used by JSCalendar) is the same
grammar with *no* offset at all; it maps to a naive :class:`~datetime.datetime`
so the type system alone prevents it being mistaken for an instant.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

from jmap.core.pointer import escape_token

if TYPE_CHECKING:
    from collections.abc import Callable

#: RFC 8620 §1.3: the largest integer that survives an IEEE-754 double intact.
MAX_SAFE_INT: Final = 2**53 - 1
MIN_SAFE_INT: Final = -(2**53) + 1


class IJSONError(ValueError):
    """A value violates an I-JSON or JMAP wire-format constraint."""


def _where(field: str | None) -> str:
    return f"{field}: " if field else ""


class IntegerRangeError(IJSONError):
    """An integer is outside the range that survives a JSON round trip."""

    def __init__(self, value: int, *, unsigned: bool = False, field: str | None = None) -> None:
        self.value = value
        self.unsigned = unsigned
        self.field = field
        low = 0 if unsigned else MIN_SAFE_INT
        kind = "UnsignedInt" if unsigned else "Int"
        super().__init__(f"{_where(field)}{value} is outside {kind} range [{low}, {MAX_SAFE_INT}]")


class DuplicateKeyError(IJSONError):
    """An object repeated a member name (RFC 7493 §2.3)."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"duplicate object key {key!r}")


class InvalidStringError(IJSONError):
    """A string is not valid I-JSON text, or a key is not a string at all."""

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        self.reason = reason
        self.field = field
        super().__init__(f"{_where(field)}{reason}")


class InvalidDateError(IJSONError):
    """A ``UTCDate`` or ``LocalDate`` is malformed or not representable.

    Subclasses :class:`IJSONError` so that one ``except`` clause covers every
    wire-format rejection this module makes.
    """

    def __init__(self, value: object, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"invalid date {value!r}: {reason}")


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #
#: Python ``str`` stores code points, so a surrogate is *always* unpaired here:
#: a real astral character is one code point, never a pair of them.
_SURROGATE_RE: Final = re.compile("[\ud800-\udfff]")


def check_int(value: int, *, unsigned: bool = False, field: str | None = None) -> int:
    """Return ``value`` if it is a JMAP ``Int`` (or ``UnsignedInt``), else raise.

    Rejects ``bool``: it is an ``int`` subclass in Python and would pass the range
    check, but it serialises as ``true``/``false`` and so can never be a number on
    the wire. Accepting it here would let a flag silently occupy a numeric field.
    """
    if isinstance(value, bool):
        raise TypeError(f"{_where(field)}bool is not a JMAP Int; it serialises as true/false")
    if value > MAX_SAFE_INT or value < (0 if unsigned else MIN_SAFE_INT):
        raise IntegerRangeError(value, unsigned=unsigned, field=field)
    return value


def check_string(value: str, *, field: str | None = None) -> str:
    """Return ``value`` if it is valid Unicode text, else raise.

    The only way a Python ``str`` can fail to encode as UTF-8 is an unpaired
    surrogate, so that is exactly what this rejects.
    """
    if value.isascii():
        # A cached flag on the string object, and a surrogate is far above
        # U+007F, so this settles the overwhelming majority without a scan.
        return value
    match = _SURROGATE_RE.search(value)
    if match is not None:
        raise InvalidStringError(
            f"unpaired surrogate U+{ord(match.group()):04X} at index {match.start()}",
            field=field,
        )
    return value


def _check_tree(value: object, path: str) -> None:
    """Validate every scalar reachable from ``value``.

    ``path`` accumulates a JSON Pointer (RFC 6901) so the error names the exact
    member at fault; a bare "integer out of range" is close to useless against a
    response holding thousands of them.

    Building that path costs a string concatenation and an :func:`escape_token`
    call for every member of every object, which is why this is not the function
    the success path runs. It is the *authority*: :func:`_scan` decides whether
    anything is wrong, and this decides what to say about it. Keeping the two
    apart is what lets the fast check be approximate without any loss of
    diagnostic quality.
    """
    # bool first: it is an int subclass, and true/false is not a number.
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        check_int(value, field=path or None)
    elif isinstance(value, str):
        check_string(value, field=path or None)
    elif isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            if not isinstance(key, str):
                raise InvalidStringError(
                    f"object key {key!r} is {type(key).__name__}, not a string",
                    field=path or None,
                )
            child = f"{path}/{escape_token(key)}"
            check_string(key, field=child)
            _check_tree(item, child)
    elif isinstance(value, (list, tuple)):
        # Tuples are here because json.dumps serialises them as arrays; skipping
        # them would leave a whole subtree unvalidated on the way out.
        for index, element in enumerate(cast("list[object] | tuple[object, ...]", value)):
            _check_tree(element, f"{path}/{index}")


#: :func:`type` itself, retyped to say what it returns for a decoded JSON value.
#:
#: ``type(x)`` where ``x`` is ``Any`` is *partially unknown* to pyright, and a
#: bare annotation on the result does not settle it. The alternatives all cost
#: something in the loop below - a ``cast`` per node, or several ``type()`` calls
#: per node to get inline narrowing - whereas this is the same builtin under a
#: different name, resolved once at import. Same problem :mod:`jmap.core.narrow`
#: exists for, and the same answer: separate the check from the cast.
_type_of = cast("Callable[[Any], type[object]]", type)


def _scan(value: object) -> bool:
    """Whether ``value`` is *provably* free of I-JSON violations.

    A fast, allocation-free traversal that answers only yes/no. ``False`` means
    "look closer", not "invalid" - anything it cannot cheaply prove clean, an
    unfamiliar type most of all, is handed to :func:`_check_tree`, which decides
    for real and produces the message. So the only way to be wrong here is to
    return ``True`` for a tree that is actually bad; returning ``False`` too
    often costs one extra traversal and nothing else.

    Iterative rather than recursive, and matching on ``type(...) is`` rather than
    :func:`isinstance`, because both show up directly in the profile: a JMAP
    response of a hundred messages is some twenty thousand nodes, and at that
    size a Python frame per node is the dominant cost of parsing it.
    """
    stack: list[Any] = [value]
    push = stack.append
    extend = stack.extend
    search = _SURROGATE_RE.search
    type_of = _type_of
    while stack:
        node = stack.pop()
        kind = type_of(node)
        # `type(...) is` keeps bool out of the int branch for free: bool is a
        # subclass of int, so isinstance would need the usual explicit guard.
        if kind is str:
            # `isascii` reads a flag set when the string was built, so it costs a
            # fraction of entering the regex engine, and a surrogate is far above
            # U+007F - an ASCII string cannot contain one.
            if not node.isascii() and search(node) is not None:
                return False
        elif kind is int:
            if node > MAX_SAFE_INT or node < MIN_SAFE_INT:
                return False
        elif kind is dict:
            for key, item in node.items():
                if type(key) is not str or (not key.isascii() and search(key) is not None):
                    return False
                push(item)
        elif kind is list or kind is tuple:
            extend(node)
        elif not (kind is bool or kind is float or node is None):
            # A subclass of one of the above, or something json will reject on
            # its own. Either way, not this function's call to make.
            return False
    return True


def _validate(value: object) -> None:
    """Raise if ``value`` violates I-JSON, naming the exact member."""
    if not _scan(value):
        _check_tree(value, "")


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Building the dict in C and comparing lengths is markedly faster than
    # testing membership per key in Python, and this runs once per object in
    # every response. The slow scan happens only when a duplicate is known to be
    # present, because naming the key is the only actionable part of the report.
    result: dict[str, Any] = dict(pairs)
    if len(result) == len(pairs):
        return result
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise DuplicateKeyError(key)
        seen.add(key)
    # Unreachable: the lengths differ only because some key appears twice, and
    # the loop above raises on the second sighting of it.
    raise AssertionError(pairs)  # pragma: no cover


def _safe_int(text: str) -> int:
    """``json``'s integer hook, rejecting anything outside ``Int`` range.

    Checking here rather than in a traversal is what keeps a clean document from
    being walked at all: the parser calls this only for integer literals, so the
    cost is proportional to the numbers in the document instead of to every node
    in it. The hook is handed a value with no idea where it sits, so the error it
    raises is deliberately path-less - :func:`loads` catches it and re-walks to
    produce the located one.
    """
    value = int(text)
    if value > MAX_SAFE_INT or value < MIN_SAFE_INT:
        raise IntegerRangeError(value)
    return value


def _may_hold_surrogate(source: str, *, literal_possible: bool) -> bool:
    """Whether any string in ``source`` could decode to an unpaired surrogate.

    Two routes in, and both are cheap to rule out across a whole document:

    * a **literal** surrogate code point in the source text, which is possible
      only when the caller handed us a ``str`` - a strict UTF-8 decode cannot
      produce one, so the bytes path skips this half entirely. ``isascii`` is a
      cached flag on the string object, so the common case costs nothing;
    * a ``\\uD800``-style **escape**. Matched by substring rather than regex
      because ``str.find`` is several times faster over a large body, and the
      over-match (a legal *paired* escape, or a literal backslash before a ``u``)
      only sends us down the precise path, which then finds nothing wrong.
    """
    if literal_possible and not source.isascii() and _SURROGATE_RE.search(source) is not None:
        return True
    return "\\u" in source and ("\\ud" in source or "\\uD" in source)


def loads(text: str | bytes) -> Any:
    """Parse an I-JSON document.

    Unlike :func:`json.loads` this rejects duplicate object members, out-of-range
    integers and unpaired surrogates. Malformed JSON still raises
    :class:`json.JSONDecodeError`, which is also a :class:`ValueError`.

    Each of the three constraints is enforced during the parse or ruled out from
    the source text, so a well-formed document is never traversed a second time.
    That matters at the sizes JMAP actually returns: a ``/get`` of a hundred
    messages decodes to tens of thousands of nodes, and re-walking them in Python
    cost several times the parse itself.
    """
    if isinstance(text, bytes):
        # Decoded here rather than by json.loads, which sniffs for UTF-16/32 via
        # detect_encoding(); RFC 7493 §2.1 allows UTF-8 only.
        try:
            source = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidStringError(f"body is not valid UTF-8: {exc}") from exc
        literal_possible = False
    else:
        source = text
        literal_possible = True

    try:
        parsed: Any = json.loads(source, object_pairs_hook=_reject_duplicates, parse_int=_safe_int)
    except IntegerRangeError:
        # The hook cannot know where the number was. Re-parse without it and walk
        # the tree so the report names the member, which is the half worth having.
        _check_tree(json.loads(source, object_pairs_hook=_reject_duplicates), "")
        raise  # pragma: no cover - the walk above always finds the same integer

    if _may_hold_surrogate(source, literal_possible=literal_possible):
        _validate(parsed)
    return parsed


def dumps(value: Any) -> str:
    """Serialise ``value`` as a compact I-JSON document.

    ``allow_nan=False`` because ``NaN``/``Infinity`` are Python extensions that no
    conforming parser accepts, and ``ensure_ascii=False`` because JMAP bodies are
    UTF-8 - which is only safe once :func:`check_string` has ruled out surrogates.
    """
    _validate(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# UTCDate / LocalDate (RFC 8620 §1.4, RFC 8984 §1.4.4)
# --------------------------------------------------------------------------- #
_DATE_TIME: Final = (
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
)
#: Case-sensitive on purpose: RFC 8620 §1.4 requires ``T`` and ``Z`` uppercase.
_UTC_DATE_RE: Final = re.compile(rf"\A{_DATE_TIME}Z\Z")
_LOCAL_DATE_RE: Final = re.compile(rf"\A{_DATE_TIME}\Z")
_OFFSET_RE: Final = re.compile(r"[+-]\d{2}:\d{2}\Z")


def _to_datetime(match: re.Match[str], value: str, *, utc: bool) -> datetime:
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            # RFC 3339 permits any number of fractional digits; datetime stops at
            # microseconds, so surplus precision is truncated rather than made a
            # parse failure - the alternative is rejecting a legal timestamp.
            int((match["fraction"] or "")[:6].ljust(6, "0")),
            tzinfo=UTC if utc else None,
        )
    except ValueError as exc:
        # The pattern fixes field widths only. 2014-13-32 and the leap second
        # 23:59:60 are well-formed RFC 3339 but not representable as a datetime.
        raise InvalidDateError(value, str(exc)) from exc


def _utc_date_reason(value: str) -> str:
    if _OFFSET_RE.search(value):
        return "UTCDate must end with 'Z'; convert the offset to UTC first"
    if _LOCAL_DATE_RE.match(value):
        return "missing time offset; UTCDate must end with 'Z'"
    return "not an RFC 3339 date-time of the form YYYY-MM-DDThh:mm:ss[.fff]Z"


def parse_utc_date(value: str) -> datetime:
    """Parse a JMAP ``UTCDate`` into an aware UTC :class:`~datetime.datetime`.

    ``+00:00`` is rejected even though it denotes the same instant: the parsed
    value is routinely sent back (as a filter bound, or an ``If-Match`` style
    condition), and some servers reject any offset that is not the literal ``Z``.
    Normalising here would hide the server's non-conformance from the caller.
    """
    match = _UTC_DATE_RE.match(value)
    if match is None:
        raise InvalidDateError(value, _utc_date_reason(value))
    return _to_datetime(match, value, utc=True)


def _local_date_reason(value: str) -> str:
    if value.endswith("Z") or _OFFSET_RE.search(value):
        return "LocalDate must not carry a time zone or offset"
    return "not a date-time of the form YYYY-MM-DDThh:mm:ss[.fff]"


def parse_local_date(value: str) -> datetime:
    """Parse a ``LocalDate`` into a *naive* :class:`~datetime.datetime`.

    Naive on purpose: a LocalDate is a wall-clock reading whose time zone lives in
    a sibling property, so attaching any ``tzinfo`` here would invent an instant
    the sender never specified.
    """
    match = _LOCAL_DATE_RE.match(value)
    if match is None:
        raise InvalidDateError(value, _local_date_reason(value))
    return _to_datetime(match, value, utc=False)


def _format(value: datetime) -> str:
    # Built by hand rather than strftime: '%Y' is platform-dependent for years
    # before 1000, where RFC 3339 still demands exactly four digits.
    text = (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    )
    # RFC 8620 §1.4 mandates the normalised form: no time-secfrac when it is zero.
    return f"{text}.{value.microsecond:06d}" if value.microsecond else text


def format_utc_date(value: datetime) -> str:
    """Render an aware datetime as a ``UTCDate``, converting to UTC first."""
    if value.utcoffset() is None:
        raise InvalidDateError(value, "naive datetime: use format_local_date, or attach a tzinfo")
    return f"{_format(value.astimezone(UTC))}Z"


def format_local_date(value: datetime) -> str:
    """Render a naive datetime as a ``LocalDate``.

    An aware datetime is rejected rather than silently stripped: dropping an
    offset changes what the value *means*, and doing it implicitly is how a
    UTC instant ends up stored as somebody's local wall clock.
    """
    if value.utcoffset() is not None:
        raise InvalidDateError(
            value, "aware datetime is an instant, not a LocalDate; drop the offset explicitly"
        )
    return _format(value)

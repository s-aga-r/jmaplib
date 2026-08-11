"""JSON Pointer evaluation with JMAP's ``*`` extension (RFC 6901, RFC 8620 §3.7).

JMAP back-references address a prior method's response with a JSON Pointer that
supports one extra token, ``*``, meaning "apply the rest of the pointer to every
element of this array".

The subtle part - and the reason this module is tested to death - is the
*flattening* rule. When the remainder of the pointer yields an array for each
element, the results are concatenated one level, not nested. So against::

    {"list": [{"ids": ["a", "b"]}, {"ids": ["c"]}]}

the pointer ``/list/*/ids`` yields ``["a", "b", "c"]`` and **not**
``[["a", "b"], ["c"]]``. Getting this wrong produces a request the server
rejects with ``invalidResultReference``, and the failure is easy to misread as a
server bug.
"""

from __future__ import annotations

from typing import Any, Final, cast

WILDCARD: Final = "*"


class PointerError(ValueError):
    """A pointer is malformed, or does not resolve against the document."""

    def __init__(self, pointer: str, reason: str) -> None:
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"JSON pointer {pointer!r}: {reason}")


def unescape_token(token: str) -> str:
    """Decode one reference token per RFC 6901 §3.

    Order matters: ``~1`` must become ``/`` before ``~0`` becomes ``~``, or the
    input ``~01`` would decode to ``/`` instead of the correct ``~1``.
    """
    return token.replace("~1", "/").replace("~0", "~")


def escape_token(token: str) -> str:
    """Encode one reference token per RFC 6901 §3 (inverse of :func:`unescape_token`)."""
    return token.replace("~", "~0").replace("/", "~1")


def split_pointer(pointer: str, *, leading_slash_required: bool = True) -> list[str]:
    """Split a pointer into decoded reference tokens.

    ``leading_slash_required`` distinguishes the two dialects JMAP uses: a
    ``ResultReference.path`` carries its leading ``/`` (RFC 8620 §3.7), while a
    ``PatchObject`` key omits it (§5.3).
    """
    if pointer == "":
        return []
    if leading_slash_required:
        if not pointer.startswith("/"):
            raise PointerError(pointer, "must be empty or start with '/'")
        body = pointer[1:]
    else:
        body = pointer.removeprefix("/")
    return [unescape_token(t) for t in body.split("/")]


def _index_array(items: list[Any], token: str, pointer: str) -> Any:
    if token == "-":
        # RFC 6901 defines "-" as the nonexistent element after the last one. It
        # is meaningful when adding to an array, never when reading one.
        raise PointerError(pointer, "'-' does not reference an existing element")
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise PointerError(pointer, f"{token!r} is not a valid array index")
    index = int(token)
    if index >= len(items):
        raise PointerError(pointer, f"index {index} out of range (length {len(items)})")
    return items[index]


def _resolve_tokens(value: Any, tokens: list[str], pointer: str) -> Any:
    # The casts are not noise: narrowing an `Any` with isinstance yields an
    # unparameterised container, which pyright reports as partially unknown. mypy
    # narrows to `list[Any]` on its own and calls the same cast redundant, so the
    # ignores below are what lets both checkers pass on one line of code.
    for position, token in enumerate(tokens):
        if token == WILDCARD:
            if not isinstance(value, list):
                raise PointerError(pointer, f"'*' applied to {type(value).__name__}, not an array")
            elements = cast("list[Any]", value)  # type: ignore[redundant-cast]
            rest = tokens[position + 1 :]
            if not rest:
                # A trailing '*' is a no-op: the array is already the result.
                return elements
            collected: list[Any] = []
            for element in elements:
                result: Any = _resolve_tokens(element, rest, pointer)
                # The flattening rule: concatenate one level when the remainder
                # produced an array, otherwise append the scalar.
                if isinstance(result, list):
                    collected.extend(cast("list[Any]", result))  # type: ignore[redundant-cast]
                else:
                    collected.append(result)
            return collected

        if isinstance(value, dict):
            mapping = cast("dict[str, Any]", value)
            if token not in mapping:
                raise PointerError(pointer, f"no property {token!r}")
            value = mapping[token]
        elif isinstance(value, list):
            items = cast("list[Any]", value)  # type: ignore[redundant-cast]
            value = _index_array(items, token, pointer)
        else:
            raise PointerError(pointer, f"cannot traverse {token!r} into {type(value).__name__}")
    return value


def resolve(document: Any, pointer: str) -> Any:
    """Evaluate ``pointer`` against ``document``.

    Raises :class:`PointerError` rather than returning a sentinel: a
    back-reference that does not resolve is a programming error we want surfaced
    before the request is sent, not a ``None`` that travels to the server.
    """
    return _resolve_tokens(document, split_pointer(pointer), pointer)


def resolve_or_default(document: Any, pointer: str, default: Any = None) -> Any:
    """Like :func:`resolve`, but returns ``default`` instead of raising."""
    try:
        return resolve(document, pointer)
    except PointerError:
        return default


def build_pointer(*tokens: str) -> str:
    """Build a pointer from raw (unescaped) tokens.

    ``build_pointer("list", "*", "id")`` -> ``"/list/*/id"``. The wildcard is
    passed through unescaped; every other token is escaped.
    """
    return "".join(f"/{token if token == WILDCARD else escape_token(token)}" for token in tokens)

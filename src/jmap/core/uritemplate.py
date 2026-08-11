"""RFC 6570 Level 1 URI Template expansion.

Every templated URL a JMAP session advertises is Level 1 and nothing more
(RFC 8620 §2): ``downloadUrl`` is ``.../{accountId}/{blobId}/{name}?accept={type}``,
``uploadUrl`` is ``.../upload/{accountId}/``, ``eventSourceUrl`` is
``.../?types={types}&closeafter={closeafter}&ping={ping}``. Level 1 is a page of
code, so it is hand-rolled here rather than taking a dependency on the
``uritemplate`` package for a fraction of what it implements.

Two details are easy to get wrong, and both fail late rather than loudly:

*Encoding.* Level 1 simple string expansion (§3.2.2) pct-encodes everything
outside the *unreserved* set - reserved characters included. ``/`` becomes
``%2F``, which is precisely what keeps a blob id or an attachment filename that
contains a slash inside its own path segment instead of inventing new ones.

*Refusing to guess.* §3.2.1 says an undefined variable expands to nothing, and a
naive parser will happily read ``{+baseUrl}`` as a variable literally named
``+baseUrl``. Both behaviours produce a plausible-looking URL that 404s minutes
later, by which point the missing path segment is the last thing anyone
suspects. So this module raises :class:`MissingVariableError` and
:class:`UnsupportedTemplateError` instead - a deliberate deviation from §3.2.1.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, overload
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping

#: RFC 6570 §2.2 operators: ``+`` and ``#`` are Level 2, ``.``, ``/``, ``;``,
#: ``?`` and ``&`` are Level 3, and ``=``, ``,``, ``!``, ``@``, ``|`` are
#: reserved for future extensions. None may be mistaken for a variable name.
_OPERATORS: Final = frozenset("+#./;?&=,!@|")

#: RFC 6570 §2.3: ``varname = varchar *( ["."] varchar )`` with
#: ``varchar = ALPHA / DIGIT / "_" / pct-encoded``. Dots appear only *between*
#: varchars, so a leading, trailing or doubled dot is malformed.
_VARCHAR: Final = r"(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})"
_VARNAME_RE: Final = re.compile(rf"\A{_VARCHAR}+(?:\.{_VARCHAR}+)*\Z")


class TemplateError(ValueError):
    """Base class for every URI template failure raised here."""


class InvalidTemplateError(TemplateError):
    """The string is not a well-formed URI template at all.

    RFC 6570 gives braces no escape sequence, so a stray ``{`` or ``}`` is
    always a malformed template rather than a literal brace.
    """

    def __init__(self, template: str, reason: str) -> None:
        self.template = template
        self.reason = reason
        super().__init__(f"invalid URI template {template!r}: {reason}")


class UnsupportedTemplateError(TemplateError):
    """The template uses an RFC 6570 feature above Level 1."""

    def __init__(self, template: str, expression: str, feature: str) -> None:
        self.template = template
        #: The offending expression body, without its braces.
        self.expression = expression
        #: Human-readable name of the Level 2+ feature, e.g. ``"operator '+'"``.
        self.feature = feature
        super().__init__(
            f"URI template {template!r}: {feature} in '{{{expression}}}' is beyond "
            f"RFC 6570 Level 1, which is all JMAP session URLs may use (RFC 8620 §2)"
        )


class MissingVariableError(TemplateError):
    """A variable in the template has no value.

    Listing what *was* supplied turns the common typo (``blob_id`` for
    ``blobId``) into a one-line diagnosis.
    """

    def __init__(self, template: str, variable: str, supplied: tuple[str, ...]) -> None:
        self.template = template
        self.variable = variable
        self.supplied = supplied
        super().__init__(
            f"URI template {template!r} needs variable {variable!r}; "
            f"supplied: {', '.join(supplied) if supplied else '(nothing)'}"
        )


class _Var:
    """A parsed ``{name}`` expression, as distinct from a literal run of text."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _varname(template: str, body: str) -> str:
    """Validate one expression body and return the variable name it names."""
    if not body:
        raise InvalidTemplateError(template, "empty expression '{}'")
    if "{" in body:
        raise InvalidTemplateError(template, "'{' inside an expression")
    if body[0] in _OPERATORS:
        raise UnsupportedTemplateError(template, body, f"operator {body[0]!r}")
    # Order matters only for the message: check the whole-expression features
    # before falling through to the catch-all varname check, which would report
    # them as unrecognised characters instead of naming the feature.
    if "," in body:
        raise UnsupportedTemplateError(template, body, "variable list ','")
    if ":" in body:
        raise UnsupportedTemplateError(template, body, "prefix modifier ':'")
    if body.endswith("*"):
        raise UnsupportedTemplateError(template, body, "explode modifier '*'")
    if not _VARNAME_RE.match(body):
        raise InvalidTemplateError(template, f"{body!r} is not a valid varname")
    return body


def _parse(template: str) -> list[str | _Var]:
    """Split ``template`` into literal runs and variable expressions."""
    parts: list[str | _Var] = []
    position = 0
    while position < len(template):
        start = template.find("{", position)
        literal = template[position:] if start < 0 else template[position:start]
        if "}" in literal:
            raise InvalidTemplateError(template, "unmatched '}'")
        if literal:
            parts.append(literal)
        if start < 0:
            break
        end = template.find("}", start + 1)
        if end < 0:
            raise InvalidTemplateError(template, "unterminated '{'")
        parts.append(_Var(_varname(template, template[start + 1 : end])))
        position = end + 1
    return parts


def variables(template: str) -> frozenset[str]:
    """Return the variable names in ``template``.

    Validates as it goes, so this doubles as the cheapest way to reject a
    session URL the client could never expand.
    """
    return frozenset(part.name for part in _parse(template) if isinstance(part, _Var))


@overload
def expand(template: str, /, **values: str | int) -> str: ...


@overload
def expand(template: str, values: Mapping[str, str | int], /) -> str: ...


def expand(
    template: str,
    values: Mapping[str, str | int] | None = None,
    /,
    **kwargs: str | int,
) -> str:
    """Expand a Level 1 template, pct-encoding each value.

    Values may be passed as keyword arguments or as a single mapping; the
    mapping form exists because RFC 6570 varnames permit ``.`` and
    pct-encoding, neither of which is a legal Python identifier.

    Literals are emitted verbatim: a session URL is already a valid URI, and
    re-encoding it would corrupt the ``?`` and ``&`` that hold the template
    together.
    """
    supplied: Mapping[str, str | int] = {**values, **kwargs} if values is not None else kwargs
    out: list[str] = []
    for part in _parse(template):
        if isinstance(part, _Var):
            if part.name not in supplied:
                raise MissingVariableError(template, part.name, tuple(supplied))
            # safe="" is the whole point: Level 1 encodes reserved characters
            # too, so a value can never break out of its path segment or query
            # parameter.
            out.append(quote(str(supplied[part.name]), safe=""))
        else:
            out.append(part)
    return "".join(out)

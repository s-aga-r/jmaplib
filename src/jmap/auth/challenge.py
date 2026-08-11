"""Parsing ``WWW-Authenticate`` challenges (RFC 9110 §11.6.1).

RFC 8620 deliberately defines no authentication scheme, so a JMAP client learns
what a server will accept only from the challenge it sends with a 401. That makes
this parser load-bearing for two decisions:

* **Whether to retry.** A 401 carrying a Bearer challenge may be worth one retry
  after refreshing the token. A 401 carrying only Basic is not: the credentials
  would be identical, and Stalwart fail2bans repeat failures.
* **Where to look next.** RFC 9728 puts the protected-resource metadata URL in
  the challenge's ``resource_metadata`` parameter, which is the entry point to
  OAuth discovery. A live Stalwart sends exactly that.

The grammar is genuinely ambiguous, and that ambiguity is the whole difficulty:
challenges are comma-separated, auth-params are *also* comma-separated, so
``Bearer realm="x", Basic realm="y"`` has to split into two challenges while
``Bearer realm="x", error="expired"`` stays as one. The rule used here is the
usual one - a fragment beginning with a bare token that is not ``name=value``
starts a new challenge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: RFC 9110 §11.6.1 auth-param: ``token "=" ( token / quoted-string )``.
_PARAM_RE: Final = re.compile(
    r"""\A(?P<name>[!#$%&'*+\-.^_`|~0-9A-Za-z]+)\s*=\s*
        (?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<bare>[^\s,]*))\Z""",
    re.VERBOSE,
)
_SCHEME_RE: Final = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


@dataclass(frozen=True, slots=True)
class Challenge:
    """One authentication challenge."""

    #: Lower-cased, because RFC 9110 says the scheme is case-insensitive and
    #: servers are inconsistent about it ("Bearer", "bearer", "BEARER").
    scheme: str
    params: Mapping[str, str] = MappingProxyType({})

    @property
    def realm(self) -> str | None:
        return self.params.get("realm")

    @property
    def resource_metadata(self) -> str | None:
        """RFC 9728 pointer to the protected-resource metadata document."""
        return self.params.get("resource_metadata")

    @property
    def error(self) -> str | None:
        """RFC 6750 ``error`` code: ``invalid_token``, ``insufficient_scope``..."""
        return self.params.get("error")


def _split_fragments(value: str) -> list[str]:
    """Split on commas that are not inside a quoted string."""
    fragments: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and in_quotes:
            current.append(char)
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "," and not in_quotes:
            fragments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    fragments.append("".join(current).strip())
    return [f for f in fragments if f]


def _unquote(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_challenges(values: Iterable[str]) -> tuple[Challenge, ...]:
    """Parse every ``WWW-Authenticate`` header value into challenges.

    Takes an iterable because a server may send the header more than once -
    Stalwart sends two, one for Bearer and one for Basic - and
    ``httpx.Headers.get_list`` is the right way to read them.

    Anything unparseable is skipped rather than raised on. A malformed challenge
    is the server's bug, and failing the whole request over it would turn a
    recoverable 401 into a crash.
    """
    challenges: list[Challenge] = []
    pending_scheme: str | None = None
    pending_params: dict[str, str] = {}

    def flush() -> None:
        nonlocal pending_scheme, pending_params
        if pending_scheme is not None:
            challenges.append(Challenge(pending_scheme, MappingProxyType(dict(pending_params))))
        pending_scheme, pending_params = None, {}

    for value in values:
        for fragment in _split_fragments(value):
            scheme, _, remainder = fragment.partition(" ")
            remainder = remainder.strip()

            if remainder and _SCHEME_RE.match(scheme):
                # "Bearer realm=x" - a scheme followed by its first parameter.
                flush()
                pending_scheme = scheme.lower()
                match = _PARAM_RE.match(remainder)
                if match is not None:
                    quoted, bare = match.group("quoted"), match.group("bare")
                    pending_params[match.group("name").lower()] = (
                        _unquote(quoted) if quoted is not None else (bare or "")
                    )
                continue

            match = _PARAM_RE.match(fragment)
            if match is not None and pending_scheme is not None:
                # A continuation parameter for the challenge being built.
                quoted, bare = match.group("quoted"), match.group("bare")
                pending_params[match.group("name").lower()] = (
                    _unquote(quoted) if quoted is not None else (bare or "")
                )
            elif _SCHEME_RE.match(fragment):
                # A bare scheme with no parameters at all.
                flush()
                pending_scheme = fragment.lower()

    flush()
    return tuple(challenges)


def find_challenge(challenges: Iterable[Challenge], scheme: str) -> Challenge | None:
    """The first challenge for ``scheme``, compared case-insensitively."""
    wanted = scheme.lower()
    return next((c for c in challenges if c.scheme == wanted), None)

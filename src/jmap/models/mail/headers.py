"""Requesting parsed header fields (RFC 8621 §4.1.2).

A header is fetched by asking for a *property whose name encodes the request*:
``header:Subject:asText:all``. That makes it unlike every other property, and
creates a trap the spec calls out explicitly - the server echoes the property
name back **exactly as sent**, including capitalisation. Ask for
``header:subject`` and the answer is keyed ``header:subject``; ask for
``header:Subject`` and it is keyed ``header:Subject``. Code that requests one
spelling and reads another silently sees ``None``.

:class:`HeaderQuery` therefore owns both halves: the string that goes out and the
key that comes back are the same object, so they cannot drift.

Two more rules are easy to get wrong:

* **Suffix order is fixed.** ``:as{Form}`` comes before ``:all``. Reversing them
  is not a different request, it is an invalid property name.
* **Without ``:all`` you get the *last* occurrence**, not the first and not a
  list - and ``null`` when the header is absent. For ``Received``, which appears
  once per hop, the last one is the *earliest* hop, which surprises people.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jmap.models.mail.objects import Email

#: RFC 5322 §3.6.8 field name: printable US-ASCII except colon.
_FIELD_NAME_RE: Final = re.compile(r"\A[\x21-\x39\x3b-\x7e]+\Z")


class HeaderForm(StrEnum):
    """How the server should parse a header value (RFC 8621 §4.1.2).

    The server returns ``null`` when a value cannot be parsed in the requested
    form, so asking for ``asDate`` on a non-date header is not an error - it is a
    quiet ``None``, which is worth knowing before debugging one.
    """

    #: The raw octets after the colon, unfolded but not decoded.
    RAW = "asRaw"
    #: Decoded, unfolded, RFC 2047 words expanded, whitespace collapsed.
    TEXT = "asText"
    #: A list of ``EmailAddress``.
    ADDRESSES = "asAddresses"
    #: A list of ``EmailAddressGroup``, preserving RFC 5322 group syntax.
    GROUPED_ADDRESSES = "asGroupedAddresses"
    #: A list of message ids with the angle brackets removed.
    MESSAGE_IDS = "asMessageIds"
    #: A ``UTCDate``.
    DATE = "asDate"
    #: A list of URLs, as in ``List-Unsubscribe``.
    URLS = "asURLs"


class InvalidHeaderQueryError(ValueError):
    """A header property name that the server would reject."""


@dataclass(frozen=True, slots=True)
class HeaderQuery:
    """One ``header:*`` property: what to request, and how to read it back."""

    name: str
    form: HeaderForm = HeaderForm.RAW
    #: Return every occurrence as a list, rather than only the last one.
    all: bool = False

    def __post_init__(self) -> None:
        if not _FIELD_NAME_RE.match(self.name):
            raise InvalidHeaderQueryError(
                f"{self.name!r} is not a valid RFC 5322 field name: it must be one or "
                f"more printable US-ASCII characters other than ':'"
            )

    @property
    def property_name(self) -> str:
        """The property to request - and the key the response will use.

        RFC 8621 §4.1.2 fixes the suffix order as form then ``:all``.
        """
        parts = ["header", self.name]
        if self.form is not HeaderForm.RAW:
            # The raw form is the default and is spelled by omission, so
            # `header:Name:all` is the correct raw-plus-all property name.
            parts.append(self.form.value)
        if self.all:
            parts.append("all")
        return ":".join(parts)

    def read(self, email: Email) -> Any:
        """Pull this header's value out of a fetched email.

        Uses :attr:`property_name` as the key, which is the whole point: the
        request and the lookup cannot disagree about capitalisation.
        """
        return email.header(self.property_name)

    def __str__(self) -> str:
        return self.property_name


def raw(name: str, *, all: bool = False) -> HeaderQuery:  # `all` mirrors the wire flag
    """``header:{name}`` - undecoded octets."""
    return HeaderQuery(name, HeaderForm.RAW, all)


def text(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asText`` - decoded and unfolded."""
    return HeaderQuery(name, HeaderForm.TEXT, all)


def addresses(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asAddresses``."""
    return HeaderQuery(name, HeaderForm.ADDRESSES, all)


def grouped_addresses(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asGroupedAddresses``."""
    return HeaderQuery(name, HeaderForm.GROUPED_ADDRESSES, all)


def message_ids(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asMessageIds``."""
    return HeaderQuery(name, HeaderForm.MESSAGE_IDS, all)


def date(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asDate``."""
    return HeaderQuery(name, HeaderForm.DATE, all)


def urls(name: str, *, all: bool = False) -> HeaderQuery:
    """``header:{name}:asURLs``."""
    return HeaderQuery(name, HeaderForm.URLS, all)


def property_names(queries: Iterable[HeaderQuery]) -> list[str]:
    """The property strings for a set of header queries, for ``Email/get``."""
    return [query.property_name for query in queries]

"""JMAP ``Id`` and creation-reference handling (RFC 8620 §1.2, §5.3).

An ``Id`` is assigned by the server and is opaque to the client, but the charset
is constrained tightly enough that we can reject malformed ids locally rather
than paying a round trip to learn the server disagrees.
"""

from __future__ import annotations

import re
from typing import Final, NewType

#: An opaque server-assigned record identifier.
#:
#: ``NewType`` rather than a subclass: ids are compared, hashed and serialised as
#: plain strings everywhere, and a subclass would silently degrade to ``str`` on
#: every concatenation anyway.
Id = NewType("Id", str)

#: RFC 8620 §1.2: 1-255 octets from the URL-and-filename-safe base64 alphabet
#: (RFC 4648 §5) excluding the pad character - i.e. ``A-Za-z0-9``, ``-`` and ``_``.
_ID_RE: Final = re.compile(r"\A[A-Za-z0-9_-]{1,255}\Z")

MAX_ID_OCTETS: Final = 255

#: RFC 8620 §5.3 reserves a leading ``#`` to reference a creation id. A literal
#: id may therefore never begin with it.
CREATION_PREFIX: Final = "#"


class InvalidIdError(ValueError):
    """A string was used as an ``Id`` but cannot be one."""

    def __init__(self, value: str, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"invalid JMAP Id {value!r}: {reason}")


def is_valid_id(value: str) -> bool:
    """Return whether ``value`` is a well-formed JMAP ``Id``."""
    return bool(_ID_RE.match(value))


def parse_id(value: str) -> Id:
    """Validate ``value`` as an ``Id``, raising :class:`InvalidIdError` if not.

    The error distinguishes the failure modes because they mean different things
    to a caller: an over-long id usually means a bug in id construction, whereas
    a ``#`` prefix almost always means a creation reference leaked into a slot
    that wanted a real id.
    """
    if not value:
        raise InvalidIdError(value, "must be at least 1 octet")
    if value.startswith(CREATION_PREFIX):
        raise InvalidIdError(value, "leading '#' is reserved for creation references")
    if len(value.encode("utf-8")) > MAX_ID_OCTETS:
        raise InvalidIdError(value, f"exceeds {MAX_ID_OCTETS} octets")
    if not is_valid_id(value):
        raise InvalidIdError(value, "contains characters outside [A-Za-z0-9_-]")
    return Id(value)


class CreationRef:
    """A reference to an object created earlier in the same request.

    Serialises as ``#<creation_id>``. Keeping this a distinct type rather than a
    bare ``"#foo"`` string means a creation reference can never be mistaken for a
    server-assigned id by the type checker, which is the bug this class exists to
    prevent.
    """

    __slots__ = ("creation_id",)

    creation_id: Id

    def __init__(self, creation_id: str) -> None:
        # A creation id must itself be a valid Id: the server echoes it back in
        # `createdIds`, where the Id charset applies.
        self.creation_id = parse_id(creation_id)

    def __str__(self) -> str:
        return f"{CREATION_PREFIX}{self.creation_id}"

    def __repr__(self) -> str:
        return f"CreationRef({self.creation_id!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CreationRef) and other.creation_id == self.creation_id

    def __hash__(self) -> int:
        return hash((CreationRef, self.creation_id))

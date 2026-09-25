"""Anniversaries, notes and personal details (RFC 9553 §2.8)."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import Discriminator, Tag

from jmap.models.jscontact.contact import Address
from jmap.models.jsdates import UTCDateTime
from jmap.models.jsobject import JSObject, type_tag


class PartialDate(JSObject):
    """A date that may lack parts: a birthday with no year (§2.8.1)."""

    year: int | None = None
    month: int | None = None
    day: int | None = None
    #: A CLDR calendar name, e.g. ``"gregory"``; needed for non-Gregorian dates.
    calendar_scale: str | None = None


class Timestamp(JSObject):
    """An exact instant (§2.8.1). Always sent with its ``@type``: dates default
    to :class:`PartialDate`, so one without it would be read as one."""

    REQUIRED_TYPE: ClassVar[str] = "Timestamp"

    utc: UTCDateTime | None = None


def _date_type(value: Any) -> str:
    if isinstance(value, JSObject):
        return type(value).__name__
    return "Timestamp" if type_tag(value) == "Timestamp" else "PartialDate"


#: ``PartialDate|Timestamp``, told apart by ``@type`` (§2.8.1).
AnniversaryDate: TypeAlias = Annotated[
    Annotated[PartialDate, Tag("PartialDate")] | Annotated[Timestamp, Tag("Timestamp")],
    Discriminator(_date_type),
]


class Anniversary(JSObject):
    """A birthday or other memorable date (§2.8.1).

    ``kind`` is ``birth``, ``death`` or ``wedding``.
    """

    kind: str | None = None
    date: AnniversaryDate | None = None
    place: Address | None = None


class Author(JSObject):
    """Who wrote a note (§2.8.3)."""

    name: str | None = None
    uri: str | None = None


class Note(JSObject):
    """A free-text note about the entity (§2.8.3)."""

    note: str | None = None
    created: UTCDateTime | None = None
    author: Author | None = None


class PersonalInfo(JSObject):
    """An expertise, hobby or interest (§2.8.4)."""

    #: ``expertise``, ``hobby`` or ``interest``.
    kind: str | None = None
    value: str | None = None
    #: ``high``, ``medium`` or ``low``.
    level: str | None = None
    list_as: int | None = None
    label: str | None = None

"""Reminders (draft-ietf-calext-jscalendarbis §3.5)."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import Discriminator, Tag

from jmap.models.jscalendar.links import Relation
from jmap.models.jsobject import JSObject, type_tag


class OffsetTrigger(JSObject):
    """Fire at a time relative to the event: ``-PT15M`` is 15 minutes before."""

    #: A SignedDuration.
    offset: str | None = None
    #: ``start`` (the default) or ``end``.
    relative_to: str | None = None


class AbsoluteTrigger(JSObject):
    """Fire at a fixed instant. Always sent with its ``@type``, since a trigger
    without one is an :class:`OffsetTrigger`."""

    REQUIRED_TYPE: ClassVar[str] = "AbsoluteTrigger"

    #: A UTCDateTime.
    when: str | None = None


class UnknownTrigger(JSObject):
    """A trigger of a type this build does not know, kept as it came."""


def _trigger_type(value: Any) -> str:
    if isinstance(value, JSObject):
        return type(value).__name__
    tag = type_tag(value)
    if tag in (None, "OffsetTrigger", "AbsoluteTrigger"):
        return tag or "OffsetTrigger"
    return "UnknownTrigger"


#: ``OffsetTrigger|AbsoluteTrigger|UnknownTrigger``, told apart by ``@type``.
Trigger: TypeAlias = Annotated[
    Annotated[OffsetTrigger, Tag("OffsetTrigger")]
    | Annotated[AbsoluteTrigger, Tag("AbsoluteTrigger")]
    | Annotated[UnknownTrigger, Tag("UnknownTrigger")],
    Discriminator(_trigger_type),
]


class Alert(JSObject):
    """A reminder (§3.5.1)."""

    trigger: Trigger | None = None
    #: When the user dismissed it, as a UTCDateTime.
    acknowledged: str | None = None
    #: Alert id -> relation; a snoozed alert points at the one it snoozes.
    related_to: dict[str, Relation] | None = None
    #: ``display`` (the default) or ``email``.
    action: str | None = None

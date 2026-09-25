"""The Group (draft-ietf-calext-jscalendarbis §2.3, §4.3)."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import Discriminator, Tag

from jmap.models.jscalendar.common import CalendarObject
from jmap.models.jscalendar.event import Event
from jmap.models.jscalendar.task import Task
from jmap.models.jsobject import JSObject, type_tag


class UnknownEntry(JSObject):
    """A group entry of a type this build does not know, kept as it came.

    §4.3.1 has implementations ignore these; keeping one is what lets the
    group go back unchanged.
    """


def _entry_type(value: Any) -> str:
    if isinstance(value, JSObject):
        return type(value).__name__
    tag = type_tag(value)
    return tag if tag in ("Event", "Task") else "UnknownEntry"


#: ``Task|Event``, told apart by ``@type``, which a group's entries must carry.
GroupEntry: TypeAlias = Annotated[
    Annotated[Event, Tag("Event")]
    | Annotated[Task, Tag("Task")]
    | Annotated[UnknownEntry, Tag("UnknownEntry")],
    Discriminator(_entry_type),
]


class Group(CalendarObject):
    """A collection of Events and Tasks, grouped by topic or calendar (§2.3)."""

    REQUIRED_TYPE: ClassVar[str] = "Group"

    entries: list[GroupEntry] | None = None
    #: A URI to fetch newer versions of the group from.
    source: str | None = None

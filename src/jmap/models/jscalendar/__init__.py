"""JSCalendar 2.0 (draft-ietf-calext-jscalendarbis-20): Event, Task and Group.

The revision JMAP Calendars builds on, and not RFC 8984: a single
``recurrenceRule``, ``organizerCalendarAddress`` for ``replyTo``, and a
Participant's ``calendarAddress`` for ``sendTo``. A JMAP CalendarEvent
(:class:`jmap.models.calendars.CalendarEvent`) is an Event with JMAP's
properties added. Every object is typed and forgiving alike - see
:mod:`jmap.models.jsobject`.
"""

from __future__ import annotations

from jmap.models.jscalendar.alerts import (
    AbsoluteTrigger,
    Alert,
    OffsetTrigger,
    Trigger,
    UnknownTrigger,
)
from jmap.models.jscalendar.common import CalendarObject, Entry
from jmap.models.jscalendar.event import Event
from jmap.models.jscalendar.group import Group, GroupEntry, UnknownEntry
from jmap.models.jscalendar.links import Link, Relation
from jmap.models.jscalendar.locations import Location, VirtualLocation
from jmap.models.jscalendar.participants import Participant
from jmap.models.jscalendar.recurrence import NDay, RecurrenceRule
from jmap.models.jscalendar.task import Task

__all__ = [
    "AbsoluteTrigger",
    "Alert",
    "CalendarObject",
    "Entry",
    "Event",
    "Group",
    "GroupEntry",
    "Link",
    "Location",
    "NDay",
    "OffsetTrigger",
    "Participant",
    "RecurrenceRule",
    "Relation",
    "Task",
    "Trigger",
    "UnknownEntry",
    "UnknownTrigger",
    "VirtualLocation",
]

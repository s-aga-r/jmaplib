"""The Event (draft-ietf-calext-jscalendarbis §2.1, §4.1)."""

from __future__ import annotations

from typing import ClassVar

from jmap.models.jscalendar.common import Entry


class Event(Entry):
    """A scheduled amount of time on a calendar (§2.1).

    ``start`` is mandatory in JSCalendar: a LocalDateTime, read in ``timeZone``.
    An Event always carries its ``@type``; a JMAP CalendarEvent leaves it to
    the server.
    """

    REQUIRED_TYPE: ClassVar[str] = "Event"

    start: str | None = None
    #: A Duration, ``PT0S`` by default.
    duration: str | None = None
    end_time_zone: str | None = None
    #: ``confirmed`` (the default), ``cancelled`` or ``tentative``.
    status: str | None = None

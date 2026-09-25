"""What JSCalendar objects share (draft-ietf-calext-jscalendarbis §3)."""

from __future__ import annotations

from typing import Any

from jmap.models.jscalendar.alerts import Alert
from jmap.models.jscalendar.links import Link, Relation
from jmap.models.jscalendar.locations import Location, VirtualLocation
from jmap.models.jscalendar.participants import Participant
from jmap.models.jscalendar.recurrence import RecurrenceRule
from jmap.models.jsobject import JSObject, wire_property


class CalendarObject(JSObject):
    """What any JSCalendar object - Event, Task or Group - may carry (§4.3).

    Every property is optional here, though ``uid`` and ``updated`` are
    mandatory in JSCalendar. Dates are strings in JSCalendar's own forms.
    """

    uid: str | None = None
    #: ``"2.0"``. Set on a Group and on an Event or Task standing alone, never
    #: on a Group's entries.
    version: str | None = None
    prod_id: str | None = None
    created: str | None = None
    updated: str | None = None
    title: str | None = None
    description: str | None = None
    description_content_type: str | None = None
    links: dict[str, Link] | None = None
    locale: str | None = None
    keywords: dict[str, bool] | None = None
    categories: dict[str, bool] | None = None
    color: str | None = None

    def jscalendar(self, name: str) -> Any:
        """Read a JSCalendar property by its exact wire name, in its wire form.

        For a vendor property, or one this library models but that arrived in
        a shape it could not read - both kept as they came.
        """
        return wire_property(self, name)


class Entry(CalendarObject):
    """An Event or a Task - what a Group's entries are - and what they share (§3)."""

    # -- metadata (§3.1) ---------------------------------------------------- #
    #: uid of another object -> how it relates to this one.
    related_to: dict[str, Relation] | None = None
    sequence: int | None = None
    #: Only on an iTIP message; a JMAP CalendarEvent never has one.
    method: str | None = None

    # -- what and where (§3.2) ---------------------------------------------- #
    #: All-day: the time is not worth showing.
    show_without_time: bool | None = None
    locations: dict[str, Location] | None = None
    #: A key of ``locations``.
    main_location_id: str | None = None
    virtual_locations: dict[str, VirtualLocation] | None = None

    # -- recurrence (§3.3) -------------------------------------------------- #
    #: Set on one occurrence of a recurring object, never with the two below.
    recurrence_id: str | None = None
    recurrence_id_time_zone: str | None = None
    recurrence_rule: RecurrenceRule | None = None
    #: LocalDateTime -> a PatchObject for that occurrence, or
    #: ``{"excluded": True}`` to drop it. See
    #: :func:`jmap.models.calendars.check_recurrence_id` for the keys.
    recurrence_overrides: dict[str, dict[str, Any]] | None = None

    # -- sharing and scheduling (§3.4) -------------------------------------- #
    #: 0 is undefined; 1 is highest and 9 lowest.
    priority: int | None = None
    #: ``busy`` (the default) or ``free``.
    free_busy_status: str | None = None
    #: ``public`` (the default), ``private`` or ``secret``.
    privacy: str | None = None
    #: The organizer's scheduling URI - RFC 8984's ``replyTo``, reshaped.
    organizer_calendar_address: str | None = None
    participants: dict[str, Participant] | None = None

    # -- alerts (§3.5) and time zone (§3.6) --------------------------------- #
    alerts: dict[str, Alert] | None = None
    #: An IANA time zone name; null makes the object floating.
    time_zone: str | None = None

    # -- draft-ietf-jmap-calendars §5.1: set only on the base object -------- #
    #: Anyone may add themselves as an attendee.
    may_invite_self: bool | None = None
    #: Attendees may invite other attendees.
    may_invite_others: bool | None = None
    #: Only owners see every participant.
    hide_attendees: bool | None = None

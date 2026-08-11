"""Calendars (draft-ietf-jmap-calendars-27, JSCalendar).

Experimental: this tracks an Internet-Draft that is itself blocked on another
draft, so it is excluded from the SemVer promise and only resolved when the caller
opts in. ``jmap.SPEC_REVISIONS`` publishes the exact revision this build targets.

**The event body is JSCalendar 2.0 (jscalendarbis), not RFC 8984.** They differ in
ways that silently corrupt data rather than failing: ``recurrenceRules`` (an array)
became ``recurrenceRule`` (one object), ``replyTo`` became
``organizerCalendarAddress`` with a different *type*, ``Participant.sendTo`` became
``calendarAddress`` - while ``Participant.email`` survives unchanged, contrary to
what is often assumed. Because the target is still moving, JSCalendar content is
carried losslessly through ``extra`` rather than modelled field by field; what is
modelled here is the JMAP layer, where the traps are.

Three of those traps are worth stating up front.

**``recurrenceOverrides`` is keyed by a naive local date-time.** Not a UTC instant:
no ``Z``, no offset, and in jscalendarbis no fractional seconds either. Rendering
an aware datetime produces ``...+11:00``, the key matches nothing, and - worse
than an error - the server accepts it as an *additional* occurrence.
:func:`check_recurrence_id` is the guard; there is no separate local-datetime type
yet, so the check is a runtime one.

**``utcStart``/``utcEnd`` are computed, not stored.** They are absent unless asked
for, must not be requested alongside ``recurrenceOverrides``, and may differ
between two identical requests if the tz database moves - a change that is
explicitly *not* an update, so ``/changes`` will never mention it.

**An expanded occurrence looks like a standalone event.** ``/get`` on a synthetic
instance id returns ``recurrenceRule`` and ``recurrenceOverrides`` as ``null``.
Writing that back wipes the series.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic import Field

from jmap.models.base import JMAPModel

#: draft-27 §5.11. ``expandRecurrences`` on a query whose window is too wide.
EXPAND_DURATION_TOO_LARGE: Final = "expandDurationTooLarge"
#: §5.11. The server cannot work out a recurrence the query needs.
CANNOT_CALCULATE_OCCURRENCES: Final = "cannotCalculateOccurrences"
#: §4.3. Destroying a calendar that still holds events.
CALENDAR_HAS_EVENT: Final = "calendarHasEvent"
#: §5.9. Asked to send invitations with no usable method for some recipient.
NO_SUPPORTED_SCHEDULE_METHODS: Final = "noSupportedScheduleMethods"

#: §4. Whether a calendar's events count towards free/busy.
AVAILABILITY_ALL: Final = "all"
AVAILABILITY_ATTENDING: Final = "attending"
AVAILABILITY_NONE: Final = "none"

#: §2.2. Precedence when merging overlapping busy periods - deliberately not
#: alphabetical, and ``unavailable`` outranks ``tentative``.
BUSY_PRECEDENCE: Final = ("confirmed", "unavailable", "tentative")

#: §2.2. What a BusyPeriod means when it names no status.
DEFAULT_BUSY_STATUS: Final = "unavailable"

#: A JSCalendar LocalDateTime: ``YYYY-MM-DDThh:mm:ss`` with no zone and, since
#: jscalendarbis, no fractional seconds.
_LOCAL_DATE_TIME: Final = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")


class InvalidRecurrenceIdError(ValueError):
    """A recurrence override key that is not a bare local date-time.

    Almost always an aware ``datetime`` rendered with ``isoformat()``: the
    trailing offset makes the key match no occurrence, and the server accepts the
    override as an *extra* one rather than rejecting it.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(
            f"{value!r} is not a JSCalendar LocalDateTime; a recurrence id is "
            f"YYYY-MM-DDThh:mm:ss with no offset, no 'Z' and no fractional seconds"
        )


def check_recurrence_id(value: str) -> str:
    """Validate a ``recurrenceOverrides`` key, returning it unchanged."""
    if not _LOCAL_DATE_TIME.match(value):
        raise InvalidRecurrenceIdError(value)
    return value


class CalendarRights(JMAPModel):
    """What the requesting user may do with a calendar (draft-27 §4).

    ``mayWriteAll`` implies ``mayWriteOwn``, ``mayUpdatePrivate`` and ``mayRSVP``;
    a server is required to report all four together.
    """

    may_read_free_busy: bool = False
    may_read_items: bool = False
    may_write_all: bool = False
    may_write_own: bool = False
    may_update_private: bool = False
    #: Aliased explicitly: the draft spells it ``mayRSVP``, and the camelCase
    #: generator produces ``mayRsvp`` - which reads as "the user may not RSVP" to
    #: every server, since the real key is simply absent.
    may_rsvp: bool = Field(default=False, alias="mayRSVP")
    may_share: bool = False
    may_delete: bool = False


class Calendar(JMAPModel):
    """A calendar (draft-ietf-jmap-calendars-27 §4)."""

    id: str | None = None
    #: Must not be empty, and is bounded at 255 octets as UTF-8.
    name: str | None = None
    description: str | None = None
    #: A CSS3 colour name or hex RGB.
    color: str | None = None
    sort_order: int | None = None
    is_subscribed: bool | None = None
    #: Clients must ignore this when ``is_subscribed`` is false.
    is_visible: bool | None = None
    #: Server-set, and true for at most one calendar per account.
    is_default: bool | None = None
    #: ``all``, ``attending`` or ``none``.
    include_in_availability: str | None = None
    #: Alert id -> Alert. Triggers here must be relative, never absolute, and the
    #: ids are unique across the whole *account* rather than per calendar.
    default_alerts_with_time: dict[str, dict[str, Any]] | None = None
    default_alerts_without_time: dict[str, dict[str, Any]] | None = None
    #: ``None`` falls back to the owning Principal's zone.
    time_zone: str | None = None
    share_with: dict[str, CalendarRights] | None = None
    #: Server-set.
    my_rights: CalendarRights | None = None


class ParticipantIdentity(JMAPModel):
    """An address the user schedules as (draft-27 §3).

    Matching a Participant to an identity compares ``calendarAddress`` after
    RFC 3986 §6.2.2 syntax normalisation, not as raw strings - so
    ``MAILTO:Alice@Example.com`` and ``mailto:alice@example.com`` are the same
    identity.
    """

    id: str | None = None
    name: str | None = None
    calendar_address: str | None = None
    #: Server-set, and true for at most one identity per account.
    is_default: bool | None = None


class CalendarEvent(JMAPModel):
    """A calendar event (draft-27 §5).

    A JSCalendar ``Event`` plus the JMAP-layer properties below. The JSCalendar
    body rides in ``extra`` and round-trips losslessly - see the module docstring
    for why it is not modelled field by field yet.
    """

    id: str | None = None
    #: Server-set, immutable, and non-null only on a synthetic expanded instance.
    #: Its presence is how you tell an occurrence from a stored event.
    base_event_id: str | None = None
    #: Calendar id -> True. Must be non-empty at all times; patching it to ``{}``
    #: is invalid rather than a delete.
    calendar_ids: dict[str, bool] | None = None
    #: Settable true only on create, and never afterwards. Must not appear in
    #: ``recurrenceOverrides``.
    is_draft: bool | None = None
    #: Server-set. When false, a client must not edit anything but the per-user
    #: properties even if ``myRights`` would allow it - the next inbound iTIP
    #: request overwrites the rest.
    is_origin: bool | None = None
    #: Computed at fetch time, and absent unless explicitly requested.
    utc_start: str | None = None
    utc_end: str | None = None
    use_default_alerts: bool | None = None

    @property
    def is_occurrence(self) -> bool:
        """Whether this is one expanded instance rather than a stored event."""
        return self.base_event_id is not None

    @property
    def calendars(self) -> list[str]:
        """The calendars this event is in, as a plain list."""
        return [key for key, value in (self.calendar_ids or {}).items() if value]

    def jscalendar(self, name: str) -> Any:
        """Read a JSCalendar property by its exact wire name.

        ``event.jscalendar("recurrenceRule")``. They live in ``extra`` because the
        target revision is still moving; reading them by name is stable across
        that churn in a way generated fields would not be.
        """
        return (self.__pydantic_extra__ or {}).get(name)


class BusyPeriod(JMAPModel):
    """One busy span from ``Principal/getAvailability`` (draft-27 §2.2).

    ``utc_start`` is inclusive and ``utc_end`` exclusive, on both the request and
    each period returned.
    """

    utc_start: str | None = None
    utc_end: str | None = None
    #: Absent means ``unavailable`` - not unknown, and not free.
    busy_status: str | None = None
    event: CalendarEvent | None = None
    #: ``None`` whenever ``event`` is, since there is then no account to name.
    account_id: str | None = None

    @property
    def status(self) -> str:
        """The busy status, resolving the absent case to its default."""
        return self.busy_status or DEFAULT_BUSY_STATUS


class AvailabilityResponse(JMAPModel):
    """``Principal/getAvailability`` (draft-27 §2.2).

    Not a ``/get`` despite the name: it takes one ``id`` rather than ``ids`` and
    answers with a bare ``list`` - no ``state``, no ``notFound``, nothing to sync
    against. Re-polling is the only way to refresh it.
    """

    items: list[BusyPeriod] = Field(default_factory=lambda: [], alias="list")


class CalendarEventNotification(JMAPModel):
    """Someone else changed an event you can see (draft-27 §7).

    ``event`` holds the state **before** the change for ``updated`` and
    ``destroyed``, and after it for ``created``. Rendering it as "the new event"
    is a plausible, silent, and completely wrong reading; ``eventPatch`` carries
    the delta and exists only for ``updated``.
    """

    id: str | None = None
    created: str | None = None
    changed_by: dict[str, Any] | None = None
    comment: str | None = None
    #: ``created``, ``updated`` or ``destroyed``.
    type: str | None = None
    #: Always the *base* event, even when one occurrence changed.
    calendar_event_id: str | None = None
    is_draft: bool | None = None
    event: dict[str, Any] | None = None
    #: Only present for ``updated``.
    event_patch: dict[str, Any] | None = None

    @property
    def describes_the_state_before(self) -> bool:
        """Whether ``event`` is the *old* state (updated and destroyed)."""
        return self.type in {"updated", "destroyed"}


class ParsedEvents(JMAPModel):
    """``CalendarEvent/parse`` (draft-27 §5.13).

    ``parsed`` maps a blob id to an **array** of events, because one iCalendar
    file holds many VEVENTs. Reading it as one event per blob is the shape most
    easily got wrong here.
    """

    account_id: str | None = None
    parsed: dict[str, list[CalendarEvent]] | None = None
    not_found: list[str] | None = None
    not_parsable: list[str] | None = None

    def events_of(self, blob_id: str) -> list[CalendarEvent]:
        """Every event parsed out of one blob, empty if it yielded none."""
        return (self.parsed or {}).get(blob_id, [])

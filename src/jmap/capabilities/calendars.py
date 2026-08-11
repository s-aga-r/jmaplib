"""``urn:ietf:params:jmap:calendars`` and companions - draft-ietf-jmap-calendars-27.

Experimental, and more so than FileNode: this draft is in the RFC Editor queue but
*blocked* on ``draft-ietf-calext-jscalendarbis``, which is itself still moving. Its
own normative reference is already a revision behind. Expect wire names to change,
and read ``jmap.SPEC_REVISIONS`` for exactly what this build targets.

Three URNs, and they are separate for a reason:

* ``:calendars`` - the data types and their methods, except parsing.
* ``:calendars:parse`` - ``CalendarEvent/parse`` alone, which servers may omit.
* ``:principals:availability`` - ``Principal/getAvailability``, which lives here
  rather than in RFC 9670. RFC 9670 defines *no* custom methods at all, so a
  client looking for availability in the sharing spec will not find it.

The capability object bounds two requests hard, and both fail the whole call
rather than degrading:

**``maxExpandedQueryDuration``** caps a recurrence-expanding query. Exceed it and
``CalendarEvent/query`` answers ``expandDurationTooLarge`` - a *method*-level
error, so nothing else comes back either. It is an ISO-8601 duration string, not a
number, so a client that wants to pre-empt it has to parse one.

**``maxAvailabilityDuration``** caps ``Principal/getAvailability`` the same way,
with ``tooLarge``.
"""

from __future__ import annotations

import re
from typing import Any, Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import LimitKey
from jmap.core.narrow import as_object, is_object
from jmap.models.base import JMAPModel
from jmap.models.calendars import (
    AvailabilityResponse,
    Calendar,
    CalendarEvent,
    CalendarEventNotification,
    ParsedEvents,
    ParticipantIdentity,
)

CALENDARS_URN: Final = "urn:ietf:params:jmap:calendars"
CALENDARS_PARSE_URN: Final = "urn:ietf:params:jmap:calendars:parse"
AVAILABILITY_URN: Final = "urn:ietf:params:jmap:principals:availability"

#: draft-27 §6.4. A push pseudo-type, and the EventSource event name differs from
#: it in case - ``calendarAlert`` on the wire, ``CalendarAlert`` as a type.
CALENDAR_ALERT_TYPE: Final = "CalendarAlert"
CALENDAR_ALERT_EVENT: Final = "calendarAlert"

#: JSCalendar's ``Duration`` is ``P (dur-date / dur-time / dur-week)``: weeks are
#: mutually exclusive with days and times, so they get their own pattern rather
#: than being another optional group that would let ``P1W1D`` through.
_WEEKS: Final = re.compile(r"\AP(?P<weeks>\d+)W\Z")

#: The ``(?=\d)`` after ``T`` is load-bearing: without it ``PT`` and ``P1DT``
#: match with every time component absent, and a dangling designator then sums to
#: zero - which reads as a real limit of zero seconds rather than as unparseable.
_DATE_TIME: Final = re.compile(
    r"\AP(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?\Z"
)

_SECONDS: Final = {
    "weeks": 604800,
    "days": 86400,
    "hours": 3600,
    "minutes": 60,
    "seconds": 1,
}


def duration_seconds(value: str) -> int | None:
    """Turn a JSCalendar ``Duration`` into seconds, or ``None`` if it is not one.

    Years and months are refused rather than approximated: they have no fixed
    length, so a client comparing a query window against ``P1Y`` cannot get a
    right answer, and guessing 365 days would make the check silently wrong near
    the boundary.
    """
    week = _WEEKS.match(value)
    if week is not None:
        return int(week["weeks"]) * _SECONDS["weeks"]
    match = _DATE_TIME.match(value)
    if match is None:
        return None
    parts = {unit: found for unit, found in match.groupdict().items() if found}
    if not parts:
        # A bare "P" names no quantity at all. Returning zero here would cap every
        # query at zero seconds rather than declining to check.
        return None
    return sum(int(found) * _SECONDS[unit] for unit, found in parts.items())


class CalendarsCapability(JMAPModel):
    """The per-account ``:calendars`` object (draft-27 §1.5.1)."""

    #: How many calendars one event may be in. ``None`` means no limit.
    max_calendars_per_event: int | None = None
    #: The earliest and latest date-times the server will store.
    min_date_time: str | None = None
    max_date_time: str | None = None
    #: An ISO-8601 duration. Bounds a recurrence-expanding query - see the module
    #: docstring.
    max_expanded_query_duration: str | None = None
    max_participants_per_event: int | None = None
    may_create_calendar: bool = False

    @classmethod
    def of(cls, value: Any) -> CalendarsCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


class AvailabilityCapability(JMAPModel):
    """The per-account ``:principals:availability`` object (draft-27 §1.5.2)."""

    #: An ISO-8601 duration bounding one ``Principal/getAvailability`` call.
    max_availability_duration: str | None = None

    @classmethod
    def of(cls, value: Any) -> AvailabilityCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def check_expand_window(seconds: float, capability: CalendarsCapability) -> None:
    """Refuse a recurrence expansion wider than the server will compute (§5.11).

    Checked locally because the failure is method-level: the query returns
    ``expandDurationTooLarge`` and *no* results, so a client that discovers the
    limit by hitting it has wasted the round trip entirely.
    """
    limit = capability.max_expanded_query_duration
    if limit is None:
        return
    bound = duration_seconds(limit)
    if bound is not None and seconds > bound:
        raise CapabilityFieldError(CALENDARS_URN, "maxExpandedQueryDuration", limit, seconds)


def check_availability_window(seconds: float, capability: AvailabilityCapability) -> None:
    """Refuse an availability window wider than the server will compute (§2.2)."""
    limit = capability.max_availability_duration
    if limit is None:
        return
    bound = duration_seconds(limit)
    if bound is not None and seconds > bound:
        raise CapabilityFieldError(AVAILABILITY_URN, "maxAvailabilityDuration", limit, seconds)


def check_expand_filter(filter_: Any, *, expand: bool) -> None:
    """Enforce §5.11's two rules for a recurrence-expanding query.

    With ``expandRecurrences`` the filter MUST be a bare FilterCondition - no
    ``AND``/``OR``/``NOT`` wrapper at all, not merely none at the top - and it
    MUST carry both ``before`` and ``after``. Both are client errors, so catching
    them here costs nothing and saves a round trip that could not have worked.
    """
    if not expand:
        return
    if not is_object(filter_):
        raise CapabilityFieldError(
            CALENDARS_URN, "filter", "a FilterCondition", type(filter_).__name__
        )
    condition = as_object(filter_)
    if "operator" in condition:
        raise CapabilityFieldError(
            CALENDARS_URN,
            "filter",
            "a bare FilterCondition (expandRecurrences forbids any FilterOperator)",
            condition.get("operator"),
        )
    missing = tuple(key for key in ("before", "after") if key not in condition)
    if missing:
        raise CapabilityFieldError(
            CALENDARS_URN, "filter", "both before and after", f"missing {', '.join(missing)}"
        )


CALENDARS: Final = CapabilitySpec(
    urn=CALENDARS_URN,
    attr="calendars",
    reference="draft-ietf-jmap-calendars-27",
    experimental=True,
    account_value=CalendarsCapability,
    data_types=(
        DataTypeSpec(name="Calendar", model=Calendar, shareable=True),
        DataTypeSpec(name="CalendarEvent", model=CalendarEvent),
        DataTypeSpec(name="ParticipantIdentity", model=ParticipantIdentity),
        DataTypeSpec(name="CalendarEventNotification", model=CalendarEventNotification),
        # §6.4. Arrives only over the push channel, but must still be a legal
        # value in PushSubscription.types and the EventSource types parameter.
        DataTypeSpec(name=CALENDAR_ALERT_TYPE, push_only=True),
    ),
    methods=(
        # -- Calendar (§4). No Calendar/query: the draft references one in §4.1
        # but never defines it, so fetching all with ids=null is the way. -- #
        MethodSpec("Calendar/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("Calendar/changes", MethodKind.CHANGES),
        MethodSpec(
            "Calendar/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={
                "onDestroyRemoveEvents": "destroy the events inside rather than failing",
                "onSuccessSetIsDefault": "make this the default; silently ignored if refused",
            },
        ),
        # -- ParticipantIdentity (§3) -- #
        MethodSpec("ParticipantIdentity/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("ParticipantIdentity/changes", MethodKind.CHANGES),
        MethodSpec(
            "ParticipantIdentity/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={"onSuccessSetIsDefault": "silently ignored if the id is unknown"},
        ),
        # -- CalendarEvent (§5) -- #
        MethodSpec(
            "CalendarEvent/get",
            MethodKind.GET,
            chunk_by=LimitKey.GET_OBJECTS,
            extra_args={
                "recurrenceOverridesBefore": "only overrides before this UTC instant",
                "recurrenceOverridesAfter": "only overrides on or after this UTC instant",
                "reduceParticipants": "return only owners and your own identities",
                "timeZone": "zone used to resolve floating events for utcStart/utcEnd",
            },
        ),
        MethodSpec("CalendarEvent/changes", MethodKind.CHANGES),
        MethodSpec(
            "CalendarEvent/query",
            MethodKind.QUERY,
            extra_args={
                "expandRecurrences": "return one id per occurrence rather than per event",
                "timeZone": "zone the before/after filter LocalDateTimes are read in",
            },
        ),
        MethodSpec("CalendarEvent/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec(
            "CalendarEvent/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            # §5.9: defaults to false, so creating an event with participants and
            # forgetting it sends no invitations at all, with no error.
            extra_args={"sendSchedulingMessages": "actually send the iTIP invitations"},
        ),
        MethodSpec("CalendarEvent/copy", MethodKind.COPY, mutating=True),
        # -- CalendarEventNotification (§7). Destroy only. -- #
        MethodSpec("CalendarEventNotification/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("CalendarEventNotification/changes", MethodKind.CHANGES),
        MethodSpec("CalendarEventNotification/query", MethodKind.QUERY),
        MethodSpec("CalendarEventNotification/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec(
            "CalendarEventNotification/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
        ),
    ),
)

#: §1.5.3. ``CalendarEvent/parse`` alone; server support is optional, so it is a
#: separate URN rather than a method a client may assume.
CALENDARS_PARSE: Final = CapabilitySpec(
    urn=CALENDARS_PARSE_URN,
    reference="draft-ietf-jmap-calendars-27 §5.13",
    experimental=True,
    requires=frozenset({CALENDARS_URN}),
    methods=(
        MethodSpec(
            "CalendarEvent/parse",
            MethodKind.CUSTOM,
            response_model=ParsedEvents,
            extra_args={
                "blobIds": "blobs to parse as iCalendar",
                "properties": "CalendarEvent properties to return",
            },
        ),
    ),
)

#: §1.5.2. ``Principal/getAvailability`` lives here, not in RFC 9670 - see the
#: module docstring.
AVAILABILITY: Final = CapabilitySpec(
    urn=AVAILABILITY_URN,
    reference="draft-ietf-jmap-calendars-27 §2.2",
    experimental=True,
    requires=frozenset({"urn:ietf:params:jmap:principals"}),
    account_value=AvailabilityCapability,
    methods=(
        MethodSpec(
            "Principal/getAvailability",
            MethodKind.CUSTOM,
            response_model=AvailabilityResponse,
            extra_args={
                "id": "the Principal to ask about - singular, not `ids`",
                "utcStart": "inclusive start of the window",
                "utcEnd": "exclusive end of the window",
                "showDetails": "include the events themselves, not just busy spans",
                "eventProperties": "which CalendarEvent properties to include",
            },
        ),
    ),
)

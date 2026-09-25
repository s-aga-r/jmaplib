"""Builders for the calendar and contact methods that follow no standard shape.

Each lives in a companion capability with no namespace of its own -
``:calendars:parse``, ``:contacts:parse`` and ``:principals:availability`` - so
it appears on the data type it acts on, and only when the server advertises it:
``batch.calendars.calendar_event.parse``, ``batch.contacts.contact_card.parse``
and ``batch.principals.principal.get_availability``.

``CalendarEvent/query`` does have the standard shape; its builder here only
adds the checks an expanding query needs before it goes out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jmap.api.entity import EntityBase, Queryable, builder
from jmap.capabilities.calendars import (
    AVAILABILITY_URN,
    CALENDARS_URN,
    AvailabilityCapability,
    CalendarsCapability,
    check_availability_window,
    check_expand_filter,
    check_expand_window,
)
from jmap.core.ids import CreationRef, Id
from jmap.core.ijson import InvalidDateError, parse_local_date, parse_utc_date
from jmap.core.invocation import Handle, ResultRef
from jmap.models.arguments import UTCDate
from jmap.models.base import UNSET, Unset, omit_unset
from jmap.models.calendars import AvailabilityResponse, ParsedEvents
from jmap.models.contacts import ParsedCards
from jmap.models.responses import QueryResponse


class CalendarEventQueryable(Queryable[Any]):
    """``CalendarEvent/query`` (draft-ietf-jmap-calendars-27 §5.11)."""

    __slots__ = ()

    @builder
    def query(
        self,
        *,
        # `filter` mirrors the wire name.
        filter: Mapping[str, Any] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[QueryResponse]:
        """Search for events; with ``expandRecurrences=True``, for occurrences.

        An expanding query is checked before it goes out, because each way it can
        fail returns no ids at all: §5.11 wants a bare FilterCondition naming both
        ``after`` and ``before``, and the span between them within the account's
        ``maxExpandedQueryDuration``. The span is read as the filter gives it, in
        local time, so across a daylight-saving change it may be an hour off what
        the server counts.
        """
        if extra.get("expandRecurrences") is True and not isinstance(filter, ResultRef):
            check_expand_filter(filter, expand=True)
            seconds = _local_span(filter)
            if seconds is not None:
                capability = CalendarsCapability.of(self._batch.capability_value(CALENDARS_URN))
                check_expand_window(seconds, capability)
        return super().query(filter=filter, **extra)


class CalendarEventParsable(EntityBase[Any]):
    """``CalendarEvent/parse`` (draft-ietf-jmap-calendars-27 §5.13)."""

    __slots__ = ()

    @builder
    def parse(
        self,
        *,
        blob_ids: Sequence[Id | CreationRef] | ResultRef[Any],
        properties: Sequence[str] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[ParsedEvents]:
        """Read iCalendar blobs as events without storing them.

        One file can hold many events, so ``events_of(blob_id)`` is a list.
        """
        return self._add("parse", omit_unset(blobIds=blob_ids, properties=properties, **extra))


class ContactCardParsable(EntityBase[Any]):
    """``ContactCard/parse`` - a Stalwart extension; RFC 9610 defines none."""

    __slots__ = ()

    @builder
    def parse(
        self,
        *,
        blob_ids: Sequence[Id | CreationRef] | ResultRef[Any],
        properties: Sequence[str] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[ParsedCards]:
        """Read vCard blobs as cards without storing them.

        Each blob yields **one** card - ``card_of(blob_id)`` - unlike the
        calendars ``/parse``. The server caps blobs per call without saying
        where; on ``requestTooLarge``, send half as many.
        """
        return self._add("parse", omit_unset(blobIds=blob_ids, properties=properties, **extra))


class AvailabilityGettable(EntityBase[Any]):
    """``Principal/getAvailability`` (draft-ietf-jmap-calendars-27 §2.2)."""

    __slots__ = ()

    @builder
    def get_availability(
        self,
        *,
        # `id` mirrors the wire name: one principal, not a list.
        id: Id | ResultRef[Any],
        utc_start: UTCDate | ResultRef[Any],
        utc_end: UTCDate | ResultRef[Any],
        show_details: bool | ResultRef[Any] | Unset = UNSET,
        event_properties: Sequence[str] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[AvailabilityResponse]:
        """When one principal is busy, from ``utc_start`` up to ``utc_end``.

        The window is checked against the account's ``maxAvailabilityDuration``
        first, because a wider one fails the whole call with ``tooLarge``.
        """
        if isinstance(utc_start, str) and isinstance(utc_end, str):
            seconds = (parse_utc_date(utc_end) - parse_utc_date(utc_start)).total_seconds()
            capability = AvailabilityCapability.of(self._batch.capability_value(AVAILABILITY_URN))
            check_availability_window(seconds, capability)
        return self._add(
            "getAvailability",
            omit_unset(
                id=id,
                utcStart=utc_start,
                utcEnd=utc_end,
                showDetails=show_details,
                eventProperties=event_properties,
                **extra,
            ),
        )


def _local_span(condition: Any) -> float | None:
    """Seconds from ``after`` to ``before``, or ``None`` if either is not a LocalDateTime.

    A value this cannot read - a back-reference, a UTCDate some servers also
    accept - is left for the server to judge.
    """
    try:
        after = parse_local_date(condition["after"])
        before = parse_local_date(condition["before"])
    except (InvalidDateError, TypeError):
        return None
    return (before - after).total_seconds()

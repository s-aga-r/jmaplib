"""Builders for the calendar and contact methods that follow no standard shape.

Each lives in a companion capability with no namespace of its own -
``:calendars:parse``, ``:contacts:parse`` and ``:principals:availability`` - so
it appears on the data type it acts on, and only when the server advertises it:
``batch.calendars.calendar_event.parse``, ``batch.contacts.contact_card.parse``
and ``batch.principals.principal.get_availability``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from jmap.api.entity import EntityBase, builder
from jmap.capabilities.calendars import (
    AVAILABILITY_URN,
    AvailabilityCapability,
    check_availability_window,
)
from jmap.core.ids import CreationRef, Id
from jmap.core.ijson import parse_utc_date
from jmap.core.invocation import Handle, ResultRef
from jmap.models.arguments import UTCDate
from jmap.models.base import UNSET, Unset, omit_unset
from jmap.models.calendars import AvailabilityResponse, ParsedEvents
from jmap.models.contacts import ParsedCards


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

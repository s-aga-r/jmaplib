"""Custom time zones (RFC 8984 §4.7.2), which jscalendarbis dropped.

JSCalendar 2.0 names IANA zones only. RFC 8984 could carry a zone's rules
inline, and data from a 1.0 source may still do so.
"""

from __future__ import annotations

from typing import Any

from jmap.models.jscalendar.recurrence import RecurrenceRule
from jmap.models.jsdates import LocalDateTime, UTCDateTime
from jmap.models.jsobject import JSObject


class TimeZoneRule(JSObject):
    """One observance - standard or daylight time - and when it applies."""

    #: When the observance first applies.
    start: LocalDateTime | None = None
    #: UTC offsets as ``+hhmm`` or ``-hhmm``.
    offset_from: str | None = None
    offset_to: str | None = None
    recurrence_rules: list[RecurrenceRule] | None = None
    recurrence_overrides: dict[str, dict[str, Any]] | None = None
    #: A set-as-map of the observance's names, e.g. ``{"CEST": True}``.
    names: dict[str, bool] | None = None
    comments: list[str] | None = None


class TimeZone(JSObject):
    """A time zone defined inline, keyed by the id that ``timeZone`` names."""

    tz_id: str | None = None
    updated: UTCDateTime | None = None
    url: str | None = None
    valid_until: UTCDateTime | None = None
    #: Other ids for the zone, as a set-as-map.
    aliases: dict[str, bool] | None = None
    standard: list[TimeZoneRule] | None = None
    daylight: list[TimeZoneRule] | None = None

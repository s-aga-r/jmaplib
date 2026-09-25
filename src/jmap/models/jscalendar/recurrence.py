"""Recurrence rules (draft-ietf-calext-jscalendarbis §3.3.3)."""

from __future__ import annotations

from jmap.models.jsobject import JSObject


class NDay(JSObject):
    """A day of the week, optionally the nth of its period: ``-1`` is the last."""

    #: ``mo``, ``tu``, ``we``, ``th``, ``fr``, ``sa`` or ``su``.
    day: str | None = None
    nth_of_period: int | None = None


class RecurrenceRule(JSObject):
    """A repeating pattern, applied to the event's ``start`` (§3.3.3).

    One object, not RFC 8984's ``recurrenceRules`` array.
    """

    #: ``yearly``, ``monthly``, ``weekly``, ``daily``, ``hourly``, ``minutely``
    #: or ``secondly``.
    frequency: str | None = None
    interval: int | None = None
    #: A CLDR calendar, ``gregorian`` by default.
    rscale: str | None = None
    #: What an invalid date in ``rscale`` becomes: ``omit``, ``backward`` or ``forward``.
    skip: str | None = None
    first_day_of_week: str | None = None
    by_day: list[NDay] | None = None
    by_month_day: list[int] | None = None
    #: Strings, not numbers: a leap month is ``"5L"``.
    by_month: list[str] | None = None
    by_year_day: list[int] | None = None
    by_week_no: list[int] | None = None
    by_hour: list[int] | None = None
    by_minute: list[int] | None = None
    by_second: list[int] | None = None
    by_set_position: list[int] | None = None
    #: At most one of ``count`` and ``until``.
    count: int | None = None
    #: A LocalDateTime, in the event's time zone.
    until: str | None = None

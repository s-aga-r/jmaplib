"""The Task (draft-ietf-calext-jscalendarbis §2.2, §4.2)."""

from __future__ import annotations

from typing import ClassVar

from jmap.models.jscalendar.common import Entry


class Task(Entry):
    """An action item, which may start, be due, take time and recur (§2.2).

    Where ``timeZone`` is unset or ``showWithoutTime`` is true, at least one
    of ``due`` and ``start`` must be set.
    """

    REQUIRED_TYPE: ClassVar[str] = "Task"

    #: A LocalDateTime, in the task's time zone.
    due: str | None = None
    #: A LocalDateTime; required when the task recurs.
    start: str | None = None
    #: A Duration.
    estimated_duration: str | None = None
    #: 0 to 100.
    percent_complete: int | None = None
    #: ``needs-action``, ``in-process``, ``completed``, ``failed`` or
    #: ``cancelled``. Unset, it follows the participants' progress.
    progress: str | None = None
    #: RFC 8984: when ``progress`` last changed.
    progress_updated: str | None = None

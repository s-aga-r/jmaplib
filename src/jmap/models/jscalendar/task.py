"""The Task (draft-ietf-calext-jscalendarbis §2.2, §4.2)."""

from __future__ import annotations

from typing import ClassVar

from jmap.models.jscalendar.common import Entry
from jmap.models.jsdates import LocalDateTime, UTCDateTime


class Task(Entry):
    """An action item, which may start, be due, take time and recur (§2.2).

    Where ``timeZone`` is unset or ``showWithoutTime`` is true, at least one
    of ``due`` and ``start`` must be set.
    """

    REQUIRED_TYPE: ClassVar[str] = "Task"

    #: In the task's time zone.
    due: LocalDateTime | None = None
    #: Required when the task recurs.
    start: LocalDateTime | None = None
    #: A Duration.
    estimated_duration: str | None = None
    #: 0 to 100.
    percent_complete: int | None = None
    #: ``needs-action``, ``in-process``, ``completed``, ``failed`` or
    #: ``cancelled``. Unset, it follows the participants' progress.
    progress: str | None = None
    #: RFC 8984: when ``progress`` last changed.
    progress_updated: UTCDateTime | None = None

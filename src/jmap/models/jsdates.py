"""JSContact and JSCalendar's two date-time forms, as Python datetimes.

A ``UTCDateTime`` is an instant - ``2026-10-05T08:00:00Z`` - and reads as an
aware :class:`~datetime.datetime` in UTC. A ``LocalDateTime`` is a wall-clock
reading whose zone is a sibling property - ``start`` beside ``timeZone`` - and
reads as a *naive* one: attaching a zone would invent an instant the sender
never named. So each refuses the other kind of datetime rather than guess.

Both go back out in their one canonical form: seconds always, and a fraction
only when it is not zero, without trailing zeros (RFC 8984 §1.4.3, RFC 9553
§1.4.5). A string that is not in the right form fails validation, which the
forgiving models turn into a property kept as it arrived.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, TypeAlias

from pydantic import PlainSerializer, PlainValidator

from jmap.core.ijson import parse_local_date, parse_utc_date


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("a UTCDateTime is an instant, so a naive datetime cannot be one")
        return value.astimezone(UTC)
    if isinstance(value, str):
        return parse_utc_date(value)
    raise ValueError(f"a UTCDateTime is a string or a datetime, not {type(value).__name__}")


def _local(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.utcoffset() is not None:
            raise ValueError(
                "a LocalDateTime has no zone of its own - it is read in the time zone "
                "beside it - so it takes a naive datetime"
            )
        return value
    if isinstance(value, str):
        return parse_local_date(value)
    raise ValueError(f"a LocalDateTime is a string or a datetime, not {type(value).__name__}")


def _format(value: datetime) -> str:
    text = (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    )
    return f"{text}.{value.microsecond:06d}".rstrip("0") if value.microsecond else text


def _format_utc(value: datetime) -> str:
    return f"{_format(value.astimezone(UTC))}Z"


#: An instant, as an aware datetime in UTC.
UTCDateTime: TypeAlias = Annotated[
    datetime, PlainValidator(_utc), PlainSerializer(_format_utc, when_used="json")
]

#: A wall-clock reading, as a naive datetime.
LocalDateTime: TypeAlias = Annotated[
    datetime, PlainValidator(_local), PlainSerializer(_format, when_used="json")
]

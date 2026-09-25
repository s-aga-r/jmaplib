"""JSContact and JSCalendar dates, read as datetimes and written canonically."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from jmap.models.jscalendar import AbsoluteTrigger, Event

# At run time: pydantic resolves the annotations of the model below.
from jmap.models.jsdates import LocalDateTime, UTCDateTime  # noqa: TC001
from jmap.models.jsobject import JSObject


class Stamps(JSObject):
    at: UTCDateTime | None = None
    wall: LocalDateTime | None = None


class TestUTCDateTime:
    def test_it_reads_as_an_aware_instant(self):
        trigger = AbsoluteTrigger.from_wire({"when": "2026-10-05T08:00:00Z"})
        assert trigger.when == datetime(2026, 10, 5, 8, tzinfo=UTC)

    def test_any_aware_datetime_is_that_instant_in_utc(self):
        berlin = timezone(timedelta(hours=2))
        stamps = Stamps(at=datetime(2026, 10, 5, 10, tzinfo=berlin))
        assert stamps.at == datetime(2026, 10, 5, 8, tzinfo=UTC)
        assert stamps.to_wire() == {"at": "2026-10-05T08:00:00Z"}

    @pytest.mark.parametrize(
        ("wire", "back"),
        [
            ("2010-10-10T10:10:10.003Z", "2010-10-10T10:10:10.003Z"),
            ("2010-10-10T10:10:10.5Z", "2010-10-10T10:10:10.5Z"),
            ("0999-01-01T00:00:00Z", "0999-01-01T00:00:00Z"),
        ],
    )
    def test_it_goes_back_in_its_one_canonical_form(self, wire, back):
        assert Stamps.from_wire({"at": wire}).to_wire() == {"at": back}

    @pytest.mark.parametrize("value", [datetime(2026, 10, 5, 8), date(2026, 10, 5)])
    def test_a_python_value_that_is_no_instant_is_refused(self, value):
        with pytest.raises(ValidationError, match="at"):
            Stamps(at=value)


class TestLocalDateTime:
    def test_it_reads_as_a_naive_wall_clock_time(self):
        event = Event.from_wire({"start": "2026-10-05T10:00:00"})
        assert event.start == datetime(2026, 10, 5, 10)
        assert event.start.tzinfo is None

    def test_a_naive_datetime_goes_out_as_the_wall_clock_time(self):
        assert Event(start=datetime(2026, 10, 5, 9, 30)).to_wire() == {
            "@type": "Event",
            "start": "2026-10-05T09:30:00",
        }

    @pytest.mark.parametrize("value", [datetime(2026, 10, 5, 9, tzinfo=UTC), date(2026, 10, 5)])
    def test_a_python_value_that_is_no_wall_clock_time_is_refused(self, value):
        # The zone is `timeZone`, beside it: an offset here is how a recurrence
        # id ends up matching nothing.
        with pytest.raises(ValidationError, match="wall"):
            Stamps(wall=value)


class TestFromTheWire:
    @pytest.mark.parametrize(
        "wire",
        [
            {"at": "2026-10-05T08:00:00+00:00", "wall": "2026-10-05T09:00:00Z"},
            {"at": 1_790_000_000, "wall": 20261005},
        ],
    )
    def test_json_in_the_wrong_form_is_kept_like_any_misfit(self, wire):
        # Plain JSON could have come off the wire, even when written in Python.
        stamps = Stamps.from_wire(wire)
        assert (stamps.at, stamps.wall) == (None, None)
        assert stamps.to_wire() == wire

    def test_a_date_in_the_wrong_form_is_kept_as_it_came(self):
        wire: dict[str, Any] = {"@type": "Event", "start": "2026-10-05T10:00:00+02:00"}
        event = Event.from_wire(wire)
        assert event.start is None
        assert event.jscalendar("start") == "2026-10-05T10:00:00+02:00"
        assert event.to_wire() == wire

    def test_python_mode_keeps_the_datetime(self):
        stamps = Stamps.from_wire({"at": "2026-10-05T08:00:00Z"})
        assert stamps.model_dump()["at"] == datetime(2026, 10, 5, 8, tzinfo=UTC)

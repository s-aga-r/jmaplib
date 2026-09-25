"""JSCalendar 2.0 (jscalendarbis): the Event's objects, typed, lossless and forgiving.

The fixture is an event Stalwart 0.16.17 returned for a CalendarEvent/set
create - including the alerts' and locations' ``@type``, which it adds itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jmap.models.calendars import CalendarEvent
from jmap.models.jscalendar import (
    AbsoluteTrigger,
    Alert,
    Event,
    Link,
    OffsetTrigger,
    Participant,
    RecurrenceRule,
    UnknownTrigger,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stalwart-0.16.17-calendar-event.json"


@pytest.fixture
def wire() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


@pytest.fixture
def event(wire: dict[str, Any]) -> CalendarEvent:
    return CalendarEvent.from_wire(wire)


class TestAStalwartEvent:
    def test_it_round_trips_exactly(self, event, wire):
        assert event.to_wire() == wire

    def test_nothing_it_sent_is_left_unmodelled(self, event):
        assert event.model_extra == {}

    def test_the_recurrence_is_one_rule(self, event):
        rule = event.recurrence_rule
        assert rule is not None
        assert (rule.frequency, rule.count) == ("weekly", 4)
        assert rule.by_day is not None
        assert rule.by_day[0].day == "mo"
        assert event.recurrence_overrides == {
            "2026-10-12T10:00:00": {
                "title": "Engine review (moved)",
                "updated": "2026-09-25T09:29:41Z",
            }
        }

    def test_each_trigger_is_its_own_type(self, event):
        assert event.alerts is not None
        offset = event.alerts["a1"].trigger
        absolute = event.alerts["a2"].trigger
        assert isinstance(offset, OffsetTrigger)
        assert (offset.offset, offset.relative_to) == ("-PT15M", "start")
        assert isinstance(absolute, AbsoluteTrigger)
        assert absolute.when == "2026-10-05T08:00:00Z"

    def test_where_and_what_read_through(self, event):
        assert (event.title, event.start, event.time_zone, event.duration) == (
            "Engine review",
            "2026-10-05T10:00:00",
            "Europe/London",
            "PT1H30M",
        )
        assert event.locations is not None
        assert event.locations["l1"].coordinates == "geo:51.5,-0.13"
        assert event.virtual_locations is not None
        assert event.virtual_locations["v1"].features == {"video": True}
        assert event.links is not None
        attachment = event.links["k1"]
        assert (attachment.rel, attachment.size) == ("enclosure", 1234)
        assert (event.is_origin, event.is_draft, event.calendars) == (True, False, ["b"])


class TestTheRestOfJscalendarbis:
    """Properties Stalwart's event did not carry."""

    WIRE: ClassVar[dict[str, Any]] = {
        "@type": "Event",
        "uid": "a8df6573-0474-496d-8496-033ad45d7fea",
        "version": "2.0",
        "sequence": 2,
        "method": "request",
        "prodId": "-//Example//EN",
        "created": "2026-01-01T00:00:00Z",
        "relatedTo": {"b1b1": {"relation": {"first": True}}},
        "descriptionContentType": "text/html",
        "locale": "en-GB",
        "categories": {"http://example.com/cat/review": True},
        "mainLocationId": "l1",
        "locations": {
            "l1": {
                "name": "Office",
                "locationTypes": {"office": True},
                "links": {"m": {"href": "https://example.com/map.png", "display": {"badge": True}}},
            }
        },
        "recurrenceId": "2026-10-05T10:00:00",
        "recurrenceIdTimeZone": "Europe/London",
        "recurrenceRule": {
            "frequency": "monthly",
            "interval": 2,
            "rscale": "gregorian",
            "skip": "forward",
            "firstDayOfWeek": "su",
            "byDay": [{"day": "fr", "nthOfPeriod": -1}],
            "byMonthDay": [1],
            "byMonth": ["5L"],
            "byYearDay": [100],
            "byWeekNo": [20],
            "byHour": [9],
            "byMinute": [30],
            "bySecond": [0],
            "bySetPosition": [1],
            "until": "2027-01-01T00:00:00",
        },
        "priority": 1,
        "privacy": "private",
        "organizerCalendarAddress": "mailto:ada@example.com",
        "participants": {
            "p1": {
                "name": "Ada",
                "email": "ada@example.com",
                "calendarAddress": "mailto:ada@example.com",
                "kind": "individual",
                "roles": {"owner": True, "attendee": True},
                "participationStatus": "accepted",
                "expectReply": False,
                "sentBy": "mailto:charles@example.com",
                "delegatedTo": {"p2": True},
                "memberOf": {"g1": True},
                "links": {"x": {"href": "https://example.com/ada", "title": "Profile"}},
                "scheduleSequence": 3,
                "scheduleUpdated": "2026-09-01T00:00:00Z",
            }
        },
        "alerts": {
            "a1": {
                "trigger": {"offset": "PT0S", "relativeTo": "end"},
                "acknowledged": "2026-10-05T11:30:00Z",
                "relatedTo": {"a0": {"relation": {"parent": True}}},
                "action": "email",
            }
        },
        "endTimeZone": "Europe/Paris",
        "status": "tentative",
        "mayInviteSelf": True,
        "mayInviteOthers": False,
        "hideAttendees": True,
    }

    def test_it_round_trips_exactly(self):
        assert Event.from_wire(self.WIRE).to_wire() == self.WIRE

    def test_every_property_is_modelled(self):
        event = Event.from_wire(self.WIRE)
        assert event.model_extra == {}
        rule = event.recurrence_rule
        assert rule is not None
        assert rule.by_month == ["5L"]
        assert rule.by_day is not None
        assert rule.by_day[0].nth_of_period == -1
        assert event.participants is not None
        ada = event.participants["p1"]
        assert ada.roles == {"owner": True, "attendee": True}
        assert (ada.calendar_address, ada.schedule_sequence) == ("mailto:ada@example.com", 3)
        assert event.related_to is not None
        assert event.related_to["b1b1"].relation == {"first": True}
        assert (event.may_invite_self, event.hide_attendees) == (True, True)
        assert event.locations is not None
        office = event.locations["l1"]
        assert office.links is not None
        assert office.links["m"].display == {"badge": True}


class TestBuildingOne:
    def test_it_sends_what_it_was_given_by_wire_name(self):
        event = CalendarEvent(
            calendar_ids={"c1": True},
            title="Review",
            start="2026-10-05T10:00:00",
            time_zone="Europe/London",
            recurrence_rule=RecurrenceRule(frequency="daily", count=3),
            participants={"p1": Participant(calendar_address="mailto:a@example.com")},
            links={"k": Link(blob_id="B1", rel="enclosure")},
        )
        assert event.to_wire() == {
            "calendarIds": {"c1": True},
            "title": "Review",
            "start": "2026-10-05T10:00:00",
            "timeZone": "Europe/London",
            "recurrenceRule": {"frequency": "daily", "count": 3},
            "participants": {"p1": {"calendarAddress": "mailto:a@example.com"}},
            "links": {"k": {"blobId": "B1", "rel": "enclosure"}},
        }

    def test_an_absolute_trigger_always_says_what_it_is(self):
        # A trigger without @type is an OffsetTrigger (the default type).
        before = Alert(trigger=OffsetTrigger(offset="-PT5M"))
        at = Alert(trigger=AbsoluteTrigger(when="2026-10-05T08:00:00Z"))
        assert before.to_wire() == {"trigger": {"offset": "-PT5M"}}
        assert at.to_wire() == {
            "trigger": {"@type": "AbsoluteTrigger", "when": "2026-10-05T08:00:00Z"}
        }

    def test_an_untouched_event_sends_nothing(self):
        assert CalendarEvent().to_wire() == {}


class TestTriggers:
    @pytest.mark.parametrize(
        ("trigger", "kind"),
        [
            ({"offset": "-PT1M"}, OffsetTrigger),
            ({"@type": "OffsetTrigger", "offset": "-PT1M"}, OffsetTrigger),
            ({"@type": "AbsoluteTrigger", "when": "2026-01-01T00:00:00Z"}, AbsoluteTrigger),
            ({"@type": "LocationTrigger", "radius": 50}, UnknownTrigger),
        ],
    )
    def test_the_type_picks_the_model(self, trigger, kind):
        alert = Alert.from_wire({"trigger": trigger})
        assert type(alert.trigger) is kind
        assert alert.to_wire() == {"trigger": trigger}

    def test_an_unknown_trigger_built_in_python_keeps_its_type(self):
        unknown = UnknownTrigger.from_wire({"@type": "LocationTrigger", "radius": 50})
        assert Alert(trigger=unknown).to_wire() == {
            "trigger": {"@type": "LocationTrigger", "radius": 50}
        }

    def test_a_trigger_that_is_no_object_is_kept_on_the_alert(self):
        alert = Alert.from_wire({"trigger": "-PT15M", "action": "display"})
        assert alert.trigger is None
        assert alert.action == "display"
        assert alert.to_wire() == {"trigger": "-PT15M", "action": "display"}


class TestForgiveness:
    def test_a_misfit_costs_only_its_own_property(self):
        wire = {"id": "e1", "title": "T", "participants": {"p1": {"roles": ["owner"]}}}
        event = CalendarEvent.from_wire(wire)
        assert event.participants is not None
        assert event.participants["p1"].roles is None
        assert event.jscalendar("participants") == {"p1": {"roles": ["owner"]}}
        assert event.to_wire() == wire

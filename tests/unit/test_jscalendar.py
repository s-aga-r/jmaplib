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
    Group,
    Link,
    OffsetTrigger,
    Participant,
    RecurrenceRule,
    Task,
    TimeZone,
    UnknownEntry,
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


class TestTasks:
    """jscalendarbis §5.2 and §5.5."""

    SIMPLE: ClassVar[dict[str, Any]] = {
        "@type": "Task",
        "version": "2.0",
        "uid": "2a358cee-6489-4f14-a57f-c104db4dc2f2",
        "updated": "2020-01-09T14:32:01Z",
        "title": "Do something",
    }

    def test_a_simple_task_round_trips(self):
        task = Task.from_wire(self.SIMPLE)
        assert task.title == "Do something"
        assert task.to_wire() == self.SIMPLE

    def test_a_task_with_a_due_date(self):
        wire = {
            "@type": "Task",
            "title": "Buy groceries",
            "due": "2020-01-19T18:00:00",
            "timeZone": "Europe/Vienna",
            "estimatedDuration": "PT1H",
            "percentComplete": 10,
            "progress": "in-process",
        }
        task = Task.from_wire(wire)
        assert (task.due, task.estimated_duration, task.progress) == (
            "2020-01-19T18:00:00",
            "PT1H",
            "in-process",
        )
        assert task.model_extra == {}
        assert task.to_wire() == wire

    def test_a_task_shares_what_an_event_has(self):
        task = Task(recurrence_rule=RecurrenceRule(frequency="weekly"), start="2020-01-06T09:00:00")
        assert task.to_wire() == {
            "@type": "Task",
            "recurrenceRule": {"frequency": "weekly"},
            "start": "2020-01-06T09:00:00",
        }


class TestGroups:
    """jscalendarbis §5.3."""

    SIMPLE: ClassVar[dict[str, Any]] = {
        "@type": "Group",
        "version": "2.0",
        "uid": "bf0ac22b-4989-4caf-9ebd-54301b4ee51a",
        "updated": "2020-01-15T18:00:00Z",
        "title": "A simple group",
        "entries": [
            {
                "@type": "Event",
                "uid": "a8df6573-0474-496d-8496-033ad45d7fea",
                "updated": "2020-01-02T18:23:04Z",
                "title": "Some event",
                "start": "2020-01-15T13:00:00",
                "timeZone": "America/New_York",
                "duration": "PT1H",
            },
            {
                "@type": "Task",
                "uid": "2a358cee-6489-4f14-a57f-c104db4dc2f2",
                "updated": "2020-01-09T14:32:01Z",
                "title": "Do something",
            },
        ],
    }

    def test_each_entry_is_its_own_type(self):
        group = Group.from_wire(self.SIMPLE)
        assert group.entries is not None
        assert [type(entry) for entry in group.entries] == [Event, Task]
        assert group.to_wire() == self.SIMPLE

    def test_an_entry_of_an_unknown_type_is_kept(self):
        # §4.3.1 has it ignored; kept, the group still goes back unchanged.
        wire = {"@type": "Group", "entries": [{"@type": "Journal", "title": "Notes"}]}
        group = Group.from_wire(wire)
        assert group.entries is not None
        assert isinstance(group.entries[0], UnknownEntry)
        assert group.to_wire() == wire

    def test_one_built_in_python_types_its_entries(self):
        group = Group(title="Week", entries=[Event(title="Standup"), Task(title="Write up")])
        assert group.to_wire() == {
            "@type": "Group",
            "title": "Week",
            "entries": [
                {"@type": "Event", "title": "Standup"},
                {"@type": "Task", "title": "Write up"},
            ],
        }

    def test_a_group_takes_the_shared_properties_only(self):
        # A Group has no recurrence; the property is kept, but not modelled.
        group = Group.from_wire({"@type": "Group", "recurrenceRule": {"frequency": "daily"}})
        assert group.model_extra == {"recurrenceRule": {"frequency": "daily"}}


class TestStandingAlone:
    def test_an_event_always_says_what_it_is(self):
        assert Event(title="Lunch").to_wire() == {"@type": "Event", "title": "Lunch"}

    def test_a_calendar_event_leaves_that_to_the_server(self):
        assert CalendarEvent(title="Lunch").to_wire() == {"title": "Lunch"}


class TestRfc8984:
    """JSCalendar 1.0's shapes, which jscalendarbis dropped or renamed, read typed."""

    PARTICIPANTS: ClassVar[dict[str, Any]] = {
        "@type": "Event",
        "title": "FooBar team meeting",
        "start": "2020-01-08T09:00:00",
        "timeZone": "Africa/Johannesburg",
        "recurrenceRules": [{"@type": "RecurrenceRule", "frequency": "weekly"}],
        "replyTo": {"imip": "mailto:f245f875-7f63-4a5e-a2c8@schedule.example.com"},
        "participants": {
            "dG9tQGZvb2Jhci5xlLmNvbQ": {
                "@type": "Participant",
                "name": "Tom Tool",
                "sendTo": {"imip": "mailto:tom@calendar.example.com"},
                "participationStatus": "accepted",
                "roles": {"attendee": True},
            }
        },
        "recurrenceOverrides": {
            "2020-03-04T09:00:00": {
                "participants/dG9tQGZvb2Jhci5xlLmNvbQ/participationStatus": "declined"
            }
        },
    }

    def test_rfc_8984s_recurring_event_with_participants(self):
        # RFC 8984 §6.10.
        event = Event.from_wire(self.PARTICIPANTS)
        assert event.model_extra == {}
        assert event.recurrence_rules == [
            RecurrenceRule(at_type="RecurrenceRule", frequency="weekly")
        ]
        assert event.reply_to == {"imip": "mailto:f245f875-7f63-4a5e-a2c8@schedule.example.com"}
        assert event.participants is not None
        tom = event.participants["dG9tQGZvb2Jhci5xlLmNvbQ"]
        assert tom.send_to == {"imip": "mailto:tom@calendar.example.com"}
        assert event.to_wire() == self.PARTICIPANTS

    def test_rfc_8984s_localized_event(self):
        # RFC 8984 §6.8, whose Location carries a description.
        wire = {
            "@type": "Event",
            "title": "Live from Music Bowl: The Band",
            "locale": "en",
            "locations": {
                "c0": {
                    "@type": "Location",
                    "name": "The Music Bowl",
                    "description": "Music Bowl, Central Park, New York",
                    "coordinates": "geo:40.7829,-73.9654",
                }
            },
            "localizations": {"de": {"title": "Live von der Music Bowl: The Band!"}},
        }
        event = Event.from_wire(wire)
        assert event.model_extra == {}
        assert event.locations is not None
        assert event.locations["c0"].description == "Music Bowl, Central Park, New York"
        assert event.localizations == {"de": {"title": "Live von der Music Bowl: The Band!"}}
        assert event.to_wire() == wire

    def test_a_time_zone_defined_inline(self):
        wire = {
            "@type": "Event",
            "timeZone": "/example.com/tz/Europe/Vienna",
            "timeZones": {
                "/example.com/tz/Europe/Vienna": {
                    "@type": "TimeZone",
                    "tzId": "Europe/Vienna",
                    "validUntil": "2030-01-01T00:00:00Z",
                    "aliases": {"Europe/Wien": True},
                    "standard": [
                        {
                            "@type": "TimeZoneRule",
                            "start": "1996-10-27T03:00:00",
                            "offsetFrom": "+0200",
                            "offsetTo": "+0100",
                            "recurrenceRules": [{"frequency": "yearly", "byMonth": ["10"]}],
                            "names": {"CET": True},
                            "comments": ["winter"],
                        }
                    ],
                    "daylight": [
                        {"start": "1981-03-29T02:00:00", "offsetFrom": "+0100", "offsetTo": "+0200"}
                    ],
                }
            },
        }
        event = Event.from_wire(wire)
        assert event.model_extra == {}
        assert event.time_zones is not None
        vienna = event.time_zones["/example.com/tz/Europe/Vienna"]
        assert isinstance(vienna, TimeZone)
        assert vienna.standard is not None
        assert (vienna.standard[0].offset_to, vienna.standard[0].names) == ("+0100", {"CET": True})
        assert event.to_wire() == wire

    def test_the_rest_of_1_0_reads_typed_too(self):
        wire = {
            "@type": "Task",
            "sentBy": "mailto:boss@example.com",
            "excluded": False,
            "excludedRecurrenceRules": [{"frequency": "yearly"}],
            "requestStatus": "2.0;Success",
            "progressUpdated": "2020-01-10T08:00:00Z",
            "links": {
                "a": {
                    "href": "cid:logo@example.com",
                    "cid": "logo@example.com",
                    "display": "badge",
                },
                "b": {"href": "https://example.com/big.png", "display": {"fullsize": True}},
            },
            "virtualLocations": {"v": {"uri": "tel:+1-555-0100", "description": "Dial in"}},
            "participants": {
                "p": {
                    "locationId": "l1",
                    "language": "de",
                    "participationComment": "Late",
                    "scheduleAgent": "client",
                    "scheduleForceSend": True,
                    "scheduleStatus": ["2.0"],
                    "invitedBy": "o",
                    "progressUpdated": "2020-01-10T08:00:00Z",
                }
            },
            "locations": {"l1": {"relativeTo": "start", "timeZone": "Europe/Berlin"}},
        }
        task = Task.from_wire(wire)
        assert task.model_extra == {}
        assert task.links is not None
        assert (task.links["a"].display, task.links["a"].cid) == ("badge", "logo@example.com")
        assert task.links["b"].display == {"fullsize": True}
        assert task.participants is not None
        assert task.participants["p"].schedule_agent == "client"
        assert task.to_wire() == wire

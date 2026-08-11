"""draft-ietf-jmap-calendars-27 models and the calendar capabilities' local gates.

Calendars is the spec where the wrong answer looks like the right one. A
recurrence override keyed by an aware date-time is accepted and orphaned; an
expanding query whose window is one second too wide fails the *whole* method
call; a ``CalendarEvent/parse`` result is an array per blob rather than an event
per blob. These tests pin the handful of places where the library is the only
thing standing between a caller and a silent, plausible, wrong result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jmap.capabilities.calendars import (
    AVAILABILITY,
    AVAILABILITY_URN,
    CALENDAR_ALERT_EVENT,
    CALENDAR_ALERT_TYPE,
    CALENDARS,
    CALENDARS_PARSE,
    CALENDARS_PARSE_URN,
    CALENDARS_URN,
    AvailabilityCapability,
    CalendarsCapability,
    check_availability_window,
    check_expand_filter,
    check_expand_window,
    duration_seconds,
)
from jmap.capabilities.spec import MethodKind
from jmap.core.errors import CapabilityFieldError
from jmap.models.calendars import (
    AVAILABILITY_ALL,
    AVAILABILITY_ATTENDING,
    AVAILABILITY_NONE,
    BUSY_PRECEDENCE,
    CALENDAR_HAS_EVENT,
    CANNOT_CALCULATE_OCCURRENCES,
    DEFAULT_BUSY_STATUS,
    EXPAND_DURATION_TOO_LARGE,
    NO_SUPPORTED_SCHEDULE_METHODS,
    AvailabilityResponse,
    BusyPeriod,
    Calendar,
    CalendarEvent,
    CalendarEventNotification,
    CalendarRights,
    InvalidRecurrenceIdError,
    ParsedEvents,
    ParticipantIdentity,
    check_recurrence_id,
)


class TestRecurrenceId:
    def test_a_bare_local_date_time_is_returned_unchanged(self):
        assert check_recurrence_id("2024-03-04T09:00:00") == "2024-03-04T09:00:00"

    def test_a_recurrence_id_must_not_carry_an_offset(self):
        # The whole point of the gate. An override keyed `...+11:00` matches no
        # occurrence, so the server stores it as an *extra* one and the series
        # silently grows an event nobody asked for.
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04T09:00:00+11:00")

    def test_a_recurrence_id_must_not_be_a_utc_instant(self):
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04T09:00:00Z")

    def test_fractional_seconds_are_refused(self):
        # RFC 8984 allowed them; jscalendarbis does not, so a key that worked
        # against an older server is now the orphaning bug above.
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04T09:00:00.500")

    def test_seconds_are_not_optional(self):
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04T09:00")

    def test_a_bare_date_is_refused_even_for_an_all_day_event(self):
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04")

    def test_trailing_text_cannot_smuggle_itself_past_the_pattern(self):
        # \Z rather than $, so a trailing newline is not a match either.
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id("2024-03-04T09:00:00\n")

    def test_a_naive_datetime_renders_to_a_legal_key(self):
        naive = datetime(2024, 3, 4, 9, 0, 0)
        assert check_recurrence_id(naive.isoformat()) == "2024-03-04T09:00:00"

    def test_an_aware_datetime_renders_to_an_illegal_one(self):
        # Spelled out with a real datetime because `isoformat()` on an aware
        # value is exactly how the bad key gets produced in the wild.
        aware = datetime(2024, 3, 4, 9, 0, 0, tzinfo=timezone(timedelta(hours=11)))
        with pytest.raises(InvalidRecurrenceIdError):
            check_recurrence_id(aware.isoformat())

    def test_the_error_carries_the_offending_value(self):
        with pytest.raises(InvalidRecurrenceIdError) as excinfo:
            check_recurrence_id("nonsense")
        assert excinfo.value.value == "nonsense"
        assert "no fractional seconds" in str(excinfo.value)

    def test_the_error_is_a_value_error(self):
        # Callers validating a whole overrides map catch ValueError; a bespoke
        # base class would slip straight through them.
        assert issubclass(InvalidRecurrenceIdError, ValueError)


class TestCalendarModel:
    def test_a_calendar_parses_from_the_wire(self):
        calendar = Calendar.from_wire(
            {
                "id": "c1",
                "name": "Personal",
                "color": "#aabbcc",
                "sortOrder": 2,
                "isSubscribed": True,
                "isVisible": True,
                "isDefault": True,
                "includeInAvailability": "all",
                "timeZone": "Australia/Melbourne",
            }
        )
        assert calendar.name == "Personal"
        assert calendar.include_in_availability == AVAILABILITY_ALL
        assert calendar.time_zone == "Australia/Melbourne"

    def test_a_null_time_zone_defers_to_the_principal(self):
        # Distinct from omitting it: null is a value the client may send to clear
        # a per-calendar zone, and to_wire has to keep it.
        assert Calendar(time_zone=None).to_wire() == {"timeZone": None}

    def test_default_alerts_are_kept_as_raw_alert_objects(self):
        calendar = Calendar.from_wire(
            {"defaultAlertsWithTime": {"a1": {"trigger": {"offset": "-PT10M"}}}}
        )
        assert calendar.default_alerts_with_time is not None
        assert calendar.default_alerts_with_time["a1"]["trigger"] == {"offset": "-PT10M"}
        assert calendar.default_alerts_without_time is None

    def test_share_with_parses_into_rights_objects(self):
        calendar = Calendar.from_wire({"shareWith": {"u2": {"mayReadItems": True}}})
        assert calendar.share_with is not None
        assert calendar.share_with["u2"].may_read_items is True
        assert calendar.share_with["u2"].may_write_all is False

    def test_rights_default_to_denying_everything(self):
        # A server that omits myRights has told us nothing, and "nothing" must not
        # read as permission to write.
        rights = CalendarRights()
        assert rights.may_read_items is False
        assert rights.may_write_all is False
        assert rights.may_delete is False

    def test_the_availability_modes_are_named(self):
        assert (AVAILABILITY_ALL, AVAILABILITY_ATTENDING, AVAILABILITY_NONE) == (
            "all",
            "attending",
            "none",
        )

    def test_the_method_level_error_types_are_named(self):
        assert EXPAND_DURATION_TOO_LARGE == "expandDurationTooLarge"
        assert CANNOT_CALCULATE_OCCURRENCES == "cannotCalculateOccurrences"
        assert CALENDAR_HAS_EVENT == "calendarHasEvent"
        assert NO_SUPPORTED_SCHEDULE_METHODS == "noSupportedScheduleMethods"


class TestParticipantIdentity:
    def test_an_identity_parses_from_the_wire(self):
        identity = ParticipantIdentity.from_wire(
            {"id": "i1", "name": "Alice", "calendarAddress": "mailto:alice@example.com"}
        )
        assert identity.calendar_address == "mailto:alice@example.com"
        assert identity.is_default is None

    def test_the_address_is_carried_exactly_as_the_server_spelled_it(self):
        # §3 matches identities after RFC 3986 normalisation, so the raw casing
        # must survive: normalising here would hide which spelling was returned.
        identity = ParticipantIdentity.from_wire({"calendarAddress": "MAILTO:Alice@Example.com"})
        assert identity.calendar_address == "MAILTO:Alice@Example.com"


class TestCalendarEvent:
    def test_a_stored_event_is_not_an_occurrence(self):
        assert CalendarEvent.from_wire({"id": "e1"}).is_occurrence is False

    def test_a_base_event_id_marks_an_expanded_instance(self):
        event = CalendarEvent.from_wire({"id": "e1;2024-03-04T09:00:00", "baseEventId": "e1"})
        assert event.is_occurrence is True
        assert event.base_event_id == "e1"

    def test_calendars_reads_the_set_as_map(self):
        event = CalendarEvent.from_wire({"calendarIds": {"c1": True, "c2": True}})
        assert sorted(event.calendars) == ["c1", "c2"]

    def test_a_false_entry_is_not_a_calendar_the_event_is_in(self):
        # calendarIds is a set-as-map: only True entries are members, and treating
        # it as `list(keys())` would put the event in a calendar it left.
        event = CalendarEvent.from_wire({"calendarIds": {"c1": True, "c2": False}})
        assert event.calendars == ["c1"]

    def test_an_absent_calendar_ids_reads_as_no_calendars(self):
        assert CalendarEvent().calendars == []

    def test_an_empty_calendar_ids_reads_as_no_calendars(self):
        assert CalendarEvent.from_wire({"calendarIds": {}}).calendars == []

    def test_jscalendar_properties_are_read_by_their_wire_name(self):
        # The JSCalendar body is not modelled field by field; it rides in extra
        # and is addressed by the exact name the draft uses.
        event = CalendarEvent.from_wire(
            {"id": "e1", "title": "Standup", "recurrenceRule": {"frequency": "weekly"}}
        )
        assert event.jscalendar("recurrenceRule") == {"frequency": "weekly"}
        assert event.jscalendar("title") == "Standup"

    def test_an_unknown_jscalendar_property_is_none(self):
        assert CalendarEvent.from_wire({"id": "e1"}).jscalendar("recurrenceRule") is None

    def test_an_event_with_no_extras_at_all_reports_none(self):
        assert CalendarEvent().jscalendar("recurrenceRule") is None

    def test_a_jscalendar_property_is_not_reachable_as_an_attribute(self):
        # `event.recurrence_rule` would be a plausible-looking AttributeError in
        # production code; jscalendar() is the only supported reader.
        event = CalendarEvent.from_wire({"recurrenceRule": {"frequency": "weekly"}})
        assert not hasattr(event, "recurrence_rule")

    def test_the_jscalendar_body_round_trips_losslessly(self):
        # The draft is still moving, so unmodelled properties must survive a
        # read-modify-write rather than being stripped on the way back out.
        wire = {
            "id": "e1",
            "@type": "Event",
            "title": "Standup",
            "recurrenceOverrides": {"2024-03-04T09:00:00": {"title": "Retro"}},
        }
        assert CalendarEvent.from_wire(wire).to_wire() == wire

    def test_utc_start_and_end_are_absent_unless_requested(self):
        event = CalendarEvent.from_wire({"id": "e1"})
        assert event.utc_start is None
        assert event.utc_end is None
        assert "utcStart" not in event.to_wire()

    def test_an_expanded_instance_reports_null_recurrence_properties(self):
        # /get on a synthetic id returns these as null; writing that shape back
        # wipes the series, so the null must be visible rather than smoothed away.
        occurrence = CalendarEvent.from_wire(
            {
                "id": "e1;2024-03-04T09:00:00",
                "baseEventId": "e1",
                "recurrenceRule": None,
                "recurrenceOverrides": None,
            }
        )
        assert occurrence.is_occurrence is True
        assert occurrence.jscalendar("recurrenceRule") is None
        assert "recurrenceRule" in occurrence.to_wire()


class TestBusyPeriod:
    def test_an_absent_busy_status_means_unavailable(self):
        # Not unknown and not free: rendering an unlabelled span as free is how a
        # scheduling UI offers a slot that is already booked.
        assert BusyPeriod.from_wire({"utcStart": "2024-03-04T09:00:00Z"}).status == "unavailable"
        assert DEFAULT_BUSY_STATUS == "unavailable"

    def test_a_stated_status_wins(self):
        assert BusyPeriod.from_wire({"busyStatus": "tentative"}).status == "tentative"
        assert BusyPeriod.from_wire({"busyStatus": "confirmed"}).status == "confirmed"

    def test_the_window_is_carried_as_given(self):
        period = BusyPeriod.from_wire(
            {"utcStart": "2024-03-04T09:00:00Z", "utcEnd": "2024-03-04T10:00:00Z"}
        )
        assert period.utc_start == "2024-03-04T09:00:00Z"
        assert period.utc_end == "2024-03-04T10:00:00Z"

    def test_details_arrive_as_a_calendar_event(self):
        period = BusyPeriod.from_wire(
            {"busyStatus": "confirmed", "event": {"id": "e1"}, "accountId": "a1"}
        )
        assert period.event is not None
        assert period.event.id == "e1"
        assert period.account_id == "a1"

    def test_no_event_means_no_account(self):
        period = BusyPeriod.from_wire({"busyStatus": "confirmed"})
        assert period.event is None
        assert period.account_id is None

    def test_the_merge_precedence_puts_unavailable_above_tentative(self):
        # §2.2's order is deliberately not alphabetical, and getting it wrong
        # merges a real booking away under a maybe.
        assert BUSY_PRECEDENCE == ("confirmed", "unavailable", "tentative")
        assert BUSY_PRECEDENCE.index("unavailable") < BUSY_PRECEDENCE.index("tentative")


class TestAvailabilityResponse:
    def test_the_wire_list_key_is_aliased(self):
        response = AvailabilityResponse.from_wire(
            {"list": [{"utcStart": "2024-03-04T09:00:00Z", "busyStatus": "confirmed"}]}
        )
        assert len(response.items) == 1
        assert response.items[0].status == "confirmed"

    def test_an_empty_response_is_a_list_not_a_none(self):
        # There is no notFound and no state to sync against, so callers iterate
        # unconditionally; a None default would make every one of them guard.
        assert AvailabilityResponse().items == []

    def test_each_response_gets_its_own_list(self):
        # A shared mutable default would leak busy periods between calls.
        first = AvailabilityResponse()
        first.items.append(BusyPeriod())
        assert AvailabilityResponse().items == []


class TestCalendarEventNotification:
    def test_an_update_describes_the_state_before_the_change(self):
        # `event` is the OLD state here. Rendering it as "the new event" is
        # plausible, silent and completely wrong.
        notification = CalendarEventNotification.from_wire(
            {
                "id": "n1",
                "type": "updated",
                "event": {"title": "Old"},
                "eventPatch": {"title": "New"},
            }
        )
        assert notification.describes_the_state_before is True
        assert notification.event == {"title": "Old"}
        assert notification.event_patch == {"title": "New"}

    def test_a_destroy_also_describes_the_state_before(self):
        notification = CalendarEventNotification.from_wire({"type": "destroyed"})
        assert notification.describes_the_state_before is True

    def test_a_create_describes_the_state_after(self):
        notification = CalendarEventNotification.from_wire(
            {"type": "created", "event": {"title": "New"}}
        )
        assert notification.describes_the_state_before is False

    def test_an_absent_type_claims_nothing(self):
        assert CalendarEventNotification().describes_the_state_before is False

    def test_the_notification_names_the_base_event(self):
        # Always the base event, even when one occurrence changed - so fetching
        # calendarEventId back gives the series, not the instance.
        notification = CalendarEventNotification.from_wire(
            {"type": "updated", "calendarEventId": "e1", "changedBy": {"name": "Alice"}}
        )
        assert notification.calendar_event_id == "e1"
        assert notification.changed_by == {"name": "Alice"}

    def test_a_patch_is_absent_outside_an_update(self):
        assert CalendarEventNotification.from_wire({"type": "created"}).event_patch is None


class TestParsedEvents:
    def test_one_blob_yields_an_array_of_events(self):
        # The shape most easily got wrong: an iCalendar file holds many VEVENTs,
        # so `parsed[blobId]` is a list. Reading it as one event drops the rest.
        parsed = ParsedEvents.from_wire(
            {"accountId": "a1", "parsed": {"G1": [{"id": "e1"}, {"id": "e2"}]}}
        )
        events = parsed.events_of("G1")
        assert [event.id for event in events] == ["e1", "e2"]

    def test_a_blob_that_yielded_nothing_reads_as_empty(self):
        parsed = ParsedEvents.from_wire({"parsed": {"G1": []}})
        assert parsed.events_of("G1") == []

    def test_an_unknown_blob_reads_as_empty(self):
        parsed = ParsedEvents.from_wire({"parsed": {"G1": [{"id": "e1"}]}})
        assert parsed.events_of("G2") == []

    def test_events_of_survives_a_response_with_no_parsed_map(self):
        response = ParsedEvents.from_wire({"notFound": ["G9"], "notParsable": ["G8"]})
        assert response.events_of("G1") == []
        assert response.not_found == ["G9"]
        assert response.not_parsable == ["G8"]


class TestDurationParsing:
    def test_the_single_unit_forms_parse(self):
        assert duration_seconds("P1D") == 86400
        assert duration_seconds("PT1H") == 3600
        assert duration_seconds("P1W") == 604800
        assert duration_seconds("PT30M") == 1800
        assert duration_seconds("PT45S") == 45

    def test_units_combine(self):
        assert duration_seconds("P1DT2H") == 86400 + 7200
        assert duration_seconds("P3DT4H5M6S") == 3 * 86400 + 4 * 3600 + 5 * 60 + 6

    def test_zero_is_a_duration_not_a_failure(self):
        # `PT0S` parses to 0, which is falsy - so the caller must distinguish it
        # from the None that means "unparseable".
        assert duration_seconds("PT0S") == 0

    def test_years_and_months_are_refused_rather_than_approximated(self):
        # They have no fixed length. Guessing 365 days would make the window
        # checks silently wrong near the boundary instead of visibly unknown.
        assert duration_seconds("P1Y") is None
        assert duration_seconds("P6M") is None

    def test_a_bare_p_is_not_a_duration(self):
        # The regex matches it (every component is optional), so it is refused
        # explicitly; without that it would parse as zero seconds and cap every
        # window at nothing.
        assert duration_seconds("P") is None

    def test_junk_is_refused(self):
        assert duration_seconds("junk") is None
        assert duration_seconds("") is None
        assert duration_seconds("1D") is None
        assert duration_seconds("P1.5D") is None

    def test_minutes_are_distinguished_from_months_by_the_t(self):
        # `P1M` is one month and `PT1M` is one minute; conflating them is off by
        # a factor of forty-three thousand.
        assert duration_seconds("P1M") is None
        assert duration_seconds("PT1M") == 60

    def test_a_negative_duration_is_refused(self):
        # JSCalendar alert triggers are signed, capability limits are not.
        assert duration_seconds("-PT10M") is None


class TestCapabilityObjects:
    def test_the_advertised_calendar_fields_are_read(self):
        capability = CalendarsCapability.of(
            {
                "maxCalendarsPerEvent": 1,
                "minDateTime": "0001-01-01T00:00:00",
                "maxDateTime": "9999-12-31T23:59:59",
                "maxExpandedQueryDuration": "P1Y",
                "maxParticipantsPerEvent": 100,
                "mayCreateCalendar": True,
            }
        )
        assert capability.max_calendars_per_event == 1
        assert capability.max_expanded_query_duration == "P1Y"
        assert capability.may_create_calendar is True

    def test_an_absent_object_grants_no_permission_and_states_no_limit(self):
        capability = CalendarsCapability.of({})
        assert capability.may_create_calendar is False
        assert capability.max_calendars_per_event is None
        assert capability.max_expanded_query_duration is None

    def test_a_malformed_calendars_object_degrades_rather_than_failing_the_session(self):
        # One bad capability object should not make an otherwise working server
        # unusable. Both halves matter: a bad field value raises ValidationError,
        # a non-mapping raises from dict() before pydantic ever sees it.
        assert CalendarsCapability.of({"maxCalendarsPerEvent": "lots"}).may_create_calendar is False
        assert CalendarsCapability.of(7).max_calendars_per_event is None
        assert CalendarsCapability.of("not an object").max_calendars_per_event is None

    def test_the_availability_limit_is_read(self):
        assert AvailabilityCapability.of({"maxAvailabilityDuration": "P8W"})

    def test_an_absent_availability_object_states_no_limit(self):
        assert AvailabilityCapability.of({}).max_availability_duration is None

    def test_a_malformed_availability_object_degrades_too(self):
        assert (
            AvailabilityCapability.of({"maxAvailabilityDuration": 7.5}).max_availability_duration
            is None
        )
        assert AvailabilityCapability.of(None).max_availability_duration is None


class TestExpandWindowGate:
    def test_a_window_inside_the_limit_passes(self):
        check_expand_window(86400, CalendarsCapability.of({"maxExpandedQueryDuration": "P7D"}))

    def test_exactly_the_limit_passes(self):
        check_expand_window(604800, CalendarsCapability.of({"maxExpandedQueryDuration": "P1W"}))

    def test_a_window_over_the_limit_is_refused(self):
        capability = CalendarsCapability.of({"maxExpandedQueryDuration": "P1W"})
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_window(604801, capability)
        assert excinfo.value.urn == CALENDARS_URN
        assert excinfo.value.field == "maxExpandedQueryDuration"
        assert excinfo.value.advertised == "P1W"
        assert excinfo.value.requested == 604801

    def test_no_advertised_limit_means_no_check(self):
        check_expand_window(10**9, CalendarsCapability.of({}))

    def test_an_unparseable_limit_is_not_enforced(self):
        # `P1Y` is a legal thing for a server to advertise and an impossible thing
        # to compare against. Refusing the query would be worse than letting the
        # server answer expandDurationTooLarge itself.
        check_expand_window(10**9, CalendarsCapability.of({"maxExpandedQueryDuration": "P1Y"}))


class TestAvailabilityWindowGate:
    def test_a_window_inside_the_limit_passes(self):
        check_availability_window(
            3600, AvailabilityCapability.of({"maxAvailabilityDuration": "P1D"})
        )

    def test_exactly_the_limit_passes(self):
        check_availability_window(
            86400, AvailabilityCapability.of({"maxAvailabilityDuration": "P1D"})
        )

    def test_a_window_over_the_limit_is_refused(self):
        capability = AvailabilityCapability.of({"maxAvailabilityDuration": "P1D"})
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_availability_window(86400.5, capability)
        assert excinfo.value.urn == AVAILABILITY_URN
        assert excinfo.value.field == "maxAvailabilityDuration"

    def test_no_advertised_limit_means_no_check(self):
        check_availability_window(10**9, AvailabilityCapability.of({}))

    def test_an_unparseable_limit_is_not_enforced(self):
        check_availability_window(
            10**9, AvailabilityCapability.of({"maxAvailabilityDuration": "P3M"})
        )


class TestExpandFilterGate:
    def test_a_bare_condition_with_both_bounds_passes(self):
        check_expand_filter(
            {"before": "2024-04-01T00:00:00", "after": "2024-03-01T00:00:00"}, expand=True
        )

    def test_nothing_is_checked_without_expand_recurrences(self):
        # An ordinary query may use any filter shape at all, including none.
        check_expand_filter({"operator": "AND", "conditions": []}, expand=False)
        check_expand_filter(None, expand=False)

    def test_a_filter_operator_is_forbidden_entirely(self):
        # Not merely "no operator at the top level": §5.11 forbids the whole
        # FilterOperator grammar, so an AND wrapping two valid conditions is still
        # a client error.
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_filter(
                {
                    "operator": "AND",
                    "conditions": [
                        {"after": "2024-03-01T00:00:00"},
                        {"before": "2024-04-01T00:00:00"},
                    ],
                },
                expand=True,
            )
        assert excinfo.value.urn == CALENDARS_URN
        assert excinfo.value.requested == "AND"

    def test_even_a_null_operator_key_is_refused(self):
        # The check is key presence, not truthiness: a serialiser that emits
        # `"operator": null` still produces a FilterOperator on the wire.
        with pytest.raises(CapabilityFieldError):
            check_expand_filter(
                {"operator": None, "before": "2024-04-01T00:00:00", "after": "2024-03-01T00:00:00"},
                expand=True,
            )

    def test_a_non_object_filter_is_refused(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_filter(None, expand=True)
        # The type *name*, not the type: a bare `type(x)` renders as
        # "<class 'NoneType'>" in the message, which reads like a bug report.
        assert excinfo.value.requested == "NoneType"

    def test_a_list_of_conditions_is_not_a_filter_condition(self):
        with pytest.raises(CapabilityFieldError):
            check_expand_filter([{"before": "x", "after": "y"}], expand=True)

    def test_both_bounds_are_required(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_filter({}, expand=True)
        assert excinfo.value.requested == "missing before, after"

    def test_a_missing_before_is_named_alone(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_filter({"after": "2024-03-01T00:00:00"}, expand=True)
        assert excinfo.value.requested == "missing before"

    def test_a_missing_after_is_named_alone(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_expand_filter({"before": "2024-04-01T00:00:00"}, expand=True)
        assert excinfo.value.requested == "missing after"

    def test_a_null_bound_still_counts_as_present(self):
        # Presence, not usefulness - the server decides whether null is a legal
        # LocalDateTime, and guessing here would reject a shape the draft allows.
        check_expand_filter({"before": None, "after": None}, expand=True)


class TestCalendarsSpec:
    def test_the_three_urns_are_separate_capabilities(self):
        # A server may implement any one without the others, so conflating them
        # produces a `using` array it will reject.
        assert CALENDARS.urn == CALENDARS_URN
        assert CALENDARS_PARSE.urn == CALENDARS_PARSE_URN
        assert AVAILABILITY.urn == AVAILABILITY_URN
        assert len({CALENDARS.urn, CALENDARS_PARSE.urn, AVAILABILITY.urn}) == 3

    def test_all_three_are_experimental(self):
        # The draft is blocked on jscalendarbis, so none of this is under the
        # SemVer promise and none of it resolves without an explicit opt-in.
        assert CALENDARS.experimental is True
        assert CALENDARS_PARSE.experimental is True
        assert AVAILABILITY.experimental is True

    def test_only_the_data_capability_gets_a_client_attribute(self):
        assert CALENDARS.attr == "calendars"
        assert CALENDARS_PARSE.attr is None
        assert AVAILABILITY.attr is None

    def test_there_is_no_calendar_query_method(self):
        # §4.1 references one but never defines it; fetching all with ids=null is
        # the way. Declaring it would put a method on the façade that no server has.
        assert CALENDARS.method("Calendar/query") is None
        assert CALENDARS.method("Calendar/queryChanges") is None
        assert CALENDARS.method("Calendar/get") is not None

    def test_calendar_events_do_have_a_query(self):
        query = CALENDARS.method("CalendarEvent/query")
        assert query is not None
        assert query.kind is MethodKind.QUERY
        assert set(query.extra_args) == {"expandRecurrences", "timeZone"}

    def test_the_mutating_methods_are_marked(self):
        for name in ("Calendar/set", "ParticipantIdentity/set", "CalendarEvent/set"):
            method = CALENDARS.method(name)
            assert method is not None
            assert method.mutating is True

    def test_scheduling_messages_are_opt_in_on_set(self):
        # §5.9 defaults it to false, so creating an event with participants and
        # forgetting the argument sends no invitations at all and reports no error.
        method = CALENDARS.method("CalendarEvent/set")
        assert method is not None
        assert "sendSchedulingMessages" in method.extra_args

    def test_calendar_destroy_declares_its_escape_hatch(self):
        # Without onDestroyRemoveEvents a non-empty calendar answers calendarHasEvent.
        method = CALENDARS.method("Calendar/set")
        assert method is not None
        assert set(method.extra_args) == {"onDestroyRemoveEvents", "onSuccessSetIsDefault"}

    def test_event_get_declares_the_override_windowing_arguments(self):
        method = CALENDARS.method("CalendarEvent/get")
        assert method is not None
        assert set(method.extra_args) == {
            "recurrenceOverridesBefore",
            "recurrenceOverridesAfter",
            "reduceParticipants",
            "timeZone",
        }

    def test_notifications_are_destroy_only_but_fully_queryable(self):
        for name in (
            "CalendarEventNotification/get",
            "CalendarEventNotification/changes",
            "CalendarEventNotification/query",
            "CalendarEventNotification/queryChanges",
            "CalendarEventNotification/set",
        ):
            assert CALENDARS.method(name) is not None

    def test_the_data_types_are_bound_to_their_models(self):
        for name, model in (
            ("Calendar", Calendar),
            ("CalendarEvent", CalendarEvent),
            ("ParticipantIdentity", ParticipantIdentity),
            ("CalendarEventNotification", CalendarEventNotification),
        ):
            spec = CALENDARS.data_type(name)
            assert spec is not None
            assert spec.model is model

    def test_only_calendars_are_shareable(self):
        calendar = CALENDARS.data_type("Calendar")
        event = CALENDARS.data_type("CalendarEvent")
        assert calendar is not None
        assert event is not None
        assert calendar.shareable is True
        assert event.shareable is False

    def test_calendar_alert_is_a_push_only_type(self):
        # It has no methods, but must still be a legal PushSubscription.types and
        # EventSource types value - and the two spellings differ in case.
        alert = CALENDARS.data_type(CALENDAR_ALERT_TYPE)
        assert alert is not None
        assert alert.push_only is True
        assert CALENDAR_ALERT_TYPE == "CalendarAlert"
        assert CALENDAR_ALERT_EVENT == "calendarAlert"

    def test_the_account_capability_models_are_declared(self):
        assert CALENDARS.account_value is CalendarsCapability
        assert AVAILABILITY.account_value is AvailabilityCapability
        assert CALENDARS_PARSE.account_value is None

    def test_parse_requires_the_calendars_urn(self):
        # It is a separate capability because servers may omit it, but it is
        # useless alone: the CalendarEvent type is defined next door.
        assert CALENDARS_PARSE.requires == frozenset({CALENDARS_URN})

    def test_parse_is_custom_with_its_own_response_model(self):
        method = CALENDARS_PARSE.method("CalendarEvent/parse")
        assert method is not None
        assert method.kind is MethodKind.CUSTOM
        assert method.response_model is ParsedEvents
        assert set(method.extra_args) == {"blobIds", "properties"}

    def test_parse_lives_nowhere_else(self):
        assert CALENDARS.method("CalendarEvent/parse") is None

    def test_availability_requires_the_principals_urn(self):
        # It hangs off Principal, but RFC 9670 defines no custom methods at all -
        # so a client looking for availability in the sharing spec will not find it.
        assert AVAILABILITY.requires == frozenset({"urn:ietf:params:jmap:principals"})

    def test_get_availability_is_custom_with_its_own_response_model(self):
        method = AVAILABILITY.method("Principal/getAvailability")
        assert method is not None
        assert method.kind is MethodKind.CUSTOM
        assert method.response_model is AvailabilityResponse

    def test_get_availability_takes_one_id_not_many(self):
        # Named like a /get but shaped nothing like one: singular `id`, a bare
        # list back, no state and no notFound.
        method = AVAILABILITY.method("Principal/getAvailability")
        assert method is not None
        assert "id" in method.extra_args
        assert "ids" not in method.extra_args
        assert set(method.extra_args) == {
            "id",
            "utcStart",
            "utcEnd",
            "showDetails",
            "eventProperties",
        }

    def test_availability_declares_no_data_types_of_its_own(self):
        assert AVAILABILITY.data_types == ()
        assert CALENDARS_PARSE.data_types == ()


class TestMalformedDurations:
    """A duration the library cannot parse must decline to check, not cap at zero.

    Each of these used to sum to zero, which reads as a real limit of zero seconds
    and rejects every query - the opposite of degrading gracefully.
    """

    def test_a_bare_designator_is_not_a_duration(self):
        assert duration_seconds("P") is None

    def test_a_dangling_time_designator_is_not_a_duration(self):
        assert duration_seconds("PT") is None

    def test_a_dangling_time_designator_after_days_is_not_a_duration(self):
        assert duration_seconds("P1DT") is None

    def test_a_dangling_time_designator_after_weeks_is_not_a_duration(self):
        assert duration_seconds("P1WT") is None

    def test_weeks_combined_with_days_are_accepted(self):
        # The ABNF makes them exclusive; a real Stalwart v0.16 advertises
        # `maxExpandedQueryDuration: "P52W1D"` regardless. This parser only ever
        # reads what a server sent, and rejecting the combination would silently
        # switch the limit check off against the most likely server.
        assert duration_seconds("P52W1D") == 52 * 604800 + 86400
        assert duration_seconds("P1W1D") == 604800 + 86400

    def test_weeks_combined_with_times_are_accepted(self):
        assert duration_seconds("P1WT2H") == 604800 + 7200

    def test_an_unparseable_limit_disables_the_check(self):
        # The point of returning None: a limit nobody can read must not become a
        # limit of zero.
        check_expand_window(10**6, CalendarsCapability.of({"maxExpandedQueryDuration": "PT"}))
        check_availability_window(
            10**6, AvailabilityCapability.of({"maxAvailabilityDuration": "P"})
        )

    def test_the_forms_that_do_parse_still_do(self):
        assert duration_seconds("P1W") == 604800
        assert duration_seconds("P1D") == 86400
        assert duration_seconds("PT1H") == 3600
        assert duration_seconds("P1DT2H30M") == 86400 + 7200 + 1800

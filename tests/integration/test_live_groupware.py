"""Contacts (RFC 9610) and calendars (draft-ietf-jmap-calendars) against a real server.

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.
Calendars track an Internet-Draft, so the client connects with
``experimental=True``, and every test skips on the method it needs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from jmap.capabilities.calendars import CALENDARS_URN, CalendarsCapability, duration_seconds
from jmap.core.errors import CapabilityFieldError
from jmap.core.ijson import format_local_date, parse_local_date
from jmap.models.calendars import CalendarEvent
from jmap.models.contacts import ContactCard
from jmap.models.jscalendar import (
    AbsoluteTrigger,
    Alert,
    Location,
    NDay,
    OffsetTrigger,
    Participant,
    RecurrenceRule,
)
from jmap.models.jscontact import (
    Anniversary,
    EmailAddress,
    Name,
    NameComponent,
    Organization,
    PartialDate,
    Timestamp,
    Title,
)
from tests.integration.conftest import connect

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jmap.client import JMAPClient

pytestmark = pytest.mark.integration

JMAP_URL = os.environ.get("JMAP_TEST_URL", "")
ALICE = os.environ.get("JMAP_TEST_USER", "")
ALICE_PASSWORD = os.environ.get("JMAP_TEST_PASS", "")

requires_server = pytest.mark.skipif(
    not (JMAP_URL and ALICE and ALICE_PASSWORD),
    reason="set JMAP_TEST_URL, JMAP_TEST_USER and JMAP_TEST_PASS to run",
)

START = "2026-10-01T00:00:00"


def requires_method(client: JMAPClient, method: str) -> None:
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with connect(ALICE, ALICE_PASSWORD, experimental=True) as client:
        yield client


def calendars_capability(client: JMAPClient) -> CalendarsCapability:
    return CalendarsCapability.of(
        client.session.capability_value(
            CALENDARS_URN, client.session.capability_account(CALENDARS_URN, client.default_account)
        )
    )


@requires_server
class TestExpandingQueries:
    def test_a_week_in_local_time_is_answered(self, alice):
        requires_method(alice, "CalendarEvent/query")
        with alice.batch() as batch:
            found = batch.calendars.calendar_event.query(
                filter={"after": START, "before": "2026-10-08T00:00:00"},
                expandRecurrences=True,
            )
        assert isinstance(found.result.ids, list)

    def test_a_window_past_the_advertised_limit_never_leaves(self, alice):
        requires_method(alice, "CalendarEvent/query")
        limit = calendars_capability(alice).max_expanded_query_duration
        bound = duration_seconds(limit) if limit is not None else None
        if bound is None:
            pytest.skip(f"no maxExpandedQueryDuration to measure against: {limit!r}")
        before = format_local_date(parse_local_date(START) + timedelta(seconds=bound + 1))
        with (
            alice.batch() as batch,
            pytest.raises(CapabilityFieldError, match="maxExpandedQueryDuration"),
        ):
            batch.calendars.calendar_event.query(
                filter={"after": START, "before": before}, expandRecurrences=True
            )


@requires_server
class TestContactCards:
    def test_a_card_built_from_models_comes_back_as_models(self, alice):
        requires_method(alice, "ContactCard/set")
        with alice.batch() as batch:
            books = batch.contacts.address_book.get(ids=None)
        book = next(book for book in books.result.items if book.is_default)
        card = ContactCard(
            address_book_ids={str(book.id): True},
            kind="individual",
            name=Name(
                components=[
                    NameComponent(kind="given", value="Ada"),
                    NameComponent(kind="surname", value="Lovelace"),
                ],
                is_ordered=True,
            ),
            emails={"e1": EmailAddress(address="ada@example.com", contexts={"work": True})},
            organizations={"o1": Organization(name="Analytical Society")},
            titles={"t1": Title(name="Mathematician", organization_id="o1")},
            anniversaries={
                "b1": Anniversary(kind="birth", date=PartialDate(year=1815, month=12, day=10)),
                "d1": Anniversary(kind="death", date=Timestamp(utc="1852-11-27T12:00:00Z")),
            },
        )
        with alice.batch() as batch:
            created = batch.contacts.contact_card.set(create={"c": card})
        card_id = str(created.result.created_id("c"))
        try:
            with alice.batch() as batch:
                fetched = batch.contacts.contact_card.get(ids=[card_id])
            stored = fetched.result.items[0]
            # Everything the server sends back is something RFC 9553 models.
            assert stored.model_extra == {}
            # A server may fill in more than was sent - Stalwart adds the full
            # name and a title's default kind - so only what was sent is compared.
            assert stored.name is not None
            assert card.name is not None
            assert stored.name.components == card.name.components
            assert stored.emails == card.emails
            assert stored.titles is not None
            title = stored.titles["t1"]
            assert (title.name, title.organization_id) == ("Mathematician", "o1")
            assert stored.anniversaries is not None
            birth = stored.anniversaries["b1"].date
            assert isinstance(birth, PartialDate)
            assert (birth.year, birth.month, birth.day) == (1815, 12, 10)
            death = stored.anniversaries["d1"].date
            assert isinstance(death, Timestamp)
            assert death.utc == datetime(1852, 11, 27, 12, tzinfo=UTC)
        finally:
            with alice.batch() as batch:
                batch.contacts.contact_card.set(destroy=[card_id])


@requires_server
class TestCalendarEvents:
    def test_an_event_built_from_models_comes_back_as_models(self, alice):
        requires_method(alice, "CalendarEvent/set")
        with alice.batch() as batch:
            calendars = batch.calendars.calendar.get(ids=None)
        calendar = next(calendar for calendar in calendars.result.items if calendar.is_default)
        event = CalendarEvent(
            calendar_ids={str(calendar.id): True},
            title="Engine review",
            start="2026-10-05T10:00:00",
            time_zone="Europe/London",
            duration="PT1H",
            recurrence_rule=RecurrenceRule(frequency="weekly", by_day=[NDay(day="mo")], count=4),
            locations={"l1": Location(name="Library")},
            participants={
                "p1": Participant(
                    calendar_address=f"mailto:{ALICE}", roles={"owner": True, "attendee": True}
                )
            },
            alerts={
                "a1": Alert(trigger=OffsetTrigger(offset="-PT15M")),
                "a2": Alert(trigger=AbsoluteTrigger(when="2026-10-05T08:00:00Z")),
            },
        )
        with alice.batch() as batch:
            created = batch.calendars.calendar_event.set(create={"e": event})
        event_id = str(created.result.created_id("e"))
        try:
            with alice.batch() as batch:
                fetched = batch.calendars.calendar_event.get(ids=[event_id])
            stored = fetched.result.items[0]
            assert stored.model_extra == {}
            assert stored.uid  # the server made one
            assert stored.recurrence_rule is not None
            assert stored.recurrence_rule.by_day == [NDay(day="mo")]
            assert stored.locations is not None
            assert stored.locations["l1"].name == "Library"
            assert stored.participants is not None
            owner = stored.participants["p1"]
            assert owner.calendar_address == f"mailto:{ALICE}"
            assert owner.roles == {"owner": True, "attendee": True}
            assert stored.alerts is not None
            assert isinstance(stored.alerts["a1"].trigger, OffsetTrigger)
            absolute = stored.alerts["a2"].trigger
            assert isinstance(absolute, AbsoluteTrigger)
            assert absolute.when == datetime(2026, 10, 5, 8, tzinfo=UTC)
        finally:
            with alice.batch() as batch:
                batch.calendars.calendar_event.set(destroy=[event_id])

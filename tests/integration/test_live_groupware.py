"""Calendars (draft-ietf-jmap-calendars) against a real server.

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.
Calendars track an Internet-Draft, so the client connects with
``experimental=True``, and every test skips on the method it needs.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from jmap.capabilities.calendars import CALENDARS_URN, CalendarsCapability, duration_seconds
from jmap.core.errors import CapabilityFieldError
from jmap.core.ijson import format_local_date, parse_local_date
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

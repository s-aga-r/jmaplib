"""Typed builders for the mail, calendar and contact methods with no standard shape.

``Email/import``, ``Email/parse``, ``SearchSnippet/get``, ``CalendarEvent/parse``,
``ContactCard/parse`` and ``Principal/getAvailability``. The last three live in
companion capabilities with no namespace of their own, so they are lent to the
namespace holding their data type. ``CalendarEvent/query`` has the standard shape
but checks an expanding query first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from jmap.api.namespace import Namespaces
from jmap.batch import Batch
from jmap.capabilities.calendars import AVAILABILITY_URN, CALENDARS_PARSE_URN, CALENDARS_URN
from jmap.capabilities.contacts import CONTACTS_PARSE_URN
from jmap.core.errors import CapabilityFieldError
from jmap.core.ids import Id
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.calendars import AvailabilityResponse, ParsedEvents
from jmap.models.contacts import ParsedCards
from jmap.models.mail.irregular import (
    EmailImport,
    EmailImportResponse,
    ParsedEmails,
    SearchSnippetResponse,
)

if TYPE_CHECKING:
    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.core.invocation import Handle

URNS = (
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    CALENDARS_URN,
    CALENDARS_PARSE_URN,
    "urn:ietf:params:jmap:principals",
    AVAILABILITY_URN,
    "urn:ietf:params:jmap:contacts",
    CONTACTS_PARSE_URN,
)


def active(
    *,
    without: tuple[str, ...] = (),
    availability: dict[str, Any] | None = None,
    calendars: dict[str, Any] | None = None,
) -> ActiveCapabilities:
    urns = [urn for urn in URNS if urn not in without]
    account_capabilities: dict[str, Any] = {urn: {} for urn in urns}
    if availability is not None:
        account_capabilities[AVAILABILITY_URN] = availability
    if calendars is not None:
        account_capabilities[CALENDARS_URN] = calendars
    session = Session.from_wire(
        {
            "capabilities": {urn: {} for urn in urns},
            "accounts": {"a": {"name": "alice", "accountCapabilities": account_capabilities}},
            "primaryAccounts": dict.fromkeys(urns, "a"),
        }
    )
    return default_registry().resolve(session, Id("a"), experimental=True)


def namespaces(**options: Any) -> Any:
    capabilities = active(**options)
    return Namespaces(Batch(capabilities), capabilities)


def wire(handle: Handle[Any]) -> dict[str, Any]:
    arguments = handle.call.to_wire_arguments()
    arguments.pop("accountId")
    return arguments


class TestEmailImport:
    def test_models_and_mappings_both_go_out_as_wire_objects(self):
        handle = namespaces().mail.email.import_(
            emails={
                "k1": EmailImport(blob_id="B1", mailbox_ids={"inbox": True}),
                "k2": {"blobId": "B2", "mailboxIds": {"inbox": True}, "keywords": {"$seen": True}},
            },
            if_in_state="s1",
        )
        assert handle.call.name == "Email/import"
        assert wire(handle) == {
            "emails": {
                "k1": {"blobId": "B1", "mailboxIds": {"inbox": True}},
                "k2": {"blobId": "B2", "mailboxIds": {"inbox": True}, "keywords": {"$seen": True}},
            },
            "ifInState": "s1",
        }

    def test_an_import_filed_in_no_mailbox_cannot_be_made(self):
        with pytest.raises(ValidationError):
            EmailImport(blob_id="B1", mailbox_ids={})

    def test_an_email_to_import_is_an_object(self):
        with pytest.raises(ValidationError, match=r"Email\.import_"):
            namespaces().mail.email.import_(emails={"k1": "B1"})

    def test_the_answer_half_succeeds_like_a_set(self):
        handle = namespaces().mail.email.import_(
            emails={"k1": {"blobId": "B1", "mailboxIds": {"inbox": True}}}
        )
        result = handle.call.parse(
            {
                "accountId": "a",
                "oldState": "1",
                "newState": "2",
                "created": {"k1": {"id": "M1", "blobId": "B1", "threadId": "T1", "size": 42}},
                "notCreated": {"k2": {"type": "alreadyExists", "existingId": "M0"}},
            }
        )
        assert isinstance(result, EmailImportResponse)
        assert result.created_id("k1") == "M1"
        assert result.created_id("k2") is None
        assert result.has_errors
        assert result.creation_errors["k2"].existing_id == "M0"

    def test_null_categories_are_empty(self):
        result = EmailImportResponse.from_wire({"created": None, "notCreated": None})
        assert (result.created, result.has_errors) == ({}, False)


class TestEmailParse:
    def test_the_arguments_keep_their_wire_spelling(self):
        handle = namespaces().mail.email.parse(
            blob_ids=("B1", "B2"),
            properties=["subject"],
            fetch_html_body_values=True,
            max_body_value_bytes=256,
        )
        assert wire(handle) == {
            "blobIds": ["B1", "B2"],
            "properties": ["subject"],
            # Not fetchHtmlBodyValues, which the server would ignore.
            "fetchHTMLBodyValues": True,
            "maxBodyValueBytes": 256,
        }

    def test_a_body_limit_is_unsigned(self):
        with pytest.raises(ValidationError):
            namespaces().mail.email.parse(blob_ids=["B1"], max_body_value_bytes=-1)

    def test_the_answer_is_one_email_per_blob(self):
        handle = namespaces().mail.email.parse(blob_ids=["B1", "B2"])
        result = handle.call.parse(
            {"parsed": {"B1": {"subject": "Hi"}}, "notParsable": ["B2"], "notFound": None}
        )
        assert isinstance(result, ParsedEmails)
        email = result.email_of("B1")
        assert email is not None
        assert email.subject == "Hi"
        assert result.email_of("B2") is None
        assert result.not_parsable == ["B2"]

    def test_the_raw_path_still_answers_with_the_wire_dict(self):
        # Email/parse has always answered a raw call with a dict; the builder's
        # typed answer must not change that.
        handle = Batch(active()).add("Email/parse", {"blobIds": ["B1"]})
        assert handle.call.parse({"parsed": {}}) == {"parsed": {}}


class TestSearchSnippets:
    def test_the_query_feeds_the_snippets_in_one_request(self):
        batch = namespaces()
        query = batch.mail.email.query(filter={"text": "invoice"})
        handle = batch.mail.search_snippet.get(
            filter={"text": "invoice"}, email_ids=query.ref_ids()
        )
        arguments = wire(handle)
        assert arguments["filter"] == {"text": "invoice"}
        assert arguments["#emailIds"]["path"] == "/ids"

    def test_the_filter_must_be_given_even_if_null(self):
        batch = namespaces()
        with pytest.raises(TypeError, match=r"SearchSnippet\.get\(\)"):
            batch.mail.search_snippet.get(email_ids=["M1"])
        assert wire(batch.mail.search_snippet.get(filter=None, email_ids=["M1"]))["filter"] is None

    def test_the_answer_is_keyed_by_email(self):
        handle = namespaces().mail.search_snippet.get(filter=None, email_ids=["M1", "M2"])
        result = handle.call.parse(
            {"list": [{"emailId": "M1", "subject": "<mark>Invoice</mark>"}], "notFound": None}
        )
        assert isinstance(result, SearchSnippetResponse)
        snippet = result.snippet_of("M1")
        assert snippet is not None
        assert snippet.subject == "<mark>Invoice</mark>"
        assert result.snippet_of("M2") is None
        assert result.not_found == []


class TestCompanionsLendTheirMethods:
    def test_each_method_appears_on_its_type(self):
        batch = namespaces()
        assert callable(batch.calendars.calendar_event.parse)
        assert callable(batch.contacts.contact_card.parse)
        assert callable(batch.principals.principal.get_availability)

    @pytest.mark.parametrize(
        ("urn", "path"),
        [
            (CALENDARS_PARSE_URN, ("calendars", "calendar_event", "parse")),
            (CONTACTS_PARSE_URN, ("contacts", "contact_card", "parse")),
            (AVAILABILITY_URN, ("principals", "principal", "get_availability")),
        ],
    )
    def test_a_companion_the_server_lacks_lends_nothing(self, urn, path):
        capability, data_type, method = path
        entity = getattr(getattr(namespaces(without=(urn,)), capability), data_type)
        assert not hasattr(entity, method)


class TestParsingCalendarsAndCards:
    def test_events_are_a_list_per_blob(self):
        handle = namespaces().calendars.calendar_event.parse(blob_ids=["B1"], properties=None)
        assert wire(handle) == {"blobIds": ["B1"], "properties": None}
        result = handle.call.parse({"parsed": {"B1": [{"title": "a"}, {"title": "b"}]}})
        assert isinstance(result, ParsedEvents)
        assert len(result.events_of("B1")) == 2

    def test_a_card_is_one_per_blob(self):
        handle = namespaces().contacts.contact_card.parse(blob_ids=["B1"])
        assert wire(handle) == {"blobIds": ["B1"]}
        result = handle.call.parse({"parsed": {"B1": {"name": {"full": "Alice"}}}})
        assert isinstance(result, ParsedCards)
        assert result.card_of("B1") is not None

    def test_blob_ids_are_a_list(self):
        with pytest.raises(ValidationError):
            namespaces().contacts.contact_card.parse(blob_ids="B1")


class TestExpandingEventQueries:
    AFTER = "2026-10-01T00:00:00"

    def query(self, batch: Any = None, **arguments: Any) -> Any:
        return (batch or namespaces()).calendars.calendar_event.query(**arguments)

    def test_a_query_that_does_not_expand_is_not_checked(self):
        handle = self.query(filter={"operator": "OR", "conditions": []}, limit=5)
        assert wire(handle) == {"filter": {"operator": "OR", "conditions": []}, "limit": 5}

    @pytest.mark.parametrize(
        ("query_filter", "message"),
        [
            ({"operator": "AND", "conditions": []}, "bare FilterCondition"),
            ({"after": AFTER}, "missing before"),
            (None, "a FilterCondition"),
        ],
    )
    def test_the_filter_must_bound_what_is_expanded(self, query_filter, message):
        # §5.11: otherwise the server could be asked for infinitely many results.
        with pytest.raises(CapabilityFieldError, match=message):
            self.query(filter=query_filter, expandRecurrences=True)

    def test_expanding_needs_a_filter_at_all(self):
        with pytest.raises(CapabilityFieldError, match="a FilterCondition"):
            self.query(expandRecurrences=True)

    def test_a_window_wider_than_the_server_expands_is_refused(self):
        batch = namespaces(calendars={"maxExpandedQueryDuration": "P7D"})
        with pytest.raises(CapabilityFieldError, match="maxExpandedQueryDuration"):
            self.query(
                batch,
                filter={"after": self.AFTER, "before": "2026-10-08T00:00:01"},
                expandRecurrences=True,
            )

    def test_a_window_within_it_goes_out_as_given(self):
        batch = namespaces(calendars={"maxExpandedQueryDuration": "P7D"})
        window = {"after": self.AFTER, "before": "2026-10-08T00:00:00"}
        handle = self.query(batch, filter=window, expandRecurrences=True, timeZone="Europe/Paris")
        assert wire(handle) == {
            "filter": window,
            "expandRecurrences": True,
            "timeZone": "Europe/Paris",
        }

    @pytest.mark.parametrize(
        "window",
        [
            # UTCDates, which §5.11.1 does not allow: left for the server to judge.
            {"after": "2026-10-01T00:00:00Z", "before": "2027-10-01T00:00:00Z"},
            {"after": AFTER, "before": 20271001},
        ],
    )
    def test_bounds_it_cannot_read_are_the_servers_to_judge(self, window):
        batch = namespaces(calendars={"maxExpandedQueryDuration": "P7D"})
        handle = self.query(batch, filter=window, expandRecurrences=True)
        assert wire(handle)["filter"] == window

    def test_a_filter_from_a_back_reference_is_the_servers_to_judge(self):
        batch = namespaces(calendars={"maxExpandedQueryDuration": "P7D"})
        source = batch.calendars.calendar_event.get(ids=["E1"])
        handle = self.query(batch, filter=source.ref("/list/0/window"), expandRecurrences=True)
        assert "#filter" in wire(handle)

    def test_the_standard_arguments_are_still_checked(self):
        with pytest.raises(ValueError, match="either `anchor` or `position`"):
            self.query(anchor="E1", position=3)


class TestAvailability:
    START = "2026-10-01T00:00:00Z"

    def test_the_window_goes_out_as_given(self):
        handle = namespaces().principals.principal.get_availability(
            id="P1", utc_start=self.START, utc_end="2026-10-02T00:00:00Z", show_details=True
        )
        assert handle.call.name == "Principal/getAvailability"
        assert wire(handle) == {
            "id": "P1",
            "utcStart": self.START,
            "utcEnd": "2026-10-02T00:00:00Z",
            "showDetails": True,
        }

    def test_a_window_the_server_would_refuse_is_refused_here(self):
        batch = namespaces(availability={"maxAvailabilityDuration": "P1D"})
        with pytest.raises(CapabilityFieldError, match="maxAvailabilityDuration"):
            batch.principals.principal.get_availability(
                id="P1", utc_start=self.START, utc_end="2026-10-03T00:00:00Z"
            )

    def test_a_window_from_a_back_reference_is_the_servers_to_judge(self):
        batch = namespaces(availability={"maxAvailabilityDuration": "P1D"})
        source = batch.principals.principal.get(ids=["P1"])
        handle = batch.principals.principal.get_availability(
            id="P1", utc_start=self.START, utc_end=source.ref("/list/0/until")
        )
        assert "#utcEnd" in wire(handle)

    def test_the_instants_are_utc_dates(self):
        with pytest.raises(ValidationError, match="invalid date"):
            namespaces().principals.principal.get_availability(
                id="P1", utc_start="2026-10-01", utc_end="2026-10-02T00:00:00Z"
            )

    def test_the_answer_is_typed(self):
        handle = namespaces().principals.principal.get_availability(
            id="P1", utc_start=self.START, utc_end="2026-10-02T00:00:00Z"
        )
        result = handle.call.parse({"list": [{"utcStart": self.START, "utcEnd": self.START}]})
        assert isinstance(result, AvailabilityResponse)
        assert result.items[0].status == "unavailable"

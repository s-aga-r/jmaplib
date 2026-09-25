"""Typed builders for the mail methods with no standard shape.

``Email/import``, ``Email/parse`` and ``SearchSnippet/get``. Their builders
answer with typed models, while the raw ``batch.add`` path keeps answering with
the wire dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from jmap.api.namespace import Namespaces
from jmap.batch import Batch
from jmap.core.ids import Id
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.mail.irregular import (
    EmailImport,
    EmailImportResponse,
    ParsedEmails,
    SearchSnippetResponse,
)

if TYPE_CHECKING:
    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.core.invocation import Handle

URNS = ("urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail")


def active() -> ActiveCapabilities:
    capabilities: dict[str, Any] = {urn: {} for urn in URNS}
    session = Session.from_wire(
        {
            "capabilities": capabilities,
            "accounts": {"a": {"name": "alice", "accountCapabilities": capabilities}},
            "primaryAccounts": dict.fromkeys(URNS, "a"),
        }
    )
    return default_registry().resolve(session, Id("a"))


def namespaces() -> Any:
    capabilities = active()
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

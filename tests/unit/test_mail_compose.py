"""Header queries and Email creation constraints (RFC 8621 §4.1.2, §4.6)."""

from __future__ import annotations

import pytest

from jmap.models.mail.create import (
    InvalidEmailCreateError,
    validate_email_create,
    validate_email_creates,
)
from jmap.models.mail.headers import (
    HeaderForm,
    HeaderQuery,
    InvalidHeaderQueryError,
    addresses,
    date,
    grouped_addresses,
    message_ids,
    property_names,
    raw,
    text,
    urls,
)
from jmap.models.mail.objects import Email


class TestHeaderPropertyNames:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            (raw("Subject"), "header:Subject"),
            (text("Subject"), "header:Subject:asText"),
            (addresses("To"), "header:To:asAddresses"),
            (grouped_addresses("To"), "header:To:asGroupedAddresses"),
            (message_ids("References"), "header:References:asMessageIds"),
            (date("Date"), "header:Date:asDate"),
            (urls("List-Unsubscribe"), "header:List-Unsubscribe:asURLs"),
        ],
    )
    def test_each_form(self, query, expected):
        assert query.property_name == expected

    def test_all_suffix_comes_after_the_form(self):
        # RFC 8621 §4.1.2 fixes the order; reversing it is not a different
        # request, it is an invalid property name.
        assert text("Received", all=True).property_name == "header:Received:asText:all"

    def test_raw_plus_all_omits_the_form(self):
        # The raw form is spelled by omission.
        assert raw("Received", all=True).property_name == "header:Received:all"

    def test_str_is_the_property_name(self):
        assert str(text("Subject")) == "header:Subject:asText"

    def test_property_names_helper(self):
        assert property_names([raw("A"), text("B")]) == ["header:A", "header:B:asText"]


class TestHeaderValidation:
    @pytest.mark.parametrize("name", ["Subject", "X-Custom", "List-Id", "a", "X_1"])
    def test_valid_field_names(self, name):
        assert HeaderQuery(name).name == name

    @pytest.mark.parametrize("name", ["", "has space", "with:colon", "tab\there"])
    def test_invalid_field_names(self, name):
        with pytest.raises(InvalidHeaderQueryError, match="valid RFC 5322 field name"):
            HeaderQuery(name)

    def test_form_defaults_to_raw(self):
        assert HeaderQuery("Subject").form is HeaderForm.RAW

    def test_queries_are_hashable_values(self):
        assert text("Subject") == text("Subject")
        assert len({text("Subject"), text("Subject"), raw("Subject")}) == 2


class TestHeaderReading:
    """The request string and the lookup key are the same object, on purpose."""

    def test_reads_back_what_it_asked_for(self):
        email = Email.from_wire({"header:Subject:asText": "Hi"})
        assert text("Subject").read(email) == "Hi"

    def test_the_all_form_returns_a_list(self):
        email = Email.from_wire({"header:Received:all": ["hop1", "hop2"]})
        assert raw("Received", all=True).read(email) == ["hop1", "hop2"]

    def test_a_capitalisation_mismatch_reads_as_none(self):
        # The trap this class exists to prevent: the server echoes the property
        # name exactly as sent, so requesting one spelling and reading another
        # silently yields nothing.
        email = Email.from_wire({"header:Subject:asText": "Hi"})
        assert text("subject").read(email) is None

    def test_an_unfetched_header_reads_as_none(self):
        assert text("Subject").read(Email.from_wire({"id": "m1"})) is None


VALID_CREATE = {
    "mailboxIds": {"mb1": True},
    "keywords": {"$draft": True},
    "textBody": [{"partId": "t", "type": "text/plain"}],
    "bodyValues": {"t": {"value": "hello"}},
}


class TestEmailCreateAccepts:
    def test_a_well_formed_creation(self):
        validate_email_create(VALID_CREATE)

    def test_a_blob_backed_attachment_needs_no_body_value(self):
        validate_email_create(
            {
                "mailboxIds": {"mb1": True},
                "attachments": [{"blobId": "b1", "type": "application/pdf"}],
            }
        )

    def test_body_structure_form(self):
        validate_email_create(
            {
                "mailboxIds": {"mb1": True},
                "bodyStructure": {"partId": "t", "type": "text/plain"},
                "bodyValues": {"t": {"value": "hi"}},
            }
        )

    def test_multipart_intermediates_need_no_content(self):
        validate_email_create(
            {
                "mailboxIds": {"mb1": True},
                "bodyStructure": {
                    "type": "multipart/mixed",
                    "subParts": [
                        {"partId": "t", "type": "text/plain"},
                        {"blobId": "b1", "type": "image/png"},
                    ],
                },
                "bodyValues": {"t": {"value": "hi"}},
            }
        )

    def test_keywords_are_optional(self):
        validate_email_create({"mailboxIds": {"mb1": True}})


class TestEmailCreateRejects:
    @pytest.mark.parametrize("field", ["id", "blobId", "threadId", "size"])
    def test_server_assigned_properties(self, field):
        with pytest.raises(InvalidEmailCreateError, match="assigned by the server") as excinfo:
            validate_email_create({**VALID_CREATE, field: "x"})
        assert field in excinfo.value.properties

    def test_the_headers_list_points_at_the_alternative(self):
        with pytest.raises(InvalidEmailCreateError, match=r"header:\{name\}"):
            validate_email_create({**VALID_CREATE, "headers": [{"name": "X", "value": "1"}]})

    def test_headers_on_a_body_part(self):
        with pytest.raises(InvalidEmailCreateError, match="body part"):
            validate_email_create(
                {
                    "mailboxIds": {"mb1": True},
                    "textBody": [{"partId": "t", "headers": []}],
                    "bodyValues": {"t": {"value": "hi"}},
                }
            )

    def test_both_body_forms_at_once(self):
        with pytest.raises(InvalidEmailCreateError, match="not both"):
            validate_email_create({**VALID_CREATE, "bodyStructure": {"partId": "t"}})

    def test_missing_mailbox_ids(self):
        create = {key: value for key, value in VALID_CREATE.items() if key != "mailboxIds"}
        with pytest.raises(InvalidEmailCreateError, match="at least one mailbox"):
            validate_email_create(create)

    @pytest.mark.parametrize("value", [{}, [], "mb1", None])
    def test_malformed_mailbox_ids(self, value):
        with pytest.raises(InvalidEmailCreateError, match="mailboxIds"):
            validate_email_create({**VALID_CREATE, "mailboxIds": value})

    def test_mailbox_id_value_must_be_true(self):
        with pytest.raises(InvalidEmailCreateError, match="must be true"):
            validate_email_create({**VALID_CREATE, "mailboxIds": {"mb1": False}})

    def test_a_malformed_keyword(self):
        # Raised as this module's error, not the patch module's, so one `except`
        # covers validating a creation object.
        with pytest.raises(InvalidEmailCreateError, match="keyword"):
            validate_email_create({**VALID_CREATE, "keywords": {"has space": True}})

    def test_keywords_must_be_a_map(self):
        with pytest.raises(InvalidEmailCreateError, match="map of keyword"):
            validate_email_create({**VALID_CREATE, "keywords": ["$draft"]})

    def test_keyword_value_must_be_true(self):
        with pytest.raises(InvalidEmailCreateError, match="must be true"):
            validate_email_create({**VALID_CREATE, "keywords": {"$draft": False}})

    def test_a_part_with_both_sources(self):
        with pytest.raises(InvalidEmailCreateError, match="not both"):
            validate_email_create({**VALID_CREATE, "textBody": [{"partId": "t", "blobId": "b1"}]})

    def test_a_part_with_neither_source(self):
        with pytest.raises(InvalidEmailCreateError, match="needs either"):
            validate_email_create({**VALID_CREATE, "textBody": [{"type": "text/plain"}]})

    def test_a_part_id_with_no_body_value(self):
        with pytest.raises(InvalidEmailCreateError, match="no entry in `bodyValues`"):
            validate_email_create({**VALID_CREATE, "bodyValues": {}})

    def test_an_unreferenced_body_value(self):
        with pytest.raises(InvalidEmailCreateError, match="not referenced"):
            validate_email_create(
                {**VALID_CREATE, "bodyValues": {"t": {"value": "hi"}, "x": {"value": "?"}}}
            )

    def test_body_values_must_be_a_map(self):
        with pytest.raises(InvalidEmailCreateError, match="map of partId"):
            validate_email_create({**VALID_CREATE, "bodyValues": ["hi"]})

    def test_nested_part_errors_name_their_path(self):
        with pytest.raises(InvalidEmailCreateError) as excinfo:
            validate_email_create(
                {
                    "mailboxIds": {"mb1": True},
                    "bodyStructure": {
                        "type": "multipart/mixed",
                        "subParts": [{"type": "text/plain"}],
                    },
                }
            )
        assert "bodyStructure/subParts/0" in excinfo.value.properties


class TestBatchValidation:
    def test_a_clean_batch_passes(self):
        validate_email_creates({"d1": VALID_CREATE, "d2": VALID_CREATE})

    def test_the_failing_creation_id_is_named(self):
        # A failure in a batch of twenty has to say which one.
        with pytest.raises(InvalidEmailCreateError, match="d2:") as excinfo:
            validate_email_creates({"d1": VALID_CREATE, "d2": {**VALID_CREATE, "id": "m1"}})
        assert excinfo.value.properties == ("d2/id",)

    def test_an_empty_batch(self):
        validate_email_creates({})

    @pytest.mark.parametrize("body", [["not an object"], [{"partId": "t"}, 42], [None]])
    def test_a_non_object_body_part_is_rejected_not_skipped(self, body):
        # Skipping it would let the object sail past validation and be refused
        # by the server instead.
        with pytest.raises(InvalidEmailCreateError, match="must be an object"):
            validate_email_create({**VALID_CREATE, "textBody": body})

    def test_a_non_object_sub_part_is_rejected(self):
        with pytest.raises(InvalidEmailCreateError, match="must be an object"):
            validate_email_create(
                {
                    "mailboxIds": {"mb1": True},
                    "bodyStructure": {"type": "multipart/mixed", "subParts": ["oops"]},
                }
            )

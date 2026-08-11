"""RFC 8621 models and the three mail capabilities."""

from __future__ import annotations

import pytest

from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import (
    EMAIL_DEFAULT_PROPERTIES,
    MAIL,
    MAIL_URN,
    SMIME_URN,
    SMIME_VERIFY,
    SUBMISSION,
    SUBMISSION_URN,
    VACATION,
    VACATION_URN,
)
from jmap.capabilities.registry import Registry
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.mail.objects import (
    Address,
    Email,
    EmailAddress,
    EmailAddressGroup,
    EmailBodyPart,
    EmailBodyValue,
    EmailHeader,
    EmailSubmission,
    Envelope,
    Identity,
    Mailbox,
    MailboxRights,
    SearchSnippet,
    Thread,
    VacationResponse,
)


class TestEmail:
    def test_from_is_aliased_because_it_is_a_keyword(self):
        # Forget this and every message loses its sender, silently.
        email = Email.from_wire({"from": [{"name": "Alice", "email": "a@example.com"}]})
        assert email.from_ is not None
        assert email.from_[0].email == "a@example.com"
        assert email.to_wire()["from"][0]["name"] == "Alice"

    def test_camel_case_round_trip(self):
        email = Email.from_wire(
            {
                "id": "m1",
                "threadId": "t1",
                "hasAttachment": True,
                "receivedAt": "2026-01-01T00:00:00Z",
            }
        )
        assert email.thread_id == "t1"
        assert email.has_attachment is True
        assert email.to_wire() == {
            "id": "m1",
            "threadId": "t1",
            "hasAttachment": True,
            "receivedAt": "2026-01-01T00:00:00Z",
        }

    def test_unfetched_properties_are_absent_not_null(self):
        # Foo/get returns only what was asked for, so absence has to mean
        # "not fetched" - which is what exclude_unset preserves.
        email = Email.from_wire({"id": "m1"})
        assert email.subject is None
        assert "subject" not in email.to_wire()

    def test_keywords_and_mailbox_ids_are_maps(self):
        # Maps, not lists, because that is what makes them patchable one entry at
        # a time: {"keywords/$seen": true} adds a flag without rewriting the set.
        email = Email.from_wire({"keywords": {"$seen": True}, "mailboxIds": {"mb1": True}})
        assert email.keywords == {"$seen": True}
        assert email.mailbox_ids == {"mb1": True}

    def test_parsed_headers_survive_as_extras(self):
        # The property name encodes the request, and RFC 8621 §4.1.2 requires the
        # response key to match the requested capitalisation.
        email = Email.from_wire({"id": "m1", "header:Subject:asText": "Hi"})
        assert email.header("header:Subject:asText") == "Hi"
        assert email.to_wire()["header:Subject:asText"] == "Hi"

    def test_unknown_header_key_reads_as_none(self):
        assert Email.from_wire({"id": "m1"}).header("header:Nope:asText") is None

    def test_unknown_server_properties_are_preserved(self):
        # A property from a spec revision we do not model yet must survive the
        # read-modify-write cycle rather than being dropped.
        email = Email.from_wire({"id": "m1", "vendorThing": 42})
        assert email.to_wire()["vendorThing"] == 42

    def test_body_structure_is_recursive(self):
        email = Email.from_wire(
            {
                "bodyStructure": {
                    "partId": "1",
                    "type": "multipart/mixed",
                    "subParts": [{"partId": "1.1", "type": "text/plain"}],
                }
            }
        )
        assert email.body_structure is not None
        assert email.body_structure.sub_parts is not None
        assert email.body_structure.sub_parts[0].type == "text/plain"

    def test_body_values_are_keyed_by_part_id(self):
        email = Email.from_wire({"bodyValues": {"1": {"value": "hello", "isTruncated": False}}})
        assert email.body_values is not None
        assert email.body_values["1"].value == "hello"
        assert email.body_values["1"].is_truncated is False


class TestOtherModels:
    def test_mailbox_rights(self):
        mailbox = Mailbox.from_wire(
            {"id": "mb1", "name": "Inbox", "role": "inbox", "myRights": {"mayAddItems": True}}
        )
        assert mailbox.my_rights is not None
        assert mailbox.my_rights.may_add_items is True

    def test_thread(self):
        thread = Thread.from_wire({"id": "t1", "emailIds": ["m1", "m2"]})
        assert thread.email_ids == ["m1", "m2"]

    def test_search_snippet_is_keyed_by_email(self):
        snippet = SearchSnippet.from_wire({"emailId": "m1", "preview": "...match..."})
        assert snippet.email_id == "m1"

    def test_identity(self):
        identity = Identity.from_wire(
            {"id": "i1", "email": "alice@example.com", "mayDelete": False}
        )
        assert identity.may_delete is False

    def test_envelope_is_not_the_message_headers(self):
        # rcptTo is who the mail actually goes to; To/Cc are display only, which
        # is exactly why Bcc works.
        envelope = Envelope.from_wire(
            {"mailFrom": {"email": "a@x"}, "rcptTo": [{"email": "b@y"}, {"email": "c@z"}]}
        )
        assert envelope.rcpt_to is not None
        assert [address.email for address in envelope.rcpt_to] == ["b@y", "c@z"]

    def test_envelope_address_parameters(self):
        address = Address.from_wire({"email": "a@x", "parameters": {"HOLDFOR": "300"}})
        assert address.parameters == {"HOLDFOR": "300"}

    def test_submission_delivery_status(self):
        submission = EmailSubmission.from_wire(
            {
                "id": "s1",
                "undoStatus": "final",
                "deliveryStatus": {"b@y": {"delivered": "yes", "smtpReply": "250 OK"}},
            }
        )
        assert submission.delivery_status is not None
        assert submission.delivery_status["b@y"].delivered == "yes"

    def test_vacation_response(self):
        vacation = VacationResponse.from_wire(
            {"id": "singleton", "isEnabled": True, "textBody": "away"}
        )
        assert vacation.is_enabled is True

    def test_address_group(self):
        group = EmailAddressGroup.from_wire({"name": "Team", "addresses": [{"email": "a@x"}]})
        assert group.addresses is not None
        assert group.addresses[0].email == "a@x"

    def test_simple_value_objects(self):
        assert EmailAddress.from_wire({"email": "a@x"}).email == "a@x"
        assert EmailHeader.from_wire({"name": "Subject", "value": "Hi"}).value == "Hi"
        assert EmailBodyValue.from_wire({"value": "v"}).value == "v"
        assert EmailBodyPart.from_wire({"partId": "1"}).part_id == "1"
        assert MailboxRights.from_wire({"mayRename": True}).may_rename is True


class TestCapabilitySplit:
    """Three URNs, because servers advertise them independently."""

    def test_each_capability_owns_its_own_urn(self):
        assert MAIL.urn == MAIL_URN
        assert SUBMISSION.urn == SUBMISSION_URN
        assert VACATION.urn == VACATION_URN

    def test_identity_belongs_to_submission_not_mail(self):
        # It exists to name what you may send from, so a server without
        # submission has no use for it.
        assert MAIL.data_type("Identity") is None
        assert SUBMISSION.data_type("Identity") is not None

    def test_submission_requires_mail(self):
        assert MAIL_URN in SUBMISSION.requires

    def test_a_plain_email_get_does_not_drag_in_submission(self):
        # The whole reason for splitting: on a server without :submission this
        # would fail the entire request.
        registry = Registry()
        for spec in (CORE, MAIL, SUBMISSION):
            registry.register(spec)
        session = Session.from_wire(
            {"capabilities": {CORE_URN: {}, MAIL_URN: {}, SUBMISSION_URN: {}}}
        )
        using = registry.resolve(session).using_for(["Email/get"])
        assert using == {CORE_URN, MAIL_URN}

    def test_sending_pulls_mail_in_alongside_submission(self):
        registry = Registry()
        for spec in (CORE, MAIL, SUBMISSION):
            registry.register(spec)
        session = Session.from_wire(
            {"capabilities": {CORE_URN: {}, MAIL_URN: {}, SUBMISSION_URN: {}}}
        )
        using = registry.resolve(session).using_for(["EmailSubmission/set"])
        assert using == {CORE_URN, MAIL_URN, SUBMISSION_URN}


class TestMethodInventory:
    @pytest.mark.parametrize(
        "method",
        [
            "Mailbox/get",
            "Mailbox/changes",
            "Mailbox/query",
            "Mailbox/queryChanges",
            "Mailbox/set",
            "Thread/get",
            "Thread/changes",
            "Email/get",
            "Email/changes",
            "Email/query",
            "Email/queryChanges",
            "Email/set",
            "Email/copy",
            "Email/import",
            "Email/parse",
            "SearchSnippet/get",
        ],
    )
    def test_mail_methods(self, method):
        assert MAIL.method(method) is not None

    @pytest.mark.parametrize(
        "method",
        [
            "Identity/get",
            "Identity/changes",
            "Identity/set",
            "EmailSubmission/get",
            "EmailSubmission/changes",
            "EmailSubmission/query",
            "EmailSubmission/queryChanges",
            "EmailSubmission/set",
        ],
    )
    def test_submission_methods(self, method):
        assert SUBMISSION.method(method) is not None

    def test_vacation_methods(self):
        assert VACATION.method("VacationResponse/get") is not None
        assert VACATION.method("VacationResponse/set") is not None

    def test_thread_has_no_query_or_set(self):
        # Threads are derived from emails, not stored, so they cannot be created
        # or searched directly.
        assert MAIL.method("Thread/query") is None
        assert MAIL.method("Thread/set") is None

    def test_the_default_registry_covers_rfc_8621(self):
        registry = default_registry()
        for urn in (MAIL_URN, SUBMISSION_URN, VACATION_URN, SMIME_URN):
            assert urn in registry


class TestSpecDetails:
    def test_mutating_methods_are_marked(self):
        for name in ("Mailbox/set", "Email/set", "Email/copy", "Email/import"):
            spec = MAIL.method(name)
            assert spec is not None, name
            assert spec.mutating, name

    def test_read_methods_are_not_marked_mutating(self):
        for name in ("Email/get", "Email/query", "Thread/get"):
            spec = MAIL.method(name)
            assert spec is not None, name
            assert not spec.mutating, name

    def test_submission_set_expects_an_implicit_email_set(self):
        # RFC 8621 §7.5: onSuccessUpdateEmail makes the server answer with an
        # extra Email/set under the same call id.
        spec = SUBMISSION.method("EmailSubmission/set")
        assert spec is not None
        assert spec.implicit_responses == 1
        assert "onSuccessUpdateEmail" in spec.extra_args

    def test_email_copy_expects_an_implicit_response_too(self):
        spec = MAIL.method("Email/copy")
        assert spec is not None
        assert spec.implicit_responses == 1

    def test_collapse_threads_is_declared_on_query(self):
        spec = MAIL.method("Email/query")
        assert spec is not None
        assert "collapseThreads" in spec.extra_args

    def test_email_get_body_arguments(self):
        spec = MAIL.method("Email/get")
        assert spec is not None
        for argument in (
            "bodyProperties",
            "fetchTextBodyValues",
            "fetchHTMLBodyValues",
            "fetchAllBodyValues",
            "maxBodyValueBytes",
        ):
            assert argument in spec.extra_args

    def test_default_get_properties_exclude_the_body_structure(self):
        # "Fetch an email" without naming properties gets no bodyStructure, which
        # surprises people.
        assert "bodyStructure" not in EMAIL_DEFAULT_PROPERTIES
        assert "subject" in EMAIL_DEFAULT_PROPERTIES
        email_type = MAIL.data_type("Email")
        assert email_type is not None
        assert email_type.default_get_properties == EMAIL_DEFAULT_PROPERTIES

    def test_search_snippet_is_identityless(self):
        spec = MAIL.data_type("SearchSnippet")
        assert spec is not None
        assert spec.identityless

    def test_vacation_response_is_a_singleton(self):
        spec = VACATION.data_type("VacationResponse")
        assert spec is not None
        assert spec.singleton_id == "singleton"

    def test_email_delivery_is_push_only(self):
        spec = MAIL.data_type("EmailDelivery")
        assert spec is not None
        assert spec.push_only

    def test_mailbox_is_shareable(self):
        spec = MAIL.data_type("Mailbox")
        assert spec is not None
        assert spec.shareable


class TestSmimeVerify:
    """A capability with properties and no methods at all (RFC 9219)."""

    def test_it_registers_no_methods(self):
        assert SMIME_VERIFY.methods == ()

    def test_requesting_an_smime_property_pulls_the_urn_into_using(self):
        # Method names alone could never surface this, which is why `using`
        # derivation inspects requested properties too.
        registry = default_registry()
        session = Session.from_wire({"capabilities": {CORE_URN: {}, MAIL_URN: {}, SMIME_URN: {}}})
        active = registry.resolve(session)
        using = active.using_for(["Email/get"], properties=[("Email", "smimeStatus")])
        assert SMIME_URN in using

    def test_an_ordinary_property_does_not(self):
        registry = default_registry()
        session = Session.from_wire({"capabilities": {CORE_URN: {}, MAIL_URN: {}}})
        active = registry.resolve(session)
        assert active.using_for(["Email/get"], properties=[("Email", "subject")]) == {
            CORE_URN,
            MAIL_URN,
        }

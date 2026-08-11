"""RFC 9007 MDN models and the ``urn:ietf:params:jmap:mdn`` capability spec.

Two rules carry the whole file. The ``$mdnsent`` keyword is lowercase and
case-*sensitive* in JMAP (§1.2), so ``$MDNSent`` is a different keyword that no
server recognises and no local check matches; and ``MDN/send`` is the one method
in the library whose server is required to police the *client's* bookkeeping
(§2.1), which is why the ``onSuccessUpdateEmail`` patch and the mail URN are
declared rather than left to the caller.

The response models are the usual half-success shapes, with the twist that every
map is nullable rather than empty - so ``None`` and ``{}`` have to read alike.
"""

from __future__ import annotations

from jmap.capabilities.mail import MAIL_URN
from jmap.capabilities.mdn import MDN_CAPABILITY, MDN_URN
from jmap.capabilities.spec import MethodKind
from jmap.models.mdn import (
    ACTION_AUTOMATIC,
    ACTION_MANUAL,
    DISPOSITION_TYPES,
    MDN,
    MDN_ALREADY_SENT,
    MDN_SENT_KEYWORD,
    SENDING_AUTOMATIC,
    SENDING_MANUAL,
    TYPE_DELETED,
    TYPE_DISPATCHED,
    TYPE_DISPLAYED,
    TYPE_PROCESSED,
    Disposition,
    MDNParseResponse,
    MDNSendResponse,
    already_sent,
    mdn_sent_patch,
    parse_report,
)


class TestSentKeyword:
    def test_the_keyword_is_lowercase(self):
        # §1.2 fixes the spelling because keywords are case-insensitive in IMAP
        # and case-sensitive in JMAP. `$MDNSent` is a different keyword.
        assert MDN_SENT_KEYWORD == "$mdnsent"

    def test_the_patch_sets_the_keyword_on_the_acknowledged_message(self):
        # Spelled out literally rather than interpolated from the constant: this
        # is the string that goes on the wire, and §2.1 makes the server reject
        # an MDN/send whose onSuccessUpdateEmail does not carry exactly it.
        assert mdn_sent_patch() == {"keywords/$mdnsent": True}

    def test_each_call_returns_a_patch_of_its_own(self):
        # Callers merge extra properties into it before sending; a shared dict
        # would leak one send's additions into the next.
        first = mdn_sent_patch()
        first["keywords/$seen"] = True
        assert mdn_sent_patch() == {"keywords/$mdnsent": True}

    def test_the_already_sent_error_type_is_named(self):
        assert MDN_ALREADY_SENT == "mdnAlreadySent"


class TestAlreadySent:
    def test_the_keyword_marks_a_message_as_acknowledged(self):
        assert already_sent({"$mdnsent": True}) is True

    def test_a_message_with_no_keywords_has_not_been_acknowledged(self):
        # None is what an Email fetched without `keywords` looks like, and {} is
        # a message with none set; neither is an acknowledgement.
        assert already_sent(None) is False
        assert already_sent({}) is False

    def test_an_explicit_false_is_not_an_acknowledgement(self):
        assert already_sent({"$mdnsent": False}) is False

    def test_other_keywords_say_nothing(self):
        assert already_sent({"$seen": True, "$flagged": True}) is False

    def test_the_lookup_is_case_sensitive(self):
        # The trap §1.2 exists for: a client that stored the IMAP-style spelling
        # sends a second receipt for every message it already acknowledged.
        assert already_sent({"$MDNSent": True}) is False


class TestDispositionVocabulary:
    def test_the_action_and_sending_modes_are_lowercase(self):
        assert (ACTION_MANUAL, ACTION_AUTOMATIC) == ("manual-action", "automatic-action")
        assert (SENDING_MANUAL, SENDING_AUTOMATIC) == (
            "mdn-sent-manually",
            "mdn-sent-automatically",
        )

    def test_the_four_disposition_types_are_the_registered_set(self):
        assert set(DISPOSITION_TYPES) == {
            TYPE_DELETED,
            TYPE_DISPATCHED,
            TYPE_DISPLAYED,
            TYPE_PROCESSED,
        }
        assert TYPE_DISPLAYED == "displayed"

    def test_the_two_modes_are_independent(self):
        # A message can be displayed automatically and acknowledged deliberately,
        # or the reverse, so the fields are not two spellings of one fact.
        disposition = Disposition(
            action_mode=ACTION_AUTOMATIC, sending_mode=SENDING_MANUAL, type=TYPE_DISPLAYED
        )
        assert disposition.action_mode == "automatic-action"
        assert disposition.sending_mode == "mdn-sent-manually"


class TestMDNModel:
    def test_a_wire_object_parses_into_a_typed_disposition(self):
        mdn = MDN.from_wire(
            {
                "forEmailId": "Md45b47b4877521",
                "subject": "Read receipt for: World domination",
                "textBody": "This receipt shows that the email has been displayed.",
                "includeOriginalMessage": True,
                "disposition": {
                    "actionMode": "manual-action",
                    "sendingMode": "mdn-sent-manually",
                    "type": "displayed",
                },
                "finalRecipient": "rfc822; jdoe@machine.example",
            }
        )
        assert mdn.for_email_id == "Md45b47b4877521"
        assert mdn.include_original_message is True
        assert mdn.disposition == Disposition(
            action_mode="manual-action", sending_mode="mdn-sent-manually", type="displayed"
        )

    def test_the_server_set_fields_are_carried_through(self):
        mdn = MDN.from_wire(
            {
                "mdnGateway": "gateway.example.com",
                "originalRecipient": "rfc822; jdoe@machine.example",
                "originalMessageId": "<199509192301.23456@example.org>",
                "error": ["The recipient's mailbox is full"],
            }
        )
        # The RFC 5322 Message-ID, not the JMAP id - the two are not
        # interchangeable and only one of them identifies an Email.
        assert mdn.original_message_id == "<199509192301.23456@example.org>"
        assert mdn.mdn_gateway == "gateway.example.com"
        assert mdn.error == ["The recipient's mailbox is full"]

    def test_an_untouched_field_stays_off_the_wire(self):
        assert MDN(for_email_id="M1").to_wire() == {"forEmailId": "M1"}

    def test_a_null_for_email_id_is_what_parse_may_return(self):
        # MDN/parse cannot always work out which message a received receipt
        # refers to, so null is a legal answer there and a malformed send.
        assert MDN.from_wire({"forEmailId": None}).for_email_id is None

    def test_extension_fields_survive_the_round_trip(self):
        mdn = MDN(extension_fields={"X-Extension": "value"})
        assert mdn.to_wire() == {"extensionFields": {"X-Extension": "value"}}


class TestSendResponse:
    def test_it_half_succeeds_like_a_set(self):
        response = MDNSendResponse.from_wire(
            {
                "accountId": "u1",
                "sent": {"k1546": {"forEmailId": "M1"}},
                "notSent": {"k1547": {"type": MDN_ALREADY_SENT}},
            }
        )
        assert response.account_id == "u1"
        assert response.sent_ids() == ["k1546"]
        assert response.has_errors is True
        assert response.not_sent is not None
        assert response.not_sent["k1547"]["type"] == "mdnAlreadySent"

    def test_a_clean_send_reports_no_errors(self):
        response = MDNSendResponse.from_wire({"sent": {"k1": {}}, "notSent": {}})
        assert response.has_errors is False
        assert response.sent_ids() == ["k1"]

    def test_the_maps_are_nullable_rather_than_empty(self):
        # Both halves may be absent entirely, so a caller that indexes into them
        # or measures their length has to survive None as well as {}.
        response = MDNSendResponse.from_wire({"accountId": "u1"})
        assert response.has_errors is False
        assert response.sent_ids() == []

    def test_an_explicit_null_reads_the_same_as_an_absent_map(self):
        response = MDNSendResponse.from_wire({"sent": None, "notSent": None})
        assert response.has_errors is False
        assert response.sent_ids() == []


class TestParseResponse:
    def test_a_parsed_blob_yields_its_mdn(self):
        response = MDNParseResponse.from_wire(
            {
                "accountId": "u1",
                "parsed": {"G1": {"forEmailId": "M1", "disposition": {"type": "displayed"}}},
            }
        )
        parsed = response.parsed_for("G1")
        assert parsed is not None
        assert parsed.for_email_id == "M1"
        assert response.unusable == []

    def test_a_blob_that_did_not_parse_yields_nothing(self):
        response = MDNParseResponse.from_wire({"parsed": {"G1": {}}, "notParsable": ["G2"]})
        assert response.parsed_for("G2") is None

    def test_parsed_for_survives_a_null_map(self):
        assert MDNParseResponse.from_wire({}).parsed_for("G1") is None
        assert MDNParseResponse.from_wire({"parsed": None}).parsed_for("G1") is None

    def test_unusable_concatenates_both_failure_lists(self):
        # Missing and unparseable mean different things to a server and the same
        # thing to a caller deciding what to display; not_found comes first.
        response = MDNParseResponse.from_wire(
            {"notFound": ["G3"], "notParsable": ["G2"], "parsed": {"G1": {}}}
        )
        assert response.unusable == ["G3", "G2"]

    def test_unusable_tolerates_either_list_being_null(self):
        assert MDNParseResponse.from_wire({"notFound": ["G3"]}).unusable == ["G3"]
        assert MDNParseResponse.from_wire({"notParsable": ["G2"]}).unusable == ["G2"]
        assert MDNParseResponse.from_wire({}).unusable == []


class TestParseReport:
    def test_it_lowercases_the_disposition_values(self):
        # RFC 8098 defines the fields case-insensitively and RFC 9007 §2 makes
        # them case-sensitive, so "Displayed" off a hand-parsed report matches
        # none of the constants until it goes through here.
        assert parse_report(
            {
                "action-mode": "Manual-Action",
                "sending-mode": "MDN-Sent-Manually",
                "type": "Displayed",
            }
        ) == {
            "action-mode": ACTION_MANUAL,
            "sending-mode": SENDING_MANUAL,
            "type": TYPE_DISPLAYED,
        }

    def test_the_keys_are_left_alone(self):
        assert parse_report({"Disposition-Type": "DISPLAYED"}) == {"Disposition-Type": "displayed"}

    def test_an_empty_report_stays_empty(self):
        assert parse_report({}) == {}


class TestSpec:
    def test_the_urn_is_registered_under_its_attribute(self):
        assert MDN_CAPABILITY.urn == MDN_URN == "urn:ietf:params:jmap:mdn"
        assert MDN_CAPABILITY.attr == "mdn"
        assert MDN_CAPABILITY.reference == "RFC 9007"

    def test_the_capability_requires_the_mail_urn(self):
        # §2.1: MDN/send implicitly performs an Email/set and reads an Identity,
        # so `using` needs mail even for a batch that calls nothing else.
        assert MAIL_URN in MDN_CAPABILITY.requires

    def test_send_declares_the_same_requirement_on_itself(self):
        send = MDN_CAPABILITY.method("MDN/send")
        assert send is not None
        assert send.also_requires == frozenset({MAIL_URN})

    def test_send_expects_one_implicit_response(self):
        # The server emits an extra Email/set under this call's id for the
        # $mdnsent update; merging it into the result would corrupt the send.
        send = MDN_CAPABILITY.method("MDN/send")
        assert send is not None
        assert send.implicit_responses == 1
        assert send.mutating is True

    def test_both_methods_are_custom_with_their_own_response_models(self):
        # Neither follows a standard shape, so the generic builder must not offer
        # /get or /set arguments for them.
        send = MDN_CAPABILITY.method("MDN/send")
        parse = MDN_CAPABILITY.method("MDN/parse")
        assert send is not None
        assert parse is not None
        assert send.kind is MethodKind.CUSTOM
        assert parse.kind is MethodKind.CUSTOM
        assert send.response_model is MDNSendResponse
        assert parse.response_model is MDNParseResponse

    def test_parse_neither_mutates_nor_needs_a_second_capability(self):
        parse = MDN_CAPABILITY.method("MDN/parse")
        assert parse is not None
        assert parse.mutating is False
        assert parse.implicit_responses == 0
        assert parse.also_requires == frozenset()

    def test_the_methods_declare_their_arguments(self):
        send = MDN_CAPABILITY.method("MDN/send")
        parse = MDN_CAPABILITY.method("MDN/parse")
        assert send is not None
        assert parse is not None
        assert set(send.extra_args) == {"identityId", "send", "onSuccessUpdateEmail"}
        assert set(parse.extra_args) == {"blobIds"}

    def test_the_mdn_data_type_is_declared(self):
        data_type = MDN_CAPABILITY.data_type("MDN")
        assert data_type is not None
        assert data_type.model is MDN

    def test_there_are_no_standard_methods(self):
        # RFC 9007 defines exactly two: an MDN is not a stored object, so there is
        # nothing to /get, /query or /changes.
        assert {method.name for method in MDN_CAPABILITY.methods} == {"MDN/send", "MDN/parse"}
        assert MDN_CAPABILITY.method("MDN/get") is None

"""Sending and parsing read receipts, end to end (RFC 9007).

The interesting assertion is about what goes *on the wire*: RFC 9007 §2.1 makes
the server check that an ``MDN/send`` also sets ``$mdnsent``, so a request without
that patch is malformed rather than merely incomplete - and the builder is what
guarantees it is there.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jmap.auth import BasicAuth
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN, SUBMISSION, SUBMISSION_URN
from jmap.capabilities.mdn import MDN_CAPABILITY, MDN_URN
from jmap.capabilities.registry import Registry
from jmap.client import JMAPClient
from jmap.models.mdn import (
    ACTION_MANUAL,
    MDN_SENT_KEYWORD,
    SENDING_MANUAL,
    TYPE_DISPLAYED,
    Disposition,
    already_sent,
)
from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, MAIL, SUBMISSION, MDN_CAPABILITY):
        reg.register(spec)
    return reg


def server() -> FakeJMAPServer:
    urns = (CORE_URN, MAIL_URN, SUBMISSION_URN, MDN_URN)
    return FakeJMAPServer(
        capabilities={urn: {} for urn in urns},
        primary_accounts=dict.fromkeys(urns, "a"),
    )


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
        registry=registry(),
    )


def receipt(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "forEmailId": "m1",
        "subject": "Read receipt",
        "disposition": Disposition(
            actionMode=ACTION_MANUAL, sendingMode=SENDING_MANUAL, type=TYPE_DISPLAYED
        ).to_wire(),
    }
    body.update(overrides)
    return body


class TestSending:
    def test_the_mdnsent_patch_is_sent_without_being_asked_for(self):
        # §2.1: the server MUST reject a send that does not set $mdnsent, and MUST
        # check onSuccessUpdateEmail to confirm it. Omitting it is not "send
        # without bookkeeping", it is "send nothing".
        fake = server()
        fake.respond("MDN/send", {"accountId": "a", "sent": {"k1": {"forEmailId": "m1"}}})
        with connect(fake) as client, client.batch() as batch:
            batch.mdn.mdn.send(identity_id="i1", send={"k1": receipt()})
        arguments = fake.requests[0]["methodCalls"][0][1]
        assert arguments["onSuccessUpdateEmail"] == {"#k1": {f"keywords/{MDN_SENT_KEYWORD}": True}}

    def test_one_patch_per_receipt(self):
        fake = server()
        fake.respond("MDN/send", {"accountId": "a", "sent": {}})
        with connect(fake) as client, client.batch() as batch:
            batch.mdn.mdn.send(identity_id="i1", send={"k1": receipt(), "k2": receipt()})
        patches = fake.requests[0]["methodCalls"][0][1]["onSuccessUpdateEmail"]
        assert set(patches) == {"#k1", "#k2"}

    def test_an_explicit_patch_wins(self):
        # A caller may need to set other keywords in the same update. The server
        # still checks $mdnsent is among them, which is its job rather than ours.
        fake = server()
        fake.respond("MDN/send", {"accountId": "a", "sent": {}})
        with connect(fake) as client, client.batch() as batch:
            batch.mdn.mdn.send(
                identity_id="i1",
                send={"k1": receipt()},
                on_success_update_email={
                    "#k1": {f"keywords/{MDN_SENT_KEYWORD}": True, "keywords/$seen": True}
                },
            )
        patch = fake.requests[0]["methodCalls"][0][1]["onSuccessUpdateEmail"]["#k1"]
        assert patch["keywords/$seen"] is True

    def test_the_response_is_typed(self):
        fake = server()
        fake.respond(
            "MDN/send",
            {
                "accountId": "a",
                "sent": {"k1": {"forEmailId": "m1", "finalRecipient": "rfc822; alice@x"}},
                "notSent": None,
            },
        )
        with connect(fake) as client, client.batch() as batch:
            sent = batch.mdn.mdn.send(identity_id="i1", send={"k1": receipt()})
        assert sent.result.sent_ids() == ["k1"]
        assert sent.result.has_errors is False
        assert sent.result.sent is not None
        assert sent.result.sent["k1"].final_recipient == "rfc822; alice@x"

    def test_a_failure_is_a_value_not_an_exception(self):
        # Like a /set, this half-succeeds by design.
        fake = server()
        fake.respond(
            "MDN/send",
            {"accountId": "a", "sent": None, "notSent": {"k1": {"type": "mdnAlreadySent"}}},
        )
        with connect(fake) as client, client.batch() as batch:
            sent = batch.mdn.mdn.send(identity_id="i1", send={"k1": receipt()})
        assert sent.result.has_errors
        assert sent.result.sent_ids() == []
        assert sent.result.not_sent is not None
        assert sent.result.not_sent["k1"]["type"] == "mdnAlreadySent"

    def test_a_model_or_a_raw_dict_both_work(self):
        fake = server()
        fake.respond("MDN/send", {"accountId": "a", "sent": {}})
        with connect(fake) as client, client.batch() as batch:
            batch.mdn.mdn.send(identity_id="i1", send={"k1": {"forEmailId": "m1"}})
        assert fake.requests[0]["methodCalls"][0][1]["send"]["k1"]["forEmailId"] == "m1"


class TestUsingDerivation:
    def test_the_mail_capability_comes_along(self):
        # §2.1 requires both URNs, because MDN/send implies an Email/set and reads
        # an Identity. A caller should not have to know that.
        fake = server()
        fake.respond("MDN/send", {"accountId": "a", "sent": {}})
        with connect(fake) as client, client.batch() as batch:
            batch.mdn.mdn.send(identity_id="i1", send={"k1": receipt()})
        using = fake.requests[0]["using"]
        assert MDN_URN in using
        assert MAIL_URN in using


class TestParsing:
    def test_a_blob_becomes_an_mdn(self):
        fake = server()
        fake.respond(
            "MDN/parse",
            {
                "accountId": "a",
                "parsed": {
                    "B1": {
                        "forEmailId": "m1",
                        "originalMessageId": "<x@example.com>",
                        "disposition": {
                            "actionMode": "manual-action",
                            "sendingMode": "mdn-sent-manually",
                            "type": "displayed",
                        },
                    }
                },
            },
        )
        with connect(fake) as client, client.batch() as batch:
            parsed = batch.mdn.mdn.parse(blob_ids=["B1"])
        mdn = parsed.result.parsed_for("B1")
        assert mdn is not None
        assert mdn.disposition is not None
        assert mdn.disposition.type == TYPE_DISPLAYED
        # Not the JMAP id - the RFC 5322 header.
        assert mdn.original_message_id == "<x@example.com>"

    def test_a_null_for_email_id_is_legal_from_parse(self):
        # §2.2: the server cannot always work out which message a received MDN
        # refers to, and says so with null rather than by failing.
        fake = server()
        fake.respond("MDN/parse", {"accountId": "a", "parsed": {"B1": {"forEmailId": None}}})
        with connect(fake) as client, client.batch() as batch:
            parsed = batch.mdn.mdn.parse(blob_ids=["B1"])
        mdn = parsed.result.parsed_for("B1")
        assert mdn is not None
        assert mdn.for_email_id is None

    def test_the_two_failure_lists_read_as_one(self):
        fake = server()
        fake.respond(
            "MDN/parse",
            {"accountId": "a", "parsed": None, "notFound": ["B9"], "notParsable": ["B8"]},
        )
        with connect(fake) as client, client.batch() as batch:
            parsed = batch.mdn.mdn.parse(blob_ids=["B8", "B9"])
        assert parsed.result.parsed_for("B8") is None
        assert set(parsed.result.unusable) == {"B8", "B9"}


class TestTheKeyword:
    def test_an_acknowledged_message_is_recognised(self):
        # §2.1: the client MUST NOT send an MDN for one of these.
        assert already_sent({MDN_SENT_KEYWORD: True})

    def test_the_spelling_is_lowercase(self):
        # Keywords are case-insensitive in IMAP and case-sensitive in JMAP; §1.2
        # resolves that by fixing the spelling. $MDNSent is a different keyword.
        assert already_sent({"$MDNSent": True}) is False
        assert MDN_SENT_KEYWORD == "$mdnsent"

    def test_an_unacknowledged_message_is_not(self):
        assert already_sent({"$seen": True}) is False
        assert already_sent(None) is False


class TestSurface:
    def test_a_server_without_mdn_has_no_namespace(self):
        fake = FakeJMAPServer(
            capabilities={CORE_URN: {}, MAIL_URN: {}},
            primary_accounts={CORE_URN: "a", MAIL_URN: "a"},
        )
        missing = pytest.raises(AttributeError, match="mdn")
        with connect(fake) as client, client.batch() as batch, missing:
            _ = batch.mdn

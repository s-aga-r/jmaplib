"""The mail and submission limits an account advertises (RFC 8621 §1.3).

Checked where a builder can see them: an email's mailboxes on ``Email/set`` and
``Email/import``, and a held submission on ``EmailSubmission/set``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jmap.api.namespace import Namespaces
from jmap.batch import Batch
from jmap.capabilities.mail import MAIL_URN, SUBMISSION_URN
from jmap.core.errors import CapabilityFieldError
from jmap.core.ids import Id
from jmap.core.limits import URN_CORE
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.mail.irregular import EmailImport
from jmap.models.mail.objects import Email


def namespaces(mail: dict[str, Any] | None = None, submission: dict[str, Any] | None = None) -> Any:
    account = {
        MAIL_URN: {"maxMailboxesPerEmail": 2} if mail is None else mail,
        SUBMISSION_URN: submission or {},
    }
    session = Session.from_wire(
        {
            "capabilities": {URN_CORE: {}, MAIL_URN: {}, SUBMISSION_URN: {}},
            "accounts": {"a": {"name": "alice", "accountCapabilities": account}},
            "primaryAccounts": dict.fromkeys(account, "a"),
        }
    )
    capabilities = default_registry().resolve(session, Id("a"))
    return Namespaces(Batch(capabilities), capabilities)


THREE = {"m1": True, "m2": True, "m3": True}


class TestMailboxesPerEmail:
    def test_an_email_within_the_limit_goes_out(self):
        handle = namespaces().mail.email.set(
            create={"e": {"mailboxIds": {"m1": True, "m2": True, "m3": False}}}, destroy=["x"]
        )
        assert handle.call.to_wire_arguments()["destroy"] == ["x"]

    @pytest.mark.parametrize(
        "change",
        [
            {"create": {"e": {"mailboxIds": THREE}}},
            {"create": {"e": Email(mailbox_ids=THREE)}},
            {"update": {"e1": {"mailboxIds": THREE}}},
        ],
    )
    def test_one_in_too_many_mailboxes_is_refused(self, change):
        with pytest.raises(CapabilityFieldError, match="maxMailboxesPerEmail") as excinfo:
            namespaces().mail.email.set(**change)
        assert (excinfo.value.advertised, excinfo.value.requested) == (2, 3)

    def test_a_patch_adding_one_mailbox_is_the_servers_to_judge(self):
        # Adding m3 to an email already in m1 and m2 is too many, but only the
        # server knows where the email already is.
        assert namespaces().mail.email.set(update={"e1": {"mailboxIds/m3": True}})

    def test_a_server_with_no_limit_refuses_nothing(self):
        batch = namespaces(mail={"maxMailboxesPerEmail": None})
        assert batch.mail.email.set(create={"e": {"mailboxIds": THREE}})

    def test_an_import_is_held_to_the_same_limit(self):
        with pytest.raises(CapabilityFieldError, match="maxMailboxesPerEmail"):
            namespaces().mail.email.import_(
                emails={"i": EmailImport(blob_id="B1", mailbox_ids=THREE)}
            )

    def test_a_back_reference_is_the_servers_to_judge(self):
        batch = namespaces()
        source = batch.mail.email.get(ids=["e1"])
        assert batch.mail.email.set(create=source.ref("/list/0"))


def held(**parameters: Any) -> dict[str, Any]:
    """A submission whose envelope asks for the given SMTP parameters."""
    return {
        "identityId": "I1",
        "emailId": "E1",
        "envelope": {
            "mailFrom": {"email": "alice@example.com", "parameters": parameters},
            "rcptTo": [{"email": "bob@example.com"}],
        },
    }


def in_days(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


WEEK = {"maxDelayedSend": 7 * 86400}


class TestDelayedSend:
    @pytest.mark.parametrize(
        "submission",
        [
            held(HOLDFOR="3600"),
            held(HOLDUNTIL=in_days(1)),
            # Already due: no hold at all.
            held(HOLDUNTIL="2020-01-01T00:00:00Z"),
            {"identityId": "I1", "emailId": "E1"},
            # Values it cannot read are the server's to judge.
            held(HOLDFOR="soon"),
            held(HOLDUNTIL="tomorrow"),
            held(HOLDUNTIL="2030-01-01T00:00:00"),
        ],
    )
    def test_a_hold_within_the_limit_goes_out(self, submission):
        batch = namespaces(submission=WEEK)
        assert batch.submission.email_submission.set(create={"s": submission})

    @pytest.mark.parametrize(
        "submission",
        [held(HOLDFOR=str(8 * 86400)), held(holdfor="700000"), held(HOLDUNTIL=in_days(10))],
    )
    def test_a_longer_hold_is_refused(self, submission):
        batch = namespaces(submission=WEEK)
        with pytest.raises(CapabilityFieldError, match="maxDelayedSend"):
            batch.submission.email_submission.set(create={"s": submission})

    def test_a_server_that_cannot_hold_refuses_any_hold(self):
        batch = namespaces(submission={"maxDelayedSend": 0})
        with pytest.raises(CapabilityFieldError, match="maxDelayedSend"):
            batch.submission.email_submission.set(create={"s": held(HOLDFOR="60")})

    def test_a_server_that_said_nothing_refuses_nothing(self):
        batch = namespaces(submission={})
        assert batch.submission.email_submission.set(create={"s": held(HOLDFOR="99999999")})

    def test_the_other_arguments_go_out_as_given(self):
        handle = namespaces(submission=WEEK).submission.email_submission.set(
            create={"s": held(HOLDFOR="60")},
            onSuccessDestroyEmail=["#s"],
        )
        assert handle.call.to_wire_arguments()["onSuccessDestroyEmail"] == ["#s"]

"""The mail and submission limits an account advertises (RFC 8621 §1.3).

Checked where a builder can see them: an email's mailboxes on ``Email/set`` and
``Email/import``.
"""

from __future__ import annotations

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

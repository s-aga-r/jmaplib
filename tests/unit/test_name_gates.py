"""``/set`` checks the names it is given before the call is queued.

Mailboxes (RFC 8621), Sieve scripts (RFC 9661) and file nodes (filenode draft)
each have a name the server must refuse for reasons the account advertises.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from jmap.api.namespace import Namespaces
from jmap.batch import Batch
from jmap.capabilities.files import FILENODE_URN
from jmap.capabilities.mail import MAIL_URN
from jmap.capabilities.sieve import SIEVE_URN
from jmap.core.errors import CapabilityFieldError
from jmap.core.ids import Id
from jmap.core.limits import URN_CORE
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.mail.objects import Mailbox


def namespaces(**mail: Any) -> Any:
    account = {
        MAIL_URN: {"maxSizeMailboxName": 10, **mail},
        SIEVE_URN: {"maxSizeScriptName": 10},
        FILENODE_URN: {
            "maxSizeFileNodeName": 20,
            "forbiddenNameChars": "/",
            "forbiddenNodeNames": [".."],
        },
    }
    session = Session.from_wire(
        {
            "capabilities": {URN_CORE: {}, **{urn: {} for urn in account}},
            "accounts": {"a": {"name": "alice", "accountCapabilities": account}},
            "primaryAccounts": dict.fromkeys(account, "a"),
        }
    )
    capabilities = default_registry().resolve(session, Id("a"), experimental=True)
    return Namespaces(Batch(capabilities), capabilities)


class TestMailboxNames:
    def test_names_that_fit_go_out_with_the_rest(self):
        handle = namespaces().mail.mailbox.set(
            create={"n": {"name": "Receipts", "parentId": "m1"}},
            update={"m2": {"name": "Old"}},
            destroy=["m3"],
            if_in_state="s1",
        )
        arguments = handle.call.to_wire_arguments()
        assert arguments["create"] == {"n": {"name": "Receipts", "parentId": "m1"}}
        assert arguments["destroy"] == ["m3"]
        assert arguments["ifInState"] == "s1"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"create": {"n": {"name": "Quittungen 2026"}}}, "maxSizeMailboxName"),
            # Ten characters, but twenty octets.
            ({"create": {"n": {"name": "éééééééééé"}}}, "maxSizeMailboxName"),
            ({"create": {"n": Mailbox(name="Quittungen 2026")}}, "maxSizeMailboxName"),
            ({"update": {"m1": {"name": ""}}}, "at least one character"),
        ],
    )
    def test_a_name_the_server_must_refuse_is_refused_here(self, change, message):
        with pytest.raises(CapabilityFieldError, match=message):
            namespaces().mail.mailbox.set(**change)

    def test_what_is_not_a_literal_name_is_the_servers_to_judge(self):
        batch = namespaces()
        source = batch.mail.mailbox.get(ids=["m1"])
        handle = batch.mail.mailbox.set(
            create=source.ref("/list/0"), update={"m1": {"name": None, "sortOrder": 3}}
        )
        assert "#create" in handle.call.to_wire_arguments()

    def test_the_standard_arguments_are_still_checked(self):
        with pytest.raises(ValidationError):
            namespaces().mail.mailbox.set(destroy="m3")


class TestTopLevelMailboxes:
    def test_one_is_refused_where_the_account_may_not_create_one(self):
        batch = namespaces(mayCreateTopLevelMailbox=False)
        with pytest.raises(CapabilityFieldError, match="mayCreateTopLevelMailbox"):
            batch.mail.mailbox.set(create={"n": {"name": "Top", "parentId": None}})
        with pytest.raises(CapabilityFieldError, match="mayCreateTopLevelMailbox"):
            batch.mail.mailbox.set(create={"n": Mailbox(name="Top")})

    def test_a_child_is_not_top_level(self):
        batch = namespaces(mayCreateTopLevelMailbox=False)
        assert batch.mail.mailbox.set(create={"n": {"name": "Child", "parentId": "#p"}})

    @pytest.mark.parametrize("allowed", [{"mayCreateTopLevelMailbox": True}, {}])
    def test_one_goes_out_where_it_is_allowed_or_nothing_was_said(self, allowed):
        assert namespaces(**allowed).mail.mailbox.set(create={"n": {"name": "Top"}})


class TestSieveScriptNames:
    def test_a_name_that_fits_goes_out(self):
        assert namespaces().sieve.sieve_script.set(create={"s": {"name": "vacation"}})

    @pytest.mark.parametrize(
        ("name", "message"), [("a\u2028b", "control characters"), ("x" * 11, "maxSizeScriptName")]
    )
    def test_a_name_the_server_must_refuse_is_refused_here(self, name, message):
        with pytest.raises(CapabilityFieldError, match=message):
            namespaces().sieve.sieve_script.set(update={"s1": {"name": name}})

    def test_a_script_with_no_name_is_the_servers_to_name(self):
        assert namespaces().sieve.sieve_script.set(create={"s": {"name": None, "blobId": "B"}})


class TestFileNodeNames:
    @pytest.mark.parametrize(
        ("name", "message"), [("a/b", "forbiddenNameChars"), ("..", "forbiddenNodeNames")]
    )
    def test_a_name_the_server_must_refuse_is_refused_here(self, name, message):
        with pytest.raises(CapabilityFieldError, match=message):
            namespaces().files.file_node.set(create={"f": {"name": name, "parentId": "d1"}})

    def test_a_name_that_fits_goes_out(self):
        assert namespaces().files.file_node.set(create={"f": {"name": "notes.txt"}})

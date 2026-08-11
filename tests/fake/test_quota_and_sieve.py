"""RFC 9425 Quota and RFC 9661 Sieve, end to end against the fake server."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jmap.auth import BasicAuth
from jmap.capabilities.blob import BLOB, BLOB_URN
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.quota import QUOTA, QUOTA_URN
from jmap.capabilities.registry import Registry
from jmap.capabilities.sieve import SIEVE, SIEVE_URN
from jmap.client import JMAPClient
from jmap.core.errors import CapabilityFieldError
from jmap.models.blob import BlobUpload, DataSource
from jmap.models.quota import ResourceType, Scope
from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"

SCRIPT = 'require ["fileinto"];\r\nfileinto "INBOX.target";\r\n'


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, BLOB, QUOTA, SIEVE):
        reg.register(spec)
    return reg


def server(**sieve_capability: Any) -> FakeJMAPServer:
    capability: dict[str, Any] = {
        "maxSizeScriptName": 512,
        "maxSizeScript": 65536,
        "maxNumberScripts": 5,
        "maxNumberRedirects": None,
        "sieveExtensions": ["fileinto", "imap4flags"],
        "notificationMethods": ["mailto"],
        "externalLists": None,
    }
    capability.update(sieve_capability)
    urns = (CORE_URN, BLOB_URN, QUOTA_URN, SIEVE_URN)
    return FakeJMAPServer(
        capabilities={
            CORE_URN: {},
            BLOB_URN: {},
            QUOTA_URN: {},
            # `implementation` is a *session*-level field; every limit is per
            # account. Reading a limit from here gets nothing, silently.
            SIEVE_URN: {"implementation": "ACME Email Filtering"},
        },
        accounts={
            "a": {
                "name": "alice",
                "isPersonal": True,
                "isReadOnly": False,
                "accountCapabilities": {
                    BLOB_URN: {"supportedTypeNames": ["SieveScript"]},
                    SIEVE_URN: capability,
                },
            }
        },
        primary_accounts=dict.fromkeys(urns, "a"),
    )


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
        registry=registry(),
    )


class TestQuota:
    def test_quotas_come_back_typed(self):
        fake = server()
        fake.respond(
            "Quota/get",
            {
                "accountId": "a",
                "state": "78540",
                "list": [
                    {
                        "id": "q1",
                        "resourceType": "count",
                        "used": 1056,
                        "warnLimit": 1600,
                        "softLimit": 1800,
                        "hardLimit": 2000,
                        "scope": "account",
                        "name": "bob@example.com",
                        "types": ["Mail", "Calendar"],
                    }
                ],
                "notFound": [],
            },
        )
        with connect(fake) as client, client.batch() as batch:
            quotas = batch.quota.quota.get(ids=None)
        quota = quotas.result.items[0]
        assert quota.resource_type == ResourceType.COUNT
        assert quota.scope == Scope.ACCOUNT
        assert quota.remaining == 944
        assert not quota.is_over_warn_limit

    def test_updated_properties_narrows_the_follow_up_get(self):
        # RFC 9425 §4.3's whole point: `used` moves constantly and everything else
        # rarely does, so the server can say "only this changed" and the client
        # feeds that straight into a /get by back-reference.
        fake = server()
        fake.respond(
            "Quota/changes",
            {
                "accountId": "a",
                "oldState": "78540",
                "newState": "78542",
                "hasMoreChanges": False,
                "updatedProperties": ["used"],
                "created": [],
                "updated": ["q1"],
                "destroyed": [],
            },
        )
        fake.respond(
            "Quota/get",
            {"accountId": "a", "state": "78542", "list": [{"id": "q1", "used": 1246}]},
        )
        with connect(fake) as client, client.batch() as batch:
            changed = batch.quota.quota.changes(since_state="78540")
            batch.quota.quota.get(
                ids=changed.ref_updated(), properties=changed.ref_updated_properties()
            )
        assert changed.result.updated_properties == ["used"]
        assert changed.result.fetch_all_properties is False

    def test_null_updated_properties_means_fetch_everything(self):
        # The inversion worth pinning: null is the *pessimistic* answer, not the
        # empty one. Reading it as "nothing changed" loses every other update.
        fake = server()
        fake.respond(
            "Quota/changes",
            {
                "accountId": "a",
                "oldState": "1",
                "newState": "2",
                "hasMoreChanges": False,
                "updatedProperties": None,
                "updated": ["q1"],
            },
        )
        with connect(fake) as client, client.batch() as batch:
            changed = batch.quota.quota.changes(since_state="1")
        assert changed.result.updated_properties is None
        assert changed.result.fetch_all_properties is True

    def test_a_quota_cannot_be_set(self):
        # Server-computed, so the façade genuinely has no .set rather than one
        # that earns unknownMethod.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            assert hasattr(batch.quota.quota, "get")
            assert hasattr(batch.quota.quota, "query")
            assert not hasattr(batch.quota.quota, "set")


class TestSieveScripts:
    def test_scripts_come_back_typed(self):
        fake = server()
        fake.respond(
            "SieveScript/get",
            {
                "accountId": "a",
                "state": "1634915373.240633104-120",
                "list": [{"id": "s1", "name": "test1", "isActive": True, "blobId": "S7"}],
                "notFound": [],
            },
        )
        with connect(fake) as client, client.batch() as batch:
            scripts = batch.sieve.sieve_script.get(ids=None)
        script = scripts.result.items[0]
        assert script.name == "test1"
        assert script.is_active is True
        assert script.blob_id == "S7"

    def test_upload_then_store_in_one_request(self):
        # RFC 9661 §2.2: content is a blob, so storing a script is two operations.
        # With RFC 9404 they fit in one round trip, which is the pairing that makes
        # the blob capability worth having.
        fake = server()
        fake.respond(
            "SieveScript/set",
            {"accountId": "a", "oldState": "1", "newState": "2", "created": {"A": {"id": "s1"}}},
        )
        with connect(fake) as client, client.batch() as batch:
            uploaded = batch.blob.blob.upload(
                create={"B": BlobUpload(data=[DataSource.text(SCRIPT)], type="application/sieve")}
            )
            # "#B", not a result reference: RFC 8620 §3.7 references are only
            # legal as top-level arguments, so inside a create object the
            # creation-id form is the only one that works. RFC 9661 §2.4.1 writes
            # it exactly this way.
            stored = batch.sieve.sieve_script.set(create={"A": {"name": "filters", "blobId": "#B"}})
        assert stored.result.created_id("A") == "s1"
        # The blob really was created, and the back-reference really resolved.
        blob_id = uploaded.result.created["B"].id
        assert blob_id is not None
        assert fake.blobs[blob_id][0].decode() == SCRIPT

    def test_validate_reports_a_bad_script_without_failing_the_call(self):
        # §2.6: invalid content is not a method error. The call succeeds and the
        # problem arrives as an `error` argument, so nothing raises.
        fake = server()
        fake.respond(
            "SieveScript/validate",
            {
                "accountId": "a",
                "error": {"type": "invalidSieve", "description": "line 2: unknown command"},
            },
        )
        with connect(fake) as client, client.batch() as batch:
            checked = batch.sieve.sieve_script.validate(blob_id="S7")
        assert checked.result.is_valid is False
        problem = checked.result.problem
        assert problem is not None
        assert problem.type == "invalidSieve"
        assert problem.description is not None
        assert "line 2" in problem.description

    def test_validate_reports_a_good_script(self):
        fake = server()
        fake.respond("SieveScript/validate", {"accountId": "a", "error": None})
        with connect(fake) as client, client.batch() as batch:
            checked = batch.sieve.sieve_script.validate(blob_id="S7")
        assert checked.result.is_valid is True
        assert checked.result.problem is None

    def test_there_is_no_changes_method(self):
        # RFC 9661 defines none, even though the type is registered as usable for
        # state change - so push can say "scripts changed" while offering no way
        # to ask what. Re-running /get is the answer.
        fake = server()
        with connect(fake) as client:
            assert not client.capabilities.supports("SieveScript/changes")
            with client.batch() as batch:
                assert not hasattr(batch.sieve.sieve_script, "changes")


class TestActivation:
    def test_activate_sends_the_activation_argument_and_nothing_else(self):
        # isActive is server-set, so activation happens through /set's arguments
        # rather than by patching the object.
        fake = server()
        fake.respond(
            "SieveScript/set",
            {"accountId": "a", "oldState": "1", "newState": "2", "updated": {"s1": None}},
        )
        with connect(fake) as client, client.batch() as batch:
            batch.sieve.sieve_script.activate("s1")
        arguments = fake.requests[0]["methodCalls"][0][1]
        assert arguments["onSuccessActivateScript"] == "s1"
        assert "update" not in arguments

    def test_a_creation_reference_can_be_activated(self):
        # §2.4: "#" plus the creation id, so a script can be created and made
        # active in a single call.
        fake = server()
        fake.respond(
            "SieveScript/set",
            {"accountId": "a", "created": {"A": {"id": "s1", "isActive": True}}},
        )
        with connect(fake) as client, client.batch() as batch:
            batch.sieve.sieve_script.set(create={"A": {"blobId": "S7"}})
            batch.sieve.sieve_script.activate("#A")
        assert fake.requests[0]["methodCalls"][1][1]["onSuccessActivateScript"] == "#A"

    def test_deactivate_sends_the_boolean(self):
        fake = server()
        fake.respond(
            "SieveScript/set",
            {"accountId": "a", "updated": {"s1": {"isActive": False}}},
        )
        with connect(fake) as client, client.batch() as batch:
            done = batch.sieve.sieve_script.deactivate()
        assert fake.requests[0]["methodCalls"][0][1]["onSuccessDeactivateScript"] is True
        assert done.result.updated["s1"] is not None

    def test_destroying_the_active_script_needs_two_calls_in_one_request(self):
        # §2.4 requires the deactivation to be a *separate* /set call, so the two
        # cannot be merged - but they can be batched, which is the whole point of
        # a JMAP request being a list.
        fake = server()
        fake.respond("SieveScript/set", {"accountId": "a", "destroyed": ["s1"]})
        with connect(fake) as client, client.batch() as batch:
            batch.sieve.sieve_script.deactivate()
            batch.sieve.sieve_script.set(destroy=["s1"])
        calls = fake.requests[0]["methodCalls"]
        assert len(calls) == 2
        assert calls[0][1]["onSuccessDeactivateScript"] is True
        assert calls[1][1]["destroy"] == ["s1"]


class TestScriptNames:
    def test_a_control_character_is_refused_locally(self):
        # §2.1 requires servers to reject these for ManageSieve compatibility, and
        # a stray newline is easy to get from a text field.
        fake = server()
        refused = pytest.raises(CapabilityFieldError, match="0xa")
        with connect(fake) as client, client.batch() as batch, refused:
            batch.sieve.sieve_script.check_name("two\nlines")

    def test_a_paragraph_separator_is_refused_locally(self):
        # Written as a codepoint because the character itself is invisible in a
        # source file - which is precisely why it reaches a name field by accident.
        fake = server()
        refused = pytest.raises(CapabilityFieldError, match="0x2029")
        with connect(fake) as client, client.batch() as batch, refused:
            batch.sieve.sieve_script.check_name("odd" + chr(0x2029) + "name")

    def test_the_length_limit_counts_octets_not_characters(self):
        # The trap: §1.2.1 gives maxSizeScriptName in octets and notes 512 is
        # "up to 128 Unicode characters". len() is wrong by up to 4x.
        fake = server(maxSizeScriptName=8)
        with connect(fake) as client, client.batch() as batch:
            # Four characters, twelve octets.
            with pytest.raises(CapabilityFieldError, match="maxSizeScriptName"):
                batch.sieve.sieve_script.check_name("日本語訳")
            # Eight characters, eight octets - the same limit, and it fits.
            batch.sieve.sieve_script.check_name("abcdefgh")

    def test_a_legal_name_passes(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            batch.sieve.sieve_script.check_name("my filters")

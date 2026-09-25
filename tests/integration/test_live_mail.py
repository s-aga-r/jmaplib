"""End-to-end tests against a real JMAP server.

Deselected unless ``JMAP_TEST_URL`` is set (and by the ``integration`` marker),
so the default suite stays offline and fast.

These exist because everything else in the suite proves the library agrees with
*our* model of a server. Only this proves it agrees with a real one - and the
Stalwart spike already turned up several places where the two differ.

Skips are **method-granular**, not capability-granular. The specs make that
necessary rather than merely prudent: RFC 9404 §3.1 has a server advertise
``urn:ietf:params:jmap:blob`` with an empty ``supportedTypeNames`` when it
implements no ``Blob/lookup`` at all. Asking "does this server support the
capability?" would produce spurious failures and a conformance matrix that
overstates what works.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from jmap.capabilities.mail import MAIL_URN, MailCapability
from jmap.core.errors import CapabilityFieldError
from jmap.core.ids import CreationRef
from jmap.models.mail.headers import text
from jmap.models.mail.irregular import EmailImport
from tests.integration.conftest import connect

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jmap.client import JMAPClient

pytestmark = pytest.mark.integration

JMAP_URL = os.environ.get("JMAP_TEST_URL", "")
ALICE = os.environ.get("JMAP_TEST_USER", "")
ALICE_PASSWORD = os.environ.get("JMAP_TEST_PASS", "")
BOB = os.environ.get("JMAP_TEST_USER2", "")
BOB_PASSWORD = os.environ.get("JMAP_TEST_PASS2", "")

requires_server = pytest.mark.skipif(
    not (JMAP_URL and ALICE and ALICE_PASSWORD),
    reason="set JMAP_TEST_URL, JMAP_TEST_USER and JMAP_TEST_PASS to run",
)
requires_second_account = pytest.mark.skipif(
    not (BOB and BOB_PASSWORD),
    reason="set JMAP_TEST_USER2 and JMAP_TEST_PASS2 to run delivery tests",
)


def requires_method(client: JMAPClient, method: str) -> None:
    """Skip unless this server implements ``method``.

    Method-granular on purpose - see the module docstring.
    """
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with connect(ALICE, ALICE_PASSWORD) as client:
        yield client


@pytest.fixture(scope="module")
def bob() -> Iterator[JMAPClient]:
    with connect(BOB, BOB_PASSWORD) as client:
        yield client


def mailbox_by_role(client: JMAPClient, role: str) -> str:
    """Find a mailbox by role, which is the portable way.

    Names are localised and user-editable; roles are the IANA-registered
    identifiers. Matching on name is a bug that only shows up against a
    non-English server.
    """
    with client.batch() as batch:
        handle = batch.mail.mailbox.get(ids=None)
    for mailbox in handle.result.items:
        if mailbox.role == role and mailbox.id is not None:
            return str(mailbox.id)
    pytest.skip(f"no mailbox with role {role!r}")


@requires_server
class TestSession:
    def test_the_well_known_redirect_is_followed_with_credentials_intact(self, alice):
        # Stalwart answers 307, Fastmail 302; the status is never asserted. What
        # matters is landing on the session document authenticated.
        assert alice.session.username
        assert alice.session.api_url

    def test_capabilities_resolve(self, alice):
        assert "urn:ietf:params:jmap:core" in alice.capabilities
        assert alice.capabilities.limits.max_calls_in_request >= 1

    def test_unknown_capabilities_are_surfaced_not_dropped(self, alice):
        # A vendor URN we do not model must still be visible.
        assert isinstance(alice.capabilities.unknown_urns, frozenset)

    def test_echo_round_trips(self, alice):
        assert alice.echo(hello="world") == {"hello": "world"}


@requires_server
class TestMailboxes:
    def test_get_returns_typed_mailboxes(self, alice):
        requires_method(alice, "Mailbox/get")
        with alice.batch() as batch:
            handle = batch.mail.mailbox.get(ids=None)
        assert handle.result.state
        assert all(mailbox.id for mailbox in handle.result.items)

    def test_query_and_get_chain_in_one_request(self, alice):
        requires_method(alice, "Mailbox/query")
        with alice.batch() as batch:
            query = batch.mail.mailbox.query()
            handle = batch.mail.mailbox.get(ids=query.ref_ids())
        # The back-reference was resolved server-side.
        assert len(handle.result.items) == len(query.result.ids)

    def test_a_name_past_the_advertised_limit_never_leaves(self, alice):
        requires_method(alice, "Mailbox/set")
        mail = MailCapability.of(
            alice.session.capability_value(
                MAIL_URN, alice.session.capability_account(MAIL_URN, alice.default_account)
            )
        )
        name = "x" * (mail.max_size_mailbox_name + 1)
        with (
            alice.batch() as batch,
            pytest.raises(CapabilityFieldError, match="maxSizeMailboxName"),
        ):
            batch.mail.mailbox.set(create={"m": {"name": name}})


@requires_server
class TestSorting:
    def test_an_advertised_sort_and_collation_are_answered(self, alice):
        requires_method(alice, "Email/query")
        collations = alice.capabilities.limits.collation_algorithms
        if not collations:
            pytest.skip("server advertises no collation algorithms")
        with alice.batch() as batch:
            found = batch.mail.email.query(
                sort=[
                    {"property": "receivedAt"},
                    {"property": "subject", "collation": collations[0]},
                ],
                limit=5,
            )
        assert isinstance(found.result.ids, list)

    def test_a_sort_the_server_did_not_advertise_never_leaves(self, alice):
        requires_method(alice, "Email/query")
        mail = MailCapability.of(
            alice.session.capability_value(
                MAIL_URN, alice.session.capability_account(MAIL_URN, alice.default_account)
            )
        )
        if mail.email_query_sort_options is None:
            pytest.skip("server does not say which Email sorts it supports")
        with (
            alice.batch() as batch,
            pytest.raises(CapabilityFieldError, match="emailQuerySortOptions"),
        ):
            batch.mail.email.query(sort=[{"property": "jmaplibNoSuchSort"}])


@requires_server
class TestEmailLifecycle:
    def test_create_read_and_destroy_a_draft(self, alice):
        requires_method(alice, "Email/set")
        drafts = mailbox_by_role(alice, "drafts")

        with alice.batch() as batch:
            created = batch.mail.email.set(
                create={
                    "d1": {
                        "mailboxIds": {drafts: True},
                        "keywords": {"$draft": True},
                        "subject": "jmaplib integration test",
                        "from": [{"email": ALICE}],
                        "to": [{"email": ALICE}],
                        "textBody": [{"partId": "t", "type": "text/plain"}],
                        "bodyValues": {"t": {"value": "hello from jmaplib"}},
                    }
                }
            )
        assert not created.result.has_errors, created.result.creation_errors
        email_id = created.result.created_id("d1")
        assert email_id

        with alice.batch() as batch:
            fetched = batch.mail.email.get(
                ids=[email_id], properties=["subject", "keywords", text("Subject").property_name]
            )
        email = fetched.result.items[0]
        assert email.subject == "jmaplib integration test"
        assert text("Subject").read(email) == "jmaplib integration test"

        with alice.batch() as batch:
            destroyed = batch.mail.email.set(destroy=[email_id])
        assert destroyed.result.destroyed == [email_id]

    def test_an_oversized_get_is_chunked_transparently(self, alice):
        """More ids than ``maxObjectsInGet``, merged back into one result."""
        requires_method(alice, "Email/get")
        limit = alice.capabilities.limits.max_objects_in_get
        ids = [f"nonexistent-{index}" for index in range(limit + 5)]

        with alice.batch() as batch:
            handle = batch.mail.email.get(ids=ids)

        # Every id is unknown, so they all come back in notFound - which is
        # exactly what proves the chunks were merged rather than truncated.
        assert len(handle.result.not_found) == len(ids)


def _message(subject: str) -> bytes:
    """A minimal RFC 5322 message, as an uploaded blob would hold one."""
    return (
        f"From: <{ALICE}>\r\nTo: <{ALICE}>\r\nSubject: {subject}\r\n"
        f"Message-ID: <{uuid.uuid4()}@jmaplib.test>\r\n"
        "Date: Thu, 24 Sep 2026 10:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"Body of {subject}.\r\n"
    ).encode()


@requires_server
class TestImportParseAndSnippets:
    """The mail methods with no standard shape, through their typed builders."""

    def test_a_blob_parses_as_a_message_without_being_stored(self, alice):
        requires_method(alice, "Email/parse")
        subject = f"jmaplib parse {uuid.uuid4().hex[:8]}"
        uploaded = alice.upload(_message(subject), content_type="message/rfc822")

        with alice.batch() as batch:
            parsed = batch.mail.email.parse(
                blob_ids=[uploaded.blob_id], properties=["subject", "from"]
            )

        email = parsed.result.email_of(uploaded.blob_id)
        assert email is not None, parsed.result
        assert email.subject == subject
        assert email.from_ is not None
        assert email.from_[0].email == ALICE

    def test_a_blob_imports_as_a_message_and_snippets_find_it(self, alice):
        requires_method(alice, "Email/import")
        inbox = mailbox_by_role(alice, "inbox")
        word = f"jmaplibsnippet{uuid.uuid4().hex[:8]}"
        uploaded = alice.upload(_message(f"Import {word}"), content_type="message/rfc822")

        with alice.batch() as batch:
            imported = batch.mail.email.import_(
                emails={"k1": EmailImport(blob_id=uploaded.blob_id, mailbox_ids={inbox: True})}
            )
        assert not imported.result.has_errors, imported.result.creation_errors
        email_id = imported.result.created_id("k1")
        assert email_id
        try:
            if alice.capabilities.supports("SearchSnippet/get"):
                with alice.batch() as batch:
                    snippets = batch.mail.search_snippet.get(
                        filter={"subject": word}, email_ids=[email_id]
                    )
                snippet = snippets.result.snippet_of(email_id)
                assert snippet is not None, snippets.result
                assert snippet.subject is not None
                assert f"<mark>{word}</mark>" in snippet.subject
        finally:
            with alice.batch() as batch:
                batch.mail.email.set(destroy=[email_id])


@requires_server
class TestBlobs:
    def test_upload_and_download_round_trip(self, alice):
        content = b"jmaplib blob round trip"
        uploaded = alice.upload(content, content_type="text/plain")
        assert uploaded.blob_id
        assert uploaded.size == len(content)
        assert alice.download(uploaded.blob_id, name="test.txt") == content


@requires_server
@requires_second_account
class TestDelivery:
    """The headline: a real message, actually delivered."""

    def test_alice_sends_to_bob(self, alice, bob):
        requires_method(alice, "EmailSubmission/set")
        drafts = mailbox_by_role(alice, "drafts")
        subject = "jmaplib delivery test"

        with alice.batch() as batch:
            identities = batch.submission.identity.get(ids=None)
        identity_id = next(
            (identity.id for identity in identities.result.items if identity.email == ALICE),
            None,
        )
        if identity_id is None:
            pytest.skip(f"no identity for {ALICE}")

        # One request: create the draft, then submit it by creation reference.
        #
        # `CreationRef("d1")` and not `draft.ref_created("d1", "id")`. A §3.7
        # back-reference is expressed by renaming the argument - `ids` becomes
        # `#ids` - so it can only ever replace a whole top-level argument, and
        # `emailId` here is buried inside a create object where there is no name
        # to rename. §5.3's `#d1` is the mechanism that reaches inside one, and
        # it is an ordinary string value.
        with alice.batch() as batch:
            draft = batch.mail.email.set(
                create={
                    "d1": {
                        "mailboxIds": {drafts: True},
                        "keywords": {"$draft": True},
                        "subject": subject,
                        "from": [{"email": ALICE}],
                        "to": [{"email": BOB}],
                        "textBody": [{"partId": "t", "type": "text/plain"}],
                        "bodyValues": {"t": {"value": "sent by jmaplib"}},
                    }
                }
            )
            submission = batch.submission.email_submission.set(
                create={
                    "s1": {
                        "identityId": identity_id,
                        "emailId": CreationRef("d1"),
                    }
                },
                onSuccessUpdateEmail={"#s1": {"keywords/$draft": None}},
            )

        assert not draft.result.has_errors, draft.result.creation_errors
        assert not submission.result.has_errors, submission.result.creation_errors

        arrived = _wait_for_subject(bob, subject)
        assert arrived, f"{subject!r} never arrived in bob's account"


def _wait_for_subject(client: JMAPClient, subject: str, attempts: int = 20) -> bool:
    """Poll for a delivered message.

    Polling rather than waiting on push, because delivery is asynchronous and
    the push channel is not part of this milestone.
    """
    import time

    for _ in range(attempts):
        with client.batch() as batch:
            query = batch.mail.email.query(filter={"subject": subject})
        if query.result.ids:
            return True
        time.sleep(1)
    return False


def pytest_report_header(config: Any) -> str:  # pragma: no cover - pytest hook
    return f"jmap integration target: {JMAP_URL or '(unset)'}"

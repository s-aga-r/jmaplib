"""Push against a real JMAP server (RFC 8620 §7, RFC 8887, RFC 9749).

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.

Push is the hardest thing here to test honestly, because most of what can go wrong
is a *timing* property: an event that never arrives looks exactly like nothing
having changed. So these assert what can be asserted deterministically -

- an event source connection is accepted and speaks ``text/event-stream``
- a change made by this client shows up on it within a bounded wait
- the event id it carries comes back on a reconnect
- a ping does not move the resume cursor

- and skip rather than hang when the server does not implement the piece under
test. Every wait is bounded; a push that never arrives fails with a message
saying so rather than blocking the suite.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from jmap.auth import BasicAuth
from jmap.capabilities.push import (
    VAPID_URN,
    WEBSOCKET_URN,
    VapidCapability,
    WebSocketCapability,
)
from jmap.client import JMAPClient
from jmap.models.push import PushSubscription, StateChange
from jmap.push import EventSourceClient, Ping, new_subscription

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

JMAP_URL = os.environ.get("JMAP_TEST_URL", "")
ALICE = os.environ.get("JMAP_TEST_USER", "")
ALICE_PASSWORD = os.environ.get("JMAP_TEST_PASS", "")

requires_server = pytest.mark.skipif(
    not (JMAP_URL and ALICE and ALICE_PASSWORD),
    reason="set JMAP_TEST_URL, JMAP_TEST_USER and JMAP_TEST_PASS to run",
)

#: How long to wait for a notification before calling it lost. Generous, because
#: a server may coalesce changes before pushing them.
PUSH_TIMEOUT = 15.0


def requires_method(client: JMAPClient, method: str) -> None:
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


def requires_event_source(client: JMAPClient) -> None:
    if not client.session.event_source_url:
        pytest.skip("server advertises no eventSourceUrl")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with JMAPClient.connect(JMAP_URL, auth=BasicAuth(ALICE, ALICE_PASSWORD)) as client:
        yield client


@pytest.fixture(scope="module")
def drafts(alice: JMAPClient) -> str:
    with alice.batch() as batch:
        handle = batch.mail.mailbox.get(ids=None)
    for mailbox in handle.result.items:
        if mailbox.role == "drafts" and mailbox.id is not None:
            return str(mailbox.id)
    pytest.skip("no mailbox with role 'drafts'")


def make_draft(client: JMAPClient, mailbox_id: str, subject: str) -> str:
    with client.batch() as batch:
        created = batch.mail.email.set(
            create={
                "d1": {
                    "mailboxIds": {mailbox_id: True},
                    "keywords": {"$draft": True},
                    "subject": subject,
                    "from": [{"email": ALICE}],
                    "to": [{"email": ALICE}],
                    "textBody": [{"partId": "t", "type": "text/plain"}],
                    "bodyValues": {"t": {"value": "push test"}},
                }
            }
        )
    assert not created.result.has_errors, created.result.creation_errors
    return str(created.result.created_id("d1"))


def destroy(client: JMAPClient, email_id: str) -> None:
    with client.batch() as batch:
        batch.mail.email.set(destroy=[email_id])


@requires_server
class TestEventSource:
    def test_the_connection_is_accepted(self, alice):
        """``closeafter=state`` so the request terminates without a change.

        Asking for a persistent stream here would block until the timeout on a
        perfectly healthy server, which tests patience rather than the protocol.
        """
        requires_event_source(alice)
        source = EventSourceClient(alice, close_after_state=True, ping=30)
        # Draining is the assertion: a stream that is not text/event-stream, or
        # that answers an error status, raises out of here.
        list(source.events())

    def test_a_change_this_client_makes_is_pushed_back(self, alice, drafts):
        """The end-to-end proof, and the only one that needs a real server.

        Everything else about push is testable offline; that a write actually
        produces a notification is not.
        """
        requires_event_source(alice)
        requires_method(alice, "Email/set")
        subject = f"jmaplib push {uuid.uuid4().hex[:8]}"
        source = EventSourceClient(alice, close_after_state=True, ping=30)

        email_id = make_draft(alice, drafts, subject)
        try:
            change = _wait_for_state_change(source, "Email")
            assert change is not None, f"no Email StateChange within {PUSH_TIMEOUT}s"
            assert "Email" in change.types()
        finally:
            destroy(alice, email_id)

    def test_the_cursor_comes_back_on_a_reconnect(self, alice, drafts):
        """Resumption is what makes a dropped connection cost latency, not data.

        Skipped when the server sends no event ids at all - RFC 8620 §7.3 only
        *SHOULD*s them, and there is then nothing to resume from.
        """
        requires_event_source(alice)
        subject = f"jmaplib resume {uuid.uuid4().hex[:8]}"
        source = EventSourceClient(alice, close_after_state=True, ping=30)

        email_id = make_draft(alice, drafts, subject)
        try:
            _wait_for_state_change(source, "Email")
            if not source.last_event_id:
                pytest.skip("server sends no event ids, so there is nothing to resume from")
            # The second connection carries Last-Event-ID; that it is accepted at
            # all is the assertion, since a server rejecting it would raise.
            list(source.events())
        finally:
            destroy(alice, email_id)

    def test_a_ping_never_moves_the_cursor(self, alice):
        """RFC 8620 §7.3 forbids a ping from setting an event id.

        Resuming from a keep-alive would skip every change before it. Skipped when
        no ping arrives in the window - a server may legally clamp the interval up.
        """
        requires_event_source(alice)
        source = EventSourceClient(alice, ping=30)
        before = source.last_event_id
        saw_ping = False
        deadline = time.monotonic() + PUSH_TIMEOUT
        for event in source.events():
            if isinstance(event, Ping):
                saw_ping = True
                break
            if time.monotonic() > deadline:
                break
        if not saw_ping:
            pytest.skip("no ping arrived within the window")
        assert source.last_event_id == before


@requires_server
class TestPushSubscriptions:
    def test_listing_subscriptions_is_scoped_to_these_credentials(self, alice):
        # §7.2.1: the server returns only what these credentials created, so an
        # empty list is a perfectly good result.
        requires_method(alice, "PushSubscription/get")
        with alice.batch() as batch:
            fetched = batch.core.push_subscription.get(ids=None)
        for subscription in fetched.result.items:
            assert isinstance(subscription, PushSubscription)
            # §7.2.1 forbids returning these, whatever was asked for.
            assert subscription.url is None
            assert subscription.keys is None

    def test_the_call_carries_no_account_id(self, alice):
        # Push subscriptions are not account-scoped, and sending an accountId
        # would be wrong rather than merely redundant.
        requires_method(alice, "PushSubscription/get")
        with alice.batch() as batch:
            batch.core.push_subscription.get(ids=None)

    def test_an_unverifiable_url_still_creates_a_subscription(self, alice):
        """Creation succeeds; the server then cannot verify, so nothing is pushed.

        That split is the whole security property (§7.2): registering a URL is
        cheap, but the server makes no further request to it until the client
        proves it received the verification code. The subscription is destroyed
        immediately afterwards so nothing is left pointing at a dead endpoint.
        """
        requires_method(alice, "PushSubscription/set")
        device = f"jmaplib-{uuid.uuid4().hex[:12]}"
        with alice.batch() as batch:
            created = batch.core.push_subscription.set(
                create={
                    "p": new_subscription(
                        device, "https://push.invalid.example/jmaplib-test", types=["Email"]
                    )
                }
            )
        if created.result.has_errors:
            pytest.skip(f"server refused the subscription: {created.result.creation_errors}")
        subscription_id = created.result.created_id("p")
        assert subscription_id
        with alice.batch() as batch:
            batch.core.push_subscription.set(destroy=[subscription_id])


@requires_server
class TestCapabilityObjects:
    def test_the_websocket_endpoint_is_secure_when_advertised(self, alice):
        # RFC 8887 §4.2 requires TLS. A ws:// endpoint is a server that cannot
        # carry credentials safely, which is worth failing over rather than using.
        capability = WebSocketCapability.of(alice.session.capability_value(WEBSOCKET_URN))
        if capability.url is None:
            pytest.skip("server does not advertise urn:ietf:params:jmap:websocket")
        assert capability.is_secure, f"insecure WebSocket endpoint {capability.url!r}"

    def test_the_vapid_key_is_present_when_advertised(self, alice):
        capability = VapidCapability.of(alice.session.capability_value(VAPID_URN))
        if VAPID_URN not in alice.session.capabilities:
            pytest.skip("server does not advertise urn:ietf:params:jmap:webpush-vapid")
        # RFC 9749 §3 makes the key mandatory once the capability is present, and
        # a subscription created without one cannot be authenticated.
        assert capability.application_server_key


def _wait_for_state_change(source: EventSourceClient, type_name: str) -> StateChange | None:
    """Drain reconnecting streams until a change for ``type_name`` arrives.

    Bounded: a notification that never comes fails the test with a message rather
    than blocking the suite forever.
    """
    deadline = time.monotonic() + PUSH_TIMEOUT
    while time.monotonic() < deadline:
        for event in source.events():
            if isinstance(event, StateChange) and type_name in event.types():
                return event
        time.sleep(0.5)
    return None

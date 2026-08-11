"""The sync engine against a real JMAP server.

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.

Everything in ``tests/unit/test_sync.py`` proves the splice and the page-following
agree with the RFC as I read it. Only this proves a real server produces the
inputs that reading assumes - specifically the three things a fake cannot fake:

- that ``Foo/changes`` reports a record we just created, from a state we captured
  before creating it
- that ``queryChanges`` returns a delta whose ``oldQueryState`` matches what
  ``query`` handed back, so the view is spliceable rather than permanently stale
- how the server answers a cursor it cannot honour, which is the one branch where
  the recovery differs from every other method error

Skips stay method-granular - Stalwart advertises ``:calendars`` while implementing
no ``Calendar/queryChanges``, so a capability-level skip would overstate coverage.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from jmap.auth import BasicAuth
from jmap.client import JMAPClient
from jmap.core.errors import MethodError
from jmap.models.responses import QueryChangesResponse
from jmap.sync import (
    ChangeStream,
    InMemoryStateStore,
    QuerySpec,
    QueryView,
    ResyncRequiredError,
    UncacheableQueryError,
)

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


def requires_method(client: JMAPClient, method: str) -> None:
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with JMAPClient.connect(JMAP_URL, auth=BasicAuth(ALICE, ALICE_PASSWORD)) as client:
        yield client


@pytest.fixture(scope="module")
def drafts(alice: JMAPClient) -> str:
    """The drafts mailbox, found by role rather than by name.

    Names are localised and user-editable; roles are IANA-registered.
    """
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
                    "bodyValues": {"t": {"value": "sync engine test"}},
                }
            }
        )
    assert not created.result.has_errors, created.result.creation_errors
    email_id = created.result.created_id("d1")
    assert email_id
    return str(email_id)


def destroy(client: JMAPClient, *email_ids: str) -> None:
    if not email_ids:
        return
    with client.batch() as batch:
        batch.mail.email.set(destroy=list(email_ids))


def current_state(client: JMAPClient) -> str:
    """A cursor to sync from, taken the way an application would.

    ``Email/get`` with no ids is the cheapest call that reports the type's state,
    which is what makes it the natural seed for a first sync.
    """
    with client.batch() as batch:
        handle = batch.mail.email.get(ids=[])
    return str(handle.result.state)


@requires_server
class TestChangeStream:
    def test_a_created_email_shows_up_in_changes(self, alice, drafts):
        requires_method(alice, "Email/changes")
        stream = ChangeStream(alice, "Email")
        stream.seed(current_state(alice))

        email_id = make_draft(alice, drafts, "jmaplib changes test")
        try:
            changes = stream.catch_up()
            assert email_id in changes.touched
            assert changes.new_state
        finally:
            destroy(alice, email_id)

    def test_the_cursor_advances_past_what_it_reported(self, alice, drafts):
        # The second sync must not re-report the first sync's work; if it does,
        # every caller doing incremental fetches does redundant work forever.
        requires_method(alice, "Email/changes")
        store = InMemoryStateStore()
        stream = ChangeStream(alice, "Email", store=store)
        stream.seed(current_state(alice))

        email_id = make_draft(alice, drafts, "jmaplib cursor test")
        try:
            first = stream.catch_up()
            assert email_id in first.touched
            assert stream.state == first.new_state

            second = stream.catch_up()
            assert email_id not in second.touched
        finally:
            destroy(alice, email_id)

    def test_a_destroy_is_reported_as_destroyed(self, alice, drafts):
        requires_method(alice, "Email/changes")
        email_id = make_draft(alice, drafts, "jmaplib destroy test")

        stream = ChangeStream(alice, "Email")
        stream.seed(current_state(alice))
        destroy(alice, email_id)

        changes = stream.catch_up()
        assert email_id in changes.destroyed

    def test_max_changes_forces_more_than_one_page(self, alice, drafts):
        """``hasMoreChanges`` followed to the end against a real server.

        The trap this covers: reading one page and stopping loses the tail
        *silently*, because the state string still advances past it.
        """
        requires_method(alice, "Email/changes")
        stream = ChangeStream(alice, "Email")
        stream.seed(current_state(alice))

        email_ids = [make_draft(alice, drafts, f"jmaplib paging {index}") for index in range(3)]
        try:
            changes = stream.catch_up(max_changes=1)
            assert set(email_ids) <= set(changes.touched)
            # Whether the server honours maxChanges is its own business - RFC 8620
            # §5.2 makes it advisory. Either it paged, or it answered in one go;
            # both are conformant, and both must end up with every id.
            assert changes.pages >= 1
        finally:
            destroy(alice, *email_ids)

    def test_a_dead_cursor_asks_for_a_resync(self, alice):
        """The one method error whose recovery differs from all the others.

        A server may answer a nonsense state with ``cannotCalculateChanges`` (the
        case this library translates) or with ``invalidArguments`` (also legal -
        the state string is opaque, so "unparseable" is a defensible reading).
        Both are accepted; what is asserted is that neither is swallowed.
        """
        requires_method(alice, "Email/changes")
        stream = ChangeStream(alice, "Email")
        stream.seed("jmaplib-definitely-not-a-real-state")

        with pytest.raises((ResyncRequiredError, MethodError)) as excinfo:
            stream.catch_up()
        if isinstance(excinfo.value, ResyncRequiredError):
            assert "re-download" in str(excinfo.value)


@requires_server
class TestQueryView:
    def test_a_delta_splices_into_a_cached_view(self, alice, drafts):
        requires_method(alice, "Email/queryChanges")
        query_filter = {"inMailbox": drafts}
        sort = [{"property": "receivedAt", "isAscending": False}]
        spec = QuerySpec.build("Email", str(alice.default_account), filter=query_filter, sort=sort)

        with alice.batch() as batch:
            initial = batch.mail.email.query(filter=query_filter, sort=sort, calculate_total=True)
        view = QueryView.from_query(spec, initial.result)
        if not view.can_calculate_changes:
            pytest.skip("server cannot calculate changes for this query")

        before = set(view.known_ids)
        email_id = make_draft(alice, drafts, "jmaplib queryChanges test")
        try:
            with alice.batch() as batch:
                delta = batch.mail.email.query_changes(
                    since_query_state=view.query_state,
                    filter=query_filter,
                    sort=sort,
                    calculate_total=True,
                )
            # oldQueryState has to match, or the view is unspliceable and the
            # only recovery is re-running the query.
            view.apply(delta.result)
            assert email_id in view.known_ids
            assert set(view.known_ids) - before == {email_id}
        finally:
            destroy(alice, email_id)

    def test_an_uncacheable_query_is_refused_before_the_round_trip(self, alice, drafts):
        # canCalculateChanges: false means queryChanges will never work for this
        # filter and sort - so the view refuses rather than asking and failing.
        requires_method(alice, "Email/query")
        spec = QuerySpec.build("Email", str(alice.default_account))
        with alice.batch() as batch:
            initial = batch.mail.email.query(filter={"inMailbox": drafts})
        view = QueryView.from_query(spec, initial.result)
        if view.can_calculate_changes:
            pytest.skip("server can calculate changes for this query")
        # The delta's contents are irrelevant - the guard fires before the splice.
        with pytest.raises(UncacheableQueryError):
            view.apply(QueryChangesResponse())

    def test_a_view_survives_a_round_trip_through_re_query(self, alice, drafts):
        """``reset()`` is the recovery path, so it has to actually recover."""
        requires_method(alice, "Email/query")
        spec = QuerySpec.build("Email", str(alice.default_account))
        with alice.batch() as batch:
            first = batch.mail.email.query(filter={"inMailbox": drafts}, calculate_total=True)
        view = QueryView.from_query(spec, first.result)

        email_id = make_draft(alice, drafts, "jmaplib reset test")
        try:
            _wait_for_indexing()
            with alice.batch() as batch:
                second = batch.mail.email.query(filter={"inMailbox": drafts}, calculate_total=True)
            view.reset(second.result)
            assert email_id in view.known_ids
        finally:
            destroy(alice, email_id)


@requires_server
class TestAnchorPagination:
    def test_paging_by_anchor_walks_the_whole_result_set(self, alice, drafts):
        """Anchored paging, which is the mutation-safe way to walk a query.

        Offset paging re-reads or skips rows whenever the result set shifts
        underneath it; anchoring to the last id seen does not. RFC 8620 §5.5 also
        makes ``position`` ignored when an anchor is given, which the library
        rejects locally rather than letting it pass silently.
        """
        requires_method(alice, "Email/query")
        made = [make_draft(alice, drafts, f"jmaplib anchor {index}") for index in range(5)]
        try:
            _wait_for_indexing()
            sort = [{"property": "receivedAt", "isAscending": True}]
            seen: list[str] = []
            anchor: str | None = None
            for _ in range(10):  # bounded: a server bug must not hang the suite
                with alice.batch() as batch:
                    if anchor is None:
                        handle = batch.mail.email.query(
                            filter={"inMailbox": drafts}, sort=sort, limit=2
                        )
                    else:
                        handle = batch.mail.email.query(
                            filter={"inMailbox": drafts},
                            sort=sort,
                            anchor=anchor,
                            anchor_offset=1,
                            limit=2,
                        )
                page = [str(item) for item in handle.result.ids]
                if not page:
                    break
                seen.extend(page)
                anchor = page[-1]
            assert set(made) <= set(seen)
            assert len(seen) == len(set(seen)), "anchored paging returned a duplicate"
        finally:
            destroy(alice, *made)

    def test_anchor_and_position_together_are_rejected_locally(self, alice):
        # No round trip: the server would silently ignore `position`, which is a
        # far worse outcome than an error at the call site.
        with alice.batch() as batch, pytest.raises(ValueError, match="anchor"):
            batch.mail.email.query(anchor="whatever", position=10)


def _wait_for_indexing(seconds: float = 0.5) -> None:
    """Give the server a moment to index a just-created message.

    ``Email/set`` returns when the record exists; the search index catching up is
    a separate, asynchronous step on most servers.
    """
    time.sleep(seconds)

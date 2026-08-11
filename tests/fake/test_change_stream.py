"""Following ``Foo/changes`` to the end, against the fake server."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jmap.auth import BasicAuth
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN
from jmap.capabilities.registry import Registry
from jmap.client import JMAPClient
from jmap.core.errors import MethodError
from jmap.sync import ChangeStream, InMemoryStateStore, ResyncRequiredError
from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, MAIL):
        reg.register(spec)
    return reg


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
        registry=registry(),
    )


def server() -> FakeJMAPServer:
    return FakeJMAPServer(
        capabilities={CORE_URN: {}, MAIL_URN: {}},
        primary_accounts={CORE_URN: "a", MAIL_URN: "a"},
    )


def page(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "created": [],
        "updated": [],
        "destroyed": [],
        "hasMoreChanges": False,
    }
    base.update(kwargs)
    return base


class TestFollowingPages:
    def test_a_single_page(self):
        fake = server()
        fake.respond("Email/changes", page(newState="s1", created=["m1"]))
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            changes = stream.catch_up()
        assert changes.created == ["m1"]
        assert changes.pages == 1

    def test_has_more_changes_is_followed_to_the_end(self):
        # A client that reads one page and stops loses the tail silently, because
        # the state string still advances.
        fake = server()
        pages = iter(
            [
                page(newState="s1", created=["m1"], hasMoreChanges=True),
                page(newState="s2", created=["m2"], hasMoreChanges=True),
                page(newState="s3", updated=["m1"], destroyed=["m0"]),
            ]
        )
        fake.handle("Email/changes", lambda _args, _srv: next(pages))

        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            changes = stream.catch_up()

        assert changes.pages == 3
        assert changes.created == ["m1", "m2"]
        assert changes.updated == ["m1"]
        assert changes.destroyed == ["m0"]
        assert changes.new_state == "s3"

    def test_each_page_resumes_from_the_previous_state(self):
        fake = server()
        seen: list[str] = []

        def handler(arguments: dict[str, Any], _srv: FakeJMAPServer) -> dict[str, Any]:
            seen.append(arguments["sinceState"])
            return page(newState=f"s{len(seen)}", hasMoreChanges=len(seen) < 2)

        fake.handle("Email/changes", handler)
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            stream.catch_up()
        assert seen == ["s0", "s1"]

    def test_max_changes_is_passed_through(self):
        fake = server()
        seen: dict[str, Any] = {}

        def handler(arguments: dict[str, Any], _srv: FakeJMAPServer) -> dict[str, Any]:
            seen.update(arguments)
            return page(newState="s1")

        fake.handle("Email/changes", handler)
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            stream.catch_up(max_changes=50)
        assert seen["maxChanges"] == 50

    def test_pages_can_be_consumed_lazily(self):
        # A large mailbox should not have to be held in memory all at once.
        fake = server()
        pages = iter(
            [
                page(newState="s1", created=["m1"], hasMoreChanges=True),
                page(newState="s2", created=["m2"]),
            ]
        )
        fake.handle("Email/changes", lambda _args, _srv: next(pages))

        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            first = next(iter(stream.pages()))
        assert first.created == ["m1"]


class TestCursorPersistence:
    def test_the_cursor_advances_and_is_stored(self):
        fake = server()
        fake.respond("Email/changes", page(newState="s9"))
        store = InMemoryStateStore()
        with connect(fake) as client:
            stream = ChangeStream(client, "Email", store=store)
            stream.seed("s0")
            stream.catch_up()
        assert store.snapshot() == {"a/Email": "s9"}
        assert stream.state == "s9"

    def test_an_abandoned_page_is_delivered_again(self):
        # Delivery is at-least-once on purpose: the cursor advances only when the
        # consumer comes back for the next page, so a page being processed when
        # the process dies arrives again rather than being lost.
        fake = server()
        pages = iter(
            [
                page(newState="s1", hasMoreChanges=True),
                page(newState="s2", hasMoreChanges=True),
            ]
        )
        fake.handle("Email/changes", lambda _args, _srv: next(pages))
        store = InMemoryStateStore()

        with connect(fake) as client:
            stream = ChangeStream(client, "Email", store=store)
            stream.seed("s0")
            next(iter(stream.pages()))
        assert store.get("a/Email") == "s0"

    def test_the_cursor_advances_once_the_next_page_is_requested(self):
        fake = server()
        pages = iter(
            [
                page(newState="s1", hasMoreChanges=True),
                page(newState="s2", hasMoreChanges=True),
            ]
        )
        fake.handle("Email/changes", lambda _args, _srv: next(pages))
        store = InMemoryStateStore()

        with connect(fake) as client:
            stream = ChangeStream(client, "Email", store=store)
            stream.seed("s0")
            iterator = stream.pages()
            next(iterator)
            next(iterator)
        assert store.get("a/Email") == "s1"

    def test_reset_forces_a_full_resync_next_time(self):
        fake = server()
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            stream.reset()
            assert stream.state is None
            with pytest.raises(ResyncRequiredError):
                stream.catch_up()

    def test_an_unseeded_stream_asks_for_a_full_download(self):
        # There is no cursor, so there is nothing to compute a delta from.
        fake = server()
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            with pytest.raises(ResyncRequiredError, match="re-download"):
                stream.catch_up()

    def test_a_shared_store_keeps_types_apart(self):
        fake = server()
        fake.respond("Email/changes", page(newState="e1"))
        fake.respond("Mailbox/changes", page(newState="mb1"))
        store = InMemoryStateStore()
        with connect(fake) as client:
            for type_name in ("Email", "Mailbox"):
                stream = ChangeStream(client, type_name, store=store)
                stream.seed("s0")
                stream.catch_up()
        assert store.snapshot() == {"a/Email": "e1", "a/Mailbox": "mb1"}

    def test_repr_shows_the_cursor(self):
        fake = server()
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s5")
            assert "s5" in repr(stream)


class TestResync:
    def test_cannot_calculate_changes_becomes_a_resync_request(self):
        # Not a retryable failure: RFC 8620 §5.2 requires the cache to be
        # invalidated, and retrying returns the same error forever.
        fake = server()
        fake.fail("Email/changes", "cannotCalculateChanges")

        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            with pytest.raises(ResyncRequiredError, match="re-download") as excinfo:
                stream.catch_up()
        assert excinfo.value.since_state == "s0"

    def test_the_cursor_is_left_alone_when_a_resync_is_needed(self):
        # Advancing it would lose the fact that a full download is owed.
        fake = server()
        fake.fail("Email/changes", "cannotCalculateChanges")
        store = InMemoryStateStore()
        with connect(fake) as client:
            stream = ChangeStream(client, "Email", store=store)
            stream.seed("s0")
            with pytest.raises(ResyncRequiredError):
                stream.catch_up()
        assert store.get("a/Email") == "s0"

    def test_the_error_names_the_dead_cursor(self):
        error = ResyncRequiredError("Email", "s0")
        assert error.type_name == "Email"
        assert error.since_state == "s0"
        assert "re-download" in str(error)

    def test_other_method_errors_are_not_swallowed(self):
        # Only cannotCalculateChanges means "resync"; everything else must
        # propagate as itself.
        fake = server()
        fake.fail("Email/changes", "accountNotFound")
        with connect(fake) as client:
            stream = ChangeStream(client, "Email")
            stream.seed("s0")
            with pytest.raises(MethodError, match="accountNotFound"):
                stream.catch_up()

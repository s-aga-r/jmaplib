"""Blob transfer (RFC 8620 §6) and the capability namespaces."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jmap.aio import AsyncJMAPClient
from jmap.api.namespace import CapabilityNamespace, Namespaces, attribute_name
from jmap.auth import BasicAuth
from jmap.batch import Batch, NoAccountError
from jmap.blobs import DEFAULT_UPLOAD_TYPE, check_upload_size, download_url, upload_url
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import (
    MAIL,
    MAIL_URN,
    SUBMISSION,
    SUBMISSION_URN,
    VACATION,
    VACATION_URN,
)
from jmap.capabilities.registry import Registry
from jmap.client import JMAPClient
from jmap.core.errors import CapabilityFieldError, RequestError
from jmap.core.ids import Id
from jmap.core.limits import Limits
from jmap.core.session import Session
from jmap.models.mail.objects import Email
from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"
ALL_URNS: dict[str, Any] = {CORE_URN: {}, MAIL_URN: {}, SUBMISSION_URN: {}, VACATION_URN: {}}


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, MAIL, SUBMISSION, VACATION):
        reg.register(spec)
    return reg


def server(**kwargs: Any) -> FakeJMAPServer:
    kwargs.setdefault("capabilities", ALL_URNS)
    kwargs.setdefault("primary_accounts", {CORE_URN: "a", MAIL_URN: "a", SUBMISSION_URN: "a"})
    return FakeJMAPServer(**kwargs)


def connect(fake: FakeJMAPServer, **kwargs: Any) -> JMAPClient:
    kwargs.setdefault("http", httpx.Client(**fake.client_kwargs()))
    kwargs.setdefault("registry", registry())
    return JMAPClient.connect(WELL_KNOWN, auth=BasicAuth("alice@example.com", "pw"), **kwargs)


class TestBlobUrls:
    @pytest.fixture
    def session(self) -> Session:
        return Session.from_wire(
            {
                "uploadUrl": "https://x/jmap/upload/{accountId}/",
                "downloadUrl": "https://x/jmap/download/{accountId}/{blobId}/{name}?accept={type}",
            }
        )

    def test_upload_url_expands_the_account(self, session):
        assert upload_url(session, Id("a")) == "https://x/jmap/upload/a/"

    def test_download_url_expands_every_variable(self, session):
        url = download_url(session, Id("a"), "B1", name="note.txt", content_type="text/plain")
        assert url == "https://x/jmap/download/a/B1/note.txt?accept=text%2Fplain"

    def test_reserved_characters_stay_inside_their_segment(self):
        # RFC 6570 level 1 encodes everything reserved, including "/", so a blob
        # id or filename containing a slash cannot escape its path segment.
        session = Session.from_wire(
            {"downloadUrl": "https://x/d/{accountId}/{blobId}/{name}?accept={type}"}
        )
        url = download_url(session, Id("a"), "b/../etc", name="a/b.txt")
        assert "b%2F..%2Fetc" in url
        assert "a%2Fb.txt" in url


class TestUploadLimit:
    def test_an_oversized_upload_is_refused_before_sending(self):
        # The alternative is discovering it after streaming the whole body.
        limits = Limits.from_capability({"maxSizeUpload": 100})
        with pytest.raises(CapabilityFieldError, match="maxSizeUpload"):
            check_upload_size(101, limits)

    def test_exactly_the_limit_is_allowed(self):
        check_upload_size(100, Limits.from_capability({"maxSizeUpload": 100}))


class TestBlobTransfer:
    def test_round_trip(self):
        fake = server()
        with connect(fake) as client:
            result = client.upload(b"hello world", content_type="text/plain")
            assert result.blob_id
            assert result.size == 11
            assert result.type == "text/plain"
            assert client.download(result.blob_id) == b"hello world"

    def test_the_default_type_says_bytes_rather_than_guessing(self):
        fake = server()
        with connect(fake) as client:
            assert client.upload(b"\x00\x01").type == DEFAULT_UPLOAD_TYPE

    def test_an_oversized_upload_never_reaches_the_wire(self):
        fake = server(capabilities={**ALL_URNS, CORE_URN: {"maxSizeUpload": 4}})
        with connect(fake) as client, pytest.raises(CapabilityFieldError):
            client.upload(b"too much data")

    def test_downloading_an_unknown_blob_raises(self):
        fake = server()
        with connect(fake) as client, pytest.raises(RequestError):
            client.download("nope")

    def test_upload_without_a_resolvable_account(self):
        fake = server(primary_accounts={})
        with connect(fake) as client, pytest.raises(NoAccountError, match="upload"):
            client.upload(b"x")

    def test_download_without_a_resolvable_account(self):
        fake = server(primary_accounts={})
        with connect(fake) as client, pytest.raises(NoAccountError, match="download"):
            client.download("B1")

    def test_an_explicit_account_overrides_the_default(self):
        fake = server()
        with connect(fake) as client:
            assert client.upload(b"x", account_id=Id("b")).account_id == "b"

    @pytest.mark.asyncio
    async def test_async_round_trip(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            result = await client.upload(b"async bytes", content_type="text/plain")
            assert result.blob_id is not None
            assert await client.download(result.blob_id) == b"async bytes"

    @pytest.mark.asyncio
    async def test_async_upload_limit(self):
        fake = server(capabilities={**ALL_URNS, CORE_URN: {"maxSizeUpload": 2}})
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            with pytest.raises(CapabilityFieldError):
                await client.upload(b"far too long")

    @pytest.mark.asyncio
    async def test_async_download_failure(self):
        fake = server()
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            with pytest.raises(RequestError):
                await client.download("nope")

    @pytest.mark.asyncio
    async def test_async_needs_an_account_too(self):
        fake = server(primary_accounts={})
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            with pytest.raises(NoAccountError):
                await client.upload(b"x")
            with pytest.raises(NoAccountError):
                await client.download("B1")


class TestAttributeNames:
    @pytest.mark.parametrize(
        ("type_name", "expected"),
        [
            ("Email", "email"),
            ("Mailbox", "mailbox"),
            ("EmailSubmission", "email_submission"),
            ("SearchSnippet", "search_snippet"),
            ("VacationResponse", "vacation_response"),
            ("PushSubscription", "push_subscription"),
        ],
    )
    def test_camel_to_snake(self, type_name, expected):
        assert attribute_name(type_name) == expected


class TestNamespaces:
    def _namespaces(self, capabilities_map: dict[str, Any] | None = None) -> Namespaces:
        session = Session.from_wire(
            {
                "capabilities": capabilities_map or ALL_URNS,
                "accounts": {"a": {"name": "alice"}},
                "primaryAccounts": {CORE_URN: "a", MAIL_URN: "a", SUBMISSION_URN: "a"},
            }
        )
        active = registry().resolve(session, Id("a"))
        return Namespaces(Batch(active), active)

    def test_capabilities_appear_as_attributes(self):
        namespaces = self._namespaces()
        assert set(namespaces) == {"core", "mail", "submission", "vacation"}

    def test_data_types_appear_under_their_capability(self):
        assert set(self._namespaces().mail) == {
            "email",
            "mailbox",
            "thread",
            "search_snippet",
        }

    def test_push_only_types_are_not_exposed(self):
        # EmailDelivery arrives over the push channel and has no methods.
        assert "email_delivery" not in set(self._namespaces().mail)

    def test_an_unadvertised_capability_has_no_attribute(self):
        # More useful than a namespace that exists and fails on every call.
        namespaces = self._namespaces({CORE_URN: {}, MAIL_URN: {}})
        with pytest.raises(AttributeError, match="does not offer a 'submission'"):
            _ = namespaces.submission

    def test_the_error_lists_what_is_available(self):
        with pytest.raises(AttributeError, match="mail"):
            _ = self._namespaces().calendars

    def test_an_unknown_data_type_has_no_attribute(self):
        with pytest.raises(AttributeError, match="has no data type 'calendar'"):
            _ = self._namespaces().mail.calendar

    def test_dir_reflects_the_server(self):
        namespaces = self._namespaces()
        assert "mail" in dir(namespaces)
        assert "email" in dir(namespaces.mail)

    def test_reprs(self):
        namespaces = self._namespaces()
        assert "mail" in repr(namespaces)
        assert isinstance(namespaces.mail, CapabilityNamespace)
        assert "email" in repr(namespaces.mail)

    def test_membership(self):
        namespaces = self._namespaces()
        assert "mail" in namespaces
        assert "calendars" not in namespaces

    def test_a_capability_with_no_data_types_reports_none(self):
        session = Session.from_wire({"capabilities": {CORE_URN: {}}})
        active = registry().resolve(session)
        namespaces = Namespaces(Batch(active), active)
        with pytest.raises(AttributeError, match=r"blob\|push_subscription|has no data type"):
            _ = namespaces.core.nope


class TestNamespaceCalls:
    def test_the_typed_path_and_the_raw_path_agree(self):
        fake = server()
        fake.respond("Email/query", {"ids": ["m1"], "queryState": "q"})
        fake.respond("Email/get", {"list": [{"id": "m1", "subject": "Hi"}], "state": "s"})

        with connect(fake) as client, client.batch() as batch:
            query = batch.mail.email.query(filter={"inMailbox": "mb1"})
            emails = batch.mail.email.get(ids=query.ref_ids(), properties=["subject"])

        assert query.result.ids == ["m1"]
        assert isinstance(emails.result.items[0], Email)
        assert emails.result.items[0].subject == "Hi"
        # One request, because the calls were queued in one block.
        assert len(fake.requests) == 1

    def test_the_wire_names_are_correct(self):
        fake = server()
        fake.respond("Mailbox/get", {"list": [], "state": "s"})
        with connect(fake) as client, client.batch() as batch:
            batch.mail.mailbox.get(ids=[Id("mb1")])
        assert fake.requests[0]["methodCalls"][0][0] == "Mailbox/get"

    def test_missing_capabilities_raise_on_the_batch_too(self):
        fake = server(capabilities={CORE_URN: {}, MAIL_URN: {}})
        with (
            connect(fake) as client,
            client.batch() as batch,
            pytest.raises(AttributeError, match="submission"),
        ):
            _ = batch.submission


class TestUploadFailures:
    """A refused upload must surface as a typed error, like any other request."""

    def _refusing(self) -> tuple[FakeJMAPServer, httpx.Client]:
        fake = server()
        fake.intercept = lambda request: (
            httpx.Response(413, json={"type": "about:blank", "status": 413})
            if "/jmap/upload/" in request.url.path
            else None
        )
        return fake, httpx.Client(**fake.client_kwargs())

    def test_sync(self):
        fake, http = self._refusing()
        with connect(fake, http=http) as client, pytest.raises(RequestError):
            client.upload(b"x")

    @pytest.mark.asyncio
    async def test_async(self):
        fake = server()
        fake.intercept = lambda request: (
            httpx.Response(413, json={"type": "about:blank", "status": 413})
            if "/jmap/upload/" in request.url.path
            else None
        )
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with await AsyncJMAPClient.connect(
            WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
        ) as client:
            with pytest.raises(RequestError):
                await client.upload(b"x")


class TestAsyncNamespaces:
    @pytest.mark.asyncio
    async def test_the_async_batch_exposes_them_too(self):
        fake = server()
        fake.respond("Email/get", {"list": [{"id": "m1"}], "state": "s"})
        http = httpx.AsyncClient(**fake.client_kwargs())
        async with (
            await AsyncJMAPClient.connect(
                WELL_KNOWN, auth=BasicAuth("u", "p"), http=http, registry=registry()
            ) as client,
            client.batch() as batch,
        ):
            emails = batch.mail.email.get(ids=[Id("m1")])
        assert [item.id for item in emails.result.items] == ["m1"]

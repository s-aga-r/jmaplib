"""RFC 9404 blob methods, end to end against the fake server.

The fake *computes* here rather than replaying canned answers - it concatenates
sources, slices ranges and digests the result - so these tests exercise the same
arithmetic a real server does. Two of them reproduce the RFC's own worked
examples, which is the strongest available check that both sides read §4 the same
way.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from jmap.auth import BasicAuth
from jmap.capabilities.blob import BLOB, BLOB_URN
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN
from jmap.capabilities.registry import Registry
from jmap.client import JMAPClient
from jmap.core.errors import CapabilityFieldError
from jmap.models.blob import BlobUpload, DataSource
from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"

#: The RFC 9404 §4.1.2 fixture, quoted exactly - the splice test depends on every
#: offset in it.
QUICK_BROWN = "The quick brown fox jumped over the lazy dog."

#: RFC 9404 §4.2.2: valid UTF-8 either side of two invalid octets.
INVALID_UTF8 = b"The quick brown fox jumped over the \x81\x81 dog."


def registry() -> Registry:
    reg = Registry()
    for spec in (CORE, MAIL, BLOB):
        reg.register(spec)
    return reg


def server(**blob_capability: Any) -> FakeJMAPServer:
    capability: dict[str, Any] = {
        "maxSizeBlobSet": None,
        "maxDataSources": 100,
        "supportedTypeNames": ["Mailbox", "Email"],
        "supportedDigestAlgorithms": ["sha", "sha-256"],
    }
    capability.update(blob_capability)
    return FakeJMAPServer(
        capabilities={CORE_URN: {}, MAIL_URN: {}, BLOB_URN: {}},
        accounts={
            "a": {
                "name": "alice",
                "isPersonal": True,
                "isReadOnly": False,
                # The blob capability object is per account (§3.1); putting it at
                # session level only would leave every field unreadable.
                "accountCapabilities": {BLOB_URN: capability},
            }
        },
        primary_accounts={CORE_URN: "a", MAIL_URN: "a", BLOB_URN: "a"},
    )


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
        registry=registry(),
    )


class TestUpload:
    def test_the_rfc_worked_example_splices_blobs_server_side(self):
        # RFC 9404 §4.1.2, reproduced exactly: three literals interleaved with two
        # ranges of a blob created earlier in the *same* request, referenced by
        # creation id. Getting any offset wrong changes the sentence.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            batch.blob.blob.upload(create={"b4": BlobUpload(data=[DataSource.text(QUICK_BROWN)])})
            spliced = batch.blob.blob.upload(
                create={
                    "cat": BlobUpload(
                        data=[
                            DataSource.text("How"),
                            DataSource.blob("#b4", offset=3, length=7),
                            DataSource.text("was t"),
                            DataSource.blob("#b4", offset=1, length=1),
                            DataSource.base64("YXQ/"),
                        ]
                    )
                }
            )
            # `#cat` is a creation reference in the *value*, which is how §4.1.2
            # reads a blob created earlier in the same request. A result
            # reference would not do: it yields a scalar where `ids` needs a list.
            fetched = batch.blob.blob.get(ids=["#cat"], properties=["data:asText", "size"])

        assert spliced.result.created["cat"].size == 19
        assert fetched.result.items[0].as_text == "How quick was that?"

    def test_an_uploaded_blob_is_downloadable_over_http(self):
        # The two upload paths must produce the same kind of blob: one is a method
        # call, the other an endpoint, and a blobId from either has to work
        # everywhere a blobId works.
        fake = server()
        with connect(fake) as client:
            with client.batch() as batch:
                created = batch.blob.blob.upload(
                    create={"b": BlobUpload(data=[DataSource.raw(b"\x00\x01\x02")])}
                )
            blob_id = created.result.created["b"].id
            assert blob_id is not None
            assert client.download(blob_id) == b"\x00\x01\x02"

    def test_the_type_hint_is_echoed_back(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.text("x")], type="application/sieve")}
            )
        assert created.result.created["b"].type == "application/sieve"

    def test_zero_sources_creates_an_empty_blob(self):
        # "An array of zero or more octet sources (zero to create an empty blob)".
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            created = batch.blob.blob.upload(create={"b": BlobUpload()})
        assert created.result.created["b"].size == 0

    def test_a_range_past_the_end_fails_the_creation(self):
        # §4.1: an unsatisfiable range makes the DataSourceObject invalid, and the
        # server must not silently clamp it.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            batch.blob.blob.upload(create={"small": BlobUpload(data=[DataSource.text("abc")])})
            failed = batch.blob.blob.upload(
                create={"over": BlobUpload(data=[DataSource.blob("#small", length=99)])}
            )
        assert failed.result.has_errors
        assert failed.result.creation_errors["over"].type == "invalidProperties"

    def test_an_unknown_blob_id_fails_the_creation(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            failed = batch.blob.blob.upload(
                create={"x": BlobUpload(data=[DataSource.blob("nope")])}
            )
        assert failed.result.has_errors

    def test_a_source_naming_nothing_fails_the_creation(self):
        # The model refuses to build one of these, so it takes a raw dict to
        # reach - which is exactly what a hand-rolled request would send.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            failed = batch.blob.blob.upload(create={"x": {"data": [{}]}})
        assert failed.result.creation_errors["x"].type == "invalidProperties"

    def test_a_non_object_source_fails_the_creation(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            failed = batch.blob.blob.upload(create={"x": {"data": ["not an object"]}})
        assert failed.result.has_errors

    def test_a_raw_mapping_is_accepted_as_well_as_a_model(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": {"data": [{"data:asText": "plain dict"}]}}
            )
        assert created.result.created["b"].size == len("plain dict")


class TestUploadLimits:
    def test_more_sources_than_the_server_accepts_is_refused_locally(self):
        fake = server(maxDataSources=2)
        refused = pytest.raises(CapabilityFieldError, match="maxDataSources")
        with connect(fake) as client, refused, client.batch() as batch:
            batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.text(str(n)) for n in range(3)])}
            )

    def test_inline_octets_over_the_limit_are_refused_locally(self):
        fake = server(maxSizeBlobSet=4)
        refused = pytest.raises(CapabilityFieldError, match="maxSizeBlobSet")
        with connect(fake) as client, refused, client.batch() as batch:
            batch.blob.blob.upload(create={"b": BlobUpload(data=[DataSource.text("abcdef")])})

    def test_base64_is_measured_decoded(self):
        # 8 base64 characters are 6 octets, and measuring the encoded length would
        # reject a blob that fits.
        fake = server(maxSizeBlobSet=6)
        with connect(fake) as client, client.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.raw(b"abcdef")])}
            )
        assert created.result.created["b"].size == 6

    def test_blob_sources_are_not_counted_against_the_size_limit(self):
        # Their length is not knowable client-side, so the check is a lower bound.
        # It must never reject on octets it cannot see.
        fake = server(maxSizeBlobSet=4)
        with connect(fake) as client, client.batch() as batch:
            batch.blob.blob.upload(create={"seed": BlobUpload(data=[DataSource.text("abcd")])})
            grown = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.blob("#seed"), DataSource.blob("#seed")])}
            )
        assert grown.result.created["b"].size == 8

    def test_an_unadvertised_capability_object_falls_back_to_the_rfc_minimum(self):
        # No accountCapabilities entry at all: 64 sources must still be allowed,
        # because §3.1 requires every server to accept that many.
        fake = FakeJMAPServer(
            capabilities={CORE_URN: {}, BLOB_URN: {}},
            primary_accounts={CORE_URN: "a", BLOB_URN: "a"},
        )
        with connect(fake) as client, client.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.text("x") for _ in range(64)])}
            )
        assert created.result.created["b"].size == 64


class TestGet:
    def test_the_default_properties_are_data_and_size(self):
        fake = server()
        blob_id = fake.store_blob(b"hello", "text/plain")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id])
        blob = fetched.result.items[0]
        assert blob.as_text == "hello"
        assert blob.size == 5
        assert blob.data == b"hello"

    def test_invalid_utf8_comes_back_as_base64_with_the_problem_flagged(self):
        # §4.2.2 G1: `data` adapts, so the caller still gets the octets - but
        # silently, unless isEncodingProblem is read.
        fake = server()
        blob_id = fake.store_blob(INVALID_UTF8)
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id])
        blob = fetched.result.items[0]
        assert blob.is_encoding_problem is True
        assert blob.as_text is None
        assert blob.data == INVALID_UTF8

    def test_asking_for_text_explicitly_returns_no_data_at_all(self):
        # §4.2.2 G2: having named data:asText, the client gets the flag and
        # nothing else. Reading `.as_text` alone cannot distinguish this from an
        # empty blob, which is what `data` and the flag are for.
        fake = server()
        blob_id = fake.store_blob(INVALID_UTF8)
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["data:asText", "size"])
        blob = fetched.result.items[0]
        assert blob.is_encoding_problem is True
        assert blob.data is None

    def test_base64_alone_is_never_an_encoding_problem(self):
        # §4.2.2 G3: nothing was asked to decode, so nothing failed to.
        fake = server()
        blob_id = fake.store_blob(INVALID_UTF8)
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["data:asBase64", "size"])
        blob = fetched.result.items[0]
        assert blob.is_encoding_problem is False
        assert blob.data == INVALID_UTF8

    def test_a_range_that_decodes_is_returned_as_text(self):
        # §4.2.2 G4: the invalid octets are outside the range, so there is no
        # encoding problem even though the blob as a whole has one.
        fake = server()
        blob_id = fake.store_blob(INVALID_UTF8)
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], offset=0, length=5)
        blob = fetched.result.items[0]
        assert blob.as_text == "The q"
        assert blob.is_truncated is False
        # The whole blob, not the range - which is why len(data) != size here.
        assert blob.size == len(INVALID_UTF8)

    def test_a_range_past_the_end_is_truncated_not_an_error(self):
        # §4.2.2 G5. The client gets what exists plus the flag; a length check
        # against `size` would not have revealed it, since size is the whole blob.
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], offset=2, length=100)
        blob = fetched.result.items[0]
        assert blob.is_truncated is True
        assert blob.data == b"llo"

    def test_an_offset_past_the_end_yields_an_empty_range(self):
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], offset=99)
        blob = fetched.result.items[0]
        assert blob.is_truncated is True
        assert blob.data == b""

    def test_an_open_ended_range_inside_the_blob_is_not_truncated(self):
        # A null length is "the rest", so it can only truncate by starting past
        # the end - the one asymmetry in §4.2's range rules.
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], offset=2)
        blob = fetched.result.items[0]
        assert blob.is_truncated is False
        assert blob.data == b"llo"

    def test_an_unknown_blob_is_not_found_rather_than_an_error(self):
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=["not-a-blob"])
        assert fetched.result.not_found == ["not-a-blob"]
        assert fetched.result.items == []


class TestDigests:
    def test_a_digest_is_returned_under_its_exact_property_name(self):
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["digest:sha-256", "size"])
        expected = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
        assert fetched.result.items[0].digest("sha-256") == expected

    def test_the_digest_covers_the_selected_range_not_the_whole_blob(self):
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(
                ids=[blob_id], properties=["digest:sha"], offset=1, length=3
            )
        expected = base64.b64encode(hashlib.sha1(b"ell").digest()).decode()
        assert fetched.result.items[0].digest("sha") == expected

    def test_an_unadvertised_algorithm_is_refused_before_the_round_trip(self):
        fake = server()
        matcher = pytest.raises(CapabilityFieldError, match="supportedDigestAlgorithms")
        with connect(fake) as client, matcher, client.batch() as batch:
            batch.blob.blob.get(ids=["b"], properties=["digest:sha-512"])

    def test_the_algorithm_names_are_lowercase(self):
        # RFC 3230 spells them SHA-256; JMAP lowercases them, and the mismatch is
        # invisible until the property comes back missing.
        fake = server()
        refused = pytest.raises(CapabilityFieldError, match="SHA-256")
        with connect(fake) as client, refused, client.batch() as batch:
            batch.blob.blob.get(ids=["b"], properties=["digest:SHA-256"])

    def test_an_advertised_algorithm_the_server_cannot_compute_is_simply_absent(self):
        fake = server(supportedDigestAlgorithms=["sha-384"])
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["digest:sha-384"])
        assert fetched.result.items[0].digest("sha-384") is None

    def test_requesting_no_digest_skips_the_check_entirely(self):
        fake = server(supportedDigestAlgorithms=[])
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["size"])
        assert fetched.result.items[0].size == 5

    def test_null_properties_skip_the_check_too(self):
        fake = server(supportedDigestAlgorithms=[])
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=None)
        assert fetched.result.items[0].size == 5


class TestLookup:
    def test_a_lookup_reports_which_objects_reference_a_blob(self):
        fake = server()
        fake.respond(
            "Blob/lookup",
            {
                "accountId": "a",
                "list": [{"id": "G1", "matchedIds": {"Email": ["e1", "e2"], "Mailbox": []}}],
                "notFound": ["nope"],
            },
        )
        with connect(fake) as client, client.batch() as batch:
            found = batch.blob.blob.lookup(type_names=["Email", "Mailbox"], ids=["G1", "nope"])
        info = found.result.items[0]
        assert info.ids_of("Email") == ["e1", "e2"]
        # An empty list, not an omission: §4.3 requires it so the response leaks
        # nothing about blobs that exist but are invisible.
        assert info.ids_of("Mailbox") == []
        assert info.ids_of("Thread") == []
        assert found.result.not_found == ["nope"]

    def test_each_requested_type_pulls_its_capability_into_using(self):
        # §4.3: the capability defining each type must be in the request, and
        # omitting it fails as `unknownDataType` - an error that says nothing
        # about `using`.
        fake = server()
        fake.respond("Blob/lookup", {"accountId": "a", "list": [], "notFound": []})
        with connect(fake) as client, client.batch() as batch:
            batch.blob.blob.lookup(type_names=["Email"], ids=["G1"])
        assert MAIL_URN in fake.requests[0]["using"]

    def test_a_type_the_server_does_not_support_is_refused_locally(self):
        # One bad name earns unknownDataType for the whole call, taking every
        # other name down with it.
        fake = server()
        refused = pytest.raises(CapabilityFieldError, match="Calendar")
        with connect(fake) as client, refused, client.batch() as batch:
            batch.blob.blob.lookup(type_names=["Email", "Calendar"], ids=["G1"])


class TestCopy:
    def test_blob_copy_is_not_the_standard_copy_shape(self):
        # RFC 8620 §6.3 takes blobIds and answers `copied`. Parsed as an ordinary
        # /copy it would yield an object whose `created` is silently always empty.
        fake = server()
        fake.respond(
            "Blob/copy",
            {"fromAccountId": "b", "accountId": "a", "copied": {"src": "dst"}, "notCopied": {}},
        )
        with connect(fake) as client, client.batch() as batch:
            copied = batch.core.blob.copy(from_account_id="b", blob_ids=["src"])
        assert copied.result.copied == {"src": "dst"}
        assert fake.requests[0]["methodCalls"][0][1]["blobIds"] == ["src"]

    def test_failures_come_back_keyed_by_source_blob_id(self):
        fake = server()
        fake.respond(
            "Blob/copy",
            {
                "fromAccountId": "b",
                "accountId": "a",
                "copied": {},
                "notCopied": {"src": {"type": "notFound"}},
            },
        )
        with connect(fake) as client, client.batch() as batch:
            copied = batch.core.blob.copy(from_account_id="b", blob_ids=["src"])
        assert copied.result.not_copied["src"]["type"] == "notFound"


class TestSurface:
    def test_the_two_capabilities_split_the_blob_type_between_them(self):
        # RFC 9404 §4 leaves Blob/copy with :core and takes the rest, so the same
        # data type genuinely has different methods in each namespace.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            assert hasattr(batch.core.blob, "copy")
            assert not hasattr(batch.core.blob, "upload")
            assert hasattr(batch.blob.blob, "upload")
            assert not hasattr(batch.blob.blob, "copy")

    def test_a_server_without_the_blob_capability_has_no_namespace(self):
        fake = FakeJMAPServer(capabilities={CORE_URN: {}}, primary_accounts={CORE_URN: "a"})
        missing = pytest.raises(AttributeError, match="blob")
        with connect(fake) as client, client.batch() as batch, missing:
            _ = batch.blob


class TestArguments:
    """Each builder checks its arguments before the call is queued."""

    def test_a_range_is_unsigned(self):
        with (
            connect(server()) as client,
            pytest.raises(ValidationError, match=r"Blob\.get") as excinfo,
        ):
            client.batch().blob.blob.get(ids=["G1"], offset=-1)
        assert [error["loc"] for error in excinfo.value.errors()] == [("offset",)]

    def test_type_names_are_a_list_not_one_name(self):
        # tuple("Email") would ask about five one-letter types.
        with connect(server()) as client, pytest.raises(ValidationError, match=r"Blob\.lookup"):
            client.batch().blob.blob.lookup(type_names="Email", ids=["G1"])

    def test_properties_given_as_a_back_reference_skip_the_digest_check(self):
        # They name properties that do not exist yet; the server resolves them.
        with connect(server(supportedDigestAlgorithms=[])) as client:
            batch = client.batch()
            echo = batch.add("Core/echo", {"properties": ["digest:md5"]})
            fetched = batch.blob.blob.get(ids=["G1"], properties=echo.ref("/properties"))
        assert fetched.call.to_wire_arguments()["#properties"]["path"] == "/properties"

    def test_blob_ids_to_copy_may_be_a_back_reference(self):
        with connect(server()) as client:
            batch = client.batch()
            uploaded = batch.blob.blob.upload(create={"k": {"data": []}})
            copied = batch.core.blob.copy(
                from_account_id="b", blob_ids=uploaded.ref("/created/k/id")
            )
        assert copied.call.to_wire_arguments()["#blobIds"]["path"] == "/created/k/id"

    def test_blob_ids_to_copy_go_out_as_a_list(self):
        with connect(server()) as client:
            copied = client.batch().core.blob.copy(from_account_id="b", blob_ids=("G1", "G2"))
        assert copied.call.arguments["blobIds"] == ["G1", "G2"]


class TestEdges:
    def test_an_upload_object_that_is_not_an_object_is_refused_locally(self):
        fake = server()
        with (
            connect(fake) as client,
            pytest.raises(ValidationError, match=r"Blob\.upload") as excinfo,
            client.batch() as batch,
        ):
            batch.blob.blob.upload(create={"x": "not an object"})
        assert {error["loc"][:2] for error in excinfo.value.errors()} == {("create", "x")}
        assert fake.requests == []

    def test_the_raw_path_still_reaches_the_server_which_refuses_it(self):
        # batch.add checks nothing, so the fake must answer as a server would:
        # notCreated, rather than falling over.
        fake = server()
        with connect(fake) as client, client.batch() as batch:
            failed = batch.add("Blob/upload", {"create": {"x": "not an object"}})
        assert failed.result.has_errors

    def test_a_property_the_server_does_not_recognise_is_simply_absent(self):
        fake = server()
        blob_id = fake.store_blob(b"hello")
        with connect(fake) as client, client.batch() as batch:
            fetched = batch.blob.blob.get(ids=[blob_id], properties=["size", "nonsense"])
        blob = fetched.result.items[0]
        assert blob.size == 5
        assert "nonsense" not in blob.model_dump()

    def test_a_private_type_name_passes_the_gate_and_adds_no_capability(self):
        # §4.3 lets supportedTypeNames include private types, and tells clients to
        # ignore names they do not recognise. There is no URN to derive from one,
        # and refusing the call over it would make private types unusable.
        fake = server(supportedTypeNames=["Email", "x-vendor-Thing"])
        fake.respond("Blob/lookup", {"accountId": "a", "list": [], "notFound": []})
        with connect(fake) as client, client.batch() as batch:
            batch.blob.blob.lookup(type_names=["Email", "x-vendor-Thing"], ids=["G1"])
        using = fake.requests[0]["using"]
        assert MAIL_URN in using
        assert all(not urn.endswith("Thing") for urn in using)

    def test_type_names_given_as_a_back_reference_derive_nothing(self):
        # A ResultRef names values that do not exist yet, so there is nothing to
        # inspect - and the check must not crash trying.
        fake = server()
        fake.respond("Blob/lookup", {"accountId": "a", "list": [], "notFound": []})
        fake.respond("Core/echo", {"typeNames": ["Email"]})
        with connect(fake) as client, client.batch() as batch:
            echo = batch.add("Core/echo", {"typeNames": ["Email"]})
            batch.add("Blob/lookup", {"typeNames": echo.ref("/typeNames"), "ids": ["G1"]})
        assert MAIL_URN not in fake.requests[0]["using"]

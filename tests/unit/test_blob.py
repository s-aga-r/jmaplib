"""RFC 9404 models and the blob capability's local gates."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from jmap.capabilities.blob import (
    BLOB,
    BLOB_URN,
    MIN_DATA_SOURCES,
    BlobCapability,
    check_blob_size,
    check_data_sources,
    check_digest_algorithm,
    check_lookup_types,
)
from jmap.capabilities.core import BLOB as CORE_BLOB
from jmap.capabilities.core import CORE
from jmap.core.errors import CapabilityFieldError
from jmap.models.blob import (
    Blob,
    BlobCopyResponse,
    BlobInfo,
    BlobLookupResponse,
    BlobUpload,
    DataSource,
    UploadedBlob,
)


class TestDataSource:
    def test_text_is_sent_under_its_colon_bearing_wire_name(self):
        # `data:asText` is a property name, not a nested structure, so it needs an
        # explicit alias - the camelCase generator would produce `dataAsText`.
        assert DataSource.text("hello").to_wire() == {"data:asText": "hello"}

    def test_base64_passes_through_untouched(self):
        assert DataSource.base64("YXQ/").to_wire() == {"data:asBase64": "YXQ/"}

    def test_raw_bytes_are_encoded_rather_than_sent_as_text(self):
        # data:asText must be valid UTF-8; pushing arbitrary bytes through it is
        # how a client ends up with isEncodingProblem on everything it uploads.
        source = DataSource.raw(b"\x81\x81")
        assert source.to_wire() == {"data:asBase64": base64.b64encode(b"\x81\x81").decode()}

    def test_a_blob_range_carries_only_what_was_asked_for(self):
        # Omission means "from the start" and "to the end"; sending explicit nulls
        # would say the same thing but bloat every request.
        assert DataSource.blob("G1").to_wire() == {"blobId": "G1"}
        assert DataSource.blob("G1", offset=3).to_wire() == {"blobId": "G1", "offset": 3}
        assert DataSource.blob("G1", offset=3, length=7).to_wire() == {
            "blobId": "G1",
            "offset": 3,
            "length": 7,
        }

    def test_zero_is_kept_rather_than_dropped(self):
        # `offset=0` is a value the caller chose; the RFC says it MAY be zero.
        assert DataSource.blob("G1", offset=0, length=0).to_wire() == {
            "blobId": "G1",
            "offset": 0,
            "length": 0,
        }

    def test_a_creation_reference_is_a_legal_blob_id(self):
        assert DataSource.blob("#b4").to_wire()["blobId"] == "#b4"

    def test_two_sources_at_once_are_refused(self):
        # RFC 9404 §4.1: the server MUST NOT guess the user's intent, so the
        # creation would fail anyway - buried in a notCreated map rather than here.
        with pytest.raises(ValidationError, match="exactly one"):
            DataSource(as_text="a", as_base64="Yg==")

    def test_a_source_naming_nothing_is_refused(self):
        with pytest.raises(ValidationError, match="exactly one"):
            DataSource()

    def test_a_range_without_a_blob_id_is_refused(self):
        # offset and length are meaningless without something to offset into.
        with pytest.raises(ValidationError, match="exactly one"):
            DataSource(offset=3, length=7)

    def test_wire_input_validates_the_same_way(self):
        parsed = DataSource.model_validate({"data:asText": "hi"})
        assert parsed.as_text == "hi"


class TestBlobUpload:
    def test_an_empty_data_array_is_legal(self):
        # "zero to create an empty blob".
        assert BlobUpload().data == []

    def test_sources_serialise_in_order(self):
        upload = BlobUpload(data=[DataSource.text("a"), DataSource.blob("G1")], type="text/plain")
        assert upload.to_wire() == {
            "data": [{"data:asText": "a"}, {"blobId": "G1"}],
            "type": "text/plain",
        }

    def test_an_omitted_type_stays_omitted(self):
        assert "type" not in BlobUpload(data=[DataSource.text("a")]).to_wire()


class TestBlobModel:
    def test_data_decodes_base64(self):
        blob = Blob.from_wire({"id": "G1", "data:asBase64": "aGVsbG8=", "size": 5})
        assert blob.data == b"hello"

    def test_data_encodes_text(self):
        blob = Blob.from_wire({"id": "G1", "data:asText": "hello", "size": 5})
        assert blob.data == b"hello"

    def test_base64_wins_when_both_arrived(self):
        # `data` returns base64 when the octets do not decode, so preferring it is
        # what keeps the lossy representation from being chosen over the exact one.
        blob = Blob.from_wire({"data:asBase64": "AAE=", "data:asText": "ignored"})
        assert blob.data == b"\x00\x01"

    def test_data_is_none_when_neither_was_requested(self):
        assert Blob.from_wire({"id": "G1", "size": 5}).data is None

    def test_an_empty_blob_is_not_the_same_as_an_unrequested_one(self):
        assert Blob.from_wire({"data:asText": ""}).data == b""

    def test_undecodable_base64_raises_rather_than_returning_garbage(self):
        blob = Blob.from_wire({"id": "G1", "data:asBase64": "not base64!!"})
        with pytest.raises(ValueError, match="undecodable base64"):
            _ = blob.data

    def test_a_digest_is_read_by_its_exact_property_name(self):
        # digest:<algorithm> is dynamic, so it lives in extras rather than as a
        # declared field.
        blob = Blob.from_wire({"id": "G1", "digest:sha-256": "abc=", "size": 1})
        assert blob.digest("sha-256") == "abc="
        assert blob.digest("sha") is None

    def test_a_blob_with_no_extras_at_all_reports_no_digest(self):
        assert Blob().digest("sha-256") is None

    def test_the_flags_default_to_false(self):
        blob = Blob.from_wire({"id": "G1"})
        assert blob.is_encoding_problem is False
        assert blob.is_truncated is False

    def test_size_is_the_whole_blob_not_the_range(self):
        # The trap: under a range request len(data) != size, and comparing them is
        # not how truncation is detected.
        blob = Blob.from_wire({"data:asText": "The q", "size": 43, "isTruncated": False})
        assert blob.size == 43
        assert blob.data is not None
        assert len(blob.data) == 5


class TestLookupModels:
    def test_a_missing_type_reads_as_empty(self):
        info = BlobInfo.from_wire({"id": "G1", "matchedIds": {"Email": ["e1"]}})
        assert info.ids_of("Email") == ["e1"]
        assert info.ids_of("Mailbox") == []

    def test_the_wire_list_key_is_aliased(self):
        response = BlobLookupResponse.from_wire(
            {"accountId": "a", "list": [{"id": "G1"}], "notFound": ["x"]}
        )
        assert response.items[0].id == "G1"
        assert response.not_found == ["x"]


class TestCopyModel:
    def test_blob_copy_answers_copied_not_created(self):
        # RFC 8620 §6.3's shape. Parsed as an ordinary /copy, `created` would be
        # silently empty and the ids lost.
        response = BlobCopyResponse.from_wire(
            {"fromAccountId": "b", "accountId": "a", "copied": {"src": "dst"}}
        )
        assert response.copied == {"src": "dst"}

    def test_failures_are_kept_as_raw_wire_dicts(self):
        response = BlobCopyResponse.from_wire({"notCopied": {"src": {"type": "notFound"}}})
        assert response.not_copied["src"]["type"] == "notFound"


class TestUploadedBlob:
    def test_the_id_is_the_blob_id(self):
        created = UploadedBlob.from_wire({"id": "G1", "type": "image/png", "size": 95})
        assert created.id == "G1"
        assert created.size == 95


class TestCapabilityObject:
    def test_the_advertised_fields_are_read(self):
        capability = BlobCapability.of(
            {
                "maxSizeBlobSet": 50000000,
                "maxDataSources": 100,
                "supportedTypeNames": ["Email"],
                "supportedDigestAlgorithms": ["sha", "sha-256"],
            }
        )
        assert capability.max_size_blob_set == 50000000
        assert capability.supported_digest_algorithms == ["sha", "sha-256"]

    def test_an_absent_object_falls_back_to_the_rfc_minimum(self):
        # §3.1 requires every server to allow at least 64 sources, so that is what
        # silence means rather than "unknown".
        assert BlobCapability.of({}).max_data_sources == MIN_DATA_SOURCES

    def test_a_null_size_limit_is_not_a_promise_to_accept_anything(self):
        assert BlobCapability.of({"maxSizeBlobSet": None}).max_size_blob_set is None

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        # One bad capability object should not make an otherwise working server
        # unusable.
        assert BlobCapability.of({"maxDataSources": "lots"}).max_data_sources == MIN_DATA_SOURCES
        assert BlobCapability.of("not an object").max_data_sources == MIN_DATA_SOURCES


class TestGates:
    def test_an_unadvertised_digest_is_refused(self):
        capability = BlobCapability.of({"supportedDigestAlgorithms": ["sha"]})
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_digest_algorithm("sha-256", capability)
        assert excinfo.value.urn == BLOB_URN

    def test_an_advertised_digest_passes(self):
        check_digest_algorithm("sha", BlobCapability.of({"supportedDigestAlgorithms": ["sha"]}))

    def test_too_many_sources_is_refused(self):
        with pytest.raises(CapabilityFieldError, match="maxDataSources"):
            check_data_sources(3, BlobCapability.of({"maxDataSources": 2}))

    def test_exactly_the_limit_passes(self):
        check_data_sources(2, BlobCapability.of({"maxDataSources": 2}))

    def test_a_size_over_the_limit_is_refused(self):
        with pytest.raises(CapabilityFieldError, match="maxSizeBlobSet"):
            check_blob_size(11, BlobCapability.of({"maxSizeBlobSet": 10}))

    def test_no_advertised_size_limit_means_no_check(self):
        check_blob_size(10**9, BlobCapability.of({"maxSizeBlobSet": None}))

    def test_an_unsupported_lookup_type_is_refused(self):
        capability = BlobCapability.of({"supportedTypeNames": ["Email"]})
        with pytest.raises(CapabilityFieldError, match="Calendar"):
            check_lookup_types(("Email", "Calendar"), capability)

    def test_supported_lookup_types_pass(self):
        check_lookup_types(("Email",), BlobCapability.of({"supportedTypeNames": ["Email"]}))


class TestSpec:
    def test_the_blob_type_is_described_exactly_once(self):
        # Two DataTypeSpecs with the same name would make data_type("Blob") depend
        # on which capability resolved first.
        assert CORE.data_type("Blob") is BLOB.data_type("Blob")
        assert CORE_BLOB is BLOB.data_type("Blob")

    def test_the_two_capabilities_split_the_methods(self):
        # RFC 9404 §4: Blob/copy stays with :core, the rest belong to :blob.
        assert CORE.method("Blob/copy") is not None
        assert BLOB.method("Blob/copy") is None
        assert BLOB.method("Blob/upload") is not None
        assert CORE.method("Blob/upload") is None

    def test_blob_copy_is_not_a_standard_copy(self):
        copy = CORE.method("Blob/copy")
        assert copy is not None
        assert copy.kind.value == "custom"
        assert copy.response_model is BlobCopyResponse

    def test_lookup_declares_where_its_type_names_live(self):
        lookup = BLOB.method("Blob/lookup")
        assert lookup is not None
        assert lookup.type_names_argument == "typeNames"

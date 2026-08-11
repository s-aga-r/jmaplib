"""Blob (RFC 9404), Quota (RFC 9425) and Sieve (RFC 9661) against a real server.

Deselected unless ``JMAP_TEST_URL`` is set, like the rest of ``tests/integration``.

These three are the capabilities most likely to be advertised but only partly
implemented, so every test here skips on the *method* rather than the capability -
RFC 9404 §3.1 explicitly describes a server that offers ``urn:ietf:params:jmap:blob``
while implementing no ``Blob/lookup`` at all.

The digest and lookup tests go further and skip on the capability *fields*, since
``supportedDigestAlgorithms`` and ``supportedTypeNames`` may legally be empty. That
is the difference between a conformance suite and a wish list.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import TYPE_CHECKING

import pytest

from jmap.auth import BasicAuth
from jmap.capabilities.blob import BLOB_URN, BlobCapability
from jmap.capabilities.sieve import SIEVE_URN, SieveAccountCapability
from jmap.client import JMAPClient
from jmap.models.blob import BlobUpload, DataSource

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

SCRIPT = 'require ["fileinto"];\r\nfileinto "INBOX";\r\n'


def requires_method(client: JMAPClient, method: str) -> None:
    if not client.capabilities.supports(method):
        pytest.skip(f"server does not implement {method}")


@pytest.fixture(scope="module")
def alice() -> Iterator[JMAPClient]:
    with JMAPClient.connect(JMAP_URL, auth=BasicAuth(ALICE, ALICE_PASSWORD)) as client:
        yield client


def blob_capability(client: JMAPClient) -> BlobCapability:
    return BlobCapability.of(client.session.capability_value(BLOB_URN, client.default_account))


def sieve_capability(client: JMAPClient) -> SieveAccountCapability:
    return SieveAccountCapability.of(
        client.session.capability_value(SIEVE_URN, client.default_account)
    )


@requires_server
class TestBlobUpload:
    def test_text_round_trips_through_upload_and_get(self, alice):
        requires_method(alice, "Blob/upload")
        requires_method(alice, "Blob/get")
        with alice.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.text("hello from jmaplib")])}
            )
            fetched = batch.blob.blob.get(ids=["#b"], properties=["data:asText", "size"])
        assert not created.result.has_errors, created.result.creation_errors
        assert fetched.result.items[0].as_text == "hello from jmaplib"

    def test_a_method_uploaded_blob_downloads_over_http(self, alice):
        """The two creation paths must produce interchangeable blobIds.

        One is a method call and one is an endpoint; a blobId from either has to
        work everywhere a blobId works, or half the library's blob handling is a
        special case.
        """
        requires_method(alice, "Blob/upload")
        with alice.batch() as batch:
            created = batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.raw(b"\x00\x01\x02")])}
            )
        blob_id = created.result.created["b"].id
        assert blob_id is not None
        assert alice.download(blob_id) == b"\x00\x01\x02"

    def test_sources_concatenate_in_order(self, alice):
        requires_method(alice, "Blob/upload")
        with alice.batch() as batch:
            batch.blob.blob.upload(
                create={"b": BlobUpload(data=[DataSource.text("one "), DataSource.text("two")])}
            )
            fetched = batch.blob.blob.get(ids=["#b"], properties=["data:asText"])
        assert fetched.result.items[0].as_text == "one two"

    def test_a_blob_can_be_spliced_from_another_blob(self, alice):
        """RFC 9404 §4.1's headline: build a blob out of ranges of existing ones.

        Two round trips collapse into one, and the octets never leave the server.
        The ``#b`` reference is a creation id, not a result reference - the two are
        different mechanisms, and only the former is legal inside a data source.
        """
        requires_method(alice, "Blob/upload")
        with alice.batch() as batch:
            batch.blob.blob.upload(
                create={"whole": BlobUpload(data=[DataSource.text("abcdefghij")])}
            )
            part = batch.blob.blob.upload(
                create={"part": BlobUpload(data=[DataSource.blob("#whole", offset=2, length=3)])}
            )
            fetched = batch.blob.blob.get(ids=["#part"], properties=["data:asText"])
        assert not part.result.has_errors, part.result.creation_errors
        assert fetched.result.items[0].as_text == "cde"


@requires_server
class TestBlobGet:
    def test_size_is_the_whole_blob_under_a_range_request(self, alice):
        # The trap: comparing len(data) against size is not how truncation is
        # detected, because size ignores offset and length entirely.
        requires_method(alice, "Blob/upload")
        requires_method(alice, "Blob/get")
        with alice.batch() as batch:
            batch.blob.blob.upload(create={"b": BlobUpload(data=[DataSource.text("abcdefghij")])})
            fetched = batch.blob.blob.get(
                ids=["#b"], properties=["data:asText", "size"], offset=0, length=4
            )
        blob = fetched.result.items[0]
        assert blob.as_text == "abcd"
        assert blob.size == 10

    def test_a_range_past_the_end_is_flagged_truncated(self, alice):
        requires_method(alice, "Blob/upload")
        with alice.batch() as batch:
            batch.blob.blob.upload(create={"b": BlobUpload(data=[DataSource.text("abc")])})
            fetched = batch.blob.blob.get(ids=["#b"], properties=["data:asText"], length=99)
        assert fetched.result.items[0].is_truncated is True

    def test_a_digest_matches_what_we_compute_locally(self, alice):
        """Both sides digesting the same octets is the point of the property.

        Skipped rather than failed when the algorithm is unadvertised: §3.1 lets
        ``supportedDigestAlgorithms`` be empty, and calling that a failure would
        make the conformance matrix a wish list.
        """
        requires_method(alice, "Blob/upload")
        capability = blob_capability(alice)
        if "sha-256" not in capability.supported_digest_algorithms:
            pytest.skip("server does not offer sha-256 digests")
        content = b"digest me"
        with alice.batch() as batch:
            batch.blob.blob.upload(create={"b": BlobUpload(data=[DataSource.raw(content)])})
            fetched = batch.blob.blob.get(ids=["#b"], properties=["digest:sha-256", "size"])
        expected = base64.b64encode(hashlib.sha256(content).digest()).decode()
        assert fetched.result.items[0].digest("sha-256") == expected

    def test_an_unknown_blob_is_not_found_rather_than_an_error(self, alice):
        requires_method(alice, "Blob/get")
        with alice.batch() as batch:
            fetched = batch.blob.blob.get(ids=["jmaplib-not-a-blob"], properties=["size"])
        assert fetched.result.not_found == ["jmaplib-not-a-blob"]


@requires_server
class TestBlobLookup:
    def test_a_lookup_names_the_objects_referencing_a_blob(self, alice):
        requires_method(alice, "Blob/lookup")
        capability = blob_capability(alice)
        if "Email" not in capability.supported_type_names:
            pytest.skip("server does not support Email in Blob/lookup")
        with alice.batch() as batch:
            found = batch.blob.blob.lookup(type_names=["Email"], ids=["jmaplib-not-a-blob"])
        # §4.3: an invisible or nonexistent blob still gets an empty array per
        # type, so the response leaks nothing either way.
        for info in found.result.items:
            assert info.ids_of("Email") == []


@requires_server
class TestQuota:
    def test_quotas_parse(self, alice):
        requires_method(alice, "Quota/get")
        with alice.batch() as batch:
            quotas = batch.quota.quota.get(ids=None)
        for quota in quotas.result.items:
            assert quota.id
            # §3.2 admits exactly these two units; anything else means the server
            # invented one and every arithmetic assumption downstream is wrong.
            assert quota.resource_type in {"count", "octets"}
            assert quota.scope in {"account", "domain", "global"}

    def test_changes_reports_whether_it_could_narrow_the_properties(self, alice):
        requires_method(alice, "Quota/changes")
        with alice.batch() as batch:
            state = batch.quota.quota.get(ids=[])
        with alice.batch() as batch:
            changed = batch.quota.quota.changes(since_state=str(state.result.state))
        # Either answer is conformant. What matters is that a null does not read
        # as "nothing changed" - §4.3 makes it "fetch everything".
        assert changed.result.fetch_all_properties in {True, False}


@requires_server
class TestSieve:
    def test_scripts_list(self, alice):
        requires_method(alice, "SieveScript/get")
        with alice.batch() as batch:
            scripts = batch.sieve.sieve_script.get(ids=None)
        active = [script for script in scripts.result.items if script.is_active]
        # §2.1: at most one script may be active at a time.
        assert len(active) <= 1

    def test_validate_accepts_a_well_formed_script(self, alice):
        """``SieveScript/validate`` is CHECKSCRIPT: no storage, just a verdict.

        Skipped when the engine lacks ``fileinto``, because the script would then
        be legitimately invalid and the test would be measuring the wrong thing.
        """
        requires_method(alice, "SieveScript/validate")
        requires_method(alice, "Blob/upload")
        if "fileinto" not in sieve_capability(alice).sieve_extensions:
            pytest.skip("server's Sieve engine has no fileinto")
        with alice.batch() as batch:
            batch.blob.blob.upload(
                create={"s": BlobUpload(data=[DataSource.text(SCRIPT)], type="application/sieve")}
            )
            checked = batch.sieve.sieve_script.validate(blob_id="#s")
        assert checked.result.is_valid, checked.result.problem

    def test_validate_rejects_a_broken_script_without_raising(self, alice):
        # §2.6: invalid content is not a method error. The call succeeds and the
        # verdict arrives as an argument, so nothing here should raise.
        requires_method(alice, "SieveScript/validate")
        requires_method(alice, "Blob/upload")
        with alice.batch() as batch:
            batch.blob.blob.upload(
                create={"s": BlobUpload(data=[DataSource.text("this is not sieve {{{")])}
            )
            checked = batch.sieve.sieve_script.validate(blob_id="#s")
        assert checked.result.is_valid is False
        assert checked.result.problem is not None

    def test_the_script_name_limits_are_advertised_sanely(self, alice):
        # §1.2.1 requires at least 512 octets for ManageSieve compatibility, so a
        # smaller value means the local gate would reject names the server accepts.
        requires_method(alice, "SieveScript/get")
        assert sieve_capability(alice).max_size_script_name >= 512

"""Builders for methods that do not follow any of the six standard shapes.

Most of JMAP fits :mod:`jmap.api.entity`'s composed façades. The rest does not,
and pretending otherwise is worse than writing them out: ``Blob/upload`` looks
like a ``/set`` but has no update or destroy half and no state,
``Blob/copy`` shares a name with the standard ``/copy`` while taking different
arguments and answering with different ones, and ``SieveScript/validate`` has no
analogue at all.

These are mixins keyed by *method name* rather than by type, so a builder appears
on an entity only when the resolved capability actually declares that method -
the same rule the standard shapes follow. A server advertising ``:blob`` without
implementing ``Blob/lookup`` yields an entity with no ``.lookup``.

Three of them also enforce a capability field before the call goes out, because
each of those failures is expensive to diagnose from the answer alone: an
unsupported digest, an unsupported lookup type, and a script name the server is
required to reject.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jmap.api.entity import EntityBase, builder
from jmap.capabilities.blob import (
    BLOB_URN,
    DIGEST_PREFIX,
    BlobCapability,
    check_blob_size,
    check_data_sources,
    check_digest_algorithm,
    check_lookup_types,
)
from jmap.capabilities.sieve import SIEVE_URN, SieveAccountCapability, check_script_name
from jmap.core.ids import CreationRef, Id
from jmap.core.invocation import Handle, ResultRef
from jmap.core.narrow import as_list_of, as_object, is_object
from jmap.models.arguments import UnsignedInt
from jmap.models.base import UNSET, JMAPModel, Unset, omit_unset
from jmap.models.blob import Blob, BlobCopyResponse, BlobLookupResponse, BlobUpload
from jmap.models.mdn import MDNParseResponse, MDNSendResponse, mdn_sent_patch
from jmap.models.responses import GetResponse, SetResponse
from jmap.models.sieve import SieveValidateResponse


def _sources_of(upload: Mapping[str, Any]) -> list[Any]:
    """The ``data`` array of one upload object, whatever shape it arrived in."""
    return as_list_of(upload.get("data") or [])


def _inline_octets(sources: list[Any]) -> int:
    """How many octets the inline sources contribute.

    A lower bound on the finished blob: ``blobId`` sources add an amount only the
    server knows. Base64 is measured decoded, since that is what the blob holds.
    """
    total = 0
    for source in sources:
        if not is_object(source):
            continue
        entry = as_object(source)
        text = entry.get("data:asText")
        if isinstance(text, str):
            total += len(text.encode())
            continue
        encoded = entry.get("data:asBase64")
        if isinstance(encoded, str):
            # Length of the decoded octets, without paying to decode them.
            total += len(encoded) // 4 * 3 - encoded.count("=")
    return total


def _ids(
    ids: Sequence[Id | CreationRef] | ResultRef[Any],
) -> list[Id | CreationRef] | ResultRef[Any]:
    """A list of ids as the wire wants it, or a back-reference as it is."""
    return ids if isinstance(ids, ResultRef) else list(ids)


class BlobUploadable(EntityBase[Any]):
    """``Blob/upload`` (RFC 9404 §4.1)."""

    __slots__ = ()

    @builder
    def upload(
        self,
        *,
        create: Mapping[str, BlobUpload | Mapping[str, Any]],
        **extra: Any,
    ) -> Handle[SetResponse[Any]]:
        """Create blobs from inline data, inside the batch.

        Not the same thing as :meth:`jmap.client.JMAPClient.upload`, which POSTs to
        the upload endpoint. This one is a method call, so it can be batched and
        its results back-referenced - but the content travels inside the JSON
        request and counts against ``maxSizeRequest``. RFC 9404 §4.1 recommends the
        upload endpoint for anything past a megabyte.

        The server adds each created blobId to the request's ``createdIds`` map
        whether or not one was passed, so ``#creationId`` works from any later call
        in the same request.
        """
        # The wire form now, not at serialisation: the checks below read it.
        wire: dict[str, Mapping[str, Any]] = {
            key: value.to_wire() if isinstance(value, BlobUpload) else value
            for key, value in create.items()
        }
        capability = BlobCapability.of(self._batch.capability_value(BLOB_URN))
        for upload in wire.values():
            sources = _sources_of(upload)
            check_data_sources(len(sources), capability)
            check_blob_size(_inline_octets(sources), capability)
        return self._add("upload", {"create": wire, **extra})


class BlobGettable(EntityBase[Any]):
    """``Blob/get`` (RFC 9404 §4.2), with its range arguments and digest check."""

    __slots__ = ()

    @builder
    def get(
        self,
        *,
        ids: Sequence[Id | CreationRef] | ResultRef[Any] | Unset | None = UNSET,
        properties: Sequence[str] | ResultRef[Any] | Unset | None = UNSET,
        offset: UnsignedInt | ResultRef[Any] | Unset | None = UNSET,
        length: UnsignedInt | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[GetResponse[Blob]]:
        """Fetch blob content and metadata.

        ``offset`` and ``length`` select a range, but ``size`` in the result is
        always the size of the *whole* blob - so comparing the two is not how you
        detect truncation. ``isTruncated`` is.

        Any ``digest:<algorithm>`` property is checked against the account's
        ``supportedDigestAlgorithms`` first: an unsupported one is a wasted round
        trip, and the names are lowercased in JMAP even though RFC 3230 spells them
        in upper case. A back-reference names properties that do not exist
        yet, so there is nothing to check it against.
        """
        if isinstance(properties, Sequence):
            self._check_digests(properties)
        return self._add(
            "get",
            omit_unset(ids=ids, properties=properties, offset=offset, length=length, **extra),
        )

    def _check_digests(self, properties: Sequence[str]) -> None:
        requested = [name for name in properties if name.startswith(DIGEST_PREFIX)]
        if not requested:
            return
        capability = BlobCapability.of(self._batch.capability_value(BLOB_URN))
        for name in requested:
            check_digest_algorithm(name[len(DIGEST_PREFIX) :], capability)


class BlobLookupable(EntityBase[Any]):
    """``Blob/lookup`` (RFC 9404 §4.3)."""

    __slots__ = ()

    @builder
    def lookup(
        self,
        *,
        type_names: Sequence[str],
        ids: Sequence[Id | CreationRef] | ResultRef[Any],
        **extra: Any,
    ) -> Handle[BlobLookupResponse]:
        """Find which objects reference each blob.

        Every requested type drags its own capability into ``using``; the batch
        derives that from ``typeNames`` so a lookup across ``Email`` does not have
        to be told twice that it needs the mail capability.

        Type names are checked against ``supportedTypeNames`` first, because one
        unsupported name earns ``unknownDataType`` for the whole call and takes
        every other name down with it.
        """
        names = tuple(type_names)
        check_lookup_types(names, BlobCapability.of(self._batch.capability_value(BLOB_URN)))
        return self._add("lookup", {"typeNames": list(names), "ids": ids, **extra})


class BlobCopyable(EntityBase[Any]):
    """``Blob/copy`` (RFC 8620 §6.3) - not the standard ``/copy`` shape."""

    __slots__ = ()

    @builder
    def copy(
        self,
        *,
        from_account_id: Id | ResultRef[Any],
        blob_ids: Sequence[Id | CreationRef] | ResultRef[Any],
        **extra: Any,
    ) -> Handle[BlobCopyResponse]:
        """Move blobs between accounts without a download and re-upload.

        Named ``/copy`` but shaped nothing like the others: ids in, a
        ``copied`` map out. The account it copies *into* is this batch's account.
        """
        return self._add(
            "copy", {"fromAccountId": from_account_id, "blobIds": _ids(blob_ids), **extra}
        )


class SieveValidatable(EntityBase[Any]):
    """``SieveScript/validate`` and the activation helpers (RFC 9661 §2.4, §2.6)."""

    __slots__ = ()

    @builder
    def validate(
        self, *, blob_id: Id | CreationRef | ResultRef[Any], **extra: Any
    ) -> Handle[SieveValidateResponse]:
        """Check a script without storing it.

        The content must already be a blob, so this pairs with ``Blob/upload`` in
        the same request. A syntactically invalid script is *not* a method error:
        the call succeeds and reports the problem in its ``error`` argument.
        """
        return self._add("validate", {"blobId": blob_id, **extra})

    @builder
    def activate(
        self, script_id: Id | CreationRef | ResultRef[Any], **extra: Any
    ) -> Handle[SetResponse[Any]]:
        """Make one script the active one, deactivating whatever held it.

        A ``/set`` with no changes beyond the activation, which is how RFC 9661
        models it - ``isActive`` is server-set and cannot be patched. Pass a
        :class:`~jmap.core.ids.CreationRef`, or ``#creationId``, to activate a
        script created earlier in the same call.
        """
        return self._add("set", {"onSuccessActivateScript": script_id, **extra})

    def deactivate(self, **extra: Any) -> Handle[SetResponse[Any]]:
        """Turn Sieve processing off by deactivating the active script.

        Deactivating is also the prerequisite for destroying it: RFC 9661 §2.4
        requires the active script to be deactivated in a *separate* ``/set`` call
        before it can be destroyed, so the two must be batched, not combined.
        """
        return self._add("set", {"onSuccessDeactivateScript": True, **extra})

    @builder
    def check_name(self, name: str) -> None:
        """Validate a script name against this account's advertised limits.

        Not called automatically: a name only reaches the wire inside a ``create``
        or ``update`` object, which is opaque to the generic ``/set`` builder.
        Calling it costs nothing and turns a per-script ``SetError`` into a local
        one.
        """
        check_script_name(name, SieveAccountCapability.of(self._batch.capability_value(SIEVE_URN)))


class MDNSendable(EntityBase[Any]):
    """``MDN/send`` and ``MDN/parse`` (RFC 9007 §2.1, §2.2)."""

    __slots__ = ()

    @builder
    def send(
        self,
        *,
        identity_id: Id | ResultRef[Any],
        send: Mapping[str, Mapping[str, Any] | JMAPModel],
        on_success_update_email: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> Handle[MDNSendResponse]:
        """Send read receipts, marking the acknowledged messages as done.

        ``on_success_update_email`` defaults to the ``$mdnsent`` patch every send
        needs, because RFC 9007 §2.1 makes the server *check* for it and reject
        the call otherwise - so leaving it out is not "send without bookkeeping",
        it is "send nothing". Pass an explicit mapping to add to it; pass one that
        omits ``$mdnsent`` and the server will refuse, which is its job rather
        than ours to enforce.

        A message that already carries ``$mdnsent`` must not be acknowledged
        again (§2.1); :func:`jmap.models.mdn.already_sent` is the check, and it
        needs the message's keywords, which this call does not have.
        """
        updates = (
            dict(on_success_update_email)
            if on_success_update_email is not None
            else {f"#{creation_id}": mdn_sent_patch() for creation_id in send}
        )
        return self._add(
            "send",
            {
                "identityId": identity_id,
                "send": {
                    key: value.to_wire() if isinstance(value, JMAPModel) else value
                    for key, value in send.items()
                },
                "onSuccessUpdateEmail": updates,
                **extra,
            },
        )

    @builder
    def parse(
        self, *, blob_ids: Sequence[Id | CreationRef] | ResultRef[Any], **extra: Any
    ) -> Handle[MDNParseResponse]:
        """Read blobs as MDN messages.

        Pairs with ``EmailSubmission``'s ``mdnBlobIds``, which is where receipts
        for messages *you* sent turn up - ``blob_ids`` can be a back-reference
        to them.
        """
        return self._add("parse", {"blobIds": _ids(blob_ids), **extra})


#: Method name -> the mixin that builds it. Consulted alongside the six standard
#: shapes, so an irregular method still yields a typed builder and, like the
#: standard ones, only appears when the server declares the method.
CUSTOM_BUILDERS: dict[str, type[EntityBase[Any]]] = {
    "Blob/upload": BlobUploadable,
    "Blob/get": BlobGettable,
    "Blob/lookup": BlobLookupable,
    "Blob/copy": BlobCopyable,
    "SieveScript/validate": SieveValidatable,
    "MDN/send": MDNSendable,
    "MDN/parse": MDNSendable,
}

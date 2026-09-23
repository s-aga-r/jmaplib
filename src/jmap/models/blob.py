"""Blob management (RFC 9404).

RFC 8620 already moves blobs over plain HTTP; this extension adds *methods*, so a
blob can be created, read and reverse-looked-up inside a batch alongside the calls
that reference it. That is the point of it: a Sieve script or a small attachment
can be uploaded and then used by back-reference in one round trip instead of two.

Three details drive the models here.

**The property names contain colons.** ``data:asText``, ``data:asBase64`` and
``digest:sha-256`` are property *names*, not nested structures, so they are
declared with explicit aliases, and the dynamic ``digest:<algorithm>`` family stays
in the model's extras where it is addressable by exact wire key.

**A DataSourceObject is exactly one of three things** (§4.1). The server is
required to reject an ambiguous one rather than guess, so this rejects it locally,
where the traceback still points at the caller that built it.

**``size`` is the whole blob, always.** Under a range request the returned octets
are shorter than ``size``, and ``isTruncated`` - not a length comparison - is what
says the range could not be satisfied.
"""

from __future__ import annotations

import base64
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from jmap.core.response import MalformedResponseError
from jmap.models.base import JMAPModel

#: The three mutually exclusive octet sources of a DataSourceObject (§4.1).
_SOURCE_FIELDS = ("as_text", "as_base64", "blob_id")


class DataSource(JMAPModel):
    """One octet source for ``Blob/upload`` (RFC 9404 §4.1).

    Exactly one of ``data:asText``, ``data:asBase64`` or ``blobId`` must be
    present. ``offset`` and ``length`` apply only to the ``blobId`` form and select
    a range of an existing blob, which is what lets a client splice blobs together
    server-side without downloading them first.

    Prefer the classmethods to the constructor: they are what makes the
    "exactly one" rule impossible to get wrong.
    """

    as_text: str | None = Field(default=None, alias="data:asText")
    as_base64: str | None = Field(default=None, alias="data:asBase64")
    blob_id: str | None = None
    #: Null means zero. Only meaningful alongside ``blob_id``.
    offset: int | None = None
    #: Null means "to the end of the blob". Only meaningful alongside ``blob_id``.
    length: int | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        """Reject an ambiguous source before it reaches the server.

        RFC 9404 §4.1 requires the server to refuse to guess the user's intent, so
        an ambiguous source fails either way - but as a ``notCreated`` entry buried
        in a response, rather than at the line that built it.
        """
        present = [name for name in _SOURCE_FIELDS if getattr(self, name) is not None]
        if len(present) != 1:
            raise ValueError(
                "a DataSource must carry exactly one of data:asText, data:asBase64 "
                f"or blobId; got {present or ['none']}"
            )
        return self

    # Each of these builds from the wire shape rather than from keyword
    # arguments, so a source constructed here validates through exactly the path
    # a server payload takes - and the colon-bearing names stay the only spelling
    # that appears anywhere.
    @classmethod
    def text(cls, value: str) -> Self:
        """Literal UTF-8 text, the form the RFC's own examples use."""
        return cls.model_validate({"data:asText": value})

    @classmethod
    def base64(cls, value: str) -> Self:
        """Already-encoded base64, passed through untouched."""
        return cls.model_validate({"data:asBase64": value})

    @classmethod
    def raw(cls, value: bytes) -> Self:
        """Arbitrary octets, base64-encoded here.

        Not ``data:asText``: that field must be valid UTF-8, and pushing arbitrary
        bytes through it is how a client ends up with ``isEncodingProblem`` on
        everything it uploads.
        """
        return cls.base64(base64.b64encode(value).decode("ascii"))

    @classmethod
    def blob(cls, blob_id: str, *, offset: int | None = None, length: int | None = None) -> Self:
        """A range of an existing blob.

        ``blob_id`` may be a ``#creationId`` back-reference to a blob created
        earlier in the same request (§4.1), which is what makes server-side
        splicing possible without a round trip in between.
        """
        source: dict[str, Any] = {"blobId": blob_id}
        # Omitted rather than null: the two mean the same thing to a server, and
        # every request is smaller for it.
        if offset is not None:
            source["offset"] = offset
        if length is not None:
            source["length"] = length
        return cls.model_validate(source)


class BlobUpload(JMAPModel):
    """One creation in a ``Blob/upload`` call (RFC 9404 §4.1).

    ``data`` may be empty, which creates an empty blob; several sources are
    concatenated in the order given.
    """

    data: list[DataSource] = Field(default_factory=lambda: [])
    #: A hint only. The server may sniff the content and answer with another type.
    type: str | None = None


class UploadedBlob(JMAPModel):
    """One entry in ``Blob/upload``'s ``created`` map (RFC 9404 §4.1).

    ``id`` is the blobId. The server also adds it to the request's ``createdIds``
    map whether or not the client asked for one, so it is usable by back-reference
    from any later call in the same request.
    """

    id: str | None = None
    #: What the server *decided*, which need not be the type that was offered.
    type: str | None = None
    size: int | None = None


class Blob(JMAPModel):
    """A blob as returned by ``Blob/get`` (RFC 9404 §4.2).

    Which of ``as_text`` and ``as_base64`` is populated depends both on what was
    requested and on whether the selected octets are valid UTF-8, so read
    :attr:`data` rather than either field unless you know which you asked for.
    """

    id: str | None = None
    #: The size of the *entire* blob, never of the selected range.
    size: int | None = None
    as_text: str | None = Field(default=None, alias="data:asText")
    as_base64: str | None = Field(default=None, alias="data:asBase64")
    #: The selected octets were not valid UTF-8, so ``as_text`` is null even though
    #: text was requested. Not the same thing as an empty blob.
    is_encoding_problem: bool = False
    #: The requested range ran past the end of the blob.
    is_truncated: bool = False

    @property
    def data(self) -> bytes | None:
        """The selected octets, from whichever representation arrived.

        ``None`` only when neither data property was requested. An empty blob and
        a blob whose text could not be decoded both yield ``b""``, so
        ``is_encoding_problem`` stays the way to tell those two apart.
        """
        if self.as_base64 is not None:
            try:
                return base64.b64decode(self.as_base64, validate=True)
            except ValueError as exc:  # binascii.Error is a ValueError subclass
                raise MalformedResponseError(
                    f"blob {self.id!r} returned undecodable base64"
                ) from exc
        if self.as_text is not None:
            return self.as_text.encode()
        return None

    def digest(self, algorithm: str) -> str | None:
        """The base64 digest for ``algorithm``, or ``None`` if it was not requested.

        ``digest:sha-256`` is a property name rather than a field, so it lives in
        the model's extras. JMAP lowercases the algorithm names even though the
        RFC 3230 registry spells them in upper case: ``sha-256``, never ``SHA-256``.
        """
        return (self.__pydantic_extra__ or {}).get(f"digest:{algorithm}")


class BlobInfo(JMAPModel):
    """One entry in a ``Blob/lookup`` result (RFC 9404 §4.3)."""

    id: str | None = None
    #: Type name -> ids of that type referencing this blob. A type holding nothing
    #: visible to the caller yields an empty list rather than being omitted, so the
    #: response leaks nothing about blobs that exist but are invisible.
    matched_ids: dict[str, list[str]] = Field(default_factory=dict)

    def ids_of(self, type_name: str) -> list[str]:
        """The referencing ids of one type, or an empty list."""
        return self.matched_ids.get(type_name, [])


class BlobLookupResponse(JMAPModel):
    """``Blob/lookup`` (RFC 9404 §4.3)."""

    account_id: str | None = None
    #: The wire name is ``list``; a field of that name would shadow the builtin.
    items: list[BlobInfo] = Field(default_factory=lambda: [], alias="list")
    not_found: list[str] = Field(default_factory=list)


class BlobCopyResponse(JMAPModel):
    """``Blob/copy`` (RFC 8620 §6.3).

    Not the standard ``/copy`` shape, which is the trap worth naming: this one
    answers ``copied``/``notCopied`` rather than ``created``/``notCreated``, and
    takes ``blobIds`` rather than a ``create`` map. Parsed as an ordinary
    ``CopyResponse`` it yields an object whose ``created`` is silently always empty.
    """

    from_account_id: str | None = None
    account_id: str | None = None
    #: Source blobId -> the id the blob has in the destination account.
    copied: dict[str, str] = Field(default_factory=dict)
    not_copied: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("copied", "not_copied", mode="before")
    @classmethod
    def _null_map_is_empty(cls, value: Any) -> Any:
        # RFC 8620 §6.3 makes both null when empty, like the /set results.
        return {} if value is None else value

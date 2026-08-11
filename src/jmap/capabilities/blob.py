"""``urn:ietf:params:jmap:blob`` - RFC 9404 blob management.

The ``Blob`` data type is described here rather than in :mod:`jmap.capabilities.core`
even though RFC 8620 owns ``Blob/copy``, because the type has to be described
*once*: two ``DataTypeSpec`` objects with the same name would make
``ActiveCapabilities.data_type("Blob")`` depend on which capability happened to
resolve first. Core imports the shared definition, so both capabilities describe
the same type and disagree only about which methods act on it - which is exactly
the split RFC 9404 §4 makes.

The capability object is per account and its fields gate real behaviour:
``supportedDigestAlgorithms`` decides whether ``digest:sha-256`` may be requested,
``supportedTypeNames`` decides what ``Blob/lookup`` may ask about, and
``maxSizeBlobSet`` bounds a create. Each is checked before the call goes out.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import Field

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.blob import Blob, BlobLookupResponse, UploadedBlob
from jmap.models.responses import SetResponse

BLOB_URN: Final = "urn:ietf:params:jmap:blob"

#: RFC 9404 §3.1. Servers MUST allow at least this many sources per creation, so
#: it is the safe assumption when the field is absent.
MIN_DATA_SOURCES: Final = 64

#: RFC 9404 §4.2. What ``Blob/get`` returns when ``properties`` is null. ``data``
#: is the adaptive one: text if the octets decode, base64 if they do not.
BLOB_DEFAULT_PROPERTIES: Final = ("data", "size")

#: The prefix that makes a ``Blob/get`` property a digest request.
DIGEST_PREFIX: Final = "digest:"


class BlobCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:blob`` object (RFC 9404 §3.1)."""

    #: Largest blob a ``Blob/upload`` may create, counting every source
    #: concatenated. Null means the server states no bound - which is not a promise
    #: to accept anything, only an absence of a published number.
    max_size_blob_set: int | None = None
    max_data_sources: int = MIN_DATA_SOURCES
    #: Type names ``Blob/lookup`` will accept. Empty means lookups are unsupported.
    #: May include private types; clients must ignore names they do not recognise.
    supported_type_names: list[str] = Field(default_factory=list)
    #: Lowercased RFC 3230 algorithm names. Empty means digests are unsupported.
    supported_digest_algorithms: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, value: Any) -> BlobCapability:
        """Parse an advertised capability object, tolerating a malformed one.

        A bad capability object should not make an otherwise working session
        unusable, so this degrades to the defaults rather than raising.
        """
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def check_digest_algorithm(algorithm: str, capability: BlobCapability) -> None:
    """Reject a digest the server did not advertise.

    Matching is exact and the names are lowercase: JMAP lowercases the RFC 3230
    registry spellings, so ``SHA-256`` never matches and asking for it costs a
    round trip to learn nothing.
    """
    if algorithm not in capability.supported_digest_algorithms:
        raise CapabilityFieldError(
            BLOB_URN,
            "supportedDigestAlgorithms",
            tuple(capability.supported_digest_algorithms),
            algorithm,
        )


def check_data_sources(count: int, capability: BlobCapability) -> None:
    """Reject a creation naming more sources than the server accepts.

    Exactly checkable, unlike the size, because the count is known before any of
    the sources are resolved.
    """
    if count > capability.max_data_sources:
        raise CapabilityFieldError(BLOB_URN, "maxDataSources", capability.max_data_sources, count)


def check_blob_size(known_octets: int, capability: BlobCapability) -> None:
    """Reject a creation whose *inline* octets already exceed ``maxSizeBlobSet``.

    A lower bound, not the final size: a ``blobId`` source contributes octets this
    side cannot count without fetching the blob. That makes the check one-sided -
    it never wrongly rejects, and it lets an over-large upload through when most
    of the bulk comes from existing blobs. Cheap and sound beats complete here.
    """
    limit = capability.max_size_blob_set
    if limit is not None and known_octets > limit:
        raise CapabilityFieldError(BLOB_URN, "maxSizeBlobSet", limit, known_octets)


def check_lookup_types(type_names: tuple[str, ...], capability: BlobCapability) -> None:
    """Reject type names ``Blob/lookup`` cannot answer for.

    An unsupported name earns ``unknownDataType`` for the *whole* call, so one
    stray name costs every other name in the same lookup.
    """
    unsupported = tuple(name for name in type_names if name not in capability.supported_type_names)
    if unsupported:
        raise CapabilityFieldError(
            BLOB_URN,
            "supportedTypeNames",
            tuple(capability.supported_type_names),
            unsupported,
        )


#: The one description of the ``Blob`` type. Shared with
#: :mod:`jmap.capabilities.core`, which owns ``Blob/copy``.
BLOB_TYPE: Final = DataTypeSpec(
    name="Blob",
    model=Blob,
    default_get_properties=BLOB_DEFAULT_PROPERTIES,
)

BLOB: Final = CapabilitySpec(
    urn=BLOB_URN,
    attr="blob",
    reference="RFC 9404",
    data_types=(BLOB_TYPE,),
    account_value=BlobCapability,
    methods=(
        MethodSpec(
            "Blob/upload",
            MethodKind.CUSTOM,
            mutating=True,
            # Blobs cannot be updated or destroyed and have no state, so this is
            # a /set with only the create half - which is why it is not one.
            response_model=SetResponse[UploadedBlob],
            extra_args={"create": "creation id -> UploadObject (data sources and a type hint)"},
        ),
        MethodSpec(
            "Blob/get",
            MethodKind.GET,
            chunk_by=LimitKey.GET_OBJECTS,
            extra_args={
                "offset": "start this many octets into the blob",
                "length": "return at most this many octets",
            },
        ),
        MethodSpec(
            "Blob/lookup",
            MethodKind.CUSTOM,
            response_model=BlobLookupResponse,
            # Each requested type drags its own capability into `using`; see the
            # field's docstring for why omitting it fails opaquely.
            type_names_argument="typeNames",
            extra_args={
                "typeNames": "data types to search for references",
                "ids": "blobIds to look up",
            },
        ),
    ),
)

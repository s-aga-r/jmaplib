"""``urn:ietf:params:jmap:sieve`` - RFC 9661.

Two capability objects, not one: ``implementation`` is a property of the server
and lives at session level, while every limit is per account. Reading a limit from
the session-level object gets nothing, silently.

``maxSizeScriptName`` is the field most likely to be mis-enforced. RFC 9661 §1.2.1
gives it in **octets** and notes the 512 minimum is "up to 128 Unicode characters",
so a client checking ``len(name)`` is wrong by up to a factor of four on
non-ASCII names. :func:`check_script_name` counts the UTF-8 encoding.

Names also have a forbidden character set inherited from ManageSieve. Sending one
earns a ``SetError`` per script, which is a slow way to learn about a stray
newline, so it is checked locally.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import Field

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.sieve import SieveScript, SieveValidateResponse

SIEVE_URN: Final = "urn:ietf:params:jmap:sieve"

#: RFC 9661 §1.2.1. The floor for ``maxSizeScriptName``, kept for compatibility
#: with ManageSieve, and the safe assumption when the field is absent.
MIN_SIZE_SCRIPT_NAME: Final = 512

#: RFC 9661 §2.1. Characters servers MUST reject in a script name, for
#: compatibility with ManageSieve. Control characters plus the two Unicode line
#: and paragraph separators.
FORBIDDEN_NAME_CHARS: Final = frozenset(
    [*map(chr, range(0x00, 0x20)), *map(chr, range(0x7F, 0xA0)), "\u2028", "\u2029"]
)


class SieveCapability(JMAPModel):
    """The session-level ``urn:ietf:params:jmap:sieve`` object (RFC 9661 §1.2.1)."""

    #: Name and version of the Sieve engine. Informational; nothing is gated on it.
    implementation: str | None = None


class SieveAccountCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:sieve`` object (RFC 9661 §1.2.1)."""

    #: In **octets**, not characters. See the module docstring.
    max_size_script_name: int = MIN_SIZE_SCRIPT_NAME
    max_size_script: int | None = None
    max_number_scripts: int | None = None
    #: ``redirect`` actions permitted per *evaluation*, which is not the same as the
    #: number a script may contain.
    max_number_redirects: int | None = None
    #: Case-sensitive Sieve capability strings, as written in a ``require``. A
    #: script naming anything absent from this list is rejected as ``invalidSieve``.
    sieve_extensions: list[str] = Field(default_factory=list)
    #: URI scheme parts for ``enotify``, or null when the extension is unsupported.
    notification_methods: list[str] | None = None
    #: URI scheme parts for ``extlists``, or null when the extension is unsupported.
    external_lists: list[str] | None = None

    @classmethod
    def of(cls, value: Any) -> SieveAccountCapability:
        """Parse an advertised capability object, tolerating a malformed one."""
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def check_script_name(name: str, capability: SieveAccountCapability) -> None:
    """Reject a script name the server is required to refuse (RFC 9661 §2.1).

    Two rules, both cheap here and both expensive to learn from a ``SetError``:
    the forbidden character set, and ``maxSizeScriptName`` counted in UTF-8 octets
    rather than characters.
    """
    forbidden = sorted({character for character in name if character in FORBIDDEN_NAME_CHARS})
    if forbidden:
        raise CapabilityFieldError(
            SIEVE_URN,
            "name",
            "no control characters, U+2028 or U+2029",
            tuple(hex(ord(character)) for character in forbidden),
        )
    octets = len(name.encode())
    if octets > capability.max_size_script_name:
        raise CapabilityFieldError(
            SIEVE_URN, "maxSizeScriptName", capability.max_size_script_name, octets
        )


def check_required_extensions(
    required: tuple[str, ...], capability: SieveAccountCapability
) -> None:
    """Reject a script requiring extensions this engine does not have.

    Matching is case-sensitive: RFC 9661 §1.2.1 says so, and ``fileInto`` will
    never match ``fileinto``. Storing the script would fail with ``invalidSieve``
    after the content had already been uploaded as a blob.
    """
    missing = tuple(name for name in required if name not in capability.sieve_extensions)
    if missing:
        raise CapabilityFieldError(
            SIEVE_URN, "sieveExtensions", tuple(capability.sieve_extensions), missing
        )


SIEVE: Final = CapabilitySpec(
    urn=SIEVE_URN,
    attr="sieve",
    reference="RFC 9661",
    data_types=(DataTypeSpec(name="SieveScript", model=SieveScript),),
    session_value=SieveCapability,
    account_value=SieveAccountCapability,
    methods=(
        MethodSpec("SieveScript/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        # No SieveScript/changes: RFC 9661 defines none, even though the type is
        # registered as usable for state change. A push notification can therefore
        # say "scripts changed" while offering no way to ask what - re-running
        # /get is the only answer, which is tolerable for a handful of scripts.
        MethodSpec("SieveScript/query", MethodKind.QUERY),
        MethodSpec(
            "SieveScript/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={
                "onSuccessActivateScript": "id (or #creationId) to activate once everything else succeeds",  # noqa: E501
                "onSuccessDeactivateScript": "deactivate the active script; processed before any activation",  # noqa: E501
            },
        ),
        MethodSpec(
            "SieveScript/validate",
            MethodKind.CUSTOM,
            response_model=SieveValidateResponse,
            extra_args={"blobId": "the uploaded script content to check"},
        ),
    ),
)

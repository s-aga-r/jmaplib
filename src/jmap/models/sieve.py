"""Sieve scripts over JMAP (RFC 9661).

A SieveScript is metadata plus a ``blobId``: the script text itself is a blob, so
storing one is two operations - upload the content, then reference it. With
RFC 9404's ``Blob/upload`` both fit in a single request.

Two properties of this data type are unusual enough to be worth stating.

**There is no ``SieveScript/changes``.** RFC 9661 defines ``/get``, ``/set``,
``/query`` and ``/validate`` and nothing else, yet the type is registered as usable
for state change - so a push notification can tell a client that scripts changed
while giving it no way to ask *what* changed. Re-running ``/get`` is the only
answer, which is fine because a user has a handful of scripts, not a mailbox full.

**Activation is a side effect of ``/set``, not a property.** ``isActive`` is
server-set and at most one script may hold it, so it is changed through the
``onSuccessActivateScript`` and ``onSuccessDeactivateScript`` arguments rather
than by patching the object.
"""

from __future__ import annotations

from typing import Any

from jmap.core.errors import SetError
from jmap.models.base import JMAPModel

#: RFC 9661 §2.4. Returned when the content violates the Sieve grammar, or requires
#: an extension the server's interpreter does not have.
INVALID_SIEVE = "invalidSieve"

#: RFC 9661 §2.4. Returned for a destroy aimed at the currently active script.
SIEVE_IS_ACTIVE = "sieveIsActive"


class SieveScript(JMAPModel):
    """One stored Sieve script (RFC 9661 §2.1).

    ``name`` must be unique within an account and is subject to
    ``maxSizeScriptName`` - which counts *octets*, not characters. See
    :func:`jmap.capabilities.sieve.check_script_name`.
    """

    id: str | None = None
    #: Null on create asks the server to pick one.
    name: str | None = None
    #: The blob holding the script text. Update this to replace the content.
    blob_id: str | None = None
    #: Server-set. At most one script per account has it, and it is changed through
    #: ``/set``'s activation arguments rather than by patching this property.
    is_active: bool | None = None


class SieveValidateResponse(JMAPModel):
    """``SieveScript/validate`` (RFC 9661 §2.6).

    Checks a script without storing it - the equivalent of ManageSieve's
    CHECKSCRIPT. Note the shape: a *successful* method call reports invalid content
    through a null-or-not ``error`` argument, so this is never a method error and
    never raises.
    """

    account_id: str | None = None
    #: Kept as the raw wire dict: :class:`~jmap.core.errors.SetError` lives in the
    #: pydantic-free kernel. Read :attr:`problem` for the interpreted view.
    error: dict[str, Any] | None = None

    @property
    def is_valid(self) -> bool:
        """Whether the script would be accepted."""
        return self.error is None

    @property
    def problem(self) -> SetError | None:
        """The failure as a typed error, or ``None`` if the script is valid.

        Its ``description`` carries the interpreter's message, which RFC 9661 §2.4
        says should name at least the line number of the first error - the only
        part of this response a user can act on.
        """
        return SetError.from_wire(self.error) if self.error is not None else None

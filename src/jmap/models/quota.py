"""Quota (RFC 9425).

A Quota is a read-only view of a limit and the usage against it. There is no
``Quota/set``: the server computes ``used``, and the limits are administrative.
That absence is expressed in the capability spec rather than here, so the entity
façade genuinely has no ``.set``.

The one thing worth reading carefully is ``Quota/changes``. It carries an extra
``updatedProperties`` argument whose meaning is inverted from what the name
suggests: a *list* is the optimisation (only these changed, fetch just them) and
*null* is the pessimistic case (the server cannot tell, so fetch everything).
Reading null as "nothing changed" is the bug this models against.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from jmap.models.base import JMAPModel
from jmap.models.responses import ChangesResponse


class Scope(StrEnum):
    """Who a quota applies to (RFC 9425 §3.1)."""

    ACCOUNT = "account"
    DOMAIN = "domain"
    GLOBAL = "global"


class ResourceType(StrEnum):
    """The unit a quota is measured in (RFC 9425 §3.2)."""

    COUNT = "count"
    OCTETS = "octets"


class Quota(JMAPModel):
    """One limit and the usage against it (RFC 9425 §4).

    ``scope`` and ``resource_type`` are plain strings rather than enums so that an
    unregistered value from a server does not fail the whole parse; compare them
    against :class:`Scope` and :class:`ResourceType`, which are ``StrEnum`` and so
    compare equal to their wire spellings.

    ``types`` is filtered by the server against the request's ``using``, and a
    Quota whose types are *all* unrecognised by the client is not returned at all -
    so an account can legitimately report fewer quotas than it holds.
    """

    id: str | None = None
    #: ``count`` or ``octets``. See :class:`ResourceType`.
    resource_type: str | None = None
    used: int | None = None
    hard_limit: int | None = None
    #: ``account``, ``domain`` or ``global``. See :class:`Scope`.
    scope: str | None = None
    name: str | None = None
    #: The JMAP type names this quota applies to, e.g. ``["Mail", "Calendar"]``.
    types: list[str] = Field(default_factory=list)
    #: Advisory: reaching it should warn, nothing is refused.
    warn_limit: int | None = None
    #: Some operations may be refused past this point; which ones is server policy.
    soft_limit: int | None = None
    description: str | None = None

    @property
    def remaining(self) -> int | None:
        """Headroom before the hard limit, or ``None`` if either half is unknown.

        Floors at zero: usage can exceed a limit that was lowered after the fact,
        and a negative "remaining" reads as a much stranger condition than it is.
        """
        if self.used is None or self.hard_limit is None:
            return None
        return max(0, self.hard_limit - self.used)

    def exceeds(self, limit: int | None) -> bool:
        """Whether usage has reached ``limit``. Unset limits are never exceeded."""
        return limit is not None and self.used is not None and self.used >= limit

    @property
    def is_over_warn_limit(self) -> bool:
        return self.exceeds(self.warn_limit)

    @property
    def is_over_soft_limit(self) -> bool:
        return self.exceeds(self.soft_limit)

    @property
    def is_over_hard_limit(self) -> bool:
        return self.exceeds(self.hard_limit)


class QuotaChangesResponse(ChangesResponse):
    """``Quota/changes`` (RFC 9425 §4.3).

    The standard ``/changes`` shape plus ``updatedProperties``. Because ``used``
    moves constantly while everything else rarely does, a server that tracks the
    two separately can say "only ``used`` changed", and the client can feed that
    straight into a ``Quota/get`` by back-reference to fetch a single property.
    """

    #: ``None`` means the server could not narrow it down - fetch every property.
    updated_properties: list[str] | None = None

    @property
    def fetch_all_properties(self) -> bool:
        """Whether a follow-up ``/get`` must ask for everything.

        Null ``updatedProperties`` is the *pessimistic* answer, not the empty one:
        RFC 9425 §4.3 requires it whenever the server cannot tell what changed.
        """
        return self.updated_properties is None

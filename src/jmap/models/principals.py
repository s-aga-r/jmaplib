"""Principals and share notifications (RFC 9670).

A Principal is a person, group, resource or place that can own or be granted
access to data. It is the thing a share is granted *to*, which makes it the entry
point for every sharing UI - and, since RFC 9670 updates RFC 8620 to stop listing
unsubscribed accounts in the Session, the only reliable way to discover shared
data at all.

Two details bite:

**A ShareNotification's ``id`` is typed ``String``, not ``Id``** (§3), alone among
the identifiers in this document. Applying JMAP's id charset validation to it will
reject values a conformant server is entitled to send.

**``oldRights`` and ``newRights`` are nullable maps** (§3), and ``null`` is
meaningful: no rights before means newly granted, no rights after means fully
revoked. Collapsing either to an empty dict throws away which of those happened.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from jmap.models.base import JMAPModel

#: RFC 9670 §2. The closed set ``Principal.type`` may take. Parsed permissively
#: anyway - a nonconformant value should cost one comparison, not the whole
#: ``/get`` it arrived in.
PRINCIPAL_INDIVIDUAL = "individual"
PRINCIPAL_GROUP = "group"
PRINCIPAL_RESOURCE = "resource"
PRINCIPAL_LOCATION = "location"
PRINCIPAL_OTHER = "other"

PRINCIPAL_TYPES = frozenset(
    {
        PRINCIPAL_INDIVIDUAL,
        PRINCIPAL_GROUP,
        PRINCIPAL_RESOURCE,
        PRINCIPAL_LOCATION,
        PRINCIPAL_OTHER,
    }
)

#: RFC 9670 §3. What kind of change a notification describes.
SHARE_CREATED = "created"
SHARE_UPDATED = "updated"
SHARE_DESTROYED = "destroyed"


class Principal(JMAPModel):
    """A person, group, resource or location (RFC 9670 §2)."""

    id: str | None = None
    #: One of :data:`PRINCIPAL_TYPES`. Kept as a string so an unregistered value
    #: from a server does not fail the whole response.
    type: str | None = None
    name: str | None = None
    description: str | None = None
    #: An RFC 5322 addr-spec when non-null.
    email: str | None = None
    #: An IANA time zone database name when non-null.
    time_zone: str | None = None
    #: Server-set. Capability URI -> whatever that capability's spec defines for a
    #: Principal - which is where a share picker finds ``mayShareWith``, so this
    #: is deliberately an open mapping rather than a modelled one.
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Server-set. Account id -> Account, for every account holding this
    #: Principal's data that the requesting user can reach. ``None`` means none,
    #: which RFC 9670 §1.4 distinguishes from an account holding no records.
    accounts: dict[str, dict[str, Any]] | None = None

    def capability(self, urn: str) -> dict[str, Any]:
        """This Principal's info for one capability, empty when it has none.

        A copy: the caller is reading a parsed response, and handing back the live
        nested dict lets an accidental mutation edit the model it came from.
        """
        return dict(self.capabilities.get(urn, {}))

    def may_share_with(self, urn: str) -> bool:
        """Whether this Principal can be granted access to ``urn``'s data.

        Read from the Principal's own capability object rather than from
        ``accounts``: a Principal may be a legal share target without the
        requesting user being able to see any of its accounts.
        """
        return bool(self.capability(urn).get("mayShareWith", False))


class Entity(JMAPModel):
    """Who made a change (RFC 9670 §3's ``changedBy``)."""

    name: str | None = None
    email: str | None = None
    #: ``None`` when the actor has no Principal the requesting user can see.
    principal_id: str | None = None


class ShareNotification(JMAPModel):
    """A record that someone changed your access to something (RFC 9670 §3).

    Entirely server-created and immutable; the only thing a client may do is
    destroy one. They also vanish on their own - §6.3 lets a server cap how many
    it keeps, coalesce them, or expire them by age - so an id disappearing between
    a ``/query`` and a ``/get`` is normal rather than a bug.
    """

    #: Typed ``String`` by the RFC, not ``Id``. See the module docstring.
    id: str | None = None
    created: str | None = None
    changed_by: Entity | None = None
    #: The JMAP data type whose sharing changed, e.g. ``"Calendar"``.
    object_type: str | None = None
    object_account_id: str | None = None
    object_id: str | None = None
    #: Rights before the change. ``None`` means none - newly granted access.
    old_rights: dict[str, bool] | None = None
    #: Rights after the change. ``None`` means none - access fully revoked.
    new_rights: dict[str, bool] | None = None
    #: The object's name at notification time, so a user who has *lost* access can
    #: still be told what they lost.
    name: str | None = None

    @property
    def is_revocation(self) -> bool:
        """Whether this notification says access was taken away entirely."""
        return self.new_rights is None

    @property
    def is_grant(self) -> bool:
        """Whether this notification says access was newly granted."""
        return self.old_rights is None and self.new_rights is not None

    def gained(self) -> set[str]:
        """Rights held after but not before."""
        return _true_keys(self.new_rights) - _true_keys(self.old_rights)

    def lost(self) -> set[str]:
        """Rights held before but not after."""
        return _true_keys(self.old_rights) - _true_keys(self.new_rights)


def _true_keys(rights: dict[str, bool] | None) -> set[str]:
    """The rights actually held. ``None`` and all-false are both "none"."""
    return {name for name, held in (rights or {}).items() if held}

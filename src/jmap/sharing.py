"""Granting and revoking access to shared data (RFC 9670 §4).

A shareable JMAP data type carries three properties: ``isSubscribed``,
``myRights`` (what *you* may do) and ``shareWith`` (a map of Principal id to the
rights that Principal has). RFC 9670 defines the shape and leaves the right names
to each data type - calendars use ``mayReadItems``, mail uses ``mayReadItems`` and
friends, FileNode uses ``mayAddChildren`` - so rights are modelled here as an open
mapping and never as a closed enum.

Three rules, each of which loses data silently when broken:

**Setting ``shareWith`` replaces the whole map.** Every Principal not in the new
value loses access. Read-modify-write is a race; :func:`grant` builds a *pointer*
patch instead, which changes one Principal's entry and leaves the rest alone.

**The owning Principal must not appear in the map** (§4, restated §4.1). Their
rights are implicit. A client that helpfully round-trips "everyone with access"
back into an update corrupts it.

**The account you address the ``/set`` at is not the account the Principal ids
come from.** The data lives in one account; the Principals live in another, named
by ``accountIdForPrincipal`` in the data account's ``:principals:owner``
capability. :func:`principal_account` is the lookup, and getting it wrong produces
either ``accountNotFound`` or - worse - a valid call against the wrong account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.capabilities.principals import (
    PRINCIPALS_OWNER_URN,
    PRINCIPALS_URN,
    PrincipalsCapability,
    PrincipalsOwnerCapability,
)
from jmap.core.errors import JMAPError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from jmap.core.session import Session

#: Principal id -> right name -> held. The right names belong to the data type
#: being shared, so this stays open.
Rights = dict[str, bool]
ShareMap = dict[str, Rights]


class OwnerInShareMapError(JMAPError):
    """The account's owning Principal was included in a ``shareWith`` map.

    RFC 9670 §4 forbids it: the owner's rights are implicit, and including them
    makes the update invalid rather than redundant.
    """

    def __init__(self, principal_id: str) -> None:
        self.principal_id = principal_id
        super().__init__(
            f"{principal_id!r} owns this account, so it must not appear in shareWith; "
            f"its rights are implicit (RFC 9670 §4)"
        )


def _owner(session: Session, data_account_id: str) -> PrincipalsOwnerCapability:
    """Read ``:principals:owner`` from the account map and nowhere else.

    Not :meth:`Session.capability_value`, which falls back to the session-level
    map: RFC 9670 §1.5.2 says this key never appears there, so the fallback can
    only ever produce a wrong answer - and a confident one, since it would give
    every unowned account the same bogus owner.
    """
    return PrincipalsOwnerCapability.of(
        session.account_capability_value(PRINCIPALS_OWNER_URN, _as_id(data_account_id))
    )


def principal_account(session: Session, data_account_id: str) -> str | None:
    """Which account to address ``Principal/*`` at, for data in ``data_account_id``.

    ``None`` when the account has no owning Principal - the account holding the
    Principals themselves is the usual case, and it is not an error.
    """
    return _owner(session, data_account_id).account_id_for_principal


def owner_of(session: Session, data_account_id: str) -> str | None:
    """The Principal that owns an account, or ``None`` if none does."""
    return _owner(session, data_account_id).principal_id


def me(session: Session, principal_account_id: str) -> str | None:
    """The requesting user's own Principal id in an account, if it has one."""
    capability = PrincipalsCapability.of(
        session.account_capability_value(PRINCIPALS_URN, _as_id(principal_account_id))
    )
    return capability.current_user_principal_id


def check_share_map(share_with: Mapping[str, Any], owner_principal_id: str | None) -> None:
    """Refuse a ``shareWith`` map that names the account's owner."""
    if owner_principal_id is not None and owner_principal_id in share_with:
        raise OwnerInShareMapError(owner_principal_id)


def _pointer(principal_id: str) -> str:
    """A ``shareWith`` pointer for one Principal, RFC 6901-escaped.

    A JMAP ``Id`` cannot contain ``/`` or ``~``, so this never fires against
    conformant data - but ``Principal.id`` is a plain string here, and an
    unescaped separator would silently address a *different*, nested path.
    """
    escaped = principal_id.replace("~", "~0").replace("/", "~1")
    return f"shareWith/{escaped}"


def grant(
    principal_id: str,
    rights: Mapping[str, bool],
    *,
    owner_principal_id: str | None = None,
) -> dict[str, Any]:
    """A patch granting one Principal a set of rights, leaving others untouched.

    A pointer patch rather than a whole-map assignment, because assigning the map
    revokes everyone absent from it. That is the difference between "add Bob" and
    "make Bob the only person with access".

    ``owner_principal_id`` enables the same owner check :func:`replace` does. It
    is optional only because the caller may not have looked the owner up; passing
    it turns an update the server will reject into a local error.
    """
    if owner_principal_id is not None and principal_id == owner_principal_id:
        raise OwnerInShareMapError(principal_id)
    return {_pointer(principal_id): dict(rights)}


def revoke(principal_id: str) -> dict[str, Any]:
    """A patch removing one Principal's access, leaving others untouched.

    ``null`` at a pointer removes the key (RFC 8620 §5.3), which is what makes
    this expressible without reading the map first.
    """
    return {_pointer(principal_id): None}


def unshare() -> dict[str, Any]:
    """A patch removing *everyone's* access.

    ``null`` rather than ``{}``: RFC 9670 §4 defines ``null`` as "shared with
    nobody", and an empty object is not stated to mean the same thing.
    """
    return {"shareWith": None}


def replace(
    share_with: Mapping[str, Mapping[str, bool]], *, owner_principal_id: str | None = None
) -> dict[str, Any]:
    """A patch replacing the whole map, checked against the owner rule.

    Use :func:`grant` and :func:`revoke` unless you really do mean "these
    Principals and no others".
    """
    check_share_map(share_with, owner_principal_id)
    return {"shareWith": {key: dict(value) for key, value in share_with.items()}}


def held(rights: Mapping[str, bool] | None) -> set[str]:
    """The rights actually held. Absent and explicitly-false are both "not held"."""
    return {name for name, value in (rights or {}).items() if value}


def may(rights: Mapping[str, bool] | None, *names: str) -> bool:
    """Whether every named right is held.

    A missing right is not held: RFC 9670 gives no default, and assuming one
    means a client offers an action the server will refuse.
    """
    granted = held(rights)
    return all(name in granted for name in names)


def subscribe(*, subscribed: bool = True) -> dict[str, Any]:
    """A patch changing whether you see this object's data.

    Its initial value when someone shares with you is implementation-dependent
    (§4), so a client that wants data visible has to set it rather than assume.
    """
    return {"isSubscribed": subscribed}


def targets(principals: Iterable[Any], urn: str) -> list[Any]:
    """The Principals that may be granted access to ``urn``'s data.

    Filters on the Principal's own capability object rather than on ``accounts``:
    a legal share target need not have any account the requesting user can see.
    """
    return [principal for principal in principals if principal.may_share_with(urn)]


def _as_id(value: str) -> Any:
    """Session lookups take an ``Id``; the distinction is a NewType, not a class."""
    return value

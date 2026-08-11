"""``urn:ietf:params:jmap:principals`` and its ``:owner`` companion (RFC 9670).

Two URNs that behave very differently, and the difference is the thing to get
right:

**``:principals`` is a normal capability.** Session value is an empty object;
the per-account value carries ``currentUserPrincipalId``, which is how a client
learns which Principal *it* is.

**``:principals:owner`` never appears at session level at all** (§1.5.2). It is an
``accountCapabilities``-only marker saying "this account is owned by a Principal,
and here is where to find it". Looking for it in ``session.capabilities`` always
fails, and putting it in ``using`` sends a URN the server never advertised there.
It declares no methods precisely so that ``using`` derivation can never reach for
it.

Its absence is also meaningful rather than an error: an account with no ``:owner``
key is one no Principal owns - typically the account that holds the Principals
themselves.

The trap worth stating loudest is in §1.4, because it changes RFC 8620 out from
under existing clients: a server implementing this spec **MUST only** list
accounts in the Session where the user is subscribed to something or owns the
account. Enumerating ``session.accounts`` to find shared data therefore misses
everything the user has permission for but has not subscribed to.
``Principal/query`` plus ``Principal.accounts`` is the discovery path that works.
"""

from __future__ import annotations

from typing import Any, Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.principals import Principal, ShareNotification

PRINCIPALS_URN: Final = "urn:ietf:params:jmap:principals"
PRINCIPALS_OWNER_URN: Final = "urn:ietf:params:jmap:principals:owner"


class PrincipalsCapability(JMAPModel):
    """The per-account ``:principals`` object (RFC 9670 §1.5.1)."""

    #: Which Principal in this account is the requesting user. ``None`` is a legal
    #: answer meaning the user has none here - not an error, and not "unknown".
    current_user_principal_id: str | None = None

    @classmethod
    def of(cls, value: Any) -> PrincipalsCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


class PrincipalsOwnerCapability(JMAPModel):
    """The per-account ``:principals:owner`` object (RFC 9670 §1.5.2).

    Both fields are required when the key is present. When it is absent the
    account simply has no owning Principal, which :func:`owner_of` reports as
    ``None`` rather than as a failure.
    """

    #: The account to address ``Principal/*`` calls at. **Not** the account the
    #: shared data lives in - see :func:`owner_of`.
    account_id_for_principal: str | None = None
    #: The Principal that owns the account this object was read from.
    principal_id: str | None = None

    @classmethod
    def of(cls, value: Any) -> PrincipalsOwnerCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


PRINCIPALS: Final = CapabilitySpec(
    urn=PRINCIPALS_URN,
    attr="principals",
    reference="RFC 9670",
    account_value=PrincipalsCapability,
    data_types=(
        DataTypeSpec(name="Principal", model=Principal),
        DataTypeSpec(name="ShareNotification", model=ShareNotification),
    ),
    methods=(
        MethodSpec("Principal/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        # §2.2 and §2.5 may answer `cannotCalculateChanges` *permanently* on a
        # directory-backed server, so a sync loop needs a full-resync fallback
        # rather than a retry.
        MethodSpec("Principal/changes", MethodKind.CHANGES),
        MethodSpec("Principal/query", MethodKind.QUERY),
        MethodSpec("Principal/queryChanges", MethodKind.QUERY_CHANGES),
        # §2.3: the server rejects anything it disallows with `forbidden`, and
        # only SHOULD allow name/description/timeZone on your own Principal.
        MethodSpec("Principal/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS),
        MethodSpec("ShareNotification/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("ShareNotification/changes", MethodKind.CHANGES),
        MethodSpec("ShareNotification/query", MethodKind.QUERY),
        MethodSpec("ShareNotification/queryChanges", MethodKind.QUERY_CHANGES),
        # §3.3: destroy only. A create or update MUST be rejected with
        # `forbidden`, which is why the builder for this one is bespoke.
        MethodSpec(
            "ShareNotification/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS
        ),
    ),
)

#: No methods, no data types, no ``attr``. It exists to be *read* from
#: ``accountCapabilities`` and must never reach ``using`` - see the module
#: docstring.
PRINCIPALS_OWNER: Final = CapabilitySpec(
    urn=PRINCIPALS_OWNER_URN,
    reference="RFC 9670 §1.5.2",
    account_value=PrincipalsOwnerCapability,
)

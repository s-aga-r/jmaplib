"""Stalwart's management dialect - ``urn:stalwart:jmap``.

Stalwart's admin API is not REST - v0.16 removed ``/api/*`` - it is a JMAP
dialect, and three things about it are worth stating before the spec:

**Method names carry an ``x:`` prefix** - ``x:Account/get``, ``x:Domain/set`` -
and only three shapes exist. The server's wire parser accepts ``get``, ``set``
and ``query`` on a management object and nothing else, so there is no
``/changes`` to sync from and no ``/copy``. Everything an admin does - creating
users, minting app passwords, retrying queued mail, running a server action -
is one of those three.

**The URN is advertised at account level only.** It appears in an account's
``accountCapabilities`` and in ``primaryAccounts``, never in the session-level
map - which is the case that motivates the registry's union rule, and why a
containment check against session ``capabilities`` reads a manageable server as
unmanageable.

**The object inventory is the server's registry schema** - roughly 150 object
types in v0.16, served machine-readable at ``/api/schema`` and grown per
release. A static spec cannot honestly claim all of it, so
:data:`STALWART_MANAGEMENT` covers the objects verified against a real v0.16
deployment and :func:`management_spec` builds a spec over any other inventory;
register that in a custom registry in place of the default.

The objects themselves are not modelled: their vocabulary is the schema's, so
results come back as :class:`~jmap.models.base.JMAPObject` mappings over the
exact wire keys. The three shapes are declared uniformly per object, and what a
given object refuses is left to the server, which is the authority on its own
schema - ``x:Log`` is read-only and pages only by ``anchor``, and ``x:Action``
is *run* by a ``set`` whose created object carries the result. The exception is
a singleton like ``x:Bootstrap`` (one object, id ``"singleton"``), whose
``/query`` the server's parser rejects outright, so the spec declines to offer
it.

There are no composed façades for these - the ``x:`` names are not attribute
material - so calls go through ``batch.add`` like the other builder-less
methods, with account resolution, ``using`` derivation and the read-only and
limit gates all applying as usual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey

if TYPE_CHECKING:
    from collections.abc import Iterable

STALWART_URN: Final = "urn:stalwart:jmap"

#: The management objects this build declares, verified against Stalwart v0.16 -
#: a deliberate subset of the ~150-type registry schema. Directory objects
#: first, then operations, reports, and server configuration.
MANAGEMENT_OBJECTS: Final = (
    "Account",
    "AppPassword",
    "Domain",
    "DkimSignature",
    "Group",
    "MailingList",
    "OAuthClient",
    "Role",
    "QueuedMessage",
    "Log",
    "Action",
    "DmarcExternalReport",
    "DmarcInternalReport",
    "TlsExternalReport",
    "TlsInternalReport",
    "ArfExternalReport",
    "Bootstrap",
    "NetworkListener",
    "Jmap",
)

#: Objects with exactly one instance, id ``"singleton"``. The server's method
#: parser rejects ``/query`` on these rather than answering it.
MANAGEMENT_SINGLETONS: Final = ("Bootstrap",)


def management_spec(
    objects: Iterable[str],
    *,
    singletons: Iterable[str] = (),
) -> CapabilitySpec:
    """A ``urn:stalwart:jmap`` spec over ``objects``.

    ``objects`` are schema names without the ``x:`` prefix; the prefix is wire
    dressing and is added here. ``singletons`` marks the subset that has exactly
    one instance - they get no ``/query``, which the server would reject.

    This exists because the inventory is the server's, not the library's: a
    Stalwart release that adds an object should not need a library release to
    manage it. Build a spec naming what your server actually has and register it
    in a custom registry in place of :data:`STALWART_MANAGEMENT`.
    """
    singleton_names = frozenset(singletons)
    object_names = tuple(objects)
    unknown = singleton_names.difference(object_names)
    if unknown:
        raise ValueError(f"singletons not in objects: {', '.join(sorted(unknown))}")

    data_types: list[DataTypeSpec] = []
    methods: list[MethodSpec] = []
    for name in object_names:
        wire = f"x:{name}"
        singleton = name in singleton_names
        data_types.append(
            DataTypeSpec(name=wire, singleton_id="singleton" if singleton else None)
        )
        methods.append(MethodSpec(f"{wire}/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS))
        methods.append(
            MethodSpec(
                f"{wire}/set",
                MethodKind.SET,
                mutating=True,
                chunk_by=LimitKey.SET_OBJECTS,
            )
        )
        if not singleton:
            methods.append(MethodSpec(f"{wire}/query", MethodKind.QUERY))

    return CapabilitySpec(
        urn=STALWART_URN,
        reference="Stalwart v0.16 management API",
        data_types=tuple(data_types),
        methods=tuple(methods),
    )


STALWART_MANAGEMENT: Final = management_spec(
    MANAGEMENT_OBJECTS, singletons=MANAGEMENT_SINGLETONS
)

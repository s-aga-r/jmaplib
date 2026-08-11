"""``urn:ietf:params:jmap:core`` - RFC 8620's own methods and types.

Small, but it is the capability that proves the spec model works, because
``PushSubscription`` breaks three assumptions the standard shapes make: it is not
account-scoped, it has no state string, and two of its properties may never be
requested. If those have to be special-cased in the client rather than declared
here, the design is wrong.
"""

from __future__ import annotations

from typing import Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey

CORE_URN: Final = "urn:ietf:params:jmap:core"

#: RFC 8620 §7.2. The server never returns ``url`` or ``keys`` - asking for them
#: earns ``forbidden`` - and the object is global rather than per-account.
PUSH_SUBSCRIPTION: Final = DataTypeSpec(
    name="PushSubscription",
    never_request_properties=frozenset({"url", "keys"}),
)

#: RFC 8620 §6.3. Blobs are the one core type with a method but no object model:
#: they are addressed by id and moved with ``Blob/copy``, never fetched as JSON.
BLOB: Final = DataTypeSpec(name="Blob")

CORE: Final = CapabilitySpec(
    urn=CORE_URN,
    attr="core",
    reference="RFC 8620",
    data_types=(PUSH_SUBSCRIPTION, BLOB),
    methods=(
        MethodSpec(
            name="Core/echo",
            kind=MethodKind.CUSTOM,
            account_scoped=False,
            stateful=False,
        ),
        MethodSpec(
            name="Blob/copy",
            kind=MethodKind.COPY,
            mutating=True,
            # RFC 8620 §6.3 takes `blobIds`, not the `create` map every other
            # /copy uses, so it cannot ride the generic copy builder.
            extra_args={"blobIds": "ids of the blobs to copy from fromAccountId"},
        ),
        MethodSpec(
            name="PushSubscription/get",
            kind=MethodKind.GET,
            account_scoped=False,
            stateful=False,
        ),
        MethodSpec(
            name="PushSubscription/set",
            kind=MethodKind.SET,
            account_scoped=False,
            stateful=False,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
        ),
    ),
)

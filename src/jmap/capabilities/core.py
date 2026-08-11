"""``urn:ietf:params:jmap:core`` - RFC 8620's own methods and types.

Small, but it is the capability that proves the spec model works, because
``PushSubscription`` breaks three assumptions the standard shapes make: it is not
account-scoped, it has no state string, and two of its properties may never be
requested. If those have to be special-cased in the client rather than declared
here, the design is wrong.
"""

from __future__ import annotations

from typing import Final

from jmap.capabilities.blob import BLOB_TYPE
from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey
from jmap.models.blob import BlobCopyResponse
from jmap.models.push import PushSubscription

CORE_URN: Final = "urn:ietf:params:jmap:core"

#: RFC 8620 §7.2. The server never returns ``url`` or ``keys`` - asking for them
#: earns ``forbidden`` - and the object is global rather than per-account.
PUSH_SUBSCRIPTION: Final = DataTypeSpec(
    name="PushSubscription",
    model=PushSubscription,
    never_request_properties=frozenset({"url", "keys"}),
)

#: RFC 8620 §6.3 owns ``Blob/copy``; RFC 9404 owns everything else about blobs.
#: The type itself is described once, in :mod:`jmap.capabilities.blob`, so that
#: ``data_type("Blob")`` cannot depend on which capability resolved first.
BLOB: Final = BLOB_TYPE

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
            # Not MethodKind.COPY, despite the name. RFC 8620 §6.3 takes `blobIds`
            # rather than the `create` map every other /copy uses, and answers
            # `copied`/`notCopied` rather than `created`/`notCreated`. Riding the
            # generic shape would send arguments the server rejects and then parse
            # the reply into an object whose `created` is always empty.
            kind=MethodKind.CUSTOM,
            mutating=True,
            response_model=BlobCopyResponse,
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

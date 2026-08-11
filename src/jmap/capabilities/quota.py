"""``urn:ietf:params:jmap:quota`` - RFC 9425.

The smallest capability in the library and the best demonstration that composing
the entity façade from the spec is worth doing: a Quota is computed by the server,
so there is no ``Quota/set``, and ``client.quota.quota`` genuinely has no ``.set``
attribute rather than one that fails on every call.

The capability object is empty at both levels, so there is nothing to gate on -
which is itself worth stating, because every other capability in this library
carries fields that change behaviour.
"""

from __future__ import annotations

from typing import Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey
from jmap.models.quota import Quota, QuotaChangesResponse

QUOTA_URN: Final = "urn:ietf:params:jmap:quota"

QUOTA: Final = CapabilitySpec(
    urn=QUOTA_URN,
    attr="quota",
    reference="RFC 9425",
    data_types=(DataTypeSpec(name="Quota", model=Quota),),
    methods=(
        MethodSpec("Quota/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec(
            "Quota/changes",
            MethodKind.CHANGES,
            # The standard shape plus `updatedProperties`, which is the whole
            # point of the method: it is fed straight into a following /get.
            response_model=QuotaChangesResponse,
        ),
        MethodSpec("Quota/query", MethodKind.QUERY),
        MethodSpec("Quota/queryChanges", MethodKind.QUERY_CHANGES),
    ),
)

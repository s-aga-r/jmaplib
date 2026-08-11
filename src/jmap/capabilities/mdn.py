"""``urn:ietf:params:jmap:mdn`` - RFC 9007 message disposition notifications.

Two methods, neither of which follows a standard shape, and one rule that makes
this capability different from every other in the library: **the server polices
the client's bookkeeping**.

RFC 9007 §2.1 requires ``MDN/send`` to also mark the acknowledged message with
``$mdnsent``, and requires the *server* to check that ``onSuccessUpdateEmail``
does so and to reject the call otherwise. That is unusual - normally a server
enforces its own invariants, not the client's record-keeping - and it means a
send without the patch is malformed rather than merely incomplete. The bespoke
builder supplies it by default for exactly that reason.

``MDN/send`` also needs ``urn:ietf:params:jmap:mail`` in ``using`` alongside this
capability, because it implicitly performs an ``Email/set`` and reads an Identity.
Declaring that here means ``using`` derivation adds it without the caller knowing
the rule.
"""

from __future__ import annotations

from typing import Final

from jmap.capabilities.mail import MAIL_URN
from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.models.mdn import MDN, MDNParseResponse, MDNSendResponse

MDN_URN: Final = "urn:ietf:params:jmap:mdn"

MDN_CAPABILITY: Final = CapabilitySpec(
    urn=MDN_URN,
    attr="mdn",
    reference="RFC 9007",
    # §2.1: MDN/send implies an Email/set and reads an Identity, both of which
    # belong to the mail capability.
    requires=frozenset({MAIL_URN}),
    data_types=(DataTypeSpec(name="MDN", model=MDN),),
    methods=(
        MethodSpec(
            "MDN/send",
            MethodKind.CUSTOM,
            mutating=True,
            response_model=MDNSendResponse,
            # The server emits an extra Email/set under this call's id for the
            # $mdnsent update, exactly as EmailSubmission/set does.
            implicit_responses=1,
            also_requires=frozenset({MAIL_URN}),
            extra_args={
                "identityId": "the identity to send the receipt as",
                "send": "creation id -> MDN",
                "onSuccessUpdateEmail": "must set $mdnsent, or the server rejects the call",
            },
        ),
        MethodSpec(
            "MDN/parse",
            MethodKind.CUSTOM,
            response_model=MDNParseResponse,
            extra_args={"blobIds": "blobs to parse as MDN messages"},
        ),
    ),
)

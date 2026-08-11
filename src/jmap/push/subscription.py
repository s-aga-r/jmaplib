"""The PushSubscription lifecycle (RFC 8620 §7.2, RFC 9749 §5).

Registering a URL with a JMAP server is a three-step dance, and each step exists
because of a specific failure:

**Create.** The subscription is tied to the credentials that made the call. The
server destroys it when they expire, and only ever shows it to the same
credentials - so "list my subscriptions" is already scoped, and a subscription
from another device is not yours to touch.

**Verify.** The server immediately POSTs a ``PushVerification`` to the URL and
makes *no further requests* until the code comes back. That is what stops a push
subscription being used to aim a JMAP server at a third party. The verification
can arrive before the create response does - §7.2.3 says the client must cope -
which is why matching is by subscription id rather than by ordering.

**Renew.** ``expires`` is advisory in one direction only: the server may shorten
what you ask for but not extend it, and it may impose one when you ask for none.
Reading back what the server actually set is the only way to know when to renew.

RFC 9749 adds a fourth concern. The VAPID application server key is part of a
subscription's identity, and a rotation destroys it silently - so the key in force
when the subscription was created has to be remembered and re-checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jmap.capabilities.push import VAPID_URN, VapidCapability, vapid_key_rotated
from jmap.core.errors import JMAPError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from jmap.core.session import Session
    from jmap.models.push import PushSubscription, PushVerification

#: RFC 8620 §7.2. The server never returns these, and asking earns ``forbidden``
#: for the whole call.
UNREADABLE_PROPERTIES = frozenset({"url", "keys"})


class InsecurePushUrlError(JMAPError):
    """A push URL that is not ``https://``.

    RFC 8620 §7.2 requires it, and the payload is a notification about a user's
    mailbox - refused locally rather than sent for the server to reject.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"a push subscription URL must begin with https://, got {url!r}")


def check_push_url(url: str) -> None:
    """Reject a push URL the server is required to refuse."""
    if not url.startswith("https://"):
        raise InsecurePushUrlError(url)


def new_subscription(
    device_client_id: str,
    url: str,
    *,
    types: list[str] | None = None,
    keys: Mapping[str, str] | None = None,
    expires: str | None = None,
) -> dict[str, Any]:
    """Build the ``create`` object for ``PushSubscription/set``.

    ``verificationCode`` is deliberately absent: §7.2 says it MUST be null or
    omitted on create, and the server rejects a guess.
    """
    check_push_url(url)
    creation: dict[str, Any] = {"deviceClientId": device_client_id, "url": url}
    # Each is omitted rather than sent as null. `types: null` does mean "every
    # type", but so does leaving it out, and the shorter request is the one the
    # RFC's own example sends.
    if types is not None:
        creation["types"] = types
    if keys is not None:
        creation["keys"] = dict(keys)
    if expires is not None:
        creation["expires"] = expires
    return creation


def mine(
    subscriptions: Iterable[PushSubscription], device_client_id: str
) -> list[PushSubscription]:
    """The subscriptions this device created.

    §7.2.2 tells clients not to update or destroy a subscription whose
    ``deviceClientId`` they do not recognise: the same credentials may be in use on
    another device, and its subscription is not yours to revoke.
    """
    return [
        subscription
        for subscription in subscriptions
        if subscription.device_client_id == device_client_id
    ]


@dataclass(slots=True)
class PendingVerification:
    """Holds verification codes until the subscription they belong to is known.

    The race is in RFC 8620 §7.2.3: the server pushes the ``PushVerification``
    the moment it creates the subscription, which can be before the
    ``PushSubscription/set`` response reaches the client. A client that only looks
    for a code *after* the create returns can therefore miss it entirely and wait
    forever for a second one that never comes.

    So codes are recorded as they arrive and claimed later, in whichever order the
    two actually happen.
    """

    _codes: dict[str, str] = field(default_factory=lambda: {})

    def record(self, verification: PushVerification) -> None:
        """Note a verification push, whether or not its subscription is known yet."""
        identifier = verification.push_subscription_id
        code = verification.verification_code
        if identifier is not None and code is not None:
            self._codes[identifier] = code

    def claim(self, subscription_id: str) -> str | None:
        """Take the code for a subscription, if one has arrived."""
        return self._codes.pop(subscription_id, None)

    def __contains__(self, subscription_id: object) -> bool:
        return subscription_id in self._codes

    def __len__(self) -> int:
        return len(self._codes)


def verification_update(verification_code: str) -> dict[str, Any]:
    """The ``update`` patch that completes verification."""
    return {"verificationCode": verification_code}


def renewal_update(expires: str) -> dict[str, Any]:
    """The ``update`` patch that extends a subscription's lifetime.

    The server may shorten what is asked for, so the ``expires`` in the response
    - not the one sent - is what the next renewal should be scheduled from.
    """
    return {"expires": expires}


def application_server_key(session: Session) -> str | None:
    """The VAPID key currently advertised, or ``None`` if the server has none."""
    return VapidCapability.of(session.capability_value(VAPID_URN)).application_server_key


def needs_recreating(session: Session, subscribed_with: str | None) -> bool:
    """Whether a rotation means the subscription must be built again (RFC 9749 §5).

    Nothing raises when this happens: the server destroys the old subscription and
    notifications simply stop, so a client that never asks never finds out.
    """
    return vapid_key_rotated(
        VapidCapability.of(session.capability_value(VAPID_URN)), subscribed_with
    )

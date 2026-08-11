"""The two push transport capabilities: WebSocket (RFC 8887) and VAPID (RFC 9749).

Neither adds a method or a data type. They exist to *describe* how push works on
this server, which makes them the clearest case for capability objects being data:
everything either one contributes is a field, read before a connection is made.

VAPID's field is the one with teeth. RFC 9749 §5 makes the application server key
part of a subscription's identity: when the server rotates it, existing
subscriptions are destroyed, and a client that does not notice keeps a
subscription the server has already thrown away and simply stops receiving
anything. Detecting the rotation is the client's job, and there is no notification
for it beyond ``sessionState`` changing.
"""

from __future__ import annotations

from typing import Any, Final

from jmap.capabilities.spec import CapabilitySpec
from jmap.models.base import JMAPModel

WEBSOCKET_URN: Final = "urn:ietf:params:jmap:websocket"
VAPID_URN: Final = "urn:ietf:params:jmap:webpush-vapid"


class WebSocketCapability(JMAPModel):
    """``urn:ietf:params:jmap:websocket`` (RFC 8887 §3)."""

    #: The ``wss://`` endpoint for the handshake. RFC 8887 §4.2 requires TLS, so a
    #: ``ws://`` value here is a server that cannot be used safely.
    url: str | None = None
    #: Whether push notifications may be delivered over the socket at all. False
    #: means the socket is a request/response transport only, and notifications
    #: still need an event source or a PushSubscription.
    supports_push: bool = False

    @classmethod
    def of(cls, value: Any) -> WebSocketCapability:
        """Parse an advertised capability object, tolerating a malformed one."""
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()

    @property
    def is_secure(self) -> bool:
        """Whether the advertised endpoint uses TLS, as §4.2 requires."""
        return (self.url or "").startswith("wss://")


class VapidCapability(JMAPModel):
    """``urn:ietf:params:jmap:webpush-vapid`` (RFC 9749 §3)."""

    #: The ECDSA P-256 public key, uncompressed and base64url-encoded. Passed to
    #: the push service verbatim when creating a subscription - the encoding was
    #: chosen to match the browser Push API so no transformation is needed.
    application_server_key: str | None = None

    @classmethod
    def of(cls, value: Any) -> VapidCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def vapid_key_rotated(advertised: VapidCapability, subscribed_with: str | None) -> bool:
    """Whether the server's key has moved since a subscription was created.

    RFC 9749 §5 requires the client to watch for this and recreate the
    subscription, because the server destroys subscriptions tied to the old key
    once any transitional period ends. There is no error to catch: notifications
    just stop.

    An unknown key on either side is not a rotation - it is an absence, and
    recreating a subscription on that basis would loop.
    """
    if subscribed_with is None or advertised.application_server_key is None:
        return False
    return advertised.application_server_key != subscribed_with


WEBSOCKET: Final = CapabilitySpec(
    urn=WEBSOCKET_URN,
    reference="RFC 8887",
    session_value=WebSocketCapability,
)

VAPID: Final = CapabilitySpec(
    urn=VAPID_URN,
    reference="RFC 9749",
    session_value=VapidCapability,
)

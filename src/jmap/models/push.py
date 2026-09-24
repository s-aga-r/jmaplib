"""Push objects (RFC 8620 §7, RFC 8887 §4.3, RFC 9749).

Push in JMAP deliberately carries almost no data. A ``StateChange`` says *which
types in which accounts have moved*, and nothing about what changed - the client
follows up with ``/changes`` if it cares. That is what makes dropped
notifications harmless: the next call discovers the same thing.

Two details here bite.

**``@type`` is the routing tag, and it is not a Python identifier.** Every pushed
object carries one, and over a WebSocket it is the *only* thing distinguishing a
``Response`` from a ``StateChange`` from a ``RequestError`` on the same socket. It
is therefore declared with an explicit alias and re-added by :meth:`Tagged.to_wire`
- because ``exclude_unset`` would otherwise drop a tag the caller never assigned,
producing a message the peer cannot route.

**A pushed state is not a delta.** Comparing it against what you hold is the whole
protocol, so :meth:`StateChange.outdated` is the method that matters; treating the
arrival of a ``StateChange`` as "something changed for me" re-fetches types that
did not move.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from jmap.models.base import JMAPModel

#: RFC 8620 §7.1 and §7.2.2, RFC 8887 §4.3.
TYPE_STATE_CHANGE = "StateChange"
TYPE_PUSH_VERIFICATION = "PushVerification"
TYPE_REQUEST = "Request"
TYPE_RESPONSE = "Response"
TYPE_REQUEST_ERROR = "RequestError"
TYPE_PUSH_ENABLE = "WebSocketPushEnable"
TYPE_PUSH_DISABLE = "WebSocketPushDisable"


class Tagged(JMAPModel):
    """A pushed object carrying an ``@type`` discriminator.

    The tag is re-added on serialisation rather than left to ``exclude_unset``:
    a caller who never assigned it - which is everyone, since it has exactly one
    correct value - would otherwise send a message with no tag at all, and over a
    WebSocket the tag is the only thing that says what the message is.
    """

    #: Overridden per subclass; also the default for the ``@type`` field.
    TAG: ClassVar[str] = ""

    type_: str = Field(default="", alias="@type")

    def __init__(self, **data: Any) -> None:
        data.setdefault("@type", type(self).TAG)
        super().__init__(**data)

    def to_wire(self) -> dict[str, Any]:
        wire = super().to_wire()
        wire["@type"] = self.type_ or type(self).TAG
        return wire


class StateChange(Tagged):
    """What moved, and where (RFC 8620 §7.1).

    ``changed`` maps an account id to a *TypeState*: type name -> the ``state``
    string ``Foo/get`` would return right now. Several changes are coalesced into
    one of these, and a dropped notification costs nothing - the next one carries
    the same state.
    """

    TAG: ClassVar[str] = TYPE_STATE_CHANGE

    #: accountId -> {typeName: state}.
    changed: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: RFC 8887 §4.3.5.1. Encodes the *entire* server state visible to the user,
    #: so a reconnecting client can ask for everything it missed in one go rather
    #: than issuing a ``/changes`` per type. Absent unless the server supports it.
    push_state: str | None = None

    def states_for(self, account_id: str) -> dict[str, str]:
        """The TypeState for one account, empty if that account did not change."""
        return self.changed.get(account_id, {})

    def accounts(self) -> list[str]:
        return list(self.changed)

    def types(self) -> set[str]:
        """Every type name mentioned, across all accounts."""
        return {name for states in self.changed.values() for name in states}

    def matches(self, account_id: str, type_name: str, state: str) -> bool:
        """Whether the pushed state equals one we already hold.

        RFC 8620 §7.1 calls this out: a notification may arrive *while* your own
        ``/set`` is in flight. If the state it announces is the one your ``/set``
        just produced, the change being announced is your own and asking for it
        again is a wasted round trip.
        """
        return self.states_for(account_id).get(type_name) == state

    def outdated(self, known: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        """The subset of this notification the caller does not already have.

        ``known`` is the caller's cursors in the same shape. A type is outdated
        when the pushed state differs from the held one, *including* when nothing
        is held for it - never having synced a type is the strongest reason to.

        Empty means there is nothing to do, which is the common case for a client
        that just made the change itself.
        """
        stale: dict[str, dict[str, str]] = {}
        for account_id, states in self.changed.items():
            held = known.get(account_id, {})
            moved = {name: state for name, state in states.items() if held.get(name) != state}
            if moved:
                stale[account_id] = moved
        return stale


class PushKeys(JMAPModel):
    """Client-generated encryption keys (RFC 8620 §7.2, RFC 8291).

    Supplying these makes the server encrypt every push payload to them. Both are
    URL-safe base64 (RFC 4648 §5), not standard base64 - a distinction that only
    shows up as a decryption failure on the client.
    """

    #: The P-256 ECDH public key. Its alias is spelled out: the camelCase
    #: generator capitalises a letter after a digit, and sent it as ``p256Dh``.
    p256dh: str | None = Field(default=None, alias="p256dh")
    #: The authentication secret. Excluded from repr: it authenticates the
    #: RFC 8291 key derivation, and model reprs end up in logs.
    auth: str | None = Field(default=None, repr=False)


class PushSubscription(JMAPModel):
    """A registered push endpoint (RFC 8620 §7.2).

    Tied to the credentials that created it: the server destroys it when they
    expire or are revoked, and only ever returns subscriptions created with the
    credentials making the request. It is not account-scoped.

    ``url`` and ``keys`` are never returned by ``PushSubscription/get`` - they may
    hold device-private data - so a fetched subscription always has them unset.
    Requesting them explicitly earns ``forbidden`` for the whole call, which the
    client refuses locally.
    """

    id: str | None = None
    #: Identifies client + device, so a client that lost its local state can still
    #: recognise its own subscriptions. Must not contain an unobfuscated device id.
    device_client_id: str | None = None
    #: Immutable, and must be ``https://``. Changing it means destroy and recreate.
    url: str | None = None
    #: Immutable.
    keys: PushKeys | None = None
    #: Null on create. The server pushes the real one to ``url``; until it is set
    #: back here the server makes no further requests to that URL.
    verification_code: str | None = None
    #: The server may shorten this, and may set one if the client gives none.
    expires: str | None = None
    #: Types to notify about; null means all of them.
    types: list[str] | None = None


class PushVerification(Tagged):
    """The proof-of-ownership push sent when a subscription is created (§7.2.2).

    Sent to the subscription's URL immediately, before any notification. It exists
    so that registering a URL cannot be used to point a JMAP server at a
    third party as a denial-of-service amplifier.

    It can arrive *before* the ``PushSubscription/set`` response that created the
    subscription does - §7.2.3 says the client must cope - which is why it carries
    the subscription id rather than relying on the client already knowing it.
    """

    TAG: ClassVar[str] = TYPE_PUSH_VERIFICATION

    push_subscription_id: str | None = None
    verification_code: str | None = None


class WebSocketPushEnable(Tagged):
    """Turn on push for this WebSocket connection (RFC 8887 §4.3.5.2)."""

    TAG: ClassVar[str] = TYPE_PUSH_ENABLE

    #: Null means every supported type.
    data_types: list[str] | None = None
    #: The last ``pushState`` seen. The server should then immediately send
    #: everything that changed since - which is what makes a reconnect cheap.
    push_state: str | None = None


class WebSocketPushDisable(Tagged):
    """Turn push back off for this connection (RFC 8887 §4.3.5.3)."""

    TAG: ClassVar[str] = TYPE_PUSH_DISABLE

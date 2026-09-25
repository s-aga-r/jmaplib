"""Reading what the server pushes to your own endpoint (RFC 8620 §7.2, RFC 8291).

A PushSubscription created with ``keys`` has the server encrypt everything it
sends to the URL, the verification included: the ``aes128gcm`` content coding of
RFC 8188, keyed by an ECDH agreement with the subscription's public key and an
authentication secret only the subscriber holds. The push service carrying the
POST, and anything else that sees it, learns the length and nothing more.

:class:`PushKeyPair` is both halves. Generate one, subscribe with its
:attr:`~PushKeyPair.keys`, keep its two secrets wherever the endpoint runs, and
hand each POST's body to :func:`read_push`. Encryption needs ``jmaplib[push]``;
reading an unencrypted push does not.

Two rules of RFC 8291 §4 are enforced rather than trusted: a push is one record,
and its padding delimiter must be ``0x02`` - anything else is discarded, as the
RFC requires, by raising :class:`PushPayloadError`.
"""

from __future__ import annotations

import base64
import secrets
from typing import TYPE_CHECKING, Final, Self

from pydantic import ValidationError

from jmap.core.errors import JMAPError
from jmap.core.ijson import loads
from jmap.core.narrow import as_object, is_object
from jmap.models.base import validation_summary
from jmap.models.push import (
    TYPE_PUSH_VERIFICATION,
    TYPE_STATE_CHANGE,
    PushKeys,
    PushVerification,
    StateChange,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

#: RFC 8291 §3.2: the authentication secret is 16 octets.
AUTH_SECRET_BYTES: Final = 16

#: A P-256 private key's scalar, and its public key as an uncompressed point
#: (X9.62: ``0x04``, then both coordinates) - also the only ``keyid`` RFC 8291 §4
#: allows.
PRIVATE_KEY_BYTES: Final = 32
PUBLIC_KEY_BYTES: Final = 65

#: RFC 8188 §2.1: salt (16), record size (4), keyid length (1), then the keyid.
_SALT_BYTES: Final = 16
_HEADER_BYTES: Final = _SALT_BYTES + 4 + 1

#: RFC 8188 §2.1: a record size below 18 cannot hold the 16-octet tag, a delimiter
#: and any content.
_MIN_RECORD_SIZE: Final = 18

#: RFC 8291 §4 and RFC 8188 §2: the delimiter ending the only, and so last, record.
_LAST_RECORD: Final = 0x02

_KEY_INFO: Final = b"WebPush: info\x00"
_CEK_INFO: Final = b"Content-Encoding: aes128gcm\x00"
_NONCE_INFO: Final = b"Content-Encoding: nonce\x00"


class PushPayloadError(JMAPError):
    """A push body that cannot be read: it does not decrypt, or is no push object.

    Either way RFC 8291 §4 and RFC 8620 §7 say the same thing - discard it. A
    body that fails to decrypt may simply be meant for keys since replaced.
    """


class PushKeyPair:
    """The keys a push subscription is encrypted to, private half included.

    ``private_key`` and ``auth`` are the secrets: URL-safe base64, as
    :meth:`generate` produces them and as they should be stored. The server is
    only ever given :attr:`keys`.
    """

    __slots__ = ("_auth", "_private", "_public")

    def __init__(self, private_key: str, auth: str) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        scalar = _decode(private_key, "private_key", PRIVATE_KEY_BYTES)
        self._auth = _decode(auth, "auth", AUTH_SECRET_BYTES)
        try:
            self._private: EllipticCurvePrivateKey = ec.derive_private_key(
                int.from_bytes(scalar, "big"), ec.SECP256R1()
            )
        except ValueError as exc:
            raise ValueError("private_key is not a P-256 private key") from exc
        self._public = self._private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )

    @classmethod
    def generate(cls) -> Self:
        """A fresh key pair and authentication secret (RFC 8291 §3.1, §3.2)."""
        from cryptography.hazmat.primitives.asymmetric import ec

        private = ec.generate_private_key(ec.SECP256R1())
        scalar = private.private_numbers().private_value.to_bytes(PRIVATE_KEY_BYTES, "big")
        return cls(_encode(scalar), _encode(secrets.token_bytes(AUTH_SECRET_BYTES)))

    @property
    def private_key(self) -> str:
        """The private key's scalar. Secret: store it, never send it."""
        return _encode(
            self._private.private_numbers().private_value.to_bytes(PRIVATE_KEY_BYTES, "big")
        )

    @property
    def auth(self) -> str:
        """The authentication secret. Secret too, though the server is given it."""
        return _encode(self._auth)

    @property
    def keys(self) -> PushKeys:
        """What ``PushSubscription/set`` is given, via ``new_subscription(keys=...)``."""
        return PushKeys(p256dh=_encode(self._public), auth=_encode(self._auth))

    def decrypt(self, body: bytes) -> bytes:
        """The plaintext of one ``aes128gcm`` push body (RFC 8291 §3.4)."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt, sender, record = _split(body)
        try:
            sender_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender)
        except ValueError as exc:
            raise PushPayloadError("the sender's key (the keyid) is not a P-256 point") from exc
        shared = self._private.exchange(ec.ECDH(), sender_key)
        secret = _hkdf(self._auth, shared, _KEY_INFO + self._public + sender, 32)
        key = _hkdf(salt, secret, _CEK_INFO, 16)
        nonce = _hkdf(salt, secret, _NONCE_INFO, 12)
        try:
            padded = AESGCM(key).decrypt(nonce, record, None)
        except InvalidTag as exc:
            raise PushPayloadError("the push does not decrypt with these keys") from exc
        content = padded.rstrip(b"\x00")
        if not content or content[-1] != _LAST_RECORD:
            raise PushPayloadError("the padding delimiter is not 0x02 (RFC 8291 §4)")
        return content[:-1]

    def __repr__(self) -> str:
        return f"PushKeyPair(p256dh={_encode(self._public)!r})"


def read_push(body: bytes, keys: PushKeyPair | None = None) -> StateChange | PushVerification:
    """What one POST to your push endpoint says.

    Pass ``keys`` when the subscription was created with them, and the body is
    decrypted first; without them it is the JSON object itself. Either way the
    answer is a ``StateChange``, or the ``PushVerification`` whose code proves
    you own the URL - see :class:`~jmap.push.PendingVerification`.
    """
    if keys is not None:
        body = keys.decrypt(body)
    try:
        decoded = loads(body)
    except ValueError as exc:
        raise PushPayloadError(f"a push must be I-JSON: {exc}") from exc
    if not is_object(decoded):
        raise PushPayloadError("a push must be a JSON object")
    pushed = as_object(decoded)
    tag = pushed.get("@type")
    try:
        if tag == TYPE_STATE_CHANGE:
            return StateChange.model_validate(pushed)
        if tag == TYPE_PUSH_VERIFICATION:
            return PushVerification.model_validate(pushed)
    except ValidationError as exc:
        raise PushPayloadError(f"a malformed {tag}: {validation_summary(exc)}") from exc
    raise PushPayloadError(
        f"unexpected @type {tag!r}; a push is a {TYPE_STATE_CHANGE} or a {TYPE_PUSH_VERIFICATION}"
    )


def _split(body: bytes) -> tuple[bytes, bytes, bytes]:
    """The salt, the sender's public key and the one record of an ``aes128gcm`` body."""
    if len(body) < _HEADER_BYTES:
        raise PushPayloadError("too short for an aes128gcm header")
    record_size = int.from_bytes(body[_SALT_BYTES : _HEADER_BYTES - 1], "big")
    key_length = body[_HEADER_BYTES - 1]
    if key_length != PUBLIC_KEY_BYTES:
        raise PushPayloadError(
            f"the keyid is {key_length} octets; RFC 8291 §4 makes it the sender's "
            f"{PUBLIC_KEY_BYTES}-octet public key"
        )
    record = body[_HEADER_BYTES + PUBLIC_KEY_BYTES :]
    if record_size < _MIN_RECORD_SIZE:
        raise PushPayloadError(f"a record size of {record_size} is below RFC 8188's 18")
    if len(record) > record_size:
        raise PushPayloadError("a push is one record (RFC 8291 §4), and this holds more")
    return body[:_SALT_BYTES], body[_HEADER_BYTES : _HEADER_BYTES + PUBLIC_KEY_BYTES], record


def _hkdf(salt: bytes, secret: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def _encode(raw: bytes) -> str:
    """URL-safe base64 without padding, as RFC 8291's own examples write keys."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(text: str, name: str, length: int) -> bytes:
    """URL-safe base64, padded or not, of exactly ``length`` octets.

    Validated, because the lenient decoder drops any character outside the
    alphabet and a mangled key would decode to something - just not the key.
    """
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError(f"{name} is not URL-safe base64: {exc}") from exc
    if len(raw) != length:
        raise ValueError(f"{name} must be {length} octets, not {len(raw)}")
    return raw

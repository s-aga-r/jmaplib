"""Encrypted pushes (RFC 8291): the keys, the decryption, and reading the push.

The RFC's own example pins the decryption. Everything else is encrypted here by
a sender written from RFC 8291 §3.4, so each rule can be broken on purpose.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from jmap.models.push import PushKeys, PushVerification, StateChange
from jmap.push import (
    PendingVerification,
    PushKeyPair,
    PushPayloadError,
    new_subscription,
    read_push,
)


def unbase64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


#: RFC 8291 §5, the receiver's side.
RFC_PRIVATE_KEY = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_PUBLIC_KEY = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_BODY = unbase64(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)

CHANGE = {"@type": "StateChange", "changed": {"a1": {"Email": "s9"}}}


def hkdf(salt: bytes, secret: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def encrypt(
    plaintext: bytes,
    keys: PushKeys,
    *,
    delimiter: bytes = b"\x02",
    padding: int = 0,
    record_size: int = 4096,
    key_id: bytes | None = None,
) -> bytes:
    """An application server's side of RFC 8291 §3.4, with every rule a parameter."""
    assert keys.p256dh is not None
    assert keys.auth is not None
    receiver, auth = unbase64(keys.p256dh), unbase64(keys.auth)
    sender = ec.generate_private_key(ec.SECP256R1())
    sender_public = sender.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = sender.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver)
    )
    secret = hkdf(auth, shared, b"WebPush: info\x00" + receiver + sender_public, 32)
    salt = b"\x07" * 16
    key = hkdf(salt, secret, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, secret, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(key).encrypt(nonce, plaintext + delimiter + b"\x00" * padding, None)
    key_id = sender_public if key_id is None else key_id
    header = salt + record_size.to_bytes(4, "big") + bytes([len(key_id)]) + key_id
    return header + record


@pytest.fixture
def pair() -> PushKeyPair:
    return PushKeyPair.generate()


def pushed(pair: PushKeyPair, value: Any, **options: Any) -> bytes:
    return encrypt(json.dumps(value).encode(), pair.keys, **options)


class TestTheRfcExample:
    def test_the_receivers_public_key_is_derived_from_its_private_key(self):
        assert PushKeyPair(RFC_PRIVATE_KEY, RFC_AUTH).keys == PushKeys(
            p256dh=RFC_PUBLIC_KEY, auth=RFC_AUTH
        )

    def test_the_message_decrypts(self):
        plaintext = PushKeyPair(RFC_PRIVATE_KEY, RFC_AUTH).decrypt(RFC_BODY)
        assert plaintext == b"When I grow up, I want to be a watermelon"


class TestKeys:
    def test_generated_keys_are_a_p256_point_and_16_octets(self, pair):
        assert len(unbase64(str(pair.keys.p256dh))) == 65
        assert unbase64(str(pair.keys.p256dh))[0] == 0x04
        assert len(unbase64(str(pair.keys.auth))) == 16

    def test_the_secrets_rebuild_the_same_pair(self, pair):
        # How a push endpoint in another process gets its keys back.
        again = PushKeyPair(pair.private_key, pair.auth)
        assert again.keys == pair.keys
        assert again.private_key == pair.private_key

    def test_padded_base64_is_accepted_too(self):
        padded = PushKeyPair(RFC_PRIVATE_KEY + "=", RFC_AUTH + "==")
        assert padded.keys.p256dh == RFC_PUBLIC_KEY

    def test_the_keys_are_what_a_subscription_is_given(self, pair):
        creation = new_subscription("device", "https://push.example.com/h", keys=pair.keys)
        assert creation["keys"] == {"p256dh": pair.keys.p256dh, "auth": pair.auth}

    def test_the_repr_shows_no_secret(self, pair):
        text = repr(pair)
        assert pair.private_key not in text
        assert pair.auth not in text
        assert str(pair.keys.p256dh) in text

    @pytest.mark.parametrize(
        ("private_key", "auth", "message"),
        [
            ("!!!", RFC_AUTH, "private_key is not URL-safe base64"),
            (RFC_PRIVATE_KEY[:20], RFC_AUTH, "private_key must be 32 octets"),
            (RFC_PRIVATE_KEY, RFC_AUTH + "AAAA", "auth must be 16 octets"),
            # Zero is no private key on any curve.
            ("A" * 43, RFC_AUTH, "not a P-256 private key"),
        ],
    )
    def test_malformed_secrets_are_refused(self, private_key, auth, message):
        with pytest.raises(ValueError, match=message):
            PushKeyPair(private_key, auth)


class TestDecrypting:
    def test_a_state_change_reads_back(self, pair):
        change = read_push(pushed(pair, CHANGE), pair)
        assert isinstance(change, StateChange)
        assert change.changed == {"a1": {"Email": "s9"}}

    def test_a_verification_reads_back_and_can_be_recorded(self, pair):
        body = pushed(
            pair,
            {"@type": "PushVerification", "pushSubscriptionId": "P1", "verificationCode": "c0"},
        )
        verification = read_push(body, pair)
        assert isinstance(verification, PushVerification)
        pending = PendingVerification()
        pending.record(verification)
        assert pending.claim("P1") == "c0"

    def test_padding_after_the_delimiter_is_stripped(self, pair):
        assert isinstance(read_push(pushed(pair, CHANGE, padding=40), pair), StateChange)

    def test_keys_since_replaced_do_not_decrypt(self, pair):
        with pytest.raises(PushPayloadError, match="does not decrypt"):
            read_push(pushed(pair, CHANGE), PushKeyPair.generate())

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            # RFC 8291 §4: any delimiter but 0x02 MUST discard the message.
            ({"delimiter": b"\x01"}, "delimiter is not 0x02"),
            ({"delimiter": b""}, "delimiter is not 0x02"),
            ({"record_size": 17}, "below RFC 8188's 18"),
            ({"record_size": 18}, "one record"),
            ({"key_id": b"k1"}, "keyid is 2 octets"),
            ({"key_id": b"\x04" + b"\x01" * 64}, "not a P-256 point"),
        ],
    )
    def test_a_body_breaking_the_rules_is_discarded(self, pair, options, message):
        with pytest.raises(PushPayloadError, match=message):
            read_push(pushed(pair, CHANGE, **options), pair)

    def test_a_body_too_short_for_a_header(self, pair):
        with pytest.raises(PushPayloadError, match="too short"):
            pair.decrypt(b"\x00" * 20)


class TestReading:
    def test_an_unencrypted_push_is_the_json_itself(self):
        assert isinstance(read_push(json.dumps(CHANGE).encode()), StateChange)

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"{not json", "must be I-JSON"),
            (b"[]", "must be a JSON object"),
            (b'{"@type": "Response"}', "unexpected @type 'Response'"),
            (b'{"changed": {}}', "unexpected @type None"),
            (b'{"@type": "StateChange", "changed": 5}', "a malformed StateChange"),
        ],
    )
    def test_anything_but_a_push_object_is_refused(self, body, message):
        with pytest.raises(PushPayloadError, match=message):
            read_push(body)


def test_the_push_package_imports_without_its_extras():
    # Both extras are imported where they are used, so a client that never
    # decrypts or opens a socket installs neither.
    blocked = "import sys; sys.modules['cryptography'] = sys.modules['httpx_ws'] = None; "
    subprocess.run([sys.executable, "-c", blocked + "import jmap.push"], check=True)

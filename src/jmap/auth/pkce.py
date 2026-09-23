"""Proof Key for Code Exchange (RFC 7636).

The problem PKCE solves is specific: an authorization code travels back through
the user's browser and, on a native or desktop client, through a redirect any
local process could intercept. Without PKCE that code is a bearer credential -
whoever gets it first redeems it. With it, the code is only redeemable by whoever
knew the verifier, which never left this process.

Two decisions here are deliberately not configurable.

**Only S256.** RFC 7636 also defines ``plain``, where the challenge *is* the
verifier. That defeats the point for exactly the clients that need it: the
verifier ends up in the same URL as the challenge, so anything that could
intercept the code could already have read it. RFC 9700 (BCP 240) says clients
MUST use S256 where the server supports it, and this library declines to run the
flow against a server that does not.

**Verifiers come from :mod:`secrets`.** A verifier is the only thing standing
between an intercepted code and a token, so it needs cryptographic randomness -
not ``random``, which is seeded predictably and reproducible from a few outputs.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Final

from jmap.core.errors import JMAPError

#: RFC 7636 §4.1 bounds the verifier at 43-128 characters of the unreserved set.
#: 32 random bytes base64url-encode to exactly 43, the minimum, which is also
#: where the entropy stops mattering: 256 bits.
_VERIFIER_BYTES: Final = 32

MIN_VERIFIER_LENGTH: Final = 43
MAX_VERIFIER_LENGTH: Final = 128

#: RFC 7636 §4.2. The only method used here; see the module docstring.
METHOD_S256: Final = "S256"


class InvalidVerifierError(JMAPError, ValueError):
    """A code verifier outside RFC 7636 §4.1's bounds."""

    def __init__(self, length: int) -> None:
        self.length = length
        super().__init__(
            f"a PKCE code verifier must be {MIN_VERIFIER_LENGTH}-{MAX_VERIFIER_LENGTH} "
            f"characters (RFC 7636 §4.1), got {length}"
        )


def _b64url(raw: bytes) -> str:
    """Base64url without padding, which is what RFC 7636 §A specifies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_verifier() -> str:
    """A fresh code verifier: 43 characters, 256 bits of entropy."""
    return _b64url(secrets.token_bytes(_VERIFIER_BYTES))


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier (RFC 7636 §4.2).

    ``BASE64URL(SHA256(ASCII(verifier)))``. The verifier is hashed as ASCII
    because §4.1 restricts it to unreserved characters, so any other encoding
    would be a bug rather than a choice.
    """
    if not MIN_VERIFIER_LENGTH <= len(verifier) <= MAX_VERIFIER_LENGTH:
        raise InvalidVerifierError(len(verifier))
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True, slots=True)
class PKCEPair:
    """A verifier and the challenge derived from it.

    Kept together because they are only meaningful as a pair, and because losing
    the verifier between the authorization request and the token request means
    the code cannot be redeemed at all.
    """

    verifier: str
    challenge: str
    method: str = METHOD_S256

    @classmethod
    def generate(cls) -> PKCEPair:
        verifier = new_verifier()
        return cls(verifier=verifier, challenge=challenge_for(verifier))

    def __repr__(self) -> str:
        # The verifier is a credential; these objects reach logs and tracebacks.
        return f"PKCEPair(method={self.method!r}, challenge={self.challenge!r})"


def new_state() -> str:
    """A fresh ``state`` value for an authorization request.

    Not part of PKCE, and not a substitute for it: ``state`` binds the redirect to
    the request this client started (CSRF), while PKCE binds the *code* to this
    client. A flow needs both, and they fail differently - a wrong ``state`` means
    someone else's redirect arrived, a wrong verifier means someone else stole the
    code.
    """
    return secrets.token_urlsafe(_VERIFIER_BYTES)

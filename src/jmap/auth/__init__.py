"""Credential presentation and challenge handling.

RFC 8620 defines no authentication scheme, so what a JMAP client can do is
present a credential and read the ``WWW-Authenticate`` challenge that comes back.
Acquisition - the interactive OAuth flows - lands separately, behind an extra.
"""

from __future__ import annotations

from jmap.auth.challenge import Challenge, find_challenge, parse_challenges
from jmap.auth.credentials import (
    AppPasswordAuth,
    BasicAuth,
    BearerAuth,
    CallableAuth,
    InsufficientScopeError,
    JMAPAuth,
    OAuth2Auth,
    OAuth2Token,
    TokenStore,
)

__all__ = [
    "AppPasswordAuth",
    "BasicAuth",
    "BearerAuth",
    "CallableAuth",
    "Challenge",
    "InsufficientScopeError",
    "JMAPAuth",
    "OAuth2Auth",
    "OAuth2Token",
    "TokenStore",
    "find_challenge",
    "parse_challenges",
]

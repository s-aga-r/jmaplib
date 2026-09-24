"""Credential presentation, challenge handling, and OAuth acquisition.

RFC 8620 defines no authentication scheme, so a JMAP client does two separable
things: it *presents* a credential and reads the ``WWW-Authenticate`` challenge
that comes back, and - where that challenge points at OAuth - it *acquires* one.

Presentation is always available. Acquisition needs no extra dependency either,
being stdlib plus the httpx the library already uses, but it is kept in its own
modules because the flows are long-lived and interactive in a way nothing else
here is.
"""

from __future__ import annotations

from jmap.auth.acquire import (
    DEFAULT_REDIRECT_TIMEOUT,
    LOOPBACK_HOST,
    LoopbackReceiver,
    OAuthClient,
    RedirectTimeoutError,
    loopback_receiver,
)
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
from jmap.auth.flows import (
    AuthorizationRequest,
    DeviceAuthorization,
    OAuthError,
    PollOutcome,
    PollResult,
    RegisteredClient,
    StateMismatchError,
    classify_poll,
    code_exchange_body,
    device_authorization_body,
    device_token_body,
    parse_redirect,
    parse_token_response,
    refresh_body,
    registration_body,
)
from jmap.auth.metadata import (
    AuthorizationServerMetadata,
    DiscoveryError,
    IssuerMismatchError,
    ProtectedResourceMetadata,
    ResourceMismatchError,
    openid_url,
    protected_resource_url,
    well_known_url,
)
from jmap.auth.pkce import (
    METHOD_S256,
    InvalidVerifierError,
    PKCEPair,
    challenge_for,
    new_state,
    new_verifier,
)

__all__ = [
    "DEFAULT_REDIRECT_TIMEOUT",
    "LOOPBACK_HOST",
    "METHOD_S256",
    "AppPasswordAuth",
    "AuthorizationRequest",
    "AuthorizationServerMetadata",
    "BasicAuth",
    "BearerAuth",
    "CallableAuth",
    "Challenge",
    "DeviceAuthorization",
    "DiscoveryError",
    "InsufficientScopeError",
    "InvalidVerifierError",
    "IssuerMismatchError",
    "JMAPAuth",
    "LoopbackReceiver",
    "OAuth2Auth",
    "OAuth2Token",
    "OAuthClient",
    "OAuthError",
    "PKCEPair",
    "PollOutcome",
    "PollResult",
    "ProtectedResourceMetadata",
    "RedirectTimeoutError",
    "RegisteredClient",
    "ResourceMismatchError",
    "StateMismatchError",
    "TokenStore",
    "challenge_for",
    "classify_poll",
    "code_exchange_body",
    "device_authorization_body",
    "device_token_body",
    "find_challenge",
    "loopback_receiver",
    "new_state",
    "new_verifier",
    "openid_url",
    "parse_challenges",
    "parse_redirect",
    "parse_token_response",
    "protected_resource_url",
    "refresh_body",
    "registration_body",
    "well_known_url",
]

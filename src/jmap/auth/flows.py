"""Building and reading OAuth flow messages (RFC 6749, RFC 7636, RFC 8628, RFC 8252).

I/O-free: everything here turns arguments into a URL or a form body, or turns a
response into a value. The sockets live in :mod:`jmap.auth.acquire`, which keeps
the parts that are easy to get wrong testable without a browser or a listener.

The authorization-code half carries two independent guards, and conflating them is
the usual mistake because both look like "a random string in the request":

* ``state`` binds the *redirect* to the request this client started. A redirect
  arriving with the wrong one is someone else's - possibly an attacker's, trying
  to have you redeem a code they control (CSRF / code injection).
* the PKCE verifier binds the *code* to this client. A code intercepted on its way
  back is useless without it.

They fail differently and a flow needs both. The comparison for ``state`` is
constant-time, because it is a secret being compared against attacker-supplied
input and an early-exit comparison leaks its prefix.

The device half is a polling state machine, and RFC 8628 §3.5 makes two of its
outcomes *not* errors: ``authorization_pending`` means keep waiting, and
``slow_down`` means keep waiting and back off - permanently, by at least five
seconds. Treating either as a failure abandons a flow the user is still
completing; treating ``slow_down`` as a one-off retry gets the client rate-limited.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from jmap.auth.metadata import CODE_CHALLENGE_S256, DEVICE_CODE_GRANT
from jmap.core.errors import JMAPError
from jmap.core.narrow import as_object, is_object

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jmap.auth.pkce import PKCEPair


def _number(value: Any) -> float | None:
    """A decoded JSON number, or ``None`` for anything else.

    ``bool`` is a subclass of ``int``, so a bare ``isinstance(value, int)`` admits
    ``true`` - and these are exactly the fields a hostile server gets to choose.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: RFC 8628 §3.5 polling outcomes that mean "not yet" rather than "no".
ERROR_AUTHORIZATION_PENDING: Final = "authorization_pending"
ERROR_SLOW_DOWN: Final = "slow_down"
ERROR_ACCESS_DENIED: Final = "access_denied"
ERROR_EXPIRED_TOKEN: Final = "expired_token"

#: RFC 8628 §3.5: the default poll interval when the server names none, and the
#: minimum increase a ``slow_down`` demands.
DEFAULT_POLL_INTERVAL: Final = 5.0
SLOW_DOWN_INCREMENT: Final = 5.0


class OAuthError(JMAPError):
    """An OAuth endpoint answered with an error, or a response did not validate."""

    def __init__(
        self, error: str, *, description: str | None = None, uri: str | None = None
    ) -> None:
        self.error = error
        self.description = description
        self.uri = uri
        super().__init__(f"{error}{f': {description}' if description else ''}")


class MissingStateError(OAuthError):
    """A redirect was checked against an empty expected state.

    Raised rather than silently comparing "" against "": an absent expected value
    means the flow's state was lost, and continuing would accept any redirect at
    all - including one with no state parameter.
    """

    def __init__(self) -> None:
        super().__init__(
            "missing_state",
            description="no state was issued for this flow, so no redirect can be trusted",
        )


class StateMismatchError(OAuthError):
    """A redirect arrived carrying a ``state`` this client did not issue.

    Not a retryable glitch. RFC 6749 §10.12 makes ``state`` the CSRF defence, so a
    mismatch means the redirect belongs to some other flow - and redeeming the
    code inside it would bind an attacker's account to this session.
    """

    def __init__(self) -> None:
        super().__init__(
            "state_mismatch",
            description=(
                "the redirect carried a state this client did not issue; the code was not redeemed"
            ),
        )


# --------------------------------------------------------------------------- #
# Authorization code flow (RFC 6749 §4.1, RFC 7636, RFC 8252)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Everything needed to send a user to an authorization server and back."""

    authorization_endpoint: str
    client_id: str
    redirect_uri: str
    pkce: PKCEPair
    state: str
    scope: str | None = None
    #: Extra authorization parameters, e.g. ``login_hint`` or ``prompt``.
    extra: Mapping[str, str] = field(default_factory=lambda: {})

    def url(self) -> str:
        """The URL to open in the user's browser."""
        # Extras first, so a caller-supplied `state` or `code_challenge` cannot
        # displace the generated one. Letting `extra` win would silently defeat
        # both guards this module exists to enforce.
        params: dict[str, str] = dict(self.extra)
        params.update(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": self.state,
                "code_challenge": self.pkce.challenge,
                "code_challenge_method": CODE_CHALLENGE_S256,
            }
        )
        if self.scope:
            params["scope"] = self.scope
        separator = "&" if urlsplit(self.authorization_endpoint).query else "?"
        return f"{self.authorization_endpoint}{separator}{urlencode(params)}"


def parse_redirect(url: str, *, expected_state: str) -> str:
    """Pull the authorization code out of the redirect, checking ``state`` first.

    Order matters: the state is verified *before* the code is looked at, so a
    forged redirect never reaches the token endpoint. The comparison is
    constant-time because an early-exit one leaks the expected value a character
    at a time to anything that can retry.
    """
    if not expected_state:
        # Failing open here would accept a redirect carrying no state at all,
        # which is precisely the CSRF the parameter exists to prevent. An empty
        # expected value means the caller lost it, not that checking is optional.
        raise MissingStateError
    params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    # Compared as bytes: `compare_digest` rejects non-ASCII `str` with a
    # TypeError, and `received` is entirely attacker-controlled - so a redirect
    # carrying one accented character would escape every `except OAuthError`.
    if not secrets.compare_digest(params.get("state", "").encode(), expected_state.encode()):
        raise StateMismatchError
    error = params.get("error")
    if error:
        raise OAuthError(
            error, description=params.get("error_description"), uri=params.get("error_uri")
        )
    code = params.get("code")
    if not code:
        raise OAuthError(
            "invalid_response", description="the redirect carried neither a code nor an error"
        )
    return code


def code_exchange_body(
    *, code: str, client_id: str, redirect_uri: str, verifier: str, client_secret: str | None = None
) -> dict[str, str]:
    """The token-endpoint form body for an authorization code (RFC 6749 §4.1.3).

    ``redirect_uri`` is repeated here even though the code already encodes it: the
    server compares the two, which is what stops a code issued for one redirect
    being redeemed against another.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret is not None:
        body["client_secret"] = client_secret
    return body


def refresh_body(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str | None = None,
    scope: str | None = None,
) -> dict[str, str]:
    """The token-endpoint form body for a refresh (RFC 6749 §6)."""
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if client_secret is not None:
        body["client_secret"] = client_secret
    if scope is not None:
        body["scope"] = scope
    return body


# --------------------------------------------------------------------------- #
# Device authorization grant (RFC 8628)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """What the device authorization endpoint hands back (RFC 8628 §3.2)."""

    device_code: str
    user_code: str
    #: Where the user goes to type the code.
    verification_uri: str
    #: The same URI with the code already in it. RFC 8628 §3.3.1 makes it
    #: OPTIONAL and notes clients should still display ``verification_uri`` and
    #: ``user_code`` - a QR code is not readable to someone reading it aloud.
    verification_uri_complete: str | None = None
    expires_in: int | None = None
    #: Seconds between polls. Absent means 5 (§3.2).
    interval: float = DEFAULT_POLL_INTERVAL

    @classmethod
    def of(cls, document: Any) -> DeviceAuthorization:
        if not is_object(document):
            raise OAuthError("invalid_response", description="not a JSON object")
        body = as_object(document)
        # The *value* is checked, not the key: a JSON null passes a presence test
        # and `str(None)` then puts the literal "None" on the wire, leaving the
        # client polling forever with a device code that never existed.
        missing = [
            key
            for key in ("device_code", "user_code", "verification_uri")
            if not isinstance(body.get(key), str)
        ]
        if missing:
            raise OAuthError("invalid_response", description=f"missing {', '.join(missing)}")
        interval = body.get("interval")
        return cls(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=(
                str(body["verification_uri_complete"])
                if body.get("verification_uri_complete")
                else None
            ),
            expires_in=int(seconds) if (seconds := _number(body.get("expires_in"))) else None,
            interval=polled if (polled := _number(interval)) is not None else DEFAULT_POLL_INTERVAL,
        )

    def __repr__(self) -> str:
        # device_code is a credential; user_code is meant to be read aloud.
        return f"DeviceAuthorization(user_code={self.user_code!r}, uri={self.verification_uri!r})"


def device_authorization_body(*, client_id: str, scope: str | None = None) -> dict[str, str]:
    """The device authorization request body (RFC 8628 §3.1)."""
    body = {"client_id": client_id}
    if scope is not None:
        body["scope"] = scope
    return body


def device_token_body(*, device_code: str, client_id: str) -> dict[str, str]:
    """The token-endpoint body for a device-code poll (RFC 8628 §3.4)."""
    return {"grant_type": DEVICE_CODE_GRANT, "device_code": device_code, "client_id": client_id}


class PollOutcome(StrEnum):
    """What a device-flow poll means for the loop around it."""

    #: A token came back. Stop.
    GRANTED = "granted"
    #: Not yet. Wait the current interval and poll again.
    PENDING = "pending"
    #: Not yet, and the interval has been increased. Wait longer and poll again.
    SLOW_DOWN = "slow_down"
    #: Stop: the user said no, or the code expired, or the server refused.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PollResult:
    """One poll's outcome, plus the interval to use next."""

    outcome: PollOutcome
    interval: float
    error: OAuthError | None = None

    @property
    def keep_polling(self) -> bool:
        return self.outcome in {PollOutcome.PENDING, PollOutcome.SLOW_DOWN}


def classify_poll(status: int, document: Any, *, interval: float) -> PollResult:
    """Decide what a device-flow token response means (RFC 8628 §3.5).

    The two outcomes that are not failures are the whole reason this exists.
    ``authorization_pending`` is the normal state of a flow the user has not
    finished, and ``slow_down`` is a *durable* instruction: the increase applies to
    every later poll, not just the next one, so it is returned rather than slept
    off and forgotten.
    """
    if 200 <= status < 300:
        return PollResult(PollOutcome.GRANTED, interval)
    if not is_object(document):
        return PollResult(
            PollOutcome.FAILED,
            interval,
            OAuthError("invalid_response", description=f"HTTP {status} with no OAuth error body"),
        )
    body = as_object(document)
    error = str(body.get("error", "invalid_response"))
    described = OAuthError(
        error,
        description=body.get("error_description"),
        uri=body.get("error_uri"),
    )
    if error == ERROR_AUTHORIZATION_PENDING:
        return PollResult(PollOutcome.PENDING, interval)
    if error == ERROR_SLOW_DOWN:
        return PollResult(PollOutcome.SLOW_DOWN, interval + SLOW_DOWN_INCREMENT)
    return PollResult(PollOutcome.FAILED, interval, described)


# --------------------------------------------------------------------------- #
# Token responses (RFC 6749 §5)
# --------------------------------------------------------------------------- #
def parse_token_response(document: Any, *, now: float) -> dict[str, Any]:
    """Validate a token response into the fields :class:`OAuth2Token` needs.

    ``expires_in`` is converted to an absolute deadline here rather than stored
    as a duration, because a duration is only meaningful next to the instant it
    was received - and that instant is lost the moment the value is persisted.
    """
    if not is_object(document):
        raise OAuthError("invalid_response", description="the token response was not a JSON object")
    body = as_object(document)
    error = body.get("error")
    if error:
        raise OAuthError(
            str(error), description=body.get("error_description"), uri=body.get("error_uri")
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("invalid_response", description="the token response had no access_token")
    expires_in = body.get("expires_in")
    refresh_token = body.get("refresh_token")
    scope = body.get("scope")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
        "expires_at": now + lifetime if (lifetime := _number(expires_in)) is not None else None,
        "scope": scope if isinstance(scope, str) else None,
    }


# --------------------------------------------------------------------------- #
# Dynamic client registration (RFC 7591)
# --------------------------------------------------------------------------- #
def registration_body(
    *,
    client_name: str,
    redirect_uris: Sequence[str] = (),
    grant_types: Sequence[str] = ("authorization_code", "refresh_token"),
    scope: str | None = None,
) -> dict[str, Any]:
    """A dynamic client registration request (RFC 7591 §2).

    ``token_endpoint_auth_method`` is ``none``: this is a public client, it cannot
    keep a secret on the user's machine, and claiming otherwise would have the
    server issue one that is trivially extractable.
    """
    body: dict[str, Any] = {
        "client_name": client_name,
        "grant_types": list(grant_types),
        "token_endpoint_auth_method": "none",
    }
    if redirect_uris:
        body["redirect_uris"] = list(redirect_uris)
        body["response_types"] = ["code"]
    if scope is not None:
        body["scope"] = scope
    return body


@dataclass(frozen=True, slots=True)
class RegisteredClient:
    """A dynamically registered client (RFC 7591 §3.2.1)."""

    client_id: str
    client_secret: str | None = None
    #: Absent means it does not expire (§3.2.1: 0 means no expiry).
    client_secret_expires_at: int | None = None
    registration_access_token: str | None = None
    registration_client_uri: str | None = None

    @classmethod
    def of(cls, document: Any) -> RegisteredClient:
        if not is_object(document):
            raise OAuthError("invalid_response", description="registration returned no object")
        body = as_object(document)
        error = body.get("error")
        if error:
            raise OAuthError(str(error), description=body.get("error_description"))
        client_id = body.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("invalid_response", description="registration returned no client_id")
        expires = body.get("client_secret_expires_at")
        return cls(
            client_id=client_id,
            client_secret=body.get("client_secret")
            if isinstance(body.get("client_secret"), str)
            else None,
            # 0 is the spec's "never expires", which is not the same as unknown.
            # 0 is the spec's "never expires", which is not the same as unknown.
            client_secret_expires_at=int(deadline) if (deadline := _number(expires)) else None,
            registration_access_token=body.get("registration_access_token")
            if isinstance(body.get("registration_access_token"), str)
            else None,
            registration_client_uri=body.get("registration_client_uri")
            if isinstance(body.get("registration_client_uri"), str)
            else None,
        )

    def __repr__(self) -> str:
        return f"RegisteredClient(client_id={self.client_id!r})"

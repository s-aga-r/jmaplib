"""OAuth discovery documents (RFC 9728, RFC 8414).

Finding a JMAP server's authorization server is a chain, and the library walks it
rather than asking the user to paste endpoints: a 401 carries a
``resource_metadata`` pointer (RFC 9728), that document names the authorization
servers, and each server's metadata (RFC 8414) names the endpoints.

I/O-free. Two rules in here are security properties rather than conveniences, and
both are easy to implement wrongly in a way that still works against a
cooperative server:

**The well-known segment is inserted, not appended.** RFC 8414 §3.1: for an issuer
with a path component, ``/.well-known/oauth-authorization-server`` goes *between
the host and the path*, so ``https://example.com/issuer1`` becomes
``https://example.com/.well-known/oauth-authorization-server/issuer1``. Appending
instead is the common mistake; it happens to work for the single-tenant case,
which is why it survives to production and then breaks on the first multi-tenant
deployment.

**The issuer must be checked against the URL it came from.** RFC 8414 §3.3 says
the returned ``issuer`` MUST be identical to the one the URL was built from, and
that mismatched metadata MUST NOT be used. Skipping the check turns any server
that can answer that path into one that can nominate an attacker's token
endpoint.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from jmap.core.errors import JMAPError
from jmap.core.narrow import as_object, is_object

#: RFC 9728 §3.1 and RFC 8414 §3.1.
PROTECTED_RESOURCE_SUFFIX: Final = "oauth-protected-resource"
AUTHORIZATION_SERVER_SUFFIX: Final = "oauth-authorization-server"
#: OpenID Connect Discovery uses a different suffix, and *appends* it. Plenty of
#: deployments answer only this one, so it is worth trying as a fallback.
OPENID_SUFFIX: Final = "openid-configuration"

#: RFC 7636 §4.2. The only challenge method this library will use - see
#: :mod:`jmap.auth.pkce`.
CODE_CHALLENGE_S256: Final = "S256"

#: RFC 8628 §3.1.
DEVICE_CODE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthDocument(BaseModel):
    """Base for the OAuth discovery documents.

    Deliberately *not* :class:`~jmap.models.base.JMAPModel`. These are OAuth
    documents, and OAuth spells its fields ``snake_case`` - so the camelCase alias
    generator every JMAP object uses would emit ``authorizationServers``, a
    spelling neither RFC 9728 nor RFC 8414 has ever used. Parsing would survive it;
    round-tripping one would not.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class DiscoveryError(JMAPError):
    """A discovery document was missing, malformed, or did not validate."""


class IssuerMismatchError(DiscoveryError):
    """Metadata claims an issuer other than the one it was fetched for.

    RFC 8414 §3.3 requires the document to be discarded. The check is what stops
    a host that merely *answers* the well-known path from nominating whichever
    token endpoint it likes.
    """

    def __init__(self, expected: str, received: str) -> None:
        self.expected = expected
        self.received = received
        super().__init__(
            f"authorization server metadata claims issuer {received!r} but was "
            f"fetched for {expected!r}; RFC 8414 §3.3 requires discarding it"
        )


def well_known_url(issuer: str, suffix: str) -> str:
    """Build a metadata URL by *inserting* the well-known segment (RFC 8414 §3.1).

    A trailing slash on the issuer is removed first, as the RFC requires - so
    ``https://example.com/t/`` and ``https://example.com/t`` produce the same URL
    rather than one with an empty path segment in the middle.
    """
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}{path}", "", ""))


def openid_url(issuer: str) -> str:
    """Build an OpenID Connect discovery URL, which *appends* instead.

    Kept separate rather than parameterised, because the two conventions differ in
    exactly the way that makes one a plausible-looking bug in the other.
    """
    return f"{issuer.rstrip('/')}/.well-known/{OPENID_SUFFIX}"


class ProtectedResourceMetadata(OAuthDocument):
    """RFC 9728 §2 - what a resource server says about protecting itself.

    Reached from the ``resource_metadata`` parameter of a ``WWW-Authenticate``
    challenge, which is how a JMAP server points at its own OAuth setup without
    the client having to be told anything in advance.
    """

    #: The resource identifier. Should match the JMAP server being talked to.
    resource: str | None = None
    #: Issuer identifiers of the authorization servers that can issue tokens for
    #: this resource. Empty means the resource named none, not that any will do.
    authorization_servers: list[str] = []
    bearer_methods_supported: list[str] = []
    scopes_supported: list[str] = []
    resource_name: str | None = None
    resource_documentation: str | None = None

    @classmethod
    def of(cls, document: Any) -> ProtectedResourceMetadata:
        """Parse a fetched document, rejecting one that is not an object."""
        if not is_object(document):
            raise DiscoveryError("protected resource metadata was not a JSON object")
        return cls.model_validate(as_object(document))

    def issuer(self) -> str:
        """The single authorization server to use, or an error naming why not.

        Refuses to choose when a resource lists several: which one is right
        depends on which the user has an account with, and guessing sends the
        user through a login at the wrong provider.
        """
        if not self.authorization_servers:
            raise DiscoveryError(
                "the resource metadata lists no authorization_servers, so there is "
                "nothing to discover; the server may expect a non-OAuth credential"
            )
        if len(self.authorization_servers) > 1:
            raise DiscoveryError(
                f"the resource lists {len(self.authorization_servers)} authorization "
                f"servers; pass the one to use explicitly rather than guessing: "
                f"{', '.join(self.authorization_servers)}"
            )
        return self.authorization_servers[0]


class AuthorizationServerMetadata(OAuthDocument):
    """RFC 8414 §2 - the endpoints and capabilities of an authorization server."""

    issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    #: RFC 8628 §4.
    device_authorization_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes_supported: list[str] = []
    response_types_supported: list[str] = []
    grant_types_supported: list[str] = []
    #: RFC 7636. A server omitting this is *presumed* not to support PKCE, which
    #: this library treats as disqualifying - see :meth:`check_pkce`.
    code_challenge_methods_supported: list[str] = []
    token_endpoint_auth_methods_supported: list[str] = []

    @classmethod
    def of(
        cls, document: Any, *, expected_issuer: str | None = None
    ) -> AuthorizationServerMetadata:
        """Parse and validate a fetched document.

        ``expected_issuer`` is the identifier the URL was built from. RFC 8414
        §3.3 makes comparing it mandatory, so it is a parameter here rather than
        something a caller can forget to do afterwards.
        """
        if not is_object(document):
            raise DiscoveryError("authorization server metadata was not a JSON object")
        metadata = cls.model_validate(as_object(document))
        if expected_issuer is not None and metadata.issuer != expected_issuer:
            raise IssuerMismatchError(expected_issuer, metadata.issuer or "")
        return metadata

    def supports_device_flow(self) -> bool:
        """Whether RFC 8628 is advertised.

        Both halves are required: an endpoint to call and the grant type to name
        at the token endpoint. A server offering one without the other cannot
        complete the flow.
        """
        return self.device_authorization_endpoint is not None and (
            not self.grant_types_supported or DEVICE_CODE_GRANT in self.grant_types_supported
        )

    def check_pkce(self) -> None:
        """Refuse an authorization server that cannot do PKCE with SHA-256.

        Not a preference. Without PKCE an intercepted authorization code is
        directly redeemable, and ``plain`` leaves the verifier in the same URL as
        the challenge - so a server advertising neither is one this library will
        not run an authorization-code flow against.
        """
        if CODE_CHALLENGE_S256 not in self.code_challenge_methods_supported:
            raise DiscoveryError(
                "this authorization server does not advertise the S256 PKCE method "
                f"(it lists {', '.join(self.code_challenge_methods_supported) or 'none'}); "
                f"without it an intercepted authorization code is directly redeemable"
            )

    def require(self, endpoint: str) -> str:
        """Read a required endpoint, or say which one is missing."""
        value = getattr(self, endpoint, None)
        if not isinstance(value, str) or not value:
            raise DiscoveryError(
                f"the authorization server metadata has no {endpoint}, so this flow "
                f"cannot be completed against it"
            )
        return value

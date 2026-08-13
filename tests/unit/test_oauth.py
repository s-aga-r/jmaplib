"""PKCE, OAuth discovery documents, and the flow messages themselves.

All three modules are pure functions over strings and decoded JSON, and all three
are the part of the library an attacker gets to talk to. Every rule pinned here
has a wrong implementation that still works perfectly against a cooperative
server - an appended well-known segment, an unchecked issuer, a ``state``
compared after the code has already been read - so a test is the only thing that
notices when one goes missing.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from jmap.auth.flows import (
    DEFAULT_POLL_INTERVAL,
    SLOW_DOWN_INCREMENT,
    AuthorizationRequest,
    DeviceAuthorization,
    MissingStateError,
    OAuthError,
    PollOutcome,
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
    AUTHORIZATION_SERVER_SUFFIX,
    DEVICE_CODE_GRANT,
    PROTECTED_RESOURCE_SUFFIX,
    AuthorizationServerMetadata,
    DiscoveryError,
    IssuerMismatchError,
    ProtectedResourceMetadata,
    openid_url,
    well_known_url,
)
from jmap.auth.pkce import (
    MAX_VERIFIER_LENGTH,
    METHOD_S256,
    MIN_VERIFIER_LENGTH,
    InvalidVerifierError,
    PKCEPair,
    challenge_for,
    new_state,
    new_verifier,
)

#: RFC 7636 Appendix B, verbatim. The one pair of values every implementation
#: agrees on, which is what makes it worth pinning rather than recomputing.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def pair(challenge: str = "chal") -> PKCEPair:
    return PKCEPair(verifier="v" * MIN_VERIFIER_LENGTH, challenge=challenge)


def request(
    endpoint: str = "https://as.example.com/authorize",
    *,
    pkce: PKCEPair | None = None,
    redirect_uri: str = "http://127.0.0.1:8765/cb",
    scope: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    return AuthorizationRequest(
        authorization_endpoint=endpoint,
        client_id="client-1",
        redirect_uri=redirect_uri,
        pkce=pkce if pkce is not None else pair(),
        state="st4te",
        scope=scope,
        extra=extra if extra is not None else {},
    ).url()


def query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def server(**fields: object) -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata.of(dict(fields))


class TestVerifiers:
    def test_a_generated_verifier_is_the_shortest_legal_one(self):
        # 43 characters is both the RFC minimum and where the entropy stops
        # mattering: 32 random bytes, 256 bits.
        assert len(new_verifier()) == MIN_VERIFIER_LENGTH

    def test_a_generated_verifier_carries_no_base64_padding(self):
        assert "=" not in new_verifier()

    def test_two_generated_verifiers_differ(self):
        # A verifier reused across flows is a verifier an interceptor already has.
        assert new_verifier() != new_verifier()

    def test_a_generated_verifier_uses_only_unreserved_characters(self):
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        assert set(new_verifier()) <= allowed

    def test_a_verifier_one_character_short_is_refused(self):
        with pytest.raises(InvalidVerifierError):
            challenge_for("a" * (MIN_VERIFIER_LENGTH - 1))

    def test_a_verifier_one_character_too_long_is_refused(self):
        with pytest.raises(InvalidVerifierError):
            challenge_for("a" * (MAX_VERIFIER_LENGTH + 1))

    def test_an_empty_verifier_is_refused(self):
        with pytest.raises(InvalidVerifierError):
            challenge_for("")

    def test_both_bounds_are_inclusive(self):
        assert challenge_for("a" * MIN_VERIFIER_LENGTH)
        assert challenge_for("a" * MAX_VERIFIER_LENGTH)

    def test_the_error_reports_the_length_it_saw(self):
        with pytest.raises(InvalidVerifierError) as excinfo:
            challenge_for("short")
        assert excinfo.value.length == 5
        assert "43-128" in str(excinfo.value)

    def test_the_error_is_a_value_error(self):
        # So a caller validating user-supplied verifiers can catch it generically.
        assert issubclass(InvalidVerifierError, ValueError)


class TestChallenges:
    def test_the_rfc_test_vector_reproduces(self):
        assert challenge_for(RFC_VERIFIER) == RFC_CHALLENGE

    def test_the_challenge_is_unpadded_base64url_of_the_sha256(self):
        verifier = new_verifier()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        assert challenge_for(verifier) == base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def test_the_challenge_carries_no_padding(self):
        # 32 bytes is not a multiple of 3, so a padded encoder leaves a `=` that
        # then has to survive percent-encoding in the authorization URL.
        assert "=" not in challenge_for(RFC_VERIFIER)

    def test_the_challenge_is_derived_deterministically(self):
        assert challenge_for(RFC_VERIFIER) == challenge_for(RFC_VERIFIER)

    def test_different_verifiers_give_different_challenges(self):
        assert challenge_for("a" * 43) != challenge_for("b" * 43)


class TestThePair:
    def test_a_generated_pair_is_internally_consistent(self):
        generated = PKCEPair.generate()
        assert challenge_for(generated.verifier) == generated.challenge

    def test_a_generated_pair_names_s256(self):
        assert PKCEPair.generate().method == METHOD_S256 == "S256"

    def test_two_generated_pairs_differ(self):
        assert PKCEPair.generate().verifier != PKCEPair.generate().verifier

    def test_the_repr_does_not_contain_the_verifier(self):
        # The verifier is the credential the whole exchange rests on, and these
        # objects reach logs and tracebacks far more often than anyone intends.
        generated = PKCEPair.generate()
        assert generated.verifier not in repr(generated)

    def test_the_repr_still_names_the_method_and_challenge(self):
        generated = PKCEPair.generate()
        assert generated.challenge in repr(generated)
        assert METHOD_S256 in repr(generated)


class TestState:
    def test_two_states_differ(self):
        assert new_state() != new_state()

    def test_a_state_is_long_enough_to_be_unguessable(self):
        assert len(new_state()) >= MIN_VERIFIER_LENGTH

    def test_a_state_is_url_safe(self):
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert set(new_state()) <= allowed


class TestWellKnownUrls:
    def test_the_segment_is_inserted_before_an_issuer_path(self):
        # RFC 8414 §3.1. Appending instead happens to work for a single-tenant
        # server, which is why the bug survives to production and then breaks on
        # the first multi-tenant deployment.
        assert (
            well_known_url("https://example.com/issuer1", AUTHORIZATION_SERVER_SUFFIX)
            == "https://example.com/.well-known/oauth-authorization-server/issuer1"
        )

    def test_an_issuer_with_no_path_gets_the_bare_url(self):
        assert (
            well_known_url("https://example.com", AUTHORIZATION_SERVER_SUFFIX)
            == "https://example.com/.well-known/oauth-authorization-server"
        )

    def test_a_trailing_slash_leaves_no_empty_segment(self):
        assert well_known_url(
            "https://example.com/t/", AUTHORIZATION_SERVER_SUFFIX
        ) == well_known_url("https://example.com/t", AUTHORIZATION_SERVER_SUFFIX)

    def test_a_bare_issuer_with_a_trailing_slash_is_the_same_as_without(self):
        assert (
            well_known_url("https://example.com/", AUTHORIZATION_SERVER_SUFFIX)
            == "https://example.com/.well-known/oauth-authorization-server"
        )

    def test_a_multi_segment_path_is_kept_whole(self):
        assert (
            well_known_url("https://example.com/a/b", AUTHORIZATION_SERVER_SUFFIX)
            == "https://example.com/.well-known/oauth-authorization-server/a/b"
        )

    def test_the_port_is_preserved(self):
        assert well_known_url("https://example.com:8443/t", PROTECTED_RESOURCE_SUFFIX).startswith(
            "https://example.com:8443/"
        )

    def test_the_protected_resource_suffix_is_placed_the_same_way(self):
        assert (
            well_known_url("https://jmap.example.com/mail", PROTECTED_RESOURCE_SUFFIX)
            == "https://jmap.example.com/.well-known/oauth-protected-resource/mail"
        )


class TestOpenIdUrls:
    def test_openid_appends_rather_than_inserting(self):
        # The two conventions differ in exactly the way that makes one a
        # plausible-looking bug in the other, so they are pinned against each
        # other rather than separately.
        assert (
            openid_url("https://example.com/issuer1")
            == "https://example.com/issuer1/.well-known/openid-configuration"
        )

    def test_openid_absorbs_a_trailing_slash(self):
        assert openid_url("https://example.com/t/") == openid_url("https://example.com/t")

    def test_the_two_conventions_disagree_for_a_path_issuer(self):
        issuer = "https://example.com/issuer1"
        assert openid_url(issuer) != well_known_url(issuer, AUTHORIZATION_SERVER_SUFFIX)


class TestProtectedResourceMetadata:
    def test_a_document_parses(self):
        metadata = ProtectedResourceMetadata.of(
            {
                "resource": "https://jmap.example.com",
                "authorization_servers": ["https://as.example.com"],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["urn:ietf:params:jmap:core"],
                "resource_name": "Example Mail",
                "resource_documentation": "https://example.com/docs",
            }
        )
        assert metadata.resource == "https://jmap.example.com"
        assert metadata.bearer_methods_supported == ["header"]
        assert metadata.resource_name == "Example Mail"

    def test_a_document_that_is_not_an_object_is_refused(self):
        with pytest.raises(DiscoveryError, match="not a JSON object"):
            ProtectedResourceMetadata.of(["https://as.example.com"])

    def test_an_empty_document_is_legal_but_useless(self):
        assert ProtectedResourceMetadata.of({}).authorization_servers == []

    def test_the_single_authorization_server_is_returned(self):
        metadata = ProtectedResourceMetadata.of(
            {"authorization_servers": ["https://as.example.com"]}
        )
        assert metadata.issuer() == "https://as.example.com"

    def test_naming_no_servers_is_not_the_same_as_allowing_any(self):
        # A resource listing none may simply not use OAuth; picking one anyway
        # would send the user to log in somewhere the resource never mentioned.
        with pytest.raises(DiscoveryError, match="no authorization_servers"):
            ProtectedResourceMetadata.of({}).issuer()

    def test_several_servers_are_not_guessed_between(self):
        # Which one is right depends on where the user has an account, and a
        # wrong guess is a completed login that grants nothing.
        metadata = ProtectedResourceMetadata.of(
            {"authorization_servers": ["https://a.example.com", "https://b.example.com"]}
        )
        with pytest.raises(DiscoveryError, match="2 authorization"):
            metadata.issuer()

    def test_the_ambiguity_error_names_the_candidates(self):
        metadata = ProtectedResourceMetadata.of(
            {"authorization_servers": ["https://a.example.com", "https://b.example.com"]}
        )
        with pytest.raises(DiscoveryError) as excinfo:
            metadata.issuer()
        assert "https://a.example.com" in str(excinfo.value)
        assert "https://b.example.com" in str(excinfo.value)


class TestAuthorizationServerMetadata:
    def test_a_document_parses(self):
        metadata = AuthorizationServerMetadata.of(
            {
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "revocation_endpoint": "https://as.example.com/revoke",
                "response_types_supported": ["code"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )
        assert metadata.token_endpoint == "https://as.example.com/token"
        assert metadata.registration_endpoint == "https://as.example.com/register"
        assert metadata.revocation_endpoint == "https://as.example.com/revoke"
        assert metadata.token_endpoint_auth_methods_supported == ["none"]

    def test_a_document_that_is_not_an_object_is_refused(self):
        with pytest.raises(DiscoveryError, match="not a JSON object"):
            AuthorizationServerMetadata.of("https://as.example.com")

    def test_a_matching_issuer_validates(self):
        metadata = AuthorizationServerMetadata.of(
            {"issuer": "https://as.example.com"}, expected_issuer="https://as.example.com"
        )
        assert metadata.issuer == "https://as.example.com"

    def test_a_mismatched_issuer_is_discarded(self):
        # RFC 8414 §3.3. Without this check, any host that merely answers the
        # well-known path can nominate whichever token endpoint it likes.
        with pytest.raises(IssuerMismatchError):
            AuthorizationServerMetadata.of(
                {"issuer": "https://evil.example.com"}, expected_issuer="https://as.example.com"
            )

    def test_the_mismatch_error_carries_both_identifiers(self):
        with pytest.raises(IssuerMismatchError) as excinfo:
            AuthorizationServerMetadata.of(
                {"issuer": "https://evil.example.com"}, expected_issuer="https://as.example.com"
            )
        assert excinfo.value.expected == "https://as.example.com"
        assert excinfo.value.received == "https://evil.example.com"

    def test_metadata_naming_no_issuer_at_all_is_a_mismatch(self):
        # Omitting the field is not a way to opt out of §3.3.
        with pytest.raises(IssuerMismatchError) as excinfo:
            AuthorizationServerMetadata.of({}, expected_issuer="https://as.example.com")
        assert excinfo.value.received == ""

    def test_no_expected_issuer_means_no_comparison(self):
        assert AuthorizationServerMetadata.of({"issuer": "https://as.example.com"}).issuer

    def test_a_mismatch_is_a_discovery_error(self):
        assert issubclass(IssuerMismatchError, DiscoveryError)


class TestPkceEnforcement:
    def test_a_server_advertising_s256_passes(self):
        server(code_challenge_methods_supported=["S256"]).check_pkce()

    def test_a_server_advertising_s256_among_others_passes(self):
        server(code_challenge_methods_supported=["plain", "S256"]).check_pkce()

    def test_a_server_advertising_nothing_is_refused(self):
        # RFC 9700 (BCP 240): a server that cannot do PKCE is one where an
        # intercepted authorization code is directly redeemable, so the flow is
        # declined rather than downgraded.
        with pytest.raises(DiscoveryError, match="does not advertise the S256"):
            server().check_pkce()

    def test_a_server_advertising_only_plain_is_refused(self):
        # `plain` puts the verifier in the same URL as the challenge, so it
        # protects exactly nobody who needed protecting.
        with pytest.raises(DiscoveryError, match="plain"):
            server(code_challenge_methods_supported=["plain"]).check_pkce()

    def test_the_error_says_none_when_the_server_listed_nothing(self):
        with pytest.raises(DiscoveryError, match="it lists none"):
            server().check_pkce()


class TestRequiredEndpoints:
    def test_a_present_endpoint_is_returned(self):
        metadata = server(token_endpoint="https://as.example.com/token")
        assert metadata.require("token_endpoint") == "https://as.example.com/token"

    def test_a_missing_endpoint_names_itself(self):
        with pytest.raises(DiscoveryError, match="no registration_endpoint"):
            server().require("registration_endpoint")

    def test_an_empty_endpoint_counts_as_missing(self):
        with pytest.raises(DiscoveryError, match="no token_endpoint"):
            server(token_endpoint="").require("token_endpoint")

    def test_a_field_that_is_not_a_string_counts_as_missing(self):
        # `extra="allow"` means any name resolves to *something*; a list of
        # scopes is not an endpoint.
        with pytest.raises(DiscoveryError, match="no scopes_supported"):
            server(scopes_supported=["mail"]).require("scopes_supported")


class TestDeviceFlowAdvertisement:
    def test_an_endpoint_and_the_grant_type_is_support(self):
        metadata = server(
            device_authorization_endpoint="https://as.example.com/device",
            grant_types_supported=[DEVICE_CODE_GRANT],
        )
        assert metadata.supports_device_flow() is True

    def test_no_endpoint_means_no_device_flow(self):
        assert server(grant_types_supported=[DEVICE_CODE_GRANT]).supports_device_flow() is False

    def test_an_endpoint_with_no_grant_types_listed_is_taken_at_its_word(self):
        # RFC 8414 makes `grant_types_supported` OPTIONAL, so an absent list is
        # silence rather than a denial.
        metadata = server(device_authorization_endpoint="https://as.example.com/device")
        assert metadata.supports_device_flow() is True

    def test_an_endpoint_the_grant_types_contradict_is_not_support(self):
        metadata = server(
            device_authorization_endpoint="https://as.example.com/device",
            grant_types_supported=["authorization_code"],
        )
        assert metadata.supports_device_flow() is False


class TestTheAuthorizationUrl:
    def test_every_required_parameter_is_present(self):
        params = query(request())
        assert params["response_type"] == "code"
        assert params["client_id"] == "client-1"
        assert params["redirect_uri"] == "http://127.0.0.1:8765/cb"
        assert params["state"] == "st4te"
        assert params["code_challenge"] == "chal"
        assert params["code_challenge_method"] == "S256"

    def test_a_scope_is_sent_when_asked_for(self):
        assert query(request(scope="urn:ietf:params:jmap:core"))["scope"] == (
            "urn:ietf:params:jmap:core"
        )

    def test_no_scope_parameter_when_none_was_asked_for(self):
        assert "scope" not in query(request())

    def test_an_empty_scope_is_omitted_rather_than_sent_blank(self):
        # An empty `scope` is a request for no access at all, not a default.
        assert "scope" not in query(request(scope=""))

    def test_extra_parameters_are_carried(self):
        params = query(request(extra={"login_hint": "alice@example.com", "prompt": "consent"}))
        assert params["login_hint"] == "alice@example.com"
        assert params["prompt"] == "consent"

    def test_the_query_starts_with_a_question_mark(self):
        assert request().startswith("https://as.example.com/authorize?")

    def test_an_endpoint_that_already_has_a_query_is_appended_to(self):
        # A second `?` would make the whole existing query part of a parameter
        # value, and the server would see neither.
        url = request("https://as.example.com/authorize?tenant=acme")
        assert "authorize?tenant=acme&" in url
        assert query(url)["tenant"] == "acme"

    def test_parameters_are_percent_encoded(self):
        url = request(redirect_uri="http://127.0.0.1:8765/cb?x=1")
        assert "cb%3Fx%3D1" in url
        assert query(url)["redirect_uri"] == "http://127.0.0.1:8765/cb?x=1"

    def test_the_challenge_comes_from_the_pair(self):
        generated = PKCEPair.generate()
        assert query(request(pkce=generated))["code_challenge"] == generated.challenge

    def test_the_verifier_never_reaches_the_url(self):
        # The whole point of S256: only the challenge travels through the browser.
        generated = PKCEPair.generate()
        assert generated.verifier not in request(pkce=generated)


class TestParsingTheRedirect:
    def test_the_code_is_returned(self):
        assert parse_redirect("http://127.0.0.1/cb?code=abc&state=st", expected_state="st") == "abc"

    def test_a_mismatched_state_is_refused(self):
        with pytest.raises(StateMismatchError):
            parse_redirect("http://127.0.0.1/cb?code=abc&state=other", expected_state="st")

    def test_a_missing_state_never_matches(self):
        with pytest.raises(StateMismatchError):
            parse_redirect("http://127.0.0.1/cb?code=abc", expected_state="st")

    def test_the_state_is_checked_before_the_code(self):
        # Order is the property: a forged redirect must never reach the token
        # endpoint, even one carrying a perfectly well-formed code.
        with pytest.raises(StateMismatchError):
            parse_redirect("http://127.0.0.1/cb?code=attacker&state=wrong", expected_state="st")

    def test_the_state_is_checked_before_the_error(self):
        # Otherwise an attacker's redirect gets reported as the server's error,
        # and the real flow looks like it failed.
        with pytest.raises(StateMismatchError):
            parse_redirect(
                "http://127.0.0.1/cb?error=access_denied&state=wrong", expected_state="st"
            )

    def test_the_mismatch_says_the_code_was_not_redeemed(self):
        with pytest.raises(StateMismatchError) as excinfo:
            parse_redirect("http://127.0.0.1/cb?state=wrong", expected_state="st")
        assert excinfo.value.error == "state_mismatch"
        assert "not redeemed" in str(excinfo.value)

    def test_an_error_parameter_is_surfaced(self):
        with pytest.raises(OAuthError, match="access_denied") as excinfo:
            parse_redirect("http://127.0.0.1/cb?error=access_denied&state=st", expected_state="st")
        assert excinfo.value.error == "access_denied"

    def test_the_error_description_and_uri_are_kept(self):
        with pytest.raises(OAuthError) as excinfo:
            parse_redirect(
                "http://127.0.0.1/cb?state=st&error=invalid_scope"
                "&error_description=bad+scope&error_uri=https%3A%2F%2Fe.example.com%2Ferr",
                expected_state="st",
            )
        assert excinfo.value.description == "bad scope"
        assert excinfo.value.uri == "https://e.example.com/err"

    def test_a_redirect_with_neither_a_code_nor_an_error_is_refused(self):
        with pytest.raises(OAuthError, match="neither a code nor an error"):
            parse_redirect("http://127.0.0.1/cb?state=st", expected_state="st")

    def test_an_empty_code_is_not_a_code(self):
        with pytest.raises(OAuthError, match="neither a code nor an error"):
            parse_redirect("http://127.0.0.1/cb?state=st&code=", expected_state="st")

    def test_an_empty_error_falls_through_to_the_code(self):
        assert parse_redirect("http://127.0.0.1/cb?state=st&error=&code=ok", expected_state="st")

    def test_a_fragment_is_not_the_query(self):
        # Response-mode `fragment` never reaches the server side of a loopback
        # listener, so a code found there is not one this flow issued.
        with pytest.raises(StateMismatchError):
            parse_redirect("http://127.0.0.1/cb#code=abc&state=st", expected_state="st")


class TestTokenRequestBodies:
    def test_the_code_exchange_carries_the_verifier(self):
        body = code_exchange_body(
            code="c", client_id="client-1", redirect_uri="http://127.0.0.1/cb", verifier="v"
        )
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "c"
        assert body["code_verifier"] == "v"
        assert body["client_id"] == "client-1"

    def test_the_code_exchange_repeats_the_redirect_uri(self):
        # RFC 6749 §4.1.3: the server compares it against the one the code was
        # issued for, which is what stops the code being redeemed elsewhere.
        body = code_exchange_body(
            code="c", client_id="client-1", redirect_uri="http://127.0.0.1/cb", verifier="v"
        )
        assert body["redirect_uri"] == "http://127.0.0.1/cb"

    def test_a_public_client_sends_no_secret_key_at_all(self):
        body = code_exchange_body(
            code="c", client_id="client-1", redirect_uri="http://127.0.0.1/cb", verifier="v"
        )
        assert "client_secret" not in body

    def test_a_secret_is_sent_when_there_is_one(self):
        body = code_exchange_body(
            code="c",
            client_id="client-1",
            redirect_uri="http://127.0.0.1/cb",
            verifier="v",
            client_secret="s3cret",
        )
        assert body["client_secret"] == "s3cret"

    def test_the_refresh_body_names_the_grant(self):
        body = refresh_body(refresh_token="r", client_id="client-1")
        assert body == {
            "grant_type": "refresh_token",
            "refresh_token": "r",
            "client_id": "client-1",
        }

    def test_a_refresh_carries_a_secret_when_there_is_one(self):
        body = refresh_body(refresh_token="r", client_id="client-1", client_secret="s3cret")
        assert body["client_secret"] == "s3cret"

    def test_a_refresh_can_narrow_the_scope(self):
        assert refresh_body(refresh_token="r", client_id="c", scope="a")["scope"] == "a"

    def test_an_empty_scope_is_still_sent_on_a_refresh(self):
        # RFC 6749 §6 lets a client drop every scope on refresh; only omitting
        # the key means "keep what I had".
        assert refresh_body(refresh_token="r", client_id="c", scope="")["scope"] == ""


class TestDeviceAuthorization:
    def test_a_response_parses(self):
        device = DeviceAuthorization.of(
            {
                "device_code": "dev-code",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://example.com/device",
                "verification_uri_complete": "https://example.com/device?user_code=WDJB-MJHT",
                "expires_in": 1800,
                "interval": 5,
            }
        )
        assert device.device_code == "dev-code"
        assert device.user_code == "WDJB-MJHT"
        assert device.verification_uri == "https://example.com/device"
        assert device.verification_uri_complete == (
            "https://example.com/device?user_code=WDJB-MJHT"
        )
        assert device.expires_in == 1800

    def test_the_interval_defaults_to_five_seconds(self):
        # RFC 8628 §3.2. A client that polled as fast as it liked would be
        # rate-limited into a `slow_down` on its second request.
        device = DeviceAuthorization.of(
            {"device_code": "d", "user_code": "U", "verification_uri": "https://x/"}
        )
        assert device.interval == DEFAULT_POLL_INTERVAL == 5.0

    def test_a_named_interval_is_used(self):
        device = DeviceAuthorization.of(
            {"device_code": "d", "user_code": "U", "verification_uri": "https://x/", "interval": 10}
        )
        assert device.interval == 10.0

    def test_a_fractional_interval_survives(self):
        device = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://x/",
                "interval": 2.5,
            }
        )
        assert device.interval == 2.5

    def test_a_zero_or_negative_interval_is_clamped(self):
        # `interval: 0` would turn the poll loop into a request cannon against
        # the token endpoint, and a negative one crashes time.sleep.
        for hostile in (0, -1):
            device = DeviceAuthorization.of(
                {
                    "device_code": "d",
                    "user_code": "U",
                    "verification_uri": "https://x/",
                    "interval": hostile,
                }
            )
            assert device.interval == 1.0

    def test_an_absurd_interval_is_clamped_to_the_ceiling(self):
        # A server-chosen interval past the code's own lifetime is a stalled
        # flow, not pacing.
        device = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://x/",
                "interval": 999_999_999,
            }
        )
        assert device.interval == 1800.0

    def test_a_non_numeric_interval_falls_back_to_the_default(self):
        device = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://x/",
                "interval": "soon",
            }
        )
        assert device.interval == DEFAULT_POLL_INTERVAL

    def test_the_complete_uri_is_optional(self):
        device = DeviceAuthorization.of(
            {"device_code": "d", "user_code": "U", "verification_uri": "https://x/"}
        )
        assert device.verification_uri_complete is None

    def test_an_empty_complete_uri_is_treated_as_absent(self):
        device = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://x/",
                "verification_uri_complete": "",
            }
        )
        assert device.verification_uri_complete is None

    def test_a_missing_expires_in_is_none(self):
        device = DeviceAuthorization.of(
            {"device_code": "d", "user_code": "U", "verification_uri": "https://x/"}
        )
        assert device.expires_in is None

    def test_a_non_integer_expires_in_is_dropped(self):
        device = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://x/",
                "expires_in": "1800",
            }
        )
        assert device.expires_in is None

    def test_a_response_that_is_not_an_object_is_refused(self):
        with pytest.raises(OAuthError, match="not a JSON object"):
            DeviceAuthorization.of("nope")

    def test_the_missing_fields_are_named(self):
        with pytest.raises(OAuthError, match="missing device_code, verification_uri"):
            DeviceAuthorization.of({"user_code": "U"})

    def test_one_missing_field_is_enough_to_refuse(self):
        with pytest.raises(OAuthError, match="missing verification_uri"):
            DeviceAuthorization.of({"device_code": "d", "user_code": "U"})

    def test_the_repr_does_not_contain_the_device_code(self):
        # The device code is the credential; the user code is meant to be read
        # aloud, so it stays.
        device = DeviceAuthorization.of(
            {"device_code": "secret-dev-code", "user_code": "U", "verification_uri": "https://x/"}
        )
        assert "secret-dev-code" not in repr(device)
        assert "U" in repr(device)


class TestDeviceBodies:
    def test_the_authorization_request_names_the_client(self):
        assert device_authorization_body(client_id="client-1") == {"client_id": "client-1"}

    def test_a_scope_is_included_when_given(self):
        body = device_authorization_body(client_id="client-1", scope="urn:jmap")
        assert body["scope"] == "urn:jmap"

    def test_the_poll_body_names_the_device_code_grant(self):
        body = device_token_body(device_code="d", client_id="client-1")
        assert body["grant_type"] == DEVICE_CODE_GRANT
        assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        assert body["device_code"] == "d"
        assert body["client_id"] == "client-1"


class TestPolling:
    def test_a_2xx_is_a_grant(self):
        result = classify_poll(200, {"access_token": "t"}, interval=5.0)
        assert result.outcome is PollOutcome.GRANTED
        assert result.error is None

    def test_a_grant_does_not_change_the_interval(self):
        assert (
            classify_poll(
                200,
                {},
                interval=7.5,
            ).interval
            == 7.5
        )

    def test_a_grant_stops_the_loop(self):
        assert classify_poll(200, {}, interval=5.0).keep_polling is False

    def test_authorization_pending_is_not_a_failure(self):
        # RFC 8628 §3.5: the normal state of a flow the user has not finished.
        # Treating it as an error abandons the flow mid-login.
        result = classify_poll(400, {"error": "authorization_pending"}, interval=5.0)
        assert result.outcome is PollOutcome.PENDING
        assert result.keep_polling is True

    def test_pending_leaves_the_interval_alone(self):
        assert classify_poll(400, {"error": "authorization_pending"}, interval=5.0).interval == 5.0

    def test_slow_down_adds_five_seconds(self):
        # §3.5 makes the increase permanent, so it is returned for the caller to
        # keep rather than slept off once and forgotten.
        result = classify_poll(400, {"error": "slow_down"}, interval=5.0)
        assert result.interval == 5.0 + SLOW_DOWN_INCREMENT == 10.0

    def test_slow_down_keeps_polling(self):
        result = classify_poll(400, {"error": "slow_down"}, interval=5.0)
        assert result.outcome is PollOutcome.SLOW_DOWN
        assert result.keep_polling is True

    def test_repeated_slow_downs_compound(self):
        first = classify_poll(400, {"error": "slow_down"}, interval=5.0)
        second = classify_poll(400, {"error": "slow_down"}, interval=first.interval)
        assert second.interval == 15.0

    def test_access_denied_stops_the_loop(self):
        result = classify_poll(400, {"error": "access_denied"}, interval=5.0)
        assert result.outcome is PollOutcome.FAILED
        assert result.keep_polling is False

    def test_expired_token_stops_the_loop(self):
        assert classify_poll(400, {"error": "expired_token"}, interval=5.0).outcome is (
            PollOutcome.FAILED
        )

    def test_a_failure_carries_the_error_it_was_told(self):
        result = classify_poll(
            400,
            {
                "error": "access_denied",
                "error_description": "the user said no",
                "error_uri": "https://e.example.com/denied",
            },
            interval=5.0,
        )
        assert result.error is not None
        assert result.error.error == "access_denied"
        assert result.error.description == "the user said no"
        assert result.error.uri == "https://e.example.com/denied"

    def test_a_failure_leaves_the_interval_alone(self):
        assert classify_poll(400, {"error": "access_denied"}, interval=8.0).interval == 8.0

    def test_an_error_body_that_is_not_an_object_still_fails_cleanly(self):
        result = classify_poll(500, "<html>gateway error</html>", interval=5.0)
        assert result.outcome is PollOutcome.FAILED
        assert result.error is not None
        assert "HTTP 500" in str(result.error)

    def test_an_error_body_naming_no_error_is_an_invalid_response(self):
        result = classify_poll(400, {"detail": "something"}, interval=5.0)
        assert result.error is not None
        assert result.error.error == "invalid_response"


class TestTokenResponses:
    def test_the_access_token_is_returned(self):
        parsed = parse_token_response({"access_token": "t", "token_type": "Bearer"}, now=1000.0)
        assert parsed["access_token"] == "t"

    def test_expires_in_becomes_an_absolute_deadline(self):
        # A duration is only meaningful next to the instant it arrived, and that
        # instant is lost the moment the token is written to a keyring.
        parsed = parse_token_response({"access_token": "t", "expires_in": 3600}, now=1000.0)
        assert parsed["expires_at"] == 4600.0

    def test_the_deadline_is_measured_from_the_now_that_was_passed_in(self):
        first = parse_token_response({"access_token": "t", "expires_in": 60}, now=0.0)
        second = parse_token_response({"access_token": "t", "expires_in": 60}, now=100.0)
        assert first["expires_at"] == 60.0
        assert second["expires_at"] == 160.0

    def test_a_fractional_expires_in_is_honoured(self):
        parsed = parse_token_response({"access_token": "t", "expires_in": 0.5}, now=1.0)
        assert parsed["expires_at"] == 1.5

    def test_no_expiry_when_the_server_names_none(self):
        assert parse_token_response({"access_token": "t"}, now=1000.0)["expires_at"] is None

    def test_a_non_numeric_expires_in_is_ignored(self):
        parsed = parse_token_response({"access_token": "t", "expires_in": "3600"}, now=1000.0)
        assert parsed["expires_at"] is None

    def test_the_refresh_token_and_scope_are_carried(self):
        parsed = parse_token_response(
            {"access_token": "t", "refresh_token": "r", "scope": "a b"}, now=0.0
        )
        assert parsed["refresh_token"] == "r"
        assert parsed["scope"] == "a b"

    def test_a_response_without_a_refresh_token_says_so(self):
        parsed = parse_token_response({"access_token": "t"}, now=0.0)
        assert parsed["refresh_token"] is None
        assert parsed["scope"] is None

    def test_a_non_string_refresh_token_is_dropped(self):
        parsed = parse_token_response({"access_token": "t", "refresh_token": 7}, now=0.0)
        assert parsed["refresh_token"] is None

    def test_a_non_string_scope_is_dropped(self):
        parsed = parse_token_response({"access_token": "t", "scope": ["a", "b"]}, now=0.0)
        assert parsed["scope"] is None

    def test_a_response_with_no_access_token_is_refused(self):
        with pytest.raises(OAuthError, match="no access_token"):
            parse_token_response({"token_type": "Bearer"}, now=0.0)

    def test_an_empty_access_token_is_refused(self):
        with pytest.raises(OAuthError, match="no access_token"):
            parse_token_response({"access_token": ""}, now=0.0)

    def test_a_non_string_access_token_is_refused(self):
        with pytest.raises(OAuthError, match="no access_token"):
            parse_token_response({"access_token": 12345}, now=0.0)

    def test_an_error_response_is_raised_rather_than_parsed(self):
        with pytest.raises(OAuthError, match="invalid_grant") as excinfo:
            parse_token_response(
                {"error": "invalid_grant", "error_description": "code expired"}, now=0.0
            )
        assert excinfo.value.description == "code expired"

    def test_an_error_wins_over_a_token_in_the_same_body(self):
        # A body carrying both is malformed; redeeming the token would be
        # trusting the half that suits us.
        with pytest.raises(OAuthError, match="invalid_client"):
            parse_token_response({"error": "invalid_client", "access_token": "t"}, now=0.0)

    def test_a_response_that_is_not_an_object_is_refused(self):
        with pytest.raises(OAuthError, match="not a JSON object"):
            parse_token_response("access_token=t", now=0.0)


class TestRegistrationRequests:
    def test_the_client_registers_as_public(self):
        # A desktop client cannot keep a secret on the user's machine; claiming
        # otherwise makes the server issue one that is trivially extractable.
        assert registration_body(client_name="jmaplib")["token_endpoint_auth_method"] == "none"

    def test_the_default_grants_are_the_code_flow_and_refresh(self):
        body = registration_body(client_name="jmaplib")
        assert body["grant_types"] == ["authorization_code", "refresh_token"]

    def test_the_grants_can_be_overridden(self):
        body = registration_body(client_name="jmaplib", grant_types=(DEVICE_CODE_GRANT,))
        assert body["grant_types"] == [DEVICE_CODE_GRANT]

    def test_redirect_uris_bring_the_response_type_with_them(self):
        body = registration_body(client_name="jmaplib", redirect_uris=["http://127.0.0.1/cb"])
        assert body["redirect_uris"] == ["http://127.0.0.1/cb"]
        assert body["response_types"] == ["code"]

    def test_a_client_with_no_redirect_declares_no_response_type(self):
        # The device flow has no redirect at all, and declaring `code` for it
        # asks the server to register something this client will never use.
        body = registration_body(client_name="jmaplib")
        assert "redirect_uris" not in body
        assert "response_types" not in body

    def test_a_scope_is_included_when_given(self):
        body = registration_body(client_name="jmaplib", scope="urn:ietf:params:jmap:core")
        assert body["scope"] == "urn:ietf:params:jmap:core"

    def test_no_scope_key_when_none_was_asked_for(self):
        assert "scope" not in registration_body(client_name="jmaplib")


class TestRegisteredClients:
    def test_a_registration_response_parses(self):
        client = RegisteredClient.of(
            {
                "client_id": "c1",
                "client_secret": "s3cret",
                "client_secret_expires_at": 1893456000,
                "registration_access_token": "rat",
                "registration_client_uri": "https://as.example.com/register/c1",
            }
        )
        assert client.client_id == "c1"
        assert client.client_secret == "s3cret"
        assert client.client_secret_expires_at == 1893456000
        assert client.registration_access_token == "rat"
        assert client.registration_client_uri == "https://as.example.com/register/c1"

    def test_an_expiry_of_zero_means_never(self):
        # RFC 7591 §3.2.1 spells "does not expire" as 0. Storing it verbatim
        # makes every secret look like it expired in 1970.
        client = RegisteredClient.of({"client_id": "c1", "client_secret_expires_at": 0})
        assert client.client_secret_expires_at is None

    def test_a_non_integer_expiry_is_dropped(self):
        client = RegisteredClient.of({"client_id": "c1", "client_secret_expires_at": "soon"})
        assert client.client_secret_expires_at is None

    def test_a_public_client_gets_no_secret(self):
        client = RegisteredClient.of({"client_id": "c1"})
        assert client.client_secret is None
        assert client.client_secret_expires_at is None

    def test_a_non_string_secret_is_dropped(self):
        assert RegisteredClient.of({"client_id": "c1", "client_secret": 7}).client_secret is None

    def test_non_string_management_fields_are_dropped(self):
        client = RegisteredClient.of(
            {"client_id": "c1", "registration_access_token": 1, "registration_client_uri": []}
        )
        assert client.registration_access_token is None
        assert client.registration_client_uri is None

    def test_a_response_that_is_not_an_object_is_refused(self):
        with pytest.raises(OAuthError, match="no object"):
            RegisteredClient.of(None)

    def test_an_error_response_is_raised(self):
        with pytest.raises(OAuthError, match="invalid_redirect_uri") as excinfo:
            RegisteredClient.of(
                {"error": "invalid_redirect_uri", "error_description": "loopback only"}
            )
        assert excinfo.value.description == "loopback only"

    def test_a_response_with_no_client_id_is_refused(self):
        with pytest.raises(OAuthError, match="no client_id"):
            RegisteredClient.of({"client_secret": "s"})

    def test_an_empty_client_id_is_refused(self):
        with pytest.raises(OAuthError, match="no client_id"):
            RegisteredClient.of({"client_id": ""})

    def test_a_non_string_client_id_is_refused(self):
        with pytest.raises(OAuthError, match="no client_id"):
            RegisteredClient.of({"client_id": 12345})

    def test_the_repr_does_not_contain_the_secret(self):
        client = RegisteredClient.of({"client_id": "c1", "client_secret": "s3cret"})
        assert "s3cret" not in repr(client)
        assert "c1" in repr(client)


class TestOAuthErrors:
    def test_the_message_joins_the_code_and_the_description(self):
        assert str(OAuthError("invalid_grant", description="expired")) == "invalid_grant: expired"

    def test_the_message_is_the_code_alone_when_there_is_no_description(self):
        assert str(OAuthError("invalid_grant")) == "invalid_grant"

    def test_the_uri_is_kept_for_the_caller(self):
        error = OAuthError("invalid_grant", uri="https://e.example.com/err")
        assert error.uri == "https://e.example.com/err"

    def test_a_state_mismatch_is_an_oauth_error(self):
        assert issubclass(StateMismatchError, OAuthError)


class TestHostileInput:
    """Guards that a cooperative server never exercises.

    Every one of these was a live bug found by testing: each looked correct, and
    each failed open or crashed on input an attacker or a broken server chooses.
    """

    def test_a_non_ascii_state_is_a_mismatch_not_a_crash(self):
        # `secrets.compare_digest` rejects non-ASCII `str` with a TypeError, and
        # the received state is entirely attacker-controlled - so one accented
        # character used to escape every `except OAuthError` around the redirect.
        with pytest.raises(StateMismatchError):
            parse_redirect("http://127.0.0.1/cb?code=a&state=café", expected_state="expected")

    def test_a_matching_non_ascii_state_still_matches(self):
        assert parse_redirect("http://127.0.0.1/cb?code=a&state=café", expected_state="café") == "a"

    def test_an_empty_expected_state_refuses_to_check(self):
        # Comparing "" against a redirect carrying no state at all succeeds, which
        # fails the CSRF guard open. An empty expected value means the flow's
        # state was lost, not that checking is optional.
        with pytest.raises(MissingStateError):
            parse_redirect("http://127.0.0.1/cb?code=abc", expected_state="")

    def test_extra_parameters_cannot_displace_the_security_ones(self):
        # `extra` is for `login_hint` and `prompt`. Letting it win would let a
        # caller silently substitute its own state and PKCE challenge.
        pkce = PKCEPair.generate()
        request = AuthorizationRequest(
            authorization_endpoint="https://auth.example/authorize",
            client_id="c",
            redirect_uri="http://127.0.0.1/cb",
            pkce=pkce,
            state="ours",
            extra={"state": "theirs", "code_challenge": "theirs", "prompt": "consent"},
        )
        params = httpx.QueryParams(request.url().split("?", 1)[1])
        assert params["state"] == "ours"
        assert params["code_challenge"] == pkce.challenge
        assert params["prompt"] == "consent"

    def test_a_null_device_code_is_a_missing_one(self):
        # A presence check passes on JSON null, and `str(None)` then puts the
        # literal "None" on the wire - leaving the client polling forever with a
        # device code that never existed.
        with pytest.raises(OAuthError, match="device_code"):
            DeviceAuthorization.of(
                {"device_code": None, "user_code": "u", "verification_uri": "https://v"}
            )

    def test_a_boolean_expires_in_is_not_a_lifetime(self):
        # `bool` subclasses `int`, so `isinstance(value, int)` admits `true` and
        # would have produced a token expiring one second from now.
        parsed = parse_token_response({"access_token": "t", "expires_in": True}, now=1000.0)
        assert parsed["expires_at"] is None

    def test_a_boolean_interval_falls_back_to_the_default(self):
        authorization = DeviceAuthorization.of(
            {
                "device_code": "d",
                "user_code": "u",
                "verification_uri": "https://v",
                "interval": True,
            }
        )
        assert authorization.interval == DEFAULT_POLL_INTERVAL

    def test_a_boolean_secret_expiry_is_discarded(self):
        registered = RegisteredClient.of({"client_id": "c", "client_secret_expires_at": True})
        assert registered.client_secret_expires_at is None

    def test_oauth_documents_keep_their_snake_case_spelling(self):
        # These are OAuth documents, not JMAP objects. Inheriting JMAP's camelCase
        # alias generator would serialise `authorizationServers`, a spelling
        # neither RFC 9728 nor RFC 8414 has ever used.
        document = ProtectedResourceMetadata.of(
            {"resource": "https://r", "authorization_servers": ["https://a"]}
        )
        assert "authorization_servers" in document.model_dump(by_alias=True)
        assert document.authorization_servers == ["https://a"]

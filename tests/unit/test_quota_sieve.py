"""RFC 9425 Quota and RFC 9661 Sieve models, and their local gates."""

from __future__ import annotations

import pytest

from jmap.capabilities.quota import QUOTA, QUOTA_URN
from jmap.capabilities.sieve import (
    MIN_SIZE_SCRIPT_NAME,
    SIEVE,
    SIEVE_URN,
    SieveAccountCapability,
    SieveCapability,
    check_required_extensions,
    check_script_name,
)
from jmap.capabilities.spec import MethodKind
from jmap.core.errors import CapabilityFieldError
from jmap.models.quota import Quota, QuotaChangesResponse, ResourceType, Scope
from jmap.models.sieve import INVALID_SIEVE, SIEVE_IS_ACTIVE, SieveScript, SieveValidateResponse


class TestQuotaModel:
    def test_the_rfc_example_parses(self):
        quota = Quota.from_wire(
            {
                "id": "2a06df0d-9865-4e74-a92f-74dcc814270e",
                "resourceType": "count",
                "used": 1056,
                "warnLimit": 1600,
                "softLimit": 1800,
                "hardLimit": 2000,
                "scope": "account",
                "name": "bob@example.com",
                "types": ["Mail", "Calendar", "Contact"],
            }
        )
        assert quota.used == 1056
        assert quota.types == ["Mail", "Calendar", "Contact"]

    def test_the_enums_compare_equal_to_the_wire_spellings(self):
        # StrEnum, so comparison works without forcing the field to be an enum -
        # which would fail the whole parse on an unregistered value.
        quota = Quota.from_wire({"scope": "domain", "resourceType": "octets"})
        assert quota.scope == Scope.DOMAIN
        assert quota.resource_type == ResourceType.OCTETS

    def test_an_unregistered_value_does_not_fail_the_parse(self):
        # A server inventing a scope should cost the caller a comparison, not the
        # entire response.
        assert Quota.from_wire({"scope": "tenant"}).scope == "tenant"

    def test_remaining_is_the_headroom(self):
        assert Quota.from_wire({"used": 5, "hardLimit": 12}).remaining == 7

    def test_remaining_floors_at_zero(self):
        # Usage can exceed a limit that was lowered afterwards, and a negative
        # "remaining" reads as a stranger condition than it is.
        assert Quota.from_wire({"used": 20, "hardLimit": 12}).remaining == 0

    def test_remaining_is_unknown_when_either_half_is(self):
        assert Quota.from_wire({"used": 5}).remaining is None
        assert Quota.from_wire({"hardLimit": 5}).remaining is None

    def test_the_limit_predicates_read_the_right_fields(self):
        quota = Quota.from_wire(
            {"used": 1800, "warnLimit": 1600, "softLimit": 1800, "hardLimit": 2000}
        )
        assert quota.is_over_warn_limit is True
        assert quota.is_over_soft_limit is True
        assert quota.is_over_hard_limit is False

    def test_an_unset_limit_is_never_exceeded(self):
        quota = Quota.from_wire({"used": 10**9})
        assert quota.is_over_warn_limit is False
        assert quota.is_over_soft_limit is False

    def test_unknown_usage_exceeds_nothing(self):
        assert Quota.from_wire({"hardLimit": 1}).is_over_hard_limit is False


class TestQuotaChanges:
    def test_updated_properties_is_carried_through(self):
        # A plain ChangesResponse would drop it into extras, losing the one thing
        # this method adds.
        response = QuotaChangesResponse.from_wire(
            {
                "accountId": "u1",
                "oldState": "78540",
                "newState": "78542",
                "hasMoreChanges": False,
                "updatedProperties": ["used"],
                "updated": ["q1"],
            }
        )
        assert response.updated_properties == ["used"]
        assert response.updated == ["q1"]
        assert response.fetch_all_properties is False

    def test_null_means_fetch_everything_not_nothing(self):
        # RFC 9425 §4.3 requires null whenever the server cannot tell what changed,
        # so it is the pessimistic answer. Reading it as "nothing" loses updates.
        response = QuotaChangesResponse.from_wire({"updatedProperties": None})
        assert response.fetch_all_properties is True

    def test_an_absent_field_is_also_pessimistic(self):
        assert QuotaChangesResponse.from_wire({}).fetch_all_properties is True


class TestQuotaSpec:
    def test_there_is_no_set_method(self):
        # Quotas are computed by the server; the absence is declared here so the
        # entity façade genuinely lacks .set.
        assert QUOTA.method("Quota/set") is None
        assert QUOTA.method("Quota/get") is not None

    def test_changes_declares_its_own_response_model(self):
        changes = QUOTA.method("Quota/changes")
        assert changes is not None
        assert changes.kind is MethodKind.CHANGES
        assert changes.response_model is QuotaChangesResponse

    def test_the_urn_is_registered_under_its_attribute(self):
        assert QUOTA.urn == QUOTA_URN
        assert QUOTA.attr == "quota"


class TestSieveModel:
    def test_a_script_is_metadata_plus_a_blob_id(self):
        script = SieveScript.from_wire(
            {"id": "s1", "name": "test1", "isActive": True, "blobId": "S7"}
        )
        assert script.blob_id == "S7"
        assert script.is_active is True

    def test_a_null_name_asks_the_server_to_choose(self):
        # Distinct from omitting it, which says nothing at all.
        assert SieveScript(name=None).to_wire() == {"name": None}

    def test_validate_reports_validity_through_a_nullable_argument(self):
        # Not a method error: the call succeeds either way.
        assert SieveValidateResponse.from_wire({"error": None}).is_valid is True
        assert SieveValidateResponse.from_wire({}).problem is None

    def test_an_invalid_script_carries_the_interpreters_message(self):
        response = SieveValidateResponse.from_wire(
            {"error": {"type": INVALID_SIEVE, "description": "line 4: syntax error"}}
        )
        assert response.is_valid is False
        problem = response.problem
        assert problem is not None
        assert problem.type == INVALID_SIEVE
        assert problem.description == "line 4: syntax error"

    def test_the_destroy_error_type_is_named(self):
        assert SIEVE_IS_ACTIVE == "sieveIsActive"


class TestSieveCapabilityObjects:
    def test_implementation_lives_at_session_level(self):
        assert SieveCapability(implementation="ACME").implementation == "ACME"

    def test_the_limits_live_per_account(self):
        capability = SieveAccountCapability.of(
            {
                "maxSizeScriptName": 512,
                "maxSizeScript": 65536,
                "maxNumberScripts": 5,
                "maxNumberRedirects": None,
                "sieveExtensions": ["fileinto"],
                "notificationMethods": ["mailto"],
                "externalLists": None,
            }
        )
        assert capability.max_size_script == 65536
        assert capability.sieve_extensions == ["fileinto"]
        # Null is meaningful: the extension is unsupported, which is not the same
        # as supporting it with an empty list of schemes.
        assert capability.external_lists is None
        assert capability.notification_methods == ["mailto"]

    def test_an_absent_object_falls_back_to_the_manage_sieve_floor(self):
        assert SieveAccountCapability.of({}).max_size_script_name == MIN_SIZE_SCRIPT_NAME

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        assert SieveAccountCapability.of(7).max_size_script_name == MIN_SIZE_SCRIPT_NAME


class TestScriptNameGate:
    def test_a_legal_name_passes(self):
        check_script_name("my filters", SieveAccountCapability.of({}))

    def test_control_characters_are_refused(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_script_name("two\nlines", SieveAccountCapability.of({}))
        assert excinfo.value.urn == SIEVE_URN

    def test_the_high_control_range_is_refused_too(self):
        # U+007F-U+009F, easy to forget because it is not the ASCII control range.
        with pytest.raises(CapabilityFieldError):
            check_script_name("odd" + chr(0x7F) + "name", SieveAccountCapability.of({}))

    def test_the_unicode_separators_are_refused(self):
        # Written as codepoints: both are invisible in a source file, which is
        # exactly how they end up pasted into a name field.
        for codepoint in (0x2028, 0x2029):
            with pytest.raises(CapabilityFieldError):
                check_script_name("a" + chr(codepoint) + "b", SieveAccountCapability.of({}))

    def test_the_length_limit_counts_octets(self):
        # §1.2.1 gives it in octets and notes 512 is "up to 128 Unicode
        # characters" - so len() is wrong by up to a factor of four.
        capability = SieveAccountCapability.of({"maxSizeScriptName": 8})
        check_script_name("abcdefgh", capability)  # 8 characters, 8 octets
        with pytest.raises(CapabilityFieldError, match="maxSizeScriptName"):
            check_script_name("日本語訳", capability)  # 4 characters, 12 octets


class TestExtensionGate:
    def test_a_supported_extension_passes(self):
        check_required_extensions(
            ("fileinto",), SieveAccountCapability.of({"sieveExtensions": ["fileinto"]})
        )

    def test_a_missing_extension_is_refused(self):
        with pytest.raises(CapabilityFieldError, match="vacation"):
            check_required_extensions(
                ("fileinto", "vacation"),
                SieveAccountCapability.of({"sieveExtensions": ["fileinto"]}),
            )

    def test_matching_is_case_sensitive(self):
        # §1.2.1 says so, and `fileInto` will never match `fileinto` - a mismatch
        # that only surfaces as invalidSieve after the content is already uploaded.
        with pytest.raises(CapabilityFieldError):
            check_required_extensions(
                ("fileInto",), SieveAccountCapability.of({"sieveExtensions": ["fileinto"]})
            )


class TestSieveSpec:
    def test_there_is_no_changes_method(self):
        # RFC 9661 defines none, so this is not a server gap to work around.
        assert SIEVE.method("SieveScript/changes") is None
        assert SIEVE.method("SieveScript/queryChanges") is None

    def test_validate_is_custom_with_its_own_response_model(self):
        validate = SIEVE.method("SieveScript/validate")
        assert validate is not None
        assert validate.kind is MethodKind.CUSTOM
        assert validate.response_model is SieveValidateResponse

    def test_set_declares_both_activation_arguments(self):
        method = SIEVE.method("SieveScript/set")
        assert method is not None
        assert set(method.extra_args) == {
            "onSuccessActivateScript",
            "onSuccessDeactivateScript",
        }

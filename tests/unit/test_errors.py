"""The four JMAP error families (RFC 8620 §3.6).

The distinction these tests pin down is which failures are fatal to a batch and
which are not: request-level errors kill every call, method errors kill one, and
set errors kill one object inside one call.
"""

from __future__ import annotations

import pytest

from jmap.core.errors import (
    URN_LIMIT,
    URN_UNKNOWN_CAPABILITY,
    AuthenticationError,
    BatchTooLargeError,
    CapabilityFieldError,
    CapabilityNotSupportedError,
    JMAPError,
    MethodError,
    RequestError,
    ServerPartialFailError,
    SetError,
    SetFailedError,
    TransportError,
)
from jmap.core.ids import Id


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            TransportError,
            AuthenticationError,
            RequestError,
            MethodError,
            SetFailedError,
            CapabilityNotSupportedError,
            CapabilityFieldError,
            BatchTooLargeError,
        ],
    )
    def test_everything_is_catchable_as_jmap_error(self, cls):
        assert issubclass(cls, JMAPError)

    def test_server_partial_fail_is_a_method_error(self):
        # It is a method error, but the *only* one after which server state may
        # have changed - so retry logic must special-case it.
        assert issubclass(ServerPartialFailError, MethodError)

    def test_set_error_is_a_value_not_an_exception(self):
        # /set routinely half-succeeds; raising would discard the objects that
        # did change.
        assert not issubclass(SetError, Exception)


class TestAuthenticationError:
    def test_carries_the_challenges(self):
        error = AuthenticationError(
            "nope",
            challenges=('Bearer realm="Stalwart Server"', 'Basic realm="Stalwart Server"'),
        )
        assert len(error.challenges) == 2
        assert "Bearer" in error.challenges[0]

    def test_challenges_default_empty(self):
        assert AuthenticationError("nope").challenges == ()


class TestRequestError:
    def test_from_problem_parses_rfc7807(self):
        error = RequestError.from_problem(
            {
                "type": URN_UNKNOWN_CAPABILITY,
                "status": 400,
                "detail": "The Request object used capability 'urn:x:nope'",
            }
        )
        assert error.type == URN_UNKNOWN_CAPABILITY
        assert error.status == 400
        assert "urn:x:nope" in str(error)

    def test_limit_errors_name_the_limit(self):
        # RFC 8620 §3.6.1 requires `limit` on this type; without it a client
        # cannot tell which ceiling it hit.
        error = RequestError.from_problem(
            {"type": URN_LIMIT, "status": 400, "limit": "maxCallsInRequest"}
        )
        assert error.limit == "maxCallsInRequest"

    def test_explicit_status_overrides_the_body(self):
        error = RequestError.from_problem({"type": "about:blank", "status": 400}, status=503)
        assert error.status == 503

    def test_defaults_to_about_blank(self):
        # Stalwart returns bare `about:blank` problems for auth failures.
        error = RequestError.from_problem({"status": 401, "title": "Unauthorized"})
        assert error.type == "about:blank"
        assert str(error) == "Unauthorized [about:blank; HTTP 401]"

    def test_message_prefers_detail_then_title_then_type(self):
        assert str(RequestError("t", detail="d", title="ti")) == "d [t]"
        assert str(RequestError("t", title="ti")) == "ti [t]"
        assert str(RequestError("t")) == "t"

    def test_the_message_carries_the_type_and_status_whatever_the_detail_says(self):
        # A server that fills `detail` with something unhelpful - one echoes the
        # request back into it - must not be able to hide which error this was.
        error = RequestError(
            "urn:ietf:params:jmap:error:limit",
            status=400,
            detail='{"using":["urn:ietf:params:jmap:core"]}',
        )
        assert "urn:ietf:params:jmap:error:limit" in str(error)
        assert "HTTP 400" in str(error)

    def test_the_type_is_not_repeated_when_it_is_all_there_is(self):
        assert str(RequestError("t", status=500)) == "t [HTTP 500]"

    def test_raw_body_is_retained(self):
        body = {"type": "about:blank", "custom": "vendor field"}
        assert RequestError.from_problem(body).raw["custom"] == "vendor field"


class TestMethodError:
    def test_names_the_failing_call(self):
        error = MethodError("unknownMethod", "c3", {})
        assert "unknownMethod" in str(error)
        assert "c3" in str(error)
        assert error.method_call_id == "c3"

    def test_arguments_are_kept(self):
        error = MethodError("invalidArguments", "c0", {"arguments": ["ids"]})
        assert error.arguments == {"arguments": ["ids"]}


class TestSetError:
    def test_from_wire_basic(self):
        error = SetError.from_wire({"type": "forbidden", "description": "no rights"})
        assert error.type == "forbidden"
        assert error.description == "no rights"
        assert error.properties is None
        assert error.existing_id is None

    def test_invalid_properties_carries_the_property_list(self):
        error = SetError.from_wire(
            {"type": "invalidProperties", "properties": ["mailboxIds", "keywords"]}
        )
        assert error.properties == ("mailboxIds", "keywords")

    def test_already_exists_carries_the_existing_id(self):
        error = SetError.from_wire({"type": "alreadyExists", "existingId": "M1"})
        assert error.existing_id == Id("M1")

    def test_unknown_type_defaults(self):
        assert SetError.from_wire({}).type == "unknown"

    def test_repr_is_informative(self):
        assert "overQuota" in repr(SetError("overQuota"))

    def test_wrapping_for_callers_who_opted_into_raising(self):
        wrapped = SetFailedError(SetError("overQuota"), key="draft1")
        assert wrapped.error.type == "overQuota"
        assert "draft1" in str(wrapped)

    def test_wrapping_without_a_key(self):
        assert str(SetFailedError(SetError("overQuota"))) == "overQuota"


class TestClientSideGuards:
    def test_capability_not_supported_names_the_urn(self):
        error = CapabilityNotSupportedError(
            "urn:ietf:params:jmap:calendars",
            advertised=frozenset({"urn:ietf:params:jmap:core"}),
        )
        assert "urn:ietf:params:jmap:calendars" in str(error)
        assert error.advertised == {"urn:ietf:params:jmap:core"}

    def test_capability_field_error_reports_advertised_versus_requested(self):
        error = CapabilityFieldError("urn:ietf:params:jmap:mail", "maxMailboxDepth", 10, 12)
        message = str(error)
        assert "maxMailboxDepth" in message
        assert "10" in message
        assert "12" in message

    def test_batch_too_large_names_every_stuck_call(self):
        # The point of naming them: the user has to break the reference chain,
        # and cannot without knowing which calls form it.
        error = BatchTooLargeError(("c0", "c1", "c2"), 2)
        assert error.limit == 2
        assert error.call_ids == ("c0", "c1", "c2")
        for call_id in ("c0", "c1", "c2"):
            assert call_id in str(error)

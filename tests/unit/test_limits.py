"""Core capability limits and the chunking policy (RFC 8620 §2)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import (
    FIELD_COLLATION_ALGORITHMS,
    KNOWN_COLLATIONS,
    MIN_CALLS_IN_REQUEST,
    MIN_CONCURRENT_REQUESTS,
    MIN_CONCURRENT_UPLOAD,
    MIN_OBJECTS_IN_GET,
    MIN_OBJECTS_IN_SET,
    MIN_SIZE_REQUEST,
    MIN_SIZE_UPLOAD,
    URN_CORE,
    DefaultLimitPolicy,
    InvalidChunkSizeError,
    LimitKey,
    LimitPolicy,
    Limits,
    check_collation,
    check_upload_size,
    chunked,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "stalwart-0.16.17-session-bootstrap.json"

#: The core capability object as captured from Stalwart v0.16.17.
STALWART_CORE = json.loads(FIXTURE.read_text())["capabilities"][URN_CORE]

STALWART_COLLATIONS = ("i;ascii-numeric", "i;ascii-casemap", "i;unicode-casemap")


class TestFromCapability:
    """The Stalwart capture is the reference: these are real advertised values."""

    def test_parses_the_stalwart_capture(self):
        limits = Limits.from_capability(STALWART_CORE)
        assert limits == Limits(
            max_size_upload=50_000_000,
            max_concurrent_upload=4,
            max_size_request=10_000_000,
            max_concurrent_requests=4,
            max_calls_in_request=16,
            max_objects_in_get=500,
            max_objects_in_set=500,
            collation_algorithms=STALWART_COLLATIONS,
        )

    @pytest.mark.parametrize(
        ("wire_name", "attribute"),
        [
            ("maxSizeUpload", "max_size_upload"),
            ("maxConcurrentUpload", "max_concurrent_upload"),
            ("maxSizeRequest", "max_size_request"),
            ("maxConcurrentRequests", "max_concurrent_requests"),
            ("maxCallsInRequest", "max_calls_in_request"),
            ("maxObjectsInGet", "max_objects_in_get"),
            ("maxObjectsInSet", "max_objects_in_set"),
        ],
    )
    def test_each_wire_name_reaches_its_field(self, wire_name, attribute):
        # A distinctive value no fallback could produce, so a typo in either the
        # wire name or the field mapping fails here instead of silently falling back.
        limits = Limits.from_capability({wire_name: 7})
        assert getattr(limits, attribute) == 7

    def test_wire_names_match_the_capture_exactly(self):
        # Guards the "maxSizeRequestObject" trap: every name we read must be a
        # name a real server actually sends.
        read_names = {
            "maxSizeUpload",
            "maxConcurrentUpload",
            "maxSizeRequest",
            "maxConcurrentRequests",
            "maxCallsInRequest",
            "maxObjectsInGet",
            "maxObjectsInSet",
            FIELD_COLLATION_ALGORITHMS,
        }
        assert read_names == set(STALWART_CORE)

    def test_misspelled_field_is_not_read(self):
        limits = Limits.from_capability({"maxSizeRequestObject": 99})
        assert limits.max_size_request == MIN_SIZE_REQUEST

    def test_empty_capability_falls_back_to_rfc_minimums(self):
        assert Limits.from_capability({}) == Limits()

    def test_rfc_minimum_defaults(self):
        limits = Limits()
        assert (limits.max_size_upload, limits.max_concurrent_upload) == (
            MIN_SIZE_UPLOAD,
            MIN_CONCURRENT_UPLOAD,
        )
        assert (limits.max_size_request, limits.max_concurrent_requests) == (
            MIN_SIZE_REQUEST,
            MIN_CONCURRENT_REQUESTS,
        )
        assert limits.max_calls_in_request == MIN_CALLS_IN_REQUEST
        assert (limits.max_objects_in_get, limits.max_objects_in_set) == (
            MIN_OBJECTS_IN_GET,
            MIN_OBJECTS_IN_SET,
        )
        assert limits.collation_algorithms == ()

    def test_partial_capability_keeps_advertised_values(self):
        limits = Limits.from_capability({"maxObjectsInGet": 50})
        assert limits.max_objects_in_get == 50
        assert limits.max_objects_in_set == MIN_OBJECTS_IN_SET

    def test_a_smaller_limit_than_the_rfc_minimum_is_honoured(self):
        # The minimum is only a fallback; a server saying "10" means 10.
        assert Limits.from_capability({"maxCallsInRequest": 10}).max_calls_in_request == 10

    @pytest.mark.parametrize(
        "bad",
        [None, 0, -1, "500", 500.0, True, False, [], {}],
        ids=["null", "zero", "negative", "string", "float", "true", "false", "list", "object"],
    )
    def test_unusable_values_fall_back(self, bad):
        # Zero especially: a chunk size of 0 would make the batch planner loop forever.
        assert Limits.from_capability({"maxObjectsInGet": bad}).max_objects_in_get == (
            MIN_OBJECTS_IN_GET
        )

    def test_unknown_keys_are_ignored(self):
        assert Limits.from_capability({"maxSizeUpload": 1, "somethingElse": 2}) == Limits(
            max_size_upload=1
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["i;octet"], ("i;octet",)),
            ([], ()),
            (None, ()),
            ("i;octet", ()),
            ({"a": "i;octet"}, ()),
            (["i;octet", 7, None, "i;ascii-numeric"], ("i;octet", "i;ascii-numeric")),
        ],
        ids=["one", "empty", "missing", "string", "object", "mixed-types"],
    )
    def test_collation_parsing(self, raw, expected):
        assert Limits.from_capability({FIELD_COLLATION_ALGORITHMS: raw}).collation_algorithms == (
            expected
        )

    def test_collations_keep_server_order(self):
        assert Limits.from_capability(STALWART_CORE).collation_algorithms == STALWART_COLLATIONS


class TestLimitsValueSemantics:
    def test_is_frozen(self):
        limits = Limits()
        with pytest.raises(dataclasses.FrozenInstanceError):
            limits.max_objects_in_get = 1  # type: ignore[misc]  # the point of the test

    def test_has_no_instance_dict(self):
        # slots=True: a typo'd attribute must fail, not create a shadow field.
        assert not hasattr(Limits(), "__dict__")

    def test_equality_is_by_value(self):
        assert Limits.from_capability(STALWART_CORE) == Limits.from_capability(STALWART_CORE)


class TestPermissive:
    def test_never_binds_against_a_real_server(self):
        permissive = Limits.permissive()
        stalwart = Limits.from_capability(STALWART_CORE)
        assert permissive.max_size_upload > stalwart.max_size_upload
        assert permissive.max_objects_in_get > stalwart.max_objects_in_get
        assert permissive.max_objects_in_set > stalwart.max_objects_in_set
        assert permissive.max_calls_in_request > stalwart.max_calls_in_request
        assert permissive.max_size_request > stalwart.max_size_request
        assert permissive.max_concurrent_upload > stalwart.max_concurrent_upload
        assert permissive.max_concurrent_requests > stalwart.max_concurrent_requests

    def test_advertises_the_known_collations(self):
        assert Limits.permissive().collation_algorithms == KNOWN_COLLATIONS

    @pytest.mark.parametrize("collation", KNOWN_COLLATIONS)
    def test_checks_pass_under_it(self, collation):
        limits = Limits.permissive()
        check_collation(collation, limits)
        check_upload_size(10**12, limits)


class TestLimitKey:
    @pytest.mark.parametrize(
        ("key", "wire_name"),
        [
            (LimitKey.GET_OBJECTS, "maxObjectsInGet"),
            (LimitKey.SET_OBJECTS, "maxObjectsInSet"),
            (LimitKey.CALLS_IN_REQUEST, "maxCallsInRequest"),
            (LimitKey.UPLOAD_SIZE, "maxSizeUpload"),
            (LimitKey.REQUEST_SIZE, "maxSizeRequest"),
        ],
    )
    def test_values_are_the_capability_field_names(self, key, wire_name):
        assert key == wire_name
        assert str(key) == wire_name
        # The name the server would report in an `error:limit` response, so it has
        # to be a field the server actually publishes.
        assert wire_name in STALWART_CORE


class TestDefaultLimitPolicy:
    @pytest.fixture
    def policy(self):
        return DefaultLimitPolicy()

    def test_satisfies_the_protocol(self, policy):
        assert isinstance(policy, LimitPolicy)

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            (LimitKey.GET_OBJECTS, 500),
            (LimitKey.SET_OBJECTS, 500),
            (LimitKey.CALLS_IN_REQUEST, 16),
            (LimitKey.UPLOAD_SIZE, 50_000_000),
            (LimitKey.REQUEST_SIZE, 10_000_000),
        ],
    )
    def test_chunk_size_reads_the_matching_limit(self, policy, key, expected):
        assert policy.chunk_size(key, Limits.from_capability(STALWART_CORE)) == expected

    def test_every_key_has_a_ceiling(self, policy):
        # A key with no entry in the ceiling table would raise KeyError here.
        ceilings = {key: policy.chunk_size(key, Limits.permissive()) for key in LimitKey}
        assert ceilings.keys() == set(LimitKey)

    def test_chunk_size_tracks_the_limits_it_is_given(self, policy):
        limits = Limits(max_objects_in_get=3, max_objects_in_set=4)
        assert policy.chunk_size(LimitKey.GET_OBJECTS, limits) == 3
        assert policy.chunk_size(LimitKey.SET_OBJECTS, limits) == 4

    @pytest.mark.parametrize(
        ("key", "chunkable"),
        [
            (LimitKey.GET_OBJECTS, True),
            (LimitKey.CALLS_IN_REQUEST, True),
            (LimitKey.SET_OBJECTS, False),
            (LimitKey.UPLOAD_SIZE, False),
            (LimitKey.REQUEST_SIZE, False),
        ],
    )
    def test_may_chunk(self, policy, key, chunkable):
        assert policy.may_chunk(key) is chunkable

    def test_set_is_never_chunked(self, policy):
        # The whole point of the module: splitting a /set forfeits ifInState
        # atomicity, so the ceiling is reported but the split is refused.
        assert policy.may_chunk(LimitKey.SET_OBJECTS) is False
        assert policy.chunk_size(LimitKey.SET_OBJECTS, Limits()) == MIN_OBJECTS_IN_SET

    def test_is_stateless(self, policy):
        assert not hasattr(policy, "__dict__")


class TestChunked:
    @pytest.mark.parametrize(
        ("items", "size", "expected"),
        [
            ([1, 2, 3, 4], 2, [[1, 2], [3, 4]]),
            ([1, 2, 3, 4, 5], 2, [[1, 2], [3, 4], [5]]),
            ([1, 2, 3], 1, [[1], [2], [3]]),
            ([1, 2, 3], 10, [[1, 2, 3]]),
            ([], 5, []),
            ([1], 1, [[1]]),
        ],
        ids=["exact", "remainder", "size-one", "oversized", "empty", "single"],
    )
    def test_slicing(self, items, size, expected):
        assert [list(chunk) for chunk in chunked(items, size)] == expected

    def test_works_on_a_tuple(self):
        assert [tuple(chunk) for chunk in chunked(("a", "b", "c"), 2)] == [("a", "b"), ("c",)]

    def test_is_lazy(self):
        chunks = chunked([1, 2, 3], 2)
        assert list(chunks) == [[1, 2], [3]]
        # A generator is exhausted once; re-reading must not silently resend work.
        assert list(chunks) == []

    @pytest.mark.parametrize("size", [0, -1, -100])
    def test_rejects_non_positive_sizes(self, size):
        with pytest.raises(InvalidChunkSizeError, match="at least 1"):
            # No iteration here on purpose: the error must be raised by the call
            # itself, not deferred to the first next().
            chunked([1, 2, 3], size)

    def test_error_carries_the_size(self):
        with pytest.raises(InvalidChunkSizeError) as exc_info:
            chunked([], 0)
        assert exc_info.value.size == 0
        assert isinstance(exc_info.value, ValueError)

    @given(
        items=st.lists(st.integers(), max_size=50),
        size=st.integers(min_value=1, max_value=10),
    )
    def test_chunks_partition_the_input(self, items, size):
        chunks = [list(chunk) for chunk in chunked(items, size)]
        assert [item for chunk in chunks for item in chunk] == items
        assert all(len(chunk) == size for chunk in chunks[:-1])
        assert all(0 < len(chunk) <= size for chunk in chunks)


class TestCheckUploadSize:
    @pytest.mark.parametrize("size", [0, 1, 49_999_999, 50_000_000])
    def test_allows_up_to_and_including_the_limit(self, size):
        check_upload_size(size, Limits.from_capability(STALWART_CORE))

    def test_rejects_one_octet_over(self):
        with pytest.raises(CapabilityFieldError) as exc_info:
            check_upload_size(50_000_001, Limits.from_capability(STALWART_CORE))
        error = exc_info.value
        assert error.urn == URN_CORE
        assert error.field == LimitKey.UPLOAD_SIZE
        assert error.advertised == 50_000_000
        assert error.requested == 50_000_001

    def test_message_names_the_field(self):
        with pytest.raises(CapabilityFieldError, match="maxSizeUpload"):
            check_upload_size(1_001, Limits(max_size_upload=1_000))


class TestCheckCollation:
    @pytest.mark.parametrize("collation", STALWART_COLLATIONS)
    def test_advertised_collations_pass(self, collation):
        check_collation(collation, Limits.from_capability(STALWART_CORE))

    @pytest.mark.parametrize(
        "collation",
        ["i;octet", "i;ASCII-numeric", "", "ascii-numeric"],
        ids=["unadvertised", "wrong-case", "empty", "no-prefix"],
    )
    def test_unadvertised_collations_are_rejected(self, collation):
        with pytest.raises(CapabilityFieldError) as exc_info:
            check_collation(collation, Limits.from_capability(STALWART_CORE))
        error = exc_info.value
        assert error.urn == URN_CORE
        assert error.field == FIELD_COLLATION_ALGORITHMS
        assert error.advertised == STALWART_COLLATIONS
        assert error.requested == collation

    def test_a_server_advertising_nothing_rejects_everything(self):
        with pytest.raises(CapabilityFieldError, match="collationAlgorithms"):
            check_collation("i;ascii-casemap", Limits())

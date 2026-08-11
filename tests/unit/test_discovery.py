"""Finding a JMAP server from an address or domain.

No network: the resolver is an argument, so the RFC 2782 ordering and the
fallback chain are testable without DNS.
"""

from __future__ import annotations

from typing import Any

import pytest

from jmap.discovery import (
    DEFAULT_PORT,
    SRV_SERVICE,
    WELL_KNOWN_PATH,
    DiscoveryUnavailableError,
    SRVTarget,
    candidate_urls,
    domain_of,
    lookup_srv,
    order_targets,
    well_known_url,
)


class FakeRecord:
    """One SRV answer, shaped the way dnspython presents them."""

    def __init__(self, target: str, port: int, priority: int, weight: int) -> None:
        self.target = target
        self.port = port
        self.priority = priority
        self.weight = weight


class FakeResolver:
    """Stands in for ``dns.resolver.Resolver``."""

    def __init__(self, records: list[FakeRecord] | None = None, error: Exception | None = None):
        self.records = records or []
        self.error = error
        self.queries: list[tuple[str, str]] = []

    def resolve(self, name: str, kind: str) -> list[FakeRecord]:
        self.queries.append((name, kind))
        if self.error is not None:
            raise self.error
        return self.records


class TestDomainOf:
    def test_an_address_yields_its_domain(self):
        assert domain_of("alice@example.com") == "example.com"

    def test_a_bare_domain_passes_through(self):
        # What a user types when they know their provider but not the address.
        assert domain_of("example.com") == "example.com"

    def test_the_last_at_wins(self):
        # Local parts may legally contain a quoted @.
        assert domain_of('"odd@name"@example.com') == "example.com"

    def test_a_trailing_at_falls_back_to_the_input(self):
        assert domain_of("alice@") == "alice@"


class TestWellKnown:
    def test_the_fallback_url(self):
        assert well_known_url("example.com") == f"https://example.com{WELL_KNOWN_PATH}"


class TestSRVTarget:
    def test_the_default_port_is_omitted_from_the_url(self):
        # A URL carrying :443 compares unequal to the same URL without it in
        # caches, redirect matching and OAuth redirect-URI checks.
        target = SRVTarget(host="jmap.example.net", port=DEFAULT_PORT)
        assert target.session_url == f"https://jmap.example.net{WELL_KNOWN_PATH}"

    def test_a_non_default_port_is_kept(self):
        target = SRVTarget(host="jmap.example.net", port=8443)
        assert target.session_url == f"https://jmap.example.net:8443{WELL_KNOWN_PATH}"


class TestOrdering:
    def test_lowest_priority_first(self):
        targets = [SRVTarget("b", priority=10), SRVTarget("a", priority=1)]
        assert [t.host for t in order_targets(targets)] == ["a", "b"]

    def test_highest_weight_within_a_priority(self):
        targets = [SRVTarget("light", weight=1), SRVTarget("heavy", weight=100)]
        assert [t.host for t in order_targets(targets)] == ["heavy", "light"]

    def test_the_host_breaks_a_full_tie(self):
        # Deterministic ordering matters: a client retrying in order should try
        # the same host first each time rather than reshuffling on every run.
        targets = [SRVTarget("z"), SRVTarget("a")]
        assert [t.host for t in order_targets(targets)] == ["a", "z"]

    def test_priority_outranks_weight(self):
        targets = [
            SRVTarget("heavy-but-late", priority=10, weight=99),
            SRVTarget("light-but-early"),
        ]
        assert order_targets(targets)[0].host == "light-but-early"


class TestLookup:
    def test_the_service_label_is_prefixed(self):
        resolver = FakeResolver()
        lookup_srv("example.com", resolver=resolver)
        assert resolver.queries == [(f"{SRV_SERVICE}.example.com", "SRV")]

    def test_records_become_targets_in_preference_order(self):
        resolver = FakeResolver(
            [
                FakeRecord("backup.example.net.", 443, 20, 0),
                FakeRecord("jmap.example.net.", 8443, 10, 0),
            ]
        )
        targets = lookup_srv("example.com", resolver=resolver)
        assert [t.host for t in targets] == ["jmap.example.net", "backup.example.net"]
        assert targets[0].port == 8443

    def test_the_trailing_dot_is_stripped(self):
        # dnspython returns absolute names; leaving the dot on produces a URL
        # whose host does not match the certificate.
        resolver = FakeResolver([FakeRecord("jmap.example.net.", 443, 0, 0)])
        assert lookup_srv("example.com", resolver=resolver)[0].host == "jmap.example.net"

    def test_no_records_is_an_answer_not_a_failure(self):
        # NXDOMAIN means "use the well-known URL", which is the common case.
        assert lookup_srv("example.com", resolver=FakeResolver()) == []

    def test_a_lookup_error_is_an_answer_too(self):
        # Timeout, SERVFAIL and NoAnswer all mean the same thing to a caller.
        resolver = FakeResolver(error=RuntimeError("SERVFAIL"))
        assert lookup_srv("example.com", resolver=resolver) == []


class TestCandidates:
    def test_srv_targets_come_before_the_fallback(self):
        resolver = FakeResolver([FakeRecord("jmap.example.net.", 443, 0, 0)])
        urls = candidate_urls("alice@example.com", resolver=resolver)
        assert urls == [
            f"https://jmap.example.net{WELL_KNOWN_PATH}",
            f"https://example.com{WELL_KNOWN_PATH}",
        ]

    def test_the_fallback_is_always_present(self):
        # A domain with SRV records may still answer there, and one extra request
        # is cheap against a server that could not be reached any other way.
        assert candidate_urls("alice@example.com", resolver=FakeResolver()) == [
            f"https://example.com{WELL_KNOWN_PATH}"
        ]

    def test_the_fallback_is_not_duplicated(self):
        resolver = FakeResolver([FakeRecord("example.com.", 443, 0, 0)])
        assert candidate_urls("alice@example.com", resolver=resolver) == [
            f"https://example.com{WELL_KNOWN_PATH}"
        ]

    def test_srv_can_be_skipped(self):
        resolver = FakeResolver([FakeRecord("jmap.example.net.", 443, 0, 0)])
        urls = candidate_urls("alice@example.com", resolver=resolver, use_srv=False)
        assert urls == [f"https://example.com{WELL_KNOWN_PATH}"]
        assert resolver.queries == []

    def test_a_missing_dns_package_still_yields_the_fallback(self, monkeypatch):
        # The caller gets something usable; `lookup_srv` is where the missing
        # package is reported, so this must not swallow it there too.
        def unavailable() -> object:
            raise DiscoveryUnavailableError

        monkeypatch.setattr("jmap.discovery._default_resolver", unavailable)
        assert candidate_urls("alice@example.com") == [f"https://example.com{WELL_KNOWN_PATH}"]

    def test_a_missing_dns_package_is_reported_from_lookup(self, monkeypatch):
        # Silently falling through turns a missing package into a mysterious
        # connection failure against a correctly configured domain.
        def unavailable() -> object:
            raise DiscoveryUnavailableError

        monkeypatch.setattr("jmap.discovery._default_resolver", unavailable)
        with pytest.raises(DiscoveryUnavailableError, match="jmaplib\\[discovery\\]"):
            lookup_srv("example.com")


class TestDefaultResolver:
    def test_it_builds_a_resolver_when_dnspython_is_present(self):
        from jmap.discovery import _default_resolver

        assert hasattr(_default_resolver(), "resolve")

    def test_it_raises_when_dnspython_is_absent(self, monkeypatch):
        import builtins

        from jmap.discovery import _default_resolver

        real_import = builtins.__import__

        def no_dns(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("dns"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_dns)
        with pytest.raises(DiscoveryUnavailableError):
            _default_resolver()

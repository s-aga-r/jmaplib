"""The conformance matrix: what a server advertises, against what this build models.

The report exists to avoid overstating support, so the tests are mostly about the
distinctions it refuses to collapse. A capability can be advertised and modelled,
advertised and unknown, modelled and absent, or - the state most likely to be
mistaken for "missing" - advertised, modelled, and deliberately not resolved
because it tracks a draft and the caller did not opt in.

Nothing here touches the network: a Session parsed from a wire dict plus a
Registry is the whole input, which is also the point of the module - the matrix
costs one Session fetch and no method calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.files import FILENODE, FILENODE_URN
from jmap.capabilities.mail import MAIL, MAIL_URN
from jmap.capabilities.mdn import MDN_URN
from jmap.capabilities.registry import Registry
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.testing.conformance import (
    CapabilityRow,
    Conformance,
    analyse,
    method_gaps,
)

if TYPE_CHECKING:
    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.capabilities.spec import CapabilitySpec

#: A URN no registry in this build models, standing in for a vendor extension.
VENDOR_URN = "urn:example:jmap:widgets"


def session(*urns: str, username: str = "alice@example.com") -> Session:
    """A Session advertising exactly ``urns``."""
    return Session.from_wire(
        {
            "username": username,
            "capabilities": {urn: {} for urn in urns},
            "apiUrl": "https://jmap.example.com/jmap/",
        }
    )


def registry_of(*specs: CapabilitySpec) -> Registry:
    registry = Registry()
    for spec in specs:
        registry.register(spec)
    return registry


def analysis(
    advertised: tuple[str, ...],
    modelled: tuple[CapabilitySpec, ...],
    *,
    experimental: bool = False,
    username: str = "alice@example.com",
) -> Conformance:
    """A matrix for a server advertising ``advertised`` against a build knowing
    ``modelled``."""
    resource = session(*advertised, username=username)
    known = registry_of(*modelled)
    return analyse(resource, known.resolve(resource, experimental=experimental), registry=known)


def row_for(report: Conformance, urn: str) -> CapabilityRow:
    return next(row for row in report.rows if row.urn == urn)


def resolved(*advertised: str) -> ActiveCapabilities:
    resource = session(*advertised)
    return registry_of(CORE, MAIL).resolve(resource)


class TestStatus:
    def test_an_advertised_and_modelled_capability_is_supported(self):
        report = analysis((CORE_URN, MAIL_URN), (CORE, MAIL))
        assert row_for(report, MAIL_URN).status == "supported"

    def test_a_draft_capability_the_caller_skipped_says_so(self):
        # The third state, and the one worth keeping separate: the server does
        # offer file nodes, the library does model them, and the caller simply
        # did not pass experimental=True. Reporting it as missing would send
        # someone looking for a server-side feature that is already there.
        report = analysis((CORE_URN, FILENODE_URN), (CORE, FILENODE))
        row = row_for(report, FILENODE_URN)
        assert row.status == "experimental (not opted in)"
        assert (row.advertised, row.known, row.resolved) == (True, True, False)

    def test_opting_in_promotes_it_to_supported(self):
        report = analysis((CORE_URN, FILENODE_URN), (CORE, FILENODE), experimental=True)
        assert row_for(report, FILENODE_URN).status == "supported"

    def test_a_urn_nobody_models_is_advertised_but_not_modelled(self):
        # RFC 8620 §2 lets a server advertise anything; the row exists so the URN
        # is visible rather than dropped, since batch.add can still reach it.
        report = analysis((CORE_URN, VENDOR_URN), (CORE,))
        row = row_for(report, VENDOR_URN)
        assert row.status == "advertised, not modelled"
        assert row.known is False
        assert row.methods == ()
        assert row.reference == ""

    def test_a_capability_the_server_omits_is_not_advertised(self):
        report = analysis((CORE_URN,), (CORE, MAIL))
        row = row_for(report, MAIL_URN)
        assert row.status == "not advertised"
        assert (row.advertised, row.known) == (False, True)
        # Its methods are still known, which is what makes the row useful when
        # someone asks why a feature is unavailable.
        assert "Email/get" in row.methods

    def test_the_reference_and_methods_come_from_the_spec(self):
        row = row_for(analysis((CORE_URN,), (CORE,)), CORE_URN)
        assert row.reference == "RFC 8620"
        assert row.methods == tuple(sorted(method.name for method in CORE.methods))


class TestBuckets:
    def test_the_three_buckets_partition_the_rows(self):
        report = analysis((CORE_URN, VENDOR_URN), (CORE, MAIL))
        assert [row.urn for row in report.supported] == [CORE_URN]
        assert [row.urn for row in report.unknown] == [VENDOR_URN]
        assert [row.urn for row in report.missing] == [MAIL_URN]
        assert len(report.rows) == 3

    def test_a_draft_capability_counts_as_neither_supported_nor_missing(self):
        # It is advertised, so `missing` must not claim the server lacks it, and
        # it is unresolved, so `supported` must not claim the client can call it.
        report = analysis((CORE_URN, FILENODE_URN), (CORE, FILENODE))
        assert [row.urn for row in report.supported] == [CORE_URN]
        assert report.missing == []
        # `unknown` is about the registry, not resolution: this one is modelled.
        assert report.unknown == []

    def test_the_rows_are_the_union_of_both_sides_in_urn_order(self):
        report = analysis((VENDOR_URN, CORE_URN), (CORE, MAIL))
        assert [row.urn for row in report.rows] == sorted([CORE_URN, MAIL_URN, VENDOR_URN])

    def test_only_supported_capabilities_contribute_methods(self):
        # Counting every row's methods would report a mail-less server as
        # offering Email/get.
        report = analysis((CORE_URN,), (CORE, MAIL))
        assert report.method_count() == len(CORE.methods)

    def test_an_empty_matrix_counts_nothing(self):
        assert Conformance().method_count() == 0
        assert Conformance().rows == []


class TestMarkdown:
    def test_it_renders_a_row_per_capability_under_a_table_header(self):
        report = analysis((CORE_URN, MAIL_URN), (CORE, MAIL))
        text = report.markdown()
        assert "| Capability | Status | Spec | Methods |" in text
        assert f"| `{CORE_URN}` | supported | RFC 8620 | {len(CORE.methods)} |" in text
        assert text.endswith("\n")

    def test_the_username_identifies_whose_view_this_is(self):
        # The matrix is per account as well as per server, so a report without
        # the user it was taken as is not reproducible.
        report = analysis((CORE_URN,), (CORE,), username="bob@example.com")
        assert "bob@example.com" in report.markdown()

    def test_a_session_without_a_username_says_so_rather_than_rendering_blank(self):
        assert "unknown user" in Conformance().markdown()

    def test_a_row_with_neither_spec_nor_methods_renders_dashes(self):
        report = analysis((CORE_URN, VENDOR_URN), (CORE,))
        assert f"| `{VENDOR_URN}` | advertised, not modelled | — | — |" in report.markdown()

    def test_the_totals_count_supported_capabilities_and_their_methods(self):
        report = analysis((CORE_URN, MAIL_URN), (CORE, MAIL))
        assert f"**2 capabilities supported**, {report.method_count()} methods" in (
            report.markdown()
        )

    def test_the_extra_sections_appear_when_they_have_content(self):
        report = analysis((CORE_URN, VENDOR_URN), (CORE, MAIL))
        text = report.markdown()
        assert "## Advertised but not modelled" in text
        assert f"- `{VENDOR_URN}`" in text
        assert "## Modelled but not advertised" in text
        assert f"- `{MAIL_URN}` (RFC 8621)" in text

    def test_neither_section_is_rendered_when_both_sides_agree(self):
        # An empty "Advertised but not modelled" heading reads as a finding; a
        # server this build fully models should produce a table and nothing else.
        text = analysis((CORE_URN, MAIL_URN), (CORE, MAIL)).markdown()
        assert "## Advertised but not modelled" not in text
        assert "## Modelled but not advertised" not in text


class TestAnalyse:
    def test_the_username_comes_from_the_session(self):
        report = analysis((CORE_URN,), (CORE,), username="carol@example.com")
        assert report.username == "carol@example.com"

    def test_an_explicit_registry_decides_what_counts_as_modelled(self):
        # Passing a registry that knows only core makes mail unknown even though
        # the default build models it - which is how a caller reports on a
        # cut-down or extended registry of their own.
        report = analysis((CORE_URN, MAIL_URN), (CORE,))
        assert row_for(report, MAIL_URN).status == "advertised, not modelled"

    def test_the_default_registry_is_used_when_none_is_given(self):
        resource = session(CORE_URN, MAIL_URN)
        report = analyse(resource, default_registry().resolve(resource))
        assert row_for(report, MAIL_URN).status == "supported"
        # Everything else this build speaks lands in `missing` rather than being
        # left out of the matrix entirely.
        assert row_for(report, MDN_URN).status == "not advertised"
        assert {row.urn for row in report.rows} >= default_registry().urns


class TestMethodGaps:
    def test_it_names_the_methods_the_server_cannot_offer(self):
        # Method-granular on purpose: RFC 9404 §3.1 has a server advertise a
        # capability while implementing none of its methods, so a skip predicate
        # that asks about the capability overstates what works.
        gaps = method_gaps(resolved(CORE_URN), ["Mailbox/get", "Core/echo", "Email/get"])
        assert gaps == ["Email/get", "Mailbox/get"]

    def test_nothing_expected_is_no_gap(self):
        assert method_gaps(resolved(CORE_URN), []) == []

    def test_a_server_offering_everything_expected_has_no_gaps(self):
        assert method_gaps(resolved(CORE_URN, MAIL_URN), ["Email/get", "Core/echo"]) == []


class TestCapabilityRow:
    def test_a_row_defaults_to_the_least_it_can_claim(self):
        # Constructed directly rather than through analyse, because callers embed
        # rows in their own reports and the defaults must not assert support.
        row = CapabilityRow(urn=VENDOR_URN, advertised=False, known=False, resolved=False)
        assert row.status == "not advertised"
        assert row.reference == ""
        assert row.experimental is False
        assert row.methods == ()

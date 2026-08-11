"""The public surface, pinned.

At 1.0 the names below become a promise. This file is what makes breaking one
deliberate rather than incidental: renaming or removing anything here fails, and
the fix is either to restore it or to change the snapshot on purpose - which
shows up in review as exactly what it is.

Additions are not failures. A snapshot that had to be edited for every new export
would be edited without being read, which is the failure mode this exists to
avoid, so the assertions are subset checks in that direction.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

import jmap

if TYPE_CHECKING:
    from jmap.capabilities.registry import Registry

#: Everything `import jmap` promises. Removing one is a breaking change.
TOP_LEVEL = {
    "SPEC_REVISIONS",
    "AuthenticationError",
    "BatchTooLargeError",
    "CapabilityFieldError",
    "CapabilityNotSupportedError",
    "CreationRef",
    "Id",
    "InvalidIdError",
    "JMAPError",
    "MethodError",
    "RequestError",
    "ServerPartialFailError",
    "SetError",
    "SetFailedError",
    "TransportError",
    "__version__",
    "is_valid_id",
    "parse_id",
}

#: Every capability URN this build claims to speak. A dropped entry means a
#: server that used to work no longer does.
CAPABILITY_URNS = {
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    "urn:ietf:params:jmap:submission",
    "urn:ietf:params:jmap:vacationresponse",
    "urn:ietf:params:jmap:smimeverify",
    "urn:ietf:params:jmap:blob",
    "urn:ietf:params:jmap:quota",
    "urn:ietf:params:jmap:sieve",
    "urn:ietf:params:jmap:websocket",
    "urn:ietf:params:jmap:webpush-vapid",
    "urn:ietf:params:jmap:principals",
    "urn:ietf:params:jmap:principals:owner",
    "urn:ietf:params:jmap:contacts",
    "urn:ietf:params:jmap:mdn",
    "https://www.fastmail.com/dev/contacts",
    "https://cyrusimap.org/ns/jmap/contacts",
    # Experimental: excluded from the SemVer promise, but still pinned - a silent
    # disappearance would be as confusing as a silent rename.
    "urn:ietf:params:jmap:calendars",
    "urn:ietf:params:jmap:calendars:parse",
    "urn:ietf:params:jmap:principals:availability",
    "urn:ietf:params:jmap:filenode",
}

#: Capabilities that track drafts and are therefore outside SemVer.
EXPERIMENTAL_URNS = {
    "urn:ietf:params:jmap:calendars",
    "urn:ietf:params:jmap:calendars:parse",
    "urn:ietf:params:jmap:principals:availability",
    "urn:ietf:params:jmap:filenode",
}


def registry() -> Registry:
    from jmap.defaults import default_registry

    return default_registry()


class TestTopLevel:
    def test_every_promised_name_is_exported(self):
        assert set(jmap.__all__) >= TOP_LEVEL

    def test_every_exported_name_resolves(self):
        # `__all__` is what `from jmap import *` reads, so a name in it that does
        # not exist is an ImportError for someone else, not for us.
        for name in jmap.__all__:
            assert hasattr(jmap, name), name

    def test_all_has_no_duplicates(self):
        # Ordering is ruff's RUF022 to enforce, and its sort is not Python's -
        # asserting `sorted()` here would contradict the linter. Duplicates it
        # does not catch.
        assert len(jmap.__all__) == len(set(jmap.__all__))

    def test_the_version_is_a_release(self):
        assert jmap.__version__.count(".") == 2


class TestCapabilities:
    def test_every_promised_capability_is_registered(self):
        assert registry().urns >= CAPABILITY_URNS

    def test_every_registered_capability_has_a_spec_revision(self):
        # SPEC_REVISIONS is published so downstream users can tell which revision
        # of a draft this build implements. A registered URN missing from it is a
        # capability nobody can pin.
        for urn in registry().urns:
            assert urn in jmap.SPEC_REVISIONS, urn

    def test_experimental_capabilities_are_flagged_as_such(self):
        known = registry()
        flagged = {urn for urn in known.urns if known.specs_for(urn)[0].experimental}
        assert flagged == EXPERIMENTAL_URNS

    def test_no_capability_claims_a_urn_twice_ambiguously(self):
        # Two specs may share a URN only with a `matches` predicate to choose
        # between them; the registry enforces that, and nothing here needs it.
        known = registry()
        for urn in known.urns:
            assert len(known.specs_for(urn)) == 1, urn

    def test_every_method_name_is_type_slash_method(self):
        known = registry()
        for urn in known.urns:
            for method in known.specs_for(urn)[0].methods:
                assert method.name.count("/") == 1, method.name

    def test_two_capabilities_sharing_a_method_describe_it_identically(self):
        # The registry refuses a genuine disagreement about what a method is, but
        # tolerates two capabilities declaring the same one the same way - which
        # the two vendor contacts URNs legitimately do. Checking the whole
        # registry catches a *divergence* at build time rather than at the resolve
        # of whichever server happens to advertise both.
        known = registry()
        seen: dict[str, tuple[str, object]] = {}
        for urn in sorted(known.urns):
            for method in known.specs_for(urn)[0].methods:
                previous = seen.get(method.name)
                if previous is not None:
                    assert previous[1] == method, f"{method.name}: {previous[0]} vs {urn}"
                seen[method.name] = (urn, method)


class TestImportability:
    def test_every_module_imports_cleanly(self):
        # A module that only imports as a side effect of another is a module that
        # breaks the first time someone imports it directly.
        for info in pkgutil.walk_packages(jmap.__path__, prefix="jmap."):
            importlib.import_module(info.name)

    def test_the_package_is_typed(self):
        # py.typed is what makes the annotations visible to a downstream checker.
        # Without it every one of them is invisible and the package reads as Any.
        assert (importlib.resources.files("jmap") / "py.typed").is_file()


class TestSpecRevisions:
    def test_drafts_name_their_revision(self):
        # "draft-ietf-jmap-calendars" without a number cannot be checked against
        # anything; the point of publishing these is that a reader can tell
        # whether the draft has moved.
        for urn, revision in jmap.SPEC_REVISIONS.items():
            if revision.startswith("draft-"):
                assert revision.rsplit("-", 1)[-1].isdigit(), urn

    def test_every_experimental_capability_tracks_a_draft(self):
        for urn in EXPERIMENTAL_URNS:
            assert jmap.SPEC_REVISIONS[urn].startswith("draft-"), urn

    def test_no_stable_capability_claims_a_draft(self):
        stable = set(registry().urns) - EXPERIMENTAL_URNS
        for urn in stable:
            revision = jmap.SPEC_REVISIONS[urn]
            assert not revision.startswith("draft-"), f"{urn} is not marked experimental"

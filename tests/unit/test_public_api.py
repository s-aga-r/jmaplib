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
from typing import TYPE_CHECKING, Any

import pytest

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


#: Wire names carrying two consecutive capitals. These are the ones pydantic's
#: camelCase generator gets wrong - it lowercases all but the first letter of an
#: acronym - and the failure is silent in *both* directions: the value is written
#: under a key no server reads, and a value read back lands in `extra` rather than
#: on the field.
#:
#: To find more, grep the spec texts for a property-shaped token with an internal
#: run of capitals:
#:
#:     grep -ohE '\b[a-z][a-zA-Z0-9]*[A-Z]{2,}[a-zA-Z0-9]*\b' rfc*.txt | sort -u
#:
#: Model field -> the wire name it must serialise to.
ACRONYM_ALIASES = {
    ("jmap.models.mdn", "MDN", "reporting_ua"): "reportingUA",
    ("jmap.models.calendars", "CalendarRights", "may_rsvp"): "mayRSVP",
}


class TestAcronymAliases:
    """Wire names the camelCase generator cannot derive.

    Two of these shipped wrong in 1.0 before being caught. The class of bug is
    worth a standing guard because nothing else detects it: the model validates,
    the request sends, the server accepts it, and the property is simply absent.
    """

    def test_each_acronym_property_keeps_its_spelling(self):
        for (module_name, class_name, field_name), wire in ACRONYM_ALIASES.items():
            model = getattr(importlib.import_module(module_name), class_name)
            alias = model.model_fields[field_name].alias
            assert alias == wire, f"{class_name}.{field_name} serialises as {alias!r}"

    def test_a_value_survives_a_round_trip(self):
        # The half a generated alias breaks silently: reading a correctly-spelled
        # payload leaves the field unset and the value stranded in `extra`.
        from jmap.models.calendars import CalendarRights
        from jmap.models.mdn import MDN

        assert MDN.from_wire({"reportingUA": "joes-pc"}).reporting_ua == "joes-pc"
        assert MDN(reportingUA="joes-pc").to_wire() == {"reportingUA": "joes-pc"}
        assert CalendarRights.from_wire({"mayRSVP": True}).may_rsvp is True
        assert CalendarRights(mayRSVP=True).to_wire() == {"mayRSVP": True}

    def test_the_generator_really_would_get_them_wrong(self):
        # Pins *why* the explicit aliases are needed, so removing one is not
        # mistaken for tidying up a redundant declaration.
        from pydantic.alias_generators import to_camel

        assert to_camel("reporting_ua") == "reportingUa"
        assert to_camel("may_rsvp") == "mayRsvp"


class TestDataclassDefaults:
    """No dataclass field may carry an unhashable default.

    Python 3.11's dataclasses reject one outright, and a ``mappingproxy`` is
    unhashable - so a bare ``MappingProxyType({})`` default makes the package fail
    to *import* on the version ``requires-python`` declares as the floor. 3.12
    relaxed the check to reject only list/dict/set by type, which is why this is
    completely invisible when developing on a newer interpreter.

    It was invisible here for the whole project: CI caught it from the second
    commit onwards and nobody read CI. This test makes the failure reachable from
    the version people actually run tests on.
    """

    def dataclass_fields(self) -> list[tuple[str, Any]]:
        import dataclasses

        found: list[tuple[str, Any]] = []
        for info in pkgutil.walk_packages(jmap.__path__, prefix="jmap."):
            module = importlib.import_module(info.name)
            for name in dir(module):
                candidate = getattr(module, name)
                if not isinstance(candidate, type) or not dataclasses.is_dataclass(candidate):
                    continue
                if candidate.__module__ != info.name:
                    continue  # re-exported; checked where it is defined
                for field in dataclasses.fields(candidate):
                    found.append((f"{info.name}.{name}", field))
        return found

    def test_the_walk_finds_something(self):
        # A guard that silently inspects nothing is worse than no guard.
        assert len(self.dataclass_fields()) > 50

    def test_every_default_is_hashable(self):
        import dataclasses

        for owner, field in self.dataclass_fields():
            if field.default is dataclasses.MISSING:
                continue
            try:
                hash(field.default)
            except TypeError:  # pragma: no cover - the failure this exists to catch
                pytest.fail(
                    f"{owner}.{field.name} defaults to an unhashable "
                    f"{type(field.default).__name__}; Python 3.11 refuses to build the "
                    f"class. Use `field(default_factory=...)`."
                )

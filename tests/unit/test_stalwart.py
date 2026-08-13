"""Stalwart's ``urn:stalwart:jmap`` management dialect.

Two behaviours matter most here. The URN is advertised at *account* level only,
so resolution must go through the union rule or a manageable server reads as
unmanageable. And the method inventory is exactly three shapes per object -
``get``, ``set``, ``query`` - with ``/query`` withheld from singletons, whose
query the server's own wire parser rejects rather than answers.
"""

from __future__ import annotations

from typing import Any

import pytest

from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.registry import Registry
from jmap.capabilities.spec import MethodKind
from jmap.capabilities.stalwart import (
    MANAGEMENT_OBJECTS,
    MANAGEMENT_SINGLETONS,
    STALWART_MANAGEMENT,
    STALWART_URN,
    management_spec,
)
from jmap.core.ids import Id
from jmap.core.limits import LimitKey
from jmap.core.session import Session
from jmap.models.base import JMAPObject


def stalwart_shaped_session() -> Session:
    """A session shaped the way a real Stalwart advertises the dialect:
    account-level only, never in the session map."""
    return Session.from_wire(
        {
            "capabilities": {CORE_URN: {}},
            "accounts": {
                "admin": {
                    "name": "admin@example.com",
                    "accountCapabilities": {CORE_URN: {}, STALWART_URN: {}},
                }
            },
            "primaryAccounts": {STALWART_URN: "admin"},
        }
    )


class TestManagementSpecFactory:
    def test_each_object_gets_the_three_shapes(self):
        spec = management_spec(["Widget"])
        get = spec.method("x:Widget/get")
        update = spec.method("x:Widget/set")
        query = spec.method("x:Widget/query")
        assert get is not None
        assert get.kind is MethodKind.GET
        assert update is not None
        assert update.kind is MethodKind.SET
        assert query is not None
        assert query.kind is MethodKind.QUERY

    def test_nothing_beyond_the_three_shapes_is_declared(self):
        # The server's wire parser accepts get, set and query on a management
        # object and nothing else - there is no /changes to sync from.
        spec = management_spec(["Widget"])
        assert {method.name for method in spec.methods} == {
            "x:Widget/get",
            "x:Widget/set",
            "x:Widget/query",
        }

    def test_a_singleton_gets_no_query(self):
        spec = management_spec(["Widget", "Config"], singletons=["Config"])
        assert spec.method("x:Config/query") is None
        assert spec.method("x:Config/get") is not None
        assert spec.method("x:Config/set") is not None
        data_type = spec.data_type("x:Config")
        assert data_type is not None
        assert data_type.singleton_id == "singleton"

    def test_a_singleton_must_be_one_of_the_objects(self):
        # A typo here would silently drop the object instead of describing it.
        with pytest.raises(ValueError, match="Confg"):
            management_spec(["Config"], singletons=["Confg"])

    def test_set_is_mutating_and_get_is_not(self):
        spec = management_spec(["Widget"])
        update = spec.method("x:Widget/set")
        get = spec.method("x:Widget/get")
        assert update is not None
        assert update.mutating is True
        assert get is not None
        assert get.mutating is False

    def test_the_bulk_methods_declare_the_limit_that_bounds_them(self):
        spec = management_spec(["Widget"])
        get = spec.method("x:Widget/get")
        update = spec.method("x:Widget/set")
        assert get is not None
        assert get.chunk_by is LimitKey.GET_OBJECTS
        assert update is not None
        assert update.chunk_by is LimitKey.SET_OBJECTS

    def test_objects_are_unmodelled_wire_mappings(self):
        # The vocabulary is the server's registry schema, so the objects resolve
        # to JMAPObject and every key stays addressable by its exact wire name.
        spec = management_spec(["Widget"])
        data_type = spec.data_type("x:Widget")
        assert data_type is not None
        assert data_type.model is JMAPObject


class TestDefaultManagementSpec:
    def test_the_urn_and_reference(self):
        assert STALWART_URN == "urn:stalwart:jmap"
        assert STALWART_MANAGEMENT.urn == STALWART_URN
        assert STALWART_MANAGEMENT.reference == "Stalwart v0.16 management API"

    def test_it_is_stable_vendor_surface_with_no_client_attribute(self):
        # No draft to track, and x:-prefixed names are not attribute material -
        # calls go through batch.add like the other builder-less methods.
        assert STALWART_MANAGEMENT.experimental is False
        assert STALWART_MANAGEMENT.attr is None

    def test_the_directory_objects_an_integration_needs_are_declared(self):
        # App-password minting is how a hosting integration gets per-user
        # credentials at all, so these are the load-bearing entries.
        for name in ("Account", "AppPassword", "Domain", "Group", "Role"):
            assert STALWART_MANAGEMENT.method(f"x:{name}/get") is not None
            assert STALWART_MANAGEMENT.method(f"x:{name}/set") is not None
            assert STALWART_MANAGEMENT.method(f"x:{name}/query") is not None

    def test_bootstrap_is_the_singleton(self):
        assert MANAGEMENT_SINGLETONS == ("Bootstrap",)
        assert STALWART_MANAGEMENT.method("x:Bootstrap/query") is None
        data_type = STALWART_MANAGEMENT.data_type("x:Bootstrap")
        assert data_type is not None
        assert data_type.singleton_id == "singleton"

    def test_every_declared_object_is_reachable(self):
        for name in MANAGEMENT_OBJECTS:
            assert STALWART_MANAGEMENT.data_type(f"x:{name}") is not None
            assert STALWART_MANAGEMENT.method(f"x:{name}/get") is not None


class TestResolutionAgainstAStalwartShapedSession:
    def registry(self) -> Registry:
        registry = Registry()
        registry.register(CORE)
        registry.register(STALWART_MANAGEMENT)
        return registry

    def test_the_account_level_urn_resolves_through_the_union_rule(self):
        session = stalwart_shaped_session()
        resolved = self.registry().resolve(session, Id("admin"))
        assert resolved.supports("x:Account/get")
        assert resolved.supports("x:AppPassword/set")

    def test_without_the_account_the_dialect_is_absent(self):
        # The URN never appears in the session-level map, so resolving with no
        # account must not claim it - this is the union rule's contrapositive.
        session = stalwart_shaped_session()
        assert not self.registry().resolve(session).supports("x:Account/get")

    def test_using_derives_the_vendor_urn(self):
        session = stalwart_shaped_session()
        resolved = self.registry().resolve(session, Id("admin"))
        using: Any = resolved.using_for(["x:AppPassword/set"])
        assert using == frozenset({CORE_URN, STALWART_URN})

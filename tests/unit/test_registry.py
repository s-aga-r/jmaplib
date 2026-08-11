"""Capability registration and per-account resolution (RFC 8620 §2).

The behaviour under test is the library's central claim: it reads what a server
advertises and refuses to send anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.registry import (
    ActiveCapabilities,
    ConflictingMethodError,
    DuplicateCapabilityError,
    Registry,
    UnsupportedMethodError,
)
from jmap.capabilities.spec import (
    Capability,
    CapabilitySpec,
    DataTypeSpec,
    MethodKind,
    MethodSpec,
)
from jmap.core.errors import CapabilityNotSupportedError
from jmap.core.ids import Id
from jmap.core.session import Session

MAIL_URN = "urn:ietf:params:jmap:mail"
SMIME_URN = "urn:ietf:params:jmap:smimeverify"
CONTACTS_URN = "urn:ietf:params:jmap:contacts"


def make_session(
    capabilities: dict[str, Any] | None = None,
    accounts: dict[str, Any] | None = None,
) -> Session:
    return Session.from_wire(
        {
            "capabilities": capabilities if capabilities is not None else {CORE_URN: {}},
            "accounts": accounts or {},
            "primaryAccounts": {},
        }
    )


#: A type carrying properties gated by a *different* capability - the shape that
#: forces `using` derivation to look past method names.
EMAIL_TYPE = DataTypeSpec(
    name="Email",
    adds_properties={"smimeStatus": SMIME_URN},
    adds_filter_fields={"smimeVerified": SMIME_URN},
    adds_sort_options={"smimeScore": SMIME_URN},
)

MAIL = CapabilitySpec(
    urn=MAIL_URN,
    attr="mail",
    reference="RFC 8621",
    data_types=(EMAIL_TYPE, DataTypeSpec(name="Thread")),
    methods=(
        MethodSpec(name="Email/get", kind=MethodKind.GET),
        MethodSpec(name="Email/set", kind=MethodKind.SET, mutating=True),
        MethodSpec(name="Thread/get", kind=MethodKind.GET),
    ),
)

MDN = CapabilitySpec(
    urn="urn:ietf:params:jmap:mdn",
    attr="mdn",
    methods=(
        MethodSpec(
            name="MDN/send",
            kind=MethodKind.CUSTOM,
            mutating=True,
            # RFC 9007: MDN/send needs the mail capability too.
            also_requires=frozenset({MAIL_URN}),
        ),
    ),
)


class TestRegistration:
    def test_register_and_lookup(self):
        registry = Registry()
        registry.register(CORE)
        assert CORE_URN in registry
        assert registry.urns == {CORE_URN}
        assert registry.specs_for(CORE_URN) == (CORE,)

    def test_unknown_urn_has_no_specs(self):
        assert Registry().specs_for("urn:nope") == ()

    def test_duplicate_without_a_discriminator_is_rejected(self):
        registry = Registry()
        registry.register(MAIL)
        with pytest.raises(DuplicateCapabilityError, match="already registered"):
            registry.register(MAIL)

    def test_second_flavour_needs_the_first_to_be_discriminated_too(self):
        # A plain spec already registered cannot be disambiguated from later
        # arrivals, so the second registration is refused even though it has a
        # predicate of its own.
        registry = Registry()
        registry.register(CapabilitySpec(urn=CONTACTS_URN))
        with pytest.raises(DuplicateCapabilityError):
            registry.register(CapabilitySpec(urn=CONTACTS_URN, matches=lambda _v: True))

    def test_two_discriminated_flavours_coexist(self):
        registry = Registry()
        modern = CapabilitySpec(
            urn=CONTACTS_URN, attr="contacts", matches=lambda v: "mayCreateAddressBook" in v
        )
        legacy = CapabilitySpec(
            urn=CONTACTS_URN, attr="contacts", matches=lambda v: "mayCreateAddressBook" not in v
        )
        registry.register(modern)
        registry.register(legacy)
        assert len(registry.specs_for(CONTACTS_URN)) == 2


class TestFlavourSelection:
    @pytest.fixture
    def registry(self) -> Registry:
        registry = Registry()
        registry.register(CORE)
        registry.register(
            CapabilitySpec(
                urn=CONTACTS_URN,
                attr="contacts",
                methods=(MethodSpec(name="ContactCard/get", kind=MethodKind.GET),),
                matches=lambda v: "mayCreateAddressBook" in v,
            )
        )
        registry.register(
            CapabilitySpec(
                urn=CONTACTS_URN,
                attr="contacts",
                methods=(MethodSpec(name="Contact/get", kind=MethodKind.GET),),
                matches=lambda v: "mayCreateAddressBook" not in v,
            )
        )
        return registry

    def test_modern_flavour_chosen_from_the_advertised_value(self, registry):
        session = make_session({CORE_URN: {}, CONTACTS_URN: {"mayCreateAddressBook": True}})
        active = registry.resolve(session)
        assert active.supports("ContactCard/get")
        assert not active.supports("Contact/get")

    def test_legacy_flavour_chosen_from_an_empty_value(self, registry):
        session = make_session({CORE_URN: {}, CONTACTS_URN: {}})
        active = registry.resolve(session)
        assert active.supports("Contact/get")
        assert not active.supports("ContactCard/get")

    def test_when_every_flavour_declines_the_urn_is_treated_as_unknown(self):
        # Guessing would mean emitting the wrong object model for every call.
        registry = Registry()
        registry.register(CORE)
        registry.register(CapabilitySpec(urn=CONTACTS_URN, matches=lambda _v: False))
        registry.register(CapabilitySpec(urn=CONTACTS_URN, matches=lambda _v: False))
        active = registry.resolve(make_session({CORE_URN: {}, CONTACTS_URN: {}}))
        assert CONTACTS_URN in active.unknown_urns


class TestResolution:
    @pytest.fixture
    def registry(self) -> Registry:
        registry = Registry()
        registry.register(CORE)
        registry.register(MAIL)
        return registry

    def test_unknown_urns_are_surfaced_not_dropped(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}, "urn:vendor:x": {}}))
        assert active.unknown_urns == {"urn:vendor:x"}
        assert "urn:vendor:x" in active.advertised
        assert "urn:vendor:x" not in active

    def test_account_capabilities_are_unioned_into_the_advertised_set(self, registry):
        # Exactly Stalwart's shape: a URN present only at account level.
        session = make_session(
            {CORE_URN: {}},
            {"a": {"name": "alice", "accountCapabilities": {MAIL_URN: {}}}},
        )
        assert registry.resolve(session, Id("a")).supports("Email/get")
        assert not registry.resolve(session).supports("Email/get")

    def test_experimental_capabilities_need_opt_in(self):
        registry = Registry()
        registry.register(CORE)
        registry.register(
            CapabilitySpec(
                urn="urn:ietf:params:jmap:calendars",
                attr="calendars",
                experimental=True,
                methods=(MethodSpec(name="Calendar/get", kind=MethodKind.GET),),
            )
        )
        session = make_session({CORE_URN: {}, "urn:ietf:params:jmap:calendars": {}})

        default = registry.resolve(session)
        assert not default.supports("Calendar/get")
        assert "urn:ietf:params:jmap:calendars" in default.unknown_urns

        opted_in = registry.resolve(session, experimental=True)
        assert opted_in.supports("Calendar/get")

    def test_two_capabilities_claiming_one_method_is_a_registry_bug(self):
        registry = Registry()
        registry.register(CORE)
        registry.register(MAIL)
        registry.register(
            CapabilitySpec(
                urn="urn:vendor:mail",
                methods=(MethodSpec(name="Email/get", kind=MethodKind.GET),),
            )
        )
        session = make_session({CORE_URN: {}, MAIL_URN: {}, "urn:vendor:mail": {}})
        with pytest.raises(ConflictingMethodError, match="Email/get"):
            registry.resolve(session)

    def test_repr_summarises(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}, "urn:vendor:x": {}}))
        assert "unknown=1" in repr(active)


class TestActiveCapabilities:
    @pytest.fixture
    def active(self) -> ActiveCapabilities:
        registry = Registry()
        registry.register(CORE)
        registry.register(MAIL)
        session = make_session(
            {CORE_URN: {"maxCallsInRequest": 16}, MAIL_URN: {"maxMailboxDepth": 10}}
        )
        return registry.resolve(session)

    def test_limits_come_from_the_core_capability(self, active):
        assert active.limits.max_calls_in_request == 16

    def test_attrs_expose_only_named_capabilities(self, active):
        assert set(active.attrs) == {"core", "mail"}

    def test_attrs_skip_capabilities_with_no_attribute(self):
        registry = Registry()
        registry.register(CORE)
        registry.register(CapabilitySpec(urn=SMIME_URN))
        active = registry.resolve(make_session({CORE_URN: {}, SMIME_URN: {}}))
        assert set(active.attrs) == {"core"}

    def test_method_and_owner_lookup(self, active):
        assert active.method("Email/get").kind is MethodKind.GET
        assert active.owner_of("Email/get").urn == MAIL_URN
        assert active.method("Nope/get") is None
        assert active.owner_of("Nope/get") is None

    def test_data_type_lookup_searches_every_capability(self, active):
        assert active.data_type("Email").name == "Email"
        assert active.data_type("PushSubscription").name == "PushSubscription"
        assert active.data_type("Nope") is None

    def test_push_types_include_types_without_methods(self, active):
        assert {"Email", "Thread", "PushSubscription", "Blob"} <= active.push_types()

    def test_require_passes_for_an_advertised_urn(self, active):
        active.require(MAIL_URN)

    def test_require_raises_locally_for_a_missing_one(self, active):
        with pytest.raises(CapabilityNotSupportedError, match="calendars"):
            active.require("urn:ietf:params:jmap:calendars")


class TestUsingDerivation:
    @pytest.fixture
    def registry(self) -> Registry:
        registry = Registry()
        registry.register(CORE)
        registry.register(MAIL)
        registry.register(MDN)
        registry.register(CapabilitySpec(urn=SMIME_URN))
        return registry

    def test_core_is_always_present(self, registry):
        active = registry.resolve(make_session())
        assert active.using_for([]) == {CORE_URN}

    def test_derived_from_the_methods_used(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}, MAIL_URN: {}}))
        assert active.using_for(["Email/get"]) == {CORE_URN, MAIL_URN}

    def test_method_level_also_requires_is_pulled_in(self, registry):
        # RFC 9007: MDN/send needs the mail capability as well as its own.
        session = make_session({CORE_URN: {}, MAIL_URN: {}, "urn:ietf:params:jmap:mdn": {}})
        active = registry.resolve(session)
        assert active.using_for(["MDN/send"]) == {
            CORE_URN,
            MAIL_URN,
            "urn:ietf:params:jmap:mdn",
        }

    def test_capability_level_requires_is_pulled_in(self):
        registry = Registry()
        registry.register(CORE)
        registry.register(
            CapabilitySpec(
                urn="urn:x:child",
                requires=frozenset({"urn:x:parent"}),
                methods=(MethodSpec(name="Child/get", kind=MethodKind.GET),),
            )
        )
        session = make_session({CORE_URN: {}, "urn:x:child": {}, "urn:x:parent": {}})
        active = registry.resolve(session)
        assert "urn:x:parent" in active.using_for(["Child/get"])

    def test_a_property_can_pull_in_a_capability_with_no_methods(self, registry):
        # smimeverify adds properties but no methods. Leaving it out of `using`
        # loses the properties silently (RFC 8620 §1.8), which is why derivation
        # cannot look at method names alone.
        session = make_session({CORE_URN: {}, MAIL_URN: {}, SMIME_URN: {}})
        active = registry.resolve(session)
        using = active.using_for(["Email/get"], properties=[("Email", "smimeStatus")])
        assert SMIME_URN in using

    def test_a_filter_field_pulls_in_its_capability(self, registry):
        session = make_session({CORE_URN: {}, MAIL_URN: {}, SMIME_URN: {}})
        active = registry.resolve(session)
        using = active.using_for(["Email/get"], filter_fields=[("Email", "smimeVerified")])
        assert SMIME_URN in using

    def test_a_sort_option_pulls_in_its_capability(self, registry):
        session = make_session({CORE_URN: {}, MAIL_URN: {}, SMIME_URN: {}})
        active = registry.resolve(session)
        using = active.using_for(["Email/get"], sort_options=[("Email", "smimeScore")])
        assert SMIME_URN in using

    def test_ungated_properties_add_nothing(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}, MAIL_URN: {}}))
        using = active.using_for(["Email/get"], properties=[("Email", "subject")])
        assert using == {CORE_URN, MAIL_URN}

    def test_properties_on_an_unknown_type_add_nothing(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}, MAIL_URN: {}}))
        assert active.using_for(["Email/get"], properties=[("Nope", "x")]) == {
            CORE_URN,
            MAIL_URN,
        }

    def test_extra_urns_are_unioned(self, registry):
        session = make_session({CORE_URN: {}, MAIL_URN: {}, SMIME_URN: {}})
        active = registry.resolve(session)
        assert SMIME_URN in active.using_for(["Email/get"], extra=frozenset({SMIME_URN}))

    def test_an_unadvertised_extra_raises_rather_than_shipping(self, registry):
        # Stalwart turns one unknown URN into a request-level notRequest, which
        # kills every unrelated call in the batch. Never let it reach the wire.
        active = registry.resolve(make_session({CORE_URN: {}, MAIL_URN: {}}))
        with pytest.raises(CapabilityNotSupportedError, match="calendars"):
            active.using_for(["Email/get"], extra=frozenset({"urn:ietf:params:jmap:calendars"}))

    def test_an_unsupported_method_raises_before_the_wire(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}}))
        with pytest.raises(UnsupportedMethodError, match="Email/get") as excinfo:
            active.using_for(["Email/get"])
        assert excinfo.value.method == "Email/get"

    def test_unsupported_method_is_catchable_as_a_capability_error(self, registry):
        active = registry.resolve(make_session({CORE_URN: {}}))
        with pytest.raises(CapabilityNotSupportedError):
            active.using_for(["Email/get"])


class TestCapabilityToken:
    @dataclass
    class MailValue:
        maxMailboxDepth: int | None = None  # noqa: N815 - mirrors the wire name

    def test_parses_an_advertised_value_into_its_model(self):
        token = Capability(MAIL_URN, self.MailValue)
        assert token.parse({"maxMailboxDepth": 10}).maxMailboxDepth == 10

    def test_repr_names_the_urn(self):
        assert MAIL_URN in repr(Capability(MAIL_URN, self.MailValue))


class TestSpecIndexing:
    def test_methods_and_types_are_indexed_by_name(self):
        method = MAIL.method("Email/get")
        assert method is not None
        assert method.name == "Email/get"
        assert MAIL.method("Nope") is None

        data_type = MAIL.data_type("Thread")
        assert data_type is not None
        assert data_type.name == "Thread"
        assert MAIL.data_type("Nope") is None

    def test_type_name_is_derived_from_the_method_name(self):
        assert MethodSpec(name="Email/get", kind=MethodKind.GET).type_name == "Email"
        assert MethodSpec(name="Core/echo", kind=MethodKind.CUSTOM).type_name == "Core"

    def test_an_empty_spec_indexes_to_nothing(self):
        empty = CapabilitySpec(urn="urn:x:empty")
        assert empty.method("anything") is None
        assert empty.data_type("anything") is None


class TestCoreCapability:
    """The core spec has to express PushSubscription without special-casing."""

    @pytest.fixture
    def active(self) -> ActiveCapabilities:
        registry = Registry()
        registry.register(CORE)
        return registry.resolve(make_session())

    def test_push_subscription_is_not_account_scoped(self, active):
        assert active.method("PushSubscription/get").account_scoped is False
        assert active.method("PushSubscription/set").account_scoped is False

    def test_push_subscription_has_no_state(self, active):
        assert active.method("PushSubscription/set").stateful is False

    def test_push_subscription_hides_url_and_keys(self, active):
        spec = active.data_type("PushSubscription")
        assert spec.never_request_properties == {"url", "keys"}

    def test_echo_is_neither_account_scoped_nor_stateful(self, active):
        echo = active.method("Core/echo")
        assert echo.kind is MethodKind.CUSTOM
        assert not echo.account_scoped
        assert not echo.stateful

    def test_blob_copy_is_mutating_and_takes_blob_ids(self, active):
        blob_copy = active.method("Blob/copy")
        assert blob_copy.mutating
        assert "blobIds" in blob_copy.extra_args

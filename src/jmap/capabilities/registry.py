"""Registering capabilities and resolving them against a session (RFC 8620 §2).

:class:`Registry` is the static table of everything the library can speak.
:class:`ActiveCapabilities` is what one server, one account, actually supports -
produced once at connect and consulted by every gate in the library thereafter.

Three rules are enforced here rather than left to callers:

**Resolution is per account.** ``accountCapabilities`` is not a subset of the
session-level map, it is a second map, and the answer is their union. Stalwart
puts ``urn:stalwart:jmap`` only in the account-level one, so a containment check
gets the wrong answer against a real server. It also means the same connection
can support ``Email/*`` on one account and not another.

**``using`` is derived, then hard-intersected.** Under-declaring degrades
silently (RFC 8620 §1.8: the server behaves as if it implements nothing the
client did not list). Over-declaring is worse - Stalwart rejects the *whole*
request with ``notRequest`` for one unknown URN, destroying every unrelated call
batched alongside it. So the set is computed from what the batch actually uses
and then intersected with what is advertised, and a gap raises locally.

**An advertised URN we do not know is surfaced, not dropped.** It lands in
``unknown_urns`` where a caller can see it and reach it through the raw call
escape hatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.core.errors import CapabilityNotSupportedError
from jmap.core.limits import Limits
from jmap.core.request import CORE_URN

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodSpec
    from jmap.core.ids import Id
    from jmap.core.session import Session


class DuplicateCapabilityError(ValueError):
    """Two specs claim one URN without a way to tell them apart."""

    def __init__(self, urn: str) -> None:
        self.urn = urn
        super().__init__(
            f"{urn} is already registered; a second spec for the same URN needs a "
            f"`matches` predicate so the flavour can be chosen from what the server "
            f"advertises"
        )


class ConflictingMethodError(ValueError):
    """One method name is claimed by two resolved capabilities."""

    def __init__(self, method: str, first: str, second: str) -> None:
        self.method = method
        super().__init__(f"method {method!r} is claimed by both {first} and {second}")


class Registry:
    """The set of capabilities this build of the library can speak."""

    __slots__ = ("_by_urn",)

    def __init__(self) -> None:
        # A list per URN because one URN can have several flavours (RFC 9610
        # ContactCard vs the legacy Contact/ContactGroup model).
        self._by_urn: dict[str, list[CapabilitySpec]] = {}

    def register(self, spec: CapabilitySpec) -> None:
        """Add ``spec``, rejecting an ambiguous duplicate."""
        existing = self._by_urn.setdefault(spec.urn, [])
        if existing and (spec.matches is None or any(s.matches is None for s in existing)):
            raise DuplicateCapabilityError(spec.urn)
        existing.append(spec)

    def __contains__(self, urn: object) -> bool:
        return urn in self._by_urn

    @property
    def urns(self) -> frozenset[str]:
        return frozenset(self._by_urn)

    def specs_for(self, urn: str) -> tuple[CapabilitySpec, ...]:
        return tuple(self._by_urn.get(urn, ()))

    def _choose(self, urn: str, value: Mapping[str, Any]) -> CapabilitySpec | None:
        """Pick the flavour matching what the server advertises."""
        candidates = self._by_urn.get(urn)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        for candidate in candidates:
            if candidate.matches is not None and candidate.matches(value):
                return candidate
        # Every flavour declined. Treating the URN as unknown is deliberate:
        # guessing here means emitting the wrong object model for every call.
        return None

    def resolve(
        self,
        session: Session,
        account_id: Id | None = None,
        *,
        experimental: bool = False,
    ) -> ActiveCapabilities:
        """Work out what ``account_id`` on this server actually supports."""
        advertised = session.advertised_for(account_id)
        resolved: dict[str, CapabilitySpec] = {}
        unknown: set[str] = set()

        for urn in sorted(advertised):
            spec = self._choose(urn, session.capability_value(urn, account_id))
            if spec is None:
                unknown.add(urn)
            elif spec.experimental and not experimental:
                # Known, but the caller has not opted into draft-tracking specs.
                unknown.add(urn)
            else:
                resolved[urn] = spec

        methods: dict[str, tuple[CapabilitySpec, MethodSpec]] = {}
        for spec in resolved.values():
            for method in spec.methods:
                clash = methods.get(method.name)
                if clash is not None:
                    raise ConflictingMethodError(method.name, clash[0].urn, spec.urn)
                methods[method.name] = (spec, method)

        return ActiveCapabilities(
            session=session,
            account_id=account_id,
            advertised=frozenset(advertised),
            specs=resolved,
            unknown_urns=frozenset(unknown),
            methods=methods,
        )


class ActiveCapabilities:
    """What one server and one account support. Every gate consults this."""

    __slots__ = (
        "_methods",
        "account_id",
        "advertised",
        "session",
        "specs",
        "unknown_urns",
    )

    session: Session
    account_id: Id | None
    #: Every URN the server offers for this account, known or not.
    advertised: frozenset[str]
    #: URN -> the spec chosen for it.
    specs: Mapping[str, CapabilitySpec]
    #: Advertised but unrecognised, or recognised but experimental and not opted
    #: into. Reachable through the raw-call escape hatch.
    unknown_urns: frozenset[str]

    def __init__(
        self,
        *,
        session: Session,
        account_id: Id | None,
        advertised: frozenset[str],
        specs: Mapping[str, CapabilitySpec],
        unknown_urns: frozenset[str],
        methods: Mapping[str, tuple[CapabilitySpec, MethodSpec]],
    ) -> None:
        self.session = session
        self.account_id = account_id
        self.advertised = advertised
        self.specs = specs
        self.unknown_urns = unknown_urns
        self._methods = methods

    # -- lookups ------------------------------------------------------------ #
    def __contains__(self, urn: object) -> bool:
        return urn in self.specs

    @property
    def limits(self) -> Limits:
        return Limits.from_capability(self.session.capability_value(CORE_URN, self.account_id))

    @property
    def attrs(self) -> Mapping[str, CapabilitySpec]:
        """Supported capabilities keyed by the attribute the client exposes."""
        return {spec.attr: spec for spec in self.specs.values() if spec.attr is not None}

    def method(self, name: str) -> MethodSpec | None:
        found = self._methods.get(name)
        return found[1] if found is not None else None

    def owner_of(self, method_name: str) -> CapabilitySpec | None:
        found = self._methods.get(method_name)
        return found[0] if found is not None else None

    def data_type(self, name: str) -> DataTypeSpec | None:
        for spec in self.specs.values():
            found = spec.data_type(name)
            if found is not None:
                return found
        return None

    def supports(self, method_name: str) -> bool:
        """Whether this account can call ``method_name`` at all.

        Capability presence does not imply method presence, and the specs say so
        outright: RFC 9404 §3.1 has a server advertise ``urn:ietf:params:jmap:blob``
        with an empty ``supportedTypeNames`` when it implements no ``Blob/lookup``
        at all. Feature checks and test skips have to be method-granular for that
        reason - asking "does this server have the capability?" overstates what
        works.
        """
        return method_name in self._methods

    def push_types(self) -> frozenset[str]:
        """Type names legal in ``PushSubscription.types`` and the EventSource
        ``types`` parameter, including push-only pseudo-types."""
        return frozenset(
            data_type.name for spec in self.specs.values() for data_type in spec.data_types
        )

    # -- `using` derivation ------------------------------------------------- #
    def require(self, urn: str) -> None:
        """Assert ``urn`` is available, raising locally if not."""
        if urn not in self.specs:
            raise CapabilityNotSupportedError(urn, advertised=self.advertised)

    def urn_owning(self, type_name: str) -> str | None:
        """The URN of the capability that defines data type ``type_name``.

        ``None`` for a type this build does not model, which is not an error: a
        server may offer private types, and RFC 9404 §4.3 tells clients to ignore
        names they do not recognise rather than refuse the call.
        """
        for urn, spec in self.specs.items():
            if spec.data_type(type_name) is not None:
                return urn
        return None

    def using_for(
        self,
        method_names: Sequence[str],
        *,
        properties: Iterable[tuple[str, str]] = (),
        filter_fields: Iterable[tuple[str, str]] = (),
        sort_options: Iterable[tuple[str, str]] = (),
        type_names: Iterable[str] = (),
        extra: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        """The ``using`` set for a batch, or a local error naming what is missing.

        ``properties``, ``filter_fields`` and ``sort_options`` take
        ``(type_name, name)`` pairs, because a capability may add properties
        without adding any methods - ``urn:ietf:params:jmap:smimeverify`` is the
        case that forces this, and leaving it out of ``using`` costs you the
        properties with no error at all.

        ``type_names`` covers the other direction: a method *argument* that names
        data types needs each type's own capability in ``using`` too.
        ``Blob/lookup`` is the case, and RFC 9404 §4.3 makes the omission fail as
        ``unknownDataType`` - an error that says nothing about ``using``.
        """
        needed: set[str] = {CORE_URN}

        for name in method_names:
            found = self._methods.get(name)
            if found is None:
                raise UnsupportedMethodError(name, advertised=self.advertised)
            owner, method = found
            needed.add(owner.urn)
            needed |= owner.requires
            needed |= method.also_requires

        for type_name in type_names:
            owning = self.urn_owning(type_name)
            if owning is not None:
                needed.add(owning)

        for type_name, prop in properties:
            needed |= self._urns_adding(type_name, prop, "adds_properties")
        for type_name, condition in filter_fields:
            needed |= self._urns_adding(type_name, condition, "adds_filter_fields")
        for type_name, comparator in sort_options:
            needed |= self._urns_adding(type_name, comparator, "adds_sort_options")

        needed |= extra

        missing = needed - self.advertised
        if missing:
            raise CapabilityNotSupportedError(sorted(missing)[0], advertised=self.advertised)
        return frozenset(needed)

    def _urns_adding(self, type_name: str, name: str, attribute: str) -> set[str]:
        urns: set[str] = set()
        for spec in self.specs.values():
            data_type = spec.data_type(type_name)
            if data_type is None:
                continue
            mapping: Mapping[str, str] = getattr(data_type, attribute)
            urn = mapping.get(name)
            if urn is not None:
                urns.add(urn)
        return urns

    def __repr__(self) -> str:
        return (
            f"ActiveCapabilities(account={self.account_id!r}, "
            f"supported={len(self.specs)}, unknown={len(self.unknown_urns)})"
        )


class UnsupportedMethodError(CapabilityNotSupportedError):
    """A method no resolved capability provides.

    A subclass of :class:`CapabilityNotSupportedError` so one ``except`` catches
    "this server cannot do that", but distinct because the fix differs: a missing
    capability means the server lacks a feature, a missing method often means it
    implements the capability only partially.
    """

    def __init__(self, method: str, *, advertised: frozenset[str] = frozenset()) -> None:
        self.method = method
        super().__init__(
            method,
            advertised=advertised,
            message=f"no advertised capability provides {method!r}",
        )

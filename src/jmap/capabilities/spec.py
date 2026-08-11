"""Capability descriptions as data (RFC 8620 §2).

The library's central claim is that it reads what a server advertises and behaves
accordingly. That only stays true if "what a capability is" lives in one place
and everything else consults it, so a capability is a *value*: three frozen
dataclasses describing its URN, its data types and its methods. Registration,
``using`` derivation, limit chunking, read-only enforcement and push-type
validation all read from these and nothing else.

The alternative - a class per capability with overridable hooks - was rejected
because it turns "which URN owns Email/get?" into a method resolution order
question, and because a third-party capability then has to subclass ours to be
usable. Data has no such constraint: a plugin builds a :class:`CapabilitySpec`
and registers it.

Note that a capability is keyed by URN, not by module. ``urn:...:calendars`` and
``urn:...:calendars:parse`` are separate capabilities that happen to be
described in the same file, and conflating them produces a wrong ``using`` array
against a server that implements one but not the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar

from jmap.models.base import JMAPObject

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jmap.core.limits import LimitKey

CapabilityValueT = TypeVar("CapabilityValueT")

_NO_STRINGS: Final[Mapping[str, str]] = MappingProxyType({})


class MethodKind(StrEnum):
    """Which of the standard method shapes a method follows (RFC 8620 §5).

    ``CUSTOM`` covers everything the standard shapes do not: ``Core/echo``,
    ``Email/import``, ``Email/parse``, ``SearchSnippet/get``, ``MDN/send`` and
    friends. Marking them explicitly is what stops the generic builder from
    offering ``/get`` arguments to a method that has none.
    """

    GET = "get"
    CHANGES = "changes"
    SET = "set"
    QUERY = "query"
    QUERY_CHANGES = "queryChanges"
    COPY = "copy"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """One JMAP method, and the handful of ways it can deviate from the norm."""

    name: str
    kind: MethodKind
    #: Turns the response arguments into a typed result. Travels with the method
    #: so an irregular response shape needs no lookup table at demux time.
    parse: Callable[[Mapping[str, Any]], Any] = dict
    #: Response model to use instead of the one implied by ``kind``. Two cases need
    #: it: a standard shape carrying an extra argument (``Quota/changes`` adds
    #: ``updatedProperties``), and a ``CUSTOM`` method whose response is still a
    #: declared model (``Blob/lookup``). Without it the first silently loses the
    #: extra field to ``extra`` and the second falls back to a raw mapping.
    response_model: type[Any] | None = None
    #: ``False`` for ``PushSubscription/*``, which take no ``accountId``.
    account_scoped: bool = True
    #: ``False`` for ``PushSubscription/*``, which have no state string and so no
    #: ``ifInState``/``oldState``/``newState``.
    stateful: bool = True
    #: Blocked locally when the account is ``isReadOnly``.
    mutating: bool = False
    #: Which limit bounds this method's payload, if any.
    chunk_by: LimitKey | None = None
    #: Extra invocations the server may emit under this call's id (RFC 8621 §7.5
    #: ``onSuccessUpdateEmail``, ``Foo/copy`` with ``onSuccessDestroyOriginal``).
    #: They must be collected beside the result, never merged into it.
    implicit_responses: int = 0
    #: URNs this method needs beyond its owner's, e.g. ``MDN/send`` also requires
    #: ``urn:ietf:params:jmap:mail``.
    also_requires: frozenset[str] = frozenset()
    #: Method-specific arguments beyond the standard shape, mapped to a short
    #: description used in error messages: ``collapseThreads``,
    #: ``expandRecurrences``, ``onDestroyRemoveEmails`` and the rest.
    extra_args: Mapping[str, str] = _NO_STRINGS
    #: Name of an argument that lists JMAP *data type names* whose owning
    #: capabilities must therefore appear in ``using``. ``Blob/lookup`` is the
    #: motivating case: RFC 9404 §4.3 requires the capability defining each
    #: requested type to be in the request, and answers ``unknownDataType``
    #: otherwise - a failure with no hint that ``using`` was the problem.
    type_names_argument: str | None = None

    @property
    def type_name(self) -> str:
        """The data type this method acts on - ``Email`` for ``Email/get``."""
        return self.name.split("/", 1)[0]


@dataclass(frozen=True, slots=True)
class DataTypeSpec:
    """One JMAP data type.

    ``model`` defaults to :class:`~jmap.models.base.JMAPObject`, which is what
    makes an advertised-but-unmodelled capability usable the day a server ships
    it: responses come back as a Mapping over the raw wire dict instead of
    failing to parse. Adding a typed model later is purely additive.
    """

    name: str
    model: type[Any] = JMAPObject
    #: Set for singletons like ``VacationResponse``, whose only id is ``singleton``.
    singleton_id: str | None = None
    #: ``True`` for ``SearchSnippet``, which has no ``id`` property at all.
    identityless: bool = False
    #: Properties the server refuses to return; requesting them earns
    #: ``forbidden`` (``PushSubscription``'s ``url`` and ``keys``).
    never_request_properties: frozenset[str] = frozenset()
    #: What ``/get`` returns when ``properties`` is null, where the spec names a
    #: subset rather than "everything" (``Email``'s default set).
    default_get_properties: tuple[str, ...] | None = None
    #: Property name -> URN that must appear in ``using`` to request it. This is
    #: how a capability that adds *properties but no methods* gets into ``using``
    #: at all; ``urn:ietf:params:jmap:smimeverify`` is the motivating case, and
    #: omitting it degrades silently rather than erroring (RFC 8620 §1.8).
    adds_properties: Mapping[str, str] = _NO_STRINGS
    #: Filter condition name -> URN that gates it.
    adds_filter_fields: Mapping[str, str] = _NO_STRINGS
    #: Comparator property -> URN that gates it.
    adds_sort_options: Mapping[str, str] = _NO_STRINGS
    #: A type that only ever arrives over the push channel and has no methods:
    #: ``EmailDelivery``. It must still be a legal value in
    #: ``PushSubscription.types`` and the EventSource ``types`` parameter.
    push_only: bool = False
    #: Whether the type carries ``shareWith``/``myRights`` (RFC 9670).
    shareable: bool = False


_NO_METHODS: Final[Mapping[str, MethodSpec]] = MappingProxyType({})
_NO_TYPES: Final[Mapping[str, DataTypeSpec]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Everything the library knows about one capability URN."""

    urn: str
    #: Attribute the client exposes it under, e.g. ``mail`` for
    #: ``client.mail.email.get(...)``. Capabilities that add no methods (the
    #: ``:parse`` companions, ``smimeverify``) leave this ``None``.
    attr: str | None = None
    #: Human-readable spec reference, surfaced in errors and the conformance
    #: matrix: ``"RFC 8621 §4.2"``.
    reference: str = ""
    data_types: tuple[DataTypeSpec, ...] = ()
    methods: tuple[MethodSpec, ...] = ()
    #: Model for the session-level capability object, if it has fields.
    session_value: type[Any] | None = None
    #: Model for the per-account capability object, if it has fields.
    account_value: type[Any] | None = None
    #: URNs that must also appear in ``using`` whenever this one does.
    requires: frozenset[str] = frozenset()
    #: Tracking a draft. Excluded from the SemVer promise, and only resolved when
    #: the caller opts in.
    experimental: bool = False
    #: Disambiguates two specs claiming the same URN by inspecting the advertised
    #: capability object - the RFC 9610 ContactCard model and the legacy
    #: Contact/ContactGroup model share ``urn:ietf:params:jmap:contacts``.
    matches: Callable[[Mapping[str, Any]], bool] | None = None
    #: Populated by ``__post_init__``; do not pass.
    methods_by_name: Mapping[str, MethodSpec] = field(default=_NO_METHODS, repr=False)
    types_by_name: Mapping[str, DataTypeSpec] = field(default=_NO_TYPES, repr=False)

    def __post_init__(self) -> None:
        # Indexed once at construction: every request looks methods up by name,
        # and a linear scan per call would be the hot path of the whole library.
        object.__setattr__(
            self, "methods_by_name", MappingProxyType({m.name: m for m in self.methods})
        )
        object.__setattr__(
            self, "types_by_name", MappingProxyType({t.name: t for t in self.data_types})
        )

    def method(self, name: str) -> MethodSpec | None:
        return self.methods_by_name.get(name)

    def data_type(self, name: str) -> DataTypeSpec | None:
        return self.types_by_name.get(name)


class Capability(Generic[CapabilityValueT]):
    """A typed handle on a capability's advertised value object.

    Phantom-typed so ``client.capability(CAP_MAIL).max_mailbox_depth`` infers as
    ``int | None`` rather than dissolving into ``Any`` the moment it leaves the
    session dict.
    """

    __slots__ = ("urn", "value_model")

    urn: str
    value_model: type[CapabilityValueT]

    def __init__(self, urn: str, value_model: type[CapabilityValueT]) -> None:
        self.urn = urn
        self.value_model = value_model

    def parse(self, value: Mapping[str, Any]) -> CapabilityValueT:
        """Validate an advertised capability object into its typed model."""
        return self.value_model(**value)

    def __repr__(self) -> str:
        return f"Capability({self.urn!r})"

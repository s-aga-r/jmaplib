"""Typed method builders, composed from what each data type actually supports.

Six method shapes cover almost all of JMAP, so they are written once and
parameterised by the object model. The interesting part is that a type does not
get all six: ``Thread`` has no ``/query`` and no ``/set`` because threads are
derived rather than stored, ``VacationResponse`` is a singleton with no ``/query``
at all, and ``PushSubscription`` is neither account-scoped nor stateful.

Rather than expose six methods everywhere and fail at run time, the façade class
is *composed from the spec*: :func:`entity_for` picks the mixins matching the
methods the capability declares, so a ``Thread`` façade genuinely has no
``.query`` attribute. The surface matches the server, which is the same principle
the rest of the library follows.

Arguments are snake_case here and camelCase on the wire, and ``UNSET`` keeps the
difference between "not passed" and "explicitly null" - which JMAP needs, because
``ids=None`` means *all records* while omitting ``ids`` is invalid.

Every builder checks its arguments against its signature before queueing the
call (see :mod:`jmap.models.arguments`), so ``ids="m1"`` or ``limit=-5`` raises
where it was written rather than coming back as ``invalidArguments``. An
argument the builder only forwards may be a :class:`~jmap.core.invocation.ResultRef`
instead, as RFC 8620 §3.7 allows. A ``/set`` or ``/copy`` ``create`` takes typed
models as well as plain mappings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Generic, TypeAlias, TypeVar

from pydantic import Field

from jmap.capabilities.spec import MethodKind
from jmap.core.ids import CreationRef, Id
from jmap.core.invocation import Handle, ResultRef
from jmap.models.arguments import Int, UnsignedInt, checked
from jmap.models.base import UNSET, JMAPModel, Unset, omit_unset
from jmap.models.responses import (
    ChangesResponse,
    CopyResponse,
    GetResponse,
    QueryChangesResponse,
    QueryResponse,
    SetResponse,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from jmap.batch import Batch
    from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec

T = TypeVar("T")
_F = TypeVar("_F", bound="Callable[..., Any]")

#: An object to create: the wire mapping, or a typed model, which serialises
#: itself through ``to_wire()``.
Creation = Mapping[str, Any] | JMAPModel


#: A ``create`` or an ``update`` argument, as a ``/set`` builder takes it.
_Changes: TypeAlias = Mapping[str, Creation] | Mapping[str, Mapping[str, Any]]


def wire_objects(changes: _Changes | ResultRef[Any] | Unset | None) -> list[Mapping[str, Any]]:
    """A ``/set``'s objects to create, or patches to apply, as wire mappings.

    For a builder checking what it is given: a back-reference, or nothing
    given, has nothing to check.
    """
    if not isinstance(changes, Mapping):
        return []
    return [
        change.to_wire() if isinstance(change, JMAPModel) else change for change in changes.values()
    ]


class EntityBase(Generic[T]):
    """Shared state for one data type's builders."""

    __slots__ = ("_batch", "_model", "_type_name")

    def __init__(self, batch: Batch, type_name: str, model: type[T]) -> None:
        self._batch = batch
        self._type_name = type_name
        self._model = model

    @property
    def type_name(self) -> str:
        """The data type these builders build calls for - ``Email``."""
        return self._type_name

    def _add(
        self,
        suffix: str,
        arguments: Mapping[str, Any],
        *,
        response_model: type[Any] | None = None,
    ) -> Handle[Any]:
        return self._batch.add(
            f"{self._type_name}/{suffix}", arguments, response_model=response_model
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._type_name!r})"


def _type_name(entity: EntityBase[Any]) -> str:
    return entity.type_name


def builder(method: _F) -> _F:
    """Wrap a builder so its arguments are checked on each call.

    See :func:`~jmap.models.arguments.checked`. A failure is titled with the
    data type and the builder - ``Email.get`` - rather than with the mixin
    that implements it for every type.
    """
    # A function, not a name bound to what checked() returns: that would be a
    # variable whose type is inferred, and the public API declares its types.
    return checked(subject=_type_name)(method)


class Gettable(EntityBase[T]):
    """``Foo/get`` (RFC 8620 §5.1)."""

    __slots__ = ()

    @builder
    def get(
        self,
        *,
        ids: Sequence[Id | CreationRef] | ResultRef[Any] | Unset | None = UNSET,
        properties: Sequence[str] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[GetResponse[T]]:
        """Fetch objects by id.

        ``ids=None`` means *every* record of this type, which is why it must be
        distinguishable from omitting the argument. Some types make that
        expensive, and servers may answer ``requestTooLarge``.
        """
        return self._add("get", omit_unset(ids=ids, properties=properties, **extra))


class Changeable(EntityBase[T]):
    """``Foo/changes`` (RFC 8620 §5.2)."""

    __slots__ = ()

    @builder
    def changes(
        self,
        *,
        since_state: str | ResultRef[Any],
        max_changes: Annotated[UnsignedInt, Field(gt=0)] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[ChangesResponse]:
        """What changed since ``since_state``.

        A server that cannot answer replies ``cannotCalculateChanges``, which
        means resynchronising from scratch - it is a normal outcome after a long
        gap, not a bug. ``max_changes`` must be above zero (RFC 8620 §5.2).
        """
        return self._add(
            "changes", omit_unset(sinceState=since_state, maxChanges=max_changes, **extra)
        )


class Queryable(EntityBase[T]):
    """``Foo/query`` (RFC 8620 §5.5)."""

    __slots__ = ()

    @builder
    def query(
        self,
        *,
        # `filter` mirrors the wire name.
        filter: Mapping[str, Any] | ResultRef[Any] | Unset | None = UNSET,
        sort: Sequence[Mapping[str, Any]] | ResultRef[Any] | Unset | None = UNSET,
        position: Int | ResultRef[Any] | Unset = UNSET,
        anchor: Id | ResultRef[Any] | Unset | None = UNSET,
        anchor_offset: Int | ResultRef[Any] | Unset = UNSET,
        limit: UnsignedInt | ResultRef[Any] | Unset | None = UNSET,
        calculate_total: bool | ResultRef[Any] | Unset = UNSET,
        **extra: Any,
    ) -> Handle[QueryResponse]:
        """Search for ids.

        ``anchor`` and ``position`` are mutually exclusive: RFC 8620 §5.5 says an
        anchor makes ``position`` ignored, so passing both is a silent surprise
        rather than an error, and this rejects it locally instead.
        """
        if not isinstance(anchor, Unset) and anchor is not None and not isinstance(position, Unset):
            raise ValueError(
                "pass either `anchor` or `position`, not both: RFC 8620 §5.5 makes "
                "the server ignore `position` whenever an anchor is given"
            )
        if isinstance(anchor, Unset) and not isinstance(anchor_offset, Unset):
            raise ValueError("`anchor_offset` is ignored without an `anchor`")
        return self._add(
            "query",
            omit_unset(
                filter=filter,
                sort=sort,
                position=position,
                anchor=anchor,
                anchorOffset=anchor_offset,
                limit=limit,
                calculateTotal=calculate_total,
                **extra,
            ),
        )


class QueryChangeable(EntityBase[T]):
    """``Foo/queryChanges`` (RFC 8620 §5.6).

    Its own mixin because not every type that can be queried can say how a
    query changed: SieveScript and the legacy Contact cannot, and a shared mixin
    gave them a ``query_changes`` that could only fail.
    """

    __slots__ = ()

    @builder
    def query_changes(
        self,
        *,
        since_query_state: str | ResultRef[Any],
        # `filter` mirrors the wire name.
        filter: Mapping[str, Any] | ResultRef[Any] | Unset | None = UNSET,
        sort: Sequence[Mapping[str, Any]] | ResultRef[Any] | Unset | None = UNSET,
        max_changes: UnsignedInt | ResultRef[Any] | Unset | None = UNSET,
        up_to_id: Id | ResultRef[Any] | Unset | None = UNSET,
        calculate_total: bool | ResultRef[Any] | Unset = UNSET,
        **extra: Any,
    ) -> Handle[QueryChangesResponse]:
        """How a previous query's results have shifted.

        ``filter`` and ``sort`` must match the original query exactly, or the
        server cannot compute a delta against it.
        """
        return self._add(
            "queryChanges",
            omit_unset(
                sinceQueryState=since_query_state,
                filter=filter,
                sort=sort,
                maxChanges=max_changes,
                upToId=up_to_id,
                calculateTotal=calculate_total,
                **extra,
            ),
        )


class Settable(EntityBase[T]):
    """``Foo/set`` (RFC 8620 §5.3)."""

    __slots__ = ()

    @builder
    def set(
        self,
        *,
        create: Mapping[str, Creation] | ResultRef[Any] | Unset | None = UNSET,
        update: Mapping[str, Mapping[str, Any]] | ResultRef[Any] | Unset | None = UNSET,
        destroy: Sequence[Id | CreationRef] | ResultRef[Any] | Unset | None = UNSET,
        if_in_state: str | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[SetResponse[T]]:
        """Create, update and destroy in one atomic call.

        ``if_in_state`` is worth passing whenever the change depends on what was
        read: it makes the call fail with ``stateMismatch`` rather than applying
        to a state that has moved on, and it is what makes a retry safe.

        ``create`` takes typed models as well as mappings: a model sends the
        fields it was given, and nothing it was not. ``update`` takes
        PatchObjects, not whole objects - see :mod:`jmap.core.patch`.
        """
        return self._add(
            "set",
            omit_unset(
                create=create,
                update=update,
                destroy=destroy,
                ifInState=if_in_state,
                **extra,
            ),
        )


class Copyable(EntityBase[T]):
    """``Foo/copy`` (RFC 8620 §5.4)."""

    __slots__ = ()

    @builder
    def copy(
        self,
        *,
        from_account_id: Id | ResultRef[Any],
        create: Mapping[str, Creation] | ResultRef[Any],
        if_from_in_state: str | ResultRef[Any] | Unset | None = UNSET,
        if_in_state: str | ResultRef[Any] | Unset | None = UNSET,
        on_success_destroy_original: bool | ResultRef[Any] | Unset = UNSET,
        destroy_from_if_in_state: str | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[CopyResponse[T]]:
        """Copy objects from another account.

        Each object in ``create`` must carry the source ``id``. With
        ``on_success_destroy_original`` the server emits an extra ``Foo/set``
        response under this call's id, which the handle keeps in ``extra``.
        """
        return self._add(
            "copy",
            omit_unset(
                fromAccountId=from_account_id,
                create=create,
                ifFromInState=if_from_in_state,
                ifInState=if_in_state,
                onSuccessDestroyOriginal=on_success_destroy_original,
                destroyFromIfInState=destroy_from_if_in_state,
                **extra,
            ),
        )


#: Which mixin provides which method shape.
_MIXINS: dict[MethodKind, type[EntityBase[Any]]] = {
    MethodKind.GET: Gettable,
    MethodKind.CHANGES: Changeable,
    MethodKind.QUERY: Queryable,
    MethodKind.QUERY_CHANGES: QueryChangeable,
    MethodKind.SET: Settable,
    MethodKind.COPY: Copyable,
}

_composed: dict[tuple[str, ...], type[EntityBase[Any]]] = {}


def entity_class(
    kinds: frozenset[MethodKind], custom: tuple[type[EntityBase[Any]], ...] = ()
) -> type[EntityBase[Any]]:
    """Build (and cache) a façade class exposing exactly ``kinds`` plus ``custom``.

    Composing rather than exposing all six keeps the object's surface honest:
    ``Thread`` really has no ``.query``, so misuse is an ``AttributeError`` at the
    call site instead of an ``unknownMethod`` from the server.

    Bespoke builders come *first* in the base list, so a method that shares a name
    with a standard shape overrides it - ``Blob/get`` takes range arguments the
    generic ``/get`` knows nothing about.
    """
    standard = tuple(dict.fromkeys(_MIXINS[kind] for kind in _MIXINS if kind in kinds))
    bases = (*custom, *standard) or (EntityBase,)
    key = tuple(base.__name__ for base in bases)
    cached = _composed.get(key)
    if cached is None:
        cached = type("Entity" + "".join(key), bases, {"__slots__": ()})
        _composed[key] = cached
    return cached


def entity_for(
    batch: Batch,
    spec: CapabilitySpec,
    data_type: DataTypeSpec,
    companions: Sequence[CapabilitySpec] = (),
) -> EntityBase[Any]:
    """The façade for one data type, exposing only the methods it supports.

    ``companions`` are capabilities with no namespace of their own that add
    methods to this type: ``:calendars:parse`` gives ``CalendarEvent`` its
    ``parse``, and ``:principals:availability`` gives ``Principal`` its
    ``get_availability``. Their methods appear here only when the server
    advertises them, like every other method.
    """
    # Local import: `irregular` builds on this module, so importing it at module
    # scope would close a cycle.
    from jmap.api.irregular import CUSTOM_BUILDERS

    methods = [
        method
        for owner in (spec, *companions)
        for method in owner.methods
        if method.type_name == data_type.name
    ]
    kinds = frozenset(method.kind for method in methods if method.kind in _MIXINS)
    custom = tuple(
        dict.fromkeys(
            mixin for method in methods if (mixin := CUSTOM_BUILDERS.get(method.name)) is not None
        )
    )
    return entity_class(kinds, custom)(batch, data_type.name, data_type.model)

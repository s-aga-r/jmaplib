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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from jmap.capabilities.spec import MethodKind
from jmap.models.base import UNSET, Unset, omit_unset

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jmap.batch import Batch
    from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec
    from jmap.core.ids import Id
    from jmap.core.invocation import Handle, ResultRef
    from jmap.models.responses import (
        ChangesResponse,
        CopyResponse,
        GetResponse,
        QueryChangesResponse,
        QueryResponse,
        SetResponse,
    )

T = TypeVar("T")


class EntityBase(Generic[T]):
    """Shared state for one data type's builders."""

    __slots__ = ("_batch", "_model", "_type_name")

    def __init__(self, batch: Batch, type_name: str, model: type[T]) -> None:
        self._batch = batch
        self._type_name = type_name
        self._model = model

    def _add(self, suffix: str, arguments: Mapping[str, Any]) -> Handle[Any]:
        return self._batch.add(f"{self._type_name}/{suffix}", arguments)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._type_name!r})"


class Gettable(EntityBase[T]):
    """``Foo/get`` (RFC 8620 §5.1)."""

    __slots__ = ()

    def get(
        self,
        *,
        ids: Sequence[Id] | ResultRef[Any] | Unset | None = UNSET,
        properties: Sequence[str] | Unset | None = UNSET,
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

    def changes(
        self,
        *,
        since_state: str,
        max_changes: int | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[ChangesResponse]:
        """What changed since ``since_state``.

        A server that cannot answer replies ``cannotCalculateChanges``, which
        means resynchronising from scratch - it is a normal outcome after a long
        gap, not a bug.
        """
        return self._add(
            "changes", omit_unset(sinceState=since_state, maxChanges=max_changes, **extra)
        )


class Queryable(EntityBase[T]):
    """``Foo/query`` and ``Foo/queryChanges`` (RFC 8620 §5.5, §5.6)."""

    __slots__ = ()

    def query(
        self,
        *,
        filter: Mapping[str, Any] | Unset | None = UNSET,  # `filter` mirrors the wire name
        sort: Sequence[Mapping[str, Any]] | Unset | None = UNSET,
        position: int | Unset = UNSET,
        anchor: Id | Unset | None = UNSET,
        anchor_offset: int | Unset = UNSET,
        limit: int | Unset | None = UNSET,
        calculate_total: bool | Unset = UNSET,
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

    def query_changes(
        self,
        *,
        since_query_state: str,
        filter: Mapping[str, Any] | Unset | None = UNSET,  # `filter` mirrors the wire name
        sort: Sequence[Mapping[str, Any]] | Unset | None = UNSET,
        max_changes: int | Unset | None = UNSET,
        up_to_id: Id | Unset | None = UNSET,
        calculate_total: bool | Unset = UNSET,
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

    def set(
        self,
        *,
        create: Mapping[str, Any] | Unset | None = UNSET,
        update: Mapping[str, Mapping[str, Any]] | Unset | None = UNSET,
        destroy: Sequence[Id] | Unset | None = UNSET,
        if_in_state: str | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[SetResponse[T]]:
        """Create, update and destroy in one atomic call.

        ``if_in_state`` is worth passing whenever the change depends on what was
        read: it makes the call fail with ``stateMismatch`` rather than applying
        to a state that has moved on, and it is what makes a retry safe.

        ``update`` takes PatchObjects, not whole objects - see
        :mod:`jmap.core.patch`.
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

    def copy(
        self,
        *,
        from_account_id: Id,
        create: Mapping[str, Any],
        if_from_in_state: str | Unset | None = UNSET,
        if_in_state: str | Unset | None = UNSET,
        on_success_destroy_original: bool | Unset = UNSET,
        destroy_from_if_in_state: str | Unset | None = UNSET,
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
    MethodKind.QUERY_CHANGES: Queryable,
    MethodKind.SET: Settable,
    MethodKind.COPY: Copyable,
}

_composed: dict[tuple[str, ...], type[EntityBase[Any]]] = {}


def entity_class(kinds: frozenset[MethodKind]) -> type[EntityBase[Any]]:
    """Build (and cache) a façade class exposing exactly ``kinds``.

    Composing rather than exposing all six keeps the object's surface honest:
    ``Thread`` really has no ``.query``, so misuse is an ``AttributeError`` at the
    call site instead of an ``unknownMethod`` from the server.
    """
    bases = tuple(dict.fromkeys(_MIXINS[kind] for kind in _MIXINS if kind in kinds)) or (
        EntityBase,
    )
    key = tuple(sorted(base.__name__ for base in bases))
    cached = _composed.get(key)
    if cached is None:
        cached = type("Entity" + "".join(key), bases, {"__slots__": ()})
        _composed[key] = cached
    return cached


def entity_for(batch: Batch, spec: CapabilitySpec, data_type: DataTypeSpec) -> EntityBase[Any]:
    """The façade for one data type, exposing only the methods it supports."""
    kinds = frozenset(
        method.kind
        for method in spec.methods
        if method.type_name == data_type.name and method.kind in _MIXINS
    )
    return entity_class(kinds)(batch, data_type.name, data_type.model)

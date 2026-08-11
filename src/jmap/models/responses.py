"""The six standard response shapes (RFC 8620 §5).

Every JMAP data type answers ``/get``, ``/changes``, ``/set``, ``/query``,
``/queryChanges`` and ``/copy`` with the same shapes, so they are modelled once
and parameterised by the object type. That is what lets ``Email/get`` and
``Mailbox/get`` share a single builder while still returning the right model.

The ``/set`` shape is the one worth reading closely: it half-succeeds by design.
``created`` and ``notCreated`` are both populated in the same response, so the
errors are values on the result rather than exceptions - raising would discard
the objects that did change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from pydantic import Field

from jmap.core.errors import SetError
from jmap.models.base import JMAPModel

T = TypeVar("T")


def parse_set_errors(raw: Mapping[str, Any]) -> dict[str, SetError]:
    """Turn a ``notCreated``-style map into :class:`SetError` values."""
    return {key: SetError.from_wire(value) for key, value in raw.items()}


class GetResponse(JMAPModel, Generic[T]):
    """``Foo/get`` (RFC 8620 §5.1).

    ``not_found`` is not an error: asking for an id that no longer exists is a
    normal outcome of acting on a stale list, and the ids that *did* resolve are
    still in ``items``.
    """

    account_id: str | None = None
    #: Opaque type-level state, for feeding ``/changes``.
    state: str | None = None
    #: The wire name is ``list``; a field of that name would shadow the builtin
    #: inside the class body, breaking every annotation after it.
    items: list[T] = Field(default_factory=lambda: [], alias="list")
    not_found: list[str] = Field(default_factory=list)


class ChangesResponse(JMAPModel):
    """``Foo/changes`` (RFC 8620 §5.2).

    ``has_more_changes`` means the server truncated at ``maxChanges``; call again
    from ``new_state`` until it is false, or the tail is silently lost.
    """

    account_id: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    has_more_changes: bool = False
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    destroyed: list[str] = Field(default_factory=list)


class SetResponse(JMAPModel, Generic[T]):
    """``Foo/set`` (RFC 8620 §5.3).

    Half-success is the normal case, so the per-object failures are values here
    rather than exceptions.
    """

    account_id: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    #: Creation id -> the created object, carrying the server-assigned id.
    created: dict[str, T] = Field(default_factory=dict)
    #: Id -> the object, or null when the server changed nothing beyond what was
    #: asked. A null value is a success, not a failure.
    updated: dict[str, T | None] = Field(default_factory=dict)
    destroyed: list[str] = Field(default_factory=list)
    #: Raw ``SetError`` objects, keyed by creation id / object id. Kept as wire
    #: dicts because :class:`~jmap.core.errors.SetError` lives in the I/O-free
    #: kernel and giving it a pydantic schema would make `core` depend on
    #: pydantic. Use the ``*_errors`` accessors for the interpreted view.
    not_created: dict[str, dict[str, Any]] = Field(default_factory=dict)
    not_updated: dict[str, dict[str, Any]] = Field(default_factory=dict)
    not_destroyed: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return bool(self.not_created or self.not_updated or self.not_destroyed)

    @property
    def creation_errors(self) -> dict[str, SetError]:
        """``notCreated`` as typed errors."""
        return parse_set_errors(self.not_created)

    @property
    def update_errors(self) -> dict[str, SetError]:
        """``notUpdated`` as typed errors."""
        return parse_set_errors(self.not_updated)

    @property
    def destroy_errors(self) -> dict[str, SetError]:
        """``notDestroyed`` as typed errors."""
        return parse_set_errors(self.not_destroyed)

    def created_id(self, creation_id: str) -> str | None:
        """The server-assigned id for one creation, or ``None`` if it failed."""
        created = self.created.get(creation_id)
        if created is None:
            return None
        identifier = getattr(created, "id", None)
        return str(identifier) if identifier is not None else None


class QueryResponse(JMAPModel):
    """``Foo/query`` (RFC 8620 §5.5).

    ``can_calculate_changes`` is worth checking before storing ``query_state``:
    when it is false the server cannot answer ``/queryChanges`` for this query,
    and the only way to refresh is to run the query again.
    """

    account_id: str | None = None
    query_state: str | None = None
    can_calculate_changes: bool = False
    position: int = 0
    ids: list[str] = Field(default_factory=list)
    total: int | None = None
    limit: int | None = None


class AddedItem(JMAPModel):
    """One insertion in a ``/queryChanges`` result, with its index."""

    id: str | None = None
    index: int | None = None


class QueryChangesResponse(JMAPModel):
    """``Foo/queryChanges`` (RFC 8620 §5.6).

    Apply ``removed`` before ``added``: the indices in ``added`` describe the
    list *after* the removals, so doing it the other way round puts items in the
    wrong places.
    """

    account_id: str | None = None
    old_query_state: str | None = None
    new_query_state: str | None = None
    total: int | None = None
    removed: list[str] = Field(default_factory=list)
    added: list[AddedItem] = Field(default_factory=lambda: [])


class CopyResponse(JMAPModel, Generic[T]):
    """``Foo/copy`` (RFC 8620 §5.4)."""

    from_account_id: str | None = None
    account_id: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    created: dict[str, T] = Field(default_factory=dict)
    not_created: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def creation_errors(self) -> dict[str, SetError]:
        """``notCreated`` as typed errors."""
        return parse_set_errors(self.not_created)

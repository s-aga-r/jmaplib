"""Where a client remembers how far it has synchronised.

JMAP hands out opaque ``state`` strings and expects them back on the next
``Foo/changes``. Keeping one across process restarts is what turns "download
everything again" into "download what changed", so it has to be *persisted* -
which is the application's business, not this library's.

So the library defines the seam and ships only an in-memory implementation. A
real one is a dict-shaped table; the docstring on :class:`StateStore` says what it
has to guarantee.

The keys are structured rather than opaque so an application can reason about
them: ``<accountId>/<TypeName>`` for a type's state,
``<accountId>/<TypeName>/query/<hash>`` for a query's. That matters when an
account is removed and its rows should go with it.

An account id is unique only on its own server, so a store shared between
servers needs a namespace in front: ``<namespace>/<accountId>/<TypeName>``.
Without one, two servers that both call an account ``a`` file their cursors
under one key, and each resumes from the other's state - which fails, or, where
both servers mint states the same way, quietly skips changes.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from jmap.sync.query import QuerySpec

#: Separates the parts of a state key. Chosen because JMAP ids cannot contain it
#: (RFC 8620 §1.2 restricts them to ``A-Za-z0-9_-``), so keys stay unambiguous.
SEPARATOR = "/"


def _prefix(namespace: str) -> str:
    if SEPARATOR in namespace:
        raise ValueError(
            f"a namespace may not contain {SEPARATOR!r}, which separates the parts of "
            f"a key: {namespace!r}"
        )
    return f"{namespace}{SEPARATOR}" if namespace else ""


def type_key(account_id: str, type_name: str, *, namespace: str = "") -> str:
    """The key under which a data type's ``/changes`` state is remembered.

    ``namespace`` goes in front, for a store shared between servers - see the
    module docstring.
    """
    return f"{_prefix(namespace)}{account_id}{SEPARATOR}{type_name}"


def query_key(spec: QuerySpec, *, namespace: str = "") -> str:
    """The key for one query's ``queryState``.

    Includes a digest of the filter and sort, because a different filter is a
    different result set and must not share a cursor. The digest must be
    *stable across processes* - a persisted cursor is the entire point of a
    :class:`StateStore` - which rules out ``hash()``: string hashing is salted
    per process, so every restart would compute a different key, orphan the
    stored cursor, and silently degrade each run to a full re-query.
    """
    material = "\x1f".join((spec.filter_key, spec.sort_key, "1" if spec.collapse_threads else "0"))
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return (
        f"{_prefix(namespace)}{spec.account_id}{SEPARATOR}{spec.type_name}"
        f"{SEPARATOR}query{SEPARATOR}{digest}"
    )


@runtime_checkable
class StateStore(Protocol):
    """Persistence for sync cursors.

    Two guarantees are required of an implementation:

    * a value written by :meth:`set` is readable by :meth:`get` afterwards, in
      this process and any later one;
    * :meth:`set` is durable *before* it returns, because the caller treats a
      successful write as "these changes are accounted for". Losing the write
      after acting on the changes means acting on them twice.

    Nothing here is async: writes are small and infrequent - one per sync round,
    not one per record - so an implementation that blocks briefly is fine, and the
    seam stays usable from both client shells.
    """

    def get(self, key: str) -> str | None:
        """The stored state, or ``None`` if this key has never been synced."""
        ...

    def set(self, key: str, state: str) -> None:
        """Durably record ``state`` for ``key``."""
        ...

    def delete(self, key: str) -> None:
        """Forget ``key``, forcing a full resync next time."""
        ...


class InMemoryStateStore:
    """A :class:`StateStore` that forgets everything on exit.

    The default, and the right choice for a script that syncs once. Anything
    long-lived wants a real store - otherwise every start is a full download.
    """

    __slots__ = ("_states",)

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._states: dict[str, str] = dict(initial or {})

    def get(self, key: str) -> str | None:
        return self._states.get(key)

    def set(self, key: str, state: str) -> None:
        self._states[key] = state

    def delete(self, key: str) -> None:
        self._states.pop(key, None)

    def snapshot(self) -> dict[str, str]:
        """Everything held, for handing to a persistent store or a later run."""
        return dict(self._states)

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, key: object) -> bool:
        return key in self._states

    def __repr__(self) -> str:
        return f"InMemoryStateStore({len(self._states)} keys)"

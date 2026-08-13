"""Following ``Foo/changes`` until caught up (RFC 8620 §5.2).

``Foo/changes`` returns a *page* of changes, not all of them: ``hasMoreChanges``
means the server truncated at ``maxChanges`` and there is more to collect from the
new state. A client that reads one page and stops silently loses the tail, and
because the state string still advances it will never notice.

The other outcome that must not be swallowed is ``cannotCalculateChanges``. It is
not a failure - it is the server saying the cursor is too old to be useful, and
the only correct response is to throw the local cache away and re-download. A
client that retries the same call gets the same error forever.

Both are modelled as values here rather than left implicit: :class:`ChangeSet`
accumulates the pages, and :class:`ResyncRequiredError` is raised the moment the server
says the cursor is dead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jmap.core.errors import JMAPError, MethodError
from jmap.core.ids import Id  # noqa: TC001 - annotated attribute
from jmap.sync.state import type_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jmap.client import JMAPClient
    from jmap.models.responses import ChangesResponse
    from jmap.sync.state import StateStore

#: RFC 8620 §5.2. The cursor is too old; the local cache must be discarded.
CANNOT_CALCULATE_CHANGES = "cannotCalculateChanges"


class StuckChangeStreamError(JMAPError):
    """The server claims more changes but its state cursor is not moving.

    RFC 8620 §5.2 requires ``newState`` to differ from ``sinceState`` whenever
    ``hasMoreChanges`` is true. A server violating that would loop
    :meth:`ChangeStream.pages` forever - and :meth:`ChangeStream.catch_up`
    accumulates every page, so the loop is also unbounded memory.
    """

    def __init__(self, type_name: str, state: str) -> None:
        self.type_name = type_name
        self.state = state
        super().__init__(
            f"{type_name}/changes reported hasMoreChanges without advancing from "
            f"state {state!r}; following it would loop forever"
        )


class ResyncRequiredError(JMAPError):
    """The server cannot describe what changed since our state.

    Not an error to retry. RFC 8620 §5.2 requires the client to invalidate its
    cache for this type and re-download, which is why this is a distinct
    exception rather than a passed-through ``MethodError``: the recovery is
    different from every other method error.
    """

    def __init__(self, type_name: str, since_state: str) -> None:
        self.type_name = type_name
        self.since_state = since_state
        super().__init__(
            f"the server cannot calculate {type_name} changes since state "
            f"{since_state!r}; discard the cached {type_name} data and re-download"
        )


@dataclass(slots=True)
class ChangeSet:
    """Everything that changed across one or more pages.

    Ids are deduplicated but their *category* is not merged: a record created and
    then updated inside one sync window appears in both lists, which is what the
    server reported and what a caller reconciling against a cache needs to see.
    """

    type_name: str
    created: list[str] = field(default_factory=lambda: [])
    updated: list[str] = field(default_factory=lambda: [])
    destroyed: list[str] = field(default_factory=lambda: [])
    #: The state to resume from next time.
    new_state: str = ""
    #: How many round trips this took, for callers tuning ``max_changes``.
    pages: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.created or self.updated or self.destroyed)

    @property
    def touched(self) -> list[str]:
        """Every id that needs re-fetching - created plus updated, deduplicated."""
        seen: dict[str, None] = dict.fromkeys(self.created)
        seen.update(dict.fromkeys(self.updated))
        return list(seen)

    def absorb(self, response: ChangesResponse) -> None:
        """Fold one page into the set."""
        self.created.extend(response.created)
        self.updated.extend(response.updated)
        self.destroyed.extend(response.destroyed)
        self.new_state = response.new_state or self.new_state
        self.pages += 1

    def __len__(self) -> int:
        return len(self.created) + len(self.updated) + len(self.destroyed)

    def __repr__(self) -> str:
        return (
            f"ChangeSet({self.type_name!r}, created={len(self.created)}, "
            f"updated={len(self.updated)}, destroyed={len(self.destroyed)}, "
            f"pages={self.pages})"
        )


def is_resync_error(error: BaseException) -> bool:
    """Whether a method error means the cursor is dead."""
    return isinstance(error, MethodError) and error.type == CANNOT_CALCULATE_CHANGES


class ChangeStream:
    """Pulls ``Foo/changes`` pages until the server says it is done.

    The cursor is read from and written back to a :class:`StateStore`, so a
    process that stops halfway resumes rather than starting over.

    Delivery is **at least once**, deliberately. The cursor advances only once the
    consumer comes back for the *next* page, so a page being processed when the
    process dies is delivered again on the next run. Writing the cursor before
    handing the page over would make it at-most-once and lose changes on a crash;
    duplicates a caller can absorb, missing changes it cannot.
    """

    __slots__ = ("_account_id", "_client", "_key", "_store", "_type_name")

    def __init__(
        self,
        client: JMAPClient,
        type_name: str,
        *,
        account_id: Id | None = None,
        store: StateStore | None = None,
    ) -> None:
        from jmap.sync.state import InMemoryStateStore  # local: avoids an import cycle

        self._client = client
        self._type_name = type_name
        self._account_id: Id | None = account_id or client.default_account
        self._store = store if store is not None else InMemoryStateStore()
        self._key = type_key(str(self._account_id or ""), type_name)

    @property
    def state(self) -> str | None:
        """The cursor this stream would resume from."""
        return self._store.get(self._key)

    def seed(self, state: str) -> None:
        """Record a starting cursor, e.g. the ``state`` from a first ``/get``."""
        self._store.set(self._key, state)

    def reset(self) -> None:
        """Forget the cursor, so the next sync starts from scratch."""
        self._store.delete(self._key)

    def pages(self, *, max_changes: int | None = None) -> Iterator[ChangesResponse]:
        """Yield each page of changes, following ``hasMoreChanges`` to the end.

        Yields rather than accumulating so a caller syncing a large mailbox can
        process each page as it arrives instead of holding every id in memory.
        """
        since = self._store.get(self._key)
        if since is None:
            raise ResyncRequiredError(self._type_name, "")

        while True:
            response = self._fetch(since, max_changes)
            yield response
            # Only reached when the consumer asks for another page, which is what
            # makes delivery at-least-once: abandoning the iterator here leaves
            # the cursor where it was, and this page arrives again next time.
            advanced = response.new_state or since
            if response.has_more_changes and advanced == since:
                # §5.2 requires newState to move when hasMoreChanges is true;
                # following a stuck cursor is an infinite request loop.
                raise StuckChangeStreamError(self._type_name, since)
            since = advanced
            self._store.set(self._key, since)
            if not response.has_more_changes:
                return

    def catch_up(self, *, max_changes: int | None = None) -> ChangeSet:
        """Collect every outstanding change into one :class:`ChangeSet`."""
        changes = ChangeSet(self._type_name)
        for page in self.pages(max_changes=max_changes):
            changes.absorb(page)
        return changes

    def _fetch(self, since: str, max_changes: int | None) -> ChangesResponse:
        arguments: dict[str, Any] = {"sinceState": since}
        if max_changes is not None:
            arguments["maxChanges"] = max_changes
        try:
            result = self._client.call(
                f"{self._type_name}/changes", arguments, account_id=self._account_id
            )
        except MethodError as error:
            if is_resync_error(error):
                raise ResyncRequiredError(self._type_name, since) from error
            raise
        return result  # type: ignore[no-any-return]

    def __repr__(self) -> str:
        return f"ChangeStream({self._type_name!r}, state={self.state!r})"

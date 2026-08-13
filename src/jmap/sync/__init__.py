"""Keeping a local view of server state in sync.

Three pieces, each solving a problem the protocol creates rather than solves:

* :class:`~jmap.sync.state.StateStore` - where sync cursors live between runs.
* :class:`~jmap.sync.changes.ChangeStream` - follows ``Foo/changes`` to the end
  rather than stopping after one page.
* :class:`~jmap.sync.query.QueryView` - splices ``Foo/queryChanges`` deltas into a
  cached, sparse result list (RFC 8620 §5.6).

The library stays stateless itself: it computes the deltas and hands them over,
and the application decides what to persist.
"""

from __future__ import annotations

from jmap.sync.changes import (
    ChangeSet,
    ChangeStream,
    ResyncRequiredError,
    StuckChangeStreamError,
)
from jmap.sync.query import (
    QuerySpec,
    QueryView,
    StaleQueryViewError,
    UncacheableQueryError,
    ViewTooLargeError,
    splice,
)
from jmap.sync.state import InMemoryStateStore, StateStore, query_key, type_key

__all__ = [
    "ChangeSet",
    "ChangeStream",
    "InMemoryStateStore",
    "QuerySpec",
    "QueryView",
    "ResyncRequiredError",
    "StaleQueryViewError",
    "StateStore",
    "StuckChangeStreamError",
    "UncacheableQueryError",
    "ViewTooLargeError",
    "query_key",
    "splice",
    "type_key",
]

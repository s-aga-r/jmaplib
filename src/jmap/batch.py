"""Collecting method calls into a request, and applying every local gate.

A JMAP request is a batch by construction, so batching is the normal path here
rather than an optimisation: :meth:`Batch.add` queues a call and hands back a
:class:`~jmap.core.invocation.Handle` immediately, long before anything is sent,
which is what lets a later call reference an earlier one's results.

Everything this class does before the wire is a gate that turns a server-side
failure into a local one:

* an unadvertised capability raises instead of poisoning the whole request
  (Stalwart answers one unknown URN with a request-level ``notRequest``);
* a mutation on a read-only account raises instead of earning ``forbidden``;
* ``accountId`` is resolved from ``primaryAccounts`` rather than guessed;
* properties the server refuses to return are rejected before being asked for;
* an oversized ``/set`` raises rather than being split, because splitting it
  would break the ``ifInState`` guarantee that makes it atomic.

The batch is I/O-free. It plans requests and absorbs responses; the client shells
do the HTTP, which is what lets one implementation serve both sync and async.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.capabilities.registry import UnsupportedMethodError
from jmap.capabilities.spec import MethodKind
from jmap.core.errors import CapabilityFieldError, JMAPError
from jmap.core.invocation import Handle, MethodCall
from jmap.core.request import plan_requests
from jmap.core.response import dispatch

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.capabilities.spec import MethodSpec
    from jmap.core.ids import Id
    from jmap.core.request import Request
    from jmap.core.response import Response


class NoAccountError(JMAPError):
    """No account id was given and none could be resolved.

    Deliberately not "use the first account": on a server where the
    authenticated principal can see a shared calendar, guessing sends ``Email/*``
    at an account that has no mail and returns ``accountNotSupportedByMethod``.
    """

    def __init__(self, method: str, urn: str) -> None:
        self.method = method
        self.urn = urn
        super().__init__(
            f"{method} needs an accountId: none was passed, no client default is set, "
            f"and the session lists no primaryAccounts entry for {urn}"
        )


class ReadOnlyAccountError(JMAPError):
    """A mutating method was aimed at an account the session marks read-only."""

    def __init__(self, method: str, account_id: Id) -> None:
        self.method = method
        self.account_id = account_id
        super().__init__(f"{method} would modify read-only account {account_id!r}")


class Batch:
    """A set of calls destined for one or more JMAP requests."""

    __slots__ = (
        "_capabilities",
        "_counter",
        "_created_ids",
        "_default_account",
        "_handles",
        "_properties",
    )

    def __init__(
        self,
        capabilities: ActiveCapabilities,
        *,
        default_account: Id | None = None,
        created_ids: Mapping[str, Id] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._default_account = default_account or capabilities.account_id
        self._handles: list[Handle[Any]] = []
        #: (type, property) pairs seen so far, for `using` derivation.
        self._properties: list[tuple[str, str]] = []
        self._counter = 0
        #: Threaded across requests so ``#`` creation references survive a split
        #: batch (RFC 8620 §3.3).
        self._created_ids: dict[str, Id] = dict(created_ids or {})

    def __len__(self) -> int:
        return len(self._handles)

    @property
    def handles(self) -> tuple[Handle[Any], ...]:
        return tuple(self._handles)

    @property
    def created_ids(self) -> Mapping[str, Id]:
        return dict(self._created_ids)

    def add(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        account_id: Id | None = None,
    ) -> Handle[Any]:
        """Queue ``name`` and return its handle.

        The handle is usable as a back-reference source straight away; its result
        only becomes readable once the batch has been executed.
        """
        spec = self._require_method(name)
        args = dict(arguments or {})

        if spec.account_scoped and "accountId" not in args:
            args["accountId"] = self._resolve_account(name, spec, account_id)
        elif account_id is not None:
            args["accountId"] = account_id

        self._check_read_only(name, spec, args)
        self._check_properties(name, spec, args)

        for prop in args.get("properties") or ():
            self._properties.append((spec.type_name, str(prop)))

        self._counter += 1
        call_id = f"c{self._counter}"
        handle: Handle[Any] = Handle(call_id, MethodCall(name, args, parse=spec.parse))
        self._handles.append(handle)
        return handle

    # -- local gates -------------------------------------------------------- #
    def _require_method(self, name: str) -> MethodSpec:
        spec = self._capabilities.method(name)
        if spec is None:
            raise UnsupportedMethodError(name, advertised=self._capabilities.advertised)
        return spec

    def _resolve_account(self, name: str, spec: MethodSpec, explicit: Id | None) -> Id:
        if explicit is not None:
            return explicit
        if self._default_account is not None:
            return self._default_account
        owner = self._capabilities.owner_of(name)
        urn = owner.urn if owner is not None else ""
        primary = self._capabilities.session.primary_account_for(urn)
        if primary is None:
            raise NoAccountError(name, urn)
        return primary

    def _check_read_only(self, name: str, spec: MethodSpec, args: Mapping[str, Any]) -> None:
        if not spec.mutating:
            return
        account_id = args.get("accountId")
        if account_id is not None and self._capabilities.session.is_read_only(account_id):
            raise ReadOnlyAccountError(name, account_id)

    def _check_properties(self, name: str, spec: MethodSpec, args: Mapping[str, Any]) -> None:
        """Reject properties the server has said it will never return.

        Asking for ``PushSubscription``'s ``url`` earns ``forbidden`` for the
        whole call, which is a confusing way to learn about a typo.
        """
        requested = args.get("properties")
        if not requested:
            return
        data_type = self._capabilities.data_type(spec.type_name)
        if data_type is None:
            return
        forbidden = sorted(data_type.never_request_properties.intersection(requested))
        if forbidden:
            raise CapabilityFieldError(
                spec.type_name, "properties", "never returned by the server", forbidden
            )

    # -- planning and absorbing --------------------------------------------- #
    def using(self, extra: frozenset[str] = frozenset()) -> frozenset[str]:
        """The ``using`` set this batch needs, validated against the server."""
        return self._capabilities.using_for(
            [handle.call.name for handle in self._handles],
            properties=self._properties,
            extra=extra,
        )

    def plan(self, *, extra_using: frozenset[str] = frozenset()) -> list[Request]:
        """Split the batch into requests that respect ``maxCallsInRequest``."""
        if not self._handles:
            return []
        self._check_set_sizes()
        return plan_requests(
            [(handle.call_id, handle.call) for handle in self._handles],
            using=self.using(extra_using),
            max_calls_in_request=self._capabilities.limits.max_calls_in_request,
            created_ids=self._created_ids or None,
        )

    def _check_set_sizes(self) -> None:
        """Refuse an oversized ``/set`` rather than splitting it.

        Splitting would break the guarantee that makes ``/set`` useful: a single
        ``ifInState`` covering every change. Two half-sets can half-apply.
        """
        limit = self._capabilities.limits.max_objects_in_set
        for handle in self._handles:
            spec = self._capabilities.method(handle.call.name)
            if spec is None or spec.kind is not MethodKind.SET:
                continue
            args = handle.call.arguments
            total = sum(len(args.get(key) or ()) for key in ("create", "update", "destroy"))
            if total > limit:
                raise CapabilityFieldError(handle.call.name, "maxObjectsInSet", limit, total)

    def absorb(self, response: Response) -> None:
        """Route one response back to its handles and keep any new creation ids."""
        dispatch(response, self._handles)
        self._created_ids.update(response.created_ids)

    def pending(self) -> tuple[Handle[Any], ...]:
        """Handles still awaiting a response - non-empty mid-split."""
        return tuple(handle for handle in self._handles if not handle.is_resolved)

    def __repr__(self) -> str:
        return f"Batch({[handle.call.name for handle in self._handles]})"


def is_mutating(batch: Batch, capabilities: ActiveCapabilities) -> bool:
    """Whether any queued call can change server state.

    Feeds the retry decision: a batch that cannot mutate is safe to re-send even
    when the failure might have applied.
    """
    return any(
        (spec := capabilities.method(handle.call.name)) is not None and spec.mutating
        for handle in batch.handles
    )


def all_mutations_guarded(batch: Batch, capabilities: ActiveCapabilities) -> bool:
    """Whether every mutating call carried ``ifInState``.

    A guarded retry cannot duplicate: the second attempt fails with
    ``stateMismatch`` if the first one landed (RFC 8620 §5.3).
    """
    mutations = [
        handle
        for handle in batch.handles
        if (spec := capabilities.method(handle.call.name)) is not None and spec.mutating
    ]
    return all(handle.call.arguments.get("ifInState") for handle in mutations)


def guarded_call_ids(batch: Batch, capabilities: ActiveCapabilities) -> Sequence[str]:
    """Call ids of the mutations that carried ``ifInState``, for diagnostics."""
    return [
        handle.call_id
        for handle in batch.handles
        if (spec := capabilities.method(handle.call.name)) is not None
        and spec.mutating
        and handle.call.arguments.get("ifInState")
    ]

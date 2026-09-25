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
* a sort the server did not advertise raises instead of failing the query;
* an oversized ``/set`` raises rather than being split, because splitting it
  would break the ``ifInState`` guarantee that makes it atomic.

The batch is I/O-free. It plans requests and absorbs responses; the client shells
do the HTTP, which is what lets one implementation serve both sync and async.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from jmap.capabilities.parsing import parser_for
from jmap.capabilities.registry import UnsupportedMethodError
from jmap.capabilities.spec import MethodKind
from jmap.chunking import ChunkedHandle, chunk_get_call
from jmap.core.errors import CapabilityFieldError, JMAPError, MethodError
from jmap.core.invocation import Handle, MethodCall
from jmap.core.limits import FIELD_COLLATION_ALGORITHMS, URN_CORE
from jmap.core.request import plan_requests
from jmap.core.response import dispatch

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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

    def __init__(self, method: str, urn: str, *, reason: str = "") -> None:
        self.method = method
        self.urn = urn
        super().__init__(
            f"{method} needs an accountId: none was passed, no client default is set, "
            + (reason or f"and the session lists no primaryAccounts entry for {urn}")
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
        "_filter_fields",
        "_handles",
        "_in_flight",
        "_properties",
        "_sort_options",
        "_type_names",
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
        #: Call ids of the request :meth:`requests` last handed out, so that
        #: :meth:`absorb` can tell a call the server skipped from one that has
        #: not been sent yet.
        self._in_flight: frozenset[str] = frozenset()
        #: (type, property) pairs seen so far, for `using` derivation.
        self._properties: list[tuple[str, str]] = []
        #: (type, name) pairs for every FilterCondition property and sort
        #: comparator named so far. A capability can add these without adding a
        #: method - RFC 9219's `hasSmime` is one - so they feed `using` too.
        self._filter_fields: list[tuple[str, str]] = []
        self._sort_options: list[tuple[str, str]] = []
        #: Data type names named as *arguments*, which pull their owning
        #: capabilities into `using` too - see ``MethodSpec.type_names_argument``.
        self._type_names: list[str] = []
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

    def capability_value(self, urn: str) -> Mapping[str, Any]:
        """The advertised capability object for ``urn``, scoped to this account.

        Exposed because a bespoke builder may need to check a capability field
        before queueing its call - an unsupported digest algorithm or an illegal
        Sieve script name is far cheaper to reject here than to diagnose from the
        response.

        Empty when the capability is unadvertised or carries no fields, so a caller
        parsing it gets the conservative defaults rather than an error.

        The account is resolved rather than assumed - see
        :meth:`Session.capability_account`. Reading the session-level copy when
        no account was pinned is what makes these gates inert, or worse, against
        a server that keeps its real limits per account.
        """
        session = self._capabilities.session
        return session.capability_value(urn, session.capability_account(urn, self._default_account))

    def add(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        account_id: Id | None = None,
        response_model: type[Any] | None = None,
    ) -> Handle[Any]:
        """Queue ``name`` and return its handle.

        The handle is usable as a back-reference source straight away; its result
        only becomes readable once the batch has been executed.

        ``response_model`` parses the answer into that model instead of the
        method's usual result. Typed builders pass it for methods whose raw call
        has always answered with the wire dict, so that answer does not change.
        """
        spec = self._require_method(name)
        args = dict(arguments or {})

        if spec.account_scoped and "accountId" not in args:
            args["accountId"] = self._resolve_account(name, spec, account_id)
        elif account_id is not None:
            args["accountId"] = account_id

        self._check_read_only(name, spec, args)
        self._check_properties(name, spec, args)
        self._check_sort(name, spec, args)

        # Only a literal array can be inspected. Both of these arguments are
        # routinely back-references - RFC 9425 §4.3 feeds `/updatedProperties`
        # straight into a Quota/get - and a ResultRef names values that do not
        # exist yet, so there is nothing here to derive `using` from. The server
        # resolves it against a response that has already declared what it needs.
        for prop in _array(args.get("properties")) or ():
            self._properties.append((spec.type_name, str(prop)))
        if spec.type_names_argument is not None:
            for type_name in _array(args.get(spec.type_names_argument)) or ():
                self._type_names.append(str(type_name))
        conditions = _object(args.get("filter"))
        if conditions is not None:
            filter_type = spec.filter_type or spec.type_name
            self._filter_fields.extend((filter_type, name) for name in _condition_names(conditions))
        for comparator in _array(args.get("sort")) or ():
            named = fields.get("property") if (fields := _object(comparator)) is not None else None
            if isinstance(named, str):
                self._sort_options.append((spec.type_name, named))

        self._counter += 1
        call_id = f"c{self._counter}"
        # The parser is chosen from the method's shape and the type it acts on,
        # so `Email/get` really does yield a GetResponse[Email] rather than a dict.
        parse = parser_for(
            spec, self._capabilities.data_type(spec.type_name), response_model=response_model
        )
        call = MethodCall(name, args, parse=parse)

        chunks = self._split_if_oversized(spec, call)
        if chunks is not None:
            handles = [
                Handle(self._next_call_id(), chunk) if index else Handle(call_id, chunk)
                for index, chunk in enumerate(chunks)
            ]
            self._handles.extend(handles)
            chunked: Handle[Any] = ChunkedHandle(handles)
            return chunked

        handle: Handle[Any] = Handle(call_id, call)
        self._handles.append(handle)
        return handle

    def _next_call_id(self) -> str:
        self._counter += 1
        return f"c{self._counter}"

    def _split_if_oversized(
        self, spec: MethodSpec, call: MethodCall[Any]
    ) -> list[MethodCall[Any]] | None:
        """Split a ``/get`` naming more ids than the server will accept.

        Only ``/get`` is chunked. ``/set`` is refused instead, because splitting
        it would break the single ``ifInState`` that makes it atomic - see
        :meth:`_check_set_sizes`.
        """
        if spec.kind is not MethodKind.GET:
            return None
        raw_ids: Any = call.arguments.get("ids")
        # A ResultRef has no length here, and `None` means "every record" - in
        # both cases the count is the server's problem, not ours. A tuple is a
        # sequence of ids like a list, and went out whole when only lists split.
        if not isinstance(raw_ids, (list, tuple)):
            return None
        ids = list(cast("Sequence[Any]", raw_ids))
        limit = self._capabilities.limits.max_objects_in_get
        if len(ids) <= limit:
            return None
        # Each id at most once: RFC 8620 §5.1 answers a repeat once, but chunks
        # are separate calls, and a repeat in two of them came back twice.
        ids = list(dict.fromkeys(ids))
        if len(ids) <= limit:
            return None
        return chunk_get_call(call, ids, limit)

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
        requested = _array(args.get("properties"))
        # A back-reference names properties that do not exist yet, so there is
        # nothing to check against; the earlier call it points at was gated when
        # it was queued.
        if requested is None:
            return
        data_type = self._capabilities.data_type(spec.type_name)
        if data_type is None:
            return
        forbidden = sorted(data_type.never_request_properties.intersection(requested))
        if forbidden:
            raise CapabilityFieldError(
                spec.type_name, "properties", "never returned by the server", forbidden
            )

    def _check_sort(self, name: str, spec: MethodSpec, args: Mapping[str, Any]) -> None:
        """Refuse a sort the server said it cannot do.

        Either mistake earns ``unsupportedSort`` for the whole query (RFC 8620
        §5.5) and no ids at all: a collation outside ``collationAlgorithms``, or
        a comparator on a property outside the type's advertised sort options.
        Each is checked only against a list the server actually advertised.
        """
        comparators = [
            fields for item in _array(args.get("sort")) or () if (fields := _object(item))
        ]
        if comparators:
            self._check_collations(comparators)
            self._check_sort_options(name, spec, args, comparators)

    def _check_collations(self, comparators: Sequence[Mapping[str, Any]]) -> None:
        core = self._capabilities.session.capability_value(URN_CORE)
        advertised = _array(core.get(FIELD_COLLATION_ALGORITHMS))
        if advertised is None:
            return
        for fields in comparators:
            collation = fields.get("collation")
            if isinstance(collation, str) and collation not in advertised:
                raise CapabilityFieldError(
                    URN_CORE, FIELD_COLLATION_ALGORITHMS, tuple(advertised), collation
                )

    def _check_sort_options(
        self,
        name: str,
        spec: MethodSpec,
        args: Mapping[str, Any],
        comparators: Sequence[Mapping[str, Any]],
    ) -> None:
        data_type = self._capabilities.data_type(spec.type_name)
        owner = self._capabilities.owner_of(name)
        if data_type is None or data_type.sort_options_field is None or owner is None:
            return
        # The account the call targets: a shared one may advertise other sorts.
        account = args.get("accountId")
        scoped = cast("Id", account) if isinstance(account, str) else None
        value = self._capabilities.session.capability_value(owner.urn, scoped)
        advertised = _array(value.get(data_type.sort_options_field))
        if advertised is None:
            return
        unsupported = tuple(
            wanted
            for fields in comparators
            if isinstance(wanted := fields.get("property"), str) and wanted not in advertised
        )
        if unsupported:
            raise CapabilityFieldError(
                owner.urn, data_type.sort_options_field, tuple(advertised), unsupported
            )

    # -- planning and absorbing --------------------------------------------- #
    def using(self, extra: frozenset[str] = frozenset()) -> frozenset[str]:
        """The ``using`` set this batch needs, validated against the server."""
        return self._capabilities.using_for(
            [handle.call.name for handle in self._handles],
            properties=self._properties,
            filter_fields=self._filter_fields,
            sort_options=self._sort_options,
            type_names=self._type_names,
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

    def requests(self, *, extra_using: frozenset[str] = frozenset()) -> Iterator[Request]:
        """Plan the batch, then yield each request as it is due to be sent.

        Lazily, because every request after the first must carry the creation
        ids the server assigned while answering the ones before it: RFC 8620
        §3.3's ``createdIds`` is the only way a ``#creationId`` survives a split
        under ``maxCallsInRequest``. Planning them all up front sent each later
        request without those ids, so its creation references resolved to
        nothing. :meth:`absorb` each response before asking for the next.
        """
        for request in self.plan(extra_using=extra_using):
            request.created_ids = dict(self._created_ids) or None
            self._in_flight = frozenset(call_id for call_id, _ in request.method_calls)
            yield request
        self._in_flight = frozenset()

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
            # Only literal containers can be counted: a ResultRef in `destroy`
            # (the query-then-destroy pattern) names ids that do not exist yet,
            # and calling len() on it crashed plan() on a legitimate batch.
            total = 0
            for key in ("create", "update", "destroy"):
                value = args.get(key)
                total += len(_object(value) or _array(value) or ())
            if total > limit:
                raise CapabilityFieldError(handle.call.name, "maxObjectsInSet", limit, total)

    def absorb(self, response: Response) -> None:
        """Route one response back to its handles and keep any new creation ids.

        A call the request carried but the response does not answer is failed
        with a ``missingResponse`` :class:`~jmap.core.errors.MethodError`. RFC
        8620 §3.4 has the server answer every call; one that did not otherwise
        left its handle reading as never sent, a RuntimeError telling the caller
        to run a batch that had run.
        """
        dispatch(response, self._handles)
        self._created_ids.update(response.created_ids)
        for handle in self._handles:
            if handle.call_id in self._in_flight and not handle.is_resolved:
                handle.fail(
                    MethodError(
                        MISSING_RESPONSE,
                        handle.call_id,
                        {"description": "the server's response did not answer this call"},
                    )
                )
        self._in_flight = frozenset()

    def pending(self) -> tuple[Handle[Any], ...]:
        """Handles still awaiting a response - non-empty mid-split."""
        return tuple(handle for handle in self._handles if not handle.is_resolved)

    def __repr__(self) -> str:
        return f"Batch({[handle.call.name for handle in self._handles]})"


#: The error type a call gets when its request came back without an answer to
#: it. The library's own, like ``malformedResult``: no RFC names this failure.
MISSING_RESPONSE: Final = "missingResponse"


def _array(value: Any) -> list[Any] | None:
    """A literal array argument as a list; ``None`` for a back-reference or anything else.

    A tuple goes out as an array just as a list does. Only lists were read as
    one, so ``properties=("id", "smimeStatus")`` - which a typed builder passes
    through as given - derived no ``using`` and skipped the property gate.
    """
    return list(cast("Sequence[Any]", value)) if isinstance(value, (list, tuple)) else None


def _object(value: Any) -> Mapping[str, Any] | None:
    """A literal object argument - any mapping, as a filter may be - or ``None``."""
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _condition_names(filter_: Mapping[str, Any]) -> list[str]:
    """Every FilterCondition property named anywhere in a filter tree (RFC 8620 §5.5).

    A FilterOperator nests further filters under ``conditions``; anything else is
    a FilterCondition, whose keys are the names a capability may have added.
    Walked with a stack rather than recursion: the tree is caller-built and
    nothing bounds its depth.
    """
    names: list[str] = []
    pending = [filter_]
    while pending:
        node = pending.pop()
        if "operator" in node:
            for item in _array(node.get("conditions")) or ():
                nested = _object(item)
                if nested is not None:
                    pending.append(nested)
        else:
            names.extend(node)
    return names


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
    return all(_is_guarded(handle) for handle in mutations)


def guarded_call_ids(batch: Batch, capabilities: ActiveCapabilities) -> Sequence[str]:
    """Call ids of the mutations that carried ``ifInState``, for diagnostics."""
    return [
        handle.call_id
        for handle in batch.handles
        if (spec := capabilities.method(handle.call.name)) is not None
        and spec.mutating
        and _is_guarded(handle)
    ]


def _is_guarded(handle: Handle[Any]) -> bool:
    """Whether a call's ``ifInState`` would fail a repeat of it.

    Only a literal state does. A back-reference resolves afresh on every
    attempt - typically to the ``state`` of a ``/get`` earlier in the same
    request, which a retry re-runs *after* the first attempt landed - so it
    matches the new state and lets the write happen twice.
    """
    state = handle.call.arguments.get("ifInState")
    return isinstance(state, str) and bool(state)

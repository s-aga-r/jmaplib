"""Method calls, back-references and result handles (RFC 8620 §3.2, §3.7).

A JMAP request is a list of *invocations*: three-element arrays
``[name, arguments, methodCallId]``. Any argument may instead be a
**back-reference** to an earlier call's response, which is how JMAP avoids a
round trip between "which emails match?" and "give me those emails".

Two details drive the design here:

* A back-reference is expressed by renaming the argument, not by wrapping the
  value: ``ids`` becomes ``"#ids"`` and its value becomes a ``ResultReference``
  object. So references cannot be represented as ordinary argument values - the
  serialiser has to know which arguments are references, which is why
  :class:`ResultRef` is a distinct type rather than a dict.
* A reference must name both the call it targets *and* the method name it
  expects that call to have. If the target call errored, its response name is
  ``error``, the name check fails, and the referring call fails with
  ``invalidResultReference`` rather than silently reading from an error payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jmap.core.errors import MethodError
    from jmap.core.ids import Id

R = TypeVar("R")
T = TypeVar("T")


class ResultRef(Generic[T]):
    """A back-reference to a path within an earlier invocation's response.

    Parameterised by what the pointer *yields*, so the type checker can reject
    ``ids=<ref to a state string>`` before it becomes a round trip that fails
    with ``invalidResultReference``.
    """

    __slots__ = ("name", "path", "result_of")

    result_of: str
    name: str
    path: str

    def __init__(self, result_of: str, name: str, path: str) -> None:
        self.result_of = result_of
        self.name = name
        self.path = path

    def to_wire(self) -> dict[str, str]:
        """The ``ResultReference`` object as it appears on the wire."""
        return {"resultOf": self.result_of, "name": self.name, "path": self.path}

    def __repr__(self) -> str:
        return f"ResultRef(resultOf={self.result_of!r}, name={self.name!r}, path={self.path!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ResultRef) and (other.result_of, other.name, other.path) == (
            self.result_of,
            self.name,
            self.path,
        )

    def __hash__(self) -> int:
        return hash((ResultRef, self.result_of, self.name, self.path))


class MethodCall(Generic[R]):
    """One invocation, plus how to parse its response.

    ``parse`` travels with the call rather than being looked up at response time
    so that a capability module can register an irregular response shape (e.g.
    ``Email/parse``) without the response demultiplexer needing a registry.
    """

    __slots__ = ("arguments", "name", "parse")

    name: str
    arguments: dict[str, Any]
    parse: Callable[[Mapping[str, Any]], R]

    def __init__(
        self,
        name: str,
        arguments: Mapping[str, Any],
        parse: Callable[[Mapping[str, Any]], R],
    ) -> None:
        self.name = name
        self.arguments = dict(arguments)
        self.parse = parse

    def split_arguments(self) -> tuple[dict[str, Any], dict[str, ResultRef[Any]]]:
        """Separate plain arguments from back-references.

        Kept separate from serialisation because the request planner needs the
        reference set to build its dependency graph *before* anything is
        serialised.
        """
        plain: dict[str, Any] = {}
        refs: dict[str, ResultRef[Any]] = {}
        for key, value in self.arguments.items():
            if isinstance(value, ResultRef):
                refs[key] = value
            else:
                plain[key] = value
        return plain, refs

    def to_wire_arguments(self) -> dict[str, Any]:
        plain, refs = self.split_arguments()
        for key, ref in refs.items():
            plain[f"#{key}"] = ref.to_wire()
        return plain

    def __repr__(self) -> str:
        return f"MethodCall({self.name!r}, {sorted(self.arguments)})"


class ParsedInvocation:
    """A raw response triple, before it is routed to a handle."""

    __slots__ = ("arguments", "method_call_id", "name")

    name: str
    arguments: dict[str, Any]
    method_call_id: str

    def __init__(self, name: str, arguments: Mapping[str, Any], method_call_id: str) -> None:
        self.name = name
        self.arguments = dict(arguments)
        self.method_call_id = method_call_id

    @classmethod
    def from_wire(cls, triple: list[Any]) -> ParsedInvocation:
        if len(triple) != 3:
            raise ValueError(f"invocation must have 3 elements, got {len(triple)}")
        name, arguments, call_id = triple
        return cls(str(name), arguments, str(call_id))

    def __repr__(self) -> str:
        return f"ParsedInvocation({self.name!r}, id={self.method_call_id!r})"


class Handle(Generic[R]):
    """The client-side reference to one queued call.

    Returned immediately when a call is added to a batch, long before the request
    is sent. It is both how you build a back-reference to the call and how you
    read its result afterwards.

    ``extra`` holds *implicit* responses - additional invocations the server may
    emit for a single call, such as the ``Email/set`` that RFC 8621 §7.5 says a
    server may return alongside ``EmailSubmission/set`` with
    ``onSuccessUpdateEmail``. They share this call's id, so they are collected
    here rather than merged into ``result``, which would corrupt it.
    """

    __slots__ = ("_error", "_extra", "_resolved", "_result", "call", "call_id")

    call_id: str
    call: MethodCall[R]

    def __init__(self, call_id: str, call: MethodCall[R]) -> None:
        self.call_id = call_id
        self.call = call
        self._result: R | None = None
        self._error: MethodError | None = None
        self._extra: tuple[ParsedInvocation, ...] = ()
        self._resolved = False

    # -- building references ------------------------------------------------ #
    def ref(self, path: str) -> ResultRef[Any]:
        """An untyped reference to an arbitrary path in this call's response."""
        return ResultRef(self.call_id, self.call.name, path)

    def ref_ids(self) -> ResultRef[list[Id]]:
        """``/ids`` - the standard chaining of ``Foo/query`` into ``Foo/get``."""
        return ResultRef(self.call_id, self.call.name, "/ids")

    def ref_list(self, prop: str) -> ResultRef[list[Id]]:
        """``/list/*/<prop>`` - fan out over a ``/get`` response."""
        return ResultRef(self.call_id, self.call.name, f"/list/*/{prop}")

    def ref_created(self, creation_key: str, prop: str = "id") -> ResultRef[Any]:
        """``/created/<key>/<prop>`` - read a server-assigned value off a ``/set``.

        Prefer this over a creation reference (``#key``) when you need a
        *property* of the created object rather than its id.
        """
        return ResultRef(self.call_id, self.call.name, f"/created/{creation_key}/{prop}")

    def ref_updated(self) -> ResultRef[list[Id]]:
        return ResultRef(self.call_id, self.call.name, "/updated")

    def ref_updated_properties(self) -> ResultRef[list[str] | None]:
        """``/updatedProperties`` - the fast path for cheap, frequently-moving
        properties (RFC 8621 §2.2 for Mailbox counts, RFC 9425 §4.3 for Quota
        usage). A ``null`` there means the server could not narrow it down and
        every property must be fetched, which is the opposite of what the empty
        reading suggests."""
        return ResultRef(self.call_id, self.call.name, "/updatedProperties")

    # -- reading results ---------------------------------------------------- #
    def fulfil(self, result: R, extra: tuple[ParsedInvocation, ...] = ()) -> None:
        """Record this call's answer.

        Called by the response dispatcher, not by user code: a handle is filled
        in exactly once, when its request comes back.
        """
        self._result = result
        self._extra = extra
        self._resolved = True

    def fail(self, error: MethodError) -> None:
        """Record that this call errored, for re-raising when the result is read.

        Called by the response dispatcher, not by user code.
        """
        self._error = error
        self._resolved = True

    @property
    def result(self) -> R:
        """This call's parsed response.

        Raises the call's :class:`MethodError` if it failed. Method errors are
        deliberately raised *here* rather than when the response arrives, so one
        failed call in a batch does not discard its siblings' results.
        """
        if not self._resolved:
            raise RuntimeError(
                f"call {self.call_id!r} has not been executed yet; "
                "exit the batch context or await the client call first"
            )
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]

    @property
    def error(self) -> MethodError | None:
        """This call's error, if it failed - the non-raising alternative to
        :attr:`result`."""
        return self._error

    @property
    def extra(self) -> tuple[ParsedInvocation, ...]:
        return self._extra

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def __repr__(self) -> str:
        state = "pending" if not self._resolved else ("error" if self._error else "ok")
        return f"Handle({self.call.name!r}, id={self.call_id!r}, {state})"

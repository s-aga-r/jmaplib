"""Checking what callers pass in, against the annotations that describe it.

A server rejects a malformed argument with ``invalidArguments`` - after a round
trip, and without naming the value at fault - and some it does not reject at
all: ``ids="m1"`` is a string where an array goes, and a lenient server reads it
as something else. So the builders and helpers that take arguments from callers
check them first, with pydantic, and a mistake raises on the line that made it.

:func:`checked` validates a function's arguments against its own annotations,
in strict mode: ``"5"`` is not a number and ``1`` is not a boolean, because the
wire would not coerce them either. Three kinds of value go through untouched
wherever an annotation names them: :data:`~jmap.models.base.UNSET`, the absence
of an argument; a :class:`~jmap.core.invocation.ResultRef`, which is the
server's to resolve - RFC 8620 §3.7 lets an argument be one; and a
:class:`~jmap.core.ids.CreationRef`, checked when it was made.

A failure raises pydantic's :class:`~pydantic.ValidationError`, a ``ValueError``,
listing every argument at fault. Calling a function with the wrong arguments at
all - a missing one, a positional one where only keywords go - is the
``TypeError`` Python raises for any function.
"""

from __future__ import annotations

import functools
import inspect
import types
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Final,
    LiteralString,
    NamedTuple,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from pydantic import AfterValidator, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from jmap.core.ids import CreationRef
from jmap.core.ijson import MAX_SAFE_INT, MIN_SAFE_INT, parse_utc_date
from jmap.core.invocation import ResultRef
from jmap.models.base import UnsetType

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Int", "UTCDate", "UnsignedInt", "checked"]

_F = TypeVar("_F", bound="Callable[..., Any]")

#: RFC 8620 §1.3: an integer JSON carries exactly, in [-2^53+1, 2^53-1].
Int = Annotated[int, Field(ge=MIN_SAFE_INT, le=MAX_SAFE_INT)]

#: RFC 8620 §1.3: an ``Int`` that is not negative.
UnsignedInt = Annotated[int, Field(ge=0, le=MAX_SAFE_INT)]


def _utc_date(value: str) -> str:
    # Parsed only to be checked: it goes on the wire as the caller spelled it.
    # The InvalidDateError is a ValueError, which pydantic reports for us.
    parse_utc_date(value)
    return value


#: RFC 8620 §1.4: a date-time in UTC, spelled with ``Z`` -
#: ``2026-09-24T10:00:00Z``.
UTCDate = Annotated[str, AfterValidator(_utc_date)]

#: Strict because an argument goes on the wire as given: a value pydantic
#: would coerce - ``"5"`` to 5, ``1`` to ``True`` - is one the server does not.
_STRICT: Final = ConfigDict(strict=True, arbitrary_types_allowed=True)

#: Values that are forwarded rather than checked, wherever an annotation names
#: their type: the absence of an argument, a back-reference, whose value exists
#: only once the server resolves it, and a creation reference, which checked its
#: id when it was made.
_FORWARDED: Final[tuple[type, ...]] = (UnsetType, ResultRef, CreationRef)


class _Parameter(NamedTuple):
    """How to check one argument."""

    adapter: TypeAdapter[Any]
    #: The forwarded types its annotation names.
    forwarded: tuple[type, ...]


def _parameter(annotation: Any) -> _Parameter:
    """Split an annotation into what is forwarded and what is checked.

    Taking the forwarded types out of the union before pydantic sees it is what
    keeps a failure to one line: a union reports each member's rejection, and
    "not an instance of UnsetType" says nothing about what went wrong.
    """
    # `X | Y` evaluates to a types.UnionType when both sides are plain classes
    # or builtin generics, and to a typing.Union otherwise.
    members = (
        get_args(annotation)
        if get_origin(annotation) in (Union, types.UnionType)
        else (annotation,)
    )
    forwarded = tuple(
        kind for kind in _FORWARDED if any((get_origin(m) or m) is kind for m in members)
    )
    checked = [member for member in members if (get_origin(member) or member) not in _FORWARDED]
    union = functools.reduce(lambda union, member: union | member, checked)
    return _Parameter(TypeAdapter(union, config=_STRICT), forwarded)


def _parameters(
    function: Callable[..., Any], signature: inspect.Signature
) -> dict[str, _Parameter]:
    """One :class:`_Parameter` per annotated argument that can be named."""
    hints = get_type_hints(function, include_extras=True)
    return {
        name: _parameter(hints[name])
        for name, parameter in signature.parameters.items()
        if name in hints
        and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    }


def _located(name: str, error: ValidationError) -> list[InitErrorDetails]:
    """One argument's failures, placed under its name."""
    return [
        InitErrorDetails(
            # The message is kept as rendered. A custom error carries no
            # context, so pydantic leaves any braces in it alone. mypy reads
            # LiteralString as str and calls the casts redundant; pyright needs
            # them.
            type=PydanticCustomError(
                cast("LiteralString", problem["type"]),  # type: ignore[redundant-cast]
                cast("LiteralString", problem["msg"]),  # type: ignore[redundant-cast]
            ),
            loc=(name, *problem["loc"]),
            input=problem["input"],
        )
        for problem in error.errors(include_url=False)
    ]


def _check(function: _F, subject: Callable[[Any], str] | None) -> _F:
    signature = inspect.signature(function)
    # Built on the first call rather than at import: resolving the annotations
    # and building a validator per argument costs milliseconds, and most
    # functions in a program are never called.
    parameters: dict[str, _Parameter] = {}

    def title(args: tuple[Any, ...]) -> str:
        # Without a first argument there is no subject to ask - a method called
        # on its class, with nothing to bind `self` to.
        if subject is None or not args:
            return function.__qualname__
        return f"{subject(args[0])}.{function.__name__}"

    @functools.wraps(function)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as error:
            # The TypeError Python raises for a missing argument, a surplus
            # positional or a keyword named twice - which bind() words without
            # saying what was being called.
            raise TypeError(f"{title(args)}(): {error}") from None
        if not parameters:
            parameters.update(_parameters(function, signature))
        problems: list[InitErrorDetails] = []
        for name, value in bound.arguments.items():
            parameter = parameters.get(name)
            if parameter is None or isinstance(value, parameter.forwarded):
                continue
            try:
                bound.arguments[name] = parameter.adapter.validate_python(value)
            except ValidationError as error:
                problems.extend(_located(name, error))
        if problems:
            raise ValidationError.from_exception_data(title(args), problems)
        return function(*bound.args, **bound.kwargs)

    return cast("_F", call)


@overload
def checked(function: _F, /) -> _F: ...
@overload
def checked(*, subject: Callable[[Any], str]) -> Callable[[_F], _F]: ...
def checked(
    function: _F | None = None, /, *, subject: Callable[[Any], str] | None = None
) -> _F | Callable[[_F], _F]:
    """Validate a function's arguments against its annotations on every call.

    Errors are titled with the function's qualified name, or - for a method
    shared by many objects, such as a builder serving every data type - with
    ``subject(self)`` and the method's name, so a failure says ``Email.get``
    rather than naming the mixin that implements it.

    The annotations are resolved at the first call, so every name in them must
    be importable at run time, not only under ``TYPE_CHECKING``.
    """
    if function is None:

        def decorate(function: _F) -> _F:
            return _check(function, subject)

        return decorate
    return _check(function, None)

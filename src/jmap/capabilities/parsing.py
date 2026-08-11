"""Choosing the parser for a method's response.

The six standard method shapes each have a fixed response shape, and the object
type inside it comes from the data type the method acts on. That pairing is what
turns ``Email/get`` into a ``GetResponse[Email]`` rather than a dict, so it is
computed here from the spec rather than written out once per method.

This lives in the capability layer, not with the models, because it needs
:class:`~jmap.capabilities.spec.MethodKind` - and the models must not import
upwards.

A method whose kind is ``CUSTOM`` keeps whatever parser its spec declares. Those
are the irregular ones (``Email/import``, ``Email/parse``, ``SearchSnippet/get``,
``Core/echo``), and their responses do not follow any of the six shapes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.capabilities.spec import MethodKind
from jmap.models.responses import (
    ChangesResponse,
    CopyResponse,
    GetResponse,
    QueryChangesResponse,
    QueryResponse,
    SetResponse,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jmap.capabilities.spec import DataTypeSpec, MethodSpec

#: Shapes that do not carry an object type, so they need no parameterisation.
_UNPARAMETERISED: dict[MethodKind, type[Any]] = {
    MethodKind.CHANGES: ChangesResponse,
    MethodKind.QUERY: QueryResponse,
    MethodKind.QUERY_CHANGES: QueryChangesResponse,
}

#: Shapes carrying objects of the method's data type.
_PARAMETERISED: dict[MethodKind, Any] = {
    MethodKind.GET: GetResponse,
    MethodKind.SET: SetResponse,
    MethodKind.COPY: CopyResponse,
}


#: Parameterising a generic pydantic model builds a new class, so the results are
#: memoised rather than rebuilt on every call.
_MODEL_CACHE: dict[tuple[MethodKind, type[Any]], type[Any]] = {}


def _response_model(kind: MethodKind, model: type[Any]) -> type[Any]:
    """The concrete response class for one kind/model pairing."""
    unparameterised = _UNPARAMETERISED.get(kind)
    if unparameterised is not None:
        return unparameterised
    key = (kind, model)
    cached = _MODEL_CACHE.get(key)
    if cached is None:
        cached = _PARAMETERISED[kind][model]
        _MODEL_CACHE[key] = cached
    return cached


def parser_for(
    method: MethodSpec, data_type: DataTypeSpec | None
) -> Callable[[Mapping[str, Any]], Any]:
    """The function that turns this method's response arguments into a result.

    Falls back to the spec's own parser when the shape is irregular, or when the
    data type is unknown - an advertised-but-unmodelled capability still has to
    return *something*, and a raw mapping is more useful than an error.
    """
    if method.kind is MethodKind.CUSTOM or data_type is None:
        return method.parse
    response_model = _response_model(method.kind, data_type.model)

    def parse(arguments: Mapping[str, Any]) -> Any:
        return response_model.model_validate(dict(arguments))

    return parse

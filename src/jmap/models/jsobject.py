"""What JSContact (RFC 9553) and JSCalendar objects have in common.

Both formats are large vocabularies that servers implement unevenly, and a
card or an event is worth most to a client when it round-trips intact. So every
object here is typed, and every object is also forgiving: a property whose value
does not fit its type is kept exactly as it arrived, among the unmodelled ones,
rather than failing the object - and with it the whole ``/get``. The typed
attribute reads ``None``; ``to_wire()`` sends the value back unchanged.

``@type`` is optional on most of these objects, implied by where the object
sits (RFC 9553 §1.3.4). It is kept when a server sends it, and only filled in
for the few types that are never implied - a ``Timestamp`` where a date is
expected, say - so that one built in Python is not read as something else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from pydantic import Field, ValidationError, model_validator

from jmap.models.base import JMAPModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import ModelWrapValidatorHandler
    from pydantic.fields import FieldInfo


class JSObject(JMAPModel):
    """A JSContact or JSCalendar object: typed, and never lost to a bad property."""

    #: The ``@type`` this object must always carry, for the few types it is not
    #: implied for; empty for the rest.
    REQUIRED_TYPE: ClassVar[str] = ""

    #: The object's type name, when it carries one. Aliased: ``@type`` is not an
    #: identifier.
    at_type: str | None = Field(default=None, alias="@type")

    def model_post_init(self, context: Any, /) -> None:
        if self.REQUIRED_TYPE and self.at_type is None:
            self.at_type = self.REQUIRED_TYPE

    @model_validator(mode="wrap")
    @classmethod
    def _keep_what_does_not_fit(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Validate, setting aside - not dropping - any property that fails.

        Only what could have come off the wire is set aside: plain JSON, under a
        wire name. A model of the wrong type, or a property given by a Python
        name that differs from its wire name - ``member_uids`` for ``members`` -
        was written in Python, and still raises.
        """
        try:
            return handler(data)
        except ValidationError as error:
            aside = _misfits(data, error, cls.model_fields)
            if not aside:
                raise
            try:
                model = handler({key: value for key, value in data.items() if key not in aside})
            except ValidationError:
                raise error from None
        # extra="allow" makes this a dict; set aside, the values are extras now.
        cast("dict[str, Any]", model.__pydantic_extra__).update(aside)
        model.__pydantic_fields_set__.update(aside)
        return model


def _misfits(data: Any, error: ValidationError, fields: Mapping[str, FieldInfo]) -> dict[str, Any]:
    """The wire properties of ``data`` that ``error`` blames."""
    if not isinstance(data, Mapping):
        return {}
    wire = cast("Mapping[str, Any]", data)
    python_only = {name for name, field in fields.items() if field.alias != name}
    blamed = {str(problem["loc"][0]) for problem in error.errors() if problem["loc"]}
    return {key: wire[key] for key in blamed - python_only if key in wire and _is_json(wire[key])}


def _is_json(value: Any) -> bool:
    """Whether ``value`` is plain decoded JSON, all the way down."""
    if isinstance(value, dict):
        entries = cast("Mapping[object, Any]", value)
        return all(isinstance(key, str) and _is_json(item) for key, item in entries.items())
    if isinstance(value, list):
        return all(_is_json(item) for item in cast("Sequence[Any]", value))
    return value is None or isinstance(value, str | int | float | bool)


def type_tag(value: Any) -> Any:
    """The ``@type`` of a wire object, or ``None`` for anything else."""
    return cast("Mapping[str, Any]", value).get("@type") if isinstance(value, Mapping) else None


def wire_property(model: JMAPModel, name: str) -> Any:
    """A property by its exact wire name, in its wire form, or ``None`` if unset.

    A modelled property comes back as it would be sent, so the answer is the
    same whether or not this library models it - or could validate the value.
    """
    extra = model.__pydantic_extra__ or {}
    if name in extra:
        return extra[name]
    for field_name, field in type(model).model_fields.items():
        if field.alias == name and field_name in model.model_fields_set:
            dumped = model.model_dump(
                mode="json", by_alias=True, exclude_unset=True, include={field_name}
            )
            return dumped[name]
    return None

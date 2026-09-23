"""Model foundations: camelCase mapping, the ``UNSET`` sentinel, and the lossless
fallback object (RFC 8620 §1.1, §5.1, §5.3).

Three problems, each of which quietly corrupts requests if ignored:

**1. Every wire property is camelCase.** Rather than restate the alias on each of
several hundred fields, :class:`JMAPModel` derives it with ``to_camel`` and
accepts *both* spellings on input, so a raw server payload and Python keyword
arguments validate through the same model.

**2. ``null`` is a value, not an absence.** RFC 8620 gives JSON ``null`` distinct
meanings the client must be able to send: ``Foo/get`` with ``ids: null`` means
"every record of this type" (§5.1), and a ``Mailbox`` with ``parentId: null``
sits at the top level (RFC 8621 §2). Omitting either key means something else
entirely, so ``None`` cannot also stand for "caller said nothing". That needs a
third state, and it exists in two halves:

* for model *fields*, pydantic records what was supplied in ``model_fields_set``
  and :meth:`JMAPModel.to_wire` drops the rest via ``exclude_unset=True``;
* for method *arguments*, "said nothing" is :data:`UNSET`, dropped by
  :func:`omit_unset`.

With both halves in place ``None`` always and only serialises to a literal JSON
``null``.

**3. Not every data type has a model.** ``extra="allow"`` keeps unmodelled
properties on typed objects (they land in ``model_fields_set``, so they survive
``exclude_unset`` - asserted in ``tests/unit/test_base.py`` so that a pydantic
upgrade which changes it fails loudly instead of silently stripping unknown
fields from every write). For data types with no registered class at all,
:class:`JMAPObject` exposes the decoded payload unchanged. It is a ``Mapping``
and not a dynamic model because JMAP property names are not identifiers:
``header:Subject`` (RFC 8621 §4.1.2), ``digest:sha-256`` and ``data:asBase64``
(RFC 9404 §5) have to stay addressable exactly as the server spelled them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Final,
    Literal,
    Self,
    TypeAlias,
    TypeVar,
    cast,
)

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, ValidationError
from pydantic.alias_generators import to_camel
from pydantic_core import core_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "UNSET",
    "JMAPModel",
    "JMAPObject",
    "Unset",
    "UnsetType",
    "omit_unset",
    "validation_summary",
]

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def validation_summary(error: ValidationError) -> str:
    """One line saying where a payload went wrong and how.

    For wrapping a pydantic failure in one of this library's errors, whose
    message should fit on a line: pydantic's own rendering runs to a paragraph
    per field, with a documentation link in each.
    """
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "the top level"
    others = error.error_count() - 1
    return f"{where}: {first['msg']}" + (f" (and {others} more)" if others else "")


class JMAPModel(BaseModel):
    """Base for every typed spec object.

    ``extra="allow"`` is deliberate: a server may return properties from a spec
    revision or extension we do not model yet, and dropping them would corrupt
    the read-modify-write cycle that ``Foo/set`` updates rely on.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        extra="allow",
    )

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the JSON shape the server expects.

        ``exclude_unset`` is the field half of the tri-state rule: a field the
        caller never touched is absent, while one explicitly set to ``None`` is
        sent as ``null``.
        """
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> Self:
        """Validate a decoded server payload into this model."""
        return cls.model_validate(data)


#: Longest payload rendered by :meth:`JMAPObject.__repr__`. An ``Email`` carrying
#: a decoded body part is megabytes wide, and an untruncated repr reaches logs
#: and tracebacks far more often than anyone intends.
_REPR_MAX_CHARS: Final = 160


class JMAPObject(Mapping[str, Any]):
    """A read-only view of a raw wire object, used when no model is registered.

    Two access styles, deliberately not equivalent:

    * item access is the *raw* wire key, unchanged, so non-identifier properties
      stay reachable: ``obj["header:Subject"]``, ``obj["digest:sha-256"]``;
    * attribute access translates snake_case to camelCase, so ``obj.mailbox_ids``
      finds ``"mailboxIds"``.

    Values come back exactly as decoded - a nested object is a plain ``dict``, not
    another :class:`JMAPObject` - because this type's entire job is to not
    reinterpret anything.
    """

    __slots__ = ("_raw",)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: Any, _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Let pydantic use this as a field type.

        Without it, ``GetResponse[JMAPObject]`` cannot be built - which is
        exactly the shape an advertised-but-unmodelled capability produces, so
        the fallback would fail at the one moment it exists for.
        """
        return core_schema.no_info_plain_validator_function(cls._validate)

    @classmethod
    def _validate(cls, value: Any) -> JMAPObject:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(cast("Mapping[str, Any]", value))
        # ValueError, not TypeError: pydantic converts only ValueError into a
        # ValidationError, which is what carries the field path. A TypeError
        # escapes as-is and the caller loses which property was wrong.
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")

    def __init__(self, raw: Mapping[str, Any]) -> None:
        # A ``dict`` is adopted rather than copied: it is already ours (freshly
        # decoded from the response body), and copying every record of a
        # 10 000-item ``Foo/get`` would double peak memory for nothing.
        self._raw: dict[str, Any] = raw if isinstance(raw, dict) else dict(raw)

    @property
    def raw(self) -> dict[str, Any]:
        """The underlying wire dict."""
        return self._raw

    def __getitem__(self, key: str) -> Any:
        return self._raw[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __getattr__(self, name: str) -> Any:
        # Dunder and private lookups must never reach the payload: ``copy``,
        # ``pickle`` and ``hasattr`` probe for them, and answering from wire data
        # would make this object lie about protocols it does not implement.
        if name.startswith("_"):
            raise AttributeError(name)
        for key in (name, to_camel(name)):
            if key in self._raw:
                return self._raw[key]
        # AttributeError, not KeyError, so ``getattr(obj, x, default)`` and
        # ``hasattr`` behave the way every caller assumes they do.
        raise AttributeError(f"{type(self).__name__} has no property {name!r}")

    def as_(self, model: type[_ModelT]) -> _ModelT:
        """Validate this object into ``model`` on demand."""
        return model.model_validate(self._raw)

    def to_wire(self) -> dict[str, Any]:
        """The wire dict, for symmetry with :meth:`JMAPModel.to_wire`.

        Returns the underlying dict itself, not a copy; callers serialise it
        immediately and must not mutate it.
        """
        return self._raw

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JMAPObject):
            return self._raw == other._raw
        if isinstance(other, Mapping):
            # Every JSON object is str-keyed, but ``isinstance`` cannot say so.
            return self._raw == dict(cast("Mapping[str, Any]", other))
        return NotImplemented

    def __repr__(self) -> str:
        body = repr(self._raw)
        if len(body) > _REPR_MAX_CHARS:
            body = f"{body[: _REPR_MAX_CHARS - 3]}..."
        return f"JMAPObject({body})"


class UnsetType:
    """The absence of a method argument, distinct from ``None``.

    ``None`` is a legal, meaningful JMAP value (see the module docstring), so the
    usual ``arg: T | None = None`` idiom cannot express "not supplied". Arguments
    that need the distinction default to :data:`UNSET` and are filtered by
    :func:`omit_unset`.
    """

    __slots__ = ()

    _instance: ClassVar[UnsetType | None] = None

    def __new__(cls) -> UnsetType:
        # A singleton so ``is UNSET`` is the identity check callers write, and so
        # a stray ``UnsetType()`` cannot produce a value that fails it.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> Literal[False]:
        # Literal[False] rather than bool so type checkers narrow ``if arg:``
        # branches correctly.
        return False

    def __repr__(self) -> str:
        return "UNSET"

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        # Sentinels embedded in argument dicts get copied by callers; a copy that
        # is not ``UNSET`` would be silently serialised as a value.
        return self

    def __reduce__(self) -> str:
        # Pickling by name preserves identity across a round trip.
        return "UNSET"


#: The "argument not supplied" sentinel. See the module docstring.
UNSET: Final[UnsetType] = UnsetType()

#: Annotation spelling: ``ids: list[Id] | None | Unset = UNSET``.
Unset: TypeAlias = UnsetType


def omit_unset(**kwargs: Any) -> dict[str, Any]:
    """Drop :data:`UNSET` arguments, keeping every other value including ``None``."""
    return {name: value for name, value in kwargs.items() if value is not UNSET}

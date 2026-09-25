"""Exposing a batch's capabilities as attributes.

``client.mail.email.get(...)`` reads better than
``batch.add("Email/get", {...})``, but the two must not diverge: the attributes
are built from the same :class:`~jmap.capabilities.registry.ActiveCapabilities`
the raw path uses, so a server that does not advertise a capability simply has no
attribute for it.

That is the point. ``client.calendars`` raising ``AttributeError`` against a
mail-only server is more useful than a namespace that exists and fails on every
call, and it matches how the entity façades already work - the surface reflects
the server rather than the spec.

Attribute names are snake_case of the JMAP type: ``Email`` -> ``email``,
``EmailSubmission`` -> ``email_submission``, ``SearchSnippet`` -> ``search_snippet``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from jmap.api.entity import entity_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from jmap.batch import Batch
    from jmap.capabilities.registry import ActiveCapabilities
    from jmap.capabilities.spec import CapabilitySpec

_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def attribute_name(type_name: str) -> str:
    """``EmailSubmission`` -> ``email_submission``."""
    return _CAMEL_BOUNDARY.sub("_", type_name).lower()


class CapabilityNamespace:
    """The data types of one capability, as attributes on a batch."""

    __slots__ = ("_entities", "_spec")

    def __init__(
        self, batch: Batch, spec: CapabilitySpec, companions: Sequence[CapabilitySpec] = ()
    ) -> None:
        self._spec = spec
        self._entities: dict[str, Any] = {
            attribute_name(data_type.name): entity_for(batch, spec, data_type, companions)
            for data_type in spec.data_types
            # A push-only pseudo-type has no methods to expose.
            if not data_type.push_only
        }

    def __getattr__(self, name: str) -> Any:
        try:
            return self._entities[name]
        except KeyError:
            raise AttributeError(
                f"{self._spec.urn} has no data type {name!r}; it offers "
                f"{', '.join(sorted(self._entities)) or 'none'}"
            ) from None

    def __dir__(self) -> list[str]:
        # So tab-completion shows what this server actually offers.
        return [*super().__dir__(), *self._entities]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entities)

    def __repr__(self) -> str:
        return f"CapabilityNamespace({self._spec.urn!r}, {sorted(self._entities)})"


class Namespaces:
    """Every supported capability, keyed by its short attribute name."""

    __slots__ = ("_capabilities", "_namespaces")

    def __init__(self, batch: Batch, capabilities: ActiveCapabilities) -> None:
        self._capabilities = capabilities
        # A capability with no attribute of its own - `:calendars:parse`,
        # `:contacts:parse`, `:principals:availability` - only adds methods to
        # types another capability declares, so it lends them to that namespace.
        companions = tuple(spec for spec in capabilities.specs.values() if spec.attr is None)
        self._namespaces: dict[str, CapabilityNamespace] = {
            attr: CapabilityNamespace(batch, spec, companions)
            for attr, spec in capabilities.attrs.items()
        }

    def __getattr__(self, name: str) -> CapabilityNamespace:
        try:
            return self._namespaces[name]
        except KeyError:
            raise AttributeError(
                f"this server does not offer a {name!r} capability; it advertises "
                f"{', '.join(sorted(self._namespaces)) or 'none'}"
            ) from None

    def __dir__(self) -> list[str]:
        return [*super().__dir__(), *self._namespaces]

    def __contains__(self, name: object) -> bool:
        return name in self._namespaces

    def __iter__(self) -> Iterator[str]:
        return iter(self._namespaces)

    def __repr__(self) -> str:
        return f"Namespaces({sorted(self._namespaces)})"

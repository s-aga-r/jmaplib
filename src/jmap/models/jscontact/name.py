"""Naming the entity, and how to address it (RFC 9553 §2.2)."""

from __future__ import annotations

from jmap.models.jsobject import JSObject


class NameComponent(JSObject):
    """One part of a name (§2.2.1.2).

    ``kind`` is ``title``, ``given``, ``given2``, ``surname``, ``surname2``,
    ``credential``, ``generation`` or ``separator``.
    """

    value: str | None = None
    kind: str | None = None
    phonetic: str | None = None


class Name(JSObject):
    """The entity's name, in parts, in full, or both (§2.2.1.1)."""

    components: list[NameComponent] | None = None
    #: Whether ``components`` are in display order; otherwise only ``kind`` counts.
    is_ordered: bool | None = None
    default_separator: str | None = None
    full: str | None = None
    #: Component kind -> the string to sort by instead, e.g. ``{"surname": "Doe"}``.
    sort_as: dict[str, str] | None = None
    phonetic_script: str | None = None
    phonetic_system: str | None = None


class Nickname(JSObject):
    """A nickname (§2.2.2)."""

    name: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None


class OrgUnit(JSObject):
    """A department or other unit of an organization (§2.2.3)."""

    name: str | None = None
    sort_as: str | None = None


class Organization(JSObject):
    """An organization the entity belongs to (§2.2.3)."""

    name: str | None = None
    #: Largest first: ``[{"name": "Research"}, {"name": "Lab 5"}]``.
    units: list[OrgUnit] | None = None
    sort_as: str | None = None
    contexts: dict[str, bool] | None = None


class Pronouns(JSObject):
    """Pronouns to refer to the entity by, e.g. ``"they/them"`` (§2.2.4)."""

    pronouns: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None


class SpeakToAs(JSObject):
    """How to address the entity (§2.2.4)."""

    #: ``animate``, ``common``, ``feminine``, ``inanimate``, ``masculine`` or ``neuter``.
    grammatical_gender: str | None = None
    pronouns: dict[str, Pronouns] | None = None


class Title(JSObject):
    """A job title or role (§2.2.5)."""

    name: str | None = None
    #: ``title`` (the default) or ``role``.
    kind: str | None = None
    #: A key of the card's ``organizations``, not a JMAP id.
    organization_id: str | None = None

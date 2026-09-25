"""Reaching the entity: addresses, numbers and services (RFC 9553 §2.3 - §2.5)."""

from __future__ import annotations

from jmap.models.jsobject import JSObject


class EmailAddress(JSObject):
    """An email address (§2.3.1)."""

    address: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None
    label: str | None = None


class OnlineService(JSObject):
    """An account on a service: ``service`` names it, ``uri`` or ``user`` finds you (§2.3.2)."""

    service: str | None = None
    uri: str | None = None
    user: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None
    label: str | None = None


class Phone(JSObject):
    """A phone number (§2.3.3)."""

    number: str | None = None
    #: A set-as-map of ``mobile``, ``voice``, ``text``, ``video``, ``textphone``,
    #: ``fax`` and ``pager``.
    features: dict[str, bool] | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None
    label: str | None = None


class LanguagePref(JSObject):
    """A language the entity prefers to be contacted in (§2.3.4)."""

    #: A language tag (RFC 5646), e.g. ``"de-AT"``.
    language: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None


class SchedulingAddress(JSObject):
    """Where to send calendar invitations, e.g. a ``mailto:`` URI (§2.4.2)."""

    uri: str | None = None
    contexts: dict[str, bool] | None = None
    pref: int | None = None
    label: str | None = None


class AddressComponent(JSObject):
    """One part of an address (§2.5.1.2).

    ``kind`` is ``room``, ``apartment``, ``floor``, ``building``, ``number``,
    ``name`` (the street), ``block``, ``subdistrict``, ``district``,
    ``locality``, ``region``, ``postcode``, ``country``, ``direction``,
    ``landmark``, ``postOfficeBox`` or ``separator``.
    """

    value: str | None = None
    kind: str | None = None
    phonetic: str | None = None


class Address(JSObject):
    """A postal address or location (§2.5.1.1)."""

    components: list[AddressComponent] | None = None
    is_ordered: bool | None = None
    #: ISO 3166-1 alpha-2, e.g. ``"GB"``.
    country_code: str | None = None
    #: A ``geo:`` URI.
    coordinates: str | None = None
    #: An IANA time zone name.
    time_zone: str | None = None
    #: ``private``, ``work``, ``billing`` or ``delivery``.
    contexts: dict[str, bool] | None = None
    full: str | None = None
    default_separator: str | None = None
    pref: int | None = None
    phonetic_script: str | None = None
    phonetic_system: str | None = None

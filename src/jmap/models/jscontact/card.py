"""The Card (RFC 9553 §2)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from jmap.models.jscontact.contact import (
    Address,
    EmailAddress,
    LanguagePref,
    OnlineService,
    Phone,
    SchedulingAddress,
)
from jmap.models.jscontact.name import Name, Nickname, Organization, SpeakToAs, Title
from jmap.models.jscontact.personal import Anniversary, Note, PersonalInfo
from jmap.models.jscontact.resource import Calendar, CryptoKey, Directory, Link, Media
from jmap.models.jsdates import UTCDateTime
from jmap.models.jsobject import JSObject, wire_property


class Relation(JSObject):
    """How another card relates to this one (§2.1.8).

    A set-as-map of relation types - ``friend``, ``colleague``, ``spouse`` and
    the rest of vCard's RELATED types. Empty means related, but not saying how.
    """

    relation: dict[str, bool] | None = None


class Card(JSObject):
    """A person, organization, group or other entity (§2).

    Every property is optional here, though RFC 9553 makes ``@type``,
    ``version`` and ``uid`` mandatory: a server fills them in, and Stalwart
    does not return ``uid`` at all. The maps - ``emails``, ``phones`` and the
    rest - are keyed by ids local to the card, which survive edits.
    """

    # -- metadata (§2.1) ---------------------------------------------------- #
    #: ``"1.0"``.
    version: str | None = None
    created: UTCDateTime | None = None
    #: ``individual`` (the default), ``group``, ``org``, ``location``, ``device``
    #: or ``application``.
    kind: str | None = None
    #: A language tag (RFC 5646) for the card's text.
    language: str | None = None
    #: A group's members: uid -> True. Named for what its keys are, since
    #: :meth:`jmap.models.contacts.ContactCard.members` lists them.
    member_uids: dict[str, bool] | None = Field(default=None, alias="members")
    prod_id: str | None = None
    #: uid of another card -> how it relates to this one.
    related_to: dict[str, Relation] | None = None
    #: The same entity across systems - not a JMAP id.
    uid: str | None = None
    updated: UTCDateTime | None = None

    # -- name and organization (§2.2) --------------------------------------- #
    name: Name | None = None
    nicknames: dict[str, Nickname] | None = None
    organizations: dict[str, Organization] | None = None
    speak_to_as: SpeakToAs | None = None
    titles: dict[str, Title] | None = None

    # -- contact (§2.3) ----------------------------------------------------- #
    emails: dict[str, EmailAddress] | None = None
    online_services: dict[str, OnlineService] | None = None
    phones: dict[str, Phone] | None = None
    preferred_languages: dict[str, LanguagePref] | None = None

    # -- calendaring and scheduling (§2.4) ---------------------------------- #
    calendars: dict[str, Calendar] | None = None
    scheduling_addresses: dict[str, SchedulingAddress] | None = None

    # -- address and location (§2.5) ---------------------------------------- #
    addresses: dict[str, Address] | None = None

    # -- resources (§2.6) --------------------------------------------------- #
    crypto_keys: dict[str, CryptoKey] | None = None
    directories: dict[str, Directory] | None = None
    links: dict[str, Link] | None = None
    #: Named apart from ``media`` for the same reason as ``member_uids``:
    #: :meth:`jmap.models.contacts.ContactCard.media` reads it.
    media_resources: dict[str, Media] | None = Field(default=None, alias="media")

    # -- multilingual (§2.7) ------------------------------------------------ #
    #: Language tag -> a PatchObject over this card, e.g.
    #: ``{"uk-UA": {"name/full": "..."}}``.
    localizations: dict[str, dict[str, Any]] | None = None

    # -- additional (§2.8) -------------------------------------------------- #
    anniversaries: dict[str, Anniversary] | None = None
    keywords: dict[str, bool] | None = None
    notes: dict[str, Note] | None = None
    personal_info: dict[str, PersonalInfo] | None = None

    def jscontact(self, name: str) -> Any:
        """Read a JSContact property by its exact wire name, in its wire form.

        For a vendor property, or one this library models but that arrived in
        a shape it could not read - both kept as they came.
        """
        return wire_property(self, name)

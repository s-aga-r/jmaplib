"""Contacts (RFC 9610, JSContact RFC 9553).

A ContactCard *is* a JSContact Card with two JMAP properties bolted on, so the
same choice applies as for calendars: the JMAP layer is modelled here and the
JSContact body rides losslessly through ``extra``. Unlike calendars, that is not
because the target is moving - RFC 9553 is stable - but because a JSContact Card
is a large, deeply nested vocabulary whose value to a JMAP *client* is mostly in
being round-tripped intact.

Three things about this type are easy to get wrong:

**``id`` and ``uid`` are different identifiers.** ``id`` is JMAP's, server-set and
account-scoped; ``uid`` is JSContact's, and is what identifies the same person
across systems. RFC 9610 §3 allows them to differ and requires ``uid`` to be
unique within an account - so deduplicating on ``id`` finds nothing, and
addressing a card by ``uid`` addresses nothing.

**A card must belong to at least one address book, always.** ``addressBookIds`` is
a set-as-map whose values must all be ``true``; patching it to ``{}`` is invalid
rather than a way to delete the card.

**A photo is a blob, not a data URI.** §3 has servers return ``blobId`` and omit
``uri`` for ``data:`` media, and lets clients send ``blobId`` in place of ``uri``
- which is the difference between a contact list that streams thumbnails on demand
and one that base64-encodes every face into the response.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from jmap.core.narrow import as_object, is_object
from jmap.models.base import JMAPModel

#: RFC 9610 §2.3. Destroying an address book that still holds cards, with
#: ``onDestroyRemoveContents`` false.
ADDRESS_BOOK_HAS_CONTENTS = "addressBookHasContents"

#: RFC 9553 §2.1.4. The ``kind`` values a Card may take. A card whose kind is
#: ``group`` uses ``members`` to name the uids it contains.
KIND_INDIVIDUAL = "individual"
KIND_GROUP = "group"
KIND_ORG = "org"
KIND_LOCATION = "location"
KIND_DEVICE = "device"
KIND_APPLICATION = "application"

#: RFC 9610 §3.3.2. Sorting a server MUST support, and what it merely SHOULD.
REQUIRED_SORTS = frozenset({"created", "updated"})
OPTIONAL_SORTS = frozenset({"name/given", "name/surname", "name/surname2"})


class AddressBookRights(JMAPModel):
    """What the requesting user may do with an address book (RFC 9610 §2)."""

    may_read: bool = False
    #: Covers creating, modifying and destroying cards, and moving them in or out.
    may_write: bool = False
    may_share: bool = False
    #: The address book itself, not its contents.
    may_delete: bool = False


class AddressBook(JMAPModel):
    """A named collection of contact cards (RFC 9610 §2)."""

    id: str | None = None
    #: Must not be empty, and is bounded at 255 octets as UTF-8.
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    #: Server-set, and true for at most one address book per account.
    is_default: bool | None = None
    #: Defaults to false for a shared book and true for one you made yourself, so
    #: a client that ignores it hides address books the user just created.
    is_subscribed: bool | None = None
    #: ``None`` also means "this server does not implement RFC 9670 at all", which
    #: is not the same as "shared with nobody" - though both render the same way.
    share_with: dict[str, AddressBookRights] | None = None
    #: Server-set.
    my_rights: AddressBookRights | None = None


class Media(JMAPModel):
    """One photo, sound or logo attached to a card (RFC 9553 §2.6.4).

    RFC 9610 §3 adds ``blobId`` and has servers prefer it over a ``data:`` URI, so
    a contact list fetches thumbnails on demand rather than carrying every face
    base64-encoded in the response. ``mediaType`` must be set alongside it.
    """

    #: The JSContact discriminator, e.g. ``"Media"``.
    at_type: str | None = Field(default=None, alias="@type")
    kind: str | None = None
    uri: str | None = None
    #: RFC 9610 §7.5.3's addition. Present instead of ``uri`` for binary content.
    blob_id: str | None = None
    media_type: str | None = None

    @property
    def is_blob_backed(self) -> bool:
        return self.blob_id is not None


class ContactCard(JMAPModel):
    """A person, company or group (RFC 9610 §3).

    A JSContact Card plus ``id`` and ``addressBookIds``. Read the JSContact half
    with :meth:`jscontact`; it round-trips unchanged whether or not this library
    knows the property.
    """

    id: str | None = None
    #: Address book id -> True. Must be non-empty at all times.
    address_book_ids: dict[str, bool] | None = None

    @property
    def address_books(self) -> list[str]:
        """The address books this card is in, as a plain list."""
        return [key for key, value in (self.address_book_ids or {}).items() if value]

    @property
    def uid(self) -> str | None:
        """The JSContact ``uid`` - *not* :attr:`id`. See the module docstring."""
        return _text(self.jscontact("uid"))

    @property
    def kind(self) -> str | None:
        """``individual``, ``group``, ``org`` and friends (RFC 9553 §2.1.4)."""
        return _text(self.jscontact("kind"))

    @property
    def is_group(self) -> bool:
        return self.kind == KIND_GROUP

    def members(self) -> list[str]:
        """The uids a group card contains (RFC 9553 §2.1.6).

        A set-as-map like every other JSContact set, so the values are ``true``
        and the keys are what matter. They are **uids**, not JMAP ids, so
        resolving them needs a ``uid`` filter rather than a ``/get``.
        """
        members = self.jscontact("members")
        if not is_object(members):
            return []
        return [key for key, value in as_object(members).items() if value]

    def media(self) -> dict[str, Media]:
        """The card's photos, sounds and logos, validated (RFC 9553 §2.6.4).

        A map keyed by JSContact's own media id. Provided because the interesting
        property - whether an entry is blob-backed rather than a ``data:`` URI -
        is otherwise reachable only by validating the raw dicts by hand, and that
        distinction is the whole reason a contact list can stream thumbnails
        instead of carrying every face inline.

        Entries that do not validate are skipped rather than raising: ``media`` is
        an extension point, and one odd entry should not cost the rest.
        """
        raw = self.jscontact("media")
        if not is_object(raw):
            return {}
        found: dict[str, Media] = {}
        for key, entry in as_object(raw).items():
            try:
                found[key] = Media.model_validate(entry)
            except ValueError:
                continue
        return found

    def photos(self) -> dict[str, Media]:
        """Just the ``photo``-kind media, which is what a contact list shows."""
        return {key: item for key, item in self.media().items() if item.kind == "photo"}

    def jscontact(self, name: str) -> Any:
        """Read a JSContact property by its exact wire name."""
        return (self.__pydantic_extra__ or {}).get(name)


def _text(value: Any) -> str | None:
    """A JSContact string property, or ``None`` for anything else.

    These come out of ``extra``, so nothing has type-checked them; a server
    sending a number where a string belongs should not make the accessor lie
    about its return type.
    """
    return value if isinstance(value, str) else None

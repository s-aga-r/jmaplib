"""Contacts (RFC 9610, JSContact RFC 9553).

A ContactCard *is* a JSContact Card with two JMAP properties bolted on: the
Card and everything in it are modelled in :mod:`jmap.models.jscontact`, and
this adds the JMAP layer. A card's value to a JMAP client is mostly in being
round-tripped intact, so the models keep what they cannot read - a vendor
property, or a value of the wrong shape - exactly as it arrived.

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

from jmap.core.narrow import as_object, is_object
from jmap.models.base import JMAPModel
from jmap.models.jscontact import Card

# Re-exported: Media lived here before JSContact had a module of its own.
from jmap.models.jscontact import Media as Media

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


class ContactCard(Card):
    """A person, company or group (RFC 9610 §3).

    A JSContact :class:`~jmap.models.jscontact.Card` plus ``id`` and
    ``addressBookIds``.
    """

    id: str | None = None
    #: Address book id -> True. Must be non-empty at all times.
    address_book_ids: dict[str, bool] | None = None

    @property
    def address_books(self) -> list[str]:
        """The address books this card is in, as a plain list."""
        return [key for key, value in (self.address_book_ids or {}).items() if value]

    @property
    def is_group(self) -> bool:
        return self.kind == KIND_GROUP

    def members(self) -> list[str]:
        """The uids a group card contains (RFC 9553 §2.1.6).

        A set-as-map like every other JSContact set, so the values are ``true``
        and the keys are what matter. They are **uids**, not JMAP ids, so
        resolving them needs a ``uid`` filter rather than a ``/get``.
        """
        return [uid for uid, member in (self.member_uids or {}).items() if member]

    def media(self) -> dict[str, Media]:
        """The card's photos, sounds and logos (RFC 9553 §2.6.4).

        A map keyed by JSContact's own media id - ``media_resources``, or, when
        one entry kept that from validating, every other entry: ``media`` is an
        extension point, and one odd entry should not cost the rest.
        """
        if self.media_resources is not None:
            return dict(self.media_resources)
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


class ParsedCards(JMAPModel):
    """``ContactCard/parse`` - a Stalwart extension; no RFC defines one for contacts.

    ``parsed`` maps a blob id to **one** Card. That is the opposite trap from the
    calendars ``/parse``, whose ``parsed`` values are arrays - so a reader porting
    code between the two will get exactly one of them wrong. Verified against
    Stalwart's implementation, which runs each blob through a single-card vCard
    parse rather than splitting a multi-card file.

    ``not_found`` and ``not_parsable`` are arrays of blob ids, as for
    ``Email/parse``.
    """

    account_id: str | None = None
    parsed: dict[str, ContactCard] | None = None
    not_found: list[str] | None = None
    not_parsable: list[str] | None = None

    def card_of(self, blob_id: str) -> ContactCard | None:
        """The card parsed out of one blob, or ``None`` if it yielded nothing."""
        return (self.parsed or {}).get(blob_id)

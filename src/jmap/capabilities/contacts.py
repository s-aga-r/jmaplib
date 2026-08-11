"""Contacts - RFC 9610, plus the two vendor URNs that predate it.

The obvious design here is wrong, and it is worth saying why. JMAP contacts went
through a long pre-RFC period in which Fastmail and Cyrus shipped ``Contact`` and
``ContactGroup`` data types, and RFC 9610 replaced both with ``AddressBook`` and
``ContactCard`` around JSContact. That looks like two flavours of one URN, needing
a predicate to tell them apart from the advertised capability object.

It is not. **The legacy methods are gated by vendor URNs**, not by
``urn:ietf:params:jmap:contacts``:

* ``https://www.fastmail.com/dev/contacts``
* ``https://cyrusimap.org/ns/jmap/contacts``

So they are simply three capabilities, and a server may advertise several at once
- Cyrus 3.10 exposes both models concurrently. Registering them separately means
``using`` derivation gets this right for free, which matters because the failure
mode is opaque: calling ``Contact/get`` with only the IETF URN in ``using`` earns
``unknownMethod`` on a server that fully implements the method, and the error
names the method rather than the missing capability.

The legacy types carry no modelled properties. Their vocabulary was never
standardised, the two vendors diverged, and both are on a removal path - so they
resolve to :class:`~jmap.models.base.JMAPObject`, which round-trips whatever the
server sends and keeps every key addressable by its exact wire name. Inventing
field names for a vocabulary nobody ratified would be worse than not having them.

The legacy method inventory is also *not* the standard six. ``Contact`` has no
``/queryChanges`` while ``ContactGroup`` does have ``/query`` - which the composed
façades reproduce, so ``client.fastmail_contacts.contact`` genuinely has no
``.query_changes``.
"""

from __future__ import annotations

from typing import Any, Final

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.contacts import AddressBook, ContactCard

CONTACTS_URN: Final = "urn:ietf:params:jmap:contacts"
#: Fastmail's vendor URN. Lowercase, ``www.``, plural, no trailing path.
FASTMAIL_CONTACTS_URN: Final = "https://www.fastmail.com/dev/contacts"
#: Cyrus's, advertised only when ``jmap_nonstandard_extensions`` is enabled.
CYRUS_CONTACTS_URN: Final = "https://cyrusimap.org/ns/jmap/contacts"

#: Both vendor URNs, for a caller that wants to ask "can this server do legacy
#: contacts at all?" without caring which vendor.
LEGACY_CONTACT_URNS: Final = frozenset({FASTMAIL_CONTACTS_URN, CYRUS_CONTACTS_URN})


class ContactsCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:contacts`` object (RFC 9610 §1.4.1)."""

    #: How many address books one card may be in. ``None`` means no limit beyond
    #: the number of address books that exist - **or** that the capability object
    #: would not parse; see :meth:`of`.
    max_address_books_per_card: int | None = None
    may_create_address_book: bool = False

    @classmethod
    def of(cls, value: Any) -> ContactsCapability:
        """Parse an advertised capability object, tolerating a malformed one.

        All or nothing, like every other capability object in this library: one
        field the server got wrong discards the valid ones beside it. That keeps a
        bad object from making an otherwise working session unusable, at the cost
        of one ambiguity worth knowing about - after a failed parse,
        ``max_address_books_per_card is None`` reads as "no limit advertised" when
        the truth is "could not tell". The conservative move on that path is to
        treat the limit as unknown rather than absent.
        """
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def looks_like_rfc9610(capability: Any) -> bool:
    """Whether an advertised capability object looks like RFC 9610's.

    Weak evidence, and deliberately named as a guess. RFC 9610 §1.4.1 makes both
    fields MUST-contain, so their presence is a good signal - but their *absence*
    cannot separate "legacy server" from "RFC 9610 server violating that MUST",
    and a real Fastmail capture shows the IETF URN advertised as a bare ``{}``
    alongside a legacy-only account.

    Nothing inside the library consults this, deliberately: resolution goes by URN,
    which is unambiguous. It is here for a caller inspecting a session by hand -
    and to put the reasoning somewhere findable, since "look at the capability
    object" is the approach a reader will otherwise reinvent.

    Note there is no ``isRFC`` flag to look for. Fastmail advertises one on
    *calendars*, and a lookup that expects the same on contacts reads as absent
    and picks the wrong model silently.
    """
    if not isinstance(capability, dict):
        return False
    return "mayCreateAddressBook" in capability or "maxAddressBooksPerCard" in capability


CONTACTS: Final = CapabilitySpec(
    urn=CONTACTS_URN,
    attr="contacts",
    reference="RFC 9610",
    account_value=ContactsCapability,
    data_types=(
        DataTypeSpec(name="AddressBook", model=AddressBook, shareable=True),
        DataTypeSpec(name="ContactCard", model=ContactCard),
    ),
    methods=(
        # -- AddressBook (§2). No /query: the RFC defines none. -- #
        MethodSpec("AddressBook/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("AddressBook/changes", MethodKind.CHANGES),
        MethodSpec(
            "AddressBook/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={
                "onDestroyRemoveContents": "remove the cards inside rather than failing",
                "onSuccessSetIsDefault": "make this the default; silently ignored if refused",
            },
        ),
        # -- ContactCard (§3) -- #
        MethodSpec("ContactCard/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("ContactCard/changes", MethodKind.CHANGES),
        MethodSpec("ContactCard/query", MethodKind.QUERY),
        MethodSpec("ContactCard/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec("ContactCard/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS),
        MethodSpec("ContactCard/copy", MethodKind.COPY, mutating=True),
    ),
)


def _legacy(urn: str, attr: str, reference: str) -> CapabilitySpec:
    """One vendor's pre-RFC contacts capability.

    The two vendors ship the same method inventory, so it is described once. Note
    what is *missing*: ``Contact`` has no ``/queryChanges``, and ``ContactGroup``
    has a ``/query`` that the standard six would not have given it.
    """
    return CapabilitySpec(
        urn=urn,
        attr=attr,
        reference=reference,
        data_types=(
            # No model: the vocabulary was never standardised and the two vendors
            # diverged. JMAPObject keeps every key addressable as sent.
            DataTypeSpec(name="Contact"),
            DataTypeSpec(name="ContactGroup"),
        ),
        methods=(
            MethodSpec("Contact/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
            MethodSpec("Contact/changes", MethodKind.CHANGES),
            MethodSpec("Contact/query", MethodKind.QUERY),
            MethodSpec("Contact/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS),
            MethodSpec("Contact/copy", MethodKind.COPY, mutating=True),
            MethodSpec("ContactGroup/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
            MethodSpec("ContactGroup/changes", MethodKind.CHANGES),
            MethodSpec("ContactGroup/query", MethodKind.QUERY),
            MethodSpec(
                "ContactGroup/set",
                MethodKind.SET,
                mutating=True,
                chunk_by=LimitKey.SET_OBJECTS,
            ),
        ),
    )


FASTMAIL_CONTACTS: Final = _legacy(
    FASTMAIL_CONTACTS_URN, "fastmail_contacts", "Fastmail vendor extension"
)
CYRUS_CONTACTS: Final = _legacy(CYRUS_CONTACTS_URN, "cyrus_contacts", "Cyrus vendor extension")

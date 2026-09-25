"""JSContact (RFC 9553): the Card and every object inside it.

A JMAP ContactCard (:class:`jmap.models.contacts.ContactCard`) is a Card with
two JMAP properties added. Each object is typed and forgiving alike - see
:mod:`jmap.models.jsobject` - so a server's odd value costs one property, kept
as it came, and never the card.
"""

from __future__ import annotations

from jmap.models.jscontact.card import Card, Relation
from jmap.models.jscontact.contact import (
    Address,
    AddressComponent,
    EmailAddress,
    LanguagePref,
    OnlineService,
    Phone,
    SchedulingAddress,
)
from jmap.models.jscontact.name import (
    Name,
    NameComponent,
    Nickname,
    Organization,
    OrgUnit,
    Pronouns,
    SpeakToAs,
    Title,
)
from jmap.models.jscontact.personal import (
    Anniversary,
    AnniversaryDate,
    Author,
    Note,
    PartialDate,
    PersonalInfo,
    Timestamp,
)
from jmap.models.jscontact.resource import (
    Calendar,
    CryptoKey,
    Directory,
    Link,
    Media,
    Resource,
)

__all__ = [
    "Address",
    "AddressComponent",
    "Anniversary",
    "AnniversaryDate",
    "Author",
    "Calendar",
    "Card",
    "CryptoKey",
    "Directory",
    "EmailAddress",
    "LanguagePref",
    "Link",
    "Media",
    "Name",
    "NameComponent",
    "Nickname",
    "Note",
    "OnlineService",
    "OrgUnit",
    "Organization",
    "PartialDate",
    "PersonalInfo",
    "Phone",
    "Pronouns",
    "Relation",
    "Resource",
    "SchedulingAddress",
    "SpeakToAs",
    "Timestamp",
    "Title",
]

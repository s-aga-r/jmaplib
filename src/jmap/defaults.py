"""The capability registry a client gets when the caller supplies none.

Kept separate from :mod:`jmap.capabilities.registry` so that constructing a
registry stays a pure act of registration, and separate from the client so that
adding a capability does not mean touching either shell.

A fresh :class:`~jmap.capabilities.registry.Registry` is built per call rather
than shared as a module-level singleton: a registry is mutable, and an
application that registers a private capability on it should not be silently
changing what every other client in the process can speak.
"""

from __future__ import annotations

from jmap.capabilities.blob import BLOB
from jmap.capabilities.calendars import AVAILABILITY, CALENDARS, CALENDARS_PARSE
from jmap.capabilities.contacts import (
    CONTACTS,
    CONTACTS_PARSE,
    CYRUS_CONTACTS,
    FASTMAIL_CONTACTS,
)
from jmap.capabilities.core import CORE
from jmap.capabilities.files import FILENODE
from jmap.capabilities.mail import (
    MAIL,
    SMIME_VERIFY,
    SUBMISSION,
    VACATION,
)
from jmap.capabilities.mdn import MDN_CAPABILITY
from jmap.capabilities.principals import PRINCIPALS, PRINCIPALS_OWNER
from jmap.capabilities.push import VAPID, WEBSOCKET
from jmap.capabilities.quota import QUOTA
from jmap.capabilities.registry import Registry
from jmap.capabilities.sieve import SIEVE


def default_registry() -> Registry:
    """Every capability this build of the library knows about."""
    registry = Registry()
    for spec in (
        CORE,
        MAIL,
        SUBMISSION,
        VACATION,
        SMIME_VERIFY,
        BLOB,
        QUOTA,
        SIEVE,
        WEBSOCKET,
        VAPID,
        PRINCIPALS,
        PRINCIPALS_OWNER,
        FILENODE,
        CALENDARS,
        CALENDARS_PARSE,
        AVAILABILITY,
        CONTACTS,
        CONTACTS_PARSE,
        FASTMAIL_CONTACTS,
        CYRUS_CONTACTS,
        MDN_CAPABILITY,
    ):
        registry.register(spec)
    return registry

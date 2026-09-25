"""Resources: things a Card points at by URI (RFC 9553 §1.4.4, §2.4.1, §2.6)."""

from __future__ import annotations

from jmap.models.jsobject import JSObject


class Resource(JSObject):
    """What every resource has: a URI, and how and when to use it (§1.4.4)."""

    #: The allowed values depend on the resource; see each subclass.
    kind: str | None = None
    uri: str | None = None
    media_type: str | None = None
    #: A set-as-map, e.g. ``{"work": True}``.
    contexts: dict[str, bool] | None = None
    #: 1 to 100; lower is more preferred, and unset is least preferred of all.
    pref: int | None = None
    label: str | None = None


class Calendar(Resource):
    """A calendar or free/busy source of the entity (§2.4.1).

    Not :class:`jmap.models.calendars.Calendar`, which is a JMAP calendar.
    ``kind`` is ``calendar`` or ``freeBusy``.
    """


class CryptoKey(Resource):
    """A public key or certificate, by URI or inline as a ``data:`` URI (§2.6.1)."""


class Directory(Resource):
    """A directory the entity is part of, or its entry in one (§2.6.2).

    ``kind`` is ``directory`` or ``entry``.
    """

    #: Position among directories of the same kind; above zero if set.
    list_as: int | None = None


class Link(Resource):
    """Any other resource (§2.6.3). ``kind`` is unset or ``contact``."""


class Media(Resource):
    """One photo, sound or logo attached to a card (§2.6.4).

    RFC 9610 §3 adds ``blobId`` and has servers prefer it over a ``data:`` URI, so
    a contact list fetches thumbnails on demand rather than carrying every face
    base64-encoded in the response. ``mediaType`` must be set alongside it.
    """

    #: RFC 9610 §7.5.3's addition. Present instead of ``uri`` for binary content.
    blob_id: str | None = None

    @property
    def is_blob_backed(self) -> bool:
        return self.blob_id is not None

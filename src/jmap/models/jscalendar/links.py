"""Relations and links (draft-ietf-calext-jscalendarbis §1.5.10, §1.5.11)."""

from __future__ import annotations

from jmap.models.jsobject import JSObject


class Relation(JSObject):
    """How another object relates to this one (§1.5.10).

    A set-as-map of ``first``, ``next``, ``child`` and ``parent``; empty means
    ``parent`` unless the property says otherwise.
    """

    relation: dict[str, bool] | None = None


class Link(JSObject):
    """An external resource, such as an attachment (§1.5.11)."""

    href: str | None = None
    #: draft-ietf-jmap-calendars §5.3: a blob in place of ``href``, for an
    #: attachment uploaded to the server.
    blob_id: str | None = None
    content_type: str | None = None
    #: Octets once decoded - an estimate, not a promise.
    size: int | None = None
    #: A link relation (RFC 8288). Unset means ``enclosure``: an attachment.
    #: ``icon`` marks an image to show, and is the only one ``display`` goes with.
    rel: str | None = None
    #: A set-as-map of ``badge``, ``graphic``, ``fullsize`` and ``thumbnail`` -
    #: or, in RFC 8984, one of them as a plain string.
    display: dict[str, bool] | str | None = None
    title: str | None = None
    #: RFC 8984: a Content-ID, for a resource in the same MIME message.
    cid: str | None = None

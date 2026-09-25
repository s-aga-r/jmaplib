"""Where an event happens (draft-ietf-calext-jscalendarbis §3.2.5, §3.2.7)."""

from __future__ import annotations

from jmap.models.jscalendar.links import Link
from jmap.models.jsobject import JSObject


class Location(JSObject):
    """A physical location (§3.2.5)."""

    name: str | None = None
    #: A set-as-map of RFC 4589 location types, e.g. ``{"restaurant": True}``.
    location_types: dict[str, bool] | None = None
    #: A ``geo:`` URI.
    coordinates: str | None = None
    links: dict[str, Link] | None = None


class VirtualLocation(JSObject):
    """A video call, chat room or dial-in (§3.2.7)."""

    name: str | None = None
    uri: str | None = None
    #: A set-as-map of ``audio``, ``chat``, ``feed``, ``moderator``, ``phone``,
    #: ``screen`` and ``video``.
    features: dict[str, bool] | None = None

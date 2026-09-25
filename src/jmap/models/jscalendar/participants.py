"""Who takes part (draft-ietf-calext-jscalendarbis §3.4.5)."""

from __future__ import annotations

from jmap.models.jscalendar.links import Link
from jmap.models.jsdates import UTCDateTime
from jmap.models.jsobject import JSObject


class Participant(JSObject):
    """An organizer, attendee or other participant (§3.4.5).

    jscalendarbis moved scheduling details out: ``calendarAddress`` replaces
    RFC 8984's ``sendTo``, while ``email`` stays.
    """

    name: str | None = None
    email: str | None = None
    description: str | None = None
    description_content_type: str | None = None
    #: The URI to schedule with, usually ``mailto:``.
    calendar_address: str | None = None
    #: ``individual``, ``group``, ``location``, ``resource`` or ``unknown``.
    kind: str | None = None
    #: A set-as-map of ``owner``, ``attendee``, ``optional``, ``informational``,
    #: ``chair`` and ``contact``.
    roles: dict[str, bool] | None = None
    #: ``needs-action`` (the default), ``accepted``, ``declined``, ``tentative``
    #: or ``delegated``.
    participation_status: str | None = None
    expect_reply: bool | None = None
    sent_by: str | None = None
    delegated_to: dict[str, bool] | None = None
    delegated_from: dict[str, bool] | None = None
    #: Participant ids of the groups this one is a member of.
    member_of: dict[str, bool] | None = None
    links: dict[str, Link] | None = None
    #: For a task's participants only.
    progress: str | None = None
    percent_complete: int | None = None
    #: draft-ietf-jmap-calendars §5.2: the latest iTIP reply applied for this
    #: participant, to discard older ones that arrive late.
    schedule_sequence: int | None = None
    schedule_updated: UTCDateTime | None = None

    # -- RFC 8984, which jscalendarbis replaced ------------------------------ #
    #: Scheduling method -> URI, where jscalendarbis has ``calendar_address``.
    send_to: dict[str, str] | None = None
    #: A key of the event's ``locations``: where this participant will be.
    location_id: str | None = None
    language: str | None = None
    participation_comment: str | None = None
    #: ``server`` (the default), ``client`` or ``none``: who sends the invitations.
    schedule_agent: str | None = None
    schedule_force_send: bool | None = None
    #: iTIP REQUEST-STATUS codes from the last delivery attempts.
    schedule_status: list[str] | None = None
    #: The participant id of whoever invited this one.
    invited_by: str | None = None
    progress_updated: UTCDateTime | None = None

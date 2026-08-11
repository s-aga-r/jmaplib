"""RFC 8621 data types: Mailbox, Thread, Email and the objects they contain.

Three details drive how these are modelled:

**``from`` is a Python keyword.** The wire name cannot be a field name, so the
field is ``from_`` with an explicit alias. It is the one place the automatic
camelCase alias generator has to be overridden, and forgetting it silently drops
the sender from every message.

**Almost everything is optional.** ``Foo/get`` returns only the properties asked
for, so a model that required ``subject`` could not represent the result of
``Email/get`` with ``properties=["id"]``. Absence therefore means "not fetched",
which is exactly what ``exclude_unset`` needs to distinguish from ``null``.

**Header values are addressed by wire key.** ``header:Subject:asText`` is a
property name, not a nested structure, and its capitalisation must match what was
requested (RFC 8621 §4.1.2). Those live in the model's ``extra`` rather than as
declared fields, which is why :class:`~jmap.models.base.JMAPModel` allows extras.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from jmap.models.base import JMAPModel

# --------------------------------------------------------------------------- #
# Shared address and body types (RFC 8621 §4.1.2, §4.1.4)
# --------------------------------------------------------------------------- #


class EmailAddress(JMAPModel):
    """One address from a header field."""

    name: str | None = None
    email: str | None = None


class EmailAddressGroup(JMAPModel):
    """An RFC 5322 address group, as returned by ``asGroupedAddresses``."""

    name: str | None = None
    addresses: list[EmailAddress] | None = None


class EmailHeader(JMAPModel):
    """A raw header field, in the order it appeared."""

    name: str | None = None
    value: str | None = None


class EmailBodyValue(JMAPModel):
    """Decoded content of one body part.

    ``is_truncated`` matters more than it looks: the server truncates at
    ``maxBodyValueBytes``, and code that renders the value without checking will
    silently show a partial message as if it were whole.
    """

    value: str | None = None
    is_encoding_problem: bool | None = None
    is_truncated: bool | None = None


class EmailBodyPart(JMAPModel):
    """One MIME part.

    ``sub_parts`` makes this recursive, which is why the model is rebuilt below:
    a self-reference cannot be resolved while the class is still being defined.
    """

    part_id: str | None = None
    blob_id: str | None = None
    size: int | None = None
    headers: list[EmailHeader] | None = None
    name: str | None = None
    type: str | None = None
    charset: str | None = None
    disposition: str | None = None
    cid: str | None = None
    language: list[str] | None = None
    location: str | None = None
    sub_parts: list[EmailBodyPart] | None = None


EmailBodyPart.model_rebuild()


# --------------------------------------------------------------------------- #
# Mailbox (RFC 8621 §2)
# --------------------------------------------------------------------------- #


class MailboxRights(JMAPModel):
    """What the authenticated principal may do with a mailbox."""

    may_read_items: bool | None = None
    may_add_items: bool | None = None
    may_remove_items: bool | None = None
    may_set_seen: bool | None = None
    may_set_keywords: bool | None = None
    may_create_child: bool | None = None
    may_rename: bool | None = None
    may_delete: bool | None = None
    may_submit: bool | None = None


class Mailbox(JMAPModel):
    """A named set of emails (RFC 8621 §2).

    ``role`` is the portable way to find a special mailbox: names are localised
    and user-editable, roles are the IANA-registered identifiers (``inbox``,
    ``drafts``, ``sent``, ``trash``, ``junk``, ``archive``). Matching on name is a
    bug that only shows up against a non-English server.
    """

    id: str | None = None
    name: str | None = None
    parent_id: str | None = None
    role: str | None = None
    sort_order: int | None = None
    total_emails: int | None = None
    unread_emails: int | None = None
    total_threads: int | None = None
    unread_threads: int | None = None
    my_rights: MailboxRights | None = None
    is_subscribed: bool | None = None


# --------------------------------------------------------------------------- #
# Thread and Email (RFC 8621 §3, §4)
# --------------------------------------------------------------------------- #


class Thread(JMAPModel):
    """A set of related emails, ordered by ``receivedAt`` (RFC 8621 §3)."""

    id: str | None = None
    email_ids: list[str] | None = None


class Email(JMAPModel):
    """One message (RFC 8621 §4).

    ``keywords`` and ``mailbox_ids`` are maps to ``True`` rather than lists,
    because that is what makes them patchable one entry at a time:
    ``{"keywords/$seen": true}`` adds a flag without rewriting the set, which a
    list could not express.
    """

    id: str | None = None
    blob_id: str | None = None
    thread_id: str | None = None
    #: Mailbox id -> True. See the class docstring.
    mailbox_ids: dict[str, bool] | None = None
    #: Lower-cased keyword -> True.
    keywords: dict[str, bool] | None = None
    size: int | None = None
    received_at: str | None = None

    # -- header-derived convenience properties -- #
    message_id: list[str] | None = None
    in_reply_to: list[str] | None = None
    references: list[str] | None = None
    sender: list[EmailAddress] | None = None
    #: ``from`` is a keyword, so the field is renamed and aliased explicitly.
    from_: list[EmailAddress] | None = Field(default=None, alias="from")
    to: list[EmailAddress] | None = None
    cc: list[EmailAddress] | None = None
    bcc: list[EmailAddress] | None = None
    reply_to: list[EmailAddress] | None = None
    subject: str | None = None
    sent_at: str | None = None
    headers: list[EmailHeader] | None = None

    # -- body -- #
    body_structure: EmailBodyPart | None = None
    #: partId -> decoded content, populated by the ``fetch*BodyValues`` arguments.
    body_values: dict[str, EmailBodyValue] | None = None
    text_body: list[EmailBodyPart] | None = None
    html_body: list[EmailBodyPart] | None = None
    attachments: list[EmailBodyPart] | None = None
    has_attachment: bool | None = None
    preview: str | None = None

    def header(self, key: str) -> Any:
        """Read a ``header:*`` property by its exact wire key.

        Parsed headers arrive as extras rather than declared fields, because the
        property name encodes the request (``header:Subject:asText``) and RFC 8621
        §4.1.2 requires the response key to match the requested capitalisation.
        """
        return (self.__pydantic_extra__ or {}).get(key)


class SearchSnippet(JMAPModel):
    """Highlighted match context for one email (RFC 8621 §5).

    Has no ``id`` of its own - it is keyed by ``emailId`` - which is why its spec
    is marked identityless.
    """

    email_id: str | None = None
    subject: str | None = None
    preview: str | None = None


# --------------------------------------------------------------------------- #
# Identity and submission (RFC 8621 §6, §7)
# --------------------------------------------------------------------------- #


class Identity(JMAPModel):
    """An address the user may send from (RFC 8621 §6)."""

    id: str | None = None
    name: str | None = None
    email: str | None = None
    reply_to: list[EmailAddress] | None = None
    bcc: list[EmailAddress] | None = None
    text_signature: str | None = None
    html_signature: str | None = None
    may_delete: bool | None = None


class Address(JMAPModel):
    """An SMTP envelope address with its ESMTP parameters (RFC 8621 §7.1)."""

    email: str | None = None
    #: ESMTP parameters such as ``HOLDFOR``. Keys are case-insensitive per the
    #: spec, and a null value means the parameter has no argument.
    parameters: dict[str, Any] | None = None


class Envelope(JMAPModel):
    """The SMTP envelope, which is *not* the same as the message headers.

    ``rcpt_to`` is who the mail actually goes to; ``To``/``Cc`` in the message are
    display only. Bcc works precisely because the two differ.
    """

    mail_from: Address | None = None
    rcpt_to: list[Address] | None = None


class DeliveryStatus(JMAPModel):
    """Per-recipient delivery state (RFC 8621 §7.1)."""

    smtp_reply: str | None = None
    delivered: str | None = None
    displayed: str | None = None


class EmailSubmission(JMAPModel):
    """A request to send an email (RFC 8621 §7).

    ``undo_status`` is the only mutable property: a submission is a record of an
    attempt, so the way to "unsend" is to set it to ``canceled`` while the server
    still holds the message.
    """

    id: str | None = None
    identity_id: str | None = None
    email_id: str | None = None
    thread_id: str | None = None
    envelope: Envelope | None = None
    send_at: str | None = None
    undo_status: str | None = None
    delivery_status: dict[str, DeliveryStatus] | None = None
    dsn_blob_ids: list[str] | None = None
    mdn_blob_ids: list[str] | None = None


class VacationResponse(JMAPModel):
    """The auto-responder (RFC 8621 §8).

    A singleton: its id is always the literal string ``singleton``, so there is no
    ``/query`` and creating one is meaningless.
    """

    id: str | None = None
    is_enabled: bool | None = None
    from_date: str | None = None
    to_date: str | None = None
    subject: str | None = None
    text_body: str | None = None
    html_body: str | None = None

"""Message Disposition Notifications (RFC 9007).

An MDN is a read receipt: a structured message saying what happened to an email
someone sent you. RFC 9007 gives JMAP two methods for them - one to send, one to
parse one you received.

The half worth reading closely is ``MDN/send``, which is unusual among JMAP
methods in that **the server is required to police the client's bookkeeping**.
RFC 9007 §2.1 says the server MUST reject a send that does not also set the
``$mdnsent`` keyword on the message being acknowledged, and that it MUST check
``onSuccessUpdateEmail`` to confirm it. So the patch is not optional garnish; it
is part of a well-formed request, which is why :func:`mdn_sent_patch` exists and
why the builder refuses to send without one.

Two smaller traps sit next to it:

**``$mdnsent`` is lowercase, always.** Keywords are case-insensitive in IMAP and
case-*sensitive* in JMAP, and §1.2 resolves that by fixing the spelling. ``$MDNSent``
is a different keyword that no server will recognise.

**The Disposition fields are lowercase too.** RFC 8098 defines them
case-insensitively; RFC 9007 §2 makes them case-sensitive and requires ``MDN/parse``
to lowercase what it finds. Sending ``"Displayed"`` produces a value nothing matches.
"""

from __future__ import annotations

from typing import Any, Final

from jmap.models.base import JMAPModel

#: RFC 9007 §1.2. The keyword marking a message as acknowledged. Lowercase, and
#: not negotiable - see the module docstring.
MDN_SENT_KEYWORD: Final = "$mdnsent"

#: RFC 9007 §2.1. The message already carried ``$mdnsent``.
MDN_ALREADY_SENT: Final = "mdnAlreadySent"

#: RFC 9007 §2's Disposition vocabulary, all lowercase.
ACTION_MANUAL: Final = "manual-action"
ACTION_AUTOMATIC: Final = "automatic-action"

SENDING_MANUAL: Final = "mdn-sent-manually"
SENDING_AUTOMATIC: Final = "mdn-sent-automatically"

TYPE_DELETED: Final = "deleted"
TYPE_DISPATCHED: Final = "dispatched"
TYPE_DISPLAYED: Final = "displayed"
TYPE_PROCESSED: Final = "processed"

DISPOSITION_TYPES: Final = frozenset(
    {TYPE_DELETED, TYPE_DISPATCHED, TYPE_DISPLAYED, TYPE_PROCESSED}
)


class Disposition(JMAPModel):
    """What happened to the message, and how (RFC 9007 §2).

    All three values are lowercase and case-sensitive here, unlike in RFC 8098
    where they are case-insensitive. ``MDN/parse`` is required to lowercase what
    it reads, so a value that arrives capitalised is a server bug rather than
    something to normalise on the way in.
    """

    #: ``manual-action`` or ``automatic-action`` - whether a human decided.
    action_mode: str | None = None
    #: ``mdn-sent-manually`` or ``mdn-sent-automatically`` - whether a human
    #: chose to send the receipt. Distinct from ``action_mode``: a message can be
    #: displayed automatically and acknowledged deliberately, or the reverse.
    sending_mode: str | None = None
    #: ``deleted``, ``dispatched``, ``displayed`` or ``processed``.
    type: str | None = None


class MDN(JMAPModel):
    """One disposition notification (RFC 9007 §2)."""

    #: The message being acknowledged. MUST be set for ``MDN/send``; MAY be null
    #: from ``MDN/parse``, which cannot always work out which message a received
    #: MDN refers to.
    for_email_id: str | None = None
    subject: str | None = None
    text_body: str | None = None
    #: Quotes the original message back in the report. RFC 8098 has security
    #: considerations for this - the report travels to whoever asked for it.
    include_original_message: bool | None = None
    #: The client's own name. ``None`` has better privacy properties, which the
    #: RFC says outright.
    reporting_ua: str | None = None
    disposition: Disposition | None = None
    #: Server-set: the gateway that translated a foreign notification into this.
    mdn_gateway: str | None = None
    #: Server-set.
    original_recipient: str | None = None
    #: Overrides what the server would derive from the identity, where the user is
    #: allowed to claim it.
    final_recipient: str | None = None
    #: Server-set. The RFC 5322 ``Message-ID``, *not* the JMAP id.
    original_message_id: str | None = None
    #: Server-set, and non-null only alongside an ``error`` disposition modifier.
    error: list[str] | None = None
    extension_fields: dict[str, str] | None = None


class MDNSendResponse(JMAPModel):
    """``MDN/send`` (RFC 9007 §2.1).

    Half-succeeds like a ``/set``: ``sent`` and ``notSent`` are both populated in
    the same response, so failures are values rather than exceptions.
    """

    account_id: str | None = None
    #: Creation id -> the MDN as sent, carrying whatever the server filled in.
    sent: dict[str, MDN] | None = None
    #: Creation id -> SetError, kept as raw wire dicts for the same reason
    #: ``SetResponse`` does: :class:`~jmap.core.errors.SetError` lives in the
    #: pydantic-free kernel.
    not_sent: dict[str, dict[str, Any]] | None = None

    @property
    def has_errors(self) -> bool:
        return bool(self.not_sent)

    def sent_ids(self) -> list[str]:
        """The creation ids that were actually sent."""
        return list(self.sent or {})


class MDNParseResponse(JMAPModel):
    """``MDN/parse`` (RFC 9007 §2.2).

    Three nullable maps rather than empty ones, so a caller must treat ``None``
    and ``{}`` alike - which is what :meth:`parsed_for` and the properties below
    do.
    """

    account_id: str | None = None
    #: Blob id -> the MDN it held.
    parsed: dict[str, MDN] | None = None
    #: Blobs that exist but are not MDNs.
    not_parsable: list[str] | None = None
    not_found: list[str] | None = None

    def parsed_for(self, blob_id: str) -> MDN | None:
        """The MDN in one blob, or ``None`` if it did not parse."""
        return (self.parsed or {}).get(blob_id)

    @property
    def unusable(self) -> list[str]:
        """Every blob id that yielded nothing, whichever way it failed.

        The two failure lists mean different things to a *server* - missing versus
        unparseable - but the same thing to a caller deciding what to display.
        """
        return [*(self.not_found or []), *(self.not_parsable or [])]


def mdn_sent_patch() -> dict[str, Any]:
    """The ``onSuccessUpdateEmail`` patch every ``MDN/send`` must carry.

    RFC 9007 §2.1 requires the server to *check* that the send sets ``$mdnsent``
    and to reject it otherwise, so this is part of a well-formed request rather
    than a convenience. Spelled once, here, because the keyword's lowercase form
    is load-bearing.
    """
    return {f"keywords/{MDN_SENT_KEYWORD}": True}


def already_sent(keywords: dict[str, bool] | None) -> bool:
    """Whether a message has already been acknowledged.

    §2.1: the client MUST NOT issue an ``MDN/send`` for one that has. Checking
    locally turns a wasted round trip - and a duplicate receipt for the recipient
    if a server is lax - into nothing at all.
    """
    return bool((keywords or {}).get(MDN_SENT_KEYWORD))


def parse_report(fields: dict[str, str]) -> dict[str, str]:
    """Lowercase the Disposition values in a hand-parsed report.

    Only needed by code that reads RFC 8098 reports itself; ``MDN/parse`` is
    required to do this server-side. Provided because the case difference between
    the two RFCs is the sort of thing that gets normalised in one direction on one
    side of a codebase and the other direction elsewhere.
    """
    return {key: value.lower() for key, value in fields.items()}

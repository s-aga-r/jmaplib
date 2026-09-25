"""Builders for the RFC 8621 methods that follow no standard shape.

``Email/import`` and ``Email/parse`` both start from a blob, and
``SearchSnippet/get`` takes the ids a query found. Each answers with its own
model (see :mod:`jmap.models.mail.irregular`), which these builders ask for; a
raw ``batch.add`` of the same method still answers with the wire dict.

``Email/set`` and ``EmailSubmission/set`` have the standard shape; their
builders here only check the account's limits first - an email's mailboxes, as
the import does, and how long a submission asks to be held.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from jmap.api.entity import Creation, EntityBase, Settable, builder, wire_objects
from jmap.capabilities.mail import (
    MAIL_URN,
    SUBMISSION_URN,
    MailCapability,
    SubmissionCapability,
    check_delayed_send,
    check_mailboxes_per_email,
)
from jmap.core.ids import CreationRef, Id
from jmap.core.invocation import Handle, ResultRef
from jmap.models.arguments import UnsignedInt
from jmap.models.base import UNSET, Unset, omit_unset
from jmap.models.mail.irregular import (
    EmailImport,
    EmailImportResponse,
    ParsedEmails,
    SearchSnippetResponse,
)
from jmap.models.responses import SetResponse


def _check_mailbox_counts(emails: list[Mapping[str, Any]], capability: MailCapability) -> None:
    """Refuse an email filed in more mailboxes than ``maxMailboxesPerEmail``.

    Only a whole ``mailboxIds`` can be counted; a patch adding one mailbox to
    an email already in some is the server's to judge.
    """
    for email in emails:
        mailboxes = email.get("mailboxIds")
        if isinstance(mailboxes, Mapping):
            members = cast("Mapping[str, Any]", mailboxes).values()
            check_mailboxes_per_email(sum(1 for member in members if member), capability)


class EmailSettable(Settable[Any]):
    """``Email/set`` (RFC 8621 §4.6)."""

    __slots__ = ()

    @builder
    def set(
        self,
        *,
        create: Mapping[str, Creation] | ResultRef[Any] | Unset | None = UNSET,
        update: Mapping[str, Mapping[str, Any]] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[SetResponse[Any]]:
        """Create, update and destroy emails in one atomic call.

        Each email's ``mailboxIds`` is counted against the account's
        ``maxMailboxesPerEmail`` first. See :meth:`jmap.api.entity.Settable.set`.
        """
        capability = MailCapability.of(self._batch.capability_value(MAIL_URN))
        _check_mailbox_counts([*wire_objects(create), *wire_objects(update)], capability)
        return super().set(create=create, update=update, **extra)


class EmailSubmissionSettable(Settable[Any]):
    """``EmailSubmission/set`` (RFC 8621 §7.5)."""

    __slots__ = ()

    @builder
    def set(
        self,
        *,
        create: Mapping[str, Creation] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[SetResponse[Any]]:
        """Send messages, and update or cancel sends still pending.

        A submission held with ``HOLDFOR`` or ``HOLDUNTIL`` (RFC 4865) is checked
        against the account's ``maxDelayedSend`` first; a ``HOLDUNTIL`` is
        measured from this machine's clock. See
        :meth:`jmap.api.entity.Settable.set`.
        """
        capability = SubmissionCapability.of(self._batch.capability_value(SUBMISSION_URN))
        now = datetime.now(UTC)
        for submission in wire_objects(create):
            seconds = _hold_seconds(submission, now)
            if seconds is not None:
                check_delayed_send(seconds, capability)
        return super().set(create=create, **extra)


def _hold_seconds(submission: Mapping[str, Any], now: datetime) -> float | None:
    """How long a submission asks to be held, or ``None`` if it does not ask.

    The hold is a parameter on the envelope's ``mailFrom``: ``HOLDFOR`` in
    seconds, or ``HOLDUNTIL`` as a date-time. A value this cannot read is the
    server's to judge.
    """
    envelope = _mapping(submission.get("envelope"))
    parameters = _mapping(_mapping(envelope.get("mailFrom")).get("parameters"))
    # SMTP parameter names ignore case.
    named = {str(name).upper(): value for name, value in parameters.items()}
    hold_for, hold_until = named.get("HOLDFOR"), named.get("HOLDUNTIL")
    if isinstance(hold_for, str) and hold_for.isdigit():
        return float(hold_for)
    if not isinstance(hold_until, str):
        return None
    try:
        until = datetime.fromisoformat(hold_until)
    except ValueError:
        return None
    if until.utcoffset() is None:
        return None
    return max(0.0, (until - now).total_seconds())


def _mapping(value: Any) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


class EmailImportable(EntityBase[Any]):
    """``Email/import`` (RFC 8621 §4.8)."""

    __slots__ = ()

    @builder
    def import_(
        self,
        *,
        emails: Mapping[str, EmailImport | Mapping[str, Any]],
        if_in_state: str | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[EmailImportResponse]:
        """File uploaded blobs as messages - a ``.eml`` a user dropped in, say.

        Named with a trailing underscore because ``import`` is a keyword. It
        half-succeeds like a ``/set``, so read ``creation_errors``; and like a
        ``/set``, ``if_in_state`` is what makes a retry safe. Each message's
        ``mailboxIds`` is counted against ``maxMailboxesPerEmail`` first.
        """
        capability = MailCapability.of(self._batch.capability_value(MAIL_URN))
        _check_mailbox_counts(wire_objects(emails), capability)
        return self._add(
            "import",
            omit_unset(emails=dict(emails), ifInState=if_in_state, **extra),
            response_model=EmailImportResponse,
        )


class EmailParsable(EntityBase[Any]):
    """``Email/parse`` (RFC 8621 §4.9)."""

    __slots__ = ()

    @builder
    def parse(
        self,
        *,
        blob_ids: Sequence[Id | CreationRef] | ResultRef[Any],
        properties: Sequence[str] | ResultRef[Any] | Unset = UNSET,
        body_properties: Sequence[str] | ResultRef[Any] | Unset = UNSET,
        fetch_text_body_values: bool | ResultRef[Any] | Unset = UNSET,
        fetch_html_body_values: bool | ResultRef[Any] | Unset = UNSET,
        fetch_all_body_values: bool | ResultRef[Any] | Unset = UNSET,
        max_body_value_bytes: UnsignedInt | ResultRef[Any] | Unset = UNSET,
        **extra: Any,
    ) -> Handle[ParsedEmails]:
        """Read blobs as messages without storing them - an attached ``.eml``.

        The body arguments work as they do for ``Email/get``.
        """
        return self._add(
            "parse",
            omit_unset(
                blobIds=blob_ids,
                properties=properties,
                bodyProperties=body_properties,
                fetchTextBodyValues=fetch_text_body_values,
                # Spelled out: the camelCase rule would give `fetchHtmlBodyValues`.
                fetchHTMLBodyValues=fetch_html_body_values,
                fetchAllBodyValues=fetch_all_body_values,
                maxBodyValueBytes=max_body_value_bytes,
                **extra,
            ),
            response_model=ParsedEmails,
        )


class SearchSnippetGettable(EntityBase[Any]):
    """``SearchSnippet/get`` (RFC 8621 §5.1)."""

    __slots__ = ()

    @builder
    def get(
        self,
        *,
        # `filter` mirrors the wire name.
        filter: Mapping[str, Any] | ResultRef[Any] | None,
        email_ids: Sequence[Id | CreationRef] | ResultRef[Any],
        **extra: Any,
    ) -> Handle[SearchSnippetResponse]:
        """Why each email matched a search: its subject and preview, highlighted.

        Pass the filter the query used and the ids it found, in the same batch:
        ``get(filter=same_filter, email_ids=query.ref_ids())``.
        """
        return self._add(
            "get",
            omit_unset(filter=filter, emailIds=email_ids, **extra),
            response_model=SearchSnippetResponse,
        )

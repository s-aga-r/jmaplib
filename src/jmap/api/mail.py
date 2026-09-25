"""Builders for the RFC 8621 methods that follow no standard shape.

``Email/import`` and ``Email/parse`` both start from a blob, and
``SearchSnippet/get`` takes the ids a query found. Each answers with its own
model (see :mod:`jmap.models.mail.irregular`), which these builders ask for; a
raw ``batch.add`` of the same method still answers with the wire dict.

``Email/set`` has the standard shape; its builder here only checks each email's
mailboxes against the account's limit first, as the import does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from jmap.api.entity import Creation, EntityBase, Settable, builder, wire_objects
from jmap.capabilities.mail import MAIL_URN, MailCapability, check_mailboxes_per_email
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

"""Builders for the RFC 8621 methods that follow no standard shape.

``Email/import`` and ``Email/parse`` both start from a blob, and
``SearchSnippet/get`` takes the ids a query found. Each answers with its own
model (see :mod:`jmap.models.mail.irregular`), which these builders ask for; a
raw ``batch.add`` of the same method still answers with the wire dict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jmap.api.entity import EntityBase, builder
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
        ``/set``, ``if_in_state`` is what makes a retry safe.
        """
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

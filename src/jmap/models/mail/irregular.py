"""Arguments and answers of the RFC 8621 methods that follow no standard shape.

``Email/import`` files uploaded blobs as messages, ``Email/parse`` reads blobs as
messages without storing them, and ``SearchSnippet/get`` shows why each email
matched a search. None of them answers like a ``/get`` or a ``/set``, so each has
its own model here. Their builders return these; the raw ``batch.add`` path keeps
answering with the wire dict it always has.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from jmap.core.errors import SetError
from jmap.models.base import JMAPModel
from jmap.models.mail.objects import Email, SearchSnippet
from jmap.models.responses import parse_set_errors


class EmailImport(JMAPModel):
    """One message to file from an uploaded blob (RFC 8621 §4.8)."""

    blob_id: str
    #: At least one: an import filed in no mailbox is refused.
    mailbox_ids: dict[str, bool] = Field(min_length=1)
    keywords: dict[str, bool] | None = None
    #: A UTCDate. The server uses the time of import when it is absent.
    received_at: str | None = None


class EmailImportResponse(JMAPModel):
    """``Email/import`` (RFC 8621 §4.8).

    It half-succeeds like a ``/set``, so failures are values: read
    ``creation_errors`` rather than waiting for an exception.
    """

    account_id: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    #: Creation id -> the new email's ``id``, ``blobId``, ``threadId`` and ``size``.
    created: dict[str, Email] = Field(default_factory=dict)
    not_created: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("created", "not_created", mode="before")
    @classmethod
    def _null_map_is_empty(cls, value: Any) -> Any:
        # Both are nullable on the wire, as they are for /set.
        return {} if value is None else value

    @property
    def has_errors(self) -> bool:
        return bool(self.not_created)

    @property
    def creation_errors(self) -> dict[str, SetError]:
        """``notCreated`` as typed errors - ``alreadyExists`` carries the id."""
        return parse_set_errors(self.not_created)

    def created_id(self, creation_id: str) -> str | None:
        """The id of one imported email, or ``None`` if it failed."""
        created = self.created.get(creation_id)
        return created.id if created is not None else None


class ParsedEmails(JMAPModel):
    """``Email/parse`` (RFC 8621 §4.9).

    ``parsed`` maps each blob to **one** email - not an array, as the calendars
    ``/parse`` returns. A parsed email is stored nowhere, so its ``id``,
    ``mailboxIds``, ``keywords`` and ``receivedAt`` are null.
    """

    account_id: str | None = None
    parsed: dict[str, Email] | None = None
    not_parsable: list[str] | None = None
    not_found: list[str] | None = None

    def email_of(self, blob_id: str) -> Email | None:
        """The email parsed out of one blob, or ``None`` if it yielded none."""
        return (self.parsed or {}).get(blob_id)


class SearchSnippetResponse(JMAPModel):
    """``SearchSnippet/get`` (RFC 8621 §5.1).

    Not a ``/get`` despite the name: it takes ``emailIds`` and a filter, and has
    no ``state`` - snippets are computed on request, not stored.
    """

    account_id: str | None = None
    #: The wire name is ``list``.
    items: list[SearchSnippet] = Field(default_factory=lambda: [], alias="list")
    not_found: list[str] = Field(default_factory=list)

    @field_validator("not_found", mode="before")
    @classmethod
    def _null_list_is_empty(cls, value: Any) -> Any:
        return [] if value is None else value

    def snippet_of(self, email_id: str) -> SearchSnippet | None:
        """The snippet for one email, or ``None`` if the server had none."""
        return next((item for item in self.items if item.email_id == email_id), None)

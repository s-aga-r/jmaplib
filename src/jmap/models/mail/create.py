"""Validating an ``Email`` before ``Email/set`` creates it (RFC 8621 §4.6).

Creating an email is the one place in JMAP where the object you send is not
shaped like the object you get back. Several properties are server-assigned and
must be absent; the body may be described in exactly one of two mutually
exclusive ways; and headers are set through ``header:*`` properties rather than
the ``headers`` list you read them from.

The server rejects any of these with ``invalidProperties``, which names the
property but not the rule, so the same mistake is easy to make twice. Checking
locally turns that into a message that says what to do instead.

This is a *subset* of §4.6, not the whole of it. The rules encoded here are the
ones that can be checked from the creation object alone; anything needing server
state - whether a mailbox exists, whether a blobId is live - is left to the
server, and the docstrings say so rather than pretending otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from jmap.core.errors import JMAPError
from jmap.core.narrow import as_list_of, as_object, is_object
from jmap.core.patch import InvalidKeywordError, parse_keyword

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: RFC 8621 §4.6. Assigned by the server; sending them is an error rather than a
#: hint, because the server has no way to honour them.
SERVER_ASSIGNED: Final = frozenset({"id", "blobId", "threadId", "size"})

#: The two mutually exclusive ways to describe a body (RFC 8621 §4.6).
STRUCTURED_BODY: Final = frozenset({"textBody", "htmlBody", "attachments"})


class InvalidEmailCreateError(JMAPError, ValueError):
    """An ``Email`` creation object the server would reject.

    ``properties`` names the offending property, mirroring the
    ``invalidProperties`` ``SetError`` the server would return, so error handling
    can be written once for both.
    """

    def __init__(self, reason: str, properties: Iterable[str] = ()) -> None:
        self.properties: tuple[str, ...] = tuple(properties)
        self.reason = reason
        named = ", ".join(repr(name) for name in self.properties)
        super().__init__(f"{reason}{f' ({named})' if named else ''}")


def _check_server_assigned(email: Mapping[str, Any]) -> None:
    present = sorted(SERVER_ASSIGNED.intersection(email))
    if present:
        raise InvalidEmailCreateError(
            "these properties are assigned by the server and must not be set on create",
            present,
        )


def _check_headers_property(email: Mapping[str, Any]) -> None:
    """``headers`` is read-only; write through ``header:*`` instead.

    The asymmetry is deliberate in the spec - ``headers`` is the parsed view of
    what arrived - but it reads like an oversight, so the message says what to do
    instead.
    """
    if "headers" in email:
        raise InvalidEmailCreateError(
            "`headers` cannot be set on create; use `header:{name}` properties "
            "(see jmap.models.mail.headers) to write individual fields",
            ["headers"],
        )


def _check_body_exclusivity(email: Mapping[str, Any]) -> None:
    """``bodyStructure`` and ``textBody``/``htmlBody``/``attachments`` are alternatives.

    Sending both leaves the server no way to know which describes the message it
    should build.
    """
    structured = sorted(STRUCTURED_BODY.intersection(email))
    if "bodyStructure" in email and structured:
        raise InvalidEmailCreateError(
            "specify the body either as `bodyStructure` or as "
            "`textBody`/`htmlBody`/`attachments`, not both",
            ["bodyStructure", *structured],
        )


def _check_mailbox_ids(email: Mapping[str, Any]) -> None:
    """An email must live somewhere (RFC 8621 §4.6).

    Whether the mailbox *exists* is server state; that it was named at all is
    not, so only the latter is checked here.
    """
    if "mailboxIds" not in email:
        raise InvalidEmailCreateError(
            "`mailboxIds` is required on create: an email must belong to at least one mailbox",
            ["mailboxIds"],
        )
    mailbox_ids: Any = email["mailboxIds"]
    if not is_object(mailbox_ids) or not mailbox_ids:
        raise InvalidEmailCreateError(
            "`mailboxIds` must be a non-empty map of mailbox id to true",
            ["mailboxIds"],
        )
    for key, value in as_object(mailbox_ids).items():
        if value is not True:
            raise InvalidEmailCreateError(
                f"`mailboxIds` values must be true, not {value!r}", [f"mailboxIds/{key}"]
            )


def _check_keywords(email: Mapping[str, Any]) -> None:
    keywords: Any = email.get("keywords")
    if keywords is None:
        return
    if not is_object(keywords):
        raise InvalidEmailCreateError("`keywords` must be a map of keyword to true", ["keywords"])
    for key, value in as_object(keywords).items():
        try:
            # parse_keyword enforces the RFC 8621 §4.1.1 charset and lower-casing.
            parse_keyword(str(key))
        except InvalidKeywordError as error:
            # Re-raised as our own type: a caller validating a creation object
            # should need one `except`, not two.
            raise InvalidEmailCreateError(str(error), [f"keywords/{key}"]) from error
        if value is not True:
            raise InvalidEmailCreateError(
                f"`keywords` values must be true, not {value!r}", [f"keywords/{key}"]
            )


def _as_part(value: Any, path: str) -> Mapping[str, Any]:
    """One body part, or a rejection naming where the bad value sits.

    Rejected rather than skipped: a non-object part would sail past validation
    and be refused by the server instead.
    """
    if not is_object(value):
        raise InvalidEmailCreateError("each body part must be an object", [path])
    return as_object(value)


def _check_body_parts(email: Mapping[str, Any]) -> None:
    """Every body part needs exactly one source of content (RFC 8621 §4.6).

    ``partId`` points at an entry in ``bodyValues``; ``blobId`` points at an
    uploaded blob. A part with both is ambiguous, and one with neither is empty.
    Intermediate ``multipart/*`` parts have ``subParts`` instead and are exempt.
    """
    for key in ("bodyStructure", *STRUCTURED_BODY):
        value: Any = email.get(key)
        if value is None:
            continue
        is_list = isinstance(value, list)
        for index, part in enumerate(as_list_of(value)):
            path = f"{key}/{index}" if is_list else key
            _check_one_body_part(_as_part(part, path), path)


def _check_one_body_part(part: Mapping[str, Any], path: str) -> None:
    children: list[Any] = part.get("subParts") or []
    if children:
        for index, child in enumerate(children):
            child_path = f"{path}/subParts/{index}"
            _check_one_body_part(_as_part(child, child_path), child_path)
        return
    if "headers" in part:
        raise InvalidEmailCreateError(
            "`headers` cannot be set on a body part; use `header:{name}` properties",
            [f"{path}/headers"],
        )
    has_part_id = part.get("partId") is not None
    has_blob_id = part.get("blobId") is not None
    if has_part_id and has_blob_id:
        raise InvalidEmailCreateError(
            "a body part must have `partId` or `blobId`, not both", [f"{path}/partId"]
        )
    if not has_part_id and not has_blob_id:
        raise InvalidEmailCreateError(
            "a body part needs either `partId` (content in `bodyValues`) or "
            "`blobId` (an uploaded blob)",
            [path],
        )


def _check_body_values(email: Mapping[str, Any]) -> None:
    """Every ``partId`` used must have content in ``bodyValues``, and vice versa."""
    raw_values: Any = email.get("bodyValues") or {}
    if not is_object(raw_values):
        raise InvalidEmailCreateError(
            "`bodyValues` must be a map of partId to value", ["bodyValues"]
        )
    body_values = as_object(raw_values)

    referenced = set(_iter_part_ids(email))
    missing = sorted(referenced - set(body_values))
    if missing:
        raise InvalidEmailCreateError(
            "these `partId`s have no entry in `bodyValues`",
            [f"bodyValues/{part_id}" for part_id in missing],
        )
    unused = sorted(set(body_values) - referenced)
    if unused:
        raise InvalidEmailCreateError(
            "these `bodyValues` entries are not referenced by any body part",
            [f"bodyValues/{part_id}" for part_id in unused],
        )


def _iter_part_ids(email: Mapping[str, Any]) -> Iterable[str]:
    for key in ("bodyStructure", *STRUCTURED_BODY):
        value: Any = email.get(key)
        if value is None:
            continue
        for part in as_list_of(value):
            # _check_body_parts already proved each of these is an object.
            yield from _iter_part_ids_of(cast("Mapping[str, Any]", part))


def _iter_part_ids_of(part: Mapping[str, Any]) -> Iterable[str]:
    sub_parts: list[Any] = part.get("subParts") or []
    if sub_parts:
        for child in sub_parts:
            yield from _iter_part_ids_of(cast("Mapping[str, Any]", child))
        return
    part_id = part.get("partId")
    if part_id is not None:
        yield str(part_id)


def validate_email_create(email: Mapping[str, Any]) -> None:
    """Check one ``Email`` creation object, raising :class:`InvalidEmailCreateError`.

    Passing means the object is well-formed, not that it will be accepted: the
    mailbox ids must exist, the blob ids must still be live, and the total size
    must fit the account's quota - none of which is knowable here.
    """
    _check_server_assigned(email)
    _check_headers_property(email)
    _check_body_exclusivity(email)
    _check_mailbox_ids(email)
    _check_keywords(email)
    # Order matters: _check_body_parts proves every part is an object, which is
    # what lets _check_body_values walk them without re-checking.
    _check_body_parts(email)
    _check_body_values(email)


def validate_email_creates(creates: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate every entry of an ``Email/set`` ``create`` map.

    The creation id is prefixed onto the reported properties so a failure in a
    batch of twenty names which one.
    """
    for creation_id, email in creates.items():
        try:
            validate_email_create(email)
        except InvalidEmailCreateError as error:
            raise InvalidEmailCreateError(
                f"{creation_id}: {error.reason}",
                [f"{creation_id}/{name}" for name in error.properties],
            ) from error

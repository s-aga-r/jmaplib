"""Reproducible payloads shaped like what a real server actually returns.

Everything here is deterministic - no ``random``, no clock - so two runs of the
benchmark measure the same work and a diff between them means a code change
rather than a different input.

The shapes are taken from real servers: the session mirrors what Stalwart 0.16
advertises (sixteen capabilities, most of the interesting values on the
*account* rather than the session), and the ``Email`` objects carry the full
RFC 8621 §4.1 property set including a nested ``bodyStructure``, because a
benchmark over ``{"id": ..., "subject": ...}`` measures nothing that happens in
production.
"""

from __future__ import annotations

from typing import Any

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"

ACCOUNT_ID = "c1"
SECOND_ACCOUNT_ID = "c2"

#: The account-level capability objects. Stalwart puts nearly every value here
#: rather than at the session level, which is what makes per-account resolution
#: the path that actually runs.
_ACCOUNT_CAPABILITIES: dict[str, Any] = {
    CORE: {
        "maxSizeUpload": 50_000_000,
        "maxConcurrentUpload": 4,
        "maxSizeRequest": 10_000_000,
        "maxConcurrentRequests": 4,
        "maxCallsInRequest": 16,
        "maxObjectsInGet": 500,
        "maxObjectsInSet": 500,
        "collationAlgorithms": ["i;ascii-numeric", "i;ascii-casemap", "i;unicode-casemap"],
    },
    MAIL: {
        "maxMailboxesPerEmail": 10,
        "maxMailboxDepth": 10,
        "maxSizeMailboxName": 255,
        "maxSizeAttachmentsPerEmail": 50_000_000,
        "emailQuerySortOptions": [
            "receivedAt",
            "size",
            "from",
            "to",
            "subject",
            "sentAt",
            "hasKeyword",
            "allInThreadHaveKeyword",
            "someInThreadHaveKeyword",
        ],
        "mayCreateTopLevelMailbox": True,
    },
    SUBMISSION: {"maxDelayedSend": 86400, "submissionExtensions": {}},
    "urn:ietf:params:jmap:vacationresponse": {},
    "urn:ietf:params:jmap:quota": {},
    "urn:ietf:params:jmap:blob": {
        "maxSizeBlobSet": 50_000_000,
        "maxDataSources": 10,
        "supportedTypeNames": ["Email", "Mailbox", "Thread", "SieveScript"],
        "supportedDigestAlgorithms": ["sha", "sha-256", "sha-512"],
    },
    "urn:ietf:params:jmap:sieve": {
        "maxSizeScriptName": 512,
        "maxSizeScript": 1_048_576,
        "maxNumberScripts": 256,
        "maxNumberRedirects": 1,
        "sieveExtensions": ["fileinto", "reject", "vacation", "envelope", "imap4flags"],
        "notificationMethods": None,
        "externalLists": None,
    },
    "urn:ietf:params:jmap:mail:share": {},
    "urn:ietf:params:jmap:principals": {},
    "urn:ietf:params:jmap:principals:owner": {"accountIdForPrincipal": ACCOUNT_ID},
    "urn:ietf:params:jmap:contacts": {},
    "urn:ietf:params:jmap:calendars": {
        "maxExpandedQueryDuration": "P365D",
        "maxParticipantsPerEvent": 100,
        "mayCreateCalendar": True,
    },
    "urn:stalwart:jmap": {},
    "urn:ietf:params:jmap:websocket": {
        "url": "wss://jmap.example.com/jmap/ws",
        "supportsPush": True,
    },
    "urn:ietf:params:jmap:webpush-vapid": {},
    "urn:ietf:params:jmap:mdn": {},
}


def session(*, accounts: int = 2) -> dict[str, Any]:
    """A Session resource with ``accounts`` accounts, shaped like Stalwart's."""
    account_ids = [ACCOUNT_ID, SECOND_ACCOUNT_ID][:accounts]
    return {
        "capabilities": {
            # Session level carries the core limits and little else: twelve of
            # the sixteen are `{}` here, which is exactly why capability values
            # have to be read per account.
            CORE: _ACCOUNT_CAPABILITIES[CORE],
            **{urn: {} for urn in _ACCOUNT_CAPABILITIES if urn != CORE},
        },
        "accounts": {
            account_id: {
                "name": f"user{index + 1}@example.com",
                "isPersonal": index == 0,
                "isReadOnly": False,
                "accountCapabilities": _ACCOUNT_CAPABILITIES,
            }
            for index, account_id in enumerate(account_ids)
        },
        "primaryAccounts": dict.fromkeys(_ACCOUNT_CAPABILITIES, ACCOUNT_ID),
        "username": "user1@example.com",
        "apiUrl": "https://jmap.example.com/jmap/",
        "downloadUrl": (
            "https://jmap.example.com/jmap/download/{accountId}/{blobId}/{name}?accept={type}"
        ),
        "uploadUrl": "https://jmap.example.com/jmap/upload/{accountId}/",
        "eventSourceUrl": (
            "https://jmap.example.com/jmap/eventsource/"
            "?types={types}&closeafter={closeafter}&ping={ping}"
        ),
        "state": "s0123456789abcdef",
    }


def _address(name: str, local: str) -> dict[str, str]:
    return {"name": name, "email": f"{local}@example.com"}


def email(index: int, *, body_parts: int = 3) -> dict[str, Any]:
    """One ``Email`` with the full RFC 8621 §4.1 property set."""
    thread = index // 3
    return {
        "id": f"M{index:08x}",
        "blobId": f"B{index:08x}",
        "threadId": f"T{thread:08x}",
        "mailboxIds": {"mb-inbox": True} if index % 4 else {"mb-inbox": True, "mb-work": True},
        "keywords": {"$seen": True} if index % 2 else {"$seen": True, "$flagged": True},
        "size": 4096 + index * 17,
        "receivedAt": f"2026-0{index % 9 + 1}-1{index % 9}T09:2{index % 6}:11Z",
        "messageId": [f"<msg-{index}@example.com>"],
        "inReplyTo": [f"<msg-{index - 1}@example.com>"] if index else None,
        "references": [f"<msg-{n}@example.com>" for n in range(max(0, index - 4), index)] or None,
        "sender": [_address("Sender Name", f"sender{index % 20}")],
        "from": [_address(f"Person {index % 20}", f"person{index % 20}")],
        "to": [_address("User One", "user1"), _address("User Two", "user2")],
        "cc": [_address(f"Copied {index % 5}", f"copied{index % 5}")],
        "bcc": None,
        "replyTo": [_address(f"Person {index % 20}", f"person{index % 20}")],
        "subject": f"Re: the quarterly figures, revision {index}",
        "sentAt": f"2026-0{index % 9 + 1}-1{index % 9}T09:2{index % 6}:09+01:00",
        "headers": [
            {"name": "Received", "value": f" from mx{index % 3}.example.com by example.com"},
            {"name": "MIME-Version", "value": " 1.0"},
            {"name": "Content-Type", "value": ' multipart/alternative; boundary="b1"'},
            {"name": "X-Mailer", "value": " ExampleMail 4.2"},
            {"name": "List-Id", "value": " <figures.example.com>"},
        ],
        "bodyStructure": {
            "partId": None,
            "blobId": None,
            "size": 0,
            "type": "multipart/alternative",
            "subParts": [
                {
                    "partId": str(part),
                    "blobId": f"B{index:08x}-{part}",
                    "size": 1024 * (part + 1),
                    "type": "text/plain" if part == 0 else "text/html",
                    "charset": "utf-8",
                    "subParts": None,
                }
                for part in range(body_parts)
            ],
        },
        "bodyValues": {
            "0": {
                "value": "Hello,\n\nPlease find the revised figures attached.\n\nRegards\n" * 4,
                "isEncodingProblem": False,
                "isTruncated": False,
            }
        },
        "textBody": [{"partId": "0", "type": "text/plain", "size": 1024}],
        "htmlBody": [{"partId": "1", "type": "text/html", "size": 2048}],
        "attachments": [],
        "hasAttachment": index % 7 == 0,
        "preview": "Please find the revised figures attached. Regards",
    }


def email_get_response(count: int = 100) -> dict[str, Any]:
    """An ``Email/get`` response holding ``count`` messages."""
    return {
        "accountId": ACCOUNT_ID,
        "state": "e0123456789",
        "list": [email(index) for index in range(count)],
        "notFound": [],
    }


def mailbox_get_response(count: int = 40) -> dict[str, Any]:
    """A ``Mailbox/get`` response - small objects, so it measures per-object cost."""
    roles = [None, "inbox", "archive", "drafts", "sent", "trash", "junk"]
    return {
        "accountId": ACCOUNT_ID,
        "state": "m0123456789",
        "list": [
            {
                "id": f"mb-{index}",
                "name": f"Folder {index}",
                "parentId": None if index < 7 else f"mb-{index % 7}",
                "role": roles[index % len(roles)],
                "sortOrder": index,
                "totalEmails": index * 13,
                "unreadEmails": index % 11,
                "totalThreads": index * 7,
                "unreadThreads": index % 5,
                "myRights": {
                    "mayReadItems": True,
                    "mayAddItems": True,
                    "mayRemoveItems": True,
                    "maySetSeen": True,
                    "maySetKeywords": True,
                    "mayCreateChild": True,
                    "mayRename": index >= 7,
                    "mayDelete": index >= 7,
                    "maySubmit": True,
                },
                "isSubscribed": True,
            }
            for index in range(count)
        ],
        "notFound": [],
    }


def full_response(count: int = 100) -> dict[str, Any]:
    """A complete Response body: a query, a get, and a set, as a real batch returns."""
    emails = email_get_response(count)
    return {
        "methodResponses": [
            [
                "Email/query",
                {
                    "accountId": ACCOUNT_ID,
                    "queryState": "q0123456789",
                    "canCalculateChanges": True,
                    "position": 0,
                    "ids": [item["id"] for item in emails["list"]],
                },
                "0",
            ],
            ["Email/get", emails, "1"],
            [
                "Email/set",
                {
                    "accountId": ACCOUNT_ID,
                    "oldState": "e0123456788",
                    "newState": "e0123456789",
                    "updated": {item["id"]: None for item in emails["list"][:10]},
                    "created": None,
                    "destroyed": None,
                    "notCreated": None,
                    "notUpdated": None,
                    "notDestroyed": None,
                },
                "2",
            ],
        ],
        "sessionState": "s0123456789abcdef",
    }

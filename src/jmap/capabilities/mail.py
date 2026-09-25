"""JMAP for Mail (RFC 8621) as three capabilities, not one.

RFC 8621 defines ``urn:ietf:params:jmap:mail``,
``urn:ietf:params:jmap:submission`` and
``urn:ietf:params:jmap:vacationresponse`` separately, and servers really do
advertise them independently - a read-only archive account may have mail without
submission. Merging them into one spec would put ``:submission`` in ``using`` for
a plain ``Email/get`` and, on a server that does not offer it, fail the entire
request.

``Identity`` lives under ``:submission`` rather than ``:mail``: it exists to name
what you may send *from*, so a server without submission has no use for it.

Both carry their fields per account (§1.3), empty at session level. A field a
server leaves out reads as "not said" rather than as a limit of nothing, so an
absent ``emailQuerySortOptions`` refuses no sort.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import Field

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.mail.objects import (
    Email,
    EmailSubmission,
    Identity,
    Mailbox,
    SearchSnippet,
    Thread,
    VacationResponse,
)

MAIL_URN: Final = "urn:ietf:params:jmap:mail"
SUBMISSION_URN: Final = "urn:ietf:params:jmap:submission"
VACATION_URN: Final = "urn:ietf:params:jmap:vacationresponse"
SMIME_URN: Final = "urn:ietf:params:jmap:smimeverify"

#: RFC 8621 §1.3.1: ``maxSizeMailboxName`` is at least this, in octets.
MIN_SIZE_MAILBOX_NAME: Final = 100


class MailCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:mail`` object (RFC 8621 §1.3.1)."""

    #: How many mailboxes one email may be in; ``None`` for no limit.
    max_mailboxes_per_email: int | None = None
    #: One more than the most ancestors a mailbox may have; ``None`` for no limit.
    max_mailbox_depth: int | None = None
    #: In UTF-8 **octets**, not characters.
    max_size_mailbox_name: int = MIN_SIZE_MAILBOX_NAME
    #: The sum of the *unencoded* attachment sizes one email may carry.
    max_size_attachments_per_email: int | None = None
    #: Every ``property`` an ``Email/query`` comparator may name, vendor ones
    #: included; ``None`` if not advertised.
    email_query_sort_options: list[str] | None = None
    #: Whether a mailbox may be created with a null ``parentId``; ``None`` if
    #: not advertised.
    may_create_top_level_mailbox: bool | None = None

    @classmethod
    def of(cls, value: Any) -> MailCapability:
        """Parse an advertised capability object, tolerating a malformed one."""
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


class SubmissionCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:submission`` object (RFC 8621 §1.3.2)."""

    #: Seconds a submission may be held before sending: 0 when the server
    #: cannot hold one at all, ``None`` if not advertised.
    max_delayed_send: int | None = None
    #: SMTP extensions a submission may use: EHLO keyword -> its arguments, e.g.
    #: ``{"FUTURERELEASE": ["86400", "2026-10-01T00:00:00Z"], "DSN": []}``.
    submission_extensions: dict[str, list[str]] = Field(default_factory=dict)

    def supports(self, extension: str) -> bool:
        """Whether an SMTP extension is offered. EHLO keywords ignore case."""
        wanted = extension.upper()
        return any(keyword.upper() == wanted for keyword in self.submission_extensions)

    @classmethod
    def of(cls, value: Any) -> SubmissionCapability:
        """Parse an advertised capability object, tolerating a malformed one."""
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()


def check_mailbox_name(name: str, capability: MailCapability) -> None:
    """Reject a mailbox name the server is required to refuse (RFC 8621 §2).

    It must be at least one character, and within ``maxSizeMailboxName``
    **octets** - which a name in accented or CJK characters reaches long before
    its character count suggests.
    """
    if not name:
        raise CapabilityFieldError(MAIL_URN, "name", "at least one character", "")
    octets = len(name.encode())
    if octets > capability.max_size_mailbox_name:
        raise CapabilityFieldError(
            MAIL_URN, "maxSizeMailboxName", capability.max_size_mailbox_name, octets
        )


def check_mailboxes_per_email(count: int, capability: MailCapability) -> None:
    """Reject an email filed in more mailboxes than the account allows (§1.3.1)."""
    limit = capability.max_mailboxes_per_email
    if limit is not None and count > limit:
        raise CapabilityFieldError(MAIL_URN, "maxMailboxesPerEmail", limit, count)


def check_delayed_send(seconds: float, capability: SubmissionCapability) -> None:
    """Reject a hold longer than the account allows (§1.3.2); a limit of 0 allows none."""
    limit = capability.max_delayed_send
    if limit is not None and seconds > limit:
        raise CapabilityFieldError(SUBMISSION_URN, "maxDelayedSend", limit, seconds)


#: RFC 8621 §4.2. What ``Email/get`` returns when ``properties`` is null. Worth
#: stating because it is a *subset* - notably it excludes ``bodyStructure`` and
#: ``bodyValues``, so "fetch an email" without naming properties gets no body.
EMAIL_DEFAULT_PROPERTIES: Final = (
    "id",
    "blobId",
    "threadId",
    "mailboxIds",
    "keywords",
    "size",
    "receivedAt",
    "messageId",
    "inReplyTo",
    "references",
    "sender",
    "from",
    "to",
    "cc",
    "bcc",
    "replyTo",
    "subject",
    "sentAt",
    "hasAttachment",
    "preview",
    "bodyValues",
    "textBody",
    "htmlBody",
    "attachments",
)

#: RFC 9219 §4.1. S/MIME verification adds properties to Email but no methods at
#: all, so it can only enter ``using`` by being named here.
#:
#: ``smimeStatusAtDelivery`` is the one worth knowing about: unlike
#: ``smimeStatus`` it does not change as trust anchors are removed, so comparing
#: the two answers "was this trusted when it arrived?" - which is a different
#: question from "is it trusted now?", and the only one that is stable.
_SMIME_PROPERTIES: Final = {
    "smimeStatus": SMIME_URN,
    "smimeStatusAtDelivery": SMIME_URN,
    "smimeErrors": SMIME_URN,
    "smimeVerifiedAt": SMIME_URN,
}

#: RFC 9219 §4.2. The filter conditions, which are *not* named after the
#: properties: there is no ``smimeStatus`` filter, and asking for one silently
#: filters on nothing.
_SMIME_FILTERS: Final = {
    "hasSmime": SMIME_URN,
    "hasVerifiedSmime": SMIME_URN,
    "hasVerifiedSmimeAtDelivery": SMIME_URN,
}

EMAIL_TYPE: Final = DataTypeSpec(
    name="Email",
    model=Email,
    default_get_properties=EMAIL_DEFAULT_PROPERTIES,
    adds_properties=_SMIME_PROPERTIES,
    adds_filter_fields=_SMIME_FILTERS,
    sort_options_field="emailQuerySortOptions",
)

#: RFC 8621 §11. Arrives only over the push channel and has no methods, but must
#: still be a legal value in ``PushSubscription.types``.
EMAIL_DELIVERY_TYPE: Final = DataTypeSpec(name="EmailDelivery", push_only=True)

MAIL: Final = CapabilitySpec(
    urn=MAIL_URN,
    attr="mail",
    reference="RFC 8621",
    account_value=MailCapability,
    data_types=(
        DataTypeSpec(name="Mailbox", model=Mailbox, shareable=True),
        DataTypeSpec(name="Thread", model=Thread),
        EMAIL_TYPE,
        # No id of its own - keyed by emailId (RFC 8621 §5).
        DataTypeSpec(name="SearchSnippet", model=SearchSnippet, identityless=True),
        EMAIL_DELIVERY_TYPE,
    ),
    methods=(
        # -- Mailbox (§2) -- #
        MethodSpec("Mailbox/get", MethodKind.GET, parse=dict, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("Mailbox/changes", MethodKind.CHANGES),
        MethodSpec(
            "Mailbox/query",
            MethodKind.QUERY,
            extra_args={
                "sortAsTree": "order children directly after their parent",
                "filterAsTree": "only include a mailbox if its ancestors match too",
            },
        ),
        MethodSpec("Mailbox/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec(
            "Mailbox/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={"onDestroyRemoveEmails": "destroy the emails inside, rather than failing"},
        ),
        # -- Thread (§3). No /query and no /set: threads are derived, not stored. -- #
        MethodSpec("Thread/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("Thread/changes", MethodKind.CHANGES),
        # -- Email (§4) -- #
        MethodSpec(
            "Email/get",
            MethodKind.GET,
            chunk_by=LimitKey.GET_OBJECTS,
            extra_args={
                "bodyProperties": "which EmailBodyPart properties to return",
                "fetchTextBodyValues": "populate bodyValues for textBody parts",
                "fetchHTMLBodyValues": "populate bodyValues for htmlBody parts",
                "fetchAllBodyValues": "populate bodyValues for every text/* part",
                "maxBodyValueBytes": "truncate each body value; 0 means no limit",
            },
        ),
        MethodSpec("Email/changes", MethodKind.CHANGES),
        MethodSpec(
            "Email/query",
            MethodKind.QUERY,
            extra_args={"collapseThreads": "return one email per thread"},
        ),
        MethodSpec(
            "Email/queryChanges",
            MethodKind.QUERY_CHANGES,
            extra_args={"collapseThreads": "must match the original query"},
        ),
        MethodSpec("Email/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS),
        MethodSpec(
            "Email/copy",
            MethodKind.COPY,
            mutating=True,
            # RFC 8620 §5.4: onSuccessDestroyOriginal makes the server emit an
            # extra Email/set under the same call id.
            implicit_responses=1,
            extra_args={
                "onSuccessDestroyOriginal": "destroy the source after copying",
                "destroyFromIfInState": "state guard for the destroy half",
            },
        ),
        MethodSpec(
            "Email/import",
            MethodKind.CUSTOM,
            mutating=True,
            extra_args={
                "emails": "creation id -> EmailImport (blobId, mailboxIds, keywords, receivedAt)",
                "ifInState": "state guard",
            },
        ),
        MethodSpec(
            "Email/parse",
            MethodKind.CUSTOM,
            extra_args={
                "blobIds": "blobs to parse as messages",
                "properties": "Email properties to return",
                "bodyProperties": "EmailBodyPart properties to return",
                "fetchTextBodyValues": "populate bodyValues for textBody parts",
                "fetchHTMLBodyValues": "populate bodyValues for htmlBody parts",
                "fetchAllBodyValues": "populate bodyValues for every text/* part",
                "maxBodyValueBytes": "truncate each body value",
            },
        ),
        # -- SearchSnippet (§5). Takes emailIds and a filter, not ids. -- #
        MethodSpec(
            "SearchSnippet/get",
            MethodKind.CUSTOM,
            filter_type="Email",
            extra_args={
                "emailIds": "the emails to snippet",
                "filter": "the query whose matches should be highlighted",
            },
        ),
    ),
)

SUBMISSION: Final = CapabilitySpec(
    urn=SUBMISSION_URN,
    attr="submission",
    reference="RFC 8621 §6-7",
    account_value=SubmissionCapability,
    # Sending needs the message, so submission always drags mail in with it.
    requires=frozenset({MAIL_URN}),
    data_types=(
        DataTypeSpec(name="Identity", model=Identity),
        DataTypeSpec(name="EmailSubmission", model=EmailSubmission),
    ),
    methods=(
        MethodSpec("Identity/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("Identity/changes", MethodKind.CHANGES),
        MethodSpec("Identity/set", MethodKind.SET, mutating=True, chunk_by=LimitKey.SET_OBJECTS),
        MethodSpec("EmailSubmission/get", MethodKind.GET, chunk_by=LimitKey.GET_OBJECTS),
        MethodSpec("EmailSubmission/changes", MethodKind.CHANGES),
        MethodSpec("EmailSubmission/query", MethodKind.QUERY),
        MethodSpec("EmailSubmission/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec(
            "EmailSubmission/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            # RFC 8621 §7.5: onSuccessUpdateEmail makes the server emit an extra
            # Email/set under this call's id - typically to clear $draft.
            implicit_responses=1,
            extra_args={
                "onSuccessUpdateEmail": "patch to apply to the email once sent",
                "onSuccessDestroyEmail": "submission ids whose email to destroy once sent",
            },
        ),
    ),
)

VACATION: Final = CapabilitySpec(
    urn=VACATION_URN,
    attr="vacation",
    reference="RFC 8621 §8",
    data_types=(
        # A singleton: its only id is the literal string "singleton", so there is
        # no /query and creating one is meaningless.
        DataTypeSpec(name="VacationResponse", model=VacationResponse, singleton_id="singleton"),
    ),
    methods=(
        MethodSpec("VacationResponse/get", MethodKind.GET),
        MethodSpec("VacationResponse/set", MethodKind.SET, mutating=True),
    ),
)

#: RFC 9219. Properties only - it adds no methods, which is exactly why
#: `using` derivation has to look at requested properties as well as method names.
SMIME_VERIFY: Final = CapabilitySpec(
    urn=SMIME_URN,
    reference="RFC 9219",
    requires=frozenset({MAIL_URN}),
)

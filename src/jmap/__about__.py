"""Version and spec-revision provenance for jmaplib.

``SPEC_REVISIONS`` is published so downstream users can detect when a capability
this library implements is tracking a draft that has since moved on. Capabilities
marked experimental are excluded from the SemVer promise precisely because these
revisions can change under us.
"""

from __future__ import annotations

from typing import Final

# Unannotated on purpose: hatchling's default version regex only matches a bare
# `__version__ = "..."` assignment.
__version__ = "1.1.0"

#: Maps a capability URN to the exact spec revision this library implements.
SPEC_REVISIONS: Final[dict[str, str]] = {
    "urn:ietf:params:jmap:core": "RFC 8620",
    "urn:ietf:params:jmap:mail": "RFC 8621",
    "urn:ietf:params:jmap:submission": "RFC 8621",
    "urn:ietf:params:jmap:vacationresponse": "RFC 8621",
    "urn:ietf:params:jmap:websocket": "RFC 8887",
    "urn:ietf:params:jmap:mdn": "RFC 9007",
    "urn:ietf:params:jmap:smimeverify": "RFC 9219",
    "urn:ietf:params:jmap:blob": "RFC 9404",
    "urn:ietf:params:jmap:quota": "RFC 9425",
    "urn:ietf:params:jmap:contacts": "RFC 9610",
    # No IETF document defines a contacts `:parse`; the URN and the shape behind
    # it are Stalwart's, mirroring the calendars companion.
    "urn:ietf:params:jmap:contacts:parse": "Stalwart vendor extension (v0.16)",
    # The pre-RFC contacts model, gated by vendor URNs rather than by the IETF
    # one. A server may advertise both at once; Cyrus 3.10 does.
    "https://www.fastmail.com/dev/contacts": "pre-RFC Contact/ContactGroup",
    "https://cyrusimap.org/ns/jmap/contacts": "pre-RFC Contact/ContactGroup",
    "urn:ietf:params:jmap:principals": "RFC 9670",
    "urn:ietf:params:jmap:principals:owner": "RFC 9670",
    "urn:ietf:params:jmap:sieve": "RFC 9661",
    "urn:ietf:params:jmap:webpush-vapid": "RFC 9749",
    # Experimental — tracking drafts, excluded from the SemVer promise.
    #
    # The calendars draft is in the RFC Editor queue but *blocked* on
    # draft-ietf-calext-jscalendarbis, which is with the IESG; -29 cites bis-20,
    # the current revision, and jmap.models.jscalendar follows it. Wire names can
    # still change; the event body is JSCalendar 2.0, not RFC 8984.
    "urn:ietf:params:jmap:calendars": "draft-ietf-jmap-calendars-29",
    "urn:ietf:params:jmap:calendars:parse": "draft-ietf-jmap-calendars-29",
    # Availability lives in the calendars draft, *not* in RFC 9670 — that
    # document defines no custom methods at all.
    "urn:ietf:params:jmap:principals:availability": "draft-ietf-jmap-calendars-29",
    "urn:ietf:params:jmap:filenode": "draft-ietf-jmap-filenode-14",
}

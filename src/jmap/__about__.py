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
__version__ = "0.1.0"

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
    "urn:ietf:params:jmap:contacts:parse": "RFC 9610",
    "urn:ietf:params:jmap:principals": "RFC 9670",
    "urn:ietf:params:jmap:principals:owner": "RFC 9670",
    "urn:ietf:params:jmap:sieve": "RFC 9661",
    "urn:ietf:params:jmap:webpush-vapid": "RFC 9749",
    # Experimental — tracking drafts, excluded from the SemVer promise.
    "urn:ietf:params:jmap:calendars": "draft-ietf-jmap-calendars-27",
    "urn:ietf:params:jmap:calendars:parse": "draft-ietf-jmap-calendars-27",
    "urn:ietf:params:jmap:principals:availability": "draft-ietf-jmap-calendars-27",
    "urn:ietf:params:jmap:filenode": "draft-ietf-jmap-filenode-14",
}

"""Logic shared by the sync and async clients.

The two shells differ only in where they put ``await``. Everything that can be
decided without doing I/O lives here so there is one implementation of it, and a
bug fixed in one flavour cannot survive in the other.

Nothing in this module performs I/O. It takes what a response *was* - a status,
some headers, a parsed body - and says what should happen next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from jmap.core.errors import RequestError, TransportError
from jmap.core.ijson import loads
from jmap.core.retry import Failure, Safety, classify, parse_retry_after

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jmap.core.retry import RetryPolicy
    from jmap.core.session import Session

#: RFC 8620 §3.1. Servers are entitled to reject anything else, and Stalwart does.
JMAP_CONTENT_TYPE: Final = "application/json"

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

#: A conservative default. The library never sends `Accept-Language` unless asked,
#: but RFC 8620 §3.8 says the server SHOULD honour it for localised error text.
DEFAULT_HEADERS: Final[Mapping[str, str]] = {
    "Content-Type": JMAP_CONTENT_TYPE,
    "Accept": JMAP_CONTENT_TYPE,
}


def request_headers(*, accept_language: str | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers


#: Why a blob operation could not work out which account it belongs to. Its own
#: constant because the reason differs from every other account resolution in the
#: library: `primaryAccounts` cannot answer for blobs at all, so pointing at it
#: would send the reader looking for a session field that is correctly absent.
NO_BLOB_ACCOUNT: Final = (
    "and this session implies no single account: blobs have no primaryAccounts "
    "entry to look up (RFC 8620 §2 keys that map by capability, and blobs belong "
    "to none), the session holds more than one account, and its primaryAccounts "
    "entries do not all name the same one - so pass account_id, or set a default "
    "on the client"
)


def problem_of(status: int, headers: Mapping[str, str], body: bytes) -> RequestError | None:
    """Build a :class:`RequestError` if the response is an RFC 7807 problem.

    Matched on status rather than content type alone: Stalwart labels its 401 as
    ``application/problem+json`` but plenty of intermediaries return a bare 502
    with an HTML body, and both need to become a typed error rather than a JSON
    decode failure.
    """
    if 200 <= status < 300:
        return None
    content_type = headers.get("content-type", headers.get("Content-Type", ""))
    if PROBLEM_CONTENT_TYPE in content_type:
        try:
            parsed = as_json_object(loads(body), "the API")
        except (ValueError, TransportError):
            parsed = None
        if parsed is not None:
            return RequestError.from_problem(parsed, status=status)
    return RequestError(
        "about:blank",
        status=status,
        title=f"HTTP {status}",
        detail=body[:512].decode("utf-8", "replace") or None,
    )


def as_json_object(value: Any, source: str) -> dict[str, Any]:
    """Narrow a parsed JSON value to an object, or say which endpoint misbehaved.

    The cast is what stops an `Any` from a JSON decode leaking into typed code:
    `isinstance` alone narrows to an unparameterised dict, which a strict type
    checker reports as partially unknown for the rest of its life.
    """
    if not isinstance(value, dict):
        raise TransportError(f"{source} returned a JSON value that is not an object")
    return cast("dict[str, Any]", value)


def failure_of(status: int, problem: RequestError | None) -> Safety:
    """Classify an HTTP response for retry purposes."""
    return classify(
        Failure.STATUS,
        status=status,
        problem_type=problem.type if problem is not None else None,
    )


def retry_pause(
    problem: RequestError,
    headers: Mapping[str, str],
    *,
    policy: RetryPolicy,
    attempt: int,
    now: float,
) -> float | None:
    """How long to wait before re-sending after ``problem``, or ``None`` to stop.

    The server's ``Retry-After`` wins over the backoff curve, up to the policy's
    ceiling (see :meth:`RetryPolicy.pause`). It is recorded on the error first,
    so a caller who gets the error back because the wait was too long knows how
    long the server asked for.
    """
    problem.retry_after = parse_retry_after(headers, now=now)
    return policy.pause(attempt, retry_after=problem.retry_after)


def session_is_stale(session: Session, response_session_state: str) -> bool:
    """Whether a response says our cached session is out of date (RFC 8620 §3.4).

    An empty state on either side means "unknown", which is treated as fresh: a
    server that omits ``sessionState`` should not put the client into a refetch
    loop.
    """
    if not response_session_state or not session.state:
        return False
    return response_session_state != session.state


def merged_created_ids(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Carry creation ids forward between the requests of a split batch."""
    merged = dict(existing)
    merged.update(incoming)
    return merged

"""When a failed JMAP request may safely be sent again.

JMAP has no idempotency key. A POST carrying a ``Foo/set`` that timed out may
have fully applied, and the client cannot tell - so "retry on failure" is not a
policy, it is a way to create duplicate drafts and double-sent mail.

The only safety signal the protocol gives is RFC 8620 §3.6.2: except for
``serverPartialFail``, if a method errors then server state did not change. That
covers *method* errors, not transport failures, so this module classifies a
failure by whether the request provably never reached application:

* **Never applied** - the connection failed before the request was sent, or the
  server rejected the whole request without running any method (429, 503, a
  request-level ``urn:ietf:params:jmap:error:limit``). Safe to retry as-is.
* **Might have applied** - a timeout after the bytes went out, or a 5xx that
  could have come after the work was done. Safe only if the request could not
  have changed anything, or if every mutation carried ``ifInState`` and would
  therefore fail the second time rather than duplicate.
* **Not worth retrying** - the request was understood and refused. Retrying
  sends the identical bytes for the identical answer.

``ifInState`` is what turns the middle case safe: RFC 8620 §5.3 makes a ``/set``
with a state guard fail with ``stateMismatch`` if anything changed, so a retry of
an already-applied request is rejected rather than duplicated.
"""

from __future__ import annotations

import email.utils
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from jmap.core.errors import URN_LIMIT

if TYPE_CHECKING:
    from collections.abc import Mapping

#: RFC 9110 §15.6.4 and §15.5.30.
STATUS_RATE_LIMITED: Final = 429
STATUS_SERVICE_UNAVAILABLE: Final = 503


class Safety(StrEnum):
    """Whether re-sending a request could duplicate work already done."""

    #: The request provably never reached application.
    NEVER_APPLIED = "never-applied"
    #: The request may have applied; only safe if it cannot have changed state,
    #: or if every mutation was guarded by ``ifInState``.
    MAYBE_APPLIED = "maybe-applied"
    #: Understood and refused. A retry sends identical bytes for the same answer.
    FUTILE = "futile"


class Failure(StrEnum):
    """What went wrong, at the level of detail the decision actually needs."""

    #: The connection never established: DNS, refused, TLS handshake.
    CONNECT = "connect"
    #: The request went out but no response came back in time.
    TIMEOUT = "timeout"
    #: A response arrived.
    STATUS = "status"


def classify(
    failure: Failure,
    *,
    status: int | None = None,
    problem_type: str | None = None,
) -> Safety:
    """Decide whether a failure could have left the request applied."""
    if failure is Failure.CONNECT:
        # Nothing was sent, so nothing can have run.
        return Safety.NEVER_APPLIED
    if failure is Failure.TIMEOUT:
        # The bytes went out. Silence is indistinguishable from a slow success.
        return Safety.MAYBE_APPLIED

    if problem_type == URN_LIMIT:
        # RFC 8620 §3.6.1: the request exceeded a limit and was rejected whole,
        # so no method in it ran.
        return Safety.NEVER_APPLIED
    if status in (STATUS_RATE_LIMITED, STATUS_SERVICE_UNAVAILABLE):
        return Safety.NEVER_APPLIED
    if status is not None and status >= 500:
        # A 500 may equally well have been raised while writing the response to
        # work that already completed.
        return Safety.MAYBE_APPLIED
    return Safety.FUTILE


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often and how fast to retry.

    Defaults are deliberately timid. A JMAP batch can carry a lot of work, and
    the failure modes worth retrying are transient by definition.
    """

    max_attempts: int = 3
    initial_backoff: float = 0.5
    max_backoff: float = 30.0
    multiplier: float = 2.0

    def backoff(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before ``attempt`` (1-based, so attempt 2 is the first retry).

        A server-supplied ``Retry-After`` wins outright, even when it is longer
        than ``max_backoff``: it is the server telling us when it will be ready,
        and ignoring it is how a thundering herd forms.
        """
        if retry_after is not None:
            return max(0.0, retry_after)
        exponent = max(0, attempt - 1)
        return min(self.max_backoff, self.initial_backoff * self.multiplier**exponent)


def should_retry(
    safety: Safety,
    *,
    policy: RetryPolicy,
    attempt: int,
    mutating: bool,
    all_mutations_guarded: bool = False,
) -> bool:
    """Whether to send the request again.

    ``mutating`` is whether the batch contains any method that can change server
    state; ``all_mutations_guarded`` whether every one of them carried
    ``ifInState``. A guarded retry cannot duplicate: the second attempt fails with
    ``stateMismatch`` if the first one landed.
    """
    if attempt >= policy.max_attempts:
        return False
    if safety is Safety.FUTILE:
        return False
    if safety is Safety.NEVER_APPLIED:
        return True
    return not mutating or all_mutations_guarded


def parse_retry_after(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Read ``Retry-After`` as seconds (RFC 9110 §10.2.3).

    The header is either a delay in seconds or an HTTP-date. The date form needs
    ``now`` to become a delay; without it the date form is ignored rather than
    guessed at, because a wrong wall-clock assumption produces a nonsense delay.

    An unparseable value yields ``None`` rather than raising. This is a hint about
    pacing, and a server sending nonsense should cost us the hint, not the retry:
    the caller falls back to its own backoff curve.
    """
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    text = raw.strip()
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    if now is None:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - now)

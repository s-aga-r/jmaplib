"""Finding a JMAP server from an email address (RFC 8620 §2.2, RFC 6186 style).

Three sources, tried in order, because no single one works everywhere:

1. **An explicit URL.** Always wins. Nothing here overrides what the user typed.
2. **SRV records.** ``_jmap._tcp.<domain>`` names the host and port. This is the
   only mechanism that works when the JMAP service does not live on the mail
   domain's web server at all - which is the normal arrangement for a hosted
   provider, and why the ``/.well-known`` guess below is not enough on its own.
3. **``https://<domain>/.well-known/jmap``.** The fallback every client tries.
   Fastmail answers 404 there, so a client that *only* does this cannot connect to
   one of the largest JMAP deployments in existence.

DNS is an optional dependency because most callers connect to a server they were
told about. When it is missing, SRV lookup is skipped with an error that says so
rather than silently falling through to the guess - a silent fallback turns a
missing package into a mysterious connection failure against a correctly
configured domain.

**Unless the resolver validates DNSSEC, SRV results are attacker-influenced.**
RFC 8620 §8.3 says so plainly: a poisoned answer points the client at someone
else's server, and whether that matters depends on the credential being presented.
TLS still authenticates the *host that was resolved to*, not the domain that was
asked about, so the name in the certificate is the SRV target - not the user's
mail domain.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jmap.core.errors import JMAPError

if TYPE_CHECKING:
    from collections.abc import Sequence

#: RFC 8620 §2.2's well-known path.
WELL_KNOWN_PATH: Final = "/.well-known/jmap"

#: The SRV service label for JMAP over TLS.
SRV_SERVICE: Final = "_jmap._tcp"

#: The scheme every JMAP endpoint uses. RFC 8620 §8.1 requires TLS.
SCHEME: Final = "https"

DEFAULT_PORT: Final = 443


class DiscoveryUnavailableError(JMAPError):
    """SRV discovery was asked for but the DNS dependency is not installed."""

    def __init__(self) -> None:
        super().__init__(
            "SRV discovery needs dnspython: install jmaplib[discovery]. Falling back "
            "to /.well-known/jmap silently would turn a missing package into a "
            "connection failure against a correctly configured domain"
        )


@dataclass(frozen=True, slots=True)
class SRVTarget:
    """One ``_jmap._tcp`` record.

    ``priority`` ascends and ``weight`` descends within a priority, which is the
    ordering RFC 2782 defines; the resolver's own ordering is not authoritative,
    so it is redone here.
    """

    host: str
    port: int = DEFAULT_PORT
    priority: int = 0
    weight: int = 0

    @property
    def session_url(self) -> str:
        """Where the session document should be, for this target.

        The port is omitted when it is the default, because a URL carrying
        ``:443`` compares unequal to the same URL without it in caches, redirect
        matching and OAuth redirect-URI checks.
        """
        authority = self.host if self.port == DEFAULT_PORT else f"{self.host}:{self.port}"
        return f"{SCHEME}://{authority}{WELL_KNOWN_PATH}"


def domain_of(address: str) -> str:
    """The domain half of an email address, or the input if it has no ``@``.

    A bare domain is accepted because that is what a user types when they know
    their provider but not the address they want to authenticate as.
    """
    _, _, domain = address.rpartition("@")
    return domain or address


def well_known_url(domain: str) -> str:
    """The RFC 8620 §2.2 fallback URL for a domain."""
    return f"{SCHEME}://{domain}{WELL_KNOWN_PATH}"


def order_targets(targets: Sequence[SRVTarget]) -> list[SRVTarget]:
    """Sort by RFC 2782 preference: lowest priority first, highest weight within it.

    Weight is meant to drive a weighted random choice among equals; sorting by it
    descending is the deterministic approximation, which is what a client wants
    when it is going to try them in order anyway.
    """
    return sorted(targets, key=lambda target: (target.priority, -target.weight, target.host))


def lookup_srv(domain: str, *, resolver: object | None = None) -> list[SRVTarget]:
    """Resolve ``_jmap._tcp.<domain>``, in preference order.

    Returns an empty list when the domain simply has no records - that is a
    normal answer meaning "use the well-known URL", not a failure. Only a missing
    dependency raises.
    """
    dns_resolver = resolver if resolver is not None else _default_resolver()
    query = getattr(dns_resolver, "resolve", None)
    if query is None:  # pragma: no cover - defensive against an odd resolver object
        raise DiscoveryUnavailableError
    try:
        answers = query(f"{SRV_SERVICE}.{domain}", "SRV")
    except Exception:
        # NXDOMAIN, NoAnswer, Timeout and friends all mean the same thing to a
        # caller: there is nothing here, try the well-known URL.
        return []
    targets = [
        SRVTarget(
            host=str(record.target).rstrip("."),
            port=int(record.port),
            priority=int(record.priority),
            weight=int(record.weight),
        )
        for record in answers
    ]
    return order_targets(targets)


def _default_resolver() -> object:
    try:
        import dns.resolver
    except ImportError as exc:
        raise DiscoveryUnavailableError from exc
    return dns.resolver.Resolver()


def candidate_urls(
    address: str, *, resolver: object | None = None, use_srv: bool = True
) -> list[str]:
    """Every URL worth trying for an address or domain, best first.

    The well-known URL is always last rather than omitted: a domain with SRV
    records may still answer there, and trying it costs one request against a
    server that has already failed to be reached any other way.
    """
    domain = domain_of(address)
    urls: list[str] = []
    if use_srv:
        # Asked for but unavailable is not fatal here: the caller still gets the
        # fallback, and finds out about the missing package from `lookup_srv`
        # when it calls that directly.
        with suppress(DiscoveryUnavailableError):
            urls.extend(target.session_url for target in lookup_srv(domain, resolver=resolver))
    fallback = well_known_url(domain)
    if fallback not in urls:
        urls.append(fallback)
    return urls

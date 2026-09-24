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
told about. When it is missing, :func:`lookup_srv` raises an error that says so
rather than returning nothing - an empty answer would pass for "this domain has
no records". :func:`candidate_urls`, and so ``JMAPClient.discover``, then skip SRV
and offer the well-known URL alone, which is all a domain answering there needs.

**Unless the resolver validates DNSSEC, SRV results are attacker-influenced.**
RFC 8620 §8.3 says so plainly: a poisoned answer points the client at someone
else's server, and whether that matters depends on the credential being presented.
TLS still authenticates the *host that was resolved to*, not the domain that was
asked about, so the name in the certificate is the SRV target - not the user's
mail domain. So RFC 6186 §6's rule applies: a target inside the queried domain is
used, and one outside it only once the caller confirms it - by asking its user,
or because its resolver validates.
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, ConfigDict, Field, ValidationError
from pydantic.dataclasses import dataclass

from jmap.core.errors import JMAPError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: RFC 8620 §2.2's well-known path.
WELL_KNOWN_PATH: Final = "/.well-known/jmap"

#: The SRV service label for JMAP over TLS.
SRV_SERVICE: Final = "_jmap._tcp"

#: The scheme every JMAP endpoint uses. RFC 8620 §8.1 requires TLS.
SCHEME: Final = "https"

DEFAULT_PORT: Final = 443

#: A host name a URL can carry: dot-separated labels of letters, digits,
#: hyphens and underscores, none starting or ending with a hyphen, at most 253
#: characters in all. DNS itself allows far more - a ":" in a label made httpx
#: refuse the whole URL.
_HOSTNAME: Final = re.compile(
    r"(?=.{1,253}\Z)"
    r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*\Z"
)


class DiscoveryUnavailableError(JMAPError):
    """SRV discovery was asked for but the DNS dependency is not installed."""

    def __init__(self) -> None:
        super().__init__(
            "SRV discovery needs dnspython: install jmaplib[discovery]. Falling back "
            "to /.well-known/jmap silently would turn a missing package into a "
            "connection failure against a correctly configured domain"
        )


def _host_name(value: str) -> str:
    if not _HOSTNAME.match(value):
        raise ValueError(f"{value!r} is not a host name a URL can carry")
    return value


#: RFC 2782 carries priority and weight as unsigned 16-bit numbers.
_UnsignedShort = Annotated[int, Field(ge=0, le=65535)]


@dataclass(frozen=True, slots=True, config=ConfigDict(strict=True, extra="forbid"))
class SRVTarget:
    """One ``_jmap._tcp`` record.

    ``priority`` ascends and ``weight`` descends within a priority, which is the
    ordering RFC 2782 defines; the resolver's own ordering is not authoritative,
    so it is redone here.

    A pydantic dataclass, and :attr:`session_url` is built from its fields, so
    each is checked when it is made: ``host`` must be a host name a URL can
    carry and ``port`` one a connection can be made to.
    """

    host: Annotated[str, AfterValidator(_host_name)]
    port: Annotated[int, Field(gt=0, le=65535)] = DEFAULT_PORT
    priority: _UnsignedShort = 0
    weight: _UnsignedShort = 0

    @property
    def session_url(self) -> str:
        """Where the session document should be, for this target.

        The port is omitted when it is the default, because a URL carrying
        ``:443`` compares unequal to the same URL without it in caches, redirect
        matching and OAuth redirect-URI checks.
        """
        authority = self.host if self.port == DEFAULT_PORT else f"{self.host}:{self.port}"
        return f"{SCHEME}://{authority}{WELL_KNOWN_PATH}"


class UnconfirmedSRVTargetError(JMAPError):
    """Nothing answered, and SRV named servers outside the domain that went untried.

    RFC 6186 §6 has a client ask before connecting to one: a record is only as
    trustworthy as the DNS answer that carried it, and TLS vouches for the host
    it names rather than for the domain asked about. Raised in place of the last
    failure, which is its ``__cause__``, so a caller can put the question to its
    user and try again with ``confirm_srv_target``.
    """

    def __init__(self, domain: str, targets: tuple[SRVTarget, ...]) -> None:
        self.domain = domain
        self.targets = targets
        named = ", ".join(f"{target.host}:{target.port}" for target in targets)
        super().__init__(
            f"no JMAP server answered for {domain}. Its SRV records also name {named}, "
            f"outside that domain, which went untried: a forged DNS answer can name any "
            f"host, so RFC 6186 §6 asks the user first. Pass confirm_srv_target to "
            f"accept one"
        )


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

    A record that makes no :class:`SRVTarget` - one naming no host a URL can
    carry, or port 0 - is left out: RFC 2782's "." target says the service is
    not offered there, and a label DNS allows but a URL does not - ``a:b`` -
    made httpx raise InvalidURL, which ended discovery before the well-known
    URL was tried.
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
    targets: list[SRVTarget] = []
    for record in answers:
        try:
            target = SRVTarget(
                host=str(record.target).rstrip("."),
                port=int(record.port),
                priority=int(record.priority),
                weight=int(record.weight),
            )
        except ValidationError:
            continue
        targets.append(target)
    return order_targets(targets)


def _default_resolver() -> object:
    try:
        import dns.resolver
    except ImportError as exc:
        raise DiscoveryUnavailableError from exc
    return dns.resolver.Resolver()


def _in_domain(host: str, domain: str) -> bool:
    """Whether ``host`` is ``domain`` or a name under it (RFC 6186 §6)."""
    host, domain = host.rstrip(".").lower(), domain.rstrip(".").lower()
    return host == domain or host.endswith(f".{domain}")


def candidate_urls(
    address: str,
    *,
    resolver: object | None = None,
    use_srv: bool = True,
    confirm_srv_target: Callable[[SRVTarget], bool] | None = None,
) -> list[str]:
    """Every URL worth trying for an address or domain, best first.

    An SRV target outside the address's domain is included only when
    ``confirm_srv_target`` returns true for it - see the module docstring for
    why an unconfirmed one must not receive a connection, let alone
    credentials. The well-known URL is always last rather than omitted: a
    domain with SRV records may still answer there, and trying it costs one
    request against a server that has already failed to be reached any other
    way.
    """
    domain = domain_of(address)
    urls: list[str] = []
    if use_srv:
        targets: list[SRVTarget] = []
        # Asked for but unavailable is not fatal here: the caller still gets the
        # fallback, and finds out about the missing package from `lookup_srv`
        # when it calls that directly.
        with suppress(DiscoveryUnavailableError):
            targets = lookup_srv(domain, resolver=resolver)
        urls.extend(
            target.session_url
            for target in targets
            if _in_domain(target.host, domain)
            or (confirm_srv_target is not None and confirm_srv_target(target))
        )
    fallback = well_known_url(domain)
    if fallback not in urls:
        urls.append(fallback)
    return urls

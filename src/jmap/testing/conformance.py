"""What a server actually supports, as a table.

Capability presence does not imply method presence, and the specs say so
outright - RFC 9404 §3.1 describes a server advertising ``urn:ietf:params:jmap:blob``
while implementing no ``Blob/lookup`` at all. A matrix built by *asking the server
what it advertises* and comparing that against what this library knows is
therefore the only honest answer to "can I use feature X here", and it is
cheap: no method calls, just the Session.

Three rows this deliberately keeps separate, because collapsing them is how a
conformance report ends up overstating things:

**Advertised and known.** The library models it and the server offers it.

**Advertised and unknown.** The server offers a URN this build does not model -
a vendor extension, or a spec newer than the library. Reachable through
``batch.add`` regardless, so it is listed rather than dropped.

**Known and not advertised.** This build could speak it; the server does not
offer it. Absent from most reports, and the most useful line when someone asks
why a feature is missing.

The matrix says nothing about whether a *method* works, only whether the
capability that defines it is present. Proving a method works needs calling it,
which is what ``tests/integration`` is for.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jmap.defaults import default_registry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from jmap.capabilities.registry import ActiveCapabilities, Registry
    from jmap.capabilities.spec import CapabilitySpec
    from jmap.core.session import Session


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    """One capability's standing against one server."""

    urn: str
    advertised: bool
    #: Whether this build models it at all.
    known: bool
    #: Whether it was *resolved* - a known experimental capability the caller did
    #: not opt into is advertised and known but unresolved, which is a third state
    #: and the one most likely to confuse.
    resolved: bool
    reference: str = ""
    experimental: bool = False
    methods: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.advertised and self.resolved:
            return "supported"
        if self.advertised and self.known and self.experimental:
            return "experimental (not opted in)"
        if self.advertised:
            return "advertised, not modelled"
        return "not advertised"


@dataclass(slots=True)
class Conformance:
    """A server's capability surface, as this build sees it."""

    username: str = ""
    rows: list[CapabilityRow] = field(default_factory=lambda: [])

    @property
    def supported(self) -> list[CapabilityRow]:
        return [row for row in self.rows if row.status == "supported"]

    @property
    def unknown(self) -> list[CapabilityRow]:
        """Advertised URNs this build does not model."""
        return [row for row in self.rows if row.advertised and not row.known]

    @property
    def missing(self) -> list[CapabilityRow]:
        """Capabilities this build speaks that the server does not offer."""
        return [row for row in self.rows if not row.advertised]

    def method_count(self) -> int:
        return sum(len(row.methods) for row in self.supported)

    def markdown(self) -> str:
        """Render the matrix as a Markdown document."""
        lines = [
            "# JMAP conformance",
            "",
            f"Server surface for `{self.username or 'unknown user'}`, as seen by "
            f"this build of jmaplib.",
            "",
            "Derived from the Session resource alone: it reports which capabilities "
            "are present, not whether any individual method works. Proving that "
            "needs calling them.",
            "",
            "| Capability | Status | Spec | Methods |",
            "|---|---|---|---|",
        ]
        for row in self.rows:
            methods = str(len(row.methods)) if row.methods else "—"
            lines.append(f"| `{row.urn}` | {row.status} | {row.reference or '—'} | {methods} |")
        lines += [
            "",
            f"**{len(self.supported)} capabilities supported**, "
            f"{self.method_count()} methods available.",
        ]
        if self.unknown:
            lines += [
                "",
                "## Advertised but not modelled",
                "",
                "Reachable through `batch.add(...)`; the library will not build "
                "typed calls for them.",
                "",
                *(f"- `{row.urn}`" for row in self.unknown),
            ]
        if self.missing:
            lines += [
                "",
                "## Modelled but not advertised",
                "",
                "This build can speak these; this server does not offer them.",
                "",
                *(f"- `{row.urn}` ({row.reference})" for row in self.missing),
            ]
        return "\n".join(lines) + "\n"


def analyse(
    session: Session,
    capabilities: ActiveCapabilities,
    *,
    registry: Registry | None = None,
) -> Conformance:
    """Build the matrix from a resolved session.

    ``capabilities`` is what the client resolved, so a caller that connected with
    ``experimental=False`` sees the draft-tracking capabilities reported as
    advertised-but-not-opted-into rather than as missing - which is the truthful
    answer, and a different one from "this server has no calendars".
    """
    known = registry or default_registry()
    advertised = capabilities.advertised
    urns = sorted(advertised | known.urns)

    rows: list[CapabilityRow] = []
    for urn in urns:
        specs = known.specs_for(urn)
        spec = specs[0] if specs else None
        resolved = capabilities.specs.get(urn)
        rows.append(
            CapabilityRow(
                urn=urn,
                advertised=urn in advertised,
                known=spec is not None,
                resolved=resolved is not None,
                reference=spec.reference if spec else "",
                experimental=bool(spec and spec.experimental),
                methods=_method_names(resolved or spec),
            )
        )
    return Conformance(username=session.username, rows=rows)


def _method_names(spec: CapabilitySpec | None) -> tuple[str, ...]:
    """Every method a spec declares, sorted, or nothing when there is no spec."""
    if spec is None:
        return ()
    return tuple(sorted(method.name for method in spec.methods))


def method_gaps(capabilities: ActiveCapabilities, expected: Iterable[str]) -> list[str]:
    """Which of ``expected`` this server does not offer.

    The method-granular check a test suite wants: a skip predicate that asks about
    the *capability* overstates what works, because a server may implement a
    capability partially and the specs allow it.
    """
    return sorted(name for name in expected if not capabilities.supports(name))


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI entry
    """Print a conformance matrix for a live server."""
    parser = argparse.ArgumentParser(description="Report a JMAP server's capability surface.")
    parser.add_argument("url", help="session URL, or anything redirecting to it")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--experimental", action="store_true", help="resolve draft specs too")
    parser.add_argument("--markdown", action="store_true", help="render as Markdown")
    args = parser.parse_args(argv)

    from jmap.auth import BasicAuth
    from jmap.client import JMAPClient

    with JMAPClient.connect(
        args.url, auth=BasicAuth(args.user, args.password), experimental=args.experimental
    ) as client:
        report = analyse(client.session, client.capabilities)
    if args.markdown:
        sys.stdout.write(report.markdown())
    else:
        for row in report.rows:
            sys.stdout.write(f"{row.status:<28} {row.urn}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())

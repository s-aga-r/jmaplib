# jmaplib

A complete, capability-driven [JMAP](https://jmap.io) client for Python.

```console
pip install jmaplib
```

```python
import jmap
```

> The distribution is `jmaplib`; the module is `jmap`. Same split as
> `python-dateutil` → `dateutil`.

## Why

Python has no JMAP client that covers the protocol. The one maintained option is
mail-only and GPL-3.0. `jmaplib` aims at the whole ecosystem — Mail, Submission,
Vacation, Contacts, Calendars, FileNode, Sieve, Quota, Blob, MDN, push over
EventSource and WebSocket, and Principals/Sharing — under MIT.

## Capability-driven by design

The library reads the server's Session resource and adapts to what that server
actually advertises. It will not put a URN on the wire that the server did not
offer, it derives `using` from the calls you make, and it enforces the server's
own advertised limits (`maxCallsInRequest`, `maxObjectsInGet`, `maxSizeUpload`)
before sending — auto-batching and auto-chunking where that is safe, and raising
where it is not.

A capability is **data**, not a class hierarchy — one `CapabilitySpec` per URN,
describing its data types and methods:

```python
from jmap.capabilities.registry import Registry
from jmap.capabilities.core import CORE

registry = Registry()
registry.register(CORE)

active = registry.resolve(session, account_id)   # per account, not per connection
active.supports("Email/get")                     # method-granular, not capability-granular
active.using_for(["Email/get"])                  # derived, then intersected with reality
active.unknown_urns                              # advertised but unrecognised — surfaced, never dropped
```

Four rules fall out of that, each of which exists because a real server
misbehaves without it:

- **Resolution is per account.** `accountCapabilities` is a *second* map, not a
  subset of the session-level one. Stalwart advertises `urn:stalwart:jmap` only
  at account level, so the answer is their union.
- **`using` is derived, then hard-intersected.** Under-declaring degrades
  silently (RFC 8620 §1.8); over-declaring makes Stalwart reject the *entire*
  request with `notRequest`, killing every unrelated call batched alongside it.
- **Capability presence ≠ method presence.** Stalwart advertises
  `urn:ietf:params:jmap:sieve` but implements no `SieveScript/changes`.
- **Properties can pull in a capability too.** `urn:ietf:params:jmap:smimeverify`
  adds properties but no methods, so derivation cannot look at method names alone.

## Status

Pre-alpha, under active development. Nothing is released yet.

| Milestone | Scope | State |
|---|---|---|
| M1 | I/O-free protocol kernel | **done** |
| M2 | Capability registry, auth, transports, sync + async shells | in progress |
| M3 | Mail (RFC 8621), blobs → **0.1.0** | planned |

M2 progress: capability spec model, registry and per-account resolution are in;
auth, transports, the twin client shells and the batch builder are next.

Specs tracked: RFC 8620, 8621, 8887, 9007, 9219, 9404, 9425, 9553, 9555, 9610,
9661, 9670, 9749, plus `draft-ietf-jmap-calendars-27` and
`draft-ietf-jmap-filenode-14` (both shipped behind an experimental flag and
excluded from the SemVer promise; see `jmap.SPEC_REVISIONS`).

## Development

```console
uv sync --all-extras
uv run pytest                    # integration tests are deselected by default
```

The full gate, which is what CI runs:

```console
uv run ruff check src/ tests/
uv run ruff format --check .
uv run mypy --strict src/ && uv run mypy tests/
uv run pyright src/
uv run pyright --verifytypes jmap --ignoreexternal   # must be 100%
uv run lint-imports                                   # layering contracts
uv run pytest --cov                                   # must be 100%
```

**Coverage is gated at 100%**, not aspirational. The kernel is pure functions
over plain data, so anything uncovered is either dead code or a branch nobody
thought through — both worth failing the build over. `lint-imports` enforces the
layering: `core` may not import httpx, asyncio or anyio, and `capabilities` may
not import the client.

## License

MIT

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

## Status

Pre-alpha, under active development. Nothing is released yet.

| Milestone | Scope | State |
|---|---|---|
| M1 | I/O-free protocol kernel | in progress |
| M2 | Capability registry, transports, sync + async shells | planned |
| M3 | Mail (RFC 8621), blobs → **0.1.0** | planned |

Specs tracked: RFC 8620, 8621, 8887, 9007, 9219, 9404, 9425, 9553, 9555, 9610,
9661, 9670, 9749, plus `draft-ietf-jmap-calendars-27` and
`draft-ietf-jmap-filenode-14` (both shipped behind an experimental flag and
excluded from the SemVer promise; see `jmap.SPEC_REVISIONS`).

## Development

```console
uv sync --all-extras
uv run pytest -m "not integration"
uv run ruff check . && uv run mypy --strict src/
uv run lint-imports
```

## License

MIT

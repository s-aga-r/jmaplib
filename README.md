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

active = registry.resolve(session, account_id)  # per account, not per connection
active.supports("Email/get")  # method-granular, not capability-granular
active.using_for(["Email/get"])  # derived, then intersected with reality
active.unknown_urns  # advertised but unrecognised — surfaced, never dropped
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
| M2 | Capability registry, auth, transports, sync + async shells | **done** |
| M3 | Mail (RFC 8621), blobs → **0.1.0** | in progress |

M3 progress: all RFC 8621 data models and the three mail capabilities (30 methods)
are registered; typed entity facades, header queries, `Email/set` creation
constraints, `/get` auto-chunking and blob upload/download are next.

### Mail is three capabilities, not one

RFC 8621 defines `…:mail`, `…:submission` and `…:vacationresponse` separately,
and servers advertise them independently — a read-only archive account may have
mail without submission. Merging them would put `:submission` in `using` for a
plain `Email/get` and, on a server that lacks it, fail the *entire* request.

`Identity` therefore lives under `:submission`, not `:mail`: it exists to name
what you may send *from*.

## Usage

```python
import httpx
from jmap.auth import BasicAuth
from jmap.client import JMAPClient

with JMAPClient.connect(
    "https://mail.example.com/.well-known/jmap",  # redirects are followed
    auth=BasicAuth("alice@example.com", "app-password"),
) as client:
    client.echo(hello="world")
```

A JMAP request *is* a batch, so batching is the normal path rather than an
optimisation. Calls queued in one block travel in one request, which is what
makes back-references natural:

```python
with client.batch() as batch:
    query = batch.add("Email/query", {"filter": {"inMailbox": inbox}})
    emails = batch.add("Email/get", {"ids": query.ref_ids()})  # resolved server-side

print(emails.result["list"])  # readable once the block exits
```

The async client is a mirror — same names, same behaviour, `await` in front:

```python
from jmap.aio import AsyncJMAPClient

async with await AsyncJMAPClient.connect(url, auth=auth) as client:
    async with client.batch() as batch:
        query = batch.add("Email/query", {})
        emails = batch.add("Email/get", {"ids": query.ref_ids()})
```

Everything either client decides — what `using` needs, how to split a batch,
whether a failure may be retried — is computed by the same I/O-free kernel, so
the two cannot disagree about protocol behaviour.

### Failures are caught locally where possible

These all raise before anything reaches the wire:

| Situation | Error |
|---|---|
| Method no advertised capability provides | `UnsupportedMethodError` |
| Mutation aimed at a read-only account | `ReadOnlyAccountError` |
| No `accountId`, and no `primaryAccounts` entry to resolve one | `NoAccountError` |
| Requesting a property the server never returns | `CapabilityFieldError` |
| `/set` larger than `maxObjectsInSet` | `CapabilityFieldError` |
| Reference chain that cannot fit `maxCallsInRequest` | `BatchTooLargeError` |

An oversized `/set` raises rather than being split, because splitting it would
break the single `ifInState` that makes it atomic.

## Testing against the library

The fake server ships with the package, so downstream tests need no network:

```python
from jmap.testing import FakeJMAPServer, ServerQuirks

server = FakeJMAPServer(quirks=ServerQuirks(unknown_using_is_not_request=True))
server.respond("Email/get", {"list": [{"id": "m1"}], "state": "s"})
client = JMAPClient.connect(url, auth=auth, http=httpx.Client(**server.client_kwargs()))
```

It resolves back-references with the library's *own* pointer evaluator, so the
test exercises what the client will really do, and `ServerQuirks` reproduces real
deviations — Stalwart answering an unknown `using` URN with a batch-destroying
`notRequest`, capabilities appearing only in `accountCapabilities`.

## Retry safety

JMAP has no idempotency key, so "retry on failure" is not a policy — it is a way
to create duplicate drafts and double-send mail. A POST carrying `Email/set` that
timed out may have fully applied, and the client cannot tell.

So failures are classified by whether the request *provably never reached
application*:

| Situation | Verdict |
|---|---|
| Connection never established | safe — nothing was sent |
| 429, 503, request-level `…:error:limit` | safe — rejected whole, no method ran |
| Timeout after the bytes went out | only if the batch cannot change state… |
| 5xx | …or every mutation carried `ifInState` |
| 4xx | never — understood and refused |

`ifInState` is what makes the middle rows safe: RFC 8620 §5.3 makes a guarded
`/set` fail with `stateMismatch` if anything changed, so a retry of an
already-applied request is rejected rather than duplicated.

## Authentication

RFC 8620 deliberately defines no auth scheme — the Session resource is simply an
authenticated endpoint — so the library presents credentials and reads the
`WWW-Authenticate` challenge that comes back.

```python
from jmap.auth import BasicAuth, BearerAuth, CallableAuth, OAuth2Auth, OAuth2Token

BasicAuth("alice@example.com", "app-password")  # self-hosted, Fastmail app passwords
BearerAuth("api-token")  # API tokens, static access tokens
CallableAuth(lambda: sign_request())  # escape hatch for anything else
OAuth2Auth(OAuth2Token("access", refresh_token="r"), refresh=renew, store=my_store)
```

Two behaviours here are correctness rather than preference:

- **Basic and Bearer never retry a 401.** The credential would be byte-identical
  the second time, so a retry cannot succeed — it can only look like brute force,
  and Stalwart fail2bans repeated failures.
- **Refresh is single-flight and persists before it discards.** Fastmail rotates
  the refresh token on every use and revokes the whole grant if an old one is
  replayed, so concurrent 401s must produce exactly one refresh, and the new
  token must reach your `TokenStore` before the old one is dropped.

Interactive OAuth *acquisition* (PKCE, device flow, RFC 9728/8414 discovery)
lands after 0.1.0; for now you bring a token.

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

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

**0.1.0** — Core protocol and RFC 8621 Mail. See [CHANGELOG.md](CHANGELOG.md).

| Milestone | Scope | State |
|---|---|---|
| M1 | I/O-free protocol kernel | **done** |
| M2 | Capability registry, auth, transports, sync + async shells | **done** |
| M3 | Mail (RFC 8621), blobs → **0.1.0** | **done** |
| M4 | Sync engine: change following, query views, state cursors | **done** |
| M5+ | Push, Contacts, Calendars, FileNode, Sieve, Quota | planned |

> **One caveat worth stating plainly.** 1161 tests and 100% coverage all run
> against the in-process fake server. The live integration suite is written and
> ready in `tests/integration/`, but has not been run against a real server:
> Stalwart's v0.16 headless bootstrap is unresolved (see
> [`docs/stalwart-spike.md`](docs/stalwart-spike.md), which includes an upstream
> documentation bug found along the way). Point it at any bootstrapped server with
> `JMAP_TEST_URL`, `JMAP_TEST_USER` and `JMAP_TEST_PASS` to verify for yourself.

### Capability namespaces

```python
with client.batch() as batch:
    query = batch.mail.email.query(filter={"inMailbox": inbox})
    emails = batch.mail.email.get(ids=query.ref_ids(), properties=["subject"])

emails.result.items[0].subject  # typed Email, one request
```

Namespaces come from the same resolution the raw path uses, so a mail-only server
has no `client.calendars` at all — an `AttributeError` at your call site rather
than a namespace that exists and fails on every call.

### Blobs

Blobs are not JMAP: they move over plain HTTP to URLs the Session advertises as
templates, so they are not batchable.

```python
uploaded = client.upload(pdf_bytes, content_type="application/pdf")
client.download(uploaded.blob_id, name="invoice.pdf")
```

`maxSizeUpload` is checked *before* sending — a 60 MB attachment against a 50 MB
limit fails in microseconds rather than after streaming 60 MB.

### Staying in sync

The library holds no cache of its own. It computes deltas and hands them over;
what to persist is the application's decision, so the only seam is a `StateStore`.

```python
from jmap.sync import ChangeStream, InMemoryStateStore

stream = ChangeStream(client, "Email", store=my_store)
changes = stream.catch_up()  # follows hasMoreChanges to the end
changes.touched  # created + updated, deduplicated
```

Two things this gets right that are easy to get wrong:

- **`hasMoreChanges` means there is more.** Reading one page and stopping loses
  the tail *silently*, because the state string still advances. `catch_up()`
  follows it to the end; `pages()` yields them one at a time for large mailboxes.
- **`cannotCalculateChanges` is not retryable.** RFC 8620 §5.2 requires the cache
  to be invalidated and re-downloaded, so it raises `ResyncRequiredError` rather
  than passing through as a generic method error — the recovery is different.

Delivery is **at least once**: the cursor advances only when you come back for the
next page, so a page being processed when the process dies arrives again. A caller
can absorb duplicates; it cannot recover changes it never saw.

`QueryView` keeps a cached result list current via `Foo/queryChanges`:

```python
view = QueryView.from_query(spec, query_response)
view.apply(query_changes_response)  # splices the delta in
view.known_ids  # what you actually hold
```

The cached list is **sparse** — RFC 8620 §5.6 models it as `["id1", null, "id3"]`,
where the nulls are positions you know exist but never fetched, and they are what
keep the indices meaningful. Removals are applied before insertions because the
`added` indices describe the list *after* removals, and an id appearing in *both*
arrays is a move, not a delete. The implementation reproduces the RFC's worked
example exactly, and that example is a test.

### `/get` chunks itself; `/set` refuses to

`maxObjectsInGet` caps how many ids one call may name, and a client holding a few
thousand ids from a `/query` exceeds it routinely. `/get` is safe to split — it
changes nothing — so it happens automatically and you still get one handle:

```python
emails = batch.add("Email/get", {"ids": three_thousand_ids})  # → 30 calls
emails.result.items  # merged back
```

The one thing that doesn't recombine cleanly is the `state` string. Each chunk
reports the state it was answered from, and if the data changed mid-read the
merged result would be a mix of two points in time — undetectable afterwards. So
mismatched states raise `TornReadError` rather than handing back a torn read.

`/set` is **not** chunked, for the reason given above: splitting it would break
the single `ifInState` that makes it atomic. It raises instead.

### Header queries own both halves of the round trip

A header is fetched by asking for a property whose *name encodes the request* —
and RFC 8621 §4.1.2 says the server echoes that name back **exactly as sent**.
Request `header:subject` and the answer is keyed `header:subject`; request
`header:Subject` and it is keyed `header:Subject`. Ask one way, read the other,
and you silently get `None`.

```python
from jmap.models.mail.headers import text, addresses, raw

subject = text("Subject")  # header:Subject:asText
subject.property_name  # what to request
subject.read(email)  # …and the key it comes back under

raw("Received", all=True)  # header:Received:all — every hop
addresses("To")  # header:To:asAddresses
```

Without `:all` you get the **last** occurrence, not the first and not a list.

### Creation constraints are checked before sending

`Email/set` is the one place the object you send is not shaped like the one you
get back. The server refuses with `invalidProperties`, which names the property
but not the rule — so the same mistake is easy to make twice:

```python
from jmap.models.mail.create import validate_email_create
validate_email_create({"mailboxIds": {"mb1": True}, "textBody": [...], ...})
```

It catches server-assigned properties (`id`, `blobId`, `threadId`, `size`), the
read-only `headers` list, describing the body *both* ways at once, empty
`mailboxIds`, malformed keywords, body parts with both or neither of
`partId`/`blobId`, and `bodyValues` entries that are unreferenced or missing.
It is deliberately a **subset** of §4.6 — anything needing server state (does the
mailbox exist? is the blob still live?) is left to the server.

### The method surface matches the server

Six method shapes cover almost all of JMAP, so they are written once and
parameterised by the object model. But a type does not get all six — and rather
than expose them everywhere and fail at run time, each façade is *composed from
the capability spec*:

| Type | Surface |
|---|---|
| `Email` | `get` `changes` `query` `query_changes` `set` `copy` |
| `Mailbox` | `get` `changes` `query` `query_changes` `set` |
| `Thread` | `get` `changes` — threads are derived, not stored |
| `VacationResponse` | `get` `set` — a singleton |

So `thread.query(...)` is an `AttributeError` at your call site rather than an
`unknownMethod` from the server. Responses are typed to match: `Email/get`
returns a `GetResponse[Email]`, `Email/set` a `SetResponse[Email]`.

A `/set` half-succeeds by design, so its per-object failures are values rather
than exceptions — raising would discard the objects that *did* change:

```python
result = emails.set(create={"d1": {...}}).result
result.created_id("d1")  # server-assigned id, or None
result.creation_errors  # {"d2": SetError(type="overQuota")}
```

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

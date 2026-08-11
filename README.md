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
- **Capability presence ≠ method presence.** The specs say so outright: RFC 9404
  §3.1 has a server advertise `urn:ietf:params:jmap:blob` with an empty
  `supportedTypeNames` when it implements no `Blob/lookup` at all.
- **Properties can pull in a capability too.** `urn:ietf:params:jmap:smimeverify`
  adds properties but no methods, so derivation cannot look at method names alone.

## Status

**1.0.0** — the whole ecosystem. See [CHANGELOG.md](CHANGELOG.md).

| Milestone | Scope | State |
|---|---|---|
| M1 | I/O-free protocol kernel | **done** |
| M2 | Capability registry, auth, transports, sync + async shells | **done** |
| M3 | Mail (RFC 8621), blobs → **0.1.0** | **done** |
| M4 | Sync engine: change following, query views, state cursors | **done** |
| M5 | Blob (RFC 9404), Quota (RFC 9425), Sieve (RFC 9661) → **0.2.0** | **done** |
| M6 | Push: EventSource, PushSubscription, VAPID, WebSocket → **0.3.0** | **done** |
| M7 | OAuth acquisition (PKCE, device flow), SRV discovery | **done** |
| M8 | Contacts (RFC 9610 + vendor), Sharing (RFC 9670) | **done** |
| M9 | Calendars (draft-27) — *experimental* | **done** |
| M10 | FileNode (draft-14) — *experimental* → **0.7.0** | **done** |
| M11 | MDN (RFC 9007), S/MIME, conformance matrix → **1.0.0** | **done** |

> **Two caveats worth stating plainly.**
>
> **Calendars and FileNode are experimental** and do not resolve unless you pass
> `experimental=True`. They track Internet-Drafts; the calendars draft is blocked
> on `jscalendarbis` and its own normative reference is already a revision stale,
> so its wire names can still change. `jmap.SPEC_REVISIONS` publishes exactly what
> this build targets.
>
> **The JSCalendar and JSContact bodies are carried, not modelled.** A
> `CalendarEvent` is a JSCalendar Event and a `ContactCard` is a JSContact Card;
> both round-trip losslessly through the model's `extra` and are readable by exact
> wire name (`event.jscalendar("recurrenceRule")`). What *is* modelled is the JMAP
> layer around them, which is where the traps live. Field-by-field models for
> those two vocabularies are a codegen job and are not done.
>
> **The live suite gates CI.** Every push bootstraps a real Stalwart v0.16
> headlessly and runs `tests/integration/` against it; see
> [`docs/stalwart-spike.md`](docs/stalwart-spike.md) for the procedure and what
> had to be discovered to make it work. Its first green run was worth the trouble
> — it found five defects no fake server could have, four of them in the library:
> a creation reference that never serialised, a back-reference nested where the
> wire format cannot express one, blob upload unable to resolve an account on any
> real server, and an event source that could not stay open past five seconds.
> Point it at your own server with `JMAP_TEST_URL`, `JMAP_TEST_USER` and
> `JMAP_TEST_PASS`.

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

### Blobs, two ways

The RFC 8620 way is not JMAP at all: blobs move over plain HTTP to URLs the
Session advertises as templates, so they are not batchable.

```python
uploaded = client.upload(pdf_bytes, content_type="application/pdf")
client.download(uploaded.blob_id, name="invoice.pdf")
```

`maxSizeUpload` is checked *before* sending — a 60 MB attachment against a 50 MB
limit fails in microseconds rather than after streaming 60 MB.

Where the server offers `urn:ietf:params:jmap:blob` (RFC 9404) there is a second
way, and it *is* batchable — which matters whenever something else in the same
request needs the new blobId:

```python
from jmap.models.blob import BlobUpload, DataSource

with client.batch() as batch:
    batch.blob.blob.upload(
        create={"s": BlobUpload(data=[DataSource.text(script)], type="application/sieve")}
    )
    batch.sieve.sieve_script.set(create={"A": {"name": "filters", "blobId": "#s"}})
```

`"#s"` is a *creation* reference, resolved by the server against `createdIds`. It
is a different mechanism from the `#argument` result references used elsewhere,
and it is the only one legal inside a `/set` object — RFC 8620 §3.7 result
references are top-level arguments only.

Sources concatenate, and a `blobId` source with `offset` and `length` splices an
existing blob without the octets ever leaving the server:

```python
DataSource.blob("#whole", offset=2, length=3)
```

Two things about reading blobs back are easy to get wrong, so the models handle
them: `size` is the size of the **whole** blob even under a range request (so
comparing it against `len(data)` is not how you detect a short read — `isTruncated`
is), and `data:asText` comes back **null with `isEncodingProblem`** when the
selected octets are not valid UTF-8, which is indistinguishable from an empty blob
unless you look at the flag. `Blob.data` reads whichever representation arrived.

Content moves inside the JSON request here, so it counts against `maxSizeRequest`;
RFC 9404 §4.1 recommends the upload endpoint past a megabyte. `Blob/copy` stays
with `:core` because RFC 8620 owns it, so the same type has different methods in
`client.core.blob` and `client.blob.blob` — the split is the spec's, not ours.

### Quotas and Sieve scripts

```python
with client.batch() as batch:
    quotas = batch.quota.quota.get(ids=None)

quotas.result.items[0].remaining  # headroom, floored at zero
```

There is no `Quota/set` — usage is server-computed — so `client.quota.quota` has
no `.set` attribute at all. `Quota/changes` carries `updatedProperties`, and its
`null` reads backwards from the obvious: RFC 9425 §4.3 requires it whenever the
server *cannot* tell what changed, so it means "fetch everything", not "nothing
changed". `fetch_all_properties` says which you have.

Sieve scripts are metadata plus a `blobId`. Activation is a side effect of `/set`
rather than a property, because `isActive` is server-set and at most one script may
hold it:

```python
with client.batch() as batch:
    batch.sieve.sieve_script.activate(script_id)
```

Destroying the active script needs *two* `/set` calls — RFC 9661 §2.4 requires the
deactivation to be separate — which is a batch, not a combined call. And
`maxSizeScriptName` counts **octets**, not characters: `check_name()` measures the
UTF-8 encoding, so a four-character CJK name is twelve.

RFC 9661 defines no `SieveScript/changes` at all, even though the type is
registered as usable for state change. Push can tell you scripts changed while
giving you no way to ask what — re-running `/get` is the answer, which is fine for
a handful of scripts.

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

### Push

Three transports, one idea: a `StateChange` names which types moved in which
accounts and nothing else. So whichever way it arrives — and whether or not some
are dropped — the follow-up is the same `/changes` call, and the client converges.
Push is an optimisation, never a second source of truth.

```python
from jmap.push import EventSourceClient

source = EventSourceClient(client, types=("Email", "Mailbox"), ping=30)
for event in source.listen():  # reconnects, resuming each time
    for account, states in event.outdated(my_cursors).items():
        ...  # only what actually moved
```

`outdated()` is the method that matters. A `StateChange` arriving does not mean
something changed *for you* — compare it against what you hold, or you re-fetch
types that never moved. And RFC 8620 §7.1 notes a notification can land while your
own `/set` is still in flight, so `matches()` recognises your own write.

Two rules here are silent when broken, so the library encodes both:

- **A ping is not a cursor.** RFC 8620 §7.3 forbids a ping from setting an event
  id. A client tracking "the last thing I received" resumes from a keep-alive and
  skips every change before it. The resume cursor is unmoved by pings.
- **`closeafter=state` ending the response is success.** Buffering proxies
  otherwise hold a stream back indefinitely, so the server ending it after one
  event is what was asked for. `listen()` reconnects rather than backing off.

`ping` also decides how long the client will wait on a silent stream. An event
source is idle by design, so it does *not* inherit the HTTP client's read timeout
— httpx defaults that to five seconds, which would hang up on every healthy
connection. A requested ping is a promise of traffic on a schedule and becomes the
deadline — allowing for the fact that §7.3 lets a server round a request up to a
minimum of 30 seconds, so asking for 5 and hanging up at 5 would kill a perfectly
conformant connection. With no ping requested there is no promise at all and the
client waits indefinitely, which is what "notify me when something changes" means.

Registering a URL instead is a three-step dance, and the middle step is the
security property: the server pushes a `PushVerification` and makes **no further
request** to that URL until the code comes back — which is what stops a
subscription being used to aim a JMAP server at a third party. The verification
can arrive *before* the `/set` response that created the subscription (§7.2.3), so
`PendingVerification` records codes as they land and claims them later, in
whichever order the two actually happen.

Over a WebSocket (RFC 8887) the same socket carries requests, responses, errors
and notifications, distinguished only by `@type`. Responses may come back **out of
order** — §4.3.2 says so — so `WebSocketProtocol` correlates by request id rather
than assuming FIFO. Its `pushState` token makes a reconnect cost one exchange
instead of a `/changes` call per type.

VAPID (RFC 9749) adds one field and one obligation: rotating the application
server key destroys subscriptions tied to the old one, and nothing raises when it
happens — notifications just stop. `needs_recreating(session, key)` is how you
find out.

### Signing in

`jmaplib` can find the server and get a token without being told either.

```python
from jmap.auth import OAuthClient

metadata = OAuthClient.discover(challenge.resource_metadata)  # RFC 9728 → RFC 8414
with OAuthClient(metadata, client_id="...") as oauth:
    token = oauth.authorize(scope="urn:ietf:params:jmap:core")  # PKCE, loopback
client = JMAPClient.discover("alice@example.com", auth=BearerAuth(token.access_token))
```

`JMAPClient.discover` tries `_jmap._tcp` SRV records before
`https://<domain>/.well-known/jmap`, because the well-known guess alone is not
enough — Fastmail answers 404 there.

Two things in the OAuth path are security properties rather than conveniences,
and both are the kind that work fine against a cooperative server:

- **The well-known segment is inserted, not appended.** RFC 8414 §3.1 puts
  `/.well-known/oauth-authorization-server` *between* host and path. Appending
  happens to work for single-tenant deployments, which is exactly why that bug
  survives to production.
- **The returned `issuer` is checked against the one the URL was built from**
  (§3.3). Without it, any host that merely answers that path can nominate
  whichever token endpoint it likes.

Only S256 PKCE is used. RFC 7636 also defines `plain`, where the challenge *is*
the verifier — which defeats the point for precisely the clients that need it, so
a server advertising neither is refused rather than downgraded to.

### Sharing

```python
from jmap import sharing

principals = sharing.principal_account(client.session, data_account_id)
with client.batch() as batch:
    batch.calendars.calendar.set(update={cal_id: sharing.grant(bob, {"mayReadItems": True})})
```

`grant()` and `revoke()` build *pointer* patches. Assigning `shareWith` wholesale
revokes everyone absent from the new map — the difference between "add Bob" and
"make Bob the only person with access". The owning Principal must never appear in
the map at all (RFC 9670 §4), and the account you address the `/set` at is **not**
the account the Principal ids come from: `principal_account()` is that lookup.

### Contacts, two models

RFC 9610 gave contacts `AddressBook` and `ContactCard`. Before it, Fastmail and
Cyrus shipped `Contact` and `ContactGroup` — and those are gated by *vendor* URNs,
not by `urn:ietf:params:jmap:contacts`. So they are three capabilities rather than
two flavours of one, a server may advertise several at once, and `using` derivation
handles it without being told:

```python
batch.contacts.contact_card.query(filter={"name/given": "Alice"})  # RFC 9610
batch.fastmail_contacts.contact.query(filter={"text": "Alice"})  # pre-RFC
```

Calling `Contact/get` with only the IETF URN in `using` earns `unknownMethod` from
a server that fully implements it, and the error names the *method* — which reads
as "this server has no contacts" rather than "you declared the wrong capability".

### What does this server actually do?

Capability presence does not imply method presence — the specs say so outright,
and RFC 9404 §3.1 describes a server advertising `urn:ietf:params:jmap:blob` while
implementing no `Blob/lookup` at all. So the honest answer is a matrix:

```console
python -m jmap.testing.conformance https://example.com/.well-known/jmap \
    --user alice@example.com --password ... --markdown
```

It costs no method calls — everything comes from the Session — and it keeps three
states apart that reports usually collapse into "supported":

| | meaning |
|---|---|
| advertised, modelled | typed calls available |
| advertised, **not** modelled | a vendor URN or a newer spec; still reachable via `batch.add` |
| modelled, **not** advertised | this build speaks it; this server does not offer it |

That last row is the one that answers "why is this feature missing", and it is
absent from most conformance reports.

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

validate_email_create(
    {
        "mailboxIds": {"mb1": True},
        "textBody": [{"partId": "t", "type": "text/plain"}],
        "bodyValues": {"t": {"value": "hello"}},
    }
)
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

## Documentation

Task-oriented guides live in [`docs/`](docs/index.md):

| | |
|---|---|
| [Getting started](docs/getting-started.md) | Install, connect, first request, async. |
| [Capabilities](docs/capabilities.md) | What the server advertises decides what you can call. |
| [Batching and references](docs/batching.md) | One request, many calls, chaining results. |
| [Mail](docs/mail.md) | Mailboxes, searching, reading, composing, sending. |
| [Blobs](docs/blobs.md) | Binary data, digests, lookup, copying. |
| [Staying in sync](docs/sync.md) | Change streams, query views, state cursors. |
| [Push](docs/push.md) | Event source, subscriptions, VAPID, WebSocket. |
| [Authentication](docs/auth.md) | Presenting credentials, and acquiring them. |
| [Errors](docs/errors.md) | Four failure levels, and which are safe to retry. |
| [Other capabilities](docs/extensions.md) | Quota, Sieve, contacts, calendars, files, sharing, MDN, S/MIME. |
| [Testing your own code](docs/testing.md) | The fake server that ships with the package. |

The rest of this README is design rationale: why the library is shaped the way
it is. If you want to *use* it, start with the guides.

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

A back-reference replaces a whole argument, because that is all the wire format
can express: `ids` becomes `#ids`. To point at an object being created in the
*same* request — a draft you are submitting as you write it — the mechanism is a
creation reference, which is an ordinary string and so works at any depth:

```python
from jmap import CreationRef

with client.batch() as batch:
    batch.mail.email.set(create={"draft": {...}})
    batch.submission.email_submission.set(
        create={"send": {"emailId": CreationRef("draft"), "identityId": identity}}
    )
```

Putting a `ResultRef` inside a `create` object instead raises
`NestedResultRefError` locally, naming the argument and pointing here — rather
than failing as a `TypeError` from inside the JSON encoder.

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
| No `accountId`, and no single account to resolve one | `NoAccountError` |
| Back-reference nested inside a `create` object | `NestedResultRefError` |
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

### Running against a live server

Integration tests are deselected by default; `-m integration` selects them. They
need a server and two accounts — the second one exists so the delivery test
(alice sends, bob receives) runs rather than skips, and that assertion is the one
thing the fake server cannot make.

Against a server you already have, no bootstrap is involved:

```console
JMAP_TEST_URL=https://mail.example.com/.well-known/jmap \
JMAP_TEST_USER=alice@example.com JMAP_TEST_PASS=... \
JMAP_TEST_USER2=bob@example.com  JMAP_TEST_PASS2=... \
  uv run pytest -m integration
```

Point `JMAP_TEST_URL` at `/.well-known/jmap` rather than the API URL: that way
the redirect and the survival of the `Authorization` header across it are
exercised for real. Everything the server does not implement skips with a reason
naming the method, so a partial server still produces a useful run.

**A TLS error from an `http://` URL is not a contradiction.** Only the session
document is fetched from the URL you supply; every call after that goes to the
`apiUrl` the *server* advertises, and a server that does not know its own public
URL will advertise `https://<hostname>` regardless of how you reached it. For
Stalwart that means `STALWART_PUBLIC_URL` is unset. Two env vars handle a
certificate no public CA vouches for, which is the normal case for a self-hosted
server:

| | |
|---|---|
| `JMAP_TEST_CA=/path/to/cert.pem` | Trust this certificate. Verification still happens, so a wrong host or an expired certificate still fails. Prefer this. |
| `JMAP_TEST_INSECURE=1` | Verify nothing. Also disables hostname checking, which is what catches a server advertising `https://localhost` when it means something else. |

To raise a throwaway Stalwart instead, the recipe CI uses:

```console
docker run -d --name jmaplib-test -p 18080:8080 \
  -e STALWART_PUBLIC_URL=http://localhost:18080 \
  -e STALWART_RECOVERY_ADMIN="admin:$STALWART_RECOVERY_ADMIN_PASS" \
  -v jmaplib-test-data:/var/lib/stalwart \
  stalwartlabs/stalwart:v0.16.17

STALWART_URL=http://localhost:18080 STALWART_CONTAINER=jmaplib-test \
STALWART_RECOVERY_ADMIN_PASS=... ./scripts/stalwart-bootstrap.sh
```

The script prints alice's and bob's generated passwords at the end. Two details
are load-bearing: **no config file is mounted**, because bootstrap mode triggers
only when the server finds none, and the volume is a **named volume**, because the
image runs as UID/GID 2000 and cannot write a bind mount. `STALWART_PUBLIC_URL`
must match the published port or the session advertises URLs nothing can reach.

> The script completes setup and restarts the container it is given. Point it only
> at a throwaway instance — never at a server holding anything you want to keep.
> Pick a host port nothing else uses, too: Stalwart defaults `socket_reuse_port`
> to true, so a second server binds an occupied port *silently* and the kernel
> then splits requests between the two.

`python -m jmap.testing.conformance <url> --user … --password … --markdown`
renders what any server actually supports, method by method.

**Coverage is gated at 100%**, not aspirational. The kernel is pure functions
over plain data, so anything uncovered is either dead code or a branch nobody
thought through — both worth failing the build over. `lint-imports` enforces the
layering: `core` may not import httpx, asyncio or anyio, and `capabilities` may
not import the client.

## License

MIT

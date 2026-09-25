# jmaplib

**A complete [JMAP](https://jmap.io) client for Python.** Read and send mail,
manage contacts and calendars, store files, and get notified the moment
something changes - on any server that speaks JMAP, such as Fastmail, Stalwart
or Cyrus.

```python
from jmap.auth import BasicAuth
from jmap.client import JMAPClient

with JMAPClient.connect(
    "https://mail.example.com/.well-known/jmap",
    auth=BasicAuth("alice@example.com", "app-password"),
) as client:
    with client.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)

    for mailbox in mailboxes.result.items:
        print(mailbox.name, mailbox.unread_emails)
```

- **Covers the whole protocol.** Mail, sending, vacation replies, contacts,
  calendars, files, Sieve filters, quotas, blobs, read receipts and sharing,
  with push over EventSource, Web Push and WebSocket.
- **Adapts to your server.** It reads what the server says it supports and
  offers exactly that, so a call the server cannot answer fails on your line of
  code, not in a response you have to decode.
- **Typed throughout.** Every response is a [pydantic](https://docs.pydantic.dev)
  model, and every argument you pass is checked before anything is sent.
- **Few round trips.** Calls travel together in one request and can feed each
  other their results. Server limits are respected for you.
- **Sync and async** with the same API. The async client runs on asyncio or trio.
- **Safe by default.** It retries a request only when that cannot make it apply
  twice, refuses to be downgraded from https to plain http, and asks before
  trusting a DNS answer with your credentials.
- **Easy to test.** An in-process fake JMAP server ships with the package.

Python 3.11+ · MIT licensed · fully typed · 100% test coverage · tested against
a live Stalwart server on every push.

## Contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
- [Common tasks](#common-tasks)
- [Handling errors](#handling-errors)
- [Configuration](#configuration)
- [Testing your code](#testing-your-code)
- [Supported specifications](#supported-specifications)
- [How it works](#how-it-works)
- [Documentation](#documentation)
- [Status and versioning](#status-and-versioning)
- [Contributing](#contributing)

## Installation

```console
pip install jmaplib
```

The package is called `jmaplib` on PyPI and is imported as `jmap`.

Optional extras - you need none of them for mail:

| Extra | Adds | Install it when |
|---|---|---|
| `jmaplib[discovery]` | `dnspython` | You want `JMAPClient.discover()` to look up SRV records. Without it, discovery tries only `https://<domain>/.well-known/jmap`. |
| `jmaplib[ws]` | `httpx-ws` | You speak JMAP over WebSocket (RFC 8887): `jmap.push.WebSocketClient` and its async twin send batches and receive push over one connection. |
| `jmaplib[push]` | `cryptography` | Your own Web Push endpoint decrypts push payloads (RFC 8291). |
| `jmaplib[cli]` | `typer`, `rich` | Nothing yet - it is reserved for command-line tools. The conformance report below needs only the standard library. |

## Quick start

Connect, find the inbox, and fetch its ten newest messages:

```python
from jmap.auth import BasicAuth
from jmap.client import JMAPClient

with JMAPClient.connect(
    "https://mail.example.com/.well-known/jmap",
    auth=BasicAuth("alice@example.com", "app-password"),
) as client:
    # 1. Queue calls in a batch; they are sent together when the block ends.
    with client.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)  # None means "all of them"

    inbox = next(m for m in mailboxes.result.items if m.role == "inbox")
    print(f"{inbox.name}: {inbox.unread_emails} unread")

    # 2. Search and fetch in a single request: the get uses the query's ids.
    with client.batch() as batch:
        found = batch.mail.email.query(
            filter={"inMailbox": inbox.id},
            sort=[{"property": "receivedAt", "isAscending": False}],
            limit=10,
        )
        emails = batch.mail.email.get(
            ids=found.ref_ids(), properties=["subject", "from", "receivedAt"]
        )

    for email in emails.result.items:
        sender = email.from_[0].email if email.from_ else "(unknown)"
        print(email.received_at, sender, email.subject)
```

What happened:

1. **`connect` fetched the session** - the server's description of itself: its
   accounts, the capabilities it supports and its limits. Everything afterwards
   is decided from it. Point it at `/.well-known/jmap`; the redirect to the real
   session URL is followed.
2. **Each `with client.batch()` block made one HTTP request.** The calls inside
   return *handles* immediately; their results are readable once the block ends.
3. **`found.ref_ids()` is a back-reference.** The server feeds the query's
   result into the `get` itself, so searching and fetching cost one round trip.
4. **Results are typed.** `inbox` is a `Mailbox` and `email` an `Email`, so your
   editor and type checker know every field. (`from` is a Python keyword, so the
   field is `from_`.)

For app passwords, OAuth and other credentials, see
[Sign in with OAuth](#sign-in-with-oauth) and [Authentication](docs/auth.md).

## Core concepts

### Batches and handles

A JMAP request is a list of method calls, so batching is the normal way to work
rather than an optimisation. Queue as many calls as you like:

```python
with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)
    identities = batch.submission.identity.get(ids=None)

print(len(mailboxes.result.items), len(identities.result.items))  # after the block
```

- A **handle** is returned as soon as a call is queued. Read `handle.result`
  after the block; reading it earlier raises `RuntimeError`.
- If one call fails, the others still succeed. The failure is raised only when
  you read *that* call's result - see [Handling errors](#handling-errors).
- `ids=None` means "every record", which is not the same as leaving `ids` out.
  The library keeps "not given" and JSON `null` apart everywhere.

### Referring to other calls

Calls in one batch can use each other's results, without a round trip:

```python
with client.batch() as batch:
    changes = batch.mail.email.changes(since_state=saved_state)
    updated = batch.mail.email.get(ids=changes.ref_updated())
```

Handles offer `ref_ids()`, `ref_list(prop)`, `ref_updated()`,
`ref_created(key)` and `ref(path)` for any other path.

To point at an object **created in the same request** - a draft you are sending
straight away, say - use a `CreationRef`. It works anywhere, including inside
the object being created, where a back-reference cannot go:

```python
from jmap import CreationRef

with client.batch() as batch:
    batch.mail.email.set(create={"draft": {...}})
    batch.submission.email_submission.set(
        create={"send": {"emailId": CreationRef("draft"), "identityId": identity_id}}
    )
```

More in [Batching and references](docs/batching.md).

### Namespaces follow the server

Each capability the server advertises becomes an attribute on the batch, and
each data type an attribute on that:

| Namespace | Data types |
|---|---|
| `batch.core` | `push_subscription`, `blob` (copying blobs between accounts) |
| `batch.mail` | `mailbox`, `thread`, `email`, `search_snippet` |
| `batch.submission` | `identity`, `email_submission` |
| `batch.vacation` | `vacation_response` |
| `batch.blob` | `blob` (upload in a request, read ranges, lookup) |
| `batch.quota` | `quota` |
| `batch.sieve` | `sieve_script` |
| `batch.contacts` | `address_book`, `contact_card` |
| `batch.fastmail_contacts`, `batch.cyrus_contacts` | `contact`, `contact_group` (the pre-RFC contact APIs) |
| `batch.principals` | `principal`, `share_notification` |
| `batch.mdn` | `mdn` (read receipts) |
| `batch.calendars` *(experimental)* | `calendar`, `calendar_event`, `participant_identity`, `calendar_event_notification` |
| `batch.files` *(experimental)* | `file_node` |

A namespace exists only when the server offers the capability, and a data type
has only the methods the server implements. A mail-only server has no
`batch.calendars`, and `Thread` has no `.query()`. Either is an
`AttributeError` at your call site rather than an error from the server. To
check before you call:

```python
if client.capabilities.supports("SieveScript/get"):
    ...
```

See [Capabilities](docs/capabilities.md).

### Typed results, checked arguments

Responses are parsed into models: `Email/get` gives a `GetResponse[Email]`,
`Email/set` a `SetResponse[Email]`. Properties the model does not know are kept,
not dropped, so reading an object and writing it back loses nothing.

Arguments are checked with pydantic before the call is queued, strictly - `"5"`
is not a number - so mistakes fail where you made them:

```python
batch.mail.email.get(ids="m1")
# pydantic_core.ValidationError: 1 validation error for Email.get
# ids
#   'str' instances are not allowed as a Sequence value [type=sequence_str, ...]
```

When creating objects you can pass either the wire mapping or a typed model. A
model sends only the fields you set:

```python
from jmap.models.mail.objects import Mailbox

with client.batch() as batch:
    created = batch.mail.mailbox.set(create={"r": Mailbox(name="Receipts")})

receipts_id = created.result.created_id("r")
```

### The raw path

Every modelled method has a typed builder. Underneath them is a raw path, which
takes the method name and wire-spelled arguments and returns the response as a
dict - for anything the typed surface does not cover:

```python
result = client.call("Mailbox/get", {"ids": None})  # one call, no batch

with client.batch() as batch:
    parsed = batch.add("Email/parse", {"blobIds": [blob_id]})  # parsed.result is a dict
```

The raw path skips the typed surface and the argument checks, but not the rest:
the account is resolved, limits apply, and an unsupported method still fails
before it is sent.

## Common tasks

### Search and read mail

```python
with client.batch() as batch:
    found = batch.mail.email.query(
        filter={"inMailbox": inbox.id, "hasKeyword": "$flagged"},
        sort=[{"property": "receivedAt", "isAscending": False}],
        limit=25,
        calculate_total=True,
    )
    emails = batch.mail.email.get(
        ids=found.ref_ids(), properties=["subject", "from", "receivedAt", "preview"]
    )

print(found.result.total, "flagged messages")
```

Ask for the properties you need; a bare `Email/get` returns the whole, large
object. Page with `position=` or `anchor=` (not both). To show *why* each result
matched, add `batch.mail.search_snippet.get(filter=..., email_ids=found.ref_ids())`
to the same batch. Message bodies arrive separately from their structure, so ask
for them explicitly:

```python
with client.batch() as batch:
    got = batch.mail.email.get(
        ids=[email_id],
        properties=["subject", "textBody", "bodyValues"],
        fetchTextBodyValues=True,  # RFC 8621 arguments keep their wire spelling
    )

email = got.result.items[0]
for part in email.text_body or []:
    body = (email.body_values or {}).get(part.part_id or "")
    if body:
        print(body.value)
```

### Flag, move and delete messages

Updates are *patches* that change only what they name, so two clients editing
the same message do not overwrite each other. Helpers build the common ones:

```python
from jmap.core.patch import keyword_patch, mailbox_patch

patch = keyword_patch(add=["$seen"], remove=["$flagged"])
patch.update(mailbox_patch(add=[archive_id], remove=[inbox_id]))  # a move

with client.batch() as batch:
    changed = batch.mail.email.set(update={email_id: patch}, destroy=[spam_id])

if changed.result.has_errors:
    print(changed.result.update_errors, changed.result.destroy_errors)
```

`destroy` deletes permanently; moving to the `trash` mailbox is the gentle
option. A `/set` can partly succeed, so per-object failures come back as
values, not exceptions.

### Send a message

Sending takes two objects in one request: the `Email` (the draft) and the
`EmailSubmission` (the instruction to send it).

```python
from jmap import CreationRef

with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)
    identities = batch.submission.identity.get(ids=None)

drafts = next(m for m in mailboxes.result.items if m.role == "drafts")
identity = identities.result.items[0]  # who you may send as

with client.batch() as batch:
    batch.mail.email.set(
        create={
            "draft": {
                "mailboxIds": {drafts.id: True},
                "keywords": {"$draft": True},
                "from": [{"email": identity.email, "name": identity.name}],
                "to": [{"email": "bob@example.com"}],
                "subject": "Lunch?",
                "textBody": [{"partId": "body", "type": "text/plain"}],
                "bodyValues": {"body": {"value": "One o'clock?"}},
            }
        }
    )
    sent = batch.submission.email_submission.set(
        create={"send": {"identityId": identity.id, "emailId": CreationRef("draft")}},
        # Applied only if sending succeeds, so the message stops being a draft.
        onSuccessUpdateEmail={"#send": {"keywords/$draft": None}},
    )

assert not sent.result.has_errors, sent.result.creation_errors
```

`jmap.models.mail.create.validate_email_create()` checks a draft against the
rules the server enforces before you send it. More in [Mail](docs/mail.md).

### Attachments and other files

Blobs - attachments, uploads, raw messages - move over plain HTTP:

```python
uploaded = client.upload(pdf_bytes, content_type="application/pdf")
data = client.download(uploaded.blob_id, name="invoice.pdf", content_type="application/pdf")
```

To attach an upload, name its `blobId` in the draft:
`"attachments": [{"blobId": uploaded.blob_id, "type": "application/pdf", "name": "invoice.pdf"}]`.
The server's `maxSizeUpload` is checked before a byte is sent. Servers with the
Blob capability (RFC 9404) also accept uploads *inside* a batch, read byte
ranges and compute digests - see [Blobs](docs/blobs.md).

### Keep a local copy in sync

Every data type has a state string, and the server can list what changed since
one. `ChangeStream` follows those changes for one type:

```python
from jmap.sync import ChangeStream, ResyncRequiredError

stream = ChangeStream(client, "Mailbox")  # pass store= to persist the cursor

with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)
stream.seed(mailboxes.result.state)  # your copy starts here

# Later - on a timer, or when push says so:
try:
    changes = stream.catch_up()  # follows every page to the end
    print(changes.created, changes.updated, changes.destroyed)
except ResyncRequiredError:
    ...  # the server can no longer diff from your state: fetch again and re-seed
```

`changes.touched` is created plus updated - the ids worth fetching again. To
keep a *search result* current, see `QueryView` in
[Staying in sync](docs/sync.md).

### Get notified when something changes

The event source is a long-lived connection on which the server announces
changes. It tells you *what moved*, and you fetch it as above:

```python
from jmap.push import EventSourceClient, Ping

known = {account_id: {"Email": email_state, "Mailbox": mailbox_state}}

for event in EventSourceClient(client, types=("Email", "Mailbox"), ping=30).listen():
    if isinstance(event, Ping):
        continue  # a keep-alive
    for account, moved in event.outdated(known).items():
        print(account, "changed:", sorted(moved))  # e.g. ['Email']: catch up on it
```

`listen()` reconnects on its own and resumes where it left off. To carry
requests *and* push over one connection, use a WebSocket instead
(`jmaplib[ws]`):

```python
from jmap.push import WebSocketClient

with WebSocketClient(client) as socket:
    socket.enable_push(["Email"])
    for change in socket.notifications():
        ...  # socket.batch() works here too, over the same connection
```

Push subscriptions (the server calls your URL), Web Push encryption and the async
WebSocket client are covered in [Push](docs/push.md).

### Use it from async code

`AsyncJMAPClient` has the same API. Await `connect` and use `async with`, but
queueing a call needs no `await`:

```python
import asyncio

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth


async def main() -> None:
    auth = BasicAuth("alice@example.com", "app-password")
    async with await AsyncJMAPClient.connect(url, auth=auth) as client:
        async with client.batch() as batch:
            mailboxes = batch.mail.mailbox.get(ids=None)
        print([m.name for m in mailboxes.result.items])


asyncio.run(main())
```

### Sign in with OAuth

The library can find the authorization server from the JMAP server and run the
sign-in flow itself - in the browser with PKCE, or with a device code:

```python
from jmap.auth import OAuth2Auth, OAuthClient, protected_resource_url

url = "https://mail.example.com/.well-known/jmap"
metadata = OAuthClient.discover(protected_resource_url("https://mail.example.com"), resource=url)

oauth = OAuthClient(metadata, client_id="my-app")
token = oauth.authorize(scope="urn:ietf:params:jmap:core", open_browser=True)
# No browser on this machine? token = oauth.device_flow(scope="urn:ietf:params:jmap:core")

client = JMAPClient.connect(
    url,
    auth=OAuth2Auth(token, refresh=lambda current: oauth.refresh(current.refresh_token or "")),
)
```

`OAuth2Auth` renews the token shortly before it expires, and only once however
many requests notice at the same time. Pass `store=` to save each new token
before the old one is dropped - some servers revoke the whole grant when an old
refresh token is used. Other credentials:

```python
from jmap.auth import BasicAuth, BearerAuth, CallableAuth

BasicAuth("alice@example.com", "app-password")  # passwords and app passwords
BearerAuth("api-token")  # a static token
CallableAuth(lambda: build_header())  # anything else: return the whole header
```

Details, including dynamic client registration and scopes, are in
[Authentication](docs/auth.md).

### Find the server from an email address

```python
client = JMAPClient.discover("alice@example.com", auth=auth)
```

It tries the domain's `_jmap._tcp` SRV records (with `jmaplib[discovery]`) and
then `https://<domain>/.well-known/jmap`. A server that DNS names *outside* your
domain is tried only if you approve it with `confirm_srv_target=`, because an
unsigned DNS answer could send your credentials anywhere. See
[Getting started](docs/getting-started.md).

### Contacts, calendars and more

The same patterns work for every data type:

```python
with client.batch() as batch:
    books = batch.contacts.address_book.get(ids=None)
    cards = batch.contacts.contact_card.query(filter={"text": "Alice"})
    quotas = batch.quota.quota.get(ids=None)
```

| For | Namespace | Notes |
|---|---|---|
| Contacts | `batch.contacts` | JSContact cards (RFC 9610), and parsing vCards where the server offers it. Older Fastmail and Cyrus APIs are `batch.fastmail_contacts` and `batch.cyrus_contacts`. |
| Calendars | `batch.calendars` | Experimental: connect with `experimental=True`. Includes parsing `.ics` files and free/busy (`batch.principals.principal.get_availability`) where the server offers them. |
| Files | `batch.files` | Experimental: connect with `experimental=True`. |
| Quotas | `batch.quota` | Read-only; `quota.remaining` is the headroom left. |
| Mail filters | `batch.sieve` | Upload, validate and activate Sieve scripts. |
| Sharing | `batch.principals` | Plus `jmap.sharing` to build share patches safely. |
| Read receipts | `batch.mdn` | Send and parse MDNs (RFC 9007). |
| Out-of-office | `batch.vacation` | The vacation response. |

See [Other capabilities](docs/extensions.md) for each one's gotchas.

## Handling errors

Every error the library raises for the server, the network or bad data derives
from `jmap.JMAPError`. What matters is *how much* failed:

| Level | Raised as | What still worked |
|---|---|---|
| Transport | `TransportError`, or `AuthenticationError` for rejected credentials | Nothing - no JMAP response arrived |
| Request | `RequestError` (an RFC 7807 problem) | Nothing - the server ran no method |
| Method | `MethodError`, when you read that call's `.result` | Every other call in the batch |
| Object | `SetError` values in a `/set` result - never raised | Every other object in the same `/set` |

```python
from jmap import MethodError, RequestError, TransportError

try:
    with client.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)
        changes = batch.mail.email.changes(since_state=saved_state)
except (TransportError, RequestError):
    ...  # nothing ran; try again later

print(len(mailboxes.result.items))  # unaffected by the other call
try:
    changes.result
except MethodError as error:
    print(error.type)  # for example "cannotCalculateChanges"
```

Many mistakes are caught before anything is sent:

| Situation | Error |
|---|---|
| An argument of the wrong type or range - `ids="m1"`, `limit=-5` | `pydantic.ValidationError` |
| A method no advertised capability provides | `UnsupportedMethodError` |
| A change aimed at a read-only account | `ReadOnlyAccountError` |
| No `accountId`, and no single account to use | `NoAccountError` |
| A back-reference nested inside an object being created | `NestedResultRefError` |
| A property the server never returns | `CapabilityFieldError` |
| A `/set` larger than the server's `maxObjectsInSet` | `CapabilityFieldError` |
| A chain of references too long for one request | `BatchTooLargeError` |

**Retries are automatic, but only when safe.** JMAP has no idempotency key, so
re-sending a `/set` that timed out could create a second copy. The library
retries only when the request provably never ran - a failed connection, a 429,
a 503 - or when it cannot have changed anything. A batch that writes is retried
after a timeout only if every write carries `if_in_state`, which makes a repeat
fail with `stateMismatch` instead of applying twice.

More in [Errors](docs/errors.md).

## Configuration

```python
from jmap.core.retry import RetryPolicy

client = JMAPClient.connect(
    url,
    auth=auth,
    account_id="u1234",  # use one account for everything
    retry_policy=RetryPolicy(max_attempts=5, initial_backoff=1.0),
    timeout=60.0,  # seconds
    experimental=True,  # opt into draft specifications (calendars, files)
)
```

| Option | Default | |
|---|---|---|
| `auth` | required | Any `httpx.Auth`; see [Authentication](docs/auth.md). |
| `account_id` | per capability | Normally each call uses the server's primary account for its capability, which is right when mail, contacts and calendars live in different accounts. |
| `retry_policy` | 3 attempts | Attempts and backoff. Values are checked when the policy is made. |
| `timeout` | 30 s | Ignored when you pass your own `http`. |
| `http` | a new client | Your own `httpx.Client` - for proxies, custom TLS or a shared pool. |
| `experimental` | `False` | Enable capabilities that track Internet-Drafts. |
| `registry` | all built in | Your own capability registry, to add or remove support. |

## Testing your code

`FakeJMAPServer` is an in-process JMAP server - no network, no container. It
answers the session request, resolves back-references like a real server, and
returns whatever you tell it to:

```python
import httpx

from jmap.auth import BasicAuth
from jmap.client import JMAPClient
from jmap.testing import FakeJMAPServer


def test_unread_count():
    server = FakeJMAPServer(
        capabilities={"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {}},
        primary_accounts={"urn:ietf:params:jmap:core": "a", "urn:ietf:params:jmap:mail": "a"},
    )
    server.respond(
        "Mailbox/get",
        {
            "accountId": "a",
            "state": "s1",
            "list": [{"id": "m1", "name": "Inbox", "unreadEmails": 3}],
        },
    )

    with JMAPClient.connect(
        "https://jmap.example.com/.well-known/jmap",
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**server.client_kwargs()),
    ) as client:
        with client.batch() as batch:
            mailboxes = batch.mail.mailbox.get(ids=None)

    assert mailboxes.result.items[0].unread_emails == 3
```

Leave capabilities out to test how your code copes without them, and use
`ServerQuirks` to reproduce known server behaviours. See
[Testing your own code](docs/testing.md).

To see what a real server supports, method by method:

```console
python -m jmap.testing.conformance https://mail.example.com/.well-known/jmap \
    --user alice@example.com --markdown
```

It asks for the password, or reads `$JMAP_PASSWORD`.

## Supported specifications

| Specification | Covers | Where |
|---|---|---|
| RFC 8620 | JMAP core: sessions, requests, blobs, push | `batch.core`, `jmap.push` |
| RFC 8621 | Mail, sending, vacation responses | `batch.mail`, `batch.submission`, `batch.vacation` |
| RFC 8887 | JMAP over WebSocket | `jmap.push.WebSocketClient`, `AsyncWebSocketClient` |
| RFC 9007 | Read receipts (MDN) | `batch.mdn` |
| RFC 9219 | S/MIME signature verification | properties on `Email` |
| RFC 9404 | Blob management | `batch.blob` |
| RFC 9425 | Quotas | `batch.quota` |
| RFC 9610 | Contacts (JSContact) | `batch.contacts` |
| RFC 9661 | Sieve scripts | `batch.sieve` |
| RFC 9670 | Principals and sharing | `batch.principals`, `jmap.sharing` |
| RFC 9749 | Web Push VAPID keys | `jmap.push` |
| Fastmail and Cyrus contacts | The pre-RFC contacts APIs | `batch.fastmail_contacts`, `batch.cyrus_contacts` |
| draft-ietf-jmap-calendars-27 | Calendars *(experimental)* | `batch.calendars` |
| draft-ietf-jmap-filenode-14 | File storage *(experimental)* | `batch.files` |

Signing in follows the OAuth 2.0 family: RFC 6749 and 6750, PKCE (RFC 7636),
native apps (RFC 8252), server metadata (RFC 8414), the device flow (RFC 8628),
dynamic registration (RFC 7591) and protected-resource metadata (RFC 9728).
`jmap.SPEC_REVISIONS` lists exactly which revision each capability implements.

## How it works

A few principles explain most of the library's behaviour:

- **The server's session decides.** On connect the client reads the session and
  works out, account by account, what the server supports - down to single
  methods, because advertising a capability does not mean implementing all of
  it. Namespaces, methods and checks all follow from that.
- **The `using` list is derived for you.** Each request must declare the
  capabilities it needs. The library works it out from your calls, arguments and
  properties, and never lists one the server did not advertise: some servers
  reject a whole request over a single unknown capability.
- **Limits are enforced before sending.** A batch longer than
  `maxCallsInRequest` is split across requests, carrying creation references
  across. A `/get` naming more ids than `maxObjectsInGet` is split and merged
  back, and refused if the data changed in between. A `/set` too large is
  refused rather than split, because splitting it would lose its all-or-nothing
  guarantee.
- **One protocol core, two clients.** Everything that can be decided without
  I/O - requests, responses, retries, references, sync - lives in a kernel that
  both the sync and async clients call, so they cannot disagree.
- **Nothing the server sends is lost.** Models keep unknown properties, types
  without a model come back as a read-only `JMAPObject`, and JSON is parsed
  strictly to I-JSON.

The guides explain the reasons behind each of these, with the RFC sections
involved.

## Documentation

Task-oriented guides live in [`docs/`](docs/index.md):

| Guide | |
|---|---|
| [Getting started](docs/getting-started.md) | Install, connect, first request, async |
| [Capabilities](docs/capabilities.md) | What the server advertises decides what you can call |
| [Batching and references](docs/batching.md) | One request, many calls, chaining results |
| [Mail](docs/mail.md) | Mailboxes, searching, reading, composing, sending |
| [Blobs](docs/blobs.md) | Binary data, digests, lookup, copying |
| [Staying in sync](docs/sync.md) | Change streams, query views, state cursors |
| [Push](docs/push.md) | Event source, subscriptions, VAPID, WebSocket |
| [Authentication](docs/auth.md) | Presenting credentials, and acquiring them |
| [Errors](docs/errors.md) | Four failure levels, and which are safe to retry |
| [Other capabilities](docs/extensions.md) | Quota, Sieve, contacts, calendars, files, sharing, MDN, S/MIME |
| [Testing your own code](docs/testing.md) | The fake server that ships with the package |

## Status and versioning

**1.1.0** - stable, and following [Semantic Versioning](https://semver.org). See
the [changelog](CHANGELOG.md) for what changed.

- **Experimental capabilities are outside the SemVer promise.** Calendars and
  files track Internet-Drafts whose wire names can still change, so they are off
  unless you pass `experimental=True`.
- **Contact and calendar bodies are carried, not modelled.** A `ContactCard`
  (JSContact) or `CalendarEvent` (JSCalendar) round-trips losslessly and each
  field is readable by its wire name, for example
  `event.jscalendar("recurrenceRule")`. The JMAP layer around them is fully
  modelled.

## Contributing

```console
uv sync --all-extras
uv run pytest
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the full check suite CI runs, how to run
the integration tests against a live server, and the benchmarks.

## License

MIT - see [LICENSE](LICENSE).

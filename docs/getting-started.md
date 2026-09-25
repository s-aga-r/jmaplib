# Getting started

## Install

```console
pip install jmaplib
```

The distribution is `jmaplib`; the import is `jmap`.

```python
import jmap
```

Optional extras, none of which you need for mail:

| Extra | Pulls in | For |
|---|---|---|
| `jmaplib[discovery]` | `dnspython` | Finding a server from an email address via SRV records |
| `jmaplib[ws]` | `httpx-ws` | JMAP over WebSocket - requests and push on one connection; see [Push](push.md#websocket) |
| `jmaplib[push]` | `cryptography` | Decrypting Web Push payloads in your own push endpoint |
| `jmaplib[cli]` | `typer`, `rich` | Reserved for command-line tools; nothing needs it yet |

OAuth sign-in needs no extra: it uses only the standard library and httpx.

Python 3.11 or newer.

## Connect

```python
from jmap.auth import BasicAuth
from jmap.client import JMAPClient

client = JMAPClient.connect(
    "https://mail.example.com/.well-known/jmap",
    auth=BasicAuth("alice@example.com", "app-password"),
)
```

Point it at `/.well-known/jmap` rather than the API endpoint. That path is a
redirect to the real session URL, and following it is how the client learns
where everything else lives. Redirects are followed and the `Authorization`
header survives them.

Use it as a context manager so the connection pool closes:

```python
with JMAPClient.connect(url, auth=auth) as client:
    client.echo(hello="world")
```

### What `connect` did

One `GET`, which returned the **session object** - the server's description of
itself. From it the client took:

- the API, upload, download and event source URLs;
- the capabilities the server supports, per account;
- the accounts you can see, and which is primary for what;
- the limits: how many calls fit in a request, how large an upload may be, how
  many requests may be in flight.

Everything after that is decided from those values rather than assumed. Nothing
is fetched again until you ask - see [Capabilities](capabilities.md).

### Naming an account

Most requests are account-scoped. By default each call resolves its own account
from the session's `primaryAccounts`, which is what you want when the server
puts your mail, contacts and calendars in different accounts. To pin one
instead:

```python
client = JMAPClient.connect(url, auth=auth, account_id="u1234")
```

The full set of `connect` options:

| | |
|---|---|
| `auth` | Required. Any `httpx.Auth` - see [Authentication](auth.md). |
| `account_id` | Pin every call to one account instead of resolving per capability. |
| `experimental` | Opt into draft specifications. See [Capabilities](capabilities.md). |
| `registry` | Supply your own capability registry, to add or remove support. |
| `retry_policy` | See [Errors](errors.md). |
| `http` | Bring your own `httpx.Client` - for proxies, custom TLS, or a shared pool. |
| `timeout` | Seconds, default 30. Ignored when you pass your own `http`. |

If you have an email address rather than a URL, `discover` will find the server
from it - a `_jmap._tcp` SRV lookup first, which needs `jmaplib[discovery]`, then
`/.well-known/jmap` on the domain:

```python
client = JMAPClient.discover("alice@example.com", auth=auth)
```

An SRV record naming a host outside the address's domain is not tried unless
`confirm_srv_target` accepts it. Without DNSSEC the answer can be forged, and the
host it names would receive your credentials, so RFC 6186 §6 has the client ask
the user first. A custom domain at a hosted provider is the usual case; when
nothing else answers, `UnconfirmedSRVTargetError` names the host, ready to ask
about:

```python
client = JMAPClient.discover(
    "alice@example.org",
    auth=auth,
    confirm_srv_target=lambda target: ask_user(f"Is {target.host} your mail server?"),
)
```

`AsyncJMAPClient.discover` works the same way, awaited. The DNS lookup blocks, so
it runs on a worker thread - and `confirm_srv_target` is called there.

## Your first request

```python
with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)

for mailbox in mailboxes.result.items:
    print(mailbox.name, mailbox.role, mailbox.total_emails)
```

Three things are happening in those two lines.

**`ids=None` means "every record".** It is not the same as omitting the argument,
and not the same as `[]`. `None` is the JSON `null` that RFC 8620 defines as
"all"; omitting an argument leaves it out of the request entirely. The library
keeps those distinct everywhere.

**The result is readable after the block, not inside it.** `batch.mail.mailbox.get`
queues a call and hands back a handle immediately. The request is sent when the
`with` block exits, and `handle.result` is what you read afterwards. Reading it
early raises rather than returning something misleading.

**`mailbox` is a typed `Mailbox`,** not a dict - `mailbox.total_emails` is an
`int | None`, and your type checker knows it.

## Doing more than one thing

Queue as many calls as you like; they travel together:

```python
with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)
    identities = batch.submission.identity.get(ids=None)
    quota = batch.quota.quota.get(ids=None)

print(len(mailboxes.result.items), len(identities.result.items))
```

That is one HTTP request. See [Batching and references](batching.md) for how a
call can use an earlier call's results.

## Async

The async client mirrors the sync one - same names, same behaviour, `await` in
front. Both are shells over the same I/O-free core, so they cannot disagree
about protocol behaviour.

```python
from jmap.aio import AsyncJMAPClient

async with await AsyncJMAPClient.connect(url, auth=auth) as client:
    async with client.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)
    print(mailboxes.result.items)
```

Note where `await` goes: on `connect` (it makes a request) and on the `async
with`, but *not* on `batch.mail.mailbox.get`, which only queues.

## A single call without a batch

For one-offs there is `call`, which builds a one-call batch and returns the
parsed result directly:

```python
result = client.call("Mailbox/get", {"ids": None})
```

This is the escape hatch for methods the library does not model - including
vendor extensions. It skips the typed surface, not the local gates: `using` is
still derived, limits still apply, and an unadvertised capability still raises
before the wire.

## Where to go next

- [Capabilities](capabilities.md) - why `batch.mail` might not exist, and how to
  ask what a server supports.
- [Mail](mail.md) - the usual next stop.
- [Errors](errors.md) - what can go wrong and which parts you may retry.

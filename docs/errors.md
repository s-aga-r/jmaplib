# Errors

JMAP fails at four distinct levels, and conflating them is the most common source
of bad error handling in JMAP clients. The difference that matters is **blast
radius**: how much of what you asked for did not happen.

| Level | Type | What survived |
|---|---|---|
| Transport | `TransportError` | Nothing - no JMAP response at all |
| Request | `RequestError` | Nothing - the whole request was rejected, no method ran |
| Method | `MethodError` | Every other call in the batch |
| Set | `SetError` | Every other object in the same `/set` |

Only the first two are fatal to a batch. The last two are per-call and
per-object, and a client that raises on them throws away results it was given.

All of them descend from `JMAPError`, and so does everything else the server or
the network can do to a call - a body that is not JSON, a push event of the wrong
shape, a refused upload - along with every error class this library defines. One
`except JMAPError` catches them all.

What it lets through is misuse of the API itself: reading a handle before its
batch has run (`RuntimeError`), passing both `anchor` and `position`
(`ValueError`), an argument a builder's signature does not allow - `ids="m1"`,
`limit=-5` - which pydantic refuses before the call is queued (its
`ValidationError`, a `ValueError`; see [Batching](batching.md)), or a `bool`
where a JMAP `Int` goes in a raw `batch.add` (`TypeError`). Those are bugs to
fix rather than failures to handle, so they raise the builtin any Python code
would. The library's own argument errors - `InvalidIdError`, `InvalidPatchError`,
`InvalidEmailCreateError` and the rest - are both: `JMAPError`s, and the
`ValueError`s they always were.

## Transport

The request never produced a JMAP response - connection refused, TLS failure,
timeout, a proxy returning HTML.

```python
from jmap import TransportError

try:
    with client.batch() as batch:
        batch.mail.mailbox.get(ids=None)
except TransportError as error:
    ...
```

`AuthenticationError` is its sibling for a 401, carrying the `WWW-Authenticate`
challenges the server sent so you can tell "wrong password" from "token needs a
scope you do not have".

## Request level

An RFC 7807 problem document. **No method in the request ran**, so every call in
your batch is dead - including the unrelated ones.

```python
from jmap import RequestError

try:
    ...
except RequestError as error:
    print(error.type, error.status, error.detail)
```

The registered types are `unknownCapability`, `notJSON`, `notRequest` and
`limit`; `error.limit` names which limit for the last one. The library works
hard to avoid ever seeing these: `using` is derived and intersected precisely
because one unknown URN turns the whole request into `notRequest` on Stalwart,
killing every call batched alongside it.

Servers are not uniformly well behaved here. Stalwart answers an unparseable
`sinceState` at the request level, where the spec would suggest a method error.
If you feed states from storage, be ready for it.

## Method level

One call's response was replaced by an error; its siblings still ran. So it is
raised when you read *that* handle's result:

```python
from jmap import MethodError

with client.batch() as batch:
    good = batch.mail.mailbox.get(ids=None)
    risky = batch.mail.email.changes(since_state=old_state)

print(good.result.items)  # unaffected

try:
    risky.result
except MethodError as error:
    print(error.type, error.method_call_id)
```

Two error types come from the library rather than the server:
`malformedResult`, for a response it could not parse, and `missingResponse`, for a
call the server's response left out - RFC 8620 §3.4 has it answer every one.

Two subclasses are worth catching by name:

- **`ServerPartialFailError`** - RFC 8620 §3.6.2's `serverPartialFail`, the one
  method error after which server state *may* have changed. Never retry a
  request that produced one.
- **`ResyncRequiredError`** (from `jmap.sync`) - `cannotCalculateChanges`
  translated. The recovery is different from every other method error: discard
  your cursor and re-download. See [Staying in sync](sync.md).

## Set level

Not an exception at all - a value. `/set` routinely half-succeeds, and raising
would discard the objects that *did* change:

```python
with client.batch() as batch:
    result = batch.mail.email.set(update={"m1": {...}, "m2": {...}})

if result.result.has_errors:
    for key, error in result.result.update_errors.items():
        print(key, error.type, error.description, error.properties)
```

`SetError` carries `.type`, `.description`, `.properties` (which fields were
invalid) and `.existing_id` (for `alreadyExists`). If you would rather it raise,
`SetFailedError` wraps one.

## Caught before the wire

These never reach the server. Each is a round trip saved, but the real value is
the error message: it names the field and the advertised value, which the
server's own response would not.

| Situation | Error |
|---|---|
| Method no advertised capability provides | `UnsupportedMethodError` |
| Capability needed but not advertised | `CapabilityNotSupportedError` |
| A capability field forbids it (digest, lookup type, blob size…) | `CapabilityFieldError` |
| Mutation aimed at a read-only account | `ReadOnlyAccountError` |
| No `accountId` and none resolvable | `NoAccountError` |
| Requesting a property the server never returns | `CapabilityFieldError` |
| `/set` larger than `maxObjectsInSet` | `CapabilityFieldError` |
| Reference chain that cannot fit `maxCallsInRequest` | `BatchTooLargeError` |
| Back-reference nested inside a `create` | `NestedResultRefError` |
| Back-reference into a `/get` split across requests | `ChunkedReferenceError` |
| `anchor` and `position` together | `ValueError` |
| An invalid id, keyword, or patch pointer | `InvalidIdError`, `InvalidKeywordError`, `InvalidPatchError` |

## Retrying

JMAP has no idempotency key. "Retry on failure" is therefore not a policy - it
is a way to create duplicate drafts and send mail twice.

The rule the library follows: **retry only when the request provably never
reached application.** In practice that means connect and TLS errors, `429` and
`503` carrying `Retry-After`, and a request-level `limit`. A timeout is *not* in
that set, because the request went out and the server may well have applied it.

On top of that, a batch containing a mutating call is never retried
automatically unless every `/set` in it carried `if_in_state` - the state guard
is what makes a repeat safe, because the second attempt fails with
`stateMismatch` instead of doing the work twice.

```python
from jmap.core.retry import RetryPolicy

policy = RetryPolicy(max_attempts=5, initial_backoff=0.5, max_backoff=30.0, multiplier=2.0)
client = JMAPClient.connect(url, auth=auth, retry_policy=policy)
```

`Retry-After` from the server always wins over the computed backoff. A server
asking to be left alone for thirty seconds means it.

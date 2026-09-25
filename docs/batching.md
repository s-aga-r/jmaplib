# Batching and references

A JMAP request is a list of method calls. Batching is not an optimisation you
opt into - it is the shape of the protocol, and the reason JMAP avoids the round
trip between "which messages match?" and "give me those messages".

```python
with client.batch() as batch:
    query = batch.mail.email.query(filter={"inMailbox": inbox_id}, limit=20)
    emails = batch.mail.email.get(ids=query.ref_ids(), properties=["subject", "from"])

for email in emails.result.items:
    print(email.subject)
```

One HTTP request. The server runs `Email/query`, then feeds its `ids` straight
into `Email/get` without the client seeing them.

## Handles

Every queued call returns a **handle** immediately, long before anything is sent.
A handle is two things: a way to reference the call's results from a later call,
and a way to read them afterwards.

```python
with client.batch() as batch:
    handle = batch.mail.mailbox.get(ids=None)
    # handle.result here raises - nothing has been sent yet

handle.result.items  # readable now
```

The request goes out when the block exits. Reading `.result` too early raises
`RuntimeError` rather than returning an empty or misleading value.

## Arguments are checked

Every builder checks its arguments with pydantic before the call is queued, so a
mistake raises on the line that made it - rather than coming back from the
server as `invalidArguments`, or not at all:

```python
batch.mail.email.get(ids="m1")  # a string is not a list of ids
batch.mail.email.query(limit=-5)  # an UnsignedInt (RFC 8620 §5.5)
batch.mail.email.changes(since_state=None)  # a state is a string
```

Each raises pydantic's `ValidationError` - a `ValueError` - titled with the call
and listing every argument at fault:

```text
1 validation error for Email.get
ids
  'str' instances are not allowed as a Sequence value [type=sequence_str, ...]
```

The checks are strict: `"5"` is not a number and `1` is not a boolean, because
the wire would not coerce them either. `UNSET` and a back-reference go through
unchecked - RFC 8620 §3.7 lets any argument be a reference, and its value only
exists once the server resolves it. Calling a builder with the wrong arguments
at all, such as one it does not take positionally, is Python's own `TypeError`.

A `/set` or `/copy` `create` takes typed models as well as mappings. A model
sends the fields it was given and nothing else, so the server defaults the rest:

```python
from jmap.models.mail.objects import Mailbox

with client.batch() as batch:
    batch.mail.mailbox.set(create={"r": Mailbox(name="Receipts", parent_id=None)})
```

More generally, a model goes anywhere its wire object can, at any depth:
anything with a `to_wire()` method is serialised through it.

## Back-references

A back-reference points at a path inside an earlier call's response. The
handle's `ref_*` methods build them:

| Method | Points at | Typical use |
|---|---|---|
| `ref_ids()` | `/ids` | `Foo/query` → `Foo/get` |
| `ref_list(prop)` | `/list/*/<prop>` | fan out over a `/get` response |
| `ref_created(key, prop="id")` | `/created/<key>/<prop>` | read a server-assigned value off a `/set` |
| `ref_updated()` | `/updated` | changed ids |
| `ref_updated_properties()` | `/updatedProperties` | the cheap-property fast path |
| `ref(path)` | any pointer you like | anything else |

```python
with client.batch() as batch:
    changes = batch.mail.email.changes(since_state=state)
    updated = batch.mail.email.get(ids=changes.ref_updated())
```

The library checks locally that a reference points at an *earlier* call whose
method name matches, so a mistake fails before the wire rather than as
`invalidResultReference`.

### The rule that catches people

**A back-reference replaces a whole top-level argument.** It is expressed on the
wire by *renaming* the argument - `ids` becomes `#ids` - so there is nowhere to
put one that is nested inside another value. This does not work, and cannot:

```python
# WRONG - there is no way to encode this
create = {"s1": {"emailId": draft.ref_created("d1")}}
```

Doing it raises `NestedResultRefError` naming the argument. The draft `refplus`
extension exists precisely to lift this restriction; until a server implements
it, use a creation reference instead.

## Creation references

To point at an object being created *in the same request*, use its creation id.
This is an ordinary string on the wire (`#d1`), so it works at any depth:

```python
from jmap import CreationRef

with client.batch() as batch:
    batch.mail.email.set(create={"draft": {...}})
    batch.submission.email_submission.set(
        create={"send": {"emailId": CreationRef("draft"), "identityId": identity_id}}
    )
```

Creation ids are valid only within one request, and the server substitutes the
real id as it goes. When a batch is split across several requests (see below)
the library threads them through, so a creation id still resolves.

One caveat worth knowing: RFC 8620 §5.3 makes this substitution normative for
foreign keys inside a `/set`. Servers vary on plain method arguments - Stalwart
resolves `#id` in `Blob/get`'s `ids` but not in `SieveScript/validate`'s
`blobId`, where it answers `blobNotFound`. If a server rejects a creation
reference in an argument position, upload or create in an earlier request and
pass the real id.

## Reading results

Each response type has a small typed surface.

```python
got = batch.mail.email.get(ids=["m1"])
got.result.items  # list[Email]
got.result.not_found  # ids the server does not have
got.result.state  # the state string for this type
```

```python
found = batch.mail.email.query(filter={"hasKeyword": "$flagged"})
found.result.ids  # list[Id], in sort order
found.result.total  # only if you asked for calculate_total
found.result.position
found.result.can_calculate_changes  # whether queryChanges will work later
```

```python
changed = batch.mail.email.set(create={"e1": {...}}, destroy=["m9"])
changed.result.created_id("e1")  # the server-assigned id
changed.result.has_errors
changed.result.creation_errors  # {key: SetError}
changed.result.new_state
```

## `/set` half-succeeds by design

This is the single most important thing to know about `/set`. It is not
all-or-nothing: the server applies what it can and reports the rest, so an
exception would throw away the objects that *did* change. Failures arrive as
values:

```python
with client.batch() as batch:
    result = batch.mail.email.set(destroy=["m1", "m2", "m3"])

if result.result.has_errors:
    for key, error in result.result.destroy_errors.items():
        print(key, error.type, error.description)
```

A `SetError` has `.type`, `.description`, and for the cases that define them
`.properties` (which fields were invalid) and `.existing_id` (for
`alreadyExists`).

If you want atomicity, ask for it with `if_in_state`. The server then rejects
the whole call with `stateMismatch` if anything changed since:

```python
batch.mail.email.set(update={"m1": {"keywords/$seen": True}}, if_in_state=state)
```

## One failed call does not sink its siblings

A method-level error replaces one call's response; the others still ran. So the
error is raised when you read *that* call's result, not when the response
arrives:

```python
with client.batch() as batch:
    good = batch.mail.mailbox.get(ids=None)
    bad = batch.mail.email.changes(since_state="nonsense")

print(good.result.items)  # fine

from jmap import MethodError

try:
    bad.result
except MethodError as error:
    print(error.type)  # e.g. "cannotCalculateChanges"
```

## The raw path

Most methods have one of six shapes, which is why their builders can be
generated. The rest have hand-written ones - `Blob/upload`, `Email/import`,
`SearchSnippet/get`, `SieveScript/validate`, `MDN/send`, `CalendarEvent/parse`
and the others - so every modelled method has a builder except `Core/echo`, which
has `client.echo(**arguments)`.

Underneath them all is `batch.add`, which takes the method name and
wire-spelled arguments. It is not a lesser path: it resolves the account,
derives `using`, enforces the read-only and limit gates, and returns a normal
handle you can reference from other calls. What you give up is the typed
response - the result is the raw response dict rather than a parsed model - and
the argument checks, since there is no signature to check against; the
arguments go out as given:

```python
with client.batch() as batch:
    parsed = batch.add("Email/parse", {"blobIds": [blob_id], "properties": ["subject"]})

print(parsed.result["parsed"])
```

It is also how you reach a capability this build does not model at all. Anything
in `client.capabilities.unknown_urns` is callable this way, with `extra_using`
to declare it.

## Limits and splitting

Servers cap how many calls fit in one request (`maxCallsInRequest`), how many
ids a `/get` may name, and how large a request may be. The library reads those
from the session and plans around them:

- **`/get` with too many ids** is split automatically, and the responses are
  recombined. The `state` of each chunk is compared, so a mutation racing your
  read is detected rather than silently producing a mixed answer. Each id goes
  into one chunk however often you name it - otherwise a repeat would come back
  once per chunk holding it - and a tuple of ids splits as a list does.
- **A batch with too many calls** is split into several requests, cut only
  between consecutive calls - so they still run in the order you queued them -
  and never between a call and one it references. If no such cut fits under the
  limit, `BatchTooLargeError` names the stretch of calls that has to travel
  together, rather than sending something that will fail or quietly running
  your calls in a different order.
- **A `/set` over `maxObjectsInSet` raises** instead of being split, because
  splitting it would break the single `if_in_state` that makes it atomic. That
  is a decision you have to make, not one the library can make for you.

## Choosing the account

Every account-scoped call resolves its own `accountId`: an explicit
`account_id=` argument, else the client's default, else the `primaryAccounts`
entry for the capability that owns the method.

```python
with client.batch() as batch:
    batch.mail.email.get(ids=None, accountId="shared-account-id")
```

Note the spelling. Named arguments are snake_case and get translated
(`since_state` → `sinceState`); anything you pass as an extra keyword goes
through **verbatim**, so it must be the wire name. That is what makes extras
useful for arguments a capability adds without this library modelling them:

```python
batch.mail.email.query(filter={"inMailbox": inbox_id}, collapseThreads=True)
```

When none of those produce an answer, `NoAccountError` says so locally. The
library deliberately does not guess "the first account" - on a server where you
can see a shared calendar, guessing sends `Email/*` at an account with no mail.

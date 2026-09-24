# Staying in sync

JMAP is built so a client never has to re-download what it already has. Every
type carries a **state string**, and the server can tell you what changed since
one. This page covers following those changes, and keeping a query result
current.

None of it needs push. Push tells you *when* to ask; this is the asking, and it
works perfectly well on a timer.

## Following changes to a type

`ChangeStream` holds a cursor for one data type and walks it forward.

```python
from jmap.sync import ChangeStream

stream = ChangeStream(client, "Email")
stream.seed(state)  # a state string you kept from an earlier response

changes = stream.catch_up()
print(changes.created, changes.updated, changes.destroyed)
print(changes.new_state)  # keep this for next time
```

`catch_up` pages until the server says there is no more, because `/changes` is
allowed to answer in instalments - `has_more_changes` means exactly that, and
reading one page and stopping loses everything after it. If you would rather
handle pages yourself:

```python
for page in stream.pages(max_changes=100):
    process(page.created, page.updated, page.destroyed)
```

The two differ in when the stored cursor moves. `pages()` moves it only when you
ask for the next page, so a page you were processing when the process died
arrives again. `catch_up()` moves it once, when it returns the whole set: a
failure part-way through leaves it untouched, but a crash while you are applying
the result will not deliver it again. Use `pages()` when that matters.

A `ChangeSet` has `.created`, `.updated`, `.destroyed`, `.new_state`, `.pages`,
and two conveniences: `.is_empty` and `.touched` (created plus updated - the ids
worth re-fetching).

The usual shape is changes then a fetch, in one request. Created and updated ids
arrive in separate arrays and a back-reference names one path, so it takes two
`/get`s - still one round trip:

```python
with client.batch() as batch:
    changed = batch.mail.email.changes(since_state=state)
    created = batch.mail.email.get(ids=changed.ref("/created"), properties=["subject"])
    updated = batch.mail.email.get(ids=changed.ref_updated(), properties=["subject"])
```

Fetching only `ref_updated()` is the easy mistake: every new message is in
`created` and never arrives. `changed.result.destroyed` needs no fetch at all.

### When the cursor dies

A server may answer `cannotCalculateChanges` - because the state is too old, or
because it never tracked that far back. This is a **normal outcome**, not a bug,
and the only recovery is to re-download and start again. The library raises a
distinct error so you can tell it apart from every other method error:

```python
from jmap.sync import ResyncRequiredError

try:
    changes = stream.catch_up()
except ResyncRequiredError:
    state = resynchronise_from_scratch()  # a fresh /get, returning its `state`
    stream.seed(state)
```

Seed the stream with the state of the download you just did. `reset()` would
leave it with no cursor at all, so the next `catch_up()` would demand another
full download.

Not every server is well behaved here. Stalwart rejects an unparseable
`sinceState` at the *request* level rather than the method level, which arrives
as `RequestError`. Catch both if you feed it states from untrusted storage.

## Remembering where you were

`StateStore` is the seam for persistence - a two-method protocol, deliberately
small, so you can put states in whatever you already use:

```python
from jmap.sync import InMemoryStateStore

store = InMemoryStateStore()
stream = ChangeStream(client, "Email", store=store)
...
saved = store.snapshot()  # dict[str, str] - persist this
```

Implement `get(key)`, `set(key, state)` and `delete(key)` against your own
database to survive restarts. The library holds no cache of its own and never
writes to disk; what you keep is your decision.

Keys are `<accountId>/<TypeName>`, and an account id is unique only on its own
server. If one store serves several servers, give each a `namespace` - any label
without a `/` - or two accounts both called `a` share a cursor, and each server is
handed the other's state:

```python
work = ChangeStream(work_client, "Email", store=store, namespace="work")
home = ChangeStream(home_client, "Email", store=store, namespace="home")
```

## Keeping a query current

Re-running `Email/query` after every change is wasteful and, on a large mailbox,
slow. `Foo/queryChanges` sends the *delta* to a result set instead, and
`QueryView` applies it.

```python
from jmap.sync import QuerySpec, QueryView

spec = QuerySpec.build("Email", account_id, filter=query_filter, sort=sort)
view = QueryView(spec)

with client.batch() as batch:
    initial = batch.mail.email.query(filter=query_filter, sort=sort, calculate_total=True)
view.reset(initial.result)

# later
with client.batch() as batch:
    delta = batch.mail.email.query_changes(
        since_query_state=view.query_state, filter=query_filter, sort=sort
    )
view.apply(delta.result)

print(view.known_ids)
```

Three constraints, because getting any of them wrong corrupts the view silently.
The type enforces the last two; the first is yours:

- **The filter and sort must be identical** to the ones the state came from.
  `QuerySpec` hashes them into a key, so each query keeps its own view and
  cursor - but a `/queryChanges` response does not repeat them, and a server may
  share one `queryState` between queries, so the view cannot tell a delta for
  another query from its own. Ask with the filter and sort the spec was built
  from. What it does catch is a delta against another *state*: that raises
  `StaleQueryViewError`.
- **Not every query supports this.** `can_calculate_changes` on the original
  response says whether the server will answer at all; a query that does not
  raises `UncacheableQueryError`.
- **A view is sparse.** You may be holding a window rather than the whole result
  set, so `known_ids` reports what you actually know and `up_to_id` marks how
  far it is trustworthy.

## What push adds

Push is a latency optimisation over exactly this loop, never a second source of
truth. A `StateChange` notification carries new state strings; you compare them
against what you hold and run the same `/changes` call. Notifications may be
coalesced, delayed or dropped entirely, and the client still converges - which
is why the library never applies a notification as data. See [Push](push.md).

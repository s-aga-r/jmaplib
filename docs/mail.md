# Mail

RFC 8621, complete: `Mailbox`, `Thread`, `Email`, `SearchSnippet`, `Identity`,
`EmailSubmission` and `VacationResponse`.

## Mailboxes

```python
with client.batch() as batch:
    mailboxes = batch.mail.mailbox.get(ids=None)

by_role = {m.role: m for m in mailboxes.result.items if m.role}
inbox = by_role["inbox"]
print(inbox.name, inbox.total_emails, inbox.unread_emails)
```

Find one by role rather than by name - `role` is the machine-readable identity
(`inbox`, `drafts`, `sent`, `trash`, `junk`, `archive`) and survives the user
renaming their folders or running a localised client.

Mailboxes form a tree through `parent_id`, with `None` meaning top level.
Creating one:

```python
with client.batch() as batch:
    created = batch.mail.mailbox.set(create={"m1": {"name": "Receipts", "parentId": inbox.id}})
mailbox_id = created.result.created_id("m1")
```

`my_rights` carries what you may do with a shared mailbox - `may_add_items`,
`may_delete`, `may_submit` and the rest. Check it before offering an action the
server will refuse.

## Searching

```python
with client.batch() as batch:
    found = batch.mail.email.query(
        filter={
            "operator": "AND",
            "conditions": [
                {"inMailbox": inbox.id},
                {"hasKeyword": "$flagged"},
                {"after": "2026-01-01T00:00:00Z"},
            ],
        },
        sort=[{"property": "receivedAt", "isAscending": False}],
        limit=50,
        calculate_total=True,
    )
    emails = batch.mail.email.get(
        ids=found.ref_ids(), properties=["subject", "from", "receivedAt", "keywords"]
    )

print(found.result.total, "matches")
for email in emails.result.items:
    print(email.received_at, email.subject)
```

**Ask for the properties you need.** A bare `Email/get` returns a large object
including body structure; naming properties keeps the response small. The
library folds the property names into `using` derivation, so a property that a
capability adds pulls that capability in automatically.

Sorting is checked against the server's advertised `emailQuerySortOptions`,
and a comparator's `collation` against `collationAlgorithms`, before the query
goes out: either would fail the whole query with `unsupportedSort`, so each
raises `CapabilityFieldError` here instead.

### Paginating

Use `position` for a page number, or `anchor` to page relative to a known id.
They are mutually exclusive - RFC 8620 §5.5 makes `position` ignored when an
`anchor` is present - and passing both raises locally rather than silently
dropping one.

```python
batch.mail.email.query(filter=..., position=50, limit=25)
batch.mail.email.query(filter=..., anchor=last_id, anchor_offset=1, limit=25)
```

For a result set you intend to keep current, see [Staying in sync](sync.md).

### Search snippets

`SearchSnippet/get` shows why each result matched: its subject and preview with
the matching terms marked. Pass the query's own filter and the ids it found, in
the same request:

```python
search = {"text": "invoice"}

with client.batch() as batch:
    found = batch.mail.email.query(filter=search, limit=20)
    snippets = batch.mail.search_snippet.get(filter=search, email_ids=found.ref_ids())

for snippet in snippets.result.items:
    print(snippet.email_id, snippet.subject, snippet.preview)
```

Both are HTML - `&`, `<` and `>` escaped, each match wrapped in `<mark>` - and
either is `None` when nothing in it matched. `snippets.result.snippet_of(email_id)`
finds one email's snippet.

## Reading a message

Bodies arrive separately from structure. Ask for `bodyValues` with a fetch flag:

```python
with client.batch() as batch:
    got = batch.mail.email.get(
        ids=[email_id],
        properties=["subject", "from", "to", "textBody", "bodyValues"],
        fetchTextBodyValues=True,
    )

email = got.result.items[0]
for part in email.text_body or []:
    value = (email.body_values or {}).get(part.part_id)
    if value:
        print(value.value)
```

`fetchTextBodyValues`, `fetchHTMLBodyValues`, `fetchAllBodyValues` and
`maxBodyValueBytes` are RFC 8621 arguments passed through as extras, so they
keep their wire spelling.

### `from` is a Python keyword

The field is `from_` on the model and `from` on the wire:

```python
sender = email.from_[0] if email.from_ else None
print(sender.email, sender.name)
```

### Headers

Arbitrary headers are addressable by exact wire name, including the form
suffix. `HeaderQuery` builds those names so you do not have to:

```python
from jmap.models.mail.headers import HeaderQuery, HeaderForm

query = HeaderQuery("List-Id", form=HeaderForm.TEXT)
print(query.property_name)  # header:List-Id:asText

with client.batch() as batch:
    got = batch.mail.email.get(ids=[email_id], properties=[query.property_name])
```

The form matters: `asText`, `asAddresses`, `asMessageIds`, `asDate`, `asURLs`
and the default `asRaw` are parsed differently by the server, and asking for the
wrong one gives you a value you then have to parse yourself.

Two more details are easy to miss. The server answers under *exactly* the name
you asked with - `header:Subject` and `header:subject` are different keys - so
ask and read with the same query: `query.read(email)` does the lookup. And
without `all=True` you get the **last** occurrence of a header, not the first
and not a list. The shorthands build the common queries:

```python
from jmap.models.mail.headers import addresses, raw, text

subject = text("Subject")  # header:Subject:asText
hops = raw("Received", all=True)  # header:Received:all - every hop
recipients = addresses("To")  # header:To:asAddresses
```

## Changing a message

Updates are **patches**, addressed by JSON Pointer, not whole objects. This is
what makes concurrent edits safe: two clients setting different keywords do not
overwrite each other.

```python
with client.batch() as batch:
    batch.mail.email.set(
        update={
            email_id: {
                "keywords/$seen": True,
                "keywords/$flagged": None,  # None removes it
                f"mailboxIds/{archive_id}": True,
                f"mailboxIds/{inbox_id}": None,  # that pair is a move
            }
        }
    )
```

`None` deletes the pointed-at member; `True` adds it. Moving a message between
mailboxes is one add and one remove in the same patch.

For the two most common patches there are helpers that get the escaping right:

```python
from jmap.core.patch import keyword_patch, mailbox_patch

patch = keyword_patch(add=["$seen"], remove=["$flagged"])
patch.update(mailbox_patch(add=[archive_id], remove=[inbox_id]))

with client.batch() as batch:
    batch.mail.email.set(update={email_id: patch})
```

Keywords are lowercase by convention and case-sensitive in JMAP - `$seen`, not
`$Seen`. The library validates them, because a mistyped keyword is not an error
on the wire, it is a *different* keyword that silently does nothing.

## Composing and sending

Sending is two objects: the `Email` (the draft) and the `EmailSubmission` (the
instruction to send it). Both fit in one request.

```python
from jmap import CreationRef

with client.batch() as batch:
    identities = batch.submission.identity.get(ids=None)

identity = next(i for i in identities.result.items if i.email == "alice@example.com")

with client.batch() as batch:
    batch.mail.email.set(
        create={
            "draft": {
                "mailboxIds": {drafts_id: True},
                "keywords": {"$draft": True},
                "from": [{"email": "alice@example.com", "name": "Alice"}],
                "to": [{"email": "bob@example.com"}],
                "subject": "Lunch?",
                "textBody": [{"partId": "t", "type": "text/plain"}],
                "bodyValues": {"t": {"value": "One o'clock?"}},
            }
        }
    )
    sent = batch.submission.email_submission.set(
        create={"send": {"identityId": identity.id, "emailId": CreationRef("draft")}},
        onSuccessUpdateEmail={"#send": {"keywords/$draft": None, "keywords/$seen": True}},
    )

assert not sent.result.has_errors, sent.result.creation_errors
```

Three things worth pointing out:

- **`CreationRef("draft")`, not a back-reference.** `emailId` sits inside a
  create object, and a back-reference cannot go there. See
  [Batching](batching.md).
- **`onSuccessUpdateEmail`** is how the draft stops being a draft. The server
  applies that patch only if the submission succeeds, so you never end up with a
  message that left the drafts folder without being sent. `#send` refers to the
  submission's creation id.
- **Check `has_errors`.** A submission that fails does so as a value.

Delivery is asynchronous. `undo_status` on the submission tells you where it
got to (`pending`, `final`, `canceled`), and `delivery_status` carries
per-recipient results once the server has them.

### Delayed send

If the server advertises a non-zero `maxDelayedSend`, a submission can ask to be
held with the SMTP `HOLDFOR` or `HOLDUNTIL` parameter on `mailFrom` (RFC 4865,
FUTURERELEASE), for up to that many seconds; the server reports the release time
as the submission's `sendAt`. The delay is not checked against `maxDelayedSend`
locally - a longer one is the server's to refuse. `SubmissionCapability` reads the
limit, and `supports("FUTURERELEASE")` whether holding is offered at all:

```python
batch.submission.email_submission.set(
    create={
        "s1": {
            "identityId": identity.id,
            "emailId": email_id,
            "envelope": {
                "mailFrom": {"email": "alice@example.com", "parameters": {"HOLDFOR": "3600"}},
                "rcptTo": [{"email": "bob@example.com"}],
            },
        }
    }
)
```

### Checking a draft first

`Email/set` is the one place the object you send is not shaped like the one you
get back, and the server refuses a malformed draft with `invalidProperties`,
which names the property but not the rule. `validate_email_create` checks the
rules locally:

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
read-only `headers` list, a body described both ways at once, empty
`mailboxIds`, malformed keywords, body parts with both or neither of
`partId`/`blobId`, and `bodyValues` entries that are unreferenced or missing. It
is deliberately a subset of RFC 8621 §4.6: anything that needs server state -
does the mailbox exist, is the blob still there - is left to the server.

## Importing and parsing

`Email/import` files a blob you have already uploaded as a message;
`Email/parse` reads a blob as a message *without* storing it, which is how you
inspect an attached `.eml`.

```python
from jmap.models.mail.irregular import EmailImport

uploaded = client.upload(eml_bytes, content_type="message/rfc822")

with client.batch() as batch:
    imported = batch.mail.email.import_(
        emails={"k1": EmailImport(blob_id=uploaded.blob_id, mailbox_ids={inbox_id: True})}
    )

print(imported.result.created_id("k1"))
```

The trailing underscore is because `import` is a Python keyword. The call
half-succeeds like a `/set`, so a failure is a value in
`imported.result.creation_errors` - `alreadyExists`, carrying the existing id,
when the message is already in the account. Pass `if_in_state` to make a retry
safe.

```python
with client.batch() as batch:
    parsed = batch.mail.email.parse(blob_ids=[blob_id], properties=["subject", "from"])

email = parsed.result.email_of(blob_id)
```

Each blob yields one email, stored nowhere - so it has no `id` or `mailbox_ids`.
Blobs that are not messages are listed in `not_parsable`. See [Blobs](blobs.md)
for getting the blob there in the first place.

## Threads

A thread is derived, not stored - so it has `/get` and `/changes` and nothing
else. `batch.mail.thread.query` does not exist, because RFC 8621 does not define
it.

```python
with client.batch() as batch:
    threads = batch.mail.thread.get(ids=[thread_id])
print(threads.result.items[0].email_ids)
```

## Vacation responses

A singleton: one object per account, with the fixed id `singleton`.

```python
with client.batch() as batch:
    batch.vacation.vacation_response.set(
        update={
            "singleton": {
                "isEnabled": True,
                "fromDate": "2026-08-01T00:00:00Z",
                "toDate": "2026-08-15T00:00:00Z",
                "subject": "Away",
                "textBody": "Back on the 15th.",
            }
        }
    )
```

There is no `create` and no `destroy` - the object always exists.

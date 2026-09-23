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

Sorting is *not* checked against the server's advertised
`emailQuerySortOptions`: an unsupported comparator comes back as an
`unsupportedSort` method error. The list is on the mail capability if you want
to check first.

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
locally - a longer one is the server's to refuse:

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

## Importing and parsing

`Email/import` takes a blob you have already uploaded and files it as a message;
`Email/parse` reads a blob as a message *without* storing it, which is how you
inspect an attached `.eml`.

Neither has a typed builder yet, so they go through `batch.add` with wire-spelled
arguments - everything else still applies, including `using` derivation and the
local gates:

```python
with client.batch() as batch:
    parsed = batch.add(
        "Email/parse",
        {
            "blobIds": [blob_id],
            "properties": ["subject", "from"],
        },
    )

print(parsed.result["parsed"])
```

See [Batching](batching.md#methods-without-a-builder) for the full list, and
[Blobs](blobs.md) for getting the blob there in the first place.

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

# The other capabilities

Everything the library models beyond mail and blobs. Each is gated on the server
advertising it, so check first - see [Capabilities](capabilities.md).

## Quota (RFC 9425)

How much of the user's allowance is gone.

```python
with client.batch() as batch:
    quotas = batch.quota.quota.get(ids=None)

for quota in quotas.result.items:
    print(quota.resource_type, quota.scope, quota.used, quota.hard_limit)
```

`resource_type` is `count` or `octets` - a quota of 500 means 500 *messages* or
500 *bytes* depending on it, so arithmetic without checking is arithmetic on an
unknown unit. `scope` is `account`, `domain` or `global`.

Quota moves constantly, which is why `Quota/changes` has a `updatedProperties`
fast path: the server can say "only `used` moved" and let you fetch one field.
**A `null` there does not mean nothing changed** - RFC 9425 §4.3 makes it "fetch
everything", the opposite of what the empty reading suggests. The library exposes
that as `fetch_all_properties` so the ambiguity cannot be misread.

## Sieve (RFC 9661)

Server-side filtering scripts.

```python
with client.batch() as batch:
    scripts = batch.sieve.sieve_script.get(ids=None)

active = [s for s in scripts.result.items if s.is_active]
```

At most one script is active at a time. Uploading and activating:

```python
from jmap.models.blob import BlobUpload, DataSource

with client.batch() as batch:
    uploaded = batch.blob.blob.upload(
        create={"s": BlobUpload(data=[DataSource.text(source)], type="application/sieve")}
    )
blob_id = uploaded.result.created_id("s")

with client.batch() as batch:
    checked = batch.sieve.sieve_script.validate(blob_id=blob_id)

if checked.result.is_valid:
    with client.batch() as batch:
        created = batch.sieve.sieve_script.set(
            create={"s1": {"name": "my-filter", "blobId": blob_id}}
        )
        batch.sieve.sieve_script.activate("#s1")
```

Three things:

- **`validate` is a verdict, not an error.** Invalid content comes back as a
  successful method call with a non-null `error` argument, so nothing raises.
  Read `is_valid` and `problem`.
- **Upload in a separate request.** `SieveScript/validate` takes a `blobId`, and
  servers differ on whether a `#creationId` resolves in that position - Stalwart
  answers `blobNotFound`. See [Blobs](blobs.md).
- **`activate`/`deactivate` are ordering-sensitive** and the library handles the
  `onSuccessActivateScript` shape for you. Check the engine's `sieve_extensions`
  before relying on `fileinto`, `vacation` or anything else optional.

## Contacts (RFC 9610)

Address books and cards, in the JSContact vocabulary.

```python
with client.batch() as batch:
    books = batch.contacts.address_book.get(ids=None)
    cards = batch.contacts.contact_card.query(filter={"inAddressBook": book_id}, limit=50)
```

Two vocabularies exist in the wild. RFC 9610's `ContactCard` is the standard;
Fastmail and Cyrus predate it and expose a `Contact`/`ContactGroup` model behind
their own vendor URNs. The library models both and picks whichever the server
advertises - `batch.contacts` for the standard one, `batch.fastmail_contacts` or
`batch.cyrus_contacts` for the legacy pair.

**JSContact bodies are carried, not modelled.** A `ContactCard` round-trips
losslessly and is readable by exact wire name, but there are no field-by-field
Python models for the JSContact vocabulary itself. What *is* modelled is the JMAP
layer around it, which is where the traps live.

**Parsing vCards server-side** is a Stalwart extension, behind
`urn:ietf:params:jmap:contacts:parse` - an IETF-spelled URN that no RFC defines;
RFC 9610 has no `/parse` at all. Upload the vCard as a blob, then
`batch.add("ContactCard/parse", {"blobIds": [blob_id]})`. Each blob parses to
**one** Card - not an array, which is what the calendars `/parse` returns - and
the per-call blob cap is server configuration advertised nowhere, so an oversized
call answers `requestTooLarge`; halve the batch and retry.

## Calendars (draft, experimental)

Tracks `draft-ietf-jmap-calendars`. Needs `experimental=True` at connect, and is
excluded from the SemVer promise.

```python
with client.batch() as batch:
    calendars = batch.calendars.calendar.get(ids=None)
    events = batch.calendars.calendar_event.query(
        filter={"after": "2026-08-01T00:00:00Z", "before": "2026-09-01T00:00:00Z"},
        expandRecurrences=True,
    )
```

Expanding recurrences is bounded by the server's `maxExpandedQueryDuration`, and
exceeding it earns `expandDurationTooLarge`. The library checks the window
against the advertised duration first, so you can chunk deliberately instead of
discovering the limit from an error.

As with contacts, JSCalendar bodies are carried through rather than modelled
field by field.

## Files (draft, experimental)

Tracks `draft-ietf-jmap-filenode`. Also behind `experimental=True`.

```python
with client.batch() as batch:
    nodes = batch.files.file_node.query(filter={"parentId": folder_id})
```

A `FileNode` is a discriminated union on `nodeType` - a folder has no blob, a
file does - and the model makes the illegal combinations unconstructible.
Names are checked against `forbiddenNameChars` and `forbiddenNodeNames` (which
includes `.`, `..` and the Windows device names) and depth against
`maxFileNodeDepth`, all before the request.

## Sharing and principals (RFC 9670)

Who else exists, and what they may see.

```python
with client.batch() as batch:
    principals = batch.principals.principal.query(filter={"name": "team"})
```

Sharing itself is a set of helpers over the `shareWith` map, because editing that
map by hand is easy to get subtly wrong:

```python
from jmap.sharing import grant, revoke, may, held, me

patch = grant(principal_id, {"mayRead": True, "mayWrite": True})
with client.batch() as batch:
    batch.mail.mailbox.set(update={mailbox_id: patch})
```

Each returns a **patch**, so it drops straight into a `/set` update and touches
only the one principal - which is the point, since rewriting the whole
`shareWith` map is how you revoke someone else's access by accident.

```python
revoke(principal_id)  # patch removing them entirely
may(rights, "mayRead", "mayWrite")  # True only if every named right is held
held(rights)  # {"mayRead", "mayWrite"} - what they have
me(client.session, principal_account_id)  # your own principal id, or None
```

`may` takes the question rather than making you index a dict that may not have
the key, and treats a missing right as `False` rather than raising.

`grant` refuses to put the **owner** in the share map. Servers reject that, and
the error you get back does not explain why - so it is checked here, with
`owner_principal_id` if you need to name the owner explicitly.

## Stalwart management (vendor)

Stalwart's admin API is a JMAP dialect behind `urn:stalwart:jmap` - v0.16
removed the `/api/*` REST surface. Method names carry an `x:` prefix and only
three shapes exist per object: `get`, `set`, `query`. There is no `/changes`.

The URN is advertised at **account level only**, never in the session-level
map - the capability resolves through the registry's union rule, and appears
only on a connection authenticated as an account that holds the management
permission.

The `x:` names are not attribute material, so calls go through `batch.add` like
the other builder-less methods - with account resolution, `using` derivation and
the read-only and limit gates all applying as usual:

```python
with client.batch() as batch:
    minted = batch.add(
        "x:AppPassword/set",
        {"create": {"p1": {"description": "imap-client"}}},
    )

secret = minted.result.created["p1"]["secret"]  # shown at creation only
```

The object inventory is the server's registry schema - ~150 types, served at
`/api/schema` and grown per release - so the default spec declares a verified
subset (accounts, app passwords, domains and DKIM, groups, mailing lists,
roles, OAuth clients, the mail queue, logs, actions, DMARC/TLS/ARF reports,
bootstrap, listeners). For anything else, build your own inventory with
`jmap.capabilities.stalwart.management_spec` and register it in a custom
registry.

Three server behaviours worth knowing: `x:Log` is read-only and pages only by
`anchor` - offset paging is refused. `x:Action` is *run* by a `set` whose
created object carries the result. And `x:Bootstrap` is a singleton (id
`"singleton"`), whose `/query` the server rejects - so the spec does not offer
it.

## Read receipts (RFC 9007)

```python
from jmap.models.mdn import Disposition, ACTION_MANUAL, SENDING_MANUAL, TYPE_DISPLAYED

with client.batch() as batch:
    batch.mdn.mdn.send(
        identity_id=identity_id,
        send={
            "r1": {
                "forEmailId": email_id,
                "subject": "Read receipt",
                "disposition": Disposition(
                    action_mode=ACTION_MANUAL, sending_mode=SENDING_MANUAL, type=TYPE_DISPLAYED
                ).to_wire(),
            }
        },
    )
```

The `$mdnsent` keyword patch is added for you. RFC 9007 §2.1 makes the server
*check* that an `MDN/send` also sets it, so omitting it is not "send without
bookkeeping", it is "send nothing". The spelling is `$mdnsent`, lowercase -
`$MDNSent` is a different keyword and does nothing.

Before sending one, check the message is not already acknowledged:

```python
from jmap.models.mdn import already_sent

if not already_sent(email.keywords):
    ...
```

## S/MIME verification (RFC 9219)

A capability that adds **properties, no methods**. The server tells you whether
it verified a signature; there is nothing to call.

```python
with client.batch() as batch:
    got = batch.mail.email.get(ids=[email_id], properties=["smimeStatus", "smimeStatusAtDelivery"])
```

Filtering works too - `hasSmime`, `hasVerifiedSmime`, `hasVerifiedSmimeAtDelivery`.

`smimeStatusAtDelivery` is the one that matters for trust: it records what the
server saw *when the message arrived*, before certificates expired or were
revoked. The live value can legitimately differ later.

Because this capability adds no methods, forgetting it in `using` costs you the
properties with no error at all - which is exactly why the library derives
`using` from the properties in your batch, not just its methods.

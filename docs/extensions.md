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

Five things:

- **`validate` is a verdict, not an error.** Invalid content comes back as a
  successful method call with a non-null `error` argument, so nothing raises.
  Read `is_valid` and `problem`.
- **Upload in a separate request.** `SieveScript/validate` takes a `blobId`, and
  servers differ on whether a `#creationId` resolves in that position - Stalwart
  answers `blobNotFound`. See [Blobs](blobs.md).
- **`activate`/`deactivate` are ordering-sensitive** and the library handles the
  `onSuccessActivateScript` shape for you. Destroying the active script takes
  two `/set` calls - RFC 9661 §2.4 requires the deactivation to be separate - so
  batch them rather than combining them. Check the engine's `sieve_extensions`
  before relying on `fileinto`, `vacation` or anything else optional.
- **Script names are measured in octets.** `maxSizeScriptName` counts the UTF-8
  encoding, so a four-character CJK name is twelve. `sieve_script.set()` checks
  every name against the account's limits before the server can refuse it, and
  `check_name()` checks one on its own - as a user types it, say.
- **There is no `SieveScript/changes`.** RFC 9661 defines none, so when push says
  scripts changed, fetch them again with `/get` - cheap for a handful of scripts.

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

**Cards are typed.** A `ContactCard` is RFC 9553's Card, modelled in
`jmap.models.jscontact`, plus `id` and `addressBookIds`. Its parts read as
attributes, and the same models build a card to create:

```python
from jmap.models.contacts import ContactCard
from jmap.models.jscontact import EmailAddress, Name, NameComponent

card = ContactCard(
    address_book_ids={book_id: True},
    name=Name(components=[NameComponent(kind="given", value="Ada")]),
    emails={"e1": EmailAddress(address="ada@example.com", contexts={"work": True})},
)
with client.batch() as batch:
    created = batch.contacts.contact_card.set(create={"c1": card})
```

The maps - `emails`, `phones`, `addresses` and the rest - are keyed by ids local
to the card, which survive edits and are what a patch names
(`emails/e1/address`). Two properties have other names in Python: `members` is
`member_uids` and `media` is `media_resources`, since `card.members()` and
`card.media()` already read them. `@type` and `version` are left for the server
to fill in.

The models are forgiving. A property whose value does not fit its type is kept
exactly as the server sent it, and reads `None` as an attribute: one odd value
never costs the card, and a card always round-trips unchanged.
`card.jscontact("name")` reads any property by its wire name, in wire form,
modelled or not - a vendor property included.

**Parsing vCards server-side** is a Stalwart extension, behind
`urn:ietf:params:jmap:contacts:parse` - an IETF-spelled URN that no RFC defines;
RFC 9610 has no `/parse` at all. Upload the vCard as a blob, then
`batch.contacts.contact_card.parse(blob_ids=[blob_id])` - the method appears on
`contact_card` only when the server advertises that URN. Each blob parses to
**one** Card, read with `result.card_of(blob_id)` - not an array, which is what
the calendars `/parse` returns - and the per-call blob cap is server
configuration advertised nowhere, so an oversized call answers
`requestTooLarge`; halve the batch and retry.

## Calendars (draft, experimental)

Tracks `draft-ietf-jmap-calendars`. Needs `experimental=True` at connect, and is
excluded from the SemVer promise.

```python
with client.batch() as batch:
    calendars = batch.calendars.calendar.get(ids=None)
    events = batch.calendars.calendar_event.query(
        filter={"after": "2026-08-01T00:00:00", "before": "2026-09-01T00:00:00"},
        expandRecurrences=True,
    )
```

`after` and `before` are LocalDateTimes, read in the query's `timeZone` argument
(UTC unless you pass one). Expanding recurrences is bounded by the server's
`maxExpandedQueryDuration`, and exceeding it earns `expandDurationTooLarge` with
no ids at all. So `calendar_event.query` checks an expanding query before it goes
out: the filter must be a single condition with both `after` and `before`, and
the span between them within the advertised duration. It raises
`CapabilityFieldError` otherwise, and you can chunk the window deliberately. A
query queued with `batch.add` goes out unchecked.

**Events are typed too**, as JSCalendar 2.0 (jscalendarbis) - the revision the
draft builds on, not RFC 8984: one `recurrenceRule` rather than an array, and a
participant's `calendarAddress` rather than `sendTo`. A `CalendarEvent` is the
Event in `jmap.models.jscalendar` plus the JMAP properties, forgiving in the same
way as a card, and `event.jscalendar(name)` reads by wire name:

```python
from jmap.models.calendars import CalendarEvent
from jmap.models.jscalendar import Alert, NDay, OffsetTrigger, RecurrenceRule

event = CalendarEvent(
    calendar_ids={calendar_id: True},
    title="Standup",
    start="2026-10-05T09:00:00",  # a LocalDateTime, read in time_zone
    time_zone="Europe/London",
    duration="PT15M",
    recurrence_rule=RecurrenceRule(frequency="weekly", by_day=[NDay(day="mo")]),
    alerts={"a1": Alert(trigger=OffsetTrigger(offset="-PT5M"))},
)
with client.batch() as batch:
    created = batch.calendars.calendar_event.set(create={"e1": event})
```

An alert's trigger reads as an `OffsetTrigger`, an `AbsoluteTrigger`, or an
`UnknownTrigger` holding a type this build does not know. Dates stay strings in
JSCalendar's own forms.

**Parsing iCalendar** is optional for a server, behind
`urn:ietf:params:jmap:calendars:parse`. When it is advertised,
`batch.calendars.calendar_event.parse(blob_ids=[blob_id])` reads uploaded `.ics`
files without storing them, and `result.events_of(blob_id)` is a **list**: one
file can hold many events.

**Free/busy** comes from `urn:ietf:params:jmap:principals:availability`, which
lends `get_availability` to the principal:

```python
with client.batch() as batch:
    busy = batch.principals.principal.get_availability(
        id=principal_id, utc_start="2026-10-01T00:00:00Z", utc_end="2026-10-08T00:00:00Z"
    )

for period in busy.result.items:
    print(period.utc_start, period.utc_end, period.status)
```

The window is checked against the account's `maxAvailabilityDuration` first,
because a wider one fails the whole call with `tooLarge`.

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
                ),
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

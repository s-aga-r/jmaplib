# Capabilities

This is the idea the rest of the library is built on. JMAP servers differ from
one another far more than a fixed client surface would suggest, and the
differences are not exotic: a server may implement Sieve but no
`SieveScript/changes`; it may advertise a blob capability whose
`supportedTypeNames` is empty, meaning `Blob/lookup` does nothing; it may support
`Email/query` sorted by `receivedAt` and not by `size`.

So the client asks, at connect time, and shapes itself to the answer.

## What you can call

Each supported capability appears as an attribute on a batch, and each data type
under it:

```python
with client.batch() as batch:
    emails = batch.mail.email.get(ids=["m1"])
```

Capabilities the server did not advertise are **absent**, not broken - so check
before reaching for one:

```python
if client.capabilities.supports("SieveScript/get"):
    with client.batch() as batch:
        scripts = batch.sieve.sieve_script.get(ids=None)
```

Reaching for a missing one raises `AttributeError` naming it, which is a better
outcome than a request the server rejects. The same is true one level down: a
`Thread` has no `/query` and no `/set` because RFC 8621 does not define them, so
`batch.mail.thread.query` does not exist. Unavailable methods are absent from the
type, and your type checker can see that.

Here is the full map for a server that supports everything this library models:

| Attribute | Data types |
|---|---|
| `batch.core` | `push_subscription`, `blob` (copy only) |
| `batch.mail` | `mailbox`, `thread`, `email`, `search_snippet` |
| `batch.submission` | `identity`, `email_submission` |
| `batch.vacation` | `vacation_response` |
| `batch.blob` | `blob` |
| `batch.quota` | `quota` |
| `batch.sieve` | `sieve_script` |
| `batch.mdn` | `mdn` |
| `batch.contacts` | `address_book`, `contact_card` |
| `batch.calendars` | `calendar`, `calendar_event`, `participant_identity`, `calendar_event_notification` |
| `batch.files` | `file_node` |
| `batch.principals` | `principal`, `share_notification` |

## Asking directly

`client.capabilities` is the resolved answer for this server and account.

```python
if client.capabilities.supports("Email/query"):
    ...
```

**Ask about methods, not capabilities.** Capability presence does not imply
method presence, and the specs say so outright: RFC 9404 §3.1 describes a server
advertising the blob capability while implementing no `Blob/lookup` at all. A
check of the form "does this server have `urn:ietf:params:jmap:blob`?" overstates
what works.

Other things worth knowing:

```python
client.capabilities.advertised  # every URN offered for this account
client.capabilities.unknown_urns  # advertised but not modelled here
client.capabilities.limits  # the core limits, parsed
```

`unknown_urns` is the honest half of the design. A URN this build does not know
is surfaced rather than dropped, and you can still reach its methods through
`client.call(...)`.

## Resolution is per account

`accountCapabilities` is not a subset of the session-level map - it is a second
map, and the answer is their union. This matters in practice:

- Stalwart advertises its vendor capability `urn:stalwart:jmap` **only** at
  account level, so a containment check against the session map gets the wrong
  answer.
- The same connection can support `Email/*` on one account and not on another.

It matters for *values*, too, and more sharply. A real Stalwart leaves most of
its capability objects empty at session level and puts every actual limit under
`accountCapabilities`. Read the session-level copy and `maxDelayedSend`,
`forbiddenNameChars` and `supportedDigestAlgorithms` all come back absent - so
any check built on them silently stops checking.

The library resolves the account for you. If you read a capability value
yourself, resolve it the same way:

```python
from jmap.capabilities.blob import BLOB_URN, BlobCapability

account = client.session.capability_account(BLOB_URN)
capability = BlobCapability.of(client.session.capability_value(BLOB_URN, account))
print(capability.supported_digest_algorithms)
```

`capability_account` answers with the account the server itself nominates for
that capability in `primaryAccounts`, falling back to whatever the session
implies. Both are the server's own statements.

## Fields gate behaviour, not just presence

A capability object carries limits, and some of them this library enforces
locally rather than letting you discover them from an error. Each check raises
`CapabilityFieldError` naming the advertised value. These run on their own:

| Field | Checked before |
|---|---|
| `maxSizeUpload` | `client.upload` |
| `maxObjectsInSet` | an oversized `/set` |
| `supportedDigestAlgorithms` | requesting `digest:<alg>` through `batch.blob.blob.get` |
| `supportedTypeNames` | naming types in `batch.blob.blob.lookup` |
| `maxDataSources`, `maxSizeBlobSet` | creating a blob with `batch.blob.blob.upload` |

The blob checks live in those builders; a call queued with `batch.add` goes out
unchecked. Others are functions for you to call before queueing the request -
nothing calls them for you:

| Field | Function |
|---|---|
| `collationAlgorithms` | `jmap.core.limits.check_collation` |
| `forbiddenNameChars`, `forbiddenNodeNames`, `maxSizeFileNodeName` | `jmap.capabilities.files.check_node_name` |
| `maxFileNodeDepth` | `jmap.capabilities.files.check_depth` |
| `fileNodeQuerySortOptions` | `jmap.capabilities.files.check_sort` |
| `maxExpandedQueryDuration` | `jmap.capabilities.calendars.check_expand_window` |
| `maxAvailabilityDuration` | `jmap.capabilities.calendars.check_availability_window` |

`emailQuerySortOptions` and `maxDelayedSend` are not checked at all; read them
from the capability when you need them.

A check is a real round trip saved, but more importantly it is a better error:
the exception names the field and the value the server advertised, which a
`invalidArguments` response does not.

## `using` is derived, then intersected

Every JMAP request declares which capabilities it uses. Getting that set wrong
fails in two different ways, and one of them is silent:

- **Under-declare** and the server behaves as if it implements nothing you left
  out (RFC 8620 §1.8). No error - just missing behaviour.
- **Over-declare** and Stalwart rejects the *entire* request with `notRequest`,
  destroying every unrelated call batched alongside it.

So the set is computed from what your batch actually uses - not just its methods
but the properties, filter conditions and sort comparators in it, because a
capability may add properties without adding any methods
(`urn:ietf:params:jmap:smimeverify` is exactly that) - and then intersected with
what the server advertises. A gap raises locally as
`CapabilityNotSupportedError`.

You never write `using` yourself. If you need to force a URN in - for a vendor
extension reached through `client.call` - there is an escape hatch:

```python
with client.batch(extra_using=frozenset({"urn:stalwart:jmap"})) as batch:
    batch.add("x:Account/get", {"accountId": account, "ids": None})
```

## Draft specifications

Calendars and FileNode track IETF drafts rather than RFCs. They are excluded
from the SemVer promise and hidden unless you opt in:

```python
client = JMAPClient.connect(url, auth=auth, experimental=True)
```

Without that, their URNs land in `unknown_urns` and `batch.calendars` does not
exist. `jmap.SPEC_REVISIONS` records exactly which revision of each document
this build implements.

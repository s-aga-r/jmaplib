# Blobs

A blob is opaque binary data with an id. Attachments, message sources, Sieve
scripts and contact photos are all blobs. Two entirely separate mechanisms move
them, and knowing which you have matters:

- **RFC 8620 §6** - plain HTTP `POST`/`GET` against the upload and download
  URLs. Always available. Streams. Not batchable.
- **RFC 9404** - the `Blob/upload`, `Blob/get` and `Blob/lookup` *methods*,
  which travel inside a normal batch and can therefore be chained. Only if the
  server advertises `urn:ietf:params:jmap:blob`.

## The HTTP endpoints

```python
uploaded = client.upload(b"hello world", content_type="text/plain")
print(uploaded.blob_id, uploaded.size, uploaded.type)

data = client.download(uploaded.blob_id)
```

Not part of a batch: these are their own HTTP requests, take no `using`, and
have no method-call semantics. The size is checked against `maxSizeUpload`
before anything is sent.

### Which account?

Blobs are account-scoped but belong to no capability, so `primaryAccounts` has
no entry to look up for them. The client resolves it in this order: an explicit
`account_id=`, the client default, the account every `primaryAccounts` entry
agrees on, then the sole account if there is only one.

```python
client.upload(data, account_id="u1234")
```

If the session genuinely implies no single account - several accounts, and
`primaryAccounts` pointing at more than one of them - `NoAccountError` says so
rather than guessing.

**A blob id belongs to the account it was uploaded to.** Uploading to one
account and referencing the blob from another fails with `blobNotFound`. That is
what `Blob/copy` is for.

## The blob methods

`Blob/upload` puts data in from inside a batch, assembling it from several
sources if you like:

```python
from jmap.models.blob import BlobUpload, DataSource

with client.batch() as batch:
    uploaded = batch.blob.blob.upload(
        create={
            "b1": BlobUpload(
                data=[
                    DataSource.text("Subject: hi\r\n\r\n"),
                    DataSource.blob(existing_blob_id),
                ],
                type="message/rfc822",
            )
        }
    )
blob_id = uploaded.result.created_id("b1")
```

`DataSource.raw(bytes)`, `.text(str)`, `.base64(str)` and `.blob(id)` are the
four sources; the last one is what makes this more than a slower upload, since
it concatenates blobs the server already has without moving the bytes.
`maxDataSources` is checked locally.

`Blob/get` reads data back as a value, with optional digests:

```python
with client.batch() as batch:
    got = batch.blob.blob.get(ids=[blob_id], properties=["data:asText", "size", "digest:sha-256"])

item = got.result.items[0]
print(item.size, item.digest("sha-256"))
```

Two fields are easy to misread. `size` is the size of the *whole* blob even when
you asked for a range, so a short read shows in `is_truncated`, not in comparing
sizes. And text that is not valid UTF-8 comes back as no text with
`is_encoding_problem` set: `item.data`, which reads whichever representation
arrived, gives `b""` for it as for an empty blob, so the flag is how to tell
them apart.

The property names carry colons and arguments; they are the wire names exactly.
A digest algorithm the server did not advertise raises `CapabilityFieldError`
before the request - so check what is on offer if you are not sure:

```python
from jmap.capabilities.blob import BLOB_URN, BlobCapability

account = client.session.capability_account(BLOB_URN)
capability = BlobCapability.of(client.session.capability_value(BLOB_URN, account))
print(capability.supported_digest_algorithms)  # e.g. ['sha', 'sha-256', 'sha-512']
```

## Finding what references a blob

`Blob/lookup` answers "which objects point at this blob?", which is how you find
out whether deleting something would orphan an attachment:

```python
with client.batch() as batch:
    found = batch.blob.blob.lookup(type_names=["Email"], ids=[blob_id])

for info in found.result.items:
    print(info.ids_of("Email"))
```

Two things to know. The type names you pass are checked against the server's
`supportedTypeNames`, and RFC 9404 §3.1 explicitly allows that list to be
**empty** - a server may advertise the capability while implementing no lookup
at all. And each named type pulls its own capability into `using`; omitting that
fails as `unknownDataType`, an error that says nothing about `using`, so the
library derives it for you.

## Copying between accounts

`Blob/copy` moves blobs across account boundaries. It is not the generic
`/copy` shape - it takes `blobIds` and answers `copied`/`notCopied`:

```python
with client.batch() as batch:
    copied = batch.core.blob.copy(from_account_id=other_account, blob_ids=[blob_id])
```

It lives under `batch.core` because RFC 8620 defines it, not RFC 9404.

## Creation references

A blob created in a batch can be referenced later in the same request by its
creation id:

```python
from jmap import CreationRef

with client.batch() as batch:
    batch.blob.blob.upload(create={"b": BlobUpload(data=[DataSource.text(source)])})
    batch.mail.email.import_(
        emails={"e1": {"blobId": CreationRef("b"), "mailboxIds": {inbox_id: True}}}
    )
```

Servers vary on how far they honour this outside a `/set`. Stalwart resolves it
in `Blob/get`'s `ids` but not in `SieveScript/validate`'s `blobId`, where it
answers `blobNotFound`. When a server refuses, upload in an earlier request and
pass the real id.

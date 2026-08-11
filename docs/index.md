# jmaplib documentation

A JMAP client for Python that reads what the server says it can do and adapts to
it, rather than assuming a fixed protocol surface.

## Start here

| | |
|---|---|
| [Getting started](getting-started.md) | Install, connect, make your first request, and the async mirror. |
| [Capabilities](capabilities.md) | The idea the rest of the library is built on: what the server advertises decides what you can call. Read this second. |
| [Batching and references](batching.md) | One request, many calls, and how a later call uses an earlier one's results. |

## By task

| | |
|---|---|
| [Mail](mail.md) | Mailboxes, searching, reading, composing, sending, vacation responses. |
| [Blobs](blobs.md) | Uploading and downloading binary data, digests, and what a blob id is good for. |
| [Staying in sync](sync.md) | Following changes without polling everything, and keeping a query result current. |
| [Push](push.md) | Event source connections, push subscriptions, VAPID, WebSocket. |
| [Authentication](auth.md) | Presenting credentials, and acquiring them over OAuth. |
| [Errors](errors.md) | The four ways JMAP fails, which ones you can retry, and what this library catches before the wire. |
| [Other capabilities](extensions.md) | Quota, Sieve, contacts, calendars, files, sharing, read receipts, S/MIME. |
| [Testing your own code](testing.md) | The in-process fake server that ships with the package. |

## Two things worth knowing up front

**A JMAP request is a batch.** Not an optimisation - it is the shape of the
protocol. One HTTP round trip carries many method calls, and a call can reference
what an earlier one returned. Writing one call at a time works, but you are
paying a round trip for something the protocol was designed to avoid.

**The server decides what exists.** Two JMAP servers rarely support the same set
of things, and the differences are not hypothetical: one implements Sieve but no
`SieveScript/changes`, another advertises a blob capability that supports no
`Blob/lookup` at all. This library resolves that at connect time, so unsupported
methods are absent from the object you hold rather than a surprise at runtime.

## Reference material

- [`stalwart-spike.md`](stalwart-spike.md) - how the CI test server is
  bootstrapped headlessly, and the server behaviours that cost time to discover.
- The [README](../README.md) covers design rationale and the development
  workflow; these guides cover using the library.

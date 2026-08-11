# Changelog

All notable changes are recorded here. Versions follow [SemVer](https://semver.org),
with one deliberate exception: capabilities marked `experimental=True` track IETF
drafts and are excluded from the compatibility promise. See `jmap.SPEC_REVISIONS`
for exactly which revision of each spec this build implements.

## 0.2.0

Blob management, Quota and Sieve - the three standalone RFCs that turn a mail
client into one that can also store filters and read its own limits.

### Added

**Blob management** (RFC 9404) - `urn:ietf:params:jmap:blob`.

- `Blob/upload` creates blobs *inside* a batch, so a script or a small attachment
  can be uploaded and referenced by the call that consumes it in one round trip.
  Sources concatenate, and a `blobId` source with `offset`/`length` splices an
  existing blob server-side without downloading it
- `Blob/get` with the range arguments, the `data`/`data:asText`/`data:asBase64`
  property family, and `digest:<algorithm>`. `Blob.data` reads whichever
  representation arrived; `size` remains the whole blob under a range request, so
  `isTruncated` rather than a length comparison is what reports a short read
- `Blob/lookup` for the reverse direction: which objects reference this blob
- `DataSource` enforces RFC 9404 §4.1's "exactly one of" rule locally, because the
  server is required to refuse to guess
- Capability fields gate the calls: `supportedDigestAlgorithms` before a digest,
  `supportedTypeNames` before a lookup, `maxDataSources` and `maxSizeBlobSet`
  before an upload

**Quota** (RFC 9425) - `urn:ietf:params:jmap:quota`. Read-only by construction:
there is no `Quota/set`, so `client.quota.quota` has no `.set` attribute at all.
`Quota/changes` carries `updatedProperties`, whose `null` means *fetch everything*
rather than *nothing changed*.

**Sieve** (RFC 9661) - `urn:ietf:params:jmap:sieve`. `SieveScript/get|query|set`
plus `/validate`, which checks a script without storing it and reports invalid
content as a value rather than a method error. `activate()` and `deactivate()`
wrap `/set`'s activation arguments, since `isActive` is server-set and cannot be
patched. Script names are checked locally against the forbidden character set and
against `maxSizeScriptName`, which counts **octets** rather than characters.

**Sync engine** (`jmap.sync`) (`jmap.sync`) — the library stays stateless; it computes deltas and
the application decides what to persist.

- `StateStore` protocol plus an in-memory implementation, with structured keys
  (`<accountId>/<TypeName>`) so an account's cursors can be removed together
- `ChangeStream` follows `Foo/changes` across pages until `hasMoreChanges` is
  false, and raises `ResyncRequiredError` on `cannotCalculateChanges` rather than
  passing it through as a generic method error. Delivery is at-least-once: the
  cursor advances only once the consumer requests the next page
- `QueryView` and `splice()` apply `Foo/queryChanges` deltas to a sparse cached
  id list, reproducing RFC 8620 §5.6's worked example
- `QuerySpec` as a stable cache key: filter key order is normalised, sort order is
  significant, and `collapseThreads` distinguishes two otherwise identical views

### Changed

- `FakeJMAPServer` now *implements* `Blob/upload` and `Blob/get` rather than
  stubbing them - it concatenates sources, resolves `#creationId` blob references,
  slices ranges and computes digests - so RFC 9404's worked examples run against
  it directly. `store_blob()`, `concatenate()` and `resolve_blob_id()` are public
- `FakeJMAPServer.fail()` makes a method answer with an `error` invocation, so
  downstream tests can drive error-recovery paths
- `MethodSpec` gained `response_model` (a typed response for a method whose shape
  is irregular, or standard-with-one-extra-argument) and `type_names_argument` (an
  argument naming data types whose capabilities must reach `using`)

### Fixed

- **`Blob/copy` was modelled as a standard `/copy` and is not one.** RFC 8620 §6.3
  takes `blobIds` rather than a `create` map and answers `copied`/`notCopied`
  rather than `created`/`notCreated`, so the generic builder sent arguments the
  server rejects, and the reply parsed into an object whose `created` was silently
  always empty. It is now a bespoke builder with its own response model
- **A back-reference passed as `properties` crashed the batch.** Both the
  `using`-derivation scan and the forbidden-property check iterated the argument,
  which a `ResultRef` is not - and feeding `/updatedProperties` straight into a
  following `/get` is exactly what RFC 9425 §4.3 and RFC 8621 §2.2 recommend
- The `Blob` data type was described twice, once per capability, which left
  `data_type("Blob")` depending on which resolved first. It is now one shared
  definition

## 0.1.0

First release. Core protocol and RFC 8621 Mail.

### Added

**Protocol kernel** — I/O-free, so it is testable without a server and shared by
both client shells.

- `Id` validation, creation references, and the four JMAP error families
- JSON Pointer with JMAP's `*` extension, including the one-level flattening rule
- `PatchObject` in three dialects (JMAP, JSCalendar 1.0, JSCalendar 2.0)
- I-JSON constraints: the 2^53 integer bound, duplicate-key detection, `UTCDate`
- RFC 6570 level 1 URI templates, hand-rolled rather than a dependency
- Request planning: `using` derivation, `createdIds` threading, and batch splitting
  that never cuts a back-reference
- Response routing, including implicit responses that share a call id
- Retry classification by whether a request *provably never applied*

**Capabilities** — a `CapabilitySpec` per URN, resolved per account against the
Session.

- `urn:ietf:params:jmap:core` (RFC 8620), `mail`, `submission`, `vacationresponse`
  (RFC 8621), `smimeverify` (RFC 9219) — 30 methods
- Per-account resolution unioning the session-level and account-level maps
- `using` derived from the batch and hard-intersected against what is advertised
- Method-granular support checks; unknown URNs surfaced rather than dropped

**Clients** — `JMAPClient` and `AsyncJMAPClient`, mirrors over one kernel.

- Discovery through `/.well-known/jmap`, following redirects
- Batching with back-references; `client.mail.email.get(...)` namespaces
- Typed responses: `GetResponse[Email]`, `SetResponse[Email]`, and the rest
- Per-type method surfaces composed from the spec — `Thread` has no `.query`
- `/get` auto-chunking against `maxObjectsInGet`, with torn-read detection
- Blob upload and download, size-checked before sending

**Auth** — Basic, app password, Bearer, callable, and OAuth2 with single-flight
refresh. `WWW-Authenticate` parsing including RFC 9728 `resource_metadata`.

**Testing** — `FakeJMAPServer` ships with the package and resolves back-references
through the library's own pointer evaluator. `ServerQuirks` reproduces real
deviations.

### Known limitations

- **The integration suite has not been run against a live server.** Every
  behaviour above is verified against the in-process fake (1161 tests, 100%
  coverage), and `tests/integration/` is written and ready, but Stalwart's v0.16
  headless bootstrap is unresolved — see `docs/stalwart-spike.md`. Treat
  real-server behaviour as untested until that lands.
- Calendars, Contacts, FileNode, Sieve, Quota, Blob-management, MDN and push
  (EventSource/WebSocket) are not implemented yet.
- OAuth *acquisition* (PKCE, device flow, RFC 9728/8414 discovery) is not
  implemented; bring your own token.
- `Email/set` creation validation covers a documented subset of RFC 8621 §4.6 —
  anything requiring server state is left to the server.

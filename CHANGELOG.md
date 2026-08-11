# Changelog

All notable changes are recorded here. Versions follow [SemVer](https://semver.org),
with one deliberate exception: capabilities marked `experimental=True` track IETF
drafts and are excluded from the compatibility promise. See `jmap.SPEC_REVISIONS`
for exactly which revision of each spec this build implements.

## 0.7.0

Four milestones in one release: OAuth acquisition, Contacts, Sharing, Calendars
and file storage. The last two are **experimental** — they track Internet-Drafts
and are excluded from the SemVer promise; `jmap.SPEC_REVISIONS` publishes exactly
which revision each targets, and neither resolves unless the caller passes
`experimental=True`.

### Added

**OAuth acquisition** (RFC 6749, 7591, 7636, 8252, 8414, 8628, 9728). A 401
carries a `resource_metadata` pointer, that document names an authorization
server, its metadata names the endpoints, and the flow runs against those — so
nothing has to be configured in advance.

- Authorization-code flow with PKCE on a one-shot loopback listener, and RFC 8628
  device flow with its full polling state machine
- **S256 only.** RFC 7636's `plain` puts the verifier in the same URL as the
  challenge, which defeats the point for exactly the clients that need it. A
  server advertising neither is refused
- RFC 8414's well-known segment is *inserted*, not appended, and the returned
  `issuer` is checked against the one the URL was built from (§3.3) — without that
  check any host answering the path can nominate a token endpoint
- Dynamic client registration as a *public* client: a desktop application cannot
  keep a secret, and one it ships is not a secret

**Discovery** (`jmap.discovery`) — `_jmap._tcp` SRV records in RFC 2782 preference
order, then `https://<domain>/.well-known/jmap`. `JMAPClient.discover(address)`
walks the candidates. The well-known guess alone is not enough: Fastmail answers
404 there.

**Contacts** (RFC 9610) — `AddressBook` and `ContactCard`, with the JSContact body
carried losslessly through `extra`. Plus **two vendor capabilities** for the
pre-RFC `Contact`/`ContactGroup` model: the legacy methods are gated by
`https://www.fastmail.com/dev/contacts` and `https://cyrusimap.org/ns/jmap/contacts`,
*not* by the IETF URN — so they are three capabilities rather than two flavours of
one, and `using` derivation gets it right for free. Their method inventory is not
the standard six: `Contact` has no `/queryChanges`, `ContactGroup` does have
`/query`.

**Sharing** (RFC 9670) — `Principal`, `ShareNotification`, and `jmap.sharing` for
`shareWith` maps. `grant()`/`revoke()` build *pointer* patches, because assigning
the map revokes everyone absent from it. `Session.account_capability_value()` reads
`accountCapabilities` without the session-level fallback, which
`urn:ietf:params:jmap:principals:owner` requires.

**Calendars** (draft-ietf-jmap-calendars-27, experimental) — `Calendar`,
`CalendarEvent`, `ParticipantIdentity`, `CalendarEventNotification`, plus
`Principal/getAvailability`, which lives in *this* draft rather than in RFC 9670.
Local gates for `maxExpandedQueryDuration` and `maxAvailabilityDuration`, and for
§5.11's rule that an expanding query forbids a FilterOperator entirely.

**File storage** (draft-ietf-jmap-filenode-14, experimental) — `FileNode` with its
`nodeType` union, and gates for the name rules, the sort list and the depth limit.
`maxFileNodeDepth` is defined as one *more* than the ancestor count, so
`max_ancestors` derives it once rather than at each call site.

### Fixed

Delegated test-writing found ten live bugs across the new code, all fixed here.
Six were in OAuth and five of those were security-relevant:

- `secrets.compare_digest` raises `TypeError` on non-ASCII `str`, and the redirect
  state is attacker-controlled — one accented character escaped every
  `except OAuthError` around the flow
- An empty expected state accepted a redirect carrying no state at all: the CSRF
  guard failed **open**
- Extra authorization parameters could overwrite `state` and `code_challenge`
- A JSON `null` for a required device-flow field became the literal string
  `"None"` on the wire, leaving the client polling forever
- `bool` subclasses `int`, so every `isinstance(value, int)` guard admitted `true`
- The discovery documents inherited JMAP's camelCase alias generator and would
  have serialised `authorizationServers`, a spelling neither RFC uses

And four elsewhere:

- `owner_of()`/`principal_account()` read `:principals:owner` through a lookup
  that falls back to the session map, giving every unowned account the same bogus
  owner and aiming `Principal/*` at the wrong account
- `grant()` had no owner check and no way to supply one
- `shareWith` pointers were not RFC 6901-escaped
- `duration_seconds` accepted `PT` and summed it to zero — reading as a real limit
  of zero seconds rather than as unparseable — and accepted `P1W1D` although weeks
  are exclusive with days

## 0.3.0

Push. Three transports for one idea: a `StateChange` names which types moved in
which accounts and nothing more, so whichever way it arrives - and whether or not
some are dropped - the follow-up is the same `/changes` call. That is what makes
push an optimisation rather than a second source of truth.

### Added

**Event source** (RFC 8620 §7.3) - `jmap.push.EventSourceClient` and its async
twin, over a `text/event-stream` parser that is I/O-free and therefore fully
testable without a socket.

- `SSEParser` handles the parts that are silent when wrong: a CRLF split across
  two reads is one terminator, several `data:` lines join with newlines, an event
  with no data is not dispatched, and the last event id **persists** across events
- Reconnection resumes rather than restarts. `Last-Event-ID` goes back every time,
  so a drop costs latency instead of data
- **A ping is not a cursor.** RFC 8620 §7.3 forbids a ping from setting an event
  id; resuming from one skips every change that arrived before it
- `closeafter=state` is modelled as success, not a lost connection - it exists
  because buffering proxies otherwise hold notifications back indefinitely
- Requested types are checked against the session, because a server simply never
  pushes a type it does not have and that is indistinguishable from silence

**PushSubscription lifecycle** (RFC 8620 §7.2) - `new_subscription()`,
`verification_update()`, `renewal_update()`, `mine()`, and `PendingVerification`
for the §7.2.3 race where the verification push beats the `/set` response that
created the subscription. `PushSubscription` is now a typed model; `url` and
`keys` are refused locally because asking earns `forbidden` for the whole call.

**WebSocket** (RFC 8887) - `WebSocketProtocol`, the I/O-free framing and
demultiplexing layer: `@type` dispatch, request/response correlation by id
(§4.3.2 lets the server answer out of order), request-level errors as messages
rather than status codes, and `pushState` tracking so a reconnect costs one
exchange instead of a `/changes` per type. `should_reauthenticate()` distinguishes
close code 1008 - credentials expired, so redialling loops forever - from the
retryable ones.

**VAPID** (RFC 9749) - `urn:ietf:params:jmap:webpush-vapid`, and
`needs_recreating()` for §5's key rotation. Nothing raises when a key rotates: the
server destroys the subscription and notifications simply stop, so the client has
to ask.

### Changed

- `JMAPClient.http` / `AsyncJMAPClient.http` expose the underlying HTTP client, so
  a push connection reuses the credentials the client already carries
- `FakeJMAPServer` serves a real event source: `push()`, `push_ping()` and
  `event_source_requests` make resumption and the ping rule testable end to end

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

# Changelog

All notable changes are recorded here. Versions follow [SemVer](https://semver.org),
with one deliberate exception: capabilities marked `experimental=True` track IETF
drafts and are excluded from the compatibility promise. See `jmap.SPEC_REVISIONS`
for exactly which revision of each spec this build implements.

## 1.1.0

### Security

A deep review of what a hostile network, server, or session document could do
to this client. Every fix below is regression-tested; the themes, worst first:

- **OAuth ran over any scheme.** Nothing in the discovery/token chain enforced
  TLS, and the RFC 8414 issuer check cannot help against a MITM serving
  self-consistent metadata for an `http://` issuer it injected - the code, PKCE
  verifier, refresh token and client secret then travelled in cleartext to an
  endpoint the attacker controls. Every URL the chain fetches or POSTs secrets
  to now requires https (loopback excepted, for development). Token POSTs also
  no longer follow redirects, the device-flow poll interval is clamped, the
  loopback listener's capture slot only accepts the registered callback path,
  and server-supplied URLs are stripped of terminal escapes before printing.
- **A transport error could double-send mail.** Every non-timeout httpx error
  was classified "connection never established" and retried - including
  ReadError and RemoteProtocolError, which arrive after the request went out.
  A server that applied an unguarded `EmailSubmission/set` and then dropped the
  connection got the batch re-sent. Only connect-phase failures retry unguarded
  mutations now.
- **The session document could redirect credentials.** Endpoint URLs were
  adopted verbatim, so a document fetched over https naming an `http://`
  `apiUrl` steered every authenticated request onto cleartext. Refused now
  (`InsecureEndpointError`); cross-host https stays legal, http sessions keep
  http endpoints, and offline parsing is not second-guessed.
- **The event stream could exhaust memory or kill the listener.** One
  unterminated `data:` line grew without bound; a UTF-8 character split across
  chunks (or `retry:²`) crashed the parser; a huge `retry:` disabled push
  forever; and the transport errors that reconnection exists for escaped
  `listen()` instead of redialling. All bounded, decoded incrementally, capped,
  and redialled with backoff now.
- **Crafted documents escaped the kernel's error contract.** Hundred-thousand-
  deep nesting killed callers with RecursionError; `NaN`/`Infinity` and
  thousand-digit integers slipped past or produced path-less stdlib errors;
  non-string `createdIds` reified Python reprs onto the wire; one malformed
  method response aborted dispatch for its siblings; hostile session shapes
  raised AttributeError. All are typed, catchable errors now.
- **The sync engine allocated whatever the server asked.** `position`/`total`/
  `index` sized real lists, so a fifty-byte response claiming `total: 2**45`
  was a multi-terabyte allocation; a stuck `hasMoreChanges` looped forever.
  Both bounded.

### Fixed

- Persisted query cursors survive a restart: `query_key` hashed with the
  process-salted `hash()`, so every run re-keyed the store and silently fell
  back to a full re-query. Now a stable SHA-256 digest.
- Token expiry deadlines are wall-clock, since `TokenStore` persists them and a
  monotonic value is meaningless in any other process.
- A `/set` whose `destroy` is a back-reference (query-then-destroy) no longer
  crashes `plan()`.

### Performance

- `splice()` applies a `queryChanges` delta in one merge pass instead of an
  `insert()` per added item - O(n+k) instead of O(k·n), hypothesis-verified
  equivalent to the RFC's own algorithm.
- The SSE parser scans by offset instead of re-slicing its buffer per line
  (quadratic on large multi-chunk events), and two str→bytes→str round trips
  per push event are gone.
- `ActiveCapabilities.limits` is computed once per resolution instead of
  re-parsed on every `add()` and `plan()`.
- The cost of the new validation: `Session.from_wire` +~1.6µs (once per
  connect) and `Response.from_wire` +~0.6µs (once per request) - noise against
  a network round trip, paid for shapes that previously crashed. Everything
  else is within benchmark noise.

### Added

**`ContactCard/parse`, behind `urn:ietf:params:jmap:contacts:parse`.** A Stalwart
extension - the URN is IETF-spelled but no RFC defines it; RFC 9610 has no
`/parse` at all. Reached through `batch.add("ContactCard/parse", {...})` like the
other builder-less methods, with the response parsed into `ParsedCards`. The
shape was verified against Stalwart's implementation, and differs from the
calendars `/parse` in the way most easily got wrong: each blob parses to **one**
Card, not an array. The per-call blob cap is server configuration advertised
nowhere - an oversized call answers `requestTooLarge`, and halving the batch is
the working strategy.

### Changed

**I-JSON enforcement no longer costs a second pass over every payload.** RFC 8620
§1.1 constraints were checked by walking the decoded tree in Python, which for a
`Email/get` of a hundred messages meant re-visiting some twenty thousand nodes and
building a JSON Pointer string at each one — for an error message that is almost
never emitted. Parsing a response cost **6.2× what the stdlib charges for the same
document**; it now costs 1.8×, and serialising a request is about half what it was.

Each constraint is now settled where it is cheapest, and none of it changes what
is accepted or what an error says:

- **integer range** moves into `json`'s own `parse_int` hook, so the cost is
  proportional to the numbers in a document rather than to every node in it;
- **unpaired surrogates** are ruled out for a whole document at once — a strict
  UTF-8 decode cannot produce one, so a `bytes` body only has to be checked for
  `\uD800`-style escapes, by substring rather than by regex;
- **duplicate keys** build their dict in C and compare lengths, scanning for the
  offending key only once one is known to be there.

What remains is a fast scan that answers only yes/no. Anything it cannot cheaply
prove clean — an unfamiliar type most of all — still goes to the original walk,
which stays the authority on both the verdict and the message. Error text,
including the JSON Pointer naming the member at fault, is unchanged.

**`PatchBuilder` no longer re-checks every key on every edit.** Each `set` copied
the whole key map and re-compared all of it, so building a patch was quadratic in
its own size — 50 keywords cost 205 µs. Overlap is now tracked incrementally
against two indexes, one per direction of the prefix relation, and the same patch
costs 45 µs. Verified equivalent to the previous implementation, error strings
included, over ~59,000 generated key sequences.

### Added

**A benchmark harness** (`benchmarks/`, `uv run python -m benchmarks.run`) over
payloads shaped like a real server's, with `--save`/`--compare` for regression
checks and `--profile` for cProfile. Everything above was found with it, including
one bug it alone could see: a boolean fell through the new fast scan's type ladder,
so every payload containing one silently took the slow path while every test
passed. `TestTheFastPathIsActuallyTaken` now asserts the path rather than the
answer, which is the only kind of test that can catch that class of regression.

## 1.0.0

The capability surface is complete: every JMAP RFC published to date, plus the two
Internet-Drafts, plus the vendor extensions two real servers ship. From here the
names below are a promise — with one stated exception, which is that capabilities
marked `experimental=True` track drafts and are excluded from it.

### Added

**MDN** (RFC 9007) — `MDN/send` and `MDN/parse`. This capability is unusual in
that the *server* polices the *client's* bookkeeping: §2.1 requires a send to also
set `$mdnsent` on the message being acknowledged, and requires the server to check
`onSuccessUpdateEmail` and reject the call otherwise. So the patch is part of a
well-formed request rather than optional garnish, and the builder supplies it by
default. `$mdnsent` is lowercase — keywords are case-insensitive in IMAP and
case-*sensitive* in JMAP, and §1.2 fixes the spelling.

**A conformance matrix** (`jmap.testing.conformance`, and
`python -m jmap.testing.conformance --markdown`). Built from the Session alone, so
it costs no method calls, and it keeps three states apart that reports usually
collapse: advertised-and-modelled, advertised-but-not-modelled (a vendor URN or a
newer spec — still reachable through `batch.add`), and modelled-but-not-advertised,
which is the line that answers "why is this feature missing".

**A public-API snapshot** (`tests/unit/test_public_api.py`). At 1.0 a rename is a
breaking change, so it is now a test failure rather than something noticed later.

### Fixed

- **The S/MIME filter conditions were wrong.** RFC 9219 §4.2 defines `hasSmime`,
  `hasVerifiedSmime` and `hasVerifiedSmimeAtDelivery`; the library declared a
  `smimeStatus` filter, which no server implements — so a query using it filtered
  on nothing. The `smimeStatusAtDelivery` *property* was also missing, and it is
  the one that does not change as trust anchors are removed, which is what makes
  "was this trusted when it arrived?" answerable at all
- **The package did not import on Python 3.11**, the version `requires-python`
  declares as the floor. A bare `MappingProxyType({})` as a dataclass field
  default is rejected there — 3.11 refuses any *unhashable* default, and a
  mappingproxy is one; 3.12 relaxed the check to reject only list/dict/set by
  type, so it is invisible on a newer interpreter. Present since the first weeks
  of the project, caught by CI from the second commit onwards, and never read.
  `tests/unit/test_public_api.py` now asserts no dataclass field carries an
  unhashable default, so the failure is reachable from whatever version you
  happen to run tests on
- **Two properties were serialised under names no server reads.** RFC 9007's
  `reportingUA` and the calendars draft's `mayRSVP` both carry an acronym, and
  pydantic's camelCase generator lowercases all but its first letter — producing
  `reportingUa` and `mayRsvp`. Silent in both directions: the value is written
  under a key the server ignores, and a correctly-spelled value read back lands in
  `extra` rather than on the field. Both now carry explicit aliases, and
  `tests/unit/test_public_api.py` guards the class
- The registry refused two capabilities declaring the same method even when they
  declared it *identically*, which the two vendor contacts URNs legitimately do
  and a server may advertise together. A genuine disagreement is still refused

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
- `MethodSpec.matches` was documented as existing for contacts, citing the very
  design `capabilities/contacts.py` opens by refuting
- `ContactCard.media()` / `.photos()` now exist. `Media` was modelled but nothing
  produced it, so the blob-vs-`data:`-URI distinction the type exists for was
  reachable only by validating raw dicts by hand

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

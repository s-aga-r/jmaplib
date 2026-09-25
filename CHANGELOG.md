# Changelog

All notable changes are recorded here. Versions follow [SemVer](https://semver.org),
with one deliberate exception: capabilities marked `experimental=True` track IETF
drafts and are excluded from the compatibility promise. See `jmap.SPEC_REVISIONS`
for exactly which revision of each spec this build implements.

## Unreleased

### Security

- **A redirect could still downgrade the session to cleartext.** 1.1.0 refused an
  `http://` endpoint in a session fetched over https, but it judged the session by
  the channel it *arrived* over. A redirect from https to http therefore made it an
  "http session", whose http endpoints passed: whoever answered the cleartext leg
  chose the `apiUrl`, and the next call carried the credentials there. The session
  fetch now refuses any hop onto a weaker channel than the one asked for
  (`InsecureEndpointError`); loopback stays allowed, as it is for endpoints.
- **The device-flow poll skipped the token endpoint's protections.** Every other
  token request is https-only and never follows a redirect. `poll_device_flow` did
  neither, so the device code - which redeems the grant once the user approves -
  could travel in cleartext or be re-posted to wherever a 307 pointed. It now takes
  the same precautions.
- **The event-stream bound could be walked around.** The per-event cap counted each
  `data:` value but not the newline joining it to the next, so an endless run of
  bare `data` lines grew memory at a tally of zero. A line now costs its value plus
  its newline, which is exactly what the spec's data buffer holds.

- **OAuth requests carried a shared client's credential.** Given the `httpx.Client`
  that authenticates to the JMAP server - which `http=` invites - every discovery
  fetch and token request went out with its `Authorization`, handing a Basic
  password or bearer token to whichever hosts the server's documents named. With
  `OAuth2Auth` on that client, a token endpoint answering the stale bearer with a
  Bearer 401 re-entered the refresh on the thread holding its lock, and every
  request after it hung for good. OAuth requests are now sent without the client's
  credential, and a refresh that re-enters its own credential fails with
  `AuthenticationError` rather than deadlocking.
- **OAuth discovery followed redirects into cleartext.** The https check covered
  the first URL only; a redirect to `http://` was followed, and whoever answered
  that leg served self-consistent metadata naming its own endpoints. Every hop is
  now checked before it is fetched.
- **The RFC 9728 `resource` was never checked.** Metadata about another resource -
  or about none - chose the authorization server all the same. It must now name a
  resource, match the well-known URL it came from, and cover the URL that drew the
  challenge when `discover(..., resource=url)` is given (§3.3). "Cover" rather than
  "equal": Stalwart names its origin for every URL beneath it. `resource` also
  resolves a relative `resource_metadata`, which is what Stalwart sends and which
  could not be fetched before.
- **An SRV record could steer the credentials.** `JMAPClient.discover` tried SRV
  targets first, wherever they pointed, and the first candidate receives the
  credentials - so an unsigned DNS answer chose who got them, and TLS vouched only
  for the host it named. A target outside the address's domain is now tried only
  when `confirm_srv_target` accepts it (RFC 6186 §6); when nothing else answers,
  `UnconfirmedSRVTargetError` names it so the user can be asked.
- **The authorization endpoint's scheme went unchecked.** `authorize(open_browser=True)`
  handed it to `webbrowser.open` - `os.startfile` on Windows - as it came, so
  `file:` and `ms-msdt:` URLs went through, as did a cleartext login page. It must
  now be https, or http on loopback.
- **The loopback listener could be held or hijacked from the same machine.** It
  served one connection at a time, so a connection that sent nothing hung
  `authorize()` for as long as it stayed open, closing included; and any request for
  the redirect path took the one-shot slot, ending the flow on a state mismatch.
  It now serves each connection on its own thread with a 10-second deadline, and
  takes only the redirect carrying the flow's `state`.

- **The conformance command wanted the password on its command line.**
  `--password` was required, which showed the password to every user on the
  machine through the process list, and kept it in shell history. The command
  now reads `$JMAP_PASSWORD`, or asks; `--password` still works for scripts that
  pass it.

### Fixed

- **A `/set` answered with `null` came back as a failure.** RFC 8620 §5.3 makes
  `created`, `updated`, `destroyed` and the three `not*` maps nullable - null when
  that category is empty - and §5.4 and §6.3 say the same for `/copy` and
  `Blob/copy`. The response models refused null, so a conformant server's answer
  to a write that had succeeded became `MethodError("malformedResult")`, inviting
  the caller to make it again. Null now reads as empty.
- **`catch_up()` could lose changes.** It moved the stored cursor page by page,
  before handing anything over, so a failure on page N discarded pages 1 to N-1
  while recording them as delivered: the retry resumed after them. The cursor now
  moves once, after the last page has arrived. `pages()` is unchanged and still
  delivers at least once, page by page; the README and the sync guide now say
  which of the two you get.
- **Refreshing an OAuth token could destroy the grant.** When the authorization
  server does not rotate refresh tokens - RFC 6749 §6 leaves that optional -
  `refresh()` returned none, `OAuth2Auth` persisted that, and the next refresh
  presented nothing and failed with `invalid_grant`. The token presented is now
  carried forward.
- **A split batch lost its creation references.** Every request was planned up
  front with only the caller's seed, so a batch split under `maxCallsInRequest`
  never passed later requests the ids the server assigned while answering earlier
  ones, and a `#creationId` there resolved to nothing. Each request now carries
  every creation id learnt so far.
- **A back-referenced `ifInState` made a retry unsafe.** It counted as a guard, but
  a retry re-resolves it against the state the first, already-applied attempt
  produced, so it always matched and a timed-out write could land twice. Only a
  literal state guards a retry now; a batch without one is not re-sent after a
  timeout.

- **Registered keywords were refused.** `keyword_patch` and
  `validate_email_create` held `$`-prefixed keywords to a short list, which left
  out `$mdnsent` - the library's own MDN keyword - and most of the IANA registry.
  Any keyword of legal characters and length is accepted now.
- **`refresh_session()` forgot `experimental=True`.** The capabilities were
  resolved again without the opt-in, so Calendars and FileNode vanished on the first
  refresh. The client keeps it as `experimental`.
- **S/MIME filters never declared their capability.** `using` came from the
  methods and properties in a batch, not its filter conditions or sort
  comparators, so `hasSmime` and friends went out without
  `urn:ietf:params:jmap:smimeverify` and the server ignored them. Filters and sorts
  now count, `SearchSnippet/get`'s as `Email`'s.
- **Splitting a batch could reorder it.** Calls tied by back-references were
  packed together wherever they sat, so a `get` queued after a `destroy` could run
  first. Requests are now cut only between calls, in order, where no
  back-reference crosses the cut; a run too long to fit between two such cuts
  raises `BatchTooLargeError`.
- **`Retry-After` had no ceiling.** One 503 could park a call for as long as the
  server said - a year, if it said so - and a large enough value raised
  `OverflowError`. `RetryPolicy.max_retry_after` (120 s) is the ceiling now; past
  it the `RequestError` is raised at once, carrying the wait as `retry_after`.
- **`JMAPClient.discover` stopped at the first page that was not a session.** A
  parked domain's HTML or JSON of the wrong shape ended the search instead of
  moving it to the next candidate. A 401 or a downgrade still ends it.
- **A back-reference into a chunked `/get` covered its first chunk.** One call's
  result can be referenced, and a `/get` over `maxObjectsInGet` is several calls,
  so the reference quietly saw 100 of 250 ids. It raises `ChunkedReferenceError`
  now; `/accountId` and `/state`, the same in every chunk, still resolve.
- **A change stream could loop forever.** A server whose state walked back to one
  already resumed from (`s1`, `s2`, `s1`, all with `hasMoreChanges`) kept the walk
  going indefinitely. That is `StuckChangeStreamError` now.
- **`QueryView` refused large queries.** It allocated a slot per position up to
  `total`, and capped that at a million to stay alive. The unfetched tail is now
  implied rather than stored: `len()` still reports the total, at no cost, and a
  negative total is refused.
- **Parsing an event stream was quadratic.** Each line searched for a CR that an
  LF-only stream - every real server's - never sends, to the end of the buffer;
  a 6 KB gzipped response cost about 30 s of CPU. Lines are found in one scan now.
- **A dropped connection could skip an event.** The resume cursor moved when an
  `id:` line was read rather than when its event was complete, so a drop mid-event
  resumed after an event never delivered. It moves at dispatch, as WHATWG's does.
- **An event id could jam `listen()` for good.** An id that cannot be sent back as
  `Last-Event-ID` - non-ASCII, or with leading whitespace - became the cursor, and
  every reconnect then failed while building its request. Only an id that can be
  sent becomes the cursor.
- **An answer that was not an event stream reset the backoff.** A 204, a portal's
  HTML or a session document read as a quiet stream that ended cleanly, so
  `listen()` redialled forever at the base delay. Anything but a 200 carrying
  `text/event-stream` is now a failed connection, and a connection that ends
  within `HEALTHY_CONNECTION_SECONDS` having delivered nothing backs off.
- **A token that could not be saved was thrown away.** When `TokenStore.save`
  raised, the new token was dropped and the old refresh token kept - which a
  rotating server had already retired, so the next refresh replayed it and
  Fastmail revoked the grant. The new token is used either way, and the error
  still reaches the caller.
- **`except JMAPError` did not catch everything.** 24 of the library's exception
  classes were plain `ValueError`s; malformed JSON raised `json.JSONDecodeError`;
  push events, WebSocket frames, upload answers and OAuth documents of the wrong
  shape leaked pydantic's `ValidationError`; blob transfers and OAuth requests
  leaked httpx's errors. All of them are `JMAPError`s now, keeping the bases they
  had, so no existing `except` stops matching.
- **Docs that promised what the code does not do.** The capabilities guide listed
  nine limits as checked before sending, five of which were not; the mail guide
  said the same of sort options and `maxDelayedSend`. The push guide's loop
  crashed on the first ping and called `matches` wrongly; the sync guide never
  fetched created records and left its reset without a cursor; getting-started had
  the discovery order backwards; and the OAuth example passed a URL `discover`
  could never use. Each now describes the library as it is.

- **A call the server left unanswered read as never sent.** RFC 8620 §3.4 has
  the server answer every method call. One that did not left its handle raising
  a RuntimeError that told the caller to run a batch that had already run; it
  now fails with a `missingResponse` MethodError.
- **`SetError` took the server's fields on trust.** A string `properties` came
  back as a tuple of its characters, and a number raised TypeError. Each field is
  now kept only when it has the type RFC 8620 §5.3 gives it.
- **A chunked `/get` could fetch an id twice, or not chunk at all.** A repeated
  id that fell into two chunks came back twice, so the answer depended on
  `maxObjectsInGet`, and ids given as a tuple went out whole. Ids are now
  deduplicated before splitting, and any sequence splits.
- **A malformed query delta rewrote a view without complaint.** `AddedItem` read
  a missing index as 0 and a missing id as a gap. Both are required now, as RFC
  8620 §5.6 has them, so such a delta fails to parse.
- **A view kept a total its latest delta had made stale.** Applied without one,
  the old total stayed and `len()` counted rows that were gone. It is unknown now
  until a response reports it again.
- **The same query could get two keys.** `QuerySpec` read dicts and lists by
  content and everything else through `repr()`, so a tuple sort or a read-only
  mapping made a different key from its equal list or dict. Any mapping or
  sequence is read by content now; keys already persisted do not change.
- **Two servers could share a sync cursor.** State keys hold the account id,
  which is unique only on its own server, so one store serving two servers filed
  both accounts named `a` under one key. `namespace=` on `ChangeStream`,
  `type_key()` and `query_key()` keeps them apart.
- **`ChangeSet` repeated ids it promised to deduplicate.** An id reported on two
  pages was listed twice; each list now holds an id once, in first-reported
  order.
- **A finished chunked `/get` still said "pending"** in its repr, which read a
  flag only an ordinary handle sets.
- **A byte-order mark split across reads survived.** Part of a character decodes
  to nothing, and that empty chunk spent the one-time BOM check - and any CR held
  over from the chunk before - so the first event could arrive typed `message`,
  or a CRLF split in two.
- **Closing the async `listen()` left its connection open** until the event
  loop got round to finalising it, and with it the cursor that connection had
  moved. It closes at once now, as the sync loop's does.
- **`listen()` gave up on a proxy's 503.** A 429 or a 5xx - the statuses the retry
  policy calls transient - ended the listener. It now redials with backoff, after
  any `Retry-After`, capped like every other delay; other 4xx still raise.
- **Two WebSocket requests could share an id.** The counter could hand out an id
  a caller had already chosen, and responses are matched by id. Generated ids
  now skip those in flight, and reusing one in flight is refused.
- **`InsufficientScopeError` missed most missing scopes.** Only a 401 was checked,
  and only for `OAuth2Auth` with a refresh callable, though RFC 6750 recommends a
  403; and its challenges were reprs the scope could not be read back from. Any
  credential now raises it on a 401 or a 403, carrying the headers as sent.
- **`OAuth2Auth` never renewed a token ahead of its expiry,** though the auth
  guide said it did: an expired token went out, and a 401 without a Bearer
  challenge never led to a refresh. It now renews thirty seconds ahead, or
  halfway through a shorter life, so a short-lived token is not renewed on every
  request.
- **A token of another type was sent as Bearer.** A DPoP or MAC token response is
  now refused with `unsupported_token_type`, as RFC 6749 §7.1 requires; a
  response naming no type is still read as Bearer.
- **`protected_resource_url` dropped the query,** which RFC 9728 §3.1 keeps, so
  two resources differing only there shared one metadata URL.
- **An OAuth endpoint could fail as a raw `httpx.InvalidURL`.** `urlsplit`
  deletes tabs and newlines before parsing, so the https check read a different
  URL from the one httpx refused - with an error no `except` in the library
  caught. Such URLs, and any other httpx cannot use, are a `DiscoveryError` now.
- **An odd SRV answer could end discovery.** A target DNS allows but a URL cannot
  hold - `a:b` - made httpx raise `InvalidURL` before the well-known URL was
  tried. Such targets, RFC 2782's "." and port 0 are left out.
- **Types without `/queryChanges` offered `query_changes()`.** SieveScript and the
  legacy contact types had it, only for it to fail at the call.
- **An import rule guarded a module that does not exist.** The one keeping the
  capabilities from importing the client named `jmap.transport`, and passed
  vacuously. It names `jmap.client` and `jmap.aio`, and a test now checks that
  every module the rules name exists.
- **The README was a release behind,** still calling itself 1.0.0 and OAuth
  sign-in future work, and the sync guide said a view refuses a delta computed
  for another filter, which it cannot tell apart. Both are corrected.

- **`PushKeys` sent its public key as `p256Dh`.** The camelCase generator
  capitalises a letter after a digit, so a subscription whose `keys` came from the
  model carried the key under a name no server reads; reading one back worked
  only because the field name matched. It goes out as RFC 8620 §7.2's `p256dh`,
  and the alias guard now covers digits as well as acronyms.
- **`/set` and `/copy` refused a typed model to create.** `Blob/upload` and
  `MDN/send` took one, and said every builder did, but a `Mailbox` in a
  `Mailbox/set` failed as `TypeError: Object of type Mailbox is not JSON
  serializable`. Any object with a `to_wire()` is now serialised through it,
  wherever in the arguments it sits.

### Added

- **Arguments are checked with pydantic before anything is sent.** Every builder
  validates its arguments against its signature, strictly - ids, states, limits,
  filters, sorts, patches, objects to create - so `ids="m1"`, `limit=-5` or
  `calculate_total="yes"` raise pydantic's `ValidationError`, a `ValueError`
  titled with the call (`Email.get`), where they were written. `UNSET` and a
  back-reference go through unchecked, and every standard builder's arguments now
  admit a `ResultRef`, as RFC 8620 §3.7 does. The push helpers
  (`new_subscription`, `renewal_update`, `verification_update`,
  `event_source_url`) and `QuerySpec.build` check theirs the same way.
- **`RetryPolicy`, `OAuth2Token` and `SRVTarget` are pydantic dataclasses,**
  checked when they are made - and `OAuth2Token` whenever a field is assigned.
  `dataclasses.asdict(token)` is what a `TokenStore` needs to keep, and
  `OAuth2Token(**saved)` restores it.
- `jmap.models.arguments`: the `checked` decorator the builders use, and the
  `Int`, `UnsignedInt` and `UTCDate` argument types (RFC 8620 §1.3, §1.4).
- `jmap.api.entity.builder`, `jmap.api.entity.Creation` and `EntityBase.type_name`.
- `new_subscription(keys=...)` takes a `PushKeys` model as well as a mapping.

- **Typed builders for the six methods that had none.**
  `batch.mail.email.import_()` and `.parse()`, `batch.mail.search_snippet.get()` -
  the `search_snippet` namespace had no methods at all -
  `batch.calendars.calendar_event.parse()`, `batch.contacts.contact_card.parse()`
  and `batch.principals.principal.get_availability()`, which checks the window
  against `maxAvailabilityDuration` before sending. The mail ones answer with new
  models - `EmailImportResponse`, `ParsedEmails` and `SearchSnippetResponse`, in
  `jmap.models.mail.irregular`, with `EmailImport` for building an import - while
  a raw `batch.add` of those methods still answers with the wire dict.
- **A capability with no namespace of its own lends its methods to the one
  holding their data type.** `:calendars:parse`, `:contacts:parse` and
  `:principals:availability` add methods to types other capabilities declare, and
  those methods appear there only when the server advertises them.
- `response_model=` on `Batch.add()` and `jmap.capabilities.parsing.parser_for()`,
  and a `companions` argument on `jmap.api.entity.entity_for()`.

- `Batch.requests()`, which yields a batch's requests one at a time, each carrying
  the creation ids learnt so far - the lazy form of `plan()` that both clients now
  send from.
- `jmap.core.session.check_session_redirects()`, the redirect half of the endpoint
  downgrade check, and a `message` argument on `InsecureEndpointError`.

- `RetryPolicy.max_retry_after` and `RetryPolicy.pause()`, and `retry_after` on
  `RequestError`.
- `JMAPClient.experimental` and `AsyncJMAPClient.experimental`.
- `MethodSpec.filter_type`, for a method that filters another type's properties.
- `jmap.chunking.ChunkedReferenceError`.
- `jmap.push.listener.HEALTHY_CONNECTION_SECONDS` and `PushListener.note_end()`.
- `OAuthClient.discover(resource=...)`, `ResourceMismatchError`, and the
  `fetched_from` and `requested` checks on `ProtectedResourceMetadata.of()`.
- `LoopbackReceiver.expect_state()`.
- `confirm_srv_target` on `JMAPClient.discover` and `candidate_urls`, and
  `jmap.discovery.UnconfirmedSRVTargetError`.
- `jmap.core.ijson.MalformedJSONError`, `jmap.core.session.MalformedSessionError`
  and `jmap.models.base.validation_summary()`.

- `AsyncJMAPClient.discover()`, which the async client lacked.
- `namespace=` on `ChangeStream`, `jmap.sync.type_key()` and
  `jmap.sync.query_key()`.
- `jmap.api.entity.QueryChangeable`, the mixin `query_changes()` now lives on.
- `jmap.auth.metadata.protected_resource_url`, which `jmap.auth` now exports from
  there, and `jmap.auth.credentials.REFRESH_AHEAD_SECONDS`.
- `jmap.batch.MISSING_RESPONSE`, the error type of an unanswered call.

### Changed

- **Releases run the whole CI gate first.** The release workflow checked only
  `pytest`, which is how 1.1.0 was published from a commit whose CI was red on
  formatting and coverage. It now calls the CI workflow - lint, both type checkers,
  every Python with its 100% coverage floor, and the live Stalwart suite - and
  builds only once that passes.
- **The deep-nesting tests stopped assuming how deep the interpreter can parse.**
  Python 3.14 bounds recursion by the stack actually in use, so on a runner with a
  large stack a 100,000-deep document parses where it used to overflow, and CI's
  3.14.7 job failed on exactly that. The contract - a typed `NestingLimitError`,
  never a raw `RecursionError` - is now tested directly, on every interpreter.
- **Every exception class the library defines descends from `JMAPError`.** The
  ones that were `ValueError`s still are; malformed JSON is still a
  `json.JSONDecodeError`.
- **Blob transfers fail as API calls do.** A 401 from the upload or download
  endpoint is an `AuthenticationError` rather than a `RequestError`, and a failed
  connection a `TransportError` rather than httpx's own.
- **`InsufficientScopeError` is raised on a 403 too, for any credential.** Code
  catching a `RequestError` for a 403 whose challenge names `insufficient_scope`
  gets `InsufficientScopeError` instead.
- **`listen()` redials after a 429 or a 5xx** instead of raising `RequestError`.
- **`AddedItem.id` and `AddedItem.index` are required.**
- **Builders refuse arguments they used to pass on.** Anything their signatures
  do not allow now raises `ValidationError` before the call is queued, where it
  went to the server as it was - including a `max_changes` of 0 on `/changes`,
  which RFC 8620 §5.2 has the server reject, and one type name given to
  `Blob/lookup`, which was read as a list of letters. Calling a builder with the
  wrong arguments is still a `TypeError`, now naming the call.
- **`RetryPolicy` refuses a policy that cannot work:** no attempts
  (`max_attempts=0` behaved as 1), a negative or non-finite delay, a
  `multiplier` below 1, or a value of the wrong type. An unknown field raises
  `ValidationError` rather than `TypeError`, as it does on `OAuth2Token` and
  `SRVTarget`.
- **`OAuth2Token` refuses an empty access token** and an expiry that is not a
  finite number; a `SRVTarget` one naming no host or port a URL can carry.
- **`new_subscription` needs a non-empty `device_client_id`,** and an `expires`
  that is a UTCDate; `event_source_url` a `close_after` of `"state"` or `"no"`
  and a `ping` of at least 0.

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

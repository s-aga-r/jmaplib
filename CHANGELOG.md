# Changelog

All notable changes are recorded here. Versions follow [SemVer](https://semver.org),
with one deliberate exception: capabilities marked `experimental=True` track IETF
drafts and are excluded from the compatibility promise. See `jmap.SPEC_REVISIONS`
for exactly which revision of each spec this build implements.

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

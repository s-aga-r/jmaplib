# M0a — Stalwart v0.16.17 bootstrap spike

Run against the native `stalwart-aarch64-apple-darwin` binary from release `v0.16.17`
(2026-08-10), on plain HTTP port 8080. Findings below are **measured**, not from docs.

## Resolved: the two disputed plan items

Both settled — not from the running server, but from the **management schema** that
`stalwart-cli` caches (`~/Library/Caches/stalwart-cli/*/schema-*.json`, 918 KB, 150 object
types). This is the authoritative machine-readable description of the management API.

### 1. `x:Account/set`, not `x:Principal/set`

Both names exist, but they are different things:

| Name | `permissionPrefix` | What it is |
|---|---|---|
| `x:Account` | `sysAccount` | *"Defines a user or group account for authentication and email access."* Variants `x:Account/User` → schema `x:UserAccount`, `x:Account/Group` → `x:GroupAccount`. **This is the user-creation object.** |
| `Principal` (no `x:`) | `jmapPrincipal` | *"Represents an entity that can own or share resources."* The standard **RFC 9670 JMAP Principal** data type. |

There is no `x:Principal`. The source-reading researcher was right; the reviewer conflated
the standard JMAP type with the management object.

`x:UserAccount` fields for provisioning: `name`, `emailAddress`, `domainId` (objectId →
`x:Domain`), `credentials` (objectList → `x:Credential`), `aliases`, `roles`, `permissions`,
`quotas`, `locale` (default `en_US`), `timeZone`, `memberGroupIds`, `memberTenantId`.

### 2. The HTTP listener: create it explicitly

Not fully settled by schema alone, but the schema makes the answer moot — and decides it in
the reviewer's favour operationally. `x:NetworkListener` defaults are
`{"protocol": "smtp", "useTls": true, …}`, so **`useTls` defaults to true**. A CI script must
therefore never rely on a seeded plain-HTTP listener; create one explicitly:

```json
["x:NetworkListener/set", {"create": {"http": {
  "name": "http-test", "bind": ["0.0.0.0:8080"], "protocol": "http", "useTls": false}}}, "c0"]
```

### Bonus: the exact bootstrap payload

`x:Bootstrap` is a **singleton** (id `"singleton"`) with these properties — note `username`
(`emailAddress` format) and `secret`, which create the *permanent* admin:

`blobStore`, `dataStore`, `defaultDomain`, `directory`, `dnsServer`, `generateDkimKeys`,
`inMemoryStore`, `requestTlsCertificate`, `searchStore`, `secret`, `serverHostname`,
`tracer`, `username`.

Defaults: `{"blobStore":{"@type":"Default"}, "dataStore":{"@type":"RocksDb","path":"/var/lib/stalwart/"},
"directory":{"@type":"Internal"}, "dnsServer":{"@type":"Manual"}, "generateDkimKeys":true,
"requestTlsCertificate":true, …}`

And the JMAP limits to pin in CI live on `x:Jmap`: `maxMethodCalls`, `setMaxObjects`,
`getMaxResults`, `queryMaxResults`, `changesMaxResults`, `maxConcurrentRequests`,
`maxConcurrentUploads`, `maxRequestSize`, `maxUploadSize`, `uploadQuota`.

## Confirmed facts

| Claim | Result |
|---|---|
| `/.well-known/jmap` redirects | **307** → `{public_url}/jmap/session`. Fastmail uses 302 — assert the invariant, never the code. |
| Unauthenticated `GET /jmap/session` | **200** with full `capabilities`, empty `accounts`/`username`. Never use it as a readiness probe. |
| `/healthz/live` | **200 even in bootstrap mode.** Also useless as a readiness probe. |
| `/api/*` REST management | **Gone** (404). v0.16 removed it, as documented. |
| `STALWART_PUBLIC_URL` | Applied to `/.well-known/jmap` redirect and session URLs. |
| Core capability limits | `maxSizeUpload` 50000000, `maxConcurrentUpload` 4, `maxSizeRequest` 10000000, `maxConcurrentRequests` 4, `maxCallsInRequest` **16**, `maxObjectsInGet` 500, `maxObjectsInSet` 500. |
| Collations | `i;ascii-numeric`, `i;ascii-casemap`, `i;unicode-casemap`. |
| Auth schemes offered | Two `www-authenticate` headers: `Bearer realm="Stalwart Server", resource_metadata="/.well-known/oauth-protected-resource"` and `Basic realm="Stalwart Server"`. |
| RFC 9728 PRM | Present at `/.well-known/oauth-protected-resource`. Scopes: `openid`, `offline_access`, `urn:ietf:params:oauth:scope:{mail,contacts,calendars}`. |
| RFC 8414 AS metadata | Present. Grants: `authorization_code`, `refresh_token`, `urn:ietf:params:oauth:grant-type:device_code`. PKCE `S256` only. |
| Password grant | **Not supported** — `POST /auth/token` with `grant_type=password` → `{"error":"invalid_grant"}`. |
| Device flow | **Works.** `POST /auth/device` → `device_code`, `user_code`, `expires_in` 1800, `interval` 5. Token polling correctly returns `authorization_pending`. |

Session capabilities advertised in bootstrap mode (16): `core`, `mail`, `calendars`,
`calendars:parse`, `contacts`, `contacts:parse`, `filenode`, `principals`,
`principals:availability`, `submission`, `vacationresponse`, `sieve`, `blob`, `quota`,
`webpush-vapid`, `websocket`. No `mdn`, no `smimeverify` — confirming those ship
fixture-verified only.

## Quirks worth encoding in `compat/`

1. **`STALWART_PUBLIC_URL` is only partially honoured.** The `/.well-known/jmap` redirect
   respects it, but these do **not** — they hardcode `https://localhost`:
   - `capabilities["urn:ietf:params:jmap:websocket"].url` → `wss://localhost/jmap/ws`
   - OAuth `issuer`, `token_endpoint`, `authorization_endpoint`, `device_authorization_endpoint`
   - device flow `verification_uri`

   A strict RFC 8414 client validates the issuer and will reject this. A strict RFC 8887
   client will dial an unreachable `wss://` host. Both need a quirk shim.

2. **`urn:ietf:params:jmap:webpush-vapid` is advertised unconditionally**, with an
   auto-generated `applicationServerKey`. Research had claimed it appears only when a VAPID
   key is configured — not so.

3. **`sieve` carries `{"implementation": "Stalwart v1.0.0"}`** — a non-empty capability value
   the models must tolerate.

## Blocker: headless authentication in bootstrap mode

**`STALWART_RECOVERY_ADMIN` is not read from the process environment.** The server's own
startup banner says to set it *"in the env file"*. Passing it as a process env var — the
form every doc page and the Docker guide shows — is silently ignored: the server still
generates a random temporary password, and the pinned credential never authenticates.

This is the single most important CI finding, and it invalidates the bootstrap step the
plan described.

With the *printed* temporary password (`admin` / random 16 chars), authentication still
failed on every route tried:

| Attempt | Result |
|---|---|
| `Basic` on `GET /jmap/session` | 401 |
| `Basic` on `POST /jmap` (`x:Bootstrap/get`) | 401 |
| `POST /api/auth` `{type:"authDevice", accountName:"admin", ...}` | `{"type":"failure"}` (= invalid credentials) |
| Same, with `admin@macbook-pro.local`, `admin@localhost`, `administrator` | `{"type":"failure"}` |
| `POST /api/auth` `{type:"authCode", ...}` | 401 (this type appears to need an existing session) |

`{"type":"auth"}` returns a *deserialization* error, which confirms `authCode` and
`authDevice` are the only valid discriminators — so the request shape is right and the
credential itself is being rejected.

The login page (`/login`) POSTs to `/api/auth` with `credentials: "same-origin"`, so the
browser flow likely establishes a cookie via `GET /admin` first. That is the untested path.

## `stalwart-cli` does not bypass the blocker

Tested: `stalwart-cli` v1.0.12 (`stalwartlabs/cli`, a separate binary, `.tar.xz` assets — not
`.tar.gz`). It authenticates with **Basic** (`--user`/`--password`) or a Bearer `--api-key`,
and its `--debug` flag prints every request. Against a bootstrap-mode server with the printed
temporary password:

```
[debug] -> GET http://localhost:8080/api/schema
[debug] <- status=401 Unauthorized
warning: failed to refresh schema (authentication failed (HTTP 401)); using cached copy
[debug] -> GET http://localhost:8080/jmap/session
[debug] <- status=401 Unauthorized
error: authentication failed (HTTP 401)
```

So the official tool hits exactly the same wall — which is strong evidence the bootstrap
temporary admin genuinely does not accept Basic auth, rather than a mistake in how it was
being presented. `stalwart-cli` is the right provisioning tool **after** bootstrap, not a way
through it.

Useful side effects of running it: it revealed the `GET /api/schema` endpoint, and its
on-disk schema cache is what resolved both disputed items above.

## The auth flow, from the WebUI itself

Opening `/admin` in a browser resolved *how* the login works, though not the blocker.

`/admin/login` asks for an account name, then redirects to the OAuth authorization
endpoint. The full parameter set, captured from the address bar:

```
/login?response_type=code
      &client_id=stalwart-webui
      &redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fadmin%2Foauth%2Fcallback
      &code_challenge=<S256>&code_challenge_method=S256
      &state=<hex>&login_hint=admin&prompt=login
      &scope=openid+offline_access
```

So the whole thing is **authorization-code + PKCE**, and the client id is
`stalwart-webui`. That page then POSTs to `/api/auth` with:

```json
{"type": "authCode", "accountName": "...", "accountSecret": "...",
 "clientId": "stalwart-webui", "redirectUri": "...", "codeChallenge": "...",
 "codeChallengeMethod": "S256", "state": "...", "scope": "openid offline_access"}
```

A successful response is `{"type": "authenticated", "client_code": "..."}`, which is
exchanged at `/auth/token` with `grant_type=authorization_code` plus the `code_verifier`.

That explained why the earlier attempts failed — they used an unregistered client id. But
replaying the corrected flow headlessly, with `client_id=stalwart-webui`, real PKCE and the
password from the startup banner, **still returns `{"type": "failure"}`**.

## The credential format is wrong in the docs

Reading the source settled two things the documentation gets wrong.

**It is a process environment variable.** `crates/store/src/registry/local.rs:42` is a plain
`std::env::var("STALWART_RECOVERY_ADMIN")`, split on the first `:` and trimmed. The startup
banner's *"in the env file"* just means the usual systemd `EnvironmentFile`, not a mechanism
of its own.

**The part after the colon is a password *hash*, not a password.**
`crates/common/src/auth/authentication.rs:86` passes it straight to `verify_secret_hash`,
and `crates/directory/src/core/secret.rs:135` accepts only:

| Form | Example |
|---|---|
| PHC prefix | `$argon2id$...`, `$2b$...` |
| BSDi crypt | `_...` |
| LDAP-style | `{PLAIN}pw`, `{ARGON2}...`, `{SSHA256}...`, `{CRYPT}...` |

Anything else falls through to an `Unsupported algorithm` error — so
`STALWART_RECOVERY_ADMIN=admin:hunter2`, exactly as every doc page and the banner itself
write it, can never authenticate.

This is confirmed empirically, not just read: with
`STALWART_RECOVERY_ADMIN='admin:{PLAIN}SpikePass123'` the server **stops printing a temporary
password**, which it only does when the pinned credential was accepted. With a bare
plaintext value it prints one, silently ignoring the variable.

## Status: still blocked

Everything tried, all with the credential the server itself printed:

| Route | Result |
|---|---|
| Basic auth on `/jmap/session`, `/jmap`, `/api/schema`, `/api/account` | 401 |
| `/api/auth` `authDevice` (device flow) | `{"type": "failure"}` |
| `/api/auth` `authCode` with correct client id + PKCE | `{"type": "failure"}` |
| `stalwart-cli` v1.0.12 | 401 |
| `STALWART_RECOVERY_ADMIN=admin:plaintext` | silently ignored (wrong format) |
| `STALWART_RECOVERY_ADMIN=admin:{PLAIN}pw`, bootstrap mode | accepted, but Basic auth still 401 |
| `STALWART_RECOVERY_ADMIN=admin:{PLAIN}pw`, recovery mode + `config.json` | Basic auth still 401 |

So the credential is now demonstrably *registered* and still does not authenticate — on
`/jmap/session`, `/jmap`, `/api/schema` or `/api/account`, in either mode, and for account
names `admin`, `admin@localhost` and `admin@<hostname>` alike.

The decisive observation: at `STALWART_RECOVERY_MODE_LOG_LEVEL=trace` the server logs **no
authentication event at all** for these requests. `route_auth_request` is never reached, so
the rejection happens in the HTTP layer above it. That is a different problem from a bad
credential, and it is where the next person should start —
`crates/http/src/auth/` rather than the directory or registry code.

## What to do next

1. **Report the documentation bug upstream.** `username:password` is wrong everywhere it
   appears; it must be `username:{PLAIN}password` or a real hash. That is worth filing
   regardless of the rest, because it silently no-ops today.
2. **Ask upstream how the recovery admin is meant to reach the HTTP auth layer**, quoting the
   trace-level silence above.
3. **Meanwhile**, point the integration suite at an already-bootstrapped server. Nothing in
   `tests/integration/` depends on how the server was created - only on
   `JMAP_TEST_URL`, `JMAP_TEST_USER` and `JMAP_TEST_PASS`.

## Reproduction

```bash
curl -sL -o sw.tar.gz \
  https://github.com/stalwartlabs/stalwart/releases/download/v0.16.17/stalwart-aarch64-apple-darwin.tar.gz
tar xzf sw.tar.gz && mkdir -p etc data
STALWART_PUBLIC_URL=http://localhost:8080 ./stalwart --config "$PWD/etc/config.json"
# password is printed once, to stdout, at startup
```

Captured fixtures: `tests/fixtures/stalwart-0.16.17-{session-bootstrap,as-metadata,prm}.json`

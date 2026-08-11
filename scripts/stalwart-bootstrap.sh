#!/usr/bin/env bash
# Bring a fresh Stalwart v0.16 instance up headlessly and provision test accounts.
#
# STATUS: INCOMPLETE. The bootstrap-mode authentication step below is unsolved —
# see docs/stalwart-spike.md. Everything around it has been verified against a real
# v0.16.17 server; only step 2 is guesswork, and the CI job that calls this script
# runs with continue-on-error until it works.
#
# What the spike established (do not "clean these up", they are all load-bearing):
#   * STALWART_PUBLIC_URL must be set, or the session advertises unreachable
#     https://<hostname> URLs for apiUrl/uploadUrl/downloadUrl/eventSourceUrl.
#   * The container runs as UID/GID 2000 — use named volumes, never bind mounts.
#   * /healthz returns 200 even in bootstrap mode, and an unauthenticated
#     GET /jmap/session returns 200 with empty accounts. Neither proves readiness;
#     poll an *authenticated* session fetch instead.
#   * The management API is JMAP under `urn:stalwart:jmap`, addressed as
#     x:<Object>/get|set|query. The user object is x:Account (variants
#     x:Account/User -> schema x:UserAccount). There is no x:Principal — the
#     unprefixed `Principal` is the standard RFC 9670 JMAP type.
#   * Usernames must be full email addresses in v0.16.
set -euo pipefail

URL="${STALWART_URL:-http://localhost:8080}"
DOMAIN="${STALWART_DOMAIN:-example.com}"
ADMIN_USER="admin@${DOMAIN}"
ADMIN_PASS="${STALWART_ADMIN_PASS:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"
ALICE_PASS="${ALICE_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"
BOB_PASS="${BOB_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"

jmap_call() {
  # $1 = auth header value, $2 = methodCalls JSON array
  curl -sS -m 30 -X POST "${URL}/jmap" \
    -H "Authorization: $1" \
    -H 'Content-Type: application/json' \
    -d "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":$2}"
}

# --- 1. wait for the bootstrap listener ------------------------------------- #
echo "waiting for ${URL} ..."
for _ in $(seq 1 60); do
  if curl -sf -m 2 "${URL}/jmap/session" >/dev/null 2>&1; then break; fi
  sleep 1
done

# --- 2. authenticate against bootstrap mode --------------------------------- #
# UNSOLVED. Passing STALWART_RECOVERY_ADMIN=admin:<pw> as a process env var is
# silently ignored — the server's own banner says it must go "in the env file".
# The randomly generated temporary password it prints instead is rejected by both
# Basic auth and the OAuth device flow (/api/auth returns {"type":"failure"}).
# `stalwart-cli` v1.0.12 hits the same wall, which suggests the bootstrap admin
# genuinely does not accept Basic auth rather than a mistake in presentation.
#
# Most promising route: complete the wizard once in a browser against a throwaway
# instance, capture the x:Bootstrap/set payload from devtools, and replay it here.
echo "ERROR: bootstrap authentication is unsolved; see docs/stalwart-spike.md" >&2
exit 1

# --- 3. complete bootstrap (payload shape confirmed from the schema) --------- #
# x:Bootstrap is a singleton; `username` and `secret` create the PERMANENT admin,
# which — unlike the temporary one — does accept Basic auth.
# shellcheck disable=SC2317  # unreachable until step 2 is solved
bootstrap() {
  jmap_call "$1" "$(cat <<JSON
[["x:Bootstrap/set", {"update": {"singleton": {
  "serverHostname": "${DOMAIN}",
  "defaultDomain": "${DOMAIN}",
  "username": "${ADMIN_USER}",
  "secret": "${ADMIN_PASS}",
  "dataStore": {"@type": "RocksDb", "path": "/var/lib/stalwart/"},
  "directory": {"@type": "Internal"},
  "requestTlsCertificate": false,
  "generateDkimKeys": false,
  "tracer": {"@type": "Stdout"}
}}}, "c0"]]
JSON
)"
}

# --- 4. after `docker restart`: pin limits and create users ------------------ #
# Stalwart's x:Jmap defaults are tight (maxMethodCalls 16, setMaxObjects 500,
# getMaxResults 500, maxConcurrentRequests 4). Pin them so a Stalwart default
# change surfaces as a clear assertion failure, not a mysterious requestTooLarge.
# Also create an explicit plain-HTTP listener: x:NetworkListener defaults to
# useTls=true, so never rely on a seeded one.
# shellcheck disable=SC2317
provision() {
  local auth="Basic $(printf '%s:%s' "${ADMIN_USER}" "${ADMIN_PASS}" | base64)"
  jmap_call "$auth" "$(cat <<JSON
[["x:NetworkListener/set", {"create": {"http": {
    "name": "http-test", "bind": ["0.0.0.0:8080"], "protocol": "http", "useTls": false}}}, "c0"],
 ["x:Jmap/set", {"update": {"singleton": {
    "maxMethodCalls": 16, "setMaxObjects": 500, "getMaxResults": 500,
    "maxConcurrentRequests": 4}}}, "c1"],
 ["x:Account/set", {"create": {
    "alice": {"name": "alice", "emailAddress": "alice@${DOMAIN}",
              "credentials": {"0": {"@type": "Password", "secret": "${ALICE_PASS}"}}},
    "bob":   {"name": "bob",   "emailAddress": "bob@${DOMAIN}",
              "credentials": {"0": {"@type": "Password", "secret": "${BOB_PASS}"}}}}}, "c2"]]
JSON
)"
  {
    echo "ALICE_PASSWORD=${ALICE_PASS}"
    echo "BOB_PASSWORD=${BOB_PASS}"
  } >> "${GITHUB_ENV:-/dev/stdout}"
}

#!/usr/bin/env bash
# Bring a fresh Stalwart v0.16 instance up headlessly and provision test accounts.
#
# The thing that blocked this for a long time, stated plainly so nobody re-derives
# it: **bootstrap mode triggers only when no configuration file exists.** Passing
# a `--config` that points at a real file - even a minimal datastore-only one -
# starts the server in normal mode with an empty database, where there is no
# administrator and never will be, and every request 401s forever. The earlier
# version of this script wrote a config file first, which is precisely why it
# could never authenticate.
#
# The rest of what the (corrected) spike established:
#
#   * `STALWART_RECOVERY_ADMIN=admin:<password>` takes a **plaintext password**
#     and IS read from the process environment. An earlier note here claimed it
#     needed a hash and was ignored unless placed in an env file; both were wrong,
#     and both were concluded from probing the wrong server. Setting it suppresses
#     the randomly generated temporary password, which is what makes this script
#     deterministic - otherwise the password is printed once, to stdout, and must
#     be scraped from the container log.
#   * Bootstrap mode listens on **8080** and serves the JMAP endpoint at `/jmap/`.
#   * The management API is JMAP under the `urn:stalwart:jmap` capability, which is
#     advertised at *account* level only - never in the session-level map.
#   * `x:Bootstrap/get` returns a `singleton` object with the whole server config.
#     `x:Bootstrap/set` applies it, writes the config file to the `--config` path,
#     and returns the **permanent administrator's username and generated secret**
#     in `updated.singleton`. That response is the only time the secret is shown.
#   * The datastore config file it writes is one tagged object; the valid `@type`
#     values are RocksDb, Sqlite, FoundationDb, PostgreSql and MySql.
#   * After the restart the temporary admin no longer applies - the permanent one
#     provisioned above is the credential to use.
#   * `STALWART_PUBLIC_URL` must be set, or the session advertises unreachable
#     `https://<hostname>` URLs for apiUrl/uploadUrl/downloadUrl/eventSourceUrl.
#   * The container runs as UID/GID 2000 - use named volumes, never bind mounts.
#   * An unauthenticated `GET /jmap/session` returns 200 with empty accounts, so it
#     is not a readiness probe. Poll an *authenticated* fetch instead.
set -euo pipefail

URL="${STALWART_URL:-http://localhost:8080}"
DOMAIN="${STALWART_DOMAIN:-example.com}"
CONTAINER="${STALWART_CONTAINER:-stalwart}"
CONFIG_PATH="${STALWART_CONFIG_PATH:-/opt/stalwart/etc/config.json}"

# Pinned so the flow is deterministic. Must match what the container was started
# with; see the CI workflow.
RECOVERY_ADMIN_USER="${STALWART_RECOVERY_ADMIN_USER:-admin}"
RECOVERY_ADMIN_PASS="${STALWART_RECOVERY_ADMIN_PASS:?set STALWART_RECOVERY_ADMIN_PASS}"

ALICE_PASS="${ALICE_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"
BOB_PASS="${BOB_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"

log() { printf '\n=== %s\n' "$*"; }

jmap() {
  # $1 = "user:pass", $2 = methodCalls JSON array
  curl -sS -m 30 -X POST "${URL}/jmap/" \
    -u "$1" \
    -H 'Content-Type: application/json' \
    -d "{\"using\":[\"urn:ietf:params:jmap:core\",\"urn:stalwart:jmap\"],\"methodCalls\":$2}"
}

wait_for_auth() {
  # $1 = "user:pass". An *authenticated* session fetch is the only honest probe:
  # the unauthenticated one answers 200 long before there is anything behind it.
  local creds="$1" i
  for i in $(seq 1 90); do
    if curl -fsS -m 5 -u "$creds" -o /dev/null "${URL}/jmap/session" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for an authenticated session at ${URL}" >&2
  return 1
}

# --- 1. bootstrap mode ------------------------------------------------------ #
log "waiting for bootstrap mode as ${RECOVERY_ADMIN_USER}"
wait_for_auth "${RECOVERY_ADMIN_USER}:${RECOVERY_ADMIN_PASS}"

ACCOUNT_ID=$(curl -fsS -m 10 -u "${RECOVERY_ADMIN_USER}:${RECOVERY_ADMIN_PASS}" "${URL}/jmap/session" \
  | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin)["accounts"])))')
log "recovery account id: ${ACCOUNT_ID}"

# --- 2. apply the server configuration -------------------------------------- #
# `dataStore` is left as the server chose it: the container image already points
# at its own volume, and overriding the path here is how you end up writing to a
# directory the UID 2000 process cannot create.
log "applying bootstrap configuration"
SET_BODY=$(python3 - "$ACCOUNT_ID" "$DOMAIN" <<'PY'
import json, sys
account_id, domain = sys.argv[1], sys.argv[2]
update = {
    "singleton": {
        "serverHostname": "localhost",
        "defaultDomain": domain,
        # Both off: CI has no public DNS, and requesting a certificate or
        # generating DKIM keys makes startup wait on the network.
        "requestTlsCertificate": False,
        "generateDkimKeys": False,
        "tracer": {"@type": "Stdout", "enable": True, "level": "info", "ansi": False},
    }
}
print(json.dumps([["x:Bootstrap/set", {"accountId": account_id, "update": update}, "c0"]]))
PY
)
SET_RESPONSE=$(jmap "${RECOVERY_ADMIN_USER}:${RECOVERY_ADMIN_PASS}" "$SET_BODY")
echo "$SET_RESPONSE" | head -c 400; echo

# The permanent administrator's secret is shown exactly once, here.
eval "$(python3 - <<PY
import json
response = json.loads('''$SET_RESPONSE''')
name, arguments, _ = response["methodResponses"][0]
if name == "error":
    raise SystemExit(f"x:Bootstrap/set failed: {arguments}")
singleton = arguments.get("updated", {}).get("singleton") or {}
if not singleton.get("username") or not singleton.get("secret"):
    raise SystemExit(f"no administrator returned: {arguments}")
print(f'ADMIN_USER={singleton["username"]}')
print(f'ADMIN_PASS={singleton["secret"]}')
PY
)"
log "permanent administrator: ${ADMIN_USER}"

# --- 3. restart into normal mode -------------------------------------------- #
# The config file now exists, so the next start is a normal one. Restarting the
# container is the only way to get there; the running process stays in bootstrap
# mode until it exits.
log "restarting into normal mode"
docker restart "$CONTAINER" >/dev/null
wait_for_auth "${ADMIN_USER}:${ADMIN_PASS}"

ADMIN_ACCOUNT=$(curl -fsS -m 10 -u "${ADMIN_USER}:${ADMIN_PASS}" "${URL}/jmap/session" \
  | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin)["accounts"])))')

# --- 4. provision the test accounts ----------------------------------------- #
# Usernames must be full email addresses in v0.16; bare ones no longer work.
log "creating alice@${DOMAIN} and bob@${DOMAIN}"
CREATE_BODY=$(python3 - "$ADMIN_ACCOUNT" "$DOMAIN" "$ALICE_PASS" "$BOB_PASS" <<'PY'
import json, sys
account_id, domain, alice_pass, bob_pass = sys.argv[1:5]
def user(name, secret):
    return {
        "@type": "User",
        "name": f"{name}@{domain}",
        "description": f"{name} (integration tests)",
        "secrets": [secret],
        "emails": [f"{name}@{domain}"],
    }
create = {"alice": user("alice", alice_pass), "bob": user("bob", bob_pass)}
print(json.dumps([["x:Account/set", {"accountId": account_id, "create": create}, "c0"]]))
PY
)
CREATE_RESPONSE=$(jmap "${ADMIN_USER}:${ADMIN_PASS}" "$CREATE_BODY")
echo "$CREATE_RESPONSE" | head -c 600; echo
python3 - <<PY
import json
response = json.loads('''$CREATE_RESPONSE''')
name, arguments, _ = response["methodResponses"][0]
if name == "error":
    raise SystemExit(f"x:Account/set failed: {arguments}")
if arguments.get("notCreated"):
    raise SystemExit(f"accounts not created: {arguments['notCreated']}")
PY

# --- 5. prove the accounts work --------------------------------------------- #
log "verifying alice can authenticate"
wait_for_auth "alice@${DOMAIN}:${ALICE_PASS}"

# Consumed by the workflow's later steps.
if [ -n "${GITHUB_ENV:-}" ]; then
  {
    echo "ALICE_PASSWORD=${ALICE_PASS}"
    echo "BOB_PASSWORD=${BOB_PASS}"
    echo "STALWART_ADMIN_USER=${ADMIN_USER}"
    echo "STALWART_ADMIN_PASS=${ADMIN_PASS}"
  } >> "$GITHUB_ENV"
fi

log "bootstrap complete"
echo "  alice@${DOMAIN} / ${ALICE_PASS}"
echo "  bob@${DOMAIN} / ${BOB_PASS}"

# Contributing to jmaplib

## Setting up

```console
uv sync --all-extras
uv run pytest                    # integration tests are deselected by default
```

## The checks CI runs

Every change must pass all of these, which is exactly what the CI workflow runs:

```console
uv run ruff check src/ tests/
uv run ruff format --check .
uv run mypy --strict src/ && uv run mypy tests/
uv run pyright src/
uv run pyright --verifytypes jmap --ignoreexternal   # must exit 0, not just score 100%
uv run lint-imports                                   # layering contracts
uv run pytest --cov                                   # must be 100%
```

**Coverage is gated at 100%**, not aspirational. The kernel is pure functions
over plain data, so anything uncovered is either dead code or a branch nobody
thought through - both worth failing the build over.

`lint-imports` enforces the layering: `jmap.core` may not import httpx, asyncio
or anyio, the capabilities may not import the clients, and the layers - client,
api, capabilities, models, core - import only downwards.

`pyright --verifytypes` can fail on an "ambiguous type" while still printing a
100% completeness score, so read its exit status. A public name bound to a
function's result, rather than declared, is the usual cause.

Record anything a user would notice in [CHANGELOG.md](CHANGELOG.md), under
**Unreleased**.

## Running against a live server

Integration tests are deselected by default; `-m integration` selects them. They
need a server and two accounts - the second one exists so the delivery test
(alice sends, bob receives) runs rather than skips, and that assertion is the one
thing the fake server cannot make.

The live suite gates CI: every push bootstraps a real Stalwart v0.16 headlessly
and runs `tests/integration/` against it. See
[docs/stalwart-spike.md](docs/stalwart-spike.md) for the procedure and what had
to be discovered to make it work. Its first green run found five defects no fake
server could have, four of them in the library: a creation reference that never
serialised, a back-reference nested where the wire format cannot express one,
blob upload unable to resolve an account on any real server, and an event source
that could not stay open past five seconds.

### Against a server you already have

No bootstrap is involved:

```console
JMAP_TEST_URL=https://mail.example.com/.well-known/jmap \
JMAP_TEST_USER=alice@example.com JMAP_TEST_PASS=... \
JMAP_TEST_USER2=bob@example.com  JMAP_TEST_PASS2=... \
  uv run pytest -m integration
```

Point `JMAP_TEST_URL` at `/.well-known/jmap` rather than the API URL: that way
the redirect and the survival of the `Authorization` header across it are
exercised for real. Everything the server does not implement skips with a reason
naming the method, so a partial server still produces a useful run.

**A TLS error from an `http://` URL is not a contradiction.** Only the session
document is fetched from the URL you supply; every call after that goes to the
`apiUrl` the *server* advertises, and a server that does not know its own public
URL will advertise `https://<hostname>` regardless of how you reached it. For
Stalwart that means `STALWART_PUBLIC_URL` is unset. Two environment variables
handle a certificate no public CA vouches for, which is the normal case for a
self-hosted server:

| | |
|---|---|
| `JMAP_TEST_CA=/path/to/cert.pem` | Trust this certificate. Verification still happens, so a wrong host or an expired certificate still fails. Prefer this. |
| `JMAP_TEST_INSECURE=1` | Verify nothing. Also disables hostname checking, which is what catches a server advertising `https://localhost` when it means something else. |

### A throwaway Stalwart, as CI raises it

```console
docker run -d --name jmaplib-test -p 18080:8080 \
  -e STALWART_PUBLIC_URL=http://localhost:18080 \
  -e STALWART_RECOVERY_ADMIN="admin:$STALWART_RECOVERY_ADMIN_PASS" \
  -v jmaplib-test-data:/var/lib/stalwart \
  stalwartlabs/stalwart:v0.16.17

STALWART_URL=http://localhost:18080 STALWART_CONTAINER=jmaplib-test \
STALWART_RECOVERY_ADMIN_PASS=... ./scripts/stalwart-bootstrap.sh
```

The script prints alice's and bob's generated passwords at the end. Two details
are load-bearing: **no config file is mounted**, because bootstrap mode triggers
only when the server finds none, and the volume is a **named volume**, because the
image runs as UID/GID 2000 and cannot write a bind mount. `STALWART_PUBLIC_URL`
must match the published port or the session advertises URLs nothing can reach.

> The script completes setup and restarts the container it is given. Point it only
> at a throwaway instance - never at a server holding anything you want to keep.
> Pick a host port nothing else uses, too: Stalwart defaults `socket_reuse_port`
> to true, so a second server binds an occupied port *silently* and the kernel
> then splits requests between the two.

`python -m jmap.testing.conformance <url> --user … --markdown` renders what any
server actually supports, method by method.

## Benchmarks

```console
uv run python -m benchmarks.run                        # all cases
uv run python -m benchmarks.run --profile "e2e query"  # cProfile one of them
```

Payloads are shaped like what a real server returns - a Stalwart-shaped session,
an `Email/get` of a hundred messages with the full RFC 8621 §4.1 property set -
because a benchmark over `{"id": …, "subject": …}` measures nothing that happens
in production. `--save` records a baseline and `--compare` diffs against it,
exiting non-zero past a 5% regression. See [benchmarks/README.md](benchmarks/README.md).

Two things worth knowing before reading a number. The statistic is the **minimum**
of N repeats, not the mean: timing noise is one-sided, so the minimum is both the
truest estimate and the stable one. And the `json.loads (floor)` / `json.dumps
(floor)` cases are the standard library doing the same work without I-JSON
enforcement - the gap to them *is* the cost of conformance, currently about 1.8×
on parse.

Performance claims in this project are expected to come with a measurement. The
one time that rule was skipped, `dumps` got 8% slower while every test still
passed, because `True` fell through the fast scan's type ladder and sent every
payload down the precise-but-slow path. Tests cannot see that - both paths give
the same answer - so `TestTheFastPathIsActuallyTaken` asserts the path directly.

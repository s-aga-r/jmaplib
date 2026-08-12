# Benchmarks

Not part of the test suite and not shipped in the wheel. These measure the paths
that run once per request, over payloads shaped like what a real server returns:
a Stalwart-shaped session, and an `Email/get` carrying a hundred messages with
the full RFC 8621 §4.1 property set.

```console
uv run python -m benchmarks.run                        # everything
uv run python -m benchmarks.run -k ijson               # a subset
uv run python -m benchmarks.run --profile "e2e query"  # cProfile one case
uv run python -m benchmarks.run --import-time          # cold import cost
```

To check a change for regressions, record a baseline first, then compare:

```console
git stash && uv run python -m benchmarks.run --save benchmarks/baseline.json
git stash pop && uv run python -m benchmarks.run --compare benchmarks/baseline.json
```

`--compare` exits non-zero if any case slowed by more than 5%. Results are
gitignored, because a number from one machine says nothing about another - the
comparison is only meaningful within a single run of the pair.

## Reading the numbers

The reported statistic is the **minimum** of N repeats, not the mean. Timing
noise is one-sided - another process stealing the CPU can only make a run slower
- so the minimum is both the closest estimate of the true cost and far the more
stable across runs, which is what makes a 5% regression visible at all.

Two cases exist only as reference points: `json.loads (floor)` and
`json.dumps (floor)` are the stdlib doing the same work with no I-JSON
enforcement. They are what the `ijson` cases should be read against, since the
gap between them *is* the cost of conformance.

## What these do and do not measure

The `e2e` cases drive a whole batch through `FakeJMAPServer`, which means they
include the fake **serialising its own response** - work a real deployment does
on another machine, in another language. They are a useful proxy for the fake's
own speed, which matters because it ships as a pytest plugin and runs in
downstream test suites. They overstate client cost.

For production client cost, read the layers directly: `ijson.loads` for the
response, `ijson.dumps` for the request, and the `models` group for validation.

Nothing here measures the network, and no benchmark of a JMAP client should
pretend to. The point of these is that everything above is what remains when the
network is taken away, and it is the only part the library controls.

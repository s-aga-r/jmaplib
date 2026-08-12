"""Benchmark runner.

    uv run python -m benchmarks.run                     # run everything
    uv run python -m benchmarks.run -k ijson            # only matching cases
    uv run python -m benchmarks.run --save base.json    # record a baseline
    uv run python -m benchmarks.run --compare base.json # diff against one
    uv run python -m benchmarks.run --profile <case>    # cProfile one case
    uv run python -m benchmarks.run --import-time       # cold import cost

Reported statistic is the **minimum** of N repeats, not the mean. Timing noise on
a shared machine is one-sided - something else stealing the CPU can only make a
run slower - so the minimum is the closest estimate of the true cost, and it is
far more stable across runs, which is what makes a 5% regression visible.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmarks.cases import REGISTRY, groups

#: Target seconds per case. Iterations are chosen to hit roughly this, so a
#: microsecond case still gets enough repetitions to be measurable.
TARGET_SECONDS = 0.4
WARMUP = 3


def measure(func: Any, *, repeats: int = 5) -> tuple[float, int]:
    """Return (seconds per call, iterations used)."""
    for _ in range(WARMUP):
        func()

    # Calibrate: grow the loop until one batch takes a measurable slice.
    iterations = 1
    while True:
        start = time.perf_counter()
        for _ in range(iterations):
            func()
        elapsed = time.perf_counter() - start
        if elapsed > TARGET_SECONDS / repeats or iterations >= 1_000_000:
            break
        wanted = iterations * TARGET_SECONDS / repeats / max(elapsed, 1e-9)
        iterations = max(iterations * 2, int(wanted))

    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(iterations):
            func()
        best = min(best, time.perf_counter() - start)
    return best / iterations, iterations


def human(seconds: float) -> str:
    if seconds >= 1e-3:
        return f"{seconds * 1e3:8.3f} ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:8.3f} µs"
    return f"{seconds * 1e9:8.1f} ns"


def run(selected: list[str], repeats: int) -> dict[str, float]:
    results: dict[str, float] = {}
    width = max(len(name) for name in selected)
    for group in groups():
        names = [n for n in selected if REGISTRY[n][0] == group]
        if not names:
            continue
        print(f"\n\033[1m{group}\033[0m")
        for name in names:
            per_call, iterations = measure(REGISTRY[name][1], repeats=repeats)
            results[name] = per_call
            print(f"  {name:<{width}}  {human(per_call)}   (n={iterations})")
    return results


def compare(results: dict[str, float], baseline_path: Path) -> int:
    baseline: dict[str, float] = json.loads(baseline_path.read_text())["results"]
    width = max(len(name) for name in results)
    print(f"\n\033[1mvs {baseline_path.name}\033[0m")
    regressions = 0
    for name, current in results.items():
        before = baseline.get(name)
        if before is None:
            print(f"  {name:<{width}}  {human(current)}   \033[2m(new)\033[0m")
            continue
        change = (current - before) / before * 100
        if change < -5:
            colour, mark = "\033[32m", "faster"
        elif change > 5:
            colour, mark = "\033[31m", "SLOWER"
            regressions += 1
        else:
            colour, mark = "\033[2m", "same"
        speedup = before / current if current else float("inf")
        detail = f"{speedup:.2f}x" if abs(change) > 5 else ""
        print(
            f"  {name:<{width}}  {human(before)} -> {human(current)}  "
            f"{colour}{change:+7.1f}%  {mark} {detail}\033[0m"
        )
    return regressions


def profile(name: str, seconds: float) -> None:
    func = REGISTRY[name][1]
    per_call, _ = measure(func, repeats=1)
    iterations = max(1, int(seconds / per_call))
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(iterations):
        func()
    profiler.disable()
    print(f"\n{name}  ({iterations} iterations)\n")
    pstats.Stats(profiler).sort_stats("tottime").print_stats(25)


def import_time(repeats: int = 5) -> None:
    """Cold-import cost, measured in a fresh interpreter each time."""
    for module in ("jmap", "jmap.client", "jmap.defaults"):
        best = float("inf")
        for _ in range(repeats):
            start = time.perf_counter()
            subprocess.run(
                [sys.executable, "-c", f"import {module}"], check=True, capture_output=True
            )
            best = min(best, time.perf_counter() - start)
        bare = float("inf")
        for _ in range(repeats):
            start = time.perf_counter()
            subprocess.run([sys.executable, "-c", "pass"], check=True, capture_output=True)
            bare = min(bare, time.perf_counter() - start)
        print(f"  import {module:<16} {human(best - bare)}  (interpreter startup excluded)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.run")
    parser.add_argument("-k", dest="filter", default="", help="substring filter over case names")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--save", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--profile", metavar="CASE")
    parser.add_argument("--profile-seconds", type=float, default=2.0)
    parser.add_argument("--import-time", action="store_true")
    args = parser.parse_args()

    if args.import_time:
        print("\n\033[1mimport\033[0m")
        import_time()
        return 0

    if args.profile:
        matches = [n for n in REGISTRY if args.profile in n]
        if not matches:
            parser.error(f"no case matching {args.profile!r}")
        profile(matches[0], args.profile_seconds)
        return 0

    selected = [name for name in REGISTRY if args.filter in name]
    if not selected:
        parser.error(f"no case matching {args.filter!r}")

    results = run(selected, args.repeats)

    if args.save:
        args.save.write_text(
            json.dumps({"python": sys.version, "results": results}, indent=2) + "\n"
        )
        print(f"\nsaved -> {args.save}")

    if args.compare:
        return 1 if compare(results, args.compare) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Timing harness for the Winnow benchmark suite.

Stdlib only, on purpose — same rule requirements-dev.txt follows (nothing
here may ever become a runtime dependency on the airgapped target), and a
perf suite that needs its own toolchain is a perf suite nobody runs.

A benchmark does its setup and returns the callable to measure:

    @benchmark("views/build_view.unfiltered")
    def _(env):
        fx = env.case()
        spec = {"source_id": fx.main, "filters": [], "sort": []}
        held = {}

        def run():
            held["v"] = fx.store.build_view(fx.main, spec)

        return Timed(run, after=lambda: fx.store.close_view(held["v"]["view_id"]),
                     items=fx.rows)

Everything outside `run` is excluded from the measurement — including the
`before`/`after` hooks, which run once per repetition, so an operation that
needs fresh state (an ingest, a compact) can still be measured repeatedly
without its setup polluting the number.

A benchmark may also return None and record only `env.metric(...)` values,
for things that aren't a duration at all (case-file bytes, index size).
"""

from __future__ import annotations

import gc
import os
import platform
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

# Row counts per size tier. The suite is meaningful at every tier — the
# tier only decides how long you wait and how far above the noise floor
# the differences land. `quick` is the "did I just break something
# obvious" run; `standard` is what a baseline should be taken at.
SIZES = {"quick": 20_000, "standard": 200_000, "large": 1_200_000}
SIZE_ORDER = ["quick", "standard", "large"]

# Adaptive repetition: aim to spend about this long on each benchmark,
# clamped to a sane rep count either way. A 2ms operation gets 25 samples,
# a 4s ingest gets 3.
TARGET_TOTAL_S = 1.0
MIN_REPS = 3
MAX_REPS = 25


@dataclass
class Metric:
    """A non-time measurement (bytes on disk, rows matched, ...). Compared
    against the baseline the same way a duration is, but it needs to say
    which direction is good and how big a change is worth reporting."""

    value: float
    unit: str = ""
    higher_is_better: bool = False
    floor: float = 0.0  # absolute change below this is never significant


@dataclass
class Timed:
    """What a benchmark function returns: the thing to time, plus the
    untimed hooks around it."""

    run: Callable[[], object]
    before: Callable[[], object] | None = None
    after: Callable[[], object] | None = None
    items: int | None = None          # for a rows/s figure in the report
    item_unit: str = "rows"
    reps: int | None = None           # override the adaptive rep count
    warmup: int = 1


@dataclass
class BenchDef:
    name: str
    fn: Callable
    group: str
    reps: int | None = None
    min_size: str = "quick"
    note: str = ""


REGISTRY: list[BenchDef] = []


def benchmark(name: str, *, group: str | None = None, reps: int | None = None,
              min_size: str = "quick", note: str = ""):
    """Register a benchmark. `group` defaults to the part of the name before
    the first '/'. `min_size` skips the benchmark on tiers below it — for
    things that measure nothing useful on 20k rows."""

    def deco(fn):
        REGISTRY.append(BenchDef(
            name=name,
            fn=fn,
            group=group or name.split("/")[0],
            reps=reps,
            min_size=min_size,
            note=note,
        ))
        return fn

    return deco


# --------------------------------------------------------------------------
# measurement


def _measure_once(spec: Timed) -> float:
    """One repetition. GC is collected before and disabled during, so a
    collection triggered by an earlier rep's garbage can't land inside this
    one's window — that's the single largest source of variance in a suite
    that allocates as heavily as CSV export or row paging does."""
    if spec.before:
        spec.before()
    gc.collect()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        t0 = time.perf_counter()
        spec.run()
        elapsed = time.perf_counter() - t0
    finally:
        if gc_was_enabled:
            gc.enable()
    if spec.after:
        spec.after()
    return elapsed


def _stats(samples: list[float]) -> dict:
    return {
        "min": min(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "max": max(samples),
    }


def run_benchmark(bd: BenchDef, env, reps_override: int | None = None) -> dict:
    """Run one benchmark to completion. Never raises: a benchmark that blows
    up is recorded with its error and the suite carries on, because half a
    perf report is still worth having."""
    rec = {"name": bd.name, "group": bd.group, "note": bd.note}
    env._metrics = {}
    try:
        spec = bd.fn(env)
    except Exception as e:  # noqa: BLE001 — reported, not swallowed
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec

    rec["metrics"] = {
        k: {"value": m.value, "unit": m.unit,
            "higher_is_better": m.higher_is_better, "floor": m.floor}
        for k, m in env._metrics.items()
    }

    if spec is None:
        return rec  # metrics-only benchmark; nothing timed

    if callable(spec):
        spec = Timed(spec)

    try:
        # Always at least one warmup pass — it primes SQLite's page cache and
        # any lazily-built state, and its duration is what the adaptive rep
        # count below is derived from.
        warm = _measure_once(spec)
        for _ in range(max(0, spec.warmup - 1)):
            warm = _measure_once(spec)

        reps = reps_override or spec.reps or bd.reps
        if reps is None:
            reps = int(TARGET_TOTAL_S / warm) if warm > 0 else MAX_REPS
            reps = max(MIN_REPS, min(MAX_REPS, reps))

        samples = [_measure_once(spec) for _ in range(reps)]
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec

    rec["reps"] = len(samples)
    rec["samples"] = samples
    rec["seconds"] = _stats(samples)
    if spec.items:
        rec["items"] = spec.items
        rec["item_unit"] = spec.item_unit
    # Metrics may also be recorded from inside run(); pick them up again.
    rec["metrics"] = {
        k: {"value": m.value, "unit": m.unit,
            "higher_is_better": m.higher_is_better, "floor": m.floor}
        for k, m in env._metrics.items()
    }
    return rec


# --------------------------------------------------------------------------
# environment fingerprint
#
# Comparing a run on one machine against a baseline from another is
# meaningless, and doing it by accident is easy. Every result file carries
# enough of a fingerprint for the comparison layer to say so out loud.


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment(size: str) -> dict:
    return {
        "host": socket.gethostname(),
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count() or 0,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "size": size,
        "rows": SIZES[size],
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# Fields that must match for a comparison to mean anything. `size` is in
# here because a 20k-row run and a 200k-row run are different workloads,
# not a fast machine and a slow one.
COMPARABLE_FIELDS = ["host", "cpu", "python", "sqlite", "size"]


def env_mismatches(a: dict, b: dict) -> list[str]:
    return [f for f in COMPARABLE_FIELDS if a.get(f) != b.get(f)]

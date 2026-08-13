"""Standalone concurrency probe — NOT part of the `bench` package/harness.

Proves (or disproves) the claim behind the reader-connection prototype:
that a long build_view no longer stalls a concurrent fetch_rows on a
*different* view. The `bench/` harness's `@benchmark` decorator times one
callable in isolation, repeated — it has no notion of "how long does B take
while A is running," which is what this needs, so it's a separate script
rather than a suite module.

Usage:
    python3 probe_concurrency.py --store-dir <dir containing store.py>

Run twice (once against a baseline worktree's store.py, once against the
working tree's) to compare before/after, e.g.:

    python3 bench/probe_concurrency.py --store-dir /tmp/winnow-baseline-store
    python3 bench/probe_concurrency.py --store-dir .
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

SEED = 12345
ROWS = 400_000


def load_store_module(store_dir: str):
    path = Path(store_dir) / "store.py"
    spec = importlib.util.spec_from_file_location("store_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["store_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_csv(path: str, rows: int) -> None:
    rng = random.Random(SEED)
    hosts = [f"host-{i:03d}" for i in range(200)]
    procs = ["svchost.exe", "powershell.exe", "explorer.exe", "cmd.exe", "chrome.exe"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Host", "Process", "EventId", "Message"])
        for i in range(rows):
            w.writerow([
                f"2026-01-{1 + i % 28:02d} {i % 24:02d}:{i % 60:02d}:{i % 60:02d}",
                rng.choice(hosts),
                rng.choice(procs),
                4624 + (i % 40),
                f"payload line {i} " + ("x" * 40),
            ])


def build_case(mod, tmpdir: str, rows: int):
    csv_path = str(Path(tmpdir) / "events.csv")
    make_csv(csv_path, rows)
    # A second, small, unrelated source for the "idle" view to page —
    # build_view evicts every other root view *for the same source_id* by
    # design (opening a new view drops the previous one), so the contended
    # build_view on the big source must not target the same table the probe
    # is concurrently paging, or its view table would vanish mid-probe.
    small_csv_path = str(Path(tmpdir) / "small.csv")
    make_csv(small_csv_path, 2000)

    case_path = str(Path(tmpdir) / "case.db")
    store = mod.Store(case_path, default_tags=[])
    # Background FTS/index builds would steal CPU from the timing below —
    # same rationale as bench/fixtures.py's _quiet_background.
    store._ensure_fts_building = lambda *a, **k: None
    store._ensure_column_index_building = lambda *a, **k: None
    big_source_id = store.ingest_csv(csv_path, name="events.csv")["id"]
    small_source_id = store.ingest_csv(small_csv_path, name="small.csv")["id"]
    return store, big_source_id, small_source_id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-dir", required=True)
    ap.add_argument("--rows", type=int, default=ROWS)
    args = ap.parse_args()

    mod = load_store_module(args.store_dir)
    tmpdir = tempfile.mkdtemp(prefix="winnow-probe-")
    try:
        store, source_id, small_source_id = build_case(mod, tmpdir, args.rows)

        # A cheap view on the small, unrelated source to page from
        # concurrently — unfiltered/unsorted would take the root_virtual
        # fast path (out of scope for this prototype), so sort it to force
        # real materialisation, same as any analyst clicking a column
        # header. Deliberately a different source_id than the contended
        # build_view below (see build_case's comment on view eviction).
        idle_view = store.build_view(small_source_id, {"sort": [{"column": "Host"}]})

        # Baseline: fetch_rows latency with nothing else running.
        baseline = []
        for i in range(20):
            t0 = time.perf_counter()
            store.fetch_rows(idle_view["view_id"], (i % 8) * 200, 200)
            baseline.append(time.perf_counter() - t0)

        # The contended op: a full LIKE scan + sort with no index — the
        # long, unchunked build_view CLAUDE.md/the plan file flags as
        # holding the writer lock for its whole duration.
        big_spec = {
            "filters": [{"column": "Message", "op": "contains", "value": "payload"}],
            "sort": [{"column": "Timestamp"}],
        }

        during: list[float] = []
        stop = threading.Event()
        build_done = threading.Event()
        build_elapsed = {}

        def run_build():
            t0 = time.perf_counter()
            store.build_view(source_id, big_spec)
            build_elapsed["s"] = time.perf_counter() - t0
            build_done.set()

        def run_fetches():
            i = 0
            while not build_done.is_set():
                t0 = time.perf_counter()
                store.fetch_rows(idle_view["view_id"], (i % 8) * 200, 200)
                during.append(time.perf_counter() - t0)
                i += 1

        t_build = threading.Thread(target=run_build)
        t_fetch = threading.Thread(target=run_fetches)
        t_build.start()
        time.sleep(0.01)  # let the build actually acquire the writer lock first
        t_fetch.start()
        t_build.join()
        t_fetch.join(timeout=5)

        def stats(xs):
            xs_ms = [x * 1000 for x in xs]
            return {
                "n": len(xs_ms),
                "mean_ms": round(statistics.mean(xs_ms), 2),
                "max_ms": round(max(xs_ms), 2),
                "p95_ms": round(sorted(xs_ms)[int(len(xs_ms) * 0.95)], 2) if len(xs_ms) > 1 else xs_ms[0],
            }

        print(f"--- {args.store_dir} ---")
        print("rows:", args.rows)
        print("build_view (contended op) elapsed: %.1f ms" % (build_elapsed["s"] * 1000))
        print("fetch_rows baseline (idle):        ", stats(baseline))
        print("fetch_rows during build_view:      ", stats(during), f"(n={len(during)} calls completed during the build)")
    finally:
        try:
            store.close()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()

"""Deterministic input data and the shared case file the suite runs against.

Two different caching rules here, and the difference matters:

* **Generated inputs** (CSV / JSONL / an external SQLite file) are cached on
  disk under `bench/.cache`. They're pure data — nothing about them depends
  on store.py — so reusing them across code changes, across `--vs-ref`
  worktrees, and across runs is safe, and it keeps a 1.2M-row run from
  spending a minute in `csv.writer` before it measures anything.
* **The case file** is rebuilt from those inputs on every run, never cached.
  Building it *is* what several benchmarks measure, and a cached case.db
  would silently be the output of whichever version of store.py happened to
  build it first.

Data is generated from a fixed seed, so row counts, value cardinalities and
the selectivity of every benchmark's filter are identical run to run. That's
load-bearing: a benchmark whose filter matched 4% of rows yesterday and 7%
today is a benchmark that reports a regression that isn't one.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import DEFAULT_TAGS, Store  # noqa: E402

# Bump when a generator changes shape — it's part of every cached file's
# name, so an old cache entry can never be silently reused against new
# generator code.
GEN_VERSION = 1
SEED = 20260813

PROCS = ["svchost.exe", "powershell.exe", "cmd.exe", "explorer.exe",
         "rundll32.exe", "lsass.exe", "chrome.exe", "wmiprvse.exe"]
USERS = ["ACME\\jsmith", "ACME\\admin", "NT AUTHORITY\\SYSTEM", "ACME\\bkupsvc"]
HOSTS = ["WKSTN-014", "WKSTN-002", "SRV-DC01", "SRV-FS02"]
CHANNELS = ["Security", "Sysmon", "System"]
EVENT_IDS = [4624, 4625, 4688, 1, 7045, 4104]

COLUMNS = ["Timestamp", "EventId", "Channel", "Computer", "User",
           "Process", "CommandLine", "SourceIp", "Bytes", "Details"]

# The three search shapes CLAUDE.md's FTS notes are written around. Under
# `detail=none` query time scales with *result count*, so a term matching
# one row in a thousand and a term matching every row are genuinely
# different benchmarks, and a change that trades one off against the other
# has to be visible as two numbers moving in opposite directions.
TERM_RARE = "base64"       # ~0.1% of rows (the IOC case)
TERM_COMMON = "netsvcs"    # every row (the worst case for detail=none)
TERM_MISS = "zzqqxxvv"     # no rows
TERM_EXCLUDE = "chrome.exe"  # ~12% of rows, for advanced-mode NOT terms

# Filter targets, fixed so selectivity never drifts.
EQ_COLUMN, EQ_VALUE = "EventId", "4624"          # ~1 in 6 rows
GROUP_COLUMN = "Process"                          # 8 distinct
GROUP_HIGH_CARD_COLUMN = "CommandLine"            # distinct per row
DATETIME_COLUMN = "Timestamp"

T0 = datetime.datetime(2026, 3, 14, 8, 0, 0)
STEP_SECONDS = 0.37


def cache_dir() -> Path:
    """Shared with `--vs-ref` subprocess runs via BENCH_CACHE_DIR so the two
    sides of a comparison are measured against byte-identical inputs."""
    d = Path(os.environ.get("BENCH_CACHE_DIR") or Path(__file__).resolve().parent / ".cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cached(name: str, build) -> str:
    """Build `name` in the cache if it isn't there. Written to a temp name
    and renamed into place, so an interrupted run can't leave a truncated
    file that later runs would happily reuse."""
    path = cache_dir() / name
    if path.exists():
        return str(path)
    tmp = path.with_suffix(path.suffix + ".partial")
    build(str(tmp))
    os.replace(tmp, path)
    return str(path)


def _row(rng: random.Random, i: int) -> list:
    t = T0 + datetime.timedelta(seconds=i * STEP_SECONDS)
    proc = rng.choice(PROCS)
    return [
        t.strftime("%Y-%m-%d %H:%M:%S"),
        rng.choice(EVENT_IDS),
        rng.choice(CHANNELS),
        rng.choice(HOSTS),
        rng.choice(USERS),
        proc,
        # "netsvcs" on every row is TERM_COMMON; the trailing id makes the
        # column distinct per row, which is what GROUP_HIGH_CARD_COLUMN
        # needs to be a real worst case for group_summary.
        f"C:\\Windows\\System32\\{proc} -k netsvcs -id {i}",
        f"10.0.{rng.randint(0, 5)}.{rng.randint(1, 254)}",
        rng.randint(0, 900_000),
        "Routine activity" if rng.random() > 0.001
        else "encoded command detected base64 IEX",
    ]


def events_csv(rows: int, seed: int = SEED) -> str:
    """The main event-log shaped fixture: 10 columns, one inferred datetime
    column, one inferred numeric column, mixed cardinalities."""

    def build(path: str) -> None:
        rng = random.Random(seed)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for i in range(rows):
                w.writerow(_row(rng, i))

    return _cached(f"events-v{GEN_VERSION}-{rows}-{seed}.csv", build)


def events_jsonl(rows: int, seed: int = SEED) -> str:
    """JSONL with one nested object, for the flatten path. Kept to the same
    field names as the CSV so the two ingest numbers are comparable per row
    (JSON's two full passes are the difference, not the shape of the data)."""

    def build(path: str) -> None:
        rng = random.Random(seed + 1)
        with open(path, "w", encoding="utf-8") as f:
            for i in range(rows):
                r = _row(rng, i)
                rec = {
                    "Timestamp": r[0], "EventId": r[1], "Channel": r[2],
                    "Computer": r[3],
                    "user": {"name": r[4], "domain": r[4].split("\\")[0]},
                    "Process": r[5], "CommandLine": r[6],
                    "SourceIp": r[7], "Bytes": r[8], "Details": r[9],
                    "tags": ["a", "b"],
                }
                f.write(json.dumps(rec) + "\n")

    return _cached(f"events-v{GEN_VERSION}-{rows}-{seed}.jsonl", build)


def chromium_sqlite(rows: int, seed: int = SEED) -> str:
    """An external SQLite file shaped like Chromium's History — including
    WebKit-epoch timestamp columns, so the ingest benchmark exercises the
    per-column `_webkit_to_iso` conversion rather than a plain copy."""

    def build(path: str) -> None:
        rng = random.Random(seed + 2)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
                    "visit_count INTEGER, typed_count INTEGER, last_visit_time INTEGER)")
        base = 13_350_000_000_000_000  # ~2024 in WebKit microseconds
        con.executemany(
            "INSERT INTO urls VALUES (?,?,?,?,?,?)",
            [(i, f"https://example{rng.randint(0, 500)}.test/path/{i}",
              f"Page title {i}", rng.randint(1, 50), rng.randint(0, 5),
              base + i * 1_000_000) for i in range(rows)],
        )
        con.commit()
        con.close()

    return _cached(f"chromium-v{GEN_VERSION}-{rows}-{seed}.db", build)


# --------------------------------------------------------------------------
# the shared case


@dataclass
class CaseFixture:
    """One built case file plus the ids every suite needs to address it.

    Layout, and why each piece exists:
      main      full row count, trigram FTS built  — the indexed paths
      nofts     same row count, no FTS index       — the LIKE-fallback paths
      members   3 x rows/4, identical headers      — merge members
      merge_id  a merge over `members`             — the UNION ALL fan-out
    """

    store: Store
    path: str
    tmpdir: str
    rows: int
    main: int
    nofts: int
    members: list[int]
    merge_id: int
    tag_id: int              # tags ~1% of main, part of the built state
    bench_tag_id: int        # unused by setup; for benchmarks that mutate
    tagged_rids: list[int]
    csv_path: str
    pristine_path: str       # a main-source-only case, no FTS, to copy per rep
    orig_ensure_fts: object = None
    orig_ensure_column_index: object = None

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001 — teardown, never fatal
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def fresh_case(self) -> tuple[Store, str]:
        """A brand-new empty case in the fixture's tmpdir — for the ingest
        benchmarks, which need somewhere to write that nothing else reads."""
        path = os.path.join(self.tmpdir, f"fresh-{next(_counter)}.db")
        return Store(path, default_tags=DEFAULT_TAGS), path

    def copy_pristine(self) -> tuple[Store, str]:
        """A copy of the main-source-only, FTS-less case. Copying a built
        file is far cheaper than re-ingesting, which is what makes it
        practical to measure build_fts/compact over several repetitions."""
        path = os.path.join(self.tmpdir, f"copy-{next(_counter)}.db")
        shutil.copyfile(self.pristine_path, path)
        return Store(path, default_tags=DEFAULT_TAGS), path


def _counter_gen():
    i = 0
    while True:
        i += 1
        yield i


_counter = _counter_gen()


def _quiet_background(store: Store) -> tuple[object, object]:
    """Stop the store from kicking off background FTS / column-index builds.

    Both are fire-and-forget threads that a Contains search or a sargable
    filter starts behind the caller's back. That's exactly right in the app
    and exactly wrong in a benchmark: the LIKE-fallback benchmark would
    build the very index it exists to measure the absence of, and every
    benchmark that ran alongside the build would be timed against a machine
    with a busy core. Originals are handed back so the benchmarks that
    *want* an index can still ask for one.
    """
    orig_fts = store._ensure_fts_building
    orig_idx = store._ensure_column_index_building
    store._ensure_fts_building = lambda *a, **k: None
    store._ensure_column_index_building = lambda *a, **k: None
    return orig_fts, orig_idx


def build_case(rows: int, *, verbose: bool = True) -> CaseFixture:
    """Build the shared case. Ingest here is untimed setup — the ingest
    benchmarks build their own throwaway cases."""
    tmpdir = tempfile.mkdtemp(prefix="tl-bench-")
    csv_path = events_csv(rows)
    path = os.path.join(tmpdir, "bench.db")

    def say(msg: str) -> None:
        if verbose:
            print(f"  {msg}", flush=True)

    store = Store(path, default_tags=DEFAULT_TAGS)

    say(f"ingesting main source ({rows:,} rows)")
    main = store.ingest_csv(csv_path, name="events.csv", build_fts=False)["id"]

    # A main-source-only snapshot, taken before anything else is added, is
    # what the build_fts and compact benchmarks copy per repetition.
    pristine = os.path.join(tmpdir, "pristine.db")
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(path, pristine)

    say("ingesting no-FTS source (LIKE-fallback comparisons)")
    nofts = store.ingest_csv(csv_path, name="events-nofts.csv", build_fts=False)["id"]

    member_rows = max(1, rows // 4)
    member_csv = events_csv(member_rows, seed=SEED + 9)
    members = []
    for n in range(3):
        say(f"ingesting merge member {n + 1}/3 ({member_rows:,} rows)")
        members.append(store.ingest_csv(member_csv, name=f"member{n}.csv", build_fts=False)["id"])
    merge_id = store.create_merge("bench merge", members)["id"]

    say("building trigram FTS index on main source")
    store.build_fts(main)

    # ~1% of main tagged, plus a slice of one member so the timeline and the
    # tagged-export benchmarks have cross-source rows to union.
    tag_id = store.list_tags()[0]["id"]
    tagged_rids = list(range(1, rows + 1, 100))
    say(f"tagging {len(tagged_rids):,} rows")
    store.set_tags(main, tagged_rids, tag_id, True)
    store.set_tags(members[0], list(range(1, member_rows + 1, 100)), tag_id, True)
    bench_tag_id = store.upsert_tag(None, "bench-scratch", "#ff00ff", None)["id"]

    store.wait_for_fts(main)
    store.wait_for_fts_maintenance(timeout=30)

    orig_fts, orig_idx = _quiet_background(store)

    return CaseFixture(
        store=store, path=path, tmpdir=tmpdir, rows=rows, main=main, nofts=nofts,
        members=members, merge_id=merge_id, tag_id=tag_id, bench_tag_id=bench_tag_id,
        tagged_rids=tagged_rids, csv_path=csv_path, pristine_path=pristine,
        orig_ensure_fts=orig_fts, orig_ensure_column_index=orig_idx,
    )


def ensure_column_index(fx: CaseFixture, source_id: int, column: str) -> None:
    """Create (and wait for) the lazy B-tree index a sargable filter would
    have created on its own, using the store's real code path rather than a
    hand-written CREATE INDEX — so the indexed benchmark is measuring the
    index the app actually builds, name and all."""
    src = fx.store.get_source(source_id)
    fx.orig_ensure_column_index(source_id, column, src["table_name"])
    fx.store.wait_for_column_index(source_id, column, timeout=300)


def time_window(rows: int, fraction: float = 0.1) -> tuple[str, str]:
    """A start/end pair covering `fraction` of the generated time span —
    fixed selectivity regardless of row count."""
    start = T0 + datetime.timedelta(seconds=rows * STEP_SECONDS * 0.2)
    end = start + datetime.timedelta(seconds=rows * STEP_SECONDS * fraction)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")

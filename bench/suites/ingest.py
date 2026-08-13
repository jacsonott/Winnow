"""Import throughput — the front door of the whole tool.

Every benchmark here runs against a throwaway case built in the `before`
hook, so each repetition starts from an empty database. That hook is
untimed; only the ingest call itself is measured.

Backlog item #1 (the DuckDB ingest path) is measured against
`ingest/csv` — if that lands, this is the number that has to move.
"""

from __future__ import annotations

import os

from ..fixtures import chromium_sqlite, events_csv, events_jsonl
from ..harness import Timed, benchmark


def _throwaway(fx):
    """A (setup, teardown) pair yielding a fresh empty case per repetition."""
    held = {}

    def before():
        held["store"], held["path"] = fx.fresh_case()

    def after():
        held["store"].close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(held["path"] + suffix)
            except OSError:
                pass

    return held, before, after


@benchmark("ingest/csv", reps=3,
           note="single-threaded csv module; the DuckDB backlog item's target")
def _(env):
    fx = env.case()
    path = events_csv(env.rows)
    held, before, after = _throwaway(fx)

    def run():
        held["store"].ingest_csv(path, name="bench.csv", build_fts=False)

    return Timed(run, before=before, after=after, items=env.rows)


@benchmark("ingest/csv_no_header", reps=3,
           note="has_header=False — every row goes through the pad/trim path")
def _(env):
    fx = env.case()
    path = events_csv(env.rows)
    held, before, after = _throwaway(fx)

    def run():
        held["store"].ingest_csv(path, name="bench.csv", build_fts=False,
                                 has_header=False)

    return Timed(run, before=before, after=after, items=env.rows)


@benchmark("ingest/build_fts", reps=3,
           note="trigram index build; copies a pre-ingested case per rep")
def _(env):
    fx = env.case()
    held = {}

    def before():
        held["store"], held["path"] = fx.copy_pristine()
        held["sid"] = held["store"].list_sources()[0]["id"]

    def after():
        held["store"].close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(held["path"] + suffix)
            except OSError:
                pass

    def run():
        held["store"].build_fts(held["sid"])

    return Timed(run, before=before, after=after, items=env.rows)


@benchmark("ingest/jsonl", reps=3,
           note="two full passes (column union, then insert) — 1/5 the rows")
def _(env):
    fx = env.case()
    rows = max(1, env.rows // 5)
    path = events_jsonl(rows)
    held, before, after = _throwaway(fx)

    def run():
        held["store"].ingest_json(path, name="bench.jsonl", build_fts=False)

    return Timed(run, before=before, after=after, items=rows)


@benchmark("ingest/jsonl_flattened", reps=3,
           note="same file with nested objects unfolded into dotted columns")
def _(env):
    fx = env.case()
    rows = max(1, env.rows // 5)
    path = events_jsonl(rows)
    held, before, after = _throwaway(fx)

    def run():
        held["store"].ingest_json(path, name="bench.jsonl", build_fts=False,
                                  flatten_mode="depth", flatten_depth=3)

    return Timed(run, before=before, after=after, items=rows)


@benchmark("ingest/sqlite_table", reps=3,
           note="external .db, read-only, with WebKit timestamp conversion")
def _(env):
    fx = env.case()
    rows = max(1, env.rows // 10)
    path = chromium_sqlite(rows)
    held, before, after = _throwaway(fx)

    def run():
        held["store"].ingest_sqlite_table(
            path, "urls", name="urls", build_fts=False,
            timestamp_columns=["last_visit_time"],
        )

    return Timed(run, before=before, after=after, items=rows)


@benchmark("ingest/preview_csv_text",
           note="the import modal's live preview — has to feel instant")
def _(env):
    fx = env.case()
    with open(events_csv(env.rows), encoding="utf-8") as f:
        text = f.read(256 * 1024)

    def run():
        fx.store.preview_csv_text(text)

    return run


@benchmark("ingest/scan_directory",
           note="re-run on every pattern edit in the folder-import modal")
def _(env):
    fx = env.case()
    root = os.path.join(fx.tmpdir, "scantree")
    if not os.path.isdir(root):
        # 2,000 files over 20 subdirectories, a plausible KAPE/EZTools output
        # tree — the scan is pure filesystem walk + fnmatch, so empty files
        # measure it exactly as well as full ones.
        for d in range(20):
            sub = os.path.join(root, f"dir{d:02d}")
            os.makedirs(sub, exist_ok=True)
            for n in range(100):
                ext = ".csv" if n % 3 else ".txt" if n % 2 else ".bin"
                open(os.path.join(sub, f"file{n:03d}{ext}"), "w").close()

    def run():
        fx.store.scan_import_directory(
            root, recursive=True,
            exclude_patterns=["*_Amcache_UnassociatedFileEntries.csv", "dir01/*"],
        )

    return run


@benchmark("ingest/compact", reps=3, min_size="standard",
           note="VACUUM — the only thing that returns freed pages to the OS")
def _(env):
    fx = env.case()
    held = {}

    def before():
        held["store"], held["path"] = fx.copy_pristine()

    def after():
        held["store"].close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(held["path"] + suffix)
            except OSError:
                pass

    def run():
        held["store"].compact()

    return Timed(run, before=before, after=after)

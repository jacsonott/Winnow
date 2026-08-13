"""Disk footprint — the metrics half of the suite.

Nothing here is timed. These are exact byte counts, and they're in the suite
because the case file's size is a first-class property of this project, not
an implementation detail: an analyst's evidence drive holds the source CSVs
*and* the case, and CLAUDE.md's own index-shape decisions are recorded as
sizes ("892MB -> 143MB, -84%"; "case files were ~4.2x their source CSVs, now
~1.6x"). A change that makes a query 10% faster by tripling the index is a
regression here even though every timing improved, and this is the only
place that would say so.

Sizes are taken as deltas against the same file at an earlier stage, after a
WAL checkpoint, so what's reported is the space the feature actually costs.
"""

from __future__ import annotations

import os

from ..fixtures import EQ_COLUMN, events_csv
from ..harness import benchmark


def _checkpointed_size(store, path: str) -> int:
    """WAL-truncate first, or the bytes are split across two files and the
    number depends on when the last checkpoint happened to run."""
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return os.path.getsize(path)


@benchmark("footprint/case_file",
           note="metrics only — source table, FTS index and column index sizes")
def _(env):
    fx = env.case()
    csv_bytes = os.path.getsize(events_csv(env.rows))
    env.metric("csv_input", csv_bytes, "bytes")

    store, path = fx.copy_pristine()
    try:
        # 1. Just the source table, no index of any kind.
        source_bytes = _checkpointed_size(store, path)
        env.metric("source_only", source_bytes, "bytes", floor=64 * 1024)
        env.metric("bytes_per_row", source_bytes / env.rows, "", floor=1.0)
        env.metric("db_to_csv", source_bytes / csv_bytes, "ratio", floor=0.02)

        # 2. The trigram index on top of it. detail=none/columnsize=0 is what
        #    keeps this from dominating the file; if that ever regresses,
        #    fts_of_source is the number that moves.
        sid = store.list_sources()[0]["id"]
        store.build_fts(sid)
        with_fts = _checkpointed_size(store, path)
        env.metric("fts_index", with_fts - source_bytes, "bytes", floor=64 * 1024)
        env.metric("fts_of_source", (with_fts - source_bytes) / source_bytes,
                   "ratio", floor=0.02)
        env.metric("case_to_csv", with_fts / csv_bytes, "ratio", floor=0.02)

        # 3. One auto-created sargable-filter index. These are created behind
        #    the analyst's back and never expire, so their cost is worth
        #    watching even though it's small.
        src = store.get_source(sid)
        store._ensure_column_index_building(sid, EQ_COLUMN, src["table_name"])
        store.wait_for_column_index(sid, EQ_COLUMN, timeout=600)
        with_index = _checkpointed_size(store, path)
        env.metric("column_index", with_index - with_fts, "bytes", floor=32 * 1024)
    finally:
        store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
    return None


@benchmark("footprint/built_case",
           note="the whole fixture case as the suite left it — 5 sources, "
                "one FTS index, tags")
def _(env):
    fx = env.case()
    env.metric("case_file", _checkpointed_size(fx.store, fx.path), "bytes",
               floor=1024 * 1024)
    env.metric("row_tags", _table_rows(fx.store, "row_tags"), "rows", floor=1)
    return None


def _table_rows(store, table: str) -> int:
    with store.lock:
        return store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

"""Compact must actually shrink — including when readers were active.

The bug: wal_checkpoint(TRUNCATE) reports failure in its result row, not
an exception, so a checkpoint blocked by a reader's snapshot passed
silently and the reclaimed space sat in a growing -wal forever."""

from __future__ import annotations

import os
import threading
import time

import pytest


def _fill(store, write_csv, n=20000):
    rows = [["A", "B"]] + [[f"r{i}", "x" * 80] for i in range(n)]
    return store.ingest_csv(write_csv(rows, "big.csv"), name="big", build_fts=False)["id"]


def _disk(store):
    total = os.path.getsize(store.path)
    try:
        total += os.path.getsize(store.path + "-wal")
    except OSError:
        pass
    return total


def test_compact_reclaims_a_dropped_source(store, write_csv):
    sid = _fill(store, write_csv)
    before = _disk(store)
    store.drop_source(sid)
    res = store.compact()
    assert res["reclaimed_bytes"] > 0
    assert _disk(store) < before / 2


def test_compact_survives_an_inflight_reader(store, write_csv):
    """A reader mid-SELECT pins the WAL snapshot; compact retries the
    checkpoint until the reader finishes instead of silently leaving the
    space in the -wal."""
    sid = _fill(store, write_csv)
    keep = _fill(store, write_csv)
    v = store.build_view(keep, {"source_id": keep, "filters": [], "sort": []})
    release = threading.Event()

    def slow_read():
        with store._reader() as ro:
            ro.execute(f"SELECT COUNT(*) FROM src_{keep}").fetchone()
            release.wait(5)  # hold the checked-out connection a while

    t = threading.Thread(target=slow_read)
    t.start()
    time.sleep(0.2)
    store.drop_source(sid)
    release.set()
    res = store.compact()  # must not raise, must reclaim
    t.join()
    assert res["reclaimed_bytes"] > 0
    # ...and reads keep working afterwards (the pool was drained mid-flight)
    assert store.fetch_rows(v["view_id"], 0, 5)["rows"]


def test_compact_refuses_loudly_when_the_wal_stays_pinned(store, write_csv):
    """If something holds a read TRANSACTION open past the timeout, compact
    must say so rather than 'succeed' with the space still parked."""
    _fill(store, write_csv, n=2000)
    ro = store._open_reader()
    ro.execute("BEGIN")
    ro.execute("SELECT COUNT(*) FROM sources").fetchone()  # open read txn pins the snapshot
    try:
        with pytest.raises(ValueError, match="checkpoint"):
            store._checkpoint_truncate_or_raise(timeout_s=0.5)
    finally:
        ro.close()

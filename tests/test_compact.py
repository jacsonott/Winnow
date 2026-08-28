"""Compact must actually shrink — including when readers are active.

The bug: wal_checkpoint(TRUNCATE) reports failure in its result row, not
an exception, so a checkpoint blocked by a reader's snapshot passed
silently and the reclaimed space sat in a growing -wal forever. The
follow-on trap, which these tests also pin: the pragma is per-*attached*
database unless qualified, and every pooled reader attaches the scratch
views db — so an unqualified checkpoint calls the case file blocked when
it's really a view being paged."""

from __future__ import annotations

import os
import threading
import time

import pytest


def _fill(store, write_csv, n=20000, name="big"):
    rows = [["A", "B"]] + [[f"r{i}", "x" * 80] for i in range(n)]
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False)["id"]


def _disk(store):
    """Whole footprint, the same definition compact() reports."""
    return os.path.getsize(store.path) + store._wal_size()


def test_compact_reclaims_a_dropped_source(store, write_csv):
    sid = _fill(store, write_csv)
    before = _disk(store)
    store.drop_source(sid)
    res = store.compact()
    assert res["reclaimed_bytes"] > 0
    assert _disk(store) < before / 2
    # The reported numbers are the on-disk ones, and they add up.
    assert res["before_bytes"] - res["after_bytes"] == res["reclaimed_bytes"]
    assert res["after_bytes"] == _disk(store)


def test_reported_sizes_count_the_wal(store, write_csv):
    """before_bytes is measured before any checkpoint, so a case whose data
    is still sitting in the -wal reports its real footprint rather than the
    one-page main file that made the old numbers nonsense."""
    sid = _fill(store, write_csv)
    store.drop_source(sid)
    _fill(store, write_csv, n=2000, name="fresh")  # writes land in the WAL
    assert store._wal_size() > 0, "fixture needs uncheckpointed WAL bytes"
    res = store.compact()
    assert res["before_bytes"] > os.path.getsize(store.path)
    assert res["reclaimed_bytes"] > 0


def test_compact_runs_through_a_live_reader(store, write_csv):
    """The integration case: a reader holds an open read transaction across
    the whole compact and lets go partway through. Compact still shrinks
    the file, still reports a truncated WAL, and reads still work after the
    pool was drained under them.

    Deliberately NOT the test for the checkpoint retry: wall time here is
    dominated by VACUUM's own waiting on the same reader, so it can't
    separate "the retry waited" from "VACUUM waited" — a single-shot
    checkpoint passes this test. The retry is pinned by
    test_a_pinned_wal_reports_instead_of_discarding_the_vacuum and
    test_checkpoint_budget_is_honoured, both of which fail against it."""
    # Deliberately small: the VACUUM has to finish well inside HOLD_S below,
    # or "compact took longer than the hold" stops proving it waited.
    sid = _fill(store, write_csv, n=4000)
    keep = _fill(store, write_csv, n=200, name="keep")
    ro = store._open_reader()
    ro.execute("BEGIN")
    ro.execute(f"SELECT COUNT(*) FROM src_{keep}").fetchone()  # snapshot now pinned
    store.drop_source(sid)
    _fill(store, write_csv, n=200, name="churn")  # WAL frames the reader's snapshot predates

    HOLD_S = 1.0

    def release_mid_compact():
        time.sleep(HOLD_S)
        ro.execute("COMMIT")

    t = threading.Thread(target=release_mid_compact)
    t.start()
    try:
        res = store.compact()
    finally:
        t.join()
        ro.close()
    assert res["wal_checkpointed"] is True
    assert res["wal_pending_bytes"] == 0
    assert res["reclaimed_bytes"] > 0
    # The pool was drained mid-flight; reads have to keep working after it.
    assert store.fetch_rows(
        store.build_view(keep, {"source_id": keep, "filters": [], "sort": []})["view_id"],
        0, 5)["rows"]


def test_a_reader_paging_a_view_does_not_block_compact(store, write_csv):
    """The views database is scratch attached alongside the case file, and
    an unqualified wal_checkpoint ORs the busy flag across every attached
    db. So a reader merely paging a VIEW used to report the case file's
    checkpoint as blocked — compact refusing over state that has nothing
    to do with the case file's size."""
    sid = _fill(store, write_csv)
    keep = _fill(store, write_csv, name="keep")
    view = store.build_view(keep, {"source_id": keep, "filters": [],
                                   "sort": [{"column": "A", "dir": "asc"}]})
    store.drop_source(sid)

    ro = store._open_reader()
    ro.execute("BEGIN")
    tbl = [r[0] for r in ro.execute("SELECT name FROM v.sqlite_master WHERE type='table'")
           if r[0].startswith("view_")][0]
    ro.execute(f"SELECT COUNT(*) FROM v.{tbl}").fetchone()  # pins ONLY the views db
    try:
        res = store.compact()
    finally:
        ro.execute("COMMIT")
        ro.close()
    assert res["wal_checkpointed"] is True, "a view reader must not block the case file's checkpoint"
    assert res["reclaimed_bytes"] > 0
    assert store.fetch_rows(view["view_id"], 0, 5)["rows"]


def test_a_pinned_wal_reports_instead_of_discarding_the_vacuum(store, write_csv):
    """If something holds the snapshot for the whole budget, the VACUUM has
    still happened — compact reports what's parked in the WAL rather than
    raising and throwing away minutes of work on a large case."""
    sid = _fill(store, write_csv)
    store.drop_source(sid)
    ro = store._open_reader()
    ro.execute("BEGIN")
    ro.execute("SELECT COUNT(*) FROM sources").fetchone()  # held for the whole call
    try:
        t0 = time.monotonic()
        res = store.compact()
        elapsed = time.monotonic() - t0
    finally:
        ro.close()
    assert res["wal_checkpointed"] is False
    assert res["wal_pending_bytes"] > 0
    # Honest by construction: bytes still in the WAL are not "reclaimed".
    assert res["after_bytes"] == res["main_after_bytes"] + res["wal_pending_bytes"]
    # And the budget is a real wall-clock bound — the pragma's own busy
    # handler used to blow past it by a full 5s per attempt.
    assert elapsed < 2 * store.CHECKPOINT_TIMEOUT_S


def test_checkpoint_budget_is_honoured(store, write_csv):
    """timeout_s bounds wall time. It didn't: TRUNCATE invokes the busy
    handler, so with the connection's default 5s one blocked attempt sat
    inside the pragma for 5s and a 0.5s budget took 5.03s."""
    _fill(store, write_csv, n=2000)
    ro = store._open_reader()
    ro.execute("BEGIN")
    ro.execute("SELECT COUNT(*) FROM sources").fetchone()
    store.ingest_rows(["C"], [["1"]], name="churn", build_fts=False)  # frames to checkpoint
    try:
        t0 = time.monotonic()
        assert store._checkpoint_truncate(timeout_s=0.5) is False
        elapsed = time.monotonic() - t0
    finally:
        ro.close()
    assert elapsed < 2.5, f"0.5s budget took {elapsed:.2f}s"
    # The connection's own busy timeout is left the way it was found.
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

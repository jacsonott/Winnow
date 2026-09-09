"""The reader-pool / writer-lock split (CLAUDE.md invariant #4).

Pure-read paths (paging, tag positions, grouping, exports, search counts)
run on read-only pooled connections that never take Store.lock, so a long
write — a big build_view, an ingest chunk — can't stall them. These tests
pin that structurally, the same philosophy as test_search.py's
_CountingLock note: a racing-thread test also passes against a broken
implementation (the racer can win the lock first), so each test here holds
the writer lock *for the entire duration* of the read and asserts the read
still completes. Against the old single-connection code that's a
deterministic deadlock, not a flaky race.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest


def _run_with_lock_held(store, fn, timeout=5.0):
    """Runs fn() on a worker thread while this thread holds store.lock the
    whole time. Returns fn's result; raises if it blocked (old behavior) or
    raised."""
    result: dict = {}

    def worker():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — reraised below
            result["error"] = e

    with store.lock:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            pytest.fail("read path blocked on Store.lock — reader pool not in use")
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_fetch_rows_completes_while_writer_lock_is_held(ingested):
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    out = _run_with_lock_held(store, lambda: store.fetch_rows(view["view_id"], 0, 10))
    assert len(out["rows"]) == 4


def test_virtual_root_fetch_completes_while_writer_lock_is_held(ingested):
    store, sid = ingested
    view = store.build_view(sid, {})  # unfiltered/unsorted -> root_virtual
    out = _run_with_lock_held(store, lambda: store.fetch_rows(view["view_id"], 0, 10))
    assert len(out["rows"]) == 4


def test_tag_positions_and_group_summary_complete_while_writer_lock_is_held(ingested):
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    store.set_tags(sid, [1], 1, True)
    vid = view["view_id"]
    positions = _run_with_lock_held(store, lambda: store.tag_positions(vid))
    assert positions  # rid 1 is tagged and in the view
    summary = _run_with_lock_held(store, lambda: store.group_summary(vid, "Process"))
    assert len(summary["groups"]) == 4


def test_csv_export_streams_while_writer_lock_is_held(ingested):
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    body = _run_with_lock_held(
        store, lambda: "".join(store.export_view_csv(view["view_id"]))
    )
    assert body.count("\n") == 5  # header + 4 rows


def test_dropped_view_table_maps_to_view_expired_keyerror(ingested):
    """The eviction race _dropped_view_is_expired exists for: a reader
    holding a handle whose v.view_N table a concurrent rebuild just
    dropped must surface the same KeyError contract as a missing handle
    (server.py maps it to the 409 the frontend rebuilds on), never a raw
    sqlite3.OperationalError = a 500 blaming nobody."""
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    vid = view["view_id"]
    # Tag a row first: tag_positions' untagged fast-exit never touches
    # v.view_N at all (returning [] against a dropped table is fine and
    # tested elsewhere) — the race only exists once the join query runs.
    store.set_tags(sid, [1], 1, True)
    with store.lock, store.db:
        store.db.execute(f'DROP TABLE v."{vid}"')
    with pytest.raises(KeyError, match="expired"):
        store.fetch_rows(vid, 0, 10)
    with pytest.raises(KeyError, match="expired"):
        store.tag_positions(vid)


def test_reader_pool_recycles_connections(ingested):
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    for _ in range(store.READER_POOL_CAP + 3):
        store.fetch_rows(view["view_id"], 0, 2)
    assert 1 <= len(store._reader_pool) <= store.READER_POOL_CAP


def test_close_drains_reader_pool_and_removes_views_db(ingested):
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    store.fetch_rows(view["view_id"], 0, 2)  # populate the pool
    pooled = list(store._reader_pool)
    views_path = store._views_path
    store.close()
    assert store._reader_pool == []
    for conn in pooled:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert not os.path.exists(views_path)


def test_reader_sees_committed_writes_immediately(ingested):
    """Read-your-writes across the connection split: a tag committed on the
    writer must be visible to the very next reader-pool query (WAL readers
    see the latest commit; nothing here should ever read stale data)."""
    store, sid = ingested
    view = store.build_view(sid, {"sort": [{"column": "Process"}]})
    assert store.tag_positions(view["view_id"]) == []
    store.set_tags(sid, [2], 1, True)
    assert store.tag_positions(view["view_id"]) != []


def test_dashboard_reads_do_not_take_the_writer_lock(store):
    """Opening a board while an import runs must not queue behind it —
    invariant #4. Held writer lock, reads still answer."""
    store.create_dashboard("Board", [{"title": "w", "source": "sql", "render": "stat",
                                      "query": {"sql": "SELECT 1"}}])
    (board,) = store.list_dashboards()
    with store.lock:                      # stand in for an ingest mid-batch
        assert len(store.list_dashboards()) == 1
        assert len(store.get_dashboard(board["id"])) == 1

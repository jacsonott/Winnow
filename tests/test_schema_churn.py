"""Reads while the writer commits schema changes back to back.

A filter, sort or search on a merge asks for an index (or a search index)
on every member, and on a case that merged 1000 IIS logs the first one
answered "database schema has changed" for the minute those builds took:
every build committed a schema change, and a read re-prepared under each
one, in autocommit, read the schema afresh for every one of SQLite's 50
tries. Each _reader() block (and each SQL pane query) is now one read
transaction, and a merge's builds are checked on one reader and built on
one worker, a few dozen to a commit."""

from __future__ import annotations

import sqlite3

import pytest


def _small(store, write_csv, n):
    ids = [store.ingest_csv(write_csv([["Host", "User"], [f"h{i}", "u"], ["h", f"v{i}"]], f"t{i}.csv"),
                            build_fts=False)["id"] for i in range(n)]
    return ids, store.create_merge("m", ids)["id"]


def test_a_reader_block_reads_one_snapshot(store, write_csv):
    sid = store.ingest_csv(write_csv([["A"], ["x"], ["y"]]), build_fts=False)["id"]
    tid = store.list_tags()[0]["id"]
    count = "SELECT count(*) FROM row_tags"
    with store._reader() as ro:
        assert ro.execute(count).fetchone()[0] == 0
        store.set_tags(sid, [1, 2], tid, True)   # committed on the writer, mid-block
        assert ro.execute(count).fetchone()[0] == 0
    with store._reader() as ro:
        assert ro.execute(count).fetchone()[0] == 2


def test_a_pane_query_reads_the_snapshot_its_views_were_made_from(store, write_csv):
    sid = store.ingest_csv(write_csv([["A"], ["x"], ["y"]]), build_fts=False)["id"]
    tid = store.list_tags()[0]["id"]
    with store._pane_query() as ro:
        store.set_tags(sid, [1, 2], tid, True)
        assert ro.execute(f"SELECT count(Tags) FROM src_{sid}").fetchone()[0] == 0
    assert store.run_sql(f"SELECT count(Tags) FROM src_{sid}")["rows"] == [[2]]


@pytest.fixture
def churning(store):
    """Every statement a reader prepares commits a schema change on another
    connection while it compiles: the writer outrunning each re-prepare,
    as 1000 index builds committing back to back did, made deterministic.
    The authorizer is SQLite's own hook into the compile."""
    writer = sqlite3.connect(store.path, check_same_thread=False)
    made = []

    def changes_schema(action, *_):
        if action == sqlite3.SQLITE_SELECT:
            writer.execute(f"CREATE TABLE churn_{len(made)}(x)")
            made.append(1)
        return sqlite3.SQLITE_OK

    opened = store._open_reader

    def open_reader():
        conn = opened()
        conn.set_authorizer(changes_schema)
        return conn

    store._reader_pool.clear()
    store._open_reader = open_reader
    yield made
    store._reader_pool.clear()
    del store._open_reader
    writer.close()


def test_reads_survive_schema_changes_committed_back_to_back(store, write_csv, churning):
    ids, mid = _small(store, write_csv, 3)
    assert store.get_source(mid)["row_count"] == 6
    assert len(store.list_sources()) == 3
    assert {v["value"] for v in store.column_values(mid, "Host")} == {"h0", "h1", "h2", "h"}
    assert churning, "the schema never changed under a read"


def test_a_merges_index_builds_are_checked_together_and_committed_together(store, write_csv, monkeypatch):
    """One reader for every member's checks, not two per member, and one
    worker that commits the lot at once: each commit is a schema change
    every reader then reloads."""
    ids, mid = _small(store, write_csv, 30)
    checkouts = []
    reader = store._reader

    def counted():
        checkouts.append(1)
        return reader()

    monkeypatch.setattr(store, "_reader", counted)
    workers = []
    worker = store._build_column_indexes_worker

    def recorded(jobs):
        workers.append(len(jobs))
        worker(jobs)

    monkeypatch.setattr(store, "_build_column_indexes_worker", recorded)
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        store.build_view(mid, {"source_id": mid, "filters": [{"column": "Host", "op": "equals", "value": "h"}]})
        with store._threads_lock:
            builds = list(store._index_threads.values())
        for b in builds:
            b.join(30)
    finally:
        store.db.set_trace_callback(None)
    assert len(builds) == 30 and workers == [30]
    assert len(checkouts) < 10, len(checkouts)
    built = [i for i, s in enumerate(statements) if s.startswith("CREATE INDEX")]
    assert len(built) == 30
    assert statements[built[0] - 1] == "BEGIN" and statements[built[-1] + 1] == "COMMIT"
    assert "COMMIT" not in statements[built[0]:built[-1]]
    for sid in ids:
        assert [i["column"] for i in store.list_column_indexes(sid)] == ["Host"]

"""Maintenance: the auto-created per-column filter indexes (listing/dropping
them), compact() / VACUUM, and the sweep for orphaned views databases.

All of it exists because everything else in the app only ever *adds* —
indexes appear behind the analyst's back, freed pages go to SQLite's
freelist rather than back to the OS, and a views database whose process was
killed sits in /dev/shm or the platform tempdir forever."""

from __future__ import annotations

import os
import sqlite3

import pytest

from winnow import store as store_module
from winnow.store import VIEWS_PREFIX, VIEWS_SUFFIX, Store, sweep_orphan_views


def test_column_index_listed_after_a_sargable_filter(ingested):
    store, source_id = ingested
    assert store.list_column_indexes(source_id) == []
    spec = {
        "source_id": source_id,
        "filters": [{"column": "EventId", "op": "equals", "value": "4624"}],
        "sort": [],
    }
    store.build_view(source_id, spec)
    assert store.wait_for_column_index(source_id, "EventId", timeout=5)
    listed = store.list_column_indexes(source_id)
    assert [ix["column"] for ix in listed] == ["EventId"]
    assert listed[0]["building"] is False


def test_column_values_triggers_an_index(ingested):
    # The value-picker dropdown's own GROUP BY is the shape a plain index
    # serves best (covering index scan, no temp b-tree) — and it's the
    # index an equals filter on the same column wants next anyway.
    store, source_id = ingested
    store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)
    assert [ix["column"] for ix in store.list_column_indexes(source_id)] == ["Process"]


def test_group_summary_triggers_an_index_only_on_a_whole_source_view(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    store.group_summary(view["view_id"], "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)


def test_group_summary_agrees_on_both_the_direct_and_joined_paths(ingested):
    # The unfiltered fast path skips the view join entirely; a filtered view
    # goes through it. Same question, same answer.
    store, source_id = ingested
    whole = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    direct = store.group_summary(whole["view_id"], "User")

    # A filter that matches everything: row counts still line up with the
    # source's, so _grouping_covers_whole_source takes the direct path here
    # too — and that's fine, it's the same row set either way.
    matches_all = store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "User", "op": "contains", "value": ""}],
        "sort": [],
    })
    assert store.group_summary(matches_all["view_id"], "User") == direct

    narrowed = store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "User", "op": "contains", "value": "ACME"}],
        "sort": [],
    })
    joined = store.group_summary(narrowed["view_id"], "User")
    assert sum(g["count"] for g in joined["groups"]) == 3
    assert all(g["value"].startswith("ACME") for g in joined["groups"])


def test_group_summary_datetime_still_buckets_on_the_fast_path(ingested):
    # A datetime column groups by DAY_BUCKET whichever branch runs.
    store, source_id = ingested
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    groups = store.group_summary(view["view_id"], "Timestamp")["groups"]
    assert {g["value"] for g in groups} == {"2024-01-05", "2024-01-06", "2024-01-07"}


def test_drop_column_index_and_rebuild(ingested):
    store, source_id = ingested
    store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)
    store.drop_column_index(source_id, "Process")
    assert store.list_column_indexes(source_id) == []
    # Nothing broke: the same query still answers correctly, and rebuilds it.
    assert store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)


def test_drop_column_index_rejects_an_unknown_column(ingested):
    store, source_id = ingested
    with pytest.raises(KeyError):
        store.drop_column_index(source_id, "NoSuchColumn")


def test_list_column_indexes_is_empty_for_a_merge(store, write_csv):
    rows = [["A", "B"], ["1", "2"], ["3", "4"]]
    a = store.ingest_csv(write_csv(rows, "a.csv"), name="a", build_fts=False)
    b = store.ingest_csv(write_csv(rows, "b.csv"), name="b", build_fts=False)
    merge = store.create_merge("m", [a["id"], b["id"]])
    assert store.list_column_indexes(merge["id"]) == []


def test_compact_reclaims_space_after_a_source_is_dropped(store, tmp_path):
    path = tmp_path / "big.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("A,B\n")
        for i in range(20000):
            f.write(f"{i},{'x' * 120}\n")
    rec = store.ingest_csv(str(path), name="big", build_fts=False)

    grown = store.compact()
    assert grown["after_bytes"] > 1_000_000

    store.drop_source(rec["id"])
    # The drop's own writes are still in the -wal here — and before_bytes
    # counts them, because compact now reports the WHOLE on-disk footprint
    # (main + -wal) measured before it checkpoints anything. That is what
    # makes reclaimed_bytes honest even when a checkpoint can't complete.
    on_disk_before = os.path.getsize(store.path) + store._wal_size()
    freed = store.compact()
    assert freed["before_bytes"] == on_disk_before
    assert freed["after_bytes"] < grown["after_bytes"] / 2
    assert freed["reclaimed_bytes"] == freed["before_bytes"] - freed["after_bytes"]


def test_compact_leaves_live_views_and_data_alone(ingested):
    store, source_id = ingested
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    before = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    store.compact()
    # Views live in the temp-attached `v` database; a bare VACUUM only
    # rewrites `main`, so they must survive it intact.
    after = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    assert [r["cells"] for r in after] == [r["cells"] for r in before]


def test_compact_refuses_without_enough_free_disk(ingested, monkeypatch):
    import shutil as shutil_module

    store, _ = ingested
    fake = shutil_module.disk_usage(".")._replace(free=1024)
    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda _p: fake)
    with pytest.raises(ValueError, match="free disk space"):
        store.compact()


def test_compact_restores_temp_store_to_memory(ingested):
    # VACUUM's scratch copy obeys temp_store, so compact() forces FILE for
    # the duration — but leaving it there would push every later sort and
    # temp b-tree onto disk.
    store, _ = ingested
    store.compact()
    assert store.db.execute("PRAGMA temp_store").fetchone()[0] == 2  # 2 == MEMORY


# ------------------------------------------- orphaned views databases (temp)

@pytest.fixture
def views_in(tmp_path, monkeypatch):
    """Point both new views databases *and* the sweep at one tmp dir, so a
    test never creates or deletes anything in the real /dev/shm or /tmp."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(store_module, "_preferred_views_dir", lambda: str(d))
    monkeypatch.setattr(store_module, "_views_dirs", lambda: [str(d)])
    return d


def _abandoned(d, stem="abandoned"):
    """The three files a killed process leaves behind, unlocked."""
    base = d / f"{VIEWS_PREFIX}{stem}{VIEWS_SUFFIX}"
    for suffix in ("", "-wal", "-shm"):
        (d / (base.name + suffix)).write_bytes(b"x" * 1024)
    return base


def test_sweep_removes_a_views_db_whose_process_is_gone(views_in):
    base = _abandoned(views_in)
    swept = sweep_orphan_views()
    assert swept["removed"] == 3  # .db plus its -wal/-shm
    assert swept["bytes_freed"] == 3 * 1024
    for suffix in ("", "-wal", "-shm"):
        assert not os.path.exists(str(base) + suffix)


def test_sweep_leaves_a_live_stores_views_db_alone(views_in, case_path):
    """The one that makes the sweep safe to run at all: a second Winnow
    starting up must not delete the scratch database this process is still
    querying out of. The flock Store.__init__ holds is what says so."""
    store = Store(case_path)
    try:
        assert os.path.exists(store._views_path)
        assert sweep_orphan_views() == {"removed": 0, "bytes_freed": 0}
        assert os.path.exists(store._views_path)
        store.db.execute("SELECT 1 FROM v.sqlite_master").fetchall()  # still attached
    finally:
        store.close()


def test_sweep_takes_the_dead_and_keeps_the_live_in_one_pass(views_in, case_path):
    store = Store(case_path)
    try:
        base = _abandoned(views_in)
        assert sweep_orphan_views()["removed"] == 3
        assert not os.path.exists(base)
        assert os.path.exists(store._views_path)
    finally:
        store.close()


def test_sweep_ignores_files_that_are_not_ours(views_in):
    keep = views_in / "case.db"
    keep.write_bytes(b"not a views database")
    (views_in / "winnow-something-else.txt").write_bytes(b"nor this")
    assert sweep_orphan_views() == {"removed": 0, "bytes_freed": 0}
    assert keep.exists()


def test_sweep_collects_a_stray_wal_whose_db_is_already_gone(views_in):
    # close() removes the .db first, so a -wal that outlived it can only
    # belong to a dead owner — there's nothing left to lock and probe.
    stray = views_in / f"{VIEWS_PREFIX}half{VIEWS_SUFFIX}-wal"
    stray.write_bytes(b"x" * 512)
    swept = sweep_orphan_views()
    assert swept == {"removed": 1, "bytes_freed": 512}
    assert not stray.exists()


def test_a_file_close_could_not_remove_is_collected_by_the_next_sweep(
    views_in, case_path, monkeypatch
):
    """Windows can't unlink a file an in-flight reader still has open, so
    close() swallows the error and leaves one behind. It must not be
    permanent: close() releases the lock first, so the next start sweeps it."""
    store = Store(case_path)
    views_path = store._views_path
    # A context, not a bare setattr: undoing the whole `monkeypatch` fixture
    # would also undo views_in's redirection and point the sweep below at
    # the developer's real /dev/shm and /tmp.
    with monkeypatch.context() as m:
        m.setattr(store_module.os, "remove", _raise_oserror)
        store.close()
    assert os.path.exists(views_path)
    assert sweep_orphan_views()["removed"] >= 1
    assert not os.path.exists(views_path)


def _raise_oserror(*_args, **_kwargs):
    raise OSError("simulated: file still open by another process")


def test_lifespan_shutdown_closes_the_open_case(views_in, case_path):
    """The other half: nothing used to call Store.close() on the way out, so
    Ctrl+C stranded the views database every time."""
    import server
    from fastapi.testclient import TestClient

    store = Store(case_path)
    server.STORE = store
    try:
        with TestClient(server.app) as client:   # `with` is what runs the lifespan
            assert client.get("/api/case/current").json()["open"] is True
        assert server.STORE is None
        assert not os.path.exists(store._views_path)
        with pytest.raises(sqlite3.ProgrammingError):
            store.db.execute("SELECT 1")
    finally:
        server.STORE = None


def test_fts_builds_are_concurrency_capped(store, monkeypatch):
    """A broad action (a search-all sweep over many unindexed sources) calls
    _ensure_fts_building for every one; each build holds the writer lock in
    chunks, so an unbounded swarm starves an interactive build_view (a preset
    apply that 'wouldn't work at all' while a sweep ran). At most
    FTS_BUILD_CONCURRENCY may build at once."""
    import threading
    import time

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_build(_sid):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1

    monkeypatch.setattr(store, "build_fts", fake_build)
    threads = [threading.Thread(target=store._build_fts_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak >= 1                                   # builds actually ran
    assert peak <= store.FTS_BUILD_CONCURRENCY         # never more than the cap at once

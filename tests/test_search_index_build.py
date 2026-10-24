"""Starting a table's search index on request — the Tables manager's
Build — and the route that says whether this machine can have one at all
(a pre-3.45 SQLite can't, and the manager says so rather than showing
"not indexed" forever)."""

from __future__ import annotations

import sqlite3

import pytest

import winnow.store as store_mod
from winnow.store import Store, TRIGRAM_LIKE_MIN_SQLITE

needs_trigram = pytest.mark.skipif(sqlite3.sqlite_version_info < TRIGRAM_LIKE_MIN_SQLITE,
                                   reason="this SQLite can't build a search index")

ROWS = [["A", "B"], ["alpha", "one"], ["beta", "two"], ["gamma", "three"]]


def test_supported_follows_the_runtime_sqlite(monkeypatch):
    assert Store.search_index_supported() is (sqlite3.sqlite_version_info >= TRIGRAM_LIKE_MIN_SQLITE)
    monkeypatch.setattr(store_mod, "TRIGRAM_LIKE_MIN_SQLITE", (99, 0, 0))
    assert Store.search_index_supported() is False


@needs_trigram
def test_start_builds_a_table_without_one_and_is_idempotent(store, write_csv):
    rec = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)
    assert store.get_source(rec["id"])["has_fts"] == 0
    first = store.start_search_index(rec["id"])
    assert first["supported"] is True
    assert first["ready"] or first["building"]
    assert store.wait_for_fts(rec["id"], timeout=10)
    assert store.get_source(rec["id"])["has_fts"] == 1
    # Once it's ready, Build again starts nothing.
    again = store.start_search_index(rec["id"])
    assert again == {"ready": True, "building": False, "supported": True}


def test_start_refuses_a_merge_and_an_unknown_table(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)
    b = store.ingest_csv(write_csv(ROWS, "b.csv"), name="b", build_fts=False)
    merge = store.create_merge("m", [a["id"], b["id"]])
    with pytest.raises(ValueError):
        store.start_search_index(merge["id"])
    with pytest.raises(KeyError):
        store.start_search_index(9999)


def test_start_does_nothing_on_an_old_sqlite(store, write_csv, monkeypatch):
    rec = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)
    monkeypatch.setattr(store_mod, "TRIGRAM_LIKE_MIN_SQLITE", (99, 0, 0))
    assert store.start_search_index(rec["id"]) == {"ready": False, "building": False, "supported": False}
    assert store.get_source(rec["id"])["has_fts"] == 0


def test_search_index_routes(client, store, write_csv, monkeypatch):
    info = client.get("/api/search_index").json()
    assert info == {"supported": Store.search_index_supported(), "sqlite": sqlite3.sqlite_version,
                    "auto_max_bytes": store_mod.FTS_AUTO_MAX_BYTES}

    rec = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)
    r = client.post(f"/api/source/{rec['id']}/search_index")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ready", "building", "supported"}
    if Store.search_index_supported():
        assert store.wait_for_fts(rec["id"], timeout=10)

    assert client.post("/api/source/9999/search_index").status_code == 404
    other = store.ingest_csv(write_csv(ROWS, "b.csv"), name="b", build_fts=False)
    merge = store.create_merge("m", [rec["id"], other["id"]])
    r = client.post(f"/api/source/{merge['id']}/search_index")
    assert r.status_code == 400 and "merge" in r.json()["detail"]


def test_commits_are_sized_by_how_long_they_hold_the_lock():
    """build_fts's chunks aim at FTS_COMMIT_SECONDS of writer lock, not a
    row count: a BATCH of wide EvtxECmd rows held it for ten seconds."""
    nxt, target = store_mod._next_fts_commit_rows, store_mod.FTS_COMMIT_SECONDS
    assert nxt(1000, target) == 1000
    assert nxt(1000, target * 4) == 250
    assert nxt(1000, target / 10) == 2000              # at most doubled per step
    assert nxt(1000, 0) == 2000
    assert nxt(100, 60) == store_mod.FTS_COMMIT_MIN_ROWS
    assert nxt(store_mod.BATCH, target / 10) == store_mod.BATCH


def _build_stopping_after(store, sid, chunks, monkeypatch):
    """build_fts in 100-row chunks, closing the case (as far as the build
    can tell) after `chunks` of them; returns how many chunks it wrote."""
    calls = []

    def fixed(rows, held):
        calls.append(rows)
        if len(calls) == chunks:
            store._closing = True
        return 100
    monkeypatch.setattr(store_mod, "FTS_COMMIT_FIRST_ROWS", 100)
    monkeypatch.setattr(store_mod, "_next_fts_commit_rows", fixed)
    store.build_fts(sid)
    store._closing = False
    return len(calls)


def _indexed(store, sid, needle):
    return [r[0] for r in store.db.execute(
        f"SELECT rowid FROM fts_{sid} WHERE doc LIKE ? ORDER BY rowid", (f"%{needle}%",))]


@needs_trigram
def test_a_build_the_case_closed_under_carries_on_where_it_stopped(store, write_csv, case_path, monkeypatch):
    rows = [["A", "B"]] + [[f"row {i:04d}", "even" if i % 2 == 0 else "odd"] for i in range(1, 1001)]
    sid = store.ingest_csv(write_csv(rows), name="a.csv", build_fts=False)["id"]
    assert _build_stopping_after(store, sid, 4, monkeypatch) == 4
    assert store.get_source(sid)["has_fts"] == 0
    assert store.db.execute("SELECT next_rid FROM fts_progress WHERE source_id=?", (sid,)).fetchone()[0] == 401
    store.close()

    again = Store(case_path)
    try:
        # 6 chunks for rows 401..1000, not 10 from the top.
        assert _build_stopping_after(again, sid, 0, monkeypatch) == 6
        assert again.get_source(sid)["has_fts"] == 1
        assert again.db.execute("SELECT count(*) FROM fts_progress").fetchone()[0] == 0
        again.db.execute(f"INSERT INTO fts_{sid}(fts_{sid}) VALUES('integrity-check')")
        assert _indexed(again, sid, "row 0399") == [399]
        assert _indexed(again, sid, "row 0401") == [401]
        assert _indexed(again, sid, "even") == list(range(2, 1001, 2))
    finally:
        again.close()


@needs_trigram
def test_a_build_that_cannot_trust_its_progress_starts_over(store, write_csv, monkeypatch):
    """A progress row read through a different doc view (other columns,
    or another release's concatenation) is not this index's progress."""
    rows = [["A"]] + [[f"row {i:04d}"] for i in range(1, 501)]
    sid = store.ingest_csv(write_csv(rows), name="a.csv", build_fts=False)["id"]
    _build_stopping_after(store, sid, 2, monkeypatch)
    with store.lock, store.db:
        store.db.execute("UPDATE fts_progress SET doc_sql = 'CREATE VIEW x AS SELECT 1'")
    assert _build_stopping_after(store, sid, 0, monkeypatch) == 5
    store.db.execute(f"INSERT INTO fts_{sid}(fts_{sid}) VALUES('integrity-check')")
    assert _indexed(store, sid, "row 0150") == [150]

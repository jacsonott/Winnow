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
    assert info == {"supported": Store.search_index_supported(), "sqlite": sqlite3.sqlite_version}

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

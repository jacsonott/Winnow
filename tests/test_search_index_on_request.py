"""A search index only when the analyst wants one: a table over
FTS_AUTO_MAX_BYTES of text gets none until Build asks for it, and Remove
takes one away and keeps it away.

The index costs about a byte of case file per byte of text, so on a big
table it is most of what the analyst pays in disk. Nothing automatic may
build one past the gate (import, a search, search-all, a watchlist scan),
and nothing may quietly bring back one the analyst removed — that would
put the disk back without anyone asking, which is the thing Remove is for.
"""

from __future__ import annotations

import sqlite3

import pytest

import winnow.store as store_mod
from winnow.store import Store, TRIGRAM_LIKE_MIN_SQLITE

pytestmark = pytest.mark.skipif(sqlite3.sqlite_version_info < TRIGRAM_LIKE_MIN_SQLITE,
                                reason="this SQLite can't build a search index")

ROWS = [["Host", "Path"]] + [[f"host{i:03d}", f"C:\\users\\jacso\\file{i:03d}.txt"] for i in range(1, 201)]


def _fts_objects(store, sid):
    return sorted(r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE name = ? OR name = ?", (f"fts_{sid}", f"src_{sid}_doc")))


def _searched(store, sid, term):
    v = store.build_view(sid, {"search": term})
    return [r["rid"] for r in store.fetch_rows(v["view_id"], 0, v["row_count"])["rows"]]


@pytest.fixture
def small_limit(monkeypatch):
    """Every table in the test counts as large: 200 rows of ~40 bytes is
    ~8 KB of text."""
    monkeypatch.setattr(store_mod, "FTS_AUTO_MAX_BYTES", 1000)


def test_a_small_table_still_builds_its_own(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    assert store.wait_for_fts(sid, timeout=10)
    src = store.get_source(sid)
    assert src["fts_mode"] is None and src["fts_on_request"] is None


def test_a_large_table_gets_none_until_build(store, write_csv, small_limit):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    # Decided before any thread starts (_ensure_fts_building), so "not
    # building" here is the answer, not a race.
    assert not store._is_fts_building(sid)
    src = store.get_source(sid)
    assert (src["has_fts"], src["fts_on_request"]) == (0, "large")
    assert [s for s in store.list_sources() if s["id"] == sid][0]["fts_on_request"] == "large"

    # Every automatic caller passes it by, and searches still find the rows.
    assert _searched(store, sid, "file012") == [12]
    assert store.search_all_sources("jacso")[0]["match_count"] == 200
    store.add_indicator("file007.txt", "filename")
    store.scan_source(sid)
    assert not store._is_fts_building(sid)
    assert _fts_objects(store, sid) == []

    # Build is the ask, and it sticks.
    store.start_search_index(sid)
    assert store.wait_for_fts(sid, timeout=10)
    src = store.get_source(sid)
    assert (src["fts_mode"], src["fts_on_request"]) == ("on", None)
    assert _searched(store, sid, "file012") == [12]


def test_the_size_estimate_is_close_and_asked_once(store, write_csv, monkeypatch):
    rows = [["A", "B"]] + [["x" * (i % 97), str(i)] for i in range(1, 5001)]
    sid = store.ingest_csv(write_csv(rows), name="a.csv", build_fts=False)["id"]
    src = store.get_source(sid)
    exact = store.db.execute(
        f"SELECT SUM(length(CAST({store_mod._blob_expr(['A', 'B'])} AS BLOB))) FROM src_{sid}").fetchone()[0]
    with store._reader() as ro:
        est = store._fts_doc_bytes(ro, src)
    assert abs(est - exact) / exact < 0.1, (est, exact)
    with store._reader() as ro:
        monkeypatch.setattr(store_mod, "_blob_expr", lambda cols: pytest.fail("estimated twice"))
        assert store._fts_doc_bytes(ro, src) == est


def test_remove_drops_the_index_and_keeps_it_off(store, write_csv, case_path):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    assert store.wait_for_fts(sid, timeout=10)
    with_index = _searched(store, sid, "jacso\\file01")
    secure = store.db.execute("PRAGMA secure_delete").fetchone()[0]

    out = store.remove_search_index(sid)
    # FAST only for the drop (see remove_search_index), then as it was.
    assert store.db.execute("PRAGMA secure_delete").fetchone()[0] == secure
    assert out["ready"] is False and out["building"] is False
    assert out["freed_bytes"] > 0
    assert _fts_objects(store, sid) == []
    src = store.get_source(sid)
    assert (src["has_fts"], src["fts_mode"], src["fts_on_request"]) == (0, "off", "removed")

    # Same rows by scanning, and the search doesn't start it again; nor does
    # opening the case again.
    assert _searched(store, sid, "jacso\\file01") == with_index
    assert not store._is_fts_building(sid)
    store.close()
    again = Store(case_path)
    try:
        assert _searched(again, sid, "jacso") == list(range(1, 201))
        assert not again._is_fts_building(sid)
        assert again.get_source(sid)["fts_on_request"] == "removed"
        # Build brings it back.
        again.start_search_index(sid)
        assert again.wait_for_fts(sid, timeout=10)
        assert again.get_source(sid)["fts_mode"] == "on"
        assert _searched(again, sid, "jacso\\file01") == with_index
    finally:
        again.close()


def _chunks(store, monkeypatch, after):
    """build_fts in 50-row chunks, calling after(n) once chunk n is in."""
    n = []

    def fixed(rows, held):
        n.append(rows)
        after(len(n))
        return 50
    monkeypatch.setattr(store_mod, "FTS_COMMIT_FIRST_ROWS", 50)
    monkeypatch.setattr(store_mod, "_next_fts_commit_rows", fixed)
    return n


def test_remove_stops_a_build_part_way(store, write_csv, monkeypatch):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)["id"]
    done = _chunks(store, monkeypatch, lambda k: k == 2 and store.remove_search_index(sid))
    store.build_fts(sid)
    assert len(done) == 2   # the third chunk never went in
    assert _fts_objects(store, sid) == []
    assert store.db.execute("SELECT COUNT(*) FROM fts_progress").fetchone()[0] == 0
    assert store.get_source(sid)["fts_on_request"] == "removed"
    # And a build asked for directly now does nothing either.
    store.build_fts(sid)
    assert _fts_objects(store, sid) == []


def test_remove_then_build_between_two_chunks_starts_over(store, write_csv, monkeypatch):
    """Build found this build alive and started none of its own, so this
    one has to do it: start over on the new index rather than stop, or
    write into the dropped one."""
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)["id"]

    def remove_and_ask_again(k):
        if k == 2:
            store.remove_search_index(sid)
            with store.lock, store.db:   # what start_search_index records
                store.db.execute("UPDATE sources SET fts_mode='on' WHERE id=?", (sid,))
    done = _chunks(store, monkeypatch, remove_and_ask_again)
    store.build_fts(sid)
    assert len(done) == 2 + 4   # two, then all four from the top
    src = store.get_source(sid)
    assert (src["has_fts"], src["fts_mode"]) == (1, "on")
    store.db.execute(f"INSERT INTO fts_{sid}(fts_{sid}) VALUES('integrity-check')")
    assert _searched(store, sid, "file150") == [150]


def test_a_build_of_a_dropped_table_just_stops(store, write_csv, monkeypatch):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv", build_fts=False)["id"]
    done = _chunks(store, monkeypatch, lambda k: k == 1 and store.drop_source(sid))
    store.build_fts(sid)   # no "no such table" out of the next chunk
    assert len(done) == 1


def test_a_search_compiled_against_a_removed_index_scans_instead(store, write_csv, monkeypatch):
    """Remove landing between a search's compile and its writer lock: the
    compiled query names an fts_N that is gone. build_view compiles it
    again rather than failing the search."""
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    assert store.wait_for_fts(sid, timeout=10)
    stale = store._source_lite(sid)
    store.remove_search_index(sid)
    real, calls = store._source_lite, []

    def first_one_stale(source_id):
        calls.append(source_id)
        return stale if len(calls) == 1 else real(source_id)
    monkeypatch.setattr(store, "_source_lite", first_one_stale)
    assert _searched(store, sid, "file012") == [12]
    assert len(calls) == 2


def test_merges_have_none_to_remove_and_say_what_their_tables_wait_for(store, write_csv, small_limit):
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a")["id"]
    b = store.ingest_csv(write_csv(ROWS, "b.csv"), name="b")["id"]
    merge = store.create_merge("m", [a, b])
    with pytest.raises(ValueError):
        store.remove_search_index(merge["id"])
    with pytest.raises(KeyError):
        store.remove_search_index(9999)
    assert store.get_source(merge["id"])["fts_on_request"] == "large"
    # A merge search builds nothing for its members either.
    _searched(store, merge["id"], "file012")
    assert not store._is_fts_building(a) and not store._is_fts_building(b)


def test_copying_a_table_into_another_case_carries_the_choice(store, write_csv, tmp_path):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    assert store.wait_for_fts(sid, timeout=10)
    store.remove_search_index(sid)
    target = str(tmp_path / "other.db")
    Store(target).close()
    store.copy_sources_to(target, [sid])
    other = Store(target)
    try:
        (copied,) = other.list_sources()
        assert (copied["has_fts"], copied["fts_on_request"]) == (0, "removed")
    finally:
        other.close()


def test_remove_route(client, store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS), name="a.csv")["id"]
    assert store.wait_for_fts(sid, timeout=10)
    r = client.delete(f"/api/source/{sid}/search_index")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ready", "building", "supported", "freed_bytes"}
    assert client.get("/api/sources").json()[0]["fts_on_request"] == "removed"
    assert client.delete("/api/source/9999/search_index").status_code == 404
    other = store.ingest_csv(write_csv(ROWS, "b.csv"), name="b")["id"]
    merge = store.create_merge("m", [sid, other])
    r = client.delete(f"/api/source/{merge['id']}/search_index")
    assert r.status_code == 400 and "merge" in r.json()["detail"]

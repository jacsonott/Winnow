"""Saved views are gone: the per-table view snapshots behind
/api/saved_views had a working backend and no UI, and saved filters
already cover what an analyst would save. What has to keep working is
what they left behind — a case file made before the removal still has a
saved_views table, and a session saved then still carries a
`saved_views` list."""

from __future__ import annotations

import json
import sqlite3

import pytest

from winnow.store import DEFAULT_TAGS, Store, case_session_document_problem

ROWS = [["When", "Who"], ["2024-01-01 10:00", "alice"], ["2024-01-01 11:00", "bob"]]

OLD_TABLE = """CREATE TABLE saved_views (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    saved_at  TEXT
)"""


@pytest.fixture
def old_case(case_path, write_csv):
    """A case file as an older Winnow left it: one table, plus a saved
    view for it in a saved_views table."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    sid = s.ingest_csv(write_csv(ROWS, "sv.csv"), build_fts=False)["id"]
    s.close()
    con = sqlite3.connect(case_path)
    con.execute(OLD_TABLE)
    con.execute("INSERT INTO saved_views(source_id, name, payload) VALUES (?, ?, ?)",
                (sid, "alice only", json.dumps({"filters": []})))
    con.commit()
    con.close()
    return case_path, sid


def test_a_new_case_has_no_saved_views_table(store):
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master")}
    assert "saved_views" not in names


def test_the_routes_are_gone(client, store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "sv.csv"), build_fts=False)["id"]
    assert client.get(f"/api/saved_views?source_id={sid}").status_code == 404
    assert client.post("/api/saved_views",
                       json={"source_id": sid, "name": "x", "payload": {}}).status_code in (404, 405)


def test_an_old_case_file_still_opens_and_its_tables_can_be_dropped(old_case, tmp_path):
    path, sid = old_case
    target = str(tmp_path / "copy.db")
    Store(target, default_tags=DEFAULT_TAGS).close()
    s = Store(path, default_tags=DEFAULT_TAGS)
    try:
        assert [src["id"] for src in s.list_sources()] == [sid]
        # Copying out of an old case must not try to carry its saved views
        # into a target that has no table for them.
        s.copy_sources_to(target, [sid])
        s.drop_source(sid)
        assert s.list_sources() == []
    finally:
        s.close()
    # Left alone, not dropped: nothing reads it, and deleting rows out of
    # someone's case file to tidy up is not this change's call.
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0] == 1
    con.close()


def test_an_old_case_whose_only_work_is_a_saved_view_is_not_swept(old_case):
    """The quick-look janitor deletes case files holding no work. An old
    file's saved view still counts as work, so dropping the feature can't
    make such a file look empty."""
    import server
    path, sid = old_case
    con = sqlite3.connect(path)
    try:
        assert server._case_holds_work(con)
        con.execute("DELETE FROM saved_views")
        assert not server._case_holds_work(con)   # it was the saved view that counted
    finally:
        con.close()


def test_a_session_that_still_carries_saved_views_loads(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "sv.csv"), name="sv.csv", build_fts=False)["id"]
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag, True)
    data = store.export_case_session()
    assert "saved_views" not in data["sources"][0]
    data["sources"][0]["saved_views"] = [{"name": "alice only", "payload": {"filters": []}}]

    assert case_session_document_problem(data) is None
    store.set_tags(sid, [1], tag, False)
    store.import_case_session(data, merge=True)

    assert store.db.execute(
        "SELECT COUNT(*) FROM row_tags WHERE source_id=? AND rid=1", (sid,)).fetchone()[0] == 1

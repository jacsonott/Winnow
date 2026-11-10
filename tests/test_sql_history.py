"""The SQL pane's query history — a per-case table (sql_history) of what the
pane has run, one row per distinct query text, newest first, so the query
behind a number survives its tab being edited into the next idea. Covers
Store record/list/delete/clear, the cap, the /api/sql_history routes, and
that the history travels with the case file."""

from __future__ import annotations

import datetime

from winnow import store as store_module


def _texts(store):
    return [h["sql"] for h in store.list_sql_history()]


def test_history_starts_empty(store):
    assert store.list_sql_history() == []


def test_a_run_is_recorded_with_how_it_went(store):
    h = store.record_sql_history("SELECT 1", tab_name="Query 1", row_count=1, elapsed_ms=3)
    assert (h["sql"], h["tab_name"], h["row_count"], h["elapsed_ms"], h["runs"]) == \
        ("SELECT 1", "Query 1", 1, 3, 1)
    assert h["error"] is None and h["stopped"] is None
    # UTC with a Z, so "5 min ago" means the same on a desk in another zone.
    ran = datetime.datetime.strptime(h["ran_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    assert abs((datetime.datetime.now(datetime.timezone.utc) - ran).total_seconds()) < 60
    assert store.list_sql_history() == [h]


def test_a_blank_query_is_not_recorded(store):
    assert store.record_sql_history("   \n\t") is None
    assert store.record_sql_history("") is None
    assert store.list_sql_history() == []


def test_newest_run_first(store):
    for q in ("SELECT 1", "SELECT 2", "SELECT 3"):
        store.record_sql_history(q)
    assert _texts(store) == ["SELECT 3", "SELECT 2", "SELECT 1"]


def test_running_a_query_again_moves_it_to_the_top_instead_of_listing_it_twice(store):
    first = store.record_sql_history("SELECT 1", tab_name="a", row_count=5, elapsed_ms=10)
    store.record_sql_history("SELECT 2")
    # Surrounding whitespace is the one edit that can't change a query.
    again = store.record_sql_history("\n  SELECT 1  \n", tab_name="b", row_count=7, elapsed_ms=4)
    assert _texts(store) == ["SELECT 1", "SELECT 2"]
    assert again["runs"] == 2
    # The latest run's details replace the earlier ones.
    assert (again["tab_name"], again["row_count"], again["elapsed_ms"]) == ("b", 7, 4)
    # A new id, so id order is run order and a stale list's id for the old
    # row deletes nothing it shouldn't.
    assert again["id"] > first["id"]


def test_any_other_difference_is_a_different_query(store):
    store.record_sql_history("SELECT 1")
    store.record_sql_history("select 1")
    store.record_sql_history("SELECT 1 -- with a comment")
    assert len(store.list_sql_history()) == 3


def test_a_failed_run_keeps_its_error_and_no_counts(store):
    h = store.record_sql_history("SELECT Foo FROM src_1", row_count=0, elapsed_ms=2,
                                 stopped="cancelled", error="no such column: Foo")
    assert h["error"] == "no such column: Foo"
    assert (h["row_count"], h["elapsed_ms"], h["stopped"]) == (None, None, None)
    # Fixed and run again: the error goes with the run that had it.
    ok = store.record_sql_history("SELECT Foo FROM src_1", row_count=3, elapsed_ms=1)
    assert ok["error"] is None and ok["row_count"] == 3 and ok["runs"] == 2


def test_a_stopped_run_says_so(store):
    h = store.record_sql_history("SELECT * FROM big", row_count=120_000, elapsed_ms=900, stopped="cancelled")
    assert h["stopped"] == "cancelled" and h["row_count"] == 120_000


def test_long_errors_and_tab_names_are_clipped(store):
    h = store.record_sql_history("SELECT 1", tab_name="t" * 5000, error="e" * 5000)
    assert len(h["tab_name"]) == store_module.SQL_HISTORY_TEXT_MAX
    assert len(h["error"]) == store_module.SQL_HISTORY_TEXT_MAX


def test_the_oldest_runs_go_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(store_module, "SQL_HISTORY_LIMIT", 5)
    for i in range(8):
        store.record_sql_history(f"SELECT {i}")
    assert _texts(store) == [f"SELECT {i}" for i in (7, 6, 5, 4, 3)]
    # Re-running an old one keeps it: it is the newest run now.
    store.record_sql_history("SELECT 3")
    store.record_sql_history("SELECT 8")
    assert _texts(store) == [f"SELECT {i}" for i in (8, 3, 7, 6, 5)]


def test_delete_removes_only_that_query_and_tolerates_a_stale_id(store):
    a = store.record_sql_history("SELECT 1")
    store.record_sql_history("SELECT 2")
    store.delete_sql_history(a["id"])
    assert _texts(store) == ["SELECT 2"]
    store.delete_sql_history(a["id"])        # already gone: not an error
    store.delete_sql_history(99999)
    assert _texts(store) == ["SELECT 2"]


def test_ids_are_not_reused(store):
    """A list open in another window holds ids; a run recorded after the
    newest entry was deleted must not take that id, or the stale list's ✕
    would delete the wrong query."""
    store.record_sql_history("SELECT 1")
    b = store.record_sql_history("SELECT 2")
    store.delete_sql_history(b["id"])
    assert store.record_sql_history("SELECT 3")["id"] > b["id"]


def test_clear_empties_it_and_leaves_the_tabs_alone(store):
    store.create_sql_tab("logons", "SELECT 1")
    store.record_sql_history("SELECT 1")
    store.record_sql_history("SELECT 2")
    assert store.clear_sql_history() == 2
    assert store.list_sql_history() == []
    assert [t["name"] for t in store.list_sql_tabs()] == ["logons"]


def test_history_survives_reopening_the_case_file(tmp_path):
    path = str(tmp_path / "case.db")
    s1 = store_module.Store(path)
    s1.record_sql_history("SELECT * FROM src_1", tab_name="Query 1", row_count=50, elapsed_ms=2)
    s1.close()

    s2 = store_module.Store(path)
    try:
        assert [(h["sql"], h["tab_name"], h["row_count"]) for h in s2.list_sql_history()] == \
            [("SELECT * FROM src_1", "Query 1", 50)]
    finally:
        s2.close()


def test_a_case_with_only_history_in_it_holds_work(tmp_path):
    """The quick-look sweep keeps a case that holds anything an analyst
    did. A query they ran is that, the same as a query tab."""
    import sqlite3
    import server

    path = tmp_path / "q.db"
    s = store_module.Store(str(path))
    s.record_sql_history("SELECT 1")
    s.close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert server._case_holds_work(conn) is True
    finally:
        conn.close()


# --------------------------------------------------------------- HTTP routes

def test_sql_history_routes(client):
    c = client
    assert c.get("/api/sql_history").json() == []

    h = c.post("/api/sql_history", json={"sql": "SELECT 1", "tab_name": "Query 1",
                                         "row_count": 1, "elapsed_ms": 2}).json()
    assert (h["sql"], h["tab_name"], h["row_count"], h["runs"]) == ("SELECT 1", "Query 1", 1, 1)
    c.post("/api/sql_history", json={"sql": "SELECT nope", "error": "no such column: nope"})
    assert [x["sql"] for x in c.get("/api/sql_history").json()] == ["SELECT nope", "SELECT 1"]

    # A blank query records nothing and says so with null.
    assert c.post("/api/sql_history", json={"sql": "  "}).json() is None

    assert c.delete(f"/api/sql_history/{h['id']}").status_code == 200
    assert [x["sql"] for x in c.get("/api/sql_history").json()] == ["SELECT nope"]

    assert c.delete("/api/sql_history").json() == {"cleared": 1}
    assert c.get("/api/sql_history").json() == []

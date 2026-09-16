"""The table menu's "Hide empty rows": a view predicate that drops rows
whose every column is NULL or '' — on a standard table, a merge, through
the /api/view route — and that keeps an otherwise unfiltered view off
the root_virtual carve-out (invariant #2).
"""
ROWS = [["When", "Msg"], ["2024-01-01 10:00", "alpha"], ["", ""], ["2024-01-01 11:00", ""], ["", ""]]
ROWS_B = [["When", "Msg"], ["", ""], ["2024-01-02 10:00", "beta"]]


def _spec(sid, **extra):
    return {"source_id": sid, "filters": [], "sort": [], **extra}


def test_drops_all_empty_rows_and_materialises(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    plain = store.build_view(sid, _spec(sid))
    assert plain["row_count"] == 4 and store._views[plain["view_id"]]["kind"] == "root_virtual"
    v = store.build_view(sid, _spec(sid, hide_empty_rows=True))
    assert v["row_count"] == 2
    assert store._views[v["view_id"]]["kind"] == "root"   # a predicate, so never the virtual path
    rows = store.fetch_rows(v["view_id"], 0, 10)["rows"]
    assert [r["rid"] for r in rows] == [1, 3]             # a half-empty row stays
    assert rows[1]["cells"] == ["2024-01-01 11:00", ""]


def test_merge_parity(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS_B, "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    assert store.build_view(mid, _spec(mid))["row_count"] == 6
    v = store.build_view(mid, _spec(mid, hide_empty_rows=True))
    assert v["row_count"] == 3
    assert store.build_view(mid, _spec(mid, hide_empty_rows=True, sort=[{"column": "When", "dir": "asc"}]))["row_count"] == 3


def test_off_by_default_and_through_the_route(client, write_csv, monkeypatch):
    import server

    sid = server.STORE.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    assert client.post("/api/view", json={"source_id": sid}).json()["row_count"] == 4
    r = client.post("/api/view", json={"source_id": sid, "hide_empty_rows": True})
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 2


def test_spec_sql_carries_the_predicate(store, write_csv):
    """The SQL pane's rendering of the view goes through the same
    _compile_where, so "show me this as SQL" agrees with the grid."""
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS_B, "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    for sid, want in ((a, 2), (mid, 3)):
        sql = store.spec_sql(sid, _spec(sid, hide_empty_rows=True))
        assert "<> ''" in sql
        assert len(store.run_sql(sql)["rows"]) == want

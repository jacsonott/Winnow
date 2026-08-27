"""Derived columns queryable in the SQL pane: a source with derived
columns gets a TEMP VIEW shadowing its own src_<id> — the table as the
grid shows it — with main.src_<id> as the raw escape hatch."""

from __future__ import annotations


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv([["Uri"], ["https://evil.example/x"], ["https://ok.com/y"]],
                                   "a.csv"), name="a", build_fts=False)["id"]
    res = store.add_derived_column(a, "Host", "Uri", "regex_extract", {"pattern": r"://([^/]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return a


def test_derived_columns_query_bare(store, write_csv):
    a = _mk(store, write_csv)
    out = store.run_sql(f"SELECT Host FROM src_{a} WHERE Host LIKE '%evil%'")
    assert out["rows"] == [["evil.example"]]


def test_select_star_is_grid_shape_plus_annotations_and_rowid(store, write_csv):
    a = _mk(store, write_csv)
    assert store.run_sql(f"SELECT * FROM src_{a} LIMIT 1")["columns"] == [
        "rid", "Uri", "Host", "Tags", "Note", "rowid"]


def test_rowid_still_reads_and_joins(store, write_csv):
    """A view has no real rowid (NULL on 3.45, an error on 3.46+) — the
    explicit alias keeps pre-existing rowid queries and joins correct."""
    a = _mk(store, write_csv)
    out = store.run_sql(f"SELECT rowid FROM src_{a} ORDER BY rowid")
    assert [r[0] for r in out["rows"]] == [1, 2]
    joined = store.run_sql(
        f"SELECT COUNT(*) FROM src_{a} v JOIN main.src_{a} raw ON raw.rowid = v.rowid")
    assert joined["rows"][0][0] == 2


def test_spec_sql_runs_in_the_pane_with_derived_columns(store, write_csv):
    """'Open filter in SQL pane' on a source with derived columns: the
    emitted SQL references the pane's bare src_N (the shadow does the
    joining) — spelling the sidecar join would be ambiguous against it."""
    a = _mk(store, write_csv)
    sql = store.spec_sql(a, {"source_id": a, "sort": [],
                             "filters": [{"column": "Host", "op": "contains", "value": "evil"}]})
    out = store.run_sql(sql)
    assert len(out["rows"]) == 1
    assert "drv_" not in sql


def test_merge_spec_sql_runs_in_the_pane_with_derived_columns(store, write_csv):
    a = _mk(store, write_csv)
    b = store.ingest_csv(write_csv([["Uri"], ["https://evil.example/z"]], "m2.csv"),
                         name="m2", build_fts=False)["id"]
    res = store.add_derived_column(b, "Host", "Uri", "regex_extract", {"pattern": r"://([^/]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    mid = store.create_merge("sm", [a, b])["id"]
    sql = store.spec_sql(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "Host", "op": "equals", "value": "evil.example"}]})
    out = store.run_sql(sql)
    assert len(out["rows"]) == 2  # one per member


def test_main_prefix_is_the_raw_import(store, write_csv):
    a = _mk(store, write_csv)
    assert store.run_sql(f"SELECT * FROM main.src_{a} LIMIT 1")["columns"] == ["rid", "Uri"]


def test_sources_without_derived_still_get_annotations(store, write_csv):
    """Every source has a pane view now — Tags/Note are universal, and the
    raw import stays one main. prefix away."""
    b = store.ingest_csv(write_csv([["X"], ["1"]], "b.csv"), name="b", build_fts=False)["id"]
    assert store.run_sql(f"SELECT * FROM src_{b}")["columns"] == ["rid", "X", "Tags", "Note", "rowid"]
    assert store.run_sql(f"SELECT * FROM main.src_{b}")["columns"] == ["rid", "X"]


def test_unused_sidecar_join_is_eliminated(store, write_csv):
    """The shadow view must cost nothing when derived columns aren't
    referenced — the LEFT JOIN is on drv's PRIMARY KEY, which SQLite
    drops from the plan entirely."""
    a = _mk(store, write_csv)
    plan = store.run_sql(f"EXPLAIN QUERY PLAN SELECT Uri FROM src_{a}")
    assert not any("drv" in " ".join(map(str, r)) for r in plan["rows"]), plan["rows"]


def test_merge_views_coexist_with_shadowed_members(store, write_csv):
    """merge_N's branches are main.-qualified — an unqualified src_N in
    the merge view would bind to the member's SHADOW view, double-join
    the sidecar, and make every derived name ambiguous."""
    a = _mk(store, write_csv)
    b = store.ingest_csv(write_csv([["Uri"], ["https://third.net/z"]], "c.csv"),
                         name="c", build_fts=False)["id"]
    res = store.add_derived_column(b, "Host", "Uri", "regex_extract", {"pattern": r"://([^/]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    mid = store.create_merge("m", [a, b])["id"]
    out = store.run_sql(f"SELECT source_id, Host FROM merge_{-mid} ORDER BY 1, 2")
    assert out["rows"] == [[a, "evil.example"], [a, "ok.com"], [b, "third.net"]]

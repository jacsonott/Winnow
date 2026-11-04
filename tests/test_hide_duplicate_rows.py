"""The table menu's "Hide duplicate rows": a view option that keeps one row
per set of rows matching in every compared column (`spec.dedupe_columns`,
which the client fills with the visible columns) — the first by Line, or
on a merge the first by member order and then Line. Applied after the
filters, on a standard table and a merge alike (invariant #9), through the
/api/view route, and in the SQL the "open this filter as a query" action
renders, so the SQL pane agrees with the grid.
"""
import csv
import io

ALL = ["When", "Host", "Msg", "SourceFile"]
NO_FILE = ["When", "Host", "Msg"]

ROWS = [
    ALL,
    ["2024-01-01 10:00", "H1", "alpha", "f1"],   # 1
    ["2024-01-01 10:00", "H1", "alpha", "f2"],   # 2: 1 again, from another file
    ["2024-01-01 11:00", "H2", "beta", "f1"],    # 3
    ["2024-01-01 10:00", "H1", "alpha", "f1"],   # 4: 1 again, exactly
    ["2024-01-01 11:00", "H2", "BETA", "f1"],    # 5: 3 in other case — not a duplicate
    ["", "", "", "f1"],                          # 6
    ["", "", "", "f2"],                          # 7: 6 again, from another file
]
# No SourceFile column: a merge reads it as NULL for these rows.
ROWS_B = [NO_FILE, ["2024-01-01 10:00", "H1", "alpha"], ["2024-01-03 09:00", "H3", "gamma"]]


def _spec(sid, **extra):
    return {"source_id": sid, "filters": [], "sort": [], **extra}


def _rids(store, sid, **extra):
    v = store.build_view(sid, _spec(sid, **extra))
    rows = store.fetch_rows(v["view_id"], 0, 100)["rows"]
    if sid < 0:
        return [(r["source_id"], r["rid"]) for r in rows]
    return [r["rid"] for r in rows]


def _ingest(store, write_csv, rows, name):
    return store.ingest_csv(write_csv(rows, name), name=name.split(".")[0], build_fts=False)["id"]


def test_every_column_compared_drops_only_exact_repeats(store, write_csv):
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    v = store.build_view(sid, _spec(sid, dedupe_columns=ALL))
    assert v["row_count"] == 6
    # Never the virtual root: that path's positions are only exact when the
    # view is every row of the source (invariant #2).
    assert store._views[v["view_id"]]["kind"] == "root"
    assert _rids(store, sid, dedupe_columns=ALL) == [1, 2, 3, 5, 6, 7]


def test_a_column_left_out_is_not_compared(store, write_csv):
    """The point of comparing the visible columns: hiding SourceFile lets
    the same event from two files collapse, empty rows included."""
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    assert _rids(store, sid, dedupe_columns=NO_FILE) == [1, 3, 5, 6]


def test_the_first_by_line_is_kept_whatever_the_sort(store, write_csv):
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    by_file_desc = [{"column": "SourceFile", "dir": "desc"}]
    # f2 sorts first, but the copies kept are still rows 1 and 6, not 2 and 7.
    assert _rids(store, sid, dedupe_columns=NO_FILE, sort=by_file_desc) == [1, 3, 5, 6]
    # And the kept rows come out in the view's order.
    by_msg_desc = [{"column": "Msg", "dir": "desc"}]
    assert _rids(store, sid, dedupe_columns=NO_FILE, sort=by_msg_desc) == [3, 5, 1, 6]
    assert _rids(store, sid, dedupe_columns=NO_FILE,
                 sort=[{"column": "__line__", "dir": "desc"}]) == [6, 5, 3, 1]


def test_filters_apply_first(store, write_csv):
    """Duplicates are looked for among the rows the filters leave, so a
    filter that drops row 1 keeps row 2 rather than leaving nothing."""
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    only_f2 = [{"column": "SourceFile", "op": "equals", "value": "f2"}]
    assert _rids(store, sid, dedupe_columns=NO_FILE, filters=only_f2) == [2, 7]
    # The search box's scan path compiles to a list of rids; same rule.
    assert _rids(store, sid, dedupe_columns=NO_FILE, search="alpha") == [1]
    # And alongside Hide empty rows, which is a filter.
    assert _rids(store, sid, dedupe_columns=NO_FILE, hide_empty_rows=True) == [1, 3, 5, 6]


def test_unknown_columns_are_ignored_and_none_left_is_off(store, write_csv):
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    assert _rids(store, sid, dedupe_columns=["Host", "Gone"]) == [1, 3, 6]
    v = store.build_view(sid, _spec(sid, dedupe_columns=["Gone"]))
    assert v["row_count"] == 7 and store._views[v["view_id"]]["kind"] == "root_virtual"
    assert store.build_view(sid, _spec(sid, dedupe_columns=[]))["row_count"] == 7


def test_a_derived_column_is_compared_like_any_other(store, write_csv):
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    res = store.add_derived_column(sid, "Letter", "Msg", "regex_extract", {"pattern": r"^(\w)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    # a, b, B, and empty (NULL: the regex found nothing in an empty cell).
    assert _rids(store, sid, dedupe_columns=["Letter"]) == [1, 3, 5, 6]
    assert _rids(store, sid, dedupe_columns=["Letter"], sort=[{"column": "Letter", "dir": "desc"}]) == [3, 5, 1, 6]


def test_merge_parity(store, write_csv):
    a = _ingest(store, write_csv, ROWS, "a.csv")
    b = _ingest(store, write_csv, ROWS_B, "b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    assert store.build_view(mid, _spec(mid))["row_count"] == 9
    # b's first row repeats a's first: a comes first in the merge, so a's stays.
    assert _rids(store, mid, dedupe_columns=NO_FILE) == [(a, 1), (b, 2), (a, 3), (a, 5), (a, 6)]
    # Comparing SourceFile too, b's copy differs (NULL is not 'f1') and stays;
    # rows tying on rid keep member order, as an ordinary merge view does.
    assert _rids(store, mid, dedupe_columns=ALL) == [(a, 1), (b, 1), (a, 2), (b, 2), (a, 3), (a, 5), (a, 6), (a, 7)]
    assert _rids(store, mid, dedupe_columns=NO_FILE, sort=[{"column": "When", "dir": "desc"}]) == \
        [(b, 2), (a, 3), (a, 5), (a, 1), (a, 6)]
    # Filtered too.
    assert _rids(store, mid, dedupe_columns=NO_FILE, filters=[{"column": "Host", "op": "equals", "value": "H1"}]) == [(a, 1)]
    # Member order decides which copy is kept.
    rev = store.create_merge("reversed", [b, a])["id"]
    assert _rids(store, rev, dedupe_columns=NO_FILE) == [(b, 1), (b, 2), (a, 3), (a, 5), (a, 6)]


def test_empty_and_missing_are_the_same_value(store, write_csv):
    """A merge reads a column a member lacks as NULL, and the other member
    has '' there: both show as an empty cell, so they are duplicates."""
    a = _ingest(store, write_csv, [["X", "Y"], ["v", ""]], "a.csv")
    b = _ingest(store, write_csv, [["X"], ["v"]], "b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    assert _rids(store, mid, dedupe_columns=["X", "Y"]) == [(a, 1)]


def test_the_merge_view_cache_is_not_used(store, write_csv):
    """An unfiltered merge view is kept and handed back for the same SELECT
    (_merge_view_cache); a deduplicated one must not be confused with it."""
    a = _ingest(store, write_csv, ROWS, "a.csv")
    b = _ingest(store, write_csv, ROWS_B, "b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    assert store.build_view(mid, _spec(mid))["row_count"] == 9
    assert store.build_view(mid, _spec(mid, dedupe_columns=NO_FILE))["row_count"] == 5
    assert store.build_view(mid, _spec(mid))["row_count"] == 9


def test_what_reads_the_view_sees_the_kept_rows(store, write_csv):
    """Grouping, export and tagging the whole view all read the view, so
    they count, write and tag only the rows it kept."""
    sid = _ingest(store, write_csv, ROWS, "a.csv")
    v = store.build_view(sid, _spec(sid, dedupe_columns=NO_FILE))
    groups = {g["value"]: g["count"] for g in store.group_summary(v["view_id"], "Host")["groups"]}
    assert groups == {"H1": 1, "H2": 2, "": 1}
    body = "".join(store.export_view_csv(v["view_id"]))
    lines = list(csv.reader(io.StringIO(body)))
    assert [r[0] for r in lines[1:]] == ["1", "3", "5", "6"]
    tag = store.list_tags()[0]["id"]
    assert store.tag_view(v["view_id"], tag, True)["changed"] == 4
    assert store.tag_counts(sid)["counts"] == {str(tag): 4}


def test_through_the_route(client, write_csv):
    import server

    sid = server.STORE.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    assert client.post("/api/view", json={"source_id": sid}).json()["row_count"] == 7
    r = client.post("/api/view", json={"source_id": sid, "dedupe_columns": NO_FILE})
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 4


def test_spec_sql_agrees_with_the_grid(store, write_csv):
    """Q (the current filter as a query) renders the same rows, in the
    same order, for a table and for a merge."""
    a = _ingest(store, write_csv, ROWS, "a.csv")
    b = _ingest(store, write_csv, ROWS_B, "b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    for sid in (a, mid):
        for extra in ({}, {"sort": [{"column": "When", "dir": "desc"}]},
                      {"filters": [{"column": "Msg", "op": "contains", "value": "a"}]}):
            spec = _spec(sid, dedupe_columns=NO_FILE, **extra)
            sql = store.spec_sql(sid, spec)
            assert "GROUP BY" in sql
            got = store.run_sql(sql)
            rid_at = got["columns"].index("rid")
            want = _rids(store, sid, dedupe_columns=NO_FILE, **extra)
            if sid < 0:
                sid_at = got["columns"].index("source_id")
                assert [(r[sid_at], r[rid_at]) for r in got["rows"]] == want, (sid, extra)
            else:
                assert [r[rid_at] for r in got["rows"]] == want, (sid, extra)

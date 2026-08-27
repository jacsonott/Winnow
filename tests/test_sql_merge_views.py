"""Merges queryable in the SQL pane: run_sql exposes each merge as a TEMP
VIEW merge_<id> (source_id, rid, every exposed column) on its own
read-only connection — the case file is never written."""

from __future__ import annotations


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv([["X", "Y"], ["1", "a"], ["2", "b"]], "a.csv"),
                         name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["X", "Y"], ["3", "c"]], "b.csv"),
                         name="b", build_fts=False)["id"]
    return a, b, store.create_merge("m", [a, b])["id"]


def test_merge_view_is_queryable_by_name(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    out = store.run_sql(f"SELECT source_id, rid, X FROM merge_{-mid} ORDER BY source_id, rid")
    assert out["columns"] == ["source_id", "rid", "X"]
    assert out["rows"] == [[a, 1, "1"], [a, 2, "2"], [b, 1, "3"]]


def test_merge_view_carries_derived_columns(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(mid, "Xd", "X", "regex_extract", {"pattern": "(.)"})
    for j in res["job_ids"]:
        store.wait_for_ingest_job(j, timeout=30)
    out = store.run_sql(f"SELECT Xd FROM merge_{-mid} ORDER BY Xd")
    assert [r[0] for r in out["rows"]] == ["1", "2", "3"]


def test_merge_view_aggregates_and_joins_row_tags(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    tag = store.list_tags()[0]
    store.set_tags(a, [1], tag["id"], True)
    out = store.run_sql(
        f"SELECT COUNT(*) FROM merge_{-mid} m "
        f"JOIN row_tags rt ON rt.source_id = m.source_id AND rt.rid = m.rid")
    assert out["rows"][0][0] == 1


def test_plain_tables_and_caseless_queries_are_unaffected(store, write_csv):
    a, _, _ = _mk(store, write_csv)
    out = store.run_sql(f"SELECT COUNT(*) FROM src_{a}")
    assert out["rows"][0][0] == 2

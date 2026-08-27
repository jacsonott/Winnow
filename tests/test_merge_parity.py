"""Invariant #9 gap burn-down: raw SQL filter fragments and batch derived
adds on merged tables — both previously refused outright."""

from __future__ import annotations

import pytest

A_ROWS = [["When", "Msg"], ["2024-01-01 10:00", "alpha beacon"], ["2024-01-01 11:00", "beta"]]
B_ROWS = [["When", "Msg"], ["2024-01-02 10:00", "gamma beacon"], ["2024-01-02 11:00", "delta"]]


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    return a, b, store.create_merge("both", [a, b])["id"]


# ------------------------------------------------------------- raw SQL

def test_raw_sql_fragment_filters_a_merge(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Msg\" LIKE '%beacon%'"}})
    assert v["row_count"] == 2  # one from each member


def test_raw_sql_fragment_reaches_a_merge_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(mid, "Word", "Msg", "regex_extract", {"pattern": r"^(\w+)"})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Word\" = 'beta'"}})
    assert v["row_count"] == 1


def test_merge_raw_sql_validates_and_rejects_like_a_table(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    store.validate_where_fragment(mid, "\"Msg\" LIKE '%a%'")  # must not raise
    # Bare unknown identifiers hit the allowlist, same as on a plain table.
    # (A double-quoted unknown falls into SQLite's string-literal misfeature
    # on plain tables too — parity means matching that, not out-stricting it.)
    with pytest.raises(ValueError, match="Unknown identifier"):
        store.validate_where_fragment(mid, "Nope = 1")
    with pytest.raises(ValueError):
        store.validate_where_fragment(mid, "1; DROP TABLE x")


def test_merge_spec_sql_carries_the_raw_fragment(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    sql = store.spec_sql(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Msg\" LIKE '%beacon%'"}})
    assert "beacon" in sql
    res = store.run_sql(sql)
    assert len(res["rows"]) == 2


def test_member_only_derived_stays_invisible_to_merge_raw_sql(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(a, "OnlyA", "Msg", "regex_extract", {"pattern": "(a)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    # not exposed on the merge (partial coverage) — the identifier check
    # runs against the merge's exposed columns, so a bare reference to one
    # member's private derived column is rejected up front.
    with pytest.raises(ValueError, match="Unknown identifier"):
        store.validate_where_fragment(mid, "OnlyA = 'a'")


# ------------------------------------------------------ batch derived add

def test_batch_derived_add_fans_out_to_every_member(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = store.add_derived_columns(mid, [
        {"name": "W1", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": r"^(\w+)"}},
        {"name": "W2", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": r"(\w+)$"}},
    ])
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    cols = [c["name"] for c in store.get_source(mid)["columns"]]
    assert "W1" in cols and "W2" in cols
    for sid in (a, b):
        assert {d["name"] for d in store.list_derived_columns(sid)} >= {"W1", "W2"}


def test_batch_add_is_all_or_nothing_across_members(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    clash = store.add_derived_column(b, "Taken", "Msg", "regex_extract", {"pattern": "(a)"})
    store.wait_for_ingest_job(clash["job_id"], timeout=30)
    with pytest.raises(ValueError, match="already has a column"):
        store.add_derived_columns(mid, [
            {"name": "Fine", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": "(a)"}},
            {"name": "Taken", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": "(a)"}},
        ])
    assert "Fine" not in [c["name"] for c in store.get_source(a)["columns"]]

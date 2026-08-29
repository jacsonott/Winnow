"""Derived columns on merged tables: creating on the merge fans out to
every member, the merge exposes a derived column only when EVERY member
has one under that name (each read through its own drv_ sidecar), and
every read path — views, filters, sort, grouping, values, export —
resolves it per member."""

from __future__ import annotations

import pytest

A_ROWS = [["When", "Who"],
          ["2024-01-01 10:00:00", "u=alice;h=SRV1"],
          ["2024-01-01 11:00:00", "u=bob;h=SRV1"]]
B_ROWS = [["When", "Who"],
          ["2024-01-02 09:00:00", "u=carol;h=WKS2"],
          ["2024-01-02 09:30:00", "u=alice;h=WKS2"]]

RX = {"pattern": r"u=(\w+)"}


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])
    return a, b, merge["id"]


def _fanout(store, mid, name="User", column="Who", params=RX):
    res = store.add_derived_column(mid, name, column, "regex_extract", params)
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    return res


def test_fanout_creates_on_every_member_and_the_merge_exposes_it(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = _fanout(store, mid)
    assert [d["source_id"] for d in res["member_definitions"]] == [a, b]
    cols = store.get_source(mid)["columns"]
    user = next(c for c in cols if c["name"] == "User")
    assert user.get("derived")
    # the values come from each member's own sidecar
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [{"column": "When", "dir": "asc"}]})
    rows = store.fetch_rows(v["view_id"], 0, 10)["rows"]
    idx = [c["name"] for c in cols].index("User")
    assert [r["cells"][idx] for r in rows] == ["alice", "bob", "carol", "alice"]


def test_merge_filters_and_sorts_on_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "User", "op": "equals", "value": "alice"}]})
    assert v["row_count"] == 2
    v2 = store.build_view(mid, {"source_id": mid, "filters": [],
                                "sort": [{"column": "User", "dir": "asc"}]})
    rows = store.fetch_rows(v2["view_id"], 0, 10)["rows"]
    cols = [c["name"] for c in store.get_source(mid)["columns"]]
    assert [r["cells"][cols.index("User")] for r in rows][:2] == ["alice", "alice"]


def test_merge_groups_by_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    groups = store.group_summary(v["view_id"], "User")["groups"]
    assert {g["value"]: g["count"] for g in groups} == {"alice": 2, "bob": 1, "carol": 1}


def test_partial_coverage_is_not_exposed(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(a, "OnlyA", "Who", "regex_extract", RX)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert "OnlyA" not in [c["name"] for c in store.get_source(mid)["columns"]]
    # ...and views over the merge still build fine
    assert store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})["row_count"] == 4


def test_fanout_is_all_or_nothing_on_a_member_collision(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = store.add_derived_column(b, "Clash", "Who", "regex_extract", RX)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    with pytest.raises(ValueError, match="already has a column"):
        store.add_derived_column(mid, "Clash", "Who", "regex_extract", RX)
    # member a was left untouched by the failed fan-out
    assert "Clash" not in [c["name"] for c in store.get_source(a)["columns"]]


def test_merge_lists_previews_and_values_for_derived(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    assert [d["name"] for d in store.list_derived_columns(mid)] == ["User"]
    prev = store.preview_derived(mid, "Who", "regex_extract", RX)
    assert prev["preview"][0]["output"] == "alice"
    vals = {v["value"] for v in store.column_values(mid, "User")}
    assert vals == {"alice", "bob", "carol"}


def test_merge_export_carries_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    text = "".join(store.export_view_csv(v["view_id"]))
    lines = text.strip().splitlines()
    assert lines[0].endswith(",User")
    assert lines[1].endswith(",alice")

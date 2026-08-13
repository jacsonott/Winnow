"""Case-file maintenance the analyst can see and act on: the auto-created
per-column filter indexes (listing/dropping them) and compact() / VACUUM.

Both exist because everything else in the app only ever *adds* to the case
file — indexes appear behind the analyst's back and freed pages go to
SQLite's freelist rather than back to the OS."""

from __future__ import annotations

import os

import pytest

from store import Store


def test_column_index_listed_after_a_sargable_filter(ingested):
    store, source_id = ingested
    assert store.list_column_indexes(source_id) == []
    spec = {
        "source_id": source_id,
        "filters": [{"column": "EventId", "op": "equals", "value": "4624"}],
        "sort": [],
    }
    store.build_view(source_id, spec)
    assert store.wait_for_column_index(source_id, "EventId", timeout=5)
    listed = store.list_column_indexes(source_id)
    assert [ix["column"] for ix in listed] == ["EventId"]
    assert listed[0]["building"] is False


def test_column_values_triggers_an_index(ingested):
    # The value-picker dropdown's own GROUP BY is the shape a plain index
    # serves best (covering index scan, no temp b-tree) — and it's the
    # index an equals filter on the same column wants next anyway.
    store, source_id = ingested
    store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)
    assert [ix["column"] for ix in store.list_column_indexes(source_id)] == ["Process"]


def test_group_summary_triggers_an_index_only_on_a_whole_source_view(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    store.group_summary(view["view_id"], "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)


def test_group_summary_agrees_on_both_the_direct_and_joined_paths(ingested):
    # The unfiltered fast path skips the view join entirely; a filtered view
    # goes through it. Same question, same answer.
    store, source_id = ingested
    whole = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    direct = store.group_summary(whole["view_id"], "User")

    # A filter that matches everything: row counts still line up with the
    # source's, so _grouping_covers_whole_source takes the direct path here
    # too — and that's fine, it's the same row set either way.
    matches_all = store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "User", "op": "contains", "value": ""}],
        "sort": [],
    })
    assert store.group_summary(matches_all["view_id"], "User") == direct

    narrowed = store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "User", "op": "contains", "value": "ACME"}],
        "sort": [],
    })
    joined = store.group_summary(narrowed["view_id"], "User")
    assert sum(g["count"] for g in joined["groups"]) == 3
    assert all(g["value"].startswith("ACME") for g in joined["groups"])


def test_group_summary_datetime_still_buckets_on_the_fast_path(ingested):
    # A datetime column groups by DAY_BUCKET whichever branch runs.
    store, source_id = ingested
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    groups = store.group_summary(view["view_id"], "Timestamp")["groups"]
    assert {g["value"] for g in groups} == {"2024-01-05", "2024-01-06", "2024-01-07"}


def test_drop_column_index_and_rebuild(ingested):
    store, source_id = ingested
    store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)
    store.drop_column_index(source_id, "Process")
    assert store.list_column_indexes(source_id) == []
    # Nothing broke: the same query still answers correctly, and rebuilds it.
    assert store.column_values(source_id, "Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)


def test_drop_column_index_rejects_an_unknown_column(ingested):
    store, source_id = ingested
    with pytest.raises(KeyError):
        store.drop_column_index(source_id, "NoSuchColumn")


def test_list_column_indexes_is_empty_for_a_merge(store, write_csv):
    rows = [["A", "B"], ["1", "2"], ["3", "4"]]
    a = store.ingest_csv(write_csv(rows, "a.csv"), name="a", build_fts=False)
    b = store.ingest_csv(write_csv(rows, "b.csv"), name="b", build_fts=False)
    merge = store.create_merge("m", [a["id"], b["id"]])
    assert store.list_column_indexes(merge["id"]) == []


def test_compact_reclaims_space_after_a_source_is_dropped(store, tmp_path):
    path = tmp_path / "big.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("A,B\n")
        for i in range(20000):
            f.write(f"{i},{'x' * 120}\n")
    rec = store.ingest_csv(str(path), name="big", build_fts=False)

    grown = store.compact()
    assert grown["after_bytes"] > 1_000_000

    store.drop_source(rec["id"])
    freed = store.compact()
    assert freed["before_bytes"] == grown["after_bytes"]  # checkpointed on both sides
    assert freed["after_bytes"] < grown["after_bytes"] / 2
    assert freed["reclaimed_bytes"] == freed["before_bytes"] - freed["after_bytes"]


def test_compact_leaves_live_views_and_data_alone(ingested):
    store, source_id = ingested
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    before = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    store.compact()
    # Views live in the temp-attached `v` database; a bare VACUUM only
    # rewrites `main`, so they must survive it intact.
    after = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    assert [r["cells"] for r in after] == [r["cells"] for r in before]


def test_compact_refuses_without_enough_free_disk(ingested, monkeypatch):
    import shutil as shutil_module

    import store as store_module

    store, _ = ingested
    fake = shutil_module.disk_usage(".")._replace(free=1024)
    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda _p: fake)
    with pytest.raises(ValueError, match="free disk space"):
        store.compact()


def test_compact_restores_temp_store_to_memory(ingested):
    # VACUUM's scratch copy obeys temp_store, so compact() forces FILE for
    # the duration — but leaving it there would push every later sort and
    # temp b-tree onto disk.
    store, _ = ingested
    store.compact()
    assert store.db.execute("PRAGMA temp_store").fetchone()[0] == 2  # 2 == MEMORY

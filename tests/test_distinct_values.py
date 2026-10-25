"""Store.distinct_values and /api/distinct_values — the whole-view half of
the grid's "Copy unique values": one column's distinct non-empty values
over the current view, sorted the way the column sorts, complete or
flagged `truncated` (never silently cut short)."""

from __future__ import annotations

import pytest

from winnow.store import TAG_GROUP_COLUMN

ROWS = [
    ["When", "Host", "Bytes", "User"],
    ["2024-01-05 13:22:01", "ws02", "10", "alice"],
    ["2024-01-05 19:04:00", "WS01", "9", ""],
    ["2024-01-06 08:00:00", "ws02", "100", "bob"],
    ["2024-01-06 08:00:00", "dc01", "9", "alice"],
]


def _view(store, write_csv, filters=None):
    rec = store.ingest_csv(write_csv(ROWS, "u.csv"), name="u.csv", build_fts=False)
    return store.build_view(rec["id"], {"source_id": rec["id"], "filters": filters or [], "sort": []})


def test_values_are_distinct_and_sorted_the_way_the_column_sorts(store, write_csv):
    v = _view(store, write_csv)
    # Text: case-insensitive, like the grid's own sort and group_summary's.
    assert store.distinct_values(v["view_id"], "Host") == {"values": ["dc01", "WS01", "ws02"], "truncated": False}
    # A number column sorts as numbers — "9" before "10" before "100".
    assert store.distinct_values(v["view_id"], "Bytes")["values"] == ["9", "10", "100"]


def test_timestamps_are_the_stored_values_not_day_buckets(store, write_csv):
    """group_summary day-buckets a datetime column by default. A bucket is
    not a value the column holds, and the whole point here is pasting the
    real ones somewhere else."""
    v = _view(store, write_csv)
    assert store.distinct_values(v["view_id"], "When")["values"] == [
        "2024-01-05 13:22:01", "2024-01-05 19:04:00", "2024-01-06 08:00:00",
    ]


def test_empty_values_are_left_out(store, write_csv):
    v = _view(store, write_csv)
    assert store.distinct_values(v["view_id"], "User")["values"] == ["alice", "bob"]


def test_only_the_rows_the_view_shows(store, write_csv):
    v = _view(store, write_csv, [{"column": "Host", "op": "equals", "value": "ws02"}])
    assert store.distinct_values(v["view_id"], "User")["values"] == ["alice", "bob"]
    assert store.distinct_values(v["view_id"], "Bytes")["values"] == ["10", "100"]


def test_over_the_cap_says_truncated_rather_than_cutting_short(store, write_csv):
    v = _view(store, write_csv)
    res = store.distinct_values(v["view_id"], "Host", limit=2)
    assert res["truncated"] is True
    assert len(res["values"]) == 2


def test_empties_do_not_count_toward_the_cap(store, write_csv):
    """User has two real values plus an empty one. A cap of exactly two
    must hand both back complete — the empty group is dropped, not counted
    as the value that tipped it over."""
    v = _view(store, write_csv)
    assert store.distinct_values(v["view_id"], "User", limit=2) == {"values": ["alice", "bob"], "truncated": False}


def test_merged_view_takes_values_from_every_member(store, write_csv):
    a = store.ingest_csv(write_csv([["Host"], ["ws02"], ["ws01"]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Host"], ["dc01"], ["ws01"]], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    assert store.distinct_values(v["view_id"], "Host")["values"] == ["dc01", "ws01", "ws02"]


def test_derived_column_values_come_from_its_sidecar(store, write_csv):
    rec = store.ingest_csv(write_csv(ROWS, "u.csv"), name="u.csv", build_fts=False)
    res = store.add_derived_column(rec["id"], "Prefix", "Host", "regex_extract", {"pattern": r"^([a-z]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    v = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    # WS01 has no lowercase prefix, so it extracts nothing and drops out as empty.
    assert store.distinct_values(v["view_id"], "Prefix")["values"] == ["dc", "ws"]


def test_not_a_column_is_a_key_error(store, write_csv):
    v = _view(store, write_csv)
    with pytest.raises(KeyError):
        store.distinct_values(v["view_id"], "Nope")
    # Grouping's tag sentinel is not a column either: its values are tag ids.
    with pytest.raises(KeyError):
        store.distinct_values(v["view_id"], TAG_GROUP_COLUMN)


def test_route_returns_the_list_and_409s_what_it_cannot_answer(client, store, write_csv):
    v = _view(store, write_csv)
    r = client.get(f"/api/distinct_values?view_id={v['view_id']}&column=Host")
    assert r.status_code == 200
    assert r.json() == {"values": ["dc01", "WS01", "ws02"], "truncated": False}

    assert client.get(f"/api/distinct_values?view_id={v['view_id']}&column=Nope").status_code == 409
    store.close_view(v["view_id"])
    assert client.get(f"/api/distinct_values?view_id={v['view_id']}&column=Host").status_code == 409

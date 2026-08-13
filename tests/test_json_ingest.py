"""store.py's JSON/JSONL ingest path: the three flatten modes (none/full/
depth-N), arrays always staying as a JSON-string blob regardless of depth,
ragged/varying-shape records, and .jsonl vs a single .json document."""

from __future__ import annotations

import json

import pytest

from store import _flatten_json, _json_leaf_text


RECORDS = [
    {"id": 1, "user": {"name": "alice", "address": {"city": "Springfield", "zip": "11111"}}, "tags": ["a", "b"]},
    {"id": 2, "user": {"name": "bob"}, "tags": []},
    {"id": 3, "user": {"name": "carol", "address": {"city": "Shelbyville"}}, "note": "only on this record"},
]


@pytest.fixture
def json_array_file(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(RECORDS))
    return str(path)


@pytest.fixture
def jsonl_file(tmp_path):
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS))
    return str(path)


def test_flatten_json_none_mode_keeps_nested_values_as_json_text():
    flat = _flatten_json(RECORDS[0], max_depth=0)
    assert flat["id"] == "1"
    assert json.loads(flat["user"]) == RECORDS[0]["user"]
    assert json.loads(flat["tags"]) == ["a", "b"]


def test_flatten_json_full_mode_unfolds_every_nested_object():
    flat = _flatten_json(RECORDS[0], max_depth=None)
    assert flat == {
        "id": "1",
        "user.name": "alice",
        "user.address.city": "Springfield",
        "user.address.zip": "11111",
        "tags": '["a", "b"]',  # arrays never unfold, at any depth
    }


def test_flatten_json_depth_1_unfolds_one_level_only():
    flat = _flatten_json(RECORDS[0], max_depth=1)
    assert flat["user.name"] == "alice"
    assert json.loads(flat["user.address"]) == {"city": "Springfield", "zip": "11111"}
    assert "user.address.city" not in flat


def test_json_leaf_text_handles_bool_none_and_containers():
    assert _json_leaf_text(None) == ""
    assert _json_leaf_text(True) == "true"
    assert _json_leaf_text(False) == "false"
    assert _json_leaf_text(42) == "42"
    assert json.loads(_json_leaf_text({"a": 1})) == {"a": 1}
    assert json.loads(_json_leaf_text([1, 2])) == [1, 2]


def test_ingest_json_none_mode_one_column_per_top_level_key(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="none.json", flatten_mode="none", build_fts=False)
    assert rec["row_count"] == 3
    assert [c["name"] for c in rec["columns"]] == ["id", "user", "tags", "note"]
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "id", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    assert json.loads(rows[0]["cells"][1]) == {"name": "alice", "address": {"city": "Springfield", "zip": "11111"}}
    assert rows[2]["cells"][3] == "only on this record"  # note, present only on record 3
    assert rows[0]["cells"][3] == ""  # note, absent on record 1 -> padded empty


def test_ingest_json_full_mode_unfolds_objects_but_not_arrays(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="full.json", flatten_mode="full", build_fts=False)
    colnames = [c["name"] for c in rec["columns"]]
    assert colnames == ["id", "user.name", "user.address.city", "user.address.zip", "tags", "note"]
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "id", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    by_col = dict(zip(colnames, rows[0]["cells"]))
    assert by_col["user.name"] == "alice"
    assert by_col["user.address.city"] == "Springfield"
    assert by_col["tags"] == '["a", "b"]'
    # Record 2 has no address at all -> padded empty, not an error
    by_col_r2 = dict(zip(colnames, rows[1]["cells"]))
    assert by_col_r2["user.address.city"] == ""


def test_ingest_json_depth_limits_flattening(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="depth1.json", flatten_mode="depth", flatten_depth=1, build_fts=False)
    colnames = [c["name"] for c in rec["columns"]]
    assert "user.address" in colnames
    assert "user.address.city" not in colnames


def test_ingest_jsonl_streams_one_record_per_line(store, jsonl_file):
    rec = store.ingest_json(jsonl_file, name="data.jsonl", flatten_mode="full", build_fts=False)
    assert rec["row_count"] == 3
    assert "user.name" in [c["name"] for c in rec["columns"]]


def test_ingest_json_single_object_becomes_one_row(store, tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"a": 1, "b": {"c": 2}}))
    rec = store.ingest_json(str(path), flatten_mode="full", build_fts=False)
    assert rec["row_count"] == 1
    assert [c["name"] for c in rec["columns"]] == ["a", "b.c"]


def test_ingest_json_empty_array_raises(store, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="no records"):
        store.ingest_json(str(path))


def test_preview_json_file_matches_ingest_columns_and_reports_record_count(store, json_array_file):
    preview = store.preview_json_file(json_array_file, flatten_mode="full")
    assert preview["record_count"] == 3
    assert preview["columns"] == ["id", "user.name", "user.address.city", "user.address.zip", "tags", "note"]
    assert len(preview["sample_rows"]) == 3

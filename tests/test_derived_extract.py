"""Extracted JSON/XML columns at the Store level.

The path syntax and the parsers themselves are covered as pure functions in
test_structparse.py; this file is about the integration choices — that an
extracted column is an ordinary derived column everywhere (sorts, filters,
survives, gets removed), that discovery describes a real column, and that a
flatten builds N columns in one pass rather than N passes.
"""

from __future__ import annotations

import json

import pytest

EVTX = (
    '<Event><System><Provider Name="Sec-Auditing"/><EventID>{eid}</EventID></System>'
    '<EventData><Data Name="TargetUserName">{user}</Data>'
    '<Data Name="LogonType">{lt}</Data></EventData></Event>'
)

JSON_ROWS = [
    ["Time", "Payload"],
    ["2024-01-05 10:00:00", json.dumps({"user": {"name": "jacson"}, "src": {"ip": "10.0.0.5"}})],
    ["2024-01-05 10:01:00", json.dumps({"user": {"name": "admin"}, "src": {"ip": "10.0.0.9"}})],
    ["2024-01-05 10:02:00", json.dumps({"user": {"name": "bob"}})],   # no src
    ["2024-01-05 10:03:00", "not json at all"],                        # not a document
]

XML_ROWS = [
    ["Time", "Payload"],
    ["2024-01-05 10:00:00", EVTX.format(eid=4624, user="jacson", lt=3)],
    ["2024-01-05 10:01:00", EVTX.format(eid=4625, user="admin", lt=10)],
]


def _ingest(store, write_csv, rows, name="t.csv"):
    path = write_csv(rows, name=name)
    return store.ingest_csv(path, build_fts=False)["id"]


def _add(store, sid, name, column, op_id, path):
    res = store.add_derived_column(sid, name, column, op_id, {"path": path})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res["definition"]["id"]


def _values(store, sid, column):
    src = store.get_source(sid)
    cols = [c["name"] for c in src["columns"]]
    view = store.build_view(sid, {})
    rows = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    return [dict(zip(cols, r["cells"]))[column] for r in rows]


# --------------------------------------------------------------- discovery

def test_detect_struct_paths_finds_json_fields_with_coverage(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    found = store.detect_struct_paths(sid, "Payload")
    assert found["kind"] == "json"
    by_path = {p["path"]: p for p in found["paths"]}
    assert by_path["$.user.name"]["count"] == 3
    assert by_path["$.src.ip"]["count"] == 2       # one row has no src
    assert by_path["$.user.name"]["suggested_name"] == "name"


def test_detect_struct_paths_on_xml(store, write_csv):
    sid = _ingest(store, write_csv, XML_ROWS)
    found = store.detect_struct_paths(sid, "Payload")
    assert found["kind"] == "xml"
    names = {p["path"]: p["suggested_name"] for p in found["paths"]}
    assert names["Event/System/EventID"] == "EventID"
    assert names["Event/EventData/Data[@Name='TargetUserName']"] == "TargetUserName"


def test_detect_struct_paths_on_a_plain_column(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    found = store.detect_struct_paths(sid, "Time")
    assert found["kind"] is None and found["paths"] == []


def test_detect_struct_paths_on_a_missing_column(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    with pytest.raises(KeyError):
        store.detect_struct_paths(sid, "Nope")


# --------------------------------------------------------------- extraction

def test_extracted_json_column_holds_the_field(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    _add(store, sid, "User", "Payload", "json_field", "$.user.name")
    assert _values(store, sid, "User") == ["jacson", "admin", "bob", None]


def test_rows_without_the_field_are_counted_not_guessed(store, write_csv):
    """Two rows can't produce a value here — one has no `src` object, one
    isn't a document at all. Both are NULL and both are counted, which is
    what makes "show me the rows this didn't work on" possible."""
    sid = _ingest(store, write_csv, JSON_ROWS)
    def_id = _add(store, sid, "SrcIP", "Payload", "json_field", "$.src.ip")
    assert _values(store, sid, "SrcIP") == ["10.0.0.5", "10.0.0.9", None, None]
    assert store.get_derived_column(def_id)["parse_failures"] == 2


def test_extracted_xml_column_by_predicate(store, write_csv):
    sid = _ingest(store, write_csv, XML_ROWS)
    _add(store, sid, "TargetUser", "Payload", "xml_field",
         "Event/EventData/Data[@Name='TargetUserName']")
    assert _values(store, sid, "TargetUser") == ["jacson", "admin"]


def test_extracted_column_filters_and_sorts_like_any_other(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    _add(store, sid, "User", "Payload", "json_field", "$.user.name")
    view = store.build_view(sid, {
        "source_id": sid,
        "filters": [{"column": "User", "op": "contains", "value": "adm"}],
        "sort": [],
    })
    assert view["row_count"] == 1
    view = store.build_view(sid, {
        "source_id": sid, "filters": [],
        "sort": [{"column": "User", "dir": "asc"}],
    })
    assert store.fetch_rows(view["view_id"], 0, 10)["rows"]  # sorts without error


def test_source_table_is_not_touched(store, write_csv):
    """Invariant #1 — the extracted values live in the drv_ sidecar."""
    sid = _ingest(store, write_csv, JSON_ROWS)
    before = {r[1] for r in store.db.execute(f"PRAGMA table_info(src_{sid})")}
    _add(store, sid, "User", "Payload", "json_field", "$.user.name")
    after = {r[1] for r in store.db.execute(f"PRAGMA table_info(src_{sid})")}
    assert before == after


def test_a_bad_path_is_refused_at_creation(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    with pytest.raises(ValueError):
        store.add_derived_column(sid, "Broken", "Payload", "json_field", {"path": "a[["})


# ---------------------------------------------------------------- flatten

def test_add_derived_columns_builds_them_all_in_one_pass(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    res = store.add_derived_columns(sid, [
        {"name": "User", "input_column": "Payload", "op_id": "json_field",
         "params": {"path": "$.user.name"}},
        {"name": "SrcIP", "input_column": "Payload", "op_id": "json_field",
         "params": {"path": "$.src.ip"}},
    ])
    assert len(res["definitions"]) == 2
    store.wait_for_ingest_job(res["job_id"], timeout=30)

    assert _values(store, sid, "User") == ["jacson", "admin", "bob", None]
    assert _values(store, sid, "SrcIP") == ["10.0.0.5", "10.0.0.9", None, None]
    # One job, not one per column — that's the whole point of the batch path.
    assert len({d["id"] for d in res["definitions"]}) == 2


def test_batch_is_all_or_nothing(store, write_csv):
    """A collision in the second spec must not leave the first column
    behind for the analyst to find and clean up."""
    sid = _ingest(store, write_csv, JSON_ROWS)
    with pytest.raises(ValueError):
        store.add_derived_columns(sid, [
            {"name": "User", "input_column": "Payload", "op_id": "json_field",
             "params": {"path": "$.user.name"}},
            {"name": "Time", "input_column": "Payload", "op_id": "json_field",
             "params": {"path": "$.src.ip"}},   # collides with a base column
        ])
    assert store.list_derived_columns(sid) == []


def test_batch_rejects_two_specs_wanting_the_same_name(store, write_csv):
    """Two paths whose last component is the same is the realistic mistake
    when ticking boxes in the flatten picker — and neither column exists
    yet, so _find_column can't catch it."""
    sid = _ingest(store, write_csv, JSON_ROWS)
    with pytest.raises(ValueError):
        store.add_derived_columns(sid, [
            {"name": "Same", "input_column": "Payload", "op_id": "json_field",
             "params": {"path": "$.user.name"}},
            {"name": "Same", "input_column": "Payload", "op_id": "json_field",
             "params": {"path": "$.src.ip"}},
        ])
    assert store.list_derived_columns(sid) == []


def test_flatten_end_to_end_from_discovery(store, write_csv):
    """What the UI does: discover the fields, then build a column per
    ticked path using the names discovery suggested."""
    sid = _ingest(store, write_csv, XML_ROWS)
    found = store.detect_struct_paths(sid, "Payload")
    specs = [
        {"name": p["suggested_name"], "input_column": "Payload",
         "op_id": "xml_field", "params": {"path": p["path"]}}
        for p in found["paths"] if p["coverage"] == 1.0
    ]
    res = store.add_derived_columns(sid, specs)
    store.wait_for_ingest_job(res["job_id"], timeout=30)

    assert _values(store, sid, "EventID") == ["4624", "4625"]
    assert _values(store, sid, "TargetUserName") == ["jacson", "admin"]
    assert all(d["status"] == "ready" for d in store.list_derived_columns(sid))


def test_removing_one_extracted_column_leaves_the_others(store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    res = store.add_derived_columns(sid, [
        {"name": "User", "input_column": "Payload", "op_id": "json_field",
         "params": {"path": "$.user.name"}},
        {"name": "SrcIP", "input_column": "Payload", "op_id": "json_field",
         "params": {"path": "$.src.ip"}},
    ])
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    store.remove_derived_column(res["definitions"][0]["id"])

    names = {d["name"] for d in store.list_derived_columns(sid)}
    assert names == {"SrcIP"}
    assert _values(store, sid, "SrcIP") == ["10.0.0.5", "10.0.0.9", None, None]


def test_batch_routes(client, store, write_csv):
    sid = _ingest(store, write_csv, JSON_ROWS)
    r = client.post("/api/derived/paths", json={"source_id": sid, "column": "Payload"})
    assert r.status_code == 200 and r.json()["kind"] == "json"

    r = client.post("/api/derived/batch", json={
        "source_id": sid,
        "columns": [{"name": "User", "input_column": "Payload",
                     "op_id": "json_field", "params": {"path": "$.user.name"}}],
    })
    assert r.status_code == 200
    store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert _values(store, sid, "User") == ["jacson", "admin", "bob", None]

    bad = client.post("/api/derived/batch", json={
        "source_id": sid,
        "columns": [{"name": "X", "input_column": "Payload",
                     "op_id": "json_field", "params": {"path": "a[["}}],
    })
    assert bad.status_code == 400

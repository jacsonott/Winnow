"""Derived columns from derived columns: the JSON-holds-XML two-step, the
re-derive cascade, and the session round-trip of a chain."""

from __future__ import annotations

import json
import time

import pytest

XML = '<Cfg><Server addr="{addr}"/><Mode>{mode}</Mode></Cfg>'
ROWS = [["Payload"]] + [
    [json.dumps({"user": u, "config": XML.format(addr=a, mode=m)})]
    for u, a, m in [("alice", "10.0.0.5", "beacon"), ("bob", "10.0.0.9", "idle")]
]


def _add(store, sid, name, column, op_id, params):
    res = store.add_derived_column(sid, name, column, op_id, params)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res["definition"]["id"]


def _values(store, sid, column):
    src = store.get_source(sid)
    idx = [c["name"] for c in src["columns"]].index(column)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    return [r["cells"][idx] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


def test_json_then_xml_chain(store, write_csv):
    """The reported case verbatim: a JSON field holds XML; take the XML
    apart in a second step."""
    sid = store.ingest_csv(write_csv(ROWS, "c.csv"), name="c", build_fts=False)["id"]
    _add(store, sid, "Config", "Payload", "json_field", {"path": "$.config"})
    _add(store, sid, "Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})
    assert _values(store, sid, "Addr") == ["10.0.0.5", "10.0.0.9"]
    # ...and the grandchild filters/sorts like any column
    v = store.build_view(sid, {"source_id": sid, "sort": [],
                               "filters": [{"column": "Addr", "op": "equals", "value": "10.0.0.9"}]})
    assert v["row_count"] == 1


def test_regex_on_extracted_chain_and_three_deep(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "d.csv"), name="d", build_fts=False)["id"]
    _add(store, sid, "Config", "Payload", "json_field", {"path": "$.config"})
    _add(store, sid, "Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})
    _add(store, sid, "Net", "Addr", "regex_extract", {"pattern": r"^(\d+\.\d+\.\d+)\."})
    assert _values(store, sid, "Net") == ["10.0.0", "10.0.0"]


def test_a_building_parent_is_refused(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    res = store.add_derived_column(sid, "Config", "Payload", "json_field", {"path": "$.config"})
    # Don't wait: the parent is (very likely) still building. If the tiny
    # backfill already finished, creation succeeds — either outcome is a
    # valid state; what may never happen is a child built from a partial
    # parent, which the guard plus this race-free re-check protect.
    try:
        store.add_derived_column(sid, "Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})
    except ValueError as e:
        assert "still building" in str(e)
    store.wait_for_ingest_job(res["job_id"], timeout=30)


def test_parent_cannot_be_deleted_under_its_child(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "f.csv"), name="f", build_fts=False)["id"]
    pid = _add(store, sid, "Config", "Payload", "json_field", {"path": "$.config"})
    _add(store, sid, "Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})
    with pytest.raises(ValueError, match="computed from this column"):
        store.remove_derived_column(pid)


def test_rederiving_a_parent_cascades_to_the_chain(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "g.csv"), name="g", build_fts=False)["id"]
    pid = _add(store, sid, "Field", "Payload", "json_field", {"path": "$.config"})
    _add(store, sid, "Mode", "Field", "xml_field", {"path": "Cfg/Mode"})
    assert _values(store, sid, "Mode") == ["beacon", "idle"]

    # repoint the PARENT at a different JSON field — the child must follow
    res = store.rederive_column(pid, {"path": "$.user"})
    assert res["cascades_to"] == ["Mode"]
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    deadline = time.time() + 30
    while time.time() < deadline:
        defs = {d["name"]: d for d in store.list_derived_columns(sid)}
        if defs["Mode"]["status"] == "ready" and _values(store, sid, "Mode") == [None, None]:
            break
        time.sleep(0.1)
    # $.user is 'alice'/'bob' — not XML, so the chained extraction now
    # correctly yields nothing, proving the child was recomputed.
    assert _values(store, sid, "Mode") == [None, None]


def test_session_round_trip_restores_the_chain(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "h.csv"), name="h", build_fts=False)["id"]
    _add(store, sid, "Config", "Payload", "json_field", {"path": "$.config"})
    _add(store, sid, "Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})
    session = store.export_session(sid)

    fresh = store.ingest_csv(write_csv(ROWS, "h2.csv"), name="h2", build_fts=False)["id"]
    out = store.import_session(fresh, session)
    assert out["derived_columns_added"] == 2, out
    # The last column's backfill is still running when import_session
    # returns — it only waits for links a later column depends on. Reading
    # values without waiting is a race that usually resolves in the test's
    # favour on two rows, and intermittently does not.
    for jid in out["derived_job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    assert _values(store, fresh, "Addr") == ["10.0.0.5", "10.0.0.9"]


def test_chain_on_a_merge(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS, "i.csv"), name="i", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS, "j.csv"), name="j", build_fts=False)["id"]
    mid = store.create_merge("cm", [a, b])["id"]
    for name, col, op, params in [("Config", "Payload", "json_field", {"path": "$.config"}),
                                  ("Addr", "Config", "xml_field", {"path": "Cfg/Server@addr"})]:
        res = store.add_derived_column(mid, name, col, op, params)
        for j in res["job_ids"]:
            store.wait_for_ingest_job(j, timeout=30)
    assert sorted(x for x in _values(store, mid, "Addr")) == ["10.0.0.5", "10.0.0.5", "10.0.0.9", "10.0.0.9"]


def test_flatten_batch_accepts_a_derived_input(store, write_csv):
    """The flatten-all path is a chain move too: json_field pulled a JSON
    blob into a column — flattening THAT column's fields must work like
    any other derivation from it."""
    rows = [["Payload"]] + [
        [json.dumps({"inner": json.dumps({"a": str(i), "b": f"x{i}"})})] for i in range(3)
    ]
    sid = store.ingest_csv(write_csv(rows, "fb.csv"), name="fb", build_fts=False)["id"]
    _add(store, sid, "Inner", "Payload", "json_field", {"path": "$.inner"})
    res = store.add_derived_columns(sid, [
        {"name": "A", "input_column": "Inner", "op_id": "json_field", "params": {"path": "$.a"}},
        {"name": "B", "input_column": "Inner", "op_id": "json_field", "params": {"path": "$.b"}},
    ])
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "A") == ["0", "1", "2"]
    assert _values(store, sid, "B") == ["x0", "x1", "x2"]


def test_flatten_batch_still_refuses_a_building_input(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "fb2.csv"), name="fb2", build_fts=False)["id"]
    res = store.add_derived_column(sid, "Config", "Payload", "json_field", {"path": "$.config"})
    try:
        store.add_derived_columns(sid, [
            {"name": "X", "input_column": "Config", "op_id": "json_field", "params": {"path": "$.x"}}])
    except ValueError as e:
        assert "still building" in str(e)
    store.wait_for_ingest_job(res["job_id"], timeout=30)

"""Row as JSON — a combine-family derived op: the row's columns as one
compact JSON object, keys from the op's inputs (state["inputs"], set by
the store), plain numbers unquoted, skip_empty as a bool param."""

from __future__ import annotations

import csv
import json

import pytest

from winnow import timeparse
from winnow.combine import _row_json
from winnow.store import Store


def _write(path, rows, header=("A", "B", "C")):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "case.db-winnow"))
    yield s
    s.close()


def _values(store, source_id, name):
    src = store._source_lite(source_id)
    col = store._col_ref(src, name)
    return [r[0] for r in store.db.execute(f"SELECT {col} FROM {store._from_clause(src)} ORDER BY rid")]


# ------------------------------------------------------------------- the op

def test_object_keys_order_and_number_rules():
    out = _row_json(["H1", "4624", "0.5", "007", "1e5", "18446744073709551615", None, ""],
                    {}, {"inputs": ["Host", "EventId", "Ratio", "Padded", "Sci", "Big", "Gone", "Blank"]})
    assert out == '{"Host":"H1","EventId":4624,"Ratio":0.5,"Padded":"007","Sci":"1e5","Big":"18446744073709551615","Gone":null,"Blank":""}'
    assert json.loads(out)["EventId"] == 4624
    # compact: no spaces, one line, non-ASCII kept as-is
    assert " " not in _row_json(["ü", "1"], {}, {"inputs": ["A", "B"]})
    assert _row_json(["ü"], {}, {"inputs": ["A"]}) == '{"A":"ü"}'
    assert _row_json(["-12", "-0.25", "-", "12."], {}, {"inputs": ["a", "b", "c", "d"]}) == '{"a":-12,"b":-0.25,"c":"-","d":"12."}'


def test_skip_empty_leaves_out_null_and_blank_cells():
    st = {"inputs": ["A", "B", "C", "D"]}
    assert _row_json(["x", None, "", "  "], {"skip_empty": True}, st) == '{"A":"x"}'
    assert _row_json([None, "", "  ", ""], {"skip_empty": True}, st) == "{}"
    assert _row_json(["x", None, "", "  "], {"skip_empty": False}, st) == '{"A":"x","B":null,"C":"","D":"  "}'


def test_bool_param_coercion_and_columns_optional():
    assert timeparse.validate_params("row_json", {}) == {"skip_empty": False}
    assert timeparse.validate_params("row_json", {"skip_empty": "yes", "extra_columns": ["B"]}) == {"extra_columns": ["B"], "skip_empty": True}
    assert timeparse.validate_params("row_json", {"skip_empty": True})["skip_empty"] is True
    assert timeparse.validate_params("row_json", {"skip_empty": "0"})["skip_empty"] is False
    with pytest.raises(ValueError, match="yes or no"):
        timeparse.validate_params("row_json", {"skip_empty": "maybe"})
    assert timeparse.op_inputs("row_json", "A", {"extra_columns": ["C", "B"]}) == ["A", "C", "B"]


def test_registry_shape():
    op = timeparse.OPERATIONS["row_json"]
    assert op["multi_input"] and op["hidden_from_detect"] and op["family"] == "combine"
    assert op["value_type"] == "text" and op["derived_kind"] == "combine"
    cols = next(p for p in op["params"] if p["name"] == "extra_columns")
    assert cols["any_type"] is True and cols["prefill"] == "all" and not cols.get("required")


# ---------------------------------------------------------------- the store

def test_backfill_names_keys_after_the_inputs(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["alice", "4624", ""], ["", "", ""]]))["id"]
    res = store.add_derived_column(sid, "Row", "A", "row_json", {"extra_columns": ["B", "C"]})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Row") == ['{"A":"alice","B":4624,"C":""}', '{"A":"","B":"","C":""}']
    d = next(d for d in store.list_derived_columns(sid) if d["name"] == "Row")
    assert d["status"] == "ready" and d["parse_failures"] == 0
    # skip_empty through the store, and the input column alone
    res = store.add_derived_column(sid, "Slim", "B", "row_json", {"extra_columns": ["A", "C"], "skip_empty": True})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Slim") == ['{"B":4624,"A":"alice"}', "{}"]
    res = store.add_derived_column(sid, "Just", "A", "row_json", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Just") == ['{"A":"alice"}', '{"A":""}']


def test_preview_uses_the_same_keys(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["alice", "1", "x"]]))["id"]
    p = store.preview_derived(sid, "C", "row_json", {"extra_columns": ["A"]}, limit=5)
    assert p["preview"][0]["output"] == '{"C":"x","A":"alice"}'


def test_merge_parity(store, tmp_path):
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["bob", "1", ""]]))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["", "2", "carol"]]))["id"]
    merge = store.create_merge("m", [a, b])["id"]
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["B", "C"], "skip_empty": True})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    assert _values(store, a, "Row") == ['{"A":"bob","B":1}']
    assert _values(store, b, "Row") == ['{"B":2,"C":"carol"}']
    assert any(c["name"] == "Row" for c in store._source_lite(merge)["columns"])


def test_session_round_trip_keeps_the_bool(store, tmp_path):
    path = _write(tmp_path / "u.csv", [["bob", "1", ""]])
    sid = store.ingest_csv(path)["id"]
    res = store.add_derived_column(sid, "Row", "A", "row_json", {"extra_columns": ["B"], "skip_empty": True})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    session = store.export_case_session()
    assert session["sources"][0]["derived_columns"][0]["params"] == {"extra_columns": ["B"], "skip_empty": True}
    other = Store(str(tmp_path / "other.db-winnow"))
    try:
        other.import_case_session(session)
        sid2 = other.list_sources()[0]["id"]
        other.wait_for_ingest_job(timeout=30)
        assert _values(other, sid2, "Row") == ['{"A":"bob","B":1}']
    finally:
        other.close()

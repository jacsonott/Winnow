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


def test_a_number_is_unquoted_only_when_it_round_trips():
    """The cell text went through int()/float() and back out through
    json.dumps, so the JSON said something the evidence didn't: "1.10"
    became 1.1, "9999999999999999.99" became 1e+16, "-0" became 0 — and
    "42\\n" became 42, because `$` also matches before a trailing newline.
    A cell is written unquoted only if the number json writes back is the
    cell's own text; anything else stays the string it was."""
    names = ["a", "b", "c", "d", "e", "f", "g", "h"]
    cells = ["1.10", "9999999999999999.99", "42\n", "-0", "100.0", "0.1", "-0.0", "-7"]
    out = _row_json(cells, {}, {"inputs": names})
    assert out == ('{"a":"1.10","b":"9999999999999999.99","c":"42\\n","d":"-0",'
                   '"e":100.0,"f":0.1,"g":-0.0,"h":-7}')
    # Whatever is unquoted reads back as exactly the cell's text.
    for name, cell in zip(names, cells):
        v = json.loads(out)[name]
        assert (v if isinstance(v, str) else json.dumps(v)) == cell


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


# ------------------------------------------------- a merge's From table column

def _merge_with_label(store, tmp_path, nickname=None):
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["bob", "1", ""]]))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["carol", "2", "x"]]))["id"]
    if nickname:
        store.set_source_nickname(b, nickname)
    merge = store.create_merge("m", [a, b], source_column=True)["id"]
    return a, b, merge


def _wait(store, res):
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)


def test_merge_from_table_is_one_of_the_columns(store, tmp_path):
    """The dialog lists a merge's From table column among Row as JSON's
    columns (every column starts listed), and Add column used to refuse
    it: "'From table' names the table each row came from… can't be
    derived from". Each table writes its own name there, as the merge
    shows it — the nickname where it has one."""
    a, b, merge = _merge_with_label(store, tmp_path, nickname="WS02 Security")
    assert [c["name"] for c in store._source_lite(merge)["columns"]] == ["From table", "A", "B", "C"]
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["From table", "B", "C"]})
    _wait(store, res)
    assert _values(store, a, "Row") == ['{"A":"bob","From table":"a.csv","B":1,"C":""}']
    assert _values(store, b, "Row") == ['{"A":"carol","From table":"WS02 Security","B":2,"C":"x"}']
    # Each member's definition says which input is its own name.
    for d in (store.get_derived_column(x["id"]) for x in res["member_definitions"]):
        assert d["params"]["table_column"] == "From table"
        assert d["status"] == "ready" and d["parse_failures"] == 0
    # The preview reads it the same way, from every member.
    p = store.preview_derived(merge, "A", "row_json", {"extra_columns": ["From table"]}, limit=4)
    assert [r["output"] for r in p["preview"]] == ['{"A":"bob","From table":"a.csv"}',
                                                    '{"A":"carol","From table":"WS02 Security"}']


def test_merge_from_table_as_the_parse_column_and_with_skip_empty(store, tmp_path):
    a, b, merge = _merge_with_label(store, tmp_path)
    res = store.add_derived_column(merge, "Row", "From table", "row_json",
                                   {"extra_columns": ["C"], "skip_empty": True})
    _wait(store, res)
    assert _values(store, a, "Row") == ['{"From table":"a.csv"}']
    assert _values(store, b, "Row") == ['{"From table":"b.csv","C":"x"}']


def test_merge_without_from_table_keeps_no_label_and_caller_cannot_set_one(store, tmp_path):
    """The merge says which input is the table's name, not the caller:
    a table_column naming a real column is dropped, and a merge whose
    From table column is not listed records none."""
    a, b, merge = _merge_with_label(store, tmp_path)
    res = store.add_derived_column(merge, "Row", "A", "row_json",
                                   {"extra_columns": ["B"], "table_column": "B"})
    _wait(store, res)
    assert _values(store, a, "Row") == ['{"A":"bob","B":1}']
    assert all("table_column" not in d["params"] for d in res["member_definitions"])


def test_other_operations_still_refuse_from_table(store, tmp_path):
    """Only an op that can read it as the table's name takes it: First
    non-empty value has no such reading, and would make an empty column."""
    a, b, merge = _merge_with_label(store, tmp_path)
    with pytest.raises(ValueError, match="only included in Row as JSON"):
        store.add_derived_column(merge, "N", "A", "coalesce", {"extra_columns": ["From table"]})
    with pytest.raises(ValueError, match="only included in Row as JSON"):
        store.preview_derived(merge, "A", "coalesce", {"extra_columns": ["From table"]})
    assert not store.list_derived_columns(a) and not store.list_derived_columns(b)


def test_merge_rederive_adds_and_drops_from_table(store, tmp_path):
    a, b, merge = _merge_with_label(store, tmp_path)
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["B"]})
    _wait(store, res)
    def_id = res["definition"]["id"]
    res = store.rederive_column(def_id, {"extra_columns": ["From table", "B"]}, source_id=merge)
    _wait(store, res)
    assert _values(store, b, "Row") == ['{"A":"carol","From table":"b.csv","B":2}']
    assert all(d["params"]["table_column"] == "From table" for d in res["member_definitions"])
    # Taken back out of the list, nothing reads as the table's name.
    res = store.rederive_column(def_id, {"extra_columns": ["B"], "table_column": "From table"},
                                source_id=merge)
    _wait(store, res)
    assert _values(store, b, "Row") == ['{"A":"carol","B":2}']
    assert all("table_column" not in d["params"] for d in res["member_definitions"])


def test_member_rederive_and_preview_on_its_own_table(store, tmp_path):
    """Re-derive… on a member opened on its own sends the definition's
    params back as loaded, table_column included; the member has no From
    table column, and still writes its own name there."""
    a, b, merge = _merge_with_label(store, tmp_path)
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["From table", "B"]})
    _wait(store, res)
    d = next(x for x in res["member_definitions"] if x["source_id"] == b)
    p = store.preview_derived(b, "A", "row_json", d["params"], limit=2)
    assert p["preview"][0]["output"] == '{"A":"carol","From table":"b.csv","B":2}'
    res = store.rederive_column(d["id"], dict(d["params"], skip_empty=True))
    _wait(store, res)
    assert _values(store, b, "Row") == ['{"A":"carol","From table":"b.csv","B":2}']
    assert res["definition"]["params"]["table_column"] == "From table"


def test_member_with_its_own_from_table_column(store, tmp_path):
    """A member with a file column called From table: the merge's label is
    then "From table 2", and the JSON carries both — the file's column
    and each table's name."""
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["evtx", "1"]], header=("From table", "B")))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["", "2"]], header=("From table", "B")))["id"]
    merge = store.create_merge("m", [a, b], source_column=True)["id"]
    assert [c["name"] for c in store._source_lite(merge)["columns"]][:2] == ["From table 2", "From table"]
    res = store.add_derived_column(merge, "Row", "B", "row_json", {"extra_columns": ["From table 2", "From table"]})
    _wait(store, res)
    assert _values(store, a, "Row") == ['{"B":1,"From table 2":"a.csv","From table":"evtx"}']
    assert _values(store, b, "Row") == ['{"B":2,"From table 2":"b.csv","From table":""}']


def test_label_wins_over_a_member_column_the_shared_merge_hides(store, tmp_path):
    """Only shared columns: a member's own file column called From table
    isn't the merge's, whose From table column is still the label — and
    the JSON says what the merge shows, the table's name."""
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["evtx", "1"]], header=("From table", "B")))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["2", "z"]], header=("B", "C")))["id"]
    merge = store.create_merge("m", [a, b], col_mode="shared", source_column=True)["id"]
    assert [c["name"] for c in store._source_lite(merge)["columns"]] == ["From table", "B"]
    res = store.add_derived_column(merge, "Row", "B", "row_json", {"extra_columns": ["From table"]})
    _wait(store, res)
    assert _values(store, a, "Row") == ['{"B":1,"From table":"a.csv"}']
    assert _values(store, b, "Row") == ['{"B":2,"From table":"b.csv"}']


def test_renaming_a_member_recomputes_its_name_in_the_json(store, tmp_path):
    """The merge's From table column follows a rename at once (it is
    computed per query); the JSON is stored, so it is recomputed, or a
    row's JSON would name the table differently from its own From table
    cell."""
    a, b, merge = _merge_with_label(store, tmp_path)
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["From table"]})
    _wait(store, res)
    plain = store.add_derived_column(b, "Plain", "A", "row_json", {"extra_columns": ["B"]})
    _wait(store, plain)
    store.set_source_nickname(b, "DC01 Security")
    store.wait_for_ingest_job(timeout=30)
    assert _values(store, b, "Row") == ['{"A":"carol","From table":"DC01 Security"}']
    assert _values(store, a, "Row") == ['{"A":"bob","From table":"a.csv"}']
    assert {d["name"]: d["status"] for d in store.list_derived_columns(b)} == {"Row": "ready", "Plain": "ready"}
    # Cleared, it is the file's name again.
    store.set_source_nickname(b, "")
    store.wait_for_ingest_job(timeout=30)
    assert _values(store, b, "Row") == ['{"A":"carol","From table":"b.csv"}']


def test_session_round_trip_keeps_from_table(store, tmp_path):
    a, b, merge = _merge_with_label(store, tmp_path, nickname="WS02 Security")
    res = store.add_derived_column(merge, "Row", "A", "row_json", {"extra_columns": ["From table", "B"]})
    _wait(store, res)
    session = store.export_case_session()
    by_name = {s["source"]["name"]: s for s in session["sources"]}
    d = by_name["b.csv"]["derived_columns"][0]
    assert d["params"]["table_column"] == "From table" and d["absent_inputs"] == ["From table"]
    other = Store(str(tmp_path / "other.db-winnow"))
    try:
        other.ingest_csv(str(tmp_path / "a.csv"))
        b2 = other.ingest_csv(str(tmp_path / "b.csv"))["id"]
        other.set_source_nickname(b2, "WS02 Security")
        other.import_case_session(session)
        other.wait_for_ingest_job(timeout=30)
        assert _values(other, b2, "Row") == ['{"A":"carol","From table":"WS02 Security","B":2}']
    finally:
        other.close()


def test_table_label_helpers():
    assert timeparse.table_label_param("row_json") == "table_column"
    assert timeparse.table_label_param("coalesce") is None
    assert timeparse.table_label_input("row_json", "A", {"extra_columns": ["from TABLE"], "table_column": "From table"}) == "from TABLE"
    assert timeparse.table_label_input("row_json", "A", {"extra_columns": ["B"], "table_column": "From table"}) is None
    assert timeparse.table_label_input("row_json", "A", {"extra_columns": ["B"]}) is None
    spec = next(p for p in timeparse.OPERATIONS["row_json"]["params"] if p["name"] == "table_column")
    assert spec["hidden"] is True and spec["type"] == "table_label"

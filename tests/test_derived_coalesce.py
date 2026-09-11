"""Coalesce — the first multi-column derived op — and the generic input
plumbing (`timeparse.op_inputs`, `parse_multi`) that replaced the
hard-wired two-column special case it would otherwise have copied."""

from __future__ import annotations

import csv

import pytest

from winnow import timeparse
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
    with store._reader() as ro:
        return [r[0] for r in ro.execute(
            f"SELECT {store._col_ref(src, name)} FROM {store._from_clause(src)} ORDER BY rid")]


# ------------------------------------------------------------ pure function

def test_coalesce_skips_null_empty_and_whitespace_in_order():
    op = timeparse.OPERATIONS["coalesce"]
    assert op["parse_multi"]([None, "", "   ", "x"], {}, {}) == "x"
    assert op["parse_multi"](["first", "second"], {}, {}) == "first"
    # Untrimmed: the derived column copies, it doesn't tidy.
    assert op["parse_multi"]([None, "  padded "], {}, {}) == "  padded "
    assert op["parse_multi"]([None, "", "\t"], {}, {}) is None
    assert op["parse_multi"]([0, "x"], {}, {}) == "0"


def test_columns_param_is_an_ordered_deduped_list():
    assert timeparse.validate_params("coalesce", {"extra_columns": ["B", " c ", "b", ""]}) == {"extra_columns": ["B", "c"]}
    assert timeparse.validate_params("coalesce", {"extra_columns": "B,C"}) == {"extra_columns": ["B", "C"]}
    with pytest.raises(ValueError, match="at least one column"):
        timeparse.validate_params("coalesce", {"extra_columns": []})
    with pytest.raises(ValueError, match="needs the 'Then try' parameter"):
        timeparse.validate_params("coalesce", {})
    with pytest.raises(ValueError, match="list of column names"):
        timeparse.validate_params("coalesce", {"extra_columns": 7})


def test_op_inputs_covers_both_shapes():
    assert timeparse.op_inputs("coalesce", "A", {"extra_columns": ["B", "C"]}) == ["A", "B", "C"]
    assert timeparse.op_inputs("duration_delta", "End", {"other_column": "Start"}) == ["End", "Start"]
    assert timeparse.op_inputs("iso8601", "T", {"utc_offset": "+01:00"}) == ["T"]
    assert timeparse.op_inputs("nope", "T", None) == ["T"]


def test_registry_advertises_the_op_as_multi_input_and_hidden_from_detect():
    listed = {o["id"]: o for o in timeparse.list_ops()}
    op = listed["coalesce"]
    assert op["multi_input"] is True and op["family"] == "combine"
    assert op["derived_kind"] == "combine" and op["value_type"] == "text"
    assert listed["duration_delta"]["multi_input"] is True
    assert listed["iso8601"]["multi_input"] is False
    assert all(r["op_id"] != "coalesce" for r in timeparse.detect(["a", "b"]))


# ---------------------------------------------------------------- the store

def test_backfill_writes_first_non_empty_per_row(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [
        ["alice", "", ""], ["", "bob", ""], ["", "  ", "carol"], ["", "", ""], ["  ", "dave", "erin"],
    ]))["id"]
    res = store.add_derived_column(sid, "User", "A", "coalesce", {"extra_columns": ["B", "C"]})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "User") == ["alice", "bob", "carol", None, "dave"]
    d = next(d for d in store.list_derived_columns(sid) if d["name"] == "User")
    # parse_failures counts NON-EMPTY inputs that produced NULL. A row whose
    # every listed column is blank had no input, so it isn't one.
    assert d["status"] == "ready" and d["parse_failures"] == 0
    # A text column like any other to the rest of the app.
    col = next(c for c in store._source_lite(sid)["columns"] if c["name"] == "User")
    assert col["type"] == "text" and col["derived"]


def test_every_named_column_must_exist(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["a", "b", "c"]]))["id"]
    with pytest.raises(ValueError, match="No column called 'Nope'"):
        store.add_derived_column(sid, "User", "A", "coalesce", {"extra_columns": ["B", "Nope"]})


def test_preview_reads_all_inputs(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["", "bob", ""], ["", "", "carol"]]))["id"]
    res = store.preview_derived(sid, "A", "coalesce", {"extra_columns": ["B", "C"]})
    assert [p["output"] for p in res["preview"]] == ["bob", "carol"]
    assert res["failures"] == 0
    with pytest.raises(KeyError):
        store.preview_derived(sid, "A", "coalesce", {"extra_columns": ["Nope"]})


def test_batch_add_refuses_a_multi_column_op(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["a", "b", "c"]]))["id"]
    with pytest.raises(ValueError, match="several columns"):
        store.add_derived_columns(sid, [{"name": "U", "input_column": "A", "op_id": "coalesce",
                                        "params": {"extra_columns": ["B"]}}])


def test_an_extra_column_counts_as_a_dependency(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["{\"n\": \"x\"}", "", "c"]]))["id"]
    parent = store.add_derived_column(sid, "Name", "A", "json_field", {"path": "n"})
    store.wait_for_ingest_job(parent["job_id"], timeout=30)
    child = store.add_derived_column(sid, "User", "B", "coalesce", {"extra_columns": ["Name", "C"]})
    store.wait_for_ingest_job(child["job_id"], timeout=30)
    assert _values(store, sid, "User") == ["x"]
    # Name is only the SECOND input of User — it still has to be seen.
    assert [d["name"] for d in store.dependent_derived_columns(sid, ["Name"])] == ["User"]
    parent_def = next(d for d in store.list_derived_columns(sid) if d["name"] == "Name")
    with pytest.raises(ValueError, match="computed from this column"):
        store.remove_derived_column(parent_def["id"])


def test_duration_delta_still_backfills_through_the_generic_path(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "t.csv", [
        ["2024-01-01 00:00:10", "2024-01-01 00:00:00", ""],
    ]))["id"]
    res = store.add_derived_column(sid, "Elapsed", "A", "duration_delta", {"other_column": "B"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Elapsed") == ["10.000000"]


def test_merge_parity(store, tmp_path):
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["", "bob", ""]]))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["", "", "carol"]]))["id"]
    merge = store.create_merge("m", [a, b])["id"]
    res = store.add_derived_column(merge, "User", "A", "coalesce", {"extra_columns": ["B", "C"]})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    assert _values(store, a, "User") == ["bob"]
    assert _values(store, b, "User") == ["carol"]
    assert any(c["name"] == "User" for c in store._source_lite(merge)["columns"])


def test_session_round_trip_recreates_the_extra_columns(store, tmp_path):
    path = _write(tmp_path / "u.csv", [["", "bob", ""], ["", "", "carol"]])
    sid = store.ingest_csv(path)["id"]
    res = store.add_derived_column(sid, "User", "A", "coalesce", {"extra_columns": ["B", "C"]})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    session = store.export_case_session()
    assert session["sources"][0]["derived_columns"][0]["params"] == {"extra_columns": ["B", "C"]}

    other = Store(str(tmp_path / "other.db-winnow"))
    try:
        other.import_case_session(session)
        sid2 = other.list_sources()[0]["id"]
        other.wait_for_ingest_job(timeout=30)   # every job — the import started the backfill
        assert _values(other, sid2, "User") == ["bob", "carol"]
    finally:
        other.close()

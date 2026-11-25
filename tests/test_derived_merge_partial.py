"""Deriving on a merge from columns only some of its tables have.

The report: two tables with different columns, merged, and a coalesce of
EvtxECmd's TimeCreated with MFTECmd's Created0x10 — each timestamp column
on one table only, which is the whole reason to coalesce them — refused
with "Member table 'evtx' has no column 'Created0x10'". It was never about
timestamps: any column some members lacked was refused, by every derive
op, the batch included.

A member that lacks an input now reads it as empty, as the merge shows that
member's rows (Store._input_ref), and a column whose values are all
timestamps in the datetime ops' own shape is a datetime column
(_infer_derived_type), so the coalesced When sorts, filters and charts by
time on the merge."""

from __future__ import annotations

import pytest

from winnow.store import Store

EVTX = [["TimeCreated", "Computer", "EventId"],
        ["2024-01-01 10:00:00.1234567", "SRV1", "4624"],
        ["2024-01-03 08:00:00.0000000", "SRV1", "4625"]]
MFT = [["Created0x10", "FileName"],
       ["2024-01-02 09:00:00.0000000", "a.exe"],
       ["2024-01-04 12:30:00.5000000", "b.dll"]]
CO = {"extra_columns": ["Created0x10"]}


@pytest.fixture
def merged(store, write_csv):
    e = store.ingest_csv(write_csv(EVTX, "evtx.csv"), name="evtx", build_fts=False)["id"]
    m = store.ingest_csv(write_csv(MFT, "mft.csv"), name="mft", build_fts=False)["id"]
    return e, m, store.create_merge("both", [e, m])["id"]


def _wait(store, res):
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)


def _col(store, sid, name):
    return next(c for c in store.get_source(sid)["columns"] if c["name"] == name)


def _cells(store, sid, column, **spec):
    spec.setdefault("filters", [])
    spec.setdefault("sort", [])
    v = store.build_view(sid, {"source_id": sid, **spec})
    names = [c["name"] for c in store.get_source(sid)["columns"]]
    return [r["cells"][names.index(column)] for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]


def _values(store, sid, name):
    src = store._source_lite(sid)
    with store._reader() as ro:
        return [r[0] for r in ro.execute(
            f"SELECT {store._col_ref(src, name)} FROM {store._from_clause(src)} ORDER BY rid")]


# ------------------------------------------------------------- the report

def test_coalesce_of_timestamp_columns_each_on_one_table(store, merged):
    e, m, mid = merged
    # The preview reads both tables, so the analyst sees the second
    # column answer too — the first table alone shows only TimeCreated.
    pre = store.preview_derived(mid, "TimeCreated", "coalesce", CO)
    assert [p["output"] for p in pre["preview"]] == [
        "2024-01-01 10:00:00.1234567", "2024-01-03 08:00:00.0000000",
        "2024-01-02 09:00:00.0000000", "2024-01-04 12:30:00.5000000"]
    assert (pre["failures"], pre["scope"]) == (0, "table")

    res = store.add_derived_column(mid, "When", "TimeCreated", "coalesce", CO)
    _wait(store, res)
    assert [d["source_id"] for d in res["member_definitions"]] == [e, m]
    # Each table's copy holds its own column's values, untouched.
    assert _values(store, e, "When") == ["2024-01-01 10:00:00.1234567", "2024-01-03 08:00:00.0000000"]
    assert _values(store, m, "When") == ["2024-01-02 09:00:00.0000000", "2024-01-04 12:30:00.5000000"]

    when = _col(store, mid, "When")
    assert (when["type"], when["derived_status"], when["parse_failures"]) == ("datetime", "ready", 0)
    assert _col(store, e, "When")["type"] == _col(store, m, "When")["type"] == "datetime"

    # It behaves as a timestamp on the merge: sorted by time across both
    # tables, a timeframe on it, and the histogram reads it.
    assert _cells(store, mid, "When", sort=[{"column": "When", "dir": "asc"}]) == [
        "2024-01-01 10:00:00.1234567", "2024-01-02 09:00:00.0000000",
        "2024-01-03 08:00:00.0000000", "2024-01-04 12:30:00.5000000"]
    tr = {"enabled": True, "column": "When", "start": "2024-01-02 00:00:00", "end": "2024-01-03 23:59:59"}
    assert sorted(_cells(store, mid, "When", time_range=tr)) == [
        "2024-01-02 09:00:00.0000000", "2024-01-03 08:00:00.0000000"]
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    hist = store.time_histogram(v["view_id"], "When")
    assert sum(n for _, n in hist["buckets"]) == 4


def test_any_column_only_some_tables_have_not_just_timestamps(store, merged):
    e, m, mid = merged
    res = store.add_derived_column(mid, "Thing", "Computer", "coalesce", {"extra_columns": ["FileName"]})
    _wait(store, res)
    assert sorted(_cells(store, mid, "Thing")) == ["SRV1", "SRV1", "a.exe", "b.dll"]
    assert _col(store, mid, "Thing")["type"] == "text"

    # A one-column op over a column one table has: the other's rows are
    # empty, and empty is not a failure.
    res = store.add_derived_column(mid, "Ext", "FileName", "regex_extract", {"pattern": r"\.(\w+)$"})
    _wait(store, res)
    assert _values(store, e, "Ext") == [None, None]
    assert _values(store, m, "Ext") == ["exe", "dll"]
    assert _col(store, mid, "Ext")["parse_failures"] == 0

    # The batch route (a column per named group, a flatten) too.
    res = store.add_derived_columns(mid, [
        {"name": "Stem", "input_column": "FileName", "op_id": "regex_extract", "params": {"pattern": r"^(\w+)"}},
        {"name": "Host", "input_column": "Computer", "op_id": "regex_extract", "params": {"pattern": r"^(\w+)"}},
    ])
    _wait(store, res)
    assert (_values(store, e, "Stem"), _values(store, m, "Stem")) == ([None, None], ["a", "b"])
    assert (_values(store, e, "Host"), _values(store, m, "Host")) == (["SRV1", "SRV1"], [None, None])


def test_a_member_with_none_of_the_inputs_gets_an_empty_column(store, merged, write_csv):
    e, m, _ = merged
    other = store.ingest_csv(write_csv([["Msg"], ["hello"]], "other.csv"), name="other", build_fts=False)["id"]
    mid = store.create_merge("three", [e, m, other])["id"]
    res = store.add_derived_column(mid, "When", "TimeCreated", "coalesce", CO)
    _wait(store, res)
    assert _values(store, other, "When") == [None]
    # Empty there says nothing about the type: still a datetime column.
    assert _col(store, mid, "When")["type"] == "datetime"
    # ...and the preview spends no rows on it.
    pre = store.preview_derived(mid, "TimeCreated", "coalesce", CO)
    assert None not in [p["output"] for p in pre["preview"]]


# ------------------------------------------------------------- re-derive

def test_rederive_on_the_merge_and_on_a_member(store, merged):
    e, m, mid = merged
    res = store.add_derived_column(mid, "Thing", "Computer", "coalesce", {"extra_columns": ["FileName"]})
    _wait(store, res)
    def_e = res["member_definitions"][0]["id"]

    # On the merge, with the merge's columns.
    out = store.rederive_column(def_e, {"extra_columns": ["EventId", "FileName"]}, source_id=mid)
    _wait(store, out)
    assert _values(store, e, "Thing") == ["SRV1", "SRV1"]
    assert _values(store, m, "Thing") == ["a.exe", "b.dll"]
    with pytest.raises(ValueError, match="This merge has no column called 'Nope'"):
        store.rederive_column(def_e, {"extra_columns": ["Nope"]}, source_id=mid)

    # On the member's own table: the columns it was made with stand,
    # FileName included, though evtx has none — but a new name has to be
    # one evtx has. Before, the backfill failed on "no such column";
    # now it would be a silently empty input, so it is refused up front.
    _wait(store, store.rederive_column(def_e))
    assert _values(store, e, "Thing") == ["SRV1", "SRV1"]
    with pytest.raises(ValueError, match="No column called 'Nope' in 'evtx'"):
        store.rederive_column(def_e, {"extra_columns": ["Nope"]})
    assert store.get_derived_column(def_e)["params"] == {"extra_columns": ["EventId", "FileName"]}


# ------------------------------------------------- show the unparsed rows

def test_unparsed_rows_filter_on_a_member_that_lacks_an_input(store, write_csv):
    """A two-column op whose second column only one table has: the other
    table's rows had input (Start) and made nothing, so they are its
    unparsed rows. The filter for them names only the columns of the table
    it goes on — on that table End is no column at all."""
    a = store.ingest_csv(write_csv([["Start", "End"], ["2024-01-01 00:00:00", "2024-01-01 00:00:10"]], "a.csv"),
                         name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Start"], ["2024-01-02 00:00:00"], ["2024-01-03 00:00:00"]], "b.csv"),
                         name="b", build_fts=False)["id"]
    mid = store.create_merge("ab", [a, b])["id"]
    res = store.add_derived_column(mid, "Took", "End", "duration_delta", {"other_column": "Start"})
    _wait(store, res)
    def_a, def_b = (d["id"] for d in res["member_definitions"])
    assert _col(store, mid, "Took")["parse_failures"] == 2

    on_b = store.unparsed_where_fragment(def_b, b)
    assert '"End"' not in on_b
    assert len(_cells(store, b, "Start", filter_tree={"type": "raw", "sql": on_b})) == 2
    # The merge's column carries the first member's id; on the merge both
    # columns exist, and the filter finds b's two rows through it.
    on_merge = store.unparsed_where_fragment(def_a, mid)
    assert len(_cells(store, mid, "Start", filter_tree={"type": "raw", "sql": on_merge})) == 2
    with pytest.raises(KeyError):
        store.unparsed_where_fragment(def_a, b)   # not a's table, nor a merge over it


def test_unparsed_filter_route_takes_the_table(client, store, write_csv):
    a = store.ingest_csv(write_csv([["Start", "End"], ["x", "y"]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Start"], ["z"]], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("ab", [a, b])["id"]
    res = store.add_derived_column(mid, "Took", "End", "duration_delta", {"other_column": "Start"})
    _wait(store, res)
    def_b = res["member_definitions"][1]["id"]
    assert '"End"' not in client.get(f"/api/derived/{def_b}/unparsed_filter?source_id={b}").json()["sql"]
    assert '"End"' in client.get(f"/api/derived/{def_b}/unparsed_filter?source_id={mid}").json()["sql"]


# --------------------------------------------------------------- sessions

def test_session_carries_the_inputs_a_table_lacks(store, merged, tmp_path):
    e, m, mid = merged
    _wait(store, store.add_derived_column(mid, "When", "TimeCreated", "coalesce", CO))
    session = store.export_case_session()
    defs = {s["source"]["name"]: s["derived_columns"][0] for s in session["sources"]}
    assert defs["evtx"]["absent_inputs"] == ["Created0x10"]
    assert defs["mft"]["absent_inputs"] == ["TimeCreated"]

    other = Store(str(tmp_path / "other.db-winnow"))
    try:
        res = other.import_case_session(session)
        assert not [w for w in res.get("warnings", []) if "When" in w]
        other.wait_for_ingest_job(timeout=30)
        by_name = {s["name"]: s["id"] for s in other.list_sources()}
        assert _values(other, by_name["evtx"], "When") == [r[0] for r in EVTX[1:]]
        assert _values(other, by_name["mft"], "When") == [r[0] for r in MFT[1:]]
    finally:
        other.close()

    # A missing input the session does not vouch for is still refused —
    # a parent that failed to come back must not leave its child empty.
    for s in session["sources"]:
        s["derived_columns"][0].pop("absent_inputs")
    third = Store(str(tmp_path / "third.db-winnow"))
    try:
        res = third.import_case_session(session)
        assert any("not recreated" in w and "Created0x10" in w for w in res["warnings"])
    finally:
        third.close()


# ------------------------------------------------------ the datetime rule

@pytest.mark.parametrize("values, typ", [
    (["2024-01-01 10:00:00", "2024-01-02 11:00:00.5", ""], "datetime"),
    (["2024-01-01 10:00:00.1234567", None], "datetime"),
    (["2024-01-01T10:00:00", "2024-01-02 11:00:00"], "text"),    # a T: not the canonical shape
    (["2024-01-01 10:00:00Z"], "text"),                          # nor a zone after it
    (["2024-01-01 10:00:00", "-"], "text"),                      # no placeholder allowance
    (["1/2/2024 10:00:00"], "text"),                             # a US date is the import's, not ours
    (["2024-01-01 9:00:00"], "text"),                            # one-digit hour sorts wrong as text
    (["2024-01-01 10:00:00.5x"], "text"),
    (["12", "7"], "number"),
    ([None, ""], "empty"),
])
def test_a_text_op_column_of_canonical_timestamps_is_datetime(store, write_csv, values, typ):
    rows = [["V"]] + [[v or ""] for v in values]
    sid = store.ingest_csv(write_csv(rows, "t.csv"), name="t", build_fts=False)["id"]
    _wait(store, store.add_derived_column(sid, "Got", "V", "regex_extract", {"pattern": r"^(.*)$"}))
    d = next(d for d in store.list_derived_columns(sid) if d["name"] == "Got")
    assert d["inferred_type"] == typ
    assert _col(store, sid, "Got")["type"] == ("text" if typ == "empty" else typ)

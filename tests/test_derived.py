"""Derived datetime columns — the analyst-added columns computed from an
existing one (see timeparse.py for the parsers themselves).

Weighted toward the invariants this feature had to thread: source tables
are never mutated (#1), the unfiltered virtual-root path still pages by rid
range with pos = rid - 1 (#2), and the writer lock is only held per batch
(#4). Also pins the deliberate exclusions — derived values aren't
searchable and don't affect merge eligibility — since those are choices,
not oversights, and a future change should have to break a test to reverse
them."""

from __future__ import annotations

import pytest

from store import Store


EPOCH_ROWS = [
    ["Epoch", "Host", "Msg"],
    ["1700000000", "web01", "alpha"],
    ["1700000060", "web02", "bravo"],
    ["1700086400", "web01", "charlie"],   # next day
    ["not-a-time", "web03", "delta"],     # unparseable, on purpose
    ["", "web03", "echo"],                # empty — not a failure
]

SYSLOG_ROWS = [
    ["When", "Msg"],
    ["Dec 31 23:59:58", "before midnight"],
    ["Dec 31 23:59:59", "still before"],
    ["Jan  1 00:00:01", "after midnight"],
    ["Jan  2 08:00:00", "next day"],
]


def _ingest(store, write_csv, rows, name="t.csv"):
    path = write_csv(rows, name=name)
    return store.ingest_csv(path, build_fts=False)["id"]


def _add(store, source_id, name, column, op_id, params=None):
    res = store.add_derived_column(source_id, name, column, op_id, params or {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res["definition"]["id"]


def _cells(store, source_id, spec=None):
    view = store.build_view(source_id, spec or {})
    rows = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    cols = [c["name"] for c in store.get_source(source_id)["columns"]]
    return [dict(zip(cols, r["cells"])) for r in rows]


# --------------------------------------------------------------- lifecycle

def test_add_derived_column_computes_values_and_counts_failures(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    def_id = _add(store, sid, "Timestamp", "Epoch", "unix_epoch")

    d = store.get_derived_column(def_id)
    assert d["status"] == "ready"
    # "not-a-time" is a failure; the empty cell is just empty, not a failure.
    assert d["parse_failures"] == 1

    rows = _cells(store, sid)
    assert rows[0]["Timestamp"] == "2023-11-14 22:13:20"
    assert rows[3]["Timestamp"] is None
    assert rows[4]["Timestamp"] is None
    # Invariant #1: the source table itself is untouched.
    src_cols = {r[1] for r in store.db.execute(f"PRAGMA table_info(src_{sid})")}
    assert src_cols == {"rid", "Epoch", "Host", "Msg"}


def test_derived_column_appears_in_column_list_as_datetime(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    col = [c for c in store.get_source(sid)["columns"] if c["name"] == "Timestamp"][0]
    assert col["type"] == "datetime"
    assert col["derived"] is True
    assert col["derived_from"] == "Epoch"
    assert col["derived_status"] == "ready"


def test_name_collisions_and_reserved_names_are_refused(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    for bad in ("Epoch", "epoch", "Timestamp", "TIMESTAMP", "rid", "  "):
        with pytest.raises(ValueError):
            store.add_derived_column(sid, bad, "Epoch", "unix_epoch", {})
    with pytest.raises(ValueError):
        store.add_derived_column(sid, "Nope", "NoSuchColumn", "unix_epoch", {})
    with pytest.raises(ValueError):
        store.add_derived_column(sid, "Nope", "Epoch", "no_such_op", {})


def test_remove_then_re_add_the_same_name(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    def_id = _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    store.remove_derived_column(def_id)
    assert store.list_derived_columns(sid) == []
    assert all(c["name"] != "Timestamp" for c in store.get_source(sid)["columns"])
    # Re-adding the same name works whether or not DROP COLUMN was
    # available (an orphan physical column gets blanked and reused).
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    assert _cells(store, sid)[0]["Timestamp"] == "2023-11-14 22:13:20"


def test_rederive_with_a_corrected_base_year(store, write_csv):
    sid = _ingest(store, write_csv, SYSLOG_ROWS)
    def_id = _add(store, sid, "Time", "When", "syslog_bsd", {"base_year": 2023})
    assert _cells(store, sid)[0]["Time"].startswith("2023-12-31")

    res = store.rederive_column(def_id, {"base_year": 2024})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    rows = _cells(store, sid)
    assert rows[0]["Time"].startswith("2024-12-31")
    # The rollover state is rebuilt from scratch, so January still lands in
    # the following year rather than inheriting the old run's counter.
    assert rows[2]["Time"].startswith("2025-01-01")


def test_syslog_year_rollover_across_the_whole_table(store, write_csv):
    sid = _ingest(store, write_csv, SYSLOG_ROWS)
    _add(store, sid, "Time", "When", "syslog_bsd", {"base_year": 2023})
    times = [r["Time"] for r in _cells(store, sid)]
    assert times == [
        "2023-12-31 23:59:58", "2023-12-31 23:59:59",
        "2024-01-01 00:00:01", "2024-01-02 08:00:00",
    ]


def test_duration_delta_between_two_datetime_columns(store, write_csv):
    sid = _ingest(store, write_csv, [
        ["Start", "End"],
        ["2024-01-05 10:00:00", "2024-01-05 10:01:05"],
        ["2024-01-05 12:00:00", "2024-01-05 11:59:00"],  # negative
        ["2024-01-05 12:00:00", "junk"],
    ])
    _add(store, sid, "Elapsed", "End", "duration_delta", {"other_column": "Start"})
    vals = [r["Elapsed"] for r in _cells(store, sid)]
    assert vals == ["65.000000", "-60.000000", None]
    col = [c for c in store.get_source(sid)["columns"] if c["name"] == "Elapsed"][0]
    # Stored as seconds and typed number, so it sorts and filters through
    # the existing numeric paths rather than as text.
    assert col["type"] == "number" and col["derived_kind"] == "duration"


def test_a_column_another_derived_column_depends_on_cant_be_removed(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    parent = _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    _add(store, sid, "Elapsed", "Timestamp", "duration_delta", {"other_column": "Timestamp"})
    with pytest.raises(ValueError, match="computed from this column"):
        store.remove_derived_column(parent)


# ------------------------------------------------------- read-path integration

def test_virtual_root_paging_keeps_pos_equal_to_rid_minus_one(store, write_csv):
    """Invariant #2's carve-out: an unfiltered, unsorted view pages the
    source table by rid range and computes pos arithmetically. The derived
    sidecar joins on drv's INTEGER PRIMARY KEY, so it can't add or drop a
    row and that arithmetic stays exact."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    view = store.build_view(sid, {})
    assert view["kind"] == "root_virtual"  # still no materialisation
    rows = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    assert [r["pos"] for r in rows] == [r["rid"] - 1 for r in rows]
    assert len(rows) == 5


def test_virtual_root_query_plan_stays_index_driven(store, write_csv):
    """The same paging query, checked structurally: both tables reached by
    their INTEGER PRIMARY KEY and no temp b-tree. A plan that started
    scanning here would still return correct rows, so only EXPLAIN catches
    it — and depth-independent paging is the thing invariant #2 exists to
    protect."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    src = store._source_lite(sid)
    sql = (f'SELECT rid, "Epoch", "Timestamp" FROM {store._from_clause(src)} '
           f"WHERE rid >= ? AND rid < ? ORDER BY rid")
    with store._reader() as ro:
        plan = " | ".join(r[3] for r in ro.execute(f"EXPLAIN QUERY PLAN {sql}", (1, 10)))
    assert "TEMP B-TREE" not in plan.upper()
    assert f"src_{sid}" in plan and f"drv_{sid}" in plan
    assert "USING INTEGER PRIMARY KEY" in plan.upper()


def test_sort_filter_and_group_on_a_derived_column(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")

    desc = _cells(store, sid, {"sort": [{"column": "Timestamp", "dir": "desc"}]})
    assert desc[0]["Timestamp"] == "2023-11-15 22:13:20"

    filtered = _cells(store, sid, {
        "filters": [{"column": "Timestamp", "op": "contains", "value": "2023-11-14"}]
    })
    assert len(filtered) == 2

    view = store.build_view(sid, {})
    groups = store.group_summary(view["view_id"], "Timestamp")["groups"]
    # DAY_BUCKET'd like any other datetime column: two days plus the NULL
    # bucket for the rows that didn't parse.
    assert {g["value"] for g in groups} == {"2023-11-14", "2023-11-15", None}


def test_expand_and_tag_a_derived_column_group(store, write_csv):
    """The `s.DAY_BUCKET(...)` alias trap: a group's value is compared back
    against rows through the same DAY_BUCKET wrapper, and for a derived
    column that reference has to resolve against the sidecar."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    view = store.build_view(sid, {})
    sub = store.expand_group(view["view_id"], "Timestamp", "2023-11-14")
    assert sub["row_count"] == 2
    rows = store.fetch_rows(sub["view_id"], 0, 10)["rows"]
    assert [r["rid"] for r in rows] == [1, 2]

    tag_id = store.list_tags()[0]["id"]
    store.tag_view(sub["view_id"], tag_id, True)
    assert store.get_source(sid)["tagged_row_count"] == 2


def test_timeframe_filter_includes_derived_datetime_columns(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    rows = _cells(store, sid, {"time_range": {
        "enabled": True, "start": "2023-11-15 00:00:00", "end": "2023-11-16 00:00:00",
    }})
    assert [r["Msg"] for r in rows] == ["charlie"]


def test_derived_column_is_in_the_timeline_and_exports(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    tag_id = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag_id, True)

    tl = store.build_timeline(configs={sid: {"timestamp_column": "Timestamp"}})
    assert tl["row_count"] == 1
    assert store.fetch_timeline_rows(tl["view_id"], 0, 10)["rows"][0]["ts"] == "2023-11-14 22:13:20"

    view = store.build_view(sid, {})
    csv_text = "".join(store.export_view_csv(view["view_id"]))
    assert "Timestamp" in csv_text.splitlines()[0]
    assert "2023-11-14 22:13:20" in csv_text


def test_derived_values_are_not_searchable(store, write_csv):
    """A deliberate exclusion: the FTS doc view covers the source table
    only, so search results stay identical whether or not the index has
    been built — and a derived value is computed from text that's already
    searchable."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    assert _cells(store, sid, {"search": "2023-11-14"}) == []
    assert len(_cells(store, sid, {"search": "1700000000"})) == 1


def test_merge_eligibility_is_unaffected_by_derived_columns(store, write_csv):
    a = _ingest(store, write_csv, EPOCH_ROWS, name="a.csv")
    b = _ingest(store, write_csv, EPOCH_ROWS, name="b.csv")
    _add(store, a, "Timestamp", "Epoch", "unix_epoch")
    merge = store.create_merge("both", [a, b])
    # The merge itself shows base columns only — one member having a
    # derived column can't change what the merged table looks like.
    assert [c["name"] for c in merge["columns"]] == ["Epoch", "Host", "Msg"]
    assert store.build_view(merge["id"], {})["row_count"] == 10


def test_derived_columns_are_refused_on_a_merge(store, write_csv):
    a = _ingest(store, write_csv, EPOCH_ROWS, name="a.csv")
    b = _ingest(store, write_csv, EPOCH_ROWS, name="b.csv")
    merge = store.create_merge("both", [a, b])
    with pytest.raises(ValueError, match="merged"):
        store.add_derived_column(merge["id"], "Timestamp", "Epoch", "unix_epoch", {})


def test_column_max_lengths_and_values_cover_derived_columns(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    assert store.column_max_lengths(sid)["Timestamp"] == len("2023-11-14 22:13:20")
    vals = {v["value"] for v in store.column_values(sid, "Timestamp")}
    assert "2023-11-14 22:13:20" in vals


def test_dropping_the_source_cleans_up_the_sidecar(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    store.drop_source(sid)
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert f"drv_{sid}" not in tables
    assert store.db.execute("SELECT COUNT(*) FROM derived_columns").fetchone()[0] == 0


# ----------------------------------------------------------------- failures

def test_unparsed_rows_can_be_filtered_back_out(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    def_id = _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    frag = store.unparsed_where_fragment(def_id)
    rows = _cells(store, sid, {"filter_tree": {"type": "raw", "sql": frag}})
    assert [r["Msg"] for r in rows] == ["delta"]


# --------------------------------------------------------------- cancellation

def test_cancelling_a_new_columns_backfill_drops_the_column(store, write_csv):
    """Mirror of cancel-drops-the-partial-source: a half-filled derived
    column looks exactly like a finished one in the grid, and the analyst
    asked for it not to exist. Driven through the backfill's own cancel
    hook so the cancellation point is deterministic, not a race."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    res = store.add_derived_column(sid, "Timestamp", "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    def_id = res["definition"]["id"]

    with pytest.raises(Exception):
        store.backfill_derived_column(def_id, cancel=lambda: True, drop_on_cancel=True)
    assert store.list_derived_columns(sid) == []
    assert all(c["name"] != "Timestamp" for c in store.get_source(sid)["columns"])


def test_cancelling_a_rederive_keeps_the_column_and_flags_it(store, write_csv):
    """The opposite call: re-deriving an existing column was not a request
    to delete the analyst's column, so it survives — marked `partial` so
    the UI can say the values are a mix of two runs."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    def_id = _add(store, sid, "Timestamp", "Epoch", "unix_epoch")

    with pytest.raises(Exception):
        store.backfill_derived_column(def_id, cancel=lambda: True, drop_on_cancel=False)
    d = store.get_derived_column(def_id)
    assert d["status"] == "partial"
    assert _cells(store, sid)[0]["Timestamp"] == "2023-11-14 22:13:20"


# ---------------------------------------------------------------- detection

def test_detect_suggests_the_right_operation_with_a_preview(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    ranked = store.detect_timestamp_format(sid, "Epoch")
    assert ranked[0]["op_id"] == "unix_epoch"
    assert ranked[0]["preview"][0]["output"] == "2023-11-14 22:13:20"


def test_detect_source_suggestions_skips_already_typed_datetime_columns(store, write_csv):
    sid = _ingest(store, write_csv, [
        ["Epoch", "Existing"],
        ["1700000000", "2024-01-05 10:00:00"],
        ["1700000060", "2024-01-05 10:01:00"],
    ])
    cols = [s["column"] for s in store.detect_source_suggestions(sid)]
    assert cols == ["Epoch"]


def test_preview_reports_failures_before_anything_is_created(store, write_csv):
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    p = store.preview_derived(sid, "Epoch", "unix_epoch", {})
    assert p["failures"] == 1
    assert p["preview"][0]["output"] == "2023-11-14 22:13:20"
    assert store.list_derived_columns(sid) == []  # preview creates nothing


# ------------------------------------------------------------------ sessions

def test_session_round_trip_recreates_derived_columns(store, write_csv, case_path, tmp_path):
    sid = _ingest(store, write_csv, SYSLOG_ROWS)
    _add(store, sid, "Time", "When", "syslog_bsd", {"base_year": 2023})
    tag_id = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag_id, True)
    session = store.export_session(sid)
    # Definitions travel; values don't (they're recomputed on import).
    assert session["derived_columns"] == [
        {"name": "Time", "input_column": "When", "op_id": "syslog_bsd",
         "params": {"base_year": 2023}}
    ]
    assert [c["name"] for c in session["source"]["columns"]] == ["When", "Msg"]

    other = Store(str(tmp_path / "other.db"))
    try:
        other_sid = _ingest(other, write_csv, SYSLOG_ROWS, name="again.csv")
        res = other.import_session(other_sid, session)
        assert res["derived_columns_added"] == 1
        other.wait_for_ingest_job(timeout=30)
        assert _cells(other, other_sid)[2]["Time"] == "2024-01-01 00:00:01"
    finally:
        other.close()


def test_import_session_warns_instead_of_failing_on_a_name_collision(store, write_csv, tmp_path):
    sid = _ingest(store, write_csv, SYSLOG_ROWS)
    _add(store, sid, "Time", "When", "syslog_bsd", {"base_year": 2023})
    session = store.export_session(sid)
    res = store.import_session(sid, session)  # same case: "Time" already exists
    assert res["derived_columns_added"] == 0
    assert any("Time" in w for w in res["warnings"])


# ----------------------------------------------------------------- migration

def test_a_case_file_without_the_new_tables_opens_cleanly(store, write_csv, case_path):
    """META_SCHEMA is all CREATE IF NOT EXISTS, so an older case file just
    grows the two new tables on next open."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    store.close()
    old = Store(case_path)
    with old.lock, old.db:
        old.db.execute("DROP TABLE derived_columns")
        old.db.execute("DROP TABLE case_settings")
    old.close()

    reopened = Store(case_path)
    try:
        assert reopened.list_derived_columns(sid) == []
        assert len(_cells(reopened, sid)) == 5
        _add(reopened, sid, "Timestamp", "Epoch", "unix_epoch")
        assert _cells(reopened, sid)[0]["Timestamp"] == "2023-11-14 22:13:20"
    finally:
        reopened.close()


# ------------------------------------------------------------- case settings

def test_case_settings_round_trip(store):
    assert store.get_case_settings() == {}
    store.set_case_setting("ts_format", "iso")
    assert store.get_case_settings()["ts_format"] == "iso"
    store.set_case_setting("ts_format", "")
    assert store.get_case_settings() == {}


def test_suggestions_stop_once_a_column_is_converted(store, write_csv):
    """The post-import hint should go quiet for a column the analyst has
    already dealt with — otherwise it nags about work that's done."""
    sid = _ingest(store, write_csv, EPOCH_ROWS)
    assert [s["column"] for s in store.detect_source_suggestions(sid, 0.5)] == ["Epoch"]
    _add(store, sid, "Timestamp", "Epoch", "unix_epoch")
    assert store.detect_source_suggestions(sid, 0.5) == []


def test_unparsed_fragment_quotes_awkward_column_names(store, write_csv):
    """Column names are user data (invariant #5) — a header with a quote in
    it has to survive being turned into a filter fragment."""
    sid = _ingest(store, write_csv, [['Ep"och'], ["1700000000"], ["nope"]])
    def_id = _add(store, sid, 'When"ish', 'Ep"och', "unix_epoch")
    frag = store.unparsed_where_fragment(def_id)
    rows = _cells(store, sid, {"filter_tree": {"type": "raw", "sql": frag}})
    assert len(rows) == 1
    assert rows[0]['Ep"och'] == "nope"

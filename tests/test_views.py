"""store.py view materialization: filter ops, sort, numeric NULL-not-zero,
identifier quoting, view expiry, and the guided filter tree."""

from __future__ import annotations

import pytest


def _cells(store, view_id, start=0, count=100):
    return [r["cells"] for r in store.fetch_rows(view_id, start, count)["rows"]]


@pytest.mark.parametrize("op,value,expect_users", [
    ("contains", "jacs", {"ACME\\jacson"}),
    ("not_contains", "jacs", {"ACME\\admin", "ACME\\bob", "NT AUTHORITY\\SYSTEM"}),
    ("equals", "ACME\\admin", {"ACME\\admin"}),
    ("not_equals", "ACME\\admin", {"ACME\\jacson", "ACME\\bob", "NT AUTHORITY\\SYSTEM"}),
    ("starts", "ACME", {"ACME\\jacson", "ACME\\admin", "ACME\\bob"}),
    ("regex", "^ACME.(admin|bob)$", {"ACME\\admin", "ACME\\bob"}),
    ("empty", "", set()),
    ("not_empty", "", {"ACME\\jacson", "ACME\\admin", "ACME\\bob", "NT AUTHORITY\\SYSTEM"}),
    ("in", "ACME\\admin\nACME\\bob", {"ACME\\admin", "ACME\\bob"}),
])
def test_filter_ops(ingested, op, value, expect_users):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [{"column": "User", "op": op, "value": value}], "sort": []}
    view = store.build_view(source_id, spec)
    users = {c[2] for c in _cells(store, view["view_id"])}
    assert users == expect_users


def test_sort_multi_column(store, write_csv):
    path = write_csv([
        ["Group", "Seq"],
        ["b", "2"],
        ["a", "2"],
        ["a", "1"],
        ["b", "1"],
    ])
    rec = store.ingest_csv(path, name="sort.csv")
    spec = {
        "source_id": rec["id"], "filters": [],
        "sort": [{"column": "Group", "dir": "asc"}, {"column": "Seq", "dir": "asc"}],
    }
    view = store.build_view(rec["id"], spec)
    rows = _cells(store, view["view_id"])
    assert rows == [["a", "1"], ["a", "2"], ["b", "1"], ["b", "2"]]


def test_numeric_filter_and_sort_null_not_zero(store, write_csv):
    # The exact invariant CLAUDE.md calls out: a numeric-typed column's
    # non-numeric values (blank/"N/A") must come out NULL in a numeric
    # sort/compare, never silently coerced to 0 like a bare CAST would.
    #
    # infer_type requires *every* sampled value to look numeric (no
    # threshold, unlike datetime) — a stray "N/A" in the sample would
    # infer the whole column as "text" and this test would no longer be
    # exercising _numeric_expr at all. column_types forces "number" here,
    # standing in for the realistic case: a column that inferred as
    # numeric from its (separately sampled) first 500 rows, then hits a
    # stray non-numeric value later in a much larger file.
    path = write_csv([
        ["Bytes"],
        ["100"],
        ["N/A"],
        ["-5"],
        [""],
        ["0"],
    ])
    rec = store.ingest_csv(path, name="nums.csv", column_types=["number"])
    assert rec["columns"][0]["type"] == "number"

    # ">-10" should include -5, 0 and 100 but not the non-numeric rows —
    # a bare CAST(...AS REAL) would have coerced "N/A" and "" to 0.0, which
    # also satisfies > -10 and would wrongly appear.
    spec = {"source_id": rec["id"], "filters": [{"column": "Bytes", "op": ">", "value": "-10"}], "sort": []}
    view = store.build_view(rec["id"], spec)
    values = {c[0] for c in _cells(store, view["view_id"])}
    assert values == {"100", "-5", "0"}

    # Sorted ascending, NULLs (the non-numeric ones) land at one edge rather
    # than being scattered through the real numeric order.
    spec2 = {"source_id": rec["id"], "filters": [], "sort": [{"column": "Bytes", "dir": "asc"}]}
    view2 = store.build_view(rec["id"], spec2)
    ordered = [c[0] for c in _cells(store, view2["view_id"])]
    numeric_tail = ordered[-3:]
    assert numeric_tail == ["-5", "0", "100"]
    assert set(ordered[:2]) == {"N/A", ""}


def test_column_names_needing_quoting(store, write_csv):
    path = write_csv([
        ['Has "Quote"', "Has Space"],
        ["x", "y"],
    ])
    rec = store.ingest_csv(path, name="quoted.csv")
    names = [c["name"] for c in rec["columns"]]
    spec = {
        "source_id": rec["id"],
        "filters": [{"column": names[1], "op": "equals", "value": "y"}],
        "sort": [{"column": names[0], "dir": "asc"}],
    }
    view = store.build_view(rec["id"], spec)
    assert _cells(store, view["view_id"]) == [["x", "y"]]


def test_paging_via_pos_does_not_resort(store, tmp_path):
    path = tmp_path / "many.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("N\n")
        for i in range(1000):
            f.write(f"{i}\n")
    rec = store.ingest_csv(str(path), name="many.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": [{"column": "N", "dir": "asc"}]}
    view = store.build_view(rec["id"], spec)
    # A page fetched deep into the view must be exactly rows 500..509 in
    # order, not re-sorted or shuffled by an offset-based re-query.
    page = _cells(store, view["view_id"], start=500, count=10)
    assert [c[0] for c in page] == [str(n) for n in range(500, 510)]


def test_view_expires_after_close(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    store.close_view(view["view_id"])
    with pytest.raises(KeyError):
        store.fetch_rows(view["view_id"], 0, 10)


def _v_table_names(store):
    return {r[0] for r in store.db.execute("SELECT name FROM v.sqlite_master WHERE type='table'")}


def test_unfiltered_unsorted_view_is_virtual_and_builds_no_table(ingested):
    # CLAUDE.md invariant #2's carve-out: opening a table with no filter and
    # no sort needs no v.view_N at all — rid order is already free.
    store, source_id = ingested
    before = _v_table_names(store)
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root_virtual"
    assert view["row_count"] == 4
    assert _v_table_names(store) == before  # no new backing table created
    rows = _cells(store, view["view_id"])
    assert len(rows) == 4
    assert rows[0][1] == "4624"  # still rid order (first ingested row)


@pytest.mark.parametrize("spec_extra", [
    {"sort": [{"column": "Timestamp", "dir": "asc"}]},  # any sort (even asc) opts out — see build_view
    {"sort": [{"column": "Timestamp", "dir": "desc"}]},
    {"sort": [{"column": "EventId", "dir": "asc"}]},  # numeric sort
    {"sort": [{"column": "Timestamp", "dir": "asc"}, {"column": "User", "dir": "asc"}]},  # multi-column
    {"filters": [{"column": "EventId", "op": "equals", "value": "4624"}]},
])
def test_filtered_or_sorted_view_still_materializes(ingested, spec_extra):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": [], **spec_extra}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root"


def test_merge_never_goes_virtual_even_when_unfiltered(store, write_csv):
    p1 = write_csv([["Host", "User"], ["H1", "alice"]], name="m1.csv")
    p2 = write_csv([["Host", "User"], ["H1", "bob"]], name="m2.csv")
    rec1 = store.ingest_csv(p1, name="m1.csv", build_fts=False)
    rec2 = store.ingest_csv(p2, name="m2.csv", build_fts=False)
    merge = store.create_merge("merged", [rec1["id"], rec2["id"]])
    spec = {"source_id": merge["id"], "filters": [], "sort": []}
    view = store.build_view(merge["id"], spec)
    assert store._views[view["view_id"]]["kind"] == "root"


def test_virtual_root_tag_positions_is_rid_minus_one(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root_virtual"

    tag = store.upsert_tag(None, "t1", "#ff0000", None)
    store.set_tags(source_id, [2], tag["id"], True)  # tag just rid 2

    positions = store.tag_positions(view["view_id"])
    assert positions == [[1, tag["id"]]]  # rid 2 -> pos 1


def test_virtual_root_find_position(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root_virtual"

    assert store.find_position(view["view_id"], source_id, 1) == 0
    assert store.find_position(view["view_id"], source_id, 4) == 3
    assert store.find_position(view["view_id"], source_id, 9999) is None  # no such rid
    assert store.find_position(view["view_id"], source_id + 1000, 1) is None  # wrong source


def test_virtual_root_tag_view_whole_table_with_exclusions(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root_virtual"

    tag = store.upsert_tag(None, "bulk", "#00ff00", None)
    out = store.tag_view(view["view_id"], tag["id"], True, exclude=[[source_id, 2]])
    assert out["affected"] == 3
    assert out["counts"][str(tag["id"])] == 3

    positions = {p for p, tid in store.tag_positions(view["view_id"])}
    assert positions == {0, 2, 3}  # rid 2 (pos 1) was excluded

    out2 = store.tag_view(view["view_id"], tag["id"], False)
    assert out2["counts"].get(str(tag["id"]), 0) == 0


def test_virtual_root_export_csv_matches_rid_order(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)
    assert store._views[view["view_id"]]["kind"] == "root_virtual"

    body = "".join(store.export_view_csv(view["view_id"]))
    lines = body.strip("\n").split("\n")
    assert lines[0] == "Line,Tags,Note,Timestamp,EventId,User,Process,CommandLine"
    assert len(lines) == 5  # header + 4 rows
    assert lines[1].startswith("1,")
    assert lines[4].startswith("4,")


def test_virtual_root_evicted_when_view_rebuilt(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view1 = store.build_view(source_id, spec)
    assert store._views[view1["view_id"]]["kind"] == "root_virtual"

    view2 = store.build_view(source_id, spec)
    assert view1["view_id"] not in store._views  # old root_virtual handle evicted
    assert store._views[view2["view_id"]]["kind"] == "root_virtual"


def test_filter_tree_and_or_nesting(ingested):
    store, source_id = ingested
    # (User contains "jacs") OR (Process equals "cmd.exe")
    tree = {
        "type": "group", "op": "OR",
        "children": [
            {"type": "cond", "column": "User", "op": "contains", "value": "jacs"},
            {"type": "cond", "column": "Process", "op": "equals", "value": "cmd.exe"},
        ],
    }
    spec = {"source_id": source_id, "filters": [], "sort": [], "filter_tree": tree}
    view = store.build_view(source_id, spec)
    users = {c[2] for c in _cells(store, view["view_id"])}
    assert users == {"ACME\\jacson", "ACME\\admin"}


def test_equals_filter_triggers_lazy_background_index(ingested):
    # A plain 'equals' filter (flat filters list) — the sargable op that
    # actually benefits from a single-column B-tree index — should kick off
    # a background CREATE INDEX for that column, same lazy pattern as FTS.
    store, source_id = ingested
    assert not store._column_index_exists("src_" + str(source_id), "EventId")
    spec = {"source_id": source_id, "filters": [{"column": "EventId", "op": "equals", "value": "4624"}], "sort": []}
    view = store.build_view(source_id, spec)
    assert {c[2] for c in _cells(store, view["view_id"])} == {"ACME\\jacson"}
    assert store.wait_for_column_index(source_id, "EventId", timeout=5)


def test_filter_tree_equals_leaf_triggers_lazy_background_index(ingested):
    store, source_id = ingested
    tree = {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "EventId", "op": "equals", "value": "4625"},
    ]}
    spec = {"source_id": source_id, "filters": [], "sort": [], "filter_tree": tree}
    store.build_view(source_id, spec)
    assert store.wait_for_column_index(source_id, "EventId", timeout=5)


def test_contains_filter_does_not_trigger_column_index(ingested):
    # 'contains' is a leading-wildcard LIKE — a plain B-tree index can't
    # accelerate it (that's what the trigram FTS index is for instead), so
    # it shouldn't trigger one.
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [{"column": "User", "op": "contains", "value": "jacs"}], "sort": []}
    store.build_view(source_id, spec)
    assert not store.wait_for_column_index(source_id, "User", timeout=1)


def test_column_index_actually_used_once_built(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [{"column": "EventId", "op": "equals", "value": "4624"}], "sort": []}
    store.build_view(source_id, spec)
    assert store.wait_for_column_index(source_id, "EventId", timeout=5)
    plan = " ".join(
        r["detail"] for r in store.db.execute(
            f'EXPLAIN QUERY PLAN SELECT rid FROM src_{source_id} WHERE "EventId" = ?', ("4624",)
        ).fetchall()
    )
    assert "USING" in plan and "INDEX" in plan  # no longer a bare SCAN


def test_sort_triggers_lazy_background_sort_index(ingested):
    # A non-numeric sort column should kick off a background index build,
    # same lazy pattern as a sargable filter — but a distinct index (purpose
    # "sort"), since it has to carry COLLATE NOCASE to match _compile_order.
    store, source_id = ingested
    table = "src_" + str(source_id)
    assert not store._column_index_exists(table, "User", "sort")
    spec = {"source_id": source_id, "filters": [], "sort": [{"column": "User", "dir": "asc"}]}
    view = store.build_view(source_id, spec)
    assert [c[2] for c in _cells(store, view["view_id"])] == sorted(
        ["ACME\\jacson", "ACME\\admin", "ACME\\bob", "NT AUTHORITY\\SYSTEM"], key=str.lower
    )
    assert store.wait_for_column_index(source_id, "User", timeout=5, purpose="sort")
    # No sargable filter was used, so the separate filter-purpose index
    # should not have been built as a side effect.
    assert not store._column_index_exists(table, "User", "filter")


def test_numeric_sort_does_not_trigger_column_index(ingested):
    # EventId sorts through _numeric_expr (a functional expression) —  a
    # plain B-tree index on the raw column can't serve that, so a numeric
    # sort shouldn't build one the way a numeric filter already doesn't.
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": [{"column": "EventId", "dir": "asc"}]}
    store.build_view(source_id, spec)
    assert not store.wait_for_column_index(source_id, "EventId", timeout=1, purpose="sort")


def test_sort_index_actually_used_once_built(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": [{"column": "User", "dir": "asc"}]}
    store.build_view(source_id, spec)
    assert store.wait_for_column_index(source_id, "User", timeout=5, purpose="sort")
    plan = " ".join(
        r["detail"] for r in store.db.execute(
            f'EXPLAIN QUERY PLAN SELECT rid FROM src_{source_id} ORDER BY "User" COLLATE NOCASE ASC'
        ).fetchall()
    )
    assert "USING" in plan and "INDEX" in plan
    assert "TEMP B-TREE" not in plan


def test_filter_and_sort_indexes_on_same_column_are_independent(ingested):
    # A column that's both equals-filtered and sorted on ends up with two
    # small physical indexes (different collation), not one shared index —
    # verify both get built, both get listed, and dropping removes both.
    store, source_id = ingested
    table = "src_" + str(source_id)
    store.build_view(source_id, {
        "filters": [{"column": "Process", "op": "equals", "value": "cmd.exe"}], "sort": [],
    })
    assert store.wait_for_column_index(source_id, "Process", timeout=5, purpose="filter")
    store.build_view(source_id, {
        "filters": [], "sort": [{"column": "Process", "dir": "asc"}],
    })
    assert store.wait_for_column_index(source_id, "Process", timeout=5, purpose="sort")

    filter_name = store._column_index_name(table, "Process", "filter")
    sort_name = store._column_index_name(table, "Process", "sort")
    assert filter_name != sort_name
    existing = {
        r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    }
    assert {filter_name, sort_name} <= existing

    listed = store.list_column_indexes(source_id)
    assert [ix["column"] for ix in listed if ix["column"] == "Process"] == ["Process"]

    store.drop_column_index(source_id, "Process")
    existing_after = {
        r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    }
    assert filter_name not in existing_after and sort_name not in existing_after


def test_filter_tree_raw_node_round_trips_through_validation(ingested):
    store, source_id = ingested
    tree = {"type": "raw", "sql": "EventId = '4624'"}
    spec = {"source_id": source_id, "filters": [], "sort": [], "filter_tree": tree}
    view = store.build_view(source_id, spec)
    assert _cells(store, view["view_id"]) == [["2024-01-05 13:22:01", "4624", "ACME\\jacson", "svchost.exe", "C:\\Windows\\System32\\svchost.exe"]]


def test_validate_where_fragment_rejects_select_and_unknown_identifier(ingested):
    store, source_id = ingested
    with pytest.raises(ValueError):
        store.validate_where_fragment(source_id, "EventId IN (SELECT tag_id FROM row_tags)")
    with pytest.raises(ValueError):
        store.validate_where_fragment(source_id, "NotAColumn = '1'")
    store.validate_where_fragment(source_id, "EventId = '4624'")  # doesn't raise


# --------------------------------------------------------------- time_range

def test_ts_normalize_handles_iso_us_ampm_and_missing_time():
    from winnow.store import _ts_normalize

    assert _ts_normalize("2026-01-05") == "2026-01-05 00:00:00"
    assert _ts_normalize("2026-01-05 13:22:01") == "2026-01-05 13:22:01"
    assert _ts_normalize("2026-01-05T13:22:01") == "2026-01-05 13:22:01"
    assert _ts_normalize("1/5/2026") == "2026-01-05 00:00:00"
    assert _ts_normalize("1/5/2026 1:22:01 PM") == "2026-01-05 13:22:01"
    assert _ts_normalize("1/5/2026 12:00:00 AM") == "2026-01-05 00:00:00"
    assert _ts_normalize("1/5/2026 12:00:00 PM") == "2026-01-05 12:00:00"
    assert _ts_normalize("not a date") is None
    assert _ts_normalize(None) is None


@pytest.fixture
def mft_like(store, write_csv):
    """A tiny stand-in for an MFT-style table: two datetime columns
    (Created can be timestomped independently of Modified), plus a couple
    of rows spanning the boundary of interest."""
    rows = [
        ["Created", "Modified", "Name"],
        ["2020-01-01T00:00:00", "2026-01-15T10:00:00", "timestomped.exe"],  # spoofed Created, real Modified in-range
        ["2026-01-10T00:00:00", "2026-01-10T00:00:00", "normal_in_range.txt"],
        ["2019-05-01T00:00:00", "2019-05-01T00:00:00", "old_file.txt"],  # fully out of range
        ["2026-01-20T00:00:00", "2020-03-03T00:00:00", "created_only.dll"],  # Created in-range, Modified isn't
    ]
    path = write_csv(rows)
    rec = store.ingest_csv(path, name="mft.csv", build_fts=False)
    return store, rec["id"]


def _names(store, spec):
    view = store.build_view(spec["source_id"], spec)
    return [r["cells"][2] for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]]


def test_time_range_disabled_returns_everything(mft_like):
    store, source_id = mft_like
    spec = {
        "source_id": source_id, "filters": [], "sort": [],
        "time_range": {"enabled": False, "column": None, "start": "2026-01-01T00:00", "end": "2026-01-31T23:59"},
    }
    assert set(_names(store, spec)) == {"timestomped.exe", "normal_in_range.txt", "old_file.txt", "created_only.dll"}


def test_time_range_specific_column_only_matches_that_column(mft_like):
    store, source_id = mft_like
    spec = {
        "source_id": source_id, "filters": [], "sort": [],
        "time_range": {"enabled": True, "column": "Created", "start": "2026-01-01T00:00", "end": "2026-01-31T23:59"},
    }
    # timestomped.exe's Created is spoofed to 2020 -> missed when scoped to Created only.
    assert set(_names(store, spec)) == {"normal_in_range.txt", "created_only.dll"}


def test_time_range_all_columns_catches_any_matching_timestamp(mft_like):
    store, source_id = mft_like
    spec = {
        "source_id": source_id, "filters": [], "sort": [],
        "time_range": {"enabled": True, "column": None, "start": "2026-01-01T00:00", "end": "2026-01-31T23:59"},
    }
    # Now timestomped.exe is caught via its real Modified date.
    assert set(_names(store, spec)) == {"timestomped.exe", "normal_in_range.txt", "created_only.dll"}


def test_time_range_combines_with_a_normal_filter_as_and(mft_like):
    store, source_id = mft_like
    spec = {
        "source_id": source_id, "sort": [],
        "filters": [{"column": "Name", "op": "contains", "value": "normal"}],
        "time_range": {"enabled": True, "column": None, "start": "2026-01-01T00:00", "end": "2026-01-31T23:59"},
    }
    assert _names(store, spec) == ["normal_in_range.txt"]


def test_time_range_open_ended_start_or_end(mft_like):
    store, source_id = mft_like
    only_after = {
        "source_id": source_id, "filters": [], "sort": [],
        "time_range": {"enabled": True, "column": "Modified", "start": "2025-01-01T00:00", "end": None},
    }
    assert set(_names(store, only_after)) == {"timestomped.exe", "normal_in_range.txt"}

    only_before = {
        "source_id": source_id, "filters": [], "sort": [],
        "time_range": {"enabled": True, "column": "Modified", "start": None, "end": "2020-12-31T23:59"},
    }
    assert set(_names(store, only_before)) == {"old_file.txt", "created_only.dll"}


def test_find_nearest_timestamp_on_every_view_kind(ingested):
    """Jump-to-timestamp measures closeness in real time (julianday over
    TS_NORMALIZE), not string order, and must return a position that
    fetch_rows agrees with on all three view shapes."""
    store, sid = ingested

    # root_virtual: pos = rid - 1 (invariant #2's carve-out)
    v = store.build_view(sid, {})
    assert v["kind"] == "root_virtual"
    hit = store.find_nearest_timestamp(v["view_id"], "2024-01-05 13:23:00")
    assert (hit["rid"], hit["pos"], hit["ts"]) == (2, 1, "2024-01-05 13:23:11")
    # 13:22:36 is 35s from row 1 and 35s from row 2 — nearer wins, ties are fine either way;
    # a clearly-nearer probe pins the math:
    assert store.find_nearest_timestamp(v["view_id"], "2024-01-05 13:22:05")["rid"] == 1

    # materialized (sorted desc): pos must be the view's own, not rid math
    v2 = store.build_view(sid, {"sort": [{"column": "Timestamp", "dir": "desc"}]})
    hit = store.find_nearest_timestamp(v2["view_id"], "2024-01-07 20:00:00", "Timestamp")
    assert hit["rid"] == 4  # 22:01:59 the same evening — ~2h away, vs ~35h to the previous row
    row = store.fetch_rows(v2["view_id"], hit["pos"], 1)["rows"][0]
    assert row["rid"] == hit["rid"]

    # group_virtual: pos is the row's rank among the group's rid-ordered rows
    root = store.build_view(sid, {})
    g = store.expand_group(root["view_id"], "User", "ACME\\bob")
    hit = store.find_nearest_timestamp(g["view_id"], "2024-01-01 00:00:00")
    assert hit["rid"] == 3 and hit["pos"] == 0


def test_find_nearest_timestamp_rejects_garbage_and_misses(ingested):
    store, sid = ingested
    v = store.build_view(sid, {})
    with pytest.raises(ValueError):
        store.find_nearest_timestamp(v["view_id"], "not a time")
    with pytest.raises(ValueError):
        store.find_nearest_timestamp(v["view_id"], "2024-01-05 10:00:00", "User")
    # A filtered view with no parseable timestamps in range still answers
    # (nearest is nearest, even far away); an all-unparseable column is the
    # None case — simulate by filtering to nothing.
    empty = store.build_view(sid, {"filters": [{"column": "User", "op": "equals", "value": "nobody"}]})
    assert store.find_nearest_timestamp(empty["view_id"], "2024-01-05 10:00:00") is None


def test_merge_names_are_unique_case_insensitively(store, write_csv):
    """Two merges named "EVTX" and "evtx" are one typo'd reference apart in
    every list both appear in — so the name is unique at *both* doors into
    merges.name: creation, and rename via set_source_nickname (a merge has
    no file behind it, so its nickname is a rename)."""
    rows = [["A", "B"], ["1", "2"], ["3", "4"]]
    ids = [store.ingest_csv(write_csv(rows, f"m{i}.csv"), name=f"m{i}", build_fts=False)["id"]
           for i in range(3)]
    first = store.create_merge("EVTX", ids[:2])

    import pytest as _pytest
    with _pytest.raises(ValueError, match="already exists"):
        store.create_merge("evtx", [ids[0], ids[2]])

    second = store.create_merge("Other", [ids[0], ids[2]])
    with _pytest.raises(ValueError, match="already exists"):
        store.set_source_nickname(second["id"], "EVTX")

    # Renaming a merge to its own name (a no-op save) is not a collision.
    assert store.set_source_nickname(first["id"], "EVTX")["name"] == "EVTX"

    with _pytest.raises(ValueError, match="needs a name"):
        store.create_merge("   ", ids[:2])

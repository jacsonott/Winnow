"""Merging tables whose columns don't match.

A merge exposes every member's columns (col_mode 'all') or only those all
members share ('shared'), matched by name ignoring case. A member lacking an
exposed column reads it as NULL. Before this, every query on a merge wrote
the merge's column names straight into each member's SQL, so a missing one
either errored or — through SQLite's double-quoted-string fallback — came
back as the column's NAME in every cell. These tests run a mismatched merge
through each read path and compare against answers worked out by hand."""

from __future__ import annotations

import csv
import io

import pytest

# A and B are "the same export" from two tool versions: B spells two
# headers differently (case only) and adds Extra. C is another tool that
# shares only Time and Msg.
A_ROWS = [["Time", "EventId", "Host", "Msg"],
          ["2024-01-01 10:00:00", "4624", "WS01", "alpha logon"],
          ["2024-01-01 11:00:00", "4688", "WS01", "beta process"]]
B_ROWS = [["TIME", "EventID", "Host", "Msg", "Extra"],
          ["2024-01-02 10:00:00", "4624", "WS02", "gamma logon", "x"],
          ["2024-01-02 11:00:00", "4625", "WS02", "delta failed", "y"],
          ["2024-01-02 12:00:00", "4624", "WS02", "epsilon logon", "x"]]
C_ROWS = [["Time", "Msg", "Other"],
          ["2024-01-03 09:00:00", "zeta note", "needle"]]


def _ingest(store, write_csv, rows, name):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=f"{name}.csv")["id"]


@pytest.fixture
def abc(store, write_csv):
    a = _ingest(store, write_csv, A_ROWS, "a")
    b = _ingest(store, write_csv, B_ROWS, "b")
    c = _ingest(store, write_csv, C_ROWS, "c")
    return a, b, c


def _rows(store, view):
    out = store.fetch_rows(view["view_id"], 0, 1000)["rows"]
    return out


def _cells(store, mid, view, column):
    names = [c["name"] for c in store.get_source(mid)["columns"]]
    i = names.index(column)
    return [r["cells"][i] for r in _rows(store, view)]


def _view(store, mid, **spec):
    spec.setdefault("filters", [])
    spec.setdefault("sort", [])
    return store.build_view(mid, {"source_id": mid, **spec})


# ------------------------------------------------------------ the column list

def test_mismatched_tables_merge_with_every_column(store, abc):
    a, b, c = abc
    m = store.create_merge("all three", [a, b, c])
    assert [col["name"] for col in m["columns"]] == ["Time", "EventId", "Host", "Msg", "Extra", "Other"]
    assert m["row_count"] == 6
    missing = {mem["id"]: mem["missing"] for mem in m["members"]}
    assert missing == {a: ["Extra", "Other"], b: ["Other"], c: ["EventId", "Host", "Extra"]}


def test_shared_mode_keeps_only_common_columns(store, abc):
    a, b, c = abc
    m = store.create_merge("shared", [a, b, c], col_mode="shared")
    assert [col["name"] for col in m["columns"]] == ["Time", "Msg"]
    m2 = store.create_merge("ab shared", [a, b], col_mode="shared")
    assert [col["name"] for col in m2["columns"]] == ["Time", "EventId", "Host", "Msg"]


def test_shared_mode_refuses_tables_with_nothing_in_common(store, write_csv, abc):
    a, _, _ = abc
    d = _ingest(store, write_csv, [["Unrelated"], ["1"]], "d")
    with pytest.raises(ValueError, match="no columns in common"):
        store.create_merge("nothing", [a, d], col_mode="shared")
    # 'all' takes them: the builder warns, the engine doesn't refuse.
    assert store.create_merge("anything", [a, d])["row_count"] == 3


def test_merge_input_is_checked(store, abc):
    a, b, _ = abc
    with pytest.raises(ValueError, match="at least 2"):
        store.create_merge("one", [a])
    with pytest.raises(ValueError, match="once"):
        store.create_merge("dup", [a, a, b])
    with pytest.raises(ValueError, match="column mode"):
        store.create_merge("mode", [a, b], col_mode="some")


# ------------------------------------------------------------ reading rows

def test_missing_columns_read_empty_and_respelled_ones_read_their_values(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    assert v["row_count"] == 6
    assert _cells(store, mid, v, "EventId") == ["4624", "4688", "4624", "4625", "4624", None]
    assert _cells(store, mid, v, "Extra") == [None, None, "x", "y", "x", None]
    assert _cells(store, mid, v, "Other") == [None] * 5 + ["needle"]
    # B's "TIME" header sorts with everyone else's "Time".
    assert _cells(store, mid, v, "Time")[2] == "2024-01-02 10:00:00"


def test_no_column_name_leaks_into_a_cell(store, abc):
    """The bug this guards: an unknown double-quoted name is a string
    literal in SQLite, so a member without "Extra" used to read "Extra"."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid)
    names = {col["name"] for col in store.get_source(mid)["columns"]}
    for r in _rows(store, v):
        assert not names & {cell for cell in r["cells"] if cell is not None}


@pytest.mark.parametrize("flt, expected", [
    ({"column": "Extra", "op": "equals", "value": "x"}, 2),
    ({"column": "Extra", "op": "not_empty", "value": ""}, 3),
    ({"column": "Extra", "op": "empty", "value": ""}, 3),
    ({"column": "Extra", "op": "not_equals", "value": "x"}, 4),
    ({"column": "EventId", "op": "equals", "value": "4624"}, 3),
    ({"column": "Other", "op": "contains", "value": "need"}, 1),
    ({"column": "Host", "op": "in", "value": ["WS01", "WS02"]}, 5),
])
def test_filters_treat_a_missing_column_as_empty(store, abc, flt, expected):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    assert _view(store, mid, filters=[flt])["row_count"] == expected


def test_filter_tree_and_raw_fragment_on_a_partial_column(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    tree = {"type": "cond", "column": "Extra", "op": "equals", "value": "y"}
    assert _view(store, mid, filter_tree=tree)["row_count"] == 1
    store.validate_where_fragment(mid, '"Extra" = \'x\'')
    raw = {"type": "raw", "sql": '"Extra" = \'x\' OR "Other" IS NOT NULL'}
    assert _view(store, mid, filter_tree=raw)["row_count"] == 3


def test_search_reaches_every_member_own_columns(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    assert _view(store, mid, search="needle")["row_count"] == 1
    assert _view(store, mid, search="logon")["row_count"] == 3
    assert _view(store, mid, search="needle", search_mode="regex")["row_count"] == 1


def test_search_in_shared_mode_still_sees_a_member_hidden_columns(store, abc):
    """The search blob is each member's own base columns, as on a plain
    table — a column the merge doesn't show is still evidence."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c], col_mode="shared")["id"]
    assert _view(store, mid, search="needle")["row_count"] == 1


def test_sort_on_a_partial_column(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Extra", "dir": "desc"}, {"column": "Time", "dir": "asc"}])
    assert _cells(store, mid, v, "Extra")[:3] == ["y", "x", "x"]


def test_timeframe_on_a_column_a_member_lacks_uses_its_own_timestamps(store, write_csv, abc):
    """The timeframe filter's documented fallback: a member that doesn't
    have the chosen datetime column is judged by its own datetime columns,
    not dropped wholesale."""
    a, _, _ = abc
    e = _ingest(store, write_csv, [["Logged", "Msg"], ["2024-01-01 10:30:00", "in"],
                                   ["2024-06-01 00:00:00", "out"]], "e")
    mid = store.create_merge("m", [a, e])["id"]
    tr = {"enabled": True, "column": "Time", "start": "2024-01-01 00:00:00", "end": "2024-01-31 23:59:59"}
    v = _view(store, mid, time_range=tr)
    assert v["row_count"] == 3
    assert sorted(_cells(store, mid, v, "Msg")) == ["alpha logon", "beta process", "in"]


# ------------------------------------------------- grouping and aggregates

def test_group_by_a_partial_column_and_expand_its_empty_group(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid)
    groups = {g["value"]: g["count"] for g in store.group_summary(v["view_id"], "Extra")["groups"]}
    assert groups == {"x": 2, "y": 1, None: 3}
    sub = store.expand_group(v["view_id"], "Extra", None)
    assert sub["row_count"] == 3
    # The view-join shape (a filtered parent) too.
    v2 = _view(store, mid, filters=[{"column": "Msg", "op": "contains", "value": "o"}])
    groups = {g["value"]: g["count"] for g in store.group_summary(v2["view_id"], "Host")["groups"]}
    assert groups == {"WS01": 2, "WS02": 2, None: 1}   # "delta failed" has no "o"
    assert store.expand_group(v2["view_id"], "Host", "WS02")["row_count"] == 2
    assert store.expand_group(v2["view_id"], "Host", None)["row_count"] == 1


def test_nested_group_path_through_a_partial_column(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, filters=[{"column": "Msg", "op": "not_empty", "value": ""}])
    path = [{"column": "Extra", "value": "x"}]
    groups = store.group_summary(v["view_id"], "EventId", path=path)["groups"]
    assert groups == [{"value": "4624", "count": 2}]


def test_histogram_counts_every_member(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    hist = store.time_histogram(_view(store, mid)["view_id"], "Time")
    assert hist["total"] == 6
    v = _view(store, mid, filters=[{"column": "Extra", "op": "empty", "value": ""}])
    assert store.time_histogram(v["view_id"], "Time")["total"] == 3


def test_value_list_and_widths_ignore_missing_columns(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    values = {r["value"]: r["count"] for r in store.column_values(mid, "Extra")}
    assert values == {None: 3, "x": 2, "y": 1}
    widths = store.column_max_lengths(mid)
    assert widths["Extra"] == 1 and widths["Other"] == len("needle")


def test_jump_to_timestamp_finds_a_member_with_respelled_column(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    hit = store.find_nearest_timestamp(v["view_id"], "2024-01-02 11:59:00", "Time")
    assert (hit["source_id"], hit["rid"]) == (b, 3)


def test_tag_time_bounds_cover_a_respelled_member(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    tag = store.list_tags()[0]["id"]
    store.set_tags(b, [3], tag, True)
    assert store.tag_time_bounds(mid)["end"].startswith("2024-01-02 12:00:00")


def test_indexes_are_built_only_where_the_column_exists(store, abc):
    """A filter on a merge column builds the member's index — under the
    member's own spelling, so the Tables manager can list and drop it —
    and none at all on a member that lacks the column (CREATE INDEX on a
    missing quoted name would index a string constant)."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    _view(store, mid, filters=[{"column": "EventId", "op": "equals", "value": "4624"}])
    assert store.wait_for_column_index(b, "EventID", timeout=10)
    assert [i["column"] for i in store.list_column_indexes(b)] == ["EventID"]
    assert store.list_column_indexes(c) == []
    with store.lock:
        tables = {r[0] for r in store.db.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'")}
    assert store.get_source(c)["table_name"] not in tables


# ------------------------------------------------------- export and SQL

def test_csv_export_pads_missing_columns(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    body = "".join(store.export_view_csv(v["view_id"]))
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][:4] == ["Line", "Table", "Tags", "Note"]
    assert rows[0][4:] == ["Time", "EventId", "Host", "Msg", "Extra", "Other"]
    assert rows[1][4:] == ["2024-01-01 10:00:00", "4624", "WS01", "alpha logon", "", ""]
    assert rows[6][4:] == ["2024-01-03 09:00:00", "", "", "zeta note", "", "needle"]


def test_spec_sql_runs_in_the_pane(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    sql = store.spec_sql(mid, {"source_id": mid, "filters": [{"column": "Extra", "op": "empty", "value": ""}],
                               "sort": [{"column": "Time", "dir": "asc"}]})
    res = store.run_sql(sql)
    assert len(res["rows"]) == 3
    assert "Extra" not in [cell for row in res["rows"] for cell in row]


def test_sql_pane_merge_view_pads_missing_columns(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    res = store.run_sql(f'SELECT "EventId", "Extra", "Other" FROM merge_{-mid} ORDER BY "Time"')
    assert res["rows"] == [["4624", None, None], ["4688", None, None], ["4624", "x", None],
                           ["4625", "y", None], ["4624", "x", None], [None, None, "needle"]]


def test_save_view_as_table_pads_missing_columns(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c], source_column=True)["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    new = store.get_source(store.save_view_as_source(v["view_id"], "copy", build_fts=False)["source"]["id"])
    assert [col["name"] for col in new["columns"]] == [
        "From table", "Time", "EventId", "Host", "Msg", "Extra", "Other"]
    rows = [tuple(r) for r in store.db.execute(
        f'SELECT "From table", "Time", "EventId", "Extra", "Other" FROM "{new["table_name"]}" ORDER BY rid')]
    assert rows == [("a.csv", "2024-01-01 10:00:00", "4624", "", ""),
                    ("a.csv", "2024-01-01 11:00:00", "4688", "", ""),
                    ("b.csv", "2024-01-02 10:00:00", "4624", "x", ""),
                    ("b.csv", "2024-01-02 11:00:00", "4625", "y", ""),
                    ("b.csv", "2024-01-02 12:00:00", "4624", "x", ""),
                    ("c.csv", "2024-01-03 09:00:00", "", "", "needle")]


def test_deriving_from_a_column_only_some_tables_have_is_refused_up_front(store, abc):
    """add_derived_column on a merge makes the column on every member, so
    it refuses one a member lacks; the previews before it say so too,
    rather than "no such column" or a preview of a column it won't make."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, filters=[{"column": "Msg", "op": "contains", "value": "o"}])
    for attempt in (lambda: store.detect_timestamp_format(mid, "Extra"),
                    lambda: store.preview_derived(mid, "Extra", "iso8601"),
                    lambda: store.preview_derived(mid, "Extra", "iso8601", view_id=v["view_id"]),
                    lambda: store.add_derived_column(mid, "When", "Extra", "iso8601")):
        with pytest.raises(ValueError, match="has no column 'Extra'"):
            attempt()
    # One every table has (B spells it "TIME") previews from every table
    # in the view: "delta failed" is the one row the filter leaves out.
    res = store.preview_derived(mid, "Time", "iso8601", view_id=v["view_id"])
    assert (res["scope"], res["failures"]) == ("view", 0)
    assert [p["input"] for p in res["preview"]] == [
        "2024-01-01 10:00:00", "2024-01-01 11:00:00", "2024-01-02 10:00:00",
        "2024-01-02 12:00:00", "2024-01-03 09:00:00"]


# ---------------------------------------------------------- source column

def test_source_column_names_each_row_table(store, abc):
    a, b, c = abc
    store.set_source_nickname(b, "WS02 Security")
    m = store.create_merge("m", [a, b, c], source_column=True)
    mid = m["id"]
    assert m["columns"][0] == {"name": "From table", "type": "text", "source_column": True}
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    assert _cells(store, mid, v, "From table") == ["a.csv", "a.csv"] + ["WS02 Security"] * 3 + ["c.csv"]
    # A real column: filter, group, value list.
    assert _view(store, mid, filters=[{"column": "From table", "op": "equals",
                                       "value": "WS02 Security"}])["row_count"] == 3
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])   # a new root view replaces the last
    groups = {g["value"]: g["count"] for g in store.group_summary(v["view_id"], "From table")["groups"]}
    assert groups == {"a.csv": 2, "WS02 Security": 3, "c.csv": 1}
    assert {r["value"] for r in store.column_values(mid, "From table")} == {"a.csv", "WS02 Security", "c.csv"}
    body = "".join(store.export_view_csv(v["view_id"]))
    assert list(csv.reader(io.StringIO(body)))[1][4] == "a.csv"


def test_source_column_steps_aside_for_a_real_column_of_that_name(store, write_csv):
    a = _ingest(store, write_csv, [["From table", "Msg"], ["evtx", "1"]], "a")
    b = _ingest(store, write_csv, [["Msg"], ["2"]], "b")
    m = store.create_merge("m", [a, b], source_column=True)
    assert [c["name"] for c in m["columns"]] == ["From table 2", "From table", "Msg"]


def test_source_column_on_same_shaped_tables(store, write_csv):
    a = _ingest(store, write_csv, A_ROWS, "a")
    a2 = _ingest(store, write_csv, A_ROWS, "a2")
    mid = store.create_merge("m", [a, a2], source_column=True)["id"]
    v = _view(store, mid)
    assert sorted(set(_cells(store, mid, v, "From table"))) == ["a.csv", "a2.csv"]


# ----------------------------------------------------------------- editing

def test_edit_merge_keeps_its_id_and_layout(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b])["id"]
    store.save_layout(mid, {"order": ["Msg"], "columns": {}})
    old = _view(store, mid)
    out = store.update_merge(-mid, source_ids=[a, b, c], col_mode="all", source_column=True)
    assert out["id"] == mid and out["row_count"] == 6
    assert out["columns"][0]["name"] == "From table"
    assert store.get_layout(mid)["order"] == ["Msg"]
    with pytest.raises(KeyError):
        store.fetch_rows(old["view_id"], 0, 10)   # drawn from the old member list
    assert _view(store, mid)["row_count"] == 6


def test_edit_merge_checks_like_create(store, abc):
    a, b, c = abc
    store.create_merge("taken", [a, b])
    mid = store.create_merge("m", [a, c])["id"]
    with pytest.raises(ValueError, match="already exists"):
        store.update_merge(-mid, name="TAKEN")
    with pytest.raises(ValueError, match="at least 2"):
        store.update_merge(-mid, source_ids=[a])
    assert store.update_merge(-mid, name="renamed")["name"] == "renamed"


def test_edit_merge_route(client, store, abc):
    a, b, c = abc
    mid = client.post("/api/merges", json={"name": "m", "source_ids": [a, b]}).json()["id"]
    r = client.put(f"/api/merges/{-mid}", json={"source_ids": [a, b, c], "col_mode": "shared"})
    assert r.status_code == 200, r.text
    assert [col["name"] for col in r.json()["columns"]] == ["Time", "Msg"]
    assert client.put("/api/merges/999", json={"name": "x"}).status_code == 404
    assert client.put(f"/api/merges/{-mid}", json={"source_ids": [a]}).status_code == 400


# ---------------------------------------------------- same-shaped merges

def test_same_shaped_merge_sql_is_unchanged(store, write_csv):
    """A merge of identical headers needs no projection: its SQL stays the
    plain member tables it always was."""
    a = _ingest(store, write_csv, A_ROWS, "a")
    a2 = _ingest(store, write_csv, A_ROWS, "a2")
    mid = store.create_merge("m", [a, a2])["id"]
    sql = store.spec_sql(mid, {"source_id": mid, "filters": [], "sort": []})
    assert f'FROM "src_{a}"' in sql and f'FROM "src_{a2}"' in sql
    assert "SELECT rid," not in sql


# ----------------------------------------------------------------- preview

def test_preview_matches_the_merge_it_previews(store, abc):
    a, b, c = abc
    store.set_source_nickname(c, "Other tool")
    pv = store.merge_preview([a, b, c], source_column=True)
    made = store.create_merge("m", [a, b, c], source_column=True)
    assert [col["name"] for col in pv["columns"]] == [col["name"] for col in made["columns"]]
    ev = next(col for col in pv["columns"] if col["name"] == "EventId")
    assert ev["in"] == {str(a): {"name": "EventId", "type": "number"}, str(b): {"name": "EventID", "type": "number"}}
    assert [m["label"] for m in pv["members"]] == ["a.csv", "b.csv", "Other tool"]
    # 12 rows split 4 per table, capped by what each has; padding as in the grid
    assert [r["source_id"] for r in pv["rows"]] == [a, a, b, b, b, c]
    c_row = pv["rows"][-1]["cells"]
    names = [col["name"] for col in pv["columns"]]
    assert c_row[names.index("From table")] == "Other tool"
    assert c_row[names.index("EventId")] is None and c_row[names.index("Other")] == "needle"


def test_preview_of_shared_with_nothing_in_common_is_empty_not_an_error(store, write_csv, abc):
    a, _, _ = abc
    d = _ingest(store, write_csv, [["Unrelated"], ["1"]], "d")
    pv = store.merge_preview([a, d], col_mode="shared")
    assert pv["columns"] == [] and len(pv["rows"]) == 3
    with pytest.raises(ValueError):
        store.merge_preview([a, -1])


def test_preview_is_a_read_and_never_waits_on_the_writer(store, abc):
    """The builder re-asks for the preview on every tick, so it must not
    queue behind an import or a big view build (CLAUDE.md invariant #4)."""
    from test_concurrency import _run_with_lock_held
    a, b, c = abc
    pv = _run_with_lock_held(store, lambda: store.merge_preview([a, b, c], sample_rows=6))
    assert [r["source_id"] for r in pv["rows"]] == [a, a, b, b, c]


def test_preview_route(client, store, abc):
    a, b, _ = abc
    r = client.post("/api/merges/preview", json={"source_ids": [a, b], "col_mode": "shared"})
    assert r.status_code == 200, r.text
    assert [col["name"] for col in r.json()["columns"]] == ["Time", "EventId", "Host", "Msg"]
    assert client.post("/api/merges/preview", json={"source_ids": [a, 999]}).status_code == 400

"""A derived column only some of a merge's tables have.

The report: a column derived on a table was missing from every merge the
table was in. A merge showed a derived column only when every member had
one of that name, whatever its column mode, while a FILE column only some
members have has been shown since mismatched merges (empty on the others,
test_merge_mismatched.py). The "every member" rule dated from before that,
when nothing could pad a member's rows with NULL; _member_projection does
that now, for derived columns as for file ones.

So now "Keep every column" shows a derived column any member has, and
"Only shared columns" one every member has. These tests run such a column
through each read path and each edit made on the merge, against answers
worked out by hand. The first member deliberately lacks the column, so
nothing can lean on members[0] carrying it."""

from __future__ import annotations

import csv
import io

import pytest

A_ROWS = [["Time", "Host", "Msg"],
          ["2024-01-01 10:00:00", "WS01", "u=alice logon"],
          ["2024-01-01 11:00:00", "WS01", "u=bob process"]]
B_ROWS = [["Time", "Host", "Msg"],
          ["2024-01-02 10:00:00", "WS02", "u=carol logon"],
          ["2024-01-02 11:00:00", "WS02", "u=alice failed"],
          ["2024-01-02 12:00:00", "WS02", "u=dave logon"]]
C_ROWS = [["Time", "Msg", "Epoch"],
          ["2024-01-03 09:00:00", "u=erin note", "1704445200"]]   # 2024-01-05 09:00:00 UTC

RX = {"pattern": r"u=(\w+)"}


def _wait(store, res):
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)
    return res


@pytest.fixture
def abc(store, write_csv):
    """a has no derived column; b derives User; c derives Seen (a datetime
    column) from a file column only c has."""
    ids = []
    for rows, name in ((A_ROWS, "a"), (B_ROWS, "b"), (C_ROWS, "c")):
        ids.append(store.ingest_csv(write_csv(rows, f"{name}.csv"), name=f"{name}.csv")["id"])
    a, b, c = ids
    _wait(store, store.add_derived_column(b, "User", "Msg", "regex_extract", RX))
    _wait(store, store.add_derived_column(c, "Seen", "Epoch", "unix_epoch", {}))
    return a, b, c


def _names(store, sid):
    return [c["name"] for c in store.get_source(sid)["columns"]]


def _col(store, sid, name):
    return next(c for c in store.get_source(sid)["columns"] if c["name"] == name)


def _view(store, mid, **spec):
    spec.setdefault("filters", [])
    spec.setdefault("sort", [])
    return store.build_view(mid, {"source_id": mid, **spec})


def _cells(store, mid, view, column):
    i = _names(store, mid).index(column)
    return [r["cells"][i] for r in store.fetch_rows(view["view_id"], 0, 1000)["rows"]]


def _by_time(store, mid, column, **spec):
    return _cells(store, mid, _view(store, mid, sort=[{"column": "Time", "dir": "asc"}], **spec), column)


# ------------------------------------------------------------ the column list

def test_keep_every_column_shows_a_derived_column_any_table_has(store, abc):
    a, b, c = abc
    m = store.create_merge("m", [a, b, c])
    assert [col["name"] for col in m["columns"]] == ["Time", "Host", "Msg", "Epoch", "User", "Seen"]
    user, seen = _col(store, m["id"], "User"), _col(store, m["id"], "Seen")
    assert user["derived"] and seen["derived"]
    # Each carries the definition of the one table that has it, typed as
    # that table types it.
    assert user["derived_id"] == _col(store, b, "User")["derived_id"]
    assert seen["derived_id"] == _col(store, c, "Seen")["derived_id"]
    assert (user["type"], seen["type"]) == ("text", "datetime")
    assert (user["derived_status"], user["parse_failures"]) == ("ready", 0)
    # The grid's "only in some tables" marker names the tables without it.
    missing = {mem["id"]: mem["missing"] for mem in m["members"]}
    assert missing == {a: ["Epoch", "User", "Seen"], b: ["Epoch", "Seen"], c: ["Host", "User"]}
    assert [d["name"] for d in store.list_derived_columns(m["id"])] == ["User", "Seen"]


def test_only_shared_columns_keeps_only_a_derived_column_every_table_has(store, abc):
    a, b, c = abc
    _wait(store, store.add_derived_column(a, "User", "Msg", "regex_extract", RX))
    m = store.create_merge("m", [a, b], col_mode="shared")
    assert [col["name"] for col in m["columns"]] == ["Time", "Host", "Msg", "User"]
    m2 = store.create_merge("m2", [a, b, c], col_mode="shared")
    assert [col["name"] for col in m2["columns"]] == ["Time", "Msg"]
    assert store.list_derived_columns(m2["id"]) == []


def test_a_column_derived_after_the_merge_appears_in_it(store, abc):
    """The merge's columns are worked out live, so deriving on a member
    later shows up in the merge without editing it."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b])["id"]
    assert "Word" not in _names(store, mid)
    _wait(store, store.add_derived_column(a, "Word", "Msg", "regex_extract", {"pattern": r" (\w+)$"}))
    assert _by_time(store, mid, "Word") == ["logon", "process", None, None, None]


def test_the_merge_builder_preview_shows_it(store, abc):
    a, b, c = abc
    pre = store.merge_preview([a, b, c])
    names = [col["name"] for col in pre["columns"]]
    assert names == ["Time", "Host", "Msg", "Epoch", "User", "Seen"]
    user = next(col for col in pre["columns"] if col["name"] == "User")
    assert list(user["in"]) == [str(b)]
    i = names.index("User")
    assert [(r["source_id"], r["cells"][i]) for r in pre["rows"]] == [
        (a, None), (a, None), (b, "carol"), (b, "alice"), (b, "dave"), (c, None)]


# ---------------------------------------------------------------- reading it

def test_tables_without_it_read_it_as_empty(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    assert _by_time(store, mid, "User") == [None, None, "carol", "alice", "dave", None]
    assert _by_time(store, mid, "Seen") == [None] * 5 + ["2024-01-05 09:00:00"]
    # Same through every column-mode and From-table combination.
    store.update_merge(-mid, source_column=True)
    assert _by_time(store, mid, "User") == [None, None, "carol", "alice", "dave", None]


@pytest.mark.parametrize("flt, expected", [
    ({"column": "User", "op": "equals", "value": "alice"}, 1),
    ({"column": "User", "op": "not_empty", "value": ""}, 3),
    ({"column": "User", "op": "empty", "value": ""}, 3),
    ({"column": "User", "op": "in", "value": ["alice", ""]}, 4),
    ({"column": "User", "op": "contains", "value": "ar"}, 1),
    ({"column": "Seen", "op": "not_empty", "value": ""}, 1),
])
def test_filters(store, abc, flt, expected):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    assert _view(store, mid, filters=[flt])["row_count"] == expected


def test_filter_tree_and_raw_fragment(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    tree = {"type": "cond", "column": "User", "op": "equals", "value": "dave"}
    assert _view(store, mid, filter_tree=tree)["row_count"] == 1
    store.validate_where_fragment(mid, "\"User\" = 'alice'")
    raw = {"type": "raw", "sql": "\"User\" = 'alice' OR \"Seen\" IS NOT NULL"}
    assert _view(store, mid, filter_tree=raw)["row_count"] == 2


def test_sort_and_search(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "User", "dir": "desc"}, {"column": "Time", "dir": "asc"}])
    assert _cells(store, mid, v, "User")[:3] == ["dave", "carol", "alice"]
    v = _view(store, mid, sort=[{"column": "Seen", "dir": "desc"}])
    assert _cells(store, mid, v, "Seen")[0] == "2024-01-05 09:00:00"
    # Search still reads the members' file columns, as on a plain table.
    assert _view(store, mid, search="dave")["row_count"] == 1


def test_timeframe_and_histogram_on_a_datetime_column_one_table_has(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    tr = {"enabled": True, "column": "Seen", "start": "2024-01-05 00:00:00", "end": "2024-01-05 23:59:59"}
    v = _view(store, mid, time_range=tr)
    # c by its Seen; a and b lack Seen, so their own Time judges them
    # (the timeframe's documented fallback) — and none of theirs is the 5th.
    assert v["row_count"] == 1
    assert store.time_histogram(_view(store, mid)["view_id"], "Seen")["total"] == 1


def test_grouping_and_its_empty_group(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid)
    assert {g["value"]: g["count"] for g in store.group_summary(v["view_id"], "User")["groups"]} == {
        "alice": 1, "carol": 1, "dave": 1, None: 3}
    assert store.expand_group(v["view_id"], "User", None)["row_count"] == 3
    assert store.expand_group(v["view_id"], "User", "dave")["row_count"] == 1
    path = [{"column": "Host", "value": "WS02"}]
    assert {g["value"]: g["count"] for g in store.group_summary(v["view_id"], "User", path=path)["groups"]} == {
        "alice": 1, "carol": 1, "dave": 1}
    # The view-join shape: a filtered parent.
    v2 = _view(store, mid, filters=[{"column": "Msg", "op": "contains", "value": "logon"}])
    assert {g["value"]: g["count"] for g in store.group_summary(v2["view_id"], "User")["groups"]} == {
        "carol": 1, "dave": 1, None: 1}
    assert store.expand_group(v2["view_id"], "User", None)["row_count"] == 1
    assert store.expand_group(v2["view_id"], "User", "carol")["row_count"] == 1


def test_value_list_and_widths(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    assert {r["value"]: r["count"] for r in store.column_values(mid, "User")} == {
        None: 3, "alice": 1, "carol": 1, "dave": 1}
    widths = store.column_max_lengths(mid)
    assert widths["User"] == len("carol")


def test_tagging_the_view_and_finding_rows(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    tag = store.list_tags()[0]["id"]
    v = _view(store, mid, filters=[{"column": "User", "op": "not_empty", "value": ""}])
    assert store.tag_view(v["view_id"], tag, True)["changed"] == 3
    assert {(r["source_id"], r["rid"]) for r in store.db.execute(
        "SELECT source_id, rid FROM row_tags WHERE tag_id=?", (tag,))} == {(b, 1), (b, 2), (b, 3)}
    v = _view(store, mid, sort=[{"column": "User", "dir": "asc"}])
    assert store.find_position(v["view_id"], b, 3) is not None


# ------------------------------------------------------- export and SQL

def test_csv_export(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    rows = list(csv.reader(io.StringIO("".join(store.export_view_csv(v["view_id"])))))
    # A derived column is placed after its input, as on screen.
    assert rows[0][4:] == ["Time", "Host", "Msg", "User", "Epoch", "Seen"]
    assert [r[7] for r in rows[1:]] == ["", "", "carol", "alice", "dave", ""]
    assert rows[6][9] == "2024-01-05 09:00:00"


def test_sql_pane(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    res = store.run_sql(f'SELECT "User", "Seen" FROM merge_{-mid} ORDER BY "Time"')
    assert res["rows"] == [[None, None], [None, None], ["carol", None], ["alice", None],
                           ["dave", None], [None, "2024-01-05 09:00:00"]]
    sql = store.spec_sql(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "User", "op": "equals", "value": "alice"}]})
    assert len(store.run_sql(sql)["rows"]) == 1


def test_save_view_as_table(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    v = _view(store, mid, sort=[{"column": "Time", "dir": "asc"}])
    new = store.get_source(store.save_view_as_source(v["view_id"], "copy", build_fts=False)["source"]["id"])
    assert "User" in [col["name"] for col in new["columns"]]
    got = [r[0] for r in store.db.execute(f'SELECT "User" FROM "{new["table_name"]}" ORDER BY rid')]
    assert got == ["", "", "carol", "alice", "dave", ""]


# ------------------------------------------------------- editing it on the merge

def test_derive_on_the_merge_from_it(store, abc):
    """A chain: a column made on the merge from one only b has. a and c
    read it as empty (PR #582's rule for an input some tables lack)."""
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    res = _wait(store, store.add_derived_column(mid, "Initial", "User", "regex_extract", {"pattern": r"^(\w)"}))
    assert [d["source_id"] for d in res["member_definitions"]] == [a, b, c]
    assert _by_time(store, mid, "Initial") == [None, None, "c", "a", "d", None]


def test_a_new_column_on_the_merge_may_not_take_its_name(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    with pytest.raises(ValueError, match="already has a column called 'User'"):
        store.add_derived_column(mid, "User", "Msg", "regex_extract", RX)
    assert _names(store, a)[-1] == "Msg"


def test_rederive_rename_and_remove_on_the_merge_act_on_the_table_that_has_it(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    did = _col(store, mid, "User")["derived_id"]

    _wait(store, store.rederive_column(did, {"pattern": r"u=(\w{3})"}, source_id=mid))
    assert _by_time(store, mid, "User") == [None, None, "car", "ali", "dav", None]

    store.rename_derived_column(did, "Account", source_id=mid)
    assert "Account" in _names(store, mid) and "Account" in _names(store, b)
    # a and c lack it, and the new name is checked against them too.
    with pytest.raises(ValueError, match="Member table 'c.csv' already has a column called 'Epoch'"):
        store.rename_derived_column(did, "Epoch", source_id=mid)

    store.remove_derived_column(did, source_id=mid)
    assert "Account" not in _names(store, mid)
    assert "Account" not in _names(store, b)


def test_unparsed_rows_filter_on_the_merge(store, write_csv, abc):
    """"Show N unparsed rows" on the merge matches the N rows of the tables
    that have the column — which a filter can only promise when no table
    without the column has its input: on such a table the column reads
    empty beside an input with something in it, which is exactly what an
    unparsed row looks like, and nothing in a filter tells the tables
    apart. There it says which table to open rather than show them."""
    a, b, c = abc
    d = store.ingest_csv(write_csv([["Time", "Msg", "Epoch"], ["2024-01-04 00:00:00", "x", "not a time"]],
                                   "d.csv"), name="d.csv")["id"]
    _wait(store, store.add_derived_column(d, "Seen", "Epoch", "unix_epoch", {}))
    mid = store.create_merge("m", [a, c, d])["id"]
    seen = _col(store, mid, "Seen")
    assert seen["parse_failures"] == 1
    # Only c and d have Epoch, and both have Seen: exact.
    frag = store.unparsed_where_fragment(seen["derived_id"], mid)
    v = _view(store, mid, filter_tree={"type": "raw", "sql": frag})
    assert v["row_count"] == 1
    assert _cells(store, mid, v, "Msg") == ["x"]

    # b has User, a has its input Msg and no User: refused, naming b.
    mid2 = store.create_merge("m2", [a, b])["id"]
    user = _col(store, mid2, "User")
    with pytest.raises(ValueError, match=r"'User' is in only some of this merge's tables.*Open b\.csv"):
        store.unparsed_where_fragment(user["derived_id"], mid2)
    # On b itself it is the plain fragment, as ever.
    assert store.unparsed_where_fragment(user["derived_id"], b)


def test_unparsed_rows_route_says_which_table_to_open(client, store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b])["id"]
    did = _col(store, mid, "User")["derived_id"]
    r = client.get(f"/api/derived/{did}/unparsed_filter", params={"source_id": mid})
    assert r.status_code == 400 and "Open b.csv" in r.json()["detail"]


def test_a_filter_builds_no_index_on_a_table_without_the_column(store, abc):
    a, b, c = abc
    mid = store.create_merge("m", [a, b, c])["id"]
    _view(store, mid, filters=[{"column": "User", "op": "equals", "value": "alice"}])
    assert store.wait_for_column_index(b, "User", timeout=10)
    assert "User" not in [i["column"] for i in store.list_column_indexes(a)]
    assert "User" not in [i["column"] for i in store.list_column_indexes(c)]

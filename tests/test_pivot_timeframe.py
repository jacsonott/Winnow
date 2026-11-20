"""The Pivot's timeframe, against the grid's.

Each pivot can count only the rows from a stretch of time (the Filters
well's ⏱ Timeframe). It compiles that range itself (`_time_clauses` in
examples/plugins/pivot) rather than through the store, because the pivot
reads the table, not a view — so the two can drift apart. They must not:
"Show these rows in the grid" hands the pivot's range to the tables'
timeframe, and a range the two read differently opens other rows than the
cell counted, under a chip that looks right.

So every case here is counted twice, through the pivot and through
`Store.build_view`'s `time_range`, and the two have to agree. The table
has what makes them disagree when they do: two datetime columns (one of
them timestomped on a row), the US shape, a 'T' separator with a zone, a
row with no timestamp, a bound exactly on a value, and bounds typed in
another spelling than the values. The merge cases cover a member without
the chosen column and one with no datetime column at all (invariant 9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [["Host", "Created", "Modified", "Note"],
        ["SRV1", "2026-03-13 01:00:00", "2026-03-13 01:00:00", "a"],    # 1: before
        ["SRV1", "2026-03-13 02:00:00", "2026-03-13 02:00:00", "b"],    # 2: in
        ["SRV2", "2020-01-01 00:00:00", "2026-03-13 02:30:00", "c"],    # 3: Created stomped, Modified in
        ["SRV2", "3/13/2026 2:45 AM", "3/13/2026 2:45 AM", "d"],        # 4: US shape, in
        ["SRV3", "2026-03-13T03:00:00Z", "2026-03-13 05:00:00", "e"],   # 5: Created on the End bound
        ["SRV3", "", "", "f"],                                          # 6: no timestamp at all
        ["SRV3", "2026-03-14 09:00:00", "2026-03-14 09:00:00", "g"]]    # 7: after

# Bounds typed in other spellings than the values: a date plus HH:MM, and
# the US shape with a meridiem. Both mean 2026-03-13 01:30:00 / 03:00:00.
START, END = "2026-03-13 01:30", "3/13/2026 3:00 AM"

CASES = [
    # (column, start, end, rids the pivot and the grid must both keep)
    (None, START, END, [2, 3, 4, 5]),            # any datetime column: 3 by its Modified
    ("Created", START, END, [2, 4, 5]),          # 5 sits on the End bound — both ends included
    ("Modified", START, END, [2, 3, 4]),
    (None, "2026-03-13 02:30:00", "", [3, 4, 5, 7]),   # open end
    (None, "", "2026-03-13 01:59", [1, 3]),             # open start: 3's stomped Created is "in"
    ("Created", "2026-03-14", "", [7]),                 # a date alone is its midnight
]


@pytest.fixture
def example_registry() -> PluginRegistry:
    # Function-scoped: a reload mutates the registry in place (see
    # test_plugins.py's fixture of the same name).
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


@pytest.fixture
def pivot(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    sid = store.ingest_csv(write_csv(ROWS, "times.csv"), name="times", build_fts=False)["id"]
    types = {c["name"]: c["type"] for c in store.get_source(sid)["columns"]}
    assert types == {"Host": "text", "Created": "datetime", "Modified": "datetime", "Note": "text"}
    return client, sid


def _post(client, route, **body):
    return client.post(f"/api/plugin/pivot/{route}", json=body)


def _count(client, source_id, **body):
    r = _post(client, "aggregate", source_id=source_id, values=[{"agg": "count"}], group_sets=[[]], **body)
    assert r.status_code == 200, r.text
    return r.json()["sets"][0]["rows"][0][0]


def _frame(column, start, end):
    return {"column": column, "start": start, "end": end}


def _grid(store, source_id, column, start, end, **spec):
    """The tables' timeframe as the grid compiles it — the range "Show
    these rows in the grid" hands over."""
    tr = {"enabled": True, "column": column, "start": start, "end": end}
    return store.build_view(source_id, {"time_range": tr, **spec})["row_count"]


@pytest.mark.parametrize("column, start, end, rids", CASES)
def test_the_timeframe_counts_the_rows_in_range(pivot, column, start, end, rids):
    client, sid = pivot
    assert _count(client, sid, timeframe=_frame(column, start, end)) == len(rids)


@pytest.mark.parametrize("column, start, end, rids", CASES)
def test_the_timeframe_keeps_the_rows_the_grid_keeps(pivot, store, column, start, end, rids):
    client, sid = pivot
    assert _count(client, sid, timeframe=_frame(column, start, end)) == _grid(store, sid, column, start, end)


@pytest.mark.parametrize("column, start, end, rids", CASES)
def test_the_rows_behind_a_cell_are_the_ones_in_range(pivot, column, start, end, rids):
    """Excel's Show Details: the drill-down is the cell's rows, so the
    timeframe it was counted under comes with it."""
    client, sid = pivot
    r = _post(client, "detail", source_id=sid, cell=[], timeframe=_frame(column, start, end))
    assert r.status_code == 200, r.text
    assert [row[0] for row in r.json()["rows"]] == rids


def test_the_timeframe_narrows_every_level(pivot):
    client, sid = pivot
    r = _post(client, "aggregate", source_id=sid, values=[{"agg": "count"}], group_sets=[["Host"], []],
              timeframe=_frame(None, START, END))
    assert r.status_code == 200, r.text
    per_host, total = r.json()["sets"]
    assert {row[0]: row[1] for row in per_host["rows"]} == {"SRV1": 1, "SRV2": 2, "SRV3": 1}
    assert total["rows"] == [[4]]


def test_the_timeframe_combines_with_filters_and_tags(pivot, store):
    """The range goes in after the field filters' bound values, beside the
    tag clauses; it carries no placeholders of its own, or every bound
    value after it would shift a slot. And the grid, given the same three,
    keeps the same rows."""
    client, sid = pivot
    suspicious = next(t["id"] for t in store.list_tags() if t["name"] == "Suspicious")
    store.set_tags(sid, [2, 3, 7], suspicious, True)
    body = dict(filters=[{"column": "Note", "op": "in", "values": ["b", "c", "d", "g"]}],
                tags=[suspicious], timeframe=_frame(None, START, END))
    assert _count(client, sid, **body) == 2   # 2 and 3: tagged, listed and in range; 7 is out of it
    tree = {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "Note", "op": "in", "value": ["b", "c", "d", "g"]}]}
    assert _grid(store, sid, None, START, END, filter_tree=tree, tags=[suspicious]) == 2


def test_an_empty_timeframe_filters_nothing(pivot):
    client, sid = pivot
    assert _count(client, sid, timeframe=_frame(None, "", "")) == 7
    assert _count(client, sid, timeframe=None) == 7


@pytest.mark.parametrize("frame, fragment", [
    (_frame("Note", START, END), "not one of"),
    (_frame("Nope", START, END), "No column"),
    (_frame(None, "yesterday", END), "Start: 'yesterday' is not a recognized timestamp"),
    (_frame(None, START, "soon"), "End: 'soon' is not a recognized timestamp"),
    ("2026-03-13", "timeframe must be"),
])
def test_a_timeframe_the_table_cannot_honour_is_refused(pivot, frame, fragment):
    """By name, and as a 400: a bound dropped or a column quietly swapped
    for another would count rows nobody asked about."""
    client, sid = pivot
    r = _post(client, "aggregate", source_id=sid, values=[{"agg": "count"}], group_sets=[[]], timeframe=frame)
    assert r.status_code == 400 and fragment in r.json()["detail"], r.text


def test_a_table_with_no_datetime_column_refuses_a_timeframe(pivot, store, write_csv):
    """The grid's timeframe hides nothing on a table like this; a pivot that
    did the same would put the whole count under a chip that says it is
    narrowed."""
    client, _ = pivot
    sid = store.ingest_csv(write_csv([["Host", "Note"], ["SRV1", "a"]], "notime.csv"),
                           name="notime", build_fts=False)["id"]
    r = _post(client, "aggregate", source_id=sid, values=[{"agg": "count"}], group_sets=[[]],
              timeframe=_frame(None, START, END))
    assert r.status_code == 400 and "has no datetime columns" in r.json()["detail"], r.text


@pytest.fixture
def merged(pivot, store, write_csv):
    """A merge of three: the table above, one without Modified, and one
    with no datetime column at all."""
    client, sid = pivot
    created_only = store.ingest_csv(write_csv([["Host", "Created", "Note"],
                                               ["SRV4", "2026-03-13 02:10:00", "h"],
                                               ["SRV4", "2026-03-15 00:00:00", "i"]], "created.csv"),
                                    name="created", build_fts=False)["id"]
    untimed = store.ingest_csv(write_csv([["Host", "Note"], ["SRV5", "j"], ["SRV5", "k"]], "untimed.csv"),
                               name="untimed", build_fts=False)["id"]
    mid = store.create_merge("times-merge", [sid, created_only, untimed])["id"]
    return client, mid


@pytest.mark.parametrize("column, start, end, rids", CASES)
def test_on_a_merge_each_member_reads_the_range_as_the_grid_does(merged, store, column, start, end, rids):
    """Invariant 9, and the grid's own rule for a merge: the chosen column
    where a member has it, every datetime column it has where it doesn't,
    and a member with no time at all left whole. The union pads a missing
    column with NULL, so reading it as the union shows it would drop the
    second member's rows wherever Modified was picked."""
    client, mid = merged
    assert _count(client, mid, timeframe=_frame(column, start, end)) == _grid(store, mid, column, start, end)


def test_a_merge_member_without_the_column_is_read_on_its_own(merged):
    client, mid = merged
    # Modified: rows 2, 3, 4 of the first table; the second member has no
    # Modified, so its Created decides (h in, i out); the third has no time
    # and is not narrowed (j, k).
    assert _count(client, mid, timeframe=_frame("Modified", START, END)) == 3 + 1 + 2


def test_the_rows_behind_a_merged_cell_keep_the_timeframe(merged):
    client, mid = merged
    r = _post(client, "detail", source_id=mid, cell=[{"column": "Host", "value": "SRV4"}],
              timeframe=_frame(None, START, END))
    assert r.status_code == 200, r.text
    assert [row[-1] for row in r.json()["rows"]] == ["h"]


@pytest.mark.parametrize("body, expected", [
    ({"start": "3/13/2026 2:45 AM", "end": "2026-03-14"}, {"start": "2026-03-13 02:45:00", "end": "2026-03-14 00:00:00"}),
    ({"start": "2026-03-13T03:00:00Z", "end": ""}, {"start": "2026-03-13 03:00:00", "end": None}),
    ({"end": "2026-03-13 01:59"}, {"start": None, "end": "2026-03-13 01:59:00"}),
])
def test_the_dialog_reads_a_range_into_the_shape_it_is_compared_in(pivot, body, expected):
    """What Apply keeps, shows on the chip and hands to the grid: each
    bound as TS_NORMALIZE writes it."""
    client, _ = pivot
    r = _post(client, "timeframe", **body)
    assert r.status_code == 200, r.text
    assert r.json() == expected


@pytest.mark.parametrize("body, fragment", [
    ({"start": "", "end": ""}, "Set a start and/or end"),
    ({"start": "last tuesday"}, "Start: 'last tuesday' is not a recognized timestamp"),
    ({"start": "2026-03-14", "end": "2026-03-13 23:00"}, "is before Start"),
])
def test_the_dialog_refuses_a_range_it_cannot_use(pivot, body, fragment):
    """Refused while the dialog is still open. A range with its ends the
    wrong way round matches nothing — a confident zero."""
    client, _ = pivot
    r = _post(client, "timeframe", **body)
    assert r.status_code == 400 and fragment in r.json()["detail"], r.text

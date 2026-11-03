"""First/Last's Filters, operator by operator, against the grid's filter.

First/Last compiles its own WHERE (`_where` in examples/plugins/first_last)
rather than going through the store's filter compiler, so the two can drift
apart: an operator that treats a NULL, an empty cell or a non-number
differently in one of them makes the tab's groups count rows the grid,
given the same condition, doesn't show. Before this only `in` and
`not_in` had run.

Each operator is applied in First/Last, grouped by Host with `{count}` as
the description, and the grid's equivalent condition is applied to the
same table; every group's count has to match the grid's rows for that
host. The table has both kinds of nothing — '' from the CSV and NULL,
written in directly — and a number column with a blank and a non-number in
it, the cells where compilers disagree. The same pairs run on a merge of
two tables holding those rows (invariant 9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [["When", "Host", "Note", "Bytes"],
        ["2026-03-14 08:00:00", "SRV1", "ok", "100"],
        ["2026-03-14 08:01:00", "SRV1", "late", "200"],
        ["2026-03-14 08:02:00", "SRV1", "", "40"],
        ["2026-03-14 08:03:00", "SRV2", "ok", ""],
        ["2026-03-14 08:04:00", "SRV2", "x", "70"],
        ["2026-03-14 08:05:00", "SRV2", "x_1", "n/a"],
        ["2026-03-14 08:06:00", "SRV2", "", "90"],
        ["2026-03-14 08:07:00", "SRV3", "NULL", "300"],
        ["2026-03-14 08:08:00", "SRV3", "NULL", "-10"],
        ["2026-03-14 08:09:00", "SRV3", "50%", "150.5"]]
NULL_NOTES = (8, 9)    # rids whose Note is written to a real NULL

# (First/Last filter, the grid conditions that mean the same rows)
FL_TO_GRID = [
    ({"op": "in", "values": ["ok"], "column": "Note"}, [{"op": "in", "value": ["ok"], "column": "Note"}]),
    ({"op": "in", "values": ["ok", "x"], "column": "Note"}, [{"op": "in", "value": ["ok", "x"], "column": "Note"}]),
    ({"op": "not_in", "values": ["ok"], "column": "Note"}, [{"op": "not_equals", "value": "ok", "column": "Note"}]),
    ({"op": "contains", "value": "x", "column": "Note"}, [{"op": "contains", "value": "x", "column": "Note"}]),
    # LIKE's own wildcards, meant literally.
    ({"op": "contains", "value": "x_", "column": "Note"}, [{"op": "contains", "value": "x_", "column": "Note"}]),
    ({"op": "contains", "value": "0%", "column": "Note"}, [{"op": "contains", "value": "0%", "column": "Note"}]),
    ({"op": "not_contains", "value": "x", "column": "Note"},
     [{"op": "not_contains", "value": "x", "column": "Note"}]),
    ({"op": "starts", "value": "la", "column": "Note"}, [{"op": "starts", "value": "la", "column": "Note"}]),
    ({"op": "gt", "value": "60", "column": "Bytes"}, [{"op": ">", "value": "60", "column": "Bytes"}]),
    ({"op": "gt", "value": "150", "column": "Bytes"}, [{"op": ">", "value": "150", "column": "Bytes"}]),
    ({"op": "lt", "value": "100", "column": "Bytes"}, [{"op": "<", "value": "100", "column": "Bytes"}]),
    ({"op": "lt", "value": "0", "column": "Bytes"}, [{"op": "<", "value": "0", "column": "Bytes"}]),
    ({"op": "empty", "column": "Note"}, [{"op": "empty", "column": "Note"}]),
    ({"op": "not_empty", "column": "Note"}, [{"op": "not_empty", "column": "Note"}]),
    ({"op": "empty", "column": "Bytes"}, [{"op": "empty", "column": "Bytes"}]),
]


@pytest.fixture(scope="module")
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


def _ingest(store, write_csv, rows, name):
    sid = store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False)["id"]
    first = int(rows[1][0][-5:-3])     # the minute each file's rows start at, which is its rid offset
    nulls = [rid - first for rid in NULL_NOTES if 0 < rid - first <= len(rows) - 1]
    if nulls:
        with store.lock:
            store.db.execute(f'UPDATE src_{sid} SET "Note" = NULL WHERE rid IN ({",".join("?" * len(nulls))})',
                             nulls)
            store.db.commit()
    return sid


@pytest.fixture(params=["table", "merge"])
def table(request, client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    if request.param == "table":
        sid = _ingest(store, write_csv, ROWS, "fl_filters")
        # One "n/a" in the sample makes Bytes a text column: the shape on
        # which the grid's `>` compared text while First/Last compared
        # numbers. The merge below has one member of each type.
        assert {c["name"]: c["type"] for c in store.get_source(sid)["columns"]}["Bytes"] == "text"
    else:
        a = _ingest(store, write_csv, ROWS[:6], "fl_a")
        b = _ingest(store, write_csv, [ROWS[0], *ROWS[6:]], "fl_b")
        sid = store.create_merge("fl merge", [a, b])["id"]
    nulls = store.build_view(sid, {"filters": [{"column": "Note", "op": "empty"}]})["row_count"]
    assert nulls == 4, "two '' and two NULL Notes"
    return client, sid


def _fl_counts(client, sid, flt):
    r = client.post("/api/plugin/first_last/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When", "columns": ["Host"],
        "template": "{count}", "filters": [flt]})
    assert r.status_code == 200, r.text
    out = r.json()
    h, d = out["columns"].index("Host"), out["columns"].index("Description")
    return {row[h]: int(row[d]) for row in out["rows"]}


def _grid_counts(store, sid, conds):
    tree = {"type": "group", "op": "AND", "children": [{"type": "cond", "value": "", **c} for c in conds]}
    counts = {}
    for host in ("SRV1", "SRV2", "SRV3"):
        n = store.build_view(sid, {"filter_tree": tree,
                                   "filters": [{"column": "Host", "op": "equals", "value": host}]})["row_count"]
        if n:
            counts[host] = n
    return counts


@pytest.mark.parametrize("fl_filter, grid_conds", FL_TO_GRID,
                         ids=[f"{f['op']}-{f['column']}-{i}" for i, (f, _) in enumerate(FL_TO_GRID)])
def test_each_operator_keeps_the_rows_the_grid_keeps(table, store, fl_filter, grid_conds):
    client, sid = table
    want = _grid_counts(store, sid, grid_conds)
    assert want, "a condition that keeps nothing compares nothing"
    assert _fl_counts(client, sid, fl_filter) == want


def test_every_operator_is_compared():
    """A new operator in First/Last's list has to say which grid
    condition means the same rows, here."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fl_ops", EXAMPLES / "first_last" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.OPERATORS) == {f["op"] for f, _ in FL_TO_GRID}

"""Drilldown from a widget to its rows. A widget may carry `drill`: the
table its rows live in (src_N or a {{evtx}}-style placeholder), the
conditions that select them, and for top-N / over-time widgets the column
a clicked bar or row pivots on. The client resolves the table through
/api/dashboard/resolve and opens the grid with the conditions as a filter
tree — so a drill is only as good as its column names. The shipped KAPE
board's drills are checked here against the header sets they bind to."""

from __future__ import annotations

import pytest

from winnow import defaults

HEADER_SETS = dict(defaults.headers()["nicknames"])
SHORTHANDS = {"evtx": "Event logs (EvtxECmd)", "registry": "Registry (RECmd batch)"}
VALID_OPS = {"equals", "in", "contains", "starts", "gt", "regex", "empty", "not_empty", "not_contains"}


def _kape():
    return next(p for p in defaults.profiles() if p["name"] == "KAPE triage")


def _columns_for(table: str) -> list[str]:
    key = table.strip("{} ").lower()
    return HEADER_SETS[SHORTHANDS[key]]


def test_every_kape_sql_widget_has_a_drill_that_names_real_columns():
    for w in _kape()["dashboard"]:
        if w["source"] != "sql":
            continue
        drill = w.get("drill")
        assert drill, w["title"]
        cols = _columns_for(drill["table"])
        for cond in drill.get("where", []):
            assert cond["column"] in cols, (w["title"], cond)
            assert cond["op"] in VALID_OPS, (w["title"], cond)
            if cond["op"] == "in":
                assert isinstance(cond["value"], list) and cond["value"], (w["title"], cond)
        if drill.get("column"):
            assert drill["column"] in cols, (w["title"], drill["column"])
        if drill.get("bucket"):
            assert drill["bucket"] in ("hour", "day") and drill.get("column"), w["title"]


def test_top_n_widgets_pivot_on_the_column_they_group_by():
    by_title = {w["title"]: w for w in _kape()["dashboard"]}
    assert by_title["Top remote hosts (4624)"]["drill"]["column"] == "RemoteHost"
    assert by_title["Rarest logon accounts (long tail)"]["drill"]["column"] == "UserName"
    assert by_title["Registry entries by category"]["drill"]["column"] == "Category"
    assert by_title["Logon volume over time (4624/4625/4648)"]["drill"] == {
        "table": "{{evtx}}", "column": "TimeCreated", "bucket": "hour",
        "where": [{"column": "Channel", "op": "equals", "value": "Security"},
                  {"column": "EventId", "op": "in", "value": ["4624", "4625", "4648"]}]}


def test_resolve_binds_a_placeholder_to_the_first_matching_source(client, store, write_csv):
    evtx_cols = HEADER_SETS["Event logs (EvtxECmd)"]
    store.ingest_csv(write_csv([["a", "b"], ["1", "2"]], "other.csv"), name="other", build_fts=False)
    sid = store.ingest_csv(write_csv([evtx_cols, ["" for _ in evtx_cols]], "evtx.csv"), name="evtx", build_fts=False)["id"]
    r = client.post("/api/dashboard/resolve", json={"table": "{{evtx}}"})
    assert r.status_code == 200 and r.json()["source_id"] == sid
    # the header_set: spelling and the {{all:…}} union spelling land on the same table
    assert client.post("/api/dashboard/resolve", json={"table": "{{header_set:Event logs (EvtxECmd)}}"}).json()["source_id"] == sid
    assert client.post("/api/dashboard/resolve", json={"table": "{{all:evtx}}"}).json()["source_id"] == sid
    # a case table by name
    assert client.post("/api/dashboard/resolve", json={"table": f"src_{sid}"}).json()["source_id"] == sid
    # SQL comes back with its placeholders substituted, ready for the SQL pane
    r = client.post("/api/dashboard/resolve", json={"sql": "SELECT COUNT(*) FROM {{evtx}} WHERE EventId='4625'"})
    assert r.status_code == 200 and f'"src_{sid}"' in r.json()["sql"] and "{{" not in r.json()["sql"]


def test_resolve_says_when_the_case_has_no_such_table(client, store, write_csv):
    store.ingest_csv(write_csv([["a", "b"], ["1", "2"]], "other.csv"), name="other", build_fts=False)
    r = client.post("/api/dashboard/resolve", json={"table": "{{registry}}"})
    assert r.status_code == 400 and "Registry (RECmd batch)" in r.json()["detail"]
    r = client.post("/api/dashboard/resolve", json={"table": "src_999"})
    assert r.status_code == 400 and "no longer" in r.json()["detail"]
    r = client.post("/api/dashboard/resolve", json={"table": "DROP TABLE x"})
    assert r.status_code == 400


def test_header_sets_lists_the_shorthands_and_their_columns(client):
    body = client.get("/api/header_sets").json()
    assert body["shorthands"]["evtx"] == "Event logs (EvtxECmd)"
    names = {s["name"]: s["columns"] for s in body["sets"]}
    assert "EventId" in names["Event logs (EvtxECmd)"]
    assert "KeyPath" in names["Registry (RECmd batch)"]

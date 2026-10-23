"""Indexes an import builds up front because its shape asks for them
(defaults/headers.json `indexes`): EventId on an EvtxECmd export, so the
shipped dashboards' `WHERE EventId = ... GROUP BY <col>` is an index
search even after the grid has indexed the grouped column."""

from __future__ import annotations

import pytest

from winnow import defaults
from winnow.defaults import DefaultsError, _indexes

EVTX_COLS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _ev(**kw):
    d = {c: "" for c in EVTX_COLS}
    d.update(kw)
    return [d[c] for c in EVTX_COLS]


ROWS = [_ev(EventId=str(4624 + i % 3), RemoteHost=f"10.0.0.{i % 7}", Channel="Security") for i in range(60)]


def _indexed(store, sid):
    return [i["column"] for i in store.list_column_indexes(sid)]


def test_an_evtxecmd_import_indexes_eventid_before_it_returns(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS] + ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    assert _indexed(store, sid) == ["EventId"]
    # The one the first equals filter would have built, not a second one.
    table = store.get_source(sid)["table_name"]
    assert store._column_index_exists(table, "EventId")


def test_the_board_s_group_by_searches_eventid_once_the_grid_indexed_the_grouped_column(store, write_csv):
    """The plan the index is there for: without it, SQLite walks the
    RemoteHost index the grid built and looks up every row."""
    sid = store.ingest_csv(write_csv([EVTX_COLS] + ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    store._ensure_column_index_building(sid, "RemoteHost")
    store.wait_for_column_index(sid, "RemoteHost", timeout=10)
    table = store.get_source(sid)["table_name"]
    plan = " ".join(r[3] for r in store.db.execute(
        f"EXPLAIN QUERY PLAN SELECT RemoteHost, COUNT(*) FROM {table} "
        f"WHERE Channel='Security' AND EventId='4624' GROUP BY RemoteHost"))
    assert store._column_index_name(table, "EventId") in plan and "EventId=?" in plan, plan


def test_the_shape_is_matched_whatever_the_header_case(store, write_csv):
    sid = store.ingest_csv(write_csv([[c.upper() for c in EVTX_COLS]] + ROWS, "evtx.csv"),
                           name="evtx", build_fts=False)["id"]
    assert _indexed(store, sid) == ["EVENTID"]


@pytest.mark.parametrize("cols", [["EventId", "RemoteHost"], EVTX_COLS[:-1]], ids=["two", "all but one"])
def test_a_table_of_another_shape_gets_no_index(store, write_csv, cols):
    rows = [[f"{c}{i}" for c in cols] for i in range(5)]
    sid = store.ingest_csv(write_csv([cols] + rows, "t.csv"), name="t", build_fts=False)["id"]
    assert _indexed(store, sid) == []


def test_saving_an_evtxecmd_view_as_a_table_indexes_it_too(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS] + ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    view = store.build_view(sid, {"filters": [{"column": "EventId", "op": "equals", "value": "4625"}]})
    new = store.save_view_as_source(view["view_id"], "logons", build_fts=False)["source"]["id"]
    assert _indexed(store, new) == ["EventId"]


def test_an_empty_import_builds_nothing(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS], "evtx.csv"), name="evtx", build_fts=False)["id"]
    assert _indexed(store, sid) == []


def test_a_header_set_can_only_index_its_own_columns():
    cols = ["A", "B"]
    assert _indexes("s", cols, {}) == []
    assert _indexes("s", cols, {"indexes": ["B"]}) == ["B"]
    with pytest.raises(DefaultsError, match="'C'"):
        _indexes("s", cols, {"indexes": ["C"]})
    with pytest.raises(DefaultsError, match="list"):
        _indexes("s", cols, {"indexes": "A"})

"""Three ways the app used to answer a question wrongly without saying so.

A crash is recoverable: the analyst sees it and retries. A quietly wrong
answer gets written into a report. Each case here produced a plausible
screen that did not mean what it said.
"""

from __future__ import annotations

import csv

import pytest

from winnow.store import UnknownFilterColumn


def _case(store, write_csv, rows=6):
    body = [["When", "Channel", "Msg"]]
    for i in range(rows):
        body.append([f"2026-03-14 08:0{i}:00", "Security" if i % 2 else "PowerShell", f"m{i}"])
    return store.ingest_csv(write_csv(body, "e.csv"), name="e", build_fts=False)["id"]


def _cond(column, op="equals", value="x"):
    return {"type": "group", "op": "AND",
            "children": [{"type": "cond", "column": column, "op": op, "value": value}]}


def test_a_filter_on_a_column_this_table_lacks_is_refused(store, write_csv):
    """It used to be dropped, which shows MORE rows under a filter chip
    saying the filter is on. Saved filters are offered across cases on a
    column-overlap heuristic, so this is reachable by design."""
    sid = _case(store, write_csv)
    with pytest.raises(UnknownFilterColumn) as e:
        store.build_view(sid, {"source_id": sid, "filters": [], "sort": [],
                               "filter_tree": _cond("ChannelName")})
    assert "ChannelName" in str(e.value)


def test_one_bad_condition_does_not_quietly_widen_the_rest(store, write_csv):
    """The insidious shape: a real condition plus a missing one used to
    return the real condition's rows, which looks entirely plausible."""
    sid = _case(store, write_csv)
    tree = {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "Channel", "op": "equals", "value": "Security"},
        {"type": "cond", "column": "ChannelName", "op": "equals", "value": "nope"}]}
    with pytest.raises(UnknownFilterColumn):
        store.build_view(sid, {"source_id": sid, "filters": [], "sort": [], "filter_tree": tree})


def test_an_empty_value_is_still_an_empty_filter_box(store, write_csv):
    """The other reason a condition compiles to nothing, and the one that
    must stay silent: a filter box the analyst has not typed into."""
    sid = _case(store, write_csv)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [],
                               "filter_tree": _cond("Channel", "contains", "")})
    assert v["row_count"] == 6
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [],
                               "filter_tree": _cond("Channel", "equals", "Security")})
    assert v["row_count"] == 3


def test_loading_a_session_clears_the_undo_journal(store, write_csv):
    """Load-Replace swaps tag state wholesale rather than by a recorded
    delta. The old entries then describe a world that is gone, and Ctrl+Z
    would strip tags off rows the loaded session owns — labelled as the
    analyst's own last action."""
    sid = _case(store, write_csv)
    ta = store.upsert_tag(None, "TA", "#ff0000", None)["id"]
    store.set_tags(sid, [1, 2, 3], ta, True)
    assert store.undo_peek()["available"] is True
    session = store.export_session(sid)

    store.set_tags(sid, [4, 5], ta, True)          # the delta undo would replay
    store.import_session(sid, session, merge=False)
    assert store.undo_peek()["available"] is False, "the journal outlived the state it described"


def test_dropping_a_source_takes_its_undo_entries(store, write_csv):
    """Source ids are reused: `sources.id` is INTEGER PRIMARY KEY with no
    AUTOINCREMENT, so the next import can inherit the entries."""
    sid = _case(store, write_csv)
    ta = store.upsert_tag(None, "TA", "#ff0000", None)["id"]
    store.set_tags(sid, [1, 2], ta, True)
    assert store.undo_peek()["available"] is True
    store.drop_source(sid)
    assert store.undo_peek()["available"] is False


def test_a_group_export_uses_the_analysts_layout(store, write_csv, tmp_path):
    """Every other export path honours the saved layout. This one used raw
    storage order, so the same rows exported from an expanded group carried
    hidden columns the analyst had taken off screen."""
    sid = _case(store, write_csv)
    store.save_layout(sid, {"order": ["Msg", "When"], "columns": {"Channel": {"hidden": True}}})
    root = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    handle = store.expand_group(root["view_id"], "Channel", "Security")
    header = next(iter(store.export_view_csv(handle["view_id"]))).splitlines()[0]
    assert "Channel" not in header, header
    assert header.startswith("Line,Tags,Note,Msg,When"), header

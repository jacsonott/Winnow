"""The Timeline read as a story, server side: a run of repeats folded onto
one line (build_timeline's `fold`), and each line's place against the one
above it (fetch_timeline_rows' `gap` and `new_day`).

What matters most about the fold is what it must never do: put a row
nobody can see inside a line that does not say so. So a run is one table,
one summary lead and one first tag, unbroken; a row with a note always
starts a line of its own; the counts everywhere (the table chips, the
overview's bars) are still rows; and a row folded away can still be found
(timeline_position) and opened back up (`unfold`)."""

from __future__ import annotations

import pytest

from winnow import defaults
from winnow.store import _tl_details, _tl_lead

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _evtx(store, write_csv, name, events):
    """An EvtxECmd-shaped table (the summary's lead is MapDescription) of
    (time, what, who) events."""
    def row(ts, what, who):
        cells = dict.fromkeys(EVTX_COLUMNS, "")
        cells.update(TimeCreated=ts, MapDescription=what, UserName=who, Computer="DC01", EventId="4625")
        return [cells[c] for c in EVTX_COLUMNS]
    path = write_csv([EVTX_COLUMNS] + [row(*e) for e in events], name=name)
    return store.ingest_csv(path, name=name, build_fts=False)["id"]


SPRAY = [(f"2026-02-01 01:{m:02d}:00", "Failed logon", u)
         for m, u in zip(range(20, 26), ["svc_sql", "admin", "svc_backup", "admin", "svc_sql", "backup"])]


def _case(store, write_csv):
    """Six failed logons in a row, then a success, then two more failures."""
    dc = _evtx(store, write_csv, "dc.csv", SPRAY + [
        ("2026-02-01 01:30:00", "Successful logon", "svc_backup"),
        ("2026-02-01 01:31:00", "Failed logon", "admin"),
        ("2026-02-01 01:32:00", "Failed logon", "admin"),
    ])
    tag = store.list_tags()[0]["id"]
    store.set_tags(dc, list(range(1, 10)), tag, True)
    return dc, tag


def _lines(store, v, start=0, count=100):
    return store.fetch_timeline_rows(v["view_id"], start, count)["rows"]


def test_a_run_of_repeats_is_one_line_that_says_how_many(store, write_csv):
    dc, tag = _case(store, write_csv)
    v = store.build_timeline(fold=True)
    lines = _lines(store, v)
    assert [(r["rid"], r["n"], r["lead"]) for r in lines] == [
        (1, 6, "Failed logon"), (7, 1, "Successful logon"), (8, 1, "Failed logon"), (9, 1, "Failed logon")]
    assert lines[0]["ts_last"] == "2026-02-01 01:25:00" and lines[0]["run_n"] == 6
    # Two in a row is not a run worth folding.
    assert lines[2]["run_n"] is None and lines[2]["ts_last"] is None
    assert v["row_count"] == 4 and v["rows"] == 9
    assert v["per_source"] == {dc: 9}


def test_without_fold_every_row_is_a_line(store, write_csv):
    _case(store, write_csv)
    v = store.build_timeline()
    assert v["row_count"] == v["rows"] == 9
    assert {(r["n"], r["run_n"], r["ts_last"]) for r in _lines(store, v)} == {(1, None, None)}


def test_a_run_breaks_at_another_table_another_tag_or_another_lead(store, write_csv):
    dc, tag = _case(store, write_csv)
    other = store.list_tags()[1]["id"]
    # The third failure carries another first tag... (lower id wins, so
    # take the first tag off it and give it the second).
    store.set_tags(dc, [3], tag, False)
    store.set_tags(dc, [3], other, True)
    # ...and a row from another table lands between the fifth and sixth.
    ws = _evtx(store, write_csv, "ws.csv", [("2026-02-01 01:24:30", "Failed logon", "svc_sql")])
    store.set_tags(ws, [1], tag, True)
    lines = _lines(store, store.build_timeline(fold=True))
    assert [(r["source_id"], r["rid"], r["n"]) for r in lines][:5] == [
        (dc, 1, 1), (dc, 2, 1), (dc, 3, 1), (dc, 4, 1), (dc, 5, 1)]
    assert (ws, 1, 1) in [(r["source_id"], r["rid"], r["n"]) for r in lines]


def test_a_row_with_a_note_is_never_inside_a_fold(store, write_csv):
    dc, tag = _case(store, write_csv)
    store.set_note(dc, 4, "the one that worked later")
    lines = _lines(store, store.build_timeline(fold=True))
    # 1-3 fold; 4 (noted) heads a run of its own with 5-6.
    assert [(r["rid"], r["n"]) for r in lines][:2] == [(1, 3), (4, 3)]
    assert lines[1]["note"] == "the one that worked later"


def test_an_unfolded_run_is_a_line_per_row_and_still_knows_it_is_a_run(store, write_csv):
    dc, tag = _case(store, write_csv)
    v = store.build_timeline(fold=True, unfold=[[dc, 1]])
    lines = _lines(store, v)
    assert [(r["rid"], r["n"]) for r in lines][:6] == [(i, 1) for i in range(1, 7)]
    assert lines[0]["run_n"] == 6 and lines[1]["run_n"] is None
    assert v["row_count"] == 9


def test_rows_around_a_finding_never_fold_and_break_a_run(store, write_csv):
    dc, tag = _case(store, write_csv)
    store.set_tags(dc, [3], tag, False)
    lines = _lines(store, store.build_timeline(fold=True, tag_ids=[tag],
                                               context=["2026-02-01 01:00:00", "2026-02-01 02:00:00"]))
    assert [(r["rid"], r["n"], r["ctx"]) for r in lines][:4] == [
        (1, 1, False), (2, 1, False), (3, 1, True), (4, 3, False)]


def test_a_table_without_a_summary_never_folds(store, write_csv):
    sid = store.ingest_csv(write_csv([["When", "What"]] + [[f"2026-02-01 01:0{i}:00", "same"] for i in range(5)],
                                     name="plain.csv"), name="plain.csv", build_fts=False)["id"]
    store.set_tags(sid, [1, 2, 3, 4, 5], store.list_tags()[0]["id"], True)
    v = store.build_timeline(fold=True)
    assert v["row_count"] == 5


def test_the_overview_counts_folded_rows_at_their_own_times(store, write_csv):
    dc, tag = _case(store, write_csv)
    v = store.build_timeline(fold=True)
    ov = store.timeline_overview(v["view_id"])
    assert sum(b["n"] for b in ov["buckets"]) == 9
    # Each row on a bar of its own, at its own time, and every one of the
    # run's bars going to the run's line.
    assert len(ov["buckets"]) == 9
    firsts = [b["first"] for b in ov["buckets"]]
    assert firsts[:6] == [0] * 6 and firsts[6:] == [1, 2, 3]


def test_a_row_folded_away_is_found_on_its_runs_line(store, write_csv):
    dc, tag = _case(store, write_csv)
    v = store.build_timeline(fold=True)
    assert store.timeline_position(v["view_id"], dc, 4, "2026-02-01 01:23:00") == {
        "pos": 0, "exact": False, "folded": True}
    assert store.timeline_position(v["view_id"], dc, 8, None) == {"pos": 2, "exact": True}


def test_folding_runs_no_summary_function(store, write_csv):
    _case(store, write_csv)
    calls = []
    store.db.create_function("TL_LEAD", -1, lambda *a: calls.append(1) or _tl_lead(*a))
    store.db.create_function("TL_DETAILS", -1, lambda *a: calls.append(1) or _tl_details(*a))
    assert store.build_timeline(fold=True)["row_count"] == 4
    assert calls == []


def test_each_line_knows_the_gap_above_it_and_where_a_day_starts(store, write_csv):
    sid = _evtx(store, write_csv, "days.csv", [
        ("2026-02-01 23:59:30", "Logon", "a"),
        ("2026-02-02 00:00:10", "Logoff", "a"),
        ("2026-02-02 02:40:10", "Logon", "b"),
        ("", "Undated", "c"),
    ])
    store.set_tags(sid, [1, 2, 3, 4], store.list_tags()[0]["id"], True)
    v = store.build_timeline()
    lines = _lines(store, v)
    assert [(r["gap"], r["new_day"]) for r in lines] == [(None, True), (40, True), (9600, False), (None, False)]
    # A page that starts part way down still knows the line above it.
    assert [(r["gap"], r["new_day"]) for r in _lines(store, v, 1, 1)] == [(40, True)]


def test_after_a_folded_run_the_gap_is_from_its_last_row(store, write_csv):
    _case(store, write_csv)
    lines = _lines(store, store.build_timeline(fold=True))
    # The run ends at 01:25; the success is at 01:30.
    assert lines[1]["gap"] == 300


def test_the_route_takes_fold_and_unfold(store, write_csv, client):
    dc, tag = _case(store, write_csv)
    assert client.post("/api/timeline", json={"fold": True}).json()["row_count"] == 4
    assert client.post("/api/timeline", json={"fold": True, "unfold": [[dc, 1]]}).json()["row_count"] == 9
    assert client.post("/api/timeline", json={"fold": True, "unfold": [[dc]]}).status_code == 400


@pytest.mark.parametrize("fold", [False, True])
def test_a_rebuild_drops_the_folded_rows_table(store, write_csv, fold):
    _case(store, write_csv)
    first = store.build_timeline(fold=True)
    store.build_timeline(fold=fold)
    names = {r[0] for r in store.db.execute("SELECT name FROM v.sqlite_master WHERE type = 'table'")}
    assert not any(n.startswith(first["view_id"]) for n in names), names

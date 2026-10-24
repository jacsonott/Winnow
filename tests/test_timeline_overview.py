"""The Timeline's overview strip, server side: Store.timeline_overview's
bars over the built view, and the two ways the strip narrows the build —
a stretch of time (`ts_range`) and tables switched off
(`exclude_sources`).

The strip's clicks take the list to a bar's `first` line, so the property
that matters most is that `first` IS the first line of that bar; the
drag sets `ts_range` from bar edges, so the range is half-open and a bar's
last fraction of a second still belongs to it."""

from __future__ import annotations

import calendar
import time


def _epoch(ts: str) -> int:
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))


def _case(store, write_csv):
    """Two tables, five tagged lines over three days, one of them carrying
    both tags."""
    evtx = store.ingest_csv(write_csv([
        ["Timestamp", "EventId", "User"],
        ["2026-01-05 10:00:00", "4624", "alice"],
        ["2026-01-05 10:20:00", "4688", "alice"],
        ["2026-01-06 11:00:00", "4625", "bob"],
        ["2026-01-06 23:59:59.900000", "4634", "bob"],
    ], name="evtx.csv"), name="evtx.csv", build_fts=False)["id"]
    web = store.ingest_csv(write_csv([
        ["VisitTime", "Url"],
        ["2026-01-05 09:30:00", "http://example.com"],
        ["2026-01-07 12:00:00", "http://evil.example"],
    ], name="history.csv"), name="history.csv", build_fts=False)["id"]
    a, b = sorted(t["id"] for t in store.list_tags()[:2])
    store.set_tags(evtx, [1, 2, 3, 4], a, True)
    store.set_tags(evtx, [1], b, True)
    store.set_tags(web, [2], b, True)
    return evtx, web, a, b


def _lines(store, view_id, n=100):
    return store.fetch_timeline_rows(view_id, 0, n)["rows"]


def test_every_line_is_on_one_bar_under_its_first_tag(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline()
    ov = store.timeline_overview(v["view_id"])
    assert sum(bar["n"] for bar in ov["buckets"]) == v["row_count"] == 5
    assert ov["undated"] == 0
    split = {}
    for bar in ov["buckets"]:
        assert sum(bar["split"].values()) == bar["n"]
        for k, n in bar["split"].items():
            split[k] = split.get(k, 0) + n
    # evtx row 1 carries both tags and counts once, under the lower id.
    assert split == {str(a): 4, str(b): 1}


def test_the_split_by_table_counts_each_tables_lines(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline()
    ov = store.timeline_overview(v["view_id"], by="sources")
    split = {}
    for bar in ov["buckets"]:
        for k, n in bar["split"].items():
            split[k] = split.get(k, 0) + n
    assert split == {str(evtx): 4, str(web): 1}
    assert v["per_source"] == {evtx: 4, web: 1}


def test_a_bars_first_is_the_first_line_in_it(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline()
    ov = store.timeline_overview(v["view_id"])
    lines = _lines(store, v["view_id"])
    for bar in ov["buckets"]:
        lo = ov["start"] + bar["b"] * ov["width"]
        on_bar = [r["pos"] for r in lines if lo <= _epoch(r["ts"]) < lo + ov["width"]]
        assert bar["first"] == min(on_bar)
        assert bar["n"] == len(on_bar)


def test_the_first_tag_is_the_first_of_the_tags_switched_on(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline(tag_ids=[b])
    ov = store.timeline_overview(v["view_id"])
    assert v["row_count"] == 2
    assert {k for bar in ov["buckets"] for k in bar["split"]} == {str(b)}


def test_a_stretch_of_time_is_start_inclusive_end_exclusive(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline(ts_range=["2026-01-06 00:00:00", "2026-01-07 00:00:00"])
    assert [r["ts"] for r in _lines(store, v["view_id"])] == [
        "2026-01-06 11:00:00", "2026-01-06 23:59:59"]
    # A line at exactly the start is in; one at exactly the end is not.
    v = store.build_timeline(ts_range=["2026-01-05 10:00:00", "2026-01-05 10:20:00"])
    assert [r["ts"] for r in _lines(store, v["view_id"])] == ["2026-01-05 10:00:00"]


def test_the_bars_rescale_to_a_narrowed_timeline(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    whole = store.timeline_overview(store.build_timeline()["view_id"])
    v = store.build_timeline(ts_range=["2026-01-05 00:00:00", "2026-01-06 00:00:00"])
    narrow = store.timeline_overview(v["view_id"])
    assert narrow["width"] < whole["width"]
    assert sum(bar["n"] for bar in narrow["buckets"]) == v["row_count"] == 2


def test_a_line_with_no_time_is_left_out_of_a_stretch_and_off_the_bars(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    blank = store.ingest_csv(write_csv([["When", "What"], ["", "no time at all"]], name="blank.csv"),
                             name="blank.csv", build_fts=False)["id"]
    store.set_tags(blank, [1], a, True)
    v = store.build_timeline()
    assert v["row_count"] == 6
    ov = store.timeline_overview(v["view_id"])
    assert ov["undated"] == 1
    assert sum(bar["n"] for bar in ov["buckets"]) == 5
    v = store.build_timeline(ts_range=["2000-01-01 00:00:00", "2100-01-01 00:00:00"])
    assert v["row_count"] == 5


def test_a_table_switched_off_is_left_out(store, write_csv):
    evtx, web, a, b = _case(store, write_csv)
    v = store.build_timeline(exclude_sources=[web])
    assert v["row_count"] == 4
    assert v["per_source"] == {evtx: 4}
    assert {r["source_id"] for r in _lines(store, v["view_id"])} == {evtx}


def test_a_pre_1970_line_lands_on_its_own_bar(store, write_csv):
    """FILETIME zero, 1601: an epoch below zero. SQLite's integer division
    truncates toward zero, so a bar number worked out against an unfloored
    start would put it a bar late (time_histogram's trap)."""
    old = store.ingest_csv(write_csv([["When", "What"], ["1601-01-01 00:00:00", "FILETIME zero"],
                                      ["1601-01-01 00:00:30", "thirty seconds later"]],
                                     name="old.csv"), name="old.csv", build_fts=False)["id"]
    a = store.list_tags()[0]["id"]
    store.set_tags(old, [1, 2], a, True)
    v = store.build_timeline()
    ov = store.timeline_overview(v["view_id"], max_buckets=240)
    assert ov["width"] == 1 and ov["start"] == _epoch("1601-01-01 00:00:00")
    assert [(bar["b"], bar["n"], bar["first"]) for bar in ov["buckets"]] == [(0, 1, 0), (30, 1, 1)]


def test_an_empty_timeline_has_no_bars(store, write_csv):
    v = store.build_timeline()
    assert store.timeline_overview(v["view_id"]) == {
        "start": None, "end": None, "width": 0, "undated": 0, "buckets": []}


def test_overview_routes(client, write_csv, store):
    evtx, web, a, b = _case(store, write_csv)
    v = client.post("/api/timeline", json={"tag_ids": [a, b], "exclude_sources": [web]}).json()
    assert v["row_count"] == 4
    ov = client.get("/api/timeline_overview", params={"view_id": v["view_id"], "by": "sources"}).json()
    assert {k for bar in ov["buckets"] for k in bar["split"]} == {str(evtx)}
    assert client.get("/api/timeline_overview", params={"view_id": v["view_id"], "by": "hosts"}).status_code == 400
    assert client.get("/api/timeline_overview", params={"view_id": "view_nope"}).status_code == 409
    bad = client.post("/api/timeline", json={"tag_ids": [a], "ts_range": ["2026-01-05 00:00:00"]})
    assert bad.status_code == 400
    ok = client.post("/api/timeline", json={"tag_ids": [a, b],
                                            "ts_range": ["2026-01-07 00:00:00", "2026-01-08 00:00:00"]})
    assert ok.json()["row_count"] == 1

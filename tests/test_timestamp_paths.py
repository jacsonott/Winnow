"""The clicks that read a datetime column a row at a time — day groups,
expanding a day, jump to timestamp, tag time bounds, the Timeline — held
to the answers TS_NORMALIZE and DAY_BUCKET give in Python.

They used to call those functions on every row; the ISO shape is now
read by SQLite (ts_normalize_sql, day_bucket_sql), and a root view that
holds every row is read through its table in the table's own order
rather than through the view (random access on any sort but rid's: half
a minute on a 4.3M-row MFTECmd table sorted by a timestamp). Neither may
change an answer, so every value here is one of the shapes the fast path
must leave to Python, or a duplicate of one, so that ties have to be
broken the way reading the view in order broke them."""

from __future__ import annotations

import collections
import contextlib
import sqlite3

import pytest

from winnow.store import _day_bucket, _ts_normalize

WHENS = [
    "2026-03-14 10:00:00", "2026-03-14T10:00:05Z", "2026-03-14 10:00:07.1234567",
    "03/14/2026 10:00:09", "2026-03-14 10:00", " 2026-03-14 10:00:13", "2026-03-14T23:59:59+05:00",
    "2026-03-15 00:00:00", "Mar 15 2026 02:00:00", "15 Mar 2026 03:00:00", "3/15/2026 4:05:06 PM",
    "0000-00-00 00:00:00", "not a time", "", "2026-03-13 23:59:59", "2026-03-14 10:00:00 ",
]
# Three rows per value, so every timestamp is tied three ways, and a view
# sorted by Host puts the copies far apart and out of rid order.
ROWS = [["When", "Host"]] + [[w, f"H{(i * 7 + h) % 3}"] for h in range(3) for i, w in enumerate(WHENS)]

_JD = sqlite3.connect(":memory:")


def _julian(ts):
    return _JD.execute("SELECT julianday(?)", (ts,)).fetchone()[0]


@pytest.fixture
def src(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "mixed.csv"), name="mixed", build_fts=False,
                            column_types=["datetime", "text"])["id"]


def _rows(store, view_id):
    return store.fetch_rows(view_id, 0, 10_000)["rows"]


# The views a click can start from: the table as it is, sorted (a root
# view holding every row), filtered (a root view holding some), and both
# kinds of group. One at a time: a build replaces the source's last view.
SPECS = {
    "unfiltered": {},
    "sorted": {"sort": [{"column": "Host", "dir": "desc"}]},
    "filtered": {"sort": [{"column": "Host", "dir": "desc"}],
                 "filters": [{"column": "Host", "op": "not_equals", "value": "H0"}]},
}
KINDS = list(SPECS)


def _view(store, sid, kind):
    if kind == "group":
        view = store.expand_group(_view(store, sid, "filtered"), "Host", "H1")["view_id"]
        assert store._views[view]["kind"] == "group"
        return view
    if kind == "virtual group":
        view = store.expand_group(_view(store, sid, "unfiltered"), "Host", "H1")["view_id"]
        assert store._views[view]["kind"] == "group_virtual"
        return view
    return store.build_view(sid, {"source_id": sid, **SPECS[kind]})["view_id"]


def _nearest(rows, value):
    """What jump to timestamp means: the row closest in time, the first of
    them in the view's order on a tie, rows julianday can't read left out."""
    want = _julian(_ts_normalize(value))
    best = None
    for r in rows:
        ts = _ts_normalize(r["cells"][0])
        jd = _julian(ts) if ts is not None else None
        if jd is None:
            continue
        d = abs(jd - want)
        if best is None or d < best[0]:
            best = (d, r)
    return best and {"pos": best[1]["pos"], "source_id": best[1]["source_id"], "rid": best[1]["rid"],
                     "ts": _ts_normalize(best[1]["cells"][0])}


@pytest.mark.parametrize("kind", KINDS)
def test_day_groups_count_what_day_bucket_says(store, src, kind):
    view = _view(store, src, kind)
    want = collections.Counter(_day_bucket(r["cells"][0]) for r in _rows(store, view))
    got = {g["value"]: g["count"] for g in store.group_summary(view, "When", limit=100)["groups"]}
    assert got == dict(want)


@pytest.mark.parametrize("materialise", [False, True], ids=["virtual", "materialised"])
@pytest.mark.parametrize("kind", KINDS)
def test_expanding_a_day_finds_its_rows(store, src, kind, materialise, monkeypatch):
    """Every day, the empty-bucket group included, expands to exactly the
    rows DAY_BUCKET puts in it: in the parent's order when the group is a
    view of its own, in rid order when it pages the table (a small group
    of a parent holding every row)."""
    if materialise:
        monkeypatch.setattr(store, "GROUP_MATERIALIZE_THRESHOLD", 0)
    view = _view(store, src, kind)
    rows = _rows(store, view)
    for day in {_day_bucket(r["cells"][0]) for r in rows}:
        group = store.expand_group(view, "When", day)
        virtual = store._views[group["view_id"]]["kind"] == "group_virtual"
        assert virtual == (not materialise and kind != "filtered")
        want = [r["rid"] for r in rows if _day_bucket(r["cells"][0]) == day]
        if virtual:
            want.sort()
        assert group["row_count"] == len(want), day
        assert [r["rid"] for r in _rows(store, group["view_id"])] == want, day


@pytest.fixture
def traced(store, monkeypatch):
    """Every statement the writer and the readers run."""
    seen = []
    store.db.set_trace_callback(seen.append)
    real = store._reader

    @contextlib.contextmanager
    def reader():
        with real() as ro:
            ro.set_trace_callback(seen.append)
            try:
                yield ro
            finally:
                ro.set_trace_callback(None)

    monkeypatch.setattr(store, "_reader", reader)
    yield seen
    store.db.set_trace_callback(None)


@pytest.mark.parametrize("materialise", [False, True], ids=["virtual", "materialised"])
def test_a_sorted_view_is_not_read_through_a_row_at_a_time(store, src, traced, materialise, monkeypatch):
    """A view holding every row is answered from the table in its own
    order: no statement joins the table to the view, the shape that cost
    one random lookup per row."""
    if materialise:
        monkeypatch.setattr(store, "GROUP_MATERIALIZE_THRESHOLD", 0)
    view = _view(store, src, "sorted")
    traced.clear()
    store.expand_group(view, "When", "2026-03-14")
    store.find_nearest_timestamp(view, "2026-03-14 10:00:06")
    store.find_nearest_timestamp(view, "2026-03-14 10:00:06", "When")
    assert traced and not [s for s in traced if " vv JOIN " in s]


@pytest.mark.parametrize("value", ["2026-03-14 10:00:06", "2026-03-14 10:00:00", "2026-03-15 00:00:01",
                                   "1999-01-01 00:00:00", "2099-01-01 00:00:00", "2026-03-15 16:05:06"])
@pytest.mark.parametrize("kind", KINDS + ["group", "virtual group"])
def test_jump_lands_on_the_first_closest_row_in_view_order(store, src, kind, value):
    view = _view(store, src, kind)
    rows = _rows(store, view)
    want = _nearest(rows, value)
    assert store.find_nearest_timestamp(view, value, "When") == want
    assert store.find_nearest_timestamp(view, value) == want
    assert _rows(store, view)[want["pos"]]["rid"] == want["rid"]


def test_more_ties_than_it_keeps_still_land_on_the_first(store, src, monkeypatch):
    view = _view(store, src, "sorted")
    want = _nearest(_rows(store, view), "2026-03-14 10:00:00")
    monkeypatch.setattr(store, "NEAREST_TIES", 2)   # three copies of 10:00:00, and the bare 10:00 too
    assert store.find_nearest_timestamp(view, "2026-03-14 10:00:00") == want


@pytest.mark.parametrize("kind", KINDS + ["virtual group"])
def test_a_zero_date_does_not_hide_the_real_timestamps(store, write_csv, kind):
    """julianday('0000-00-00 00:00:00') is NULL, and ORDER BY puts NULL
    first: one such row used to be "the closest", and with no distance to
    compare it left the jump with nothing at all."""
    rows = [["When", "Host"], ["0000-00-00 00:00:00", "H1"], ["2026-03-14 09:00:00", "H1"],
            ["2026-03-14 11:00:00", "H1"], ["0000-00-00 00:00:00", "H0"]]
    sid = store.ingest_csv(write_csv(rows, "zero.csv"), name="zero", build_fts=False,
                           column_types=["datetime", "text"])["id"]
    view = _view(store, sid, kind)
    hit = store.find_nearest_timestamp(view, "2026-03-14 10:45:00")
    assert hit is not None and hit["ts"] == "2026-03-14 11:00:00"


def test_a_merge_sorted_and_respelled(store, src, write_csv):
    """A sorted merge holds every row of every member, so each member is
    read in its own order, under its own spelling of the column."""
    other = store.ingest_csv(write_csv([["WHEN", "Host"], ["2026-03-14 10:00:06", "Z"],
                                        ["03/14/2026 10:00:06", "Z"], ["bad", "Z"]], "other.csv"),
                             name="other", build_fts=False, column_types=["datetime", "text"])["id"]
    mid = store.create_merge("both", [src, other])["id"]
    view = store.build_view(mid, {"source_id": mid, "sort": [{"column": "Host", "dir": "desc"}]})["view_id"]
    rows = _rows(store, view)
    want = _nearest(rows, "2026-03-14 10:00:06")
    assert want["source_id"] == other
    assert store.find_nearest_timestamp(view, "2026-03-14 10:00:06") == want
    for day in ("2026-03-14", None):
        group = store.expand_group(view, "When", day)
        assert [(r["source_id"], r["rid"]) for r in _rows(store, group["view_id"])] == [
            (r["source_id"], r["rid"]) for r in rows if _day_bucket(r["cells"][0]) == day]


def test_tag_time_bounds_and_the_timeline_read_timestamps_as_ts_normalize_does(store, src):
    red = store.upsert_tag(None, "Red", "#ff0000", "1")["id"]
    picked = list(range(4, 46, 3))
    store.set_tags(src, picked, red, True)
    raw = {i + 1: row[0] for i, row in enumerate(ROWS[1:])}
    stamps = sorted(t for t in (_ts_normalize(raw[r]) for r in picked) if t is not None)
    assert store.tag_time_bounds(src) == {"start": stamps[0], "end": stamps[-1]}
    timeline = store.build_timeline({})
    got = {r["rid"]: r["ts"] for r in store.fetch_timeline_rows(timeline["view_id"], 0, 1000)["rows"]}
    assert got == {r: _ts_normalize(raw[r]) for r in picked}

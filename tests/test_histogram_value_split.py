"""The histogram's bars, split by a column's values — the strip's Value
view (the selected cell's value) and Groups view (the grid's first N
groups).

Same contract as the tag split beside it (test_histogram_tag_stack.py):
one row, one segment, so a split bar is exactly as tall as the plain one.
A row holds one value per column, so here that comes for free — what this
file is about is that the segment a row lands in is the one the grid
would put it in: the stored value for a cell, the DAY_BUCKET for a
datetime group, NULL for the "(empty)" group, through a filter, a
derived column and a merge.
"""

from __future__ import annotations

import json

import pytest

ROWS = [["When", "User", "Host"]] + [
    # 24 rows over 8 hours, three to the hour; four users in turn, one
    # of them with no name at all.
    [f"2026-03-14 {i // 3:02d}:{(i % 3) * 10:02d}:00", ["admin", "bob", "carol", ""][i % 4], f"H{i % 3}"]
    for i in range(24)
]


@pytest.fixture
def src(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "events.csv"), name="events", build_fts=False)["id"]


def _hist(store, sid, spec=None, **kw):
    v = store.build_view(sid, {"source_id": sid, **(spec or {})})
    return store.time_histogram(v["view_id"], "When", **kw)


def _per_slot(h):
    return [sum(c[i] for _, c in h["stack"]["buckets"]) for i in range(len(h["stack"]["values"]) + 1)]


def test_the_value_segment_is_the_rows_holding_it(store, src):
    h = _hist(store, src, stack="value", split_column="User", split_values=["admin"])
    assert h["stack"]["kind"] == "value" and h["stack"]["values"] == ["admin"]
    # Every fourth row is admin's; the rest is slot 0.
    assert _per_slot(h) == [18, 6]


def test_the_segments_add_up_to_the_bar(store, src):
    h = _hist(store, src, stack="groups", split_column="User", split_values=["bob", "carol"])
    flat = dict(h["buckets"])
    for b, counts in h["stack"]["buckets"]:
        assert sum(counts) == flat[b], f"bucket {b}: {counts} against {flat[b]}"
    assert sum(_per_slot(h)) == h["total"] == 24


def test_slots_follow_the_order_the_values_were_asked_in(store, src):
    """The client colors slot i as the i-th value it sent; a server that
    sorted them would hand bob's rows carol's color."""
    h = _hist(store, src, stack="groups", split_column="User", split_values=["carol", "admin", "bob"])
    assert h["stack"]["values"] == ["carol", "admin", "bob"]
    assert _per_slot(h) == [6, 6, 6, 6]


def test_the_split_follows_the_filter(store, src):
    h = _hist(store, src, {"filters": [{"column": "Host", "op": "equals", "value": "H1"}]},
              stack="value", split_column="User", split_values=["admin"])
    # H1 is i % 3 == 1; admin is i % 4 == 0 — i = 4, 16.
    assert h["total"] == 8 and _per_slot(h) == [6, 2]


def test_the_empty_group_finds_its_rows(store, src):
    """group_summary names the no-name group by its stored value; whatever
    that is ('' here, NULL elsewhere), the split has to match it with IS,
    not =, or the "(empty)" group's rows all land in Other."""
    groups = [g["value"] for g in store.group_summary(
        store.build_view(src, {"source_id": src})["view_id"], "User")["groups"]]
    empty = next(v for v in groups if v in ("", None))
    h = _hist(store, src, stack="groups", split_column="User", split_values=[empty])
    assert _per_slot(h) == [18, 6]


def test_a_null_value_matches_null_rows(store, write_csv):
    sid = store.ingest_csv(write_csv([["When", "User"], ["2026-03-14 00:00:00", "a"]], "one.csv"),
                           name="one", build_fts=False)["id"]
    # A regex that never matches leaves the derived cell NULL.
    res = store.add_derived_column(sid, "Never", "User", "regex_extract", {"pattern": "(zzz)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    h = _hist(store, sid, stack="value", split_column="Never", split_values=[None])
    assert _per_slot(h) == [0, 1]


def test_a_datetime_group_is_a_day(store, src):
    """The Groups view sends what the group strip shows, and a datetime
    column groups by DAY_BUCKET there — so "groups" compares the day, where
    a cell's "value" is its exact stored timestamp."""
    h = _hist(store, src, stack="groups", split_column="When", split_values=["2026-03-14"])
    assert _per_slot(h) == [0, 24]
    h = _hist(store, src, stack="value", split_column="When", split_values=["2026-03-14 00:10:00"])
    assert _per_slot(h) == [23, 1]


def test_a_derived_column_splits(store, src):
    res = store.add_derived_column(src, "HostNo", "Host", "regex_extract", {"pattern": r"H(\d)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    # Through the view join (a filter on) and the direct shape (none).
    for spec in ({}, {"filters": [{"column": "User", "op": "equals", "value": "bob"}]}):
        h = _hist(store, src, spec, stack="value", split_column="HostNo", split_values=["0"])
        want = sum(1 for i in range(24) if i % 3 == 0 and (not spec or i % 4 == 1))
        assert _per_slot(h)[1] == want, spec


def test_a_merge_splits_through_each_member(store, src, write_csv):
    other = store.ingest_csv(write_csv([["When", "User", "Host"],
                                        ["2026-03-14 09:00:00", "admin", "Z"],
                                        ["2026-03-14 09:10:00", "dave", "Z"]], "more.csv"),
                             name="more", build_fts=False)["id"]
    mid = store.create_merge("both", [src, other])["id"]
    for spec in ({}, {"sort": [{"column": "When", "dir": "desc"}]}):
        h = _hist(store, mid, spec, stack="groups", split_column="User", split_values=["admin", "dave"])
        assert h["total"] == 26
        assert _per_slot(h) == [18, 7, 1], spec
        # A date's day is worked out in the union and compared outside it,
        # so its values are bound once, not once per member.
        h = _hist(store, mid, spec, stack="groups", split_column="When", split_values=["2026-03-14"])
        assert _per_slot(h) == [0, 26], spec


def test_refusals(store, src):
    v = store.build_view(src, {"source_id": src})["view_id"]
    with pytest.raises(KeyError):
        store.time_histogram(v, "When", stack="value", split_column="Nope", split_values=["x"])
    with pytest.raises(ValueError):
        store.time_histogram(v, "When", stack="value", split_column="User", split_values=[])
    with pytest.raises(ValueError):
        store.time_histogram(v, "When", stack="groups", split_column="User",
                             split_values=[str(i) for i in range(store.HISTOGRAM_SPLIT_MAX + 1)])
    # Grouped by tag, the Tags view is the split.
    with pytest.raises(ValueError):
        store.time_histogram(v, "When", stack="groups", split_column="__tag__", split_values=[1])


def test_the_route_takes_the_values_as_json(client, store, src):
    v = store.build_view(src, {"source_id": src})["view_id"]
    r = client.get("/api/histogram", params={
        "view_id": v, "column": "When", "stack": "groups", "split_column": "User",
        "split_values": json.dumps(["admin", "bob"])})
    assert r.status_code == 200, r.text
    assert _per_slot(r.json()) == [12, 6, 6]


def test_the_route_400s_what_it_cannot_split_by(client, store, src):
    v = store.build_view(src, {"source_id": src})["view_id"]
    base = {"view_id": v, "column": "When", "stack": "value"}
    r = client.get("/api/histogram", params={**base, "split_column": "User", "split_values": "admin"})
    assert r.status_code == 400 and "JSON list" in r.json()["detail"]
    r = client.get("/api/histogram", params={**base, "split_column": "Gone", "split_values": '["x"]'})
    # Names the column that is missing — the split's, not the charted one.
    assert r.status_code == 400 and "'Gone'" in r.json()["detail"]
    r = client.get("/api/histogram", params={**base, "split_column": "User", "split_values": "[]"})
    assert r.status_code == 400


@pytest.fixture
def passes(store, monkeypatch):
    """The aggregate passes time_histogram runs, by shape: "span" (MIN/MAX
    of the epoch), "bars" (grouped by bar width), "seconds" (grouped by
    exact second) and "tagged" (the tagged rows' bars, from row_tags)."""
    import contextlib

    seen = []
    real = store._reader

    def shape(sql):
        if "MIN(tag_id) AS tag FROM row_tags" in sql:
            seen.append("tagged")
        elif sql.startswith("SELECT MIN(e), MAX(e)"):
            seen.append("span")
        elif sql.startswith("SELECT e - (("):
            seen.append("bars")
        elif sql.startswith("SELECT e,"):
            seen.append("seconds")

    @contextlib.contextmanager
    def traced():
        with real() as ro:
            ro.set_trace_callback(shape)
            try:
                yield ro
            finally:
                ro.set_trace_callback(None)

    monkeypatch.setattr(store, "_reader", traced)
    return seen


KINDS = [
    {},
    {"stack": "tags"},
    {"stack": "value", "split_column": "User", "split_values": ["admin"]},
    {"stack": "groups", "split_column": "User", "split_values": ["carol", "", "bob"]},
    {"stack": "groups", "split_column": "When", "split_values": ["2026-03-14"]},
]


def _per_second(store, monkeypatch, view_id, **kw):
    """The answer the one-pass-by-second shape gives — the reference the
    faster shapes are held to."""
    with monkeypatch.context() as mp:
        mp.setattr(store, "_histogram_parsed_in_sql", lambda *a: False)
        store._histogram_widths.clear()
        return store.time_histogram(view_id, "When", **kw)


@pytest.mark.parametrize("kw", KINDS)
@pytest.mark.parametrize("spec", [{}, {"filters": [{"column": "Host", "op": "equals", "value": "H1"}]}],
                         ids=["whole-table", "filtered"])
def test_sql_read_timestamps_skip_the_pass_by_second(store, src, passes, monkeypatch, kw, spec):
    """A column SQLite reads itself (ISO text, here) needs no pass by
    second, which sends Python every distinct second to fold. The width
    comes first — from the column's raw MIN/MAX on a whole table, from a
    MIN/MAX pass over a filtered view's rows — and then one pass groups by
    bar. A re-ask about the same view goes straight to the bars: the strip
    asks about one view many times (the plain chart as it lands, then Tags,
    each Value, Groups, First N) and the width is the same each time. Every
    path, the same bars."""
    v = store.build_view(src, {"source_id": src, **spec})["view_id"]
    first = store.time_histogram(v, "When", **kw)
    assert passes == (["bars"] if not spec else ["span", "bars"])
    passes.clear()
    assert store.time_histogram(v, "When", **kw) == first
    assert passes == ["bars"]
    passes.clear()
    assert _per_second(store, monkeypatch, v, **kw) == first
    assert passes == ["seconds"]


def test_a_wrong_guess_costs_a_pass_not_the_chart(store, write_csv, passes, monkeypatch):
    """The raw MIN/MAX is text order. A column mixing 'T' and ' ' after the
    date sorts every T one after every spaced one on the same day, so here
    it reads an hour where the rows span 23: the pass that tried that width
    found the real span, and one more pass draws the right bars."""
    rows = [["When"], ["2026-03-14 01:00:00"], ["2026-03-14T00:00:00"], ["2026-03-14 23:00:00"]]
    sid = store.ingest_csv(write_csv(rows, "mixed.csv"), name="mixed", build_fts=False)["id"]
    v = store.build_view(sid, {"source_id": sid})["view_id"]
    got = store.time_histogram(v, "When")
    assert passes == ["bars", "bars"]
    assert got == _per_second(store, monkeypatch, v)
    assert got["start"] == "2026-03-14 00:00:00" and got["end"] == "2026-03-14 23:00:00"


def _tags_in_the_one_pass(store, monkeypatch, view_id):
    """The tag split with every row's first tag looked up in the one pass,
    as on a filtered view — the reference the tagged-rows-apart shape is
    held to."""
    with monkeypatch.context() as mp:
        mp.setattr(store, "_histogram_tagged_count", lambda ro, members, cap: cap)
        return store.time_histogram(view_id, "When", stack="tags")


def test_few_tagged_rows_are_counted_by_themselves(store, src, write_csv, passes, monkeypatch):
    """Tags over a whole table with a few rows tagged: the bars without
    the split, then the tagged rows on their own, reached from row_tags,
    and the untagged segment is what each bar has left. The lookup of
    every row's first tag was a third of the pass. Row 2 carries both tags
    and is counted once, under the first; on a merge only one member is
    tagged."""
    red = store.upsert_tag(None, "Red", "#ff0000", "1")
    blue = store.upsert_tag(None, "Blue", "#0000ff", "2")
    store.set_tags(src, [1, 2, 9], red["id"], True)
    store.set_tags(src, [2, 20], blue["id"], True)
    other = store.ingest_csv(write_csv(ROWS, "more.csv"), name="more", build_fts=False)["id"]
    merge = store.create_merge("both", [src, other])["id"]
    for sid in (src, merge):
        v = store.build_view(sid, {"source_id": sid})["view_id"]
        passes.clear()
        got = store.time_histogram(v, "When", stack="tags")
        assert passes == ["bars", "tagged"]
        assert got == _tags_in_the_one_pass(store, monkeypatch, v)
        per_tag = [sum(c[i] for _, c in got["stack"]["buckets"]) for i in range(3)]
        assert per_tag == [got["total"] - 4, 3, 1]


def test_a_mostly_tagged_table_looks_its_tags_up_in_the_one_pass(store, src, passes, monkeypatch):
    """Past a third of the rows tagged, reaching each from row_tags costs
    more than the lookup it saves, so the split stays in the one pass."""
    red = store.upsert_tag(None, "Red", "#ff0000", "1")
    store.set_tags(src, list(range(1, 10)), red["id"], True)   # 9 of 24
    v = store.build_view(src, {"source_id": src})["view_id"]
    got = store.time_histogram(v, "When", stack="tags")
    assert passes == ["bars"]
    assert sum(got["stack"]["buckets"][i][1][1] for i in range(len(got["stack"]["buckets"]))) == 9


@pytest.mark.parametrize("kw", KINDS[:2])
def test_python_read_timestamps_are_read_once(store, write_csv, passes, kw):
    """A column TS_NORMALIZE (Python) has to read would be read twice by
    the span-then-bars shape, which costs more than the fold it saves; it
    keeps the one pass by second, and a re-ask goes by bar."""
    rows = [["When", "User"]] + [[f"3/14/2026 {h}:{m:02d}:00 AM", "x"] for h in range(1, 12) for m in (0, 30)]
    sid = store.ingest_csv(write_csv(rows, "us.csv"), name="us", build_fts=False)["id"]
    v = store.build_view(sid, {"source_id": sid})["view_id"]
    first = store.time_histogram(v, "When", **kw)
    assert passes == ["seconds"] and first["total"] == 22
    passes.clear()
    assert store.time_histogram(v, "When", **kw) == first
    assert passes == ["bars"]


def test_a_width_that_no_longer_fits_falls_back(store, src):
    """The remembered width is a hint. A wrong one — a derived column that
    filled in since, a stale entry — must cost a second pass, never a
    chart in the wrong bars."""
    v = store.build_view(src, {"source_id": src})["view_id"]
    want = store.time_histogram(v, "When", stack="groups", split_column="User", split_values=["bob"])
    for wrong in (1, 86400):
        store._histogram_widths[(v, "When", 160)] = wrong
        assert store.time_histogram(v, "When", stack="groups", split_column="User", split_values=["bob"]) == want


def test_a_re_ask_about_an_empty_view_is_empty(store, src):
    v = store.build_view(src, {"source_id": src, "filters": [{"column": "User", "op": "equals", "value": "nobody"}]})["view_id"]
    store._histogram_widths[(v, "When", 160)] = 300
    assert store.time_histogram(v, "When")["total"] == 0

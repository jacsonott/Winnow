"""The histogram's bars, split by tag.

One row, one segment: each row counts under the FIRST tag it carries in
ribbon order — which is the lowest tag id, since the ribbon renders
tag_defs in id order — and untagged rows make the base. So a stacked bar
is exactly as tall as the plain one, and the two charts are the same
chart read two ways.

Counting a two-tag row under both segments is what grouping by tag does,
deliberately, and it is the wrong trade here: bars taller than the rows
they describe, for a per-tag total the ribbon already carries exactly.
That is the property most of this file is about.
"""

from __future__ import annotations

import pytest

from winnow.store import Store


@pytest.fixture
def case(tmp_path):
    p = tmp_path / "events.csv"
    rows = ["Timestamp,EventId"]
    # 24 rows over 8 hours, three to the hour.
    for i in range(24):
        rows.append(f"2026-03-14 {i // 3:02d}:{(i % 3) * 10:02d}:00,{4624 + (i % 2)}")
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    store = Store(str(tmp_path / "case.db"))
    try:
        src = store.ingest_csv(str(p), build_fts=False)
        red = store.upsert_tag(None, "Red", "#ff0000", "1")
        blue = store.upsert_tag(None, "Blue", "#0000ff", "2")
        yield store, src, red, blue
    finally:
        store.close()


def _hist(store, src, **kw):
    v = store.build_view(src["id"], {"filters": [], "sort": []})
    return store.time_histogram(v["view_id"], "Timestamp", **kw)


def test_the_segments_add_up_to_the_bar(case):
    store, src, red, blue = case
    store.set_tags(src["id"], [1, 2, 3, 4], red["id"], True)
    store.set_tags(src["id"], [10, 11], blue["id"], True)
    h = _hist(store, src, stack="tags")
    flat = {b: n for b, n in h["buckets"]}
    for b, counts in h["stack"]["buckets"]:
        assert sum(counts) == flat[b], f"bucket {b}: {counts} against {flat[b]}"
    assert sum(sum(c) for _, c in h["stack"]["buckets"]) == h["total"] == 24


def test_a_row_with_two_tags_is_counted_once(case):
    """The property the whole design turns on. Under "a segment per tag"
    this row would appear twice and the bar would be taller than the
    number of rows in it."""
    store, src, red, blue = case
    store.set_tags(src["id"], [1], red["id"], True)
    store.set_tags(src["id"], [1], blue["id"], True)     # the same row, twice tagged
    h = _hist(store, src, stack="tags")
    assert sum(sum(c) for _, c in h["stack"]["buckets"]) == 24
    # …and it is under the FIRST tag in ribbon order, which is the lower id.
    red_slot = h["stack"]["tags"].index(red["id"]) + 1
    blue_slot = h["stack"]["tags"].index(blue["id"]) + 1 if blue["id"] in h["stack"]["tags"] else None
    assert sum(c[red_slot] for _, c in h["stack"]["buckets"]) == 1
    assert blue_slot is None or sum(c[blue_slot] for _, c in h["stack"]["buckets"]) == 0


def test_untagged_rows_are_the_base_segment(case):
    store, src, red, blue = case
    store.set_tags(src["id"], [1, 2], red["id"], True)
    h = _hist(store, src, stack="tags")
    assert h["stack"]["tags"] == [red["id"]]
    assert sum(c[0] for _, c in h["stack"]["buckets"]) == 22
    assert sum(c[1] for _, c in h["stack"]["buckets"]) == 2


def test_a_view_with_no_tags_at_all_is_one_grey_layer(case):
    store, src, red, blue = case
    h = _hist(store, src, stack="tags")
    assert h["stack"]["tags"] == []
    assert sum(c[0] for _, c in h["stack"]["buckets"]) == 24


def test_the_plain_chart_is_untouched(case):
    """Asking for the split adds a key; it does not change the answer the
    strip has always drawn."""
    store, src, red, blue = case
    store.set_tags(src["id"], [1, 2, 3], red["id"], True)
    plain = _hist(store, src)
    stacked = _hist(store, src, stack="tags")
    assert "stack" not in plain
    assert plain["buckets"] == stacked["buckets"]
    assert plain["total"] == stacked["total"]
    assert plain["bucket_seconds"] == stacked["bucket_seconds"]


def test_the_split_follows_the_view(case):
    """The chart is about the rows in the view, so the segments are too —
    a tag on rows the filter excludes is not in it."""
    store, src, red, blue = case
    store.set_tags(src["id"], [1, 2, 3], red["id"], True)
    v = store.build_view(src["id"], {"sort": [], "filters": [
        {"column": "EventId", "op": "equals", "value": "4625"}]})
    h = store.time_histogram(v["view_id"], "Timestamp", stack="tags")
    assert h["total"] == 12
    assert sum(sum(c) for _, c in h["stack"]["buckets"]) == 12
    # rids 1 and 3 are EventId 4624 (odd i → 4625), so one of the three
    # tagged rows is in this view.
    tagged = sum(c[1] for _, c in h["stack"]["buckets"]) if h["stack"]["tags"] else 0
    assert tagged == 1


def test_it_works_on_a_merge(tmp_path):
    """Invariant #9: the split is per member, and a member with no tags at
    all skips the lookup rather than being left out of the chart."""
    store = Store(str(tmp_path / "case.db"))
    try:
        srcs = []
        for name in ("a.csv", "b.csv"):
            p = tmp_path / name
            rows = ["Timestamp,EventId"]
            for i in range(6):
                rows.append(f"2026-03-14 {i:02d}:00:00,{4624 + (i % 2)}")
            p.write_text("\n".join(rows) + "\n", encoding="utf-8")
            srcs.append(store.ingest_csv(str(p), build_fts=False))
        a, b = srcs
        red = store.upsert_tag(None, "Red", "#ff0000", "1")
        store.set_tags(a["id"], [1, 2], red["id"], True)   # only one member is tagged
        merge = store.create_merge("both", [a["id"], b["id"]])
        v = store.build_view(merge["id"], {"filters": [], "sort": []})
        h = store.time_histogram(v["view_id"], "Timestamp", stack="tags")
        assert h["total"] == 12
        assert sum(sum(c) for _, c in h["stack"]["buckets"]) == 12
        assert sum(c[1] for _, c in h["stack"]["buckets"]) == 2
        assert sum(c[0] for _, c in h["stack"]["buckets"]) == 10
    finally:
        store.close()


def test_the_route_only_honours_the_split_it_knows(client, store, tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("Timestamp\n2026-03-14 01:00:00\n", encoding="utf-8")
    src = store.ingest_csv(str(p), build_fts=False)
    v = store.build_view(src["id"], {"filters": [], "sort": []})
    assert "stack" in client.get(
        f"/api/histogram?view_id={v['view_id']}&column=Timestamp&stack=tags").json()
    # Anything else is ignored rather than refused: an old client and a new
    # server agree on the chart they already share.
    assert "stack" not in client.get(
        f"/api/histogram?view_id={v['view_id']}&column=Timestamp&stack=nonsense").json()

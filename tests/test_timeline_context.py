"""The rows around a finding (build_timeline's `context`) and a line's row
in full (timeline_row): the server half of the Timeline's detail pane.

The window is read two ways — narrowed on the raw text first when every
value of the column starts with a date, the exact test alone otherwise —
and the property that matters is that both find the same rows: a row the
fast path missed would be missing from "what else happened then" with
nothing on screen to say so."""

from __future__ import annotations

import pytest

from winnow.store import RecordGone

WINDOW = ["2026-01-05 10:00:00", "2026-01-05 10:10:00"]


def _ingest(store, write_csv, name, rows):
    return store.ingest_csv(write_csv(rows, name=name), name=name, build_fts=False)["id"]


def _case(store, write_csv):
    """Three tables with the same moments in three spellings — ISO with a
    space, ISO with a T, US dates — and the finding on the first."""
    times = ["2026-01-05 09:59:59", "2026-01-05 10:00:00", "2026-01-05 10:05:00",
             "2026-01-05 10:09:59", "2026-01-05 10:10:00", "2026-01-06 10:05:00"]
    space = _ingest(store, write_csv, "space.csv",
                    [["When", "What"]] + [[t, f"space {i}"] for i, t in enumerate(times)])
    tee = _ingest(store, write_csv, "tee.csv",
                  [["When", "What"]] + [[t.replace(" ", "T") + ".1234567Z", f"tee {i}"] for i, t in enumerate(times)])

    def us(t):
        d, h = t.split(" ")
        y, m, dd = d.split("-")
        return f"{m}/{dd}/{y} {h}"
    usd = _ingest(store, write_csv, "us.csv", [["When", "What"]] + [[us(t), f"us {i}"] for i, t in enumerate(times)])
    tag = store.list_tags()[0]["id"]
    store.set_tags(space, [3], tag, True)
    configs = {sid: {"timestamp_column": "When"} for sid in (space, tee, usd)}
    return space, tee, usd, tag, configs


def _lines(store, v):
    return store.fetch_timeline_rows(v["view_id"], 0, 500)["rows"]


def test_the_rows_around_a_finding_join_it_from_every_table(store, write_csv):
    space, tee, usd, tag, configs = _case(store, write_csv)
    v = store.build_timeline(configs, context=WINDOW)
    lines = _lines(store, v)
    found = [(r["source_id"], r["rid"]) for r in lines if not r["ctx"]]
    assert found == [(space, 3)]
    near = {(r["source_id"], r["rid"]) for r in lines if r["ctx"]}
    # 10:00:00, 10:05:00 and 10:09:59 from each table: the start is in, the
    # end is not, the finding itself is a line already, and the next day's
    # 10:05 is outside the window.
    assert near == {(space, 2), (space, 4), (tee, 2), (tee, 3), (tee, 4), (usd, 2), (usd, 3), (usd, 4)}
    assert v["context_rows"] == 8
    assert v["per_source"] == {space: 1}
    assert [r["ts"] for r in lines] == sorted(r["ts"] for r in lines)


def test_both_ways_of_reading_the_window_agree(store, write_csv):
    """The date-led path on, then forced off: the same rows either way."""
    space, tee, usd, tag, configs = _case(store, write_csv)
    assert store._column_is_date_led(store.get_source(space), "When")
    assert store._column_is_date_led(store.get_source(tee), "When")
    assert not store._column_is_date_led(store.get_source(usd), "When")
    fast = {(r["source_id"], r["rid"]) for r in _lines(store, store.build_timeline(configs, context=WINDOW))}
    store._date_led = {k: False for k in store._date_led}
    exact = {(r["source_id"], r["rid"]) for r in _lines(store, store.build_timeline(configs, context=WINDOW))}
    assert fast == exact


def test_a_window_ending_at_midnight_keeps_the_last_seconds_of_the_day(store, write_csv):
    sid = _ingest(store, write_csv, "late.csv", [["When", "What"], ["2026-01-05 23:59:59", "late"],
                                                 ["2026-01-06 00:00:00", "midnight"]])
    v = store.build_timeline(context=["2026-01-05 23:00:00", "2026-01-06 00:00:00"])
    assert [(r["rid"], r["ctx"]) for r in _lines(store, v)] == [(1, True)]
    assert v["row_count"] == 1 and sid


def test_a_row_carrying_only_a_tag_switched_off_is_around_the_finding(store, write_csv):
    space, tee, usd, tag, configs = _case(store, write_csv)
    other = store.list_tags()[1]["id"]
    store.set_tags(space, [2], other, True)
    v = store.build_timeline(configs, tag_ids=[tag], context=WINDOW)
    by = {(r["source_id"], r["rid"]): r for r in _lines(store, v)}
    assert by[(space, 2)]["ctx"] is True and by[(space, 2)]["tags"] == [other]


def test_a_table_switched_off_brings_no_rows_around(store, write_csv):
    space, tee, usd, tag, configs = _case(store, write_csv)
    v = store.build_timeline(configs, context=WINDOW, exclude_sources=[tee])
    assert tee not in {r["source_id"] for r in _lines(store, v)}


def test_a_derived_timestamp_is_read_as_it_is(store, write_csv):
    sid = _ingest(store, write_csv, "epoch.csv", [["Epoch", "What"], ["1767607500", "in"], ["1767611100", "out"]])
    job = store.add_derived_column(sid, "When", "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(job["job_id"], timeout=30)
    v = store.build_timeline({sid: {"timestamp_column": "When"}}, context=WINDOW)
    assert [(r["rid"], r["ts"][:19]) for r in _lines(store, v)] == [(1, "2026-01-05 10:05:00")]


def test_the_date_range_is_read_off_the_import_sort_index(store, write_csv):
    space, tee, usd, tag, configs = _case(store, write_csv)
    src = store.get_source(space)
    p = store._timeline_parts(src, configs[space])
    sql, params = store._timeline_context_branch(src, p, WINDOW, "", None)
    plan = " | ".join(r[3] for r in store.db.execute(f"EXPLAIN QUERY PLAN {sql}", params))
    # A search of the index by the day range, not a scan of the table.
    assert "SEARCH s USING" in plan and "(When>? AND When<?)" in plan.replace('"', ""), plan


def test_the_overview_counts_the_rows_around_apart(store, write_csv):
    space, tee, usd, tag, configs = _case(store, write_csv)
    v = store.build_timeline(configs, context=WINDOW)
    ov = store.timeline_overview(v["view_id"])
    split = {}
    for bar in ov["buckets"]:
        for k, n in bar["split"].items():
            split[k] = split.get(k, 0) + n
    assert split == {str(tag): 1, "ctx": 8}


def test_a_malformed_window_is_refused(store, write_csv, client):
    with pytest.raises(ValueError):
        store.build_timeline(context=["2026-01-05 10:00:00"])
    assert client.post("/api/timeline", json={"context": ["x"]}).status_code == 400


def test_a_lines_row_in_full(store, write_csv, client):
    sid = _ingest(store, write_csv, "row.csv", [["When", "What", "Who"], ["2026-01-05 10:00:00", "logon", "alice"]])
    a, b = sorted(t["id"] for t in store.list_tags()[:2])
    store.set_tags(sid, [1], b, True)
    store.set_tags(sid, [1], a, True)
    store.set_note(sid, 1, "first sighting")
    row = client.get("/api/timeline_row", params={"source_id": sid, "rid": 1}).json()
    assert [c["name"] for c in row["columns"]] == ["When", "What", "Who"]
    assert row["values"] == ["2026-01-05 10:00:00", "logon", "alice"]
    assert row["tags"] == [a, b] and row["note"] == "first sighting"
    assert row["source_name"] == "row.csv"
    with pytest.raises(RecordGone):
        store.timeline_row(sid, 99)
    assert client.get("/api/timeline_row", params={"source_id": sid, "rid": 99}).status_code == 404
    assert client.get("/api/timeline_row", params={"source_id": 9999, "rid": 1}).status_code == 404

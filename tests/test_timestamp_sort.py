"""Sorting a recognized timestamp column puts its rows in time order,
whatever shape the file wrote the timestamps in.

A column typed `datetime` at import (DATE_RE: ISO, US M/D/YYYY, month
names) used to sort as text, so a US-dated table, sorted by its timestamp
(which is how every table opens), put "11/3/2026" before "12/1/2025" and
"2/14/2026" after both. The display format setting changed what the cells
showed but not the order, and deriving a new timestamp column from it was
the only way to get time order, because a derived column is canonical ISO.
ISO columns keep sorting as text, through the COLLATE NOCASE index the
first datetime column gets at import: that order is already time order."""

from __future__ import annotations

import pytest

US = [
    ["When", "Event"],
    ["11/3/2026 9:05:00 PM", "evening"],
    ["2/14/2026 8:00:00 AM", "february"],
    ["11/3/2026 9:05:00 AM", "morning"],
    ["12/1/2025 11:59:59 PM", "last year"],
    ["3/2/2026 12:30:00 PM", "march noon"],
    ["11/3/2026 12:00:00 AM", "midnight"],
]
US_IN_TIME_ORDER = ["last year", "february", "march noon", "midnight", "morning", "evening"]


def _ingest(store, write_csv, rows, name="t", **kw):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False, **kw)["id"]


def _sorted(store, source_id, column="When", direction="asc", filters=None, col=1):
    view = store.build_view(source_id, {"source_id": source_id, "filters": filters or [],
                                        "sort": [{"column": column, "dir": direction}]})
    return [r["cells"][col] for r in store.fetch_rows(view["view_id"], 0, 100)["rows"]]


def test_a_us_dated_column_sorts_in_time_order_both_ways(store, write_csv):
    sid = _ingest(store, write_csv, US)
    assert store.get_source(sid)["columns"][0]["type"] == "datetime"
    assert _sorted(store, sid) == US_IN_TIME_ORDER
    assert _sorted(store, sid, direction="desc") == US_IN_TIME_ORDER[::-1]
    # A filtered view is sorted the same way.
    narrowed = [{"column": "Event", "op": "not_equals", "value": "midnight"}]
    assert _sorted(store, sid, filters=narrowed) == [e for e in US_IN_TIME_ORDER if e != "midnight"]


def test_a_month_name_column_sorts_in_time_order(store, write_csv):
    rows = [["When", "Event"],
            ["Mar 2 2026 10:00:00", "march"],
            ["Dec 31 2025 23:00:00", "new year's eve"],
            ["Feb 14 2026 08:00:00", "february"],
            ["Apr 1 2026 00:00:01", "april"]]
    sid = _ingest(store, write_csv, rows)
    assert store.get_source(sid)["columns"][0]["type"] == "datetime"
    assert _sorted(store, sid) == ["new year's eve", "february", "march", "april"]


def test_rows_in_the_same_second_keep_their_fraction_order(store, write_csv):
    """The time key is whole seconds, so the raw text breaks a tie: the
    fraction digits sit at the same place in each value, and compare as
    a decimal fraction does."""
    rows = [["When", "Event"],
            ["1/5/2026 1:22:01.5 PM", "half"],
            ["1/5/2026 1:22:01.25 PM", "quarter"],
            ["1/5/2026 1:22:01.75 PM", "three quarters"],
            ["1/5/2026 1:22:00 PM", "second before"],
            ["1/5/2026 11:00:00 PM", "late"]]
    sid = _ingest(store, write_csv, rows)
    want = ["second before", "quarter", "half", "three quarters", "late"]
    assert _sorted(store, sid) == want
    assert _sorted(store, sid, direction="desc") == want[::-1]


def test_values_that_are_not_timestamps_sort_to_one_edge(store, write_csv):
    """As a number column's non-numbers do: they have no time to sort by,
    so they come first ascending and last descending, among themselves by
    their text, rather than interleaved by where their text would fall."""
    rows = [["When", "Event"]] + [[f"1/{d}/2026 10:00:00 AM", f"jan {d}"] for d in range(1, 9)]
    rows += [["N/A", "na"], ["", "blank"]]
    sid = _ingest(store, write_csv, rows, column_types=["datetime", "text"])
    order = _sorted(store, sid)
    assert order[:2] == ["blank", "na"]
    assert order[2:] == [f"jan {d}" for d in range(1, 9)]
    assert _sorted(store, sid, direction="desc")[-2:] == ["na", "blank"]


def test_the_order_matches_a_derived_timestamp_column(store, write_csv):
    """Deriving a timestamp column from it was the workaround. The
    recognized column now sorts the rows exactly as its derived twin does."""
    sid = _ingest(store, write_csv, US)
    res = store.add_derived_column(sid, "Parsed", "When", "us_datetime", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _sorted(store, sid) == _sorted(store, sid, column="Parsed")
    assert _sorted(store, sid, direction="desc") == _sorted(store, sid, column="Parsed", direction="desc")


def test_an_iso_column_still_sorts_as_text_through_its_index(store, write_csv):
    """ISO text order is time order, so nothing changes for it: the sort
    stays the plain COLLATE NOCASE one the import's index serves."""
    rows = [["When", "Event"]] + [[f"2026-03-{d:02d} 10:00:00", f"d{d}"] for d in (5, 1, 3, 2, 4)]
    sid = _ingest(store, write_csv, rows)
    src = store.get_source(sid)
    spec = {"sort": [{"column": "When", "dir": "asc"}]}
    colnames = {c["name"]: c["type"] for c in src["columns"]}
    assert store._time_sort_columns(src, spec, colnames) == frozenset()
    assert store._column_index_exists(src["table_name"], "When", "sort")
    assert _sorted(store, sid) == ["d1", "d2", "d3", "d4", "d5"]
    assert 'TS_NORMALIZE' not in store.spec_sql(sid, spec)


def test_a_us_dated_column_gets_no_sort_index_nothing_could_use(store, write_csv):
    """Its sort is by an expression, which no index serves, so neither the
    import nor the first sort spends time building one."""
    sid = _ingest(store, write_csv, US)
    table = store.get_source(sid)["table_name"]
    assert not store._column_index_exists(table, "When", "sort")
    _sorted(store, sid)
    assert not store.wait_for_column_index(sid, "When", timeout=1, purpose="sort")


def test_a_us_date_at_the_end_of_an_iso_file_is_noticed(store, write_csv):
    """Two exports appended into one file: the column is ISO at the top and
    US at the bottom. Both ends are looked at, so it sorts by time."""
    rows = [["When", "Event"]] + [[f"2026-01-01 10:{m:02d}:00", f"iso {m}"] for m in range(300)]
    rows += [["3/1/2025 11:00:00 PM", "us, the earliest"]]
    sid = _ingest(store, write_csv, rows)
    assert _sorted(store, sid)[0] == "us, the earliest"


def test_the_sql_pane_query_sorts_the_same_way(store, write_csv):
    """"Open in SQL" compiles the same ORDER BY, and the pane can run it."""
    sid = _ingest(store, write_csv, US)
    sql = store.spec_sql(sid, {"sort": [{"column": "When", "dir": "asc"}]})
    assert "TS_NORMALIZE" in sql
    got = store.run_sql(sql)
    event = got["columns"].index("Event")
    assert [r[event] for r in got["rows"]] == US_IN_TIME_ORDER


def test_copy_unique_values_lists_them_in_time_order(store, write_csv):
    sid = _ingest(store, write_csv, US)
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    assert store.distinct_values(view["view_id"], "When")["values"] == [
        "12/1/2025 11:59:59 PM", "2/14/2026 8:00:00 AM", "3/2/2026 12:30:00 PM",
        "11/3/2026 12:00:00 AM", "11/3/2026 9:05:00 AM", "11/3/2026 9:05:00 PM"]


@pytest.mark.parametrize("filtered", [False, True])
def test_a_merge_of_iso_and_us_tables_sorts_both_in_time_order(store, write_csv, filtered):
    """Merge parity: a single ORDER BY orders every member's rows against
    each other, so one US-dated member puts the whole merge on the time
    key. Unfiltered and filtered merges are built by different SQL."""
    iso = _ingest(store, write_csv, [["When", "Event"],
                                     ["2026-02-01 09:00:00", "iso feb"],
                                     ["2025-12-15 09:00:00", "iso dec"],
                                     ["2026-11-03 13:00:00", "iso nov 1pm"]], name="iso")
    us = _ingest(store, write_csv, US, name="us")
    mid = store.create_merge("both", [iso, us])["id"]
    filters = [{"column": "Event", "op": "not_equals", "value": "nothing"}] if filtered else []
    want = ["last year", "iso dec", "iso feb", "february", "march noon", "midnight", "morning",
            "iso nov 1pm", "evening"]
    assert _sorted(store, mid, filters=filters) == want
    assert _sorted(store, mid, direction="desc", filters=filters) == want[::-1]


@pytest.mark.parametrize("dedupe", [False, True])
def test_a_merges_sql_pane_query_sorts_by_time_too(store, write_csv, dedupe):
    """A merge's "Open in SQL" is a UNION ALL, and a compound's ORDER BY
    may only name its own columns, so a sort by an expression failed with
    "1st ORDER BY term does not match any column". It did for a number
    column before this; a US-dated member made the default sort one too.
    The union is sorted from outside instead."""
    iso = _ingest(store, write_csv, [["When", "Event", "N"],
                                     ["2026-02-01 09:00:00", "iso feb", "10"],
                                     ["2025-12-15 09:00:00", "iso dec", "9"]], name="iso")
    us = _ingest(store, write_csv, [["When", "Event", "N"],
                                    ["11/3/2026 9:05:00 PM", "us nov", "100"],
                                    ["1/2/2026 8:00:00 AM", "us jan", "2"]], name="us")
    mid = store.create_merge("both", [iso, us])["id"]
    extra = {"dedupe_columns": ["When", "Event", "N"]} if dedupe else {}

    def events(sort):
        got = store.run_sql(store.spec_sql(mid, {"source_id": mid, "sort": sort, **extra}))
        return [r[got["columns"].index("Event")] for r in got["rows"]]

    assert events([{"column": "When", "dir": "asc"}]) == ["iso dec", "us jan", "iso feb", "us nov"]
    assert events([{"column": "When", "dir": "desc"}]) == ["us nov", "iso feb", "us jan", "iso dec"]
    assert events([{"column": "N", "dir": "asc"}]) == ["us jan", "iso dec", "iso feb", "us nov"]

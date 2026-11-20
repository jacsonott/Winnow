"""Day-first dates written with dashes, dd-mm-yyyy hh:mm:ss
("23-06-2026 13:05:01"), are a recognized timestamp shape.

Before, nothing read them: a column of them imported as text, so it sorted
as text ("01-07-2026" before "23-06-2026"), the timeframe filter, the
histogram and the Timeline could not place it, no display format applied,
and Add datetime column had no operation for it. The shape is now read
everywhere the US and ISO shapes are: DATE_RE types the column at import,
TS_NORMALIZE and DAY_BUCKET read it on the server, parseTimestamp in the
browser (tests/ui/test_timestamp_dmy.py), the "dd-mm-yyyy hh:mm:ss"
derived-column operation, and the duration columns' reader. Dashes read
day first and slashes month first, so the two never compete for a value.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from winnow import timeparse
from winnow import workspace as WS
from winnow.store import DATE_RE, _day_bucket, _ts_normalize, infer_type

# Value as a tool writes it -> what TS_NORMALIZE makes of it.
READ = [
    ("23-06-2026 13:05:01", "2026-06-23 13:05:01"),
    ("01-07-2026 00:00:00", "2026-07-01 00:00:00"),
    ("5-1-2026 9:05:01", "2026-01-05 09:05:01"),
    ("05-01-2026 1:05 PM", "2026-01-05 13:05:00"),
    ("05-01-2026 12:30:00 a.m.", "2026-01-05 00:30:00"),
    ("23-06-2026 13:05", "2026-06-23 13:05:00"),
    ("23-06-2026 13:05:01.123456", "2026-06-23 13:05:01"),
    ("23-06-2026T13:05:01", "2026-06-23 13:05:01"),
    ("23-06-2026 @ 13:05:01", "2026-06-23 13:05:01"),
    ("23-06-2026, 13:05:01", "2026-06-23 13:05:01"),
    ("31-12-2025 23:59:59", "2025-12-31 23:59:59"),
    ("23-06-2026", "2026-06-23 00:00:00"),
]

# Not this shape: no day 32, no month 13, no fifth year digit, and a
# month-first value with its day past 12 is not misread as day-first.
NOT_READ = [
    "32-01-2026 10:00:00",
    "00-01-2026 10:00:00",
    "12-25-2026 10:00:00",
    "01-13-2026 10:00:00",
    "01-00-2026 10:00:00",
    "23-06-20261 10:00:00",
    "23-06-26 10:00:00",
    "123-06-2026 10:00:00",
]


@pytest.mark.parametrize("value, want", READ)
def test_a_dd_mm_yyyy_value_is_read_day_first(value, want):
    assert DATE_RE.match(value)
    assert _ts_normalize(value) == want
    assert _day_bucket(value) == want[:10]


@pytest.mark.parametrize("value", NOT_READ)
def test_a_value_outside_the_shape_is_left_alone(value):
    assert not DATE_RE.match(value)
    assert _ts_normalize(value) is None
    assert _day_bucket(value) is None


def test_slashes_stay_month_first():
    """The US shape is untouched: 5/1/2026 is still May 1st."""
    assert _ts_normalize("5/1/2026 10:00:00") == "2026-05-01 10:00:00"
    assert _ts_normalize("5-1-2026 10:00:00") == "2026-01-05 10:00:00"


def test_a_column_of_them_imports_as_datetime():
    assert infer_type(["23-06-2026 13:05:01", "01-07-2026 08:00:00", "5-1-2026 9:05:01"]) == "datetime"


def test_a_month_first_column_with_dashes_is_not_typed_day_first():
    """Most month-first values put 13-31 where a day-first reading wants
    the month, so the column stays text instead of being misread."""
    us_dashed = [f"12-{d:02d}-2026 10:00:00" for d in range(1, 32)]
    assert infer_type(us_dashed) == "text"


# ------------------------------------------------------- derived-column op

def parse(value, params=None):
    op = timeparse.OPERATIONS["dmy_datetime"]
    return op["parse"](value, timeparse.validate_params("dmy_datetime", params or {}), {})


@pytest.mark.parametrize("value, want", [
    ("23-06-2026 13:05:01", "2026-06-23 13:05:01"),
    ("5-1-2026 1:05 PM", "2026-01-05 13:05:00"),
    ("05-01-2026 12:00:00 AM", "2026-01-05 00:00:00"),
    ("23-06-2026 13:05:01.25", "2026-06-23 13:05:01.250000"),
    ("23-06-2026 @ 13:05:01", "2026-06-23 13:05:01"),
    ("23-06-2026 13:05:01 +0200", "2026-06-23 11:05:01"),
    ("23-06-2026 13:05:01 UTC", "2026-06-23 13:05:01"),
])
def test_the_derived_operation_reads_it(value, want):
    assert parse(value) == want


@pytest.mark.parametrize("value", [
    "31-04-2026 10:00:00",     # April has 30 days
    "29-02-2025 10:00:00",     # not a leap year
    "23-06-2026",              # a time is required, as for the US operation
    "12-25-2026 10:00:00",
    "2026-06-23 13:05:01",
    "23/06/2026 13:05:01",
    "23-06-2026 13:05:01 CEST",
])
def test_the_derived_operation_refuses_what_it_cannot_read(value):
    assert parse(value) is None


def test_a_fixed_offset_shifts_a_naive_value():
    assert parse("23-06-2026 13:05:01", {"utc_offset": "-05:00"}) == "2026-06-23 18:05:01"


def test_detection_picks_it_and_nothing_else():
    ranked = timeparse.detect(["23-06-2026 13:05:01", "01-07-2026 08:00:00", "5-1-2026 9:05 PM"])
    assert ranked[0]["op_id"] == "dmy_datetime"
    assert ranked[0]["confidence"] == 1.0
    assert all(r["op_id"] != "us_datetime" for r in ranked)


def test_the_delta_columns_reader_reads_it():
    assert timeparse.parse_datetime_lenient("23-06-2026 13:05:01.5") == datetime(2026, 6, 23, 13, 5, 1, 500000)
    assert timeparse.parse_datetime_lenient("5-1-2026 1:05 PM") == datetime(2026, 1, 5, 13, 5)


# ------------------------------------------------------------- the store

DMY = [
    ["When", "Event"],
    ["01-07-2026 08:00:00", "july"],
    ["23-06-2026 13:05:01", "june afternoon"],
    ["31-12-2025 23:59:59", "new year's eve"],
    ["23-06-2026 09:00:00", "june morning"],
    ["2-1-2026 12:00:00", "january"],
]
DMY_IN_TIME_ORDER = ["new year's eve", "january", "june morning", "june afternoon", "july"]


def _ingest(store, write_csv, rows, name="t"):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False)["id"]


def _events(store, source_id, spec):
    view = store.build_view(source_id, {"source_id": source_id, **spec})
    return [r["cells"][1] for r in store.fetch_rows(view["view_id"], 0, 100)["rows"]]


def test_a_dd_mm_yyyy_column_sorts_in_time_order(store, write_csv):
    """As text, "01-07-2026" (July) sorts before "23-06-2026" (June)."""
    sid = _ingest(store, write_csv, DMY)
    assert store.get_source(sid)["columns"][0]["type"] == "datetime"
    asc = _events(store, sid, {"sort": [{"column": "When", "dir": "asc"}]})
    assert asc == DMY_IN_TIME_ORDER
    desc = _events(store, sid, {"sort": [{"column": "When", "dir": "desc"}]})
    assert desc == DMY_IN_TIME_ORDER[::-1]


def test_the_timeframe_filter_reads_it(store, write_csv):
    sid = _ingest(store, write_csv, DMY)
    window = {"enabled": True, "column": "When", "start": "2026-06-23 12:00:00", "end": "2026-07-01 12:00:00"}
    got = _events(store, sid, {"time_range": window, "sort": [{"column": "When", "dir": "asc"}]})
    assert got == ["june afternoon", "july"]


def test_the_histogram_places_it(store, write_csv):
    sid = _ingest(store, write_csv, DMY)
    view = store.build_view(sid, {"source_id": sid})
    h = store.time_histogram(view["view_id"], "When")
    assert (h["start"], h["end"]) == ("2025-12-31 23:59:59", "2026-07-01 08:00:00")


def test_a_derived_column_from_it_is_canonical(store, write_csv):
    sid = _ingest(store, write_csv, DMY)
    res = store.add_derived_column(sid, "Parsed", "When", "dmy_datetime", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    view = store.build_view(sid, {"source_id": sid, "sort": [{"column": "Parsed", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    assert [r["cells"][1] for r in rows] == DMY_IN_TIME_ORDER
    assert [r["cells"][2] for r in rows] == [
        "2025-12-31 23:59:59", "2026-01-02 12:00:00", "2026-06-23 09:00:00",
        "2026-06-23 13:05:01", "2026-07-01 08:00:00"]


# ------------------------------------------------------- display format

def test_the_display_format_lists_agree():
    """The server refuses a format the browser does not list, and a format
    the browser lists but the server refuses cannot be saved. The new
    DD-MM-YYYY HH:MM:SS format has to be on both."""
    js = (Path(__file__).resolve().parent.parent / "static/js/tsformat.js").read_text(encoding="utf-8")
    block = re.search(r"export const TS_FORMATS = \{(.*?)\};", js, re.S).group(1)
    keys = set(re.findall(r"^\s*(\w+):", block, re.M))
    assert keys == WS.AppSettings.TS_FORMATS
    assert "dmy" in keys

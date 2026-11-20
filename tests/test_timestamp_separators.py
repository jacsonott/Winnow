"""A recognized timestamp keeps its time of day whatever stands between the
date and the time.

DATE_RE types a column as datetime on its date alone, but the readers
behind the display formats, sort-by-time, the timeframe filter, the
histogram and the Timeline (store.py's _TS_ISO_RE, _TS_US_RE and
_TS_MONTH_RE, and their twins in static/js/tsformat.js) only read a time
after a single space or a T (ISO) or spaces and commas (the others), with
a two-digit hour. "2026-06-23 @ 13:05:01", "2026-06-23 9:05:01" and
"June 23, 2026 at 5:11:22 PM" were read as their date at 00:00:00: every
value in the column was shown at midnight in every format but As stored,
and filtered as midnight too. timeparse.DATE_TIME_SEP is the separator
all three read now. tests/ui/test_timestamp_time_of_day.py holds the
browser's copy to the same answers."""

from __future__ import annotations

from datetime import datetime

import pytest

from winnow import timeparse
from winnow.store import _day_bucket, _ts_normalize

# Value as a tool writes it -> what TS_NORMALIZE makes of it.
READ_WITH_THEIR_TIME = [
    ("2026-06-23 9:05:01", "2026-06-23 09:05:01"),
    ("2026-06-23  13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23 T 13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23t13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23 @ 13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23@13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23 - 13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23 at 13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23, 13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23\t13:05:01", "2026-06-23 13:05:01"),
    ("2026-06-23 13:05:01", "2026-06-23 13:05:01"),
    ("6/23/2026 - 1:02:03 PM", "2026-06-23 13:02:03"),
    ("6/23/2026@13:02:03", "2026-06-23 13:02:03"),
    ("6/23/2026 at 1:02 PM", "2026-06-23 13:02:00"),
    ("6/23/2026 1:02:03 PM", "2026-06-23 13:02:03"),
    ("June 23, 2026 at 5:11:22 PM", "2026-06-23 17:11:22"),
    ("Jun 23, 2026, 5:11:22 PM", "2026-06-23 17:11:22"),
    ("Jun 23 2026 @ 12:34:56.123", "2026-06-23 12:34:56"),
    ("23 Jun 2026 - 12:34", "2026-06-23 12:34:00"),
    ("Jun 23 2026 1:02:03", "2026-06-23 01:02:03"),
]

# Near misses: a date and something that is not a time of day. Read as the
# date alone, as before — never as a time made up out of the rest.
READ_AS_THEIR_DATE = [
    "2026-06-23 -05:00",        # a date and a UTC offset, no time
    "2026-06-23-12:34",         # no space after the dash
    "2026-06-23 attic 1:02",    # "at" starting a word
    "2026-06-23 at noon",
    "2026-06-23 12",
]


@pytest.mark.parametrize("value, want", READ_WITH_THEIR_TIME)
def test_the_time_after_any_separator_is_read(value, want):
    assert _ts_normalize(value) == want
    assert _day_bucket(value) == want[:10]


@pytest.mark.parametrize("value", READ_AS_THEIR_DATE)
def test_a_date_with_no_time_after_it_is_still_its_date(value):
    assert _ts_normalize(value) == "2026-06-23 00:00:00"


@pytest.mark.parametrize("value, want", [
    ("2026-06-23 9:05:01", datetime(2026, 6, 23, 9, 5, 1)),
    ("2026-06-23 @ 13:05:01.25", datetime(2026, 6, 23, 13, 5, 1, 250000)),
    ("2026-06-23 at 13:05", datetime(2026, 6, 23, 13, 5)),
    ("2026-06-23 13:05:01", datetime(2026, 6, 23, 13, 5, 1)),
    ("2026-06-23 -05:00", datetime(2026, 6, 23)),
])
def test_the_delta_columns_reader_agrees(value, want):
    """parse_datetime_lenient (the duration columns' reader) shares the
    separator, so a delta between two such columns uses their times."""
    assert timeparse.parse_datetime_lenient(value) == want


def _ingest(store, write_csv, rows, name="t"):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False)["id"]


def _events(store, source_id, spec):
    view = store.build_view(source_id, {"source_id": source_id, **spec})
    return [r["cells"][1] for r in store.fetch_rows(view["view_id"], 0, 100)["rows"]]


ONE_DIGIT_HOURS = [
    ["When", "Event"],
    ["2026-06-23 10:00:00", "ten"],
    ["2026-06-23 9:05:00", "nine"],
    ["2026-06-23 23:59:59", "late"],
    ["2026-06-22 9:00:00", "day before"],
    ["2026-06-23 1:30:00", "half one"],
]


def test_a_one_digit_hour_sorts_by_time_not_as_text(store, write_csv):
    """As text, "2026-06-23 9:05:00" sorts after "2026-06-23 10:00:00"
    (":" comes after "0"), so a date-led column is not taken as already in
    time order when an hour is one digit."""
    sid = _ingest(store, write_csv, ONE_DIGIT_HOURS)
    assert store.get_source(sid)["columns"][0]["type"] == "datetime"
    in_order = ["day before", "half one", "nine", "ten", "late"]
    assert _events(store, sid, {"sort": [{"column": "When", "dir": "asc"}]}) == in_order
    assert _events(store, sid, {"sort": [{"column": "When", "dir": "desc"}]}) == in_order[::-1]
    # And it gets no sort index: its sort is by an expression none serves.
    assert not store._column_index_exists(store.get_source(sid)["table_name"], "When", "sort")


def test_the_timeframe_filter_reads_the_time_after_an_at_sign(store, write_csv):
    """Read as midnight, every row of the day fell outside a range that
    started after 00:00, and every row was inside one that ended there."""
    rows = [["When", "Event"],
            ["2026-06-23 @ 08:00:00", "morning"],
            ["2026-06-23 @ 13:05:01", "lunch"],
            ["2026-06-23 @ 22:30:00", "night"]]
    sid = _ingest(store, write_csv, rows)
    assert store.get_source(sid)["columns"][0]["type"] == "datetime"
    window = {"enabled": True, "column": "When", "start": "2026-06-23 12:00:00", "end": "2026-06-23 18:00:00"}
    assert _events(store, sid, {"time_range": window}) == ["lunch"]


def test_the_histogram_puts_each_row_at_its_time(store, write_csv):
    rows = [["When"], ["2026-06-23 @ 08:00:00"], ["2026-06-23 @ 22:30:00"]]
    sid = _ingest(store, write_csv, rows)
    view = store.build_view(sid, {"source_id": sid})
    h = store.time_histogram(view["view_id"], "When")
    assert (h["start"], h["end"]) == ("2026-06-23 08:00:00", "2026-06-23 22:30:00")

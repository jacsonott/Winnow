"""timeparse.py — the parser registry behind derived datetime columns.

Pure-function tests: every operation's happy path, the plausibility-window
auto-ranging rules, the syslog year-rollover state machine, and the
canonical-shape guarantee (every output must be accepted by store.py's
_TS_ISO_RE / TS_NORMALIZE / DAY_BUCKET — that's what makes derived columns
work with zero new regex twins to hand-sync against app.js)."""

from __future__ import annotations

from datetime import datetime

import pytest

from winnow import timeparse
from winnow.store import _TS_ISO_RE, _day_bucket, _ts_normalize


def parse(op_id, value, params=None, state=None):
    op = timeparse.OPERATIONS[op_id]
    return op["parse"](value, params or {}, state if state is not None else {})


# ----------------------------------------------------------- unix epoch

def test_unix_epoch_auto_ranges_by_plausible_year():
    # 2023-11-14T22:13:20Z in each unit — the windows are x1000 apart while
    # the plausible-year span is only ~x6.5, so exactly one unit ever fits.
    assert parse("unix_epoch", "1700000000") == "2023-11-14 22:13:20"
    assert parse("unix_epoch", "1700000000000") == "2023-11-14 22:13:20.000000"
    assert parse("unix_epoch", "1700000000000000") == "2023-11-14 22:13:20.000000"
    assert parse("unix_epoch", "1700000000000000000") == "2023-11-14 22:13:20.000000"


def test_integer_epochs_convert_exactly():
    """A float carries ~16 significant digits; a nanosecond epoch has 19
    and a WebKit value 17. Dividing as floats put the result up to a
    microsecond off either way, and a nanosecond value at the very end of
    the window rounded into 2101 and was refused outright."""
    assert parse("unix_epoch", "1700000000123456789") == "2023-11-14 22:13:20.123456"
    assert parse("unix_epoch", "1700000000123456999", {"unit": "ns"}) == "2023-11-14 22:13:20.123456"
    assert parse("unix_epoch", "4133980799999999999") == "2100-12-31 23:59:59.999999"
    assert parse("webkit_epoch", "13348668000123457") == "2024-01-02 11:20:00.123457"
    assert parse("unix_epoch", "1700000000.5") == "2023-11-14 22:13:20.500000"   # a fraction stays float
    assert parse("unix_epoch", "9" * 40) is None                                # and so does anything absurd


def test_unix_epoch_rejects_out_of_window_and_garbage():
    assert parse("unix_epoch", "620000000") is None  # 1989 — below the 1990 floor
    assert parse("unix_epoch", "-1700000000") is None
    assert parse("unix_epoch", "0") is None
    assert parse("unix_epoch", "not a number") is None
    assert parse("unix_epoch", "") is None


def test_unix_epoch_explicit_unit_overrides_auto():
    # In auto mode 1700000000 reads as seconds; forcing ms lands in 1970,
    # outside the window, so the explicit unit must fail rather than fall
    # back to the auto answer.
    assert parse("unix_epoch", "1700000000", {"unit": "ms"}) is None
    assert parse("unix_epoch", "1700000000000", {"unit": "ms"}) == "2023-11-14 22:13:20.000000"


def test_unix_epoch_float_seconds_keep_subseconds():
    assert parse("unix_epoch", "1700000000.5") == "2023-11-14 22:13:20.500000"


# ----------------------------------------------------------- FILETIME / WebKit

# 2023-11-14 22:13:20 UTC as FILETIME ticks: (unix 1700000000 + 11644473600) * 1e7
_FT = (1700000000 + 11644473600) * 10_000_000


def test_filetime_decimal_hex_and_bare_hex():
    want = "2023-11-14 22:13:20.000000"
    assert parse("windows_filetime", str(_FT)) == want
    assert parse("windows_filetime", f"0x{_FT:x}") == want
    # Bare hex is only read as hex when a letter proves it — f"{_FT:x}" is
    # 1db1751... which contains letters.
    assert parse("windows_filetime", f"{_FT:x}") == want


def test_filetime_all_digit_string_stays_decimal():
    # A 16-digit all-numeric string is a plausible decimal value; reading it
    # as hex would silently shift it by orders of magnitude.
    s = str(_FT)
    assert s.isdigit()
    assert parse("windows_filetime", s) == parse("windows_filetime", str(int(s)))


def test_filetime_overflow_zero_and_floor_fail():
    assert parse("windows_filetime", "0") is None
    assert parse("windows_filetime", str(2**63)) is None  # past year 9999 — timestomp/corruption
    assert parse("windows_filetime", "119600000000000000") is None  # ~1980, below the 1990 floor


def test_webkit_epoch():
    us = (1700000000 + 11644473600) * 1_000_000
    assert parse("webkit_epoch", str(us)) == "2023-11-14 22:13:20.000000"
    assert parse("webkit_epoch", "12345") is None  # 1601-adjacent — below floor


# ----------------------------------------------------------- syslog

def test_syslog_basic_and_padded_day():
    state = {}
    p = {"base_year": 2023}
    assert parse("syslog_bsd", "Nov 14 22:13:20", p, state) == "2023-11-14 22:13:20"
    assert parse("syslog_bsd", "Dec  5 01:02:03", p, state) == "2023-12-05 01:02:03"


def test_syslog_year_rollover_and_multi_year():
    state = {}
    p = {"base_year": 2023}
    assert parse("syslog_bsd", "Dec 31 23:59:59", p, state) == "2023-12-31 23:59:59"
    # Month decreased (12 -> 1) walking forward: the file crossed New Year.
    assert parse("syslog_bsd", "Jan  1 00:00:01", p, state) == "2024-01-01 00:00:01"
    assert parse("syslog_bsd", "Jun 15 12:00:00", p, state) == "2024-06-15 12:00:00"
    # A second wrap increments again — multi-year files work.
    assert parse("syslog_bsd", "Jan  2 08:00:00", p, state) == "2025-01-02 08:00:00"


def test_syslog_state_survives_batch_boundaries():
    # The backfill loop parses in BATCH-sized chunks but threads one state
    # dict through all of them — simulate that: same dict, separate calls.
    state = {}
    p = {"base_year": 2023}
    parse("syslog_bsd", "Dec 30 10:00:00", p, state)
    parse("syslog_bsd", "Dec 31 10:00:00", p, state)
    assert parse("syslog_bsd", "Jan  1 10:00:00", p, state).startswith("2024-")


def test_syslog_feb29_leap_vs_nonleap():
    # 2024 is a leap year; 2023 is not. Feb 29 against a non-leap resolved
    # year is genuinely ambiguous evidence — a parse failure, not a guess.
    assert parse("syslog_bsd", "Feb 29 12:00:00", {"base_year": 2024}, {}) == "2024-02-29 12:00:00"
    assert parse("syslog_bsd", "Feb 29 12:00:00", {"base_year": 2023}, {}) is None


def test_syslog_utc_offset_shift():
    # A -05:00 local log line lands 5 hours later in UTC.
    got = parse("syslog_bsd", "Nov 14 22:13:20", {"base_year": 2023, "utc_offset": "-05:00"}, {})
    assert got == "2023-11-15 03:13:20"


# ----------------------------------------------------------- ISO 8601

def test_iso_variants():
    assert parse("iso8601", "2024-01-05T13:22:01") == "2024-01-05 13:22:01"
    assert parse("iso8601", "2024-01-05 13:22:01") == "2024-01-05 13:22:01"
    assert parse("iso8601", "2024-01-05T13:22:01.123456") == "2024-01-05 13:22:01.123456"
    assert parse("iso8601", "2024-01-05T13:22:01,123") == "2024-01-05 13:22:01.123000"


def test_iso_offsets_convert_to_utc_naive_kept():
    assert parse("iso8601", "2024-01-05T13:22:01Z") == "2024-01-05 13:22:01"
    assert parse("iso8601", "2024-01-05T13:22:01+05:00") == "2024-01-05 08:22:01"
    assert parse("iso8601", "2024-01-05T13:22:01-0500") == "2024-01-05 18:22:01"
    # Naive: components kept exactly as written (no TZ math)...
    assert parse("iso8601", "2024-01-05T13:22:01") == "2024-01-05 13:22:01"
    # ...unless the analyst supplies the fixed source offset.
    assert parse("iso8601", "2024-01-05T13:22:01", {"utc_offset": "+02:00"}) == "2024-01-05 11:22:01"


def test_iso_rejects_bad_dates_and_bare_dates():
    assert parse("iso8601", "2023-02-29T00:00:00") is None  # not a leap year
    assert parse("iso8601", "2024-01-05") is None  # date-only: same rule as ingest typing
    assert parse("iso8601", "5 Jan 2024") is None


@pytest.mark.parametrize("value, want", [
    # Minute precision: the seconds were never written, so they are zero.
    ("2024-01-05T13:22", "2024-01-05 13:22:00"),
    ("2024-01-05 13:22Z", "2024-01-05 13:22:00"),
    # A fraction shorter than microseconds is that many tenths, not that
    # many microseconds; one longer is cut, not rounded.
    ("2024-01-05T13:22:01.5", "2024-01-05 13:22:01.500000"),
    ("2024-01-05T13:22:01.05", "2024-01-05 13:22:01.050000"),
    ("2024-01-05T13:22:01.1234567", "2024-01-05 13:22:01.123456"),
])
def test_iso_reads_what_was_left_out_as_zero(value, want):
    assert parse("iso8601", value) == want


@pytest.mark.parametrize("value, want", [
    ("2024-01-05", datetime(2024, 1, 5)),
    ("2024-01-05 13:22", datetime(2024, 1, 5, 13, 22)),
    ("2024-01-05T13:22:01.5", datetime(2024, 1, 5, 13, 22, 1, 500000)),
    ("2024-01-05 13:22:01.123456789", datetime(2024, 1, 5, 13, 22, 1, 123456)),
    ("1/5/2024 1:22 PM", datetime(2024, 1, 5, 13, 22)),
])
def test_lenient_parse_reads_what_was_left_out_as_zero(value, want):
    """The delta columns' reader. A part a value leaves out is zero, the
    same as the derived column that produced it would have written."""
    assert timeparse.parse_datetime_lenient(value) == want


# ----------------------------------------------------------- other text shapes

def test_dd_mmm_yyyy():
    assert parse("dd_mmm_yyyy", "05 Jan 2024 13:22:01") == "2024-01-05 13:22:01"
    assert parse("dd_mmm_yyyy", "5-Jan-2024 13:22:01") == "2024-01-05 13:22:01"
    assert parse("dd_mmm_yyyy", "05 Foo 2024 13:22:01") is None


@pytest.mark.parametrize("value, want", [
    # The value that asked for it, and the rest of the ordinal suffixes.
    ("Jul 28th 2026 01:02:22", "2026-07-28 01:02:22"),
    ("Jul 1st 2026 01:02:22", "2026-07-01 01:02:22"),
    ("Jul 2nd 2026 01:02:22", "2026-07-02 01:02:22"),
    ("Jul 3rd 2026 01:02:22", "2026-07-03 01:02:22"),
    ("Jul 21st 2026 01:02:22", "2026-07-21 01:02:22"),
    ("Jul 22nd 2026 01:02:22", "2026-07-22 01:02:22"),
    ("Jul 23rd 2026 01:02:22", "2026-07-23 01:02:22"),
    ("Jul 11th 2026 01:02:22", "2026-07-11 01:02:22"),
    ("JUL 28TH 2026 01:02:22", "2026-07-28 01:02:22"),
    # No suffix, full names, Sept., commas, 12-hour times, a fraction.
    ("Jun 23 2026 00:11:00", "2026-06-23 00:11:00"),
    ("July 28, 2026 1:02 PM", "2026-07-28 13:02:00"),
    ("Sept. 21st, 2026, 23:59:59", "2026-09-21 23:59:59"),
    ("September 1st 2026 12:00:00 a.m.", "2026-09-01 00:00:00"),
    ("Jul 28th 2026 01:02:22.5", "2026-07-28 01:02:22.500000"),
    ("Jul 28th 2026 01:02:22.123 PM", "2026-07-28 13:02:22.123000"),
    # A trailing zone is applied; trailing text that names none is left alone.
    ("Jul 28th 2026 01:02:22 +02:00", "2026-07-27 23:02:22"),
    ("Jul 28th 2026 01:02:22 UTC", "2026-07-28 01:02:22"),
    ("Jul 28th 2026 01:02:22 sshd[42]: Accepted", "2026-07-28 01:02:22"),
])
def test_mmm_dd_yyyy(value, want):
    assert parse("mmm_dd_yyyy", value) == want


@pytest.mark.parametrize("value", [
    "Jul 28 01:02:22",              # syslog: no year, and that op's job
    "Jul 28th 2026",                # date-only: same rule as ISO's
    "Feb 29th 2026 00:00:00",       # not a leap year
    "Jul 32nd 2026 01:02:22",
    "Marathon 28th 2026 01:02:22",  # only starts like a month
    "Monday 28th 2026 01:02:22",
    "28th Jul 2026 01:02:22",       # day first is dd_mmm_yyyy's shape
    "Jul 28th 2026 01:02:22 EST",   # a zone it cannot apply
])
def test_mmm_dd_yyyy_refuses(value):
    assert parse("mmm_dd_yyyy", value) is None


def test_mmm_dd_yyyy_utc_offset_shifts_naive_values():
    got = parse("mmm_dd_yyyy", "Jul 28th 2026 01:02:22", {"utc_offset": "-05:00"})
    assert got == "2026-07-28 06:02:22"


def test_detect_offers_mmm_dd_yyyy_first_for_ordinal_dates():
    got = timeparse.detect(["Jul 28th 2026 01:02:22", "Aug 1st 2026 10:00:00", "Aug 2nd 2026 9:15 PM"])
    assert got[0]["op_id"] == "mmm_dd_yyyy"
    assert got[0]["confidence"] == 1.0


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


@pytest.mark.parametrize("number, name", list(enumerate(MONTH_NAMES, 1)))
def test_every_month_name_is_its_own_month(number, name):
    """One table serves every month-name shape, and nothing else checks
    that each of its twelve entries says the right number: the tests above
    use the handful of months their bugs were found in."""
    want = f"2026-{number:02d}-09 10:11:12"
    assert parse("mmm_dd_yyyy", f"{name} 9th 2026 10:11:12") == want
    assert parse("mmm_dd_yyyy", f"{name[:3]} 9 2026 10:11:12") == want
    assert parse("dd_mmm_yyyy", f"09 {name[:3]} 2026 10:11:12") == want
    assert parse("apache_clf", f"09/{name[:3]}/2026:10:11:12 +0000") == want
    assert parse("syslog_bsd", f"{name[:3]}  9 10:11:12", {"base_year": 2026}) == want


def test_compact_ymd():
    assert parse("compact_ymd", "20240105132201") == "2024-01-05 13:22:01"
    assert parse("compact_ymd", "20241305132201") is None  # month 13
    assert parse("compact_ymd", "1700000000") is None  # wrong digit count


def test_mac_absolute_overlaps_unix_seconds():
    # 7e8 is plausible in both epochs — that's the documented ambiguity the
    # preview disambiguates. Mac: 2001+700000000s ≈ 2023; Unix: 1992.
    assert parse("mac_absolute", "700000000") == "2023-03-08 20:26:40"
    assert parse("unix_epoch", "700000000") == "1992-03-07 20:26:40"


def test_mac_absolute_reads_dates_before_2001():
    """Negative Cocoa time is before 2001 and inside the 1990 floor — a
    1990s date in a plist or Photos store. It was refused outright. Zero
    (the unset default) still is, and detection still scores negatives as
    misses, so a column of -1 sentinels is not offered as Mac time."""
    assert parse("mac_absolute", "-100000000") == "1997-10-31 14:13:20"
    assert parse("mac_absolute", "-1.5") == "2000-12-31 23:59:58.500000"
    assert parse("mac_absolute", "0") is None
    assert parse("mac_absolute", "-400000000") is None          # 1988 — below the window
    assert not [r for r in timeparse.detect(["-1", "-1", "-5"]) if r["op_id"] == "mac_absolute"]


def test_excel_serial():
    assert parse("excel_serial", "45296") == "2024-01-05 00:00:00"
    assert parse("excel_serial", "45296.5") == "2024-01-05 12:00:00"
    assert parse("excel_serial", "3") is None  # 1900 — below the window


def test_dotnet_ticks():
    # 2024-01-05 13:22:01 UTC = 638400577210000000 ticks
    assert parse("dotnet_ticks", "638400577210000000") == "2024-01-05 13:22:01.000000"
    assert parse("dotnet_ticks", "-5") is None
    assert parse("dotnet_ticks", "9999999999999999999") is None


def test_apache_clf():
    assert parse("apache_clf", "10/Oct/2000:13:55:36 -0700") == "2000-10-10 20:55:36"
    assert parse("apache_clf", "[10/Oct/2000:13:55:36 +0000]") == "2000-10-10 13:55:36"
    assert parse("apache_clf", "10/Oct/2000:13:55:36") == "2000-10-10 13:55:36"


def test_rfc2822():
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0700") == "2006-01-02 22:04:05"
    assert parse("rfc2822", "garbage") is None


def test_rfc2822_obsolete_years_follow_the_rfc():
    """RFC 5322 §4.3: a two-digit year is 19xx from 50 up and 20xx below;
    three digits are 1900 + n. email.utils splits at 68 and keeps three
    digits as written, so "55" read as 2055 — plausible, and wrong — and
    "124" as year 124, refused."""
    assert parse("rfc2822", "Wed, 02 Jan 124 15:04:05 GMT") == "2024-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 55 15:04:05 +0000") is None    # 1955, below the window
    assert parse("rfc2822", "Mon, 02 Jan 95 15:04:05 +0000") == "1995-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 06 15:04:05 -0700") == "2006-01-02 22:04:05"
    assert parse("rfc2822", "02 Jan 06 15:04 GMT") == "2006-01-02 15:04:00"


def test_rfc2822_zones_the_rfc_defines_are_converted():
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 GMT") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 UT") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 UTC") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 Z") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 EST") == "2006-01-02 20:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 EDT") == "2006-01-02 19:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 CST") == "2006-01-02 21:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 CDT") == "2006-01-02 20:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 MST") == "2006-01-02 22:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 MDT") == "2006-01-02 21:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 PST") == "2006-01-02 23:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 PDT") == "2006-01-02 22:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 +0530") == "2006-01-02 09:34:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 +0000") == "2006-01-02 15:04:05"
    # A comment after the zone is a comment; the zone still counts.
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0800 (PST)") == "2006-01-02 23:04:05"
    # A zone written with the offset as an explicit param beside it: the
    # value's own zone wins, as everywhere else.
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 EST", {"utc_offset": "+02:00"}) == "2006-01-02 20:04:05"


def test_rfc2822_zone_it_does_not_know_is_a_failure_not_utc():
    """"15:04:05 CET" came out 15:04:05 — the wall-clock time read as UTC,
    an hour off, counted as a success — while "15:04:05 EST" was
    converted. A zone that cannot be applied is a value that does not
    parse: NULL, and counted, so it shows under "unparsed rows"."""
    for zone in ("CET", "CEST", "IST", "BST", "JST", "AEST", "cet", "Europe/Berlin",
                 "AST", "ADT",          # Atlantic, or Arabia (+03:00) — can't tell
                 "A", "M", "N", "Y",    # military letters: RFC 822 had their signs backwards
                 "+01:00x", "+05", "+1500", "+0560", "(PDT)"):
        assert parse("rfc2822", f"Mon, 02 Jan 2006 15:04:05 {zone}") is None, zone
        assert parse("rfc2822", f"Mon, 02 Jan 2006 15:04:05 {zone}", {"utc_offset": "+01:00"}) is None, zone
    # date(1)'s layout puts the zone before the year — same rule there.
    assert parse("rfc2822", "Mon Jan  2 15:04:05 CET 2006") is None
    assert parse("rfc2822", "Mon Jan  2 15:04:05 UTC 2006") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon Jan  2 15:04:05 PST 2006") == "2006-01-02 23:04:05"


def test_rfc2822_no_zone_is_local_time_and_minus_zero_is_utc():
    """No zone at all is naive local time — kept as written, or shifted by
    the analyst's fixed offset. "-0000" is not that: RFC 5322 §3.3 says
    the time IS in UT and only the sender's own zone is unknown, so the
    offset param (for values that carry no zone) must not move it."""
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05", {"utc_offset": "+02:00"}) == "2006-01-02 13:04:05"
    assert parse("rfc2822", "Mon Jan  2 15:04:05 2006", {"utc_offset": "-01:00"}) == "2006-01-02 16:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0000") == "2006-01-02 15:04:05"
    assert parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0000", {"utc_offset": "+02:00"}) == "2006-01-02 15:04:05"


def test_us_datetime_ampm_rules():
    assert parse("us_datetime", "1/5/2024 1:22:01 PM") == "2024-01-05 13:22:01"
    assert parse("us_datetime", "1/5/2024 12:00:00 AM") == "2024-01-05 00:00:00"
    assert parse("us_datetime", "1/5/2024 12:00:00 PM") == "2024-01-05 12:00:00"
    assert parse("us_datetime", "1/5/2024 13:22") == "2024-01-05 13:22:00"
    assert parse("us_datetime", "13/45/2024 10:00:00") is None


# ----------------------------------------------------------- duration delta

def test_duration_delta_pair():
    pp = timeparse.OPERATIONS["duration_delta"]["parse_pair"]
    assert pp("2024-01-05 13:22:01", "2024-01-05 13:00:00", {}) == "1321.000000"
    assert pp("2024-01-05 13:00:00", "2024-01-05 13:22:01", {}) == "-1321.000000"
    assert pp("2024-01-05 13:22:01.500000", "2024-01-05 13:22:01", {}) == "0.500000"
    assert pp("2024-01-05 13:22:01", "not a time", {}) is None
    assert pp(None, "2024-01-05 13:22:01", {}) is None


def test_duration_delta_reads_a_missing_seconds_field_as_zero():
    """Minute-precision times are routine (an ISO `13:22`, a US export's
    `1:22 PM`). Their seconds are zero; read as anything else, every delta
    against a time that has seconds is out by that much."""
    pp = timeparse.OPERATIONS["duration_delta"]["parse_pair"]
    assert pp("2024-01-05 13:22", "2024-01-05 13:21:00", {}) == "60.000000"
    assert pp("1/5/2024 1:22 PM", "2024-01-05 13:21:00", {}) == "60.000000"


# ----------------------------------------------------------- detection

def test_detect_ranks_by_success_rate_registry_order_breaks_ties():
    got = timeparse.detect(["1700000000", "1700000100", "1700000200"])
    ids = [r["op_id"] for r in got]
    # unix_epoch parses 3/3; nothing else should outrank it (mac_absolute
    # can't parse these — 1.7e9s past 2001 is year 2054, plausible! — so it
    # ties, and registry order puts unix first).
    assert ids[0] == "unix_epoch"
    assert got[0]["confidence"] == 1.0


def test_detect_puts_the_best_guess_first_whatever_the_registry_order():
    """Two ISO values and an epoch: ISO reads two of three, the epoch ops
    one. unix_epoch is registered before iso8601, so a list left in
    registry order would offer the weaker guess first."""
    got = timeparse.detect(["2024-01-05T13:22:01", "2024-01-05 13:22:01", "1704460921"])
    assert got[0]["op_id"] == "iso8601"
    confidences = [r["confidence"] for r in got]
    assert confidences == sorted(confidences, reverse=True)
    assert len(set(confidences)) > 1


def test_detect_syslog_uses_provisional_year():
    got = timeparse.detect(["Jan  5 13:22:01", "Feb  1 08:00:00"])
    assert got[0]["op_id"] == "syslog_bsd"
    assert got[0]["confidence"] == 1.0


def test_detect_hides_multi_input_ops_and_zero_confidence():
    got = timeparse.detect(["completely", "unparseable", "text"])
    assert got == []
    got = timeparse.detect(["2024-01-05T10:00:00"])
    assert all(r["op_id"] != "duration_delta" for r in got)


def test_a_counter_is_not_offered_as_a_timestamp():
    """EvtxECmd's RecordNumber — 1, 2, 3… — was suggested as "Mac absolute
    (Cocoa) time" with every value parsing: Mac time's epoch is inside the
    plausible window, so each small number is a moment just after
    2001-01-01. Excel serial dates catch whole numbers one range up
    (record ids, ephemeral ports). Detection offers neither now."""
    record_number = [str(i) for i in range(1, 201)]
    assert timeparse.detect(record_number) == []
    for sample in ([str(100000 + i) for i in range(200)],          # EventRecordId
                   ["4624", "4625", "4688", "1"],                  # EventId
                   [str(49152 + 7 * i) for i in range(200)],       # ephemeral ports
                   ["0.25", "12.5", "3600.75"]):                   # a duration in seconds
        assert timeparse.detect(sample) == [], sample

    # Only the guess changed. Picked by hand, both still read every value.
    assert parse("mac_absolute", "1") == "2001-01-01 00:00:01"
    assert parse("excel_serial", "49152") == "2034-07-27 00:00:00"

    # And real values are still offered: Cocoa time from this decade,
    # with or without a fraction, and an Excel serial with a time of day.
    for sample in (["726153721", "726153781"], ["726153721.5"]):
        assert "mac_absolute" in [r["op_id"] for r in timeparse.detect(sample)], sample
    assert timeparse.detect(["45296.5576", "45297.25"])[0]["op_id"] == "excel_serial"


# ----------------------------------------------------------- params

def test_validate_params():
    assert timeparse.validate_params("unix_epoch", {}) == {"unit": "auto"}
    assert timeparse.validate_params("unix_epoch", {"unit": "ms"}) == {"unit": "ms"}
    with pytest.raises(ValueError):
        timeparse.validate_params("unix_epoch", {"unit": "fortnights"})
    with pytest.raises(ValueError):
        timeparse.validate_params("syslog_bsd", {})  # base_year required
    assert timeparse.validate_params("syslog_bsd", {"base_year": "2023"}) == {"base_year": 2023}
    with pytest.raises(ValueError):
        timeparse.validate_params("syslog_bsd", {"base_year": 1800})
    with pytest.raises(ValueError):
        timeparse.validate_params("iso8601", {"utc_offset": "banana"})
    with pytest.raises(ValueError):
        timeparse.validate_params("nope", {})


# ----------------------------------------------------------- canonical shape

def test_every_output_matches_the_shapes_store_already_recognizes():
    """The regex-twin guarantee: derived values must be accepted by the
    existing store.py machinery (and therefore app.js's parseTimestamp,
    hand-synced to the same shapes) with no parser changes anywhere."""
    outputs = [
        parse("unix_epoch", "1700000000"),
        parse("unix_epoch", "1700000000000"),
        parse("windows_filetime", str(_FT)),
        parse("syslog_bsd", "Nov 14 22:13:20", {"base_year": 2023}, {}),
        parse("iso8601", "2024-01-05T13:22:01.123456+05:00"),
        parse("dd_mmm_yyyy", "05 Jan 2024 13:22:01"),
        parse("mmm_dd_yyyy", "Jul 28th 2026 01:02:22"),
        parse("mmm_dd_yyyy", "Jul 28th 2026 01:02:22.5 PM"),
        parse("mac_absolute", "700000000"),
        parse("excel_serial", "45296.5"),
        parse("apache_clf", "10/Oct/2000:13:55:36 -0700"),
        parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0700"),
        parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 EST"),
        parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0000"),
        parse("us_datetime", "1/5/2024 1:22:01 PM"),
        parse("compact_ymd", "20240105132201"),
    ]
    for out in outputs:
        assert out is not None
        assert _TS_ISO_RE.match(out), out
        assert _ts_normalize(out) == out[:19], out
        assert _day_bucket(out) == out[:10], out



def test_a_year_below_1000_keeps_four_digits():
    """strftime's %Y on glibc drops the zero padding below year 1000, so
    a string op holding "0999-…" wrote "999-06-01 00:00:00": not a shape
    the store recognises, and as text it sorted after every 2024 date."""
    outs = [
        parse("iso8601", "0999-06-01T00:00:00"),
        parse("dd_mmm_yyyy", "05 Jan 0999 13:22:01"),
        parse("apache_clf", "05/Jan/0999:13:22:01"),
        parse("us_datetime", "1/5/0999 1:22 PM"),
        parse("iso8601", "0001-01-01T00:00:00.5"),
    ]
    assert outs == ["0999-06-01 00:00:00", "0999-01-05 13:22:01", "0999-01-05 13:22:01",
                    "0999-01-05 13:22:00", "0001-01-01 00:00:00.500000"]
    for out in outs:
        assert _TS_ISO_RE.match(out), out
        assert _ts_normalize(out) == out[:19], out
        assert out < "2024-01-01 00:00:00"


# ------------------------------------------ month-name native recognition

def test_month_name_timestamps_normalize_natively():
    """"JUN 23 2026 00:11:00" arrived in real tooling output and wasn't
    recognized — the third native family (no derived column needed):
    name-first and day-first, abbreviated and full months, optional comma,
    optional 12-hour time, case-insensitive."""
    assert _ts_normalize("JUN 23 2026 00:11:00") == "2026-06-23 00:11:00"
    assert _ts_normalize("23 Jun 2026") == "2026-06-23 00:00:00"
    assert _ts_normalize("June 23, 2026 5:11 PM") == "2026-06-23 17:11:00"
    assert _ts_normalize("23 September 2026 12:00 AM") == "2026-09-23 00:00:00"
    assert _ts_normalize("Foo 23 2026") is None      # not a month
    assert _ts_normalize("Monday 23 2026") is None   # a weekday isn't either
    assert _day_bucket("JUN 23 2026 00:11:00") == "2026-06-23"
    assert _day_bucket("23 Jun 2026 08:00:00") == "2026-06-23"


def test_ordinal_days_normalize_natively():
    """"Jul 28th 2026 01:02:22" — the month-name family with the day's
    ordinal suffix, either order, any suffix, any case."""
    assert _ts_normalize("Jul 28th 2026 01:02:22") == "2026-07-28 01:02:22"
    assert _ts_normalize("July 1st, 2026 5:11 PM") == "2026-07-01 17:11:00"
    assert _ts_normalize("Aug 2ND 2026") == "2026-08-02 00:00:00"
    assert _ts_normalize("3rd September 2026 12:00 AM") == "2026-09-03 00:00:00"
    assert _ts_normalize("Jul 28xx 2026") is None    # not a suffix
    assert _day_bucket("Jul 28th 2026 01:02:22") == "2026-07-28"
    assert _day_bucket("23rd Jun 2026") == "2026-06-23"


def test_month_name_columns_type_as_datetime_at_ingest():
    from winnow.store import DATE_RE

    assert DATE_RE.match("JUN 23 2026 00:11:00")
    assert DATE_RE.match("Jul 28th 2026 01:02:22")
    assert DATE_RE.match("28th Jul 2026")
    assert DATE_RE.match("23 June, 2026")
    assert not DATE_RE.match("word salad 2026")
    assert not DATE_RE.match("mayhem 5 2026")  # 'may' must be the whole word


@pytest.mark.parametrize("bad", ["inf", "-inf", "Infinity", "nan", "1e400"])
def test_webkit_ignores_values_that_are_not_finite(bad):
    """int(float("inf")) raises OverflowError, which is not a ValueError —
    it escaped the guard and 500ed the column-suggestion route on any
    column holding one."""
    assert timeparse._parse_webkit(bad, {}, {}) is None


@pytest.mark.parametrize("op", ["_parse_unix", "_parse_mac", "_parse_excel", "_parse_webkit"])
@pytest.mark.parametrize("bad", ["inf", "-inf", "Infinity", "nan", "1e400", "-1e400"])
def test_no_numeric_parser_raises_on_a_value_that_is_not_finite(op, bad):
    assert getattr(timeparse, op)(bad, {}, {}) is None


def test_detecting_a_column_of_infinities_suggests_nothing_rather_than_raising():
    """What 500ed the column-suggestion route: detect() runs every parser
    over the samples, so one unguarded conversion took the whole request
    down with an OverflowError."""
    assert timeparse.detect(["inf", "nan", "1e400", "inf"]) == []


# ------------------------------------------- 12-hour times and trailing zones

@pytest.mark.parametrize("value", [
    "1/5/2024 1:22:01.123 PM", "1/5/2024 1:22:01.1234567 PM", "1/5/2024 1:22:01 Pm",
    "1/5/2024 1:22:01 p.m.", "1/5/2024 1:22:01 P.M.", "1/5/2024 1:22:01pm",
])
def test_an_afternoon_is_an_afternoon_however_it_is_written(value):
    """The AM/PM group followed the seconds directly, so a fraction in
    between — or "Pm", or "p.m." — left it unmatched and the time read as
    morning: 12 hours early, in the derived column and in the Timeline's
    and timeframe filter's TS_NORMALIZE alike."""
    assert parse("us_datetime", value) == "2024-01-05 13:22:01"
    assert _ts_normalize(value) == "2024-01-05 13:22:01"
    assert _day_bucket(value) == "2024-01-05"
    assert timeparse.parse_datetime_lenient(value).hour == 13


def test_twelve_hour_edges_and_words_that_only_start_like_one():
    assert parse("us_datetime", "1/5/2024 12:30:00 a.m.") == "2024-01-05 00:30:00"
    assert parse("us_datetime", "1/5/2024 12:30:00.5 PM") == "2024-01-05 12:30:00"
    assert _ts_normalize("June 23, 2026 5:11:00.250 p.m.") == "2026-06-23 17:11:00"
    # "Am…" is a word, not a meridiem.
    assert _ts_normalize("1/5/2024 1:22:01 Amsterdam") == "2024-01-05 01:22:01"


@pytest.mark.parametrize("value, want", [
    ("2024-01-05 13:22:01+05", "2024-01-05 08:22:01"),                  # PostgreSQL text
    ("2024-01-05 13:22:01.123+05", "2024-01-05 08:22:01.123000"),
    ("2024-01-05T13:22:01.1234567890+05:00", "2024-01-05 08:22:01.123456"),
    ("2024-01-05T13:22:01−05:00", "2024-01-05 18:22:01"),         # U+2212 minus
    ("2024-01-05T13:22:01 UTC", "2024-01-05 13:22:01"),
    ("2024-01-05 13:22:01 GMT+02:00", "2024-01-05 11:22:01"),
    ("2024-01-05T13:22:01+01:00[Europe/Paris]", "2024-01-05 12:22:01"),
    ("2024-01-05 13:22:01 INFO service started", "2024-01-05 13:22:01"),  # not a zone: kept
])
def test_iso_offsets_it_can_apply(value, want):
    assert parse("iso8601", value) == want
    # An explicit zone beats the analyst's fixed offset, as Z/±HH:MM always did.
    if "INFO" not in value:
        assert parse("iso8601", value, {"utc_offset": "+09:00"}) == want


@pytest.mark.parametrize("op, value", [
    ("iso8601", "2024-01-05T13:22:01 EST"),
    ("iso8601", "2024-01-05T13:22:01 America/New_York"),
    ("iso8601", "2024-01-05T13:22:01+25:00"),
    ("iso8601", "2024-01-05T13:22:01+5:3"),
    ("dd_mmm_yyyy", "05 Jan 2024 13:22:01 PST"),
    ("mmm_dd_yyyy", "Jan 5th 2024 1:22:01 PM Europe/Paris"),
    ("us_datetime", "1/5/2024 1:22:01 PM CEST"),
])
def test_a_zone_it_cannot_apply_is_refused_not_dropped(op, value):
    """Dropping it read the wall-clock time as if it were already UTC (or
    whatever the analyst's fixed offset says) — hours off, and nothing in
    the column says so. A parse failure is counted and shown."""
    assert parse(op, value) is None


@pytest.mark.parametrize("op, value, want", [
    ("dd_mmm_yyyy", "05 Jan 2024 13:22:01 +0200", "2024-01-05 11:22:01"),
    ("dd_mmm_yyyy", "05 Jan 2024 13:22:01.123 -05:00", "2024-01-05 18:22:01"),
    ("dd_mmm_yyyy", "05 Jan 2024 13:22:01 GMT", "2024-01-05 13:22:01"),
    ("us_datetime", "1/5/2024 1:22:01 PM +02:00", "2024-01-05 11:22:01"),
    ("us_datetime", "1/5/2024 13:22:01 -0500", "2024-01-05 18:22:01"),
    ("us_datetime", "1/5/2024 1:22:01 PM UTC", "2024-01-05 13:22:01"),
])
def test_dd_mmm_and_us_apply_a_trailing_offset(op, value, want):
    assert parse(op, value) == want
    assert parse(op, value, {"utc_offset": "+09:00"}) == want
    assert _TS_ISO_RE.match(want) and _ts_normalize(want) == want[:19]


# ----------------------------------------------------------- totality
#
# A parser that raises on one value used to fail the whole backfill of the
# column it was building, leave it 'building' for good and, on a re-derive,
# leave the batches before the bad row under the new params and the rest
# under the old; detect() raising made the suggestion and detect routes
# 500 for the whole table. Each of these came out of real evidence.

@pytest.mark.parametrize("op_id,value,params", [
    # An offset no clock has, inside the value: one cell's problem, not
    # the analyst's param (validate_params still refuses THAT loudly).
    ("iso8601", "2024-01-05T13:22:01+15:00", {}),
    ("iso8601", "2024-01-05T13:22:01+99:99", {}),
    ("apache_clf", "05/Jan/2024:13:22:01 +9999", {}),
    ("apache_clf", "05/Jan/2024:13:22:01 +1500", {}),
    # .NET's DateTime.MinValue / MaxValue, which exports write for "never",
    # converted to UTC past either end of datetime's range.
    ("iso8601", "0001-01-01T00:00:00+05:00", {}),
    ("iso8601", "9999-12-31T23:59:59-05:00", {}),
    ("iso8601", "0001-01-01T00:00:00", {"utc_offset": "+02:00"}),
    ("apache_clf", "01/Jan/0001:00:00:00 +0100", {}),
    ("us_datetime", "1/1/0001 12:00:00 AM", {"utc_offset": "+01:00"}),
    ("us_datetime", "12/31/9999 23:59:59", {"utc_offset": "-01:00"}),
    ("dd_mmm_yyyy", "01 Jan 0001 00:00:00", {"utc_offset": "+14:00"}),
    ("mmm_dd_yyyy", "Jan 1st 0001 00:00:00", {"utc_offset": "+14:00"}),
    ("mmm_dd_yyyy", "Dec 31st 9999 23:59:59 -01:00", {}),
    # Digits that are not decimal digits, and decimal strings int() refuses.
    ("windows_filetime", "²", {}),
    ("windows_filetime", "¹²³", {}),
    ("windows_filetime", "9" * 5000, {}),
    ("windows_filetime", "0" * 5000, {}),
    ("dotnet_ticks", "²", {}),
    ("dotnet_ticks", "1" * 5000, {}),
], ids=lambda v: f"{v[0]}x{len(v)}" if isinstance(v, str) and len(v) > 40 else None)
def test_a_value_a_parser_cannot_read_is_none_not_an_exception(op_id, value, params):
    assert parse(op_id, value, params) is None


def test_the_offset_param_is_still_refused_loudly():
    """The in-value offset became a quiet failure; the analyst's own
    utc_offset must not — a typo there would shift nothing silently."""
    with pytest.raises(ValueError):
        timeparse.validate_params("iso8601", {"utc_offset": "+15:00"})


def test_a_year_before_1000_is_still_canonical():
    """strftime's %Y drops the zero padding below year 1000 on glibc
    ("999-01-01", which sorts after "2024"); _fmt writes the fields out."""
    out = parse("iso8601", "0999-01-01T00:00:00")
    assert out == "0999-01-01 00:00:00"
    assert _TS_ISO_RE.match(out)


_HOSTILE = [
    "", " ", "\x00", "-1", "nan", "inf", "1e400", "9" * 400, "0x" + "f" * 400, "²", "¹²³",
    "١٧٠٠٠٠٠٠٠٠", "１７００００００００", "{" * 1000, "[1,2", '{"a": 1e999}',
    '{"a":"trunc \\ud83d"}', '{"a":{"\\udc00k":"v"}}', "<a>", "<a></b>",
    "2024-01-05T13:22:01+15:00", "0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00",
    "05/Jan/2024:13:22:01 +9999", "01/Jan/0001:00:00:00 +0100", "1/1/0001 0:00",
    "12/31/9999 23:59:59", "Mon, 02 Jan 2006 15:04:05 +9999", "Jan 32 00:00:00",
    "3155378976000000000", "-13300000000000000",
]


def _params_for(op):
    params = timeparse.detect_defaults(op)
    params.update({
        "json_field": {"path": "$.a"}, "xml_field": {"path": "a"},
        "regex_extract": {"pattern": r"(\d+)"}, "duration_delta": {"other_column": "b"},
        "coalesce": {"extra_columns": ["b"]}, "row_json": {"extra_columns": ["b"]},
    }.get(op["id"], {}))
    out = [params]
    if any(p["name"] == "utc_offset" for p in op["params"]):
        out += [dict(params, utc_offset="+14:00"), dict(params, utc_offset="-14:00")]
    return out


@pytest.mark.parametrize("op_id", sorted(timeparse.OPERATIONS))
def test_every_registered_operation_is_total_over_hostile_values(op_id):
    """The class, not the instances above: every op the registry holds
    returns text SQLite can store, or None, for every value — including
    one under each extreme utc_offset."""
    op = timeparse.OPERATIONS[op_id]
    for params in _params_for(op):
        for value in _HOSTILE:
            state = {"map": {"1": "one"}} if op_id == "lookup" else {}
            if op["multi_input"]:
                state["inputs"] = ["a", "b"]
                out = op["parse_multi"]([value, value], params, state)
            else:
                out = op["parse"](value, params, state)
            assert out is None or isinstance(out, str), (params, value, out)
            if out is not None:
                out.encode("utf-8")   # a lone surrogate cannot be stored


def test_detect_ranks_around_a_parser_that_raises(monkeypatch):
    """Registered ops need not be ours (nothing stops a plugin registering
    into the same table). One whose parse raises ranks as reading nothing;
    one whose own detect raises is left out — neither takes the ranking
    down."""
    def boom(value, params, state):
        raise RuntimeError("not today")

    def bad_detect(samples, params):
        raise RuntimeError("nor today")

    for op in ({"id": "t_boom_parse", "label": "boom", "parse": boom},
               {"id": "t_boom_detect", "label": "boom", "parse": boom, "detect": bad_detect}):
        monkeypatch.setitem(timeparse.OPERATIONS, op["id"], op)
        timeparse.register_op(op)
    got = timeparse.detect(["1700000000", "1700000060"])
    assert got[0]["op_id"] == "unix_epoch"
    assert not {"t_boom_parse", "t_boom_detect"} & {r["op_id"] for r in got}
    assert timeparse.parse_or_none(timeparse.OPERATIONS["t_boom_parse"], "x", {}, {}) is None

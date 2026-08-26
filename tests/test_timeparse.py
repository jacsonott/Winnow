"""timeparse.py — the parser registry behind derived datetime columns.

Pure-function tests: every operation's happy path, the plausibility-window
auto-ranging rules, the syslog year-rollover state machine, and the
canonical-shape guarantee (every output must be accepted by store.py's
_TS_ISO_RE / TS_NORMALIZE / DAY_BUCKET — that's what makes derived columns
work with zero new regex twins to hand-sync against app.js)."""

from __future__ import annotations

import pytest

import timeparse
from store import _TS_ISO_RE, _day_bucket, _ts_normalize


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


# ----------------------------------------------------------- other text shapes

def test_dd_mmm_yyyy():
    assert parse("dd_mmm_yyyy", "05 Jan 2024 13:22:01") == "2024-01-05 13:22:01"
    assert parse("dd_mmm_yyyy", "5-Jan-2024 13:22:01") == "2024-01-05 13:22:01"
    assert parse("dd_mmm_yyyy", "05 Foo 2024 13:22:01") is None


def test_compact_ymd():
    assert parse("compact_ymd", "20240105132201") == "2024-01-05 13:22:01"
    assert parse("compact_ymd", "20241305132201") is None  # month 13
    assert parse("compact_ymd", "1700000000") is None  # wrong digit count


def test_mac_absolute_overlaps_unix_seconds():
    # 7e8 is plausible in both epochs — that's the documented ambiguity the
    # preview disambiguates. Mac: 2001+700000000s ≈ 2023; Unix: 1992.
    assert parse("mac_absolute", "700000000") == "2023-03-08 20:26:40"
    assert parse("unix_epoch", "700000000") == "1992-03-07 20:26:40"


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


# ----------------------------------------------------------- detection

def test_detect_ranks_by_success_rate_registry_order_breaks_ties():
    got = timeparse.detect(["1700000000", "1700000100", "1700000200"])
    ids = [r["op_id"] for r in got]
    # unix_epoch parses 3/3; nothing else should outrank it (mac_absolute
    # can't parse these — 1.7e9s past 2001 is year 2054, plausible! — so it
    # ties, and registry order puts unix first).
    assert ids[0] == "unix_epoch"
    assert got[0]["confidence"] == 1.0


def test_detect_syslog_uses_provisional_year():
    got = timeparse.detect(["Jan  5 13:22:01", "Feb  1 08:00:00"])
    assert got[0]["op_id"] == "syslog_bsd"
    assert got[0]["confidence"] == 1.0


def test_detect_hides_two_input_ops_and_zero_confidence():
    got = timeparse.detect(["completely", "unparseable", "text"])
    assert got == []
    got = timeparse.detect(["2024-01-05T10:00:00"])
    assert all(r["op_id"] != "duration_delta" for r in got)


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
        parse("mac_absolute", "700000000"),
        parse("excel_serial", "45296.5"),
        parse("apache_clf", "10/Oct/2000:13:55:36 -0700"),
        parse("rfc2822", "Mon, 02 Jan 2006 15:04:05 -0700"),
        parse("us_datetime", "1/5/2024 1:22:01 PM"),
        parse("compact_ymd", "20240105132201"),
    ]
    for out in outputs:
        assert out is not None
        assert _TS_ISO_RE.match(out), out
        assert _ts_normalize(out) == out[:19], out
        assert _day_bucket(out) == out[:10], out


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


def test_month_name_columns_type_as_datetime_at_ingest():
    from store import DATE_RE

    assert DATE_RE.match("JUN 23 2026 00:11:00")
    assert DATE_RE.match("23 June, 2026")
    assert not DATE_RE.match("word salad 2026")
    assert not DATE_RE.match("mayhem 5 2026")  # 'may' must be the whole word

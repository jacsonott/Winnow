"""Day-first dashed timestamps, dd-mm-yyyy hh:mm:ss, in the browser.

tests/test_timestamp_dmy.py covers the server (import typing, sort,
timeframe, histogram, the derived operation). This holds what the grid
paints for such a column, holds parseTimestamp to TS_NORMALIZE value for
value on the new shape, and checks the new DD-MM-YYYY HH:MM:SS display
format writes every recognized shape day first."""

import pytest

from winnow.store import _ts_normalize

pytestmark = pytest.mark.ui

# What the top row shows under a column, read off the DOM.
_TOP = """(name) => {
  const ci = __winnow.visibleCols().indexOf(name);
  const row = document.querySelector('#rows .row[data-pos="0"]:not(.pending)');
  const cell = row && row.querySelector(`.cell[data-col="${ci}"]`);
  return cell ? cell.textContent : null;
}"""


@pytest.fixture
def dmy_table(page, imported, tmp_path):
    f = tmp_path / "dmy.csv"
    f.write_text("When,Event\n"
                 "23-06-2026 13:05:01,afternoon\n"
                 "01-07-2026 08:00:00,july\n"
                 "5-1-2026 9:05 PM,january\n", encoding="utf-8")
    sid = imported({"path": str(f), "name": "dmy.csv", "kind": "csv"})[0]
    page.evaluate("() => __winnow.loadSources()")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector('#rows .row[data-pos="0"]:not(.pending)')
    try:
        yield sid
    finally:
        page.evaluate("(id) => __winnow.api('/api/source/' + id, { method: 'DELETE' })"
                      ".then(() => __winnow.loadSources())", sid)


def test_a_dd_mm_yyyy_column_is_a_timestamp_and_shown_day_first_read(page, dmy_table):
    types = page.evaluate("() => Object.fromEntries(__winnow.S.columns.map((c) => [c.name, c.type]))")
    assert types["When"] == "datetime"
    assert page.evaluate("() => __winnow.tsFormatFor('When')") == "iso"
    # The table opens sorted by its timestamp, so the earliest row is on
    # top: January, which as text ("5-1-2026") would have sorted last.
    page.wait_for_function(f"([n, v]) => ({_TOP})(n) === v", arg=["When", "2026-01-05 21:05:00"],
                           timeout=10_000)


def test_the_browser_reads_each_value_as_the_server_does(page):
    corpus = [
        "23-06-2026 13:05:01", "01-07-2026 08:00:00", "5-1-2026 9:05:01", "05-01-2026 1:05 PM",
        "05-01-2026 12:30:00 a.m.", "23-06-2026 13:05", "23-06-2026 13:05:01.123456",
        "23-06-2026T13:05:01", "23-06-2026 @ 13:05:01", "23-06-2026, 13:05:01", "23-06-2026",
        "5/1/2026 10:00:00",
        # Not this shape on either side.
        "32-01-2026 10:00:00", "12-25-2026 10:00:00", "01-13-2026 10:00:00", "23-06-20261 10:00:00",
        "23-06-26 10:00:00",
    ]
    shown = page.evaluate("(vs) => vs.map((v) => __winnow.formatTimestamp(v, 'iso'))", corpus)
    for value, got in zip(corpus, shown):
        assert got == (_ts_normalize(value) or value), repr(value)


def test_the_dd_mm_yyyy_display_format_writes_day_first(page):
    cases = {
        "2026-06-23 13:05:01.5": "23-06-2026 13:05:01",
        "6/23/2026 1:05:01 PM": "23-06-2026 13:05:01",
        "June 23, 2026 at 1:05:01 PM": "23-06-2026 13:05:01",
        "5-1-2026 9:05 PM": "05-01-2026 21:05:00",
        "not a timestamp": "not a timestamp",
    }
    got = page.evaluate("(vs) => vs.map((v) => __winnow.formatTimestamp(v, 'dmy'))", list(cases))
    assert got == list(cases.values())


def test_the_case_can_show_every_timestamp_day_first(page, api, dmy_table):
    """The case's own format (Case settings…) set to DD-MM-YYYY HH:MM:SS:
    the server accepts it, and the grid shows the column in it."""
    before = page.evaluate("() => __winnow.S.caseSettings.ts_format || ''")
    try:
        saved = api("/api/case_settings", "POST", {"ts_format": "dmy"})
        assert saved["ts_format"] == "dmy"
        page.evaluate("(s) => { __winnow.S.caseSettings = s; __winnow.render(); }", saved)
        assert page.evaluate("() => __winnow.tsFormatFor('When')") == "dmy"
        page.wait_for_function(f"([n, v]) => ({_TOP})(n) === v", arg=["When", "05-01-2026 21:05:00"],
                               timeout=10_000)
    finally:
        restored = api("/api/case_settings", "POST", {"ts_format": before})
        page.evaluate("(s) => { __winnow.S.caseSettings = s; __winnow.render(); }", restored)

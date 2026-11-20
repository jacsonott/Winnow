"""A reformatted timestamp column showed 00:00:00 on every row while As
stored showed the real times. The column was typed datetime on its date
alone (DATE_RE), but the display formats only read a time after a single
space or a T, with a two-digit hour, so "2026-06-23 @ 13:05:01",
"2026-06-23 9:05:01" and "June 23, 2026 at 5:11:22 PM" were each shown
as their date at midnight. tests/test_timestamp_separators.py covers the
server's side (sort, timeframe, histogram); this holds what the grid
paints, and holds parseTimestamp to TS_NORMALIZE value for value."""

import pytest

from winnow.store import _ts_normalize

pytestmark = pytest.mark.ui

# Column -> (as stored, as shown under "YYYY-MM-DD HH:MM:SS").
SHAPES = {
    "At": ("2026-06-23 @ 13:05:01", "2026-06-23 13:05:01"),
    "Dash": ("2026-06-23 - 13:05:01", "2026-06-23 13:05:01"),
    "OneDigit": ("2026-06-23 9:05:01", "2026-06-23 09:05:01"),
    "Spelled": ("June 23, 2026 at 5:11:22 PM", "2026-06-23 17:11:22"),
    "UsAt": ("6/23/2026 @ 1:02:03 PM", "2026-06-23 13:02:03"),
}

# What the top row shows under a column, read off the DOM.
_TOP = """(name) => {
  const ci = __winnow.visibleCols().indexOf(name);
  const row = document.querySelector('#rows .row[data-pos="0"]:not(.pending)');
  const cell = row && row.querySelector(`.cell[data-col="${ci}"]`);
  return cell ? cell.textContent : null;
}"""


@pytest.fixture
def separated_table(page, imported, tmp_path):
    f = tmp_path / "separators.csv"
    names = list(SHAPES)
    lines = [",".join(names + ["Event"])]
    for i in range(3):
        lines.append(",".join(f'"{SHAPES[n][0]}"' for n in names) + f",e{i}")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sid = imported({"path": str(f), "name": "separators.csv", "kind": "csv"})[0]
    page.evaluate("() => __winnow.loadSources()")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector('#rows .row[data-pos="0"]:not(.pending)')
    try:
        yield sid
    finally:
        page.evaluate("(id) => __winnow.api('/api/source/' + id, { method: 'DELETE' })"
                      ".then(() => __winnow.loadSources())", sid)


def test_a_reformatted_column_shows_the_time_not_midnight(page, separated_table):
    types = page.evaluate("() => Object.fromEntries(__winnow.S.columns.map((c) => [c.name, c.type]))")
    for name in SHAPES:
        assert types[name] == "datetime", name
        assert page.evaluate("(n) => __winnow.tsFormatFor(n)", name) == "iso"
    for name, (_, shown) in SHAPES.items():
        page.wait_for_function(f"([n, v]) => ({_TOP})(n) === v", arg=[name, shown], timeout=10_000)


def test_the_browser_reads_each_value_as_the_server_does(page):
    """Display, the timeframe dialog and the histogram strip read values
    with parseTimestamp; sorting, filtering and bucketing on the server
    with TS_NORMALIZE. Hand-kept twins, so held to the same answers."""
    corpus = [
        "2026-06-23 13:05:01", "2026-06-23T13:05:01Z", "2026-06-23 9:05:01", "2026-06-23  13:05:01",
        "2026-06-23 T 13:05:01", "2026-06-23t13:05:01", "2026-06-23 @ 13:05:01", "2026-06-23@13:05:01",
        "2026-06-23 - 13:05:01", "2026-06-23 at 13:05:01", "2026-06-23, 13:05:01",
        "2026-06-23\t13:05:01", "2026-06-23 13:05:01",
        "6/23/2026 1:02:03 PM", "6/23/2026 - 1:02:03 PM", "6/23/2026@13:02:03", "6/23/2026 at 1:02 PM",
        "6/23/2026 1:02:03 PM", "June 23, 2026 at 5:11:22 PM", "Jun 23, 2026, 5:11:22 PM",
        "Jun 23 2026 @ 12:34:56.123", "23 Jun 2026 - 12:34", "Jun 23 2026 1:02:03",
        # Near misses: the date alone, on both sides.
        "2026-06-23 -05:00", "2026-06-23-12:34", "2026-06-23 attic 1:02", "2026-06-23 at noon", "2026-06-23",
    ]
    shown = page.evaluate("(vs) => vs.map((v) => __winnow.formatTimestamp(v, 'iso'))", corpus)
    for value, got in zip(corpus, shown):
        assert got == _ts_normalize(value), repr(value)

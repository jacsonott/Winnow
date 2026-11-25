"""A millisecond epoch converted to a timestamp shows its milliseconds.

The report: "Converting an epoch to a timestamp is losing resolution past
seconds." The stored value had them all along ("2025-11-25
19:46:40.087000"), and the Add derived column preview showed them, but
the grid shows a datetime column in the case's format, and that is
"YYYY-MM-DD HH:MM:SS" unless someone changes it. So every row of a
second showed the same second, and the preview's .087 was gone the
moment the column was added.

A converted column now records how much of a second it carries
(tests/test_derived_subsecond.py), and that one inherited format is
widened to it: .mmm for milliseconds, .ffffff for finer. A format picked
on the column itself, or any other format the case uses, stays as chosen.
Asserted on the painted cells and the header menu's tick, what the
analyst sees."""

import re
import time

import pytest

pytestmark = pytest.mark.ui

T0 = 1_764_100_000   # 2025-11-25 19:46:40 UTC

# What the top row shows under a column, read off the DOM.
_TOP = """(name) => {
  const ci = __winnow.visibleCols().indexOf(name);
  const row = document.querySelector('#rows .row[data-pos="0"]:not(.pending)');
  const cell = row && row.querySelector(`.cell[data-col="${ci}"]`);
  return cell ? cell.textContent : null;
}"""


def _wait_top(page, name, value, timeout=10_000):
    try:
        page.wait_for_function(f"([n, v]) => ({_TOP})(n) === v", arg=[name, value], timeout=timeout)
    except Exception:
        got = page.evaluate(f"(n) => ({_TOP})(n)", name)
        raise AssertionError(f"{name}: top row shows {got!r}, expected {value!r}") from None


def _header_menu(page, col):
    page.locator(f'.hcell[data-col="{col}"]').click(button="right")
    page.wait_for_selector(".menu .menu-item")


def _ticked(page):
    return page.evaluate("""() => [...document.querySelectorAll('.menu .menu-item')]
        .filter((b) => (b.querySelector('.menu-check') || {}).textContent === '✓')
        .map((b) => b.querySelector('.menu-item-text').textContent)""")


@pytest.fixture
def epochs(page, api, imported, tmp_path):
    """A table of epochs in three units, each converted with Parse a
    timestamp → Unix epoch, open in the grid. The first row is the
    earliest in every column, so it is on top however the table opens."""
    f = tmp_path / "epoch_subsecond.csv"
    f.write_text("EsSec,EsMs,EsNs,EsMsg\n"
                 f"{T0},{T0}087,{T0}087123456,first\n"
                 f"{T0 + 1},{T0}412,{T0}412000999,second\n", encoding="utf-8")
    sid = imported({"path": str(f), "name": "epoch_subsecond.csv", "kind": "csv"})[0]
    home = page.evaluate("() => __winnow.S.sourceId")
    try:
        for col in ("EsSec", "EsMs", "EsNs"):
            api("/api/derived", "POST", {"source_id": sid, "name": f"{col} time", "input_column": col,
                                         "op_id": "unix_epoch", "params": {}})
        # Polled from Python: page.wait_for_function does not await a
        # promise predicate (see test_derived_merge_ui.py).
        deadline = time.monotonic() + 20
        while not all(d["status"] == "ready" for d in api(f"/api/derived?source_id={sid}")):
            assert time.monotonic() < deadline, api(f"/api/derived?source_id={sid}")
            time.sleep(0.2)
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)
        page.wait_for_selector('#rows .row[data-pos="0"]:not(.pending)')
        yield sid
    finally:
        page.keyboard.press("Escape")
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => __winnow.loadSources()
            .then(() => home != null && __winnow.openSource(home))""", home)


def test_the_default_format_shows_what_each_conversion_carries(page, epochs):
    assert page.evaluate("() => __winnow.caseTsFormat()") == "iso"
    _wait_top(page, "EsMs time", "2025-11-25 19:46:40.087")
    # Nanoseconds are kept to the microsecond, the finest the stored
    # shape holds.
    _wait_top(page, "EsNs time", "2025-11-25 19:46:40.087123")
    # Whole seconds have no fraction to show: unchanged.
    _wait_top(page, "EsSec time", "2025-11-25 19:46:40")
    # The column is wide enough for what it shows.
    clipped = page.evaluate("""() => {
        const ci = __winnow.visibleCols().indexOf('EsNs time');
        const c = document.querySelector(`#rows .row[data-pos="0"] .cell[data-col="${ci}"]`);
        return c.scrollWidth > c.clientWidth; }""")
    assert not clipped


def test_the_header_menu_says_which_format_and_picking_one_wins(page, epochs):
    _header_menu(page, "EsMs time")
    assert _ticked(page) == ["YYYY-MM-DD HH:MM:SS.mmm"]
    # To the second, on purpose: picked on the column, it is what shows.
    page.locator(".menu .menu-item", has=page.locator(
        ".menu-item-text", has_text=re.compile(r"^YYYY-MM-DD HH:MM:SS$"))).click()
    _wait_top(page, "EsMs time", "2025-11-25 19:46:40")
    _header_menu(page, "EsMs time")
    assert _ticked(page) == ["YYYY-MM-DD HH:MM:SS"]


def test_a_format_the_case_chose_is_kept(page, api, epochs):
    """Only the plain YYYY-MM-DD HH:MM:SS is widened. A case set to US
    dates keeps its layout, and one set to milliseconds keeps that
    precision for a column that carries more."""
    before = page.evaluate("() => __winnow.S.caseSettings.ts_format || ''")
    try:
        for fmt, ms, ns in (("us", "11/25/2025 19:46:40", "11/25/2025 19:46:40"),
                            ("iso_ms", "2025-11-25 19:46:40.087", "2025-11-25 19:46:40.087"),
                            ("raw", "2025-11-25 19:46:40.087000", "2025-11-25 19:46:40.087123")):
            saved = api("/api/case_settings", "POST", {"ts_format": fmt})
            page.evaluate("(s) => { __winnow.S.caseSettings = s; __winnow.render(); }", saved)
            _wait_top(page, "EsMs time", ms)
            _wait_top(page, "EsNs time", ns)
    finally:
        restored = api("/api/case_settings", "POST", {"ts_format": before})
        page.evaluate("(s) => { __winnow.S.caseSettings = s; __winnow.render(); }", restored)


def test_with_fraction_widens_only_the_plain_format(page):
    got = page.evaluate("""() => [
        __winnow.withFraction('iso', { type: 'datetime', subsecond: 3 }),
        __winnow.withFraction('iso', { type: 'datetime', subsecond: 6 }),
        __winnow.withFraction('iso', { type: 'datetime', subsecond: 0 }),
        __winnow.withFraction('iso', { type: 'datetime' }),
        __winnow.withFraction('iso', { type: 'text', subsecond: 3 }),
        __winnow.withFraction('us', { type: 'datetime', subsecond: 6 }),
        __winnow.withFraction('iso_ms', { type: 'datetime', subsecond: 6 }),
        __winnow.withFraction('date', { type: 'datetime', subsecond: 6 }),
        __winnow.withFraction('iso', null)]""")
    assert got == ["iso_ms", "iso_us", "iso", "iso", "iso", "us", "iso_ms", "date", "iso"]

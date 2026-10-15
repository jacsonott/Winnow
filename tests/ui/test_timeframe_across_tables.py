"""The timeframe filter across a table switch, both ways the analyst can
have it: one case-wide range that follows them from table to table (the
default — what every install did before it was a choice), or one range
per table, the way the other filters are kept.

Each test asserts what the analyst reads after the switch — the row count
the arriving table lands on and the ⏱ button's label — rather than the
stash that produces it.
"""

from __future__ import annotations

import urllib.request

import pytest

pytestmark = pytest.mark.ui

RANGE = {"enabled": True, "column": None,
         "start": "2026-03-14 08:00:00", "end": "2026-03-14 08:00:29"}


@pytest.fixture
def tables(page, server, tmp_path):
    """(the shared 200-row table, a 60-row second table) — the second one
    a second a row from 08:00:00, so RANGE leaves 30 of either. Dropped
    afterwards so the shared case is left as found."""
    csv2 = tmp_path / "tf_second.csv"
    csv2.write_text("Timestamp,Name\n" + "".join(
        f"2026-03-14 08:00:{i:02d},n{i}\n" for i in range(60)), encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'tf_second.csv')", timeout=15_000)
    first = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id")
    second = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'tf_second.csv').id")
    _open(page, first)
    yield first, second
    page.evaluate("""() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' };
                             __winnow.setTimeframeAcrossTables(true); }""")
    urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + f"/api/source/{second}", method="DELETE",
        headers={"X-Timeline-Lite-Client": "1"})).read()


def _open(page, sid):
    """Through the tab, the way an analyst switches — the sidebar row and
    the tab both land in openSource."""
    tab = page.locator(f'#sourceTabs .tab[data-id="{sid}"]')
    if tab.count():
        tab.click()
    else:
        page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("""(id) => __winnow.S.sourceId === id && __winnow.S.view
      && __winnow.S.view.source_id === id && __winnow.busyCount === 0""", arg=sid)


def _set_range(page, rng):
    """Through the dialog, so the test drives the same Apply the analyst does."""
    page.locator("#body").focus()
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])")
    inputs = page.locator("#modalBody input[type=text]")
    inputs.nth(0).fill(rng["start"])
    inputs.nth(1).fill(rng["end"])
    # No Enabled box to tick any more: Apply is what turns it on.
    page.get_by_role("button", name="Apply").click()
    page.wait_for_selector("#modal[hidden]", state="attached")


def _rows(page):
    page.wait_for_function("() => __winnow.busyCount === 0")
    return page.evaluate("() => __winnow.S.view.row_count")


def test_by_default_the_timeframe_follows_you_to_the_next_table(page, tables):
    first, second = tables
    assert page.evaluate("() => __winnow.timeframeAcrossTables()") is True
    _set_range(page, RANGE)
    page.wait_for_function("() => __winnow.S.view.row_count === 30")

    _open(page, second)
    assert _rows(page) == 30, "the second table opened without the timeframe"
    assert page.locator("#btnTimeRange").get_attribute("aria-pressed") == "true"


def test_off_each_table_keeps_its_own_timeframe(page, tables):
    first, second = tables
    # Turned off from the dialog itself, where the analyst sets the range.
    page.locator("#body").focus()
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#tfAcrossTables").uncheck()
    assert "each table keeps its own" in page.locator("#modalBody p").first.inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    # Saved as a preference on the spot, Cancel/Escape notwithstanding.
    assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.appearance')).timeframeAcrossTables") is False

    _set_range(page, RANGE)
    page.wait_for_function("() => __winnow.S.view.row_count === 30")

    # A table it was never set on opens whole, and the button says so.
    _open(page, second)
    assert _rows(page) == 60
    assert page.locator("#btnTimeRange").get_attribute("aria-pressed") == "false"
    assert page.locator("#btnTimeRange").inner_text() == "⏱ Timeframe"

    # This table's own range…
    _set_range(page, {**RANGE, "end": "2026-03-14 08:00:09"})
    page.wait_for_function("() => __winnow.S.view.row_count === 10")

    # …and coming back to the first table puts back the one it had.
    _open(page, first)
    assert _rows(page) == 30
    assert page.evaluate("() => __winnow.S.timeRange.end") == RANGE["end"]

    _open(page, second)
    assert _rows(page) == 10


def test_off_a_refresh_of_the_open_table_keeps_its_timeframe(page, tables):
    """Every refresh idiom (a derived column added, another tab closed)
    re-enters openSource for the table already open. That is not a
    switch, and must not take the range just set on it away."""
    first, _second = tables
    page.evaluate("() => __winnow.setTimeframeAcrossTables(false)")
    _set_range(page, RANGE)
    page.wait_for_function("() => __winnow.S.view.row_count === 30")
    page.evaluate("(id) => __winnow.openSource(id)", first)
    assert _rows(page) == 30
    assert page.evaluate("() => __winnow.S.timeRange.enabled") is True


def test_settings_and_the_dialog_are_the_same_switch(page, tables):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".settings-section-head", has_text="Filter & search syntax").click()
    cb = page.locator("#settingsTimeframeAcrossTables")
    assert cb.is_checked()
    cb.uncheck()
    page.wait_for_function("() => __winnow.timeframeAcrossTables() === false")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

    page.locator("#body").focus()
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])")
    assert not page.locator("#tfAcrossTables").is_checked()
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

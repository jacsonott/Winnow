"""Apply turns the timeframe filter on.

The dialog used to carry an Enabled box as well, unticked until the filter
had been on once. A range typed in and Applied with it unticked was saved
but not applied: the dialog closed, every row was still there, and Apply
read as broken. Apply now always turns the filter on; Clear and the toggle
key are the ways off."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

ALL_ROWS = 200
FROM_08_01 = 140   # ui.csv's Timestamp runs 08:00:00 → 08:03:19, one row a second


def _rows(page):
    return page.evaluate("() => __winnow.S.view.row_count")


@pytest.fixture(autouse=True)
def _no_timeframe_left_behind(page):
    yield
    page.evaluate("""() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' };
                       __winnow.updateTimeRangeButton(); return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function(f"() => __winnow.S.view && __winnow.S.view.row_count === {ALL_ROWS}")


def _apply(page):
    before = page.evaluate("() => __winnow.S.view.view_id")
    page.locator("#modalBody .btn", has_text="Apply").click()
    page.wait_for_function("(vid) => __winnow.S.view && __winnow.S.view.view_id !== vid", arg=before)


def test_a_first_range_applies_on_apply(page):
    assert _rows(page) == ALL_ROWS
    page.locator("#btnTimeRange").click()
    page.wait_for_selector("#modal:not([hidden])")
    # Nothing in the dialog stands between typing a range and using it.
    # (The one box left, "Keep applied when switching tables", is a saved
    # preference about table switches, not an on switch.)
    assert page.locator("#modalBody label", has_text="Enabled").count() == 0
    page.locator("#modalBody .dlg-pair input").first.fill("2026-03-14 08:01:00")
    _apply(page)
    assert page.evaluate("() => __winnow.S.timeRange.enabled") is True
    assert _rows(page) == FROM_08_01
    assert page.get_attribute("#btnTimeRange", "aria-pressed") == "true"


def test_apply_turns_a_range_that_was_toggled_off_back_on(page):
    """The toggle key keeps the range and turns the filter off; the dialog
    opened on it says so, and its Apply is the way back on."""
    page.evaluate("""() => { __winnow.S.timeRange = { enabled: false, column: null,
                                                      start: '2026-03-14 08:01:00', end: '' };
                       __winnow.updateTimeRangeButton(); }""")
    page.evaluate("() => __winnow.openTimeRangeModal()")
    page.wait_for_selector("#modal:not([hidden])")
    assert "Off right now — Apply turns it back on." in page.locator("#modalBody").inner_text()
    assert page.locator("#modalBody .dlg-pair input").first.input_value() == "2026-03-14 08:01:00"
    _apply(page)
    assert _rows(page) == FROM_08_01
    assert page.get_attribute("#btnTimeRange", "aria-pressed") == "true"
    # And the toggle key still turns it off without forgetting the range.
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    before = page.evaluate("() => __winnow.S.view.view_id")
    page.keyboard.press("r")
    page.wait_for_function("(vid) => __winnow.S.view && __winnow.S.view.view_id !== vid", arg=before)
    assert _rows(page) == ALL_ROWS
    assert page.evaluate("() => __winnow.S.timeRange.start") == "2026-03-14 08:01:00"

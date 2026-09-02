"""The 📅 button beside every free-typed timestamp input: it drives the
browser's native picker and writes the choice back in the app's own
YYYY-MM-DD HH:MM:SS shape, firing the text input's handlers. The native
control is driven directly here (Playwright can't click through the OS
picker popup) — what's under test is the seeding and the write-back."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_timeframe_inputs_have_pickers_that_write_back(page):
    page.locator("#btnTimeRange").click()
    page.wait_for_selector("#modal:not([hidden])")
    wraps = page.locator("#modal .date-pick-wrap")
    assert wraps.count() == 2                        # start + end
    start = page.locator("#modal input[placeholder='YYYY-MM-DD HH:MM:SS']").first
    start.fill("2026-03-14 08:15:30")
    # The button seeds the native control from the typed value…
    wraps.first.locator(".date-pick-btn").click()
    native = wraps.first.locator(".date-pick-native")
    assert native.input_value() == "2026-03-14T08:15:30"
    # …and a pick writes back in the app's shape, seconds included.
    native.evaluate("(n) => { n.value = '2026-03-15T09:30'; n.onchange(); }")
    assert start.input_value() == "2026-03-15 09:30:00"
    # The click above may have left the NATIVE picker popup open (headless
    # Chromium honours showPicker) — the first Escape closes it, so give
    # the modal a second one if it's still up.
    page.keyboard.press("Escape")
    try:
        page.wait_for_selector("#modal[hidden]", state="attached", timeout=1500)
    except Exception:
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal[hidden]", state="attached")


def test_jump_modal_picker_and_validation_accepts_result(page):
    page.locator("#body").focus()
    page.keyboard.press("a")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modal .date-pick-wrap").count() == 1
    native = page.locator("#modal .date-pick-native")
    native.evaluate("(n) => { n.value = '2026-03-14T08:00:10'; n.onchange(); }")
    inp = page.locator("#modal input[placeholder='YYYY-MM-DD HH:MM:SS']")
    assert inp.input_value() == "2026-03-14 08:00:10"
    # The written value is one the jump actually accepts (fixture rows are
    # 2026-03-14 08:00:xx) — the jump runs rather than toasting a format error.
    page.locator("#modal button", has_text="Jump").click()
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_filterbuilder_datetime_condition_gets_a_picker(page):
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal button", has_text="+ condition").first.click()
    cond = page.locator("#modal .fb-cond").first
    cond.locator("select").nth(0).select_option("Timestamp")   # datetime column
    cond.locator("select").nth(1).select_option(">")
    page.wait_for_selector("#modal .fb-cond .date-pick-wrap")
    native = cond.locator(".date-pick-native")
    native.evaluate("(n) => { n.value = '2026-03-14T08:01'; n.onchange(); }")
    assert cond.locator("input:not(.date-pick-native)").input_value() == "2026-03-14 08:01:00"
    # The dispatched input event reached the tree (node.value updated).
    sql = page.locator("#modal .fb-sql").input_value()
    assert "2026-03-14 08:01:00" in sql
    # A text column's condition has no calendar.
    cond.locator("select").nth(0).select_option("Host")
    page.wait_for_function(
        "() => !document.querySelector('#modal .fb-cond .date-pick-wrap')")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] }; }")

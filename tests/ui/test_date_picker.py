"""The 📅 button beside every free-typed timestamp input opens Winnow's own
token-themed calendar panel (an anchoredPanel, not the browser's native
popup) and writes the pick back in the app's YYYY-MM-DD HH:MM:SS shape,
firing the text input's handlers so previews and validation see typing."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_timeframe_calendar_seeds_selects_and_writes_back(page):
    page.locator("#btnTimeRange").click()
    page.wait_for_selector("#modal:not([hidden])")
    btns = page.locator("#modal .date-pick-btn")
    assert btns.count() == 2                         # start + end
    start = page.locator("#modal input[placeholder='YYYY-MM-DD HH:MM:SS']").first
    start.fill("2026-03-14 08:15:30")
    btns.first.click()
    panel = page.locator(".date-pick-panel")
    panel.wait_for(state="visible")
    # Seeded from the typed value: right month, day highlighted, time kept.
    assert panel.locator(".dp-title").inner_text() == "March 2026"
    assert "selected" in panel.locator(".dp-day[data-date='2026-03-14']").get_attribute("class")
    assert panel.locator(".dp-time").input_value() == "08:15:30"
    # Picking another day writes date + the panel's time, and closes.
    panel.locator(".dp-day[data-date='2026-03-15']").click()
    page.wait_for_selector(".date-pick-panel", state="detached")
    assert start.input_value() == "2026-03-15 08:15:30"

    # Clear empties the field from the panel too.
    btns.first.click()
    page.locator(".date-pick-panel .dp-act", has_text="Clear").click()
    page.wait_for_selector(".date-pick-panel", state="detached")
    assert start.input_value() == ""
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_month_year_nav_and_escape_leaves_the_modal_up(page):
    page.locator("#btnTimeRange").click()
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal .date-pick-btn").nth(1).click()   # End — empty, opens on today
    panel = page.locator(".date-pick-panel")
    panel.wait_for(state="visible")
    before = panel.locator(".dp-title").inner_text()
    panel.locator(".dp-nav", has_text="‹").click()
    after_month = panel.locator(".dp-title").inner_text()
    assert after_month != before
    panel.locator(".dp-nav", has_text="»").click()
    assert panel.locator(".dp-title").inner_text() != after_month
    # Escape dismisses the panel like any menu — the dialog stays up.
    page.keyboard.press("Escape")
    page.wait_for_selector(".date-pick-panel", state="detached")
    assert not page.locator("#modal").is_hidden()
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_jump_modal_pick_passes_the_jumps_own_validation(page):
    page.locator("#body").focus()
    page.keyboard.press("a")
    page.wait_for_selector("#modal:not([hidden])")
    # Type first, so the panel opens seeded on the fixture's month.
    page.locator("#modal input[placeholder='YYYY-MM-DD HH:MM:SS']").fill("2026-03-01 08:00:30")
    page.locator("#modal .date-pick-btn").click()
    panel = page.locator(".date-pick-panel")
    panel.wait_for(state="visible")
    assert panel.locator(".dp-title").inner_text() == "March 2026"
    panel.locator(".dp-day[data-date='2026-03-14']").click()
    page.wait_for_selector(".date-pick-panel", state="detached")
    inp = page.locator("#modal input[placeholder='YYYY-MM-DD HH:MM:SS']")
    assert inp.input_value() == "2026-03-14 08:00:30"
    page.locator("#modal button", has_text="Jump").click()
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_filterbuilder_datetime_condition_gets_a_calendar(page):
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal button", has_text="+ condition").first.click()
    cond = page.locator("#modal .fb-cond").first
    cond.locator("select").nth(0).select_option("Timestamp")   # datetime column
    cond.locator("select").nth(1).select_option(">")
    page.wait_for_selector("#modal .fb-cond .date-pick-btn")
    cond.locator(".date-pick-btn").click()
    panel = page.locator(".date-pick-panel")
    panel.wait_for(state="visible")
    day = panel.locator(".dp-day:not(.other)").first
    key = day.get_attribute("data-date")
    day.click()
    page.wait_for_selector(".date-pick-panel", state="detached")
    assert cond.locator("input").input_value() == f"{key} 00:00:00"
    # The dispatched input event reached the tree — the SQL mirror shows it.
    assert key in page.locator("#modal .fb-sql").input_value()
    # A text column's condition has no calendar.
    cond.locator("select").nth(0).select_option("Host")
    page.wait_for_function(
        "() => !document.querySelector('#modal .fb-cond .date-pick-btn')")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] }; }")

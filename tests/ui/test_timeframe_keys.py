"""The timeframe pair on one letter: r toggles the filter, Shift+R opens
the dialog — shift as "the bigger version of the action". Moved off T in
keymap v3; a stored default from before migrates, a deliberate custom
binding does not."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def test_r_toggles_and_shift_r_opens(page):
    assert sorted(page.evaluate("() => __winnow.S.keymap.toggleTimeRange")) == ["a", "r"]
    assert sorted(page.evaluate("() => __winnow.S.keymap.openTimeRange")) == ["A", "R"]
    # Shift+R opens the dialog…
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])")
    assert "timeframe" in page.locator("#modal").inner_text().lower()
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    # …while plain r is the TOGGLE. With a range configured it flips the
    # filter without opening any dialog. (With NO range it opens the
    # dialog as a courtesy — that behavior predates this binding.)
    page.evaluate(
        """() => { __winnow.S.timeRange = { enabled: false, column: 'Timestamp',
                    start: '2026-03-14 08:00:00', end: '2026-03-14 09:00:00' }; }""")
    page.keyboard.press("r")
    page.wait_for_function("() => __winnow.S.timeRange.enabled === true")
    assert page.evaluate("() => document.getElementById('modal').hidden")
    page.keyboard.press("r")
    page.wait_for_function("() => __winnow.S.timeRange.enabled === false")


def test_v3_migration_moves_only_the_default(page, browser, server):
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('winnow.remotePrompt', 'seen');"
        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }));"
        # Simulate a v2-era install: stored map at version 2 with the old default.
        "localStorage.setItem('winnow.keymap', JSON.stringify({ toggleTimeRange: ['T', 'a'] }));"
        "localStorage.setItem('winnow.keymap.v', '2');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row")
        keys = pg.evaluate("() => __winnow.S.keymap.toggleTimeRange")
        assert sorted(keys) == ["a", "r"]
    finally:
        ctx.close()

    ctx2 = browser.new_context()
    ctx2.add_init_script(
        "localStorage.setItem('winnow.remotePrompt', 'seen');"
        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }));"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ toggleTimeRange: ['x'] }));"
        "localStorage.setItem('winnow.keymap.v', '2');")
    pg2 = ctx2.new_page()
    try:
        pg2.goto(server, wait_until="networkidle")
        pg2.wait_for_selector(".row")
        keys = pg2.evaluate("() => __winnow.S.keymap.toggleTimeRange")
        assert keys == ["x"], "a deliberate rebinding must never be migrated over"
    finally:
        ctx2.close()

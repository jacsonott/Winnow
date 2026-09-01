"""Timeframe on r/R (r toggles, Shift+R opens the dialog), and `a` on
jump-to-timestamp. `a`/`A` moved OFF the timeframe filter in keymap v4 (it
already lives on r/R) and ONTO jump-to-timestamp; a stored default from
before migrates, a deliberate custom binding does not."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_r_toggles_timeframe_and_a_jumps_to_timestamp(page):
    assert sorted(page.evaluate("() => __winnow.S.keymap.toggleTimeRange")) == ["r"]
    assert sorted(page.evaluate("() => __winnow.S.keymap.openTimeRange")) == ["R"]
    assert sorted(page.evaluate("() => __winnow.S.keymap.openJumpTs")) == ["J", "a"]

    # Shift+R opens the timeframe dialog…
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "timeframe filter"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

    # …`a` opens jump-to-timestamp, NOT the timeframe filter.
    page.keyboard.press("a")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "jump to timestamp"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

    # plain r is the TOGGLE (no dialog when a range is configured).
    page.evaluate(
        """() => { __winnow.S.timeRange = { enabled: false, column: 'Timestamp',
                    start: '2026-03-14 08:00:00', end: '2026-03-14 09:00:00' }; }""")
    page.keyboard.press("r")
    page.wait_for_function("() => __winnow.S.timeRange.enabled === true")
    assert page.evaluate("() => document.getElementById('modal').hidden")
    page.keyboard.press("r")
    page.wait_for_function("() => __winnow.S.timeRange.enabled === false")


def test_migration_moves_a_off_timeframe_onto_jump(page, browser, server):
    # A v2-era install with the old default migrates all the way through v4:
    # ['T','a'] -> ['r','a'] (v3) -> ['r'] (v4), and jump-ts picks up 'a'.
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('winnow.remotePrompt', 'seen');"
        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }));"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ toggleTimeRange: ['T', 'a'] }));"
        "localStorage.setItem('winnow.keymap.v', '2');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row")
        assert sorted(pg.evaluate("() => __winnow.S.keymap.toggleTimeRange")) == ["r"]
        assert sorted(pg.evaluate("() => __winnow.S.keymap.openJumpTs")) == ["J", "a"]
    finally:
        ctx.close()

    # A deliberate rebinding is never migrated over.
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
        assert pg2.evaluate("() => __winnow.S.keymap.toggleTimeRange") == ["x"]
    finally:
        ctx2.close()

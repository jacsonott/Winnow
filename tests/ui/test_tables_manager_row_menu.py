"""The Tables manager's rows: Open/Close and a ⋯, where there were five
buttons — and a ⋯ menu that the manager's status poll leaves alone.

The poll refetches every table every 1.5 s and used to redraw every row
on every tick. With the row actions in a menu, a redraw under an open
menu leaves it anchored to a button that is no longer in the document,
so the poll now waits for the menu to close (and skips ticks that
changed nothing at all).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_manager(page):
    page.evaluate("() => __winnow.openTablesManager()")
    row = page.locator("#modal .tables-row", has_text="ui.csv").first
    row.wait_for()
    return row


def test_a_row_is_open_close_and_a_menu(page):
    row = _open_manager(page)
    labels = [b.inner_text().strip() for b in row.locator("button").all()]
    assert labels == ["Close", "⋯"], labels
    row.locator(".tables-more").click()
    items = [i.inner_text().strip() for i in page.locator(".menu .menu-item").all()]
    assert items == ["Settings…", "Nickname…", "Remove…"], items
    page.keyboard.press("Escape")   # the menu
    page.keyboard.press("Escape")   # the manager
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_the_row_menu_outlasts_the_status_poll(page, api):
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id")
    row = _open_manager(page)
    try:
        row.locator(".tables-more").click()
        page.wait_for_selector(".menu .menu-item")
        # Something the poll reads changes under the open menu…
        api(f"/api/source/{sid}/nickname", "POST", {"nickname": "polled nickname"})
        page.wait_for_function(
            "(id) => __winnow.S.sources.find((s) => s.id === id).nickname === 'polled nickname'",
            arg=sid, timeout=5000)
        page.wait_for_timeout(1700)   # and at least one more tick goes by
        # …and the menu is still hanging off the button on screen.
        assert page.evaluate("() => !!__winnow.openMenuAnchor && __winnow.openMenuAnchor.isConnected")
        assert page.locator(".menu .menu-item", has_text="Settings…").is_visible()
        page.keyboard.press("Escape")
        # Once it closes, the next tick paints what changed.
        page.locator("#modal .tables-row", has_text="polled nickname").wait_for(timeout=5000)
    finally:
        api(f"/api/source/{sid}/nickname", "POST", {"nickname": ""})
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
        page.evaluate("() => __winnow.loadSources()")

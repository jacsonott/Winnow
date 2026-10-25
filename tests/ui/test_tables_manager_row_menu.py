"""The Tables manager's rows: a tick, the counts and a faint ⋯ — and the
⋯ is the table's own right-click menu, which the status poll leaves alone.

The manager used to carry its own row menu (Settings…, Nickname…,
Remove…), worded differently from the tab's and refusing Remove its own
way. It is tableMenuItems now, item for item, so the two can't disagree.

The poll refetches every table every 1.5 s. A repaint under an open menu
leaves it anchored to a button that is no longer in the document, so the
poll waits for the menu to close (and skips ticks that changed nothing).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_manager(page):
    page.evaluate("() => __winnow.openTablesManager()")
    row = page.locator("#modal .tables-list .tables-row", has_text="ui.csv").first
    row.wait_for()
    return row


def test_a_row_is_a_tick_its_counts_and_the_tables_menu(page):
    row = _open_manager(page)
    tick = row.locator("input[type=checkbox]")
    assert tick.is_checked(), "ui.csv is open as a tab, so its tick is on"
    assert row.locator(".tables-name").inner_text() == "ui.csv"
    assert row.locator(".tables-rows").inner_text() == "200"
    # The only button besides the name is the ⋯: Open/Close is the tick.
    assert [b.inner_text().strip() for b in row.locator("button").all()] == ["ui.csv", "⋯"]
    row.locator(".tables-more").click()
    # By label: Columns… carries the C keycap on the table that's showing.
    items = [i.inner_text().strip() for i in page.locator(".menu .menu-item .menu-item-text").all()]
    expected = page.evaluate("""() => {
      const s = __winnow.S.sources.find((x) => x.name === 'ui.csv');
      return __winnow.tableMenuItems(s.id).filter((i) => i !== '-').map((i) => i.label);
    }""")
    assert items == expected, (items, expected)
    assert items[0] == "Close this tab" and "Columns…" in items and items[-1] == "Remove…", items
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
        assert page.locator(".menu .menu-item", has_text="Columns…").is_visible()
        page.keyboard.press("Escape")
        # Once it closes, the next tick paints what changed.
        page.locator("#modal .tables-row", has_text="polled nickname").wait_for(timeout=5000)
    finally:
        api(f"/api/source/{sid}/nickname", "POST", {"nickname": ""})
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
        page.evaluate("() => __winnow.loadSources()")

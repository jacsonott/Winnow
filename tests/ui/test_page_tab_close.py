"""Page tabs (SQL, Timeline, Notes, Watchlist, plugin tabs) close and
reopen from the sidebar's Pages section (✕ / ＋ per row — the strip
itself carries no per-tab chrome), the preference persists across
reloads, and Alt+digit slots skip closed tabs instead of addressing
invisible ones."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _strip_tab(page, key):
    return page.locator(f'#pageTabs .tab[data-page-key="{key}"]')


def _sidebar_row(page, label):
    return page.locator("#sidebarList .sidebar-row", has_text=label).first


def test_close_and_reopen_from_the_sidebar(page):
    tab = _strip_tab(page, "notes")
    assert tab.is_visible()
    assert tab.locator(".x").count() == 0             # no ✕ on the strip any more
    row = _sidebar_row(page, "Notes")
    row.hover()
    row.locator(".menu-item-action[title='Close this page tab']").click()
    page.wait_for_selector('#pageTabs .tab[data-page-key="notes"]', state="hidden")

    # The sidebar row survives, dimmed, with a ＋ to reopen it.
    row = page.locator("#sidebarList .sidebar-row.sidebar-page-closed", has_text="Notes")
    assert row.count() == 1
    row.hover()
    row.locator(".menu-item-action[title='Reopen this page tab']").click()
    page.wait_for_selector("#notesview:not([hidden])")
    assert _strip_tab(page, "notes").is_visible()
    assert page.locator("#sidebarList .sidebar-row.sidebar-page-closed").count() == 0


def test_closing_the_active_page_returns_to_the_grid(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    row = _sidebar_row(page, "Watchlist")
    row.hover()
    row.locator(".menu-item-action[title='Close this page tab']").click()
    # Back on the grid, watchlist gone from the strip.
    page.wait_for_selector("#watchlistview", state="hidden")
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
    assert not _strip_tab(page, "watchlist").is_visible()
    page.evaluate("() => __winnow.reopenPageTab('watchlist')")


def test_closed_state_persists_and_digit_slots_skip_it(page):
    page.evaluate("() => __winnow.closePageTab('sql')")
    page.wait_for_selector('#pageTabs .tab[data-page-key="sql"]', state="hidden")
    # Slot 2 is the first VISIBLE page tab — with SQL closed, that's Timeline.
    page.locator("#body").focus()
    page.keyboard.press("Alt+2")
    page.wait_for_selector("#timelineview:not([hidden])")

    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    assert not _strip_tab(page, "sql").is_visible()
    assert page.evaluate("() => __winnow.pageTabClosed('sql')")
    page.evaluate("() => __winnow.reopenPageTab('sql')")
    assert _strip_tab(page, "sql").is_visible()

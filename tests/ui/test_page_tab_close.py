"""Page tabs (SQL, Timeline, Notes, Watchlist, plugin tabs) close like table
tabs: ✕ removes one from the strip, the sidebar's Pages section is the
reopen path, the preference persists across reloads, and Alt+digit slots
skip closed tabs instead of addressing invisible ones."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _strip_tab(page, key):
    return page.locator(f'#pageTabs .tab[data-page-key="{key}"]')


def test_close_from_strip_and_reopen_from_sidebar(page):
    # Close Notes from its ✕.
    tab = _strip_tab(page, "notes")
    assert tab.is_visible()
    tab.locator(".x").click()
    page.wait_for_selector('#pageTabs .tab[data-page-key="notes"]', state="hidden")

    # The sidebar row survives, dimmed, and clicking it reopens + shows.
    row = page.locator("#sidebarList .sidebar-row.sidebar-page-closed", has_text="Notes")
    assert row.count() == 1
    row.locator(".menu-item").click()
    page.wait_for_selector("#notesview:not([hidden])")
    assert _strip_tab(page, "notes").is_visible()
    assert page.locator("#sidebarList .sidebar-row.sidebar-page-closed").count() == 0


def test_closing_the_active_page_returns_to_the_grid(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    _strip_tab(page, "watchlist").locator(".x").click()
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

"""The Occurrences page is gone (it duplicated Search-all), and the sidebar
can close every open tab at once."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_occurrences_tab_is_gone(page):
    assert page.locator("#tabEntity").count() == 0
    labels = page.eval_on_selector_all("#pageTabs .tab", "els => els.map(e => e.textContent.trim())")
    assert not any("occurrences" in (l or "").lower() for l in labels), labels
    # and the cell menu no longer offers the pivot
    cell = page.locator(".row").nth(1).locator(".cell").nth(1)
    cell.click(button="right")
    page.wait_for_selector(".menu")
    assert page.locator(".menu >> text=Occurrences").count() == 0
    page.keyboard.press("Escape")


def test_close_all_tabs_from_the_sidebar(page):
    table = page.locator("#sidebarList .sidebar-openrow .menu-item").first.inner_text()
    assert table
    page.evaluate("() => __winnow.closeAllTabs()")
    page.wait_for_function("() => !document.querySelector('#sidebarList .sidebar-openrow')")
    # the table stays in the case, under All tables
    assert page.locator("#sidebarList .sidebar-row:not(.sidebar-openrow)", has_text=table).count() >= 1
    # reopen it so the shared case is left as found
    page.locator("#sidebarList .sidebar-row:not(.sidebar-openrow)", has_text=table).locator(".menu-item").click()
    page.wait_for_selector("#sidebarList .sidebar-openrow")

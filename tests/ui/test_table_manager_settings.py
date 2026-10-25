"""The Tables manager reaches each table's own menu: Columns… in a row's ⋯
opens the same per-table modal as Columns… on the tab's right-click menu
(it is the same menu), so a closed table's settings are reachable without
reopening it by hand first (openTableMenu does that itself)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_columns_opens_the_table_menu(page):
    page.keyboard.press("t")
    page.wait_for_selector("#modal:not([hidden]) .tables-list .tables-row")
    row = page.locator("#modal .tables-list .tables-row").first
    name = row.locator(".tables-name").inner_text()
    row.locator(".tables-more").click()
    page.locator(".menu .menu-item", has_text="Columns…").click()
    page.wait_for_selector("#modal:not([hidden]) .table-menu")
    title = page.locator("#modalTitle").inner_text().lower()   # CSS uppercases it
    assert title.startswith("table"), title
    assert name.strip().lower() in title, (name, title)
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

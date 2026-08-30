"""The Tables manager reaches each table's own menu — a row's Settings…
button opens the same per-table modal the tab's right-click does, so a
closed table's settings are reachable without reopening it by hand
first (openTableMenu does that itself)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_settings_button_opens_the_table_menu(page):
    page.keyboard.press("t")
    page.wait_for_selector("#modal:not([hidden])")
    row = page.locator("#modal .session-row").first
    name = row.locator(".session-name").inner_text()
    row.locator("button", has_text="Settings…").click()
    page.wait_for_selector("#modal:not([hidden]) .table-menu-section")
    title = page.locator("#modalTitle").inner_text().lower()   # CSS uppercases it
    assert title.startswith("table"), title
    assert name.split(" ·")[0].strip().lower() in title, (name, title)
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

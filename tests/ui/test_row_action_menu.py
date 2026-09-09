"""The row menu's Plugins section: registered row actions appear as
entries, grey out past their max_rows, and post the selection to the
dispatch route (the server half is tests/test_plugin_row_actions.py)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

def test_plugin_section_lists_actions_and_respects_max_rows(page, row_menu, flyout, fake_row_action):
    fake_row_action(max_rows=2)
    root = row_menu(row=1, cell=1)
    # Plugin actions live under a Plugins ▸ submenu, counted in its hint.
    plugins = root.locator(".menu-item-sub", has_text="Plugins")
    assert plugins.count() == 1 and plugins.locator(".menu-item-hint").inner_text() == "1"
    item = flyout("Plugins").locator(".menu-item", has_text="Look up on VT")
    assert item.get_attribute("aria-disabled") in (None, "false") and not item.is_disabled()
    page.keyboard.press("Escape")

    # Select 3 rows → the entry is disabled (max_rows 2).
    page.locator(".row").nth(0).locator(".cell").nth(1).click()
    page.locator(".row").nth(2).locator(".cell").nth(1).click(modifiers=["Shift"])
    row_menu(row=1, cell=1)
    item = flyout("Plugins").locator(".menu-item", has_text="Look up on VT")
    assert item.is_disabled() or item.get_attribute("aria-disabled") == "true"
    page.keyboard.press("Escape")

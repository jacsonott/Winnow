"""The row menu's Plugins section: registered row actions appear as
entries, grey out past their max_rows, and post the selection to the
dispatch route (the server half is tests/test_plugin_row_actions.py)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

FAKE = ("() => { __winnow.S.pluginRowActions = [{ id: 'demo.vt', local_id: 'vt', plugin: 'demo', "
        "plugin_fs: 'demo', label: 'Look up on VT', description: 'demo', max_rows: 2 }]; }")


def test_plugin_section_lists_actions_and_respects_max_rows(page):
    page.evaluate(FAKE)
    try:
        page.locator(".row").nth(1).locator(".cell").nth(1).click(button="right")
        page.wait_for_selector(".menu")
        # Plugin actions live under a Plugins ▸ submenu, counted in its hint.
        plugins = page.locator(".menu .menu-item-sub", has_text="Plugins")
        assert plugins.count() == 1 and plugins.locator(".menu-item-hint").inner_text() == "1"
        plugins.click()
        page.wait_for_selector(".menu-sub")
        item = page.locator(".menu-sub .menu-item", has_text="Look up on VT")
        assert item.get_attribute("aria-disabled") in (None, "false") and not item.is_disabled()
        page.keyboard.press("Escape")

        # Select 3 rows → the entry is disabled (max_rows 2).
        page.locator(".row").nth(0).locator(".cell").nth(1).click()
        page.locator(".row").nth(2).locator(".cell").nth(1).click(modifiers=["Shift"])
        page.locator(".row").nth(1).locator(".cell").nth(1).click(button="right")
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item-sub", has_text="Plugins").click()
        page.wait_for_selector(".menu-sub")
        item = page.locator(".menu-sub .menu-item", has_text="Look up on VT")
        assert item.is_disabled() or item.get_attribute("aria-disabled") == "true"
        page.keyboard.press("Escape")
    finally:
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")

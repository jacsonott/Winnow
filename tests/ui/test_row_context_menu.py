"""The row right-click menu — completely uncovered until now, despite
being the primary per-row action surface (rowmenu.js's section registry:
tags, cell ops, clipboard)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_right_click_opens_the_menu_with_its_sections(page):
    cell = page.locator(".row").nth(2).locator(".cell").nth(1)
    cell.click(button="right")
    menu = page.locator(".menu")
    menu.wait_for(state="visible")
    text = menu.inner_text()
    # One stable item from each registered section: clipboard ops and the
    # tag list (default tags are seeded into every new case).
    assert "Copy cell" in text
    assert "Copy" in text and "with headers" in text
    tag_names = page.evaluate("() => __winnow.S.tags.map((t) => t.name)")
    assert any(n in text for n in tag_names)
    page.keyboard.press("Escape")
    assert page.locator(".menu").count() == 0


def test_menu_closes_on_outside_click(page):
    page.locator(".row").nth(2).locator(".cell").nth(1).click(button="right")
    page.locator(".menu").wait_for(state="visible")
    page.locator("#toolbar, header.bar").first.click(force=True)
    page.wait_for_timeout(150)
    assert page.locator(".menu").count() == 0

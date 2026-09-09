"""The row right-click menu — completely uncovered until now, despite
being the primary per-row action surface (rowmenu.js's section registry:
tags, cell ops, clipboard)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_right_click_opens_the_menu_with_its_sections(page, row_menu, flyout):
    menu = row_menu(row=2, cell=1)
    text = menu.inner_text()
    # One stable entry from each registered section at the top level: the
    # Tag and Copy submenus and the clicked column's filters.
    assert "Tag this row" in text and "Copy" in text and "Filter to" in text
    # The tag list (default tags are seeded into every new case) is a click away.
    flyout("Tag this row")
    sub = page.locator(".menu-sub").inner_text()
    tag_names = page.evaluate("() => __winnow.S.tags.map((t) => t.name)")
    assert any(n in sub for n in tag_names)
    page.keyboard.press("Escape")
    assert page.locator(".menu").count() == 0   # root and flyout both gone


def test_menu_closes_on_outside_click(page):
    page.locator(".row").nth(2).locator(".cell").nth(1).click(button="right")
    page.locator(".menu").wait_for(state="visible")
    page.locator("#toolbar, header.bar").first.click(force=True)
    # The claim is "closes on an outside click", not "within 150 ms" — a
    # fixed wait flaked on busy CI runners twice.
    page.wait_for_selector(".menu", state="detached", timeout=5_000)

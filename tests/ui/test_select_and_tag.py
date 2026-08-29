"""Selection ergonomics: Ctrl+A selects the whole view, and a rectangular
cell range counts as a row selection when a tag key is pressed."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_ctrl_a_selects_every_row_in_the_view(page):
    page.keyboard.press("Control+a")
    page.wait_for_timeout(150)
    assert page.evaluate("() => __winnow.S.selectAll")
    assert page.evaluate("() => document.getElementById('selectAllRows').checked")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert not page.evaluate("() => __winnow.S.selectAll")

    # Inside an input it stays the browser's select-all, not the grid's.
    page.keyboard.press("/")
    page.keyboard.type("abc")
    page.keyboard.press("Control+a")
    page.wait_for_timeout(100)
    assert not page.evaluate("() => __winnow.S.selectAll")
    assert page.evaluate(
        "() => { const i = document.getElementById('search'); return i.selectionEnd - i.selectionStart; }") == 3
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")


def test_tag_key_tags_every_row_a_cell_range_spans(page):
    """Highlighting cells across four rows and pressing a tag key used to
    tag only the cursor's row — the other three needed a separate row
    selection. The range IS the statement of which rows are meant."""
    rows = page.locator(".row")
    rows.nth(2).locator(".cell").nth(1).click()
    rows.nth(5).locator(".cell").nth(3).click(modifiers=["Shift"])
    page.wait_for_timeout(150)
    assert page.evaluate("() => __winnow.S.cellRange.r1 - __winnow.S.cellRange.r0") == 3

    page.keyboard.press("1")  # the TA hotkey
    page.wait_for_function(
        "() => [2, 3, 4, 5].every((p) => (__winnow.rowAt(p) || { tags: [] }).tags.length === 1)")

    page.keyboard.press("1")  # same range, toggles back off — leaves the case clean
    page.wait_for_function(
        "() => [2, 3, 4, 5].every((p) => (__winnow.rowAt(p) || { tags: [] }).tags.length === 0)")

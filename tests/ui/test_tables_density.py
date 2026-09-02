"""The Tables manager as a dense grid: rows are single-line with the name
ellipsizing (buttons never leave the modal), the modal takes the extra
width class, and Compact case file lives in the Case menu now rather than
as a third button here."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_rows_are_grid_dense_and_buttons_stay_inside(page):
    page.evaluate("() => __winnow.openTablesManager()")
    page.wait_for_selector("#modal:not([hidden])")
    card = page.locator(".modal-card")
    assert "xwide" in card.get_attribute("class")
    row = page.locator("#modal .tables-row").first
    box_row = row.bounding_box()
    box_card = card.bounding_box()
    for btn in row.locator(".tables-acts .btn").all():
        bb = btn.bounding_box()
        assert bb["x"] + bb["width"] <= box_card["x"] + box_card["width"] + 1
    # Single-line density: the row is one text line tall, not a wrapped stack.
    assert box_row["height"] < 40
    # No Compact button in the manager any more…
    assert page.locator("#modal button", has_text="Compact case file").count() == 0
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_compact_lives_in_the_case_menu(page):
    page.locator("#btnCase").click()
    item = page.locator(".menu .menu-item", has_text="Compact case file…")
    assert item.count() == 1
    item.click()
    # It opens the confirm dialog (cancel out — actually compacting is slow).
    page.wait_for_selector(".confirm-overlay, .confirm-card, #confirmCard", state="attached", timeout=5000)
    page.keyboard.press("Escape")

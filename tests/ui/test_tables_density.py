"""The Tables manager as the table menu's two panes: its sections down a
side list, Every table a dense list in a box that scrolls, one line a
table with the counts in columns that line up, nothing leaving the
modal — and Compact case file in the Case menu rather than here."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_two_panes_and_dense_rows_that_stay_inside(page):
    page.evaluate("() => __winnow.openTablesManager()")
    page.wait_for_selector("#modal:not([hidden]) .tables-list .tables-row")
    card = page.locator(".modal-card")
    assert "xwide" in card.get_attribute("class")
    rail = [t.inner_text().split("\n")[0] for t in page.locator(".tables-manager .side-rail-item").all()]
    assert rail == ["Every table", "Merges & saved tables", "Indexes"], rail
    assert page.locator('.side-rail-item[aria-selected="true"]').inner_text().startswith("Every table")

    box_card = card.bounding_box()
    row = page.locator("#modal .tables-list .tables-row").first
    # Single-line density: one text line tall, not a wrapped stack.
    assert row.bounding_box()["height"] < 40
    more = row.locator(".tables-more").bounding_box()
    assert more["x"] + more["width"] <= box_card["x"] + box_card["width"] + 1
    # The Rows column lines up under its head.
    head = page.locator(".tables-head .tables-sort[data-sort='row_count']").bounding_box()
    cell = row.locator(".tables-rows").bounding_box()
    assert abs((head["x"] + head["width"]) - (cell["x"] + cell["width"])) <= 2, (head, cell)
    # No Compact button in the manager…
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

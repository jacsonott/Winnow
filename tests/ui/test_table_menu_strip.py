"""The table menu's top strip, and the Columns panel's filter box and
end-of-list arrows.

The everyday actions — Tables manager, Reset view, Close this tab — sit
first now. Under a two-hundred-column list they were a scroll away every
time, which is the wrong price for closing a tab."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open(page):
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector(".collist-row")


def _restore(page, orig):
    page.evaluate("""(o) => { __winnow.S.order = o; __winnow.renderHead(); __winnow.saveLayout(); }""", orig)


def test_the_strip_comes_first_and_holds_the_three_everyday_actions(page):
    _open(page)
    strip = page.locator("#modalBody .table-menu-section").first.locator(".table-menu-strip")
    assert strip.count() == 1, "the first section of the menu is the strip"
    labels = strip.locator("button").all_inner_texts()
    assert labels == ["Tables manager…", "Reset view", "Close this tab"], labels
    # ...and the bottom section no longer repeats them.
    bottom = page.locator("#modalBody .table-menu-section").last
    assert "Tables manager" not in bottom.inner_text()
    assert "Close this tab" not in bottom.inner_text()
    assert "Save layout as default" in bottom.inner_text()


def test_the_filter_box_narrows_the_rows_and_clearing_it_brings_them_back(page):
    _open(page)
    order = page.evaluate("() => [...__winnow.S.order]")
    target = order[1]
    box = page.locator("#modalBody .collist-search")
    box.fill(target.lower())
    visible = page.locator(".collist-row:visible").all_inner_texts()
    assert all(target.lower() in v.lower() for v in visible), visible
    assert any(target in v for v in visible)
    box.fill("")
    assert page.locator(".collist-row:visible").count() == len(order)


def test_the_arrows_send_a_column_to_either_end(page):
    _open(page)
    orig = page.evaluate("() => [...__winnow.S.order]")
    try:
        rows = page.locator(".collist-row")
        # The second row's ⤒: it becomes first in the order, the panel and
        # the grid header alike.
        moved = orig[1]
        rows.nth(1).hover()
        rows.nth(1).locator(".collist-move", has_text="⤒").click()
        assert page.evaluate("() => __winnow.S.order[0]") == moved
        assert page.locator(".collist-row").first.inner_text().startswith(moved) or \
            moved in page.locator(".collist-row").first.inner_text()
        assert page.evaluate("() => document.querySelectorAll('.hcell[data-col]')[0].dataset.col") == moved
        # Then ⤓ on that same row: last in the order.
        page.locator(".collist-row").first.hover()
        page.locator(".collist-row").first.locator(".collist-move", has_text="⤓").click()
        after = page.evaluate("() => [...__winnow.S.order]")
        assert after[-1] == moved
        assert sorted(after) == sorted(orig), "moving must not add or drop columns"
        assert moved in page.locator(".collist-row").last.inner_text()
    finally:
        _restore(page, orig)


def test_moving_a_column_keeps_the_typed_filter(page):
    _open(page)
    orig = page.evaluate("() => [...__winnow.S.order]")
    try:
        target = orig[2]
        page.locator("#modalBody .collist-search").fill(target)
        row = page.locator(".collist-row:visible").first
        row.hover()
        row.locator(".collist-move", has_text="⤒").click()
        assert page.locator("#modalBody .collist-search").input_value() == target
        assert page.evaluate("() => __winnow.S.order[0]") == target
    finally:
        _restore(page, orig)

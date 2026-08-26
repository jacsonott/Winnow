"""The table menu's Columns panel: reordering by drag, not just hide/show.
Hidden columns can only be repositioned here — the grid header drag can't
reach them."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_columns_reorder_by_dragging_panel_rows(page):
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector(".collist-row")
    orig = page.evaluate("() => [...__winnow.S.order]")

    rows = page.locator(".collist-row")
    rows.nth(2).drag_to(rows.nth(0))
    page.wait_for_timeout(400)

    after = page.evaluate("() => [...__winnow.S.order]")
    assert after[0] == orig[2], f"dragged {orig[2]!r} to the front, got {after[:3]}"
    assert sorted(after) == sorted(orig), "reordering must not add or drop columns"
    # ...and the grid header follows immediately.
    assert page.evaluate("() => document.querySelectorAll('.hcell[data-col]')[0].dataset.col") == orig[2]

    # Restore for the other tests sharing this server's saved layout.
    page.evaluate("""(o) => { __winnow.S.order = o; __winnow.renderHead(); __winnow.saveLayout(); }""", orig)

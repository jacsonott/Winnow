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
    # Drop in the top third of the target: wireDragReorder decides
    # before/after against the row's midpoint, and a drop dead on it is a
    # coin toss that any change to the row's height can flip.
    rows.nth(2).drag_to(rows.nth(0), target_position={"x": 10, "y": 3})
    page.wait_for_timeout(400)

    after = page.evaluate("() => [...__winnow.S.order]")
    assert after[0] == orig[2], f"dragged {orig[2]!r} to the front, got {after[:3]}"
    assert sorted(after) == sorted(orig), "reordering must not add or drop columns"
    # ...and the grid header follows immediately.
    assert page.evaluate("() => document.querySelectorAll('.hcell[data-col]')[0].dataset.col") == orig[2]

    # Restore for the other tests sharing this server's saved layout.
    page.evaluate("""(o) => { __winnow.S.order = o; __winnow.renderHead(); __winnow.saveLayout(); }""", orig)

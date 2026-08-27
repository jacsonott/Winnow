"""The gutter's Line header as the way back to file order, and the table
menu's Reset view."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _first_line(page):
    return page.evaluate("() => __winnow.rowAt(0) && __winnow.rowAt(0).rid")


def test_line_header_restores_and_reverses_file_order(page):
    # Sort by Host first — the state you can't get back from today.
    page.evaluate("""() => { __winnow.S.sort = [{ column: 'Host', dir: 'desc' }];
      __winnow.renderHead(); return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function("() => __winnow.S.sort.length === 1 && __winnow.S.sort[0].column === 'Host'")

    page.click(".gutter-head .line-sort")
    page.wait_for_function("() => __winnow.S.sort.length === 0")
    page.wait_for_function("() => __winnow.rowAt(0) && __winnow.rowAt(0).rid === 1")

    page.click(".gutter-head .line-sort")  # again: reverse file order
    page.wait_for_function(
        "() => __winnow.S.sort.length === 1 && __winnow.S.sort[0].column === '__line__'")
    page.wait_for_function("() => __winnow.rowAt(0) && __winnow.rowAt(0).rid === 200")

    page.click(".gutter-head .line-sort")  # back to normal order
    page.wait_for_function("() => __winnow.rowAt(0) && __winnow.rowAt(0).rid === 1")


def test_reset_view_clears_filters_and_restores_default_sort(page):
    page.locator('.fcell input[data-col="EventId"]').fill("=4624")
    page.locator('.fcell input[data-col="EventId"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")
    page.evaluate("""() => { __winnow.S.sort = [{ column: 'Host', dir: 'desc' }];
      return __winnow.rebuildView({ keepScroll: false }); }""")

    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector("#modal:not([hidden])")
    page.click("#modalBody .btn:has-text('Reset view')")
    page.wait_for_function("() => __winnow.S.view.row_count === 200")
    assert page.evaluate("() => __winnow.S.filters") == {}
    # default sort = first datetime column ascending, as on open
    assert page.evaluate("() => __winnow.S.sort") == [{"column": "Timestamp", "dir": "asc"}]

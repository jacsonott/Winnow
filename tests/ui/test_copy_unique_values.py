"""Copy unique values — one column's distinct values, one per line — from
the three places it lives: Ctrl+Shift+U, the row menu's Copy flyout and
the column header menu.

Every case reads the clipboard, not just the toast: the claim worth
pinning is which rows the list was taken from (the picks, a cell range,
or the whole view), and only the pasted text shows that. The fixture's
Host column cycles H0–H4 down its 200 rows, so a slice of rows and the
whole view give visibly different lists.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean(page):
    page.evaluate("""() => {
      __winnow.selClear(); __winnow.clearCellSelection();
      __winnow.S.cursor = 0; __winnow.render();
    }""")
    yield
    page.keyboard.press("Escape")


def _col(page, name):
    return page.evaluate("(n) => __winnow.visibleCols().indexOf(n)", name)


def _select(page, r0, r1, col):
    c = _col(page, col)
    page.evaluate("""([r0, r1, c]) => {
      __winnow.S.cellAnchor = { pos: r0, col: c };
      __winnow.S.cellFocus = { pos: r1, col: c, name: __winnow.visibleCols()[c] };
      __winnow.setCellRange(__winnow.S.cellAnchor, __winnow.S.cellFocus);
      __winnow.S.cellRangeExplicit = true;
      __winnow.render();
    }""", [r0, r1, c])


def _result(page):
    page.wait_for_selector("#toast:not([hidden]):has-text('unique value')", timeout=10_000)
    page.wait_for_timeout(300)
    return (page.locator("#toast").inner_text().strip(),
            page.evaluate("() => navigator.clipboard.readText()"))


def test_the_key_on_one_cell_copies_the_whole_view_sorted(page):
    """Ctrl+Shift+C was the asked-for chord and already means "copy with
    headers", so the action is on Ctrl+Shift+U. One clicked cell narrows
    nothing — one row has one value — so the list is the whole view's."""
    _select(page, 7, 7, "Host")
    page.keyboard.press("Control+Shift+U")
    msg, clip = _result(page)
    assert clip == "H0\nH1\nH2\nH3\nH4", clip
    assert "5 unique values of Host" in msg and "this view" in msg, msg


def test_the_key_on_a_cell_range_copies_those_rows_in_grid_order(page):
    """Rows 3–7 hold H3 H4 H0 H1 H2: first-appearance order, not sorted,
    which is what proves the list came from the range and not the view."""
    _select(page, 3, 7, "Host")
    page.keyboard.press("Control+Shift+U")
    msg, clip = _result(page)
    assert clip == "H3\nH4\nH0\nH1\nH2", clip
    assert "from 5 rows" in msg, msg


def test_the_key_with_picked_rows_copies_from_the_picks(page):
    # Rows 1 and 6 are both H1; the cell cursor sits in Host on row 1.
    page.locator("#body .row").nth(1).locator(".gutter").click()
    page.locator("#body .row").nth(6).locator(".gutter").click()
    page.evaluate("""(c) => {
      __winnow.S.cellAnchor = { pos: 1, col: c };
      __winnow.S.cellFocus = { pos: 1, col: c, name: 'Host' };
      __winnow.setCellRange(__winnow.S.cellAnchor, __winnow.S.cellFocus);
      __winnow.render();
    }""", _col(page, "Host"))
    page.keyboard.press("Control+Shift+U")
    msg, clip = _result(page)
    assert clip == "H1", clip
    assert "1 unique value of Host from 2 rows" in msg, msg


def test_ctrl_shift_c_still_copies_with_headers(page):
    """The chord this feature did not take keeps doing what it did."""
    _select(page, 2, 2, "Host")
    page.keyboard.press("Control+Shift+C")
    page.wait_for_selector("#toast:not([hidden]):has-text('with headers')", timeout=10_000)
    page.wait_for_timeout(300)
    assert page.evaluate("() => navigator.clipboard.readText()") == "Host\nH2"


def test_row_menu_item_names_its_scope_and_its_key(page, row_menu, flyout):
    host = _col(page, "Host")
    row_menu(row=4, cell=host)
    item = flyout("Copy").locator(".menu-item", has_text="Copy unique values")
    text = item.inner_text()
    assert "of Host in the whole view" in text, text
    assert "Ctrl+Shift+U" in text, text   # the binding as pressed, not as stored ("Ctrl+U")
    item.click()
    msg, clip = _result(page)
    assert clip == "H0\nH1\nH2\nH3\nH4", clip


def test_row_menu_inside_a_cell_range_uses_the_range(page, row_menu, flyout):
    _select(page, 10, 12, "Host")   # H0 H1 H2
    row_menu(row=11, cell=_col(page, "Host"))
    item = flyout("Copy").locator(".menu-item", has_text="Copy unique values")
    assert "in 3 selected rows" in item.inner_text(), item.inner_text()
    item.click()
    _, clip = _result(page)
    assert clip == "H0\nH1\nH2", clip


def test_header_menu_copies_the_whole_view_numbers_sorted_as_numbers(page):
    page.locator('.hcell[data-col="EventId"]').click(button="right")
    page.locator(".menu .menu-item", has_text="Copy unique values").click()
    _, clip = _result(page)
    assert clip == "1\n4624\n4625\n4688", clip


def test_whole_view_values_are_copied_as_displayed(page):
    """The server hands back stored text; what reaches the clipboard is
    the column's chosen display format, as with every other copy."""
    page.evaluate("""() => {
      __winnow.S.layout['Timestamp'] = Object.assign({}, __winnow.S.layout['Timestamp'] || {}, { tsFormat: 'us' });
      __winnow.render();
    }""")
    try:
        page.locator('.hcell[data-col="Timestamp"]').click(button="right")
        page.locator(".menu .menu-item", has_text="Copy unique values").click()
        msg, clip = _result(page)
        lines = clip.splitlines()
        assert len(lines) == 200 and "200 unique values" in msg, msg
        assert lines[0].startswith("03/14/2026 08:00:00"), lines[0]
    finally:
        page.evaluate("() => { delete __winnow.S.layout['Timestamp'].tsFormat; __winnow.render(); }")

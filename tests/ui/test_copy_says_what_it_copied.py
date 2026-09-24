"""What the copy toast claims, against what reached the clipboard.

Ctrl+C on a cell rectangle reported the ROWS it spanned: one cell read
"Copied 1 row", and a block four rows by three columns read "Copied 4
rows" — which claims whole rows came across when only three columns did.
The row count alone cannot describe a rectangle, because a rectangle has
a width.

Each case here reads the toast AND the clipboard, so the claim is checked
against the thing it is a claim about. A wording-only assertion would
have passed happily while the two disagreed.
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


def _select(page, r0, r1, c0, c1):
    page.evaluate("""([r0, r1, c0, c1]) => {
      __winnow.S.cellAnchor = { pos: r0, col: c0 };
      __winnow.S.cellFocus = { pos: r1, col: c1, name: __winnow.visibleCols()[c1] };
      __winnow.setCellRange(__winnow.S.cellAnchor, __winnow.S.cellFocus);
      __winnow.S.cellRangeExplicit = true;
      __winnow.render();
    }""", [r0, r1, c0, c1])


def _copy(page):
    page.keyboard.press("Control+c")
    page.wait_for_selector("#toast:not([hidden])", timeout=10_000)
    page.wait_for_timeout(350)
    return (page.locator("#toast").inner_text().strip(),
            page.evaluate("() => navigator.clipboard.readText()"))


def test_one_cell_says_one_cell(page):
    _select(page, 2, 2, 0, 0)
    msg, clip = _copy(page)
    assert "1 cell" in msg, msg
    assert "row" not in msg.lower(), msg
    assert len(clip.splitlines()) == 1 and "\t" not in clip, clip


def test_a_rectangle_says_its_shape(page):
    _select(page, 2, 5, 0, 2)
    msg, clip = _copy(page)
    lines = clip.strip().splitlines()
    assert len(lines) == 4 and len(lines[0].split("\t")) == 3, clip
    # The shape, because "12 cells" leaves you guessing what to expect
    # when you paste it.
    assert "4 rows" in msg and "3 columns" in msg, msg


def test_a_single_column_of_cells_is_not_whole_rows(page):
    """The case the old wording got closest to being right, and still
    wasn't: three cells down one column is not three rows."""
    _select(page, 1, 3, 1, 1)
    msg, clip = _copy(page)
    assert "\t" not in clip, clip
    assert "3 rows" in msg and "1 column" in msg, msg


def test_copying_whole_rows_still_says_rows(page):
    """The row path is not the one that was wrong — picked rows really do
    copy every column, and must keep saying so."""
    page.locator("#body .row").nth(1).locator(".gutter").click()
    page.locator("#body .row").nth(2).locator(".gutter").click()
    msg, clip = _copy(page)
    assert "2 rows" in msg and "column" not in msg, msg
    assert len(clip.strip().splitlines()) == 2, clip

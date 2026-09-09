"""The row number selects its row.

The checkbox was the only way to pick a row: a 12px target that has to be
aimed at, sitting beside a number that looked just as clickable and did
nothing. Clicking the number now does what ticking the box does, and
dragging down the column paints that choice onto the rows it crosses.

Asserted through the checkbox's own state, because "functions as the
checkbox does" is the claim — not through a class the implementation
happens to add.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _rid(page, row):
    return page.locator("#body .row").nth(row).locator(".rid")


def _checked(page, row):
    return page.locator("#body .row").nth(row).locator(".rowcheck").is_checked()


def _selected_count(page):
    return page.evaluate("() => __winnow.selCount()")


@pytest.fixture(autouse=True)
def _clear(page):
    yield
    page.evaluate("() => { __winnow.selClear(); __winnow.render(); }")


def test_clicking_the_row_number_ticks_the_box(page):
    assert not _checked(page, 2)
    _rid(page, 2).click()
    page.wait_for_function("() => __winnow.selCount() === 1")
    assert _checked(page, 2)


def test_clicking_it_again_unticks_it(page):
    _rid(page, 3).click()
    page.wait_for_function("() => __winnow.selCount() === 1")
    _rid(page, 3).click()
    page.wait_for_function("() => __winnow.selCount() === 0")
    assert not _checked(page, 3)


def test_it_toggles_one_row_and_leaves_the_others(page):
    """The checkbox's own semantics — not a cell click's, which replaces
    the selection."""
    _rid(page, 1).click()
    _rid(page, 4).click()
    page.wait_for_function("() => __winnow.selCount() === 2")
    assert _checked(page, 1) and _checked(page, 4)
    assert not _checked(page, 2)


def test_shift_click_extends_from_the_last_one(page):
    _rid(page, 2).click()
    page.wait_for_function("() => __winnow.selCount() === 1")
    _rid(page, 6).click(modifiers=["Shift"])
    page.wait_for_function("() => __winnow.selCount() === 5")
    for r in range(2, 7):
        assert _checked(page, r), r


def test_dragging_down_the_column_paints_the_rows_it_crosses(page):
    start = _rid(page, 1).bounding_box()
    end = _rid(page, 5).bounding_box()
    page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    page.mouse.down()
    page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=12)
    page.mouse.up()
    page.wait_for_function("() => __winnow.selCount() === 5")
    for r in range(1, 6):
        assert _checked(page, r), r


def test_a_cell_click_still_replaces_the_selection(page):
    """The row number is the checkbox; the cells are unchanged."""
    _rid(page, 1).click()
    _rid(page, 2).click()
    page.wait_for_function("() => __winnow.selCount() === 2")
    page.locator("#body .row").nth(7).locator(".cell").first.click()
    page.wait_for_function("() => __winnow.selCount() === 0")


def test_the_selection_survives_and_can_be_tagged(page):
    """The point of selecting rows at all."""
    _rid(page, 0).click()
    _rid(page, 1).click()
    page.wait_for_function("() => __winnow.selCount() === 2")
    page.keyboard.press("1")
    page.wait_for_function("() => (__winnow.rowAt(0) || { tags: [] }).tags.length === 1")
    assert page.evaluate("() => __winnow.rowAt(1).tags.length") == 1
    page.keyboard.press("1")
    page.wait_for_function("() => (__winnow.rowAt(0) || { tags: [] }).tags.length === 0")

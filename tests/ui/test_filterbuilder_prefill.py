"""Opening the filter builder shows what's already narrowing the grid: the
header boxes' quick filters are absorbed into the tree (moved, not copied —
the box clears, the rows don't change), and an applied saved filter's tree
is simply rendered. Neither used to populate anything visible."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _filter_box(page, col):
    return page.locator(f'.fcell input[data-col="{col}"]')


def test_quick_filter_populates_the_builder(page):
    _filter_box(page, "Host").fill("=H1")
    page.keyboard.press("Enter")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")

    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    cond = page.locator("#modal .fb-cond")
    assert cond.count() == 1
    assert cond.locator("select").nth(0).input_value() == "Host"
    assert cond.locator("select").nth(1).input_value() == "equals"
    assert cond.locator("input").input_value() == "H1"
    # Moved, not copied — the header box is empty now.
    assert _filter_box(page, "Host").input_value() == ""

    # Apply keeps the same 40 rows, now via the tree.
    page.locator("#modal button", has_text="Apply").first.click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
    page.locator("#btnReset").click()
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")


def test_multi_value_and_saved_filter_populate(page):
    # a|b quick syntax lands as one 'is any of' condition, one value per line.
    _filter_box(page, "EventId").fill("4624|4625")
    page.keyboard.press("Enter")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 100")
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    cond = page.locator("#modal .fb-cond")
    assert cond.count() == 1
    assert cond.locator("select").nth(1).input_value() == "in"
    assert cond.locator("textarea").input_value() == "4624\n4625"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] }; }")
    page.locator("#btnReset").click()
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")

    # An applied saved filter's tree renders in the builder (pin the path
    # that already worked so it stays working).
    page.evaluate("""() => __winnow.applyPreset({ name: 'p', payload: { filter_tree: {
      type: 'group', op: 'AND', children: [
        { type: 'cond', column: 'Host', op: 'equals', value: 'H2' }] } } })""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    cond2 = page.locator("#modal .fb-cond")
    assert cond2.count() == 1
    assert cond2.locator("input").input_value() == "H2"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] }; }")
    page.locator("#btnReset").click()
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")

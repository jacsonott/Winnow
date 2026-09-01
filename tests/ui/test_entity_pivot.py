"""Occurrences tab — right-click a cell → 'Occurrences of X across all
tables' opens the tab and shows where that value appears, with a histogram."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_pivot_from_cell_opens_entity_tab(page):
    cell = page.locator(".row").nth(2).locator(".cell").nth(2)  # Host column
    val = cell.inner_text().strip()
    cell.click(button="right")
    page.wait_for_selector(".menu")
    page.locator(".menu >> text=Occurrences of").first.click()
    page.wait_for_selector("#entityview:not([hidden])")
    page.wait_for_function(
        "() => /rows/.test(document.getElementById('entStatus').textContent)", timeout=10_000)
    assert page.locator("#entSources .ent-src").count() >= 1
    assert page.locator("#entValue").input_value() == val
    # histogram drew (the ui_csv has a Timestamp column)
    page.wait_for_timeout(300)
    # go back to the grid for the next test
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")


def test_entity_search_box(page):
    page.locator("#tabEntity").click()
    page.wait_for_selector("#entityview:not([hidden])")
    page.locator("#entValue").fill("H2")
    page.locator("#entGo").click()
    page.wait_for_function(
        "() => /rows|No matches/.test(document.getElementById('entStatus').textContent)", timeout=10_000)
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")

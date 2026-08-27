"""SQL pane result interactions: selection, tag hotkeys, and double-click
to the row's table — active whenever the query resolves real rows."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _run(page, sql):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table")


def test_select_tag_and_untag_result_rows(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 10")
    rows = page.locator("#sqlResult tr")
    rows.nth(1).click()
    rows.nth(3).click()
    page.wait_for_timeout(150)
    assert page.locator(".sql-row-sel").count() == 2

    page.keyboard.press("1")
    page.wait_for_function("() => document.querySelectorAll('.sql-tag-chip').length === 2")
    assert page.locator(".sql-row-sel").count() == 2, "selection must survive the repaint"

    page.keyboard.press("1")  # toggles off — leaves the shared case clean
    page.wait_for_function("() => document.querySelectorAll('.sql-tag-chip').length === 0")


def test_shift_click_selects_a_display_order_range(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 10")
    rows = page.locator("#sqlResult tr")
    rows.nth(2).click()
    rows.nth(6).click(modifiers=["Shift"])
    page.wait_for_timeout(150)
    assert page.locator(".sql-row-sel").count() == 5


def test_aggregated_results_offer_no_row_interactions(page):
    _run(page, "SELECT EventId, COUNT(*) FROM src_1 GROUP BY 1")
    heads = page.locator("#sqlResult th")
    assert "Tags" not in [heads.nth(i).inner_text() for i in range(heads.count())]
    assert page.locator(".sql-row-sel").count() == 0


def test_double_click_opens_the_row_in_its_table(page):
    _run(page, "SELECT rid, EventId FROM src_1 WHERE rid = 7")
    page.locator("#sqlResult tr").nth(1).dblclick()
    page.wait_for_function("() => __winnow.S.activeTab === 'grid'")
    page.wait_for_function("() => __winnow.S.cursor >= 0 && __winnow.rowAt(__winnow.S.cursor) && __winnow.rowAt(__winnow.S.cursor).rid === 7")

"""The SQL pane's Tags column, once.

A pane view already carries a Tags column (the names, comma-joined), and
the renderer joined a second one (the chips) whenever rid resolved — so
`SELECT * FROM src_1` showed two Tags columns saying the same thing. The
chips now land in the view's column when it's in the result."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


def _run(page, sql):
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_function("() => /tags joined via rid/.test(document.querySelector('#sqlResult').textContent)")


def _headers(page):
    heads = page.locator("#sqlResult th")
    return [heads.nth(i).inner_text() for i in range(heads.count())]


def test_the_views_tags_column_hosts_the_chips_instead_of_a_second_column(page):
    page.locator(".row").nth(0).click()
    page.keyboard.press("1")
    page.wait_for_function("() => __winnow.rowAt(0).tags.length === 1")
    rid = page.evaluate("() => __winnow.rowAt(0).rid")
    try:
        _open_sql(page)
        _run(page, f"SELECT rid, EventId, Tags FROM src_1 WHERE rid = {rid}")
        assert _headers(page) == ["rid", "EventId", "Tags"]
        assert page.locator(".sql-tag-chip").count() == 1
        # ...and the chip sits in the Tags column, not off the end.
        assert page.locator("#sqlResult tr").nth(1).locator("td").nth(2).locator(".sql-tag-chip").count() == 1

        # SELECT * carries every view column; still exactly one Tags header.
        _run(page, f"SELECT * FROM src_1 WHERE rid = {rid}")
        heads = _headers(page)
        assert heads.count("Tags") == 1, heads
        assert page.locator(".sql-tag-chip").count() == 1

        # Without the view's column in the result the chips still get their own.
        _run(page, f"SELECT rid, EventId FROM src_1 WHERE rid = {rid}")
        assert _headers(page) == ["rid", "EventId", "Tags"]
        assert page.locator(".sql-tag-chip").count() == 1
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        page.locator(".row").nth(0).click()
        page.keyboard.press("1")
        page.wait_for_function("() => __winnow.rowAt(0).tags.length === 0")

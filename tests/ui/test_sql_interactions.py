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
    rows.nth(3).click(modifiers=["Control"])  # grid parity: plain click replaces, Ctrl adds
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


def test_selection_parity_click_ctrl_shift_escape_copy(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 10")
    rows = page.locator("#sqlResult tr")
    rows.nth(1).click()
    rows.nth(4).click()  # plain click REPLACES the selection
    page.wait_for_timeout(100)
    assert page.locator(".sql-row-sel").count() == 1
    rows.nth(2).click(modifiers=["Control"])  # Ctrl toggles in place
    page.wait_for_timeout(100)
    assert page.locator(".sql-row-sel").count() == 2
    rows.nth(6).click(modifiers=["Shift"])  # Shift replaces with anchor→here
    page.wait_for_timeout(100)
    assert page.locator(".sql-row-sel").count() == 5

    page.keyboard.press("Control+c")  # copies the selection as TSV
    page.wait_for_timeout(150)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert clip.startswith("rid\tEventId") and len(clip.strip().splitlines()) == 6

    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert page.locator(".sql-row-sel").count() == 0


def _wait_fetch_quiet(page, quiet_ms=600, timeout_s=20):
    """Waits until the page has made no fetches at all for `quiet_ms`.

    wait_for_load_state('networkidle') is useless here — the lifecycle
    event already fired at page load, so mid-session it resolves
    immediately. This counts real fetch() activity instead."""
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        page.wait_for_function("() => window.__inflightFetches === 0")
        seen = page.evaluate("() => window.__totalFetches")
        page.wait_for_timeout(quiet_ms)
        if page.evaluate("(n) => window.__inflightFetches === 0 && window.__totalFetches === n", seen):
            return
    raise AssertionError("page fetch activity never went quiet")


def test_result_toolbar_and_save_as_table(page):
    # Count fetch activity so the cleanup below can wait for true quiet.
    page.evaluate("""() => {
      window.__inflightFetches = 0;
      window.__totalFetches = 0;
      const orig = window.fetch;
      window.fetch = (...a) => {
        window.__inflightFetches++;
        window.__totalFetches++;
        return orig(...a).finally(() => window.__inflightFetches--);
      };
    }""")
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 5")
    assert page.locator(".sql-result-bar .btn", has_text="CSV").count() == 1
    page.locator(".sql-result-bar .btn", has_text="Save as table").click()
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill("saved-from-sql")
    page.locator(".confirm-card .btn", has_text="OK").first.click()
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'saved-from-sql')")
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'saved-from-sql').id")
    assert page.evaluate("(id) => __winnow.S.sources.find((s) => s.id === id).row_count", sid) == 5
    # Saving ends by OPENING the new table (layout, tags, view, first page,
    # plus fire-and-forget follow-ups). Deleting it out from under those
    # in-flight fetches 500s as an uncaught rejection the conftest teardown
    # rightly fails the test for — so wait for true fetch quiet, hand the
    # grid back to the fixture table, wait again, and only then drop the
    # saved one, when nothing can still be asking for it.
    _wait_fetch_quiet(page)
    page.evaluate("() => __winnow.openSource(1)")
    page.wait_for_function("() => __winnow.S.sourceId === 1")
    _wait_fetch_quiet(page)
    page.evaluate("""(id) => fetch('/api/source/' + id, { method: 'DELETE',
      headers: { 'X-Timeline-Lite-Client': '1' } })""", sid)
    # A deleted source's id can be reused — same stale-view guard as the
    # Tables manager's own remove path.
    page.evaluate("(id) => __winnow.S.viewCache.delete(id)", sid)
    page.evaluate("() => __winnow.loadSources()")


def test_copy_schema_and_rightclick_rename(page):
    _run(page, "SELECT 1")
    page.click("#btnSqlSchema")
    page.wait_for_timeout(150)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert "src_1" in clip and "ui.csv" in clip

    tab = page.locator("#sqlTabs .sql-tab").first
    tab.click(button="right")
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill("renamed-by-rightclick")
    page.locator(".confirm-card .btn", has_text="OK").first.click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('.sql-tab-name')].some((n) => n.textContent === 'renamed-by-rightclick')")

"""SQL pane assists: autocomplete, the Tables insert menu, sortable
results, the rid-joined Tags column, and the resize bar."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    # The tab list (and the starter query it seeds into the editor) loads
    # async — filling the editor before that lands gets overwritten.
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


def test_autocomplete_keywords_tables_and_columns(page):
    _open_sql(page)
    ta = page.locator("#sqlText")
    ta.click()
    ta.fill("")
    ta.type("SEL")
    page.wait_for_selector(".sql-ac")
    page.keyboard.press("Tab")
    assert ta.input_value() == "SELECT"

    ta.type(" * FROM ui")  # the fixture table is ui.csv
    page.wait_for_selector(".sql-ac")
    first = page.locator(".sql-ac .menu-item").first.inner_text()
    assert "src_1" in first
    page.keyboard.press("Tab")
    assert ta.input_value() == "SELECT * FROM src_1"

    ta.type(" WHERE Even")
    page.wait_for_selector(".sql-ac")
    page.keyboard.press("Tab")
    assert ta.input_value() == "SELECT * FROM src_1 WHERE EventId"
    page.keyboard.press("Escape")


def test_tables_menu_inserts_the_src_name(page):
    _open_sql(page)
    page.locator("#sqlText").fill("")
    page.click("#btnSqlTables")
    page.wait_for_selector(".menu .menu-item")
    item = page.locator(".menu .menu-item").first
    assert "src_1" in item.inner_text() and "ui.csv" in item.inner_text()
    item.click()
    assert page.locator("#sqlText").input_value() == "src_1"


def test_results_sort_by_clicking_headers_and_show_tags(page):
    # Tag the cursor row so the Tags column has something to prove.
    page.locator(".row").nth(0).click()
    page.keyboard.press("1")
    page.wait_for_function("() => __winnow.rowAt(0).tags.length === 1")
    rid = page.evaluate("() => __winnow.rowAt(0).rid")

    _open_sql(page)
    page.locator("#sqlText").fill(f"SELECT rid, EventId FROM src_1 WHERE rid = {rid}")
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table")
    heads = page.locator("#sqlResult th")
    assert [heads.nth(i).inner_text() for i in range(heads.count())] == ["rid", "EventId", "Tags"]
    assert page.locator(".sql-tag-chip").count() == 1
    assert page.locator(".sql-tag-chip").inner_text() == "TA"

    # ORDER BY rid matters: without it SQLite may walk a lazy column index
    # (built by earlier tests' filters) and hand back 20 rows sharing one
    # EventId — making the client sort a visual no-op.
    page.locator("#sqlText").fill("SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 20")
    page.click("#btnRunSql")
    page.wait_for_function("() => document.querySelectorAll('#sqlResult tr').length === 21")

    cell = lambda: page.locator("#sqlResult tr").nth(1).locator("td").nth(1).inner_text()  # noqa: E731
    page.locator("#sqlResult th", has_text="EventId").click()
    asc = cell()
    page.locator("#sqlResult th", has_text="EventId").click()
    desc = cell()
    assert asc == "1" and desc == "4688"  # EventId cycles 4624/4625/4688/1

    # cleanup: untag
    page.evaluate("() => __winnow.showGridTab()")
    page.locator(".row").nth(0).click()
    page.keyboard.press("1")
    page.wait_for_function("() => __winnow.rowAt(0).tags.length === 0")


def test_query_box_resizes_from_the_bar(page):
    _open_sql(page)
    h0 = page.evaluate("() => document.getElementById('sqlText').offsetHeight")
    bar = page.locator("#sqlResize").bounding_box()
    page.mouse.move(bar["x"] + 200, bar["y"] + 3)
    page.mouse.down()
    page.mouse.move(bar["x"] + 200, bar["y"] + 120, steps=5)
    page.mouse.up()
    h1 = page.evaluate("() => document.getElementById('sqlText').offsetHeight")
    assert h1 > h0 + 80


def test_autocomplete_marks_and_completes_derived_columns(page):
    """Derived columns were always suggested — before the shadow views
    that was a trap (the query returned the literal column name). Now the
    suggestion is real and tagged 'derived'."""
    rec = page.evaluate("""() => fetch('/api/derived', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_id: 1, name: 'AcBinary', input_column: 'CommandLine',
                             op_id: 'regex_extract', params: { pattern: '\\\\\\\\([\\\\w.]+\\\\.exe)' } }) })
      .then((r) => r.json())""")
    try:
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function("() => __winnow.S.sources[0].columns.some((c) => c.name === 'AcBinary')")
        # ...and for the BACKFILL, not just the definition — querying before
        # the drv rows land would read NULL and flake. Polled from Python:
        # page.evaluate awaits promises, wait_for_function does NOT (a
        # promise-returning predicate is truthy immediately).
        import time as _time

        deadline = _time.time() + 15
        while _time.time() < deadline:
            if page.evaluate("""() => fetch('/api/derived?source_id=1',
              { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())
              .then((defs) => defs.some((d) => d.name === 'AcBinary' && d.status === 'ready'))"""):
                break
            _time.sleep(0.2)
        else:
            raise AssertionError("AcBinary never finished building")

        _open_sql(page)
        ta = page.locator("#sqlText")
        ta.click()
        ta.fill("SELECT * FROM src_1 WHERE AcBin")
        page.wait_for_selector(".sql-ac")
        first = page.locator(".sql-ac .menu-item").first
        assert "AcBinary" in first.inner_text() and "derived" in first.inner_text()
        page.keyboard.press("Tab")
        assert ta.input_value().endswith("WHERE AcBinary")

        # ...and the completed query actually reads the derived value
        ta.fill("SELECT AcBinary FROM src_1 WHERE rid = 1")
        page.click("#btnRunSql")
        page.wait_for_selector("#sqlResult table")
        assert page.locator("#sqlResult td").nth(0).inner_text() == "powershell.exe"
    finally:
        # The server is session-scoped — a leaked column would haunt every
        # later test's grid and autocomplete.
        page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
          headers: { 'X-Timeline-Lite-Client': '1' } })""", rec["definition"]["id"])
        page.evaluate("() => __winnow.loadSources()")


def _hold_first_sql_run(page):
    """Intercept /api/sql: the first run is held (its route returned for
    the test to answer whenever it likes), later ones go straight through.
    That makes "a slow run is still in flight" a state the test controls
    rather than a query it hopes is slow enough (test_search_supersede.py's
    idiom)."""
    import re

    held = []

    def handler(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    pattern = re.compile(r".*/api/sql$")
    page.route(pattern, handler)
    return held, pattern


def _answer_late(route, column):
    """Answer a held run with a one-cell result naming `column` — or
    nothing, if the page already gave up on it (which is the fix)."""
    import json

    try:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"columns": [column], "rows": [[1]], "elapsed_ms": 1, "truncated": False}))
    except Exception:
        pass


def test_a_slow_run_does_not_paint_over_the_run_after_it(page):
    """runSql painted by the tab id it started from and nothing else, so a
    slow run that came back after a quicker re-run in the same tab put
    its rows over the newer result — and Copy/CSV/tagging then took the
    old query's rows."""
    _open_sql(page)
    held, pattern = _hold_first_sql_run(page)
    try:
        page.locator("#sqlText").fill("SELECT 1 AS slow_one")
        page.click("#btnRunSql")
        page.wait_for_function("() => document.getElementById('sqlResult').textContent.startsWith('Running')")
        assert held, "the first run was not intercepted"
        page.locator("#sqlText").fill("SELECT 2 AS quick_one")
        page.click("#btnRunSql")
        page.wait_for_function("() => /quick_one/.test(document.getElementById('sqlResult').textContent)")

        _answer_late(held[0], "slow_one")
        page.wait_for_timeout(500)
        assert "quick_one" in page.locator("#sqlResult").inner_text()
        assert "slow_one" not in page.locator("#sqlResult").inner_text()
        assert page.evaluate("() => __winnow.S.sqlResults.get(__winnow.S.sqlTabId).columns") == ["quick_one"]
        assert page.evaluate("() => __winnow.busyCount") == 0
    finally:
        page.unroute(pattern)

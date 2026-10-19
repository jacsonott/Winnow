"""A SQL run lands only where it was asked for, and can be stopped.

A run is awaited, and while it is out the analyst can do three things
that used to send its result to the wrong place:

* run again in the same tab — a slow first run landing after a fast
  second one replaced it on screen, and Copy / CSV / Excel then took the
  slow run's rows;
* close the tab and open a new one — sql_tabs ids were reused, so the
  closed tab's result appeared in the new, empty tab (ids are never
  reused now, but the closed tab's query still has to stop);
* open another case — the result painted under the new case's "Query 1",
  and tagging rows in it wrote tags into the wrong case.

Each run carries a token, and a result paints only while its token is
still the one its tab is waiting on. The same token is what Stop
cancels: a runaway query (a recursive CTE with no stop condition) used
to be stoppable by nothing short of killing Winnow.

The runaway below never ends on its own, so every test cancels whatever
it started on the way out, pass or fail: a query left running would hold
the shared server's pane for every module after this one."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

RUNAWAY = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT i FROM n"
# Never returns a first row: the aggregate has to read all of n first.
SILENT = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT max(i) FROM n"
QUICK = "SELECT rid FROM src_1 WHERE rid <= 3"


@pytest.fixture
def sql(page):
    """The SQL page, open, with the active tab's text put back afterwards
    and every run this test started cancelled."""
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.evaluate("() => { window.__tokens = []; }")
    original = page.evaluate("() => [__winnow.S.sqlTabId, document.getElementById('sqlText').value]")
    yield page
    page.evaluate("""async () => {
        const tokens = [...(window.__tokens || []), ...__winnow.S.sqlRuns.values()];
        for (const token of tokens) await __winnow.post('/api/cancel_op', { token }).catch(() => {});
        if (window.__run) await window.__run;
        window.__run = null;
    }""")
    page.evaluate("""async ([id, text]) => {
        if (!__winnow.S.sqlTabs.some((t) => t.id === id)) return;
        const m = await import('/static/js/sql.js');
        await m.activateSqlTab(id);
        const box = document.getElementById('sqlText');
        box.value = text;
        box.dispatchEvent(new Event('input', { bubbles: true }));
        await m.flushSqlTabSave();
    }""", original)


def _start(page, query):
    """Start a run the way the Run button does, keeping its promise on
    window.__run so the test can wait for exactly that run to finish."""
    return page.evaluate("""async (query) => {
        const m = await import('/static/js/plugins.js');
        const box = document.getElementById('sqlText');
        box.value = query;
        box.dispatchEvent(new Event('input', { bubbles: true }));
        const tabId = __winnow.S.sqlTabId;
        window.__run = m.runSql();
        const token = __winnow.S.sqlRuns.get(tabId);
        window.__tokens.push(token);
        return { tabId, token };
    }""", query)


def _progress(page, token):
    return page.evaluate(
        "(t) => __winnow.api('/api/sql/run/progress?token=' + encodeURIComponent(t))", token)


def _wait_for_rows(page, token):
    """Until the server has stored some of the run's rows — so it is
    really running, not queued behind something."""
    for _ in range(150):   # polled with evaluate, which awaits — see test_ui_test_hygiene
        p = _progress(page, token)
        if p["running"] and p["rows"] > 0:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the run never started landing rows: {p}")


def _wait_until_stopped(page, token, why):
    """Until the server no longer counts the run as in flight. A run its
    tab abandoned is aborted on the client at once, but its thread still
    finishes on the server — notices the cancel, sizes the rows it kept —
    and can be doing that after the next run has already painted. The
    runaway never ends on its own, so one left running still fails here."""
    for _ in range(150):   # polled with evaluate, which awaits — see test_ui_test_hygiene
        p = _progress(page, token)
        if not p["running"]:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"{why}: {p}")


def _status(page):
    return page.locator(".sql-result-bar .note-status").inner_text()


def test_stop_keeps_the_rows_a_runaway_query_had_returned(sql):
    page = sql
    page.locator("#sqlText").fill(RUNAWAY)
    page.click("#btnRunSql")
    token = page.evaluate("() => __winnow.S.sqlRuns.get(__winnow.S.sqlTabId)")
    page.evaluate("(t) => window.__tokens.push(t)", token)
    # What the analyst watches while it runs: a count that moves.
    page.wait_for_function(
        "() => /[1-9][\\d,]* rows so far/.test(document.querySelector('.sql-running-count')?.textContent || '')")
    page.locator(".sql-running .btn", has_text="Stop").click()
    page.wait_for_selector(".sql-result-bar .note-status")
    status = _status(page)
    assert "stopped" in status
    assert int(status.split(" rows")[0].replace(",", "")) > 0
    assert page.locator('#sqlResult tbody tr[data-i="0"] td').first.inner_text() == "1"
    assert not _progress(page, token)["running"], "the query is still running on the server"


def test_stop_before_the_first_row_says_so(sql):
    page = sql
    page.locator("#sqlText").fill(SILENT)
    page.click("#btnRunSql")
    token = page.evaluate("() => __winnow.S.sqlRuns.get(__winnow.S.sqlTabId)")
    page.evaluate("(t) => window.__tokens.push(t)", token)
    page.wait_for_selector(".sql-running .btn")
    page.locator(".sql-running .btn", has_text="Stop").click()
    page.wait_for_selector("#sqlResult .sql-error")
    assert page.locator("#sqlResult .sql-error").inner_text() == "Query cancelled"
    assert not _progress(page, token)["running"]


def test_a_rerun_is_not_overwritten_by_the_run_it_replaced(sql):
    page = sql
    first = _start(page, RUNAWAY)
    _wait_for_rows(page, first["token"])
    page.locator("#sqlText").fill(QUICK)
    page.click("#btnRunSql")
    page.wait_for_function(
        "() => /^3 rows/.test(document.querySelector('.sql-result-bar .note-status')?.textContent || '')")
    landed = page.evaluate("() => window.__run")
    assert landed.get("error"), "the replaced run should not have come back as a result"
    # The quick result is still the one on screen and the one the exports
    # read — and the replaced query was stopped, not left to run.
    assert _status(page).startswith("3 rows")
    assert page.evaluate("(id) => __winnow.S.sqlResults.get(id).total", first["tabId"]) == 3
    _wait_until_stopped(page, first["token"], "the replaced query is still running on the server")


def test_a_closed_tabs_run_stops_and_lands_nowhere(sql):
    page = sql
    tabs = page.evaluate("() => __winnow.S.sqlTabs.length")
    page.locator("#sqlTabs .sql-tab-add").click()
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n", arg=tabs + 1)
    run = _start(page, RUNAWAY)
    _wait_for_rows(page, run["token"])
    page.locator("#sqlTabs .sql-tab[aria-selected=true] .x").click()
    page.locator(".confirm-card .btn", has_text="Close").first.click()
    page.wait_for_function("(id) => !__winnow.S.sqlTabs.some((t) => t.id === id)", arg=run["tabId"])
    page.locator("#sqlTabs .sql-tab-add").click()
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n", arg=tabs + 1)
    new_id = page.evaluate("() => __winnow.S.sqlTabId")
    try:
        landed = page.evaluate("() => window.__run")
        assert landed.get("error")
        assert page.locator(".sql-result-bar").count() == 0, "the closed tab's result is in the new tab"
        for tab in (new_id, run["tabId"]):
            assert not page.evaluate("(id) => __winnow.S.sqlResults.has(id)", tab)
        _wait_until_stopped(page, run["token"], "closing the tab left its query running")
    finally:
        # The new tab is empty, so closing it asks nothing.
        page.evaluate("""(id) => import('/static/js/sql.js').then((m) => {
            const t = __winnow.S.sqlTabs.find((x) => x.id === id);
            return t && m.closeSqlTab(t);
        })""", new_id)


def test_a_run_from_the_last_case_does_not_land_in_this_one(sql):
    """Driven through resetSqlPane rather than through a second case: the
    UI suite shares one server and one case file (tests/ui/conftest.py).
    resetSqlPane is the seam a case switch goes through, and reloading
    the same case's tabs afterwards gives the same tab ids — exactly what
    the next case's "Query 1" did. Closing the old Store is what stopped
    its query; the cancel below stands in for it."""
    page = sql
    run = _start(page, RUNAWAY)
    _wait_for_rows(page, run["token"])
    page.evaluate("() => __winnow.resetSqlPane()")
    page.evaluate("() => __winnow.showGridTab()")
    page.click("#tabSql")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0")
    page.evaluate("(t) => __winnow.post('/api/cancel_op', { token: t })", run["token"])
    landed = page.evaluate("() => window.__run")
    assert landed.get("error")
    assert not page.evaluate("(id) => __winnow.S.sqlResults.has(id)", run["tabId"])
    page.evaluate("(id) => import('/static/js/sql.js').then((m) => m.activateSqlTab(id))", run["tabId"])
    assert page.locator(".sql-result-bar").count() == 0, "the old case's result is on screen"
    # Nothing a tag hotkey or Ctrl+C could act on.
    assert page.evaluate("() => import('/static/js/sqlassist.js').then((m) => m.sqlSelectionCount())") == 0

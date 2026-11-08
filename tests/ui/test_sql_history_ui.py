"""The SQL pane's query history (History ▾, static/js/sqlhistory.js).

A query tab is a draft that autosaves as it is edited, so the query behind
a number found an hour ago has usually been edited into the next idea.
The history keeps what actually ran. What is pinned here is what would
quietly make it lie or cost the analyst work:

* a run is recorded by the PANE, when it lands — and a run that was
  superseded (run again before it answered) is not, since nobody got an
  answer from it;
* opening an entry never overwrites a draft: it replaces the editor's
  text without asking only when that text is blank, a starter, or itself
  in the history, and otherwise asks, with a new tab offered beside
  replacing it;
* opening in a new tab is the analyst's choice: "Open in a new tab" (off
  by default, remembered in this browser) or Ctrl/⌘+click;
* the list is newest first, filters by every word typed, and Enter opens
  the first match.

The history is per case and every module in a shard shares the case, so
each test clears it first rather than trusting what an earlier module
ran.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

QUICK = "SELECT rid, Host FROM src_1 WHERE rid <= 7"
# Never ends on its own; see test_sql_run_tokens.py.
RUNAWAY = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT i FROM n"
# The panel's "Open in a new tab" lives here (sqlhistory.js PREFS_KEY).
PREFS_KEY = "winnow.sqlHistory"


@pytest.fixture
def sql(page, api, open_sql):
    """The SQL pane with an empty history, its tabs and the active tab's
    text put back afterwards, and the history left empty."""
    api("/api/sql_history", "DELETE")
    page.evaluate("(k) => localStorage.removeItem(k)", PREFS_KEY)
    open_sql(page)
    tabs_before = {t["id"] for t in api("/api/sql_tabs")}
    original = page.evaluate("() => [__winnow.S.sqlTabId, document.getElementById('sqlText').value]")
    try:
        yield page
    finally:
        page.evaluate("""async ([id, text]) => {
            if (!__winnow.S.sqlTabs.some((t) => t.id === id)) return;
            await __winnow.activateSqlTab(id);
            const box = document.getElementById('sqlText');
            box.value = text;
            box.dispatchEvent(new Event('input', { bubbles: true }));
            await __winnow.flushSqlTabSave();
        }""", original)
        for t in api("/api/sql_tabs"):
            if t["id"] not in tabs_before:
                api(f"/api/sql_tabs/{t['id']}", "DELETE")
        api("/api/sql_history", "DELETE")
        page.evaluate("(k) => localStorage.removeItem(k)", PREFS_KEY)


def _set_editor(page, text):
    """Type into the editor and wait for the autosave, the way an analyst
    who stopped typing would have it."""
    page.locator("#sqlText").fill(text)
    page.evaluate("() => __winnow.flushSqlTabSave()")


def _open_history(page):
    page.locator("#btnSqlHistory").click()
    page.wait_for_selector(".sql-history .sqlh-row, .sql-history .vp-note:not(:has-text('Loading'))")
    return page.locator(".sql-history")


def _entry(page, sql):
    return page.locator(".sql-history .sqlh-row").filter(has_text=sql.split("\n")[0])


def _tabs(page):
    return page.evaluate("() => __winnow.S.sqlTabs.length")


def _saved(api, tab_id):
    """What the case file holds for a query tab, not just the editor."""
    return next(t for t in api("/api/sql_tabs") if t["id"] == tab_id)["sql"]


def test_running_a_query_records_it_with_its_tab_and_row_count(sql, api, until):
    page = sql
    _set_editor(page, QUICK)
    tab = page.evaluate("() => __winnow.activeSqlTab().name")
    page.locator("#btnRunSql").click()
    page.wait_for_selector("#sqlResult table")
    until(page, lambda: api("/api/sql_history"), "the run to reach the history")
    [h] = api("/api/sql_history")
    assert (h["sql"], h["tab_name"], h["row_count"], h["error"]) == (QUICK, tab, 7, None)

    # The same query again is the same entry, run twice — not a second one.
    page.locator("#btnRunSql").click()
    until(page, lambda: api("/api/sql_history")[0]["runs"] == 2, "the second run to be counted")
    assert len(api("/api/sql_history")) == 1


def test_a_failed_run_is_recorded_with_its_error(sql, api, until):
    page = sql
    _set_editor(page, "SELECT NoSuchColumn FROM src_1")
    page.locator("#btnRunSql").click()
    page.wait_for_selector("#sqlResult .sql-error")
    until(page, lambda: api("/api/sql_history"), "the failed run to reach the history")
    [h] = api("/api/sql_history")
    assert "no such column" in h["error"].lower()
    assert h["row_count"] is None

    _open_history(page)
    assert "no such column" in _entry(page, h["sql"]).locator(".sqlh-error").inner_text().lower()


def test_a_superseded_run_is_not_recorded(sql, api, until):
    """Run again before the first run answered: the first is abandoned
    (test_sql_run_tokens.py), and it is not something the analyst got an
    answer from, so it is not in the history. The run that replaced it is."""
    page = sql
    try:
        page.evaluate("""async ([slow, quick]) => {
            const m = await import('/static/js/plugins.js');
            const box = document.getElementById('sqlText');
            const type = (t) => { box.value = t; box.dispatchEvent(new Event('input', { bubbles: true })); };
            type(slow);
            const first = m.runSql();
            window.__slowToken = __winnow.S.sqlRuns.get(__winnow.S.sqlTabId);
            type(quick);
            await Promise.all([first, m.runSql()]);
        }""", [RUNAWAY, QUICK])
        until(page, lambda: [h["sql"] for h in api("/api/sql_history")] == [QUICK],
              "only the run that answered to be in the history")
        # Checking that something did NOT happen: nothing to wait for but
        # time, and the abandoned run settled before the quick one did, so
        # anything it was going to write was already on its way.
        page.wait_for_timeout(300)
        assert [h["sql"] for h in api("/api/sql_history")] == [QUICK]
    finally:
        page.evaluate("() => window.__slowToken && __winnow.post('/api/cancel_op', { token: window.__slowToken }).catch(() => {})")


def test_the_list_is_newest_first_and_filters_by_every_word(sql, api):
    page = sql
    api("/api/sql_history", "POST", {"sql": "SELECT Host, COUNT(*) FROM src_1 GROUP BY Host",
                                     "tab_name": "Hosts", "row_count": 5, "elapsed_ms": 1})
    api("/api/sql_history", "POST", {"sql": "SELECT * FROM src_1 WHERE EventId = '4624'",
                                     "tab_name": "Logons", "row_count": 50, "elapsed_ms": 2})
    api("/api/sql_history", "POST", {"sql": "SELECT * FROM src_1 WHERE EventId = '4688'",
                                     "tab_name": "Processes", "row_count": 50, "elapsed_ms": 2})
    panel = _open_history(page)
    items = panel.locator(".sqlh-sql")
    assert [items.nth(i).inner_text() for i in range(items.count())] == [
        "SELECT * FROM src_1 WHERE EventId = '4688'",
        "SELECT * FROM src_1 WHERE EventId = '4624'",
        "SELECT Host, COUNT(*) FROM src_1 GROUP BY Host",
    ]
    assert "Logons" in panel.locator(".sqlh-meta").nth(1).inner_text()
    assert "3 queries" in panel.locator(".sqlh-foot").inner_text()

    # Words in any order, across the query and its tab's name.
    page.locator(".sqlh-search").fill("logons 4624")
    page.wait_for_function("() => document.querySelectorAll('.sql-history .sqlh-row').length === 1")
    assert "4624" in panel.locator(".sqlh-sql").inner_text()
    assert "1 of 3 queries" in panel.locator(".sqlh-foot").inner_text()

    page.locator(".sqlh-search").fill("zzz")
    page.wait_for_selector(".sql-history .vp-note:has-text('No query matches that.')")


def test_enter_opens_the_first_match(sql, api):
    page = sql
    api("/api/sql_history", "POST", {"sql": "SELECT * FROM src_1 WHERE EventId = '4624'"})
    api("/api/sql_history", "POST", {"sql": "SELECT * FROM src_1 WHERE EventId = '4688'"})
    _set_editor(page, "")
    _open_history(page)
    page.locator(".sqlh-search").fill("4624")
    page.locator(".sqlh-search").press("Enter")
    page.wait_for_function("() => document.getElementById('sqlText').value.includes('4624')")
    assert page.locator(".sql-history").count() == 0


def test_ctrl_enter_opens_the_first_match_and_runs_it(sql, api, until):
    page = sql
    api("/api/sql_history", "POST", {"sql": QUICK})
    api("/api/sql_history", "POST", {"sql": "SELECT * FROM src_1 WHERE EventId = '4688'"})
    _set_editor(page, "")
    _open_history(page)
    page.locator(".sqlh-search").fill("rid <= 7")
    page.locator(".sqlh-search").press("ControlOrMeta+Enter")
    page.wait_for_selector("#sqlResult table")
    assert page.locator("#sqlText").input_value() == QUICK
    until(page, lambda: api("/api/sql_history")[0]["sql"] == QUICK, "the run to move it to the top")


def test_opening_an_entry_replaces_a_query_that_is_already_in_the_history(sql, api, until):
    page = sql
    a = "SELECT * FROM src_1 WHERE Host = 'H1'"
    b = "SELECT * FROM src_1 WHERE Host = 'H2'"
    api("/api/sql_history", "POST", {"sql": b})
    api("/api/sql_history", "POST", {"sql": a})
    _set_editor(page, a)     # what the editor holds is in the list, so nothing is lost
    tabs = page.evaluate("() => __winnow.S.sqlTabs.length")
    tab_id = page.evaluate("() => __winnow.S.sqlTabId")

    _open_history(page)
    _entry(page, b).locator(".sqlh-item").click()
    page.wait_for_function("(b) => document.getElementById('sqlText').value === b", arg=b)
    assert page.evaluate("() => __winnow.S.sqlTabs.length") == tabs
    assert page.evaluate("() => __winnow.S.sqlTabId") == tab_id
    # In the case file, not just in the box.
    until(page, lambda: next(t for t in api("/api/sql_tabs") if t["id"] == tab_id)["sql"] == b,
          "the opened query to be saved into its tab")


def test_opening_an_entry_leaves_no_autocomplete_open(sql, api):
    """The query goes in through the editor's input event, which is what
    opens autocomplete for the word before the caret. A query ending in
    DESC landed with a "DESC — keyword" suggestion open over its result,
    waiting to take the next Enter."""
    page = sql
    q = "SELECT Host, COUNT(*) AS n FROM src_1 GROUP BY Host ORDER BY n DESC"
    api("/api/sql_history", "POST", {"sql": q})
    _set_editor(page, "")
    _open_history(page)
    _entry(page, q).locator(".sqlh-run").click()
    page.wait_for_selector("#sqlResult table")
    assert page.locator("#sqlText").input_value() == q
    assert page.locator(".sql-ac").count() == 0


H4 = "SELECT * FROM src_1 WHERE Host = 'H4'"
DRAFT = "SELECT EventId FROM src_1 WHERE Host = 'H3' -- half written, never run"


def _asked(page):
    page.wait_for_selector('.confirm-card:has-text("isn\'t in the history")')
    return page.locator(".confirm-actions")


def test_a_query_that_is_not_in_the_history_is_asked_about_before_it_is_replaced(sql, api, until):
    """The current tab holds something nobody has run: replacing it would
    lose it for good, so the analyst says which they want."""
    page = sql
    api("/api/sql_history", "POST", {"sql": H4, "tab_name": "Host four"})
    _set_editor(page, DRAFT)
    drafted = page.evaluate("() => __winnow.S.sqlTabId")
    before = _tabs(page)

    # Cancel: nothing moves.
    _open_history(page)
    _entry(page, H4).locator(".sqlh-item").click()
    _asked(page).get_by_role("button", name="Cancel").click()
    page.wait_for_selector(".confirm-card", state="detached")
    assert page.locator("#sqlText").input_value() == DRAFT
    assert _tabs(page) == before

    # Open in a new tab, which has the focus, so a stray Enter keeps the
    # draft: it stays where it was typed, in the case file too.
    _open_history(page)
    _entry(page, H4).locator(".sqlh-item").click()
    _asked(page)
    page.wait_for_function("() => document.activeElement?.textContent === 'Open in a new tab'")
    page.keyboard.press("Enter")
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n + 1", arg=before)
    assert page.locator("#sqlText").input_value() == H4
    # Named for the tab it was run from, which is free in this case.
    assert page.evaluate("() => __winnow.activeSqlTab().name") == "Host four"
    assert _saved(api, drafted) == DRAFT

    # Replace it: in the draft's own tab, and no new one.
    page.evaluate("(id) => __winnow.activateSqlTab(id)", drafted)
    page.wait_for_function("(d) => document.getElementById('sqlText').value === d", arg=DRAFT)
    _open_history(page)
    _entry(page, H4).locator(".sqlh-item").click()
    _asked(page).get_by_role("button", name="Replace it").click()
    page.wait_for_function("(q) => document.getElementById('sqlText').value === q", arg=H4)
    assert page.evaluate("() => __winnow.S.sqlTabId") == drafted
    assert _tabs(page) == before + 1
    until(page, lambda: _saved(api, drafted) == H4, "the replacement to be saved into the draft's tab")


def test_open_in_a_new_tab_is_off_by_default_and_remembered(sql, api):
    page = sql
    a = "SELECT * FROM src_1 WHERE Host = 'H1'"
    b = "SELECT * FROM src_1 WHERE Host = 'H2'"
    api("/api/sql_history", "POST", {"sql": b})
    api("/api/sql_history", "POST", {"sql": a})
    _set_editor(page, a)     # in the history, so it could be replaced without asking
    first = page.evaluate("() => __winnow.S.sqlTabId")
    before = _tabs(page)

    _open_history(page)
    box = page.locator(".sqlh-newtab input")
    assert not box.is_checked()
    box.check()
    _entry(page, b).locator(".sqlh-item").click()
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n + 1", arg=before)
    assert page.locator("#sqlText").input_value() == b
    assert _saved(api, first) == a

    # Still ticked the next time the list opens.
    _open_history(page)
    assert page.locator(".sqlh-newtab input").is_checked()
    # Unticked, a query opens in the current tab again.
    page.locator(".sqlh-newtab input").uncheck()
    assert page.evaluate("(k) => JSON.parse(localStorage.getItem(k)).newTab", PREFS_KEY) is False
    _entry(page, a).locator(".sqlh-item").click()
    page.wait_for_function("(q) => document.getElementById('sqlText').value === q", arg=a)
    assert _tabs(page) == before + 1


def test_ctrl_click_opens_a_new_tab_with_the_switch_off(sql, api):
    page = sql
    api("/api/sql_history", "POST", {"sql": H4})
    _set_editor(page, "")    # empty: a plain click would fill this tab
    before = _tabs(page)
    _open_history(page)
    _entry(page, H4).locator(".sqlh-item").click(modifiers=["ControlOrMeta"])
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n + 1", arg=before)
    assert page.locator("#sqlText").input_value() == H4


def test_run_opens_and_runs_it(sql, api, until):
    page = sql
    api("/api/sql_history", "POST", {"sql": QUICK, "row_count": 7})
    _set_editor(page, "")
    _open_history(page)
    _entry(page, QUICK).locator(".sqlh-run").click()
    page.wait_for_selector("#sqlResult table")
    assert page.locator("#sqlText").input_value() == QUICK
    until(page, lambda: api("/api/sql_history")[0]["runs"] == 2, "the run to be counted")


def test_remove_one_and_clear_all(sql, api):
    page = sql
    for q in ("SELECT 1", "SELECT 2", "SELECT 3"):
        api("/api/sql_history", "POST", {"sql": q})
    _open_history(page)
    _entry(page, "SELECT 2").locator(".sqlh-rm").click()
    page.wait_for_function("() => document.querySelectorAll('.sql-history .sqlh-row').length === 2")
    assert [h["sql"] for h in api("/api/sql_history")] == ["SELECT 3", "SELECT 1"]

    page.locator(".sqlh-clear").click()
    page.locator(".confirm-actions").get_by_role("button", name="Clear history").click()
    page.wait_for_function("() => document.getElementById('toast').textContent === 'Query history cleared'")
    assert api("/api/sql_history") == []

    _open_history(page)
    page.wait_for_selector(".sql-history .vp-note:has-text('Nothing has been run in this case yet')")

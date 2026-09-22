"""First/Last keeps its sheets in the case file, and re-runs the rows.

Closing a case (or a reload, or a plugin toggle) tears the mount down with
no callback to the plugin, so the tab saves the sheet DEFINITIONS as they
change and rebuilds from them on the way back in. What is asserted here is
the complaint that started it: the tab is still in the strip, and clicking
it used to show an empty sheet. Also the two ways a saved spec can be wrong
by the time it is read — a source id handed to a different table, and a
column that is gone — which the plugin resolves against the live source
list rather than sending into a 400.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:first-last.firstlast"

DRAG = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""

# planRestore is pure — payload in, specs out — so the cases that are
# awkward to stage against a live case (an id reused by another table, a
# merge, a column that has gone) are driven straight at it. Imported under
# its own query string so the mounted tab's module instance is untouched.
PLAN = """(args) => {
  window.__plan = undefined;
  import('/plugin_assets/first_last/ui/tab.js?restoretest=1')
    .then((m) => { window.__plan = m.planRestore(args[0], args[1], args[2] || []); });
}"""
PIVOT_PLAN = """(args) => {
  window.__plan = undefined;
  import('/plugin_assets/pivot/ui/tab.js?restoretest=1')
    .then((m) => { window.__plan = m.planRestore(args[0], args[1]); });
}"""


def _saved_post(response):
    """The POST /api/plugin_state body the page just sent, as the server
    stored it."""
    return response.json()


def _plan(pg, script, payload, sources, tags=()):
    pg.evaluate(script, [payload, sources, list(tags)])
    pg.wait_for_function("() => window.__plan !== undefined", timeout=10_000)
    return pg.evaluate("() => window.__plan")


def _is_state_post(r):
    return "/api/plugin_state" in r.url and r.request.method == "POST"


@pytest.fixture(scope="module")
def fl_page(browser, server, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.removeItem('winnow.firstlast.auto');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false, pagesMenu: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                         timeout=10_000)
    yield pg
    ctx.close()
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "off_all"})
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _open_tab(pg):
    pg.locator(".tab-plugin", has_text="First/Last").click()
    pg.wait_for_selector("[data-zone='groupBy']", timeout=10_000)


def test_two_sheets_come_back_and_their_rows_are_re_run(fl_page):
    pg = fl_page
    _open_tab(pg)

    # Sheet 1: group by Host. Sheet 2: group by EventId, and it is the one
    # left on top — which sheet was active is part of what should return.
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    pg.wait_for_selector(".pluginview tbody tr", timeout=10_000)
    pg.locator(".pluginview .sql-tabs .sql-tab").last.click()
    pg.wait_for_function("() => document.querySelectorAll('.pluginview .sql-tabs .sql-tab').length === 3",
                         timeout=10_000)
    with pg.expect_response(_is_state_post, timeout=15_000) as saved:
        pg.evaluate(DRAG, ["[data-field='EventId']", "[data-zone='groupBy']"])
        pg.wait_for_selector(".pluginview tbody tr", timeout=10_000)
    payload = _saved_post(saved.value)["payload"]

    # What was written is the QUESTION, not the answer: no rows, no preview.
    assert payload["active"] == 1
    assert [sh["groupBy"] for sh in payload["sheets"]] == [["Host"], ["EventId"]]
    assert [sh["source"]["name"] for sh in payload["sheets"]] == ["ui.csv", "ui.csv"]
    blob = str(payload)
    assert "preview" not in blob and "rows" not in blob, blob[:400]

    # A reload is the same teardown a case switch performs: new module
    # instance, new mount, nothing left in memory.
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                         timeout=10_000)
    _open_tab(pg)

    strip = pg.locator(".pluginview .sql-tabs .sql-tab")
    pg.wait_for_function("() => document.querySelectorAll('.pluginview .sql-tabs .sql-tab').length === 3",
                         timeout=10_000)
    assert strip.nth(1).get_attribute("aria-selected") == "true", "the sheet left on top comes back on top"
    assert pg.locator(".pluginview [data-zone='groupBy'] [data-field='EventId']").count() == 1
    # The rows were never saved, so their presence is a re-run against the case.
    pg.wait_for_selector(".pluginview tbody tr", timeout=10_000)
    assert pg.locator(".pluginview tbody tr").count() > 0

    # And it says so, with when — a grouping from three weeks ago that
    # reappears silently is worse than an empty tab.
    banner = pg.locator(".pluginview .fl-restored")
    assert banner.is_visible()
    assert "Restored the sheets" in banner.inner_text()
    assert "saved 20" in banner.inner_text()


def test_start_fresh_forgets_them(fl_page):
    """The escape hatch. Without it a restore that is wrong for today's
    question is a tab the analyst has to dismantle by hand."""
    pg = fl_page
    with pg.expect_response(_is_state_post, timeout=15_000) as cleared:
        pg.locator(".pluginview .fl-start-fresh").click()
    assert _saved_post(cleared.value)["payload"] is None

    pg.wait_for_function("() => document.querySelectorAll('.pluginview .sql-tabs .sql-tab').length === 2",
                         timeout=10_000)
    assert pg.locator(".pluginview [data-zone='groupBy'] [data-field]").count() == 0
    assert not pg.locator(".pluginview .fl-restored").is_visible()


# ------------------------------------------- what a payload cannot promise

def _src(sid, name, columns=("Host", "EventId", "Timestamp"), **extra):
    return dict(id=sid, name=name, error=None, row_count=10,
                columns=[{"name": c, "type": "text"} for c in columns], **extra)


def _payload(**sheet):
    base = {"name": "Logons by host", "source": {"id": 1, "name": "ui.csv"},
            "groupBy": ["Host"], "carry": [], "sums": [], "filters": [],
            "tags": {"mode": "", "ids": []}, "rowJson": False,
            "sortColumn": "Timestamp", "colWidths": {}, "template": "{which} of {count}"}
    base.update(sheet)
    return {"v": 1, "active": 0, "sheets": [base]}


def test_an_id_reused_by_another_table_does_not_restore_onto_it(fl_page):
    """SQLite hands a dropped source's id to the next import, so "source 1"
    in a payload can be somebody else's evidence by the time it is read.
    The sheet comes back without a table rather than grouping another
    file's rows under the old sheet's name."""
    plan = _plan(fl_page, PLAN, _payload(), [_src(1, "something-else.csv")])
    assert plan["sheets"][0]["sourceId"] is None
    assert plan["sheets"][0]["groupBy"] == []
    assert "ui.csv" in plan["notes"][0] and "not in this case" in plan["notes"][0]


def test_a_table_re_imported_under_a_new_id_is_found_by_name(fl_page):
    plan = _plan(fl_page, PLAN, _payload(), [_src(7, "ui.csv")])
    assert plan["sheets"][0]["sourceId"] == 7
    assert plan["sheets"][0]["groupBy"] == ["Host"]
    assert plan["notes"] == []


def test_columns_the_table_no_longer_has_are_dropped_and_named(fl_page):
    """They would otherwise go straight into the preview request, and the
    backend answers a missing column with a 400 — so the tab would open on
    an error banner instead of the sheet."""
    plan = _plan(fl_page, PLAN, _payload(
        groupBy=["Host", "Gone"], carry=["EventId", "AlsoGone"],
        filters=[{"column": "Vanished", "op": "eq", "value": "x", "values": []}],
        sortColumn="Missing"), [_src(1, "ui.csv")])
    sheet = plan["sheets"][0]
    assert sheet["groupBy"] == ["Host"] and sheet["carry"] == ["EventId"]
    assert sheet["filters"] == [] and sheet["sortColumn"] is None
    note = plan["notes"][0]
    for gone in ("Gone", "AlsoGone", "Vanished", "Missing"):
        assert gone in note, note


def test_a_tag_filter_whose_tag_is_gone_is_not_left_filtering_on_nothing(fl_page):
    """"Only these tags" against an id this case no longer has is not an
    error the analyst would see — it is an empty result, which reads as
    "there is nothing here" instead of "your filter is stale"."""
    payload = _payload(tags={"mode": "ids", "ids": [4, 5]})
    plan = _plan(fl_page, PLAN, payload, [_src(1, "ui.csv")],
                 tags=[{"id": 4, "name": "TA", "color": "#c0392b"}])
    assert plan["sheets"][0]["tags"] == {"mode": "ids", "ids": [4]}
    assert "tag filter" in plan["notes"][0]

    # None of them left: the sheet goes back to every row, and says so.
    plan = _plan(fl_page, PLAN, payload, [_src(1, "ui.csv")], tags=[])
    assert plan["sheets"][0]["tags"] == {"mode": "", "ids": []}
    assert "every row is in again" in plan["notes"][0]


def test_a_sheet_on_a_merge_restores(fl_page):
    """Merge parity (invariant #9): First/Last groups merges, a merge id is
    negative, and a saved sheet may name one."""
    payload = _payload(source={"id": -2, "name": "All logons (merged)"})
    plan = _plan(fl_page, PLAN, payload,
                 [_src(1, "ui.csv"), _src(-2, "All logons (merged)", is_merge=True)])
    assert plan["sheets"][0]["sourceId"] == -2
    assert plan["sheets"][0]["groupBy"] == ["Host"]
    assert plan["notes"] == []


def test_a_payload_from_another_version_is_ignored(fl_page):
    """An older or newer shape starts the tab fresh rather than half
    restoring into it."""
    payload = _payload()
    payload["v"] = 99
    assert _plan(fl_page, PLAN, payload, [_src(1, "ui.csv")]) is None
    assert _plan(fl_page, PLAN, {"v": 1, "sheets": []}, [_src(1, "ui.csv")]) is None


def test_pivot_validates_the_same_way(fl_page):
    """The mechanism is the host's, but the validating is each plugin's —
    pivot was the one whose comment said the in-memory assumption was
    systemic, so it gets the same treatment rather than a copy of half."""
    payload = {"v": 1, "active": 0, "pivots": [{
        "name": "Logons by host", "source": {"id": 1, "name": "ui.csv"},
        "rows": ["Host", "Gone"], "cols": [], "filters": [],
        "values": [{"agg": "count", "column": "EventId"}, {"agg": "sum", "column": "AlsoGone"}],
        "subtotals": True, "grandTotals": False}]}
    plan = _plan(fl_page, PIVOT_PLAN, payload, [_src(1, "ui.csv")])
    p = plan["pivots"][0]
    assert p["sourceId"] == 1 and p["rows"] == ["Host"]
    assert [m["column"] for m in p["values"]] == ["EventId"]
    assert p["grandTotals"] is False
    assert "Gone" in plan["notes"][0] and "AlsoGone" in plan["notes"][0]

    # And the reused-id rule is the same one.
    assert _plan(fl_page, PIVOT_PLAN, payload, [_src(1, "other.csv")])["pivots"][0]["sourceId"] is None

"""Pivot's wells and its cross-tab come back when the case is reopened.

The validating is driven straight at `planRestore` in
`test_firstlast_state.py`, but restoring happens in the MOUNT, and that is
where this change did its work: the table a restored pivot names has to
beat both the select's last value and whatever the grid is showing (or the
first `fillSources` empties the wells it just filled), the shared `meta`
has to be on the pivot whose value chip reads its aggregations from it, and
something has to run the query, because a restored pivot arrives with a
definition and no data. None of that is visible to a pure test.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"

DRAG_FIELD = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""


def _is_state_post(r):
    return "/api/plugin_state" in r.url and r.request.method == "POST"


@pytest.fixture(scope="module")
def pv_page(browser, server, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false, pagesMenu: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                         timeout=10_000)
    yield pg
    ctx.close()
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _open_tab(pg):
    pg.locator(".tab-plugin", has_text="Pivot").click()
    pg.wait_for_selector(".pluginview [data-zone='rows']", timeout=10_000)
    pg.wait_for_selector(".pluginview [data-field]", timeout=10_000)


def _use_ui_csv(pg):
    """Point the pivot at ui.csv by id. The session's case is shared and
    another module may have imported tables into it by now; this one names
    ui.csv's columns, so it cannot take whatever the grid happens to show."""
    want = str(pg.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id"))
    if pg.eval_on_selector(".pluginview select", "(s) => s.value") != want:
        # Switching tables clears the wells, which is a save of its own.
        with pg.expect_response(_is_state_post, timeout=15_000):
            pg.select_option(".pluginview select", want)
    pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
    return want


def test_two_pivots_come_back_with_their_wells_and_a_re_run_cross_tab(pv_page):
    pg = pv_page
    _open_tab(pg)
    want = _use_ui_csv(pg)

    with pg.expect_response(_is_state_post, timeout=15_000):
        pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='rows']"])
    with pg.expect_response(_is_state_post, timeout=15_000):
        pg.evaluate(DRAG_FIELD, ["[data-field='EventId']", "[data-zone='values']"])
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)

    # A second pivot, left on top — which one was on top is part of this.
    with pg.expect_response(_is_state_post, timeout=15_000):
        pg.locator(".pluginview .sql-tabs .sql-tab", has_text="+").click()
    pg.wait_for_function("() => document.querySelectorAll('.pluginview .sql-tabs .sql-tab').length === 3",
                         timeout=10_000)
    _use_ui_csv(pg)
    with pg.expect_response(_is_state_post, timeout=15_000) as saved:
        pg.evaluate(DRAG_FIELD, ["[data-field='EventId']", "[data-zone='rows']"])
    payload = saved.value.json()["payload"]

    assert payload["active"] == 1
    assert [p["rows"] for p in payload["pivots"]] == [["Host"], ["EventId"]]
    assert [m["column"] for m in payload["pivots"][0]["values"]] == ["EventId"]
    assert payload["pivots"][0]["source"]["name"] == "ui.csv"
    # The question, not the answer: the aggregate response is nowhere in it.
    assert "sets" not in str(payload)

    # A reload is the same teardown a case switch performs — new module
    # instance, new mount, nothing left in memory.
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                         timeout=10_000)
    _open_tab(pg)

    pg.wait_for_function("() => document.querySelectorAll('.pluginview .sql-tabs .sql-tab').length === 3",
                         timeout=10_000)
    assert pg.locator(".pluginview .sql-tabs .sql-tab").nth(1).get_attribute("aria-selected") == "true"
    assert pg.locator(".pluginview [data-zone='rows'] [data-field='EventId']").count() == 1
    # Its own table, not the one the grid is on and not the first in the list.
    assert pg.eval_on_selector(".pluginview select", "(s) => s.value") == want
    # The cross-tab was never saved, so rows on screen are a re-run.
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)

    banner = pg.locator(".pluginview .pv-restored")
    assert banner.is_visible()
    assert "Restored the pivots" in banner.inner_text()


def test_looking_at_the_other_restored_pivot_keeps_the_banner(pv_page):
    """The line carries what the restore dropped and the only Start fresh
    there is. Clicking the other restored pivot to look at it changes
    nothing about either, so it must not be what destroys the line."""
    pg = pv_page
    pg.locator(".pluginview .sql-tabs .sql-tab").first.click()
    pg.wait_for_selector(".pluginview [data-zone='values'] [data-field='EventId']", timeout=10_000)
    assert pg.locator(".pluginview [data-zone='rows'] [data-field='Host']").count() == 1
    # Restored with a definition and no data: switching to it runs it.
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)
    assert pg.locator(".pluginview .pv-restored").is_visible()

    # An edit does clear it — by then it has stopped being news.
    pg.locator(".pluginview [data-zone='rows'] [data-field='Host'] button").click()
    pg.wait_for_function("() => document.querySelector('.pluginview .pv-restored').hidden",
                         timeout=10_000)

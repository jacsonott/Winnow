"""A drawn pivot keeps its table while the next answer is fetched, inert.

Starting a query used to redraw the whole table before the request went
out: the previous answer rebuilt under the new field layout, which drew as
a mostly empty table, synchronously, ahead of the fetch. On a pivot of a few
thousand rows that put a few hundred ms in front of every edit. The table
now stays as it was until the answer replaces it — and has to be inert
meanwhile, because its cells open details through the CURRENT field
layout, so a click on one would show the rows of a cell that isn't there.
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

# An /aggregate request waits for the test while window.__pivotHold is set,
# so the moment between "the query started" and "its answer landed" lasts
# as long as the assertions need instead of a few ms.
HOLD_AGGREGATE = """(() => {
  const fetch = window.fetch;
  window.__pivotHold = null;
  window.fetch = (url, opts) => (String(url).includes('/aggregate') && window.__pivotHold
    ? window.__pivotHold.then(() => fetch(url, opts))
    : fetch(url, opts));
})()"""

TABLE = ".pluginview table"


def _is_state_post(r):
    return "/api/plugin_state" in r.url and r.request.method == "POST"


@pytest.fixture(scope="module")
def pv_page(own_page, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    try:
        with own_page(init=HOLD_AGGREGATE) as pg:
            pg.evaluate("() => __winnow.loadPlugins()")
            pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                                 timeout=10_000)
            pg.locator(".tab-plugin", has_text="Pivot").click()
            pg.wait_for_selector(".pluginview [data-zone='rows']", timeout=10_000)
            want = str(pg.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id"))
            if pg.eval_on_selector(".pluginview select", "(s) => s.value") != want:
                with pg.expect_response(_is_state_post, timeout=15_000):
                    pg.select_option(".pluginview select", want)
            pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
            yield pg
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


def test_the_drawn_table_waits_inert_for_the_next_answer(pv_page):
    pg = pv_page
    with pg.expect_response(_is_state_post, timeout=15_000):
        pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='rows']"])
    pg.wait_for_function(f"() => document.querySelectorAll('{TABLE} tbody tr').length > 1",
                         timeout=15_000)
    pg.evaluate(f"() => {{ document.querySelector('{TABLE}').dataset.before = '1'; }}")

    pg.evaluate("() => { window.__pivotHold = new Promise((r) => { window.__pivotRelease = r; }); }")
    try:
        pg.evaluate(DRAG_FIELD, ["[data-field='EventId']", "[data-zone='cols']"])
        pg.wait_for_function("() => [...document.querySelectorAll('.pluginview .note-status')]"
                             ".some((n) => n.textContent === 'Aggregating…')", timeout=10_000)
        # The query is out and held: what is on screen is the table that was
        # there before it — the same node, not a rebuild — and nothing in it
        # can be clicked.
        held = pg.eval_on_selector(TABLE, "(t) => ({ before: t.dataset.before, inert: t.inert })")
        assert held == {"before": "1", "inert": True}
    finally:
        pg.evaluate("() => { const go = window.__pivotRelease; window.__pivotHold = null; if (go) go(); }")

    # The answer replaces it with a live table drawn for the new layout.
    pg.wait_for_function(f"() => {{ const t = document.querySelector('{TABLE}');"
                         " return t && !t.dataset.before; }", timeout=15_000)
    assert pg.eval_on_selector(TABLE, "(t) => t.inert") is False
    assert pg.locator(f"{TABLE} thead th", has_text="4624").count() == 1

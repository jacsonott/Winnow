"""The reworked First/Last tab, driven end to end: pivot-style drag-drop
zones, bookend sheets as sub-tabs, header-drag column reordering, table-tab
row selection, and the top-right result actions (Copy result / Create
table… with the timeline tag)."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


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


@pytest.fixture(scope="module")
def fl_page(browser, server):
    _post(server, "/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900},
                              permissions=["clipboard-read", "clipboard-write"])
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                         timeout=10_000)
    pg.locator(".tab-plugin", has_text="First/Last").click()
    pg.wait_for_selector("[data-zone='groupBy']", timeout=10_000)
    yield pg
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _chip(field):
    return f"[data-field='{field}']"


def test_drag_drop_builds_a_grouping_with_preview(fl_page):
    pg = fl_page
    pg.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    # Grouping alone is enough — the ordering column defaulted to the
    # datetime column, so the preview runs.
    pg.wait_for_selector("tbody tr", timeout=10_000)
    assert "5 groups" in pg.locator("text=5 groups").first.inner_text()
    # Include a column by dragging it into carry.
    pg.evaluate(DRAG, [_chip("EventId"), "[data-zone='carry']"])
    pg.wait_for_function(
        "() => [...document.querySelectorAll('thead th')].map(h => h.textContent).join('|') === 'Timestamp|EventId|Description'",
        timeout=10_000)


def test_header_drag_reorders_included_columns(fl_page):
    pg = fl_page
    pg.evaluate(DRAG, [_chip("Host"), "[data-zone='carry']"])
    pg.wait_for_function(
        "() => [...document.querySelectorAll('thead th')].map(h => h.textContent).join('|') === 'Timestamp|EventId|Host|Description'",
        timeout=10_000)
    # Drag the Host header onto the EventId header — Host moves first.
    pg.evaluate("""() => {
      const ths = [...document.querySelectorAll('thead th')];
      const dt = new DataTransfer();
      const src = ths[2], dst = ths[1];
      src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
      dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
      dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
      src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
    }""")
    pg.wait_for_function(
        "() => [...document.querySelectorAll('thead th')].map(h => h.textContent).join('|') === 'Timestamp|Host|EventId|Description'",
        timeout=10_000)


def test_row_selection_and_ctrl_c(fl_page):
    pg = fl_page
    pg.wait_for_selector("tbody tr")
    rows = pg.locator("tbody tr")
    rows.nth(0).click()
    rows.nth(2).click(modifiers=["Shift"])
    pg.wait_for_function("() => document.querySelector('.note-status') !== null")
    assert "3 rows selected" in pg.locator("text=selected").first.inner_text()
    pg.keyboard.press("Control+c")
    clip = pg.evaluate("() => navigator.clipboard.readText()")
    assert clip.count("\n") == 2 and "\t" in clip  # 3 TSV lines


def test_sheets_are_independent(fl_page):
    pg = fl_page
    pg.locator(".sql-tab", has_text="+").click()
    pg.wait_for_selector(".sql-tab[aria-selected='true']:has-text('Bookends 2')")
    # The new sheet starts empty…
    assert pg.locator("[data-zone='groupBy'] [data-field]").count() == 0
    # …and switching back restores the first sheet's grouping.
    pg.locator(".sql-tab", has_text="Bookends 1").click()
    pg.wait_for_selector("[data-zone='groupBy'] [data-field='Host']")
    # Close the second sheet.
    pg.locator(".sql-tab", has_text="Bookends 2").locator("span", has_text="✕").click()
    pg.wait_for_function(
        "() => [...document.querySelectorAll('.sql-tab')].every(t => !t.textContent.includes('Bookends 2'))")


def test_create_table_with_timeline_tag(fl_page):
    pg = fl_page
    pg.locator("button", has_text="Create table…").click()
    pg.wait_for_selector("#modal:not([hidden])")
    pg.locator("#modal .confirm-input").first.fill("host bookends")
    pg.locator("#modal input[type=checkbox]").check()
    tag_inputs = pg.locator("#modal .confirm-input")
    tag_inputs.nth(1).fill("Host bookends")
    pg.locator("#modal button", has_text="Create table").click()
    pg.wait_for_selector("#modal[hidden]", state="attached", timeout=15_000)
    # Lands as a real source, every row tagged with the named tag.
    pg.wait_for_function(
        "() => __winnow.S.sources.some((s) => s.name === 'host bookends')", timeout=10_000)
    info = pg.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const s = __winnow.S.sources.find((x) => x.name === 'host bookends');
      const tags = await fetch('/api/tags?source_id=' + s.id, { headers: h }).then(r => r.json());
      const tag = tags.tags.find((t) => t.name === 'Host bookends');
      return { rows: s.row_count, tagged: tag ? (tags.counts[String(tag.id)] || 0) : -1 };
    }""")
    assert info["rows"] == 10 and info["tagged"] == 10
    # Let the just-opened grid finish loading before tearing its table out
    # from under it — an in-flight page fetch against a deleted source is
    # a 500 the pageerror hook would rightly flag.
    pg.wait_for_function(
        """() => { const s = __winnow.S.sources.find((x) => x.name === 'host bookends');
               return s && __winnow.S.sourceId === s.id && __winnow.S.view
                 && __winnow.S.view.row_count === 10; }""")
    # Leave the shared case as found: drop the created table and its tag
    # (other tests count open rows and tag defs).
    pg.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const s = __winnow.S.sources.find((x) => x.name === 'host bookends');
      const tags = await fetch('/api/tags', { headers: h }).then(r => r.json());
      const tag = tags.tags.find((t) => t.name === 'Host bookends');
      await fetch('/api/source/' + s.id, { method: 'DELETE', headers: h });
      if (tag) await fetch('/api/tags/' + tag.id, { method: 'DELETE', headers: h });
      // Same bookkeeping the Tables manager's Remove does before reloading.
      __winnow.S.viewCache.delete(s.id);
      if (__winnow.S.sourceId === s.id) __winnow.S.sourceId = null;
      await __winnow.loadSources();
    }""")
    pg.wait_for_function("() => !__winnow.S.sources.some((s) => s.name === 'host bookends')")

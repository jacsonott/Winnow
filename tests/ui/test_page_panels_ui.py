"""Page panels in the browser: the toggle in the page's toolbar, the side
column, and the sqlPage / notesPage APIs a panel drives the page with.

The panel is a stand-in registered straight into client state, its
module served by a routed response — no plugin on disk, same as
fake_row_action. The module keeps the context it was mounted with on
window.__panelCtx so the tests can call the page APIs the way a plugin
would.
"""
import json

import pytest

pytestmark = pytest.mark.ui

PANEL_JS = """
export default function mount(container, winnow) {
  container.textContent = 'panel here';
  window.__panelCtx = winnow;
  window.__shows = 0;
}
export function onShow() { window.__shows = (window.__shows || 0) + 1; }
"""


def _register(page, which="sql"):
    page.route("**/plugin_assets/fake/ui/panel.js*",
               lambda route: route.fulfill(status=200, content_type="text/javascript", body=PANEL_JS))
    page.evaluate("(which) => { __winnow.S.pluginPagePanels = [{ id: 'fake.helper', local_id: 'helper', plugin: 'fake', "
                  "plugin_fs: 'fake', page: which, label: 'Helper', entry: 'ui/panel.js', description: 'a helper', gen: 1 }]; "
                  "__winnow.renderPluginPanelButtons(); }", which)


def _unregister(page):
    page.evaluate("() => { __winnow.togglePluginPanel('fake.helper', false); __winnow.S.pluginPagePanels = []; "
                  "__winnow.renderPluginPanelButtons(); localStorage.removeItem('winnow.panels'); __winnow.showGridTab(); }")


@pytest.fixture
def sql_panel(page):
    _register(page, "sql")
    yield
    _unregister(page)


@pytest.fixture
def notes_panel(page):
    _register(page, "notes")
    yield
    _unregister(page)


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


def test_toggle_sits_left_of_run_and_opens_the_column(page, sql_panel):
    _open_sql(page)
    btn = page.locator("#sqlPluginButtons .plugin-panel-btn", has_text="Helper")
    assert btn.count() == 1
    assert btn.get_attribute("aria-pressed") == "false"
    bx = btn.bounding_box()
    rx = page.locator("#btnRunSql").bounding_box()
    assert bx["x"] + bx["width"] <= rx["x"]
    # No panel yet: no column, no handle, the page is exactly as it was.
    assert page.evaluate("() => document.getElementById('sqlPluginPanels').hidden") is True
    btn.click()
    page.wait_for_selector("#sqlPluginPanels:not([hidden]) .plugin-panel", state="visible")
    assert page.locator("#sqlPluginPanels .plugin-panel").inner_text() == "panel here"
    assert btn.get_attribute("aria-pressed") == "true"
    assert page.evaluate("() => document.getElementById('sqlPanelResize').hidden") is False
    # The column sits to the right of the editor
    ax = page.locator("#sqlPluginPanels").bounding_box()
    tx = page.locator("#sqlText").bounding_box()
    assert ax["x"] >= tx["x"] + tx["width"]
    # Leaves with the page, returns with it, and onShow fires on the return
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_selector("#sqlview[hidden]", state="attached")
    assert page.evaluate("() => document.getElementById('sqlPluginPanels').hidden") is True
    shows = page.evaluate("() => window.__shows")
    _open_sql(page)
    page.wait_for_selector("#sqlPluginPanels:not([hidden])")
    page.wait_for_function("(n) => window.__shows > n", arg=shows)
    # The toggle is remembered
    assert json.loads(page.evaluate("() => localStorage.getItem('winnow.panels')"))["fake.helper"] is True


def test_sql_page_api_sets_runs_and_reports(page, sql_panel):
    _open_sql(page)
    page.locator("#sqlPluginButtons .plugin-panel-btn").click()
    page.wait_for_function("() => !!window.__panelCtx")
    page.evaluate("() => { window.__runs = []; window.__panelCtx.sqlPage.onRun((d) => window.__runs.push(d)); }")
    # setText replaces the active tab and persists it; text() reads it back
    page.evaluate("() => window.__panelCtx.sqlPage.setText('SELECT 41 + 1 AS answer')")
    page.wait_for_function("() => document.getElementById('sqlText').value === 'SELECT 41 + 1 AS answer'")
    assert page.evaluate("() => window.__panelCtx.sqlPage.text()") == "SELECT 41 + 1 AS answer"
    page.wait_for_function("() => __winnow.activeSqlTab().savedSql === 'SELECT 41 + 1 AS answer'")
    # run(): the page's own Run path — the result paints in the pane and resolves
    r = page.evaluate("() => window.__panelCtx.sqlPage.run()")
    assert r["columns"] == ["answer"] and r["rows"][0][0] == 42
    page.wait_for_selector("#sqlResult table")
    assert page.evaluate("() => window.__panelCtx.sqlPage.result()")["rows"][0][0] == 42
    assert page.evaluate("() => window.__runs.length") == 1
    # Run through the button fires onRun too
    page.click("#btnRunSql")
    page.wait_for_function("() => window.__runs.length === 2")
    assert page.evaluate("() => window.__runs[1].sql") == "SELECT 41 + 1 AS answer"
    # A bad query rejects, and onRun carries the error
    page.evaluate("() => window.__panelCtx.sqlPage.setText('SELECT * FROM no_such_table')")
    err = page.evaluate("() => window.__panelCtx.sqlPage.run().then(() => 'resolved', (e) => e.message)")
    assert "no_such_table" in err
    assert "error" in page.evaluate("() => window.__runs[window.__runs.length - 1].result")
    # selectedRows follows the pane's selection (a rid-bearing result)
    page.evaluate("() => window.__panelCtx.sqlPage.setText('SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 5')")
    page.evaluate("() => window.__panelCtx.sqlPage.run()")
    page.wait_for_selector("#sqlResult table")
    assert page.evaluate("() => window.__panelCtx.sqlPage.selectedRows()")["rows"] == []
    page.locator("#sqlResult tr").nth(2).click()
    sel = page.evaluate("() => window.__panelCtx.sqlPage.selectedRows()")
    assert sel["columns"][:2] == ["rid", "EventId"] and len(sel["rows"]) == 1 and sel["rows"][0][0] == 2


def test_set_text_in_a_new_tab_keeps_the_analysts_query(page, sql_panel):
    _open_sql(page)
    page.locator("#sqlText").fill("SELECT 'mine'")
    page.locator("#sqlPluginButtons .plugin-panel-btn").click()
    page.wait_for_function("() => !!window.__panelCtx")
    before = page.evaluate("() => __winnow.S.sqlTabs.length")
    page.evaluate("() => window.__panelCtx.sqlPage.setText('SELECT 2', { newTab: 'From plugin' })")
    page.wait_for_function("(n) => __winnow.S.sqlTabs.length === n + 1", arg=before)
    assert page.locator("#sqlTabs .sql-tab", has_text="From plugin").count() == 1
    assert page.locator("#sqlText").input_value() == "SELECT 2"
    assert any(t["sql"] == "SELECT 'mine'" for t in page.evaluate("() => __winnow.S.sqlTabs"))


def test_notes_api_loads_before_it_writes(page, notes_panel):
    """An insert before Notes was ever opened must land AFTER the saved body,
    not replace it — the editor is seeded first."""
    page.evaluate("() => __winnow.post('/api/case/notes', { body: '# Existing narrative\\n' })")
    # Mount from the Notes page, then leave it: the API is used while the page is hidden
    page.click("#tabNotes")
    page.wait_for_selector("#notesview:not([hidden])")
    page.locator("#notesPluginButtons .plugin-panel-btn", has_text="Helper").click()
    page.wait_for_function("() => !!window.__panelCtx")
    assert page.evaluate("() => document.getElementById('notesPluginPanels').hidden") is False
    page.evaluate("() => __winnow.resetNotes()")   # forget the body, as a fresh page would not have it yet
    page.evaluate("() => __winnow.showGridTab()")
    page.evaluate("() => { window.__changes = 0; window.__panelCtx.notesPage.onChange(() => window.__changes++); }")
    page.evaluate("() => window.__panelCtx.notesPage.insert('- inserted by a plugin\\n')")
    page.wait_for_function("() => window.__changes === 1")
    text = page.evaluate("() => window.__panelCtx.notesPage.text()")
    assert text.startswith("# Existing narrative") and "inserted by a plugin" in text
    page.wait_for_function("() => ['Saved', ''].includes(document.getElementById('notesSaved').textContent)")
    saved = page.evaluate("() => __winnow.api('/api/case/notes')")
    assert saved["body"].startswith("# Existing narrative") and "inserted by a plugin" in saved["body"]
    page.evaluate("() => window.__panelCtx.notesPage.setText('replaced')")
    page.wait_for_function("() => document.getElementById('notesEditor').value === 'replaced'")
    assert page.evaluate("() => window.__changes") == 2
    page.evaluate("() => __winnow.post('/api/case/notes', { body: '' })")


def test_the_column_resizes_and_remembers(page, sql_panel):
    _open_sql(page)
    page.locator("#sqlPluginButtons .plugin-panel-btn").click()
    page.wait_for_selector("#sqlPluginPanels:not([hidden])")
    w0 = page.locator("#sqlPluginPanels").bounding_box()["width"]
    h = page.locator("#sqlPanelResize").bounding_box()
    page.mouse.move(h["x"] + 2, h["y"] + 100)
    page.mouse.down()
    page.mouse.move(h["x"] - 120, h["y"] + 100, steps=6)
    page.mouse.up()
    w1 = page.locator("#sqlPluginPanels").bounding_box()["width"]
    assert w1 > w0 + 80
    assert abs(int(page.evaluate("() => localStorage.getItem('winnow.pagepanels.width')")) - w1) <= 2
    page.evaluate("() => localStorage.removeItem('winnow.pagepanels.width')")

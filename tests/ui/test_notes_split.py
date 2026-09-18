"""Notes: the editor and a live preview side by side. Typing renders
without a click, the three buttons choose which panes show, the divider
drags (its position remembered per browser) and double-clicks back to
half, and a plugin's notesPage.setText lands in the preview like typing.

The server and its case are session-scoped, so every test here leaves
the notes body empty again — through the editor's own input event, so
the autosave that follows is the one that clears it.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PANEL_JS = """
export default function mount(container, winnow) {
  container.textContent = 'panel here';
  window.__panelCtx = winnow;
}
"""


def _open_notes(page):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")


def _leave_clean(page):
    page.evaluate("() => { const ed = document.getElementById('notesEditor'); ed.value = ''; "
                  "ed.dispatchEvent(new Event('input')); }")
    page.wait_for_function(
        "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)


def _mode(page):
    return page.evaluate("() => document.getElementById('notesSplit').dataset.mode")


@pytest.fixture
def notes_panel(page):
    page.route("**/plugin_assets/fake/ui/panel.js*",
               lambda route: route.fulfill(status=200, content_type="text/javascript", body=PANEL_JS))
    page.evaluate("() => { __winnow.S.pluginPagePanels = [{ id: 'fake.helper', local_id: 'helper', plugin: 'fake', "
                  "plugin_fs: 'fake', page: 'notes', label: 'Helper', entry: 'ui/panel.js', description: 'a helper', gen: 1 }]; "
                  "__winnow.renderPluginPanelButtons(); }")
    yield
    page.evaluate("() => { __winnow.togglePluginPanel('fake.helper', false); __winnow.S.pluginPagePanels = []; "
                  "__winnow.renderPluginPanelButtons(); localStorage.removeItem('winnow.panels'); __winnow.showGridTab(); }")


def test_typing_renders_the_preview_without_a_click(page):
    _open_notes(page)
    try:
        assert _mode(page) == "split"
        editor = page.locator("#notesEditor")
        preview = page.locator("#notesPreview")
        assert editor.is_visible() and preview.is_visible() and page.locator("#notesDivider").is_visible()
        # Side by side: the preview starts where the editor ends, and both
        # take the page's height.
        ed, pv = editor.bounding_box(), preview.bounding_box()
        assert pv["x"] >= ed["x"] + ed["width"] - 1
        assert abs(pv["height"] - ed["height"]) < 2
        editor.fill("# Live\n- **rclone** from FILESRV01")
        # No button was clicked; the preview follows the keystrokes.
        page.wait_for_function(
            "() => document.getElementById('notesPreview').innerHTML.includes('<h1>Live</h1>')", timeout=3000)
        assert "<strong>rclone</strong>" in preview.inner_html()
        assert _mode(page) == "split"
        # A case switch takes the preview with the editor.
        page.evaluate("() => __winnow.resetNotes()")
        assert page.evaluate("() => document.getElementById('notesPreviewBody').innerHTML") == ""
    finally:
        _leave_clean(page)


def test_edit_split_preview_choose_the_panes(page):
    _open_notes(page)
    try:
        editor, preview, divider = (page.locator("#notesEditor"), page.locator("#notesPreview"),
                                    page.locator("#notesDivider"))
        page.locator("#btnNotesEdit").click()
        assert _mode(page) == "edit"
        assert editor.is_visible() and not preview.is_visible() and not divider.is_visible()
        assert page.locator("#btnNotesEdit").get_attribute("aria-pressed") == "true"
        assert page.locator("#btnNotesSplit").get_attribute("aria-pressed") == "false"
        assert page.locator("#btnNotesPreview").get_attribute("aria-pressed") == "false"
        # Editor-only: the editor takes the whole row.
        ed = editor.bounding_box()
        row = page.locator("#notesSplit").bounding_box()
        assert abs(ed["width"] - row["width"]) < 2
        # Typed while the preview was hidden…
        editor.fill("## Typed in edit mode")
        page.locator("#btnNotesPreview").click()
        assert _mode(page) == "preview"
        assert preview.is_visible() and not editor.is_visible() and not divider.is_visible()
        assert page.locator("#btnNotesPreview").get_attribute("aria-pressed") == "true"
        # …and there the moment the preview shows.
        assert "<h2>Typed in edit mode</h2>" in preview.inner_html()
        # Link ▾ writes at the editor's cursor, so from preview-only it brings the editor back.
        page.locator("#btnNotesLink").click()
        page.wait_for_selector(".menu")
        assert _mode(page) == "split"
        assert editor.is_visible() and preview.is_visible() and divider.is_visible()
        page.evaluate("() => __winnow.closeMenu()")
        page.locator("#btnNotesSplit").click()
        assert _mode(page) == "split"
        assert page.locator("#btnNotesSplit").get_attribute("aria-pressed") == "true"
    finally:
        _leave_clean(page)


def test_the_divider_drags_remembers_and_resets(page):
    _open_notes(page)
    row_w = page.locator("#notesSplit").bounding_box()["width"]
    w0 = page.locator("#notesEditor").bounding_box()["width"]
    assert abs(w0 - row_w / 2) < 6, (w0, row_w)
    h = page.locator("#notesDivider").bounding_box()
    page.mouse.move(h["x"] + h["width"] / 2, h["y"] + 100)
    page.mouse.down()
    page.mouse.move(h["x"] + h["width"] / 2 - 200, h["y"] + 100, steps=8)
    page.mouse.up()
    w1 = page.locator("#notesEditor").bounding_box()["width"]
    assert w1 < w0 - 150, (w0, w1)
    prefs = json.loads(page.evaluate("() => localStorage.getItem('winnow.notes')"))
    assert abs(prefs["split"] - w1 / row_w) < 0.02, (prefs, w1, row_w)
    # A reload keeps the ratio…
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    _open_notes(page)
    w2 = page.locator("#notesEditor").bounding_box()["width"]
    assert abs(w2 - w1) < 3, (w1, w2)
    # …and opens in Split regardless of where the last visit left the mode.
    assert _mode(page) == "split"
    # Double-click puts it back to half and remembers that too.
    page.locator("#notesDivider").dblclick()
    w3 = page.locator("#notesEditor").bounding_box()["width"]
    assert abs(w3 - row_w / 2) < 6, (w3, row_w)
    assert json.loads(page.evaluate("() => localStorage.getItem('winnow.notes')"))["split"] == 0.5


def test_a_plugin_set_text_lands_in_the_preview(page, notes_panel):
    _open_notes(page)
    try:
        page.locator("#notesPluginButtons .plugin-panel-btn", has_text="Helper").click()
        page.wait_for_function("() => !!window.__panelCtx")
        # The plugin column is open with its own handle; the split's divider is untouched by it.
        assert page.evaluate("() => document.getElementById('notesPanelResize').hidden") is False
        assert page.locator("#notesDivider").is_visible()
        page.evaluate("() => window.__panelCtx.notesPage.setText('# From a plugin\\n- via setText')")
        page.wait_for_function(
            "() => document.getElementById('notesPreview').innerHTML.includes('<h1>From a plugin</h1>')", timeout=3000)
        assert _mode(page) == "split"
        # Both panes still clear the 220px floor beside the plugin column.
        assert page.locator("#notesEditor").bounding_box()["width"] >= 220
        assert page.locator("#notesPreview").bounding_box()["width"] >= 220
        page.wait_for_function(
            "() => ['Saved', ''].includes(document.getElementById('notesSaved').textContent)", timeout=6000)
    finally:
        _leave_clean(page)

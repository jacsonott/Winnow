"""Notes: the editor and a live preview side by side. Typing renders
without a click, the three buttons choose which panes show, the divider
drags (its position remembered per browser) and double-clicks back to
half, a plugin's notesPage.setText lands in the preview like typing, the
220px floor holds when the row narrows underneath a set split, and the
editor's right edge belongs to the editor rather than the divider.

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
        # (The floor beside the column is pinned where it actually binds —
        # test_the_row_narrowing_re_clamps_and_widening_gives_it_back — not
        # here, where a 50/50 of ~895px clears 220px without trying.)
        page.wait_for_function(
            "() => ['Saved', ''].includes(document.getElementById('notesSaved').textContent)", timeout=6000)
    finally:
        _leave_clean(page)


def test_the_clamp_floors_each_pane_and_falls_back_to_even(page):
    """clampNotesSplit pinned as a function: on a narrow row the 220px floor
    beats the 0.2–0.8 clamp, a row too narrow for two floors splits evenly
    whatever was asked for, and no layout at all (width 0) leaves only the
    ratio clamp. The function is pure, so the bare-clamp implementation
    that the layout assertions can't tell apart fails here."""
    def clamp(ratio, width):
        return page.evaluate(f"() => __winnow.clampNotesSplit({ratio}, {width})")
    # 500px row: the floor is 220/500 = 0.44 of it, so 0.9 stops at 0.56.
    assert clamp(0.9, 500) == pytest.approx(1 - 220 / 500)
    assert clamp(0.1, 500) == pytest.approx(220 / 500)
    assert clamp(0.5, 500) == 0.5
    # 300px: two floors need 440 — even split, whatever was asked for.
    assert clamp(0.1, 300) == 0.5
    assert clamp(0.9, 300) == 0.5
    # No layout yet: the ratio clamp alone.
    assert clamp(0.8, 0) == 0.8
    assert clamp(0.95, 0) == 0.8
    assert clamp(0.05, 0) == 0.2
    assert page.evaluate("() => __winnow.clampNotesSplit('junk', 500)") == 0.5


def test_the_row_narrowing_re_clamps_and_widening_gives_it_back(page, notes_panel):
    """A stored 0.8 split, then the plugin column opened and dragged wider —
    the sequence an analyst actually performs — squeezes the preview only
    to the floor, leaves the stored ratio alone, and closing the column
    hands the editor its 0.8 back. Nothing touches the divider."""
    page.evaluate("() => localStorage.setItem('winnow.notes', JSON.stringify({ split: 0.8 }))")
    _open_notes(page)
    try:
        row0 = page.locator("#notesSplit").bounding_box()["width"]
        ed0 = page.locator("#notesEditor").bounding_box()["width"]
        pv0 = page.locator("#notesPreview").bounding_box()["width"]
        # Wide row: 0.8 fits over the floor, so 0.8 it is.
        assert abs(ed0 - row0 * 0.8) < 6 and pv0 >= 220, (row0, ed0, pv0)
        # Open the plugin column. Nothing on the Notes page ran; only the row got narrower.
        page.locator("#notesPluginButtons .plugin-panel-btn", has_text="Helper").click()
        page.wait_for_function("() => !!window.__panelCtx")
        page.wait_for_function(
            "(w) => document.getElementById('notesSplit').getBoundingClientRect().width < w",
            arg=row0 - 300, timeout=3000)
        row1 = page.locator("#notesSplit").bounding_box()["width"]
        # Here 0.8 of the row leaves the preview under 220px: the floor binds, not the 0.8 cap.
        assert row1 * 0.2 < 220 < row1 * 0.8 - 20, row1
        page.wait_for_function(
            "() => { const w = document.getElementById('notesPreview').getBoundingClientRect().width; "
            "return w >= 218 && w < 240; }", timeout=3000)
        ed1 = page.locator("#notesEditor").bounding_box()["width"]
        assert ed1 < row1 * 0.8 - 20, (row1, ed1)
        # The stored ratio is untouched: this was the floor, not a new choice.
        assert json.loads(page.evaluate("() => localStorage.getItem('winnow.notes')"))["split"] == 0.8
        # Drag the COLUMN wider (its own handle, not the divider): the preview
        # holds at the floor and the editor is what gives.
        h = page.locator("#notesPanelResize").bounding_box()
        page.mouse.move(h["x"] + h["width"] / 2, h["y"] + 100)
        page.mouse.down()
        page.mouse.move(h["x"] + h["width"] / 2 - 250, h["y"] + 100, steps=8)
        page.mouse.up()
        page.wait_for_function(
            "(w) => document.getElementById('notesSplit').getBoundingClientRect().width < w",
            arg=row1 - 200, timeout=3000)
        row2 = page.locator("#notesSplit").bounding_box()["width"]
        page.wait_for_function(
            "() => { const w = document.getElementById('notesPreview').getBoundingClientRect().width; "
            "return w >= 218 && w < 240; }", timeout=3000)
        ed2 = page.locator("#notesEditor").bounding_box()["width"]
        assert ed2 < ed1 - 150, (ed1, ed2)
        assert ed2 + page.locator("#notesPreview").bounding_box()["width"] <= row2 + 1, (ed2, row2)
        assert json.loads(page.evaluate("() => localStorage.getItem('winnow.notes')"))["split"] == 0.8
        # Close the column: the room comes back, and so does the 0.8.
        page.evaluate("() => __winnow.togglePluginPanel('fake.helper', false)")
        page.wait_for_function(
            "() => Math.abs(document.getElementById('notesEditor').getBoundingClientRect().width - "
            "document.getElementById('notesSplit').getBoundingClientRect().width * 0.8) < 6", timeout=3000)
        assert page.locator("#notesPreview").bounding_box()["width"] >= 220
    finally:
        _leave_clean(page)


def test_the_editor_edge_belongs_to_the_editor_not_the_divider(page):
    """The divider's hit target reaches into the preview only. The editor's
    right edge is where the textarea's vertical scrollbar sits on
    classic-scrollbar platforms (headless Chromium's overlay scrollbars
    take no width, so the strip is asserted by position), and a handle
    that reached into it made grabbing the scrollbar thumb start a split
    drag instead of a scroll."""
    _open_notes(page)
    ed = page.locator("#notesEditor").bounding_box()
    right = ed["x"] + ed["width"]
    y = ed["y"] + ed["height"] / 2

    def under(x):
        return page.evaluate("([x, y]) => document.elementFromPoint(x, y).id", [x, y])
    for dx in (1, 2, 3, 4):
        assert under(right - dx) == "notesEditor", (dx, under(right - dx))
    # The handle starts at the editor's edge and reaches over the preview's padding.
    assert under(right + 1) == "notesDivider"
    assert under(right + 7) == "notesDivider"
    h = page.locator("#notesDivider").bounding_box()
    assert abs(h["x"] - right) < 1, (h, right)
    # A drag started 2px inside the editor's edge is the editor's (a scroll,
    # a text selection) — the split does not move and nothing is remembered.
    page.mouse.move(right - 2, y)
    page.mouse.down()
    page.mouse.move(right - 2 - 200, y, steps=8)
    page.mouse.up()
    assert abs(page.locator("#notesEditor").bounding_box()["width"] - ed["width"]) < 1
    assert page.evaluate("() => localStorage.getItem('winnow.notes')") is None

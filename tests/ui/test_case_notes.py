"""Case notes tab — a Markdown scratchpad that travels in the .db: type,
autosave, preview renders, and it survives a reload (server persistence)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_notes_type_preview_and_persist(page):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    editor = page.locator("#notesEditor")
    editor.fill("# Findings\n- **rclone** exfil from FILESRV01\n- `psexec` lateral")
    # autosave debounce
    page.wait_for_function(
        "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)
    # preview renders the markdown
    page.locator("#btnNotesPreview").click()
    html = page.locator("#notesPreview").inner_html()
    assert "<h1>" in html and "<strong>rclone</strong>" in html and "<code>psexec</code>" in html

    # reload: the body comes back from the case file
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    page.wait_for_function(
        "() => document.getElementById('notesEditor').value.includes('rclone')", timeout=8000)
    # leave it clean for the next test
    page.locator("#notesEditor").fill("")
    page.wait_for_function(
        "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)


def test_markdown_renderer_escapes_and_formats(page):
    html = page.evaluate("""(md) => __winnow.renderMarkdown(md)""",
                         "# H\n<script>x</script>\n- **b** and `c`\n[link](https://ex.com)")
    assert "&lt;script&gt;" in html          # escaped, not executed
    assert "<h1>H</h1>" in html
    assert "<strong>b</strong>" in html and "<code>c</code>" in html
    assert '<a href="https://ex.com" target="_blank"' in html

"""The Excel sheet picker, driven through the real import queue: an .xlsx
queued by path must demand its sheets be picked (same gate as SQLite),
list every data sheet in its pane, and import the checked one as a
background job. The wiring under test is exactly what a refactor of the
pane SQLite and Excel share (buildUnitSettings) could silently drop for
one of its two kinds."""

from __future__ import annotations

import datetime
import time

import pytest

pytestmark = pytest.mark.ui

openpyxl = pytest.importorskip("openpyxl")


def test_sheet_picker_flow(page, tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["When", "Host"])
    ws.append([datetime.datetime(2026, 1, 2, 3, 4, 5), "srv01"])
    ws.append([datetime.datetime(2026, 1, 2, 3, 4, 6), "srv02"])
    wb.create_sheet("Notes").append(["Ignore me"])
    path = tmp_path / "mini.xlsx"
    wb.save(path)

    page.evaluate("async (p) => { __winnow.S.importQueue = []; await __winnow.queuePaths([{ path: p, name: 'mini.xlsx' }]); }", str(path))
    page.evaluate("() => __winnow.openImportModal()")
    page.wait_for_selector("#modal:not([hidden])")

    # Queued as an xlsx that still needs its sheets picked — said in the
    # side list, in the accent — and the gate refuses to import it before
    # that happens, keeping the dialog open on it.
    hint = page.locator("#modalBody .side-rail-item", has_text="mini.xlsx").locator(".side-rail-hint")
    assert hint.inner_text() == "Excel · pick sheets"
    assert "warn" in hint.get_attribute("class")
    page.locator("#modalBody .import-foot .btn.primary").click()
    assert "Pick which sheets" in page.locator("#toast").inner_text()
    assert page.locator("#modal").is_visible()

    page.wait_for_selector("#modalBody .import-unit-name >> text=Events")
    rows = page.locator("#modalBody .import-unit")
    assert rows.count() == 2  # both data sheets listed, with row counts
    assert "2 rows" in rows.first.inner_text()
    rows.first.locator("input[type=checkbox]").check()

    # The side list says what was picked; the import goes ahead.
    page.wait_for_function("() => [...document.querySelectorAll('#modalBody .side-rail-hint')].some((n) => n.textContent === 'Excel · 1 sheet')")
    assert "1 of 2 sheets ticked" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
    page.locator("#modalBody .import-foot .btn.primary").click()

    new_id = None
    deadline = time.time() + 30
    while time.time() < deadline and new_id is None:
        # Poll from Python — wait_for_function doesn't await promises.
        srcs = page.evaluate("""() => fetch('/api/sources',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        new_id = next((s["id"] for s in srcs if s["name"] == "mini.Events"), None)
        if new_id is None:
            time.sleep(0.3)
    assert new_id is not None, "the imported sheet never became a source"
    try:
        # The date-styled cell must land in the grid as ISO text, not a serial.
        page.evaluate("() => __winnow.loadSources()")
        page.evaluate("(id) => __winnow.openSource(id)", new_id)
        page.wait_for_function(
            """() => { const r = __winnow.rowAt(0);
                       return r && r.cells.includes('2026-01-02 03:04:05'); }""", timeout=15000)
    finally:
        page.evaluate("""(id) => fetch('/api/source/' + id, { method: 'DELETE',
          headers: { 'X-Timeline-Lite-Client': '1' } })""", new_id)
        page.evaluate("() => __winnow.loadSources()")

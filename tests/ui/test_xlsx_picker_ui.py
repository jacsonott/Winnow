"""The Excel sheet picker, driven through the real import queue: an .xlsx
queued by path must demand its sheets be picked (same gate as SQLite),
open the picker listing every data sheet, and import the checked one as a
background job. The wiring under test is exactly what a refactor of the
shared openUnitPicker could silently drop for one of its two callers."""

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

    page.evaluate("(p) => __winnow.queuePaths([{ path: p, name: 'mini.xlsx' }])", str(path))
    page.evaluate("() => __winnow.openImportModal()")
    page.wait_for_selector("#modal:not([hidden])")

    # Queued as an xlsx that still needs its sheets picked — and the gate
    # refuses to import it before that happens.
    assert "pick sheets · xlsx" in page.locator(".session-row .count").first.inner_text()
    page.locator("#modalBody .btn", has_text="Import all queued").click()
    assert "Pick which sheets" in page.locator("#toast").inner_text()

    page.locator("#modalBody .btn", has_text="Pick sheets…").click()
    page.wait_for_selector(".session-name >> text=Events")
    rows = page.locator("#modalBody .session-row")
    assert rows.count() == 2  # both data sheets listed, with row counts
    assert "2 rows" in rows.first.inner_text()
    rows.first.locator("input[type=checkbox]").check()
    page.locator("#modalBody .btn", has_text="Use selected sheets").click()

    # Back on the queue: configured, showing what was picked.
    page.wait_for_selector(".session-row .count >> text=1 sheet · xlsx")
    page.locator("#modalBody .btn", has_text="Import all queued").click()

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

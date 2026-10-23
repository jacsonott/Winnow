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

    page.evaluate("(p) => { __winnow.S.importQueue = []; __winnow.queuePaths([{ path: p, name: 'mini.xlsx' }]); }", str(path))
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


SHEETS = {"tables": [{"name": "Events", "row_count": 2, "columns": [{"name": "When", "type": "TEXT"}],
                      "likely_timestamp_columns": []}]}


def _choose(page, name):
    page.locator("#modalBody .side-rail-item", has_text=name).click()
    page.wait_for_function("(n) => document.querySelector('#modalBody .import-file-name')?.textContent === n", arg=name)


@pytest.fixture
def listings(page):
    """The upload route that lists a workbook's sheets, held in the page
    until the test answers it — page.route would copy each 33 MB body out
    to the test first, which takes seconds. Yields a wait for the n-th."""
    page.evaluate("""() => {
      window.__realFetch = window.fetch;
      window.__listings = [];
      window.fetch = (url, opts) => (String(url).includes('/api/ingest/xlsx/preview')
        ? new Promise((resolve) => window.__listings.push(resolve))
        : window.__realFetch(url, opts));
    }""")
    yield lambda n: page.wait_for_function("(n) => window.__listings.length === n", arg=n)
    page.evaluate("() => { window.fetch = window.__realFetch; __winnow.closeModal(); __winnow.S.importQueue = []; }")


def _answer(page, i, status=200):
    page.evaluate("""([i, status, body]) => window.__listings[i](new Response(JSON.stringify(body),
      { status, headers: { 'Content-Type': 'application/json' } }))""",
                  [i, status, SHEETS if status == 200 else {"detail": "not a workbook"}])


def _queue(page, name, size):
    """A workbook of `size` bytes and a small CSV after it, both
    browser-picked, and the dialog open on the workbook."""
    page.evaluate("""async ([name, size]) => {
      __winnow.S.importQueue = [];
      await __winnow.queueFiles([new File([new Uint8Array(size)], name),
                                 new File(['When,User\\n2026-03-14 08:00:00,carol\\n'], 'comma.csv')]);
      __winnow.openImportModal();
    }""", [name, size])
    page.wait_for_function("(n) => document.querySelector('#modalBody .import-file-name')?.textContent === n", arg=name)


def test_a_workbooks_sheets_are_read_once_however_often_it_is_chosen(page, listings):
    """Each time a file was chosen its pane was built afresh and asked
    afresh: a large browser-picked workbook showed Load preview again, the
    answer to the first press was thrown away when another file was chosen
    in the meantime, and every press sent the whole file up and had the
    server read it again — the sheets never showed, and the reads piled up
    on the server until a CSV import beside them stalled. The list is read
    once, and shown from then on."""
    _queue(page, "huge.xlsx", 33 << 20)
    page.locator("#modalBody .import-preview .btn", has_text="Load preview").click()
    listings(1)

    # Another file while the list is on its way, and back: still that one
    # request, and no button asking to send the file again.
    _choose(page, "comma.csv")
    page.wait_for_selector("#modalBody .preview-colname >> text=User")
    _choose(page, "huge.xlsx")
    assert page.locator("#modalBody .import-preview .btn", has_text="Load preview").count() == 0
    assert "Loading preview" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
    _answer(page, 0)
    page.wait_for_selector("#modalBody .import-unit-name >> text=Events")

    _choose(page, "comma.csv")
    _choose(page, "huge.xlsx")
    page.wait_for_selector("#modalBody .import-unit-name >> text=Events")
    assert page.evaluate("() => window.__listings.length") == 1


def test_a_listing_that_failed_is_asked_for_again(page, listings):
    _queue(page, "book.xlsx", 1024)
    listings(1)
    _answer(page, 0, status=400)
    page.wait_for_selector("#modalBody .import-preview .side-preview-foot >> text=Preview failed")
    _choose(page, "comma.csv")
    _choose(page, "book.xlsx")
    listings(2)
    _answer(page, 1)
    page.wait_for_selector("#modalBody .import-unit-name >> text=Events")

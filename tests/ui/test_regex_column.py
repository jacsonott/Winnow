"""The regex-capture derived column, driven through the real modal: the
reported use case is extracting the base of a URI to filter and sort on."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_regex_capture_column_from_the_modal(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('CommandLine')")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "add derived column"

    op_sel = page.locator("#modalBody select").nth(1)
    op_sel.select_option(label="Regex capture")
    page.wait_for_timeout(200)
    # the suggestion tracks the op kind
    assert page.locator(".derived-name").input_value() == "CommandLine (extract)"

    page.locator(".derived-param input").first.fill(r"\\([\w.]+\.exe)")
    page.wait_for_selector(".derived-preview-row")
    assert "powershell.exe" in page.locator(".derived-preview-row").first.inner_text()

    page.locator(".derived-name").fill("Binary")
    page.locator("#modalBody .btn", has_text="Add column").first.click()
    page.wait_for_function(
        "() => __winnow.S.columns.some((c) => c.name === 'Binary' && c.derived)", timeout=15000)
    page.wait_for_function("""() => {
      const i = __winnow.S.columns.findIndex((c) => c.name === 'Binary');
      const r = __winnow.rowAt(0);
      return r && r.cells[i] === 'powershell.exe';
    }""", timeout=15000)

    # filterable like any column — then clean up for the other tests.
    page.locator('.fcell input[data-col="Binary"]').fill("=powershell.exe")
    page.locator('.fcell input[data-col="Binary"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 200")
    defs = page.evaluate("""() => fetch('/api/derived?source_id=' + __winnow.S.sourceId,
      { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
    binary = next(d for d in defs if d["name"] == "Binary")
    page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
      headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.status)""", binary["id"])
    page.evaluate("(id) => __winnow.openSource(id)", page.evaluate("() => __winnow.S.sourceId"))

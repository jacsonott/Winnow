"""Adding a Row as JSON column through the modal: every other column
pre-filled as chips, the Skip empty cells checkbox, a live preview, and
the column arriving in the grid as one-line JSON."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def test_row_as_json_prefills_every_column_and_lands_in_the_grid(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('Host')")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody select").nth(0).select_option(label="Combine columns")
    page.wait_for_timeout(150)
    page.locator("#modalBody select").nth(2).select_option(label="Row as JSON")
    page.wait_for_timeout(200)
    assert page.locator(".derived-name").input_value() == "Row as JSON"
    chips = [c.replace("✕", "").strip() for c in page.locator(".derived-columns .fb-groupby-chip").all_inner_texts()]
    assert chips == ["Timestamp", "EventId", "ExtremelyLongColumnHeaderName", "CommandLine"]   # all but Host
    cb = page.locator("#modalBody .derived-param-bool input[type=checkbox]")
    assert cb.count() == 1 and not cb.is_checked()
    # The preview shows a JSON object with the parse column first
    page.wait_for_function("() => /\\{\"Host\":\"H\\d\",\"Timestamp\"/.test(document.querySelector('#modalBody').textContent)")
    # Drop one column, tick skip-empty, add the column
    page.locator(".derived-columns .fb-groupby-chip", has_text="ExtremelyLongColumnHeaderName").locator("button").click()
    cb.check()
    page.wait_for_timeout(300)
    page.locator("#modalBody button", has_text="Add column").click()
    page.wait_for_function("() => __winnow.S.columns.some((c) => c.name === 'Row as JSON')", timeout=30_000)
    # The backfill is a job: poll the first row through the rows API (cells
    # are in S.columns order) until the new column has its value.
    cell = ""
    for _ in range(100):
        cell = page.evaluate("async () => { const i = __winnow.S.columns.findIndex((c) => c.name === 'Row as JSON'); "
                             "if (i < 0 || !__winnow.S.view) return ''; "
                             "const r = await __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=1`); "
                             "return (r.rows[0] && r.rows[0].cells[i]) || ''; }")
        if cell:
            break
        page.wait_for_timeout(200)
    obj = json.loads(cell)
    assert list(obj) == ["Host", "Timestamp", "EventId", "CommandLine"]
    assert obj["EventId"] in (4624, 4625, 4688, 1) and isinstance(obj["EventId"], int)
    assert obj["Host"].startswith("H")

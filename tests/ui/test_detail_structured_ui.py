"""The detail pane's structured view of a JSON cell, on the shared table's
Payload column. The pane lays the document out as nodes that each know
their path and value, and right-clicking one filters the column to that
value or adds the field as a column of its own. Before ui.csv carried a
JSON column no browser test had opened one in the pane, so none of this
had run in a test.

Every test reads the row it opened rather than assuming which one that
is, so a sort another module left on the shared view can't move it."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PAYLOAD = '#detailFields dd[data-col="Payload"]'


def _open_first_row(page):
    """Opens row 0 in the pane and returns its Payload, parsed."""
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_selector(".row")
    page.locator(".row").first.locator(".cell").first.click()
    page.evaluate("() => __winnow.showDetail(0)")
    page.wait_for_selector(f"{PAYLOAD} pre.detail-pretty")
    raw = page.evaluate("() => __winnow.rowAt(0).cells[__winnow.S.columns.findIndex((c) => c.name === 'Payload')]")
    return json.loads(raw)


def _close(page):
    page.evaluate("() => { __winnow.closeMenu(); __winnow.hideDetailPane(); }")


def _field_menu(page, path, item):
    page.locator(f'{PAYLOAD} .struct-node[data-path="{path}"]').first.click(button="right")
    page.locator(".menu .menu-item", has_text=item).click()


def test_a_json_cell_opens_as_a_document_of_addressable_fields(page):
    doc = _open_first_row(page)
    try:
        nodes = page.locator(f"{PAYLOAD} .struct-node").evaluate_all(
            "(ns) => ns.map((n) => [n.dataset.path, n.dataset.value === undefined ? null : n.dataset.value])")
        # Each leaf answers to its path and holds its value as the data has
        # it, which is what the filter and copy actions act on. A key and
        # its value share the path, so both halves of `"user": "u0"` are
        # the same field.
        assert nodes.count(["$.user", doc["user"]]) == 2, nodes
        assert nodes.count(["$.proc.pid", str(doc["proc"]["pid"])]) == 2, nodes
        assert nodes.count(["$.proc.name", doc["proc"]["name"]]) == 2, nodes
        # The object in between is addressable too, with no single value.
        assert ["$.proc", None] in nodes
    finally:
        _close(page)


def test_filtering_to_a_fields_value_keeps_only_the_rows_that_hold_it(page):
    doc = _open_first_row(page)
    # ui.csv's users cycle u0, u1, u2 down its 200 rows.
    want = sum(1 for i in range(200) if f"u{i % 3}" == doc["user"])
    try:
        _field_menu(page, "$.user", "Filter Payload to this value")
        page.wait_for_function("(n) => __winnow.S.view && __winnow.S.view.row_count === n", arg=want)
    finally:
        _close(page)
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")


def test_add_as_a_column_extracts_the_field_from_every_row(page, api):
    doc = _open_first_row(page)
    sid = page.evaluate("() => __winnow.S.sourceId")
    try:
        _field_menu(page, "$.proc.pid", "Add as a column")
        page.wait_for_function(
            "() => (__winnow.S.columns.find((c) => c.name === 'proc.pid') || {}).derived_status === 'ready'",
            timeout=30_000)
        page.wait_for_function(
            "(want) => { const i = __winnow.S.columns.findIndex((c) => c.name === 'proc.pid');"
            " const r = __winnow.rowAt(0); return !!r && r.cells[i] === want; }",
            arg=str(doc["proc"]["pid"]), timeout=10_000)
        # Every row, not just the one it was picked from: the 200 pids are
        # 4000-4199 whatever order the view is in.
        col = next(c for c in api(f"/api/derived?source_id={sid}") if c["name"] == "proc.pid")
        assert col["op_id"] == "json_field" and col["params"] == {"path": "$.proc.pid"}
        values = api("/api/sql", "POST",
                     {"sql": 'SELECT MIN(CAST("proc.pid" AS INTEGER)), MAX(CAST("proc.pid" AS INTEGER)), '
                             f'COUNT(DISTINCT "proc.pid") FROM src_{sid}'})
        assert values["rows"][0] == [4000, 4199, 200], values
    finally:
        _close(page)
        for c in api(f"/api/derived?source_id={sid}"):
            if c["name"] == "proc.pid":
                api(f"/api/derived/{c['id']}", "DELETE")
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
        page.wait_for_function("() => !__winnow.S.columns.some((c) => c.name === 'proc.pid')")

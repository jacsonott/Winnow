"""Column names are user data, including the ones a plain JS object
already has a member for.

S.filters and S.layout were `{}` maps keyed by column name, so a CSV with
a `toString`, `constructor` or `__proto__` column read Object.prototype's
members back as that column's filter and layout. With the filter bar on,
the grid never rendered and said nothing; with the filter row, the
`toString` box was prefilled with `function toString() { [native code] }`,
the value picker threw, and typing into `__proto__` filtered nothing —
the assignment set the map's prototype instead of a key. Each test here
asserts one of those symptoms in the terms an analyst met it.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

HEADERS = ["Timestamp", "toString", "constructor", "valueOf", "__proto__", "hasOwnProperty", "Message"]
ROWS = [
    ["2026-03-14 08:00:00", "a", "b", "c", "d", "e", "hello"],
    ["2026-03-14 08:00:01", "a", "b", "c", "d", "e", "world"],
    ["2026-03-14 08:00:02", "x", "y", "z", "w", "v", "foo"],
]
TRAPS = ["toString", "constructor", "valueOf", "__proto__", "hasOwnProperty"]


def _delete_source(server, sid):
    req = urllib.request.Request(server.rstrip("/") + f"/api/source/{sid}", method="DELETE",
                                 headers={"X-Timeline-Lite-Client": "1"})
    return urllib.request.urlopen(req, timeout=30).status


@pytest.fixture
def proto(page, server, server_post, ui_csv):
    path = ui_csv.parent / "proto.csv"
    path.write_text("\n".join(",".join(r) for r in [HEADERS, *ROWS]) + "\n", encoding="utf-8")
    home = page.evaluate("() => __winnow.S.sourceId")
    server_post("/api/ingest/jobs/path", {"path": str(path), "build_fts": False})
    # Queued from outside the page, so nothing in it polls the job.
    for _ in range(150):
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
        if page.evaluate("() => __winnow.S.sources.some((s) => s.name === 'proto.csv' && s.row_count === 3)"):
            break
        page.wait_for_timeout(200)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'proto.csv').id")
    try:
        yield sid
    finally:
        # Shared server: leave the grid on the table the other modules expect.
        _delete_source(server, sid)
        page.evaluate("(id) => { __winnow.loadSources(id); }", home)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=home,
                               timeout=30_000)


def _open(page, sid, filter_ui="row"):
    page.evaluate("(ui) => { __winnow.S.appearance.filterUi = ui; }", filter_ui)
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view"
                           " && __winnow.S.view.source_id === id", arg=sid, timeout=30_000)
    page.wait_for_function("() => document.querySelectorAll('#rows .row').length === 3", timeout=30_000)


def _rows(page):
    return page.evaluate("() => __winnow.S.view.row_count")


def _cells(page, col):
    return page.evaluate("""(col) => {
      const i = __winnow.S.columns.findIndex((c) => c.name === col);
      return [...__winnow.S.pages.values()].flat().map((r) => r.cells[i]).sort();
    }""", col)


def test_the_filter_row_starts_empty_and_filters_every_column(page, proto):
    _open(page, proto, "row")
    for col in TRAPS:
        box = page.locator(f'.fcell input[data-col="{col}"]')
        assert box.input_value() == "", col
        assert page.locator(f'.hcell[data-col="{col}"] .hcell-filter.filtered').count() == 0, col
    for col, value, expect in (("__proto__", "w", ["w"]), ("toString", "x", ["x"]),
                               ("constructor", "b", ["b", "b"])):
        box = page.locator(f'.fcell input[data-col="{col}"]')
        box.fill(value)
        box.press("Enter")
        page.wait_for_function("(n) => __winnow.S.view.row_count === n", arg=len(expect))
        page.wait_for_function("() => document.querySelectorAll('#rows .row').length > 0")
        assert _cells(page, col) == expect, col
        box.fill("")
        box.press("Enter")
        page.wait_for_function("() => __winnow.S.view.row_count === 3")


def test_the_filter_bar_renders_the_grid_and_filters(page, proto):
    _open(page, proto, "bar")     # never painted a row before the fix
    assert page.locator(".filter-chip").count() == 0
    page.locator('.hcell[data-col="__proto__"] .hcell-filter').click()
    box = page.locator('.fcell input[data-col="__proto__"]')
    box.wait_for()
    assert box.input_value() == ""
    box.fill("w")
    box.press("Enter")
    page.wait_for_selector('.filter-chip[data-col="__proto__"]')
    page.wait_for_function("() => __winnow.S.view.row_count === 1")


def test_z_undoes_a_filter_on_a_trap_column(page, proto):
    # The undo history compares snapshots of S.filters, and a snapshot is a
    # plain object: `toString` read off it is Object.prototype's function.
    _open(page, proto, "row")
    for col, value, n in (("toString", "x", 1), ("__proto__", "d", 0)):
        box = page.locator(f'.fcell input[data-col="{col}"]')
        box.fill(value)
        box.press("Enter")
        page.wait_for_function("(n) => __winnow.S.view.row_count === n", arg=n)
    page.locator("#body").focus()
    page.keyboard.press("z")
    page.wait_for_function("() => __winnow.S.view.row_count === 1")
    assert page.locator('.fcell input[data-col="__proto__"]').input_value() == ""
    assert page.locator('.fcell input[data-col="toString"]').input_value() == "x"
    page.keyboard.press("z")
    page.wait_for_function("() => __winnow.S.view.row_count === 3")
    assert page.locator('.fcell input[data-col="toString"]').input_value() == ""


@pytest.mark.parametrize("filter_ui", ["row", "bar"])
def test_the_value_picker_lists_a_trap_columns_values(page, proto, filter_ui):
    _open(page, proto, filter_ui)
    page.evaluate("() => __winnow.openValuePickerForColumn('toString')")
    picker = page.locator(".value-picker")
    picker.wait_for()
    page.wait_for_function("() => /\\bx\\b/.test(document.querySelector('.value-picker').innerText)")
    assert "native code" not in picker.inner_text()


def test_hiding_a_proto_column_hides_only_that_column(page, proto):
    """The Columns panel writes S.layout['__proto__'] = {..., hidden: true},
    which used to set the map's prototype — every column without a layout
    entry of its own then inherited `hidden`, so unticking one column
    emptied the grid."""
    _open(page, proto, "row")
    page.evaluate("() => __winnow.openTableMenu()")
    row = page.locator(".collist-row", has=page.locator("span", has_text="__proto__")).first
    row.locator("input[type=checkbox]").first.uncheck()
    page.wait_for_function("() => !__winnow.visibleCols().includes('__proto__')")
    assert page.evaluate("() => __winnow.visibleCols()") == [c for c in HEADERS if c != "__proto__"]
    # And it is that column's entry, so it saves like one (as JSON text:
    # Playwright's own serialiser drops a `__proto__` key on the way out).
    saved = json.loads(page.evaluate("() => JSON.stringify(__winnow.S.layout)"))
    assert saved["__proto__"]["hidden"] is True
    page.evaluate("() => __winnow.closeModal()")


def test_an_unpinned_trap_column_does_not_draw_as_pinned(page, proto):
    """pinnedOffsets() answered `offsets['toString']` with the inherited
    function, which is not undefined, so an unpinned toString column was
    drawn sticky — it stayed put over the columns scrolling under it."""
    _open(page, proto, "row")
    pinned = page.eval_on_selector_all(".hcell.pinned, #rows .cell.pinned",
                                       "els => els.map((e) => e.dataset.col || e.className)")
    assert pinned == []

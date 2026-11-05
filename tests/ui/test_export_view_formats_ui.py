"""Export's Current view, in each format the dialog offers.

It was CSV or nothing. The pane now has a Format row (CSV, TSV, Excel,
JSON Lines, JSON, Markdown, HTML), and what matters is not that a file
arrives but that each one holds the view on screen: these tests filter
the table and sort it backwards, so a format that read the whole table or
its stored order would download the wrong rows in the wrong order. The
choice is remembered, so the next export starts on the format the analyst
last used, and the button, the side list and the column list all say
which format that is.

The column list is also where the file's columns are chosen: ticked or
not, in any order, Winnow's own included, a column hidden in the grid
taken in. What has to hold is that the file is the list — what was
ticked, in the order it was left — in whatever format, and that choosing
for an export changes nothing in the grid.

A table of its own, deleted again at the end of each test.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from pathlib import Path

import pytest
from openpyxl import load_workbook

pytestmark = pytest.mark.ui

ROWS = [("WKSTN-1", "=cmd|' /C calc'!A1"), ("SRV-02", "whoami /all"), ("WKSTN-3", "net user | findstr <x>")]
FORMATS = [("csv", "csv"), ("tsv", "tsv"), ("xlsx", "xlsx"), ("jsonl", "jsonl"), ("json", "json"),
           ("md", "md"), ("html", "html")]


@pytest.fixture
def table(api, tmp_path):
    f = tmp_path / "exfmt.csv"
    f.write_text("Host,CommandLine\n" + "".join(f'"{h}","{c}"\n' for h, c in ROWS), encoding="utf-8")
    sid = api("/api/ingest/path", "POST", {"path": str(f), "name": f.name, "build_fts": False})["id"]
    try:
        yield sid
    finally:
        api(f"/api/source/{sid}", "DELETE")


def _open_filtered(page, sid):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=sid)
    page.evaluate("""() => { __winnow.S.filters = { Host: 'WKSTN' };
      __winnow.S.sort = [{ column: 'Host', dir: 'desc' }];
      return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 2 && __winnow.busyCount === 0",
                           timeout=15_000)


def _open_export(page):
    page.evaluate("() => __winnow.openExportModal('view')")
    page.wait_for_selector("#modal:not([hidden]) .export-pane[data-section=view] .export-formats")


def _lines_and_commands(fmt, path):
    """(Line, CommandLine) for each row of the file, in file order."""
    if fmt == "xlsx":
        with open(path, "rb") as fh:   # the download has no .xlsx on its name
            ws = load_workbook(fh).active
        rows = [[("" if v is None else str(v)) for v in r] for r in ws.iter_rows(values_only=True)]
        return [(r[0], r[4]) for r in rows[1:]]
    text = Path(path).read_text(encoding="utf-8")
    if fmt in ("csv", "tsv"):
        rows = list(csv.reader(io.StringIO(text), delimiter="," if fmt == "csv" else "\t"))
        return [(r[0], r[4]) for r in rows[1:]]
    if fmt in ("jsonl", "json"):
        objs = [json.loads(x) for x in text.splitlines()] if fmt == "jsonl" else json.loads(text)
        return [(str(o["Line"]), o["CommandLine"]) for o in objs]
    if fmt == "md":
        rows = [re.split(r"(?<!\\)\|", line[1:-1]) for line in text.splitlines()[2:]]
        unescape = lambda c: re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", c.strip())  # noqa: E731
        return [(unescape(r[0]), unescape(r[4])) for r in rows]
    cells = re.findall(r'<tr><td class="n">(\d+)</td><td>.*?</td><td>.*?</td><td>.*?</td><td>(.*?)</td></tr>', text)
    return [(n, html.unescape(c)) for n, c in cells]


def test_each_format_downloads_the_filtered_sorted_view(page, table):
    _open_filtered(page, table)
    want_raw = [("3", "net user | findstr <x>"), ("1", "=cmd|' /C calc'!A1")]
    for key, ext in FORMATS:
        _open_export(page)
        page.locator(f"#modal .export-formats button[data-key={key}]").click()
        assert page.locator(f"#modal .export-formats button[data-key={key}]").get_attribute("aria-pressed") == "true"
        btn = page.locator("#modal .table-menu-foot .btn.primary")
        assert btn.inner_text() == f"Export 2 rows (.{ext})"
        hint = page.locator("#modalBody .side-rail-item[data-section=view] .side-rail-hint").inner_text()
        assert hint == f"2 rows · .{ext}"
        with page.expect_download(timeout=15_000) as dl:
            btn.click()
        download = dl.value
        assert download.suggested_filename == f"timeline-export.{ext}"
        got = _lines_and_commands(key, download.path())
        # The spreadsheet formats guard a formula; the others keep it exact.
        want = [(n, "'" + c if key in ("csv", "tsv", "xlsx") and c.startswith("=") else c) for n, c in want_raw]
        assert got == want, key
        assert page.locator("#modal").is_hidden()


def test_the_html_page_says_what_narrows_the_view(page, table):
    _open_filtered(page, table)
    _open_export(page)
    page.locator("#modal .export-formats button[data-key=html]").click()
    with page.expect_download(timeout=15_000) as dl:
        page.locator("#modal .table-menu-foot .btn.primary").click()
    text = Path(dl.value.path()).read_text(encoding="utf-8")
    about = re.search(r'<p class="about">(.*?)</p>', text).group(1)
    assert about == "Narrowed by Host WKSTN, sorted by Host ↓."
    assert "<title>exfmt.csv</title>" in text


def test_the_format_is_remembered_and_described(page, table):
    _open_filtered(page, table)
    _open_export(page)
    pressed = "#modal .export-formats button[aria-pressed=true]"
    assert page.locator(pressed).get_attribute("data-key") == "csv", "CSV until something else is chosen"
    page.locator("#modal .export-formats button[data-key=jsonl]").click()
    notes = page.locator("#modal .export-pane .mb-notes").inner_text()
    assert "as JSON Lines:" in notes and "exactly as the case holds it" in notes
    assert page.locator("#modal .export-col-row[data-id='w:tags'] .tables-sub").inner_text() == "its tags, as a list"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal", state="hidden")
    _open_export(page)
    assert page.locator(pressed).get_attribute("data-key") == "jsonl"
    assert page.locator("#modal .table-menu-foot .btn.primary").inner_text() == "Export 2 rows (.jsonl)"


@pytest.fixture
def wide(api, tmp_path):
    """Three columns, User hidden in the grid's saved layout."""
    f = tmp_path / "excols.csv"
    f.write_text("Host,CommandLine,User\n" + "".join(f'"{h}","{c}","u{i}"\n' for i, (h, c) in enumerate(ROWS, 1)),
                 encoding="utf-8")
    sid = api("/api/ingest/path", "POST", {"path": str(f), "name": f.name, "build_fts": False})["id"]
    try:
        api("/api/layout", "POST", {"source_id": sid, "payload": {"order": ["Host", "CommandLine", "User"],
                                                                  "columns": {"User": {"hidden": True}}}})
        yield sid
    finally:
        api(f"/api/source/{sid}", "DELETE")


def _listed(page):
    """The list as it reads: each row's key, and whether it is ticked."""
    return page.eval_on_selector_all(
        "#modal .export-col-row",
        "rs => rs.map((r) => [r.dataset.id, r.querySelector('input[type=checkbox]').checked])")


GRID = [["w:line", True], ["w:tags", True], ["w:note", True], ["c:Host", True], ["c:CommandLine", True],
        ["c:User", False]]


def test_the_file_holds_the_columns_ticked_in_the_order_left(page, wide):
    _open_filtered(page, wide)
    layout_before = page.evaluate("() => JSON.stringify([__winnow.S.order, __winnow.S.layout])")
    _open_export(page)
    assert _listed(page) == GRID, "starts as the grid has it, the hidden column unticked"
    assert page.locator("#modal .export-col-row[data-id='c:User'] .tables-sub").inner_text() == "text · hidden in the grid"
    row = lambda key: page.locator(f"#modal .export-col-row[data-id='{key}']")  # noqa: E731
    row("w:line").locator("input").uncheck()
    row("w:note").locator("input").uncheck()
    row("c:User").locator("input").check()
    # The top half of Host's row: User lands before it.
    row("c:User").drag_to(row("c:Host"), target_position={"x": 40, "y": 3})
    page.wait_for_function("() => document.querySelectorAll('#modal .export-col-row')[3].dataset.id === 'c:User'")
    row("c:CommandLine").hover()
    row("c:CommandLine").locator("[data-role=top]").click()
    assert _listed(page) == [["c:CommandLine", True], ["w:line", False], ["w:tags", True], ["w:note", False],
                             ["c:User", True], ["c:Host", True]]
    btn = page.locator("#modal .table-menu-foot .btn.primary")
    with page.expect_download(timeout=15_000) as dl:
        btn.click()
    text = Path(dl.value.path()).read_text(encoding="utf-8")
    assert list(csv.reader(io.StringIO(text))) == [
        ["CommandLine", "Tags", "User", "Host"],
        ["net user | findstr <x>", "", "u3", "WKSTN-3"],
        ["'=cmd|' /C calc'!A1", "", "u1", "WKSTN-1"]]

    # The grid is as it was; the next export starts from it again.
    assert page.evaluate("() => JSON.stringify([__winnow.S.order, __winnow.S.layout])") == layout_before
    _open_export(page)
    assert _listed(page) == GRID
    assert page.locator("#modal .export-cols-tool", has_text="Reset to the grid").is_disabled()


def test_a_choice_holds_across_formats_and_reset_brings_the_grid_back(page, wide):
    _open_filtered(page, wide)
    _open_export(page)
    page.locator("#modal .export-format .export-formats button[data-key=csv]").click()
    tools = lambda label: page.locator("#modal .export-cols-tool", has_text=label)  # noqa: E731
    btn = page.locator("#modal .table-menu-foot .btn.primary")
    tools("None").click()
    assert btn.is_disabled() and btn.inner_text() == "Choose a column to export"
    # All and None act on what the filter lists.
    page.locator("#modal .export-cols-top .side-search").fill("o")
    assert [k for k, _ in _listed(page) if page.locator(f"#modal .export-col-row[data-id='{k}']").is_visible()] == \
        ["w:note", "c:Host", "c:CommandLine"]
    tools("All").click()
    page.locator("#modal .export-cols-top .side-search").fill("")
    picked = [["w:line", False], ["w:tags", False], ["w:note", True], ["c:Host", True], ["c:CommandLine", True],
              ["c:User", False]]
    assert _listed(page) == picked
    page.locator("#modal .export-formats button[data-key=jsonl]").click()
    assert _listed(page) == picked, "the choice survives a change of format"
    with page.expect_download(timeout=15_000) as dl:
        btn.click()
    objs = [json.loads(x) for x in Path(dl.value.path()).read_text(encoding="utf-8").splitlines()]
    assert [list(o) for o in objs] == [["Note", "Host", "CommandLine"]] * 2
    assert [o["Host"] for o in objs] == ["WKSTN-3", "WKSTN-1"]
    _open_export(page)
    tools("None").click()
    assert not tools("Reset to the grid").is_disabled()
    tools("Reset to the grid").click()
    assert _listed(page) == GRID
    assert btn.inner_text() == "Export 2 rows (.jsonl)"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal", state="hidden")

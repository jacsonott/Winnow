"""The detail pane's "Filter <col> to <selection>", "… only" and "Exclude",
and a document node's "Filter <col> to this value", promise a *contains*
of the text. They used to paste the text into the column's filter box,
which reads its own syntax: selecting `whoami | findstr` in a command line
filtered to the rows EQUAL to `whoami` or `findstr` and lost the two that
held the selection; `=cmd` became an equals, `>5` a comparison, `^HKLM`
starts-with, `/usr/bin/` a regex, `*` not-empty, `""` empty and
`!important` does-not-contain.

Each test asserts the rows the grid lands on against the rows that hold
the text, which is what broke, on a table of its own whose command lines
are built so that every misreading lands on a different set."""

from __future__ import annotations

import csv
import json

import pytest

pytestmark = pytest.mark.ui

NAME = "detail_selection_filter.csv"
DOCS = {3: {"op": "=admin"}, 4: {"op": "admin"}, 5: {"note": " padded"}, 6: {"note": "padded"}}
LINES = [
    "cmd.exe /c whoami | findstr admin",
    "cmd.exe /c whoami | findstr admin",
    "whoami",
    "findstr",
    "set opt=cmd",
    "cmd",
    "if %n% >5 goto end",
    "7",
    "echo ^HKLM",
    "HKLM\\Software\\Run",
    "ls /usr/bin/ -la",
    "cat usr/bin/x",
    "echo *",
    'echo ""',
    "color: red !important",
    "net user | findstr admin",
]
ROWS = [(str(i + 1), line, json.dumps(DOCS[i + 1]) if i + 1 in DOCS else "") for i, line in enumerate(LINES)]
CMD = '#detailFields dd[data-col="CommandLine"]'


def _containing(text, *, exclude=False):
    return sorted((rid for rid, line, _ in ROWS if (text in line) != exclude), key=int)


@pytest.fixture(scope="module")
def table(tmp_path_factory, server_post, server_call):
    f = tmp_path_factory.mktemp("detail_selection") / NAME
    with f.open("w", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(["Id", "CommandLine", "Doc"])
        w.writerows(ROWS)
    sid = server_post("/api/ingest/path", {"path": str(f), "name": NAME, "build_fts": False})["id"]
    try:
        yield sid
    finally:
        server_call(f"/api/source/{sid}", "DELETE")


@pytest.fixture
def opened(page, table):
    home = page.evaluate("() => __winnow.S.sourceId")
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", table)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=table)
    page.wait_for_selector('.hcell[data-col="CommandLine"]')
    page.wait_for_selector(".row")
    try:
        yield page
    finally:
        page.evaluate("() => { __winnow.closeMenu(); __winnow.hideDetailPane(); }")
        page.evaluate("(home) => __winnow.openSource(home)", home)
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=home)


def _shown(page):
    """Every Id the view holds, once its rows are in (the table is one page)."""
    page.wait_for_function("() => { const n = __winnow.S.view.row_count;"
                           " return n === 0 || !!__winnow.rowAt(n - 1); }")
    return page.evaluate("() => Array.from({ length: __winnow.S.view.row_count },"
                         " (_, i) => __winnow.rowAt(i).cells[0])")


def _open_row_holding(page, column, text):
    """Puts the cursor on the first row whose `column` holds `text` and shows
    it in the detail pane, the way an analyst does: click it, press d. The
    cursor has to be on it, since a page landing re-shows the cursor's row."""
    pos = page.evaluate("""([col, t]) => {
      const i = __winnow.S.columns.findIndex((c) => c.name === col);
      for (let p = 0; p < __winnow.S.view.row_count; p++) {
        const r = __winnow.rowAt(p);
        if (r && String(r.cells[i]).includes(t)) return p;
      }
      return -1; }""", [column, text])
    assert pos >= 0, text
    page.locator(f'.row[data-pos="{pos}"] .cell').nth(1).click()
    if page.evaluate("() => document.getElementById('detail').hidden"):
        page.keyboard.press("d")   # an open pane follows the cursor; d would close it
    page.wait_for_function("([col, t]) => { const dd = document.querySelector("
                           "`#detailFields dd[data-col=\"${col}\"]`); return !!dd && dd.textContent.includes(t); }",
                           arg=[column, text])


def _select_and_right_click(page, text):
    """Selects `text` inside the CommandLine field and right-clicks it."""
    page.evaluate("""([sel, t]) => {
      const dd = document.querySelector(sel);
      const w = document.createTreeWalker(dd, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        const i = n.data.indexOf(t);
        if (i < 0) continue;
        const r = document.createRange();
        r.setStart(n, i); r.setEnd(n, i + t.length);
        getSelection().removeAllRanges(); getSelection().addRange(r);
        const b = r.getBoundingClientRect();
        n.parentElement.dispatchEvent(new MouseEvent('contextmenu', {
          bubbles: true, cancelable: true, clientX: b.left + 2, clientY: b.top + 2, button: 2 }));
        return;
      }
      throw new Error('selection text not found: ' + t); }""", [CMD, text])
    page.wait_for_selector(".menu .menu-item")


def _click_item(page, label):
    # Exact label, not has_text: "Filter … to X" is also the start of
    # "Filter … to X only".
    page.evaluate("""(label) => {
      const hit = [...document.querySelectorAll('.menu .menu-item')]
        .find((b) => (b.querySelector('.menu-item-text') || b).textContent === label);
      if (!hit) throw new Error('no menu item ' + label);
      hit.click(); }""", label)


def _clear(page, view_rebuilt):
    if page.evaluate("() => Object.keys(__winnow.S.filters).length > 0 || __winnow.hasActiveFilterTree()"):
        with view_rebuilt(page):
            page.evaluate("() => __winnow.clearAllFilters()")


def test_a_selection_holding_a_pipe_filters_to_the_rows_containing_it(opened, view_rebuilt):
    page = opened
    sel = "whoami | findstr"
    _open_row_holding(page, "CommandLine", sel)
    _select_and_right_click(page, sel)
    try:
        with view_rebuilt(page):
            _click_item(page, f"Filter CommandLine to {sel}")
        # Rows 1 and 2 hold the text. The box read it as `whoami|findstr`
        # and showed rows 3 and 4 instead.
        assert _shown(page) == _containing(sel) == ["1", "2"]
    finally:
        _clear(page, view_rebuilt)


@pytest.mark.parametrize("verb", ["Filter", "Exclude"])
def test_selections_that_look_like_filter_syntax_are_matched_as_text(opened, view_rebuilt, verb):
    page = opened
    for sel in ["=cmd", ">5", "^HKLM", "/usr/bin/", "*", '""', "!important"]:
        _open_row_holding(page, "CommandLine", sel)
        _select_and_right_click(page, sel)
        label = f"Filter CommandLine to {sel}" if verb == "Filter" else f"Exclude {sel}"
        try:
            with view_rebuilt(page):
                _click_item(page, label)
            assert _shown(page) == _containing(sel, exclude=verb == "Exclude"), (verb, sel)
        finally:
            _clear(page, view_rebuilt)


def test_only_drops_the_other_filters_and_keeps_the_text_literal(opened, view_rebuilt):
    page = opened
    sel = "whoami | findstr"
    _open_row_holding(page, "CommandLine", sel)
    try:
        # Something for "only" to drop: a box filter on another column.
        with view_rebuilt(page):
            page.evaluate("() => { __winnow.setColumnFilter('Id', '!3'); return __winnow.rebuildView(); }")
        _select_and_right_click(page, sel)
        with view_rebuilt(page):
            _click_item(page, f"Filter CommandLine to {sel} only")
        assert _shown(page) == ["1", "2"]
        assert page.evaluate("() => ({ ...__winnow.S.filters })") == {}
    finally:
        _clear(page, view_rebuilt)


@pytest.mark.parametrize("path,value", [("$.op", "=admin"), ("$.note", " padded")])
def test_a_document_value_filters_to_the_rows_containing_it(opened, view_rebuilt, path, value):
    """`=admin` was read as an equals on `admin`, which no cell is; a value
    with a space at an edge was refused outright, because the box trims."""
    page = opened
    want = sorted((rid for rid, _, doc in ROWS if value in doc), key=int)
    assert len(want) == 1
    _open_row_holding(page, "Doc", json.dumps(value))
    page.locator(f'#detailFields dd[data-col="Doc"] .struct-node[data-path="{path}"]').first.click(button="right")
    try:
        with view_rebuilt(page):
            _click_item(page, "Filter Doc to this value")
        assert _shown(page) == want
    finally:
        _clear(page, view_rebuilt)


def test_a_second_selection_replaces_the_first_as_it_does_in_the_box(opened, view_rebuilt):
    """The box holds one filter per column, so a second selection on the
    column replaces the first; text that has to go through the filter tree
    is replaced the same way, rather than piling up as an AND."""
    page = opened
    first, second = "whoami | findstr", "| findstr admin"
    try:
        _open_row_holding(page, "CommandLine", first)
        _select_and_right_click(page, first)
        with view_rebuilt(page):
            _click_item(page, f"Filter CommandLine to {first}")
        assert _shown(page) == ["1", "2"]
        _open_row_holding(page, "CommandLine", second)
        _select_and_right_click(page, second)
        with view_rebuilt(page):
            _click_item(page, f"Filter CommandLine to {second}")
        assert _shown(page) == _containing(second) == ["1", "2", "16"]
    finally:
        _clear(page, view_rebuilt)

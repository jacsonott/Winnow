"""What the page holds about one case must not survive opening another.

Ids are numbered per case file — tags, tables, SQL tabs — so anything the
client keeps from the previous case names the wrong thing (or nothing) in
the next. These open a second case, which is why the module boots its own
server: the shared fixture's case is read by every other module, and
switching it would pull the case out from under them (the reason
test_notes_page_rows.py drives resetNotes() instead).
"""

from __future__ import annotations

import re

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui


@pytest.fixture
def own_server(tmp_path):
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("When,Host,Cmd\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i},cmd{i}\n" for i in range(30)),
                        encoding="utf-8")
    with running_server(tmp_path, "--case", tmp_path / "caseA.db", "--open", csv_path, "--no-fts",
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        cwd=tmp_path, ready="/api/sources") as srv:
        yield {"base": srv.base, "post": srv.post, "get": srv.get, "dir": tmp_path}


@pytest.fixture
def case_page(own_page, own_server):
    with own_page(base=own_server["base"], viewport=(1400, 900)) as pg:
        yield pg


def test_opening_another_case_replaces_the_tag_list(case_page, own_server):
    """S.tags was only reloaded by opening a table, which a case with no
    tables never does: case A's tags stayed in the Watchlist's auto-tag
    picker and the Timeline's chips, and an auto-tag picked from them was
    stored in case B naming a tag B does not have."""
    page = case_page
    own_server["post"]("/api/tags", {"name": "OnlyInCaseA", "color": "#00ff00", "hotkey": None})
    page.evaluate("() => __winnow.loadTags()")
    assert "OnlyInCaseA" in page.evaluate("() => __winnow.S.tags.map(t => t.name)")

    case_b = str(own_server["dir"] / "caseB.db-winnow")
    own_server["post"]("/api/cases", {"path": case_b, "name": "Empty B", "group": "", "notes": ""})
    page.evaluate("(p) => __winnow.openCase(p)", case_b)
    page.wait_for_function("() => __winnow.S.sources.length === 0")

    server_tags = [(t["id"], t["name"]) for t in own_server["get"]("/api/tags")["tags"]]
    assert page.evaluate("() => __winnow.S.tags.map(t => [t.id, t.name])") == [list(t) for t in server_tags]

    page.evaluate("() => __winnow.showWatchlistTab()")
    page.wait_for_selector("#wlAutoTag")
    options = page.evaluate("() => [...document.querySelectorAll('#wlAutoTag option')].map(o => o.textContent)")
    assert "OnlyInCaseA" not in options
    assert options[1:] == [name for _, name in server_tags]


def test_a_query_still_running_does_not_land_in_the_next_case(case_page, own_server):
    """A SQL run started in case A and answered after case B opened was
    painted under B's "Query 1" — both cases number their tabs from 1 —
    and tagging one of its rows wrote a tag into case B for a row of
    case A's table."""
    import json
    import re

    page = case_page
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    held = []
    pattern = re.compile(r".*/api/sql/run$")
    page.route(pattern, lambda route: held.append(route) if not held else route.continue_())
    try:
        page.locator("#sqlText").fill("SELECT 'from case A' AS stale_col")
        page.click("#btnRunSql")
        page.wait_for_function("() => document.getElementById('sqlResult').textContent.startsWith('Running')")
        assert held, "the run was not intercepted"

        case_b = str(own_server["dir"] / "caseB.db-winnow")
        own_server["post"]("/api/cases", {"path": case_b, "name": "B", "group": "", "notes": ""})
        page.evaluate("(p) => __winnow.openCase(p)", case_b)
        page.wait_for_function("() => __winnow.S.sources.length === 0")
        page.click("#tabSql")
        page.wait_for_selector("#sqlview:not([hidden])")
        page.wait_for_function("() => __winnow.S.sqlTabs.length > 0")

        try:
            held[0].fulfill(status=200, content_type="application/json", body=json.dumps(
                {"id": "stale", "columns": ["stale_col"], "rows": [["from case A"]], "total": 1,
                 "widths": [11], "stopped": None, "elapsed_ms": 1}))
        except Exception:
            pass   # the page already let go of it
        page.wait_for_timeout(500)   # long enough for the stale answer to land if it is going to
        assert "stale_col" not in page.locator("#sqlResult").inner_text()
        assert page.evaluate("() => [...__winnow.S.sqlResults.keys()]") == []
        assert page.evaluate("() => __winnow.busyCount") == 0
    finally:
        page.unroute(pattern)


def _strip(page):
    """What the histogram strip says: its count line, and the one-line
    empty state when that is what it is showing instead of a chart."""
    return page.evaluate("""() => {
      const info = document.querySelector('#histogramPanel .th-info');
      const empty = document.querySelector('#histogramPanel .th-empty');
      return { info: info.textContent, empty: empty.hidden ? null : empty.textContent };
    }""")


def _open_strip(page, rows):
    page.evaluate("() => __winnow.toggleHistogram(true)")
    page.wait_for_function(
        "(n) => document.querySelector('#histogramPanel .th-info').textContent.startsWith(n + ' rows')",
        arg=str(rows), timeout=15_000)


def test_a_case_with_no_table_does_not_keep_the_last_cases_histogram(case_page, own_server):
    """With the strip open, opening another case left the previous case's
    chart up — its bars and its row count — until something rebuilt the
    view. The strip only redrew on a view change, and a case that opens on
    no table (a new one, or one with every tab closed) never builds one."""
    page = case_page
    _open_strip(page, 30)

    case_b = str(own_server["dir"] / "caseB.db-winnow")
    own_server["post"]("/api/cases", {"path": case_b, "name": "Empty B", "group": "", "notes": ""})
    page.evaluate("(p) => __winnow.openCase(p)", case_b)
    page.wait_for_function("() => __winnow.S.sources.length === 0")

    page.wait_for_selector("#histogramPanel .th-empty", state="visible", timeout=10_000)
    assert _strip(page) == {"info": "", "empty": "Open a table to chart when its rows happened."}
    assert page.locator("#histogramPanel canvas.th-canvas").is_hidden()
    assert page.evaluate("() => __winnow.histogramData()") is None


def test_the_last_cases_histogram_is_gone_before_the_new_cases_view_lands(case_page, own_server, until):
    """The same report on a case that has tables: the old chart stayed up
    for as long as the new case's first view took to build, which on a
    large case is long enough to read as stuck. The build is held here so
    that window lasts as long as the test needs."""
    page = case_page
    case_a = str(own_server["dir"] / "caseA.db")
    case_b = str(own_server["dir"] / "caseB.db-winnow")
    own_server["post"]("/api/cases", {"path": case_b, "name": "B", "group": "", "notes": ""})
    page.evaluate("(p) => __winnow.openCase(p)", case_b)
    page.wait_for_function("() => __winnow.S.sources.length === 0")
    csv_b = own_server["dir"] / "b.csv"
    csv_b.write_text("When,Host\n" + "".join(f"2024-06-0{d} 12:00:00,B{d}\n" for d in range(1, 8)),
                     encoding="utf-8")
    own_server["post"]("/api/ingest/path", {"path": str(csv_b), "build_fts": False})
    page.evaluate("(p) => __winnow.openCase(p)", case_a)
    page.wait_for_function("() => __winnow.S.sources.length === 1 && __winnow.S.sources[0].row_count === 30")
    _open_strip(page, 30)

    held = []
    pattern = re.compile(r".*/api/view$")
    page.route(pattern, lambda route: held.append(route) if not held else route.continue_())
    try:
        # Not awaited: openCase is waiting on the build held below.
        page.evaluate("(p) => { __winnow.openCase(p); }", case_b)
        until(page, lambda: held, "case B's first view build held")
        assert _strip(page) == {"info": "", "empty": "Loading…"}
        assert page.evaluate("() => __winnow.histogramData()") is None

        held[0].continue_()
        page.wait_for_function(
            "() => document.querySelector('#histogramPanel .th-info').textContent.startsWith('7 rows')",
            timeout=15_000)
        assert page.evaluate("() => __winnow.histogramData().total") == 7
    finally:
        page.unroute(pattern)


def test_the_last_cases_filters_do_not_open_on_the_next_cases_table(case_page, own_server, view_rebuilt):
    """A header filter and a search set on case A's table came back on
    case B's when the two tables shared an id, which the first table of
    any two cases does: B opened filtered by a column value and a search
    nobody typed there, on rows that never matched them, and read as an
    empty table. The case switch cleared the per-table stash, then the
    first table it opened stashed what was still on screen under that
    shared id and read it straight back."""
    page = case_page
    case_a = str(own_server["dir"] / "caseA.db")
    case_b = str(own_server["dir"] / "caseB.db-winnow")
    own_server["post"]("/api/cases", {"path": case_b, "name": "B", "group": "", "notes": ""})
    page.evaluate("(p) => __winnow.openCase(p)", case_b)
    page.wait_for_function("() => __winnow.S.sources.length === 0")
    csv_b = own_server["dir"] / "b.csv"
    csv_b.write_text("When,Host\n" + "".join(f"2024-06-0{d} 12:00:00,B{d}\n" for d in range(1, 8)),
                     encoding="utf-8")
    own_server["post"]("/api/ingest/path", {"path": str(csv_b), "build_fts": False})
    page.evaluate("(p) => __winnow.openCase(p)", case_a)
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 30 && __winnow.busyCount === 0")
    assert page.evaluate("() => __winnow.S.sourceId") == 1

    with view_rebuilt(page):
        page.locator('.fcell input[data-col="Host"]').fill("H1")
    page.click("#btnSearchToggle")
    with view_rebuilt(page):
        page.locator("#search").fill("cmd1")
    assert page.evaluate("() => __winnow.S.view.row_count") == 11

    page.evaluate("(p) => __winnow.openCase(p)", case_b)
    page.wait_for_function("() => __winnow.S.sources.length === 1 && __winnow.S.sources[0].row_count === 7"
                           " && __winnow.S.view && __winnow.busyCount === 0", timeout=15_000)
    assert page.evaluate("""() => ({ source: __winnow.S.sourceId, rows: __winnow.S.view.row_count,
                                     filters: __winnow.S.filters, search: __winnow.S.search })""") == \
        {"source": 1, "rows": 7, "filters": {}, "search": ""}
    assert page.locator('.fcell input[data-col="Host"]').input_value() == ""
    assert page.locator("#search").input_value() == ""

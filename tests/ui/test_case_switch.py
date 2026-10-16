"""What the page holds about one case must not survive opening another.

Ids are numbered per case file — tags, tables, SQL tabs — so anything the
client keeps from the previous case names the wrong thing (or nothing) in
the next. These open a second case, which is why the module boots its own
server: the shared fixture's case is read by every other module, and
switching it would pull the case out from under them (the reason
test_notes_page_rows.py drives resetNotes() instead).
"""

from __future__ import annotations

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui


@pytest.fixture
def own_server(tmp_path):
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("Host,Cmd\n" + "".join(f"H{i},cmd{i}\n" for i in range(30)), encoding="utf-8")
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

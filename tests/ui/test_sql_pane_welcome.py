"""The SQL pane as it looks before anything has been run in it.

Three things were wrong at once and all three are pinned here, in the
terms they were wrong in. The seeded query was `SELECT * FROM src_1 LIMIT
50;` — `src_1` is a name that appears nowhere else in the product, while
the tab strip, the sidebar and the dashboards all call that table
`ui.csv`. Above it sat two lines of uppercase, letter-spaced prose
explaining `src_n` against `main.src_n`. Below it, four fifths of the pane
was blank, with nothing saying that Ctrl+Enter is what runs a query.

The starters are the part most likely to be quietly re-broken: it is very
easy to replace a generated query with a canned example, which reads fine
in a screenshot and errors on the first click in a real case. So they are
asserted against the tables the case actually holds, and against a merge,
which has no `src_N` of its own at all (invariant #9).
"""

from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    # The tab list (and the starter query it seeds into the editor) loads
    # async — reading the editor before that lands reads the wrong text.
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


def test_the_seeded_query_names_the_table_the_analyst_sees(page, api):
    # The case file is shared by the whole UI session and earlier modules
    # type into this editor, so the seeded text can only be observed on a
    # pane with no saved tabs — which is exactly the state an analyst's
    # first visit to a new case is in. Nothing is restored afterwards: one
    # freshly seeded "Query 1" is a cleaner state than whatever was there.
    for t in api("/api/sql_tabs"):
        api(f"/api/sql_tabs/{t['id']}", "DELETE")
    page.evaluate("() => __winnow.showSqlTab()")
    _open_sql(page)

    text = page.locator("#sqlText").input_value()
    first, _, rest = text.partition("\n")
    assert first == "-- ui.csv (src_1)", f"line 1 must name the file, got {first!r}"
    assert rest.strip() == "SELECT * FROM src_1 LIMIT 50;"


def test_the_header_reads_as_a_sentence_not_a_shouted_label(page):
    _open_sql(page)
    head = page.locator(".sql-head")
    # Asserted as computed style rather than as "it fits on one line". The
    # second line was a consequence of uppercase (~12% wider) plus .1em
    # tracking, and a geometry assertion at one fixed viewport would sit
    # close enough to the wrap point to flake — which CLAUDE.md rates worse
    # than no check at all. These two properties are what was wrong.
    style = page.evaluate(
        "() => { const cs = getComputedStyle(document.querySelector('.sql-head'));"
        " return [cs.textTransform, cs.letterSpacing]; }")
    assert style[0] == "none", style
    assert style[1] in ("normal", "0px"), style

    # The three names an analyst has to tell apart are marked as names.
    codes = head.locator("code")
    assert [codes.nth(i).inner_text() for i in range(codes.count())] == \
        ["src_N", "merge_N", "main.src_N"]


def test_the_results_area_says_how_to_run_a_query(page):
    _open_sql(page)
    empty = page.locator("#sqlResult .sql-empty")
    empty.wait_for()
    text = empty.inner_text()
    assert "Nothing run yet" in text
    # The keystroke, spelled the same way the Run button spells it.
    assert "runs the query" in text
    assert "Ctrl+⏎" in text or "⌘⏎" in text, text
    assert page.locator("#sqlResult .sql-starter").count() >= 2


def test_every_offered_starter_names_a_table_this_case_has(page):
    _open_sql(page)
    starters = page.evaluate("() => __winnow.sqlStarters()")
    tables = set(page.evaluate(
        "() => __winnow.S.sources.filter((s) => !s.error).map((s) => __winnow.paneTable(s))"))
    assert starters
    for st in starters:
        refs = set(re.findall(r"\b(?:src|merge)_\d+\b", st["sql"]))
        assert refs, f"{st['label']} references no table at all"
        assert refs <= tables, f"{st['label']} names a table this case does not have: {refs - tables}"
        assert "src_-" not in st["sql"]


def test_clicking_a_starter_runs_it(page):
    _open_sql(page)
    # An empty editor is "disposable", so the starter fills this tab rather
    # than opening one of its own — see the draft test below for the other
    # half of that rule.
    page.locator("#sqlText").fill("")
    tabs = page.evaluate("() => __winnow.S.sqlTabs.length")
    page.locator("#sqlResult .sql-starter").first.click()
    page.wait_for_selector("#sqlResult table")
    assert page.locator("#sqlResult .sql-error").count() == 0
    assert page.locator("#sqlResult .sql-empty").count() == 0
    assert page.evaluate("() => __winnow.S.sqlTabs.length") == tabs


def test_a_starter_does_not_overwrite_a_query_being_written(page, api):
    _open_sql(page)
    draft = "SELECT EventId FROM src_1 WHERE Host = 'H3' -- half written"
    page.locator("#sqlText").fill(draft)
    before = page.evaluate("() => __winnow.S.sqlTabs.map((t) => t.id)")
    drafted = page.evaluate("() => __winnow.S.sqlTabId")
    try:
        page.locator("#sqlResult .sql-starter").first.click()
        page.wait_for_selector("#sqlResult table")
        after = page.evaluate("() => __winnow.S.sqlTabs.map((t) => t.id)")
        assert len(after) == len(before) + 1, "the starter must open a tab of its own"
        # The draft is still there, in the tab it was typed into.
        assert draft in page.evaluate(
            f"() => (__winnow.S.sqlTabs.find((t) => t.id === {drafted}) || {{}}).sql")
    finally:
        for tid in page.evaluate("() => __winnow.S.sqlTabs.map((t) => t.id)"):
            if tid not in before:
                api(f"/api/sql_tabs/{tid}", "DELETE")
        page.evaluate("() => __winnow.loadSqlTabs()")


def test_starters_are_built_from_the_case_not_from_a_canned_example(page):
    """Two real tables, one of them tagged: the three queries an analyst is
    offered are all written out of what this case holds."""
    _open_sql(page)
    starters = page.evaluate("""() => {
      const S = __winnow.S;
      const keepSources = S.sources, keepId = S.sourceId;
      try {
        S.sources = [
          { id: 1, name: 'Security.csv', row_count: 6000, tagged_row_count: 4,
            columns: [{ name: 'TimeCreated', type: 'datetime' }, { name: 'EventId', type: 'number' }] },
          { id: 2, name: "O'Brien.csv", row_count: 1800, tagged_row_count: 0,
            columns: [{ name: 'EventId', type: 'number' }] },
        ];
        S.sourceId = 1;
        return __winnow.sqlStarters();
      } finally { S.sources = keepSources; S.sourceId = keepId; }
    }""")
    assert [s["label"] for s in starters] == [
        "First 50 rows of Security.csv",
        "Rows you tagged in Security.csv",
        "Rows per table",
    ]
    assert starters[0]["sql"].startswith("-- Security.csv (src_1)\n")
    # Driven off row_tags by rid, not a scan of the Tags column.
    assert "rid IN (SELECT rid FROM row_tags WHERE source_id = 1)" in starters[1]["sql"]
    # A file name is embedded as DATA, and quoted as SQLite quotes data.
    assert "'O''Brien.csv'" in starters[2]["sql"]
    assert "src_2" in starters[2]["sql"]


def test_a_merge_gets_starters_it_can_actually_run(page):
    """A merge has no src_N of its own — only the merge_<id> view the pane
    connection builds — and its rids are unique per member, so the tagged
    rows have to be found by (source_id, rid). Invariant #9."""
    _open_sql(page)
    starters = page.evaluate("""() => {
      const S = __winnow.S;
      const keepSources = S.sources, keepId = S.sourceId;
      try {
        S.sources = [{ id: -3, name: 'All event logs', is_merge: true, row_count: 7800,
                       tagged_row_count: 9, columns: [{ name: 'EventId', type: 'number' }] }];
        S.sourceId = -3;
        return __winnow.sqlStarters();
      } finally { S.sources = keepSources; S.sourceId = keepId; }
    }""")
    joined = "\n".join(s["sql"] for s in starters)
    assert "merge_3" in joined
    assert "src_-3" not in joined, "a merge has no src_N; that is a syntax error, not a wrong answer"
    assert "(source_id, rid) IN (SELECT source_id, rid FROM row_tags)" in joined
    # Nothing to count per table when the only source is a merge: counting
    # a merge alongside its members would report the case as larger.
    assert "Rows per table" not in [s["label"] for s in starters]


def _ac_labels(page):
    """Every autocomplete item's LABEL, read in ONE call. An empty list is
    a real answer — it means nothing matched, which is what the second
    probe below is asking about, so this never waits for an item.

    One call because `count()` and then `nth(i)` is two round trips over a
    list the editor rebuilds on each keystroke: on a loaded runner an item
    counted in the first call is gone by the second, and nth(i) then waits
    its full timeout for a node that no longer exists. That is what failed
    on CI.

    The first span is the label and the second is the kind (sqlassist.js
    builds them that way), so the label span is addressed directly —
    textContent over the whole item runs the two together with no
    separator to split on.
    """
    return [t.strip() for t in
            page.locator(".sql-ac .menu-item span:first-child").all_text_contents()]


def test_a_left_behind_table_comment_is_not_a_second_table(page, server_post, api, tmp_path):
    """The comment on line one is there to survive the query being edited
    into something real — which means the everyday state of the editor is
    a comment naming one table above a FROM naming another. Scanned for
    `src_N` with the comment still in it, that query looked like it read
    two tables, so the result resolved to no row at all: no live Tags
    column, no row selection, no tag hotkeys, no Ctrl+C on a selection and
    no double-click into the table, with nothing on screen saying why. The
    autocomplete widened the same way, offering the commented table's
    columns beside the real one's.
    """
    csv = tmp_path / "gadgets.csv"
    csv.write_text("When,Gadget\n2026-04-01 00:00:00,widget\n2026-04-01 00:00:01,sprocket\n")
    server_post("/api/ingest/jobs/path", {"path": str(csv)})
    # Polled from Python, one awaited call at a time (the same reason
    # test_merge_flow.py polls this way): wait_for_function does not await
    # a promise, so that form both passed instantly AND left a loadSources
    # per poll in flight — and a no-argument loadSources navigates, so
    # whichever landed after the SQL page opened pulled the app back to
    # the grid under it.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        names = page.evaluate(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if "gadgets.csv" in names:
            break
        time.sleep(0.25)
    else:
        pytest.fail("gadgets.csv never appeared in S.sources")
    sid = page.evaluate("() => (__winnow.S.sources.find((s) => s.name === 'gadgets.csv') || {}).id")
    try:
        _open_sql(page)
        ta = page.locator("#sqlText")
        # Exactly what the analyst is left with after editing the seeded
        # query's FROM clause and keeping the line that named the file.
        ta.fill(f"-- ui.csv (src_1)\nSELECT rid, Gadget FROM src_{sid} LIMIT 5;")
        page.click("#btnRunSql")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#sqlResult th')]"
            ".some((h) => h.textContent === 'Gadget')")

        heads = page.locator("#sqlResult th")
        assert [heads.nth(i).inner_text() for i in range(heads.count())] == ["rid", "Gadget", "Tags"]
        assert "tags joined via rid" in page.locator("#sqlResult").inner_text()
        # And the rows are rows again: clicking one selects it, which is
        # what the tag hotkeys and Ctrl+C act on.
        page.locator("#sqlResult tr").nth(1).click()
        assert page.locator("#sqlResult .sql-row-sel").count() == 1

        # The columns offered are the queried table's, not the comment's.
        ta.click()
        ta.fill(f"-- ui.csv (src_1)\nSELECT rid, Gadget FROM src_{sid} WHERE ")
        ta.type("Ga")
        page.wait_for_selector(".sql-ac .menu-item")
        # Offered, not necessarily on top: the file is called gadgets.csv,
        # so the TABLE matches "Ga" as well, and which of the two a ranking
        # puts first is not what this test is about. Each item renders its
        # label on one line and its kind on the next.
        labels = _ac_labels(page)
        assert "Gadget" in labels, labels

        ta.fill(f"-- ui.csv (src_1)\nSELECT rid, Gadget FROM src_{sid} WHERE ")
        ta.type("Ex")  # ExtremelyLongColumnHeaderName belongs to ui.csv alone
        # Wait for the editor to have answered THIS word rather than
        # reading the previous one's list: "Ex" matches nothing in the
        # queried table, so what says the rebuild happened is Gadget
        # leaving. Reading straight after typing asserts against whatever
        # "Ga" left on screen, which would pass no matter what.
        page.wait_for_function(
            "() => { const n = [...document.querySelectorAll('.sql-ac .menu-item')];"
            " return !n.length || !n.some((x) => x.textContent.includes('Gadget')); }")
        labels = _ac_labels(page)
        assert not any("ExtremelyLong" in t for t in labels), labels
        page.keyboard.press("Escape")
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")

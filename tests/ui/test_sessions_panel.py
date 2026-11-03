"""The Sessions panel: save, fresh pass, and the QC diff.

The diff is the reason this panel exists, so the test that matters is the
one that tags rows, saves, changes the conclusions, and reads what the
comparison actually renders — not just that a table appeared."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open(page, section="saved"):
    page.evaluate("(s) => __winnow.openSessionManager(s)", section)
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".btn", has_text="Save current work").wait_for(state="visible")


def _open_compare(page):
    """The Compare section: the two sides are fields on top, and changing
    either compares — there is no Compare button."""
    _open(page, "compare")
    page.wait_for_function("""() => [...document.querySelectorAll('.sessions-compare-top select')]
      .every((s) => s.options.length > 0)""")


def _sides(page, left, right):
    """Sets both sides and waits for the comparison of exactly those two."""
    sels = page.locator(".sessions-compare-top select")
    sels.first.select_option(left)
    sels.nth(1).select_option(right)
    page.wait_for_function("""() => document.querySelector('.sessions-diff-row, .sessions-diff-box .tables-empty')
      && !/Comparing/.test(document.querySelector('.sessions-diff-box').textContent)""")


def _tag_rows(page, rows, hotkey="1"):
    for i in rows:
        page.locator("#body .row").nth(i).click()
        page.keyboard.press(hotkey)
        page.wait_for_timeout(60)


def _cleanup(page):
    """Sessions AND tags. The UI suite shares one case file, so a test that
    leaves rows tagged decides the next test's starting state — these
    passed individually and failed together until this cleared both."""
    page.evaluate("""() => fetch('/api/case_sessions/new', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ save_as: null }) })""")
    page.evaluate("""() => fetch('/api/case_sessions', { headers: { 'X-Timeline-Lite-Client': '1' } })
      .then((r) => r.json())
      .then((d) => Promise.all(d.sessions.map((s) =>
        fetch('/api/case_sessions/' + encodeURIComponent(s.name),
              { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } }))))""")
    page.evaluate("() => __winnow.loadSources()")


def test_saving_a_session_lists_it_with_its_tag_count(page):
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _open(page)
        page.locator(".btn", has_text="Save current work").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("first pass")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        page.wait_for_selector(".sessions-row [data-role=name]")

        row = page.locator(".sessions-row", has_text="first pass")
        assert row.count() == 1
        assert row.locator(".tables-tagged").inner_text() == "2"
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_row_with_two_tags_is_one_tagged_row(page):
    """"N tagged" sits beside the notes and table counts and reads as rows;
    it summed tag assignments, so one row with two tags said "2 tagged"."""
    _cleanup(page)
    _tag_rows(page, [0])
    _tag_rows(page, [0], "2")
    try:
        _save(page, "two tags")
        _open(page)
        tagged = page.locator(".sessions-row", has_text="two tags").locator(".tables-tagged")
        tagged.wait_for()
        assert tagged.inner_text() == "1", tagged.inner_text()
        # and what a session covers: every table, not only the ones open in a tab
        foot = page.locator(".sessions-box .side-preview-foot").inner_text()
        assert "every open table" not in foot and "open or not" in foot, foot
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_compare_starts_on_the_newest_session_against_current_work(page):
    """Both dropdowns started on Current work: a comparison of the case with
    itself, and Compare only said "Pick two different sessions". With a
    session saved, the useful default is the newest one against the live
    work — what changed since the last save."""
    _cleanup(page)
    try:
        _save(page, "older")
        _save(page, "newer")
        _open(page)
        page.locator(".side-rail-item[data-section=compare]").click()
        page.wait_for_function("""() => [...document.querySelectorAll('.sessions-compare-top select')]
          .every((s) => s.options.length === 3)""")
        sels = page.locator(".sessions-compare-top select")
        assert (sels.first.input_value(), sels.nth(1).input_value()) == ("newer", "__live__")
        # compared as it opens, with nothing to press
        page.wait_for_selector(".sessions-diff-box >> text=No differences")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_comparison_over_different_tables_counts_them_too(page):
    """The note above the counts said they "cover what they share" — but a
    table only one side has is counted, every tagged row of it a
    difference, on the line right below the note."""
    _cleanup(page)
    foreign = {"format": "winnow-case-session/1", "sources": [{
        "source": {"name": "elsewhere.csv", "file_hash": "nope", "columns": []},
        "tag_defs": [{"id": 1, "name": "TA"}], "row_tags": [{"rid": 5, "tag_id": 1}], "row_notes": []}]}
    try:
        status = page.evaluate("""async (doc) => (await fetch('/api/case_sessions/adopt', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'theirs', session: doc }) })).status""", foreign)
        assert status == 200, status
        _compare(page, "theirs", "__live__")
        note = page.locator(".sessions-pane .mb-note.warn").first.inner_text()
        assert "1 only in theirs" in note and "what they share" not in note, note
        assert "counts as a difference" in note, note
        row = page.locator(".sessions-diff-row", has_text="elsewhere.csv")
        assert row.locator(".diff-n-removed").inner_text() == "1"
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def _save(page, name):
    # evaluate awaits the returned promise: the save has landed when this returns
    status = page.evaluate("""async (name) => (await fetch('/api/case_sessions', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }) })).status""", name)
    assert status == 200, status


def _compare(page, left, right):
    _open_compare(page)
    _sides(page, left, right)


def test_the_diff_is_counts_per_table_and_never_lists_rows(page):
    """The QC question, end to end: the panel says how many rows each side
    has that the other does not; the rows themselves are the grid's job."""
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _save(page, "analyst")
        _tag_rows(page, [1])          # reviewer removes row 1 (toggles it off)
        _tag_rows(page, [2])          # and adds row 2
        _compare(page, "analyst", "__live__")
        # the figures spell the two sides out; the column heads say A and B,
        # as the marks on the rows will
        stats = page.locator(".sessions-pane .mb-stats").inner_text().lower()
        assert "only in analyst" in stats and "only in current work" in stats, stats
        heads = page.locator(".sessions-diff-head").inner_text().lower()
        assert "a only" in heads and "b only" in heads, heads
        row = page.locator(".sessions-diff-row").first
        assert row.locator("button.diff-n-removed").inner_text() == "1"
        assert row.locator("button.diff-n-added").inner_text() == "1"
        assert row.locator(".diff-n-changed").inner_text() == "—"
        # counts, a legend, and nothing else — no row detail in the panel
        assert page.locator("#modalBody .diff-table, #modalBody .diff-cell").count() == 0
        legend = page.locator(".diff-legend").inner_text().lower()
        assert "analyst only" in legend and "current work only" in legend, legend
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_count_pivots_to_the_table_with_the_rows_marked(page):
    """From a number to the evidence: the grid shows exactly those rows —
    not those rows intersected with whatever search the table had — each
    marked with which session tagged it, under a banner that names the
    two sides; Done drops the marks and lands on the filter the table had."""
    _cleanup(page)
    _tag_rows(page, [0, 1, 2])
    try:
        _save(page, "analyst")
        _tag_rows(page, [1, 2])           # the reviewer drops two of the three
        _tag_rows(page, [5], "2")         # and adds one, with a different tag
        rids = page.evaluate("() => [1, 2].map((p) => __winnow.rowAt(p).rid)")
        # A search that matches none of them, and a filter the table had:
        # the pivot must not AND itself under either, and Done must bring
        # the filter back.
        prev = {"type": "group", "op": "AND", "children": [
            {"type": "cond", "column": "Host", "op": "equals", "value": "H4"}]}
        page.evaluate("(t) => { __winnow.S.filterTree = t; __winnow.S.search = 'zzz-nothing'; }", prev)
        page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 0")
        _compare(page, "analyst", "__live__")
        page.locator(".sessions-diff-row").first.locator("button.diff-n-removed").click()
        page.wait_for_selector("#modal[hidden]", state="attached")
        page.wait_for_function("(n) => __winnow.S.view && __winnow.S.view.row_count === n", arg=2)
        assert page.evaluate("() => [__winnow.S.filterTree.type, __winnow.S.filterTree.column, __winnow.S.search]") == ["cond", "rid", ""]
        assert sorted(page.evaluate("() => [0, 1].map((p) => __winnow.rowAt(p).rid)")) == sorted(rids)
        # every shown row wears the mark for "analyst only" (A), with the detail on hover
        marks = page.locator("#body .diff-mark")
        assert marks.count() == 2 and set(marks.all_inner_texts()) == {"A"}
        assert "analyst" in marks.first.get_attribute("title") and "TA" in marks.first.get_attribute("title")
        banner = page.locator("#diffBanner")
        assert banner.is_visible() and "analyst" in banner.inner_text() and "Current work" in banner.inner_text()
        assert "showing only in analyst (2 rows)" in banner.inner_text().lower()
        # a rebuild (a sort here) keeps the banner and its caption
        vid = page.evaluate("() => __winnow.S.view.view_id")
        page.evaluate("() => { __winnow.S.sort = [{ column: 'Host', dir: 'desc' }]; __winnow.rebuildView({ keepScroll: false }); }")
        page.wait_for_function("(v) => __winnow.S.view && __winnow.S.view.view_id !== v", arg=vid)
        assert "showing only in analyst (2 rows)" in banner.inner_text().lower()
        # the banner is grid chrome: gone on the SQL tab, back on the grid
        page.locator("#tabSql").click()
        page.wait_for_selector("#sqlview:not([hidden])")
        assert page.locator("#diffBanner").is_hidden()
        page.evaluate("() => __winnow.showGridTab()")
        page.wait_for_selector("#grid:not([hidden])")
        assert banner.is_visible()
        # all differences: the added row joins, wearing B
        banner.locator(".btn", has_text="All differences").click()
        page.wait_for_function("(n) => __winnow.S.view && __winnow.S.view.row_count === n", arg=3)
        page.wait_for_function("() => document.querySelectorAll('#body .diff-mark').length === 3")
        assert sorted(page.locator("#body .diff-mark").all_inner_texts()) == ["A", "A", "B"]
        # Done: no marks, the table's own filter back (the H4 rows), the search gone
        banner.locator(".btn", has_text="Done").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        assert page.evaluate("() => __winnow.S.filterTree") == prev
        assert page.locator("#diffBanner").is_hidden() and page.locator("#body .diff-mark").count() == 0
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
    finally:
        page.evaluate("() => { __winnow.S.diffMarks = null; }")
        _cleanup(page)
        page.keyboard.press("Escape")


def test_clear_filters_drops_the_marks_with_the_pivot(page):
    """Any way out of the filter is a way out of the comparison: Clear
    filters must not leave A/B pills over the whole table."""
    _cleanup(page)
    _tag_rows(page, [3])
    try:
        _save(page, "analyst")
        _tag_rows(page, [3])              # reviewer drops it: one row only in analyst
        _compare(page, "analyst", "__live__")
        page.locator(".sessions-diff-row").first.locator("button.diff-n-removed").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 1")
        assert page.locator("#body .diff-mark").count() == 1
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        assert page.evaluate("() => __winnow.S.diffMarks") is None
        assert page.locator("#diffBanner").is_hidden() and page.locator("#body .diff-mark").count() == 0
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_identical_sessions_say_so_rather_than_showing_an_empty_table(page):
    _cleanup(page)
    _tag_rows(page, [0])
    try:
        _save(page, "same")
        _compare(page, "same", "__live__")
        page.wait_for_selector(".sessions-diff-box >> text=No differences")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_two_tags_of_one_name_are_not_reported_as_agreement(page):
    """Two sessions that put a row on different tags called "New tag" —
    Add tag's name for every new tag until it got free ones — were "No
    differences — the two agree on every row": tags are compared by name.
    From another case nothing can tell those tags apart, so the panel says
    that instead of vouching for rows nobody compared."""
    _cleanup(page)

    def doc(on_row_1):
        return {"format": "winnow-case-session/1", "sources": [{
            "source": {"name": "elsewhere.csv", "file_hash": "nope", "columns": []},
            "tag_defs": [{"id": 1, "name": "New tag"}, {"id": 2, "name": "New tag"}],
            "row_tags": [{"rid": 1, "tag_id": on_row_1}, {"rid": 2, "tag_id": 3 - on_row_1}],
            "row_notes": []}]}
    try:
        for name, first in (("theirs", 1), ("swapped", 2)):
            status = page.evaluate("""async ([name, doc]) => (await fetch('/api/case_sessions/adopt', {
              method: 'POST', headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, session: doc }) })).status""", [name, doc(first)])
            assert status == 200, status
        _compare(page, "theirs", "swapped")
        page.wait_for_selector(".sessions-diff-box >> text=No differences")
        said = page.locator(".sessions-pane").inner_text()
        assert "agree on every row" not in said, said
        assert "“New tag” is the name of more than one tag" in said, said
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_fresh_pass_saves_then_clears(page):
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _open(page)
        page.locator(".btn", has_text="Start a fresh pass").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("pass one")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        page.wait_for_selector("#modal", state="hidden", timeout=15000)

        # Cleared in the case...
        counts = page.evaluate("""() => fetch('/api/tags?source_id=' + __winnow.S.sourceId,
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert sum(counts.get("counts", {}).values()) == 0
        # ...and recoverable.
        saved = page.evaluate("""() => fetch('/api/case_sessions',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert [s["name"] for s in saved["sessions"]] == ["pass one"]
        assert saved["sessions"][0]["tagged_rows"] == 2
    finally:
        _cleanup(page)
        page.evaluate("() => __winnow.loadSources()")


def test_a_fresh_pass_suggests_a_name_no_session_has(page):
    """The suggestion was "pass N+1" for N sessions. With pass 1 and pass 3
    left after a delete it offered "pass 3" — and OK saved this pass over
    that one, then cleared the case, so what pass 3 held was gone."""
    _cleanup(page)
    try:
        _save(page, "pass 1")
        _save(page, "pass 3")
        _open(page)
        page.locator(".btn", has_text="Start a fresh pass").click()
        page.wait_for_selector(".confirm-overlay input")
        suggested = page.locator(".confirm-overlay input").input_value()
        assert suggested not in ("pass 1", "pass 3"), suggested
        page.locator(".confirm-card .btn", has_text="Cancel").click()
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_fresh_pass_over_a_taken_name_asks_first(page):
    """Typing a name that is taken would replace that session (saving over
    a name is how save_session works) and then clear the case — two
    irreversible steps on one OK. It asks, and Cancel leaves both alone."""
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _save(page, "triage")
        before = page.evaluate("""() => fetch('/api/case_sessions',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")["sessions"][0]
        _tag_rows(page, [2])
        _open(page)
        page.locator(".btn", has_text="Start a fresh pass").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("triage")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        ask = page.locator(".confirm-card", has_text="already exists")
        ask.wait_for(state="visible", timeout=5000)
        ask.locator(".btn", has_text="Cancel").click()
        page.wait_for_timeout(500)   # long enough for a save or a clear to have gone through

        after = page.evaluate("""() => fetch('/api/case_sessions',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")["sessions"]
        assert [s["name"] for s in after] == ["triage"]
        assert after[0]["tagged_rows"] == before["tagged_rows"] == 2
        counts = page.evaluate("""() => fetch('/api/tags?source_id=' + __winnow.S.sourceId,
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert sum(counts.get("counts", {}).values()) == 3, "nothing was cleared"
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def _toast(page, text):
    page.wait_for_function(
        "(t) => { const n = document.getElementById('toast'); return !n.hidden && n.textContent.includes(t); }",
        arg=text, timeout=5000)


def test_a_session_that_will_not_load_says_why(page):
    """Load of a session another window deleted meanwhile is a 404. The
    handler had try/finally and no catch: an unhandled rejection, no toast,
    and a dialog that looked as if Load had done nothing."""
    _cleanup(page)
    try:
        _save(page, "gone")
        _open(page)
        row = page.locator("#modalBody .sessions-row", has_text="gone")
        row.wait_for()
        page.evaluate("""() => fetch('/api/case_sessions/gone',
          { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""")
        row.locator("[data-role=more]").click()
        page.locator(".menu .menu-item", has_text="Load…").click()
        page.locator(".confirm-card .btn", has_text="Load").click()
        _toast(page, 'Could not load "gone"')
        assert page.evaluate("() => document.getElementById('busyBar').hidden")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_fresh_pass_or_a_delete_the_server_refuses_says_why(page):
    _cleanup(page)
    try:
        _save(page, "kept")
        page.route("**/api/case_sessions/new", lambda r: r.fulfill(
            status=500, content_type="application/json", body='{"detail": "disk full"}'))
        page.route("**/api/case_sessions/kept", lambda r: r.fulfill(
            status=500, content_type="application/json", body='{"detail": "database is locked"}')
            if r.request.method == "DELETE" else r.continue_())
        _open(page)
        page.locator(".btn", has_text="Start a fresh pass").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("pass x")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        _toast(page, "disk full")
        assert page.evaluate("() => !document.getElementById('modal').hidden"), \
            "the dialog stays, so the analyst can try again"

        row = page.locator("#modalBody .sessions-row", has_text="kept")
        row.locator("[data-role=more]").click()
        page.locator(".menu .menu-item", has_text="Delete…").click()
        page.locator(".confirm-card .btn", has_text="Delete").click()
        _toast(page, "database is locked")
    finally:
        page.unroute("**/api/case_sessions/new")
        page.unroute("**/api/case_sessions/kept")
        _cleanup(page)
        page.keyboard.press("Escape")


def _session_menu(page, name, item):
    page.locator(".sessions-row", has_text=name).locator("[data-role=more]").click()
    page.locator(".menu .menu-item", has_text=item).click()


def _answer_prompt(page, text):
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill(text)
    page.locator(".confirm-card .btn", has_text="OK").first.click()
    page.wait_for_selector(".confirm-overlay", state="detached")


def _compare_sides(page):
    sels = page.locator(".sessions-compare-top select")
    return sels.first.input_value(), sels.nth(1).input_value()


def test_a_session_s_name_and_its_menu_both_compare_it_with_current_work(page):
    """The name is the one click that changes nothing, and opens the
    question a saved session is usually opened with. Not the newest
    session here, so the sides are the one clicked, not the default."""
    _cleanup(page)
    try:
        _save(page, "before")
        _save(page, "newest")
        _tag_rows(page, [0, 1])
        _open(page)
        page.locator(".sessions-row", has_text="before").locator("[data-role=name]").click()
        page.wait_for_selector(".sessions-diff-row")
        assert _compare_sides(page) == ("before", "__live__")
        assert page.locator(".sessions-diff-row").first.locator(".diff-n-added").inner_text() == "2"

        page.locator(".side-rail-item[data-section=saved]").click()
        _session_menu(page, "newest", "Compare with current work")
        page.wait_for_function("""() => document.querySelector('.sessions-compare-top select').value === 'newest'
          && document.querySelector('.sessions-diff-row')""")
        assert _compare_sides(page) == ("newest", "__live__")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_renaming_a_session_renames_it_where_it_is_compared_too(page):
    """A comparison open on the session follows it to its new name. Were
    it dropped, the sides would fall back to the newest session, which
    here is the other one."""
    _cleanup(page)
    try:
        _save(page, "draft")
        _save(page, "other")
        _open(page)
        page.locator(".sessions-row", has_text="draft").locator("[data-role=name]").click()
        page.wait_for_function("() => document.querySelector('.sessions-compare-top select').value === 'draft'")

        page.locator(".side-rail-item[data-section=saved]").click()
        _session_menu(page, "draft", "Rename…")
        _answer_prompt(page, "  final  ")
        page.wait_for_selector(".sessions-row:has-text('final')")
        names = page.locator(".sessions-row [data-role=name]").all_inner_texts()
        assert sorted(names) == ["final", "other"]
        assert sorted(s["name"] for s in page.evaluate(
            "() => fetch('/api/case_sessions').then((r) => r.json()).then((d) => d.sessions)")) == ["final", "other"]

        page.locator(".side-rail-item[data-section=compare]").click()
        page.wait_for_function("() => document.querySelector('.sessions-compare-top select').value === 'final'")
        assert _compare_sides(page) == ("final", "__live__")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_renaming_onto_a_taken_name_is_refused_and_says_why(page):
    _cleanup(page)
    try:
        _save(page, "mine")
        _save(page, "taken")
        _open(page)
        page.wait_for_selector(".sessions-row:has-text('mine')")
        _session_menu(page, "mine", "Rename…")
        _answer_prompt(page, "taken")
        _toast(page, "A session named 'taken' already exists")
        assert sorted(page.locator(".sessions-row [data-role=name]").all_inner_texts()) == ["mine", "taken"]
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")

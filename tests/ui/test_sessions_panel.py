"""The Sessions panel: save, fresh pass, and the QC diff.

The diff is the reason this panel exists, so the test that matters is the
one that tags rows, saves, changes the conclusions, and reads what the
comparison actually renders — not just that a table appeared."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open(page):
    page.evaluate("() => __winnow.openSessionManager()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".btn", has_text="Save current work").wait_for(state="visible")


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
        page.wait_for_selector(".session-row .session-name")

        row = page.locator(".session-row", has_text="first pass")
        assert row.count() == 1
        assert "2 tagged" in row.inner_text()
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def _save(page, name):
    page.evaluate("""(name) => fetch('/api/case_sessions', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }) })""", name)
    page.wait_for_timeout(200)


def _compare(page, left, right):
    _open(page)
    page.locator(".session-compare select").first.select_option(left)
    page.locator(".session-compare select").nth(1).select_option(right)
    page.locator(".btn", has_text="Compare").click()
    page.wait_for_selector(".diff-stats")


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
        heads = [h.lower() for h in page.locator(".diff-stats th").all_inner_texts()]
        assert "only in analyst" in heads and "only in current work" in heads, heads
        row = page.locator(".diff-stats tr").nth(1)
        assert row.locator(".diff-n-removed .btn").inner_text() == "1"
        assert row.locator(".diff-n-added .btn").inner_text() == "1"
        assert row.locator(".diff-n-changed .zero").inner_text() == "·"
        # counts, a legend, and nothing else — no row detail in the panel
        assert page.locator(".session-diff .diff-table, .session-diff .diff-cell").count() == 0
        assert "a = analyst, b = current work" in page.locator(".diff-legend").inner_text().lower()
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_count_pivots_to_the_table_with_the_rows_marked(page):
    """From a number to the evidence: the grid shows exactly those rows,
    each marked with which session tagged it, under a banner that names
    the two sides; Done drops both the marks and the filter."""
    _cleanup(page)
    _tag_rows(page, [0, 1, 2])
    try:
        _save(page, "analyst")
        _tag_rows(page, [1, 2])           # the reviewer drops two of the three
        _tag_rows(page, [5], "2")         # and adds one, with a different tag
        rids = page.evaluate("() => [1, 2].map((p) => __winnow.rowAt(p).rid)")
        _compare(page, "analyst", "__live__")
        page.locator(".diff-stats tr").nth(1).locator(".diff-n-removed .btn").click()
        page.wait_for_selector("#modal[hidden]", state="attached")
        page.wait_for_function("(n) => __winnow.S.view && __winnow.S.view.row_count === n", arg=2)
        assert page.evaluate("() => __winnow.S.filterTree.type") == "raw"
        assert sorted(page.evaluate("() => [0, 1].map((p) => __winnow.rowAt(p).rid)")) == sorted(rids)
        # every shown row wears the mark for "analyst only" (A), with the detail on hover
        marks = page.locator("#body .diff-mark")
        assert marks.count() == 2 and set(marks.all_inner_texts()) == {"A"}
        assert "analyst" in marks.first.get_attribute("title") and "TA" in marks.first.get_attribute("title")
        banner = page.locator("#diffBanner")
        assert banner.is_visible() and "analyst" in banner.inner_text() and "Current work" in banner.inner_text()
        # all differences: the added row joins, wearing B
        banner.locator(".btn", has_text="All differences").click()
        page.wait_for_function("(n) => __winnow.S.view && __winnow.S.view.row_count === n", arg=3)
        page.wait_for_function("() => document.querySelectorAll('#body .diff-mark').length === 3")
        assert sorted(page.locator("#body .diff-mark").all_inner_texts()) == ["A", "A", "B"]
        # Done: no marks, no filter, the table back
        banner.locator(".btn", has_text="Done").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        assert page.locator("#diffBanner").is_hidden() and page.locator("#body .diff-mark").count() == 0
    finally:
        page.evaluate("() => { __winnow.S.diffMarks = null; }")
        _cleanup(page)
        page.keyboard.press("Escape")


def test_identical_sessions_say_so_rather_than_showing_an_empty_table(page):
    _cleanup(page)
    _tag_rows(page, [0])
    try:
        page.evaluate("""() => fetch('/api/case_sessions', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'same' }) })""")
        page.wait_for_timeout(200)
        _open(page)
        page.locator(".session-compare select").first.select_option("same")
        page.locator(".session-compare select").nth(1).select_option("__live__")
        page.locator(".btn", has_text="Compare").click()
        page.wait_for_selector("text=No differences")
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

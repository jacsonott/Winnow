"""State that used to outlive the thing it described.

Source ids, view ids and job ids all restart at 1 in a new case, so
anything the client keeps keyed by one of them attaches to the wrong
thing after a switch. Each test drives the sequence that produced a wrong
screen rather than asserting the shape of the fix.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_case_switch_clears_the_notes_editor(page):
    """The editor kept the previous case's narrative, and showNotesTab's
    anti-clobber guard then refused to seed the new case's body over it —
    so the analyst read case A's notes under case B's title, and the first
    keystroke autosaved them onto case B."""
    page.evaluate("() => __winnow.showNotesTab()")
    page.wait_for_selector("#notesEditor")
    page.fill("#notesEditor", "CASE A NARRATIVE")
    page.evaluate("() => __winnow.resetNotes()")
    assert page.input_value("#notesEditor") == ""
    # …and the next open seeds this case's body rather than skipping it.
    page.evaluate("() => __winnow.showNotesTab()")
    page.wait_for_function("() => !document.getElementById('notesEditor').value.includes('CASE A')")
    page.evaluate("() => __winnow.showGridTab()")


def test_removing_a_table_drops_the_filters_stashed_for_it(page):
    """`sources.id` is INTEGER PRIMARY KEY with no AUTOINCREMENT, so a
    re-import can land on the same id and inherit the previous file's
    filters and sort — showing a slice of a table nobody filtered, or a
    filter on a column the new file does not have. closeTab already
    dropped the stash; Remove did not."""
    sid = page.evaluate("() => __winnow.S.sourceId")
    page.evaluate("() => { __winnow.S.filters = { Host: 'H1' }; }")
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")

    # Re-opening stashes on the way out, which is what a later open (or a
    # re-import onto the same id) would restore from.
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
    assert page.evaluate("() => __winnow.S.filters.Host") == "H1", "no stash to drop"

    # What tables.js's Remove now calls: the entry existed and is gone.
    assert page.evaluate("(id) => __winnow.dropViewStateFor(id)", sid) is True
    assert page.evaluate("(id) => __winnow.dropViewStateFor(id)", sid) is False

    page.evaluate("() => { __winnow.S.filters = {}; }")
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")


def test_a_rebuild_that_outlives_its_table_does_not_paint(page):
    """The only guard was a monotonic sequence number, which catches "a
    newer rebuild started" but not "the table changed underneath" — and
    openSource's cached-view path changes tables without rebuilding at
    all, so it never bumps that number."""
    sid = page.evaluate("() => __winnow.S.sourceId")
    seen = page.evaluate("""async (id) => {
      const before = __winnow.S.view.view_id;
      const p = __winnow.rebuildView({ keepScroll: false });
      __winnow.S.sourceId = id + 999;          // the table changed mid-flight
      await p;
      const after = __winnow.S.view.view_id;
      __winnow.S.sourceId = id;
      return { before, after };
    }""", sid)
    assert seen["before"] == seen["after"], "a stale rebuild painted over the current view"


def test_a_pivot_turns_the_timeframe_off_and_says_so(page):
    """A pivot from a count must show the rows behind that count. The
    counts ignore the timeframe, so leaving it on showed fewer rows than
    the number promised — and clearing it silently wiped case-wide state."""
    page.evaluate("""() => {
      __winnow.S.timeRange = { enabled: true, column: 'Timestamp',
                               start: '2026-03-14 08:00:00', end: '2026-03-14 08:10:00' };
    }""")
    page.evaluate("""() => __winnow.replaceFilters(
      { type: 'group', op: 'AND', children: [] }, { clearTimeframe: true })""")
    page.wait_for_function("() => __winnow.S.timeRange.enabled === false")
    assert "on" not in page.locator("#btnTimeRange").get_attribute("class").split()


def test_the_cached_view_path_still_scopes_the_tag_counts(page):
    """Returning to a table with an unchanged spec restores the view from
    cache without rebuilding. That handle carried no source_id, so every
    view-scoped tag count bailed and the ribbon showed whole-table numbers
    beside a filtered view for as long as it lasted."""
    sid = page.evaluate("() => __winnow.S.sourceId")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
    page.evaluate("(id) => __winnow.openSource(id)", sid)      # unchanged spec: cache hit
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
    assert page.evaluate("() => __winnow.S.view.source_id") == sid

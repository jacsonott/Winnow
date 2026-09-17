"""A tag or note written while the grid is grouped is still on the row
after Ungroup — and a whole-view write while grouped reaches the rows on
screen.

Grouping does not empty the flat page cache: the group pages sit on top of
it, for the same view id, and a write made while grouped only ever reached
the group-page row objects. `dropGrouping` then painted the flat cache's
copies, whose `tags` arrays predate the write, so the stripe was missing
until something else rebuilt the view. The rail and the ribbon read the
server and were right all along, which is what made it look like the rows
had merely not caught up yet. The inverse held too: Shift+hotkey and undo
dropped only the flat cache, so under a grouping by an ordinary column the
rows on screen kept their old stripes.

The fixture's view is sorted by Timestamp (openSource's default) and a
sort can be left persisted by an earlier module, so a row's flat position
is always found by rid, never by arithmetic. The server case is shared by
the whole UI session: every test puts back exactly what it changed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

TAG = "__winnow.S.tags[0]"

# Synchronous scans of the flat cache by row identity. The 200-row fixture
# is one page, and once the fix is in that page is refetched after Ungroup,
# so each wait is on the row's *arrival* with the expected state rather
# than on a cached object — the stale object is precisely the bug.
FLAT_HAS_RID = "(rid) => [...__winnow.S.rowsByPos.values()].some((r) => r.rid === rid)"
FLAT_ROW_TAGGED = "(rid) => [...__winnow.S.rowsByPos.values()].some((r) => r.rid === rid && r.tags.length === 1)"
FLAT_ROW_CLEAN = "(rid) => [...__winnow.S.rowsByPos.values()].some((r) => r.rid === rid && r.tags.length === 0)"
FLAT_ROW_NOTE = "([rid, text]) => [...__winnow.S.rowsByPos.values()].some((r) => r.rid === rid && (r.note || '') === text)"
FLAT_POS_OF = "(rid) => { const r = [...__winnow.S.rowsByPos.values()].find((x) => x.rid === rid); return r ? r.pos : -1; }"
GROUP_ROW_TAGGED = "() => !!__winnow.rowAt(1) && __winnow.rowAt(1).tags.length === 1"


def _group_and_expand_first(page):
    """Group by Host, open the first group, and hand back the rid of the
    first data row under it (pos 1 — pos 0 is the header)."""
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    # The precondition that makes any of this bite: grouping leaves the
    # flat cache alive for the same view id.
    assert page.evaluate("() => __winnow.S.pages.size") > 0
    page.evaluate("() => __winnow.toggleGroup(0)")
    page.wait_for_function("() => __winnow.S.groups[0].expanded && !!__winnow.rowAt(1)")
    return page.evaluate("() => __winnow.rowAt(1).rid")


def _ungroup(page):
    if page.evaluate("() => __winnow.S.groupByCols.length"):
        page.evaluate("() => __winnow.dropGrouping()")
    page.wait_for_function("() => !__winnow.S.groupByCols.length && __winnow.S.pages.size > 0")


def _gutter_mark_count(page, rid, cls):
    """How many `.gutter .<cls>` marks the painted flat row for `rid` has."""
    pos = page.evaluate(FLAT_POS_OF, rid)
    assert pos >= 0
    page.evaluate("(p) => { __winnow.scrollIntoView(p); __winnow.render(); }", pos)
    return page.locator(f".row[data-pos='{pos}'] .gutter .{cls}").count()


def _untag_flat_row(page, rid):
    """Put the shared case back: untag by rid in flat mode, with an explicit
    direction so it never reads the (possibly stale) cached row."""
    _ungroup(page)
    page.wait_for_function(FLAT_HAS_RID, arg=rid)
    pos = page.evaluate(FLAT_POS_OF, rid)
    page.evaluate(f"(p) => __winnow.tagRowsAtPositions({TAG}, [p], false)", pos)
    page.wait_for_function(FLAT_ROW_CLEAN, arg=rid)


def test_a_row_tagged_while_grouped_keeps_its_tag_after_ungroup(page):
    rid = _group_and_expand_first(page)
    try:
        page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [1], true)")
        page.wait_for_function(GROUP_ROW_TAGGED)
        _ungroup(page)
        # The flat copy of that row used to answer tags.length === 0 forever.
        page.wait_for_function(FLAT_ROW_TAGGED, arg=rid)
        assert _gutter_mark_count(page, rid, "stripe") == 1
    finally:
        _untag_flat_row(page, rid)


def test_a_group_tagged_from_its_menu_shows_on_its_rows_after_ungroup(page):
    rid = _group_and_expand_first(page)
    value = page.evaluate("() => __winnow.S.groups[0].value")
    try:
        page.locator(".group-header-row").first.click(button="right")
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item:has(.menu-swatch)").first.click()   # the first tag, in apply mode
        page.keyboard.press("Escape")                                       # the item keeps the menu open
        page.wait_for_function(GROUP_ROW_TAGGED)
        _ungroup(page)
        page.wait_for_function(FLAT_ROW_TAGGED, arg=rid)
        assert _gutter_mark_count(page, rid, "stripe") == 1
    finally:
        # The inverse of what the menu did, on the same group, found by its
        # value rather than its index.
        if not page.evaluate("() => __winnow.S.groupByCols.length"):
            page.evaluate("() => __winnow.addGroupLevel('Host')")
            page.wait_for_function("() => __winnow.S.groups.length > 0")
        page.evaluate(f"(v) => __winnow.tagWholeGroup(__winnow.S.groups.find((g) => g.value === v), {TAG}, false)", value)
        _ungroup(page)
        page.wait_for_function(FLAT_ROW_CLEAN, arg=rid)


def test_shift_hotkey_while_grouped_repaints_the_open_group(page):
    """The whole-view tag is server-side; the open group's rows on screen
    come from the group cache, which that write used to leave alone."""
    _group_and_expand_first(page)
    tag_id, hotkey = page.evaluate(f"() => [{TAG}.id, {TAG}.hotkey]")
    assert hotkey, "the fixture's first tag has no hotkey to press"
    before = page.evaluate("(id) => __winnow.S.tagCountsAll[id] || 0", tag_id)
    tagged = False
    try:
        page.keyboard.press(f"Shift+{hotkey}")
        page.wait_for_selector(".confirm-overlay")
        page.locator(".confirm-card .btn", has_text="OK").click()
        # Server truth first: the write landed (the count came back with it).
        page.wait_for_function("([id, n]) => (__winnow.S.tagCountsAll[id] || 0) > n", arg=[tag_id, before])
        tagged = True
        page.wait_for_function(GROUP_ROW_TAGGED)
    finally:
        if tagged:
            page.evaluate("() => __winnow.undoLastTagChange()")   # exact: the server replays its own delta
            page.wait_for_function("([id, n]) => (__winnow.S.tagCountsAll[id] || 0) === n", arg=[tag_id, before])
        _ungroup(page)


def test_a_note_written_while_grouped_keeps_its_mark_after_ungroup(page, api):
    rid = _group_and_expand_first(page)
    source_id = page.evaluate("() => __winnow.S.sourceId")
    text = "written while grouped"
    try:
        page.evaluate("() => { __winnow.S.cursor = 1; __winnow.showDetail(1); }")
        page.locator("#noteInput").fill(text)
        page.wait_for_function("() => document.getElementById('noteStatus').textContent === 'Saved'")
        _ungroup(page)
        page.wait_for_function(FLAT_ROW_NOTE, arg=[rid, text])
        assert _gutter_mark_count(page, rid, "has-note") == 1
    finally:
        # Straight to the server: cleanup must not depend on which cached
        # row object the detail pane happens to be looking at.
        api("/api/note", "POST", {"source_id": source_id, "rid": rid, "note": ""})
        _ungroup(page)

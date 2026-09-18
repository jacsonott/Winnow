"""A filter change lands on the row you were on, not at the top.

The tag chips rebuilt the view and did nothing else: the grid went to
row 0, and `S.cursor` stayed the number it had been — which, once the
view widened and every position renumbered, named some unrelated row far
below the fold. Only "Clear filters" found the row again, by identity.
Place-keeping lives in `rebuildView` itself now (`keepRow`), so a filter
change that lands somewhere new comes back to the same row, and a row the
narrower view no longer has clears the cursor rather than leaving a stale
number behind. The header box keeps its scroll offset as before, and
whether the cursor follows the row there depends on the detail pane: open,
it must (the pane shows whatever row the cursor names); closed, nothing
looks, so no row lookup is issued for a keystroke.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

DEEP = 150  # a position well below the first screen of the 200-row fixture


def _chip(page):
    """The ribbon chip for the tag on hotkey 1 (TA), by its place in
    S.tags — the ribbon renders them in that order, and the chip's text
    carries a count that changes under the test."""
    idx = page.evaluate("() => __winnow.S.tags.findIndex((t) => t.hotkey === '1')")
    assert idx >= 0
    return page.locator("#tagRibbon .tag-chip").nth(idx)


def _tag_row(page, pos, on):
    """Tag (or untag) the row at `pos` the way the analyst does — cursor on
    it, the hotkey-1 tag — and wait for the row cache to agree."""
    page.evaluate("(p) => __winnow.moveCursor(p, false)", pos)
    page.wait_for_function("(p) => __winnow.S.cursor === p && !!__winnow.rowAt(p)", arg=pos)
    page.evaluate("(on) => __winnow.applyTag(__winnow.S.tags.find((t) => t.hotkey === '1'), on)", on)
    page.wait_for_function("([p, on]) => (__winnow.rowAt(p) || { tags: [] }).tags.length === (on ? 1 : 0)", arg=[pos, on])


def _drop_filters(page):
    page.evaluate("""() => {
      __winnow.S.tagFilter = [];
      __winnow.S.filters = {};
      __winnow.renderTagRibbon();
      __winnow.renderHead();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && !__winnow.S.tagFilter.length")


def _park(page):
    """Leave the shared fixture as found: no cursor, top of the table,
    detail pane closed."""
    page.evaluate("""() => {
      __winnow.S.cursor = -1; __winnow.S.anchor = -1;
      document.getElementById('detail').hidden = true;
      document.getElementById('detailResize').hidden = true;
      document.getElementById('body').scrollTop = 0;
      __winnow.render();
    }""")


def _scroll_top(page):
    return page.evaluate("() => document.getElementById('body').scrollTop")


def test_dropping_the_tag_filter_lands_on_the_row(page):
    rid = DEEP + 1
    try:
        _tag_row(page, DEEP, True)
        _chip(page).click()
        page.wait_for_function("() => __winnow.S.tagFilter.length === 1 && __winnow.S.view && __winnow.S.view.row_count < 200")
        # The analyst's place: the cursor on that row, in the narrower view.
        pos = page.evaluate("(rid) => [...__winnow.S.rowsByPos.values()].find((r) => r.rid === rid).pos", rid)
        page.evaluate("(p) => __winnow.moveCursor(p, false)", pos)
        page.wait_for_function("(p) => __winnow.S.cursor === p", arg=pos)
        _chip(page).click()
        # Back on the same row — by identity, so at the deep position again
        # (an unfiltered view's position is rid - 1) — not on whatever row
        # now holds the narrower view's position, and not at the top.
        page.wait_for_function(
            "(p) => !__winnow.S.tagFilter.length && __winnow.S.view.row_count === 200 && __winnow.S.cursor === p",
            arg=DEEP)
        page.wait_for_function("() => document.querySelector('#body .row.cursor') !== null")
        assert page.evaluate("() => __winnow.rowAt(__winnow.S.cursor).rid") == rid
        body, row, head_h = page.evaluate("""() => {
          const b = document.getElementById('body').getBoundingClientRect();
          const r = document.querySelector('#body .row.cursor').getBoundingClientRect();
          return [{ top: b.top, bottom: b.bottom }, { top: r.top, bottom: r.bottom }, __winnow.headH()];
        }""")
        assert body["top"] + head_h <= row["top"] and row["bottom"] <= body["bottom"], (body, row, head_h)
        assert _scroll_top(page) > 0
    finally:
        _drop_filters(page)
        _tag_row(page, DEEP, False)
        _park(page)


def test_a_filter_the_cursor_row_fails_clears_the_cursor_and_the_pane(page):
    other = 4   # the tagged row; the cursor sits on DEEP, which is not
    try:
        _tag_row(page, other, True)
        page.evaluate("(p) => __winnow.moveCursor(p, false)", DEEP)
        page.wait_for_function("(p) => __winnow.S.cursor === p && document.querySelector('#body .row.cursor') !== null", arg=DEEP)
        page.evaluate("() => __winnow.toggleDetailPane()")
        page.wait_for_selector("#detail:not([hidden])")
        # textContent: the title is upper-cased by CSS, and inner_text reads through that.
        assert page.evaluate("() => document.getElementById('detailTitle').textContent") == f"Line {DEEP + 1}"
        _chip(page).click()
        page.wait_for_function("() => __winnow.S.tagFilter.length === 1 && __winnow.S.view && __winnow.S.view.row_count < 200")
        # Not a stale number naming whatever the narrower view has at 150
        # (nothing), and not a pane still showing a row the view lacks.
        page.wait_for_function("() => __winnow.S.cursor === -1")
        assert page.locator("#detail").is_hidden()
        assert _scroll_top(page) == 0
    finally:
        _drop_filters(page)
        _tag_row(page, other, False)
        _park(page)


def _row_position_asks(page):
    """Every /api/row_position GET the page issues from here on."""
    asks = []
    page.on("request", lambda r: asks.append(r.url) if "/api/row_position" in r.url else None)
    return asks


def test_the_header_box_keeps_its_offset_and_asks_nothing_with_the_pane_closed(page):
    """Typing in a header box keeps the scroll offset, and with the detail
    pane closed the cursor keeps its number too — the highlight stays at
    its screen spot, as it always has. No row lookup is issued for it: on a
    materialised view that is a scan of the whole view table, which every
    debounced keystroke would otherwise pay before the grid could repaint.
    """
    at = 100
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        page.evaluate("(p) => __winnow.moveCursor(p, false)", at)
        page.wait_for_function("(p) => __winnow.S.cursor === p && document.querySelector('#body .row.cursor') !== null", arg=at)
        assert page.locator("#detail").is_hidden()
        before = _scroll_top(page)
        assert before > 0
        asks = _row_position_asks(page)
        box.fill("4")   # contains: 4624/4625/4688 stay, 1 goes — 150 rows
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 150")
        # The swap re-points (or clears) the cursor in the same turn it
        # installs the view, so once the count is in the cursor is settled.
        assert page.evaluate("() => __winnow.S.cursor") == at
        assert _scroll_top(page) == before
        box.press("Escape")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        assert page.evaluate("() => __winnow.S.cursor") == at
        assert _scroll_top(page) == before
        assert asks == []
    finally:
        _drop_filters(page)
        _park(page)


def test_the_header_box_keeps_the_open_pane_on_its_row(page):
    """With the detail pane open the cursor is the pane's row, and a number
    left behind puts whatever row now holds it into the pane as its page
    lands. So on this path — the pane open — the cursor follows the row to
    its new position and back and the pane stays on the line it was
    showing, while the viewport still does not move."""
    at = 100   # rid 101: EventId 4624, which a "4" filter keeps
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        page.evaluate("(p) => __winnow.moveCursor(p, false)", at)
        page.wait_for_function("(p) => __winnow.S.cursor === p && document.querySelector('#body .row.cursor') !== null", arg=at)
        page.evaluate("() => __winnow.toggleDetailPane()")
        page.wait_for_selector("#detail:not([hidden])")
        assert page.evaluate("() => document.getElementById('detailTitle').textContent") == f"Line {at + 1}"
        before = _scroll_top(page)
        assert before > 0
        box.fill("4")   # contains: 4624/4625/4688 stay, 1 goes — 150 rows
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 150")
        # The same row at its new position (three of every four rows above
        # it survive), not the number 100 naming a stranger under the pane,
        # and not a cleared cursor with the pane closed.
        page.wait_for_function("(p) => __winnow.S.cursor === p", arg=75)
        assert page.evaluate("() => __winnow.rowAt(__winnow.S.cursor).rid") == at + 1
        assert page.locator("#detail").is_visible()
        assert page.evaluate("() => document.getElementById('detailTitle').textContent") == f"Line {at + 1}"
        assert _scroll_top(page) == before
        box.press("Escape")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        page.wait_for_function("(p) => __winnow.S.cursor === p", arg=at)
        assert page.locator("#detail").is_visible()
        assert _scroll_top(page) == before
    finally:
        _drop_filters(page)
        _park(page)

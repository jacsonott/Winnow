"""Dropping a filter lands on the row you were on, whichever way you drop it.

Analysts reported that with a row selected, taking the filters off
sometimes kept the row centered on screen and sometimes sent the grid
to the top, where the only way back was an arrow key (the cell cursor
starts from the row cursor, so the first press jumped down to it). The
difference was the control used. "Clear filters" rebuilt with
`keepScroll: false` and found the row by identity. The header box
(Escape, or emptying it), a filter chip's ✕ and `z` rebuilt with the
default `keepScroll: true`, which holds the old scroll offset. On a view
that just grew, that offset names some other part of the table, usually
the top: a narrow filter's rows fit on one screen at scrollTop 0. With
the detail pane closed the cursor also stayed a number, naming whatever
row now held it. With the pane open it was re-pointed at the right row,
off screen.

And "Clear filters" itself went to the top on a big table: the row is
captured from the page cache, which keeps ten pages, so once the analyst
had scrolled around far enough the cursor's page was gone and there was
nothing to land on.

Each test asserts the symptom: after the drop the cursor names the same
row, and that row is on screen below the sticky header.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

ROWS = 200
# EventId cycles 4624, 4625, 4688, 1, so `=1` keeps rows 4, 8, ... 200:
# 50 rows. Position 40 of those is line 164, at position 163 in the full
# table: several screens down, where the old offset (a few rows into the
# filtered view) is not.
FILTER = "=1"
AT = 40
RID = AT * 4 + 4


def _row_on_screen(page):
    """Wait for the cursor to name `RID` with a painted row, and return
    whether that row sits between the sticky header and the body's
    bottom edge."""
    page.wait_for_function(
        """(rid) => { const W = __winnow, r = W.S.cursor >= 0 && W.rowAt(W.S.cursor);
             return r && r.rid === rid && document.querySelector('#body .row.cursor') !== null; }""",
        arg=RID)
    return page.evaluate("""() => {
      const b = document.getElementById('body').getBoundingClientRect();
      const r = document.querySelector('#body .row.cursor').getBoundingClientRect();
      return b.top + __winnow.headH() <= r.top && r.bottom <= b.bottom;
    }""")


def _filtered_with_cursor(page, view_rebuilt, *, pane):
    """The EventId filter on through its header box, then the cursor on
    line 164 of the filtered rows, the way a click puts it there."""
    box = page.locator('.fcell input[data-col="EventId"]')
    with view_rebuilt(page):
        box.fill(FILTER)
    assert page.evaluate("() => __winnow.S.view.row_count") == ROWS // 4
    page.evaluate("(p) => __winnow.moveCursor(p)", AT)
    page.wait_for_function("(rid) => (__winnow.rowAt(__winnow.S.cursor) || {}).rid === rid", arg=RID)
    if pane:
        page.evaluate("() => __winnow.toggleDetailPane()")
        page.wait_for_selector("#detail:not([hidden])")
    return box


def _bar(page):
    """The filter bar instead of the row, as test_filter_bar.py turns it on."""
    page.evaluate("""() => { __winnow.S.appearance.filterUi = 'bar';
      __winnow.S.filters = {}; __winnow.S.filterOpen = []; __winnow.renderHeadResized(); }""")


def _drop(page, box, how):
    if how == "escape":
        box.press("Escape")
    elif how == "emptied":
        box.fill("")          # the box's own debounced rebuild
    elif how == "chip":
        page.locator(".filter-chip-rm").click()
    elif how == "undo":
        page.locator("#body").focus()
        page.keyboard.press("z")
    elif how == "clear":
        page.locator("#btnReset").click()
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("pane", [False, True], ids=["pane-closed", "pane-open"])
@pytest.mark.parametrize("how", ["escape", "emptied", "chip", "undo", "clear"])
def test_dropping_the_filter_lands_on_the_row(page, view_rebuilt, how, pane):
    if how == "chip":
        _bar(page)
        page.locator('.hcell[data-col="EventId"] .hcell-filter').click()
        page.wait_for_selector('.fcell input[data-col="EventId"]')
    box = _filtered_with_cursor(page, view_rebuilt, pane=pane)
    if how == "chip":
        with view_rebuilt(page):
            box.press("Enter")   # folds the box into its chip; same view, rebuilt
    with view_rebuilt(page):
        _drop(page, box, how)
    assert page.evaluate("() => __winnow.S.view.row_count") == ROWS
    assert _row_on_screen(page), f"line {RID} is not on screen after dropping the filter ({how})"
    assert page.evaluate("() => document.getElementById('body').scrollTop") > 0
    assert page.locator("#detail").is_visible() == pane


def test_clearing_the_search_lands_on_the_row(page, view_rebuilt):
    """The search box narrows rows too, and emptying it is the same drop:
    Escape in it always landed on the row, deleting its text did not."""
    page.locator("#body").focus()
    page.keyboard.press("/")
    search = page.locator("#search")
    search.wait_for(state="visible")
    with view_rebuilt(page):
        search.fill("08:02:")   # lines 121-180: 60 rows
        search.press("Enter")
    assert page.evaluate("() => __winnow.S.view.row_count") == 60
    # Line 164 is position 43 of those 60.
    page.evaluate("() => __winnow.moveCursor(43)")
    page.wait_for_function("(rid) => (__winnow.rowAt(__winnow.S.cursor) || {}).rid === rid", arg=RID)
    with view_rebuilt(page):
        search.fill("")
    assert page.evaluate("() => __winnow.S.view.row_count") == ROWS
    assert _row_on_screen(page)


def test_clear_filters_finds_a_row_whose_page_left_the_cache(page, view_rebuilt):
    """The cache keeps ten pages, so on a big table a cursor the analyst
    has scrolled a long way from is no longer in it. This table is one
    page, so the page is dropped the way trimPageCache drops one, in the
    same turn as the click, before the rebuild captures the row."""
    _filtered_with_cursor(page, view_rebuilt, pane=False)
    with view_rebuilt(page):
        page.evaluate("""() => {
          const W = __winnow, S = W.S;
          for (const [idx, rows] of S.pages) { for (const r of rows) S.rowsByPos.delete(r.pos); S.pages.delete(idx); }
          S.rowsByPos.clear();   // the screenful a glimpse put there too
          if (W.rowAt(S.cursor)) throw new Error('the cursor row is still cached');
          document.getElementById('btnReset').click();
        }""")
    assert page.evaluate("() => __winnow.S.view.row_count") == ROWS
    assert _row_on_screen(page)


def test_typing_a_filter_still_holds_the_viewport(page, view_rebuilt):
    """The other half of the rule: writing a filter is not dropping one.
    Narrowing or editing keeps the scroll offset and issues no row lookup,
    which on a materialized view is a scan of the whole view table for
    every debounced keystroke."""
    asks = []
    page.on("request", lambda r: asks.append(r.url) if "/api/row_position" in r.url else None)
    page.evaluate("() => __winnow.moveCursor(60)")
    page.wait_for_function("() => document.querySelector('#body .row.cursor') !== null")
    before = page.evaluate("() => document.getElementById('body').scrollTop")
    assert before > 0
    box = page.locator('.fcell input[data-col="EventId"]')
    with view_rebuilt(page):
        box.fill("4")    # 150 rows
    with view_rebuilt(page):
        box.fill("462")  # edited, still a filter: 100 rows
    assert page.evaluate("() => __winnow.S.view.row_count") == 100
    assert page.evaluate("() => document.getElementById('body').scrollTop") == before
    assert asks == []

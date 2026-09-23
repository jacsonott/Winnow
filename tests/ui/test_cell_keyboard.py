"""Excel-style cell selection from the keyboard.

Arrow keys move the highlighted cell, Shift grows the rectangle the way
dragging does, Ctrl jumps to the far edge, and Ctrl+Shift does both. None
of it can be checked without a document: what a keyboard cell cursor is
FOR is knowing where you are in a table too wide and too long to see, so
every claim here is about something rendered — which cell wears the ring,
whether the grid scrolled to keep it visible, whether it survived a
repaint.

The two that are here because they would otherwise be found by an analyst:

* Arrows now create a cell range where only the mouse could before, and
  Ctrl+C prefers a cell range over picked rows. An analyst who picks forty
  rows in the gutter and then presses Down to read the next one must not
  find that Ctrl+C has quietly become "copy one cell" — so a one-cell
  range loses to picked rows, while a rectangle still wins.
* renderHead() drops the cell selection because a column's index is only
  meaningful against the column list it was taken from. It also runs on
  every filter keystroke and every sort, so the active cell is carried
  across by column NAME instead. A cursor that vanished whenever the
  analyst typed in a filter box would not be a cursor.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean(page):
    """Both selections and the scroll, before and after: this module moves
    the cell cursor to the far corners of a shared case file."""
    page.evaluate("""() => {
      __winnow.selClear();
      __winnow.clearCellSelection();
      __winnow.S.selHidden = 0;
      __winnow.S.cursor = 0;
      document.getElementById('body').scrollTop = 0;
      document.getElementById('body').scrollLeft = 0;
      __winnow.render();
    }""")
    yield
    page.keyboard.press("Escape")
    page.evaluate("""async () => {
      __winnow.selClear();
      __winnow.clearCellSelection();
      document.getElementById('body').scrollLeft = 0;
      __winnow.clearAllFilters();
    }""")


def _cols(page):
    return page.evaluate("() => __winnow.visibleCols()")


def _focus(page):
    """Position and column index only — S.cellFocus also carries the
    column's NAME, which is bookkeeping for surviving a repaint rather
    than something these assertions are about."""
    f = page.evaluate("() => __winnow.S.cellFocus")
    return None if f is None else {"pos": f["pos"], "col": f["col"]}


def _range(page):
    return page.evaluate("() => __winnow.S.cellRange")


def _click_cell(page, row, col_name):
    ci = _cols(page).index(col_name)
    page.locator("#body .row").nth(row).locator(".cell").nth(ci).click()


# ------------------------------------------------------------- moving

def test_an_arrow_moves_the_active_cell_and_only_one_cell_wears_the_ring(page):
    cols = _cols(page)
    _click_cell(page, 3, cols[0])
    assert _focus(page) == {"pos": 3, "col": 0}

    page.keyboard.press("ArrowRight")
    assert _focus(page) == {"pos": 3, "col": 1}
    page.keyboard.press("ArrowDown")
    assert _focus(page) == {"pos": 4, "col": 1}

    # The ring is the whole point: it says where the next arrow starts.
    assert page.locator("#body .cell-active").count() == 1
    ring = page.locator("#body .cell-active")
    assert ring.evaluate("(c) => Number(c.dataset.col)") == 1
    assert ring.evaluate("(c) => Number(c.closest('.row').dataset.pos)") == 4


def test_the_arrows_work_before_anything_has_been_clicked(page):
    """A grid nobody has clicked in still has a row cursor — a search hit,
    a row opened from the watchlist. The first arrow continues from there
    rather than teleporting to the top of the table."""
    page.evaluate("() => { __winnow.clearCellSelection(); __winnow.S.cursor = 7; __winnow.render(); }")
    assert _focus(page) is None
    page.keyboard.press("ArrowDown")
    assert _focus(page) == {"pos": 8, "col": 0}


def test_a_plain_arrow_collapses_a_rectangle_to_where_it_lands(page):
    """Excel's rule: moving without Shift is leaving the selection, not
    dragging it along."""
    cols = _cols(page)
    _click_cell(page, 2, cols[0])
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    assert _range(page)["r1"] - _range(page)["r0"] == 2
    page.keyboard.press("ArrowDown")
    r = _range(page)
    assert (r["r0"], r["r1"], r["c0"], r["c1"]) == (5, 5, 0, 0)


def test_the_row_cursor_follows_the_active_cell(page):
    """The detail pane, the note box, the tag keys and the row menu all
    read S.cursor. A ring on one row while those act on another is the bug
    this feature would otherwise ship."""
    cols = _cols(page)
    _click_cell(page, 1, cols[0])
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => __winnow.S.cursor") == 3
    assert _focus(page)["pos"] == 3


# --------------------------------------------------------- extending

def test_shift_arrow_grows_the_rectangle_from_the_anchor(page):
    cols = _cols(page)
    _click_cell(page, 4, cols[1])
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowRight")
    r = _range(page)
    assert (r["r0"], r["r1"]) == (4, 5)
    assert (r["c0"], r["c1"]) == (1, 2)
    # The anchor stayed where the click put it; only the active cell moved.
    assert page.evaluate("() => __winnow.S.cellAnchor") == {"pos": 4, "col": 1}
    assert _focus(page) == {"pos": 5, "col": 2}
    # Shrinking works too — an over-shoot is one keystroke back, not a restart.
    page.keyboard.press("Shift+ArrowUp")
    assert _range(page)["r1"] == 4


def test_shift_arrow_continues_from_where_the_mouse_left_off(page):
    """The pointer and the keyboard write the same two corners, so a drag
    can be finished with the keyboard."""
    cols = _cols(page)
    _click_cell(page, 2, cols[0])
    ci = _cols(page).index(cols[1])
    page.locator("#body .row").nth(4).locator(".cell").nth(ci).click(modifiers=["Shift"])
    assert _focus(page) == {"pos": 4, "col": 1}
    page.keyboard.press("Shift+ArrowDown")
    r = _range(page)
    assert (r["r0"], r["r1"], r["c0"], r["c1"]) == (2, 5, 0, 1)


# ------------------------------------------------------------- edges

def test_ctrl_arrow_jumps_to_the_far_edge_keeping_the_other_axis(page):
    cols = _cols(page)
    total = page.evaluate("() => __winnow.S.view.row_count")
    _click_cell(page, 3, cols[1])
    page.keyboard.press("Control+ArrowDown")
    assert _focus(page) == {"pos": total - 1, "col": 1}      # column kept
    page.keyboard.press("Control+ArrowUp")
    assert _focus(page) == {"pos": 0, "col": 1}
    page.keyboard.press("Control+ArrowRight")
    assert _focus(page) == {"pos": 0, "col": len(cols) - 1}  # row kept
    page.keyboard.press("Control+ArrowLeft")
    assert _focus(page) == {"pos": 0, "col": 0}


def test_ctrl_shift_arrow_takes_everything_from_here_to_the_edge(page):
    cols = _cols(page)
    total = page.evaluate("() => __winnow.S.view.row_count")
    _click_cell(page, 5, cols[0])
    page.keyboard.press("Control+Shift+ArrowDown")
    r = _range(page)
    assert (r["r0"], r["r1"]) == (5, total - 1)
    assert (r["c0"], r["c1"]) == (0, 0), "the column is not part of this gesture"
    page.keyboard.press("Control+Shift+ArrowRight")
    assert _range(page)["c1"] == len(cols) - 1


def test_an_arrow_at_the_edge_stays_put_rather_than_wrapping(page):
    cols = _cols(page)
    _click_cell(page, 0, cols[0])
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowLeft")
    assert _focus(page) == {"pos": 0, "col": 0}


# ---------------------------------------------------------- scrolling

def test_moving_down_past_the_window_scrolls_and_the_cell_is_still_drawn(page):
    """Only the visible window is in the DOM, so an active cell the grid
    did not scroll to would be a ring on a row that does not exist."""
    cols = _cols(page)
    _click_cell(page, 0, cols[0])
    page.keyboard.press("Control+ArrowDown")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.getElementById('body').scrollTop") > 100
    assert page.locator("#body .cell-active").count() == 1
    assert page.locator("#body .cell-active").is_visible()


def test_moving_right_brings_the_column_into_view(page):
    page.evaluate("""() => {
      for (const c of __winnow.S.columns) {
        __winnow.S.layout[c.name] = { ...(__winnow.S.layout[c.name] || {}), hidden: false, w: 600 };
      }
      __winnow.renderHead(); __winnow.render();
    }""")
    page.wait_for_timeout(150)
    cols = _cols(page)
    _click_cell(page, 1, cols[0])
    assert page.evaluate("() => document.getElementById('body').scrollLeft") == 0
    page.keyboard.press("Control+ArrowRight")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.getElementById('body').scrollLeft") > 0
    assert page.locator("#body .cell-active").is_visible()


def test_the_active_cell_does_not_park_under_a_pinned_column(page):
    """A pinned column is position:sticky over the left edge of the
    scroller. Scrolling a cell flush to scrollLeft puts it underneath one,
    where the ring is invisible and the analyst has lost their place."""
    page.evaluate("""() => {
      for (const c of __winnow.S.columns) {
        __winnow.S.layout[c.name] = { ...(__winnow.S.layout[c.name] || {}), hidden: false, w: 600 };
      }
      __winnow.renderHead(); __winnow.render();
    }""")
    cols = _cols(page)
    page.evaluate("""(name) => {
      __winnow.S.layout[name] = { ...(__winnow.S.layout[name] || {}), pinned: true };
      __winnow.renderHead(); __winnow.render();
    }""", cols[0])
    page.wait_for_timeout(200)
    try:
        _click_cell(page, 1, cols[1])
        page.keyboard.press("Control+ArrowRight")
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(300)
        ring = page.locator("#body .cell-active").bounding_box()
        pinned = page.locator(f'#body .row .cell.pinned').first.bounding_box()
        assert ring["x"] >= pinned["x"] + pinned["width"] - 1, (ring, pinned)
    finally:
        page.evaluate("""() => {
          for (const k of Object.keys(__winnow.S.layout)) {
            if (__winnow.S.layout[k].pinned) __winnow.S.layout[k].pinned = false;
          }
          __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
          document.getElementById('body').scrollLeft = 0;
        }""")
        page.wait_for_timeout(600)


# ------------------------------------------- what the selection means

def test_arrowing_with_rows_picked_does_not_steal_the_copy(page):
    """The regression this feature would otherwise ship. Pick rows in the
    gutter, press an arrow to read the next one, and Ctrl+C must still
    copy the rows — a one-cell range is where you are, not what you
    chose."""
    page.locator("#body .row").nth(1).locator(".gutter").click()
    page.locator("#body .row").nth(2).locator(".gutter").click()
    assert page.evaluate("() => __winnow.selCount()") == 2
    page.keyboard.press("ArrowDown")
    assert _range(page) is not None, "the arrow does leave a one-cell range behind"

    page.keyboard.press("Control+c")
    page.wait_for_timeout(400)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert len(clip.strip().splitlines()) == 2, clip


def test_a_rectangle_still_wins_the_copy(page):
    """Dragging one out — or Shift+Arrowing one — is a choice, and it
    keeps beating whatever was picked earlier."""
    cols = _cols(page)
    page.locator("#body .row").nth(1).locator(".gutter").click()
    _click_cell(page, 5, cols[0])
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Control+c")
    page.wait_for_timeout(400)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    lines = clip.strip().splitlines()
    assert len(lines) == 2, clip
    # One column wide: the rectangle, not whole rows.
    assert "\t" not in lines[0], clip


# ------------------------------------------------------- staying alive

def test_the_active_cell_survives_a_filter_edit(page):
    """renderHead() drops the rectangle because column indices stop
    meaning anything, and it runs on every filter keystroke. The cell is
    carried across by column name."""
    cols = _cols(page)
    name = "Host" if "Host" in cols else cols[1]
    ci = cols.index(name)
    _click_cell(page, 4, name)
    page.evaluate("() => __winnow.renderHead()")
    page.wait_for_timeout(150)
    assert _focus(page) == {"pos": 4, "col": ci}
    assert page.locator("#body .cell-active").count() == 1


def test_a_hidden_column_takes_the_active_cell_with_it(page):
    """The other half: if the column it was on is gone, so is it — a ring
    on a column index that now means a different column is worse than no
    ring."""
    cols = _cols(page)
    name = cols[1]
    _click_cell(page, 2, name)
    page.evaluate("""(n) => {
      __winnow.S.layout[n] = { ...(__winnow.S.layout[n] || {}), hidden: true };
      __winnow.renderHead(); __winnow.render();
    }""", name)
    try:
        assert _focus(page) is None
        assert page.locator("#body .cell-active").count() == 0
    finally:
        page.evaluate("""(n) => {
          __winnow.S.layout[n] = { ...(__winnow.S.layout[n] || {}), hidden: false };
          __winnow.renderHead(); __winnow.render();
        }""", name)


def test_escape_puts_the_cell_cursor_away(page):
    cols = _cols(page)
    _click_cell(page, 2, cols[0])
    page.keyboard.press("Escape")
    assert _focus(page) is None
    assert page.locator("#body .cell-active").count() == 0


# ------------------------------------------------------- grouped mode

def test_arrows_step_over_group_headings(page):
    """A group heading owns a position but has no cells — grouping.js
    paints one spanning element for it. An active cell parked there would
    be a ring on nothing, and every consumer that asks which rows a range
    means skips headings anyway. So the arrows step over them, and the
    range a Shift run builds never starts or ends on one."""
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal .fb-groupby")
    page.locator("#modal .fb-groupby-add").select_option("EventId")
    page.wait_for_selector("#modal .fb-groupby-chip:has-text('EventId')")
    page.locator("#modal button", has_text="Apply").click()
    page.wait_for_function("() => (__winnow.S.groupByCols || []).includes('EventId')")
    try:
        # Open the first group so there are data rows to walk between the
        # headings; a collapsed board is all headings and nothing else.
        page.wait_for_function("() => __winnow.S.groups.length > 0", timeout=10_000)
        headings = page.evaluate("() => __winnow.S.groups.length")
        page.evaluate("() => __winnow.toggleGroup(0)")
        page.wait_for_function("(n) => __winnow.gridRowCount() > n", arg=headings, timeout=10_000)

        # Position 0 is the first group's heading. Starting the cursor
        # there is the case that matters: the first arrow has to find a row.
        page.evaluate("() => { __winnow.clearCellSelection(); __winnow.S.cursor = 0; }")
        assert page.evaluate("() => !__winnow.groupCoordAt(0)"), "position 0 should be a heading"
        page.keyboard.press("ArrowDown")
        # Wherever it landed, it is a row — never a heading.
        for _ in range(6):
            pos = _focus(page)["pos"]
            assert page.evaluate("(p) => !!__winnow.groupCoordAt(p)", pos), pos
            page.keyboard.press("ArrowDown")
        # And the far edge is a row too, not the last heading.
        page.keyboard.press("Control+ArrowDown")
        assert page.evaluate("(p) => !!__winnow.groupCoordAt(p)", _focus(page)["pos"])
    finally:
        page.evaluate("() => { __winnow.dropGrouping(); return __winnow.rebuildView({ keepScroll: false }); }")
        page.wait_for_function("() => (__winnow.S.groupByCols || []).length === 0")

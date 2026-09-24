"""Space over a cell rectangle picks the rows the rectangle covers.

Arrows and Shift+Arrow can build a cell range from the keyboard, and a
drag can build one from the mouse, but Space only ever toggled `S.cursor`
— one row, the one the active cell was standing on. On a rectangle four
rows tall that meant four rows highlighted and one row picked, and the
gap was silent: nothing in the toolbar said Space had ignored the other
three.

So the rule these tests pin is that Space toggles whatever the cell
selection says you are pointing at, and that a multi-row rectangle is ONE
block:

* All-or-nothing across the block, the way a header checkbox behaves.
  Anything short of every row picked fills the rest in; only a fully
  picked block is let go. Toggling each row independently is the obvious
  alternative and is wrong — on a half-picked block it swaps which half
  is picked, and pressing again swaps it back, so the block can never be
  made whole. `test_a_half_picked_block_fills_in` is that claim.
* The rectangle survives the press. That is the whole reason a second
  press can mean "take that back": Shift+Space spends the rectangle, and
  if Space did too there would be nothing left to let go of.
* Rows the rectangle does not cover are not its business, and one press
  is one Undo. Rows it DOES cover are the block whether or not this press
  picked them, so releasing takes a pick that predates the rectangle
  too — `test_a_pick_the_rectangle_covers_goes_with_the_block`. That is
  the all-or-nothing rule being consistent rather than leaking, and the
  toolbar's Undo is what restores the earlier state; the alternative
  (remember what each press added and release only that) is what a reader
  would assume, so it is pinned.

`test_a_mouse_drag_then_space` is the shape the report arrived in, and
the sharpest of them: a drag leaves `S.cursor` back at the mousedown row,
so the old code picked the row the drag STARTED on while the highlight
covered seven.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean(page):
    page.evaluate("""() => {
      __winnow.selClear();
      __winnow.clearCellSelection();
      __winnow.S.selHidden = 0;
      __winnow.S.selUndo.length = 0;
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
      __winnow.S.selUndo.length = 0;
      document.getElementById('body').scrollTop = 0;
      document.getElementById('body').scrollLeft = 0;
      __winnow.clearAllFilters();
    }""")


def _picked(page):
    return page.evaluate("() => __winnow.selPositions()")


def _range(page):
    return page.evaluate("() => __winnow.S.cellRange")


def _cell(page, row, col=1):
    return page.locator("#body .row").nth(row).locator(".cell").nth(col)


def _build_range(page, top, height, col=1):
    """A rectangle `height` rows tall with its top at `top`, built the way
    an analyst builds one: click a cell, then Shift+Down. The active cell
    — and with it S.cursor — ends up at the BOTTOM, which is what makes
    "Space picked one row" visible as the wrong row."""
    _cell(page, top, col).click()
    for _ in range(height - 1):
        page.keyboard.press("Shift+ArrowDown")
    page.wait_for_function(
        "(e) => __winnow.S.cellRange && __winnow.S.cellRange.r0 === e[0] && __winnow.S.cellRange.r1 === e[1]",
        arg=[top, top + height - 1],
    )


def test_space_picks_every_row_the_rectangle_covers(page):
    _build_range(page, 4, 4)
    assert _picked(page) == []                       # a rectangle picks nothing by itself
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5, 6, 7]
    # Still up, still the same rectangle: Space chose rows, it did not
    # spend the selection the way Shift+Space does.
    assert _range(page)["r0"] == 4 and _range(page)["r1"] == 7
    assert page.evaluate("() => __winnow.S.cellFocus.pos") == 7


def test_a_second_press_lets_the_whole_block_go(page):
    _build_range(page, 4, 4)
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5, 6, 7]
    page.keyboard.press(" ")
    assert _picked(page) == []
    assert _range(page)["r0"] == 4 and _range(page)["r1"] == 7


def test_a_half_picked_block_fills_in(page):
    """Grow a picked rectangle and press again. Every row is picked after,
    including the ones that already were — a per-row toggle would have
    swapped them for the two new ones and left the block half picked
    either way round."""
    _build_range(page, 4, 2)
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5]
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    page.wait_for_function("() => __winnow.S.cellRange.r1 === 7")
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5, 6, 7]
    # And now that it IS whole, the next press lets all four go.
    page.keyboard.press(" ")
    assert _picked(page) == []


def test_picks_outside_the_rectangle_are_left_alone(page):
    """The gutter clears a cell selection, so the pick has to come first —
    which is the honest order anyway: pick a row you spotted, then work a
    block somewhere else."""
    page.locator("#body .row").nth(1).locator(".gutter").click()
    assert _picked(page) == [1]
    _build_range(page, 5, 3)
    assert _picked(page) == [1]                      # building the rectangle kept it
    page.keyboard.press(" ")
    assert _picked(page) == [1, 5, 6, 7]
    page.keyboard.press(" ")
    assert _picked(page) == [1]                      # letting the block go is not clearing


def test_a_pick_the_rectangle_covers_goes_with_the_block(page):
    """The other side of the rule above. A row picked before the
    rectangle was drawn is still inside the block, so the release takes
    it with the rest — "all-or-nothing" means the block, not "the rows
    this press added". Undo is the thing that restores it, which is why
    every press snapshots first."""
    page.locator("#body .row").nth(6).locator(".gutter").click()
    assert _picked(page) == [6]
    _build_range(page, 4, 5)                         # rows 4-8, with 6 inside
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5, 6, 7, 8]          # 6 was already picked and stays
    page.keyboard.press(" ")
    assert _picked(page) == []                       # and goes with the block
    page.evaluate("() => { __winnow.selUndoLast(); __winnow.render(); }")
    assert _picked(page) == [4, 5, 6, 7, 8]


def test_one_press_is_one_undo(page):
    """Four rows picked by one keystroke come back off with one Undo —
    the toolbar's Undo, and the snapshot behind it, work in gestures and
    not in rows."""
    page.locator("#body .row").nth(1).locator(".gutter").click()
    _build_range(page, 5, 4)
    page.keyboard.press(" ")
    assert _picked(page) == [1, 5, 6, 7, 8]
    assert page.evaluate("() => __winnow.selUndoAvailable()")
    page.evaluate("() => { __winnow.selUndoLast(); __winnow.render(); }")
    assert _picked(page) == [1]


def test_a_mouse_drag_then_space(page):
    """The report's own shape. A drag leaves S.cursor at the row the
    mouse went DOWN on, so the row Space used to toggle was the top of
    the highlight, not the bottom and not all of it — and dragging upward
    made it the bottom instead, which is how "it toggles the wrong row"
    reads from the outside."""
    a = _cell(page, 3, 1).bounding_box()
    b = _cell(page, 9, 2).bounding_box()
    page.mouse.move(a["x"] + 10, a["y"] + 8)
    page.mouse.down()
    page.mouse.move(b["x"] + 10, b["y"] + 8, steps=6)
    page.mouse.up()
    page.wait_for_function("() => __winnow.S.cellRange && __winnow.S.cellRange.r1 === 9")
    assert page.evaluate("() => __winnow.S.cursor") == 3      # the drag never moved it
    page.keyboard.press(" ")
    assert _picked(page) == [3, 4, 5, 6, 7, 8, 9]


def test_a_held_key_does_not_re_toggle_the_block(page):
    """OS auto-repeat sends a press every ~30ms while the key is down.
    Space is a toggle and the rectangle deliberately survives it, so
    without a guard a held key flips the whole block on and off until
    release and what you are left with is whichever repeat landed last.
    On a rectangle reaching the end of the view each of those repeats
    walks every position in it, which is the cost that makes ignoring
    them worth a line.

    Dispatched rather than pressed: Playwright's keyboard never sets
    `repeat`, so a real press cannot reproduce what the OS sends."""
    _build_range(page, 4, 4)
    page.keyboard.press(" ")
    assert _picked(page) == [4, 5, 6, 7]
    page.evaluate("""() => {
      document.body.dispatchEvent(new KeyboardEvent('keydown',
        { key: ' ', repeat: true, bubbles: true, cancelable: true }));
    }""")
    assert _picked(page) == [4, 5, 6, 7]              # the repeat did nothing
    page.keyboard.press(" ")                          # a real press still does
    assert _picked(page) == []


def test_a_single_row_rectangle_still_toggles_that_row(page):
    """One row wide or ten columns wide, a one-row rectangle is the cursor
    row and Space means what it always meant. The block rule only has
    something to say once a rectangle spans more than one row."""
    _cell(page, 4, 1).click()
    page.keyboard.press(" ")
    assert _picked(page) == [4]
    page.keyboard.press(" ")
    assert _picked(page) == []
    # Widened across columns, same row: still one row.
    page.keyboard.press("Shift+ArrowRight")
    page.keyboard.press("Shift+ArrowRight")
    page.wait_for_function("() => __winnow.S.cellRange.c1 > __winnow.S.cellRange.c0")
    page.keyboard.press(" ")
    assert _picked(page) == [4]


def test_shift_space_still_only_adds_and_spends_the_rectangle(page):
    """Unchanged, and worth pinning next to the gesture that now overlaps
    it: Shift+Space never un-picks, which is what makes it the way to
    gather several rectangles into one set of picks. Space over the second
    rectangle would have let the first one's overlap go."""
    _build_range(page, 4, 3)
    page.keyboard.press("Shift+ ")
    assert _picked(page) == [4, 5, 6]
    assert _range(page) is None                      # spent, unlike Space
    _build_range(page, 5, 4)
    page.keyboard.press("Shift+ ")
    assert _picked(page) == [4, 5, 6, 7, 8]
    assert _range(page) is None


def test_the_far_edge_jump_picks_the_whole_column_of_rows(page):
    """Ctrl+Shift+Down makes the rectangle the rest of the table, and
    Space has to mean all of it — this is the press where the difference
    between "the cursor row" and "the block" is thousands of rows."""
    total = page.evaluate("() => __winnow.gridRowCount()")
    _cell(page, 2, 1).click()
    page.keyboard.press("Control+Shift+ArrowDown")
    page.wait_for_function("(n) => __winnow.S.cellRange.r1 === n - 1", arg=total)
    page.keyboard.press(" ")
    assert page.evaluate("() => __winnow.selCount()") == total - 2
    page.keyboard.press(" ")
    assert page.evaluate("() => __winnow.selCount()") == 0


def test_group_headings_inside_the_block_are_not_picked(page):
    """A heading owns a position but is not a row: nothing can tag it or
    copy it, and `cellRangeRows` has always skipped them. The block rule
    asks "is every row picked", so a heading that answered would make a
    block that can never be whole — and the press that follows would
    never be able to let it go."""
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal .fb-groupby")
    page.locator("#modal .fb-groupby-add").select_option("EventId")
    page.wait_for_selector("#modal .fb-groupby-chip:has-text('EventId')")
    page.locator("#modal button", has_text="Apply").click()
    page.wait_for_function("() => (__winnow.S.groupByCols || []).includes('EventId')")
    try:
        page.wait_for_function("() => __winnow.S.groups.length > 1", timeout=10_000)
        headings = page.evaluate("() => __winnow.S.groups.length")
        page.evaluate("async () => { await __winnow.toggleGroup(0); }")
        page.wait_for_function("(n) => __winnow.gridRowCount() > n", arg=headings, timeout=10_000)
        opened = page.evaluate("() => __winnow.gridRowCount()")
        page.evaluate("""async () => {
          const gi = __winnow.S.groups.findIndex((g, i) => i > 0);
          await __winnow.toggleGroup(gi);
        }""")
        page.wait_for_function("(n) => __winnow.gridRowCount() > n", arg=opened, timeout=10_000)

        # Start on the last row of the first open group, so the rectangle
        # the arrows build has to cross the next group's heading.
        last_row = page.evaluate("""() => {
          const n = __winnow.gridRowCount();
          for (let p = 0; p < n - 1; p++) {
            if (__winnow.groupCoordAt(p) && !__winnow.groupCoordAt(p + 1)) return p;
          }
          return null;
        }""")
        assert last_row is not None
        page.evaluate("(p) => { __winnow.clearCellSelection(); __winnow.S.cursor = p; __winnow.render(); }", last_row)
        page.keyboard.press("ArrowUp")               # cold-start the cell cursor on a real row
        page.keyboard.press("ArrowDown")
        for _ in range(4):
            page.keyboard.press("Shift+ArrowDown")
        r0, r1 = page.evaluate("() => [__winnow.S.cellRange.r0, __winnow.S.cellRange.r1]")
        spanned = list(range(r0, r1 + 1))
        real = [p for p in spanned if page.evaluate("(p) => !!__winnow.groupCoordAt(p)", p)]
        assert len(real) < len(spanned), "expected a heading inside the rectangle"

        page.keyboard.press(" ")
        assert _picked(page) == real                 # the headings were stepped over, not picked
        # And the block is whole, so the next press lets it go rather than
        # stalling on a heading that can never be picked.
        page.keyboard.press(" ")
        assert _picked(page) == []
    finally:
        page.evaluate("() => { __winnow.dropGrouping(); return __winnow.rebuildView({ keepScroll: false }); }")
        page.wait_for_function("() => (__winnow.S.groupByCols || []).length === 0")

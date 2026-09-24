"""The row-selection model: the whole gutter is the handle, ranges add
rather than replace, a drag selects the span it covers however fast it
moves, picks survive cell clicks, arrow keys, a sort and a filter, and the
toolbar chip reports and undoes.

Arrow keys drive the CELL cursor (see test_cell_keyboard.py); what this
file cares about is that they leave the row picks alone, and that
Shift+Space — the gesture that turns a cell rectangle into picks without
toggling — adds to them rather than replacing them. Space turns a
rectangle into picks too, as a toggle and all-or-nothing; that rule and
its edges live in test_space_over_a_cell_range.py."""
import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean(page):
    page.evaluate("() => { __winnow.selClear(); __winnow.S.selHidden = 0; __winnow.S.selUndo.length = 0; __winnow.render(); }")
    yield
    page.evaluate("() => { __winnow.selClear(); __winnow.S.cellRange = null; __winnow.S.selHidden = 0; __winnow.render(); }")
    page.evaluate("() => __winnow.clearAllFilters()")


def _gutter(page, n):
    return page.locator("#body .row").nth(n).locator(".gutter")


def _picked(page):
    return page.evaluate("() => __winnow.selPositions()")


def test_anywhere_in_the_gutter_picks_and_ranges_add(page):
    _gutter(page, 2).click(position={"x": 50, "y": 10})          # the strip between box and number
    _gutter(page, 5).click(position={"x": 50, "y": 10}, modifiers=["Shift"])
    assert _picked(page) == [2, 3, 4, 5]
    _gutter(page, 9).click(modifiers=["Control"])                 # Ctrl+click on the gutter: a plain toggle, kept
    _gutter(page, 12).click(modifiers=["Shift"])                  # Shift adds a run from the anchor (9)
    assert _picked(page) == [2, 3, 4, 5, 9, 10, 11, 12]
    _gutter(page, 4).click(modifiers=["Shift", "Control"])        # Ctrl+Shift removes the run from the anchor (9, the last plain pick) to 4
    assert _picked(page) == [2, 3, 10, 11, 12]
    _gutter(page, 10).click(); _gutter(page, 12).click(modifiers=["Shift", "Control"])   # a plain pick moves the anchor
    assert _picked(page) == [2, 3]
    assert "2 selected" in page.locator("#tagToolbar").inner_text()


def test_right_click_outside_the_picks_targets_that_row_and_keeps_them(page, row_menu):
    _gutter(page, 1).click(); _gutter(page, 2).click()
    row_menu(row=8)
    assert "THIS ROW" in page.locator(".menu .menu-item-sub").first.inner_text().upper()
    page.keyboard.press("Escape")
    assert _picked(page) == [1, 2]


def test_a_fast_drag_selects_the_whole_span_and_shrinks_back(page):
    b3 = _gutter(page, 2).bounding_box(); b12 = _gutter(page, 11).bounding_box(); b6 = _gutter(page, 5).bounding_box()
    page.mouse.move(b3["x"] + 40, b3["y"] + 12)
    page.mouse.down()
    page.mouse.move(b12["x"] + 40, b12["y"] + 12, steps=1)       # one jump: nothing in between is "touched"
    page.wait_for_function("() => __winnow.selCount() === 10")
    assert _picked(page) == list(range(2, 12))
    page.mouse.move(b6["x"] + 40, b6["y"] + 12, steps=1)
    page.mouse.up()
    page.wait_for_function("() => __winnow.selCount() === 4")
    assert _picked(page) == [2, 3, 4, 5]


def test_picks_survive_cell_clicks_and_arrows_and_escape_lets_go(page):
    _gutter(page, 1).click(); _gutter(page, 3).click()
    page.locator("#body .row").nth(6).locator(".cell").nth(1).click()
    page.keyboard.press("ArrowDown"); page.keyboard.press("ArrowDown")
    assert _picked(page) == [1, 3] and page.evaluate("() => __winnow.S.cursor") == 8
    page.keyboard.press("Escape")
    assert _picked(page) == [] and page.evaluate("() => __winnow.S.cellRange") is None


def test_space_and_shift_space(page):
    """One cell clicked is a one-row rectangle, which is the cursor row,
    so Space means here what it has always meant. The multi-row case is
    test_space_over_a_cell_range.py."""
    page.locator("#body .row").nth(4).locator(".cell").nth(1).click()
    page.keyboard.press(" ")
    assert _picked(page) == [4]
    page.keyboard.press(" ")
    assert _picked(page) == []
    page.locator("#body .row").nth(6).locator(".cell").nth(1).click()
    page.locator("#body .row").nth(8).locator(".cell").nth(2).click(modifiers=["Shift"])
    assert _picked(page) == []                                            # a cell range picks no rows by itself
    assert "3 rows in the cell range" in page.locator("#tagToolbar").inner_text()
    page.keyboard.press("Shift+ ")
    assert _picked(page) == [6, 7, 8] and page.evaluate("() => __winnow.S.cellRange") is None


def test_shift_arrows_build_a_range_that_picks_without_eating_earlier_picks(page):
    """Shift+Arrow grows the CELL rectangle now, not the row picks — the
    Excel gesture won the chord. The claim this test was written for
    survives the change and is what is asserted: the rows an analyst adds
    by extending never swallow the ones they picked earlier.

    The extra keystroke is Space (a toggle over the whole rectangle) or
    Shift+Space (which only adds, and is what this test presses, since
    "never swallows earlier picks" is the claim). Either is the
    documented replacement for the old chord."""
    _gutter(page, 1).click()
    page.locator("#body .row").nth(5).locator(".cell").nth(1).click()
    assert _picked(page) == [1]                                           # a cell click picks no rows
    page.keyboard.press("Shift+ArrowDown"); page.keyboard.press("Shift+ArrowDown"); page.keyboard.press("Shift+ArrowDown")

    # The rectangle grew and the picks did not move.
    assert page.evaluate("() => __winnow.S.cellRange.r0") == 5
    assert page.evaluate("() => __winnow.S.cellRange.r1") == 8
    assert _picked(page) == [1]

    page.keyboard.press("Shift+ArrowUp")                                  # the rectangle shrinks
    assert page.evaluate("() => __winnow.S.cellRange.r1") == 7

    page.keyboard.press("Shift+ ")                                        # …and becomes picks, adding
    assert _picked(page) == [1, 5, 6, 7]                                  # row 1 stays


def test_picks_survive_a_sort_and_a_filter_and_the_chip_counts_the_hidden(page):
    _gutter(page, 0).click(); _gutter(page, 4).click(); _gutter(page, 8).click()     # rids 1, 5, 9
    rids = page.evaluate("() => __winnow.selPositions().map((p) => __winnow.S.rowsByPos.get(p).rid)")
    assert rids == [1, 5, 9]
    page.locator('.hcell[data-col="Timestamp"] .label').click()                    # sort toggles
    page.wait_for_function("() => __winnow.selCount() === 3 && __winnow.S.selHidden === 0", timeout=10_000)
    assert sorted(page.evaluate("() => __winnow.selPositions().map((p) => __winnow.S.rowsByPos.get(p) && __winnow.S.rowsByPos.get(p).rid).filter(Boolean)")) == [1, 5, 9]
    page.locator('.fcell input[data-col="Host"]').fill("=H0")                       # keeps rids 1, 6, 11, … → of ours only rid 1
    page.wait_for_function("() => __winnow.S.selHidden === 2 && __winnow.selCount() === 1", timeout=10_000)
    assert "filtered out" in page.locator("#tagToolbar").inner_text()


def test_undo_takes_back_the_last_gesture(page):
    _gutter(page, 2).click(); _gutter(page, 3).click()
    page.locator("#tagToolbar button", has_text="Undo").click()
    assert _picked(page) == [2]
    page.locator("#tagToolbar button", has_text="Invert").click()
    assert page.evaluate("() => __winnow.S.selectAll") is True and page.evaluate("() => __winnow.selCount()") == 199
    page.locator("#tagToolbar button", has_text="Undo").click()
    assert _picked(page) == [2]


def test_a_tag_from_the_row_menu_outside_the_picks_tags_that_row_only(page, row_menu, flyout):
    """The menu's header says "this row" when the right-click lands outside
    the picks — and the tag entries used to tag the picks anyway."""
    _gutter(page, 1).click(); _gutter(page, 2).click()
    try:
        row_menu(row=8)
        flyout("Tag this row").locator(".menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(8) || { tags: [] }).tags.length === 1")
        assert page.evaluate("() => [1, 2].map((p) => (__winnow.rowAt(p) || { tags: [] }).tags.length)") == [0, 0]
        assert _picked(page) == [1, 2]
        page.keyboard.press("Escape")
        # …and from inside the picks, the whole selection
        row_menu(row=1)
        flyout("Tag").locator(".menu-item").first.click()
        page.wait_for_function("() => [1, 2].every((p) => (__winnow.rowAt(p) || { tags: [] }).tags.length === 1)")
        page.keyboard.press("Escape")
    finally:
        page.evaluate("() => __winnow.tagRowsAtPositions(__winnow.S.tags[0], [1, 2, 8], false)")
        page.wait_for_function("() => [1, 2, 8].every((p) => (__winnow.rowAt(p) || { tags: [] }).tags.length === 0)")


def test_undo_does_not_outlive_the_view_that_gave_it_positions(page):
    _gutter(page, 2).click(); _gutter(page, 3).click()
    assert page.locator("#tagToolbar button", has_text="Undo").count() == 1
    page.locator('.hcell[data-col="Timestamp"] .label').click()      # a sort: new positions, same rows
    page.wait_for_function("() => __winnow.selCount() === 2 && __winnow.S.selUndo.length === 0", timeout=10_000)
    assert page.locator("#tagToolbar button", has_text="Undo").count() == 0


def test_select_all_from_the_keyboard_can_be_undone(page):
    _gutter(page, 2).click()
    page.keyboard.press("Control+a")
    page.wait_for_function("() => __winnow.S.selectAll === true")
    page.locator("#tagToolbar button", has_text="Undo").click()
    assert _picked(page) == [2]


def test_space_on_a_focused_button_is_the_buttons_not_a_row_toggle(page):
    _gutter(page, 2).click(); _gutter(page, 3).click()
    page.locator("#tagToolbar button", has_text="Undo").focus()
    page.keyboard.press("Space")      # the button's own Space: it activates Undo, nothing toggles
    page.wait_for_timeout(150)
    assert _picked(page) == [2]


def test_picks_survive_two_rebuilds_in_quick_succession(page):
    """A rebuild that starts while the previous one is still putting the
    picks back (the position lookup is a round trip) used to find them
    cleared and carry nothing."""
    _gutter(page, 0).click(); _gutter(page, 4).click(); _gutter(page, 8).click()
    page.evaluate("""() => { window.__realFetch = window.fetch;
      window.fetch = (u, o) => String(u).includes('/api/view/positions')
        ? new Promise((r) => setTimeout(r, 400)).then(() => window.__realFetch(u, o)) : window.__realFetch(u, o); }""")
    try:
        page.evaluate("() => { __winnow.rebuildView(); }")
        page.wait_for_function("() => __winnow.selCount() === 0", timeout=10_000)   # cleared; the lookup is in flight
        page.evaluate("() => { __winnow.rebuildView(); }")
        page.wait_for_function("() => __winnow.selCount() === 3 && __winnow.S.selHidden === 0", timeout=10_000)
    finally:
        page.evaluate("() => { window.fetch = window.__realFetch; }")

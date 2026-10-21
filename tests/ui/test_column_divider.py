"""The line between two column headers: drag it to resize, double-click it to
fit the column to its contents.

What broke: the grab was a 7px .grip meant to straddle the line, but the
header's own `overflow: hidden` clipped it to the 4px LEFT of it. The line
itself, and everything to its right, belonged to the headers — so the
double-click an analyst aimed at the line landed on a header and sorted
that column twice instead of fitting the one they meant. These press where
an analyst aims (on the line, and a few pixels either side of it) and
assert what the press did, not how the zone is built.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _keep(page):
    """Hold on to the layout and sort as found. Resizing saves the whole
    layout to the case every module in the shard shares, so each test puts
    them back through _restore."""
    page.evaluate("""() => { window.__dividerKept = JSON.stringify(
      { layout: __winnow.S.layout, sort: __winnow.S.sort }); }""")


def _restore(page, layout_saved):
    page.evaluate("""() => {
      const kept = JSON.parse(window.__dividerKept);
      const L = __winnow.S.layout;
      for (const k of Object.keys(L)) delete L[k];
      Object.assign(L, kept.layout);
      __winnow.S.sort = kept.sort;
      __winnow.closeMenu();
      __winnow.renderHead(); __winnow.render();
      __winnow.saveLayout();
      document.getElementById('body').scrollLeft = 0;
    }""")
    layout_saved()


def _prepare(page, w=None, **widths):
    """Every column shown, unpinned, `w` px wide if given (or as `widths`
    names it), and no sort recorded — so a press that lands on a header
    rather than a divider shows up as a sort. In memory only; _restore
    saves what was found. Other modules hide, pin and reorder columns, and
    the layout remembers, so nothing here assumes where a column is."""
    page.evaluate("""([w, widths]) => {
      for (const c of __winnow.S.columns) {
        const was = __winnow.S.layout[c.name] || {};
        __winnow.S.layout[c.name] = { ...was, hidden: false, pinned: false,
          ...(widths[c.name] ? { w: widths[c.name] } : w ? { w } : {}) };
      }
      __winnow.S.sort = [];
      __winnow.renderHead(); __winnow.render();
      document.getElementById('body').scrollLeft = 0;
    }""", [w, widths])


def _line(page, col):
    """Where a column's divider is drawn: the middle of its header cell's
    last pixel (the 1px right border), halfway down the header row."""
    return page.evaluate("""(col) => {
      const r = document.querySelector(`.hcell[data-col="${col}"]`).getBoundingClientRect();
      return [r.right - 0.5, r.top + r.height / 2];
    }""", col)


def _width(page, col):
    return page.evaluate(
        "(c) => document.querySelector(`.hcell[data-col=\"${c}\"]`).getBoundingClientRect().width", col)


def _right_of(page, col):
    """The column whose header is drawn right after `col`'s, or None."""
    return page.evaluate("""(c) => {
      const n = document.querySelector(`.hcell[data-col="${c}"]`).nextElementSibling;
      return n ? n.dataset.col : null;
    }""", col)


@pytest.mark.parametrize("dx", [-4, 0, 4], ids=["just-left-of-the-line", "on-the-line", "just-right-of-the-line"])
def test_double_click_at_the_divider_fits_the_column_instead_of_sorting(page, layout_saved, dx):
    _keep(page)
    try:
        _prepare(page, EventId=300)
        x, y = _line(page, "EventId")
        page.mouse.dblclick(x + dx, y)
        # Whichever happens first: the fit, or the sort a header would do.
        page.wait_for_function("() => __winnow.S.layout.EventId.w !== 300 || __winnow.S.sort.length")
        assert page.evaluate("() => __winnow.S.sort") == [], (
            f"a double-click {dx:+d}px from the divider sorted a column instead of fitting EventId")
        # Four-digit ids under a short name: fitted, it is a fraction of 300.
        assert _width(page, "EventId") < 200
    finally:
        _restore(page, layout_saved)


def test_dragging_from_either_side_of_the_line_resizes_and_never_reorders(page, layout_saved):
    _keep(page)
    try:
        _prepare(page, 150)
        order = page.evaluate("() => [...__winnow.S.order]")
        for dx in (-4, 4):
            before = _width(page, "EventId")
            x, y = _line(page, "EventId")
            page.mouse.move(x + dx, y)
            page.mouse.down()
            page.mouse.move(x + dx + 60, y, steps=6)
            page.mouse.up()
            grew = _width(page, "EventId") - before
            assert abs(grew - 60) <= 1, f"a drag from {dx:+d}px of the divider widened EventId by {grew}px, not 60"
        assert page.evaluate("() => __winnow.S.order") == order, "the drag reordered the columns"
        assert page.evaluate("() => __winnow.S.sort") == []
    finally:
        _restore(page, layout_saved)


def test_a_header_still_sorts_and_opens_its_menu_right_up_to_its_name(page, layout_saved):
    """The zone is for the line, not the headers either side of it: a click
    on a column's name sorts it however close to a divider the name runs,
    and a right-click on the divider is still a header's column menu."""
    _keep(page)
    try:
        _prepare(page, EventId=300)
        nxt = _right_of(page, "EventId")
        assert nxt, "EventId is the last column, so there is no header right of its divider to click"
        # Fitted, EventId's name runs as close to its divider as it ever does.
        x, y = _line(page, "EventId")
        page.mouse.dblclick(x, y)
        page.wait_for_function("() => __winnow.S.layout.EventId.w !== 300 || __winnow.S.sort.length")
        assert page.evaluate("() => __winnow.S.sort") == [], "double-clicking the divider sorted instead of fitting"

        left = page.locator('.hcell[data-col="EventId"] .label').bounding_box()
        page.mouse.click(left["x"] + left["width"] - 1, y)
        page.wait_for_function("() => __winnow.S.sort.length")
        assert page.evaluate("() => __winnow.S.sort[0].column") == "EventId"

        right = page.locator(f'.hcell[data-col="{nxt}"] .label').bounding_box()
        page.mouse.click(right["x"] + 1, y)
        page.wait_for_function("(c) => __winnow.S.sort[0].column === c", arg=nxt)

        x, y = _line(page, "EventId")
        page.mouse.click(x + 3, y, button="right")
        page.wait_for_selector(".menu .menu-item:has-text('Derive a column from this…')")
    finally:
        _restore(page, layout_saved)


def test_the_line_between_two_pinned_columns_fits_the_left_one(page, layout_saved, painted):
    """A pinned header is drawn over whatever scrolls under it, and two pins
    park side by side whatever lies between them in the column order — so
    the line between them is the left pin's edge, with the right pin, not
    the left pin's neighbour in the order, drawn just past it."""
    _keep(page)
    try:
        # The rest wide enough that the grid scrolls far past both pins'
        # own places; the pins narrow enough to park side by side.
        _prepare(page, 600, EventId=150, Host=150)
        for col in ("EventId", "Host"):
            page.locator(f'.hcell[data-col="{col}"] .label').click(modifiers=["Alt"])
            page.wait_for_selector(f'.hcell.pinned[data-col="{col}"]')
        page.evaluate("() => { __winnow.S.sort = []; __winnow.renderHead(); }")
        first, second = page.evaluate("""() => __winnow.visibleCols()
          .filter((c) => c === 'EventId' || c === 'Host')""")
        page.evaluate("() => { const b = document.getElementById('body'); b.scrollLeft = b.scrollWidth; }")
        painted(page)
        a = page.locator(f'.hcell[data-col="{first}"]').bounding_box()
        b = page.locator(f'.hcell[data-col="{second}"]').bounding_box()
        assert abs(b["x"] - (a["x"] + a["width"])) < 1, f"the two pins are not side by side: {a} {b}"

        x, y = _line(page, first)
        page.mouse.dblclick(x + 4, y)
        page.wait_for_function("(c) => __winnow.S.layout[c].w !== 150 || __winnow.S.sort.length", arg=first)
        assert page.evaluate("() => __winnow.S.sort") == [], (
            f"a double-click on the line between the pins sorted {second} instead of fitting {first}")
        assert _width(page, first) != 150
    finally:
        _restore(page, layout_saved)

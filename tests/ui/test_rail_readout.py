"""The tag rail says which tag a mark belongs to — without covering the
grid to do it.

The rail was the one surface in the app where a tag appeared as a colour
and nothing else: 14px of dashes down the right edge, no name anywhere
near it. It could not have said, either — it lay across #body's vertical
scrollbar, so it carried `pointer-events: none` to keep a grab of the
thumb from landing on 14px of canvas, and an element the pointer never
reaches shows no `title`.

What bought the title back is the gutter: #body gives up a rail's width
and the strip stands in it, so it overlays neither the thumb nor the
rows. Sliding the strip inward instead, by measuring #body's scrollbar on
every draw, is the version that has to keep failing here — it puts the
canvas on top of the rightmost column of cells, and it goes stale the
moment something repaints without redrawing the rail (expanding a group
is one). So the geometry tests are as much a part of the readout as the
hover is: the tooltip is only safe while nothing is under the strip.

The case file is shared by the whole UI session, so the test that tags a
row untags it again.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

TAG = "__winnow.S.tags[0]"

# Half way down the fixture view: a mark at the canvas's midpoint, far
# from either edge, so the hover has slack in both directions.
POS = 100

GEOMETRY = """() => {
  const g = document.getElementById('grid'), b = document.getElementById('body'),
        r = document.getElementById('rail');
  const gb = g.getBoundingClientRect(), bb = b.getBoundingClientRect(),
        rb = r.getBoundingClientRect();
  // What the browser hands the pointer just inside the scroller's right
  // edge — the pixels the rail used to sit on. `null` when nothing is
  // there, the id/class when it is something other than the grid.
  const at = document.elementFromPoint(bb.right - 2, bb.top + bb.height / 2);
  return { gridTop: gb.top, gridBottom: gb.bottom, gridRight: gb.right,
           bodyTop: bb.top, bodyRight: bb.right,
           railLeft: rb.left, railRight: rb.right, railTop: rb.top,
           railBottom: rb.bottom, railWidth: rb.width,
           bitmap: [r.width, r.height], box: [r.clientWidth, r.clientHeight],
           inlineStyle: r.getAttribute('style') || '',
           overflowing: b.scrollHeight > b.clientHeight,
           atBodyEdge: at ? (b.contains(at) ? 'grid' : (at.id || at.className)) : null };
}"""

# A y on the canvas with nothing painted within 8px of it — read out of the
# pixels rather than worked out from the positions, so it stays true
# whatever else the shared case happens to have tagged. -1 when the rail is
# too crowded to have one, which is a fact worth failing on here.
EMPTY_Y = """() => {
  const c = document.getElementById('rail');
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  const lit = new Uint8Array(c.height);
  for (let y = 0; y < c.height; y++)
    for (let x = 0; x < c.width; x++)
      if (d[(y * c.width + x) * 4 + 3] > 0) { lit[y] = 1; break; }
  for (let y = 8; y < c.height - 8; y++) {
    let clear = true;
    for (let k = -8; k <= 8; k++) if (lit[y + k]) { clear = false; break; }
    if (clear) return y;
  }
  return -1;
}"""


def _assert_clear_of_the_grid(g):
    """The one thing that must hold however the grid is scrolled, grouped
    or sized: the strip begins where the scroller ends. Everything the
    rail is allowed to do with the pointer rests on this."""
    assert g["railLeft"] >= g["bodyRight"] - 0.5, g
    assert abs(g["railRight"] - g["gridRight"]) <= 0.5, g
    # Given room rather than squeezed into the margin of error: a rail
    # sitting flush at zero width would satisfy the line above.
    assert g["railWidth"] >= 13.5, g
    # And the last pixels of the scroller still answer as the scroller.
    # This is the symptom the covered-up version had: a mousedown there
    # found no .cell, so it selected nothing, and a right-click got
    # Chromium's own menu instead of the row menu.
    assert g["atBodyEdge"] == "grid", g


def test_the_rail_stands_beside_the_grid_not_on_it(page):
    page.evaluate("() => __winnow.drawRail()")
    g = page.evaluate(GEOMETRY)
    _assert_clear_of_the_grid(g)
    # Placed by the stylesheet alone — top to bottom of .grid, beside the
    # scroll track it maps, with no per-draw JS writing top/right/bottom.
    # An inline style here means the measuring version is back.
    assert g["inlineStyle"] == "", g
    # Top to bottom of .grid. Worth stating as the whole height and not
    # "tall enough": with `top: 0; bottom: 0` and no `height`, a canvas —
    # a replaced element — takes its intrinsic height from the attribute
    # and drops `bottom` on the floor, which leaves the strip 150px long
    # and every mark crammed into the top sixth of the grid.
    assert abs(g["railTop"] - g["gridTop"]) <= 0.5, g
    assert abs(g["railBottom"] - g["gridBottom"]) <= 0.5, g
    # And the bitmap is the box, so a mark is one device pixel per CSS
    # pixel and the hover's arithmetic is the paint's.
    assert g["bitmap"] == g["box"], g


def test_the_rail_stays_clear_when_a_group_expands(page):
    """The path that broke the measured version. Grouped by a column with
    four values the grid does not overflow, so a draw taken then reads a
    scrollbar of zero; expanding a group calls render() and nothing else,
    which brings the scrollbar in without redrawing the rail. A strip
    placed from a stale measurement is over the thumb from here on."""
    page.evaluate("() => __winnow.addGroupLevel('EventId')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    try:
        page.evaluate("() => __winnow.drawRail()")
        # Four collapsed headers don't fill the viewport — this is the draw
        # that would have measured a scrollbar of zero and kept it.
        assert not page.evaluate(GEOMETRY)["overflowing"], "grouped view already overflows"
        page.evaluate("async () => { if (!__winnow.S.groups[0].expanded)"
                      " await __winnow.toggleGroup(0); }")
        page.wait_for_function("() => __winnow.S.groups[0].expanded")
        page.wait_for_function("() => { const b = document.getElementById('body');"
                               " return b.scrollHeight > b.clientHeight; }")
        _assert_clear_of_the_grid(page.evaluate(GEOMETRY))
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_a_mark_names_its_tag_under_the_pointer(page):
    name = page.evaluate(f"() => {TAG}.name")
    page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], true)")
    try:
        # evaluate awaits the promise, so the canvas has finished painting
        # (and the mark map is rebuilt) before the pointer moves.
        page.evaluate("() => __winnow.drawRail()")
        box = page.locator("#rail").bounding_box()
        total = page.evaluate("() => __winnow.S.view.row_count")
        page.mouse.move(box["x"] + box["width"] / 2,
                        box["y"] + box["height"] * (POS / total))
        title = page.locator("#rail").get_attribute("title")
        assert name in title, title
        # And which rows it stands for. Read as a span, not as a literal:
        # at 200 rows over ~700px of strip the rows either side of this one
        # are within the hover's slack, and whether they are tagged is the
        # shared case's business.
        m = re.search(r"rows? ([\d,]+)(?:–([\d,]+))?", title)
        assert m, title
        lo = int(m.group(1).replace(",", ""))
        hi = int((m.group(2) or m.group(1)).replace(",", ""))
        assert lo <= POS + 1 <= hi, title

        empty = page.evaluate(EMPTY_Y)
        assert empty > 0, "the rail is fully painted; nowhere to test the resting state"
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + empty)
        assert "Tagged rows in this view" in page.locator("#rail").get_attribute("title")
    finally:
        page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], false)")


def test_the_readout_follows_the_strip_after_a_resize_with_no_redraw(page):
    """Dragging the detail pane's divider resizes the grid and calls
    nothing (detail.js) — so the rail's box changes while its bitmap does
    not, and the browser stretches the painted marks to fit. The resize is
    done here by hand for the same reason: what has to be true is that a
    hover reads the strip as it is on screen now, not as it was painted.
    Without the scale the readout names whatever row is that many *canvas*
    rows down, which after a halving is the wrong half of the view."""
    name = page.evaluate(f"() => {TAG}.name")
    page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], true)")
    try:
        page.evaluate("() => __winnow.drawRail()")
        total = page.evaluate("() => __winnow.S.view.row_count")
        page.evaluate("""() => {
          const cv = document.getElementById('rail');
          cv.style.bottom = 'auto';
          cv.style.height = (cv.getBoundingClientRect().height / 2) + 'px';
        }""")
        box = page.locator("#rail").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2,
                        box["y"] + box["height"] * (POS / total))
        assert name in page.locator("#rail").get_attribute("title")
    finally:
        page.evaluate("() => { const cv = document.getElementById('rail');"
                      " cv.style.bottom = ''; cv.style.height = ''; }")
        page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], false)")


def test_a_wheel_over_the_rail_scrolls_the_grid(page):
    """Nothing scrolls on its own here: the rail is not inside #body, so
    without the forwarding handler the right-hand 14px of the grid is a
    strip where the wheel does nothing at all."""
    before = page.evaluate("() => document.getElementById('body').scrollTop")
    box = page.locator("#rail").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 400)
    page.wait_for_function("(b) => document.getElementById('body').scrollTop > b", arg=before)

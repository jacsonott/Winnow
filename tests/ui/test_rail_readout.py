"""The tag rail says which tag a mark belongs to.

The rail was the one surface in the app where a tag appeared as a colour
and nothing else: 14px of dashes down the right edge, no name anywhere
near it. It could not have said, either — it lay across #body's vertical
scrollbar, so it carried `pointer-events: none` to keep a grab of the
thumb from landing on 14px of canvas, and an element the pointer never
reaches shows no `title`.

So these three go together, and all three are the fix: the strip is
parked inside the scrollbar, the mark under the pointer names its tag,
and the wheel is forwarded by hand because the rail is a sibling of the
scroller rather than a child of it.

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
  const b = document.getElementById('body'), r = document.getElementById('rail');
  const bb = b.getBoundingClientRect(), rb = r.getBoundingClientRect();
  return { scrollbar: b.offsetWidth - b.clientWidth, head: __winnow.headH(),
           bodyRight: bb.right, bodyTop: bb.top, railRight: rb.right, railTop: rb.top };
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


def test_the_rail_stops_where_the_scrollbar_starts(page):
    """Asserted against the measurement rather than a constant: headless
    Chromium's scrollbars are overlays and take no width, so the number
    here is 0 in CI and ~11px in the Edge/Chromium app window an analyst
    runs — what has to hold on both is that the rail gives up exactly what
    the scrollbar takes."""
    page.evaluate("() => __winnow.drawRail()")
    g = page.evaluate(GEOMETRY)
    assert abs(g["railRight"] - (g["bodyRight"] - g["scrollbar"])) <= 0.5, g
    # And below the sticky header, so the strip spans the rows and a mark
    # sits beside the row it stands for.
    assert g["railTop"] >= g["bodyTop"] + g["head"] - 0.5, g


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


def test_a_wheel_over_the_rail_scrolls_the_grid(page):
    """Nothing scrolls on its own here: the rail is not inside #body, so
    without the forwarding handler the right-hand 14px of the grid is a
    strip where the wheel does nothing at all."""
    before = page.evaluate("() => document.getElementById('body').scrollTop")
    box = page.locator("#rail").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 400)
    page.wait_for_function("(b) => document.getElementById('body').scrollTop > b", arg=before)

"""The grid's vertical scrollbar starts below the column headers, and the
headers run all the way across to the tag rail.

#body's own scrollbar ran the full height of the scroller, up beside the
sticky header that lives inside it — which is how it was reported: the
scrollbar on the right protruding up into the table headers. Shortening
that bar's track (a ::-webkit-scrollbar-track margin) was mocked up first
and turned down for what it left behind: the header still stopped a bar's
width short of the edge, cut off beside an empty strip. So #body's bar is
hidden and #vbar stands in for it from the header's foot down
(style.css .vbar, grid.js syncVbar).

Everything here opens its page in `scrollbar_browser`: the shared browser
runs with --hide-scrollbars, where every scrollbar has no thickness, so
#body's own bar coming back beside the header would pass unseen there.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

GEOMETRY = """() => {
  const b = document.getElementById('body'), bb = b.getBoundingClientRect();
  const head = document.getElementById('gridHead').getBoundingClientRect();
  const v = document.getElementById('vbar'), vb = v.getBoundingClientRect();
  const r = document.getElementById('rail').getBoundingClientRect();
  // What the pointer finds just inside the rail, by what it belongs to.
  const at = (x, y) => {
    const n = document.elementFromPoint(x, y);
    if (!n) return null;
    if (n.closest('#gridHead')) return 'header';
    if (n.closest('#vbar')) return 'vbar';
    if (n.closest('#rows')) return 'rows';
    return n.id || n.className || n.tagName;
  };
  return {
    headTop: head.top, headBottom: head.bottom, railLeft: r.left, railTop: r.top,
    bar: { shown: !v.hidden && vb.width > 0, top: vb.top, left: vb.left, right: vb.right,
           bottom: vb.bottom, thickness: v.offsetWidth - v.clientWidth },
    bodyBar: b.offsetWidth - b.clientWidth,
    bodyBottom: bb.bottom, hbar: b.offsetHeight - b.clientHeight,
    besideHead: at(r.left - 2, (head.top + head.bottom) / 2),
    besideRows: at(r.left - 2, head.bottom + 40),
    ranges: [b.scrollHeight - b.clientHeight, v.scrollHeight - v.clientHeight],
    tops: [b.scrollTop, v.scrollTop],
  };
}"""


@pytest.fixture
def pg(own_page, scrollbar_browser):
    with own_page(browser=scrollbar_browser, viewport=(1100, 700)) as page:
        yield page


def _assert_below_the_header(g):
    assert g["bar"]["shown"], g
    # A scrollbar the browser really draws, not an empty strip.
    assert g["bar"]["thickness"] > 0, g
    # And #body draws none of its own, which is the bar that ran up
    # beside the header.
    assert g["bodyBar"] == 0, g
    assert abs(g["bar"]["top"] - g["headBottom"]) <= 0.5, g
    assert abs(g["bar"]["right"] - g["railLeft"]) <= 0.5, g
    assert abs(g["bar"]["bottom"] - (g["bodyBottom"] - g["hbar"])) <= 0.5, g
    # The header reaches the rail: the last pixels before it, at header
    # height, are header — not a scrollbar, and not the empty strip the
    # first mockup left there.
    assert g["besideHead"] == "header", g
    assert g["besideRows"] == "vbar", g
    # The rail maps the rows the thumb travels over, so it starts where
    # the track does.
    assert abs(g["railTop"] - g["bar"]["top"]) <= 0.5, g


def test_the_scrollbar_starts_below_the_header(pg, painted):
    _assert_below_the_header(pg.evaluate(GEOMETRY))
    # A shorter header (the filter bar instead of the filter row) moves
    # the track up with it.
    g = pg.evaluate(GEOMETRY)
    tall = g["headBottom"] - g["headTop"]
    pg.evaluate("() => { __winnow.S.appearance.filterUi = 'bar'; __winnow.renderHeadResized(); }")
    painted(pg)
    g = pg.evaluate(GEOMETRY)
    assert g["headBottom"] - g["headTop"] < tall, g
    _assert_below_the_header(g)


def test_dragging_the_bar_scrolls_the_grid(pg, painted):
    g = pg.evaluate(GEOMETRY)
    bar = g["bar"]
    x = (bar["left"] + bar["right"]) / 2
    # At the top of the table the thumb is at the top of the track.
    pg.mouse.move(x, bar["top"] + 6)
    pg.mouse.down()
    pg.mouse.move(x, bar["top"] + 6 + (bar["bottom"] - bar["top"]) * 0.4, steps=10)
    pg.mouse.up()
    painted(pg)
    g = pg.evaluate(GEOMETRY)
    assert g["tops"][0] > 0, g
    assert abs(g["tops"][0] - g["tops"][1]) < 1, g
    # And the rows followed, not just the number.
    assert pg.evaluate("() => __winnow.onScreenRows().first") > 0


def test_the_bar_follows_the_grid(pg, painted, wheel_settled):
    """Every way #body moves on its own — the wheel over the rows, the
    keyboard — brings the thumb along."""
    with wheel_settled(pg, "#body"):
        pg.mouse.move(500, 400)
        pg.mouse.wheel(0, 600)
    painted(pg)
    g = pg.evaluate(GEOMETRY)
    assert g["tops"][0] >= 600, g
    assert abs(g["tops"][0] - g["tops"][1]) < 1, g

    pg.locator("#body").focus()
    pg.keyboard.press("Control+End")
    pg.wait_for_function("() => { const b = document.getElementById('body');"
                         " return b.scrollTop >= b.scrollHeight - b.clientHeight - 1; }")
    painted(pg)
    g = pg.evaluate(GEOMETRY)
    assert abs(g["tops"][1] - g["ranges"][1]) < 1, g


def test_a_scroll_is_not_pulled_back_by_its_own_echo(pg, painted):
    """Each side's write to the other comes back as a scroll event a frame
    later, and a smooth scroll (or a thumb drag) has moved on by then. Here
    #body moves, the bar is brought along, and #body moves again before the
    bar's event arrives — the next frame of a wheel animation. Passing that
    event on as a move of the bar's own put #body back where it was a
    frame earlier: the scroll fought itself and ended short. Driven by hand
    because headless Chromium doesn't animate the wheel."""
    end = pg.evaluate("""async () => {
      const b = document.getElementById('body'), v = document.getElementById('vbar');
      b.scrollTop = 100;
      // The grid's own scroll listener runs first and brings the bar along.
      await new Promise((r) => b.addEventListener('scroll', r, { once: true }));
      b.scrollTop = 200;
      for (let i = 0; i < 3; i++) await new Promise((r) => requestAnimationFrame(r));
      return [b.scrollTop, v.scrollTop];
    }""")
    assert end == [200, 200], end


def test_the_bars_range_is_the_grids(pg, painted):
    """The thumb is only the share of the table on screen while the bar
    scrolls exactly as far as #body: on arrival, when grouping changes
    the row count, and when the viewport changes size with no paint."""
    g = pg.evaluate(GEOMETRY)
    assert abs(g["ranges"][0] - g["ranges"][1]) <= 1, g

    pg.evaluate("() => __winnow.addGroupLevel('EventId')")
    pg.wait_for_function("() => __winnow.S.groups.length > 0")
    try:
        painted(pg)
        # Four collapsed groups fit: nothing to scroll, and no bar either,
        # as #body's own would have shown none.
        g = pg.evaluate(GEOMETRY)
        assert g["ranges"][0] <= 0 and not g["bar"]["shown"], g
        pg.evaluate("async () => { if (!__winnow.S.groups[0].expanded) await __winnow.toggleGroup(0); }")
        pg.wait_for_function("() => !document.getElementById('vbar').hidden")
        g = pg.evaluate(GEOMETRY)
        assert g["ranges"][0] > 0 and abs(g["ranges"][0] - g["ranges"][1]) <= 1, g
    finally:
        pg.evaluate("() => __winnow.dropGrouping()")
    painted(pg)

    # A resize paints nothing unless it uncovers columns; the
    # ResizeObserver brings the bar along anyway.
    before = pg.evaluate(GEOMETRY)["ranges"][0]
    pg.set_viewport_size({"width": 1100, "height": 520})
    pg.wait_for_function("(b) => { const e = document.getElementById('body');"
                         " return e.scrollHeight - e.clientHeight !== b; }", arg=before)
    pg.wait_for_function("() => { const b = document.getElementById('body'), v = document.getElementById('vbar');"
                         " return Math.abs((b.scrollHeight - b.clientHeight) - (v.scrollHeight - v.clientHeight)) <= 1; }")
    _assert_below_the_header(pg.evaluate(GEOMETRY))


def test_the_last_column_is_not_left_under_the_bar(pg, painted):
    """The bar sits over the right-hand strip of the rows, the way a
    scrollbar does, so at the far right there has to be room past the last
    column for it — and a cell the keyboard brings in from the right has
    to stop at the bar's edge, not under it."""
    g = pg.evaluate(GEOMETRY)
    assert g["hbar"] > 0, "the fixture table doesn't overflow sideways at this width"
    bar_left = g["bar"]["left"]

    pg.locator("#rows .row .cell").first.click()
    pg.keyboard.press("Control+ArrowRight")
    painted(pg)
    right = pg.evaluate("() => document.querySelector('#rows .cell-active').getBoundingClientRect().right")
    assert right <= bar_left + 0.5, (right, bar_left)

    pg.evaluate("() => { const b = document.getElementById('body'); b.scrollLeft = b.scrollWidth; }")
    painted(pg)
    ends = pg.evaluate("""() => ({
      head: document.querySelector('#headRow .hcell:last-child').getBoundingClientRect().right,
      row: [...document.querySelectorAll('#rows .row:first-child .cell')].pop().getBoundingClientRect().right,
    })""")
    assert ends["head"] <= bar_left + 0.5, (ends, bar_left)
    assert ends["row"] <= bar_left + 0.5, (ends, bar_left)

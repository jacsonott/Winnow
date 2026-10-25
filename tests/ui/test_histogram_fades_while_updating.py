"""The histogram strip never shows the previous view's chart as this one's.

The report: after a filter change "there's a period of time where you can
scroll back and forth and it looks like you're still viewing the old
histogram until it suddenly updates". It was: the strip asked for the new
chart only on `winnow:viewchange` — after the build, the grid's first
page, the selection remap and the paint, then 150 ms more — and the
answer took seconds on a large table. For all of that the old chart sat
at full strength, its position band following the new view's rows.

Now the chart fades the moment a rebuild starts and comes back only with
the answer for the view the grid has, and that answer is asked for as the
view lands. Both halves are held open here by holding the request that
ends them, so nothing races.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

ROWS = 200
FILTERED = 50       # EventId 4625

VIEW = re.compile(r".*/api/view(\?.*)?$")
HIST = re.compile(r".*/api/histogram.*")
POSITIONS = re.compile(r".*/api/view/positions(\?.*)?$")

# Asked of the browser, not of the markup — the busy cue once shipped with
# its attribute present and its animation not running. The fade waits a
# beat before it starts, so this waits for the opacity it ends at.
FADED_JS = """() => {
  const c = document.querySelector('#histogramPanel canvas');
  return !!c && c.offsetHeight > 0 && parseFloat(getComputedStyle(c).opacity) < 0.5;
}"""
SOLID_JS = """() => {
  const c = document.querySelector('#histogramPanel canvas');
  return !!c && c.offsetHeight > 0 && getComputedStyle(c).opacity === '1';
}"""


def _info(page):
    return page.evaluate("() => (document.querySelector('#histogramPanel .th-info') || {}).textContent || ''")


def _wait_charted(page, n):
    page.wait_for_function(
        "(n) => (document.querySelector('#histogramPanel .th-info')||{}).textContent.startsWith(n + ' rows')",
        arg=str(n), timeout=15_000)
    page.wait_for_function(SOLID_JS, timeout=5_000)


def _filter(page, value):
    """Starts the rebuild and returns at once — the test holds a request
    the rebuild needs, so awaiting it here would wait for ever."""
    page.evaluate("""(v) => { __winnow.S.filters = v ? { EventId: v } : {}; __winnow.renderHead();
      __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""", value)


@pytest.fixture(autouse=True)
def strip(page):
    page.evaluate("() => { if (!__winnow.histogramOpen()) __winnow.toggleHistogram(true); }")
    page.wait_for_selector("#histogramPanel:not([hidden])", timeout=10_000)
    _wait_charted(page, ROWS)
    yield
    for pattern in (VIEW, HIST, POSITIONS):
        page.unroute(pattern)
    page.evaluate("""() => { __winnow.S.filters = {}; __winnow.renderHead(); __winnow.selClear();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""")
    page.wait_for_function(
        "() => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === 200",
        timeout=15_000)
    page.evaluate("() => __winnow.toggleHistogram(false)")


def test_the_old_chart_fades_the_moment_the_view_starts_changing(page):
    held = []
    page.route(VIEW, lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    _filter(page, "4625")
    page.wait_for_function(FADED_JS, timeout=10_000)
    # Nothing has come back yet — the build is held — and the strip has
    # already stopped presenting the old chart as current: no numbers that
    # read as the new view's, and no place in the grid marked on an axis
    # that is about to be another view's.
    assert held, "the rebuild never reached the server"
    assert _info(page) == "Updating…"
    assert page.evaluate("() => { const p = __winnow.histogramPosition(); return !!(p && (p.band || p.ticks.length || p.cursor)); }") is False
    # Faded, not dropped: the strip keeps its height, so the grid under it
    # does not jump on every filter change.
    assert page.locator("#histogramPanel canvas").is_visible()

    for r in held:
        r.continue_()
    page.unroute(VIEW)
    _wait_charted(page, FILTERED)
    assert "Updating" not in _info(page)


def test_a_rebuild_that_does_not_land_brings_the_old_chart_back(page):
    """Refused, the build leaves the previous view in the grid — and the
    chart describing it is current again, not faded until the next filter
    change."""
    page.route(VIEW, lambda route: route.fulfill(
        status=400, content_type="application/json", body='{"detail": "no such column"}')
        if route.request.method == "POST" else route.continue_())
    _filter(page, "4625")
    page.wait_for_function("() => __winnow.busyCount === 0", timeout=10_000)
    page.unroute(VIEW)
    _wait_charted(page, ROWS)
    assert page.evaluate("() => __winnow.S.view.row_count") == ROWS


def test_the_new_chart_is_asked_for_as_the_view_lands(page, until):
    """Not after the grid has finished with it. Held here: the remap of
    the analyst's picks into the new view, one of the things installView
    awaits before it paints and fires winnow:viewchange — which is where
    the strip used to ask from."""
    page.evaluate("() => { __winnow.selAdd(0); __winnow.selAdd(1); }")
    held, asked = [], []
    page.route(POSITIONS, lambda route: held.append(route))
    page.route(HIST, lambda route: (asked.append(route.request.url), route.continue_()))
    _filter(page, "4625")
    until(page, lambda: held, "the selection remap never went out")
    until(page, lambda: asked, "the histogram was not asked for while the grid was still installing the view")
    view_id = page.evaluate("() => __winnow.S.view.view_id")
    assert f"view_id={view_id}" in asked[-1]
    # The answer is back and the grid has still not finished: the chart is
    # the new view's, so it is not held faded on account of the grid's own
    # work.
    _wait_charted(page, FILTERED)
    assert held, "the remap was let go early; this proved nothing"

    for r in held:
        r.continue_()
    page.unroute(POSITIONS)
    _wait_charted(page, FILTERED)
    # One ask for that view, not a second one from the view change after it.
    assert sum(f"view_id={view_id}" in u for u in asked) == 1

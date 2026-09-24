"""Whether the histogram strip admits to reloading, and to giving up.

The reported symptom was "sometimes the histogram isn't updating with
filter changes — or it isn't clear it's loading updates". The refresh
wiring turns out to be sound: every rebuild dispatches `winnow:viewchange`
and a sequence guard keeps an older answer from painting over a newer
one. What the strip had no way to say was that it was working.

`draw()` only ever mentioned loading inside its "nothing to draw" branch,
so the FIRST chart showed "Loading…" and every one after it repainted the
previous answer byte-for-byte while a new request was out. A chart that
is one filter behind and a chart that has stopped updating look
identical, which is how one becomes a report of the other.

The give-up was silent for the same reason: after a bounded number of
re-asks the strip stops trying, and said nothing about it.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

HIST = re.compile(r".*/api/histogram.*")


@pytest.fixture(autouse=True)
def strip_open(page):
    page.evaluate("() => __winnow.toggleHistogram(true)")
    page.wait_for_selector("#histogramPanel:not([hidden])", timeout=10_000)
    page.wait_for_function("() => !!document.querySelector('#histogramPanel .th-info')", timeout=10_000)
    yield
    page.unroute(HIST)
    page.evaluate("() => { __winnow.clearAllFilters(); __winnow.toggleHistogram(false); }")


def _busy(page):
    return page.evaluate("() => document.getElementById('histogramPanel').hasAttribute('aria-busy')")


def _info(page):
    return page.locator("#histogramPanel .th-info").inner_text()


def test_the_strip_says_it_is_working_while_a_chart_is_already_up(page):
    """The case that had no cue at all. A chart is on screen, a filter
    changes, and until the new answer lands the strip repaints the old
    one — so the only thing that can say a request is out is the chrome."""
    page.wait_for_function("() => /rows/.test(document.querySelector('#histogramPanel .th-info').textContent)",
                           timeout=15_000)
    assert _busy(page) is False, "idle strip should not claim to be busy"

    held = []
    page.route(HIST, lambda route: held.append(route))          # answer nothing yet
    page.evaluate("() => { __winnow.S.filters = { EventId: '=4624' }; return __winnow.rebuildView({ keepScroll: false }); }")
    page.wait_for_function("() => document.getElementById('histogramPanel').hasAttribute('aria-busy')",
                           timeout=15_000)
    # The old chart is still up — deliberately, an empty strip mid-filter
    # is worse than a slightly old one — which is exactly why the cue has
    # to live somewhere other than the canvas.
    assert page.locator("#histogramPanel canvas").is_visible()

    for r in held:
        r.continue_()
    page.unroute(HIST)
    page.wait_for_function("() => !document.getElementById('histogramPanel').hasAttribute('aria-busy')",
                           timeout=15_000)


def test_the_busy_cue_is_on_the_chrome_not_the_canvas(page):
    """A canvas does not inherit CSS, so anything expressed in drawn
    pixels would need a redraw on a timer. The count line carries it."""
    style = page.evaluate("""() => {
      const el = document.querySelector('#histogramPanel .th-info');
      document.getElementById('histogramPanel').setAttribute('aria-busy', 'true');
      const a = getComputedStyle(el).animationName;
      document.getElementById('histogramPanel').removeAttribute('aria-busy');
      return { busy: a, idle: getComputedStyle(el).animationName };
    }""")
    assert style["busy"] != "none" and style["idle"] == "none", style


def test_a_strip_that_gave_up_stops_claiming_to_be_current(page):
    """The other half of the report. After a bounded number of re-asks the
    strip stops trying — right, since a view that keeps 409ing would
    otherwise be polled forever — but it used to stop in silence, leaving
    a chart that describes a filter nobody has on any more."""
    page.wait_for_function("() => /rows/.test(document.querySelector('#histogramPanel .th-info').textContent)",
                           timeout=15_000)
    page.route(HIST, lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"detail": "view expired"}'))
    page.evaluate("() => { __winnow.S.filters = { EventId: '=4625' }; return __winnow.rebuildView({ keepScroll: false }); }")
    page.wait_for_function(
        "() => /previous filter/.test(document.querySelector('#histogramPanel .th-info').textContent)",
        timeout=15_000)
    assert not _busy(page), "given up means not still working"

    # …and it takes the admission back when something makes it work again.
    page.unroute(HIST)
    page.evaluate("() => { __winnow.S.filters = {}; return __winnow.rebuildView({ keepScroll: false }); }")
    page.wait_for_function(
        "() => !/previous filter/.test(document.querySelector('#histogramPanel .th-info').textContent)",
        timeout=15_000)

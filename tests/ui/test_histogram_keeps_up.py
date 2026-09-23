"""The histogram strip keeps up with the filters.

`/api/histogram` names a view id, and a view is evicted the moment the
next rebuild lands — which is exactly what a burst of filter changes is.
The answer to a request that went out just before an eviction is a 409,
and the strip swallowed it on the reasoning that "the rebuild's own view
change refetches". It does not: that change fired *before* the 409 came
back, so nothing was coming. One lost answer and the chart went on
describing the previous filter until something else happened to rebuild
the view — the reported "sometimes the histogram stops updating".

The 409 is produced here rather than raced for: the request is answered
from the test, which is the same answer the server gives when the view
went under it, and makes the case deterministic instead of a burst of
typing that has to be unlucky.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

ROWS = 200
FILTERED = 50       # EventId 4625


def _info(page):
    return page.evaluate("() => (document.querySelector('.th-info') || {}).textContent || ''")


def _charted(page):
    """The row count the strip says it is describing."""
    text = _info(page).split(" rows")[0].replace(",", "")
    return int(text) if text.isdigit() else None


def _open_strip(page):
    page.evaluate("() => { if (!__winnow.histogramOpen()) __winnow.toggleHistogram(true); }")
    page.wait_for_function("() => !document.getElementById('histogramPanel').hidden")
    page.wait_for_function("(n) => (document.querySelector('.th-info')||{}).textContent.startsWith(n)",
                           arg=str(ROWS), timeout=15_000)


def _filter(page, value):
    page.evaluate("""(v) => { __winnow.S.filters = v ? { EventId: v } : {};
      __winnow.renderHead();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""", value)
    page.wait_for_function(
        "(n) => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === n",
        arg=FILTERED if value else ROWS, timeout=15_000)


def _reset(page):
    page.evaluate("""() => { __winnow.S.filters = {}; __winnow.renderHead();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""")
    page.wait_for_function(
        "() => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === 200")


def test_a_lost_answer_does_not_leave_the_strip_on_the_old_filter(page):
    lost = {"one": True}

    def stub(route):
        if lost["one"]:
            lost["one"] = False
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"detail": "View expired — rebuild it"}))
        else:
            route.continue_()

    try:
        _open_strip(page)
        assert _charted(page) == ROWS
        page.route("**/api/histogram*", stub)
        _filter(page, "4625")
        # The strip has to come back to the view on screen on its own —
        # nothing else is going to rebuild it.
        page.wait_for_function(
            "(n) => (document.querySelector('.th-info')||{}).textContent.startsWith(n)",
            arg=f"{FILTERED}", timeout=15_000)
        assert _charted(page) == FILTERED
        assert not lost["one"], "the test never actually lost an answer"
    finally:
        page.unroute("**/api/histogram*")
        _reset(page)


def test_it_gives_up_rather_than_polling_a_view_that_keeps_failing(page):
    """The other direction: a view that is the grid's own and answers 409
    every time must not turn the strip into a poller. It re-asks a few
    times and then leaves the chart alone."""
    seen = []

    def always_409(route):
        seen.append(1)
        route.fulfill(status=409, content_type="application/json",
                      body=json.dumps({"detail": "View expired — rebuild it"}))

    try:
        _open_strip(page)
        page.route("**/api/histogram*", always_409)
        _filter(page, "4625")
        page.wait_for_timeout(2_500)     # many multiples of the 150ms debounce
        asked = len(seen)
        assert asked <= 6, f"the strip asked {asked} times — it is polling"
        page.wait_for_timeout(1_000)
        assert len(seen) == asked, "it is still asking"
        # …and the chart that is up is the last one it could draw, not an
        # error message: the rows are still on screen and still real.
        assert _charted(page) == ROWS
    finally:
        page.unroute("**/api/histogram*")
        _reset(page)
        page.wait_for_function(
            "() => (document.querySelector('.th-info')||{}).textContent.startsWith('200')",
            timeout=15_000)


def test_an_ordinary_filter_change_still_redraws_once(page):
    """The re-ask must not turn one filter change into two requests: the
    answer describes the view on screen, so there is nothing to chase."""
    asks = []
    page.on("request", lambda r: asks.append(r.url) if "/api/histogram" in r.url else None)
    try:
        _open_strip(page)
        before = len(asks)
        _filter(page, "4625")
        page.wait_for_function(
            "(n) => (document.querySelector('.th-info')||{}).textContent.startsWith(n)",
            arg=f"{FILTERED}", timeout=15_000)
        page.wait_for_timeout(700)
        assert len(asks) - before == 1, f"{len(asks) - before} requests for one filter change"
    finally:
        _reset(page)

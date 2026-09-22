"""Where "Open ↦" puts you, and what the grid says while it gets there.

Two symptoms with one root. A sweep counts rows in the table itself and
ignores every filter on it, but Open ↦ went through the ordinary open
path, which restores the filters that table was last left with and builds
them — so the grid showed fewer rows than the count that was clicked, and
on a filter that matched nothing, none at all. That first build is also
what painted the "no rows match" overlay, and nothing took it down for
the search that followed: the answer to the previous question sat on
screen, in the analyst's terms, for the whole of the next one.

So: clear what narrows the table, open with skipBuild so exactly one
build runs, and derive the overlay from whether a build is in flight
rather than leaving it where the last one put it.

The sweep is stubbed (page.route) — the shared session case has one
table, and what is under test here is the landing, not the counting.
"""

from __future__ import annotations

import json
import re
import time

import pytest

pytestmark = pytest.mark.ui

TERM = "4624"          # one of the four EventIds — 50 of the fixture's 200 rows
OTHER = "4625"         # a different one, so the two never overlap


def _stub_sweep(page, hits):
    """Answer the sweep's start and poll from here, reporting `hits`."""
    scope = {"requested": None, "source_ids": None, "merges": []}

    def _start(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 11, "scope": scope, "scanned": 0, "total": 1,
            "hits": [], "done": False, "error": None, "cancelled": False,
        }))

    def _poll(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 11, "scope": scope, "scanned": 1, "total": 1,
            "hits": hits, "done": True, "error": None, "cancelled": False,
        }))

    page.route("**/api/search_all/start", _start)
    page.route("**/api/search_all/job*", _poll)


def _narrow_the_table(page):
    """Leave the open table with a filter that matches nothing the term
    matches, a tag filter and a timeframe — one of each kind of thing that
    can hide a row, so the assertion afterwards is about all of them."""
    page.evaluate("""(other) => {
      __winnow.S.filters = { EventId: other };
      __winnow.S.tagFilter = ['__any__'];
      __winnow.S.timeRange = { enabled: true, column: 'Timestamp',
                               start: '2026-03-14 08:00:00', end: '2026-03-14 08:00:10' };
      __winnow.renderHead();
      __winnow.updateTimeRangeButton();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""", OTHER)
    page.wait_for_function("() => __winnow.busyCount === 0 && __winnow.S.view")


def _reset(page):
    page.evaluate("""() => {
      document.getElementById('search').value = '';
      __winnow.S.search = ''; __winnow.S.searchMode = 'contains'; __winnow.S.searchTerms = [];
      __winnow.S.filters = {}; __winnow.S.tagFilter = [];
      __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' };
      __winnow.S.searchAll = null;
      __winnow.renderHead(); __winnow.updateTimeRangeButton();
      __winnow.syncSearchExpansion(false); __winnow.updateSearchHint();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function(
        "() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")


def test_opening_a_hit_lands_on_the_count_that_was_clicked(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    _stub_sweep(page, [{"source_id": source_id, "name": "ui.csv",
                        "match_count": 50, "capped": False, "terms": []}])
    try:
        _narrow_the_table(page)
        assert page.evaluate("() => __winnow.S.view.row_count") == 0, \
            "the setup has to leave the table showing nothing, or this proves nothing"

        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modal .search-all-paste").fill(TERM)
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_selector("#modalBody .search-all-row", timeout=15_000)
        page.locator("#modalBody .search-all-row .btn", has_text="Open ↦").first.click()

        # The row said 50. The grid shows 50 — not 50 intersected with
        # whatever was on the table, which was nothing at all.
        page.wait_for_function(
            "() => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === 50",
            timeout=15_000)
        state = page.evaluate("""() => ({
          filters: __winnow.S.filters,
          tree: __winnow.S.filterTree.children.length,
          tags: __winnow.S.tagFilter.length,
          timeframe: !!__winnow.S.timeRange.enabled,
          terms: __winnow.S.searchTerms.map((t) => t.term),
        })""")
        assert state == {"filters": {}, "tree": 0, "tags": 0, "timeframe": False, "terms": [TERM]}
        # ...and the empty state that the old first build painted is gone.
        assert page.locator("#noRows").is_hidden()
    finally:
        page.keyboard.press("Escape")
        page.unroute("**/api/search_all/start")
        page.unroute("**/api/search_all/job*")
        _reset(page)


def test_what_was_cleared_is_named_rather_than_silently_dropped(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    _stub_sweep(page, [{"source_id": source_id, "name": "ui.csv",
                        "match_count": 50, "capped": False, "terms": []}])
    try:
        _narrow_the_table(page)
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modal .search-all-paste").fill(TERM)
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_selector("#modalBody .search-all-row", timeout=15_000)
        page.locator("#modalBody .search-all-row .btn", has_text="Open ↦").first.click()

        page.wait_for_function(
            "() => { const t = document.getElementById('toast');"
            " return !t.hidden && t.textContent.startsWith('Cleared '); }", timeout=15_000)
        said = page.locator("#toast").inner_text()
        # Each of the three, named — a count of filters is not a sentence
        # an analyst can act on.
        assert "1 column filter" in said, said
        assert "the tag filter" in said, said
        assert "the timeframe" in said, said
    finally:
        page.keyboard.press("Escape")
        page.unroute("**/api/search_all/start")
        page.unroute("**/api/search_all/job*")
        _reset(page)


def test_a_build_in_flight_does_not_say_there_are_no_rows(page):
    """The overlay belongs to the view on screen. While a build runs there
    isn't one yet, and the stats line is what carries "still working" —
    in words, rather than as an answer the analyst can misread as final.

    The build is held open by intercepting its request, so "while it runs"
    is a state this test controls rather than a sleep it hopes is long
    enough (the idiom test_search_supersede.py established)."""
    route_re = re.compile(r".*/api/view(/start)?(\?.*)?$")
    held = []
    page.route(route_re, lambda route: held.append(route))
    try:
        # Something that matches nothing, so the overlay is genuinely up
        # before the build under test starts.
        # Fired, not awaited: the route below holds the build open, so a
        # returned promise would leave page.evaluate waiting for the thing
        # this test is holding.
        page.evaluate("""() => {
          __winnow.S.filters = { EventId: 'nothing-matches-this' };
          __winnow.renderHead();
          __winnow.rebuildView({ keepScroll: false, keepRow: false });
        }""")
        deadline = time.time() + 10
        while not held:
            assert time.time() < deadline, "no view build was intercepted"
            page.wait_for_timeout(25)
        held.pop().continue_()
        page.wait_for_function(
            "() => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === 0")
        assert page.locator("#noRows").is_visible(), "the setup needs the overlay up"

        # Now ask a different question and hold the answer.
        page.evaluate("""() => {
          __winnow.S.filters = { EventId: '4624' };
          __winnow.renderHead();
          __winnow.rebuildView({ keepScroll: false, keepRow: false });
        }""")
        deadline = time.time() + 10
        while not held:
            assert time.time() < deadline, "the second build was not intercepted"
            page.wait_for_timeout(25)
        page.wait_for_function("() => document.getElementById('noRows').hidden", timeout=5_000)
        held.pop().continue_()
        page.wait_for_function(
            "() => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === 50")
        assert page.locator("#noRows").is_hidden()
    finally:
        for r in held:
            try:
                r.continue_()
            except Exception:
                pass
        page.unroute(route_re)
        _reset(page)

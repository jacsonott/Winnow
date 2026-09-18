"""Two rebuilds meeting: one handing the screen to the next, and one
landing for a caller that is going to act on what it built.

A view build is not always started by the search box, even when it goes
through the search box's own helper. `landOnFilters` (Reset, Shift+F, the
row menu's "…only", a session comparison's "open these differing rows")
normalises the box back to substring mode on its way, and `setSearchMode`
ends in a rebuild — the detaching kind. Detaching means resolving with
the OLD view still on screen, and `landOnFilters` goes on to expand the
search bar and recentre on the row it kept, against the view it thinks it
just built. So that one build is awaited: `{ detach: false }`
(docs/notes/ui.md — only search-box rebuilds detach).

The other way two builds meet is a supersede, and what they hand over is
the cancel chip. A build that supersedes one whose chip is already up
arms its own at once rather than 1.2 s later, so a long search's Cancel
doesn't blink off on every keystroke — but the superseded build disarms
the moment its aborted fetch rejects, which is before the newer build has
finished asking the server where the analyst's picks went. The chip is
therefore claimed at the supersede itself.

The third handoff is to nobody: the table a build was for can be removed
while it is in flight, and the landing then runs against an S.sources
that no longer holds its record.

No test here changes anything the server keeps: filters, picks, the
search box and the client's copy of the source list are all client
state, and each puts the full table back.
"""
from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui

NOTICES = "#jobsPanel .job-notice"


def _until(page, pred, what, timeout=10):
    """Python-side state (a route handler's list) is invisible to
    wait_for_function; poll it here, bounded."""
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, what
        page.wait_for_timeout(25)


class _Held:
    """Holds every request matching `pattern` until release(); after that
    they pass straight through."""

    def __init__(self, page, pattern):
        self.page = page
        self.pattern = re.compile(pattern)
        self.held = []
        self.seen = 0
        self.open = True
        page.route(self.pattern, self._on)

    def _on(self, route):
        self.seen += 1
        if self.open:
            self.held.append(route)
        else:
            route.continue_()

    def wait_held(self, n=1):
        _until(self.page, lambda: len(self.held) >= n, f"nothing held for {self.pattern.pattern}")

    def release(self):
        self.open = False
        for r in self.held:
            try:
                r.continue_()
            except Exception:
                pass   # the page aborted this one itself; nothing to release
        self.held = []

    def close(self):
        self.release()
        self.page.unroute(self.pattern)


def _reset(page):
    """The shared fixture as found: no search, no filters, no picks, the
    full table, nothing pending, the detach back at its default."""
    page.evaluate("""() => {
      __winnow.setSearchDetachMs(5000);
      for (const id of [...__winnow.S.pendingViews.keys()]) __winnow.cancelPendingView(id);
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);
      __winnow.selClear();
      document.getElementById('search').value = '';
      __winnow.S.search = ''; __winnow.S.searchMode = 'contains'; __winnow.S.searchTerms = [];
      __winnow.S.filters = {}; __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] };
      __winnow.renderHead(); __winnow.syncSearchExpansion(false); __winnow.updateSearchHint();
      document.querySelectorAll('#searchModeToggle button').forEach((b) =>
        b.setAttribute('aria-pressed', String(b.dataset.mode === 'contains')));
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")
    page.wait_for_function(f"() => __winnow.S.pendingViews.size === 0 && !document.querySelector('{NOTICES}')")


def test_reset_from_regex_mode_lands_on_the_view_it_built(page):
    """Reset with the box in regex mode goes through setSearchMode, and
    what comes back has to be the cleared view — not the regex one with a
    search still running behind it while every piece of chrome says the
    filters are gone.

    The detach is rebound to 0 and /api/view/start masked as still running
    (tests/ui/test_search_background.py does the same): a build that takes
    the detaching path here cannot land, which is exactly what the caller
    after it would be reading."""
    starts = []

    def on_start(route):
        starts.append(1)
        resp = route.fetch()
        route.fulfill(response=resp, json=dict(resp.json(), status="running", view=None))

    def on_job(route):
        job = route.fetch().json()
        route.fulfill(json=dict(job, status="running", view=None))

    page.click("#btnSearchToggle")
    try:
        page.click('#searchModeToggle button[data-mode="regex"]')
        page.locator("#search").fill("4625")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        assert page.evaluate("() => __winnow.S.searchMode") == "regex"
        page.evaluate("() => __winnow.setSearchDetachMs(0)")
        page.route(re.compile(r".*/api/view/start(\?.*)?$"), on_start)
        page.route(re.compile(r".*/api/view/job\?.*"), on_job)
        page.click("#btnReset")
        # The rows on screen are the cleared view's, and nothing is still
        # out looking for them.
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.locator(NOTICES).count() == 0
        assert starts == []
        # ...and the chrome that was already showing the cleared state
        # agrees with them.
        assert page.evaluate("() => __winnow.S.searchMode") == "contains"
        assert page.locator("#search").input_value() == ""
    finally:
        page.unroute(re.compile(r".*/api/view/start(\?.*)?$"))
        page.unroute(re.compile(r".*/api/view/job\?.*"))
        _reset(page)


def test_the_cancel_chip_does_not_blink_when_a_build_supersedes_another(page):
    """Picks are what make the window: the superseding build asks the
    server where they went (/api/view/keys) before it starts building, and
    the build it superseded disarms the chip as soon as its own fetch is
    aborted. Held here, that window is as long as the test likes; in the
    app it is a round trip on every keystroke."""
    page.evaluate("() => { __winnow.selAdd(0); __winnow.selAdd(1); __winnow.renderTagToolbar(); }")
    builds = _Held(page, r".*/api/view(\?.*)?$")
    keys = _Held(page, r".*/api/view/keys(\?.*)?$")
    keys.open = False       # the first build's own lookup goes through
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        box.fill("4")
        builds.wait_held()
        page.wait_for_selector("#busyCancel:not([hidden])")   # armed once the build has run ~1.2s
        # Every time the chip is hidden from here on, recorded.
        page.evaluate("""() => {
          window.__chipHidden = 0;
          const btn = document.getElementById('busyCancel');
          new MutationObserver(() => { if (btn.hidden) window.__chipHidden++; })
            .observe(btn, { attributes: true, attributeFilter: ['hidden'] });
        }""")
        keys.open = True
        box.fill("46")
        keys.wait_held()
        # The superseded build is done with (its fetch was aborted), the
        # newer one is still asking about the picks — and the Cancel the
        # analyst was looking at is still there to press.
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__chipHidden") == 0
        assert page.locator("#busyCancel").is_visible()
    finally:
        keys.close()
        builds.close()
        page.wait_for_function("() => __winnow.busyCount === 0")
        _reset(page)


def test_a_build_lands_after_its_table_is_removed_under_it(page):
    """Remove takes the table out of S.sources; a build already out for it
    lands a beat later and asks that list for the record — for the total
    the stats line denominates with, and for whether the table has a
    trigram index worth following. Both threw when the record had gone,
    which left the view half installed: S.view swapped, nothing painted,
    no winnow:viewchange, and an uncaught error in the console.

    The build is held here so the window is as wide as the test likes; in
    the app it is whatever the round trip costs. The search in the spec is
    what makes the index question get asked at all."""
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    sid = page.evaluate("() => __winnow.S.sourceId")
    builds = _Held(page, r".*/api/view(\?.*)?$")
    try:
        page.evaluate("""() => {
          window.__landed = 0;
          document.addEventListener('winnow:viewchange', () => { window.__landed++; });
          document.getElementById('search').value = '4625';
          __winnow.S.search = '4625';
          __winnow.rebuildView({ keepScroll: false, keepRow: false });
        }""")
        builds.wait_held()
        # What Remove does to the client's list, with the build already out.
        page.evaluate("(id) => { __winnow.S.sources = __winnow.S.sources.filter((s) => s.id !== id); }", sid)
        builds.release()
        # All the way through: the event fires from the tail of
        # installView, after the paint.
        page.wait_for_function("() => window.__landed === 1 && __winnow.busyCount === 0", timeout=15_000)
        assert page.evaluate("() => __winnow.S.view.row_count") == 50
        # With no table record to give a total, the view's own count is
        # the denominator — the rows it holds are the rows it found.
        stats = page.evaluate("() => document.getElementById('viewStats').textContent")
        assert re.fullmatch(r"50 of 50 rows · [\d.]+ ms", stats), stats
        assert errors == []
    finally:
        builds.close()
        page.evaluate("(id) => { __winnow.loadSources(id); }", sid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view"
                               " && __winnow.S.view.source_id === id", arg=sid, timeout=30_000)
        _reset(page)

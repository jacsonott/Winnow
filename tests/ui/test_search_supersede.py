"""A superseded view build is cancelled, and a running one says so.

Every keystroke past the search box's debounce posted its own /api/view
and nothing cancelled the one before: each queued on the writer lock in
turn, each stale one still committed — evicting the view under the grid,
whose next page fetch 409'd into yet another rebuild — and until it
reached the lock its request sat on one of the browser's six per-host
connections. The only sign of any of it was the 2px bar; the row count
sat there unchanged. rebuildView now cancels and aborts the build it
supersedes before starting its own, says nothing about it, and while a
build runs the stats count its seconds and the box marks itself busy.

"A slow build" is a state the tests control, not a sleep they hope is
long enough: /api/view is intercepted and the request released when the
test says so.
"""
from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui


class _HeldViews:
    """Intercepts the build request — POST /api/view for a header-box
    filter, POST /api/view/start for the search box, whose builds run as
    a job (see test_search_background.py; within the detach window the
    two behave the same). The detach clock starts before the start is
    posted, but the deadline is only checked once the start has
    answered — and the fixture's real answer is `done` (200 rows search
    in microseconds), so a start held here installs on release rather
    than detaching. The first `hold` requests stay pending until
    release(); the rest pass straight through."""

    ROUTE = re.compile(r".*/api/view(/start)?(\?.*)?$")

    def __init__(self, page, hold=1):
        self.page = page
        self.hold = hold
        self.held = []
        self.seen = []     # every request's JSON body, in order
        page.route(self.ROUTE, self._on)

    def _on(self, route):
        self.seen.append(route.request.post_data_json)
        if len(self.held) < self.hold:
            self.held.append(route)
        else:
            route.continue_()

    def wait_held(self, n=1):
        _until(self.page, lambda: len(self.held) >= n, f"no /api/view held (saw {len(self.seen)})")

    def release(self):
        for r in self.held:
            try:
                r.continue_()
            except Exception:
                pass   # the page aborted this one itself; nothing left to release
        self.held = []

    def close(self):
        self.release()
        self.page.unroute(self.ROUTE)


def _until(page, pred, what, timeout=10):
    """Python-side state (a route handler's list, a request sniffer) is
    invisible to wait_for_function; poll it here, bounded."""
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, what
        page.wait_for_timeout(25)


def _cancel_sniffer(page):
    """Every /api/cancel_op the page sent, recorded when its RESPONSE
    arrives — so "the cancel landed" means the server has acted on it,
    not merely that the request left the browser."""
    seen = []
    page.on("response", lambda r: seen.append(r.request.post_data_json) if r.url.endswith("/api/cancel_op") else None)
    return seen


def _arm_recorders(page):
    """Count installed views and remember every toast shown — the two
    things a superseded build must not produce."""
    page.evaluate("""() => {
      window.__installed = 0;
      document.addEventListener('winnow:viewchange', () => { window.__installed++; });
      window.__toasts = [];
      const t = document.getElementById('toast');
      new MutationObserver(() => { if (!t.hidden) window.__toasts.push(t.textContent); })
        .observe(t, { childList: true, characterData: true, subtree: true, attributes: true });
    }""")


def _stats(page):
    return page.evaluate("() => document.getElementById('viewStats').textContent")


def _reset(page):
    """Leave the shared fixture as found: no search, no filters, the full
    table, nothing in flight."""
    page.evaluate("""() => {
      document.getElementById('search').value = '';
      __winnow.S.search = ''; __winnow.S.searchMode = 'contains'; __winnow.S.filters = {};
      __winnow.renderHead(); __winnow.syncSearchExpansion(false); __winnow.updateSearchHint();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")


def test_a_slow_search_says_so_while_it_runs(page):
    hold = _HeldViews(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        hold.wait_held()
        page.wait_for_function("""() => document.getElementById('viewStats').textContent.startsWith('Searching "4624"…')""")
        text = _stats(page)
        assert re.fullmatch(r'Searching "4624"… \d+\.\d s', text), text
        assert page.locator("#search").get_attribute("aria-busy") == "true"
        # ...and it is a clock, not a label: the next tick reads differently.
        page.wait_for_function("(t) => document.getElementById('viewStats').textContent !== t", arg=text)
        hold.release()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.includes('of 200 rows')")
        assert page.locator("#search").get_attribute("aria-busy") is None
        assert page.locator("#busyBar").is_hidden()
    finally:
        hold.close()
        _reset(page)


def test_typing_again_cancels_the_build_it_supersedes(page):
    hold = _HeldViews(page)
    cancels = _cancel_sniffer(page)
    _arm_recorders(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("46")
        hold.wait_held()
        first = hold.seen[0]["op_token"]
        assert first
        page.locator("#search").fill("4624")
        # The second build lands (it was let through); the first never does.
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.includes('of 200 rows')")
        _until(page, lambda: cancels, "no /api/cancel_op was sent for the superseded build")
        assert [c["token"] for c in cancels] == [first]
        assert len(hold.seen) == 2 and hold.seen[1]["op_token"] != first
        assert page.evaluate("() => window.__installed") == 1
        toasts = page.evaluate("() => window.__toasts")
        assert not [t for t in toasts if "Cancelled" in t], toasts
        assert page.locator("#busyBar").is_hidden()
        assert page.locator("#busyCancel").is_hidden()
        assert page.locator("#search").get_attribute("aria-busy") is None
    finally:
        hold.close()
        _reset(page)


def test_the_chip_cancel_still_says_so_and_keeps_the_old_count(page):
    """The analyst's own cancel is not a supersede: it toasts, and the
    count from before the build comes back — the old rows are still the
    rows on screen."""
    hold = _HeldViews(page)
    cancels = _cancel_sniffer(page)
    _arm_recorders(page)
    before = _stats(page)
    assert before.startswith("200 of 200 rows"), before
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        hold.wait_held()
        page.wait_for_selector("#busyCancel:not([hidden])")   # armed once the build has run ~1.2s
        page.wait_for_function("""() => document.getElementById('viewStats').textContent.startsWith('Searching')""")
        page.click("#busyCancel")
        _until(page, lambda: cancels, "the chip sent no /api/cancel_op")
        # The build reaches the server after its cancel did: 499, no view.
        hold.release()
        page.wait_for_function("() => (window.__toasts || []).some((t) => t.includes('Cancelled'))")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('200 of 200 rows')")
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert page.evaluate("() => window.__installed") == 0
        page.wait_for_function("() => __winnow.busyCount === 0")
        assert page.locator("#busyBar").is_hidden()
        assert page.locator("#search").get_attribute("aria-busy") is None
    finally:
        hold.close()
        _reset(page)


def test_a_filter_build_says_filtering(page):
    hold = _HeldViews(page)
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        box.fill("4")   # contains: 4624/4625/4688 stay, 1 goes — 150 rows
        hold.wait_held()
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('Filtering…')")
        assert re.fullmatch(r"Filtering… \d+\.\d s", _stats(page))
        # Not the search box's build, so the search box does not claim it.
        assert page.locator("#search").get_attribute("aria-busy") is None
        hold.release()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 150")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.includes('of 200 rows')")
    finally:
        hold.close()
        _reset(page)


def test_the_first_search_on_an_unindexed_table_follows_the_index_build(page):
    """The server starts the trigram build from inside the search's own
    build (Store._ensure_fts_building), the view's payload says nothing
    about it, and the jobs poll is not running on an idle case. So the
    client has to ask: a search build landing on a table whose record says
    no index refetches the sources, and the poll then follows the build to
    its end — the hint, the panel row, the toast.

    The fixture's table was imported --no-fts, but the session's first
    search had it indexed long ago, so this test plays the server's part
    for /api/sources: an unindexed table with nothing running, then — once
    the search is in — the build it started, then the finished index. What
    the page does with each answer is the thing under test; nothing here
    writes client state by hand."""
    sid = page.evaluate("() => __winnow.S.sourceId")
    phase = {"n": 0}
    asks = []

    def on_sources(route):
        asks.append(phase["n"])
        resp = route.fetch()
        rows = resp.json()
        for s in rows:
            if s["id"] == sid:
                s["has_fts"] = 1 if phase["n"] == 2 else 0
                s["fts_building"] = phase["n"] == 1
        route.fulfill(response=resp, json=rows)

    page.route("**/api/sources", on_sources)
    # Boot against that server: the page's record of the table says no
    # index, as it would on a machine where nobody has searched it yet.
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    page.wait_for_function("() => __winnow.S.view && __winnow.busyCount === 0")
    _arm_recorders(page)
    asked = len(asks)
    page.click("#btnSearchToggle")
    try:
        assert page.locator("#searchMode").text_content() == "substring"
        assert len(asks) == asked   # an idle case asks nothing
        page.locator("#search").fill("4624")
        phase["n"] = 1   # the search's build has started the index build
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50")
        # Landing is what asks, and the hint says what the answer was.
        page.wait_for_function("() => document.getElementById('searchMode').textContent === 'substring · index building'")
        assert len(asks) > asked
        page.wait_for_selector("#jobsPanel .job-phase.indexing")
        phase["n"] = 2   # the index landed
        page.wait_for_function("() => document.getElementById('searchMode').textContent === 'substring'")
        page.wait_for_function("() => (window.__toasts || []).some((t) => t.includes('Search index ready'))")
        page.wait_for_function("() => !document.querySelector('#jobsPanel .job-phase.indexing')")
    finally:
        page.unroute("**/api/sources")
        _reset(page)


def test_the_hint_names_the_regex_scan(page):
    """Regex never uses the index, whatever state it is in."""
    page.click("#btnSearchToggle")
    try:
        page.click('#searchModeToggle button[data-mode="regex"]')
        page.wait_for_function("() => document.getElementById('searchMode').textContent === 'regex · full scan'")
    finally:
        page.click('#searchModeToggle button[data-mode="contains"]')
        page.wait_for_function("() => document.getElementById('searchMode').textContent === 'substring'")
        _reset(page)

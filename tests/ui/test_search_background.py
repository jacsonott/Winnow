"""A search that takes too long goes to the background; the result waits
for Apply.

A search-box build blocks the grid — busy bar, cancel chip, "Searching…
N s" — for SEARCH_DETACH_MS, then leaves the build to finish on its own:
the old rows stay on screen (a held build evicts nothing), a jobs-panel
row with Cancel stands for it, and when it lands the row offers Apply and
Discard and a toast offers Apply. Nothing installs itself. Typing again
cancels the pending search and starts a new one; Escape cancels it;
Discard closes it and the old count comes back.

"Takes too long" is a state the tests set, not a sleep: the detach is
rebound to 0 (setSearchDetachMs), and /api/view/start's answer is the
real job masked as still running, with the polls held until the test
lets them through. The adopt that Apply does is real.

Two rules around the pending search are pinned here too. Any other
rebuild of its table — a header-box filter here — calls it off FIRST:
that build lands as a normal build, which evicts the held view, and
while the search runs it would queue on the writer lock behind it. And
coming back to the table with the search still in the box shows the
rows the table had, with the search still pending — not a second build
of the same search.
"""
from __future__ import annotations

import json
import re
import time

import pytest

pytestmark = pytest.mark.ui

ROWS = "#jobsPanel:not([hidden]) .job-row.job-notice"


class _Background:
    """Plays the server for a search that outlives the detach. The start
    route runs the real build (so the held view exists) and answers
    `running`; every poll is answered `running` too until release(), after
    which they reach the server and come back `done` with the view."""

    def __init__(self, page):
        self.page = page
        self.released = False
        self.final = None   # a status the polls answer instead of "running" (finish_as)
        self.jobs = []      # every job /api/view/start really returned, in order
        self.polls = 0
        page.route(re.compile(r".*/api/view/start(\?.*)?$"), self._on_start)
        page.route(re.compile(r".*/api/view/job\?.*"), self._on_job)

    def _on_start(self, route):
        resp = route.fetch()
        job = resp.json()
        self.jobs.append(job)
        route.fulfill(response=resp, json=dict(job, status="running", view=None))

    def _on_job(self, route):
        self.polls += 1
        if self.released:
            route.continue_()
            return
        job = self.jobs[-1] if self.jobs else {"job_id": 0, "source_id": 0}
        body = {"job_id": job["job_id"], "source_id": job["source_id"],
                "status": "running", "view": None, "error": None,
                "error_status": None, "elapsed_ms": None, "started_at": 0}
        if self.final:
            body.update(self.final)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def release(self):
        self.released = True

    def finish_as(self, status, error=None):
        """The polls answer `status` from now on — the server's word that
        the job ended without a view (cancelled, superseded, an error)."""
        self.final = {"status": status, "error": error}

    def close(self):
        self.released = True
        self.page.unroute(re.compile(r".*/api/view/start(\?.*)?$"))
        self.page.unroute(re.compile(r".*/api/view/job\?.*"))


def _until(page, pred, what, timeout=10):
    """Python-side state (a route's list, a response sniffer) is invisible
    to wait_for_function; poll it here, bounded."""
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, what
        page.wait_for_timeout(25)


def _cancels(page):
    """Every /api/view/job/cancel the page sent, recorded when the server
    has answered it."""
    seen = []
    page.on("response", lambda r: seen.append(r.url) if "/api/view/job/cancel" in r.url else None)
    return seen


class _Requests:
    """Every request the page sent, in the order it sent them, as
    (method, path) with the host stripped — for asserting what a build
    did and did not post, and in which order."""

    def __init__(self, page):
        self.seen = []
        page.on("request", lambda r: self.seen.append((r.method, re.sub(r"^.*?/api/", "/api/", r.url))))

    def posts(self, start, pattern):
        """The POSTs since index `start` whose path matches `pattern`."""
        return [u for m, u in self.seen[start:] if m == "POST" and re.search(pattern, u)]


def _second_table(page, tmp_path):
    """A second table in the shared case (tests/ui/test_view_state.py
    adds one the same way, and a later test finds it already there).
    Returns (the open table's id, the other's)."""
    if page.evaluate("() => __winnow.S.sources.length") < 2:
        csv2 = tmp_path / "second.csv"
        csv2.write_text("Alpha,Beta\n" + "".join(f"{i},x{i}\n" for i in range(30)), encoding="utf-8")
        status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
        assert status == 200
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function("() => __winnow.S.sources.length >= 2 && __winnow.S.view && __winnow.busyCount === 0")
    return page.evaluate("() => [__winnow.S.sourceId, __winnow.S.sources.find((s) => s.id !== __winnow.S.sourceId).id]")


def _arm(page):
    page.evaluate("""() => {
      __winnow.setSearchDetachMs(0);
      window.__installed = 0;
      document.addEventListener('winnow:viewchange', () => { window.__installed++; });
    }""")


def _stats(page):
    return page.evaluate("() => document.getElementById('viewStats').textContent")


def _notice_text(page):
    return page.locator(ROWS).first.inner_text()


def _reset(page):
    """Leave the shared fixture as found: nothing pending, no search, the
    full table, the detach back at its default."""
    page.evaluate("""() => {
      __winnow.setSearchDetachMs(5000);
      for (const id of [...__winnow.S.pendingViews.keys()]) __winnow.cancelPendingView(id);
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);   // a finished row still lingering
      document.getElementById('search').value = '';
      __winnow.S.search = ''; __winnow.S.searchMode = 'contains'; __winnow.S.filters = {};
      __winnow.renderHead(); __winnow.syncSearchExpansion(false); __winnow.updateSearchHint();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")
    page.wait_for_function("() => __winnow.S.pendingViews.size === 0 && !document.querySelector('#jobsPanel .job-notice')")


def test_a_long_search_goes_to_the_background_and_waits_for_apply(page):
    bg = _Background(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        # The build detached: a notice with Cancel, the stats say so, and
        # every piece of blocking chrome is down.
        page.wait_for_selector(ROWS)
        assert 'Searching "4624"' in _notice_text(page)
        assert page.locator(f"{ROWS} .job-action", has_text="Cancel").count() == 1
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('Searching in background')")
        page.wait_for_function("() => __winnow.busyCount === 0")
        assert page.locator("#busyBar").is_hidden()
        assert page.locator("#busyCancel").is_hidden()
        assert page.locator("#search").get_attribute("aria-busy") is None
        # The old rows are still the rows on screen, and the app knows the
        # search is running.
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert page.locator("#body .row").count() > 0
        assert any("search" in b for b in page.evaluate("() => __winnow.inFlightWork()"))
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 1
        _until(page, lambda: bg.polls >= 2, "the pending search is not being polled")
        # It lands: the notice offers Apply and Discard, a toast offers
        # Apply — and nothing installed itself.
        bg.release()
        page.wait_for_selector(f"{ROWS} .job-action:has-text('Apply')")
        assert page.locator(f"{ROWS} .job-action", has_text="Discard").count() == 1
        assert "50 rows" in _notice_text(page)
        page.wait_for_function("""() => { const t = document.getElementById('toast');
          return !t.hidden && t.textContent.includes('Search finished') && t.textContent.includes('50 rows'); }""")
        assert page.locator("#toast .toast-action", has_text="Apply").count() == 1
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('Search finished')")
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert page.evaluate("() => window.__installed") == 0
        # Apply installs it, through the real adopt.
        page.locator(f"{ROWS} .job-action", has_text="Apply").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.includes('of 200 rows')")
        assert page.evaluate("() => window.__installed") == 1
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.locator(ROWS).count() == 0
        assert page.locator("#search").input_value() == "4624"
        assert page.evaluate("() => __winnow.S.search") == "4624"
    finally:
        bg.close()
        _reset(page)


def test_typing_again_replaces_the_pending_search(page):
    bg = _Background(page)
    cancels = _cancels(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("46")
        page.wait_for_selector(ROWS)
        assert 'Searching "46"' in _notice_text(page)
        first = bg.jobs[0]["job_id"]
        page.locator("#search").fill("4624")
        # One notice, the new search's; the first job was cancelled.
        page.wait_for_function("""() => { const rows = document.querySelectorAll('#jobsPanel .job-notice');
          return rows.length === 1 && rows[0].textContent.includes('Searching "4624"'); }""")
        _until(page, lambda: cancels, "the superseded search was not cancelled")
        assert cancels[0].endswith(f"job_id={first}")
        assert len(bg.jobs) == 2 and bg.jobs[1]["job_id"] != first
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 1
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        bg.release()
        page.wait_for_selector(f"{ROWS} .job-action:has-text('Apply')")
        page.locator(f"{ROWS} .job-action", has_text="Apply").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
    finally:
        bg.close()
        _reset(page)


def test_discard_closes_the_notice_and_keeps_the_old_view(page):
    bg = _Background(page)
    cancels = _cancels(page)
    _arm(page)
    before = _stats(page)
    assert before.startswith("200 of 200 rows"), before
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(ROWS)
        bg.release()
        page.wait_for_selector(f"{ROWS} .job-action:has-text('Discard')")
        page.locator(f"{ROWS} .job-action", has_text="Discard").click()
        page.wait_for_function("() => !document.querySelector('#jobsPanel .job-notice')")
        _until(page, lambda: cancels, "Discard sent no cancel")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('200 of 200 rows')")
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.evaluate("() => window.__installed") == 0
        assert page.locator("#body .row").count() > 0
    finally:
        bg.close()
        _reset(page)


def test_cancel_while_running_restores_the_stats(page):
    bg = _Background(page)
    cancels = _cancels(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(f"{ROWS} .job-action:has-text('Cancel')")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('Searching in background')")
        page.locator(f"{ROWS} .job-action", has_text="Cancel").click()
        page.wait_for_function("() => !document.querySelector('#jobsPanel .job-notice')")
        _until(page, lambda: cancels, "Cancel sent no cancel")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('200 of 200 rows')")
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert not any("search" in b for b in page.evaluate("() => __winnow.inFlightWork()"))
    finally:
        bg.close()
        _reset(page)


def test_escape_in_the_box_cancels_the_pending_search(page):
    bg = _Background(page)
    cancels = _cancels(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(ROWS)
        first = bg.jobs[0]["job_id"]
        page.locator("#search").press("Escape")
        # The cleared box is its own (instant) build — masked as running
        # here like every other start, so it detaches too and stands a
        # row of its own ("Filtering …"). The end state is what counts:
        # the search it replaced is gone, its job cancelled, the box empty.
        page.wait_for_function("""() => [...document.querySelectorAll('#jobsPanel .job-notice')]
          .every((r) => !r.textContent.includes('Searching "4624"'))""")
        _until(page, lambda: cancels, "Escape sent no cancel")
        assert cancels[0].endswith(f"job_id={first}")
        assert page.locator("#search").input_value() == ""
        assert page.evaluate("() => __winnow.S.search") == ""
    finally:
        bg.close()
        _reset(page)


def test_the_notice_x_cancels_the_pending_search(page):
    """✕ on the row is the search's Cancel (and its Discard once it has
    landed) — a plain dismiss would leave the search polling with
    nothing on screen to apply or drop it from."""
    bg = _Background(page)
    cancels = _cancels(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(f"{ROWS} .job-x")
        page.locator(f"{ROWS} .job-x").click()
        page.wait_for_function("() => !document.querySelector('#jobsPanel .job-notice')")
        _until(page, lambda: cancels, "the ✕ sent no cancel")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('200 of 200 rows')")
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert not any("search" in b for b in page.evaluate("() => __winnow.inFlightWork()"))
    finally:
        bg.close()
        _reset(page)


def test_a_search_cancelled_server_side_ends_as_a_finished_row_not_an_error(page):
    """Superseded by another window's search on the same table, or
    cancelled through the chip just before the deadline: the poll answers
    `cancelled`. Not this search's fault, so its row finishes and lingers
    like a finished import's instead of waiting for a ✕ in error red."""
    bg = _Background(page)
    _arm(page)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(ROWS)
        bg.finish_as("cancelled")
        page.wait_for_function("""() => { const r = document.querySelector('#jobsPanel .job-notice');
          return !!(r && r.querySelector('.job-phase.done') && r.textContent.includes('cancelled')); }""")
        assert page.locator(f"{ROWS} .job-phase.error").count() == 0
        assert page.locator(f"{ROWS} .job-action").count() == 0
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('200 of 200 rows')")
    finally:
        bg.close()
        _reset(page)


def test_a_header_filter_calls_the_pending_search_off_before_it_builds(page):
    """A header-box filter on a table whose search is in the background
    lands as a normal build — which evicts the held view, and which,
    while the search runs, would queue on the writer lock behind it. So
    the search is cancelled FIRST (its cancel leaves before /api/view
    does), its row goes, and the filter build blocks with the chip as it
    always has; nothing starts a second job."""
    bg = _Background(page)
    reqs = _Requests(page)
    _arm(page)
    page.click("#btnSearchToggle")
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(ROWS)
        job = bg.jobs[0]["job_id"]
        since = len(reqs.seen)
        box.fill("4")   # with "4624" still in the box: the 50 rows whose EventId is 4624
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        assert reqs.posts(since, r"/api/view(/job/cancel\?.*)?$") == [f"/api/view/job/cancel?job_id={job}", "/api/view"]
        assert reqs.posts(since, r"/api/view/start") == []
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.locator(ROWS).count() == 0
        page.wait_for_function("() => document.getElementById('viewStats').textContent.includes('of 200 rows')")
        assert not page.evaluate("() => document.getElementById('viewStats').textContent").startswith("Search")
    finally:
        bg.close()
        _reset(page)


def test_coming_back_to_the_table_keeps_the_old_rows_and_the_search_pending(page, tmp_path):
    """Leave the table while its search runs, come back: the stash puts
    the search in the box again, and that spec IS the pending search —
    so the table shows the rows it had (the held build touched nothing),
    the stats say the search is still out, and nothing posts a second,
    blocking build of the same search to queue behind the first on the
    writer lock. Apply still installs it, here."""
    bg = _Background(page)
    reqs = _Requests(page)
    _arm(page)
    first, second = _second_table(page, tmp_path)
    page.click("#btnSearchToggle")
    try:
        page.locator("#search").fill("4624")
        page.wait_for_selector(ROWS)
        page.evaluate("(id) => __winnow.openSource(id)", second)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.busyCount === 0", arg=second)
        since = len(reqs.seen)
        page.evaluate("(id) => __winnow.openSource(id)", first)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.busyCount === 0", arg=first)
        assert reqs.posts(since, r"/api/view(/start)?(\?.*)?$") == []
        assert page.evaluate("() => __winnow.S.view.row_count") == 200
        assert page.locator("#body .row").count() > 0
        assert page.locator("#search").input_value() == "4624"
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 1
        assert page.locator(ROWS).count() == 1
        assert 'Searching "4624"' in _notice_text(page)
        assert _stats(page).startswith("Searching in background")
        assert len(bg.jobs) == 1
        installed = page.evaluate("() => window.__installed")
        bg.release()
        page.wait_for_selector(f"{ROWS} .job-action:has-text('Apply')")
        page.wait_for_function("() => document.getElementById('viewStats').textContent.startsWith('Search finished')")
        page.locator(f"{ROWS} .job-action", has_text="Apply").click()
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        assert page.evaluate("() => window.__installed") == installed + 1
        assert page.evaluate("(id) => __winnow.S.sourceId === id", first)
    finally:
        bg.close()
        _reset(page)


def test_a_filter_build_still_blocks_with_the_chip(page):
    """The detach is the search box's alone: a header-box filter goes
    through /api/view and blocks, never /api/view/start."""
    starts = []
    page.route(re.compile(r".*/api/view/start(\?.*)?$"), lambda route: (starts.append(1), route.continue_()))
    _arm(page)
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        box.fill("4")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 150 && __winnow.busyCount === 0")
        assert starts == []
        assert page.evaluate("() => __winnow.S.pendingViews.size") == 0
        assert page.locator(ROWS).count() == 0
    finally:
        page.unroute(re.compile(r".*/api/view/start(\?.*)?$"))
        _reset(page)

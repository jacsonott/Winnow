"""A new watchlist entry is in the list the moment the server has it.

Add used to post the indicator, then await a scan of every table for
every indicator, and only then reload the list — so on a real case the
new row was invisible for the length of the scan, with the input already
cleared and nothing else on screen to say anything had happened. Now the
row renders from the add's own response with "…" for a count, and the
scan runs behind it as a job whose result fills the count in.

Timing is shaped with page.route, not sleeps: the scan's start request
is held (never answered) while the row is asserted, then released; the
second test aborts it outright to pin that a scan that cannot start
leaves the row saying what is actually true of it — nothing has read it
yet — rather than a marker that never resolves (the trap the search-all
badge documents) and rather than the "0" that used to stand in for it,
which reads as "looked, and it is not in this case". The third holds the
job polls instead, so the first Add's scan is still being followed when
the second Add starts its own: the row the newer scan displaces settles
at once, and nothing is left in the running state.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

START = re.compile(r".*/api/watchlist/scan/start$")
JOB = re.compile(r".*/api/watchlist/scan/job\?.*")


def _clear_watchlist(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const wl = await fetch('/api/watchlist', { headers: h }).then(r => r.json());
      for (const i of wl) await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
      await fetch('/api/watchlist/seen', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 0 }) });
    }""")


def _row_state(value):
    """The count cell's text for the row whose value is `value`, or null.

    The cell is read through `.wl-count` whatever it holds — a number, the
    "…" marker, or one of the scanned/not-scanned words — because that
    class is the slot. Optional chaining, not a bare `.textContent`: these
    predicates run inside wait_for_function, where a missing cell must be
    a false poll and not an exception that ends the wait.
    """
    return f"""() => {{ const r = [...document.querySelectorAll('.wl-row')]
        .find(x => x.querySelector('.wl-val')?.textContent === {value!r});
      return r ? r.querySelector('.wl-count')?.textContent ?? null : null; }}"""


def test_the_row_appears_before_the_scan_has_even_started(page):
    _clear_watchlist(page)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    held = []
    page.route(START, lambda route: held.append(route))   # the start never answers until released
    try:
        page.locator("#wlValue").fill("H2")
        page.locator("#wlAdd").click()
        # The row is on screen with its count pending — while the scan's
        # start request is still sitting in `held`, unanswered.
        page.wait_for_function(f"() => ({_row_state('H2')})() === '…'")
        assert len(held) == 1
        assert page.locator("#wlValue").input_value() == ""
        assert page.locator("#wlSummary").inner_text().startswith("1 indicator")
        assert page.locator(".wl-count.scanning").count() == 1
        # Released, the scan lands and the count arrives: H2 is 40 rows of ui.csv.
        held[0].continue_()
        page.wait_for_function(f"() => ({_row_state('H2')})() === '40'", timeout=15_000)
        assert page.locator(".wl-count.scanning").count() == 0
        page.wait_for_function("() => !/Scanning/.test(document.getElementById('wlStatus').textContent)")
        # The jobs-panel row stood for the scan; on the tab it finishes quietly.
        page.wait_for_function("() => [...document.querySelectorAll('#jobsPanel .job-notice')]"
                               ".some(r => /hits/.test(r.textContent) && !/Open watchlist/.test(r.textContent))")
    finally:
        page.unroute(START)
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        _clear_watchlist(page)


def test_a_scan_that_cannot_start_leaves_a_settled_state_not_a_marker(page):
    """No scan ran, so the row settles on "not scanned" — the honest
    answer, and the one that is not a marker waiting on a job that will
    never land. It is deliberately not "0": that would claim the tables
    had been read for this indicator and it was absent, which is the
    sentence an analyst quotes into a report."""
    _clear_watchlist(page)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.route(START, lambda route: route.abort())
    try:
        page.locator("#wlValue").fill("H3")
        page.locator("#wlAdd").click()
        page.wait_for_function(f"() => ({_row_state('H3')})() !== null")
        page.wait_for_function(f"() => ({_row_state('H3')})() === 'not scanned'")
        assert page.locator(".wl-count.scanning").count() == 0
        assert page.locator(".wl-row .wl-count.unscanned").count() == 1
        assert page.locator("#wlStatus").inner_text() == ""
        page.wait_for_selector("#toast:not([hidden])")
        assert "scan" in page.locator("#toast").inner_text().lower()
    finally:
        page.unroute(START)
        _clear_watchlist(page)


def test_a_second_add_settles_the_first_scans_row_and_leaves_none_running(page):
    """One scan is followed at a time. The row of the scan a newer one
    displaces settles the moment the newer start lands — as folded (the
    server widened the new job to its scope) or, had it already finished,
    as done — rather than staying a running row whose Cancel reaches a job
    the server no longer has. The polls are held so the first scan cannot
    land on its own before the second Add; the first start's answer is
    read as still running so the two scans overlap on the client whatever
    the server's timing."""
    _clear_watchlist(page)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    held = []
    page.route(JOB, lambda route: held.append(route))

    def still_running(route):
        resp = route.fetch()
        body = resp.json()
        body["status"] = "running"
        route.fulfill(response=resp, json=body)

    page.route(START, still_running, times=1)
    try:
        page.locator("#wlValue").fill("H1")
        page.locator("#wlAdd").click()
        page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-notice .job-phase.running').length === 1")
        page.locator("#wlValue").fill("H2")
        page.locator("#wlAdd").click()
        # The first row settled when the second start landed; one row runs.
        page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-notice').length === 2")
        assert page.locator("#jobsPanel .job-notice .job-phase.running").count() == 1
        settled = page.locator("#jobsPanel .job-notice", has=page.locator(".job-phase.done"))
        assert settled.count() == 1
        assert re.search(r"folded into the newer scan|finished", settled.inner_text())
        page.unroute(JOB)
        for r in held:
            r.continue_()
        # Both counts land (H1 and H2 are 40 rows each of ui.csv), no marker
        # and no running row is left behind.
        page.wait_for_function(f"() => ({_row_state('H1')})() === '40' && ({_row_state('H2')})() === '40'",
                               timeout=15_000)
        page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-notice .job-phase.running').length === 0")
        assert page.locator(".wl-count.scanning").count() == 0
    finally:
        page.unroute(JOB)
        page.unroute(START)
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        _clear_watchlist(page)

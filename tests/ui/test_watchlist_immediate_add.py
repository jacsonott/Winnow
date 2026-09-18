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
leaves the row with its real count rather than a marker that never
resolves (the trap the search-all badge documents).
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

START = re.compile(r".*/api/watchlist/scan/start$")


def _clear_watchlist(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const wl = await fetch('/api/watchlist', { headers: h }).then(r => r.json());
      for (const i of wl) await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
      await fetch('/api/watchlist/seen', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 0 }) });
    }""")


def _row_state(value):
    """The count cell's text for the row whose value is `value`, or null."""
    return f"""() => {{ const r = [...document.querySelectorAll('.wl-row')]
        .find(x => x.querySelector('.wl-val')?.textContent === {value!r});
      return r ? r.querySelector('.wl-count').textContent : null; }}"""


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


def test_a_scan_that_cannot_start_leaves_a_real_count_not_a_marker(page):
    _clear_watchlist(page)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.route(START, lambda route: route.abort())
    try:
        page.locator("#wlValue").fill("H3")
        page.locator("#wlAdd").click()
        page.wait_for_function(f"() => ({_row_state('H3')})() !== null")
        # No scan ran, so the honest count is 0 — and it is a count, not "…".
        page.wait_for_function(f"() => ({_row_state('H3')})() === '0'")
        assert page.locator(".wl-count.scanning").count() == 0
        assert page.locator("#wlStatus").inner_text() == ""
        page.wait_for_selector("#toast:not([hidden])")
        assert "scan" in page.locator("#toast").inner_text().lower()
    finally:
        page.unroute(START)
        _clear_watchlist(page)

"""Watchlist hits in the browser: each hit shows the column and cell it
matched in plus the row as one line, clicking one lands on the row, and a
scan that finds hits while the analyst is elsewhere raises a jobs-panel
notice whose button opens the watchlist.
"""
import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _clear_indicators(server):
    req = urllib.request.Request(server.rstrip("/") + "/api/watchlist", headers={"X-Timeline-Lite-Client": "1"})
    for ind in json.loads(urllib.request.urlopen(req).read()):
        urllib.request.urlopen(urllib.request.Request(
            server.rstrip("/") + f"/api/watchlist/{ind['id']}", method="DELETE",
            headers={"X-Timeline-Lite-Client": "1"})).read()


@pytest.fixture
def indicator(page, server, server_post):
    _clear_indicators(server)
    server_post("/api/watchlist", {"value": "H2", "kind": "other"})
    yield
    _clear_indicators(server)
    page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); __winnow.resetJobState(); }")


def test_hit_shows_where_it_matched_and_lands_on_the_row(page, indicator, server_post):
    server_post("/api/watchlist/scan", {})
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator(".wl-row", has=page.locator(".wl-val", has_text="H2")).click()
    page.wait_for_selector(".wl-hit")
    first = page.locator(".wl-hit").first
    assert first.locator(".wl-hit-col").inner_text().startswith("Host: H2")
    preview = first.locator(".wl-hit-preview").inner_text()
    assert "H2" in preview and "|" in preview
    rid = int(first.locator(".wl-hit-rid").inner_text().split()[-1])
    first.click()
    page.wait_for_function("() => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === 1 && !!__winnow.S.view")
    # Landed on the row: the cursor sits on it (an unfiltered view's
    # position is rid - 1) rather than at the top of the table.
    page.wait_for_function("(rid) => __winnow.S.cursor === rid - 1", arg=rid, timeout=10_000)


def test_scan_elsewhere_raises_a_notice_with_a_way_to_the_hits(page, indicator):
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.evaluate("() => __winnow.scanWatchlistForSources([1])")
    # The scan's own progress row becomes the alert when it lands, so wait
    # for the finished form — the one carrying the way to the hits.
    row = page.locator("#jobsPanel .job-notice", has_text="Open watchlist")
    row.wait_for(state="visible", timeout=10_000)
    assert "hits" in row.locator(".job-name").inner_text()
    assert "in ui.csv" in row.locator(".job-detail").inner_text()
    row.locator(".job-action", has_text="Open watchlist").click()
    page.wait_for_function("() => __winnow.S.activeTab === 'watchlist'")
    assert page.locator("#jobsPanel .job-notice", has_text="Watchlist").count() == 0

"""The reworked watchlist page: header bar with a live summary, the add
form as its own panel, kind pills + note sub-lines on rows — and the
new-hit badge on the page tab, cleared by looking at the tab (the seen
mark lives in the case file, so it travels with the .db)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _clear_watchlist(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const wl = await fetch('/api/watchlist', { headers: h }).then(r => r.json());
      for (const i of wl) await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
      await fetch('/api/watchlist/seen', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 0 }) });
    }""")


def test_layout_summary_and_note_subline(page):
    _clear_watchlist(page)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator("#wlValue").fill("H1")
    page.locator("#wlAdd").click()
    page.wait_for_selector(".wl-row")
    # Summary reflects the scan the Add triggered.
    page.wait_for_function(
        "() => /1 indicator · 1 with hits · \\d+ hits/.test(document.getElementById('wlSummary').textContent)")
    row = page.locator(".wl-row").first
    assert "IOC" in row.locator(".wl-kind").inner_text()
    # Kind pill carries its kind class (the hue-coding hook).
    assert "wl-kind-other" in row.locator(".wl-kind").get_attribute("class")
    # The add form sits in the left panel, above the list.
    add_box = page.locator(".wl-add").bounding_box()
    list_box = page.locator(".wl-list").bounding_box()
    assert add_box["y"] < list_box["y"]
    _clear_watchlist(page)


def test_new_hit_badge_lights_and_clears(page):
    _clear_watchlist(page)
    # Seed an indicator + hits WITHOUT the tab open, then refresh the badge —
    # this is the "a scan found something while you were elsewhere" path.
    page.evaluate("""async () => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      await fetch('/api/watchlist', { method: 'POST', headers: h,
        body: JSON.stringify({ value: 'H2', kind: 'other' }) });
      await fetch('/api/watchlist/scan', { method: 'POST', headers: h });
      await __winnow.refreshWatchlistBadge();
    }""")
    page.wait_for_selector("#tabWatchlist.has-new-hits")

    # Looking at the tab clears the dot and records the seen high-water.
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.wait_for_selector("#tabWatchlist.has-new-hits", state="detached")
    page.wait_for_function("""() => fetch('/api/watchlist/badge').then(r => r.json())
      .then(b => b.seen > 0 && b.seen === b.total_hits)""")
    # A badge refresh with nothing new stays dark.
    page.evaluate("() => __winnow.refreshWatchlistBadge()")
    page.wait_for_timeout(200)
    assert page.locator("#tabWatchlist.has-new-hits").count() == 0
    _clear_watchlist(page)

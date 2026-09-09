"""IOC watchlist tab — add an indicator, import a table which auto-scans
it, and drill into the hits. Cleans up after itself (indicators are
case-level server state)."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_watchlist_add_autoscan_and_hits(page, server, tmp_path):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    try:
        # Add an indicator that matches the shared ui_csv (host H2 exists).
        page.locator("#wlValue").fill("H2")
        page.locator("#wlKind").select_option("other")
        page.locator("#wlAdd").click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('.wl-val')].some(e => e.textContent === 'H2')",
            timeout=10_000)
        # It got scanned on add → non-zero count, and clicking shows hits.
        row = page.locator(".wl-row", has=page.locator(".wl-val", has_text="H2"))
        page.wait_for_function(
            """() => { const r=[...document.querySelectorAll('.wl-row')].find(x=>x.querySelector('.wl-val')?.textContent==='H2');
                       return r && +r.querySelector('.wl-count').textContent > 0; }""", timeout=10_000)
        row.click()
        page.wait_for_selector(".wl-hit")
        assert page.locator(".wl-hit").count() >= 1
    finally:
        # indicators are case-level server state — remove what this test added
        req = urllib.request.Request(server.rstrip("/") + "/api/watchlist",
                                     headers={"X-Timeline-Lite-Client": "1"})
        for ind in json.loads(urllib.request.urlopen(req).read()):
            urllib.request.urlopen(urllib.request.Request(
                server.rstrip("/") + f"/api/watchlist/{ind['id']}",
                method="DELETE", headers={"X-Timeline-Lite-Client": "1"})).read()


def test_watchlist_export_downloads_csv(page):
    """The Export button downloads the indicators as CSV, client-side."""
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    try:
        page.locator("#wlValue").fill("evil.example.com")
        page.locator("#wlKind").select_option("domain")
        page.locator("#wlAdd").click()
        page.wait_for_selector(".wl-val:has-text('evil.example.com')")
        with page.expect_download() as dl:
            page.locator("#wlExport").click()
        content = open(dl.value.path(), encoding="utf-8").read()
        assert content.splitlines()[0] == "value,kind,note,hits"
        assert "evil.example.com,domain" in content
    finally:
        page.evaluate("""async () => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          for (const i of await fetch('/api/watchlist', { headers: h }).then(r => r.json()))
            await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
        }""")

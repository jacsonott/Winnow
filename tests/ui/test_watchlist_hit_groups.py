"""The watchlist's hits pane groups hits by the table they landed in: one
collapsible header per table, named as the sidebar names it, carrying
that table's exact count; folding a header hides its rows and only its
rows. A second table is imported for the duration of the test — the
shared case has one — and dropped again at the end.
"""

from __future__ import annotations

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


def _drop_source(server, sid):
    urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + f"/api/source/{sid}", method="DELETE",
        headers={"X-Timeline-Lite-Client": "1"})).read()


@pytest.fixture
def second_table(page, server, tmp_path):
    """A small second table holding the indicator twice, imported before
    the indicator exists (so the import hook's auto-scan has nothing to
    do), dropped afterwards so the shared case is left as found."""
    _clear_indicators(server)   # an empty watchlist: the import hook's auto-scan has nothing to do
    csv2 = tmp_path / "wl_second.csv"
    csv2.write_text("Alpha,Beta\n1,H2 seen here\n2,H2 again\n3,nothing\n", encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'wl_second.csv')", timeout=15_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wl_second.csv').id")
    yield sid
    _drop_source(server, sid)


def test_hits_group_by_table_with_exact_counts_and_fold(page, server, server_post, second_table):
    _clear_indicators(server)
    try:
        server_post("/api/watchlist", {"value": "H2", "kind": "other"})
        server_post("/api/watchlist/scan", {})
        page.locator("#tabWatchlist").click()
        page.wait_for_selector("#watchlistview:not([hidden])")
        page.locator(".wl-row", has=page.locator(".wl-val", has_text="H2")).click()
        page.wait_for_selector(".wl-hit-group-head")
        heads = page.locator(".wl-hit-group-head")
        groups = page.locator(".wl-hit-group")
        assert heads.count() == 2
        # One header per table, heaviest first, each with that table's exact count.
        assert heads.nth(0).locator(".wl-hit-group-label").inner_text() == "ui.csv"
        assert heads.nth(0).locator(".wl-hit-group-count").inner_text() == "40 hits"
        assert heads.nth(1).locator(".wl-hit-group-label").inner_text() == "wl_second.csv"
        assert heads.nth(1).locator(".wl-hit-group-count").inner_text() == "2 hits"
        # The rows under a header are that table's — the count is what it says.
        assert groups.nth(0).locator(".wl-hit").count() == 40
        assert groups.nth(1).locator(".wl-hit").count() == 2
        assert groups.nth(1).locator(".wl-hit-col").first.inner_text() == "Beta: H2 seen here"
        # Folding the first table hides its rows and leaves the other's showing.
        heads.nth(0).click()
        page.wait_for_function("() => document.querySelector('.wl-hit-group .wl-hit-group-body').hidden")
        assert groups.nth(0).locator(".wl-hit:visible").count() == 0
        assert groups.nth(1).locator(".wl-hit:visible").count() == 2
        assert heads.nth(0).locator(".wl-hit-group-arrow").inner_text() == "▸"
        heads.nth(0).click()
        page.wait_for_function("() => !document.querySelector('.wl-hit-group .wl-hit-group-body').hidden")
        assert groups.nth(0).locator(".wl-hit:visible").count() == 40
        assert heads.nth(0).locator(".wl-hit-group-arrow").inner_text() == "▾"
    finally:
        _clear_indicators(server)

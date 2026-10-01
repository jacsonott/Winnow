"""Navigating to a hit in a CLOSED table opens that table properly.

"Open" is server-side state (`sources.is_open`), and the jump from a
watchlist hit was only calling openSource — which switches the grid to
the table without touching it. The analyst landed in a table with no tab,
absent from the sidebar's Open section and still listed under the closed
ones: nothing on screen to come back to, and nothing to close.

Driven through a real hit on a real closed table, because the symptom is
the chrome around the grid rather than the grid itself. jumpToTimelineRow
is shared with the Timeline's rows, so this covers that jump too.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _req(server, route, method="GET", body=None):
    req = urllib.request.Request(
        server.rstrip("/") + route, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    return json.loads(raw) if raw else None


def _clear_indicators(server):
    for ind in _req(server, "/api/watchlist"):
        _req(server, f"/api/watchlist/{ind['id']}", method="DELETE")


@pytest.fixture
def closed_table_with_a_hit(page, server, tmp_path):
    """A second table carrying one indicator value, scanned and then
    closed — the state the bug needs. Dropped afterwards so the shared
    case is left as found."""
    _clear_indicators(server)   # the import hook's auto-scan has nothing to do
    csv2 = tmp_path / "wl_closed.csv"
    csv2.write_text("Alpha,Beta\n1,ZEBRACHECK here\n2,nothing\n", encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'wl_closed.csv')",
                           timeout=15_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wl_closed.csv').id")

    _req(server, "/api/watchlist", method="POST", body={"value": "ZEBRACHECK", "kind": "other"})
    _req(server, "/api/watchlist/scan", method="POST", body={})

    # Close it, which is what makes this a test of anything.
    _req(server, f"/api/source/{sid}/open", method="POST", body={"open": False})
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("(id) => __winnow.S.sources.find((s) => s.id === id).is_open === false",
                           arg=sid, timeout=15_000)
    assert page.locator(f'#sourceTabs .tab[data-id="{sid}"]').count() == 0
    yield sid

    _clear_indicators(server)
    _req(server, f"/api/source/{sid}", method="DELETE")
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")


def _click_the_hit(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator(".wl-row", has=page.locator(".wl-val", has_text="ZEBRACHECK")).click()
    page.wait_for_selector(".wl-hit")
    page.locator(".wl-hit").first.click()
    page.wait_for_selector("#gridview:not([hidden]), #app .grid", timeout=10_000)


def test_the_hit_opens_the_table_as_a_tab(page, closed_table_with_a_hit):
    sid = closed_table_with_a_hit
    _click_the_hit(page)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid, timeout=10_000)
    # The three things a closed table was missing, all of them chrome:
    page.wait_for_function("(id) => __winnow.S.sources.find((s) => s.id === id).is_open === true",
                           arg=sid, timeout=10_000)
    assert page.locator(f'#sourceTabs .tab[data-id="{sid}"]').count() == 1
    assert page.evaluate("(id) => __winnow.openTabsSorted().some((s) => s.id === id)", sid) is True
    assert page.evaluate("(id) => __winnow.tablesToOpen().some((s) => s.id === id)", sid) is False
    assert page.evaluate("""() => [...document.querySelectorAll('.sidebar-openrow')]
      .some((r) => r.textContent.includes('wl_closed.csv'))""") is True


def test_it_lands_on_the_row_the_hit_names(page, closed_table_with_a_hit):
    """Opening the table is only half of it — the jump still has to put the
    cursor on the hit's own row."""
    _click_the_hit(page)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=closed_table_with_a_hit,
                           timeout=10_000)
    page.wait_for_function("() => __winnow.S.cursor >= 0", timeout=10_000)
    assert page.evaluate("() => __winnow.rowAt(__winnow.S.cursor).cells.join(' ')").find("ZEBRACHECK") >= 0


def test_an_already_open_table_still_jumps(page, closed_table_with_a_hit, server):
    """The fast path: an open table needs no reload, and the jump must not
    have become conditional on the table being closed."""
    sid = closed_table_with_a_hit
    _req(server, f"/api/source/{sid}/open", method="POST", body={"open": True})
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("(id) => __winnow.S.sources.find((s) => s.id === id).is_open === true",
                           arg=sid, timeout=15_000)
    _click_the_hit(page)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid, timeout=10_000)
    assert page.locator(f'#sourceTabs .tab[data-id="{sid}"]').count() == 1

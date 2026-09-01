"""Search-all can seed the watchlist: the terms you sweep for are usually
the IOCs worth watching as new data lands."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_add_search_terms_to_watchlist(page):
    page.locator("#btnSearchAll").click()
    page.wait_for_selector("#modal:not([hidden])")
    try:
        page.locator("#modal .search-all-paste").fill("H2\nH3")
        page.locator("#modal button", has_text="Add to watchlist").click()
        page.wait_for_function(
            """() => fetch('/api/watchlist', { headers: { 'X-Timeline-Lite-Client': '1' } })
                 .then(r => r.json())
                 .then(w => { window.__wl = w; return w.some(i => i.value === 'H2') && w.some(i => i.value === 'H3'); })""",
            timeout=10_000)
    finally:
        page.keyboard.press("Escape")
        page.evaluate("""async () => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          for (const i of await fetch('/api/watchlist', { headers: h }).then(r => r.json()))
            await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
        }""")

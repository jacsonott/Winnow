"""Search-all can seed the watchlist: the terms you sweep for are usually
the IOCs worth watching as new data lands."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


def test_add_search_terms_to_watchlist(page):
    page.locator("#btnSearchAll").click()
    page.wait_for_selector("#modal:not([hidden])")
    try:
        page.locator("#modal .search-all-paste").fill("H2\nH3")
        page.locator("#modal button", has_text="Add to watchlist").click()
        # Polled from Python: wait_for_function does not await a promise
        # predicate, so this passed the moment it was called.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            values = page.evaluate(
                """() => fetch('/api/watchlist', { headers: { 'X-Timeline-Lite-Client': '1' } })
                     .then((r) => r.json()).then((w) => w.map((i) => i.value))""")
            if "H2" in values and "H3" in values:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("the watchlist never received H2/H3")
    finally:
        page.keyboard.press("Escape")
        page.evaluate("""async () => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          for (const i of await fetch('/api/watchlist', { headers: h }).then(r => r.json()))
            await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
        }""")

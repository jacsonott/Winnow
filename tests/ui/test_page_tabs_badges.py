"""Pages stay labelled, and a count sits on the page it counts.

Measured with eight tables open: the table strip overflowed (1,212px of
tabs in 1,171px) and the page strip was a single 83px button carrying a
caret, the name of whichever page was up — "SQL" — and a "99+" badge. The
99+ counted watchlist hits. A number against the wrong name is worse than
no number, and it is the kind of wrong that only shows up in a walk,
because every part of it is individually correct.

Two halves, both pinned here. The strip is compact and expanded by
default, so the pages are labelled and reachable without opening
anything. And in either mode the badge travels with its own page — onto
the Watchlist tab when the strip is out, onto the Watchlist row inside
the menu when the analyst has collapsed it — never onto a control that
merely names whatever page is showing.

The dropdown mode's own behaviour (sizing, the drag handle, switching
pages) is tests/ui/test_pages_dropdown.py.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

PAGE_KEYS = ["sql", "timeline", "notes", "watchlist"]


def _clear_watchlist(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      const wl = await fetch('/api/watchlist', { headers: h }).then(r => r.json());
      for (const i of wl) await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
      await fetch('/api/watchlist/seen', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 0 }) });
      await __winnow.refreshWatchlistBadge();
    }""")


def _seed_hits(page):
    """An indicator scanned while the analyst was elsewhere — 'H2' matches
    40 of the 200 rows in the shared fixture CSV. This is the path that
    lights the badge: hits found without the Watchlist tab ever showing."""
    page.evaluate("""async () => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      await fetch('/api/watchlist', { method: 'POST', headers: h,
        body: JSON.stringify({ value: 'H2', kind: 'other' }) });
      await fetch('/api/watchlist/scan', { method: 'POST', headers: h });
      await __winnow.refreshWatchlistBadge();
    }""")
    page.wait_for_selector("#tabWatchlist.has-new-hits")


def _set_pages_menu(page, on):
    page.evaluate("""(on) => { __winnow.S.appearance.pagesMenu = on;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""", on)


@pytest.fixture(autouse=True)
def clean(page):
    yield
    _clear_watchlist(page)
    _set_pages_menu(page, False)


def test_a_fresh_install_shows_the_pages_rather_than_a_caret(page):
    """The collapsed button hid four destinations behind a menu and named
    only one of them. Expanded is the default now; the preference to
    collapse is still there (test_pages_dropdown.py)."""
    assert page.evaluate("() => __winnow.defaultAppearance().pagesMenu") is False
    assert page.locator("#pagesMenuBtn").count() == 0
    for key, label in zip(PAGE_KEYS, ["SQL", "Timeline", "Notes", "Watchlist"]):
        tab = page.locator(f'#pageTabs .tab[data-page-key="{key}"]')
        assert tab.is_visible(), key
        assert tab.inner_text().strip().startswith(label), key
    # Labelled AND reachable: the four fit inside the strip at the test
    # viewport rather than needing a scroll, which is the point of making
    # them compact. Measured against these four by name, not against the
    # strip's whole content — the case is shared by the UI session and a
    # neighbouring module may have left a dashboard pinned to it.
    assert page.evaluate("""(keys) => {
      const s = document.getElementById('pageTabs');
      const w = keys.reduce((a, k) => a +
        s.querySelector(`.tab[data-page-key="${k}"]`).getBoundingClientRect().width, 0);
      return w <= s.clientWidth + 1;
    }""", PAGE_KEYS)


def test_the_pages_cost_the_table_strip_less_than_they_did(page):
    """What buys the room: the compact rules are scoped to `.page-tabs`, so
    a clone of each page tab parked in the TABLE strip renders in exactly
    the full-size shape these used to have. The difference is the width
    handed back to the tables — padding and rules alone, so this holds
    whatever the mono fallback on the box turns out to be."""
    _clear_watchlist(page)   # a badge would be measured on one side only
    got = page.evaluate("""() => {
      const strip = document.getElementById('pageTabs');
      const tabs = [...strip.querySelectorAll('.tab:not([hidden])')];
      const w = (ns) => ns.reduce((a, n) => a + n.getBoundingClientRect().width, 0);
      const host = document.getElementById('sourceTabs');
      const probes = tabs.map((n) => {
        const c = n.cloneNode(true);
        c.removeAttribute('id');          // no duplicate ids, even for a frame
        host.appendChild(c);
        return c;
      });
      const before = w(probes);
      for (const c of probes) c.remove();
      return { now: w(tabs), before };
    }""")
    assert got["before"] - got["now"] >= 40, got


def test_the_count_sits_on_the_watchlist_tab(page):
    _clear_watchlist(page)
    _seed_hits(page)
    assert page.locator('#pageTabs .tab[data-page-key="watchlist"] .tab-badge').inner_text() == "40"
    # And on no other page: the badge is a property of the page it counts.
    assert page.locator("#pageTabs .tab-badge").count() == 1


def test_the_collapsed_button_never_wears_another_page_s_count(page):
    """The regression itself. Collapsed, the button is labelled with the
    page that is up — SQL here — so a count on it reads as SQL's."""
    _clear_watchlist(page)
    _seed_hits(page)
    page.evaluate("() => __winnow.showSqlTab()")
    _set_pages_menu(page, True)
    btn = page.locator("#pagesMenuBtn")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('SQL')")
    assert btn.locator(".tab-badge").count() == 0
    # It still says there is news — as a dot, which claims no owner, and a
    # title that names the one it has.
    assert btn.locator(".pages-news-dot").count() == 1
    assert "watchlist" in btn.get_attribute("title").lower()

    # The number itself is in the menu, on the row that owns it.
    btn.click()
    page.wait_for_selector(".menu")
    wl = page.locator(".menu .menu-item", has_text="Watchlist")
    assert wl.locator(".tab-badge").inner_text() == "40"
    assert page.locator(".menu .menu-item", has_text="SQL").locator(".tab-badge").count() == 0
    assert page.locator(".menu .tab-badge").count() == 1
    page.keyboard.press("Escape")
    page.evaluate("() => __winnow.showGridTab()")


def test_the_dot_and_the_menu_count_clear_together(page):
    """Looking at the Watchlist is what marks the hits seen, and the
    collapsed strip has to hear about it — the dot outliving the count was
    the same class of lie in a quieter voice."""
    _clear_watchlist(page)
    _seed_hits(page)
    _set_pages_menu(page, True)
    page.wait_for_selector("#pagesMenuBtn .pages-news-dot")
    page.evaluate("() => __winnow.showWatchlistTab()")
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.wait_for_selector("#pagesMenuBtn .pages-news-dot", state="detached")
    page.locator("#pagesMenuBtn").click()
    page.wait_for_selector(".menu")
    assert page.locator(".menu .tab-badge").count() == 0
    page.keyboard.press("Escape")
    page.evaluate("() => __winnow.showGridTab()")

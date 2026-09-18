"""Saving picked rows as a new table from the row menu: the rows the
analyst chose become a table of their own, opened on the spot with their
count and the ⊂ subset badge, and Filters ▾ offers the whole-view form.

The server and its case are session-scoped, so the table this creates is
removed again and the original tab put back — with the fetch-quiet dance
tests/ui/test_sql_interactions.py uses for exactly the same cleanup."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


def _count_fetches(page):
    page.evaluate("""() => {
      window.__inflightFetches = 0;
      window.__totalFetches = 0;
      const orig = window.fetch;
      window.fetch = (...a) => {
        window.__inflightFetches++;
        window.__totalFetches++;
        return orig(...a).finally(() => window.__inflightFetches--);
      };
    }""")


def _wait_fetch_quiet(page, quiet_ms=600, timeout_s=20):
    """No fetch() at all for `quiet_ms` — networkidle already fired at page
    load, so mid-session it resolves at once and says nothing."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_function("() => window.__inflightFetches === 0")
        seen = page.evaluate("() => window.__totalFetches")
        page.wait_for_timeout(quiet_ms)
        if page.evaluate("(n) => window.__inflightFetches === 0 && window.__totalFetches === n", seen):
            return
    raise AssertionError("page fetch activity never went quiet")


def _remove_table(page, sid):
    """Hand the grid back to the fixture table, then drop the saved one only
    once nothing can still be asking for it (a deleted source's id is
    reused — same stale-view guard as the Tables manager's remove)."""
    _wait_fetch_quiet(page)
    page.evaluate("() => __winnow.openSource(1)")
    page.wait_for_function("() => __winnow.S.sourceId === 1")
    _wait_fetch_quiet(page)
    page.evaluate("""(id) => fetch('/api/source/' + id, { method: 'DELETE',
      headers: { 'X-Timeline-Lite-Client': '1' } })""", sid)
    page.evaluate("(id) => __winnow.S.viewCache.delete(id)", sid)
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function("(id) => !__winnow.S.sources.some((s) => s.id === id)", arg=sid)


def test_row_menu_saves_the_picked_rows_as_a_badged_table(page, row_menu, flyout):
    _count_fetches(page)
    # Three picks, then right-click one of them: the menu acts on the picks.
    page.evaluate("() => { __winnow.selReplace(false, new Set([1, 3, 4])); __winnow.render(); }")
    hosts = page.evaluate("() => [1, 3, 4].map((p) => __winnow.rowAt(p).cells[2])")
    row_menu(row=3, cell=1)
    sub = flyout("Save as table")
    item = sub.locator(".menu-item", has_text="Save 3 selected rows as new table")
    assert item.count() == 1
    item.click()
    page.wait_for_selector(".confirm-overlay input")
    assert page.locator(".confirm-overlay input").input_value() == "ui.csv — subset"
    page.locator(".confirm-overlay input").fill("picked-rows")
    page.locator(".confirm-card .btn", has_text="OK").first.click()

    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'picked-rows')")
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'picked-rows').id")
    try:
        src = page.evaluate("(id) => __winnow.S.sources.find((s) => s.id === id)", sid)
        assert src["row_count"] == 3 and src["origin"] == "subset"
        assert src["origin_meta"]["parent_name"] == "ui.csv" and src["origin_meta"]["selection"] == "picks"
        # ...and it opened: the grid shows those three rows, in view order
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === 3", arg=sid)
        page.wait_for_function("() => __winnow.rowAt(2) != null")
        assert page.evaluate("() => [0, 1, 2].map((p) => __winnow.rowAt(p).cells[2])") == hosts
        tab = page.locator(f'.tab.tab-subset[data-id="{sid}"]')
        assert tab.count() == 1 and tab.get_attribute("aria-selected") == "true"
        assert tab.inner_text().startswith("⊂ picked-rows")
        assert "Subset of ui.csv (3 of 200 rows)" in tab.get_attribute("title")
        # every sidebar row for it (Open, All tables) wears the same glyph
        rows = page.locator(".sidebar-row .menu-item", has_text="picked-rows")
        assert rows.count() >= 1
        assert rows.count() == page.locator(".sidebar-row .menu-item", has_text="⊂ picked-rows").count()
    finally:
        _remove_table(page, sid)


def test_filters_menu_offers_the_whole_view(page):
    page.click("#btnFilters")
    page.wait_for_selector(".menu")
    assert page.locator(".menu .menu-item", has_text="Save this view as a table").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector(".menu", state="detached")

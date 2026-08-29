"""The sidebar's resize handle, and what hovering a table name tells you.

The tooltip used to name the source file only when a nickname was set, so
the common case — no nickname — hovered to a tooltip that said nothing
about the table. Both are things only a live document can show."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _width(page):
    return page.evaluate("() => document.getElementById('sidebar').getBoundingClientRect().width")


def test_sidebar_drags_wider_and_the_width_persists(page):
    start = _width(page)
    handle = page.locator("#sidebarResize")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 120, box["y"] + 200, steps=8)
    page.mouse.up()

    widened = _width(page)
    assert widened > start + 80, f"expected a wider sidebar, got {start} -> {widened}"
    # Persisted the way the collapsed flag is, and without losing it.
    stored = page.evaluate("() => JSON.parse(localStorage.getItem('winnow.sidebar') || '{}')")
    assert round(stored["width"]) == round(widened)
    assert "collapsed" in stored, "resizing must not drop the visibility pref"

    # Double-click resets to the default.
    handle.dblclick()
    assert abs(_width(page) - 220) < 2


def test_sidebar_resize_is_clamped(page):
    handle = page.locator("#sidebarResize")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + 3, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] - 900, box["y"] + 200, steps=6)  # drag far past zero
    page.mouse.up()
    assert _width(page) >= 140, "a sidebar dragged to nothing can't be grabbed again"
    handle.dblclick()


def test_hovering_a_table_name_shows_its_file_and_path(page):
    row = page.locator("#sidebarList .sidebar-row .menu-item").first
    title = row.get_attribute("title")
    assert "ui.csv" in title, "the source file name belongs in the tooltip"
    assert "/" in title or "\\" in title, "so does the path it was imported from"
    assert "Imported" in title

    # The tab strip carries the same text.
    tab_title = page.locator(".tabs .tab").first.get_attribute("title")
    assert "ui.csv" in tab_title


def test_a_nicknamed_table_shows_both_names(page):
    sid = page.evaluate("() => __winnow.S.sourceId")
    page.evaluate("""(id) => fetch('/api/source/' + id + '/nickname', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname: 'Host A events' }) }).then((r) => r.status)""", sid)
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.nickname === 'Host A events')")
    try:
        title = page.locator("#sidebarList .sidebar-row .menu-item").first.get_attribute("title")
        # Which file this nickname stands for is the whole question a
        # nickname raises, so both names have to be there.
        assert "Host A events" in title and "ui.csv" in title
    finally:
        page.evaluate("""(id) => fetch('/api/source/' + id + '/nickname', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ nickname: '' }) })""", sid)
        page.evaluate("() => __winnow.loadSources()")

"""Ctrl/⌘+F opens Winnow's search box instead of the browser's find bar.

Find-in-page reads the DOM, and the grid keeps only the visible window of
rows there (invariant #6), so Chromium answers "not found" for values that
are in the table. Winnow claims the chord everywhere inside a case — from
the grid, from a filter cell, from the SQL editor (switching to the grid
first, since the toolbar is grid-only chrome) — and stops the native bar.
A dialog keeps its own find, and an analyst who unbinds the chord gets the
browser's behaviour back.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _watch_chord(pg):
    """Record whether the app cancelled the chord. Registered after app.js
    wired its own document listener, so it runs second and sees the verdict
    — the only way from here to tell "Winnow took it" from "the browser's
    find bar opened", which Playwright cannot see at all."""
    pg.evaluate(
        """() => { window.__chordPrevented = null;
             document.addEventListener('keydown', (e) => {
               if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
                 window.__chordPrevented = e.defaultPrevented;
               }
             }); }""")


def test_ctrl_f_opens_and_focuses_the_search_box(page):
    assert sorted(page.evaluate("() => __winnow.S.keymap.focusSearch")) == ["/", "Ctrl+f"]
    assert page.locator("#searchWrap").is_hidden()
    _watch_chord(page)

    page.locator("#body").focus()
    page.keyboard.press("Control+f")

    page.wait_for_selector("#searchWrap:not([hidden])", timeout=5_000)
    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)
    # The native bar must not also open: the chord is cancelled, not shared.
    assert page.evaluate("() => window.__chordPrevented") is True
    # The copy names the chord, or nobody finds out it exists.
    assert "Ctrl+F" in page.locator("#btnSearchToggle").get_attribute("title")


def test_ctrl_f_works_from_a_filter_cell(page):
    """The dispatcher bails on INPUT/TEXTAREA before it looks at the keymap.
    Ctrl+F is claimed above that guard — the search box is exactly what you
    want from a filter cell, and the box the browser would open there is the
    one that lies."""
    cell = page.locator(".fcell input").first
    cell.focus()
    page.wait_for_function("() => document.activeElement.closest('.fcell') !== null", timeout=5_000)
    _watch_chord(page)

    page.keyboard.press("Control+f")

    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)
    assert page.evaluate("() => window.__chordPrevented") is True


def test_ctrl_f_from_the_sql_editor_switches_to_the_grid(page):
    """syncTabChrome hides the toolbar off the grid, so focusing #search from
    a page tab would put the caret in a display:none input — a keystroke that
    reads as broken. Switch, then open."""
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled",
        timeout=10_000)
    page.locator("#sqlText").focus()
    _watch_chord(page)

    page.keyboard.press("Control+f")

    page.wait_for_function("() => __winnow.S.activeTab === 'grid'", timeout=5_000)
    assert page.locator("#toolbar").is_visible()
    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)
    assert page.evaluate("() => window.__chordPrevented") is True


def test_ctrl_f_dismisses_an_open_menu(page):
    """A dropdown is neither #modal nor a confirm overlay, so nothing else in
    the dispatcher would have closed it — and a search box opening behind a
    menu still floating over it is the worst of both."""
    page.click("#btnFilters")
    page.wait_for_selector(".menu", timeout=5_000)

    page.keyboard.press("Control+f")

    page.wait_for_function("() => document.querySelector('.menu') === null", timeout=5_000)
    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)


def test_ctrl_f_leaves_a_dialog_its_own_find(page):
    """A dialog owns the keyboard and its text really is all in the DOM, so
    find-in-page is the right tool over one. The chord is not cancelled and
    the search box behind the dialog stays shut."""
    page.locator("#body").focus()
    page.keyboard.press("R")
    page.wait_for_selector("#modal:not([hidden])", timeout=5_000)
    _watch_chord(page)

    page.keyboard.press("Control+f")

    page.wait_for_function("() => window.__chordPrevented !== null", timeout=5_000)
    assert page.evaluate("() => window.__chordPrevented") is False
    assert page.locator("#modal").is_visible()
    assert page.locator("#searchWrap").is_hidden()

    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=5_000)


def test_keymap_v5_migration_adds_the_chord_to_an_existing_install(browser, server, first_run_init):
    """loadKeymap persists the whole default map on a profile's first load, so
    without a migration the new binding reaches nobody who has run Winnow
    before."""
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ focusSearch: ['/'] }));"
        "localStorage.setItem('winnow.keymap.v', '4');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert sorted(pg.evaluate("() => __winnow.S.keymap.focusSearch")) == ["/", "Ctrl+f"]
        pg.locator("#body").focus()
        pg.keyboard.press("Control+f")
        pg.wait_for_function("() => document.activeElement === document.getElementById('search')",
                             timeout=5_000)
    finally:
        ctx.close()


def test_a_rebound_search_key_keeps_the_browsers_find(browser, server, first_run_init):
    """A binding the analyst chose is never migrated over — and because the
    chord is only claimed while focusSearch still holds it, taking it off in
    Settings really does hand Ctrl+F back to the browser."""
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ focusSearch: ['p'] }));"
        "localStorage.setItem('winnow.keymap.v', '4');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert pg.evaluate("() => __winnow.S.keymap.focusSearch") == ["p"]
        _watch_chord(pg)
        pg.locator("#body").focus()
        pg.keyboard.press("Control+f")
        pg.wait_for_function("() => window.__chordPrevented !== null", timeout=5_000)
        assert pg.evaluate("() => window.__chordPrevented") is False
        assert pg.locator("#searchWrap").is_hidden()
        # …and the key they chose still opens the box.
        pg.keyboard.press("p")
        pg.wait_for_function("() => document.activeElement === document.getElementById('search')",
                             timeout=5_000)
    finally:
        ctx.close()

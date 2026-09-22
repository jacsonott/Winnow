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
    # The copy names the chord, or nobody finds out it exists — spelled the
    # way the Settings chip spells it, since both come off the same binding.
    assert page.locator("#btnSearchToggle").get_attribute("title").endswith("press / or Ctrl+f")


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
    the search box behind the dialog stays shut.

    This pins the carve-out rather than the feature: it passed before the
    chord was claimed at all, and it is here so that a later gate moved
    above the dialog check has something to fail."""
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
    Settings really does hand Ctrl+F back to the browser.

    The dispatch half of that pins a carve-out (the chord reached the
    browser here before this change too); the copy half does not — the
    toolbar used to name Ctrl+F in a fixed string whatever the binding
    said."""
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
        # …and the copy names what they bound, not a key the browser now
        # answers to.
        title = pg.locator("#btnSearchToggle").get_attribute("title")
        assert title.endswith("press p"), title
        assert "Ctrl" not in title
        # …and the key they chose still opens the box.
        pg.keyboard.press("p")
        pg.wait_for_function("() => document.activeElement === document.getElementById('search')",
                             timeout=5_000)
        placeholder = pg.locator("#search").get_attribute("placeholder")
        assert placeholder.endswith("p"), placeholder
        assert "Ctrl" not in placeholder
    finally:
        ctx.close()


def test_meta_f_opens_the_box_for_a_mac_hand(page):
    """⌘+F is spelled in the dispatcher, not bound a second time in the
    keymap (which has no platform branch anywhere), so if that gate ever
    narrows to the one stored spelling nothing else in the app catches it."""
    _watch_chord(page)
    page.locator("#body").focus()

    page.keyboard.press("Meta+f")

    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)
    assert page.evaluate("() => window.__chordPrevented") is True


def test_the_capital_spelling_opens_the_box(page):
    """With Caps Lock on — or Shift held — the press arrives as e.key 'F',
    which the stored spelling 'Ctrl+f' would never match. Dispatched by hand
    because Playwright's Control+Shift+f still delivers 'f' in Chromium."""
    page.locator("#body").focus()

    cancelled = page.evaluate(
        """() => !document.dispatchEvent(new KeyboardEvent('keydown',
             { key: 'F', ctrlKey: true, bubbles: true, cancelable: true }))""")

    assert cancelled is True
    page.wait_for_function("() => document.activeElement === document.getElementById('search')",
                           timeout=5_000)


def test_ctrl_alt_f_is_left_to_the_layout(page):
    """Ctrl+Alt is AltGr on a European layout, where AltGr+F is a character
    somebody is typing rather than a chord — the gate excludes it, and the
    keypress goes wherever it was going before."""
    page.locator("#body").focus()

    cancelled = page.evaluate(
        """() => !document.dispatchEvent(new KeyboardEvent('keydown',
             { key: 'F', ctrlKey: true, altKey: true, bubbles: true, cancelable: true }))""")

    assert cancelled is False
    assert page.locator("#searchWrap").is_hidden()


def test_settings_refuses_the_spellings_the_keymap_cannot_store(page):
    """The gate answers to four spec strings and focusSearch stores one, so
    binding any of the others to another action would do nothing at all —
    a chip in Settings for a key that is silently swallowed elsewhere.
    findKeyConflict is the only thing standing between the two."""
    conflict = page.evaluate("() => __winnow.findKeyConflict('Meta+F', 'openSearchAll')")
    assert conflict == "Focus search box"
    assert page.evaluate("() => __winnow.findKeyConflict('Ctrl+f', 'openSearchAll')") == "Focus search box"
    # Search's own chip is not in conflict with itself, and an unrelated
    # chord is still free to bind.
    assert page.evaluate("() => __winnow.findKeyConflict('Meta+F', 'focusSearch')") is None
    assert page.evaluate("() => __winnow.findKeyConflict('Ctrl+k', 'openSearchAll')") is None


def test_the_migration_stands_down_when_the_chord_is_already_spent(browser, server, first_run_init):
    """Ctrl+F matched nothing in Winnow until now, so Settings accepted it
    for any action. Where an analyst spent it on something else, handing it
    to focusSearch as well would put the pre-gate in front of their binding
    and shadow it — with the chip still sitting in Settings looking bound,
    and re-adding it refused as a conflict. So the migration leaves that
    install alone, and their key keeps doing what they asked for."""
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify("
        "  { focusSearch: ['/'], openSearchAll: ['s', 'Ctrl+f'] }));"
        "localStorage.setItem('winnow.keymap.v', '4');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert pg.evaluate("() => __winnow.S.keymap.focusSearch") == ["/"]
        assert pg.evaluate("() => __winnow.S.keymap.openSearchAll") == ["s", "Ctrl+f"]

        pg.locator("#body").focus()
        pg.keyboard.press("Control+f")

        # Their action, not the toolbar box. Asserted on the pane's own
        # controls rather than its title: the sweep dialog has been renamed
        # once already (it scopes now, so it is not always "all tables"),
        # and this test is about which action the chord reached.
        pg.wait_for_selector("#modal:not([hidden])", timeout=5_000)
        assert pg.locator("#modalBody .search-mode-toggle").count() == 1, \
            pg.locator("#modal").inner_text()
        assert pg.locator("#searchWrap").is_hidden()
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#modal[hidden]", state="attached", timeout=5_000)
    finally:
        ctx.close()


_MEASURE_PLACEHOLDER = """() => {
  const box = document.getElementById('search');
  const cs = getComputedStyle(box);
  // Laid out rather than measured on a canvas, so this is an independent
  // check of the box's real geometry and not a re-run of the same sum.
  const probe = document.createElement('span');
  probe.textContent = box.placeholder;
  probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;'
    + `font-family:${cs.fontFamily};font-size:${cs.fontSize};font-weight:${cs.fontWeight}`;
  document.body.append(probe);
  const width = probe.getBoundingClientRect().width;
  probe.remove();
  const room = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  return { text: box.placeholder, fits: width <= room };
}"""


def test_the_placeholder_names_the_keys_without_clipping(page):
    """The placeholder is built from the binding, so its length is not fixed
    — and the mode hint claims the box's right padding, which leaves less
    room in regex mode than in substring. A hint clipped mid-chord advertises
    a key nobody can read, so the keys come off rather than be cut."""
    page.locator("#body").focus()
    page.keyboard.press("Control+f")
    page.wait_for_selector("#searchWrap:not([hidden])", timeout=5_000)

    substring = page.evaluate(_MEASURE_PLACEHOLDER)
    assert "/ or Ctrl+f" in substring["text"]
    assert substring["fits"] is True, substring

    page.click("#searchModeToggle button[data-mode='regex']")
    page.wait_for_function("() => document.getElementById('searchMode').textContent.includes('regex')",
                           timeout=5_000)

    regex = page.evaluate(_MEASURE_PLACEHOLDER)
    assert regex["fits"] is True, regex
    assert regex["text"].startswith("All columns")

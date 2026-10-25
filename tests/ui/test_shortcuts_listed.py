"""Settings → Keyboard shortcuts lists every key Winnow answers to.

The rebindable map lists itself, but a key handled by hand — Ctrl+Enter in
the SQL editor, Ctrl+Space for its autocomplete, the arrows in a menu,
Alt-click on a header — only reached Settings if whoever wrote the handler
also remembered the fixed list, and a good dozen of them never did. The
fixed list is FIXED_KEYS in keymap.js now; these tests hold the pane to
both halves, and pin the keys that went missing by name."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_keys(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .settings-nav-item", has_text="Keyboard shortcuts").click()
    page.wait_for_selector(".settings-pane[data-section='keys'] .settings-keys")
    return page.locator(".settings-pane[data-section='keys']")


def test_every_rebindable_action_is_listed_under_its_own_label(page):
    """An action added to DEFAULT_KEYMAP without an ACTION_LABELS entry
    still gets a row, labelled with its bare action name — `undoFilter`
    where every other row reads as a sentence."""
    pane = _open_keys(page)
    try:
        actions = page.evaluate("() => Object.keys(__winnow.DEFAULT_KEYMAP)")
        labels = page.evaluate("() => __winnow.ACTION_LABELS")
        assert [a for a in actions if not labels.get(a)] == [], "actions with no label"
        shown = pane.locator(".settings-key-row .settings-key-label").all_inner_texts()
        assert shown == [labels[a] for a in actions]
    finally:
        page.keyboard.press("Escape")


def test_the_hand_dispatched_keys_are_listed(page):
    pane = _open_keys(page)
    try:
        groups = page.evaluate("() => __winnow.FIXED_KEYS")
        where = pane.locator(".settings-fixed-where").all_inner_texts()
        assert where == [g[0] for g in groups]
        tables = pane.locator(".settings-fixed-keys")
        for i, (_, keys) in enumerate(groups):
            assert tables.nth(i).locator("kbd").all_inner_texts() == [k for k, _ in keys]

        listed = set(pane.locator(".settings-fixed-keys kbd").all_inner_texts())
        # Each of these is handled outside the keymap and was missing
        # from Settings before FIXED_KEYS: run a query / a search-all
        # (plugins.js, search.js), SQL autocomplete (sqlassist.js), menu
        # navigation and the keyboard context menu (ui.js, sources.js),
        # Settings' own section list, the splash skip, and the modified
        # clicks on cells, row numbers and headers (grid.js, columns.js).
        for key in ("Ctrl/⌘ + Enter", "Ctrl + Space", "↑ / ↓", "→ / ←",
                    "Menu key, Shift + F10", "↑ / ↓, Home / End", "Any key or click",
                    "Shift-click a cell", "Ctrl/⌘-click a cell", "Shift-click a row number",
                    "Shift-click a header", "Alt-click a header", "Right-click a tag chip"):
            assert key in listed, f"{key!r} is not in Settings → Keyboard shortcuts"
    finally:
        page.keyboard.press("Escape")

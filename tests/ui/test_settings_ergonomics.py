"""Settings, the small things: one section shown and the list says which,
the card holds still while sections change, the skin tile is clickable,
and Saved filters opened from here comes back here."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_settings(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector("#modalBody .settings-nav-item")


def test_one_section_is_shown_and_the_list_says_which(page):
    _open_settings(page)
    items = page.locator("#modalBody .settings-nav-item")
    states = {items.nth(i).inner_text().strip().lower(): items.nth(i).get_attribute("aria-selected")
              for i in range(items.count())}
    assert list(states.values()).count("true") == 1, states
    assert all(v in ("true", "false") for v in states.values()), states
    # The pane on screen is the one the selected item names.
    shown = page.locator("#modalBody .settings-pane:not([hidden])")
    assert shown.count() == 1
    assert shown.get_attribute("aria-labelledby") == \
        page.locator("#modalBody .settings-nav-item[aria-selected='true']").get_attribute("id")
    # Shut down — the thing Settings is often opened for on the way out —
    # is one click away, at the bottom of the list.
    page.locator("#modalBody .settings-nav-item", has_text="Shut down").click()
    assert page.locator("#modalBody .btn.danger", has_text="Shut down Winnow").is_visible()
    page.keyboard.press("Escape")


def test_the_card_holds_still_while_sections_change(page):
    """A card sized to its content re-centres (or regrows) on every
    change, so the header jumped. The panes are a fixed height instead:
    from Shut down's one button to the keymap's forty rows, the card is
    the same box, and the long section scrolls inside its own pane."""
    _open_settings(page)
    vh = page.evaluate("() => window.innerHeight")
    page.locator("#modalBody .settings-nav-item", has_text="Shut down").click()
    short = page.locator(".modal-card").bounding_box()
    page.locator("#modalBody .settings-nav-item", has_text="Keyboard shortcuts").click()
    page.wait_for_selector(".settings-pane[data-section='keys'] .settings-keys")
    long = page.locator(".modal-card").bounding_box()
    for k in ("x", "y", "width", "height"):
        assert abs(long[k] - short[k]) < 1, (k, short, long)
    assert long["height"] <= vh * 0.82 + 1
    # The body itself never scrolls; the section's pane does.
    assert page.evaluate("() => { const b = document.getElementById('modalBody'); return b.scrollHeight <= b.clientHeight + 1; }")
    assert page.evaluate("""() => { const p = document.querySelector('#modalBody .settings-pane:not([hidden])');
      return p.scrollHeight > p.clientHeight + 50 && getComputedStyle(p).overflowY === 'auto'; }""")
    page.keyboard.press("Escape")


def test_clicking_the_skin_tile_opens_the_picker(page):
    _open_settings(page)
    page.click("#modalBody .settings-nav-item:has-text('Appearance')")
    tile = page.locator(".appearance-current .style-card-static")
    assert tile.count() == 1
    assert page.evaluate("() => getComputedStyle(document.querySelector('.appearance-current .style-card-static')).cursor") == "pointer"
    top_before = page.locator(".modal-card").bounding_box()["y"]
    tile.click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Skins'")
    # Same top anchor as Settings: the card must not jump to centre.
    assert abs(page.locator(".modal-card").bounding_box()["y"] - top_before) < 2
    page.locator("#modalBody .btn", has_text="Done").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    assert page.locator("#modalBody .settings-pane:not([hidden])").get_attribute("data-section") == "appearance"
    page.keyboard.press("Escape")


def test_saved_filters_opened_from_settings_returns_to_settings(page):
    _open_settings(page)
    page.click("#modalBody .settings-nav-item:has-text('Saved filters')")
    page.locator("#modalBody .btn", has_text="Open saved filters…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Saved filters'")
    # A way back in the footer...
    assert page.locator("#modalBody .sf-back").count() == 1
    # ...and the × goes back too, rather than to the grid.
    page.click("#modalClose")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    assert page.locator("#modal").is_visible()
    assert page.locator("#modalBody .settings-pane:not([hidden])").get_attribute("data-section") == "filters"
    # Closing Settings itself closes for real — the hook was one-shot.
    page.click("#modalClose")
    page.wait_for_selector("#modal", state="hidden")
    assert page.locator("#modal").is_hidden()

    # Opened from Filters ▾ instead: no back button, × closes to the grid.
    page.locator("#btnFilters").click()
    page.wait_for_selector(".menu")
    page.locator(".menu .menu-item", has_text="Saved filters…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Saved filters'")
    assert page.locator("#modalBody .sf-back").count() == 0
    page.click("#modalClose")
    page.wait_for_selector("#modal", state="hidden")


def test_escape_on_a_confirm_inside_saved_filters_stays_there(page):
    """The confirm's own Escape closes the confirm; the keymap's must not
    also close the modal behind it (and so bounce to Settings)."""
    _open_settings(page)
    page.click("#modalBody .settings-nav-item:has-text('Saved filters')")
    page.locator("#modalBody .btn", has_text="Open saved filters…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Saved filters'")
    page.evaluate("() => { __winnow.confirmDialog('Keep going?'); }")   # not awaited: it resolves when the dialog closes
    page.wait_for_selector(".confirm-overlay")
    page.keyboard.press("Escape")
    page.wait_for_selector(".confirm-overlay", state="detached")
    assert page.evaluate("() => document.getElementById('modalTitle').textContent") == "Saved filters"
    assert page.locator("#modal").is_visible()
    page.click("#modalClose")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    page.click("#modalClose")
    page.wait_for_selector("#modal", state="hidden")

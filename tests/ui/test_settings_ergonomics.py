"""Settings, the small things: Shut down arrives open, the card is no
taller than its sections, the skin tile is clickable, and Saved filters
opened from here comes back here."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_settings(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector("#modalBody .settings-section")


def test_shut_down_is_open_on_arrival_and_everything_else_is_not(page):
    _open_settings(page)
    heads = page.locator("#modalBody .settings-section-head")
    states = {heads.nth(i).locator(".settings-section-title").inner_text().strip().lower():
              heads.nth(i).get_attribute("aria-expanded") for i in range(heads.count())}
    assert states["shut down"] == "true", states
    assert all(v == "false" for k, v in states.items() if k != "shut down"), states
    assert page.locator("#modalBody .btn.danger", has_text="Shut down Winnow").is_visible()
    page.keyboard.press("Escape")


def test_the_card_is_sized_to_its_content_not_to_the_viewport(page):
    _open_settings(page)
    card = page.locator(".modal-card").bounding_box()
    vh = page.evaluate("() => window.innerHeight")
    # Eleven collapsed headers and one open section fit without a scroll
    # — the card ends at its content, not at the 82vh it used to be pinned to.
    assert card["height"] < vh * 0.82 - 20, (card["height"], vh)
    assert page.evaluate("() => { const b = document.getElementById('modalBody'); return b.scrollHeight <= b.clientHeight + 1; }")
    top_before = card["y"]
    # Opening a section grows the card downward; the top edge stays put.
    page.click(".settings-section-head:has-text('Keyboard shortcuts')")
    page.wait_for_selector(".settings-keys")
    after = page.locator(".modal-card").bounding_box()
    assert after["height"] > card["height"]
    assert abs(after["y"] - top_before) < 2, (after["y"], top_before)
    assert after["height"] <= vh * 0.82 + 1
    page.keyboard.press("Escape")


def test_clicking_the_skin_tile_opens_the_picker(page):
    _open_settings(page)
    page.click(".settings-section-head:has-text('Appearance')")
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
    page.keyboard.press("Escape")


def test_saved_filters_opened_from_settings_returns_to_settings(page):
    _open_settings(page)
    page.click(".settings-section-head:has-text('Saved filters')")
    page.locator("#modalBody .btn", has_text="Open saved filters…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Saved filters'")
    # A way back in the footer...
    assert page.locator("#modalBody .sf-back").count() == 1
    # ...and the × goes back too, rather than to the grid.
    page.click("#modalClose")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    assert page.locator("#modal").is_visible()
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
    page.click(".settings-section-head:has-text('Saved filters')")
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

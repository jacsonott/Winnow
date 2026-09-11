"""Shutting down from Settings.

The ⏻ button lives on the home screen, and the Case ▾ menu has an entry,
but neither is where someone looks once they are inside a case with the
Settings dialog already open. This adds the third door to the same room —
same `shutdownWinnow`, so the same confirmation and the same "still
running" warning, which is the part a second button must not bypass.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_settings(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")


def _shutdown_section(page):
    return page.locator("#modalBody .settings-section", has_text="Shut down")


def test_settings_offers_a_shutdown_and_it_is_last(page):
    _open_settings(page)
    # Open on arrival — the one section that is — so no click to expand it.
    assert _shutdown_section(page).count() == 1
    # The headings are uppercased by CSS, so compare case-insensitively.
    heads = [h.strip().lower() for h in page.locator("#modalBody .settings-section-title").all_inner_texts()]
    assert heads[-1] == "shut down", heads
    page.keyboard.press("Escape")


def test_it_asks_before_stopping_the_server(page):
    """The button must route through the same confirmation as every other
    way out — declining leaves the server up."""
    _open_settings(page)
    # Open on arrival — the one section that is — so no click to expand it.
    _shutdown_section(page).locator("button", has_text="Shut down Winnow").click()
    page.wait_for_selector(".confirm-overlay")
    assert "Shut down the Winnow server?" in page.locator(".confirm-card").inner_text()
    page.locator(".confirm-card .btn", has_text="Keep running").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    # Still alive: the app answers.
    assert page.evaluate("""() => fetch('/api/sources', { headers: { 'X-Timeline-Lite-Client': '1' } })
      .then((r) => r.ok)""") is True
    page.keyboard.press("Escape")


def test_the_dialog_gets_out_of_the_way_first(page):
    """The confirmation is the thing to read; Settings closing behind it is
    what makes it readable."""
    _open_settings(page)
    # Open on arrival — the one section that is — so no click to expand it.
    _shutdown_section(page).locator("button", has_text="Shut down Winnow").click()
    page.wait_for_selector(".confirm-overlay")
    assert page.locator("#modal").is_hidden()
    page.locator(".confirm-card .btn", has_text="Keep running").click()
    page.wait_for_selector(".confirm-overlay", state="detached")

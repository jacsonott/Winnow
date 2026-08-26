"""Backdrop close needs BOTH ends of the gesture on the backdrop.

The press-side guard (an earlier fix) covered select-drags out of the
card; a drag that starts on the backdrop and releases inside the card
also resolves its click to the backdrop via the common-ancestor rule —
that one closed the Search-all modal when highlighting from the card's
edge or clicking just wide of it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_backdrop_close_requires_press_and_release_there(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    card = page.locator(".modal-card").bounding_box()

    # backdrop → card (the newly reported direction)
    page.mouse.move(card["x"] - 60, card["y"] + 100)
    page.mouse.down()
    page.mouse.move(card["x"] + 200, card["y"] + 100, steps=5)
    page.mouse.up()
    page.wait_for_timeout(150)
    assert not page.locator("#modal").is_hidden(), "a drag from the backdrop into the card closed the modal"

    # card → backdrop (must stay fixed)
    page.mouse.move(card["x"] + 200, card["y"] + 130)
    page.mouse.down()
    page.mouse.move(card["x"] - 80, card["y"] + 130, steps=5)
    page.mouse.up()
    page.wait_for_timeout(150)
    assert not page.locator("#modal").is_hidden(), "a select-drag out of the card closed the modal"

    page.mouse.click(card["x"] - 60, card["y"] + 100)
    page.wait_for_timeout(150)
    assert page.locator("#modal").is_hidden(), "a deliberate backdrop click must still close"

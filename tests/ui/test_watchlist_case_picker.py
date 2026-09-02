"""The watchlist's "From a case…" picker: opens from the header, and with
no other registered case holding indicators it says so instead of
rendering an empty list. (The import itself is covered end-to-end by
tests/test_watchlist_from_case.py — building a second real case file
inside the shared UI server isn't worth what it would add.)"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_picker_opens_with_honest_empty_state(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator("#wlFromCase").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "watchlist from a case"
    page.wait_for_selector("#modal .note-status:has-text('No other recent case')")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

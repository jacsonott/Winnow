"""The table sidebar: closed by default when a case opens (the evidence
first, navigation on demand), toggled with ` — and the analyst's choice
persists once made."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _fresh(own_page):
    """No sidebar key, unlike every other page here: what the sidebar does
    unset is the question."""
    return own_page(seed="localStorage.setItem('winnow.remotePrompt', 'seen');"
                         "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")


def test_a_case_opens_with_the_sidebar_closed(own_page):
    with _fresh(own_page) as pg:
        assert pg.evaluate("() => document.getElementById('sidebar').hidden")


def test_backtick_toggles_and_the_choice_persists(own_page):
    with _fresh(own_page) as pg:
        pg.keyboard.press("`")
        pg.wait_for_function("() => !document.getElementById('sidebar').hidden")
        pg.keyboard.press("`")
        pg.wait_for_function("() => document.getElementById('sidebar').hidden")
        pg.keyboard.press("`")   # leave it open, then reload: the choice sticks
        pg.wait_for_function("() => !document.getElementById('sidebar').hidden")
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector(".row")
        assert not pg.evaluate("() => document.getElementById('sidebar').hidden")


def test_backtick_in_an_input_types_a_backtick(page):
    page.keyboard.press("/")
    page.keyboard.press("`")
    assert page.evaluate("() => document.getElementById('search').value") == "`"
    was_hidden = page.evaluate("() => document.getElementById('sidebar').hidden")
    assert was_hidden is False   # shared fixture keeps it open
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")

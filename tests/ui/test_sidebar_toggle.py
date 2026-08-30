"""The table sidebar: closed by default when a case opens (the evidence
first, navigation on demand), toggled with ` — and the analyst's choice
persists once made."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _fresh(browser, server):
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    return ctx, pg


def test_a_case_opens_with_the_sidebar_closed(browser, server):
    ctx, pg = _fresh(browser, server)
    try:
        assert pg.evaluate("() => document.getElementById('sidebar').hidden")
    finally:
        ctx.close()


def test_backtick_toggles_and_the_choice_persists(browser, server):
    ctx, pg = _fresh(browser, server)
    try:
        pg.keyboard.press("`")
        pg.wait_for_function("() => !document.getElementById('sidebar').hidden")
        pg.keyboard.press("`")
        pg.wait_for_function("() => document.getElementById('sidebar').hidden")
        pg.keyboard.press("`")   # leave it open, then reload: the choice sticks
        pg.wait_for_function("() => !document.getElementById('sidebar').hidden")
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector(".row")
        assert not pg.evaluate("() => document.getElementById('sidebar').hidden")
    finally:
        ctx.close()


def test_backtick_in_an_input_types_a_backtick(page):
    page.keyboard.press("/")
    page.keyboard.press("`")
    assert page.evaluate("() => document.getElementById('search').value") == "`"
    was_hidden = page.evaluate("() => document.getElementById('sidebar').hidden")
    assert was_hidden is False   # shared fixture keeps it open
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")

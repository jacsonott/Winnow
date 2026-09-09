"""Appearance → "Pages as a dropdown" collapses the page strip into one
Pages ▾ button; the search toggle is a themed SVG rather than an emoji;
group headers no longer paint their values in the accent."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_pages_dropdown_setting(page):
    page.evaluate("() => { __winnow.S.appearance.pagesMenu = true; __winnow.saveAppearance(); __winnow.renderPageTabs(); }")
    page.wait_for_selector("#pagesMenuBtn")
    # Individual page tabs are hidden; the one button opens a menu of them.
    assert not page.locator('#pageTabs .tab[data-page-key="sql"]').is_visible()
    page.locator("#pagesMenuBtn").click()
    item = page.locator(".menu .menu-item", has_text="Notes")
    assert item.count() == 1
    item.click()
    page.wait_for_selector("#notesview:not([hidden])")
    # The button reads as the current page.
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.startsWith('Notes')")
    assert page.locator("#pagesMenuBtn").get_attribute("aria-selected") == "true"
    # Off again → the strip is back.
    page.evaluate("() => { __winnow.S.appearance.pagesMenu = false; __winnow.saveAppearance(); __winnow.renderPageTabs(); }")
    page.wait_for_selector("#pagesMenuBtn", state="detached")
    assert page.locator('#pageTabs .tab[data-page-key="sql"]').is_visible()
    # It's exposed in Settings → Appearance.
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modal label", has_text="Pages as a dropdown").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_search_icon_is_an_svg_in_the_theme_colour(page):
    btn = page.locator("#btnSearchToggle")
    assert btn.locator("svg").count() == 1
    assert "🔍" not in btn.inner_text()
    stroke = page.evaluate("() => getComputedStyle(document.querySelector('#btnSearchToggle svg')).stroke")
    # currentColor → resolves to the button's own text colour, never a fixed blue.
    assert stroke not in ("rgb(0, 0, 255)", "blue")


def test_group_header_value_is_not_the_accent(page):
    page.evaluate("() => __winnow.setGrouping(['Host'], 'count', 'desc')")
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
    page.wait_for_selector(".group-header-value")
    color = page.evaluate("() => getComputedStyle(document.querySelector('.group-header-value')).color")
    accent = page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    # Normalise the accent hex to rgb() for comparison.
    r, g, b = int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16)
    assert color != f"rgb({r}, {g}, {b})"
    page.evaluate("() => __winnow.dropGrouping()")

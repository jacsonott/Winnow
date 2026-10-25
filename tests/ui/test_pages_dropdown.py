"""The Pages dropdown, sized to the page it names.

The page strip and the table strip share one bar, which is why the
dropdown exists: a case with several plugin or pinned-dashboard tabs wins
width the tables needed. It is no longer the default — the compact strip
is (see test_page_tabs_badges.py) — but it stays a choice, so this file
turns it on for itself. Collapsed, the strip IS one button whose label is
the page that is up, so it has to size to that label rather than to a
width dragged for the expanded strip.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _pages_btn(page):
    return page.locator("#pagesMenuBtn")


def _width(page):
    return page.evaluate("() => document.getElementById('pageTabs').getBoundingClientRect().width")


@pytest.fixture(autouse=True)
def dropdown_on(page):
    """The strip is expanded by default now, so this file turns the
    preference on for itself and puts it back afterwards."""
    page.evaluate("""() => { __winnow.S.appearance.pagesMenu = true;
      __winnow.S.pageTabPrefs.width = null;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""")
    yield
    page.evaluate("""() => { __winnow.S.appearance.pagesMenu = false;
      __winnow.S.pageTabPrefs.width = null;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""")


def test_a_fresh_install_gets_the_strip_not_the_dropdown(page):
    """Flipped deliberately: four labelled pages that are always there beat
    one button that is not, now that the strip is compact enough to afford
    them. The preference itself still collapses the strip."""
    assert page.evaluate("() => __winnow.defaultAppearance().pagesMenu") is False
    assert _pages_btn(page).count() == 1   # this file's fixture turned it on


def test_the_button_names_the_page_that_is_up(page):
    assert "Pages" in _pages_btn(page).inner_text()   # grid is up: no page selected
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('SQL')")
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('Pages')")


def test_the_strip_resizes_to_fit_the_selected_page(page):
    """The whole complaint: it kept the width it had, so a long page name
    clipped and a short one rattled around."""
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('Pages')")
    narrow = _width(page)
    page.evaluate("() => __winnow.showTimelineTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('Timeline')")
    wide = _width(page)
    assert wide > narrow, (narrow, wide)
    # and the button is not clipped by the strip it sits in
    assert page.evaluate("""() => { const b = document.getElementById('pagesMenuBtn');
      return b.scrollWidth <= b.clientWidth + 1; }""")


def test_a_stored_drag_width_does_not_squeeze_the_dropdown(page):
    """Turning the dropdown on with a width dragged for the expanded strip
    must not leave that width applied — but must not forget it either."""
    page.evaluate("""() => { __winnow.S.pageTabPrefs.width = 320;
      __winnow.S.appearance.pagesMenu = true;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""")
    assert _width(page) < 320
    assert page.evaluate("() => __winnow.S.pageTabPrefs.width") == 320   # kept for when it goes back off
    page.evaluate("""() => { __winnow.S.appearance.pagesMenu = false;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""")
    page.wait_for_function("() => Math.abs(document.getElementById('pageTabs').getBoundingClientRect().width - 320) < 2")


def test_the_drag_handle_is_gone_while_collapsed(page):
    """It sets a width the dropdown ignores."""
    assert page.locator("#tabSplit").is_hidden()
    page.evaluate("""() => { __winnow.S.appearance.pagesMenu = false;
      __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""")
    assert page.locator("#tabSplit").is_visible()


def test_the_dropdown_still_switches_pages(page):
    _pages_btn(page).click()
    page.wait_for_selector(".menu")
    page.locator(".menu .menu-item", has_text="Timeline").first.click()
    page.wait_for_function("() => __winnow.S.activeTab === 'timeline'")
    page.evaluate("() => __winnow.showGridTab()")

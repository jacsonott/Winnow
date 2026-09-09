"""The "N selected" tagging bar belongs to the grid.

It is `position: fixed` at the bottom of the viewport, so with rows
selected it followed the analyst onto SQL, Timeline, Notes, the watchlist,
a dashboard and every plugin tab — offering to tag rows that nothing on
those pages has, beside a Clear selection button for a selection they
could not see.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def two_selected(page):
    page.evaluate("() => __winnow.showGridTab()")
    page.locator("#body .row").nth(1).locator(".rowcheck").check()
    page.locator("#body .row").nth(2).locator(".rowcheck").check()
    page.wait_for_selector("#tagToolbar:not([hidden])")
    yield
    page.evaluate("() => { __winnow.selClear(); __winnow.render(); }")
    page.evaluate("() => __winnow.showGridTab()")


def test_it_shows_on_the_grid_with_a_selection(page, two_selected):
    assert page.locator("#tagToolbar").is_visible()
    assert "2 selected" in page.locator("#tagToolbar").inner_text()


@pytest.mark.parametrize("go", ["showSqlTab", "showTimelineTab", "showNotesTab", "showWatchlistTab"])
def test_it_does_not_follow_the_analyst_to_another_page(page, two_selected, go):
    page.evaluate(f"() => __winnow.{go}()")
    page.wait_for_function("() => __winnow.S.activeTab !== 'grid'")
    assert page.locator("#tagToolbar").is_hidden(), go


def test_the_selection_itself_survives_the_trip(page, two_selected):
    """Hidden, not cleared — the rows are still picked when you come back."""
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => __winnow.S.activeTab === 'sql'")
    assert page.evaluate("() => __winnow.selCount()") == 2
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_selector("#tagToolbar:not([hidden])")
    assert "2 selected" in page.locator("#tagToolbar").inner_text()


def test_it_stays_hidden_on_another_page_when_the_selection_changes(page, two_selected):
    """Selection can change while away — a plugin tab can select rows —
    and the bar must not reappear over that page."""
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => __winnow.S.activeTab === 'sql'")
    page.evaluate("() => { __winnow.selAdd(5); __winnow.renderTagToolbar(); }")
    assert page.locator("#tagToolbar").is_hidden()

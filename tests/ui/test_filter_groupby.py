"""The filter builder can set/edit a Group by — so a filter carries a
grouping the way the shipped defaults do, not just the ones hand-authored
in defaults/filters.json."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_group_by_in_the_builder_applies_and_is_saved(page):
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal .fb-groupby")
    # choose a grouping column from the builder's own control
    page.locator("#modal .fb-groupby-add").select_option("EventId")
    page.wait_for_selector("#modal .fb-groupby-chip:has-text('EventId')")
    page.locator("#modal button", has_text="Apply").click()
    # grouping is now live on the grid…
    page.wait_for_function("() => (__winnow.S.groupByCols || []).includes('EventId')")
    # …and rides along in what a saved filter would persist
    assert page.evaluate("() => __winnow.currentFilterPayload().group_by") == ["EventId"]
    # reopening the builder shows it as an editable chip (not lost)
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal .fb-groupby-chip:has-text('EventId')")
    page.keyboard.press("Escape")
    # leave the shared case ungrouped
    page.evaluate("() => { __winnow.dropGrouping(); return __winnow.rebuildView({ keepScroll: false }); }")
    page.wait_for_function("() => (__winnow.S.groupByCols || []).length === 0")

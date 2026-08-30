"""The Timeline tab: tagged rows across the case, in one place. The
backend (build_timeline) is tested in test_timeline.py; nothing drove the
tab itself — the wiring, the virtualized rows, the empty state."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_timeline_shows_tagged_rows_and_empties_on_untag(page):
    # Tag one row with the first default tag's hotkey.
    cell = page.locator(".row").nth(1).locator(".cell").nth(1)
    cell.click()
    page.keyboard.press("1")
    page.wait_for_timeout(250)

    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_selector(".timeline-row", timeout=10_000)
    assert page.locator(".timeline-row").count() >= 1
    assert page.locator("#timelineStats").inner_text() != ""

    # Back to the grid, toggle the tag off (leaves the shared case clean).
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    page.keyboard.press("1")
    page.wait_for_timeout(250)

    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_function(
        "() => document.querySelectorAll('.timeline-row').length === 0", timeout=10_000)

    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")

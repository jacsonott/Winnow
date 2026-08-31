"""Mouse back/forward buttons walk the recently-visited page tabs — the
browser's own history has no entries in a single-page app, so the thumb
buttons were dead weight. Playwright's mouse API has no thumb buttons;
the synthetic MouseEvent exercises the same window listener."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _press(page, button):
    page.evaluate(f"() => window.dispatchEvent(new MouseEvent('mouseup', {{ button: {button} }}))")
    page.wait_for_timeout(150)


def test_thumb_buttons_walk_the_recent_tabs(page):
    src = page.evaluate("() => __winnow.S.sourceId")
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_selector("#sqlview:not([hidden])")

    _press(page, 3)   # back → timeline
    assert page.evaluate("() => __winnow.S.activeTab") == "timeline"
    _press(page, 3)   # back → the grid tab we started on
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
    assert page.evaluate("() => __winnow.S.sourceId") == src

    _press(page, 4)   # forward → timeline again
    assert page.evaluate("() => __winnow.S.activeTab") == "timeline"
    _press(page, 4)   # forward → sql
    assert page.evaluate("() => __winnow.S.activeTab") == "sql"
    _press(page, 4)   # already at the newest entry: stays put
    assert page.evaluate("() => __winnow.S.activeTab") == "sql"

    # Restore the grid for whoever runs next.
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector("#grid:not([hidden])")


def test_a_new_visit_truncates_the_forward_side(page):
    src = page.evaluate("() => __winnow.S.sourceId")
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_selector("#sqlview:not([hidden])")
    _press(page, 3)   # back to grid
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
    page.locator("#tabTimeline").click()          # new branch
    page.wait_for_selector("#timelineview:not([hidden])")
    _press(page, 4)   # forward: the sql entry was truncated — nowhere to go
    assert page.evaluate("() => __winnow.S.activeTab") == "timeline"
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector("#grid:not([hidden])")

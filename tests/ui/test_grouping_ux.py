"""Group headers: highlight-to-copy must not toggle; plain click still
does. Group-by pills reorder by drag."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _grouped(page):
    page.evaluate("() => __winnow.addGroupLevel('EventId')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")


def test_selecting_header_text_does_not_toggle(page):
    _grouped(page)
    val = page.locator(".group-header-row").first.locator(".group-header-value")
    box = val.bounding_box()
    before = page.evaluate("() => __winnow.S.groups[0].expanded")
    page.mouse.move(box["x"] + 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2, steps=4)
    page.mouse.up()
    page.wait_for_timeout(200)
    assert page.evaluate("() => window.getSelection().toString()") != ""
    assert page.evaluate("() => __winnow.S.groups[0].expanded") == before

    # a plain click (no selection) still toggles
    page.evaluate("() => window.getSelection().removeAllRanges()")
    page.locator(".group-header-row").first.locator(".group-header-col").click()
    page.wait_for_function(f"() => __winnow.S.groups[0].expanded === {str(not before).lower()}")
    page.evaluate("() => __winnow.dropGrouping()")


def test_group_pills_reorder_by_drag(page):
    page.evaluate("() => __winnow.addGroupLevel('EventId')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function("() => __winnow.S.groupByCols.length === 2")
    pills = page.locator(".group-pill")
    pills.nth(1).drag_to(pills.nth(0))
    page.wait_for_function("() => __winnow.S.groupByCols[0] === 'Host'")
    assert page.evaluate("() => __winnow.S.groupByCols") == ["Host", "EventId"]
    page.evaluate("() => __winnow.dropGrouping()")

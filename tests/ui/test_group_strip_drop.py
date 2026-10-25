"""Dragging into and along the Group by strip lands where the analyst
lets go, and shows where that is before they do.

Reported as "feels like it doesn't work half the time". It didn't: a drop
only counted on the exact element that listened for it. A pill dragged to
reorder had to be released ON another pill, so letting go just under one,
or on the › between two, did nothing. A header released on a pill already
in the strip did nothing either, while the strip lit up as if it would
take it. And nothing on screen said where a drop would land until after.

Playwright's drag emulation sends dragover only when the mouse moves,
where Chromium sends it every 50ms while the pointer rests; `_rest` is
that rest, one small move at the spot, so the drop is judged on the
dragover a real release would have had.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

TAG = "__tag__"


def _group(page, cols):
    for c in cols:
        page.evaluate("(c) => __winnow.addGroupLevel(c)", c)
    page.wait_for_function(
        "(n) => __winnow.S.groupByCols.length === n && __winnow.S.groups.length > 0", arg=len(cols))


def _box(page, selector, i=0):
    return page.locator(selector).nth(i).bounding_box()


def _grab(page, box):
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 6, y - 3, steps=3)


def _rest(page, x, y):
    page.mouse.move(x, y, steps=8)
    page.mouse.move(x + 1, y, steps=2)


# The labels the strip is showing, in the order they read on screen —
# what the analyst sees, whichever element is drawing each one.
SHOWN = """() => [...document.querySelectorAll('#groupStrip .group-pill')]
    .filter((p) => p.offsetParent !== null)
    .map((p) => ({ r: p.getBoundingClientRect(), t: p.firstChild.textContent }))
    .map((p) => ({ ...p, y: p.r.top + p.r.height / 2 }))
    .sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.r.left - b.r.left))
    .map((p) => p.t)"""


def _shows(page, labels):
    page.wait_for_function(f"(want) => JSON.stringify(({SHOWN})()) === JSON.stringify(want)",
                           arg=labels, timeout=5000)


def _levels(page):
    return page.evaluate("() => __winnow.S.groupByCols")


def test_header_released_on_a_pill_is_added_there(page):
    _group(page, ["EventId"])
    try:
        _grab(page, _box(page, ".hcell >> text=Host"))
        pill = _box(page, ".group-pill")
        _rest(page, pill["x"] + 6, pill["y"] + pill["height"] / 2)
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.groupByCols.length === 2")
        assert _levels(page) == ["Host", "EventId"]
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_reorder_released_just_below_a_pill(page):
    _group(page, ["EventId", "Host"])
    try:
        _grab(page, _box(page, ".group-pill", 1))
        first = _box(page, ".group-pill", 0)
        strip = _box(page, "#groupStrip")
        _rest(page, first["x"] + 8, strip["y"] + strip["height"] - 2)
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.groupByCols[0] === 'Host'")
        assert _levels(page) == ["Host", "EventId"]
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_reorder_released_outside_the_strips_border(page):
    """The zone reaches past the dashed border: a release a few pixels
    under the box, still in the toolbar, is a release in the strip."""
    _group(page, ["EventId", "Host"])
    try:
        _grab(page, _box(page, ".group-pill", 1))
        first = _box(page, ".group-pill", 0)
        strip = _box(page, "#groupStrip")
        _rest(page, first["x"] + 8, strip["y"] + strip["height"] + 5)
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.groupByCols[0] === 'Host'")
        assert _levels(page) == ["Host", "EventId"]
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_reorder_released_on_the_separator(page):
    _group(page, ["EventId", "Host", TAG])
    try:
        _grab(page, _box(page, ".group-pill", 0))
        arrow = _box(page, ".group-strip-arrow", 1)       # the › between Host and Tag
        _rest(page, arrow["x"] + arrow["width"] / 2, arrow["y"] + arrow["height"] / 2)
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.groupByCols[0] === 'Host'")
        assert _levels(page) == ["Host", "EventId", TAG]
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_the_strip_shows_where_a_drop_will_land_before_it_happens(page):
    _group(page, ["EventId", "Host"])
    try:
        _grab(page, _box(page, ".hcell >> text=Timestamp"))
        first = _box(page, ".group-pill", 0)
        _rest(page, first["x"] + first["width"] - 4, first["y"] + first["height"] / 2)
        # Between the two levels, on screen, while nothing has changed yet.
        _shows(page, ["EventId", "Timestamp", "Host"])
        assert _levels(page) == ["EventId", "Host"]

        # Taken back down once the pointer leaves the strip, and a release
        # out there leaves the levels, and the strip, as they were.
        body = _box(page, "#body")
        _rest(page, body["x"] + 300, body["y"] + 200)
        _shows(page, ["EventId", "Host"])
        page.mouse.up()
        assert _levels(page) == ["EventId", "Host"]
        assert not page.evaluate("() => document.getElementById('groupStrip').classList.contains('drag-over')")
    finally:
        page.evaluate("() => __winnow.dropGrouping()")


def test_a_pill_being_moved_shows_in_its_new_place(page):
    """Dragged left past its neighbour, a pill reads in its new place
    before the release, not twice and not in its old one."""
    _group(page, ["EventId", "Host"])
    try:
        _grab(page, _box(page, ".group-pill", 1))
        first = _box(page, ".group-pill", 0)
        _rest(page, first["x"] + 6, first["y"] + first["height"] / 2)
        _shows(page, ["Host", "EventId"])
        assert _levels(page) == ["EventId", "Host"]
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.groupByCols[0] === 'Host'")
        _shows(page, ["Host", "EventId"])
    finally:
        page.evaluate("() => __winnow.dropGrouping()")

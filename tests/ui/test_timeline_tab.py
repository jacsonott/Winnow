"""The Timeline tab: tagged rows across the case, in one place. The
backend (build_timeline) is tested in test_timeline.py; nothing drove the
tab itself — the wiring, the virtualized rows, the empty state."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_timeline(page):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")


TIMELINE_COUNT = "(n) => __winnow.S.timeline.view && __winnow.S.timeline.view.row_count === n"


def test_timeline_shows_tagged_rows_and_empties_on_untag(page, api):
    src = page.evaluate("() => __winnow.S.sourceId")
    pos = int(page.locator(".row").nth(1).get_attribute("data-pos"))
    rid = page.evaluate("(p) => __winnow.rowAt(p).rid", pos)
    tag_id = page.evaluate("() => __winnow.S.tags.find((t) => String(t.hotkey) === '1').id")
    has = "([p, id]) => (__winnow.rowAt(p)?.tags || []).includes(id)"
    cell = page.locator(f'.row[data-pos="{pos}"]').locator(".cell").nth(1)

    # Counted from where the case stands rather than from zero. The case is
    # shared by every module in the shard, and a tag one of them left
    # behind failed this test instead of the one that left it (#227).
    _open_timeline(page)
    page.wait_for_function("() => __winnow.S.timeline.view && __winnow.busyCount === 0")
    before = page.evaluate("() => __winnow.S.timeline.view.row_count")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")
    try:
        # Tag one row with the first default tag's hotkey.
        cell.click()
        page.keyboard.press("1")
        page.wait_for_function(has, arg=[pos, tag_id])

        _open_timeline(page)
        page.wait_for_function(TIMELINE_COUNT, arg=before + 1)
        page.wait_for_selector(".timeline-row", timeout=10_000)
        assert page.locator("#timelineStats").inner_text() != ""

        # Back to the grid, toggle the tag off with the same key.
        page.evaluate("(id) => __winnow.openSource(id)", src)
        page.wait_for_selector(".row")
        cell.click()
        page.keyboard.press("1")
        page.wait_for_function(f"(a) => !({has})(a)", arg=[pos, tag_id])

        # The row leaves the timeline; one that started empty empties.
        _open_timeline(page)
        page.wait_for_function(TIMELINE_COUNT, arg=before)
        if before == 0:
            page.wait_for_function(
                "() => document.querySelectorAll('.timeline-row').length === 0", timeout=10_000)

        page.evaluate("(id) => __winnow.openSource(id)", src)
        page.wait_for_selector(".row")
    finally:
        # Straight to the server by row identity, so a failure anywhere
        # above still hands the case on untagged. A no-op when the hotkey
        # already took it off.
        api("/api/row_tags", "POST", {"pairs": [[src, rid]], "tag_id": tag_id, "on": False})

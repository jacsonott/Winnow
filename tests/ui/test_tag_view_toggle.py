"""Shift+<tag key> tags the view, and takes it off again.

It used to mean one thing: tag every row in this view. Pressing it twice
was a confirm dialog for a no-op, and there was no keyboard way back —
the analyst who tagged a 50,000-row view by mistake had to select all and
untag, or undo, if they noticed in time.

It reads the coverage first now. Anything in the view still untagged
means tag the lot; everything already tagged means take it off. So the
direction is decided by the whole view (never by one row — that is what
the unshifted key does, on purpose), a repeat press undoes the press
before it, and the confirm says which of the two it is about to do rather
than offering a bare OK.

The session case is shared, so this works inside a filter and puts
everything back.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

FILTERED = 50      # rows matching EventId 4625 in the 200-row fixture


def _hotkey(page):
    return page.evaluate("() => (__winnow.S.tags.find((t) => t.hotkey) || {}).hotkey")


def _tag(page):
    return page.evaluate("() => __winnow.S.tags.find((t) => t.hotkey)")


def _confirm(page):
    """Take the affirmative and return what it said it would do."""
    page.wait_for_selector(".confirm-overlay")
    said = page.locator(".confirm-card .confirm-message").inner_text()
    label = page.locator(".confirm-card .confirm-actions .btn:not(.ghost)").inner_text()
    page.locator(".confirm-card .confirm-actions .btn:not(.ghost)").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    return said, label


def _narrow(page):
    page.evaluate("""() => {
      __winnow.S.filters = { EventId: '4625' };
      __winnow.renderHead();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function(
        "(n) => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === n",
        arg=FILTERED)


def _reset(page):
    page.evaluate("""() => {
      __winnow.S.filters = {}; __winnow.S.tagFilter = [];
      __winnow.renderHead();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function(
        "() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")


def _tagged_in_view(page, tag_id):
    return page.evaluate("(id) => Number(__winnow.S.tagCounts[id] || 0)", tag_id)


def test_the_second_press_takes_the_tag_back_off(page):
    key = _hotkey(page)
    assert key, "the fixture case has no tag with a hotkey"
    tag = _tag(page)
    try:
        _narrow(page)
        assert _tagged_in_view(page, tag["id"]) == 0

        page.keyboard.press(f"Shift+{key}")
        said, label = _confirm(page)
        assert f"Tag all {FILTERED}" in said, said
        assert label == "Tag them"
        page.wait_for_function("(a) => Number(__winnow.S.tagCounts[a[0]] || 0) === a[1]",
                               arg=[tag["id"], FILTERED], timeout=15_000)

        # Everything in the view carries it now, so the same keystroke can
        # only mean take it off — and says so.
        page.keyboard.press(f"Shift+{key}")
        said, label = _confirm(page)
        assert "Every row in this view is tagged" in said, said
        assert f"Remove it from all {FILTERED}" in said, said
        assert label == "Remove the tag"
        page.wait_for_function("(id) => Number(__winnow.S.tagCounts[id] || 0) === 0",
                               arg=tag["id"], timeout=15_000)
    finally:
        # Whatever happened above, leave the case as found.
        page.evaluate("""(id) => __winnow.post('/api/row_tags/view',
            { view_id: __winnow.S.view.view_id, tag_id: id, on: false })""", tag["id"])
        _reset(page)


def test_the_direction_is_decided_by_the_whole_view_not_one_row(page):
    """A view with one tagged row and forty-nine untagged ones is not
    "tagged" — the press tags, and the confirm says how many already are
    rather than pretending it is starting from nothing."""
    key = _hotkey(page)
    tag = _tag(page)
    try:
        _narrow(page)
        # Tag exactly one row, the unshifted way: cursor on it, press the key.
        page.evaluate("() => { __winnow.S.cursor = 0; }")
        page.keyboard.press(key)
        page.wait_for_function("(id) => Number(__winnow.S.tagCounts[id] || 0) === 1",
                               arg=tag["id"], timeout=15_000)

        page.keyboard.press(f"Shift+{key}")
        said, label = _confirm(page)
        assert f"Tag all {FILTERED}" in said, said
        assert "1 already is" in said, said
        assert label == "Tag them"
        page.wait_for_function("(a) => Number(__winnow.S.tagCounts[a[0]] || 0) === a[1]",
                               arg=[tag["id"], FILTERED], timeout=15_000)
    finally:
        page.evaluate("""(id) => __winnow.post('/api/row_tags/view',
            { view_id: __winnow.S.view.view_id, tag_id: id, on: false })""", tag["id"])
        _reset(page)
        page.evaluate("() => { __winnow.S.cursor = -1; }")


def test_the_rows_outside_the_view_are_not_touched(page):
    """The untag half is the dangerous one: it must reach exactly the rows
    the view holds. A tag put on rows outside the filter has to survive
    the toggle running inside it."""
    key = _hotkey(page)
    tag = _tag(page)
    try:
        # Tag the whole table first, from the unfiltered view.
        _reset(page)
        page.keyboard.press(f"Shift+{key}")
        _confirm(page)
        page.wait_for_function("(a) => Number(__winnow.S.tagCounts[a[0]] || 0) === a[1]",
                               arg=[tag["id"], 200], timeout=15_000)

        # Then untag inside a filter: 50 lose it, 150 keep it.
        _narrow(page)
        page.keyboard.press(f"Shift+{key}")
        said, _ = _confirm(page)
        assert "Every row in this view is tagged" in said, said
        page.wait_for_function("(id) => Number(__winnow.S.tagCounts[id] || 0) === 0",
                               arg=tag["id"], timeout=15_000)

        _reset(page)
        page.wait_for_function("(a) => Number(__winnow.S.tagCounts[a[0]] || 0) === a[1]",
                               arg=[tag["id"], 150], timeout=15_000)
    finally:
        page.evaluate("""(id) => __winnow.post('/api/row_tags/view',
            { view_id: __winnow.S.view.view_id, tag_id: id, on: false })""", tag["id"])
        _reset(page)
        page.wait_for_function("(id) => Number(__winnow.S.tagCounts[id] || 0) === 0",
                               arg=tag["id"], timeout=15_000)

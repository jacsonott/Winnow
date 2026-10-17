"""Excluding a tag from the ribbon.

The ribbon could only narrow TO tags, so "everything except what I already
marked Benign" had no gesture at all. Right-clicking a chip now offers
Exclude, which hides the rows carrying that tag. It is part of the same
tag filter as the chips that narrow, so it ANDs with them, shows on the
chip, and clears the way the rest of the tag filter does.

The session case is shared: this tags two rows and takes the tags back off.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

ROWS = 200


def _exactly(label):
    # has_text alone is a case-insensitive substring: "TA" would find
    # "Any tag" and "(untagged)".
    return re.compile(f"^{re.escape(label)}$")


def _chip(page, label):
    return page.locator("#tagRibbon .tag-chip").filter(has=page.locator(".name", has_text=_exactly(label)))


def _menu_item(page, label):
    return page.locator(".menu .menu-item", has_text=label).first


def _excluded(page):
    return page.evaluate("""() => [...document.querySelectorAll('#tagRibbon .tag-chip.excluded .name')]
      .map((n) => n.textContent)""")


def _wait_rows(page, n):
    page.wait_for_function(
        "(n) => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === n",
        arg=n, timeout=15_000)


def _reset(page):
    page.evaluate("""() => { __winnow.S.tagFilter = []; __winnow.renderTagRibbon();
      return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""")
    _wait_rows(page, ROWS)


@pytest.fixture
def overlapping_tags(page, api):
    """Row 1 carries both tags, row 2 only the second — so excluding the
    first from "rows with the second" has a row to drop. Put back after."""
    tags = page.evaluate("() => __winnow.S.tags.slice(0, 2).map((t) => ({id: t.id, name: t.name}))")
    assert len(tags) >= 2, "the fixture case needs two tags"
    a, b = tags
    src = page.evaluate("() => __winnow.S.sourceId")
    api("/api/row_tags", "POST", {"source_id": src, "rids": [1], "tag_id": a["id"], "on": True})
    api("/api/row_tags", "POST", {"source_id": src, "rids": [1, 2], "tag_id": b["id"], "on": True})
    page.evaluate("() => __winnow.loadTags()")
    page.wait_for_function("(id) => Number(__winnow.S.tagCountsAll[id] || 0) >= 2", arg=b["id"])
    try:
        yield a, b
    finally:
        api("/api/row_tags", "POST", {"source_id": src, "rids": [1], "tag_id": a["id"], "on": False})
        api("/api/row_tags", "POST", {"source_id": src, "rids": [1, 2], "tag_id": b["id"], "on": False})
        page.evaluate("() => __winnow.loadTags()")
        _reset(page)


def test_right_click_excludes_and_a_click_brings_them_back(page, overlapping_tags):
    a, b = overlapping_tags
    try:
        _chip(page, b["name"]).click(button="right")
        _menu_item(page, f"Exclude {b['name']}").click()
        _wait_rows(page, ROWS - 2)
        assert _excluded(page) == [b["name"]]
        # Lit like a filter that is on, but not pressed: it hides these
        # rows, it does not narrow to them.
        assert _chip(page, b["name"]).get_attribute("aria-pressed") == "false"
        # The menu says which way the chip is set.
        _chip(page, b["name"]).click(button="right")
        assert _menu_item(page, f"Exclude {b['name']}").locator(".menu-check").text_content() == "✓"
        page.keyboard.press("Escape")
        # A click on the excluded chip lets its rows back in.
        _chip(page, b["name"]).click()
        _wait_rows(page, ROWS)
        assert _excluded(page) == []
    finally:
        _reset(page)


def test_an_exclusion_narrows_what_the_other_chips_show(page, overlapping_tags):
    """ANDed, not OR'd: rows with the second tag that do not also carry
    the first. A union would have brought every untagged row back."""
    a, b = overlapping_tags
    try:
        _chip(page, b["name"]).click()
        _wait_rows(page, 2)
        _chip(page, a["name"]).click(button="right")
        _menu_item(page, f"Exclude {a['name']}").click()
        _wait_rows(page, 1)
        assert page.evaluate("() => __winnow.rowAt(0).rid") == 2
        # Picking the excluded tag from the menu swaps it back to a
        # narrowing chip rather than leaving both, which would be no rows.
        _chip(page, a["name"]).click(button="right")
        _menu_item(page, f"Filter to {a['name']}").click()
        _wait_rows(page, 2)
        assert _excluded(page) == []
        assert _chip(page, a["name"]).get_attribute("aria-pressed") == "true"
    finally:
        _reset(page)


def test_clear_filters_takes_an_exclusion_off_too(page, overlapping_tags):
    a, b = overlapping_tags
    try:
        _chip(page, a["name"]).click(button="right")
        _menu_item(page, f"Exclude {a['name']}").click()
        _wait_rows(page, ROWS - 1)
        page.click("#btnReset")
        _wait_rows(page, ROWS)
        assert _excluded(page) == []
        assert page.evaluate("() => __winnow.S.tagFilter") == []
    finally:
        _reset(page)


def test_a_tag_group_header_can_exclude_its_tag(page, overlapping_tags):
    """The group menu offers Exclude for a tag group, the way it does for a
    value group, and it lands on the ribbon's chip."""
    a, b = overlapping_tags
    try:
        page.evaluate("() => __winnow.addGroupLevel(__winnow.TAG_GROUP_COLUMN)")
        page.wait_for_function("() => __winnow.busyCount === 0 && (__winnow.S.groups || []).length >= 2")
        header = page.locator(".group-header-row").filter(
            has=page.locator(".group-header-value", has_text=_exactly(a["name"])))
        header.click(button="right")
        _menu_item(page, f"Exclude {a['name']}").click()
        page.wait_for_function("() => __winnow.busyCount === 0")
        assert _excluded(page) == [a["name"]]
        assert page.evaluate("() => __winnow.S.tagFilter") == [f"!{a['id']}"]
    finally:
        page.evaluate("() => __winnow.dropGrouping()")
        _reset(page)

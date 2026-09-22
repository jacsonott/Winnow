"""Ticking more than one tag, and asking for the untagged ones.

The ribbon could only ever hold one thing: clicking a chip replaced the
filter with that tag, and "Any tag" replaced it with all of them. So
"show me what I marked Malware or Lateral movement" was not expressible,
and neither was the question an analyst working through a table asks most
— what is still untagged.

Chips add up now (rows carrying either), there is a "No tags" chip, and
"Any tag" steps aside when something specific is picked, since it already
contains every tag.

The session case is shared: this tags two rows and takes both back off.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

ROWS = 200


def _chip(page, label):
    return page.locator("#tagRibbon .tag-chip", has_text=label).first


def _pressed(page):
    return page.evaluate("""() => [...document.querySelectorAll('#tagRibbon .tag-chip')]
      .filter((c) => c.getAttribute('aria-pressed') === 'true')
      .map((c) => c.textContent.replace(/\\d+$/, '').trim())""")


def _rows(page):
    return page.evaluate("() => (__winnow.S.view || {}).row_count")


def _wait_rows(page, n):
    page.wait_for_function(
        "(n) => __winnow.busyCount === 0 && __winnow.S.view && __winnow.S.view.row_count === n",
        arg=n, timeout=15_000)


@pytest.fixture
def two_tagged_rows(page, api):
    """One row under the first tag, one under the second, put back after."""
    tags = page.evaluate("() => __winnow.S.tags.slice(0, 2).map((t) => ({id: t.id, name: t.name}))")
    assert len(tags) >= 2, "the fixture case needs two tags"
    src = page.evaluate("() => __winnow.S.sourceId")
    api("/api/row_tags", "POST", {"source_id": src, "rids": [1], "tag_id": tags[0]["id"], "on": True})
    api("/api/row_tags", "POST", {"source_id": src, "rids": [2], "tag_id": tags[1]["id"], "on": True})
    page.evaluate("() => __winnow.loadTags()")
    page.wait_for_function("(id) => Number(__winnow.S.tagCountsAll[id] || 0) >= 1", arg=tags[0]["id"])
    try:
        yield tags
    finally:
        api("/api/row_tags", "POST", {"source_id": src, "rids": [1], "tag_id": tags[0]["id"], "on": False})
        api("/api/row_tags", "POST", {"source_id": src, "rids": [2], "tag_id": tags[1]["id"], "on": False})
        page.evaluate("""() => { __winnow.S.tagFilter = [];
          return __winnow.loadTags().then(() => __winnow.rebuildView({ keepScroll: false, keepRow: false })); }""")
        _wait_rows(page, ROWS)


def test_two_chips_mean_rows_carrying_either(page, two_tagged_rows):
    a, b = two_tagged_rows
    try:
        _chip(page, a["name"]).click()
        _wait_rows(page, 1)
        _chip(page, b["name"]).click()
        _wait_rows(page, 2)
        assert sorted(_pressed(page)) == sorted([a["name"], b["name"]])
        # …and clicking one again takes just that one back out.
        _chip(page, a["name"]).click()
        _wait_rows(page, 1)
        assert _pressed(page) == [b["name"]]
    finally:
        page.evaluate("() => { __winnow.S.tagFilter = []; __winnow.renderTagRibbon();"
                      " return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }")
        _wait_rows(page, ROWS)


def test_no_tags_is_what_is_left_to_go_through(page, two_tagged_rows):
    a, b = two_tagged_rows
    try:
        _chip(page, "No tags").click()
        _wait_rows(page, ROWS - 2)
        assert _pressed(page) == ["No tags"]
        # It adds to a tag beside it rather than replacing it: untagged
        # rows plus that tag's is a filter an analyst asks for.
        _chip(page, a["name"]).click()
        _wait_rows(page, ROWS - 1)
    finally:
        page.evaluate("() => { __winnow.S.tagFilter = []; __winnow.renderTagRibbon();"
                      " return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }")
        _wait_rows(page, ROWS)


def test_any_tag_steps_aside_for_a_specific_one(page, two_tagged_rows):
    a, b = two_tagged_rows
    try:
        _chip(page, "Any tag").click()
        _wait_rows(page, 2)
        assert _pressed(page) == ["Any tag"]
        # "Any tag" already contains this one, so leaving both on would
        # show a filter that narrows nothing.
        _chip(page, a["name"]).click()
        _wait_rows(page, 1)
        assert _pressed(page) == [a["name"]]
        # …and the other way round: it replaces the lot.
        _chip(page, "Any tag").click()
        _wait_rows(page, 2)
        assert _pressed(page) == ["Any tag"]
    finally:
        page.evaluate("() => { __winnow.S.tagFilter = []; __winnow.renderTagRibbon();"
                      " return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }")
        _wait_rows(page, ROWS)

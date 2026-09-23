"""One word per thing, on the surfaces that had drifted off it.

A count over the source found three words for a profile ("profile",
"bundle", "case type") and two for a table ("source", "table"). The UI had
mostly settled on PROFILE and TABLE; what was left was a handful of labels
that never caught up, so the new-case dialog offered a "Case type", the
Case menu offered "Merge sources…", the timeline offered "Configure
sources…" and an empty grid said "No file open" — four names for two
things, on screens an analyst moves between in the same minute.

Only the words an analyst reads are pinned here. `source_id`, `sourceLabel`,
`/api/plugin_bundles` and the `sources` table keep their names: renaming
those is a different change with a different risk profile, and a test that
demanded otherwise would be the thing standing in its way.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

# "source"/"sources" as a whole word. Not `re.search("source")`, which
# matches sourceLabel, #sourceTabs and every /api path that carries the id.
SOURCE_WORD = re.compile(r"\bsources?\b", re.I)
CASE_TYPE = re.compile(r"\bcase[ -]types?\b", re.I)


def _modal_text(page) -> str:
    return page.locator("#modal .modal-card").inner_text()


def _modal_title(page) -> str:
    # text_content, not inner_text: .modal-head is text-transform: uppercase
    # and inner_text hands back what the transform painted.
    return page.locator("#modalTitle").text_content()


def _close_modal(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_the_chrome_calls_a_table_a_table(page):
    """The tab strip and the empty grid, read as a screen reader and an
    analyst with nothing open respectively see them."""
    assert page.locator("#sourceTabs").get_attribute("aria-label") == "Open tables"
    # text_content, not inner_text: the overlay is hidden while a table is
    # open, and what it will say when it is not is the point.
    assert page.locator("#empty .empty-title").text_content() == "No table open"


def test_the_case_menu_merges_tables(page):
    page.locator("#btnCase").click()
    page.wait_for_selector(".menu")
    labels = page.locator(".menu:not(.menu-sub) .menu-item").all_inner_texts()
    assert any("Merge tables" in l for l in labels), labels
    assert not [l for l in labels if SOURCE_WORD.search(l)], labels
    page.keyboard.press("Escape")


def test_the_merge_builder_is_about_tables(page):
    page.evaluate("() => __winnow.openMergeBuilder()")
    page.wait_for_selector("#modal:not([hidden])")
    try:
        assert _modal_title(page) == "Merge tables"
        text = _modal_text(page)
        assert not SOURCE_WORD.search(text), text
    finally:
        _close_modal(page)


def test_the_timeline_configures_tables(page):
    # text_content throughout: the timeline section is `hidden` until its
    # page tab is opened, and inner_text of a hidden node is "".
    assert page.locator("#btnTimelineConfig").text_content() == "Configure tables…"
    # The per-row label is still a type — one template serves every table
    # with the same header set, so "Windows Event Log" names a kind of
    # table and not one table — but it says so without the word "source".
    assert page.locator(".tl-col-type").text_content() == "Type"
    page.evaluate("() => __winnow.openTimelineSourceConfig()")
    page.wait_for_selector("#modal:not([hidden])")
    try:
        assert _modal_title(page) == "Configure timeline tables"
        text = _modal_text(page)
        assert not SOURCE_WORD.search(text), text
    finally:
        _close_modal(page)


def test_the_new_case_dialog_asks_for_a_profile(page):
    page.evaluate("() => __winnow.openNewCaseModal()")
    page.wait_for_selector("#modal:not([hidden])")
    try:
        text = _modal_text(page)
        assert not CASE_TYPE.search(text), text
        labels = page.locator("#modal label").all_inner_texts()
        assert "Profile" in labels, labels
    finally:
        _close_modal(page)

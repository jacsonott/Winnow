"""The clicked cell's filter verbs in the row menu.

These three entries read "Filter to <value>", "Filter to <value> only" and
"Exclude <value>" until 2026-09: the value spelled out three times, and the
whole difference between the first two carried by the word "only", at the
end of the longer label — past where the eye stops, and past where a long
value gets ellipsized. They now sit under one heading that names the value
once, with a second line each saying what the verb does to the filters
already on.

So the first two tests pin the shape (the value named once; a second line
on each verb), and the rest pin that relabelling moved nothing: each entry
still makes the filterByValue call it always made, which is the failure a
pure-wording diff is most likely to cause.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

VERBS = ("Narrow to this value", "Reset to this value", "Exclude this value")


def _open_on_host(page, row=0):
    """Right-click the Host cell of `row` and return the value under the
    pointer — the one the old labels repeated three times."""
    i = page.evaluate("() => __winnow.visibleCols().indexOf('Host')")
    cell = page.locator(".row").nth(row).locator(".cell").nth(i)
    value = cell.inner_text()
    cell.click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")
    return value


def _root(page):
    return page.locator(".menu:not(.menu-sub)")


def _filter_eventid(page):
    """A filter on another column, already on before the menu opens — the
    thing the three verbs differ about."""
    box = page.locator('.fcell input[data-col="EventId"]')
    box.fill("=4624")
    box.press("Enter")
    page.wait_for_function("() => __winnow.S.filters.EventId === '=4624'")
    # Wait for that rebuild to land before anything reads a cell: the rows
    # move under the pointer when it does, and a value read from the grid
    # a moment before the repaint is a value the menu was never opened on.
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 200")
    page.wait_for_timeout(150)


def test_the_value_is_named_once_in_the_heading(page):
    value = _open_on_host(page)
    root = _root(page)
    # The heading carries column and value, in the spelling the header
    # filter box will end up holding.
    assert root.locator(".menu-header").last.inner_text() == f"Host = {value}"
    labels = root.locator(".menu-item .menu-item-text").all_inner_texts()
    assert [v for v in VERBS if v in labels] == list(VERBS), labels
    # Once means once: no entry repeats it.
    assert [l for l in labels if value in l] == [], labels
    # And no entry is another entry plus a trailing word — which is exactly
    # what "Filter to X" beside "Filter to X only" was.
    for a in labels:
        for b in labels:
            assert not (b.startswith(a + " ") and len(b.split()) == len(a.split()) + 1), (a, b)
    page.keyboard.press("Escape")


def test_each_verb_says_what_it_does_to_the_filters_already_on(page):
    _open_on_host(page)
    root = _root(page)
    descs = {}
    for verb in VERBS:
        item = root.locator(".menu-item", has_text=verb)
        assert item.count() == 1, verb
        descs[verb] = item.locator(".menu-item-desc").inner_text().strip()
    # Each says something, and says something different: the second line is
    # where the choice between the three is made now.
    assert len(set(descs.values())) == 3, descs
    assert "adds to the filters" in descs["Narrow to this value"]
    assert "clears the other filters" in descs["Reset to this value"]
    assert "other filters" in descs["Exclude this value"]
    # The picker below them is deliberately one line — it is not a verb on
    # this value, so it has nothing true to say about the filters on.
    picker = root.locator(".menu-item", has_text="Filter by values…")
    assert picker.locator(".menu-item-desc").count() == 0
    page.keyboard.press("Escape")


def test_narrow_keeps_the_filters_already_on(page):
    _filter_eventid(page)
    value = _open_on_host(page)
    page.click(".menu:not(.menu-sub) .menu-item:has-text('Narrow to this value')")
    page.wait_for_function("(v) => __winnow.S.filters.Host === '=' + v", value)
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == "=4624"
    assert page.locator('.fcell input[data-col="Host"]').input_value() == f"={value}"


def test_reset_clears_the_other_filters_first(page):
    _filter_eventid(page)
    value = _open_on_host(page)
    page.click(".menu:not(.menu-sub) .menu-item:has-text('Reset to this value')")
    page.wait_for_function("(v) => __winnow.S.filters.Host === '=' + v", value)
    page.wait_for_function("() => !__winnow.S.filters.EventId")
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == ""
    assert page.locator('.fcell input[data-col="Host"]').input_value() == f"={value}"


def test_exclude_hides_the_value_and_leaves_the_others_on(page):
    _filter_eventid(page)
    value = _open_on_host(page)
    page.click(".menu:not(.menu-sub) .menu-item:has-text('Exclude this value')")
    page.wait_for_function("(v) => __winnow.S.filters.Host === '!=' + v", value)
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == "=4624"
    # Not just the filter string — the grid no longer shows that value.
    i = page.evaluate("() => __winnow.visibleCols().indexOf('Host')")
    shown = page.locator(".row").first.locator(".cell").nth(i).inner_text()
    assert shown != value

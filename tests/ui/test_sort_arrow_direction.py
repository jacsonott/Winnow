"""Which way a sort arrow points, and what it says when hovered.

Analysts reported the grid's sort arrows as backwards. They were not: ▲
has meant ascending since the first commit, the way Excel, Windows
Explorer and most data grids draw it. But the opposite habit (▼ read as
"A at the top, growing downward") is real, and an arrow alone cannot tell
an analyst which convention it follows. So each arrow carries its order in
words, and this pins the three things together: the glyph, the words and
the rows actually on screen. A flip of any one of them alone fails here."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

HOST_ARROW = '.hcell[data-col="Host"] .sort'


def _hosts(page, n=3):
    return page.evaluate("""(n) => { const ci = __winnow.S.columns.findIndex((c) => c.name === 'Host');
      return [...Array(n).keys()].map((i) => __winnow.rowAt(i).cells[ci]); }""", n)


def test_grid_arrow_matches_rows_and_says_so(page, view_rebuilt):
    before = page.evaluate("() => __winnow.S.sort")
    try:
        with view_rebuilt(page):
            page.click('.hcell[data-col="Host"] .label')
        assert page.evaluate("() => __winnow.S.sort") == [{"column": "Host", "dir": "asc"}]
        assert page.inner_text(HOST_ARROW) == "▲"
        assert page.get_attribute(HOST_ARROW, "title") == (
            "Sorted ascending: A to Z, smallest or earliest first")
        assert _hosts(page) == ["H0", "H0", "H0"], "▲ must sit over rows that run A to Z"

        with view_rebuilt(page):
            page.click('.hcell[data-col="Host"] .label')
        assert page.inner_text(HOST_ARROW) == "▼"
        assert page.get_attribute(HOST_ARROW, "title") == (
            "Sorted descending: Z to A, largest or latest first")
        assert _hosts(page) == ["H4", "H4", "H4"], "▼ must sit over rows that run Z to A"

        # A second key: the arrow carries its place in the order, and so do the words.
        with view_rebuilt(page):
            page.click('.hcell[data-col="EventId"] .label', modifiers=["Shift"])
        arrow = '.hcell[data-col="EventId"] .sort'
        assert page.inner_text(arrow) == "▲2"
        assert page.get_attribute(arrow, "title") == (
            "Sort key 2 of 2, ascending: A to Z, smallest or earliest first")
        assert page.get_attribute(HOST_ARROW, "title").startswith("Sort key 1 of 2, descending")
    finally:
        page.evaluate("""(sort) => { __winnow.S.sort = sort; __winnow.renderHead();
          __winnow.saveLayoutNow(); return __winnow.rebuildView({ keepScroll: false }); }""", before)


def test_line_arrow_says_which_end_is_on_top(page, view_rebuilt):
    before = page.evaluate("() => __winnow.S.sort")
    try:
        if before:
            with view_rebuilt(page):
                page.evaluate("() => { __winnow.S.sort = []; __winnow.renderHead();"
                              " return __winnow.rebuildView({ keepScroll: false }); }")
        line = ".gutter-head .line-sort .sort"
        assert page.inner_text(line) == "▲"
        assert page.get_attribute(line, "title") == "In original line order: the file’s first line at the top"
        assert page.evaluate("() => __winnow.rowAt(0).rid") == 1

        with view_rebuilt(page):
            page.click(".gutter-head .line-sort")
        assert page.inner_text(line) == "▼"
        assert page.get_attribute(line, "title") == "In reverse line order: the file’s last line at the top"
        assert page.evaluate("() => __winnow.rowAt(0).rid") == 200
    finally:
        page.evaluate("""(sort) => { __winnow.S.sort = sort; __winnow.renderHead();
          __winnow.saveLayoutNow(); return __winnow.rebuildView({ keepScroll: false }); }""", before)

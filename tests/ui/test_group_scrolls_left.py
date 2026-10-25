"""Grouping a column puts the grid back at its left edge.

A group header is an ordinary row whose label sits at the row's left,
indented per level, and nothing about it is sticky (.group-header-row).
So dragging a header into the Group by strip while the grid was scrolled
right painted a screen of header rows with every label off past the left
edge — the view the analyst asked for, with the part that names each
group not on screen.

Asserted as the symptom rather than as scrollLeft alone: the first group
header's label has to be inside the scroller's own box.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def _ungroup(page):
    yield
    page.evaluate("() => __winnow.dropGrouping()")


def _scroll_right(page):
    """Widen a column until the grid overflows, then scroll to the far
    right. The fixture table fits in this viewport as imported (the
    autofit cap keeps the 300-character CommandLine at 360px), and a grid
    with nothing to scroll would say nothing about scrolling — so the
    width goes on the way a drag of the column's edge puts it there."""
    page.evaluate("""() => {
      __winnow.S.layout.CommandLine = { ...(__winnow.S.layout.CommandLine || {}), w: 2400 };
      __winnow.renderHead();
      __winnow.render();
      const b = document.getElementById('body');
      b.scrollLeft = b.scrollWidth;
    }""")
    assert page.evaluate("() => document.getElementById('body').scrollLeft") > 0


def _group_by(page, column):
    page.evaluate("(c) => __winnow.addGroupLevel(c)", arg=column)
    page.wait_for_selector(".group-header-row", state="attached")


def _label_is_in_view(page):
    return page.evaluate("""() => {
      const body = document.getElementById('body');
      const label = document.querySelector('.group-header-row .group-header-label');
      if (!label) return null;
      const b = body.getBoundingClientRect(), l = label.getBoundingClientRect();
      return { left: l.left - b.left, visible: l.left >= b.left - 1 && l.left < b.right };
    }""")


def test_grouping_scrolls_back_to_the_left(page):
    _scroll_right(page)
    _group_by(page, "Host")
    assert page.evaluate("() => document.getElementById('body').scrollLeft") == 0


def test_the_group_label_is_on_screen(page):
    _scroll_right(page)
    _group_by(page, "Host")
    where = _label_is_in_view(page)
    assert where is not None, "no group header label rendered"
    assert where["visible"], f"group label is {where['left']:.0f}px from the scroller's left edge"


def test_a_second_level_keeps_the_left_edge(page):
    _group_by(page, "Host")
    _scroll_right(page)
    _group_by(page, "EventId")
    assert page.evaluate("() => document.getElementById('body').scrollLeft") == 0
    assert _label_is_in_view(page)["visible"]


def test_restoring_a_parked_grouping_scrolls_left_too(page):
    """toggleGrouping's restore branch is the same act — the grouping comes
    back, so the headers do, at the same left edge."""
    _group_by(page, "Host")
    page.evaluate("() => __winnow.toggleGrouping()")   # parks it
    page.wait_for_selector(".group-header-row", state="detached")
    _scroll_right(page)
    page.evaluate("() => __winnow.toggleGrouping()")   # brings it back
    page.wait_for_selector(".group-header-row", state="attached")
    assert page.evaluate("() => document.getElementById('body').scrollLeft") == 0

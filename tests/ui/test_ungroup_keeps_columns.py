"""A column grouped on comes back to the grid, and to the Columns panel,
in its own place — whichever way the grouping ends.

Grouping used to cut the column out of S.order and Ungroup put back a
snapshot taken when the grouping started (S.preGroupOrder). Every way the
two drifted apart lost the column until the table was opened again:

- a Group-by that failed turned the grouping off without the restore, so
  the column was missing from the grid AND the Columns panel;
- a layout saved while grouped (a resize, a sort) was saved without it,
  so it reopened at the far right;
- a level removed from its pill was appended at the far right;
- a column moved while grouped jumped back on Ungroup.

The column now never leaves S.order; visibleCols() is what leaves it out
of the grid while it is grouped on. Each test below is one of those
paths, checked where the analyst looks: the header cells and the panel.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

GROUP_SUMMARY = "**/api/group_summary*"

HEAD = "() => [...document.querySelectorAll('#headRow .hcell[data-col]')].map((h) => h.dataset.col)"
PANEL = "() => [...document.querySelectorAll('#modalBody .collist-row label > span:not(.collist-type)')].map((s) => s.textContent)"


@pytest.fixture(autouse=True)
def _put_back(page, layout_saved):
    """The case is shared by the whole UI session, and its saved layout
    with it: every test leaves the order and widths as it found them."""
    before = page.evaluate("() => JSON.stringify({ order: __winnow.S.order, layout: __winnow.S.layout })")
    yield
    page.unroute(GROUP_SUMMARY)
    page.evaluate("""async (b) => {
      if (!document.getElementById('modal').hidden) __winnow.closeModal();
      if (__winnow.S.groupByCols.length) await __winnow.dropGrouping();
      const s = JSON.parse(b);
      __winnow.S.order = s.order;
      __winnow.S.layout = __winnow.nameMap(s.layout);
      __winnow.renderHead();
      __winnow.render();
      __winnow.saveLayout();
    }""", before)
    layout_saved()


def _head(page):
    return page.evaluate(HEAD)


def _panel(page):
    """The Columns panel's list, as the table menu draws it."""
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector("#modalBody .collist-row")
    names = page.evaluate(PANEL)
    page.evaluate("() => __winnow.closeModal()")
    return names


def _group(page, col):
    page.evaluate("(c) => __winnow.addGroupLevel(c)", col)
    page.wait_for_function("(c) => __winnow.S.groupByCols.includes(c) && __winnow.S.groups.length > 0", arg=col)


def _ungroup(page):
    page.locator(".group-drop-btn").click()
    page.wait_for_function("() => !__winnow.S.groupByCols.length")


def test_ungroup_puts_the_column_back_where_it_was(page):
    head = _head(page)
    assert "Host" in head
    _group(page, "Host")
    # Grouped on, it is in the group headers instead: out of the header
    # row and out of the panel, as it always was.
    assert _head(page) == [c for c in head if c != "Host"]
    assert "Host" not in _panel(page)
    _ungroup(page)
    assert _head(page) == head
    assert _panel(page) == head


def test_a_failed_group_by_leaves_the_column_in_the_grid_and_the_panel(page):
    """The report: the grouping went away and the column with it, from the
    grid and the Columns panel both, until the table was opened again."""
    head = _head(page)
    page.route(GROUP_SUMMARY, lambda route: route.fulfill(
        status=500, content_type="application/json", body=json.dumps({"detail": "boom"})))
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function("() => !__winnow.S.groupByCols.length")
    page.unroute(GROUP_SUMMARY)
    page.wait_for_function("(want) => JSON.stringify((%s)()) === JSON.stringify(want)" % HEAD, arg=head)
    assert _panel(page) == head


def test_a_layout_saved_while_grouped_keeps_the_column_in_its_place(page, api, layout_saved):
    """Anything that saves the layout while grouped (here a column's width)
    saved the order without the column grouped on — and the table opened
    again later put it at the far right."""
    head = _head(page)
    sid = page.evaluate("() => __winnow.S.sourceId")
    _group(page, "Host")
    page.evaluate("""() => {
      __winnow.S.layout.EventId = { ...(__winnow.S.layout.EventId || {}), w: 155 };
      __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
    }""")
    layout_saved()
    saved = api(f"/api/layout?source_id={sid}")["order"]
    assert [c for c in saved if c in head] == head, saved
    # Opened again (every refresh idiom, and coming back from another
    # page, goes through here): the grouping is put back from the stash,
    # and Ungroup puts the column back where it was.
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("() => __winnow.S.groupByCols.join() === 'Host' && __winnow.S.groups.length > 0")
    _ungroup(page)
    assert _head(page) == head


def test_removing_one_level_puts_that_column_back_in_its_place(page):
    head = _head(page)
    _group(page, "Host")
    _group(page, "EventId")
    page.locator(".group-pill").first.locator(".group-pill-rm").click()   # Host's ✕
    page.wait_for_function("() => __winnow.S.groupByCols.join() === 'EventId'")
    # Host is a data column again, where it was — it used to be appended
    # at the far right, off screen on any wide table.
    assert _head(page) == [c for c in head if c != "EventId"]
    _ungroup(page)
    assert _head(page) == head


def test_a_column_moved_while_grouped_stays_moved_after_ungroup(page):
    head = _head(page)
    _group(page, "Host")
    page.evaluate("() => __winnow.moveColumn('Payload', 'top')")
    _ungroup(page)
    assert _head(page) == ["Payload"] + [c for c in head if c != "Payload"]

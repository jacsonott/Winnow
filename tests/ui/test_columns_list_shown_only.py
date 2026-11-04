"""The Columns pane's All / Shown switch: on a wide table with most of its
columns put away, the list was mostly unticked rows with the few the grid
shows scattered through it. Shown lists only the ticked ones.

What has to hold: the hidden columns leave the list but not the table (a
drag under Shown still reorders the whole of S.order), the way back is
always on screen (the note under the list, which also counts the hidden
columns a typed name matches), a row unticked under Shown stays put so a
misclick is one click back, and the choice is remembered in this browser,
not in the table's layout."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

HIDE = ["Host", "CommandLine", "Payload"]


def _open(page):
    page.evaluate("() => __winnow.openTableMenu(undefined, 'columns')")
    page.wait_for_selector('.table-menu-pane[data-section="columns"] .tm-cols-seg')


def _listed(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.collist-row')].filter((r) => !r.hidden).map((r) => r.dataset.col)")


def _set_hidden(page, names, hidden):
    page.evaluate("""([names, hidden]) => {
      for (const n of names) __winnow.S.layout[n] = { ...(__winnow.S.layout[n] || {}), hidden };
      __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
    }""", [names, hidden])


@pytest.fixture
def three_hidden(page, layout_saved):
    page.wait_for_function("() => !!__winnow.S.view && __winnow.S.columns.length > 0")
    order = page.evaluate("() => [...__winnow.S.order]")
    _set_hidden(page, HIDE, True)
    yield order
    page.evaluate("() => __winnow.closeModal()")
    page.evaluate("(o) => { __winnow.S.order = o; }", order)
    _set_hidden(page, HIDE, False)
    layout_saved()


def test_shown_lists_only_the_ticked_columns_and_the_note_lists_the_rest(page, three_hidden):
    order = three_hidden
    shown = [n for n in order if n not in HIDE]
    _open(page)
    # All is where a new browser starts: nothing about the menu changes
    # for anyone who never touches the switch.
    seg = page.locator(".tm-cols-seg")
    assert seg.locator('button[data-key="all"]').get_attribute("aria-pressed") == "true"
    assert _listed(page) == order
    assert page.locator(".collist-more").is_hidden()

    seg.locator('button[data-key="shown"]').click()
    assert _listed(page) == shown
    more = page.locator(".collist-more")
    assert more.is_visible()
    assert more.inner_text().startswith("3 hidden columns not listed")
    # The count still counts the table, not the list.
    assert page.locator(".tm-cols-count").inner_text() == f"{len(shown)} of {len(order)} shown"
    # Only the list changed: the columns are still in the table, hidden.
    assert page.evaluate("() => [...__winnow.S.order]") == order
    assert all(page.evaluate("(n) => __winnow.S.layout[n].hidden", n) for n in HIDE)

    more.locator("button", has_text="List all columns").click()
    assert _listed(page) == order
    assert more.is_hidden()
    assert seg.locator('button[data-key="all"]').get_attribute("aria-pressed") == "true"


def test_a_name_typed_under_shown_counts_the_hidden_columns_it_matches(page, three_hidden):
    _open(page)
    page.locator('.tm-cols-seg button[data-key="shown"]').click()
    page.locator(".collist-search").fill("o")
    # ExtremelyLongColumnHeaderName is shown and matches; Host, CommandLine
    # and Payload are hidden and match too, so they are counted, not lost.
    assert _listed(page) == [n for n in three_hidden if n not in HIDE and "o" in n.lower()]
    assert page.locator(".collist-more").inner_text().startswith("3 hidden columns match “o”")
    page.locator(".collist-search").fill("Payload")
    assert _listed(page) == []
    note = page.locator(".collist-more")
    assert note.inner_text().startswith("1 hidden column matches “Payload”")
    # Nothing above it to rule off.
    assert "alone" in note.get_attribute("class").split()
    note.locator("button").click()
    assert _listed(page) == ["Payload"], "List all keeps the name typed"


def test_a_row_unticked_under_shown_stays_until_the_list_is_filtered_again(page, three_hidden, layout_saved):
    _open(page)
    page.locator('.tm-cols-seg button[data-key="shown"]').click()
    box = page.locator('.collist-row[data-col="EventId"] input[type=checkbox]')
    box.uncheck()
    page.wait_for_function("() => __winnow.S.layout.EventId.hidden === true")
    # Still there, so the misclick is undone where it was made.
    assert "EventId" in _listed(page)
    box.check()
    page.wait_for_function("() => __winnow.S.layout.EventId.hidden === false")
    box.uncheck()
    page.wait_for_function("() => __winnow.S.layout.EventId.hidden === true")
    try:
        # The next time the list is filtered it goes with the other hidden ones.
        page.locator(".collist-search").fill("e")
        page.locator(".collist-search").fill("")
        assert "EventId" not in _listed(page)
        assert page.locator(".collist-more").inner_text().startswith("4 hidden columns not listed")
    finally:
        _set_hidden(page, ["EventId"], False)
        layout_saved()


def test_a_drag_under_shown_reorders_around_the_hidden_columns(page, three_hidden):
    order = three_hidden
    _open(page)
    page.locator('.tm-cols-seg button[data-key="shown"]').click()
    listed = _listed(page)
    rows = page.locator(".collist-row:not([hidden])")
    # Top third of the target, as test_columns_panel.py does: the drop
    # decides before/after against the row's midpoint.
    rows.nth(len(listed) - 1).drag_to(rows.nth(0), target_position={"x": 10, "y": 3})
    page.wait_for_function("(was) => __winnow.S.order.join('\\n') !== was", arg="\n".join(order))
    after = page.evaluate("() => [...__winnow.S.order]")
    assert sorted(after) == sorted(order), "the hidden columns are still in the order"
    assert after.index(listed[-1]) < after.index(listed[0])
    assert _listed(page)[0] == listed[-1]


def test_the_choice_is_remembered_in_this_browser_not_in_the_layout(page, three_hidden, api, layout_saved):
    _open(page)
    page.locator('.tm-cols-seg button[data-key="shown"]').click()
    stored = page.evaluate("() => localStorage.getItem('winnow.tableMenu')")
    assert json.loads(stored)["columnsList"] == "shown"
    page.evaluate("() => __winnow.closeModal()")
    _open(page)
    assert page.locator('.tm-cols-seg button[data-key="shown"]').get_attribute("aria-pressed") == "true"
    assert _listed(page) == [n for n in three_hidden if n not in HIDE]
    # A preference of how the menu reads, not part of the table: nothing
    # about it reaches the saved layout.
    layout_saved()
    sid = page.evaluate("() => __winnow.S.sourceId")
    assert "columnsList" not in json.dumps(api(f"/api/layout?source_id={sid}"))

"""Hide all, in both column lists: the table menu's Show, hide & order
pane and the list pinned to the sidebar.

The request: an option in the column chooser that unticks every column,
so that on a wide table (a KAPE export with dozens of PayloadData and
Keywords columns) the few wanted can be ticked back instead of the rest
unticked one at a time.

What has to hold, in the terms an analyst would notice it failing:

* every box is unticked and the grid shows no column, and that is saved
  with the table like any other tick;
* the rows stay listed, under Shown too, because they are what the
  analyst ticks from next: a list that emptied itself would leave
  nothing to pick from;
* Undo puts back the columns that were shown, not every column: Show
  all is not the way back from a misclick on a set someone spent a while
  choosing;
* a column grouped on is left alone, so it comes back on Ungroup.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PANE = '.table-menu-pane[data-section="columns"]'
DOCK = "#colDock:not([hidden])"


def _listed(page, row_cls):
    return page.evaluate(
        "(cls) => [...document.querySelectorAll(cls)].filter((r) => !r.hidden).map((r) => r.dataset.col)",
        row_cls)


def _ticked(page, row_cls):
    return page.evaluate(
        "(cls) => [...document.querySelectorAll(cls)].filter((r) => r.querySelector('input[type=checkbox]').checked)"
        ".map((r) => r.dataset.col)", row_cls)


def _set_hidden(page, names, hidden):
    page.evaluate("""([names, hidden]) => {
      for (const n of names) __winnow.S.layout[n] = { ...(__winnow.S.layout[n] || {}), hidden };
      __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
    }""", [names, hidden])


@pytest.fixture
def layout_restored(page, layout_saved):
    """The case is shared by the whole UI session: put the open table's
    order and layout back, and drop any grouping, on the way out."""
    page.wait_for_function("() => !!__winnow.S.view && __winnow.S.columns.length > 0")
    was = page.evaluate("() => JSON.stringify({ order: __winnow.S.order, layout: __winnow.S.layout })")
    yield json.loads(was)
    page.evaluate("""async (was) => {
      if (!document.getElementById('modal').hidden) __winnow.closeModal();
      if (__winnow.S.groupByCols.length) await __winnow.dropGrouping();
      if (__winnow.columnDockOpen()) __winnow.setColumnDockOpen(false);
      const w = JSON.parse(was);
      __winnow.S.order = w.order;
      __winnow.S.layout = __winnow.nameMap(w.layout);
      __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
    }""", was)
    layout_saved()


def _open_pane(page):
    page.evaluate("() => __winnow.openTableMenu(undefined, 'columns')")
    page.wait_for_selector(f"{PANE} .collist-row")


def test_hide_all_in_the_table_menu_unticks_every_column_and_keeps_them_listed(
        page, layout_restored, layout_saved, api):
    order = layout_restored["order"]
    # Shown is the switch most likely to be on when the list is long, and
    # the one under which an emptied list would leave nothing to tick.
    page.evaluate("() => __winnow.saveColumnsListMode('shown')")
    _open_pane(page)
    assert _listed(page, ".collist-row") == order
    page.locator(f"{PANE} .row-actions button", has_text="Hide all").click()
    page.wait_for_function("() => __winnow.visibleCols().length === 0")
    assert page.locator("#headRow .hcell[data-col]").count() == 0
    assert _ticked(page, ".collist-row") == []
    assert _listed(page, ".collist-row") == order, "every row is still there to tick from"
    assert page.locator(".tm-cols-count").inner_text() == f"0 of {len(order)} shown"
    # The side list's line follows, as it does for a tick by hand.
    page.wait_for_function(
        "(n) => [...document.querySelectorAll('.side-rail-hint')].some((h) => h.textContent === `0 of ${n} shown`)",
        arg=len(order))

    # Then the few wanted, ticked back from the same list.
    for name in ("Host", "EventId"):
        page.locator(f'.collist-row[data-col="{name}"] input[type=checkbox]').check()
    page.wait_for_function("() => __winnow.visibleCols().length === 2")
    assert page.evaluate("() => __winnow.visibleCols()") == [n for n in order if n in ("Host", "EventId")]
    layout_saved()
    sid = page.evaluate("() => __winnow.S.sourceId")
    saved = api(f"/api/layout?source_id={sid}")["columns"]
    assert {n for n in order if not saved.get(n, {}).get("hidden")} == {"Host", "EventId"}


def test_undo_puts_back_the_columns_that_were_shown_not_every_column(page, layout_restored):
    order = layout_restored["order"]
    put_away = ["CommandLine", "Payload"]
    _set_hidden(page, put_away, True)
    was = [n for n in order if n not in put_away]
    _open_pane(page)
    page.locator(f"{PANE} .row-actions button", has_text="Hide all").click()
    page.wait_for_function("() => __winnow.visibleCols().length === 0")
    toast = page.locator("#toast")
    assert toast.inner_text().startswith(f"Hid all {len(order)} columns")
    toast.locator(".toast-action", has_text="Undo").click()
    page.wait_for_function("(n) => __winnow.visibleCols().length === n", arg=len(was))
    assert page.evaluate("() => __winnow.visibleCols()") == was
    # The menu is still open, and its boxes say so too: the pane does not
    # repaint from the layout on its own.
    assert page.locator(PANE).is_visible()
    assert _ticked(page, ".collist-row") == was


def test_hide_all_in_the_sidebar_list_keeps_the_rows_listed_under_shown(page, layout_restored):
    order = layout_restored["order"]
    page.evaluate("() => __winnow.saveColumnsListMode('shown')")
    page.click("#btnColDock")
    page.wait_for_selector(f"{DOCK} .coldock-row")
    page.locator(f"{DOCK} .coldock-foot button", has_text="Hide all").click()
    page.wait_for_function("() => __winnow.visibleCols().length === 0")
    assert page.locator(f"{DOCK} .coldock-count").inner_text() == f"0 of {len(order)}"
    assert _ticked(page, "#colDock .coldock-row") == []
    assert _listed(page, "#colDock .coldock-row") == order
    page.locator(f'{DOCK} .coldock-row[data-col="CommandLine"] input[type=checkbox]').check()
    page.wait_for_function("() => __winnow.visibleCols().join() === 'CommandLine'")
    page.locator("#toast .toast-action", has_text="Undo").click()
    page.wait_for_function("(n) => __winnow.visibleCols().length === n", arg=len(order))


def test_hide_all_leaves_a_grouped_column_to_come_back_on_ungroup(page, layout_restored):
    order = layout_restored["order"]
    page.evaluate("(c) => __winnow.addGroupLevel(c)", "Host")
    page.wait_for_function("() => __winnow.S.groupByCols.includes('Host') && __winnow.S.groups.length > 0")
    _open_pane(page)
    page.locator(f"{PANE} .row-actions button", has_text="Hide all").click()
    page.wait_for_function("() => __winnow.visibleCols().length === 0")
    # Still compared by Hide duplicate rows, which counts a grouped column.
    assert not page.evaluate("() => !!(__winnow.S.layout.Host || {}).hidden")
    assert page.locator("#toast").inner_text().startswith(f"Hid all {len(order) - 1} columns")
    page.evaluate("() => __winnow.closeModal()")
    page.locator(".group-drop-btn").click()
    page.wait_for_function("() => !__winnow.S.groupByCols.length")
    page.wait_for_function("() => __winnow.visibleCols().join() === 'Host'")

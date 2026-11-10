"""The column list pinned to the bottom of the sidebar (#527).

The request: a vertical list of the columns that stays on screen, that
can be searched by name, and where a column can be shown, hidden and
reordered without opening anything, the way EDR and SIEM consoles keep
their field list docked in a corner. The table menu's Show, hide & order
pane already did all of that, as a dialog over the grid it was changing.

What has to hold, in the terms an analyst would notice it failing:

* it lists the table on screen, and a tick or a drag there changes the
  grid (and the saved layout) the way the table menu's list does;
* it never goes stale: a column hidden, pinned or moved from anywhere
  else (the header, Alt-click, the table menu) shows in it at once, and
  a different table relists it;
* a filter typed into it survives the ticks, pins and drops it is there
  to make, and a table switch;
* it stays pinned across a reload, and its ✕ unpins it for good;
* merges work the same (invariant #9).
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

DOCK = "#colDock:not([hidden])"
ROWS = f"{DOCK} .coldock-row"


def _listed(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#colDock .coldock-row')].filter((r) => !r.hidden).map((r) => r.dataset.col)")


def _box(page, name):
    return page.locator(f'{ROWS}[data-col="{name}"] input[type=checkbox]')


def _pin_dock(page):
    page.click("#btnColDock")
    page.wait_for_selector(ROWS)


def _reload_keeping_sidebar(page):
    """Reload the way an analyst's browser does. The shared context's
    first-run seed writes winnow.sidebar on every navigation (it models an
    analyst who keeps the sidebar open), which would wipe the very prefs
    under test; this carries them over the reload in sessionStorage and
    puts them back after the seed has run."""
    page.evaluate("() => sessionStorage.setItem('__keepSidebar', localStorage.getItem('winnow.sidebar'))")
    page.context.add_init_script(
        "{ const v = sessionStorage.getItem('__keepSidebar'); if (v) localStorage.setItem('winnow.sidebar', v); }")
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")


def _grid_cols(page):
    return page.evaluate("() => __winnow.visibleCols()")


@pytest.fixture
def layout_restored(page, layout_saved):
    """Puts the open table's order, hidden columns and pins back as they
    were, so the next test (and the next module) starts from the same
    layout."""
    page.wait_for_function("() => !!__winnow.S.view && __winnow.S.columns.length > 0")
    sid = page.evaluate("() => __winnow.S.sourceId")
    was = page.evaluate("() => JSON.stringify({ order: __winnow.S.order, layout: __winnow.S.layout })")
    yield json.loads(was)
    page.evaluate("() => __winnow.closeModal()")
    page.evaluate("""([sid, was]) => (async () => {
      if (__winnow.S.sourceId !== sid || __winnow.S.activeTab !== 'grid') await __winnow.openSource(sid);
      const w = JSON.parse(was);
      __winnow.S.order = w.order;
      __winnow.S.layout = w.layout;
      __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
    })()""", [sid, was])
    layout_saved()


def test_the_sidebar_button_pins_the_open_tables_columns(page, layout_restored):
    assert page.locator("#colDock").is_hidden(), "a browser that never asked for it does not get it"
    _pin_dock(page)
    order = page.evaluate("() => [...__winnow.S.order]")
    assert _listed(page) == order
    assert page.locator(f"{DOCK} .coldock-count").inner_text() == f"{len(order)} of {len(order)}"
    assert page.locator("#btnColDock").get_attribute("aria-pressed") == "true"
    # Below the table tree, inside the sidebar: the bottom-left corner.
    dock, tree, side = (page.locator(s).bounding_box() for s in ("#colDock", "#sidebarList", "#sidebar"))
    assert dock["y"] >= tree["y"] + tree["height"] - 4
    assert abs((dock["y"] + dock["height"]) - (side["y"] + side["height"])) < 2
    assert dock["x"] >= side["x"] and dock["x"] + dock["width"] <= side["x"] + side["width"] + 1


def test_a_tick_shows_and_hides_the_column_in_the_grid(page, layout_restored, layout_saved, api):
    _pin_dock(page)
    _box(page, "Host").uncheck()
    page.wait_for_function("() => !__winnow.visibleCols().includes('Host')")
    assert page.locator('.hcell[data-col="Host"]').count() == 0
    n = len(page.evaluate("() => __winnow.S.order"))
    assert page.locator(f"{DOCK} .coldock-count").inner_text() == f"{n - 1} of {n}"
    layout_saved()
    sid = page.evaluate("() => __winnow.S.sourceId")
    assert api(f"/api/layout?source_id={sid}")["columns"]["Host"]["hidden"] is True
    _box(page, "Host").check()
    page.wait_for_selector('.hcell[data-col="Host"]')


def test_the_filter_narrows_the_list_and_survives_what_it_is_for(page, layout_restored):
    _pin_dock(page)
    search = page.locator(f"{DOCK} .coldock-search")
    search.fill("o")
    want = [n for n in page.evaluate("() => [...__winnow.S.order]") if "o" in n.lower()]
    assert _listed(page) == want
    # A tick, a pin and a move to the end each repaint the grid, and the
    # list with it; none of them may cost the filter typed.
    _box(page, "Host").uncheck()
    page.wait_for_function("() => __winnow.S.layout.Host && __winnow.S.layout.Host.hidden === true")
    page.locator(f'{ROWS}[data-col="CommandLine"]').hover()
    page.locator(f'{ROWS}[data-col="CommandLine"] .coldock-pin').click()
    page.wait_for_function("() => __winnow.isPinned('CommandLine')")
    long = "ExtremelyLongColumnHeaderName"
    page.locator(f'{ROWS}[data-col="{long}"]').hover()
    page.locator(f'{ROWS}[data-col="{long}"] .coldock-move').nth(1).click()
    page.wait_for_function(f"() => __winnow.S.order[__winnow.S.order.length - 1] === '{long}'")
    assert search.input_value() == "o"
    assert _listed(page) == [n for n in page.evaluate("() => [...__winnow.S.order]") if "o" in n.lower()]
    assert _listed(page)[-1] == long


def test_changes_made_elsewhere_show_in_it_at_once(page, layout_restored):
    """The list holds nothing of its own; it is drawn from the layout. A
    copy that only its own controls updated would go stale the first time
    the analyst used the header instead."""
    _pin_dock(page)
    page.locator('.hcell[data-col="EventId"]').click(modifiers=["Alt"])   # Alt-click pins
    page.wait_for_function("() => __winnow.isPinned('EventId')")
    page.wait_for_function(
        "() => document.querySelector('#colDock .coldock-row[data-col=\"EventId\"]').classList.contains('pinned')")
    assert page.locator(f'{ROWS}[data-col="EventId"] .coldock-pin').inner_text() == "pinned"
    # Hidden and moved through the same calls the table menu makes.
    page.evaluate("() => { __winnow.setColumnHidden('Payload', true); __winnow.moveColumn('Host', 'top'); }")
    assert _box(page, "Payload").is_checked() is False
    assert _listed(page)[0] == "Host"
    # The table menu's own list, ticked: the pinned list follows.
    page.evaluate("() => __winnow.openTableMenu(undefined, 'columns')")
    page.locator('.collist-row[data-col="Payload"] input[type=checkbox]').check()
    page.wait_for_function("() => document.querySelector('#colDock .coldock-row[data-col=\"Payload\"] input').checked")


def test_a_dragged_row_reorders_the_grid(page, layout_restored):
    _pin_dock(page)
    order = page.evaluate("() => [...__winnow.S.order]")
    rows = page.locator(ROWS)
    # Top of the target: the drop goes before or after by the row's midpoint.
    rows.nth(len(order) - 1).drag_to(rows.nth(0), target_position={"x": 20, "y": 2})
    page.wait_for_function("(was) => __winnow.S.order.join('\\n') !== was", arg="\n".join(order))
    after = page.evaluate("() => [...__winnow.S.order]")
    assert after[0] == order[-1]
    assert _grid_cols(page)[0] == order[-1]
    assert _listed(page) == after


def test_another_table_relists_it_and_a_page_tab_says_why_it_is_empty(page, layout_restored, api, tmp_path):
    f = tmp_path / "dock_other.csv"
    f.write_text("When,Who,What\n2026-03-14 09:00:00,alice,logon\n", encoding="utf-8")
    home = page.evaluate("() => __winnow.S.sourceId")
    other = api("/api/ingest/path", "POST", {"path": str(f), "name": "dock_other.csv", "build_fts": False})["id"]
    try:
        _pin_dock(page)
        page.locator(f"{DOCK} .coldock-search").fill("w")
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", other)
        page.wait_for_function(
            "() => [...document.querySelectorAll('#colDock .coldock-row')].map((r) => r.dataset.col).join() === 'When,Who,What'")
        # The name typed keeps looking in the new table.
        assert _listed(page) == ["When", "Who", "What"]
        assert page.locator(f"{DOCK} .coldock-search").input_value() == "w"
        page.click("#tabTimeline")
        page.wait_for_selector(f"{DOCK} .coldock-empty:not([hidden])")
        assert page.locator(f"{DOCK} .coldock-body").is_hidden()
        page.evaluate("(id) => __winnow.openSource(id)", home)
        # Attached, not visible: "w" is still typed, and Host has no w.
        page.wait_for_selector(f'{ROWS}[data-col="Host"]', state="attached")
        assert page.locator(f"{DOCK} .coldock-empty").is_hidden()
        assert _listed(page) == [n for n in page.evaluate("() => [...__winnow.S.order]") if "w" in n.lower()]
    finally:
        page.evaluate("(id) => __winnow.openSource(id)", home)
        api(f"/api/source/{other}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")


def test_it_stays_pinned_across_a_reload_and_the_x_unpins_it(page, layout_restored):
    _pin_dock(page)
    assert json.loads(page.evaluate("() => localStorage.getItem('winnow.sidebar')"))["columns"] is True
    _reload_keeping_sidebar(page)
    page.wait_for_selector(ROWS)
    page.locator(f"{DOCK} .coldock-close").click()
    page.wait_for_selector("#colDock", state="hidden")
    assert page.locator("#btnColDock").get_attribute("aria-pressed") == "false"
    _reload_keeping_sidebar(page)
    assert page.locator("#colDock").is_hidden()


def test_the_table_menu_pins_the_list_and_gets_out_of_the_way(page, layout_restored):
    page.evaluate("() => __winnow.openTableMenu(undefined, 'columns')")
    btn = page.locator(".tm-coldock-toggle")
    assert btn.inner_text() == "Pin to sidebar"
    btn.click()
    page.wait_for_selector("#modal", state="hidden")
    page.wait_for_selector(ROWS)
    # The caret moves with the list, so typing a name is the next thing.
    assert page.evaluate("() => document.activeElement.classList.contains('coldock-search')")
    page.evaluate("() => __winnow.openTableMenu(undefined, 'columns')")
    assert page.locator(".tm-coldock-toggle").inner_text() == "Unpin from sidebar"
    page.locator(".tm-coldock-toggle").click()
    page.wait_for_selector("#colDock", state="hidden")
    assert page.locator("#modal").is_visible(), "unpinning leaves the dialog where it is"


def test_pinning_it_shows_a_hidden_sidebar(page, layout_restored):
    page.evaluate("() => __winnow.setSidebarVisible(false)")
    page.wait_for_selector("#sidebar", state="hidden")
    # The keymap's action, which has no key until one is given in Settings.
    page.evaluate("() => __winnow.ACTION_HANDLERS.toggleColumnDock()")
    page.wait_for_selector("#sidebar", state="visible")
    page.wait_for_selector(ROWS)


def test_the_split_gives_it_more_of_the_sidebar_and_remembers(page, layout_restored):
    _pin_dock(page)
    split = page.locator("#colDockSplit").bounding_box()
    before = page.locator("#colDock").bounding_box()["height"]
    x, y = split["x"] + split["width"] / 2, split["y"] + split["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y - 120, steps=4)
    page.mouse.up()
    after = page.locator("#colDock").bounding_box()["height"]
    assert after > before + 60
    ratio = json.loads(page.evaluate("() => localStorage.getItem('winnow.sidebar')"))["columnsRatio"]
    _reload_keeping_sidebar(page)
    page.wait_for_selector(ROWS)
    assert abs(page.locator("#colDock").bounding_box()["height"] - after) < 4
    page.locator("#colDockSplit").dblclick()
    assert json.loads(page.evaluate("() => localStorage.getItem('winnow.sidebar')"))["columnsRatio"] != ratio


def test_a_merged_table_works_the_same(page, layout_restored, api, tmp_path):
    """Invariant #9: a merge has its own layout over its members'
    columns, and the list writes it through the same calls."""
    ids = []
    for n in range(2):
        f = tmp_path / f"dock_merge_{n}.csv"
        f.write_text(f"DkWhen,DkWho,DkWhat\n2026-03-1{n} 09:00:00,u{n},x\n", encoding="utf-8")
        ids.append(api("/api/ingest/path", "POST", {"path": str(f), "name": f"dock_merge_{n}.csv", "build_fts": False})["id"])
    home = page.evaluate("() => __winnow.S.sourceId")
    mid = api("/api/merges", "POST", {"name": "dock merge", "source_ids": ids})["id"]
    try:
        _pin_dock(page)
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", -mid if mid > 0 else mid)
        page.wait_for_selector(f'{ROWS}[data-col="DkWho"]')
        assert set(["DkWhen", "DkWho", "DkWhat"]) <= set(_listed(page))
        _box(page, "DkWho").uncheck()
        page.wait_for_function("() => !__winnow.visibleCols().includes('DkWho')")
        assert page.locator('.hcell[data-col="DkWho"]').count() == 0
    finally:
        page.evaluate("(id) => __winnow.openSource(id)", home)
        api(f"/api/merges/{abs(mid)}", "DELETE")
        for sid in ids:
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")

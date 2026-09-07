"""Dashboards built from what you are looking at, and widgets that lead
back to it. The column header menu, a cell's menu and the Filters menu
each add a ready-made widget with no editor; the editor itself writes the
SQL from a template and a column; a new board can start as a starter
built from the open table; and every widget made these ways carries a
drill — click the number, a list row or a bar and the grid opens on
exactly those rows. The shared fixture table has 200 rows over five Host
values (40 each) and four EventIds."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

H = "{ 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' }"


def _new_board(page, name):
    """A board in the case, loaded into the page, NOT shown — the grid
    stays on screen for the menus."""
    return page.evaluate(f"""async (name) => {{
      const d = await fetch('/api/dashboards', {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ name }}) }}).then(r => r.json());
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
      return d.id;
    }}""", name)


def _delete_board(page, did):
    page.evaluate(f"""async (id) => {{
      await fetch('/api/dashboards/' + id, {{ method: 'DELETE', headers: {H} }});
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
    }}""", did)


def _widgets(page, did):
    return page.evaluate("(id) => fetch('/api/dashboards/' + id).then(r => r.json()).then(b => b.widgets)", did)


def _wait_widgets(page, did, n):
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ws = _widgets(page, did)
        if len(ws) >= n:
            return ws
        time.sleep(0.15)
    raise AssertionError(f"board {did} never reached {n} widgets")


def _preview(page, w):
    return page.evaluate(f"""(w) => fetch('/api/dashboard/widget/preview', {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ source: w.source, query: w.query }}) }}).then(r => r.json())""", w)


def _put_widgets(page, did, widgets):
    page.evaluate(f"""([id, widgets]) => fetch('/api/dashboards/' + id, {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ widgets }}) }})""", [did, widgets])


def _src(page):
    return page.evaluate("() => __winnow.S.sourceId")


def _cond_tree(page):
    return page.evaluate("() => __winnow.S.filterTree")


def _cell(page, row, name):
    """A grid cell by column NAME — cells carry their visible-column index,
    the header carries the name."""
    idx = page.evaluate("(n) => [...document.querySelectorAll('#headRow .hcell[data-col]')].findIndex(h => h.dataset.col === n)", name)
    assert idx >= 0, name
    return page.locator(".row").nth(row).locator(f'.cell[data-col="{idx}"]')


# ------------------------------------------------------------ one click


def test_column_menu_adds_top_values_with_a_drill(page):
    did = _new_board(page, "Quick A")
    try:
        page.locator('.hcell[data-col="Host"]').click(button="right")
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item", has_text="Top values of Host").click()
        (w,) = _wait_widgets(page, did, 1)
        assert w["title"] == "Top Host" and w["render"] == "bar"
        assert w["build"] == {"template": "top", "table": f"src_{_src(page)}", "column": "Host"}
        assert w["drill"] == {"table": f"src_{_src(page)}", "column": "Host"}
        assert 'GROUP BY "Host"' in w["query"]["sql"]
        rows = _preview(page, w)["rows"]
        assert len(rows) == 5 and all(r[1] == 40 for r in rows)
    finally:
        page.keyboard.press("Escape")
        _delete_board(page, did)


def test_column_menu_offers_over_time_only_for_datetime_columns(page):
    did = _new_board(page, "Quick B")
    try:
        page.locator('.hcell[data-col="Timestamp"]').click(button="right")
        page.wait_for_selector(".menu")
        assert page.locator(".menu .menu-item", has_text="Events over time by Timestamp").count() == 1
        page.keyboard.press("Escape")
        page.locator('.hcell[data-col="Host"]').click(button="right")
        page.wait_for_selector(".menu")
        assert page.locator(".menu .menu-item", has_text="Events over time").count() == 0
        page.keyboard.press("Escape")
    finally:
        _delete_board(page, did)


def test_row_menu_adds_a_count_of_the_cell_value(page):
    did = _new_board(page, "Quick C")
    try:
        cell = _cell(page, 2, "Host")
        value = cell.inner_text().strip()
        cell.click(button="right")
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item-sub", has_text="Add to dashboard").click()
        page.wait_for_selector(".menu-sub")
        page.locator(".menu-sub .menu-item", has_text=f"Count of Host = {value}").click()
        (w,) = _wait_widgets(page, did, 1)
        assert w["render"] == "stat" and w["title"] == f"Host = {value}"
        assert w["drill"] == {"table": f"src_{_src(page)}", "where": [{"column": "Host", "op": "equals", "value": value}]}
        assert _preview(page, w)["rows"][0][0] == 40
    finally:
        page.keyboard.press("Escape")
        _delete_board(page, did)


def test_filters_menu_adds_a_count_of_the_view_that_reopens_it(page):
    did = _new_board(page, "Quick D")
    box = page.locator('#filterRow input[data-col="Host"]')
    try:
        box.fill("H1")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        page.locator("#btnFilters").click()
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item", has_text="count of this view").click()
        (w,) = _wait_widgets(page, did, 1)
        assert "Host: H1" in w["title"]
        assert w["drill"]["table"] == f"src_{_src(page)}"
        assert w["drill"]["spec"]["filters"] == {"Host": "H1"}
        assert _preview(page, w)["rows"][0][0] == 40
        # the drill reopens the table in that state: clear, drill, count is back
        box.fill("")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        page.evaluate("(w) => __winnow.drillInto(w)", w)
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        assert page.locator('#filterRow input[data-col="Host"]').input_value() == "H1"
    finally:
        page.keyboard.press("Escape")
        page.locator('#filterRow input[data-col="Host"]').fill("")
        _delete_board(page, did)


def test_a_board_picker_appears_only_when_there_are_several(page):
    a = _new_board(page, "Pick A")
    b = _new_board(page, "Pick B")
    try:
        page.locator('.hcell[data-col="Host"]').click(button="right")
        page.wait_for_selector(".menu")
        page.locator(".menu .menu-item", has_text="Distinct count of Host").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-pick-board")
        page.locator("#modal .dash-pick-board").select_option(str(b))
        page.locator("#modal button", has_text="Add").click()
        (w,) = _wait_widgets(page, b, 1)
        assert w["build"]["template"] == "distinct"
        assert _widgets(page, a) == []
    finally:
        page.keyboard.press("Escape")
        _delete_board(page, a)
        _delete_board(page, b)


# ------------------------------------------------------------ drilldown


def _show(page, did):
    page.evaluate("(id) => __winnow.showDashboard(id)", did)
    page.wait_for_selector("#dashboardview:not([hidden])")


def test_clicking_a_stat_widget_opens_its_rows(page):
    did = _new_board(page, "Drill A")
    src = _src(page)
    try:
        w = page.evaluate("(t) => __winnow.widgetFrom({ template: 'countwhere', table: t, column: 'Host', value: 'H2', match: 'equals' })", f"src_{src}")
        _put_widgets(page, did, [w])
        _show(page, did)
        page.wait_for_selector("#dashGrid .dash-card .dash-stat")
        page.locator("#dashGrid .dash-widget-body.drillable").first.click()
        page.wait_for_selector("#dashboardview", state="hidden")
        page.wait_for_function("() => __winnow.S.sourceId != null && __winnow.S.view && __winnow.S.view.row_count === 40")
        tree = _cond_tree(page)
        assert tree["children"] == [{"type": "cond", "column": "Host", "op": "equals", "value": "H2"}]
        assert _cell(page, 0, "Host").inner_text().strip() == "H2"
    finally:
        _delete_board(page, did)


def test_clicking_a_list_row_pivots_on_its_value(page):
    did = _new_board(page, "Drill B")
    src = _src(page)
    try:
        w = page.evaluate("(t) => __winnow.widgetFrom({ template: 'rare', table: t, column: 'EventId' })", f"src_{src}")
        _put_widgets(page, did, [w])
        _show(page, did)
        page.wait_for_selector("#dashGrid .dash-list-row.drillable")
        first = page.locator("#dashGrid .dash-list-row.drillable").first
        label = first.locator(".t").inner_text().strip()
        first.click()
        page.wait_for_selector("#dashboardview", state="hidden")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50")
        assert _cond_tree(page)["children"] == [{"type": "cond", "column": "EventId", "op": "equals", "value": label}]
    finally:
        _delete_board(page, did)


def test_clicking_a_bar_pivots_on_its_value(page):
    did = _new_board(page, "Drill C")
    src = _src(page)
    try:
        w = page.evaluate("(t) => __winnow.widgetFrom({ template: 'top', table: t, column: 'Host' })", f"src_{src}")
        _put_widgets(page, did, [w])
        _show(page, did)
        page.wait_for_selector("#dashGrid canvas.drillable")
        page.wait_for_timeout(300)   # the bars draw on the next frame
        page.locator("#dashGrid canvas.drillable").first.click(position={"x": 12, "y": 8})   # the first bar's row
        page.wait_for_selector("#dashboardview", state="hidden")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        (cond,) = _cond_tree(page)["children"]
        assert cond["column"] == "Host" and cond["op"] == "equals" and cond["value"].startswith("H")
    finally:
        _delete_board(page, did)


def test_clicking_a_histogram_bucket_sets_the_timeframe(page):
    did = _new_board(page, "Drill D")
    src = _src(page)
    try:
        w = page.evaluate("(t) => __winnow.widgetFrom({ template: 'time', table: t, column: 'Timestamp', bucket: 'hour' })", f"src_{src}")
        _put_widgets(page, did, [w])
        _show(page, did)
        page.wait_for_selector("#dashGrid canvas.drillable")
        page.wait_for_timeout(300)
        page.locator("#dashGrid canvas.drillable").first.click(position={"x": 5, "y": 60})
        page.wait_for_selector("#dashboardview", state="hidden")
        page.wait_for_function("() => __winnow.S.timeRange.enabled === true")
        tr = page.evaluate("() => __winnow.S.timeRange")
        assert tr["column"] == "Timestamp" and tr["start"] == "2026-03-14 08:00:00" and tr["end"] == "2026-03-14 08:59:59"
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
    finally:
        page.evaluate("() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' }; }")
        _delete_board(page, did)


def test_a_widget_with_only_sql_opens_as_a_query(page):
    did = _new_board(page, "Drill E")
    src = _src(page)
    tab_id = None
    try:
        _put_widgets(page, did, [{"title": "Hand-written", "source": "sql", "render": "stat",
                                  "query": {"sql": f"SELECT COUNT(*) FROM src_{src} WHERE EventId = '1'"}}])
        _show(page, did)
        page.wait_for_selector("#dashGrid .dash-drill")
        assert page.locator("#dashGrid .dash-widget-body.drillable").count() == 0   # no drill, so the number is not a link
        page.locator("#dashGrid .dash-drill").first.click()
        page.wait_for_selector("#sqlview:not([hidden])")
        page.wait_for_function("() => document.getElementById('sqlText').value.includes('WHERE EventId')")
        tab_id = page.evaluate("() => __winnow.S.sqlTabId")
    finally:
        if tab_id is not None:
            page.evaluate(f"(id) => fetch('/api/sql_tabs/' + id, {{ method: 'DELETE', headers: {H} }})", tab_id)
        _delete_board(page, did)


# ------------------------------------------------------------ the editor


def test_guided_editor_writes_the_sql_from_a_template_and_a_column(page):
    did = _new_board(page, "Editor A")
    try:
        _show(page, did)
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-template")
        # the SQL stays folded away while a template is doing the writing
        page.locator("#modal .dash-template").select_option("top")
        page.locator("#modal .dash-column").select_option("Host")
        assert 'GROUP BY "Host"' in page.locator("#modal .dash-sql").input_value()
        assert page.locator("#modal .confirm-input").first.input_value() == "Top Host"
        assert not page.evaluate("() => document.querySelector('#modal .dash-advanced').open")
        page.locator("#modal button", has_text="Preview").click()
        page.wait_for_selector("#modal .dash-preview canvas")
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_selector("#modal[hidden]", state="attached")
        (w,) = _wait_widgets(page, did, 1)
        assert w["build"]["column"] == "Host" and w["drill"]["column"] == "Host"
        # reopening is guided: the recipe comes back, not a wall of SQL
        page.locator("#dashGrid .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden]) .dash-template")
        assert page.locator("#modal .dash-template").input_value() == "top"
        assert page.locator("#modal .dash-column").input_value() == "Host"
        page.keyboard.press("Escape")
    finally:
        _delete_board(page, did)


def test_hand_edited_sql_drops_the_recipe_and_the_drill(page):
    did = _new_board(page, "Editor B")
    try:
        _show(page, did)
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-template")
        page.locator("#modal .dash-template").select_option("count")
        page.evaluate("() => { document.querySelector('#modal .dash-advanced').open = true; }")
        box = page.locator("#modal .dash-sql")
        box.fill(box.input_value() + " WHERE EventId = '1'")
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_selector("#modal[hidden]", state="attached")
        (w,) = _wait_widgets(page, did, 1)
        assert "WHERE EventId" in w["query"]["sql"]
        assert "build" not in w and "drill" not in w
    finally:
        _delete_board(page, did)


def test_blank_template_shows_the_sql_up_front(page):
    did = _new_board(page, "Editor C")
    try:
        _show(page, did)
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-template")
        assert page.locator("#modal .dash-template").input_value() == "blank"
        assert page.evaluate("() => document.querySelector('#modal .dash-advanced').open")
        page.keyboard.press("Escape")
    finally:
        _delete_board(page, did)


# ------------------------------------------------------------ new board


def test_new_dashboard_starter_builds_from_the_open_table(page):
    page.locator("#sidebarList .menu-item", has_text="New dashboard").click()
    page.wait_for_selector("#modal:not([hidden]) .dash-start-from")
    assert page.locator("#modal .dash-start-from").input_value() == "starter"
    page.locator("#modal .confirm-input").first.fill("Starter board")
    page.keyboard.press("Enter")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_selector("#dashboardview:not([hidden])")
    did = page.evaluate("() => __winnow.S.dashboardId")
    try:
        # row count, activity window, over time, top values of EventId (4) and Host (5)
        page.wait_for_function("() => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === 5", timeout=10_000)
        titles = [w["title"] for w in _widgets(page, did)]
        assert titles[0].startswith("Rows in") and "Activity window" in titles and "Timestamp over time" in titles
        assert "Top EventId" in titles and "Top Host" in titles
        assert all("drill" in w for w in _widgets(page, did))
    finally:
        _delete_board(page, did)


def test_new_dashboard_can_start_from_a_shipped_board(page):
    from winnow import defaults
    kape = next(p for p in defaults.profiles() if p["name"] == "KAPE triage")
    page.locator("#sidebarList .menu-item", has_text="New dashboard").click()
    page.wait_for_selector("#modal:not([hidden]) .dash-start-from")
    page.locator("#modal .confirm-input").first.fill("My triage")
    page.locator("#modal .dash-start-from").select_option(label=f"Shipped: KAPE triage ({len(kape['dashboard'])} widgets)")
    page.locator("#modal button", has_text="Create").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_selector("#dashboardview:not([hidden])")
    did = page.evaluate("() => __winnow.S.dashboardId")
    try:
        assert len(_widgets(page, did)) == len(kape["dashboard"])
    finally:
        _delete_board(page, did)

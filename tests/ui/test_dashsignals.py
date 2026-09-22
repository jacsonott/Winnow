"""A signals card on screen: many numbers, one request, and every cell
still leads back to its own rows.

The point of the render kind is the drill. Eleven single-number cards
each opened exactly the rows it counted; folding them into one `kv` card
would have folded eleven drills into one OR-of-everything, and the card
would still have looked fine. So what is asserted here is the click —
the cell clicked, not the card, decides which rows the grid opens — plus
the two things only a browser can show: a cell whose table is not in this
case reporting in its own place while its neighbours show numbers, and
the editor giving a four-number card back with its four numbers.

The shared fixture table is 200 rows over four EventIds, 50 each.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

PREVIEW = re.compile(r".*/api/dashboard/widget/preview$")
H = "{ 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' }"


def _src(page):
    return page.evaluate("() => __winnow.S.sourceId")


def _board(page, name, widgets):
    return page.evaluate(f"""async ([name, widgets]) => {{
      const d = await fetch('/api/dashboards', {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ name, widgets }}) }}).then(r => r.json());
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
      return d.id;
    }}""", [name, widgets])


def _drop(page, did):
    page.evaluate(f"""async (id) => {{
      await fetch('/api/dashboards/' + id, {{ method: 'DELETE', headers: {H} }});
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
    }}""", did)


def _widgets(page, did):
    return page.evaluate("(id) => fetch('/api/dashboards/' + id).then(r => r.json()).then(b => b.widgets)", did)


def _show(page, did):
    page.evaluate("(id) => __winnow.showDashboard(id)", did)
    page.wait_for_selector("#dashboardview:not([hidden])", timeout=15_000)


def _card(page, src):
    t = f"src_{src}"
    return {
        "title": "Triage signals", "source": "cells", "render": "signals", "span": 4,
        "cells": [
            {"label": "Logons", "source": "sql",
             "query": {"sql": f'SELECT COUNT(*) FROM {t} WHERE "EventId" = \'4624\''},
             "drill": {"table": t, "where": [{"column": "EventId", "op": "equals", "value": "4624"}]}},
            {"label": "Failed logons", "source": "sql", "tone": "warn",
             "query": {"sql": f'SELECT COUNT(*) FROM {t} WHERE "EventId" = \'4625\''},
             "drill": {"table": t, "where": [{"column": "EventId", "op": "equals", "value": "4625"}]}},
            # No RECmd batch in this case: this cell cannot answer, and the
            # ones beside it must not care.
            {"label": "Registry values", "source": "sql",
             "query": {"sql": "SELECT COUNT(*) FROM {{registry}}"}},
            {"label": "Command lines", "chip": True, "source": "sql",
             "query": {"sql": f'SELECT COUNT(*) > 0 FROM {t} WHERE "CommandLine" <> \'\''},
             "drill": {"table": t, "where": [{"column": "CommandLine", "op": "not_empty", "value": ""}]}},
        ],
    }


def _signals(page):
    return page.locator("#dashGrid .dash-signal")


def _clear_grid(page):
    """The drill leaves the grid filtered; the case fixture is shared."""
    page.evaluate("""async () => {
      __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] };
      __winnow.updateFiltersButton();
      await __winnow.rebuildView({ keepScroll: false, keepRow: false });
    }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200", timeout=15_000)


def test_each_cell_of_a_signals_card_opens_the_rows_it_counted(page):
    did = _board(page, "Signals drill", [_card(page, _src(page))])
    try:
        _show(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-signal').length === 3", timeout=15_000)
        assert [s.strip() for s in _signals(page).locator(".l").all_text_contents()] \
            == ["Logons", "Failed logons", "Registry values"]
        assert [s.strip() for s in _signals(page).locator(".n").all_text_contents()] == ["50", "50", "—"]
        # The cell that could not answer says so in its own place, and
        # carries the reason; the two beside it are unaffected.
        assert "Registry (RECmd batch)" in _signals(page).nth(2).get_attribute("title")
        # The warn cell is the red one, and only it.
        assert page.locator("#dashGrid .dash-signal.warn .l").inner_text().strip() == "Failed logons"
        # The chip cell reads as a yes/no pill, above the numbers.
        chip = page.locator("#dashGrid .dash-chipline .dash-chip")
        assert chip.count() == 1 and chip.inner_text().strip() == "Command lines ✓"

        # The click that matters: the SECOND cell, not the card.
        _signals(page).nth(1).click()
        page.wait_for_selector("#dashboardview", state="hidden", timeout=15_000)
        page.wait_for_function(
            "() => __winnow.S.view && __winnow.S.view.row_count === 50", timeout=15_000)
        assert page.evaluate("() => __winnow.S.filterTree.children") \
            == [{"type": "cond", "column": "EventId", "op": "equals", "value": "4625"}]
    finally:
        _clear_grid(page)
        _drop(page, did)


def test_a_card_of_many_numbers_is_one_request(page):
    """Eleven cards were eleven round trips on every open. Eleven cells
    are one — which is half of why the KAPE board got shorter."""
    did = _board(page, "Signals cost", [_card(page, _src(page))])
    bodies = []
    page.route(PREVIEW, lambda route: (bodies.append(route.request.post_data_json or {}), route.continue_()))
    try:
        _show(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-signal').length === 3", timeout=15_000)
        assert len(bodies) == 1, bodies
        assert len(bodies[0]["cells"]) == 4
    finally:
        page.unroute(PREVIEW)
        _drop(page, did)


def test_saving_a_signals_card_in_the_editor_keeps_its_cells(page):
    """The editor writes one question per widget, and this card asks four.
    A Save that dropped them would turn a four-number card into an empty
    one, with nothing on screen saying it had happened."""
    did = _board(page, "Signals editor", [_card(page, _src(page))])
    try:
        _show(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-signal').length === 3", timeout=15_000)
        page.locator("#dashGrid .dash-card:not(.dash-add) .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden]) .dash-form", timeout=15_000)
        # It names what it is not editing rather than showing a blank SQL box.
        assert "4 signals on this card" in page.locator("#modal .dash-cells-note").inner_text()
        page.locator("#modal .dash-form input.confirm-input").first.fill("Renamed signals")
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=15_000)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-signal').length === 3", timeout=15_000)
        (w,) = _widgets(page, did)
        assert w["title"] == "Renamed signals"
        assert w["source"] == "cells" and w["render"] == "signals"
        assert [c["label"] for c in w["cells"]] == ["Logons", "Failed logons", "Registry values", "Command lines"]
        assert w["cells"][1]["drill"]["where"][0]["value"] == "4625"
    finally:
        page.keyboard.press("Escape")
        _drop(page, did)


def test_a_card_moved_off_signals_does_not_keep_its_cells(page):
    """`cells` has to be deleted on save for the same reason `build`,
    `drill` and `live` are: `Object.assign` cannot clear a key the draft
    leaves out. A widget that is a stat over one query, still carrying the
    four questions of the card it used to be, is a card whose cache
    fingerprint and whose editor disagree with what it draws."""
    did = _board(page, "Signals moved", [_card(page, _src(page))])
    try:
        _show(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-signal').length === 3", timeout=15_000)
        page.locator("#dashGrid .dash-card:not(.dash-add) .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden]) .dash-form", timeout=15_000)
        page.locator("#modal .dash-form select").first.select_option("sql")
        page.locator("#modal .dash-form-row-3 select").first.select_option("stat")
        page.locator("#modal textarea.dash-sql").fill(f"SELECT COUNT(*) FROM src_{_src(page)}")
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=15_000)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 1"
            " && document.querySelectorAll('#dashGrid .dash-signal').length === 0", timeout=15_000)
        (w,) = _widgets(page, did)
        assert w["source"] == "sql" and w["render"] == "stat"
        assert "cells" not in w, w.get("cells")
    finally:
        page.keyboard.press("Escape")
        _drop(page, did)

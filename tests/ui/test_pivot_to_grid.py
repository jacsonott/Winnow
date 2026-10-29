"""A Pivot cell's "Show these rows in the grid" (examples/plugins/pivot).

What was asked for was the Pivot and the rows under it, one gesture
apart. That the grid's filters SELECT the cell's rows is pinned operator by
operator in tests/test_plugins.py, against the grid's own build_view. What
is pinned here is what only the app shows: the gesture lands on the table
with the number the cell said, its Filters area, its tag chips and the
timeframe all accounted for, and it lands in the MAIN window when the
Pivot has been popped out beside it.

The pivot under test is seeded as saved tab state rather than built by
dragging fields, which tests/ui/test_pivot_tags.py already covers. On
ui.csv (row i: Host H{i%5}, EventId cycling 4624/4625/4688/1, Payload user
u{i%3}, Timestamp 08:mm:ss counting seconds from 08:00:00), the cell
H1 × 4625 is i = 1, 21, 41, ..., 181. Tagged Suspicious on rows 1-120
(i < 120) that is 1, 21, 41, 61, 81, 101; without user u2, 1, 21, 61, 81:
four rows. A timeframe of the first minute keeps 1 and 21 of those, so a
grid that kept it would show two rows under a cell that said four.
"""

from __future__ import annotations

import contextlib
import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"
SUSPICIOUS_RIDS = list(range(1, 121))
CELL = ("H1", "4625")
CELL_COUNT = 4
FIRST_MINUTE = {"enabled": True, "column": "Timestamp",
                "start": "2026-03-14 08:00:00", "end": "2026-03-14 08:00:59"}
POPPED = "document.documentElement.hasAttribute('data-popout') && !document.getElementById('popoutBar').hidden"

# Marks the cell at (row label, column header) so a locator can
# reach it. Nothing until the cross-tab has drawn, so it doubles as the
# wait for that.
MARK_CELL = """(args) => {
  const [row, col] = args;
  const heads = [...document.querySelectorAll('.pluginview table thead tr:first-child th')].slice(1);
  const j = heads.findIndex((th) => th.textContent.trim() === col);
  const tr = [...document.querySelectorAll('.pluginview table tbody tr')]
    .find((r) => r.firstElementChild && r.firstElementChild.title === row);
  const td = tr && j >= 0 ? tr.children[1 + j] : null;
  if (!td || !td.textContent.trim()) return false;
  td.setAttribute('data-test-cell', '');
  return true;
}"""


def _get(server, route):
    with urllib.request.urlopen(server.rstrip("/") + route, timeout=30) as r:
        return json.loads(r.read())


def _pivot(source_id, filters):
    return {"v": 1, "active": 0, "pivots": [{
        "name": "Pivot 1", "source": {"id": source_id, "name": "ui.csv"},
        "rows": ["Host"], "cols": ["EventId"], "values": [{"agg": "count"}],
        "filters": filters, "tags": [], "subtotals": True, "grandTotals": True,
    }]}


@pytest.fixture
def seeded(page, server, server_post):
    """The pivot plugin on, ui.csv's rows 1-120 tagged Suspicious, and a
    function that seeds the saved pivot and opens its tab."""
    tags = {t["name"]: t["id"] for t in _get(server, "/api/tags")["tags"]}
    sid = next(s["id"] for s in _get(server, "/api/sources") if s["name"] == "ui.csv")
    sus = tags["Suspicious"]
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/row_tags", {"source_id": sid, "rids": SUSPICIOUS_RIDS, "tag_id": sus, "on": True})

    def _open(filters, tag_filter):
        state = _pivot(sid, filters)
        state["pivots"][0]["tags"] = tag_filter
        server_post("/api/plugin_state", {"key": KEY, "payload": state})
        page.evaluate("() => __winnow.loadTags()")
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                               timeout=10_000)
        page.locator(".tab-plugin", has_text="Pivot").click()
        page.wait_for_function(MARK_CELL, arg=list(CELL), timeout=15_000)
        return page.evaluate("() => __winnow.S.pluginTabs.find((t) => t.id.includes('pivot')).id")

    try:
        yield {"source_id": sid, "sus": sus, "open": _open}
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})
        server_post("/api/row_tags", {"source_id": sid, "rids": SUSPICIOUS_RIDS, "tag_id": sus, "on": False})


# Every kind of thing the Filters area holds: a list kept (in), a list left
# out (not in), and a single value (does not contain).
FILTERS = [
    {"column": "Host", "op": "in", "values": ["H1", "H2"], "value": ""},
    {"column": "EventId", "op": "not_in", "values": ["1"], "value": ""},
    {"column": "Payload", "op": "not_contains", "values": [], "value": '"u2"'},
]


def _set_timeframe(page):
    page.evaluate("(r) => __winnow.setCaseTimeRange(r)", FIRST_MINUTE)
    page.wait_for_function("() => __winnow.S.timeRange.enabled && __winnow.busyCount === 0")


def _grid_shows(page, source_id, n):
    page.wait_for_function(
        """([sid, n]) => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === sid
             && __winnow.S.view && __winnow.S.view.row_count === n && __winnow.busyCount === 0""",
        arg=[source_id, n], timeout=15_000)


def test_a_cell_opens_the_grid_on_the_rows_it_counted(page, seeded):
    page.wait_for_selector("#grid .row")
    _set_timeframe(page)
    seeded["open"](FILTERS, [seeded["sus"]])
    cell = page.locator("[data-test-cell]")
    assert int(cell.inner_text().replace(",", "")) == CELL_COUNT

    cell.click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")
    page.locator(".menu .menu-item", has_text="Show these rows in the grid").click()

    _grid_shows(page, seeded["source_id"], CELL_COUNT)
    got = page.evaluate("""() => ({ filters: { ...__winnow.S.filters }, tags: [...__winnow.S.tagFilter],
                                   timeframe: __winnow.S.timeRange.enabled })""")
    # The cell's own values are in the boxes, where the filter bar shows them.
    assert set(got["filters"]) == {"Host", "EventId"}
    assert got["tags"] == [seeded["sus"]]
    assert got["timeframe"] is False
    page.wait_for_function("() => /Timeframe filter turned off/.test(document.querySelector('#toast').textContent)")


@contextlib.contextmanager
def _popped_out(page, tab_id):
    tab = ".tab-plugin:has-text('Pivot')"
    page.click(tab, button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")
    with page.expect_popup() as info:
        page.locator(".menu:not(.menu-sub) .menu-item", has_text="Open in its own window").click()
    pop = info.value
    errors: list[str] = []
    pop.on("pageerror", lambda e: errors.append(str(e)))
    pop.wait_for_load_state()
    pop.wait_for_function(POPPED)
    try:
        yield pop
    finally:
        if not pop.is_closed():
            pop.close()
        page.wait_for_function("(k) => !window.__winnow.isPoppedOut(k)", arg=f"plugin:{tab_id}")
    assert not errors, "uncaught JS errors in the pop-out: " + " | ".join(errors)


def test_from_a_popped_out_pivot_the_rows_open_in_the_main_window(page, seeded):
    """The case the pop-out was asked for: the Pivot in its own window,
    the table beside it. The rows behind a cell open in the main window —
    a grid filtered inside the pop-out would be one nobody can see — and
    the Pivot stays where it is."""
    page.wait_for_selector("#grid .row")
    tab_id = seeded["open"](FILTERS, [seeded["sus"]])
    page.evaluate("() => __winnow.showGridTab()")
    _set_timeframe(page)

    with _popped_out(page, tab_id) as pop:
        pop.wait_for_function(MARK_CELL, arg=list(CELL), timeout=15_000)
        # From the drill-down's own button this time: the same plan, the
        # other way to it.
        pop.locator("[data-test-cell]").click()
        pop.wait_for_selector("#modal:not([hidden]) table tbody tr", timeout=10_000)
        assert pop.locator("#modal table tbody tr").count() == CELL_COUNT
        pop.locator("#modal").get_by_role("button", name="Show these rows in the grid").click()

        _grid_shows(page, seeded["source_id"], CELL_COUNT)
        assert page.evaluate("() => __winnow.S.timeRange.enabled") is False
        assert pop.locator("#modal").is_hidden()
        assert pop.locator(".pluginview table").count() == 1, "the pop-out left its Pivot"


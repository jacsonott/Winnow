"""Plugins show a timestamp the way the case says timestamps read.

Reported as: the grid shows a column as YYYY-MM-DD HH:MM:SS, the case's
format, and the Pivot beside it shows the same values as the file wrote
them, `6/3/2026 1:02:03 PM`. Every bundled plugin painted the stored text.
The plugin context now hands them the grid's own formatter
(winnow.formatTimestamp, winnow.displayCell) and says when the format
changes (winnow.onTimestampFormatChange), so what is asserted here is what
the analyst reads, in the terms it broke in: the label text, its order,
and that a format change reaches a tab already drawn, in this window and
in a pop-out.

The table is US-format on purpose. Its stored text is neither the case's
format nor in time order (`6/10/2026` sorts before `6/3/2026`, and both
after `12/1/2025` only by luck of the digits), so a plugin still painting
or ordering the stored text fails every assertion below.
"""

from __future__ import annotations

import contextlib

import pytest

from test_popout import popped_out

pytestmark = pytest.mark.ui

PIVOT_KEY = "tab:pivot-table.pivot"
FL_KEY = "tab:first-last.firstlast"
NAME = "plugin_ts.csv"

ROWS = [
    ("12/1/2025 8:00:00 AM", "WKS1"),
    ("6/3/2026 1:02:03 PM", "WKS1"),
    ("6/3/2026 1:02:03 PM", "WKS2"),
    ("6/4/2026 9:00:00 AM", "WKS2"),
    ("6/10/2026 9:00:00 AM", "WKS3"),
]
# Each distinct stored value as the case's default (ISO) and US formats
# show it, in time order.
ISO = ["2025-12-01 08:00:00", "2026-06-03 13:02:03", "2026-06-04 09:00:00", "2026-06-10 09:00:00"]
US = ["12/01/2025 08:00:00", "06/03/2026 13:02:03", "06/04/2026 09:00:00", "06/10/2026 09:00:00"]


@pytest.fixture(scope="module")
def table(tmp_path_factory, imported, server_post, server_call):
    path = tmp_path_factory.mktemp("plugin_ts") / NAME
    path.write_text("When,Host\n" + "".join(f'"{w}",{h}\n' for w, h in ROWS), encoding="utf-8")
    sid = imported({"path": str(path), "name": NAME, "kind": "csv"})[0]
    src = next(s for s in server_call("/api/sources") if s["id"] == sid)
    assert {c["name"]: c["type"] for c in src["columns"]}["When"] == "datetime"
    try:
        yield sid
    finally:
        # The case's format back to the machine default, whatever a test
        # left it at, and the table gone.
        server_post("/api/case_settings", {"ts_format": ""})
        server_call(f"/api/source/{sid}", "DELETE")


def _pivot_payload(sid, rows, values, grand=True):
    return {"v": 1, "active": 0, "pivots": [{
        "name": "Times", "source": {"id": sid, "name": NAME},
        "rows": rows, "cols": [], "values": values, "filters": [], "tags": [],
        "subtotals": True, "grandTotals": grand,
    }]}


@contextlib.contextmanager
def _plugin_on(server_post, fs_name, key, payload):
    """A plugin switched on with its tab's saved state seeded, so the tab
    opens on a pivot or sheet already built — and both undone after."""
    server_post("/api/plugins/toggle", {"fs_name": fs_name, "scope": "on_all"})
    server_post("/api/plugin_state", {"key": key, "payload": payload})
    try:
        yield
    finally:
        server_post("/api/plugin_state", {"key": key, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": fs_name, "scope": "off_all"})


def _open_tab(pg, label, tab_id_part):
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("(p) => __winnow.S.pluginTabs.some((t) => t.id.includes(p))", arg=tab_id_part,
                         timeout=10_000)
    pg.locator(".tab-plugin", has_text=label).click()


# The first cell of each body row of the pivot: its row labels, totals out.
ROW_LABELS = """() => [...document.querySelectorAll('.pluginview table tbody tr')]
  .map((tr) => tr.firstElementChild.textContent)
  .filter((t) => !/total$/i.test(t))"""


def _labels_are(pg, want):
    pg.wait_for_function(f"(w) => JSON.stringify(({ROW_LABELS})()) === JSON.stringify(w)", arg=want,
                         timeout=15_000)


def _set_case_format(pg, key):
    """Pick the case's format the way the analyst does: Case settings →
    Timestamps, a row per format ('' is Machine default)."""
    pg.evaluate("() => __winnow.openCaseSettings('timestamps')")
    row = pg.locator(f".cs-ts-row[data-id='{key or 'default'}'] input")
    with pg.expect_response(lambda r: "/api/case_settings" in r.url and r.request.method == "POST"):
        row.check()
    pg.evaluate("() => __winnow.closeModal()")


# ------------------------------------------------------------------ pivot


def test_pivot_labels_read_in_the_case_format_and_in_time_order(own_page, server_post, table):
    with _plugin_on(server_post, "pivot", PIVOT_KEY, _pivot_payload(table, ["When"], [{"agg": "count"}])), \
            own_page() as pg:
        _open_tab(pg, "Pivot", "pivot")
        # The machine default is ISO, and the case sets nothing.
        _labels_are(pg, ISO)
        # The pivot still groups on the stored text: the two rows stored
        # as "6/3/2026 1:02:03 PM" are one group of 2.
        counts = pg.evaluate("""() => [...document.querySelectorAll('.pluginview table tbody tr')]
          .map((tr) => [tr.firstElementChild.textContent, tr.lastElementChild.textContent])""")
        assert dict(counts)["2026-06-03 13:02:03"] == "2"

        # A new case format reaches the tab already drawn, without a re-run.
        aggregates = []
        pg.on("request", lambda r: aggregates.append(r) if "/api/plugin/pivot/aggregate" in r.url else None)
        _set_case_format(pg, "us")
        _labels_are(pg, US)
        assert not aggregates, "a format change re-ran the pivot; it only has to redraw"

        _set_case_format(pg, "")
        _labels_are(pg, ISO)


def test_pivot_min_and_max_of_a_timestamp_read_in_the_case_format(own_page, server_post, table):
    values = [{"column": "When", "agg": "min"}, {"column": "When", "agg": "max"}]
    with _plugin_on(server_post, "pivot", PIVOT_KEY, _pivot_payload(table, ["Host"], values, grand=False)), \
            own_page() as pg:
        _open_tab(pg, "Pivot", "pivot")
        pg.wait_for_function("() => document.querySelectorAll('.pluginview table tbody tr').length === 3",
                             timeout=15_000)
        cells = pg.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.pluginview table tbody tr')]
          .map((tr) => [tr.children[0].textContent, [tr.children[1].textContent, tr.children[2].textContent]]))""")
        assert cells["WKS1"] == ["2025-12-01 08:00:00", "2026-06-03 13:02:03"]
        assert cells["WKS3"] == ["2026-06-10 09:00:00", "2026-06-10 09:00:00"]


def test_a_pivot_popped_out_follows_a_format_change_in_the_main_window(page, server_post, table):
    with _plugin_on(server_post, "pivot", PIVOT_KEY, _pivot_payload(table, ["When"], [{"agg": "count"}])):
        _open_tab(page, "Pivot", "pivot")
        _labels_are(page, ISO)
        tab_id = page.evaluate("() => __winnow.S.pluginTabs.find((t) => t.id.includes('pivot')).id")
        with popped_out(page, key=f"plugin:{tab_id}", tab=".tab-plugin:has-text('Pivot')") as pop:
            _labels_are(pop, ISO)
            _set_case_format(page, "us")
            _labels_are(pop, US)
            _set_case_format(page, "")
            _labels_are(pop, ISO)


# ------------------------------------------------------------- first/last


def test_first_last_preview_and_its_copy_read_in_the_case_format(own_page, server_post, table, copied):
    sheet = {"name": "Times", "source": {"id": table, "name": NAME}, "groupBy": ["Host"], "carry": [],
             "sums": [], "unique": [], "values": [], "filters": [], "tags": [], "rowJson": False,
             "sortColumn": "When", "colWidths": {}, "template": ""}
    with _plugin_on(server_post, "first_last", FL_KEY, {"v": 1, "active": 0, "sheets": [sheet]}), \
            own_page(permissions=["clipboard-read", "clipboard-write"]) as pg:
        _open_tab(pg, "First/Last", "firstlast")
        body = ".pluginview table tbody tr"
        pg.wait_for_function(f"() => document.querySelectorAll('{body}').length === 5", timeout=15_000)
        cells = pg.evaluate(f"""() => [...document.querySelectorAll('{body}')]
          .map((tr) => [...tr.children].map((td) => td.textContent))""")
        when = {r[0] for r in cells}
        assert when == set(ISO), cells
        # The description is text the plugin wrote and reads as written.
        assert {r[-1] for r in cells} == {"First of 2", "Last of 2", "Only of 1"}

        # What is copied is what is shown, as a cell copied from the grid is.
        pg.locator(body).first.click()
        with copied(pg) as clip:
            pg.keyboard.press("Control+c")
        assert clip.text.split("\t")[0] in ISO

        _set_case_format(pg, "us")
        pg.wait_for_function(
            f"(w) => [...document.querySelectorAll('{body}')].every((tr) => w.includes(tr.firstElementChild.textContent))",
            arg=US, timeout=15_000)
        _set_case_format(pg, "")


# ------------------------------------------------------------- top values

PANEL = '#pluginPanels section[data-panel-id="top-values.top_values"]'


def test_top_values_reads_as_the_grid_under_it_does(page, server_post, table):
    server_post("/api/plugins/toggle", {"fs_name": "top_values", "scope": "on_all"})
    try:
        page.evaluate("() => __winnow.loadSources()")
        page.evaluate("(id) => __winnow.openSource(id)", table)
        page.wait_for_function("(id) => __winnow.S.view && __winnow.S.sourceId === id", arg=table)
        page.evaluate("() => __winnow.loadPlugins()")
        btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Top values")
        btn.wait_for(state="visible", timeout=10_000)
        btn.click()
        page.wait_for_selector(f"{PANEL} .tv-value", timeout=10_000)
        page.locator(PANEL).locator("select").select_option("When")
        top = f"() => (document.querySelector('{PANEL} .tv-value') || {{}}).textContent"
        page.wait_for_function(f"() => ({top})() === '2026-06-03 13:02:03 2'", timeout=10_000)

        # A format picked on the column itself beats the case's, in the
        # grid and in the panel above it.
        page.locator('.hcell[data-col="When"]').click(button="right")
        page.locator(".menu .menu-item", has_text="MM/DD/YYYY HH:MM:SS").first.click()
        page.wait_for_function(f"() => ({top})() === '06/03/2026 13:02:03 2'", timeout=10_000)
        page.locator(f"{PANEL} .tv-value").first.click()
        assert page.evaluate("() => navigator.clipboard.readText()") == "06/03/2026 13:02:03"
    finally:
        page.evaluate("() => { __winnow.togglePluginPanel('top-values.top_values', false); "
                      "localStorage.removeItem('winnow.panels'); }")
        server_post("/api/plugins/toggle", {"fs_name": "top_values", "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")


def test_the_context_formats_with_the_grids_formatter(page):
    """One formatter, not a plugin's copy of it: whatever shape the grid
    learns to read, a plugin reads too."""
    got = page.evaluate("""() => {
      const ctx = __winnow.buildPluginTabContext({ plugin: 't', plugin_fs: 't', id: 'probe' });
      const vs = ['6/3/2026 1:02:03 PM', 'June 23, 2026 at 5:11:22 PM', '2026-06-23T13:05:01.123Z', 'not a time', ''];
      return {
        fmt: ctx.state.tsFormat,
        same: vs.every((v) => ctx.formatTimestamp(v) === __winnow.formatTimestamp(v, __winnow.caseTsFormat())),
        us: ctx.formatTimestamp('2026-06-23 13:05:01', 'us'),
        sortable: ctx.formatTimestamp('6/3/2026 1:02:03.5 PM', 'iso_us'),
        odd: ctx.formatTimestamp('not a time'),
      };
    }""")
    assert got == {"fmt": "iso", "same": True, "us": "06/23/2026 13:05:01",
                   "sortable": "2026-06-03 13:02:03.500000", "odd": "not a time"}

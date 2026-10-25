"""The pivot's tag chips: the grid ribbon's filter, in the Filters well.

What the chips SELECT is pinned in tests/test_plugins.py, against the grid's
own build_view. What is pinned here is what only a browser shows: that the
chips are there, that a click, a right-click Exclude and Clear send the
grid's spelling and re-run the cross-tab, that the drill-down carries the
filter, and that coming back from the grid — where rows get tagged and
tags get deleted — re-runs a pivot whose filter is on tags rather than
leaving yesterday's count on screen.

ui.csv is the shared session table: rows 1-20 are tagged Suspicious and
11-30 Benign here, so 11-20 carry both, and every tag is taken off again in
a finally.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"

DRAG_FIELD = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""

SUSPICIOUS_RIDS = list(range(1, 21))
BENIGN_RIDS = list(range(11, 31))

# planRestore is pure; imported under its own query string so the mounted
# tab's module instance is untouched (test_firstlast_state.py does the same).
PIVOT_PLAN = """(args) => {
  window.__plan = undefined;
  import('/plugin_assets/pivot/ui/tab.js?tagrestoretest=1')
    .then((m) => { window.__plan = m.planRestore(args[0], args[1], args[2]); });
}"""


def _get(server, route):
    with urllib.request.urlopen(server.rstrip("/") + route, timeout=30) as r:
        return json.loads(r.read())


def _is_aggregate(r):
    return "/api/plugin/pivot/aggregate" in r.url and r.request.method == "POST"


def _aggregate_with(tags):
    """The aggregate request sent for exactly this tag filter. Waiting on
    any aggregate would race: two quick edits inside the debounce are one
    request, two slower ones are two, and the first answer is the stale one."""
    return lambda r: _is_aggregate(r) and (r.request.post_data_json or {}).get("tags") == tags


@pytest.fixture(scope="module")
def tagged(server, server_post):
    """{tag name: id} for the case, with ui.csv's rows tagged for the module."""
    tags = {t["name"]: t["id"] for t in _get(server, "/api/tags")["tags"]}
    sid = next(s["id"] for s in _get(server, "/api/sources") if s["name"] == "ui.csv")
    writes = [(SUSPICIOUS_RIDS, tags["Suspicious"]), (BENIGN_RIDS, tags["Benign"])]
    try:
        for rids, tag_id in writes:
            server_post("/api/row_tags", {"source_id": sid, "rids": rids, "tag_id": tag_id, "on": True})
        yield {"ids": tags, "source_id": sid}
    finally:
        for rids, tag_id in writes:
            server_post("/api/row_tags", {"source_id": sid, "rids": rids, "tag_id": tag_id, "on": False})


@pytest.fixture(scope="module")
def pv_page(own_page, server_post, tagged):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    try:
        with own_page() as pg:
            pg.evaluate("() => __winnow.loadPlugins()")
            pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                                 timeout=10_000)
            yield pg
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


def _open_tab(pg):
    pg.locator(".tab-plugin", has_text="Pivot").click()
    pg.wait_for_selector(".pluginview [data-zone='rows']", timeout=10_000)
    pg.wait_for_selector(".pluginview [data-field]", timeout=10_000)


def _count_of_host_by_host(pg, source_id):
    """Rows: Host, Values: Count of Host, on ui.csv — so the grand total is
    the number of rows the pivot counted."""
    sel = ".pluginview select"
    if pg.eval_on_selector(sel, "(s) => s.value") != str(source_id):
        pg.select_option(sel, str(source_id))
    pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
    pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='rows']"])
    with pg.expect_response(_is_aggregate, timeout=15_000):
        pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='values']"])
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)


GRAND_TOTAL = """() => {
  const rows = document.querySelectorAll('.pluginview table tbody tr');
  const cell = rows.length ? rows[rows.length - 1].lastElementChild : null;
  return cell ? cell.textContent.replace(/,/g, '') : null;
}"""


def _total_is(pg, n):
    """Waits for the cross-tab's grand total to read `n` — the answer lands
    a tick after its response does, so reading it once would race."""
    pg.wait_for_function(f"() => ({GRAND_TOTAL})() === '{n}'", timeout=15_000)


def _chip(pg, key):
    return pg.locator(f".pluginview [data-zone='filters'] [data-tag='{key}']")


def test_the_chips_filter_the_cross_tab_the_way_the_ribbon_does(pv_page, tagged):
    pg = pv_page
    ids = tagged["ids"]
    sus, ben = ids["Suspicious"], ids["Benign"]
    _open_tab(pg)
    _count_of_host_by_host(pg, tagged["source_id"])
    _total_is(pg, 200)

    # Every tag the case has, plus Any tag and No tags, in the Filters well,
    # with this table's count on each.
    for t in ids.values():
        assert _chip(pg, t).count() == 1
    assert _chip(pg, "__any__").count() == 1 and _chip(pg, "__none__").count() == 1
    pg.wait_for_function(
        f"() => document.querySelector(\".pluginview [data-tag='{sus}'] .n\")?.textContent === '20'",
        timeout=10_000)

    with pg.expect_response(_aggregate_with([sus]), timeout=15_000):
        _chip(pg, sus).click()
    assert _chip(pg, sus).get_attribute("aria-pressed") == "true"
    _total_is(pg, 20)

    # Right-click opens the ribbon's own menu; Exclude is ANDed, so the ten
    # rows carrying both tags go.
    _chip(pg, ben).click(button="right")
    pg.wait_for_selector(".menu", timeout=5_000)
    with pg.expect_response(_aggregate_with([sus, f"!{ben}"]), timeout=15_000):   # the grid's spelling
        pg.locator(".menu .menu-item", has_text="Exclude Benign").click()
    assert "excluded" in _chip(pg, ben).get_attribute("class")
    _total_is(pg, 10)

    # The drill-down is behind the same filter: its rows are the cell's.
    cell = pg.locator(".pluginview table tbody tr").first.locator("td").last
    n = int(cell.inner_text())
    with pg.expect_response(lambda x: "/api/plugin/pivot/detail" in x.url, timeout=15_000) as d:
        cell.click()
    assert d.value.request.post_data_json["tags"] == [sus, f"!{ben}"]
    pg.wait_for_selector("#modal:not([hidden]) table tbody tr", timeout=10_000)
    assert pg.locator("#modal table tbody tr").count() == n
    pg.locator("#modal").get_by_role("button", name="Close").click()

    # Clear the tag filter, from the same menu; then what is left to go through.
    _chip(pg, sus).click(button="right")
    with pg.expect_response(_aggregate_with([]), timeout=15_000):
        pg.locator(".menu .menu-item", has_text="Clear the tag filter").click()
    _total_is(pg, 200)
    with pg.expect_response(_aggregate_with(["__none__"]), timeout=15_000):
        _chip(pg, "__none__").click()
    _total_is(pg, 170)


def test_back_from_the_grid_a_tag_filtered_pivot_counts_the_rows_tagged_since(pv_page, tagged, server_post):
    """Tagging happens in the grid. A pivot filtered to Suspicious that kept
    its old cross-tab on the way back would show a count that is no longer
    true, under a chip saying it is."""
    pg = pv_page
    sus = tagged["ids"]["Suspicious"]
    # From the last test's No tags, picking a tag ADDS it (untagged rows plus
    # Suspicious's); taking No tags off again leaves Suspicious alone.
    with pg.expect_response(_aggregate_with(["__none__", sus]), timeout=15_000):
        _chip(pg, sus).click()
    with pg.expect_response(_aggregate_with([sus]), timeout=15_000):
        _chip(pg, "__none__").click()
    _total_is(pg, 20)

    more = [101, 102, 103, 104, 105]
    try:
        pg.evaluate("() => __winnow.showGridTab()")
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": more,
                                      "tag_id": sus, "on": True})
        with pg.expect_response(_aggregate_with([sus]), timeout=15_000):
            pg.locator(".tab-plugin", has_text="Pivot").click()
        _total_is(pg, 25)
        pg.wait_for_function(
            f"() => document.querySelector(\".pluginview [data-tag='{sus}'] .n\")?.textContent === '25'",
            timeout=10_000)
    finally:
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": more,
                                      "tag_id": sus, "on": False})


def test_a_tag_deleted_in_the_grid_leaves_the_filter_rather_than_emptying_the_pivot(
        pv_page, tagged, server, server_post, server_call):
    pg = pv_page
    new = server_post("/api/tags", {"name": "PivotScratch", "color": "#3a7bd5", "hotkey": None})
    try:
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": [1, 2, 3],
                                      "tag_id": new["id"], "on": True})
        pg.evaluate("() => __winnow.loadTags()")
        pg.evaluate("() => __winnow.showGridTab()")
        pg.locator(".tab-plugin", has_text="Pivot").click()
        _chip(pg, new["id"]).wait_for(timeout=10_000)
        # Only this tag: Clear first, so the count is the scratch tag's alone.
        _chip(pg, new["id"]).click(button="right")
        pg.locator(".menu .menu-item", has_text="Clear the tag filter").click()
        with pg.expect_response(_aggregate_with([new["id"]]), timeout=15_000):
            _chip(pg, new["id"]).click()
        _total_is(pg, 3)

        pg.evaluate("() => __winnow.showGridTab()")
        server_call(f"/api/tags/{new['id']}", method="DELETE")
        pg.evaluate("() => __winnow.loadTags()")
        # Re-run without it — not a 400, and not a pivot of nothing.
        with pg.expect_response(_aggregate_with([]), timeout=15_000) as r:
            pg.locator(".tab-plugin", has_text="Pivot").click()
        assert r.value.ok
        assert _chip(pg, new["id"]).count() == 0
        _total_is(pg, 200)
    finally:
        if any(t["id"] == new["id"] for t in _get(server, "/api/tags")["tags"]):
            server_call(f"/api/tags/{new['id']}", method="DELETE")
        pg.evaluate("() => __winnow.loadTags()")


def test_the_tag_filter_is_saved_in_the_grids_spelling_and_restored(pv_page, tagged):
    pg = pv_page
    ids = tagged["ids"]
    sus, ben = ids["Suspicious"], ids["Benign"]
    with pg.expect_response(_aggregate_with([sus]), timeout=15_000):
        _chip(pg, sus).click()   # on, from the empty filter the last test ended on
    with pg.expect_response(lambda r: "/api/plugin_state" in r.url and r.request.method == "POST",
                            timeout=15_000) as saved:
        _chip(pg, ben).click(button="right")
        pg.locator(".menu .menu-item", has_text="Exclude Benign").click()
    assert saved.value.json()["payload"]["pivots"][0]["tags"] == [sus, f"!{ben}"]

    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                         timeout=10_000)
    _open_tab(pg)
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)
    assert _chip(pg, sus).get_attribute("aria-pressed") == "true"
    assert "excluded" in _chip(pg, ben).get_attribute("class")
    _total_is(pg, 10)


def test_a_restore_drops_a_tag_the_case_no_longer_has_and_says_so(pv_page):
    pg = pv_page
    src = {"id": 1, "name": "ui.csv", "row_count": 200, "columns": [{"name": "Host", "type": "text"}]}
    payload = {"v": 1, "active": 0, "pivots": [{
        "name": "Pivot 1", "source": {"id": 1, "name": "ui.csv"}, "rows": ["Host"], "cols": [],
        "values": [], "filters": [], "tags": [2, "!7", "__none__", "!3", "junk"],
    }]}
    pg.evaluate(PIVOT_PLAN, [payload, [src], [{"id": 2, "name": "Suspicious"}, {"id": 3, "name": "Benign"}]])
    pg.wait_for_function("() => window.__plan !== undefined", timeout=10_000)
    plan = pg.evaluate("() => window.__plan")
    assert plan["pivots"][0]["tags"] == [2, "__none__", "!3"]
    assert any("tag filter" in n for n in plan["notes"])

    # Tags unknown (an older Winnow): kept as saved, for the backend to judge.
    pg.evaluate(PIVOT_PLAN, [payload, [src], None])
    pg.wait_for_function("() => window.__plan !== undefined", timeout=10_000)
    assert pg.evaluate("() => window.__plan")["pivots"][0]["tags"] == [2, "!7", "__none__", "!3"]

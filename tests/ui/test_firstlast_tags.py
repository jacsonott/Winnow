"""First/Last's tag chips: the grid ribbon's filter, in the Filters well.

It was a select (every row / tagged / untagged / these tags) and a checkbox
list, the last tag control in the app answering with a form. What the
chips SELECT is pinned in tests/test_plugins.py, against the grid's own
build_view. What is pinned here is what only a browser shows: that the
chips are there, that a click, a right-click Exclude and Clear send the
grid's spelling and re-run the preview, that coming back from the grid —
where rows get tagged and tags get deleted — re-runs a sheet filtered on
tags, and that the filter is saved and restored in that spelling.

ui.csv is the shared session table: rows 1-20 are tagged Suspicious and
11-30 Benign here, so 11-20 carry both, and every tag is taken off again in
a finally. Its Host is H0-H4 by turns, so grouped on Host every group gets
a fifth of whatever rows the filter keeps.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:first-last.firstlast"

DRAG = """(args) => {
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

# The rows the preview grouped: each group's size off its first row (an
# Only is its own first), summed. Five hosts, so every group is on screen.
ROWS_GROUPED = """() => {
  const rows = [...document.querySelectorAll('.pluginview tbody tr')];
  if (!rows.length) return null;
  let n = 0;
  for (const tr of rows) {
    const [which, count] = tr.lastElementChild.textContent.split(':');
    if (which !== 'Last') n += Number(count);
  }
  return n;
}"""


def _get(server, route):
    with urllib.request.urlopen(server.rstrip("/") + route, timeout=30) as r:
        return json.loads(r.read())


def _preview_with(tags):
    """The preview job started for exactly this tag filter. Waiting on any
    start would race: two quick edits inside the debounce are one request,
    two slower ones are two, and the first answer is the stale one."""
    def match(r):
        req = getattr(r, "request", r)   # a Response for expect_response, a Request for expect_request
        if "/api/plugin/first_last/job/start" not in req.url or req.method != "POST":
            return False
        body = req.post_data_json or {}
        return body.get("kind") == "preview" and (body.get("spec") or {}).get("tags") == tags
    return match


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
def fl_tags_page(first_last_page, server_post, tagged):
    # The sheets live in the case, shared with every other module: this one
    # starts from none and leaves none, as test_firstlast_state.py does.
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    try:
        with first_last_page(init="localStorage.removeItem('winnow.firstlast.auto')") as pg:
            yield pg
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})


def _saved_with(tags):
    """The save of the sheet once its tag filter reads `tags` — the saves
    before it, of the filter on its way there, are not the one asked about."""
    def match(r):
        if "/api/plugin_state" not in r.url or r.request.method != "POST":
            return False
        payload = (r.request.post_data_json or {}).get("payload") or {}
        return [sh.get("tags") for sh in payload.get("sheets", [])] == [tags]
    return match


def _rows_are(pg, n):
    """Waits for the preview to have grouped `n` rows — the answer lands a
    tick after its response does, so reading it once would race."""
    pg.wait_for_function(f"() => ({ROWS_GROUPED})() === {n}", timeout=15_000)


def _chip(pg, key):
    return pg.locator(f".pluginview [data-zone='filters'] [data-tag='{key}']")


def _open_tab(pg):
    pg.locator(".tab-plugin", has_text="First/Last").click()
    pg.wait_for_selector(".pluginview [data-zone='groupBy']", timeout=10_000)


def test_the_chips_filter_the_bookends_the_way_the_ribbon_does(fl_tags_page, tagged):
    pg = fl_tags_page
    ids = tagged["ids"]
    sus, ben = ids["Suspicious"], ids["Benign"]
    # Each row says which end it is and its group's size, for ROWS_GROUPED.
    pg.locator(".pluginview input[placeholder='{which} of {count}']").fill("{which}:{count}")
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    _rows_are(pg, 200)

    # Every tag the case has, plus Any tag and No tags, at the top of the
    # Filters well, with this table's count on each — and no select or
    # checkbox list left beside them.
    for t in ids.values():
        assert _chip(pg, t).count() == 1
    assert _chip(pg, "__any__").count() == 1 and _chip(pg, "__none__").count() == 1
    assert pg.locator(".pluginview [data-zone='filters'] .tag-ribbon .tag-chip").count() == len(ids) + 2
    assert pg.locator(".pluginview select[title='Keep only rows with (or without) tags']").count() == 0
    pg.wait_for_function(
        f"() => document.querySelector(\".pluginview [data-tag='{sus}'] .n\")?.textContent === '20'",
        timeout=10_000)

    with pg.expect_request(_preview_with([sus]), timeout=15_000):
        _chip(pg, sus).click()
    assert _chip(pg, sus).get_attribute("aria-pressed") == "true"
    _rows_are(pg, 20)

    # Right-click opens the ribbon's own menu; Exclude is ANDed, so the ten
    # rows carrying both tags go.
    _chip(pg, ben).click(button="right")
    pg.wait_for_selector(".menu", timeout=5_000)
    with pg.expect_request(_preview_with([sus, f"!{ben}"]), timeout=15_000):   # the grid's spelling
        pg.locator(".menu .menu-item", has_text="Exclude Benign").click()
    assert "excluded" in _chip(pg, ben).get_attribute("class")
    _rows_are(pg, 10)

    # A click on the excluded chip lets its rows back in, as on the ribbon.
    with pg.expect_request(_preview_with([sus]), timeout=15_000):
        _chip(pg, ben).click()
    assert "excluded" not in _chip(pg, ben).get_attribute("class")
    _rows_are(pg, 20)

    # Clear the tag filter, from the same menu; then what is left to go through.
    _chip(pg, sus).click(button="right")
    with pg.expect_request(_preview_with([]), timeout=15_000):
        pg.locator(".menu .menu-item", has_text="Clear the tag filter").click()
    _rows_are(pg, 200)
    with pg.expect_request(_preview_with(["__none__"]), timeout=15_000):
        _chip(pg, "__none__").click()
    _rows_are(pg, 170)


def test_back_from_the_grid_a_tag_filtered_sheet_groups_the_rows_tagged_since(fl_tags_page, tagged, server_post):
    """Tagging happens in the grid. A sheet filtered to Suspicious that kept
    its old bookends on the way back would show groups that are no longer
    true, under a chip saying they are."""
    pg = fl_tags_page
    sus = tagged["ids"]["Suspicious"]
    # From the last test's No tags, picking a tag ADDS it (untagged rows plus
    # Suspicious's); taking No tags off again leaves Suspicious alone.
    with pg.expect_request(_preview_with(["__none__", sus]), timeout=15_000):
        _chip(pg, sus).click()
    with pg.expect_request(_preview_with([sus]), timeout=15_000):
        _chip(pg, "__none__").click()
    _rows_are(pg, 20)

    more = [101, 102, 103, 104, 105]
    try:
        pg.evaluate("() => __winnow.showGridTab()")
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": more,
                                      "tag_id": sus, "on": True})
        with pg.expect_request(_preview_with([sus]), timeout=15_000):
            _open_tab(pg)
        _rows_are(pg, 25)
        pg.wait_for_function(
            f"() => document.querySelector(\".pluginview [data-tag='{sus}'] .n\")?.textContent === '25'",
            timeout=10_000)
    finally:
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": more,
                                      "tag_id": sus, "on": False})


def test_a_tag_deleted_in_the_grid_leaves_the_filter_rather_than_emptying_the_sheet(
        fl_tags_page, tagged, server, server_post, server_call):
    pg = fl_tags_page
    new = server_post("/api/tags", {"name": "FirstLastScratch", "color": "#3a7bd5", "hotkey": None})
    try:
        server_post("/api/row_tags", {"source_id": tagged["source_id"], "rids": [1, 2, 3],
                                      "tag_id": new["id"], "on": True})
        pg.evaluate("() => __winnow.loadTags()")
        pg.evaluate("() => __winnow.showGridTab()")
        _open_tab(pg)
        _chip(pg, new["id"]).wait_for(timeout=10_000)
        # Only this tag: Clear first, so the rows are the scratch tag's alone.
        _chip(pg, new["id"]).click(button="right")
        pg.locator(".menu .menu-item", has_text="Clear the tag filter").click()
        with pg.expect_request(_preview_with([new["id"]]), timeout=15_000):
            _chip(pg, new["id"]).click()
        _rows_are(pg, 3)

        pg.evaluate("() => __winnow.showGridTab()")
        server_call(f"/api/tags/{new['id']}", method="DELETE")
        pg.evaluate("() => __winnow.loadTags()")
        # Re-run without it — not a 400, and not a sheet of nothing.
        with pg.expect_response(_preview_with([]), timeout=15_000) as r:
            _open_tab(pg)
        assert r.value.ok
        assert _chip(pg, new["id"]).count() == 0
        _rows_are(pg, 200)
    finally:
        if any(t["id"] == new["id"] for t in _get(server, "/api/tags")["tags"]):
            server_call(f"/api/tags/{new['id']}", method="DELETE")
        pg.evaluate("() => __winnow.loadTags()")


def test_the_tag_filter_is_saved_in_the_grids_spelling_and_restored(fl_tags_page, tagged):
    pg = fl_tags_page
    ids = tagged["ids"]
    sus, ben = ids["Suspicious"], ids["Benign"]
    with pg.expect_request(_preview_with([sus]), timeout=15_000):
        _chip(pg, sus).click()   # on, from the empty filter the last test ended on
    with pg.expect_response(_saved_with([sus, f"!{ben}"]), timeout=15_000):
        _chip(pg, ben).click(button="right")
        pg.locator(".menu .menu-item", has_text="Exclude Benign").click()

    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                         timeout=10_000)
    _open_tab(pg)
    pg.wait_for_selector(".pluginview .fl-restored", timeout=10_000)
    _rows_are(pg, 10)
    assert _chip(pg, sus).get_attribute("aria-pressed") == "true"
    assert "excluded" in _chip(pg, ben).get_attribute("class")

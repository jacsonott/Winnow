"""A tag key tags every picked row, including the ones the grid hasn't
loaded.

The page cache only holds the pages the analyst has scrolled through, and
the original code tagged `positions.map(rowAt).filter(Boolean)`: select
all 1.2M rows, press a tag key, and a few hundred were tagged, with a
toast reporting the smaller number (docs/notes/grid.md). The shared table
is 200 rows, less than one 5,000-row page, so nothing in the suite could
pick a row that wasn't cached. This module imports a 12,000-row table:
three pages.

The table is removed at the end of the module, and each test takes its
tag off the table's rows when it finishes."""

from __future__ import annotations

import json
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.ui

ROWS = 12_000
NAME = "twelve_thousand.csv"


@pytest.fixture(scope="module")
def big(server, server_post, tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / NAME
    path.write_text("N,Host\n" + "".join(f"{i},h{i % 7}\n" for i in range(1, ROWS + 1)))
    server_post("/api/ingest/jobs/path", {"path": str(path), "build_fts": False})

    def sources():
        with urllib.request.urlopen(server.rstrip("/") + "/api/sources", timeout=10) as r:
            return json.loads(r.read())

    deadline = time.monotonic() + 30
    while not any(s["name"] == NAME and s["row_count"] == ROWS for s in sources()):
        assert time.monotonic() < deadline, f"{NAME} never finished importing"
        time.sleep(0.2)
    sid = next(s["id"] for s in sources() if s["name"] == NAME)
    yield sid
    req = urllib.request.Request(server.rstrip("/") + f"/api/source/{sid}", method="DELETE",
                                 headers={"X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=10).read()


@pytest.fixture
def tag(page, api, big):
    """The case's first tag with a hotkey. Taken back off every row of the
    big table afterwards, and the table the page started on reopened."""
    home = page.evaluate("() => __winnow.S.sourceId")
    t = page.evaluate("() => __winnow.S.tags.find((t) => t.hotkey)")
    assert t, "the fixture case has no tag with a hotkey"
    yield t
    api("/api/row_tags", "POST", {"source_id": big, "rids": list(range(1, ROWS + 1)),
                                  "tag_id": t["id"], "on": False})
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", home)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=home)


def _open(page, sid):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function(
        "([id, n]) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === n"
        " && __winnow.busyCount === 0", arg=[sid, ROWS])
    page.wait_for_selector('.row[data-pos="0"]:not(.pending)')


def _tagged(api, sid, tag_id):
    """[how many, lowest rid, highest rid] of the table's rows carrying the tag."""
    res = api("/api/sql", "POST", {"sql": "SELECT COUNT(*), MIN(rid), MAX(rid) FROM row_tags"
                                          f" WHERE source_id = {sid} AND tag_id = {tag_id}"})
    return res["rows"][0]


def _is_tagged(api, sid, tag_id, rid):
    res = api("/api/sql", "POST", {"sql": "SELECT COUNT(*) FROM row_tags"
                                          f" WHERE source_id = {sid} AND tag_id = {tag_id} AND rid = {rid}"})
    return res["rows"][0][0] == 1


def _tag_write(resp):
    """Either of the two ways a tag key writes: picked rows by id, or a
    whole view less its exclusions."""
    return resp.request.method == "POST" and urlparse(resp.url).path in ("/api/row_tags", "/api/row_tags/view")


def _confirm(page):
    page.wait_for_selector(".confirm-overlay")
    said = page.locator(".confirm-card .confirm-message").inner_text()
    page.locator(".confirm-card .confirm-actions .btn:not(.ghost)").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    return said


def test_select_all_then_a_tag_key_tags_every_row_but_the_one_unticked(page, api, big, tag):
    _open(page, big)
    page.locator('.row[data-pos="0"] .cell').first.click()
    page.keyboard.press("Control+a")
    page.wait_for_function("(n) => __winnow.S.selectAll && __winnow.selCount() === n", arg=ROWS)
    # The last of the three pages has never been fetched.
    assert page.evaluate("() => __winnow.S.pages.has(2)") is False
    unticked = page.evaluate("() => __winnow.rowAt(3).rid")
    page.locator('.row[data-pos="3"] .rowcheck').click()
    page.wait_for_function("(n) => __winnow.selCount() === n", arg=ROWS - 1)

    with page.expect_response(_tag_write) as written:
        page.keyboard.press(tag["hotkey"])
        assert "11,999" in _confirm(page)
    assert written.value.ok

    count, lo, hi = _tagged(api, big, tag["id"])
    assert (count, lo, hi) == (ROWS - 1, 1, ROWS)
    assert not _is_tagged(api, big, tag["id"], unticked)


def test_picks_running_into_a_page_still_loading_are_all_tagged(page, api, big, tag):
    # Hold back the second page, so the run of picks crosses into rows the
    # grid is still waiting for when the key is pressed.
    held = []

    def hold_second_page(route):
        if parse_qs(urlparse(route.request.url).query).get("start") == ["5000"]:
            held.append(route)
        else:
            route.continue_()

    page.route(lambda url: urlparse(url).path == "/api/rows", hold_second_page)
    try:
        _open(page, big)
        page.evaluate("() => { document.getElementById('body').scrollTop = 4985 * __winnow.ROW_H; }")
        page.wait_for_selector('.row[data-pos="5010"]')
        page.wait_for_function("() => __winnow.S.pending.has(1)")
        assert "pending" in page.locator('.row[data-pos="5000"]').get_attribute("class")

        page.locator('.row[data-pos="4990"] .rowcheck').click()
        page.locator('.row[data-pos="5010"] .rowcheck').click(modifiers=["Shift"])
        page.wait_for_function("() => __winnow.selCount() === 21")

        with page.expect_response(_tag_write) as written:
            # Pressed while eleven of the 21 rows are still on their way.
            page.keyboard.press(tag["hotkey"])
            assert held, "the second page was never asked for"
            for route in held:
                route.continue_()
        assert written.value.ok
    finally:
        page.unroute_all(behavior="ignoreErrors")

    # Rids 4,991 to 5,011: positions 4,990 to 5,010 of an unsorted table.
    assert _tagged(api, big, tag["id"]) == [21, 4991, 5011]

"""The Pivot's timeframe: ⏱ Timeframe in the Filters well (examples/plugins/pivot).

What the range SELECTS is pinned in tests/test_pivot_timeframe.py, against
the grid's own build_view, and the hand-over to the grid in
tests/ui/test_pivot_to_grid.py. What is pinned here is what only a browser
shows: the dialog sends the range in the shape it is compared in (a US
date typed in comes back as ISO), text that is not a time is refused with
the dialog still open, the chip says what is being counted and stays,
dimmed, when the range is off, the drill-down carries the range, and the
range is saved with the pivot and comes back with it.

On ui.csv row i is Timestamp 2026-03-14 08:mm:ss counting seconds from
08:00:00 and Host H{i%5}, so the minute from 08:01:00 is rows 60-119:
sixty rows, twelve per host.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"
SECOND_MINUTE = {"column": None, "start": "2026-03-14 08:01:00", "end": "2026-03-14 08:01:59"}

GRAND_TOTAL = """() => {
  const rows = document.querySelectorAll('.pluginview table tbody tr');
  const cell = rows.length ? rows[rows.length - 1].lastElementChild : null;
  return cell ? cell.textContent.replace(/,/g, '') : null;
}"""

# planRestore is pure; imported under its own query string so the mounted
# tab's module instance is untouched (test_pivot_tags.py does the same).
PIVOT_PLAN = """(args) => {
  window.__plan = undefined;
  import('/plugin_assets/pivot/ui/tab.js?tfrestoretest=1')
    .then((m) => { window.__plan = m.planRestore(args[0], args[1], args[2]); });
}"""


def _get(server, route):
    with urllib.request.urlopen(server.rstrip("/") + route, timeout=30) as r:
        return json.loads(r.read())


def _aggregate_with(frame):
    """The aggregate request sent with exactly this timeframe — waiting on
    any aggregate would race the one the last edit sent."""
    return lambda r: ("/api/plugin/pivot/aggregate" in r.url and r.request.method == "POST"
                      and (r.request.post_data_json or {}).get("timeframe") == frame)


def _total_is(pg, n):
    pg.wait_for_function(f"() => ({GRAND_TOTAL})() === '{n}'", timeout=15_000)


@pytest.fixture
def pivot(page, server, server_post):
    """The pivot plugin on and a function that seeds a saved pivot — Rows
    Host, Count — over ui.csv, with `timeframe`, and opens its tab."""
    sid = next(s["id"] for s in _get(server, "/api/sources") if s["name"] == "ui.csv")
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})

    def _open(timeframe=None):
        server_post("/api/plugin_state", {"key": KEY, "payload": {"v": 1, "active": 0, "pivots": [{
            "name": "Pivot 1", "source": {"id": sid, "name": "ui.csv"},
            "rows": ["Host"], "cols": [], "values": [{"agg": "count"}], "filters": [], "tags": [],
            "timeframe": timeframe, "subtotals": True, "grandTotals": True,
        }]}})
        page.wait_for_selector("#grid .row")
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                               timeout=10_000)
        page.locator(".tab-plugin", has_text="Pivot").click()
        page.wait_for_selector(".pluginview table tbody tr", timeout=15_000)

    try:
        yield _open
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


def test_the_dialog_narrows_the_pivot_and_the_chip_says_to_what(page, pivot):
    pivot()
    _total_is(page, 200)
    btn = page.locator(".pluginview [data-zone='filters'] .pv-tf-btn")
    assert btn.get_attribute("aria-pressed") == "false"
    assert page.locator(".pv-tf-chip").count() == 0

    btn.click()
    page.wait_for_selector("#modal:not([hidden]) .pv-tf-start")
    options = page.eval_on_selector_all(".pv-tf-column option", "(os) => os.map((o) => o.value)")
    assert options == ["", "Timestamp"]    # every datetime column, or the one this table has
    page.fill(".pv-tf-start", "2026-03-14 08:01")
    page.fill(".pv-tf-end", "3/14/2026 8:01:59 AM")
    # Typed in two other spellings; sent, kept and shown as the one the
    # range is compared in.
    with page.expect_response(_aggregate_with(SECOND_MINUTE), timeout=15_000):
        page.click(".pv-tf-apply")
    _total_is(page, 60)
    assert page.locator("#modal").is_hidden()

    chip = page.locator(".pv-tf-chip")
    text = chip.inner_text()
    assert "2026-03-14 08:01:00" in text and "2026-03-14 08:01:59" in text
    assert "on any datetime column" in text
    assert chip.get_attribute("data-on") == "true"
    assert btn.get_attribute("aria-pressed") == "true"

    # The rows behind a cell are the cell's: the range goes with them.
    cell = page.locator(".pluginview table tbody tr").first.locator("td").last
    assert cell.inner_text() == "12"
    with page.expect_response(lambda r: "/api/plugin/pivot/detail" in r.url, timeout=15_000) as d:
        cell.click()
    assert d.value.request.post_data_json["timeframe"] == SECOND_MINUTE
    page.wait_for_selector("#modal:not([hidden]) table tbody tr", timeout=10_000)
    assert page.locator("#modal table tbody tr").count() == 12


def test_text_that_is_not_a_time_is_refused_with_the_dialog_still_open(page, pivot):
    """Refused where it was typed. Closing the dialog first and failing
    under the table would lose what was typed and leave the analyst
    guessing which box was wrong."""
    pivot()
    _total_is(page, 200)
    page.click(".pluginview .pv-tf-btn")
    page.wait_for_selector("#modal:not([hidden]) .pv-tf-start")
    page.fill(".pv-tf-start", "yesterday")
    with page.expect_response(lambda r: "/api/plugin/pivot/timeframe" in r.url, timeout=15_000) as r:
        page.click(".pv-tf-apply")
    assert r.value.status == 400
    page.wait_for_function("() => /Start: 'yesterday' is not a recognized timestamp/"
                           ".test(document.querySelector('#toast').textContent)")
    # Apply's only way on is through the answer it was just refused, so the
    # dialog being open is the pivot being left alone.
    assert page.locator("#modal").is_visible()
    assert page.input_value(".pv-tf-start") == "yesterday"
    assert page.locator(".pv-tf-chip").count() == 0


def test_off_keeps_the_range_and_clear_forgets_it(page, pivot):
    """The tables' timeframe can be off without being gone (its toggle
    key); the pivot's is too, from the chip's menu — and says so, rather
    than keep a chip that reads as if it narrowed a whole-table count."""
    pivot({"enabled": True, **SECOND_MINUTE})
    _total_is(page, 60)
    chip = page.locator(".pv-tf-chip")

    chip.click(button="right")
    page.wait_for_selector(".menu", timeout=5_000)
    with page.expect_response(_aggregate_with(None), timeout=15_000):
        page.locator(".menu .menu-item", has_text="Turn it off").click()
    _total_is(page, 200)
    assert chip.get_attribute("data-on") == "false"
    assert "Off — every row is counted" in chip.inner_text()
    assert page.locator(".pluginview .pv-tf-btn").get_attribute("aria-pressed") == "false"

    # The dialog opened on it says so, and Apply turns it back on.
    chip.click()
    page.wait_for_selector("#modal:not([hidden]) .tf-off")
    assert page.input_value(".pv-tf-start") == SECOND_MINUTE["start"]
    with page.expect_response(_aggregate_with(SECOND_MINUTE), timeout=15_000):
        page.click(".pv-tf-apply")
    _total_is(page, 60)
    assert chip.get_attribute("data-on") == "true"

    # ✕ forgets it.
    with page.expect_response(_aggregate_with(None), timeout=15_000):
        chip.locator(".tl-range-x").click()
    _total_is(page, 200)
    assert page.locator(".pv-tf-chip").count() == 0


def test_the_timeframe_is_saved_with_the_pivot_and_comes_back(page, pivot):
    pivot()
    _total_is(page, 200)
    page.click(".pluginview .pv-tf-btn")
    page.wait_for_selector("#modal:not([hidden]) .pv-tf-start")
    page.select_option(".pv-tf-column", "Timestamp")
    page.fill(".pv-tf-start", SECOND_MINUTE["start"])
    page.fill(".pv-tf-end", SECOND_MINUTE["end"])
    with page.expect_response(lambda r: "/api/plugin_state" in r.url and r.request.method == "POST",
                              timeout=15_000) as saved:
        page.click(".pv-tf-apply")
    assert saved.value.json()["payload"]["pivots"][0]["timeframe"] == {
        "enabled": True, **SECOND_MINUTE, "column": "Timestamp"}

    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row", timeout=30_000)
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                           timeout=10_000)
    with page.expect_response(_aggregate_with({**SECOND_MINUTE, "column": "Timestamp"}), timeout=15_000):
        page.locator(".tab-plugin", has_text="Pivot").click()
    _total_is(page, 60)
    assert "on Timestamp" in page.locator(".pv-tf-chip").inner_text()


def test_a_restore_reads_every_datetime_column_when_its_own_is_gone_and_says_so(page, pivot):
    pivot()
    frame = {"enabled": False, "column": "Created", "start": "2026-03-14 08:01:00", "end": ""}
    payload = {"v": 1, "active": 0, "pivots": [{
        "name": "Pivot 1", "source": {"id": 1, "name": "ui.csv"}, "rows": ["Host"], "cols": [],
        "values": [], "filters": [], "tags": [], "timeframe": frame,
    }]}
    timed = {"id": 1, "name": "ui.csv", "row_count": 200,
             "columns": [{"name": "Host", "type": "text"}, {"name": "Timestamp", "type": "datetime"}]}
    page.evaluate(PIVOT_PLAN, [payload, [timed], []])
    page.wait_for_function("() => window.__plan !== undefined", timeout=10_000)
    plan = page.evaluate("() => window.__plan")
    # Kept, and kept off; the column it named falls back to every one.
    assert plan["pivots"][0]["timeframe"] == {**frame, "column": None}
    assert any("reads every datetime column now" in n for n in plan["notes"])

    # A table with no time left in it has nothing for the range to read.
    untimed = {**timed, "columns": [{"name": "Host", "type": "text"}]}
    page.evaluate(PIVOT_PLAN, [payload, [untimed], []])
    page.wait_for_function("() => window.__plan !== undefined", timeout=10_000)
    plan = page.evaluate("() => window.__plan")
    assert plan["pivots"][0]["timeframe"] == {"enabled": False, "column": None, "start": "", "end": ""}
    assert any("dropped its timeframe" in n for n in plan["notes"])

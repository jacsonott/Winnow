"""Per-tab view state: switching tables used to reset every filter, so
coming back to a tab lost what the analyst was looking at. The stash in
sources.js restores filters/search/sort/grouping/scroll per source."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def second(page, api, tmp_path):
    """A second table of this test's own, dropped again at the end. It
    used to be whichever source came second in the list, and was never
    removed: the test depended on what earlier modules had left in the
    shared case, and left its own table for later ones (#227)."""
    first = page.evaluate("() => __winnow.S.sourceId")
    csv2 = tmp_path / "view_state_second.csv"
    csv2.write_text("Alpha,Beta\n" + "".join(f"{i},x{i}\n" for i in range(30)), encoding="utf-8")
    second = api("/api/ingest/path", "POST", {"path": str(csv2)})["id"]
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function("(id) => __winnow.S.sources.some((s) => s.id === id)", arg=second)
    try:
        yield second
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.evaluate("(id) => __winnow.openSource(id)", first)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=first)
        api(f"/api/source/{second}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")


def _filter_and_scroll(page):
    """Filter the first table down and scroll a bit."""
    page.locator('.fcell input[data-col="EventId"]').fill("=4624")
    page.locator('.fcell input[data-col="EventId"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")  # 200 rows, 4 EventId values
    # The spacer grows when the first page lands; a scrollTop set before
    # that is clamped to 0 and stashed as 0. Keep setting it until it
    # takes (lost under a loaded machine, where the page fetch is slower).
    page.wait_for_function(
        "() => { const b = document.getElementById('body'); b.scrollTop = 240; return b.scrollTop === 240; }")


def test_filters_survive_switching_tables(page, second):
    first = page.evaluate("() => __winnow.S.sourceId")
    _filter_and_scroll(page)

    # Away and back.
    page.evaluate("(id) => __winnow.openSource(id)", second)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=second)
    assert page.evaluate("() => __winnow.S.filters") == {}, "the other table must start clean"
    page.evaluate("(id) => __winnow.openSource(id)", first)
    # S.view is reassigned when the restored view is BUILT, which is after
    # sourceId flips; waiting on sourceId alone read the other table's view.
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === 50", arg=first)

    assert page.evaluate("() => __winnow.S.filters") == {"EventId": "=4624"}
    assert page.evaluate("() => __winnow.S.view.row_count") == 50
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == "=4624"
    page.wait_for_function("() => document.getElementById('body').scrollTop === 240")

    # Clearing on the tab sticks across another round trip.
    page.click("#btnReset")
    page.wait_for_function("() => __winnow.S.view.row_count === 200")
    page.evaluate("(id) => __winnow.openSource(id)", second)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=second)
    page.evaluate("(id) => __winnow.openSource(id)", first)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === 200", arg=first)
    assert page.evaluate("() => __winnow.S.filters") == {}


def test_enter_in_a_filter_box_builds_the_view_once(page):
    """Typing armed a 220 ms debounce that Enter did not call off, so the
    same view was built twice: once for Enter, once more when the debounce
    fired. That second build is what made the test above flaky in CI: it
    could fire while the analyst was coming back to the table, and land
    over the view the switch had just put back (see the test below)."""
    builds = []
    page.on("request", lambda r: builds.append(r) if r.method == "POST" and r.url.endswith("/api/view") else None)
    page.locator('.fcell input[data-col="EventId"]').fill("=4624")
    page.locator('.fcell input[data-col="EventId"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")
    page.wait_for_timeout(600)   # well past the debounce: a second build is what must NOT happen
    assert len(builds) == 1


def test_a_build_started_mid_switch_does_not_land_over_the_restored_view(page, second, until):
    """openSource clears the filters and sort, waits on the table's
    layout, and only then puts the stash back. A build that started in
    between built the whole table, and when the return took the cached
    view nothing superseded it: it landed afterwards, and the grid showed
    all 200 rows under a filter box that said =4624, scrolled back to the
    top. A header box's debounce did that in CI. The test starts the
    build itself at that point, since a typing debounce no longer survives
    Enter (the test above)."""
    first = page.evaluate("() => __winnow.S.sourceId")
    _filter_and_scroll(page)
    page.evaluate("(id) => __winnow.openSource(id)", second)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view.row_count === 30", arg=second)

    layout, builds = [], []
    page.route(re.compile(rf".*/api/layout\?source_id={first}$"), lambda route: layout.append(route))
    page.route(re.compile(r".*/api/view$"), lambda route: builds.append(route))
    # Not awaited: openSource is waiting on the layout held here.
    page.evaluate("(id) => { __winnow.openSource(id); }", first)
    until(page, lambda: layout, "the first table's layout held")
    assert page.evaluate("() => __winnow.S.filters") == {}, "mid-switch, the filters are cleared"
    page.evaluate("() => { __winnow.rebuildView(); }")
    until(page, lambda: builds, "the mid-switch build held")
    assert builds[0].request.post_data_json["filters"] == []

    layout[0].continue_()
    page.wait_for_function("() => __winnow.S.view.row_count === 50"
                           " && document.getElementById('body').scrollTop === 240")
    # Let the mid-switch build go on to the server, as it did unheld.
    # Called off, the browser has given up on it already, and a Playwright
    # that refuses to continue a request nobody is waiting for is fine.
    try:
        builds[0].continue_()
    except Exception:
        pass
    page.wait_for_function("() => !__winnow.rebuildInFlight()")

    assert page.evaluate("() => __winnow.S.view.row_count") == 50
    assert page.evaluate("() => __winnow.S.filters") == {"EventId": "=4624"}
    assert page.evaluate("() => document.getElementById('body').scrollTop") == 240

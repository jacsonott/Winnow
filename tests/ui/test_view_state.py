"""Per-tab view state: switching tables used to reset every filter, so
coming back to a tab lost what the analyst was looking at. The stash in
sources.js restores filters/search/sort/grouping/scroll per source."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_filters_survive_switching_tables(page, api, tmp_path):
    # The second table is this test's own, dropped again at the end. It
    # used to be whichever source came second in the list, and was never
    # removed: the test depended on what earlier modules had left in the
    # shared case, and left its own table for later ones (#227).
    first = page.evaluate("() => __winnow.S.sourceId")
    csv2 = tmp_path / "view_state_second.csv"
    csv2.write_text("Alpha,Beta\n" + "".join(f"{i},x{i}\n" for i in range(30)), encoding="utf-8")
    second = api("/api/ingest/path", "POST", {"path": str(csv2)})["id"]
    try:
        _round_trip(page, first, second)
    finally:
        page.evaluate("(id) => __winnow.openSource(id)", first)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=first)
        api(f"/api/source/{second}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")


def _round_trip(page, first, second):
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function("(id) => __winnow.S.sources.some((s) => s.id === id)", arg=second)

    # Filter the first table down and scroll a bit.
    page.locator('.fcell input[data-col="EventId"]').fill("=4624")
    page.locator('.fcell input[data-col="EventId"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")  # 200 rows, 4 EventId values
    # The spacer grows when the first page lands; a scrollTop set before
    # that is clamped to 0 and stashed as 0. Keep setting it until it
    # takes (lost under a loaded machine, where the page fetch is slower).
    page.wait_for_function(
        "() => { const b = document.getElementById('body'); b.scrollTop = 240; return b.scrollTop === 240; }")

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

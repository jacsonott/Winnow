"""Per-tab view state: switching tables used to reset every filter, so
coming back to a tab lost what the analyst was looking at. The stash in
sources.js restores filters/search/sort/grouping/scroll per source."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_filters_survive_switching_tables(page, tmp_path):
    csv2 = tmp_path / "second.csv"
    csv2.write_text("Alpha,Beta\n" + "".join(f"{i},x{i}\n" for i in range(30)), encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function("() => __winnow.S.sources.length >= 2")
    first, second = page.evaluate("() => __winnow.S.sources.map((s) => s.id).slice(0, 2)")

    # Filter the first table down and scroll a bit.
    page.locator('.fcell input[data-col="EventId"]').fill("=4624")
    page.locator('.fcell input[data-col="EventId"]').press("Enter")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")  # 200 rows, 4 EventId values
    page.evaluate("() => { document.getElementById('body').scrollTop = 240; }")

    # Away and back.
    page.evaluate("(id) => __winnow.openSource(id)", second)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=second)
    assert page.evaluate("() => __winnow.S.filters") == {}, "the other table must start clean"
    page.evaluate("(id) => __winnow.openSource(id)", first)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=first)

    assert page.evaluate("() => __winnow.S.filters") == {"EventId": "=4624"}
    assert page.evaluate("() => __winnow.S.view.row_count") == 50
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == "=4624"
    assert page.evaluate("() => document.getElementById('body').scrollTop") == 240

    # Clearing on the tab sticks across another round trip.
    page.click("#btnReset")
    page.wait_for_function("() => __winnow.S.view.row_count === 200")
    page.evaluate("(id) => __winnow.openSource(id)", second)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=second)
    page.evaluate("(id) => __winnow.openSource(id)", first)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === 200", arg=first)
    assert page.evaluate("() => __winnow.S.filters") == {}

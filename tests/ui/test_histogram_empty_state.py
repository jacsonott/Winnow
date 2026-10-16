"""The histogram strip with nothing to chart: one line of text, not a
96px canvas with a clipped sentence painted on it plus the drag hint."""

from __future__ import annotations

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui


def test_no_rows_collapses_the_panel_to_a_sentence(page):
    try:
        btn = page.locator("#btnHistogram")
        btn.wait_for(state="visible", timeout=10_000)
        if btn.get_attribute("aria-pressed") != "true":
            btn.click()
        page.wait_for_selector("#pluginPanels:not([hidden]) #histogramPanel canvas.th-canvas", timeout=10_000)
        full = page.locator("#histogramPanel").bounding_box()["height"]

        # A filter nothing matches: the view is empty, the canvas goes away.
        page.locator('.fcell input[data-col="Host"]').fill("=nothing-matches-this")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 0")
        page.wait_for_selector("#histogramPanel .th-empty", state="visible", timeout=10_000)
        assert page.locator("#histogramPanel .th-empty").inner_text() == "No rows with a parsable timestamp in this view."
        assert page.locator("#histogramPanel canvas.th-canvas").is_hidden()
        # ...but the way back from a timeframe that emptied the view stays.
        assert page.locator("#histogramPanel .btn", has_text="Clear timeframe").is_visible()
        assert "Drag across" not in page.locator("#histogramPanel").inner_text()
        small = page.locator("#histogramPanel").bounding_box()["height"]
        assert small < full / 2, (small, full)
        # The sentence is DOM text, so it wraps rather than clipping.
        assert page.evaluate(
            "() => { const n = document.querySelector('#histogramPanel .th-empty'); return n.scrollWidth <= n.clientWidth; }")

        # Data back → chart back.
        page.locator('.fcell input[data-col="Host"]').fill("")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        page.wait_for_selector("#histogramPanel canvas.th-canvas", state="visible", timeout=10_000)
        assert page.locator("#histogramPanel .th-empty").is_hidden()
    finally:
        page.locator('.fcell input[data-col="Host"]').fill("")
        page.keyboard.press("Enter")
        page.evaluate("() => { __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")


@pytest.fixture
def own_server(tmp_path):
    """Its own case: the test below closes the only tab, which the shared
    case's other modules would open to find gone."""
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("When,Host\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i}\n" for i in range(30)),
                        encoding="utf-8")
    with running_server(tmp_path, "--case", tmp_path / "case.db", "--open", csv_path, "--no-fts",
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        cwd=tmp_path, ready="/api/sources") as srv:
        yield srv


def test_closing_the_last_tab_takes_its_chart_with_it(own_page, own_server):
    """No table is open, so there is no view, and no view change to redraw
    on: the strip went on charting the table whose tab had just closed."""
    with own_page(base=own_server.base, viewport=(1400, 900)) as page:
        page.evaluate("() => __winnow.toggleHistogram(true)")
        page.wait_for_function(
            "() => document.querySelector('#histogramPanel .th-info').textContent.startsWith('30 rows')",
            timeout=15_000)
        page.locator("#sourceTabs .tab .x").click()
        page.wait_for_function("() => __winnow.S.sourceId === null")
        page.wait_for_selector("#histogramPanel .th-empty", state="visible", timeout=10_000)
        assert page.locator("#histogramPanel .th-empty").inner_text() == "Open a table to chart when its rows happened."
        assert page.locator("#histogramPanel .th-info").inner_text() == ""
        assert page.locator("#histogramPanel canvas.th-canvas").is_hidden()

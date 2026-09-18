"""The histogram strip with nothing to chart: one line of text, not a
96px canvas with a clipped sentence painted on it plus the drag hint."""

from __future__ import annotations

import pytest

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

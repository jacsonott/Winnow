"""The histogram panel with nothing to chart: one line of text, not a
96px canvas with a clipped sentence painted on it plus the drag hint."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_no_rows_collapses_the_panel_to_a_sentence(page, server):
    _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "on_all"})
    try:
        page.evaluate("() => __winnow.loadPlugins()")
        btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Histogram")
        btn.wait_for(state="visible", timeout=10_000)
        if btn.get_attribute("aria-pressed") != "true":
            btn.click()
        page.wait_for_selector("#pluginPanels:not([hidden]) .plugin-panel canvas.th-canvas", timeout=10_000)
        full = page.locator(".plugin-panel").bounding_box()["height"]

        # A filter nothing matches: the view is empty, the canvas goes away.
        page.locator('.fcell input[data-col="Host"]').fill("=nothing-matches-this")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 0")
        page.wait_for_selector(".plugin-panel .th-empty", state="visible", timeout=10_000)
        assert page.locator(".plugin-panel .th-empty").inner_text() == "No rows with a parsable timestamp in this view."
        assert page.locator(".plugin-panel canvas.th-canvas").is_hidden()
        assert page.locator(".plugin-panel .btn", has_text="Clear timeframe").is_hidden()
        assert "Drag across" not in page.locator(".plugin-panel").inner_text()
        small = page.locator(".plugin-panel").bounding_box()["height"]
        assert small < full / 2, (small, full)
        # The sentence is DOM text, so it wraps rather than clipping.
        assert page.evaluate(
            "() => { const n = document.querySelector('.plugin-panel .th-empty'); return n.scrollWidth <= n.clientWidth; }")

        # Data back → chart back.
        page.locator('.fcell input[data-col="Host"]').fill("")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        page.wait_for_selector(".plugin-panel canvas.th-canvas", state="visible", timeout=10_000)
        assert page.locator(".plugin-panel .th-empty").is_hidden()
    finally:
        page.locator('.fcell input[data-col="Host"]').fill("")
        page.keyboard.press("Enter")
        _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")

"""The table_histogram plugin end to end — and with it the toolbar-panel
hook: enabling the plugin puts a toggle beside search, toggling drops the
panel between the toolbar and the grid, the histogram follows a filter,
a drag on it writes the timeframe filter (narrowing the view), Clear
removes it, and the panel hides with the toolbar on page tabs."""

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


def _info(page):
    return page.locator(".plugin-panel span", has_text="rows").first.inner_text()


def test_histogram_panel_follows_filters_and_sets_the_timeframe(page, server):
    _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "on_all"})
    try:
        page.evaluate("() => __winnow.loadPlugins()")
        btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Histogram")
        btn.wait_for(state="visible", timeout=10_000)
        # Beside search, in the toolbar's right cluster.
        assert page.locator("#toolbar #pluginToolbarButtons").count() == 1
        assert btn.get_attribute("aria-pressed") == "false"

        btn.click()
        page.wait_for_selector("#pluginPanels:not([hidden]) .plugin-panel canvas.th-canvas", timeout=10_000)
        assert btn.get_attribute("aria-pressed") == "true"
        # Sits between the toolbar and the grid.
        tb = page.locator("#toolbar").bounding_box()
        pp = page.locator("#pluginPanels").bounding_box()
        gb = page.locator("#body").bounding_box()
        assert tb["y"] + tb["height"] <= pp["y"] + 1 and pp["y"] + pp["height"] <= gb["y"] + 1
        page.wait_for_function("() => /200 rows/.test(document.querySelector('.plugin-panel').textContent)", timeout=10_000)

        # A quick filter narrows the grid → the histogram follows.
        page.locator('.fcell input[data-col="Host"]').fill("=H1")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        page.wait_for_function("() => /40 rows/.test(document.querySelector('.plugin-panel').textContent)", timeout=10_000)

        # Drag across the bars → the timeframe filter is set and the view narrows.
        box = page.locator(".plugin-panel canvas.th-canvas").bounding_box()
        y = box["y"] + box["height"] * 0.4
        page.mouse.move(box["x"] + box["width"] * 0.10, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.45, y, steps=6)
        page.mouse.up()
        page.wait_for_function("() => __winnow.S.timeRange.enabled === true && __winnow.S.timeRange.column === 'Timestamp'")
        tr = page.evaluate("() => __winnow.S.timeRange")
        assert tr["start"].startswith("2026-03-14 08:") and tr["end"].startswith("2026-03-14 08:")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 40 && __winnow.S.view.row_count > 0")
        assert page.locator("#btnTimeRange").get_attribute("aria-pressed") == "true"

        # Clear from the panel → filter off, view back to the 40.
        page.locator(".plugin-panel button", has_text="Clear timeframe").click()
        page.wait_for_function("() => __winnow.S.timeRange.enabled === false")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")

        # The strip hides with the toolbar on a page tab, comes back on the grid.
        page.locator("#tabSql").click()
        page.wait_for_selector("#pluginPanels", state="hidden")
        page.locator("#sourceTabs .tab").first.click()
        page.wait_for_selector("#pluginPanels:not([hidden])")

        # Toggle off → gone; the preference stuck while it was on.
        btn.click()
        page.wait_for_selector("#pluginPanels", state="hidden")
        assert btn.get_attribute("aria-pressed") == "false"
    finally:
        page.evaluate("() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' }; }")
        page.locator("#btnReset").click()
        page.evaluate("() => localStorage.removeItem('winnow.panels')")
        _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")

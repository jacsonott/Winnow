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
        # In the toolbar's right cluster, immediately LEFT of the ⏱ timeframe button.
        assert page.locator("#toolbar #pluginToolbarButtons").count() == 1
        bb, tb = btn.bounding_box(), page.locator("#btnTimeRange").bounding_box()
        assert bb["x"] + bb["width"] <= tb["x"] + 1
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


def _bar_colour(page):
    """The colour of a bar on the panel canvas, sampled at the baseline of
    the tallest bar."""
    return page.evaluate("""() => {
      const c = document.querySelector('.plugin-panel canvas.th-canvas');
      const ctx = c.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      // scan the row just above the baseline for the first painted pixel
      const y = Math.round((c.height / dpr - 17) * dpr);
      const row = ctx.getImageData(0, y, c.width, 1).data;
      for (let x = 0; x < c.width; x++) {
        const i = x * 4;
        if (row[i + 3] > 200) return [row[i], row[i + 1], row[i + 2]];
      }
      return null;
    }""")


def test_bars_follow_the_accent_colour_live(page, server):
    _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "on_all"})
    try:
        page.evaluate("() => __winnow.loadPlugins()")
        page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Histogram").click()
        page.wait_for_selector("#pluginPanels:not([hidden]) canvas.th-canvas", timeout=10_000)
        page.wait_for_function("() => /200 rows/.test(document.querySelector('.plugin-panel').textContent)", timeout=10_000)
        before = _bar_colour(page)
        assert before is not None
        # A custom accent, applied while the panel is open — no view change.
        page.evaluate("() => __winnow.applyAccent('#3aa0ff', true)")
        page.wait_for_function("""(b) => {
          const c = document.querySelector('.plugin-panel canvas.th-canvas');
          const ctx = c.getContext('2d'); const dpr = window.devicePixelRatio || 1;
          const y = Math.round((c.height / dpr - 17) * dpr);
          const row = ctx.getImageData(0, y, c.width, 1).data;
          for (let x = 0; x < c.width; x++) { const i = x * 4; if (row[i+3] > 200) return row[i] !== b[0] || row[i+2] !== b[2]; }
          return false; }""", arg=before, timeout=5_000)
        after = _bar_colour(page)
        assert after[2] > after[0]          # bluish now, not the skin's gold
    finally:
        page.evaluate("() => { __winnow.S.appearance.accentCustomized = false; __winnow.applyAccent('#d9a441', false); }")
        page.evaluate("() => localStorage.removeItem('winnow.panels')")
        _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")

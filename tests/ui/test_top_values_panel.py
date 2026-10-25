"""The top_values example in the browser — and with it the toolbar-panel
host now that the histogram is built in: enabling the plugin puts a toggle
in #pluginToolbarButtons, toggling mounts the panel in the strip between
the toolbar and the grid, the list follows a filter, a click copies the
value, and the strip shows for the plugin panel alone, for the built-in
histogram alone, survives a plugin reload, and hides with the toolbar on
page tabs."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

PANEL = "top-values.top_values"
SECTION = f'section[data-panel-id="{PANEL}"]'      # the plugin's mount…
IN_STRIP = f"#pluginPanels {SECTION}"               # …scoped to the grid host, never the built-in
HISTOGRAM = "#pluginPanels:not([hidden]) #histogramPanel canvas.th-canvas"


@pytest.fixture
def top_values(page, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "top_values", "scope": "on_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_function("() => (__winnow.S.pluginPanels || []).some((p) => p.id === 'top-values.top_values')")
    yield
    page.evaluate("() => { __winnow.togglePluginPanel('top-values.top_values', false); __winnow.toggleHistogram(false); "
                  "localStorage.removeItem('winnow.panels'); localStorage.removeItem('winnow.histogram'); }")
    page.locator('.fcell input[data-col="Host"]').fill("")
    page.keyboard.press("Enter")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
    server_post("/api/plugins/toggle", {"fs_name": "top_values", "scope": "off_all"})
    page.evaluate("() => __winnow.loadPlugins()")


def _values(page):
    return page.evaluate(
        f"() => [...document.querySelectorAll('{IN_STRIP} .tv-value')].map((b) => [b.dataset.value, b.textContent.trim()])")


def test_panel_lists_the_top_values_of_the_view_and_copies_one(page, top_values):
    btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Top values")
    btn.wait_for(state="visible", timeout=10_000)
    # Left of the built-in histogram toggle, which is left of the timeframe.
    bb, hb = btn.bounding_box(), page.locator("#btnHistogram").bounding_box()
    assert bb["x"] + bb["width"] <= hb["x"] + 1
    btn.click()
    page.wait_for_selector(f"#pluginPanels:not([hidden]) {SECTION} .tv-value", timeout=10_000)
    section = page.locator(IN_STRIP)
    section.locator("select").select_option("EventId")
    page.wait_for_function(
        f"() => [...document.querySelectorAll('{IN_STRIP} .tv-value')].some((b) => b.dataset.value === '4624')",
        timeout=10_000)
    got = dict(_values(page))
    assert set(got) == {"4624", "4625", "4688", "1"} and all(t.endswith(" 50") for t in got.values()), got
    assert "200 rows" in section.inner_text()

    # A quick filter narrows the grid → the counts follow the view.
    page.locator('.fcell input[data-col="Host"]').fill("=H1")
    page.keyboard.press("Enter")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
    page.wait_for_function(
        f"() => {{ const b = [...document.querySelectorAll('{IN_STRIP} .tv-value')];"
        f" return b.length === 4 && b.every((x) => / 10$/.test(x.textContent.trim())); }}", timeout=10_000)
    assert "40 rows" in section.inner_text()

    # Click a value → it is on the clipboard.
    section.locator(".tv-value", has_text="4688").click()
    assert page.evaluate("() => navigator.clipboard.readText()") == "4688"


def test_the_strip_shows_for_either_panel_and_hides_on_page_tabs(page, top_values):
    def strip_hidden():
        return page.evaluate("() => document.getElementById('pluginPanels').hidden")
    plugin_btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Top values")
    plugin_btn.wait_for(state="visible", timeout=10_000)
    assert strip_hidden() is True

    # The built-in alone.
    page.locator("#btnHistogram").click()
    page.wait_for_selector(HISTOGRAM, timeout=10_000)
    assert page.locator(IN_STRIP).count() == 0          # no plugin mount yet
    # Both: the plugin panel joins the strip below the histogram.
    plugin_btn.click()
    page.wait_for_selector(f"#pluginPanels:not([hidden]) {SECTION} .tv-value", timeout=10_000)
    hb, sb = page.locator("#histogramPanel").bounding_box(), page.locator(IN_STRIP).bounding_box()
    assert hb["y"] + hb["height"] <= sb["y"] + 1
    # The plugin alone: toggling the built-in off must not take the strip
    # with it — and the other way round.
    page.locator("#btnHistogram").click()
    page.wait_for_selector("#histogramPanel", state="hidden")
    assert strip_hidden() is False and page.locator(IN_STRIP).is_visible()
    page.locator("#btnHistogram").click()
    page.wait_for_selector(HISTOGRAM)
    plugin_btn.click()
    page.wait_for_selector(IN_STRIP, state="hidden")
    assert strip_hidden() is False and page.locator("#histogramPanel canvas.th-canvas").is_visible()

    # A plugin reload (boot and every case switch do one) redraws the
    # plugin toggles and leaves both panels where they were.
    plugin_btn.click()
    page.wait_for_selector(f"#pluginPanels:not([hidden]) {SECTION}", state="visible")
    page.evaluate("() => __winnow.loadPlugins()")
    assert page.locator("#btnHistogram").get_attribute("aria-pressed") == "true"
    assert page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Top values").get_attribute("aria-pressed") == "true"
    assert strip_hidden() is False

    # Page tabs hide the strip with the toolbar; the grid brings it back.
    page.locator("#tabSql").click()
    page.wait_for_selector("#pluginPanels", state="hidden")
    assert page.evaluate("() => document.getElementById('histogramPanel').hidden") is True
    page.locator("#sourceTabs .tab").first.click()
    page.wait_for_selector("#pluginPanels:not([hidden])")
    assert page.locator(IN_STRIP).is_visible() and page.locator("#histogramPanel canvas.th-canvas").is_visible()

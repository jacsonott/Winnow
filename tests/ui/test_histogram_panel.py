"""The built-in histogram strip end to end: its toggle sits in the toolbar
after the plugin toggles and left of the timeframe button, toggling drops
the strip between the toolbar and the grid, the histogram follows a
filter, a drag on it writes the timeframe filter (narrowing the view),
Clear removes it, the strip hides with the toolbar on page tabs, a plugin
reload leaves the toggle where it is, the bars follow the accent, `h`
toggles it, the first chart is asked for at the canvas width, a view
rebuilt behind a page tab is fetched once on the way back, the button
tooltips follow a rebinding, and the retired plugin's per-browser toggle
carries over."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.ui

CANVAS = "#pluginPanels:not([hidden]) #histogramPanel canvas.th-canvas"


def test_histogram_panel_follows_filters_and_sets_the_timeframe(page):
    btn = page.locator("#btnHistogram")
    try:
        btn.wait_for(state="visible", timeout=10_000)
        # In the toolbar's right cluster: AFTER the span the plugin toggles
        # are rendered into (a plugin reload wipes that span), immediately
        # LEFT of the ⏱ timeframe button.
        assert page.locator("#toolbar #btnHistogram").count() == 1
        assert page.evaluate("() => document.getElementById('pluginToolbarButtons').nextElementSibling.id") == "btnHistogram"
        bb, tb = btn.bounding_box(), page.locator("#btnTimeRange").bounding_box()
        assert bb["x"] + bb["width"] <= tb["x"] + 1
        assert btn.get_attribute("aria-pressed") == "false"
        assert page.evaluate("() => document.getElementById('pluginPanels').hidden") is True

        btn.click()
        page.wait_for_selector(CANVAS, timeout=10_000)
        assert btn.get_attribute("aria-pressed") == "true"
        # Sits between the toolbar and the grid.
        tb = page.locator("#toolbar").bounding_box()
        pp = page.locator("#pluginPanels").bounding_box()
        gb = page.locator("#body").bounding_box()
        assert tb["y"] + tb["height"] <= pp["y"] + 1 and pp["y"] + pp["height"] <= gb["y"] + 1
        page.wait_for_function("() => /200 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)

        # A quick filter narrows the grid → the histogram follows.
        page.locator('.fcell input[data-col="Host"]').fill("=H1")
        page.keyboard.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")
        page.wait_for_function("() => /40 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)

        # Drag across the bars → the timeframe filter is set and the view narrows.
        box = page.locator("#histogramPanel canvas.th-canvas").bounding_box()
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

        # Clear from the strip → filter off, view back to the 40.
        page.locator("#histogramPanel button", has_text="Clear timeframe").click()
        page.wait_for_function("() => __winnow.S.timeRange.enabled === false")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 40")

        # The strip hides with the toolbar on a page tab, comes back on the grid.
        page.locator("#tabSql").click()
        page.wait_for_selector("#pluginPanels", state="hidden")
        page.locator("#sourceTabs .tab").first.click()
        page.wait_for_selector(CANVAS)

        # A plugin reload (boot and every case switch do one) redraws the
        # plugin toggles; the built-in toggle and its state survive it.
        page.evaluate("() => __winnow.loadPlugins()")
        assert page.locator("#toolbar #btnHistogram").count() == 1
        assert btn.get_attribute("aria-pressed") == "true"
        assert page.locator("#histogramPanel canvas.th-canvas").is_visible()

        # Toggle off → gone; the preference stuck while it was on.
        assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.histogram')).open") is True
        btn.click()
        page.wait_for_selector("#pluginPanels", state="hidden")
        assert btn.get_attribute("aria-pressed") == "false"
    finally:
        page.evaluate("() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' }; }")
        page.locator("#btnReset").click()
        page.evaluate("() => localStorage.removeItem('winnow.histogram')")


def _bar_colour(page):
    """The colour of a bar on the strip's canvas, sampled at the baseline of
    the tallest bar."""
    return page.evaluate("""() => {
      const c = document.querySelector('#histogramPanel canvas.th-canvas');
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


def test_bars_follow_the_accent_colour_live(page):
    try:
        page.locator("#btnHistogram").click()
        page.wait_for_selector(CANVAS, timeout=10_000)
        page.wait_for_function("() => /200 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)
        before = _bar_colour(page)
        assert before is not None
        # A custom accent, applied while the strip is open — no view change.
        page.evaluate("() => __winnow.applyAccent('#3aa0ff', true)")
        page.wait_for_function("""(b) => {
          const c = document.querySelector('#histogramPanel canvas.th-canvas');
          const ctx = c.getContext('2d'); const dpr = window.devicePixelRatio || 1;
          const y = Math.round((c.height / dpr - 17) * dpr);
          const row = ctx.getImageData(0, y, c.width, 1).data;
          for (let x = 0; x < c.width; x++) { const i = x * 4; if (row[i+3] > 200) return row[i] !== b[0] || row[i+2] !== b[2]; }
          return false; }""", arg=before, timeout=5_000)
        after = _bar_colour(page)
        assert after[2] > after[0]          # bluish now, not the skin's gold
    finally:
        page.evaluate("() => { __winnow.S.appearance.accentCustomized = false; __winnow.applyAccent('#d9a441', false); }")
        page.evaluate("() => { __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")


def test_h_toggles_the_strip_from_the_grid(page):
    try:
        page.locator("#body").focus()
        page.keyboard.press("h")
        page.wait_for_selector(CANVAS, timeout=10_000)
        assert page.locator("#btnHistogram").get_attribute("aria-pressed") == "true"
        page.keyboard.press("h")
        page.wait_for_selector("#pluginPanels", state="hidden")
        assert page.locator("#btnHistogram").get_attribute("aria-pressed") == "false"
        # Not from a page tab: the strip belongs to the grid's toolbar.
        page.locator("#tabSql").click()
        page.wait_for_selector("#sqlview:not([hidden])")
        page.locator("#body").focus()
        page.keyboard.press("h")
        page.wait_for_timeout(200)
        assert page.locator("#btnHistogram").get_attribute("aria-pressed") == "false"
    finally:
        page.evaluate("() => { __winnow.showGridTab(); __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")


def _histogram_asks(page):
    """Every /api/histogram request the page makes from here on."""
    asks = []
    page.on("request", lambda r: asks.append(r.url) if "/api/histogram" in r.url else None)
    return asks


def test_the_first_chart_is_asked_for_at_the_canvas_width(page):
    """Opening the strip measured the canvas for its bucket count while
    "Loading…" had it hidden, so the first ask fell through to a 600px
    fallback (85 bars): on a wide window the first chart was coarser than
    the strip could show until the next view change asked again."""
    asks = _histogram_asks(page)
    try:
        page.locator("#btnHistogram").click()
        page.wait_for_selector(CANVAS, timeout=10_000)
        page.wait_for_function("() => /200 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)
        width = page.evaluate("() => document.querySelector('#histogramPanel canvas.th-canvas').clientWidth")
        assert width > 700, width          # wide enough that 85 bars is visibly the wrong answer
        got = [int(parse_qs(urlparse(a).query)["max_buckets"][0]) for a in asks]
        assert got, "the strip never asked for a histogram"
        assert got[0] == max(20, min(400, width // 7)), (got, width)
    finally:
        page.evaluate("() => { __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")


def test_a_view_rebuilt_behind_a_page_tab_is_fetched_once_on_return(page):
    """Open but hidden behind a page tab, the strip used to fetch on every
    view change — for a canvas nobody could see — and again on the way
    back to the grid: two aggregate passes for one chart."""
    asks = _histogram_asks(page)
    try:
        page.locator("#btnHistogram").click()
        page.wait_for_selector(CANVAS, timeout=10_000)
        page.wait_for_function("() => /200 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)
        page.locator("#tabSql").click()
        page.wait_for_selector("#pluginPanels", state="hidden")
        before = len(asks)
        page.evaluate("() => { window.__viewChanges = 0; document.addEventListener('winnow:viewchange', () => { window.__viewChanges++; }); }")
        page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
        page.wait_for_function("() => window.__viewChanges > 0", timeout=10_000)
        page.wait_for_timeout(600)         # well past the strip's 150 ms debounce
        assert len(asks) == before, "the hidden strip fetched: " + " | ".join(asks[before:])
        # Back on the grid, the show edge fetches — once.
        page.evaluate("() => __winnow.showGridTab()")
        page.wait_for_selector(CANVAS)
        deadline = time.monotonic() + 10
        while len(asks) < before + 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        page.wait_for_timeout(600)
        assert len(asks) == before + 1, asks[before:]
    finally:
        page.evaluate("() => { __winnow.showGridTab(); __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")


def test_the_button_tooltips_follow_a_rebinding(page):
    """The Histogram and ⏱ Timeframe tooltips spell their keys from
    S.keymap when the buttons sync; Settings saved a rebinding and redrew
    its own list only, so both named the old key until the next tab
    switch."""
    hist, tr = page.locator("#btnHistogram"), page.locator("#btnTimeRange")
    assert '"h" to show/hide' in hist.get_attribute("title")
    assert '"r" to toggle' in tr.get_attribute("title")
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Keyboard shortcuts')")
    hrow = page.locator(".settings-key-row", has_text="histogram")
    trow = page.locator(".settings-key-row", has_text="Toggle the timeframe filter")
    assert hrow.count() == 1 and trow.count() == 1
    hrow.locator(".settings-key-chip .btn").first.click()       # ✕ on "h"
    assert "to show/hide" not in hist.get_attribute("title")
    trow.locator(".settings-key-chip .btn").first.click()       # ✕ on "r"
    assert '"r" to toggle' not in tr.get_attribute("title")
    hrow.locator(".btn", has_text="+ key").click()
    page.keyboard.press("y")
    page.wait_for_function(
        "() => /\"y\" to show\\/hide/.test(document.getElementById('btnHistogram').title)", timeout=5_000)
    page.click("#modalBody .btn:has-text('Reset to defaults')")
    assert '"h" to show/hide' in hist.get_attribute("title")
    assert '"r" to toggle' in tr.get_attribute("title")


def test_the_retired_plugins_toggle_carries_over(browser, server, first_run_init):
    """An analyst who kept the table_histogram plugin's panel open (one key
    in winnow.panels, the plugin host's map) gets the built-in strip open on
    first load — once: the key goes, the rest of the map stays."""
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script(
        first_run_init + ";"
        "if (!localStorage.getItem('winnow.histogram')) localStorage.setItem('winnow.panels',"
        " JSON.stringify({ 'table-histogram.histogram': true, 'other.panel': true }));")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        pg.wait_for_selector(CANVAS, timeout=10_000)
        assert pg.locator("#btnHistogram").get_attribute("aria-pressed") == "true"
        pg.wait_for_function("() => /200 rows/.test(document.getElementById('histogramPanel').textContent)", timeout=10_000)
        prefs = pg.evaluate("() => [JSON.parse(localStorage.getItem('winnow.histogram')), JSON.parse(localStorage.getItem('winnow.panels'))]")
        assert prefs[0]["open"] is True
        assert prefs[1] == {"other.panel": True}
    finally:
        ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)

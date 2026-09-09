"""Selecting a smaller timeframe on the histogram, and seeing it.

Two complaints, one cause and one consequence.

The selection rectangle was painted with `--sel` at full opacity over the
plot, so it covered the bars you were choosing between — you picked a
range by hiding the thing you were picking it from.

And the drag was rounded outwards to the CURRENT bar width, so a range
narrower than one bar could not be expressed: over a table spanning days
the bars are hours wide, any drag inside one became that whole bar, and
the view never narrowed enough for the server to re-bucket. "Zoom in" did
nothing. The server has always chosen its bucket from the span it is
given (Store.HISTOGRAM_BUCKETS); it was never being given a smaller one.
"""

from __future__ import annotations

import datetime
import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


@pytest.fixture
def wide_table(page, server, tmp_path):
    """Three days of events, so the bars are hours wide — the shared
    fixture spans three minutes, where every bucket is already seconds and
    the rounding never bit."""
    base = datetime.datetime(2026, 3, 14, 0, 0, 0)
    lines = ["When,Host"]
    for i in range(720):                       # every 6 minutes for 3 days
        lines.append(f"{(base + datetime.timedelta(minutes=6 * i)):%Y-%m-%d %H:%M:%S},H{i % 3}")
    f = tmp_path / "wide.csv"
    f.write_text("\n".join(lines) + "\n")
    _post(server, "/api/ingest/jobs/path", {"path": str(f), "name": "wide.csv", "kind": "csv"})
    page.wait_for_function(
        """() => __winnow.loadSources().then(() =>
             __winnow.S.sources.some((s) => s.name === 'wide.csv'))""",
        timeout=25_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wide.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)

    _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "on_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    btn = page.locator("#pluginToolbarButtons .plugin-panel-btn", has_text="Histogram")
    btn.wait_for(state="visible", timeout=10_000)
    if btn.get_attribute("aria-pressed") == "false":
        btn.click()
    page.wait_for_selector("#pluginPanels:not([hidden]) canvas.th-canvas", timeout=10_000)
    page.wait_for_function("() => /rows/.test(document.querySelector('.plugin-panel').textContent)",
                           timeout=10_000)
    yield sid
    page.evaluate("() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' }; }")
    page.evaluate("""async (id) => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      await fetch('/api/source/' + id, { method: 'DELETE', headers: h });
      __winnow.S.viewCache.delete(id);
      await __winnow.loadSources();
      const first = __winnow.S.sources.find((s) => !s.is_merge);
      if (first) __winnow.openSource(first.id);
    }""", sid)
    page.evaluate("() => localStorage.removeItem('winnow.panels')")
    _post(server, "/api/plugins/toggle", {"fs_name": "table_histogram", "scope": "off_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_selector(".row")


def _bucket_seconds(page):
    """Read the bucket width off the panel's own caption ("… 1h buckets …")."""
    txt = page.locator(".plugin-panel").inner_text()
    import re
    m = re.search(r"·\s*(\d+)([smhd])\s*buckets", txt)
    assert m, txt
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _span_seconds(page):
    tr = page.evaluate("() => __winnow.S.timeRange")
    fmt = "%Y-%m-%d %H:%M:%S"
    a = datetime.datetime.strptime(tr["start"], fmt)
    b = datetime.datetime.strptime(tr["end"], fmt)
    return (b - a).total_seconds()


def _bucket_count_hint(page):
    """How many bars the panel asked for, derived the way the panel does."""
    w = page.locator("canvas.th-canvas").bounding_box()["width"]
    return max(20, min(400, int(w // 7)))


def _drag(page, frm, to):
    box = page.locator("canvas.th-canvas").bounding_box()
    y = box["y"] + box["height"] * 0.4
    page.mouse.move(box["x"] + box["width"] * frm, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * to, y, steps=8)
    page.mouse.up()


def test_the_bars_get_finer_for_the_smaller_range(page, wide_table):
    """Narrowing the timeframe is meant to be more detail, not the same
    chart over fewer rows. The server has always chosen its bucket from the
    span it is given; what it was not given was a bucket count the canvas
    could actually show, so a zoomed view came back just as dense."""
    before = _bucket_seconds(page)
    assert before >= 300, f"expected coarse bars over three days, got {before}s"
    _drag(page, 0.40, 0.46)
    page.wait_for_function("() => __winnow.S.timeRange.enabled === true")
    # "timeframe on" appears as soon as the panel redraws, which happens
    # before its refetch lands — wait for the row count the new data brings.
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 720")
    page.wait_for_function(
        """() => { const m = /(\\d[\\d,]*) rows/.exec(document.querySelector('.plugin-panel').textContent);
             return m && Number(m[1].replace(/,/g, '')) === __winnow.S.view.row_count; }""",
        timeout=15_000)
    after = _bucket_seconds(page)
    assert after < before, (before, after)


def test_the_panel_asks_for_as_many_bars_as_it_can_show(page, wide_table):
    """A fixed 160 buckets made 5px slivers on a wide canvas and stayed 160
    at every zoom level. The ask comes from the canvas now, so it changes
    when the canvas does."""
    asks = []
    page.on("request", lambda r: asks.append(r.post_data)
            if r.url.endswith("/histogram") and r.post_data else None)

    wide = page.locator("canvas.th-canvas").bounding_box()["width"]
    page.evaluate("() => __winnow.S.appearance && void 0")   # no-op; keep the panel mounted
    page.set_viewport_size({"width": 900, "height": 900})
    page.wait_for_function("() => document.querySelector('canvas.th-canvas').clientWidth < %d" % int(wide))
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false })")
    page.wait_for_function("(n) => window.__asks === undefined || true", arg=1)
    page.wait_for_timeout(800)

    narrow = page.locator("canvas.th-canvas").bounding_box()["width"]
    got = [json.loads(a).get("max_buckets") for a in asks if a]
    assert got, "the panel never asked for a histogram"
    assert all(isinstance(n, int) and 20 <= n <= 400 for n in got), got
    # The last ask was made for the narrower canvas, and fits it.
    assert got[-1] <= narrow / 6 + 1, (got[-1], narrow)
    assert got[-1] < wide / 6, (got[-1], wide)


def test_the_selection_does_not_hide_the_bars_under_it(page, wide_table):
    """It used to be painted opaque over the plot."""
    box = page.locator("canvas.th-canvas").bounding_box()
    y = box["y"] + box["height"] * 0.4
    page.mouse.move(box["x"] + box["width"] * 0.20, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.80, y, steps=8)
    try:
        # Inside the brush, the plot must still hold more than one colour:
        # bars visible through the tint rather than a flat wash.
        distinct = page.evaluate("""() => {
          const c = document.querySelector('canvas.th-canvas');
          const ctx = c.getContext('2d');
          const dpr = window.devicePixelRatio || 1;
          const y = Math.round((c.height / dpr - 25) * dpr);
          const x0 = Math.round(c.width * 0.3), x1 = Math.round(c.width * 0.7);
          const row = ctx.getImageData(x0, y, x1 - x0, 1).data;
          const seen = new Set();
          for (let i = 0; i < row.length; i += 4) seen.add(`${row[i]},${row[i+1]},${row[i+2]}`);
          return seen.size;
        }""")
        assert distinct > 1, "the selection painted a flat wash over the bars"
    finally:
        page.mouse.up()
        page.evaluate("() => { __winnow.S.timeRange = { enabled: false, column: null, start: '', end: '' }; }")

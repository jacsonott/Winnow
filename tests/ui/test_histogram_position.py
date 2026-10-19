"""The histogram strip says where the grid is.

Asked for as "the histogram should show the current position in the
table": the strip charted the whole view and nothing on it moved as the
analyst scrolled, so there was no telling which part of the chart the
rows on screen were. It now marks them — a band, with a thumb along the
top of the plot, while they are one stretch of time; a tick per row when
the table is sorted by something else and they are scattered — and a
line for the row the cursor is on.

Asserted against the rows the grid actually has in the document and the
pixels the canvas actually holds, not against the code that computes the
marks, so a refactor of either side that drifts fails here.
"""

from __future__ import annotations

import datetime
import time

import pytest

pytestmark = pytest.mark.ui

BASE = datetime.datetime(2026, 3, 14, 0, 0, 0)
STEP = datetime.timedelta(minutes=6)
ROWS = 720                                   # every 6 minutes for 3 days


def _epoch(text):
    d = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return int(d.replace(tzinfo=datetime.timezone.utc).timestamp())


def _put_back(page, sid):
    """Delete the table and close the strip — from teardown, and from a
    setup that failed before its yield (which runs no teardown)."""
    page.evaluate("""async (id) => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      if (id != null) {
        await fetch('/api/source/' + id, { method: 'DELETE', headers: h });
        __winnow.S.viewCache.delete(id);
      }
      await __winnow.loadSources();
      const first = __winnow.S.sources.find((s) => !s.is_merge);
      if (first) __winnow.openSource(first.id);
    }""", sid)
    page.evaluate("() => { __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")
    page.wait_for_selector(".row")


@pytest.fixture
def timed_table(page, tmp_path, server_post):
    """Three days in time order, plus a Shuffle column whose order has
    nothing to do with time — sorting by it scatters the rows on screen
    across the whole chart."""
    lines = ["When,Host,Shuffle"]
    for i in range(ROWS):
        # 7919 is prime to 720, so this visits every value once, out of order.
        lines.append(f"{(BASE + STEP * i):%Y-%m-%d %H:%M:%S},H{i % 3},{(i * 7919) % ROWS:04d}")
    f = tmp_path / "position.csv"
    f.write_text("\n".join(lines) + "\n")
    sid = None
    try:
        server_post("/api/ingest/jobs/path", {"path": str(f), "name": "position.csv", "kind": "csv"})
        # Polled from Python: wait_for_function does not await a promise
        # predicate (tests/test_ui_test_hygiene.py).
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            names = page.evaluate(
                "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
            if "position.csv" in names:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("position.csv never appeared in S.sources")
        sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'position.csv').id")
        page.evaluate("(id) => __winnow.openSource(id)", sid)
        page.wait_for_function(
            "(id) => __winnow.S.sourceId === id && __winnow.S.view"
            f" && __winnow.S.view.row_count === {ROWS} && __winnow.busyCount === 0", arg=sid)
        btn = page.locator("#btnHistogram")
        btn.wait_for(state="visible", timeout=30_000)
        if btn.get_attribute("aria-pressed") == "false":
            btn.click()
        page.wait_for_selector("#pluginPanels:not([hidden]) #histogramPanel canvas.th-canvas", timeout=30_000)
        page.wait_for_function(f"() => /{ROWS} rows/.test(document.getElementById('histogramPanel').textContent)",
                               timeout=30_000)
        page.wait_for_function("() => { const p = __winnow.histogramPosition(); return !!(p && p.band); }")
    except Exception:
        _put_back(page, sid)
        raise
    yield sid
    _put_back(page, sid)


def _rows_in_view(page):
    """The When of every row the grid shows below its header, read off the
    DOM: what the analyst can see, worked out without the grid's own
    geometry."""
    return page.evaluate("""() => {
      const body = document.getElementById('body');
      const b = body.getBoundingClientRect();
      const top = document.getElementById('gridHead').getBoundingClientRect().bottom;
      const bottom = b.top + body.clientHeight;
      const col = __winnow.visibleCols().indexOf('When');
      const out = [];
      for (const row of document.querySelectorAll('#rows .row')) {
        const r = row.getBoundingClientRect();
        if (r.bottom <= top + 1 || r.top >= bottom - 1) continue;
        const cell = row.querySelector(`.cell[data-col="${col}"]`);
        if (cell && cell.textContent) out.push(cell.textContent);
      }
      return out;
    }""")


def _lane_run(page):
    """The painted columns of the strip above the plot, as [first, last]
    in CSS px, or None when it is empty."""
    return page.evaluate("""() => {
      const c = document.querySelector('#histogramPanel canvas.th-canvas');
      const dpr = window.devicePixelRatio || 1;
      const row = c.getContext('2d').getImageData(0, Math.round(2 * dpr), c.width, 1).data;
      let a = -1, z = -1;
      for (let x = 0; x < c.width; x++) if (row[x * 4 + 3] > 200) { if (a < 0) a = x; z = x; }
      return a < 0 ? null : [a / dpr, z / dpr];
    }""")


def _x_of(page, epoch):
    """Where a time lands on the canvas, from the strip's own answer."""
    return page.evaluate("""(t) => {
      const d = __winnow.histogramData();
      const t0 = d.buckets[0][0];
      const t1 = Math.max(d.buckets[d.buckets.length - 1][0] + d.bucket_seconds, t0 + d.bucket_seconds);
      return (t - t0) / (t1 - t0) * document.querySelector('canvas.th-canvas').clientWidth;
    }""", epoch)


def _assert_band_covers_the_screen(page):
    shown = [_epoch(t) for t in _rows_in_view(page)]
    assert len(shown) > 3, shown
    band = page.evaluate("() => __winnow.histogramPosition().band")
    assert band, "no band for rows in time order"
    # Every row on screen is inside the band, and the band reaches at most
    # one row past them at either end: a row the bottom edge cuts to a
    # sliver is on screen to the grid and may not count as such here.
    step = int(STEP.total_seconds())
    assert min(shown) - step <= band["start"] <= min(shown), (band, min(shown))
    assert max(shown) <= band["end"] <= max(shown) + step, (band, max(shown))
    # And it is painted where those times are on the chart.
    run = _lane_run(page)
    assert run, "nothing painted along the top of the plot"
    mid = (_x_of(page, band["start"]) + _x_of(page, band["end"])) / 2
    assert abs((run[0] + run[1] + 1) / 2 - mid) <= 3, (run, mid)
    return band


def test_the_band_marks_the_rows_on_screen_and_follows_a_scroll(page, timed_table):
    first = _assert_band_covers_the_screen(page)
    asks = []
    page.on("request", lambda r: asks.append(r.url) if "/api/histogram" in r.url else None)
    page.evaluate("() => { document.getElementById('body').scrollTop = 400 * __winnow.ROW_H; }")
    page.wait_for_function("(s) => __winnow.histogramPosition().band.start !== s", arg=first["start"])
    later = _assert_band_covers_the_screen(page)
    assert later["start"] > first["end"], (first, later)
    # A scroll repaints the canvas; it does not ask the server again.
    assert not asks, asks


def test_a_line_marks_the_selected_row(page, timed_table):
    page.locator("#rows .row").nth(4).locator(".cell").first.click()
    when = page.evaluate("() => __winnow.rowAt(__winnow.S.cursor).cells["
                         "__winnow.S.columns.findIndex((c) => c.name === 'When')]")
    page.wait_for_function("(t) => __winnow.histogramPosition().cursor === t", arg=_epoch(when))
    page.locator("#body").press("ArrowDown")
    page.wait_for_function("(t) => __winnow.histogramPosition().cursor === t", arg=_epoch(when) + 360)
    # The line is drawn in the text colour, through the plot and over the
    # bars: the pixel at its x, halfway down, is that colour.
    hit = page.evaluate("""(x) => {
      const c = document.querySelector('#histogramPanel canvas.th-canvas');
      const dpr = window.devicePixelRatio || 1;
      const px = c.getContext('2d').getImageData(Math.round(Math.round(x) * dpr), Math.round(c.height / 2), 1, 1).data;
      const ref = document.createElement('canvas').getContext('2d');
      ref.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text').trim();
      ref.fillRect(0, 0, 1, 1);
      const want = ref.getImageData(0, 0, 1, 1).data;
      return [Array.from(px), Array.from(want)];
    }""", _x_of(page, _epoch(when) + 360))
    got, want = hit
    assert got[3] == 255 and all(abs(a - b) <= 2 for a, b in zip(got[:3], want[:3])), hit


def test_rows_out_of_time_order_get_a_tick_each(page, timed_table, view_rebuilt):
    """Sorted by a column unrelated to time, the rows on screen are spread
    over the whole three days. A band from the earliest to the latest
    would cover most of the chart and say nothing; each row is marked on
    its own instead."""
    with view_rebuilt(page):
        page.locator(".hcell", has_text="Shuffle").first.click()
    page.wait_for_function("() => { const p = __winnow.histogramPosition(); return !!p && !p.band && p.ticks.length > 3; }")
    shown = sorted(_epoch(t) for t in _rows_in_view(page))
    ticks = page.evaluate("() => __winnow.histogramPosition().ticks")
    assert set(ticks) <= set(shown), (ticks, shown)
    # Several separate marks along the top, not one run.
    gaps = page.evaluate("""() => {
      const c = document.querySelector('#histogramPanel canvas.th-canvas');
      const dpr = window.devicePixelRatio || 1;
      const row = c.getContext('2d').getImageData(0, Math.round(2 * dpr), c.width, 1).data;
      let runs = 0, on = false;
      for (let x = 0; x < c.width; x++) { const lit = row[x * 4 + 3] > 200; if (lit && !on) runs++; on = lit; }
      return runs;
    }""")
    assert gaps > 3, gaps
    # Back in time order, it is a band again.
    with view_rebuilt(page):
        page.locator(".hcell", has_text="When").first.click()
    page.wait_for_function("() => { const p = __winnow.histogramPosition(); return !!p && !!p.band; }")
    _assert_band_covers_the_screen(page)

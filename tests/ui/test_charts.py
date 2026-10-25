"""The shared canvas chart module (charts.js) — the visualization surface
stack / entity-pivot / dashboards draw through. Renders to a scratch
canvas and asserts pixels land, since a chart that silently draws nothing
is exactly the failure a backend test can't see."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _lit(page, expr):
    """Count non-transparent pixels a draw call produced on a fresh canvas."""
    return page.evaluate(
        """(call) => {
          const c = document.createElement('canvas');
          c.style.width = '400px'; c.style.height = '200px';
          document.body.appendChild(c);
          Object.defineProperty(c, 'clientWidth', { value: 400 });
          Object.defineProperty(c, 'clientHeight', { value: 200 });
          const charts = { drawBars: __winnow.drawBars, drawHistogram: __winnow.drawHistogram, pickBar: __winnow.pickBar };
          new Function('charts', 'c', call)(charts, c);
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let lit = 0;
          for (let i = 3; i < d.length; i += 4) if (d[i] > 40) lit++;
          c.remove();
          return lit;
        }""", expr)


def test_draw_bars_renders(page):
    lit = _lit(page, """charts.drawBars(c, {
      rows: [{ v: 'alpha', n: 5 }, { v: 'beta', n: 12 }, { v: 'gamma', n: 3 }],
      label: 'v', value: 'n' });""")
    assert lit > 500, lit


def test_draw_histogram_renders(page):
    lit = _lit(page, """charts.drawHistogram(c, {
      buckets: [['08', [3, 1]], ['09', [7, 0]], ['10', [2, 5]]],
      colors: ['#d9a441', '#e0836a'] });""")
    assert lit > 300, lit


def test_pick_bar_maps_click_to_row(page):
    row = page.evaluate("""() => {
      const c = document.createElement('canvas');
      Object.defineProperty(c, 'clientWidth', { value: 400 });
      Object.defineProperty(c, 'clientHeight', { value: 90 });
      const r = __winnow.drawBars(c, { rows: [{v:'a',n:1},{v:'b',n:2},{v:'c',n:3}], label:'v', value:'n' });
      const picked = __winnow.pickBar(r.boxes, 10, 45);  // middle row
      return picked ? picked.v : null;
    }""")
    assert row == "b"


def test_long_labels_stay_out_of_the_bar(page):
    """A 60-character label used to be cut by character count (40) against
    a 220 px gutter and then painted UNDER the bar drawn after it. Now the
    gutter grows to fit (up to half the width) and the label is cut by
    measured width, so the bar region holds nothing but bar: every pixel
    across it is the same colour."""
    res = page.evaluate(
        """() => {
          const c = document.createElement('canvas');
          c.style.width = '900px'; c.style.height = '60px';
          document.body.appendChild(c);
          Object.defineProperty(c, 'clientWidth', { value: 900 });
          Object.defineProperty(c, 'clientHeight', { value: 60 });
          const label = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Enc AAAAAAAAAAAAAAAA';
          const r = __winnow.drawBars(c, { rows: [{ v: label, n: 7 }, { v: 'short', n: 3 }], label: 'v', value: 'n', horizontal: true });
          const ctx = c.getContext('2d');
          const scale = c.width / 900;
          const y = Math.round(15 * scale);              // mid-row of row 0 (rowH = 30)
          const x0 = Math.round((r.labelWidth + 3) * scale), x1 = Math.round((r.labelWidth + 60) * scale);
          const colours = new Set();
          for (let x = x0; x < x1; x++) { const d = ctx.getImageData(x, y, 1, 1).data; colours.add(d.join(',')); }
          const gutterLit = (() => { let n = 0; for (let x = 2; x < Math.round(r.labelWidth * scale); x += 2) { if (ctx.getImageData(x, y, 1, 1).data[3] > 40) n++; } return n; })();
          c.remove();
          return { labelWidth: r.labelWidth, distinct: colours.size, gutterLit };
        }""")
    # The gutter grew for the long label (past the 220 default, never past half)
    assert 220 < res["labelWidth"] <= 450, res
    # …the label was drawn in it, and the bar band holds one colour only
    assert res["gutterLit"] > 0, res
    assert res["distinct"] == 1, res


def test_short_labels_keep_the_default_gutter(page):
    res = page.evaluate(
        """() => {
          const c = document.createElement('canvas');
          c.style.width = '900px'; c.style.height = '60px';
          document.body.appendChild(c);
          Object.defineProperty(c, 'clientWidth', { value: 900 });
          Object.defineProperty(c, 'clientHeight', { value: 60 });
          const r = __winnow.drawBars(c, { rows: [{ v: 'alpha', n: 5 }, { v: 'beta', n: 2 }], label: 'v', value: 'n', horizontal: true });
          c.remove();
          return r.labelWidth;
        }""")
    assert res == 220

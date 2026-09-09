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

/* The histogram strip: bars of WHEN the current view's rows happened,
   between the toolbar and the grid, following every filter — and a drag
   across the bars that becomes the timeframe filter. It was the
   table_histogram example plugin until 2026-09; the counting
   (Store.time_histogram) was always core, and this is the panel and its
   route brought in with it. The top_values example holds the
   register_toolbar_panel slot now.

   Layout is time-proportional: a bar's x and width come from its
   bucket's start and width over the view's span, so gaps in activity
   show as gaps rather than being squeezed out — which is why this is
   not charts.js drawHistogram, whose x is index-proportional. A drag
   writes S.timeRange exactly as the Timeframe dialog does; the view
   rebuilds; the strip hears winnow:viewchange and redraws over the
   narrowed span. Everything visual comes from the CSS tokens (accent,
   panel, dim, sel), read at draw time, so a skin or accent change is one
   redraw (winnow:appearance) — a canvas does not inherit CSS.

   Declarations only. wireHistogram() builds the chrome and attaches the
   document listeners once, for the page's life (session.js does the same
   for the diff banner): the strip is not a plugin mount, so there is
   nothing to dispose. It shares #pluginPanels with plugin toolbar
   panels; plugins.js syncPluginPanels() is the one writer of that host's
   `hidden` and asks histogramOpen() so the host shows for either kind. */
import { $, ROW_H, api, el } from './core.js';
import { headH, rowAt, vScroll } from './grid.js';
import { syncPluginPanels } from './plugins.js';
import { S } from './state.js';
import { datetimeColumns, updateTimeRangeButton } from './timeframe.js';
import { parseTimestamp } from './tsformat.js';
import { rebuildView } from './view.js';

/* Per browser, like the appearance and keymap prefs: {open, column}. */
export const HISTOGRAM_PREFS_KEY = 'winnow.histogram';
/* Where the example plugin kept its toggle, and its panel id there. */
const LEGACY_PANELS_KEY = 'winnow.panels';
const LEGACY_PANEL_ID = 'table-histogram.histogram';
const REFRESH_MS = 150;
/* How many times the strip re-asks about a view that is still the grid's
   and still answers 409. Small: the grid's own paging hits the same
   expired view and rebuilds, which brings a view change with it. */
const RETRY_MAX = 3;
const HEIGHT = 96;

let data = null;        // last /api/histogram response
let column = null;      // the datetime column being charted
let brush = null;       // {x0, x1} while dragging, in canvas CSS px
let timer = null;
let inflight = 0;
let seq = 0;            // request counter: only the newest answer lands
let problem = null;     // a 400's message, shown in place of the chart
let drawnFor = null;    // the view id the chart on screen describes
let retries = 0;        // consecutive re-asks after an answer we could not use
/* Set when the re-ask budget runs out with the chart still describing a
   view the grid has moved on from. Keeping the old chart up is right —
   an empty strip mid-filter is worse than a slightly old one — but it
   has to stop claiming to be current, which is the half of "the
   histogram isn't updating" that is really "the histogram didn't say it
   had given up". */
let stale = false;
let shown = false;      // what syncHistogramPanel last applied (its onShow edge)
let markKey = null;     // the viewport marker's last window, so a repaint that
                        // would not move it costs nothing
let ui = null;          // {colSel, info, canvas, hint, empty} once wired

function prefs() {
  try {
    const p = JSON.parse(localStorage.getItem(HISTOGRAM_PREFS_KEY) || '{}');
    return p && typeof p === 'object' ? p : {};
  } catch { return {}; }
}
function savePrefs(patch) {
  localStorage.setItem(HISTOGRAM_PREFS_KEY, JSON.stringify({ ...prefs(), ...patch }));
}

/* The example plugin's toggle was one key in winnow.panels, the plugin
   host's map. An analyst who kept that panel open gets the built-in open
   on first load; the key is removed so this runs once and the map stays
   a map of plugins. */
export function migrateHistogramPrefs() {
  let panels;
  try { panels = JSON.parse(localStorage.getItem(LEGACY_PANELS_KEY) || '{}'); } catch { return; }
  if (!panels || typeof panels !== 'object' || !(LEGACY_PANEL_ID in panels)) return;
  if (panels[LEGACY_PANEL_ID]) savePrefs({ open: true });
  delete panels[LEGACY_PANEL_ID];
  localStorage.setItem(LEGACY_PANELS_KEY, JSON.stringify(panels));
}

export function histogramOpen() { return !!prefs().open; }

/* Colour the bars by tag, per browser like the open flag and the chosen
   column. Off by default: the plain chart answers "when did this view
   happen", and the stacked one answers a question you only have once you
   have started tagging. */
export function histogramStacked() { return !!prefs().stack; }

export function toggleHistogramStack(on = !histogramStacked()) {
  savePrefs({ stack: !!on });
  data = null;          // the split is asked for at fetch time, not derived
  schedule();
  draw();
}

export function toggleHistogram(on = !histogramOpen()) {
  savePrefs({ open: !!on });
  // Host first: the strip is measured on show, and a hidden host gives
  // the canvas no width to measure.
  syncPluginPanels();
  syncHistogramPanel();
}

/* Shows the strip only while toggled on AND the grid is the active tab
   (the toolbar hides on page tabs, and this belongs to it). Idempotent —
   syncTabChrome calls it on every switch, toggleHistogram after every
   click, main.js once the keymap is loaded — and the one place the
   button's pressed state and tooltip come from. */
export function syncHistogramPanel() {
  const panel = $('histogramPanel');
  const btn = $('btnHistogram');
  if (!panel || !btn) return;
  const open = histogramOpen();
  const show = open && S.activeTab === 'grid';
  btn.setAttribute('aria-pressed', String(open));
  const key = ((S.keymap && S.keymap.toggleHistogram) || [])[0];
  btn.title = 'When the rows in this view happened, as time buckets — drag across the bars to set the timeframe filter'
    + (key ? ` — "${key}" to show/hide` : '');
  panel.hidden = !show;
  // Coming back into view: the grid may have rebuilt while a page tab
  // hid the strip, and a resize meanwhile left the canvas unmeasured.
  if (show && !shown) load();
  shown = show;
}

export function refreshHistogram() { return load(); }

/* What the strip last drew, and the layers it derived from it. Read-only
   windows onto module state — the chart lives on a canvas, so this is the
   only way anything outside (a UI test, an eventual export-the-chart) can
   see what is actually on screen. */
export function histogramData() { return data; }
export function histogramLayers() { return stackLayers(); }
/* And the viewport marker's own numbers, for the same reason: it is drawn
   on the canvas, so this is the only way to check that the epochs it
   places are the ones the bars were bucketed on. */
export function histogramViewport() { return viewportMarks(); }

/* ------------------------------------------------------------- chrome */

function buildChrome(container) {
  const bar = el('div', 'histogram-head');
  const title = el('span', 'th-title', 'Histogram');
  const colSel = el('select');
  colSel.title = 'Which datetime column to chart';
  colSel.onchange = () => { column = colSel.value || null; savePrefs({ column }); schedule(); };
  const info = el('span', 'th-info', '');
  const legend = el('span', 'th-legend');
  const stackBtn = el('button', 'btn ghost th-stack', 'Tags');
  stackBtn.title = 'Colour the bars by tag — each row under the first tag it carries, '
    + 'untagged underneath, so the bars stay as tall as the rows they count';
  stackBtn.onclick = () => toggleHistogramStack();
  const clearBtn = el('button', 'btn ghost', 'Clear timeframe');
  clearBtn.title = 'Remove the timeframe filter this strip set (the ⏱ filter in the toolbar)';
  clearBtn.onclick = () => clearTimeRange();
  bar.append(title, colSel, info, legend, stackBtn, clearBtn);

  const canvas = el('canvas', 'th-canvas');
  canvas.style.height = `${HEIGHT}px`;
  const hint = el('div', 'note-status th-hint', 'Drag across the bars to set the timeframe filter to that range.');
  // The empty states are one line of DOM text, not a sentence painted
  // onto a 96px canvas: text on the canvas neither wraps nor shrinks, so
  // a narrow window clipped it, and the strip kept its full chart height
  // (plus the drag hint, plus Clear) to say there was nothing to chart.
  const empty = el('div', 'note-status th-empty');
  empty.hidden = true;
  container.append(bar, canvas, hint, empty);
  ui = { colSel, info, legend, stackBtn, canvas, hint, empty };

  /* ------------------------------------------------------------ brush */
  canvas.addEventListener('mousedown', (e) => {
    if (!data || !data.total) return;
    const r = canvas.getBoundingClientRect();
    brush = { x0: e.clientX - r.left, x1: e.clientX - r.left };
    draw();
    e.preventDefault();
  });
  canvas.addEventListener('mousemove', (e) => {
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    if (brush) { brush.x1 = x; draw(); }
    if (data && data.total) {
      const w = canvas.clientWidth || 1;
      const tt = tOf(x, w);
      const b = data.buckets.find((bb) => tt >= bb[0] && tt < bb[0] + data.bucket_seconds);
      const stack = stackLayers();
      const seg = stack && b && stack.rows.find((r) => r[0] === b[0]);
      canvas.title = b
        ? `${iso(b[0])} — ${b[1].toLocaleString()} rows`
          + (seg ? '\n' + seg[1].map((n, i) => (n ? `${stack.labels[i]}: ${n.toLocaleString()}` : ''))
                            .filter(Boolean).join('\n') : '')
        : iso(tt);
    }
  });
  canvas.addEventListener('mouseup', endBrush);
  canvas.addEventListener('mouseleave', () => { if (brush) endBrush(); });
}

// Clear timeframe stays through the empty state: the drag that emptied
// the view is the most likely reason it IS empty, and the button is the
// way back from it.
function showEmpty(text) {
  ui.canvas.hidden = true;
  ui.hint.hidden = true;
  ui.empty.textContent = text;
  ui.empty.hidden = false;
}
function showChart() {
  ui.empty.hidden = true;
  ui.canvas.hidden = false;
  ui.hint.hidden = false;
}

/* ------------------------------------------------------------ helpers */

const tokens = () => {
  const cs = getComputedStyle(document.documentElement);
  return {
    accent: cs.getPropertyValue('--accent').trim() || '#d9a441',
    dim: cs.getPropertyValue('--dim').trim() || '#888',
    line: cs.getPropertyValue('--line').trim() || '#333',
    line2: cs.getPropertyValue('--line-2').trim() || '#444',
    sel: cs.getPropertyValue('--sel').trim() || 'rgba(255,255,255,.1)',
    text: cs.getPropertyValue('--text').trim() || '#ddd',
    mono: cs.getPropertyValue('--mono').trim() || 'monospace',
  };
};
const pad2 = (n) => String(n).padStart(2, '0');
/* The TS_NORMALIZE'd 'YYYY-MM-DD HH:MM:SS' shape the timeframe filter
   compares through — what a brush must write. */
const iso = (epoch) => {
  const d = new Date(epoch * 1000);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} `
    + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
};
const humanBucket = (s) => (s % 86400 === 0 ? `${s / 86400}d` : s % 3600 === 0 ? `${s / 3600}h`
  : s % 60 === 0 ? `${s / 60}m` : `${s}s`);

/* Which datetime column to chart. S.columns includes derived columns and
   a merge's, the same list Store.time_histogram accepts. A column the
   analyst picked (kept per browser) wins while the table has it; else
   the timeframe filter's column; else the first. */
function pickColumn() {
  const cols = datetimeColumns();
  if (!(column && cols.includes(column))) {
    const want = prefs().column;
    const tr = S.timeRange;
    column = want && cols.includes(want) ? want
      : tr && tr.column && cols.includes(tr.column) ? tr.column
        : (cols[0] || null);
  }
  ui.colSel.replaceChildren();
  for (const c of cols) { const o = el('option', null, c); o.value = c; ui.colSel.append(o); }
  if (column) ui.colSel.value = column;
  ui.colSel.hidden = !cols.length;
  return column;
}

/* Span used for the x axis: the view's [start, end + one bucket). */
function span() {
  if (!data || !data.buckets.length) return null;
  const first = data.buckets[0][0];
  const last = data.buckets[data.buckets.length - 1][0] + data.bucket_seconds;
  return { t0: first, t1: Math.max(last, first + data.bucket_seconds) };
}
const xOf = (t, w) => { const s = span(); return ((t - s.t0) / (s.t1 - s.t0)) * w; };
const tOf = (x, w) => { const s = span(); return s.t0 + (x / w) * (s.t1 - s.t0); };

/* The stacked layers to paint, or null when the chart is a plain one.
   Layer 0 is the untagged remainder; the rest follow the server's tag
   order, which is tag id order, which is ribbon order — the same order
   the row attribution used, so a segment's colour and the tag it counts
   can't drift apart.

   A tag deleted since the answer came back has no colour to draw in and
   is folded into the untagged base rather than dropped: the rows are
   still in the view, and a bar that shrank would be a lie about them. */
function stackLayers() {
  if (!histogramStacked() || !data || !data.stack) return null;
  const cs = tokens();
  // --line-2 rather than --line for the untagged base: the bar's full
  // height is the point of stacking this way (it is the same height the
  // plain chart draws), and a base the eye cannot separate from the panel
  // throws that away. Quiet enough that the tagged segments still lead,
  // which they should — untagged is usually most of the view.
  const colours = [cs.line2];
  const labels = ['Untagged'];
  const fold = [];
  data.stack.tags.forEach((id, i) => {
    const tag = S.tags.find((x) => x.id === id);
    if (!tag) { fold.push(i + 1); return; }
    colours.push(tag.color);
    labels.push(tag.name);
  });
  const rows = data.stack.buckets.map(([b, counts]) => {
    const kept = [counts[0], ...counts.slice(1).filter((_, i) => !fold.includes(i + 1))];
    for (const i of fold) kept[0] += counts[i];
    return [b, kept];
  });
  return { colours, labels, rows };
}

/* A swatch and a name per layer, beside the count line. Only what is
   actually in the chart: a case with twelve tags and two of them in this
   view lists two. */
function drawLegend() {
  const { legend, stackBtn } = ui;
  stackBtn.setAttribute('aria-pressed', String(histogramStacked()));
  legend.replaceChildren();
  const stack = stackLayers();
  if (!stack) return;
  const present = stack.rows.length
    ? stack.labels.map((_, i) => stack.rows.some(([, c]) => c[i] > 0))
    : stack.labels.map(() => false);
  stack.labels.forEach((name, i) => {
    if (!present[i]) return;
    const chip = el('span', 'th-legend-item');
    const sw = el('span', 'swatch');
    sw.style.background = stack.colours[i];
    chip.append(sw, el('span', null, name));
    legend.append(chip);
  });
}

/* ------------------------------------------- the viewport marker */

/* Where the rows the analyst is actually looking at sit on the timeline.

   The strip has always charted the whole view; scrolling the grid moved
   through it with nothing on the chart to say so, which is the one
   question a time chart above a time-ordered table is asked most —
   "where am I?". This is the answer, drawn as a bracket over the span
   the on-screen rows cover, with a tick per row.

   Read from the rows already in the client (invariant #6 keeps only the
   visible window in the DOM, and S.rowsByPos holds the pages behind it),
   so scrolling costs no request. Timestamps are parsed here rather than
   asked for: SQLite's strftime('%s') reads the normalised string as UTC
   (Store.time_histogram), so Date.UTC over parseTimestamp's fields is
   the same epoch the bars were bucketed on.

   Ticks rather than a bare band because a band alone would lie by
   omission. Sorted by the charted column the rows are contiguous and the
   ticks pack into a solid block — the SIEM-style "you are here". Sorted
   by anything else they scatter across the whole chart, and the same
   drawing says THAT instead of implying a window that isn't one. */
/* Two minimums, because the marker has to stay findable across four
   orders of magnitude of zoom. A time-sorted grid showing forty rows of a
   four-thousand-row day is forty seconds out of eleven hours — a third of
   a pixel — so the band gets a floor, and the handle above it a larger
   one, since a 2px band on its own is a smudge you have to already know
   to look for. */
const MARK_MIN_PX = 2;
const HANDLE_MIN_PX = 11;
const HANDLE_H = 4;

function viewportWindow() {
  // Grouped mode has group headers interleaved with rows and collapsed
  // groups that are not loaded at all; there is no honest "these rows".
  if (!S.view || S.groupByCols.length || S.activeTab !== 'grid') return null;
  const body = $('body');
  const total = S.view.row_count;
  if (!body || !total) return null;
  const head = headH();
  const virt = vScroll(body, total, head);
  const first = Math.max(0, Math.floor(virt / ROW_H));
  // The rows area, not the scroller: #gridHead is sticky INSIDE #body, so
  // its height is not row space.
  const tall = Math.max(1, Math.ceil(Math.max(0, body.clientHeight - head) / ROW_H));
  const last = Math.min(total, first + tall);
  return last > first ? { first, last } : null;
}

/* The epochs of the rows in that window, plus how many could not be read.
   A page still in flight has no row yet (rowAt is null) and a value the
   client's parser does not know is skipped — both are "not counted"
   rather than "at zero", the same rule the server's own bucketing uses. */
function viewportMarks() {
  const win = viewportWindow();
  if (!win || !column || !data || !data.total) return null;
  const ci = S.columns.findIndex((c) => c.name === column);
  if (ci < 0) return null;
  const times = [];
  let unread = 0;
  for (let pos = win.first; pos < win.last; pos++) {
    const r = rowAt(pos);
    if (!r) { unread++; continue; }
    const t = parseTimestamp(r.cells[ci]);
    if (!t) { unread++; continue; }
    times.push(Date.UTC(t.y, t.mo - 1, t.d, t.h, t.mi, t.s) / 1000);
  }
  if (!times.length) return null;
  return {
    t0: Math.min(...times), t1: Math.max(...times), times, unread,
    rows: win.last - win.first, first: win.first, last: win.last,
  };
}

/* Cheap identity for "has the marker moved?" — the window, the view it is
   a window into, and how much of it has actually arrived. That last part
   is why this is called from the grid's render() rather than from a
   scroll listener: scroll past a page boundary and the rows are not there
   yet, so the marker would be drawn from the handful that are and then
   never corrected, because the thing that fixes it is a page landing, not
   another scroll event. render() runs for both. */
function markIdentity() {
  const win = viewportWindow();
  if (!win) return null;
  let loaded = 0;
  for (let pos = win.first; pos < win.last; pos++) if (rowAt(pos)) loaded++;
  return `${S.view && S.view.id}:${win.first}:${win.last}:${loaded}:${column}`;
}

/* Called by grid.render() after every paint — scroll, page arrival,
   filter change, grouping on or off. Repaints the canvas only when the
   marker would actually land somewhere new, and leaves the legend (DOM,
   and unchanged by scrolling) alone. */
export function syncHistogramMarker() {
  if (!shown || !data) return;
  const key = markIdentity();
  if (key === markKey) return;
  markKey = key;
  draw({ legend: false });
}

function drawViewportMarker(ctx, w, t, top, plotH) {
  const m = viewportMarks();
  if (!m) return null;
  const s = span();
  // A row can sit outside the charted span: the histogram answers for the
  // view it was built for, and the grid may already be showing a rebuilt
  // one. Clamp rather than draw off-canvas — the marker is a position
  // cue, and one drawn past the axis is worse than one pinned at it.
  const clamp = (x) => Math.max(0, Math.min(w, x));
  const x0 = clamp(xOf(Math.max(m.t0, s.t0), w));
  const x1 = clamp(xOf(Math.min(m.t1, s.t1), w));
  const left = Math.min(x0, x1);
  const width = Math.max(MARK_MIN_PX, Math.abs(x1 - x0));

  ctx.save();
  // The tint is faint on purpose: it sits OVER the bars it is locating,
  // and the brush already taught this chart that covering the data you
  // are pointing at is the wrong trade.
  ctx.globalAlpha = 0.14;
  ctx.fillStyle = t.accent;
  ctx.fillRect(left, top, width, plotH);
  // One tick per on-screen row. Sub-pixel rows land on the same column
  // and simply darken it, which is the right behaviour: a dense block is
  // exactly what "these forty rows are all in this minute" looks like,
  // and a scattered spray is what a view sorted by something other than
  // time actually is.
  ctx.globalAlpha = 0.45;
  for (const time of m.times) {
    if (time < s.t0 || time > s.t1) continue;
    ctx.fillRect(Math.round(clamp(xOf(time, w))), top, 1, plotH);
  }
  ctx.restore();
  // The edges, full height and fully opaque. At a wide span they bracket
  // the tint; at a narrow one they land on each other and read as the
  // single line the situation actually calls for.
  ctx.fillStyle = t.accent;
  ctx.fillRect(Math.round(left), top, MARK_MIN_PX, plotH);
  ctx.fillRect(Math.round(left + width) - MARK_MIN_PX, top, MARK_MIN_PX, plotH);
  // And the handle, in the margin above the plot so it covers no bar —
  // the one part with a minimum wide enough to catch the eye on its own,
  // centred on the span rather than left-aligned to it so it stays over
  // what it points at.
  const hw = Math.max(HANDLE_MIN_PX, width);
  const hx = Math.max(0, Math.min(w - hw, left + width / 2 - hw / 2));
  ctx.fillRect(hx, top - HANDLE_H - 1, hw, HANDLE_H);
  return m;
}

/* --------------------------------------------------------------- draw */

function draw({ legend = true } = {}) {
  if (!ui) return;
  const { canvas, info } = ui;
  /* The one place the strip says it is working. Every other cue lives in
     the `!data` branch below, so a chart that was already up repainted
     the PREVIOUS answer byte-for-byte while a new one was in flight and
     looked exactly like a chart that had stopped updating — which is
     what most of "the histogram isn't updating" turns out to be. An
     attribute on the chrome rather than pixels on the canvas: a canvas
     does not inherit CSS (see the module header), so anything drawn into
     it would need a redraw on a timer to animate. */
  const panel = $('histogramPanel');
  /* Written out longhand rather than with toggleAttribute, which sets the
     EMPTY string: the stylesheet asks for [aria-busy="true"] — as every
     other busy cue in the app does, and as ARIA requires, the attribute
     having no boolean form — so `aria-busy=""` left the cue switched on
     in the markup and invisible on screen. */
  if (panel) {
    if (inflight > 0) panel.setAttribute('aria-busy', 'true');
    else panel.removeAttribute('aria-busy');
  }
  if (!S.sourceId) {
    showEmpty('Open a table to chart when its rows happened.');
    info.textContent = '';
    return;
  }
  if (!column) {
    showEmpty('No datetime column in this table — derive one from a column header to chart it.');
    info.textContent = '';
    return;
  }
  if (problem) {
    showEmpty(problem);
    info.textContent = '';
    return;
  }
  if (!data || !data.total) {
    showEmpty(inflight ? 'Loading…'
      : stale ? 'Could not read the histogram for this view — change a filter to try again.'
      : 'No rows with a parsable timestamp in this view.');
    info.textContent = data ? '0 rows' : '';
    return;
  }
  showChart();   // before measuring: a hidden canvas has no width
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(HEIGHT * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, HEIGHT);
  const t = tokens();
  ctx.font = `10px ${t.mono}`;
  const top = 6, bottom = HEIGHT - 16, plotH = bottom - top;
  const max = Math.max(...data.buckets.map((b) => b[1]));
  const s = span();
  const bw = Math.max(1, (data.bucket_seconds / (s.t1 - s.t0)) * w - 1);
  const stack = stackLayers();
  if (stack) {
    // Bottom-up, untagged first: each row is in exactly one segment (the
    // server puts it under the first tag it carries), so the bar is the
    // same height it would be plain and the two charts can be read
    // against each other.
    for (const [b, counts] of stack.rows) {
      const x = xOf(b, w);
      const total = counts.reduce((a, c) => a + c, 0);
      if (!total) continue;
      const full = Math.max(1, (total / max) * plotH);
      let y = bottom;
      counts.forEach((n, i) => {
        if (!n) return;
        // Proportional to this bar's own height, so rounding cannot make
        // the segments add up to more or less than the bar.
        const h = (n / total) * full;
        ctx.fillStyle = stack.colours[i];
        ctx.fillRect(x, y - h, bw, h);
        y -= h;
      });
    }
  } else {
    ctx.fillStyle = t.accent;
    for (const [b, n] of data.buckets) {
      const x = xOf(b, w);
      const h = Math.max(1, (n / max) * plotH);
      ctx.fillRect(x, bottom - h, bw, h);
    }
  }
  // baseline + axis labels (start / end, and a middle tick)
  ctx.strokeStyle = t.line;
  ctx.beginPath(); ctx.moveTo(0, bottom + 0.5); ctx.lineTo(w, bottom + 0.5); ctx.stroke();
  ctx.fillStyle = t.dim;
  ctx.fillText(iso(s.t0), 4, HEIGHT - 4);
  const endLabel = iso(s.t1);
  ctx.fillText(endLabel, w - ctx.measureText(endLabel).width - 4, HEIGHT - 4);
  const mid = iso((s.t0 + s.t1) / 2);
  ctx.fillText(mid, w / 2 - ctx.measureText(mid).width / 2, HEIGHT - 4);
  const mark = drawViewportMarker(ctx, w, t, top, plotH);
  // brush overlay
  if (brush) {
    const x0 = Math.min(brush.x0, brush.x1), x1 = Math.max(brush.x0, brush.x1);
    // Translucent: --sel is an opaque-enough panel tint, and painting it
    // over the plot hid the bars being selected — you were choosing a
    // range by covering up the thing you were choosing it from. The
    // outline stays fully opaque, so the edges are still exact.
    ctx.save();
    ctx.globalAlpha = 0.28;
    ctx.fillStyle = t.sel;
    ctx.fillRect(x0, top, x1 - x0, plotH);
    ctx.restore();
    ctx.strokeStyle = t.accent;
    ctx.strokeRect(x0 + 0.5, top + 0.5, x1 - x0, plotH);
    // What the drag would apply, while it is being dragged.
    const r = snapped();
    const label = `${iso(r.start)} — ${iso(r.end)}`;
    ctx.fillStyle = t.text;
    const lw = ctx.measureText(label).width;
    ctx.fillText(label, Math.max(2, Math.min(w - lw - 2, (x0 + x1) / 2 - lw / 2)), top + 10);
  }
  if (legend) drawLegend();
  const tr = S.timeRange;
  info.textContent = `${data.total.toLocaleString()} rows · ${humanBucket(data.bucket_seconds)} buckets · max ${max.toLocaleString()}`
    + (mark ? ` · on screen ${iso(mark.t0)}${mark.t1 > mark.t0 ? ` → ${iso(mark.t1)}` : ''}` : '')
    + (tr && tr.enabled && (tr.start || tr.end) ? ' · timeframe on' : '')
    // Said plainly rather than left for the analyst to notice: a chart
    // describing a filter that is no longer on is worse than no chart,
    // because it is quietly wrong instead of obviously absent.
    + (stale ? ' · showing the previous filter' : '');
}

/* Snapping. Ranges should read as clean clock times, which is why the
   drag is rounded outwards — but rounding to the CURRENT bar width made
   a smaller timeframe unselectable: with 6h bars, any drag inside one
   bar became that whole 6h bar, so the view never narrowed enough for
   the histogram to re-bucket and "zoom in" did nothing. The unit is
   picked from the drag itself instead: fine enough that the rounding
   cannot grow the range much, never finer than a second, and never
   coarser than the bar it was dragged over. The server re-buckets from
   whatever span it is given (Store.HISTOGRAM_BUCKETS), so a genuinely
   small range is what produces the finer bars. */
const SNAP_UNITS = [1, 5, 15, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400];
function snapUnit(seconds) {
  const cap = data ? data.bucket_seconds : 1;
  const want = Math.max(1, seconds / 12);   // rounding adds ≤ ~8% per end
  let unit = SNAP_UNITS[0];
  for (const u of SNAP_UNITS) { if (u <= want && u <= cap) unit = u; }
  return unit;
}

/* The range the current drag would apply: both ends rounded outwards
   with one unit chosen from the drag's own width. */
function snapped() {
  if (!brush) return null;
  const w = ui.canvas.clientWidth || 1;
  const t0 = tOf(Math.min(brush.x0, brush.x1), w);
  const t1 = tOf(Math.max(brush.x0, brush.x1), w);
  const u = snapUnit(Math.max(1, t1 - t0));
  return { start: Math.floor(t0 / u) * u, end: Math.ceil(t1 / u) * u };
}

function endBrush() {
  if (!brush) return;
  const w = ui.canvas.clientWidth || 1;
  const x0 = Math.max(0, Math.min(brush.x0, brush.x1)), x1 = Math.min(w, Math.max(brush.x0, brush.x1));
  const wasDrag = Math.abs(x1 - x0) > 3;
  const r = snapped();
  brush = null;
  draw();
  if (!wasDrag || !r) return;
  setTimeRange(iso(r.start), iso(r.end));
}

/* The case timeframe filter, written the way the Timeframe dialog writes
   it — the same object, so the ⏱ button, the toggle key and every other
   consumer see it as if typed there. */
function setTimeRange(start, end) {
  S.timeRange = { enabled: true, column: column || null, start: start || '', end: end || '' };
  updateTimeRangeButton();
  if (S.sourceId) rebuildView({ keepScroll: false });
}
function clearTimeRange() {
  S.timeRange = { enabled: false, column: null, start: '', end: '' };
  updateTimeRangeButton();
  if (S.sourceId) rebuildView({ keepScroll: false });
}

/* ------------------------------------------------------------ refresh */

async function load() {
  if (!ui) return;
  const v = S.view;
  if (!S.sourceId || !v || !pickColumn()) { data = null; problem = null; drawnFor = null; draw(); return; }
  const mine = ++seq;
  inflight++;
  draw();
  // Ask for as many buckets as the canvas can actually show, rather than
  // a fixed 160: at 900px that was 5px slivers, and a zoomed-in view came
  // back just as dense as the one it zoomed out of, which is what made
  // narrowing the timeframe feel like it had changed nothing. ~7px a bar
  // is readable, and the server picks the nearest clean width from
  // Store.HISTOGRAM_BUCKETS for the span it is given. The draw() above
  // hides the canvas behind "Loading…" while there is nothing to draw, so
  // the first ask after opening measures the section the canvas fills:
  // measuring the hidden canvas fell through to 600px (85 bars), and the
  // first chart stayed coarser than the strip until the next view change.
  const width = ui.canvas.clientWidth || ui.canvas.parentElement.clientWidth || 600;
  const maxBuckets = Math.max(20, Math.min(400, Math.floor(width / 7)));
  let next = null, err = null;
  try {
    next = await api(`/api/histogram?view_id=${encodeURIComponent(v.view_id)}`
      + `&column=${encodeURIComponent(column)}&max_buckets=${maxBuckets}`
      + (histogramStacked() ? '&stack=tags' : ''));
  } catch (e) { err = e; }
  inflight = Math.max(0, inflight - 1);
  if (mine !== seq) return;   // a newer request is out; its answer paints
  if (!err) { data = next; problem = null; drawnFor = v.view_id; stale = false; }
  // A 409 is the view going mid-rebuild. Anything else — a 400 for a
  // column the table no longer has, or one that is not a datetime — is
  // worth a line of text, and waiting would fix nothing.
  else if (err.status !== 409) { data = null; problem = err.message; drawnFor = v.view_id; }
  draw();
  // An answer describes the view it was asked about. If the chart on
  // screen is not about the view the grid has now, ask again rather than
  // waiting for a view change: the rebuild that evicted the old view
  // fired its own change before this request came back, so no further one
  // is coming. That is how one lost answer during a burst of filter
  // changes left the strip describing the PREVIOUS filter — the reported
  // "the histogram stops updating" — until something else happened to
  // rebuild the view. Bounded: a view that is current and keeps 409ing is
  // re-asked a few times and then left alone, rather than polled forever.
  if (!shown || !S.view || S.view.view_id === drawnFor) return;
  if (S.view.view_id !== v.view_id) { retries = 0; schedule(); }
  else if (retries < RETRY_MAX) { retries += 1; schedule(); }
  // Out of re-asks with the chart still about an older view. Stop, and
  // say so — silence here is indistinguishable from a strip that simply
  // never updates.
  else { stale = true; draw(); }
}
function schedule() { clearTimeout(timer); timer = setTimeout(load, REFRESH_MS); }

/* DOM wiring for this module, called once by main.js. */
export function wireHistogram() {
  migrateHistogramPrefs();
  buildChrome($('histogramPanel'));
  $('btnHistogram').onclick = () => toggleHistogram();
  // Attached once, for the page's life. Closed, or open but hidden behind
  // a page tab, the strip costs nothing: no fetch until it is on screen,
  // and syncHistogramPanel's show edge fetches then. Keying on the pref
  // alone ran the aggregate twice for a view rebuilt behind the SQL tab —
  // once for a canvas nobody could see, again on the way back.
  document.addEventListener('winnow:viewchange', () => { if (shown) { retries = 0; stale = false; schedule(); } });
  // Tokens are read at draw time, so a skin/accent change is one redraw.
  document.addEventListener('winnow:appearance', () => { if (shown) draw(); });
  window.addEventListener('resize', () => { if (shown) draw(); });
  syncHistogramPanel();
}

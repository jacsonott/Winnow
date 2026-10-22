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

   It also says where the grid is: the rows on screen are marked along the
   top of the plot, and the row cursor as a line through it (gridMarks).
   The grid's render() asks for that through histogramFollowGrid, so a
   scroll or a cursor move repaints the canvas and never refetches.

   And it never passes the previous view's chart off as this one's. From
   the moment a rebuild starts until the answer for the view it lands has
   come back, the bars are faded and grey, the position marks are gone and
   the count line says "Updating…" (fresh(), and view.js calls
   histogramFollowBuild / histogramViewLanded to say when). The answer is
   asked for as the view lands, not after the grid has painted it.

   The bars can be coloured four ways, each its own view (histogramView):
   plain; by tag (Tags — the first tag a row carries); by the value of the
   selected cell (Value — that value's rows in the accent, everything else
   grey, as if it were a tag); and, while the grid is grouped, by the first
   N groups of its first level (Groups — one palette colour each, the rest
   "Other", and the same colour beside each group's heading in the grid).
   All four are one row, one segment, so every view's bars are as tall as
   the plain chart's.

   Declarations only. wireHistogram() builds the chrome and attaches the
   document listeners once, for the page's life (session.js does the same
   for the diff banner): the strip is not a plugin mount, so there is
   nothing to dispose. It shares #pluginPanels with plugin toolbar
   panels; plugins.js syncPluginPanels() is the one writer of that host's
   `hidden` and asks histogramOpen() so the host shows for either kind. */
import { $, api, el } from './core.js';
import { displayValue, ellipsize } from './filters.js';
import { onScreenRows, render, rowAt } from './grid.js';
import { groupValueLabel, isTagGroupCol } from './grouping.js';
import { syncPluginPanels } from './plugins.js';
import { S } from './state.js';
import { datetimeColumns, updateTimeRangeButton } from './timeframe.js';
import { parseTimestamp } from './tsformat.js';
import { rebuildInFlight, rebuildView } from './view.js';

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
let askedFor = null;    // the view id of the newest ask, in flight or answered (not 409'd)
let retries = 0;        // consecutive re-asks after an answer we could not use
/* Set when the re-ask budget runs out with the chart still describing a
   view the grid has moved on from. Keeping the old chart up is right —
   an empty strip mid-filter is worse than a slightly old one — but it
   has to stop claiming to be current, which is the half of "the
   histogram isn't updating" that is really "the histogram didn't say it
   had given up". */
let stale = false;
/* Set by resetHistogram when another case opens, cleared by the first
   view change after it (or refreshHistogram, when the case lands on no
   table at all). In between, S.view and S.sourceId still name the
   previous case's table — loadSources keeps S.sourceId on purpose — so
   the strip neither draws nor asks: it says "Loading…" instead of
   keeping the last case's chart up as if it were this one's. */
let opening = false;
let shown = false;      // what syncHistogramPanel last applied (its onShow edge)
let ui = null;          // {colSel, info, canvas, hint, empty} once wired
let chartUp = false;    // the canvas holds a chart (draw() reached paintChart)
let marks = null;       // the grid position the canvas last painted (gridMarks)
let marksRaf = null;    // one repaint per frame, however often the grid renders
/* The Value view's value: {column, value} — the selected cell's. It
   follows the cell focus, so a right-click charts the cell too (that
   moves the focus). Kept when the focus goes away (a group heading,
   Escape), so the chart does not drop back to plain under a click that
   was not about it. */
let picked = null;
let pickedFocus = null; // the S.cellFocus object `picked` was last taken from (followSplit)
let askedSplit = null;  // the split key of the newest ask
let splitTimer = null;  // a split change waiting out SPLIT_MS
let splitTimerKey = null;
/* A cell move is a new ask. Waited out briefly so holding an arrow key
   down a column asks once where it stops, not once per row. */
const SPLIT_MS = 200;
/* The values ride in the GET's query string, as JSON, so a cell holding a
   whole command line or JSON document would make a request line the
   server refuses. This many characters of JSON, percent-encoded at worst
   three bytes apiece, stays well inside uvicorn's limit; a value past it
   is not charted, and a group past it falls into "Other". */
const SPLIT_JSON_MAX = 3000;
/* The Groups view's colours follow the VALUE, not its rank: a filter that
   reorders the top groups must not repaint the survivors, or the colour
   an analyst learnt for "admin" means someone else after it. A value
   keeps its slot while it stays in the top N; one that joins takes the
   lowest free slot. Reset with the column or N. */
let groupSlots = { key: null, map: new Map() };
let lastGroups = null;  // the top groups as last read, for the gap while a regroup refetches them

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

/* How the bars are coloured: 'plain', 'tags', 'value' or 'groups'. Per
   browser like the open flag and the chosen column. Plain by default: the
   plain chart answers "when did this view happen", and the others answer
   questions you only have once you have started reading the rows. Before
   there were four, the Tags toggle was the `stack` flag, read here so an
   analyst who had it on keeps it. */
export const HISTOGRAM_VIEWS = ['plain', 'tags', 'value', 'groups'];
export function histogramView() {
  const p = prefs();
  return HISTOGRAM_VIEWS.includes(p.view) ? p.view : p.stack ? 'tags' : 'plain';
}

export function setHistogramView(view) {
  if (!HISTOGRAM_VIEWS.includes(view)) return;
  savePrefs({ view, stack: view === 'tags' });
  // The Value view starts from the cell already selected, if there is one.
  if (view === 'value') takeFocusedValue();
  // The split is asked for at fetch time, not derived. Until it is back
  // the chart stays up, faded — fresh() knows an answer without the split
  // cannot be drawn as one.
  schedule();
  draw();
  // The group headings carry the Groups view's swatches.
  if (S.groupByCols.length) render();
}

/* The Tags view, under the name it had while it was the only one. */
export function histogramStacked() { return histogramView() === 'tags'; }
export function toggleHistogramStack(on = !histogramStacked()) { setHistogramView(on ? 'tags' : 'plain'); }

/* How many groups the Groups view colours. */
export const GROUPS_TOP = [3, 5, 8];
export function histogramGroupsTop() {
  const n = Number(prefs().groupsTop);
  return GROUPS_TOP.includes(n) ? n : 5;
}
function setGroupsTop(n) {
  savePrefs({ groupsTop: n });
  schedule();
  draw();
  render();
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
  const edge = show !== shown;
  shown = show;
  // The group headings wear the Groups view's colours only while the
  // strip that explains them is on screen.
  if (edge && S.activeTab === 'grid' && S.groupByCols.length && histogramView() === 'groups') render();
}

/* Re-read the grid's state now rather than on the next view change —
   for the changes that never fire one: the grid left with no table (an
   empty case, the last tab closed), which is a view going away rather
   than a view landing. */
export function refreshHistogram() { opening = false; return load(); }

/* A case switch. Everything the strip holds describes the previous
   case's table, and the only thing that used to replace it was a view
   change in the new one: a case that opened on no table never sent one,
   and a large one sent it only once its first view had built, so the
   old chart stayed up — the previous case's rows under this case's name
   — until something rebuilt the view. Dropped here, with home.js's other
   per-case resets; the answer to a request still out is about the old
   case, and the seq bump drops that too. */
export function resetHistogram() {
  seq += 1;
  clearTimeout(timer);
  data = null;
  problem = null;
  drawnFor = null;
  askedFor = null;
  stale = false;
  retries = 0;
  brush = null;
  column = null;
  marks = null;
  picked = null;
  pickedFocus = null;
  askedSplit = null;
  lastGroups = null;
  clearTimeout(splitTimer);
  splitTimerKey = null;
  opening = true;
  draw();
}

/* What the strip last drew, and the layers it derived from it. Read-only
   windows onto module state — the chart lives on a canvas, so this is the
   only way anything outside (a UI test, an eventual export-the-chart) can
   see what is actually on screen. */
export function histogramData() { return data; }
export function histogramLayers() { return stackLayers(); }
/* The grid position as last painted, in time: {band: {start, end} | null,
   ticks: [epoch…], cursor: epoch | null}, or null with no chart up. */
export function histogramPosition() {
  if (!chartUp || !marks) return null;
  return { band: marks.band && { start: marks.band.start, end: marks.band.end },
    ticks: marks.ticks.map((tk) => tk.t), cursor: marks.cursor && marks.cursor.t };
}

/* Whether the chart on screen is the answer for the grid as it is now.
   It is not while a rebuild for this table is in flight (the grid is
   about to change under it), once a new view has landed and its answer
   is not back, or when the column or the tag split was changed and the
   answer for that is not back. Turning the split OFF needs no new answer:
   the plain bars are in every one.

   The previous view's chart stays up while it is not — a strip that
   collapsed to a line and back on every filter change would move the
   grid under the pointer — but faded, without the grid's position, and
   with "Updating…" in the count line. A chart kept at full strength there
   looked like the new view's for as long as the answer took, scrolling
   and all, which on a 500k-row table was seconds, and then "suddenly
   updated". */
function fresh() {
  return !!data && !!S.view && drawnFor === S.view.view_id && !rebuildInFlight()
    && data.column === column && splitAnswered();
}

/* Whether the answer on screen carries the split the current view asks
   for. A view that asks for none (plain, or Value with nothing picked, or
   Groups with the grid not grouped) is answered by any of them. */
function splitAnswered() {
  if (histogramView() === 'tags') return !!(data.stack && data.stack.tags);
  const want = wantedSplit();
  return !want || data.splitKey === want.key;
}

/* ------------------------------------------------------------- splits */

/* The value split the current view asks the server for —
   {kind, column, values, key} — or null for none. `key` is what an
   answer is matched against. */
function wantedSplit() {
  const view = histogramView();
  if (view === 'value') {
    if (!picked || !S.columns.some((c) => c.name === picked.column) || pickedTooLong()) return null;
    return { kind: 'value', column: picked.column, values: [picked.value],
      key: `value\u0000${picked.column}\u0000${JSON.stringify(picked.value)}` };
  }
  if (view === 'groups') {
    const top = topGroups();
    if (!top) return null;
    return { kind: 'groups', column: top.column, values: top.values,
      key: `groups\u0000${top.column}\u0000${JSON.stringify(top.values)}` };
  }
  return null;
}

/* The first N groups of the grid's first grouping level, in the order the
   grid lists them — {column, values} — or null when there are none to
   colour: not grouped, or grouped by tag (the Tags view already colours
   those, in the tags' own colours). While a regroup has emptied S.groups
   to refetch them, the last ones read stand in, so the chart does not
   drop to plain and back on every filter change. */
function topGroups() {
  const col = S.groupByCols[0];
  if (!col || isTagGroupCol(col)) return null;
  const n = histogramGroupsTop();
  const values = [];
  let size = 2;
  for (const g of S.groups) {
    if (g.level !== 0) continue;
    size += JSON.stringify(g.value).length + 1;
    if (size > SPLIT_JSON_MAX) break;
    values.push(g.value);
    if (values.length === n) break;
  }
  if (!values.length) return lastGroups && lastGroups.column === col && lastGroups.n === n ? lastGroups : null;
  lastGroups = { column: col, n, values };
  return lastGroups;
}

/* The palette slot (1..8) each of `values` is drawn in — see groupSlots. */
function groupSlotsFor(column, values) {
  const key = `${column}\u0000${histogramGroupsTop()}`;
  if (groupSlots.key !== key) groupSlots = { key, map: new Map() };
  const keys = values.map((v) => JSON.stringify(v));
  for (const k of [...groupSlots.map.keys()]) if (!keys.includes(k)) groupSlots.map.delete(k);
  const used = new Set(groupSlots.map.values());
  for (const k of keys) {
    if (groupSlots.map.has(k)) continue;
    let slot = 1;
    while (used.has(slot)) slot += 1;
    groupSlots.map.set(k, slot);
    used.add(slot);
  }
  return keys.map((k) => groupSlots.map.get(k));
}

const seriesColour = (cs, slot) => cs.getPropertyValue(`--series-${slot}`).trim() || '#3987e5';

/* The colour a top-level group heading wears in the grid while the Groups
   view is on screen — the same colour as its segment of the bars — or
   null. grouping.js's renderGroupHeaderRow asks. */
export function histogramGroupColour(g) {
  if (!shown || histogramView() !== 'groups' || !g || g.level !== 0) return null;
  const top = topGroups();
  if (!top) return null;
  const i = top.values.findIndex((v) => Object.is(v, g.value));
  if (i < 0) return null;
  const slots = groupSlotsFor(top.column, top.values);
  return seriesColour(getComputedStyle(document.documentElement), slots[i]);
}

/* The focused cell's column and value, or null when the focus is not on a
   data cell (none, or a group heading). */
function focusedValue() {
  const f = S.cellFocus;
  if (!f || !f.name) return null;
  const r = rowAt(f.pos);
  const ci = S.columns.findIndex((c) => c.name === f.name);
  if (!r || ci < 0) return null;
  return { column: f.name, value: r.cells[ci] };
}

const pickedTooLong = () => !!picked && JSON.stringify(picked.value).length > SPLIT_JSON_MAX;

function takeFocusedValue() {
  const v = focusedValue();
  if (v) { picked = v; pickedFocus = S.cellFocus; }
}

/* Called from histogramFollowGrid — every grid render. The Value view
   follows the cell focus; the Groups view follows the grid's groups. When
   what the view asks for has changed, the chart fades (fresh()) and the
   new split is asked for once the changes stop. */
function followSplit() {
  if (!shown || !ui || opening || !S.view) return;
  const view = histogramView();
  if (view !== 'value' && view !== 'groups') return;
  // Every move of the cell focus assigns S.cellFocus a new object, so
  // "moved" is identity, not position: a view rebuild leaves a position
  // naming another row, and that is not the analyst picking it.
  if (view === 'value' && S.cellFocus && S.cellFocus !== pickedFocus) {
    const v = focusedValue();
    // A focus on a row whose page is not back yet has no value to read;
    // the next render, once it lands, takes it.
    if (v) { picked = v; pickedFocus = S.cellFocus; }
  }
  const want = wantedSplit();
  const key = want ? want.key : null;
  if (key === askedSplit || key === splitTimerKey) return;
  splitTimerKey = key;
  clearTimeout(splitTimer);
  splitTimer = setTimeout(() => { splitTimerKey = null; load(); }, SPLIT_MS);
  draw();
}

/* A rebuild started, or ended without landing a view (cancelled,
   superseded, refused, or left to finish in the background) — view.js
   calls this at both. The first fades the chart; the second brings it
   back, since the view it describes is still the grid's. A build that
   lands a view says so through histogramViewLanded instead. */
export function histogramFollowBuild() {
  if (!shown || !ui) return;
  draw();
  if (!rebuildInFlight() && S.view && S.view.view_id !== drawnFor && S.view.view_id !== askedFor) schedule();
}

/* A rebuild landed: S.view is the new view. Called by view.js's
   installView the moment it swaps the view in, before the selection
   remap, the regroup and the paint, rather than leaving the ask to
   winnow:viewchange — which fires after all of those, and then waited
   REFRESH_MS more. Not any earlier: asked while the grid's first page was
   still being fetched, the aggregate (a Python function per row, so it
   holds the GIL) took that fetch from ~2 ms to 40–80 ms on a 500k-row
   table, and the grid is the thing the analyst is waiting on. */
export function histogramViewLanded() {
  if (!shown || !ui || opening) return;
  clearTimeout(timer);
  retries = 0;
  stale = false;
  load();
}

/* ------------------------------------------------------------- chrome */

function buildChrome(container) {
  const bar = el('div', 'histogram-head');
  const title = el('span', 'th-title', 'Histogram');
  const colSel = el('select');
  colSel.title = 'Which datetime column to chart';
  colSel.onchange = () => { column = colSel.value || null; savePrefs({ column }); draw(); schedule(); };
  const info = el('span', 'th-info', '');
  const legend = el('span', 'th-legend');
  // Three views, one pressed at a time; pressing the pressed one goes
  // back to the plain chart. Separate buttons rather than one menu,
  // because which one is on is what the bars' colours mean.
  const views = el('span', 'th-views');
  views.setAttribute('role', 'group');
  views.setAttribute('aria-label', 'Colour the bars by');
  const viewBtn = (view, cls, label, tip) => {
    const b = el('button', `btn ghost ${cls}`, label);
    b.title = tip;
    b.onclick = () => setHistogramView(histogramView() === view ? 'plain' : view);
    views.append(b);
    return b;
  };
  const stackBtn = viewBtn('tags', 'th-stack', 'Tags',
    'Colour the bars by tag — each row under the first tag it carries, '
    + 'untagged underneath, so the bars stay as tall as the rows they count');
  const valueBtn = viewBtn('value', 'th-view-value', 'Value',
    'Colour the rows holding the selected cell\'s value, as if they were tagged — '
    + 'click another cell to chart its value');
  const groupsBtn = viewBtn('groups', 'th-view-groups', 'Groups',
    'While the grid is grouped, give its first groups a colour each — the same colour '
    + 'beside each group\'s heading — and the rest "Other"');
  const topSel = el('select', 'th-top');
  topSel.title = 'How many groups get a colour of their own';
  for (const n of GROUPS_TOP) { const o = el('option', null, `First ${n}`); o.value = String(n); topSel.append(o); }
  topSel.onchange = () => setGroupsTop(Number(topSel.value));
  views.append(topSel);
  const clearBtn = el('button', 'btn ghost', 'Clear timeframe');
  clearBtn.title = 'Remove the timeframe filter this strip set (the ⏱ filter in the toolbar)';
  clearBtn.onclick = () => clearTimeRange();
  bar.append(title, colSel, info, legend, views, clearBtn);

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
  ui = { colSel, info, legend, stackBtn, viewBtns: { tags: stackBtn, value: valueBtn, groups: groupsBtn }, topSel,
    canvas, hint, empty };

  /* ------------------------------------------------------------ brush */
  canvas.addEventListener('mousedown', (e) => {
    // A faded chart is the previous view's: its axis is not this view's
    // span, so a drag across it would set a timeframe from the wrong one.
    if (!data || !data.total || !fresh()) return;
    const r = canvas.getBoundingClientRect();
    brush = { x0: e.clientX - r.left, x1: e.clientX - r.left };
    draw();
    e.preventDefault();
  });
  canvas.addEventListener('mousemove', (e) => {
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    if (brush) { brush.x1 = x; draw(); }
    if (data && data.total && !fresh()) canvas.title = 'Updating for the current view…';
    else if (data && data.total) {
      const w = canvas.clientWidth || 1;
      const tt = tOf(x, w);
      const b = data.buckets.find((bb) => tt >= bb[0] && tt < bb[0] + data.bucket_seconds);
      const stack = stackLayers();
      const seg = stack && b && stack.rows.find((r) => r[0] === b[0]);
      canvas.title = (b
        ? `${iso(b[0])} — ${b[1].toLocaleString()} rows`
          + (seg ? '\n' + seg[1].map((n, i) => (n ? `${stack.labels[i]}: ${n.toLocaleString()}` : ''))
                            .filter(Boolean).join('\n') : '')
        : iso(tt)) + marksTitle(x);
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
  // The legend names the layers of a chart that is no longer drawn — the
  // previous case's tags, after a case switch.
  ui.legend.replaceChildren();
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
    panel: cs.getPropertyValue('--panel').trim() || '#191d23',
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

/* A stored value as epoch seconds, read the way the server reads it for
   the bars: tsformat's parseTimestamp is the client twin of TS_NORMALIZE
   (store.py keeps the two in step), both whole seconds, both UTC. Null for
   anything neither can read — the same rows the bars leave out. Built
   with setUTCFullYear rather than Date.UTC, which reads years 0–99 as
   1900–1999. */
function epochOf(raw) {
  if (raw == null || raw === '') return null;
  const p = parseTimestamp(raw);
  if (!p || p.mo < 1 || p.mo > 12 || p.d < 1 || p.d > 31 || p.h > 23 || p.mi > 59 || p.s > 59) return null;
  const d = new Date(0);
  d.setUTCFullYear(p.y, p.mo - 1, p.d);
  d.setUTCHours(p.h, p.mi, p.s, 0);
  return Math.floor(d.getTime() / 1000);
}

/* Where the grid is, in the chart's x: the rows on screen and the row
   cursor, each read from the charted column of rows the grid already
   holds — nothing is fetched for this.

   The rows on screen are a BAND only when they are in time order (either
   way): then they are one stretch of time and the band is that stretch.
   Sorted by something like a process id they are scattered across the
   chart, and a band from the earliest to the latest would claim most of
   it while saying nothing; each row gets a TICK of its own instead.
   Deciding from the rows rather than from S.sort means a table that
   arrived in time order (most tool output does) gets the band without
   being sorted — and so does a screenful of one host when sorted by
   host, since ties fall back to rid order and those rows ARE one stretch
   of that host's time.

   The cursor is the row the grid's cursor sits on, on screen or not, as
   long as its page is still cached. A row with no parsable time in the
   charted column has no place here, just as it has no bar. */
const MIN_BAND_PX = 4;
function gridMarks(w) {
  const none = { key: '', band: null, ticks: [], cursor: null };
  const s = span();
  const ci = column ? S.columns.findIndex((c) => c.name === column) : -1;
  if (!s || ci < 0 || !S.view) return none;
  // Only times the chart can place: while a rebuilt view's answer is
  // still out, the rows are the new view's and the axis the old one's.
  const at = (r) => {
    const t = r ? epochOf(r.cells[ci]) : null;
    return t != null && t >= s.t0 && t <= s.t1 ? t : null;
  };
  const times = [];
  const rows = onScreenRows();
  if (rows) {
    for (let pos = rows.first; pos <= rows.last; pos++) {
      const t = at(rowAt(pos));
      if (t != null) times.push(t);
    }
  }
  let up = true, down = true;
  for (let i = 1; i < times.length; i++) {
    if (times[i] < times[i - 1]) up = false;
    if (times[i] > times[i - 1]) down = false;
  }
  let band = null, ticks = [];
  if (times.length && (up || down)) {
    const start = Math.min(times[0], times[times.length - 1]);
    const end = Math.max(times[0], times[times.length - 1]);
    let x0 = xOf(start, w), x1 = xOf(end, w);
    // A screenful of rows is often one second of a year-long table; it
    // still has to be findable.
    if (x1 - x0 < MIN_BAND_PX) { const mid = (x0 + x1) / 2; x0 = mid - MIN_BAND_PX / 2; x1 = mid + MIN_BAND_PX / 2; }
    band = { start, end, x0: Math.round(x0), x1: Math.round(x1) };
  } else {
    const seen = new Set();
    for (const t of times) {
      const x = Math.round(xOf(t, w));
      if (!seen.has(x)) { seen.add(x); ticks.push({ t, x }); }
    }
  }
  const ct = S.cursor >= 0 ? at(rowAt(S.cursor)) : null;
  const cursor = ct == null ? null : { t: ct, x: Math.round(xOf(ct, w)) };
  // Pixels, not times: a scroll that moves nothing on the canvas is not
  // worth a repaint.
  const key = `${band ? `${band.x0}-${band.x1}` : ''}|${ticks.map((tk) => tk.x).join(',')}|${cursor ? cursor.x : ''}`;
  return { key, band, ticks, cursor };
}

/* What the marks under the pointer are, for the canvas tooltip — the one
   place they are explained on screen. Within a couple of pixels, since
   the thumb can be 4px wide and the cursor line is 1. */
function marksTitle(x) {
  if (!marks) return '';
  const near = (a, b) => x >= a - 2 && x <= b + 2;
  const out = [];
  if (marks.cursor && near(marks.cursor.x, marks.cursor.x + 1)) out.push(`Selected row: ${iso(marks.cursor.t)}`);
  if (marks.band && near(marks.band.x0, marks.band.x1)) {
    out.push(marks.band.start === marks.band.end ? `Rows on screen: ${iso(marks.band.start)}`
      : `Rows on screen: ${iso(marks.band.start)} — ${iso(marks.band.end)}`);
  }
  if (marks.ticks.some((tk) => near(tk.x, tk.x + 1))) {
    out.push('Rows on screen — not in time order, so each one is marked on its own');
  }
  return out.length ? '\n\n' + out.join('\n') : '';
}

/* Called by the grid's render() — every scroll frame, cursor move and
   page landing. Coalesced to one look per frame, and the canvas is only
   repainted when the marks would land somewhere else. */
export function histogramFollowGrid() {
  followSplit();
  if (!shown || !chartUp || marksRaf || !fresh()) return;
  marksRaf = requestAnimationFrame(() => {
    marksRaf = null;
    if (!shown || !chartUp || !ui || !fresh()) return;
    const w = ui.canvas.clientWidth || 1;
    if (gridMarks(w).key !== (marks ? marks.key : '')) paintChart();
  });
}

/* The stacked layers to paint, or null when the chart is a plain one.
   Layer 0 is the untagged remainder; the rest follow the server's tag
   order, which is tag id order, which is ribbon order — the same order
   the row attribution used, so a segment's colour and the tag it counts
   can't drift apart.

   A tag deleted since the answer came back has no colour to draw in and
   is folded into the untagged base rather than dropped: the rows are
   still in the view, and a bar that shrank would be a lie about them. */
function stackLayers() {
  if (!data || !data.stack) return null;
  const view = histogramView();
  if (view === 'tags' && data.stack.tags) return tagLayers();
  if ((view === 'value' || view === 'groups') && data.stack.kind === view) return valueLayers();
  return null;
}

/* A value split's layers: slot 0 is every other row, then the values in
   the order they were asked for. The Value view draws its one value in the
   accent — the plain chart's own colour, so "these bars, and the rest
   greyed" — and the Groups view each group in its palette slot. */
function valueLayers() {
  const cs = tokens();
  const { kind, column: col, values } = data.stack;
  if (kind === 'value') {
    return { colours: [cs.line2, cs.accent], labels: ['Everything else', `${col} = ${ellipsize(displayValue(values[0]), 40)}`],
      rows: data.stack.buckets, kind };
  }
  const root = getComputedStyle(document.documentElement);
  const slots = groupSlotsFor(col, values);
  return {
    colours: [cs.line2, ...slots.map((sl) => seriesColour(root, sl))],
    labels: ['Other', ...values.map((v) => ellipsize(groupValueLabel(col, v), 32))],
    rows: data.stack.buckets, kind,
  };
}

function tagLayers() {
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
function drawLegend(current = true) {
  const { legend } = ui;
  syncViewButtons();
  legend.replaceChildren();
  // A view with nothing to split by says what it is waiting for, rather
  // than drawing the plain chart under a pressed button with no word on
  // why the bars are not coloured.
  const view = histogramView();
  const hint = view === 'value' && pickedTooLong() ? 'That cell\'s value is too long to chart — select another'
    : view === 'value' && !wantedSplit() ? 'Select a cell to colour the rows holding its value'
    : view === 'groups' && !wantedSplit()
      ? (isTagGroupCol(S.groupByCols[0]) ? 'Grouped by tag — the Tags view colours these'
        : 'Group by a column to colour its first groups')
      : null;
  if (hint) { legend.append(el('span', 'th-legend-hint', hint)); return; }
  // A faded chart is drawn grey (style.css), so a legend of tag colours
  // would be naming colours that are not on screen.
  const stack = current ? stackLayers() : null;
  if (!stack) return;
  const totals = stack.labels.map((_, i) => stack.rows.reduce((a, [, c]) => a + c[i], 0));
  stack.labels.forEach((name, i) => {
    // The picked value is named even when none of its rows is in the
    // chart: that is the answer to "when did this happen", not a gap.
    if (!totals[i] && !(stack.kind === 'value' && i === 1)) return;
    const chip = el('span', 'th-legend-item');
    const sw = el('span', 'swatch');
    sw.style.background = stack.colours[i];
    chip.append(sw, el('span', null, stack.kind === 'value' && i === 1
      ? `${name} · ${totals[i].toLocaleString()} row${totals[i] === 1 ? '' : 's'}` : name));
    if (stack.kind === 'groups' && i > 0) chip.title = `${data.stack.column} = ${displayValue(data.stack.values[i - 1])} — ${totals[i].toLocaleString()} rows in this chart`;
    legend.append(chip);
  });
}

/* The view buttons' pressed state, and the Top N picker beside Groups. */
function syncViewButtons() {
  const view = histogramView();
  for (const [v, b] of Object.entries(ui.viewBtns)) b.setAttribute('aria-pressed', String(view === v));
  ui.topSel.hidden = view !== 'groups';
  ui.topSel.value = String(histogramGroupsTop());
}

/* --------------------------------------------------------------- draw */

function draw() {
  if (!ui) return;
  const { info } = ui;
  chartUp = false;   // until the chart path below says otherwise
  /* The one place the strip says it is working. Every other cue lives in
     the `!data` branch below, so a chart that was already up repainted
     the PREVIOUS answer byte-for-byte while a new one was in flight and
     looked exactly like a chart that had stopped updating — which is
     what most of "the histogram isn't updating" turns out to be. An
     attribute on the chrome rather than pixels on the canvas: a canvas
     does not inherit CSS (see the module header), so anything drawn into
     it would need a redraw on a timer to animate. A rebuild in flight is
     working too: its answer is the next thing this strip will ask for. */
  const panel = $('histogramPanel');
  const building = rebuildInFlight();
  /* Written out longhand rather than with toggleAttribute, which sets the
     EMPTY string: the stylesheet asks for [aria-busy="true"] — as every
     other busy cue in the app does, and as ARIA requires, the attribute
     having no boolean form — so `aria-busy=""` left the cue switched on
     in the markup and invisible on screen. */
  if (panel) {
    if (inflight > 0 || building) panel.setAttribute('aria-busy', 'true');
    else panel.removeAttribute('aria-busy');
  }
  // Ahead of the S.sourceId test: mid case switch it still names the
  // previous case's table (see `opening`).
  if (opening) {
    showEmpty('Loading…');
    info.textContent = '';
    return;
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
  // A 400 is about the view it was asked of; a rebuild may fix it.
  if (problem && !building) {
    showEmpty(problem);
    info.textContent = '';
    return;
  }
  if (!data || !data.total) {
    showEmpty(inflight || building ? 'Loading…'
      : stale ? 'Could not read the histogram for this view — change a filter to try again.'
      : 'No rows with a parsable timestamp in this view.');
    info.textContent = data ? '0 rows' : '';
    return;
  }
  showChart();   // before measuring: a hidden canvas has no width
  const current = fresh();
  if (panel) panel.classList.toggle('is-outdated', !current);
  const max = paintChart(current);
  drawLegend(current);
  if (!current && !stale) {
    // Not the previous answer's numbers: they describe the faded chart,
    // and beside it they read as the new view's.
    info.textContent = 'Updating…';
    return;
  }
  const tr = S.timeRange;
  info.textContent = `${data.total.toLocaleString()} rows · ${humanBucket(data.bucket_seconds)} buckets · max ${max.toLocaleString()}`
    + (tr && tr.enabled && (tr.start || tr.end) ? ' · timeframe on' : '')
    // Said plainly rather than left for the analyst to notice: a chart
    // describing a filter that is no longer on is worse than no chart,
    // because it is quietly wrong instead of obviously absent.
    + (stale ? ' · showing the previous filter' : '');
}

/* The canvas alone — bars, axis, the grid's position, the brush — so a
   scroll can repaint it without touching the chrome around it. Returns
   the tallest bar, which the count line reports. */
const LANE = 6;   // the strip above the plot the position marks sit in
function paintChart(current = true) {
  chartUp = true;
  const { canvas } = ui;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(HEIGHT * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, HEIGHT);
  const t = tokens();
  ctx.font = `10px ${t.mono}`;
  const top = LANE + 2, bottom = HEIGHT - 16, plotH = bottom - top;
  const max = Math.max(...data.buckets.map((b) => b[1]));
  const s = span();
  const bw = Math.max(1, (data.bucket_seconds / (s.t1 - s.t0)) * w - 1);
  // No position on a faded chart: the rows on screen are (or are about to
  // be) another view's, placed on this one's axis.
  marks = current ? gridMarks(w) : { key: '', band: null, ticks: [], cursor: null };
  // The rows on screen, as a column lit BEHIND the bars: over them it
  // would tint every bar it crossed, and the bars' colour is what the
  // tag split is read by.
  if (marks.band) {
    ctx.save();
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = t.text;
    ctx.fillRect(marks.band.x0, top, marks.band.x1 - marks.band.x0, plotH);
    ctx.restore();
  }
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
  // The lane above the plot: the band's thumb, like a scrollbar's, or a
  // tick per row when the rows on screen are not one stretch of time.
  ctx.fillStyle = t.text;
  if (marks.band) ctx.fillRect(marks.band.x0, 0, marks.band.x1 - marks.band.x0, LANE - 1);
  for (const tk of marks.ticks) ctx.fillRect(tk.x, 0, 1, LANE - 1);
  // The row cursor: a line through the plot, over the bars, which would
  // otherwise hide it. On a halo of the panel colour, because a bare
  // 1px line laid over a bar, or in the gap between two tall ones, all
  // but disappeared into it. Plot only, never the lane: the cursor is
  // usually one of the rows on screen, and a mark of its own up there
  // sat on the band's thumb and hid it.
  if (marks.cursor) {
    ctx.fillStyle = t.panel;
    ctx.fillRect(marks.cursor.x - 1, top, 3, plotH);
    ctx.fillStyle = t.text;
    ctx.fillRect(marks.cursor.x, top, 1, plotH);
  }
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
  return max;
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
  // S.view is the previous case's until this one's first view lands; an
  // ask now would name it to the new case's server (see `opening`).
  if (opening) { draw(); return; }
  const v = S.view;
  // Nothing to ask about is still the newest answer: one still out for
  // the table that just went away must not land on top of it.
  if (!S.sourceId || !v || !pickColumn()) {
    seq += 1; data = null; problem = null; drawnFor = null; askedFor = null; draw(); return;
  }
  const mine = ++seq;
  inflight++;
  askedFor = v.view_id;
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
  const want = wantedSplit();
  askedSplit = want ? want.key : null;
  clearTimeout(splitTimer);
  splitTimerKey = null;
  const split = histogramStacked() ? '&stack=tags'
    : want ? `&stack=${want.kind}&split_column=${encodeURIComponent(want.column)}`
      + `&split_values=${encodeURIComponent(JSON.stringify(want.values))}` : '';
  let next = null, err = null;
  try {
    next = await api(`/api/histogram?view_id=${encodeURIComponent(v.view_id)}`
      + `&column=${encodeURIComponent(column)}&max_buckets=${maxBuckets}` + split);
  } catch (e) { err = e; }
  inflight = Math.max(0, inflight - 1);
  if (mine !== seq) return;   // a newer request is out; its answer paints
  if (!err) { data = next; data.splitKey = want ? want.key : null; problem = null; drawnFor = v.view_id; stale = false; }
  // A 409 is the view going mid-rebuild. Anything else — a 400 for a
  // column the table no longer has, or one that is not a datetime — is
  // worth a line of text, and waiting would fix nothing.
  else if (err.status !== 409) { data = null; problem = err.message; drawnFor = v.view_id; }
  // A 409 answered nothing, so the view's landing must not take it as
  // already asked (see the viewchange listener).
  else askedFor = null;
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
  // A rebuild in flight evicted that view and will land the next one,
  // and landing is an ask (histogramViewLanded) — re-asking about the view
  // it is replacing would spend the bounded re-asks on a 409 apiece.
  if (rebuildInFlight()) return;
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
  // A view landing ends a case switch whether or not the strip is on
  // screen: hidden, the show edge's load() is what asks about it.
  // A view that landed through a rebuild was asked about already, as it
  // landed (histogramViewLanded): asking again here would only throw that
  // answer away. A view change with no rebuild behind it (a table
  // reopened on its cached view) asks here, and fades the chart first.
  document.addEventListener('winnow:viewchange', () => {
    opening = false;
    if (!shown) return;
    retries = 0;
    stale = false;
    if (!(S.view && askedFor === S.view.view_id)) schedule();
    draw();
  });
  // Tokens are read at draw time, so a skin/accent change is one redraw.
  document.addEventListener('winnow:appearance', () => { if (shown) draw(); });
  window.addEventListener('resize', () => { if (shown) draw(); });
  syncHistogramPanel();
}

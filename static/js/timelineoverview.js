/* The Timeline's overview strip: the Timeline's lines over time, above
   the list.

   The list shows a screenful of lines out of what can be hundreds of
   thousands, and nothing on it said where in the case that screenful sat,
   how the rest was spread, or that a burst of 160 failed logons overnight
   was most of what was tagged. The strip answers those at a glance and is
   the way to move around in time:

   - one bar per stretch of time (the server picks the width:
     Store.timeline_overview, HISTOGRAM_BUCKETS), split by each line's
     first tag in the tag's colour (Tags) or by table in the Type pill's
     colour (Tables);
   - a band behind the bars, and a thumb above them, where the lines on
     screen are, and a line through the plot at the selected line;
   - a click on a bar takes the list to that bar's first line and selects
     it; a drag across bars sets the Timeline's timeframe to that stretch
     (the ⏱ Timeframe dialog's range, timeline.js), said by the chip in
     the header with ✕ to undo it;
   - in Tables, a chip per table with its count, which switches that
     table off the Timeline (S.timeline.sourcesOff, the tag chips' rule:
     the tables OFF are remembered, so one tagged later is on).

   Bars are square-root scaled: a burst of a few hundred lines beside
   single findings is the usual shape of a case, and on a linear scale the
   findings were a pixel tall. The hover says each bar's real count.

   Same canvas technique as the histogram strip (histogram.js): CSS tokens
   read at paint time, so a skin change is one repaint (winnow:appearance),
   and every mark painted from data already on the client — a scroll costs
   no fetch. Its look too, under class names of its own (.tl-ov-*): the
   histogram's tests, and anything else that finds that strip's parts by
   class, would find this one's as well. */
import { calendarRules, drawCalendarRules } from './charts.js';
import { $, ROW_H, api, el } from './core.js';
import { rScroll, vScroll } from './grid.js';
import { S } from './state.js';
import { buildTimeline, renderTimelineRows, selectTimelineLine, setTimelineTimeRange, timelineDayLabel,
  timelineKinds, timelineRowAt, timelineTimeRangeOn } from './timeline.js';

export const TIMELINE_OVERVIEW_KEY = 'winnow.timelineOverview';
const HEIGHT = 76;
const LANE = 5;     // the strip above the plot the screen thumb sits in
const AXIS = 15;    // under the plot, for the time labels
const PAD = 4;      // either side, so the first and last bars are whole
const BAR_PX = 6;   // about one bar per this many pixels of strip

let ui = null;
let brush = null;   // {x0, x1} while a drag is under way
let down = null;    // where the mouse went down on the canvas, until it comes up
let seq = 0;
let marksKey = '';
let rules = null;   // the day/week rules last painted (calendarRules)
let followRaf = null;

/* ------------------------------------------------------------ prefs */

function prefs() {
  try { return JSON.parse(localStorage.getItem(TIMELINE_OVERVIEW_KEY) || '{}') || {}; } catch { return {}; }
}
function savePrefs(patch) {
  try { localStorage.setItem(TIMELINE_OVERVIEW_KEY, JSON.stringify({ ...prefs(), ...patch })); } catch { /* private window */ }
}
export function timelineOverviewOpen() { return prefs().open !== false; }
export function timelineOverviewBy() { return prefs().by === 'tables' ? 'tables' : 'tags'; }

export function toggleTimelineOverview(on = !timelineOverviewOpen()) {
  savePrefs({ open: on });
  renderTimelineOverview();
  if (on) loadTimelineOverview();
}

function setBy(by) {
  if (by === timelineOverviewBy()) return;
  savePrefs({ by });
  loadTimelineOverview();
}

/* ------------------------------------------------------------- time */

const pad2 = (n) => String(n).padStart(2, '0');

/* A Timeline timestamp ('YYYY-MM-DD HH:MM:SS', what build_timeline
   orders by) as epoch seconds, read as the server's unixepoch() reads it:
   UTC, whole seconds. setUTCFullYear rather than Date.UTC, which reads
   years 0–99 as 1900–1999. */
export function timelineEpoch(ts) {
  const m = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)/.exec(ts || '');
  if (!m) return null;
  const d = new Date(0);
  d.setUTCFullYear(+m[1], +m[2] - 1, +m[3]);
  d.setUTCHours(+m[4], +m[5], +m[6], 0);
  const t = d.getTime();
  return Number.isNaN(t) ? null : Math.floor(t / 1000);
}

/* The other way: the shape the timeframe's bounds are written in. */
export function timelineIso(epoch) {
  const d = new Date(epoch * 1000);
  return `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} `
    + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
}

const dayOf = (epoch) => timelineDayLabel(timelineIso(epoch));
const clock = (epoch) => {
  const s = timelineIso(epoch);
  return s.endsWith(':00') ? s.slice(11, 16) : s.slice(11, 19);
};

const barUnit = (w) => (w % 86400 === 0 ? `${w / 86400} d` : w % 3600 === 0 ? `${w / 3600} h`
  : w % 60 === 0 ? `${w / 60} min` : `${w} s`);

/* ------------------------------------------------------------- data */

/* The bars, for the Timeline as it is now. Asked after every build
   (buildTimeline) and when the split changes; nothing is asked while the
   strip is closed. The answer is kept with the view it describes, so a
   chart about a view since replaced is drawn faded and takes no clicks
   (fresh). */
export async function loadTimelineOverview() {
  const v = S.timeline.view;
  const mine = ++seq;
  if (!ui || !timelineOverviewOpen() || !v || !v.view_id || !v.row_count) {
    S.timeline.overview = null;
    renderTimelineOverview();
    return;
  }
  ui.box.setAttribute('aria-busy', 'true');
  renderTimelineOverview();
  const by = timelineOverviewBy();
  const width = ui.canvas.clientWidth || ui.box.clientWidth || 600;
  const buckets = Math.max(20, Math.min(400, Math.floor((width - 2 * PAD) / BAR_PX)));
  let data = null;
  try {
    data = await api(`/api/timeline_overview?view_id=${encodeURIComponent(v.view_id)}`
      + `&by=${by === 'tables' ? 'sources' : 'tags'}&buckets=${buckets}`);
  } catch { /* a 409 is a rebuild under way; its own ask follows */ }
  if (mine !== seq) return;
  ui.box.removeAttribute('aria-busy');
  S.timeline.overview = data ? { ...data, view_id: v.view_id, by } : null;
  renderTimelineOverview();
}

function fresh() {
  const o = S.timeline.overview;
  const v = S.timeline.view;
  return !!(o && v && o.view_id === v.view_id && o.by === timelineOverviewBy());
}

/* Whether the strip has anything to say: lines to chart, or a narrowing
   (a timeframe, a table off) that left no lines — the Tables view is
   where a table switched off is put back. */
function wanted() {
  const v = S.timeline.view;
  return !!((v && v.row_count) || timelineTimeRangeOn() || S.timeline.sourcesOff.size);
}

/* ------------------------------------------------------------ chrome */

function buildChrome(box) {
  const head = el('div', 'tl-ov-head');
  const title = el('span', 'tl-ov-title', 'Overview');
  const views = el('span', 'tl-ov-views');
  views.setAttribute('role', 'group');
  views.setAttribute('aria-label', 'Colour the bars by');
  const viewBtn = (by, label, tip) => {
    const b = el('button', 'btn ghost', label);
    b.type = 'button';
    b.title = tip;
    b.onclick = () => setBy(by);
    views.append(b);
    return b;
  };
  const tags = viewBtn('tags', 'Tags', 'Colour each bar by tag: each line under the first tag it carries');
  const tables = viewBtn('tables', 'Tables',
    'Colour each bar by table, with a chip per table that takes it off the Timeline');
  const legend = el('span', 'tl-ov-legend');
  const info = el('span', 'tl-ov-info');
  head.append(title, views, legend, info);
  const canvas = el('canvas', 'tl-ov-canvas');
  canvas.style.height = `${HEIGHT}px`;
  const hint = el('div', 'note-status tl-ov-hint',
    'Click a bar to go to its first line · drag across bars to show only that stretch of time');
  const empty = el('div', 'note-status tl-ov-empty');
  empty.hidden = true;
  box.append(head, canvas, hint, empty);
  ui = { box, head, legend, info, canvas, hint, empty, views: { tags, tables } };
}

/* Everything but the bars: which split is pressed, the legend or the
   table chips, the line under the bars. Then the bars. */
export function renderTimelineOverview() {
  if (!ui) return;
  const open = timelineOverviewOpen();
  const btn = $('btnTimelineOverview');
  if (btn) btn.setAttribute('aria-pressed', String(open));
  ui.box.hidden = !open || !wanted();
  if (ui.box.hidden) return;
  const by = timelineOverviewBy();
  for (const [k, b] of Object.entries(ui.views)) b.setAttribute('aria-pressed', String(by === k));
  ui.box.classList.toggle('is-outdated', !!S.timeline.overview && !fresh());
  renderLegend(by);
  const o = fresh() ? S.timeline.overview : null;
  const v = S.timeline.view;
  if (!v || !v.row_count) {
    showEmpty(timelineTimeRangeOn() ? 'No tagged rows in this timeframe.' : 'No tagged rows on the Timeline.');
    ui.info.textContent = '';
    return;
  }
  const shown = o || S.timeline.overview;
  if (shown && !shown.buckets.length) {
    showEmpty('None of these lines has a timestamp to place on the strip.');
    ui.info.textContent = '';
    return;
  }
  if (!shown) {
    showEmpty('Loading…');
    ui.info.textContent = '';
    return;
  }
  ui.empty.hidden = true;
  ui.canvas.hidden = false;
  ui.hint.hidden = false;
  ui.info.textContent = `${barUnit(shown.width)} bars`
    + (shown.undated ? ` · ${shown.undated.toLocaleString()} with no time` : '');
  ui.info.title = shown.undated
    ? `${shown.undated.toLocaleString()} line${shown.undated === 1 ? ' has' : 's have'} no timestamp: `
      + 'they are at the end of the list, and on no bar'
    : '';
  paintTimelineOverview();
}

function showEmpty(text) {
  ui.canvas.hidden = true;
  ui.hint.hidden = true;
  ui.empty.textContent = text;
  ui.empty.hidden = false;
}

/* Tags: the tags on the bars, each with how many lines it heads. Tables:
   a chip per table that has tagged rows, pressed while it is on the
   Timeline — the tag chips' look, since it is the same question asked of
   tables. */
function renderLegend(by) {
  ui.legend.replaceChildren();
  if (by === 'tables') {
    const kinds = timelineKinds();
    const per = (S.timeline.view && S.timeline.view.per_source) || {};
    for (const s of S.sources) {
      const k = kinds.get(s.id);
      if (!k || !s.tagged_row_count) continue;
      const on = !S.timeline.sourcesOff.has(s.id);
      const chip = el('button', 'tag-chip tl-table-chip');
      chip.type = 'button';
      chip.dataset.sourceId = s.id;
      chip.setAttribute('aria-pressed', String(on));
      // The pressed border in the table's colour, as the tag chips take
      // their tag's (renderTimelineTagFilter).
      chip.style.color = on ? `var(--series-${k.slot})` : '';
      const sw = el('span', 'swatch');
      sw.style.background = `var(--series-${k.slot})`;
      chip.append(sw, el('span', null, k.host ? `${k.kind} · ${k.host}` : k.kind));
      if (on) chip.append(el('span', 'n', (per[s.id] || 0).toLocaleString()));
      chip.title = (on ? 'On the timeline. Click to take this table off it.'
        : 'Off the timeline. Click to put this table back on it.') + `\n${s.name}`;
      chip.onclick = () => {
        if (on) S.timeline.sourcesOff.add(s.id); else S.timeline.sourcesOff.delete(s.id);
        buildTimeline();
      };
      ui.legend.append(chip);
    }
    return;
  }
  const o = fresh() ? S.timeline.overview : null;
  if (!o) return;
  const per = {};
  for (const b of o.buckets) for (const [k, n] of Object.entries(b.split)) per[k] = (per[k] || 0) + n;
  for (const t of S.tags) {
    const n = per[String(t.id)];
    if (!n) continue;
    const item = el('span', 'tl-ov-item');
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    item.append(sw, el('span', null, t.name), el('span', 'tl-chip-n', n.toLocaleString()));
    item.title = `${n.toLocaleString()} line${n === 1 ? '' : 's'} whose first tag is ${t.name}`;
    ui.legend.append(item);
  }
  if (per.ctx) {
    const item = el('span', 'tl-ov-item tl-ov-ctx');
    item.append(el('span', 'swatch'), el('span', null, 'Untagged'), el('span', 'tl-chip-n', per.ctx.toLocaleString()));
    item.title = `${per.ctx.toLocaleString()} untagged line${per.ctx === 1 ? '' : 's'} around a finding`;
    ui.legend.append(item);
  }
}

/* ----------------------------------------------------------- canvas */

const tokens = () => {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, dflt) => cs.getPropertyValue(name).trim() || dflt;
  return {
    accent: v('--accent', '#d9a441'), dim: v('--dim', '#888'), line: v('--line', '#333'),
    line2: v('--line-2', '#444'), text: v('--text', '#ddd'), panel: v('--panel', '#191d23'),
    sel: v('--sel', 'rgba(255,255,255,.1)'), mono: v('--mono', 'monospace'), cs,
  };
};

/* The x axis: [start, the end of the last bar), across the canvas less
   PAD each side. */
function span(o) {
  const last = o.buckets[o.buckets.length - 1].b;
  return { t0: o.start, t1: o.start + (last + 1) * o.width };
}

/* The day or week rules as last painted — {unit: 'day' | 'week' | null,
   times: [epoch…]} — or null with no chart up. For tests/ui, which
   cannot read a dashed line's meaning off the canvas. */
export function timelineOverviewRules() {
  if (!rules || !S.timeline.overview) return null;
  return { unit: rules.unit, times: [...rules.times] };
}

/* Where a time sits on the strip, in CSS pixels from the canvas's left
   edge — also what tests/ui read the bars' places from. Null with no
   chart up. */
export function timelineOverviewX(epoch) {
  const o = fresh() ? S.timeline.overview : null;
  if (!o || !o.buckets.length || !ui) return null;
  const s = span(o);
  const w = ui.canvas.clientWidth || 1;
  return PAD + ((epoch - s.t0) / (s.t1 - s.t0)) * (w - 2 * PAD);
}
function timeAt(x) {
  const o = S.timeline.overview;
  const s = span(o);
  const w = ui.canvas.clientWidth || 1;
  return s.t0 + ((x - PAD) / (w - 2 * PAD)) * (s.t1 - s.t0);
}

/* The lines on screen as a stretch of time, and the selected line's time:
   both from lines the list already holds. */
function screenMarks() {
  const v = S.timeline.view;
  const body = $('timelineBody');
  if (!v || !v.row_count) return { band: null, sel: null };
  const virt = vScroll(body, v.row_count);
  const a = timelineRowAt(Math.floor(virt / ROW_H));
  const b = timelineRowAt(Math.max(0, Math.min(v.row_count - 1, Math.floor((virt + body.clientHeight) / ROW_H) - 1)));
  const ea = a ? timelineEpoch(a.ts) : null;
  const eb = b ? timelineEpoch(b.ts) : null;
  const sel = S.timeline.sel && S.timeline.sel.ts ? timelineEpoch(S.timeline.sel.ts) : null;
  return { band: ea != null && eb != null ? [Math.min(ea, eb), Math.max(ea, eb)] : null, sel };
}

/* The categories a bar is split into, bottom up, and each one's colour:
   tags in tag order, tables in the sidebar's order. */
function layers(by, t) {
  if (by === 'tables') {
    const kinds = timelineKinds();
    return S.sources.filter((s) => kinds.has(s.id)).map((s) => [String(s.id),
      t.cs.getPropertyValue(`--series-${kinds.get(s.id).slot}`).trim() || t.dim]);
  }
  // The untagged rows around a finding (timelinepane.js) on top, grey.
  return [...S.tags.map((tg) => [String(tg.id), tg.color]), ['ctx', t.dim]];
}

export function paintTimelineOverview() {
  if (!ui || ui.box.hidden || ui.canvas.hidden) return;
  const o = S.timeline.overview;
  if (!o || !o.buckets.length) return;
  const current = fresh();
  const canvas = ui.canvas;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(HEIGHT * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, HEIGHT);
  const t = tokens();
  ctx.font = `10px ${t.mono}`;
  const s = span(o);
  const top = LANE + 3;
  const bottom = HEIGHT - AXIS;
  const plotH = bottom - top;
  const xOf = (sec) => PAD + ((sec - s.t0) / (s.t1 - s.t0)) * (w - 2 * PAD);
  const bw = Math.max(1, (o.width / (s.t1 - s.t0)) * (w - 2 * PAD) - 1);
  const max = Math.max(...o.buckets.map((b) => b.n));

  const ticks = axisTicks(s, w);
  // Midnights as faint rules when the bars are finer than a day, so a
  // night reads as one; the start of each week once they are not, or
  // the days are too close to tell apart. Shared with the histogram
  // strip over the grid (charts.js calendarRules).
  rules = calendarRules(s.t0, s.t1, o.width, (w - 2 * PAD) / (s.t1 - s.t0));
  drawCalendarRules(ctx, rules.times, xOf, top, bottom, t.line2);

  // The lines on screen, lit behind the bars (over them it would tint the
  // colours the split is read by), and as a thumb in the lane above.
  const m = current ? screenMarks() : { band: null, sel: null };
  let band = null;
  if (m.band) {
    let x0 = xOf(m.band[0]);
    let x1 = xOf(m.band[1] + 1);
    if (x1 - x0 < 3) { const mid = (x0 + x1) / 2; x0 = mid - 1.5; x1 = mid + 1.5; }
    band = [Math.round(x0), Math.round(x1)];
    ctx.save();
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = t.text;
    ctx.fillRect(band[0], top, band[1] - band[0], plotH);
    ctx.restore();
  }

  const order = layers(o.by, t);
  const known = new Set(order.map(([k]) => k));
  for (const b of o.buckets) {
    const x = xOf(s.t0 + b.b * o.width);
    const full = Math.max(2, Math.sqrt(b.n / max) * plotH);
    let y = bottom;
    const parts = [...order.filter(([k]) => b.split[k]),
      ...Object.keys(b.split).filter((k) => !known.has(k)).map((k) => [k, t.dim])];
    for (const [k, colour] of parts) {
      // Proportional to this bar's own height, so the parts add up to it.
      const h = (b.split[k] / b.n) * full;
      ctx.fillStyle = colour;
      ctx.fillRect(x, y - h, bw, h);
      y -= h;
    }
  }
  ctx.strokeStyle = t.line;
  ctx.beginPath(); ctx.moveTo(0, bottom + 0.5); ctx.lineTo(w, bottom + 0.5); ctx.stroke();

  ctx.fillStyle = t.dim;
  let lastRight = -Infinity;
  for (const tk of ticks) {
    const x = xOf(tk.t);
    const tw = ctx.measureText(tk.label).width;
    const left = Math.min(Math.max(2, x + 3), w - tw - 2);
    if (left < lastRight + 12) continue;
    ctx.fillText(tk.label, left, HEIGHT - 4);
    lastRight = left + tw;
  }

  ctx.fillStyle = t.text;
  if (band) ctx.fillRect(band[0], 0, band[1] - band[0], LANE - 1);
  // The selected line, through the plot, on a halo of the panel colour so
  // a 1px line over a bar still shows (the histogram's row cursor).
  if (m.sel != null && m.sel >= s.t0 && m.sel < s.t1) {
    const x = Math.round(xOf(m.sel));
    ctx.fillStyle = t.panel;
    ctx.fillRect(x - 1, top, 3, plotH);
    ctx.fillStyle = t.accent;
    ctx.fillRect(x, top, 1, plotH);
  }

  if (brush) {
    const x0 = Math.min(brush.x0, brush.x1);
    const x1 = Math.max(brush.x0, brush.x1);
    ctx.save();
    ctx.globalAlpha = 0.28;
    ctx.fillStyle = t.sel;
    ctx.fillRect(x0, top, x1 - x0, plotH);
    ctx.restore();
    ctx.strokeStyle = t.accent;
    ctx.strokeRect(x0 + 0.5, top + 0.5, x1 - x0, plotH);
    const r = snapped();
    const label = `${timelineIso(r.start)} — ${timelineIso(r.end)}`;
    ctx.fillStyle = t.text;
    const lw = ctx.measureText(label).width;
    ctx.fillText(label, Math.max(2, Math.min(w - lw - 2, (x0 + x1) / 2 - lw / 2)), top + 10);
  }
  marksKey = marksKeyOf(m, xOf);
}

function marksKeyOf(m, xOf) {
  return `${m.band ? `${Math.round(xOf(m.band[0]))}-${Math.round(xOf(m.band[1] + 1))}` : ''}|`
    + `${m.sel != null ? Math.round(xOf(m.sel)) : ''}`;
}

/* Called by renderTimelineRows, so every scroll frame and page landing:
   one look per frame, and a repaint only when the band or the selection
   mark would land on other pixels. */
export function timelineOverviewFollow() {
  if (followRaf || !ui || ui.box.hidden || ui.canvas.hidden || !fresh()) return;
  followRaf = requestAnimationFrame(() => {
    followRaf = null;
    if (!fresh() || ui.canvas.hidden) return;
    const o = S.timeline.overview;
    const s = span(o);
    const w = ui.canvas.clientWidth || 1;
    const xOf = (sec) => PAD + ((sec - s.t0) / (s.t1 - s.t0)) * (w - 2 * PAD);
    if (marksKeyOf(screenMarks(), xOf) !== marksKey) paintTimelineOverview();
  });
}

/* Labels under the plot: clock times when the strip spans under two
   days (with the day at each midnight), days under a few months,
   months beyond. The step is the smallest that leaves room for a label. */
const CLOCK_STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600];
const DAY_STEPS = [1, 2, 7, 14];
const MONTH_STEPS = [1, 2, 3, 6, 12, 24, 60, 120];
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function axisTicks(s, w) {
  const room = Math.max(1, Math.floor((w - 2 * PAD) / 90));
  const len = s.t1 - s.t0;
  const out = [];
  const clockStep = CLOCK_STEPS.find((st) => len / st <= room);
  if (clockStep && len <= 2 * 86400) {
    out.push({ t: s.t0, label: `${dayOf(s.t0)} ${clock(s.t0)}` });
    for (let tt = Math.ceil(s.t0 / clockStep) * clockStep; tt < s.t1; tt += clockStep) {
      if (tt === s.t0) continue;
      out.push({ t: tt, label: tt % 86400 === 0 ? dayOf(tt) : clock(tt) });
    }
    return out;
  }
  const dayStep = DAY_STEPS.find((st) => len / (st * 86400) <= room);
  if (dayStep) {
    const first = Math.ceil(s.t0 / 86400);
    if (first * 86400 - s.t0 > 0) out.push({ t: s.t0, label: dayOf(s.t0) });
    for (let d = first; d * 86400 < s.t1; d += 1) {
      if ((d - first) % dayStep === 0) out.push({ t: d * 86400, label: dayOf(d * 86400) });
    }
    return out;
  }
  const months = len / (30.44 * 86400);
  const monthStep = MONTH_STEPS.find((st) => months / st <= room) || MONTH_STEPS[MONTH_STEPS.length - 1];
  const d0 = new Date(s.t0 * 1000);
  let y = d0.getUTCFullYear();
  let mo = d0.getUTCMonth();
  for (let i = 0; i < 2000; i++) {
    const d = new Date(0);
    d.setUTCFullYear(y, mo, 1);
    const tt = Math.floor(d.getTime() / 1000);
    if (tt >= s.t1) break;
    if (tt >= s.t0 && (y * 12 + mo) % monthStep === 0) {
      out.push({ t: tt, label: monthStep >= 12 ? String(y) : `${MON[mo]} ${y}` });
    }
    mo += 1;
    if (mo === 12) { mo = 0; y += 1; }
  }
  return out;
}

/* ------------------------------------------------------- the pointer */

/* The bar under x, or the nearest one after it (a click in a quiet
   stretch goes on to the next thing that happened), or the last bar. */
function barAt(x) {
  const o = S.timeline.overview;
  const b = Math.floor((timeAt(x) - o.start) / o.width);
  return o.buckets.find((k) => k.b === b) || o.buckets.find((k) => k.b > b) || o.buckets[o.buckets.length - 1];
}

/* The drag's stretch, both ends rounded outwards to a unit picked from
   the drag's own width — the histogram's rule (snapUnit there): rounding
   to the bar width made a stretch inside one bar impossible to pick. */
const SNAP_UNITS = [1, 5, 15, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400];
function snapped() {
  const o = S.timeline.overview;
  const w = ui.canvas.clientWidth || 1;
  const t0 = timeAt(Math.max(PAD, Math.min(brush.x0, brush.x1)));
  const t1 = timeAt(Math.min(w - PAD, Math.max(brush.x0, brush.x1)));
  const want = Math.max(1, (t1 - t0) / 12);
  let unit = 1;
  for (const u of SNAP_UNITS) if (u <= want && u <= o.width) unit = u;
  return { start: Math.floor(t0 / unit) * unit, end: Math.max(Math.ceil(t1 / unit) * unit, Math.floor(t0 / unit) * unit + unit) };
}

/* A click on a bar: the list to that bar's first line, at the top of the
   list, and that line selected. */
export function goToTimelineBar(bar) {
  const v = S.timeline.view;
  if (!v || !v.row_count) return;
  const body = $('timelineBody');
  body.scrollTop = rScroll(body, v.row_count, bar.first * ROW_H);
  selectTimelineLine(bar.first);
  renderTimelineRows();
  body.focus({ preventScroll: true });
}

function barTitle(bar) {
  const o = S.timeline.overview;
  const t0 = o.start + bar.b * o.width;
  const name = o.by === 'tables'
    ? (k) => { const kk = timelineKinds().get(Number(k)); return kk ? (kk.host ? `${kk.kind} · ${kk.host}` : kk.kind) : k; }
    : (k) => (k === 'ctx' ? 'Untagged'
      : (S.tags.find((tg) => String(tg.id) === k) || {}).name || 'a tag since deleted');
  return `${timelineIso(t0)} → ${timelineIso(t0 + o.width - 1)} · ${bar.n.toLocaleString()} line${bar.n === 1 ? '' : 's'}\n`
    + Object.entries(bar.split).map(([k, n]) => `${name(k)}: ${n.toLocaleString()}`).join('\n');
}

/* DOM wiring, called once by main.js. */
export function wireTimelineOverview() {
  buildChrome($('timelineOverview'));
  $('btnTimelineOverview').onclick = () => toggleTimelineOverview();
  const canvas = ui.canvas;
  const xIn = (e) => e.clientX - canvas.getBoundingClientRect().left;
  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || !fresh()) return;
    down = xIn(e);
    e.preventDefault();
  });
  canvas.addEventListener('mousemove', (e) => {
    const x = xIn(e);
    if (down != null && Math.abs(x - down) > 3) {
      brush = { x0: down, x1: x };
      paintTimelineOverview();
      return;
    }
    const o = S.timeline.overview;
    if (!fresh() || !o.buckets.length) { canvas.title = ''; return; }
    const b = Math.floor((timeAt(x) - o.start) / o.width);
    const bar = o.buckets.find((k) => k.b === b);
    canvas.title = bar ? barTitle(bar) : '';
  });
  // On the window, so a drag let go of past the strip's edge still ends.
  window.addEventListener('mouseup', (e) => {
    if (down == null) return;
    const at = down;
    down = null;
    if (brush) {
      brush.x1 = xIn(e);
      const wide = Math.abs(brush.x1 - brush.x0) > 3;
      const r = wide ? snapped() : null;
      brush = null;
      if (r) {
        // The bars' edges as a timeframe, which keeps both its ends: the
        // last whole second of the last bar, so a line at the next bar's
        // first second is not taken in with it.
        setTimelineTimeRange(timelineIso(r.start), timelineIso(r.end - 1));
        return;
      }
      paintTimelineOverview();
    }
    if (fresh()) goToTimelineBar(barAt(at));
  });
  window.addEventListener('resize', () => { if (S.activeTab === 'timeline') paintTimelineOverview(); });
  document.addEventListener('winnow:appearance', () => { if (S.activeTab === 'timeline') paintTimelineOverview(); });
  renderTimelineOverview();
}

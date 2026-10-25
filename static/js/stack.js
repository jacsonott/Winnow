/* Stack / long-tail analysis — least-frequency-of-occurrence, the DFIR
   staple: pick a column, see value → count sorted RAREST first, because
   rare is where evil hides. A modal over the current view's counts
   (Store.group_summary, which already aggregates a filtered view). Click
   a bar to filter the grid to that value. See docs/design/analysis-suite.md.

   In the side-list frame: the table's columns down the left, so stacking
   the next column is a click rather than a trip back to its header. The
   rest of the dialog is the chart — one horizontal bar per value against
   a shared scale, its count at the bar's end and its share of the rows on
   the right — because the shape of the column is the answer: one value
   carrying the table and a tail of ones, or no tail at all.

   Bars are elements rather than a canvas (charts.js drawBars, which the
   dashboard's widgets draw with): a stack runs to hundreds of values, and
   as elements each bar is focusable, says its full value when the label
   is cut, takes the row menu, and scrolls without a canvas the height of
   every bar. */

import { api, el, toast } from './core.js';
import { filterByValue } from './filters.js';
import { writeClipboardText } from './grouping.js';
import { S } from './state.js';
import { keepingFocus, plural } from './tables.js';
import { tableMenuSeg, tableMenuStat } from './timeframe.js';
import { contextMenu, dropdownMenu, modal, sideRail } from './ui.js';

/* How many bars the chart draws, of the up to LIMIT values the server
   counts; past either, the chart's foot says so. */
const SHOWN = 500;
const LIMIT = 1000;

/* A bar's row is as tall as the chart's height shared out between the
   bars, within these: a column of four values draws four broad bars, not
   four slivers at the top of an empty box, and a long tail stays dense
   enough to read down. */
const ROW_MIN = 26;
const ROW_MAX = 44;

/* A round step for the scale's gridlines: 1, 2 or 5 times a power of ten,
   the one nearest a quarter of `max`, so three to six lines fall on
   numbers worth reading. The scale itself ends at `max` — the longest bar
   spans the chart — rather than at the next line past it. */
export function scaleStep(max) {
  const raw = Math.max(1, max) / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const near = (s) => Math.abs(Math.log(s / raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).reduce((a, b) => (near(b) < near(a) ? b : a));
  return Math.max(1, Math.round(step));
}

export function openStack(column) {
  if (!S.view || !S.view.view_id) { toast('Open a table first'); return; }
  const state = { column, direction: 'asc', groups: [], truncated: false, rows: 0, query: '', seq: 0, error: '', note: '' };

  modal('Stack', (b) => {
    b.dataset.kind = 'stack';
    const cols = S.columns.filter((c) => !(S.layout[c.name] || {}).hidden || c.name === column);
    // A datetime column stacks by calendar day (Store.group_summary).
    const typeLabel = { datetime: 'datetime · by day', int: 'number', float: 'number', number: 'number' };
    const rail = sideRail({
      entries: () => cols.map((c) => ({ key: c.name, label: c.name, group: 'Columns', hint: S.filters[c.name] ? `filtered: ${S.filters[c.name]}` : (typeLabel[c.type] || 'text') })),
      groups: ['Columns'], selected: () => state.column,
      onPick: (entry) => { state.column = entry.key; state.query = ''; filter.value = ''; rail.sync(); load(); },
      pinsKey: 'stack', label: 'Columns',
    });
    rail.search.placeholder = 'Find a column…';

    const pane = el('div', 'side-pane list-pane stack-pane');
    const summary = el('div', 'mb-summary');
    const stats = el('div', 'mb-stats');
    const order = tableMenuSeg([
      ['asc', 'Rarest first', 'Where anomalies surface: one-off command lines, a single logon from an odd host'],
      ['desc', 'Most common', 'The bulk of the column, biggest first'],
    ], () => state.direction, (key) => { state.direction = key; order.paint(); load(); });
    summary.append(stats, order.seg);
    const top = el('div', 'tm-cols-top');
    const filter = el('input', 'side-search');
    filter.type = 'search';
    filter.placeholder = 'Filter values…';
    filter.setAttribute('aria-label', 'Filter values');
    const count = el('span', 'tm-cols-count');
    top.append(filter, count);

    const chart = el('div', 'side-preview stack-chart');
    const head = el('div', 'stack-head');
    const axis = el('div', 'stack-axis');
    axis.setAttribute('aria-hidden', 'true');
    head.append(el('span', 'stack-head-label', 'Value'), axis, el('span', 'stack-head-share', 'Share'));
    const scroller = el('div', 'stack-scroll');
    const bars = el('div', 'stack-bars');
    bars.setAttribute('role', 'list');
    bars.setAttribute('aria-label', 'Values by count');
    scroller.append(bars);
    const foot = el('div', 'side-preview-foot stack-foot');
    const readout = el('p', 'fb-help stack-readout');
    readout.setAttribute('aria-live', 'polite');
    foot.append(readout);
    chart.append(head, scroller, foot);
    pane.append(summary, top, chart);

    const panes = el('div', 'side-panes list-panes stack-panes');
    panes.append(rail.rail, pane);
    b.append(panes);

    const shown = (v) => (v == null || v === '' ? '(empty)' : String(v));
    const pick = (g, opts) => { document.getElementById('modal').hidden = true; filterByValue(state.column, g.value ?? '', opts); };
    const valueMenu = (g) => [
      { label: 'Filter to this value', onclick: () => pick(g) },
      { label: 'Exclude this value', onclick: () => pick(g, { exclude: true }) },
      '-',
      { label: 'Copy value', onclick: () => writeClipboardText(Promise.resolve(g.value == null ? '' : String(g.value)), 'Copied the value') },
    ];
    const share = (n) => {
      const pct = state.rows ? (n / state.rows) * 100 : 0;
      return pct >= 1 ? `${Math.round(pct)}%` : pct > 0 ? '<1%' : '';
    };
    const say = (g) => `${shown(g.value)} — ${plural(g.count, 'row')}${share(g.count) ? ', ' + share(g.count) : ''}`;
    const hint = () => 'Click a bar to filter the grid to its value. Right-click a bar, or its ⋯, for more.';

    function bar(g, scaleMax) {
      const r = el('div', 'stack-row');
      r.setAttribute('role', 'listitem');
      r.dataset.value = shown(g.value);
      r.dataset.count = String(g.count);
      const go = el('button', 'stack-bar-btn');
      go.type = 'button';
      go.dataset.role = 'bar';
      go.title = `${shown(g.value)}\n${g.count.toLocaleString()} rows — click to filter ${state.column} to it`;
      go.setAttribute('aria-label', say(g));
      const label = el('span', 'stack-label' + (g.value == null || g.value === '' ? ' empty' : ''), shown(g.value));
      const track = el('span', 'stack-track');
      const fill = el('span', 'stack-fill' + (g.count === 1 ? ' once' : ''));
      fill.style.width = `${(g.count / scaleMax) * 100}%`;
      const n = el('span', 'stack-count', g.count.toLocaleString());
      track.append(fill, n);
      go.append(label, track, el('span', 'stack-share', share(g.count)));
      go.onclick = () => pick(g);
      const onIn = () => { readout.textContent = say(g); };
      go.addEventListener('mouseenter', onIn);
      go.addEventListener('focus', onIn);
      const more = el('button', 'btn ghost row-more stack-more', '⋯');
      more.type = 'button';
      more.dataset.role = 'more';
      more.setAttribute('aria-label', `Actions for ${shown(g.value)}`);
      more.setAttribute('aria-haspopup', 'menu');
      more.onclick = () => dropdownMenu(more, valueMenu(g));
      r.append(go, more);
      r.addEventListener('contextmenu', (e) => { e.preventDefault(); contextMenu(e, valueMenu(g)); });
      return r;
    }

    /* The scale: rounded gridlines across every bar, numbered along the
       head. Shared by the whole column, so a filter over the values keeps
       each bar the length it had. */
    function paintScale(max) {
      const step = scaleStep(max);
      chart.style.setProperty('--stack-grid', `${(step / max) * 100}%`);
      const lines = Math.floor(max / step);
      axis.replaceChildren(...Array.from({ length: lines + 1 }, (_, i) => {
        const t = el('span', 'stack-tick', (i * step).toLocaleString());
        t.style.left = `${((i * step) / max) * 100}%`;
        return t;
      }));
      return max;
    }

    /* The value gutter is as wide as the longest value drawn, measured in
       the chart's own face, and never more than two fifths of the chart:
       past that a command line is cut, and its bar's title says it all. */
    const measure = document.createElement('canvas').getContext('2d');
    function gutter(groups) {
      const face = getComputedStyle(bars).getPropertyValue('--stack-face').trim() || '12px monospace';
      measure.font = face;
      const need = Math.max(60, ...groups.slice(0, SHOWN).map((g) => measure.measureText(shown(g.value)).width)) + 14;
      chart.style.setProperty('--stack-label', `${Math.round(Math.min(need, Math.max(120, chart.clientWidth * 0.4)))}px`);
    }

    function size(n) {
      const room = scroller.clientHeight || 400;
      const row = Math.max(ROW_MIN, Math.min(ROW_MAX, Math.floor(room / Math.max(1, n))));
      chart.style.setProperty('--stack-row', `${row}px`);
    }

    function paint() {
      const q = state.query.trim().toLowerCase();
      const hits = state.groups.filter((g) => !q || shown(g.value).toLowerCase().includes(q));
      const n = state.groups.length;
      stats.replaceChildren(tableMenuStat(state.truncated ? `${n.toLocaleString()}+` : n, n === 1 ? 'distinct value' : 'distinct values'),
        tableMenuStat(state.rows, 'rows counted'),
        tableMenuStat(state.groups.filter((g) => g.count === 1).length, 'seen once'));
      count.textContent = q ? `${hits.length} of ${plural(n, 'value')}` : plural(n, 'value');
      const scaleMax = paintScale(Math.max(1, ...state.groups.map((g) => g.count)));
      const drawn = hits.slice(0, SHOWN);
      gutter(drawn);
      size(drawn.length);
      keepingFocus(bars, () => bars.replaceChildren(...(state.error ? [el('p', 'fb-help stack-empty', state.error)]
        : drawn.length ? drawn.map((g) => bar(g, scaleMax))
          : [el('p', 'fb-help stack-empty', n ? `No value matches “${state.query.trim()}”.` : 'Counting…')])));
      const which = state.direction === 'asc' ? 'rarest' : 'most common';
      state.note = state.truncated
        ? `The ${which} ${n.toLocaleString()} values; the column holds more. Filter the grid to narrow it.`
        : hits.length > SHOWN ? `Drawing the ${which} ${SHOWN} of ${hits.length.toLocaleString()}. Filter the values to find the rest.`
          : hint();
      readout.textContent = state.note;
      document.getElementById('modalTitle').textContent = `Stack — ${state.column}`;
    }

    async function load() {
      const seq = ++state.seq;
      chart.classList.add('stale');
      try {
        const res = await api(`/api/group_summary?view_id=${encodeURIComponent(S.view.view_id)}`
          + `&column=${encodeURIComponent(state.column)}&order=count&direction=${state.direction}&limit=${LIMIT}`);
        if (seq !== state.seq) return;
        state.groups = res.groups || [];
        state.truncated = !!res.truncated;
        // Cut short, the counts no longer add up to the view: share of it.
        state.rows = state.truncated ? S.view.row_count : state.groups.reduce((n, g) => n + g.count, 0);
        state.error = '';
      } catch (e) {
        if (seq !== state.seq) return;
        state.groups = [];
        state.error = 'Could not count: ' + e.message;
      }
      chart.classList.remove('stale');
      paint();
      scroller.scrollTop = 0;
    }

    // Up and down walk the bars, as the side list's arrows walk the columns.
    bars.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const all = [...bars.querySelectorAll('.stack-bar-btn')];
      const at = all.indexOf(document.activeElement);
      if (at === -1) return;
      e.preventDefault();
      all[Math.max(0, Math.min(all.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)))].focus();
    });
    // Off the bars, the foot goes back to what it says about the chart.
    bars.addEventListener('mouseleave', () => { readout.textContent = state.note; });
    bars.addEventListener('focusout', (e) => { if (!bars.contains(e.relatedTarget)) readout.textContent = state.note; });
    filter.oninput = () => { state.query = filter.value; paint(); };
    filter.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && filter.value) { e.preventDefault(); e.stopPropagation(); filter.value = ''; state.query = ''; paint(); }
    });
    // The bars' height and the gutter follow the chart's size.
    let seen = '';
    new ResizeObserver(() => {
      const now = `${chart.clientWidth}x${chart.clientHeight}`;
      if (!b.isConnected || now === seen) return;
      seen = now;
      if (state.groups.length) paint();
    }).observe(chart);
    rail.paint();
    // The stacked column can be far down a wide table's list.
    rail.list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'center' });
    paint();
    load();
  }, { wide: 'x', tall: true, focus: (b) => b.querySelector('.stack-pane .side-search') });
}

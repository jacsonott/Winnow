/* Stack / long-tail analysis — least-frequency-of-occurrence, the DFIR
   staple: pick a column, see value → count sorted RAREST first, because
   rare is where evil hides. A modal over the current view's counts
   (Store.group_summary, which already aggregates a filtered view), drawn
   with the shared chart module. Click a bar to filter the grid to that
   value. See docs/design/analysis-suite.md. */

import { drawBars, pickBar } from './charts.js';
import { api, el, toast } from './core.js';
import { filterByValue } from './filters.js';
import { S } from './state.js';
import { modal } from './ui.js';

export function openStack(column) {
  if (!S.view || !S.view.view_id) { toast('Open a table first'); return; }
  const state = { order: 'count', direction: 'asc', rows: [], boxes: [], limit: 60 };

  modal(`Stack — ${column}`, (b) => {
    b.dataset.kind = 'stack';
    const bar = el('div', 'row-actions');
    const rareBtn = el('button', 'btn', 'Rarest first');
    const commonBtn = el('button', 'btn ghost', 'Most common');
    const status = el('span', 'note-status', 'Counting…');
    const setMode = (dir) => {
      state.direction = dir;
      rareBtn.className = dir === 'asc' ? 'btn' : 'btn ghost';
      commonBtn.className = dir === 'desc' ? 'btn' : 'btn ghost';
      load();
    };
    rareBtn.onclick = () => setMode('asc');
    commonBtn.onclick = () => setMode('desc');
    bar.append(rareBtn, commonBtn, status);
    b.append(bar);
    b.append(el('p', 'fb-help',
      'Distinct values in the current view (filters applied), by count. '
      + 'Rarest first is where anomalies surface — one-off command lines, a '
      + 'single logon from an odd host. Click a bar to filter the grid to it.'));

    const wrap = el('div');
    wrap.style.cssText = 'position:relative;height:min(60vh,520px);overflow:auto;border:1px solid var(--line-2)';
    const canvas = el('canvas');
    canvas.style.cssText = 'width:100%;display:block';
    wrap.append(canvas);
    b.append(wrap);

    function paint() {
      canvas.style.height = Math.max(120, state.rows.length * 22) + 'px';
      const r = drawBars(canvas, { rows: state.rows, label: 'value', value: 'count', horizontal: true });
      state.boxes = r.boxes;
    }
    canvas.onclick = (e) => {
      const rect = canvas.getBoundingClientRect();
      const row = pickBar(state.boxes, e.clientX - rect.left, e.clientY - rect.top);
      if (!row) return;
      document.getElementById('modal').hidden = true;
      filterByValue(column, row.value === '(empty)' ? '' : row.value);
    };

    async function load() {
      status.textContent = 'Counting…';
      try {
        const res = await api(`/api/group_summary?view_id=${encodeURIComponent(S.view.view_id)}`
          + `&column=${encodeURIComponent(column)}&order=count&direction=${state.direction}&limit=1000`);
        const groups = (res.groups || []).slice(0, state.limit);
        state.rows = groups.map((g) => ({
          value: g.value == null || g.value === '' ? '(empty)' : String(g.value),
          count: g.count,
        }));
        const total = (res.groups || []).length;
        status.textContent = `${total.toLocaleString()} distinct value${total === 1 ? '' : 's'}`
          + (total > state.limit ? ` · showing ${state.direction === 'asc' ? 'rarest' : 'top'} ${state.limit}` : '');
        paint();
      } catch (e) {
        status.textContent = ''; toast('Stack failed: ' + e.message, 6000);
      }
    }
    new ResizeObserver(paint).observe(wrap);
    setMode('asc');
  }, { wide: true });
}

/* The "Occurrences" tab — pick any value (host, user, hash, IP) and see
   everywhere it appears across every table at once: per-source counts,
   which columns it landed in, a merged time histogram, and a chronological
   evidence stream. It's search-all made visual, focused on one value.
   Reachable from any cell's right-click ("Occurrences of X"), from the
   watchlist, and from this tab's own search box. (The code keeps the
   internal `entity`/`openEntityPivot` names — only the label changed.) See
   docs/design/analysis-suite.md. */

import { drawHistogram } from './charts.js';
import { $, api, el, post, toast } from './core.js';
import { recordTabVisit } from './tabhistory.js';
import { showMainView, syncTabChrome } from './sql.js';
import { openSource, syncTabSelection } from './sources.js';
import { S } from './state.js';

let current = null;   // last pivot result, so onShow can repaint the histogram

/* Public entry point — used by the row menu and the watchlist. Switches to
   the tab and runs the pivot. */
export function openEntityPivot(value) {
  showEntityTab();
  runPivot(String(value == null ? '' : value));
}

async function runPivot(value) {
  const inp = $('entValue');
  if (inp) inp.value = value;
  $('entStatus').textContent = 'Searching…';
  try {
    current = await post('/api/entity/pivot', { value });
    render();
  } catch (e) {
    $('entStatus').textContent = '';
    toast('Pivot failed: ' + e.message, 6000);
  }
}

function render() {
  const r = current;
  const sTotal = r.sources.reduce((a, s) => a + s.count, 0);
  $('entStatus').textContent = sTotal
    ? `${sTotal.toLocaleString()} rows · ${r.sources.length} table${r.sources.length === 1 ? '' : 's'}`
    : 'No matches in any table.';

  const rail = $('entSources');
  rail.replaceChildren();
  for (const s of r.sources) {
    const row = el('div', 'ent-src');
    row.append(el('span', 'ent-src-n', s.source_name),
               el('span', 'ent-src-c', String(s.count)));
    if (s.columns && s.columns.length) {
      row.append(el('div', 'ent-src-cols', 'in ' + s.columns.join(', ')));
    }
    row.title = 'Open this table';
    row.onclick = () => openSource(s.source_id);
    rail.append(row);
  }

  paintHistogram();

  const body = $('entRows');
  body.replaceChildren();
  for (const row of r.rows) {
    const tr = el('div', 'ent-row');
    tr.append(el('span', 'ent-ts', row.ts || '—'),
              el('span', 'ent-src-tag', row.source_name),
              el('span', 'ent-body', row.preview || ''));
    tr.title = 'Open this table';
    tr.onclick = () => openSource(row.source_id);
    body.append(tr);
  }
}

function paintHistogram() {
  const canvas = $('entHist');
  if (!canvas || !current) return;
  const buckets = (current.buckets || []).map(([k, n]) => [k, [n]]);
  if (!buckets.length) { canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height); return; }
  drawHistogram(canvas, { buckets, colors: [getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()] });
}

export function wireEntity() {
  $('tabEntity').onclick = showEntityTab;
  $('entValue').onkeydown = (e) => { if (e.key === 'Enter') runPivot($('entValue').value.trim()); };
  $('entGo').onclick = () => runPivot($('entValue').value.trim());
}

export function resetEntity() { current = null; }

export function showEntityTab() {
  recordTabVisit({ kind: 'page', key: 'entity' });
  S.activeTab = 'entity';
  showMainView('entityview');
  syncTabSelection();
  syncTabChrome();
  if (current) render(); else if ($('entRows')) $('entRows').replaceChildren();
  setTimeout(() => $('entValue') && $('entValue').focus(), 0);
}

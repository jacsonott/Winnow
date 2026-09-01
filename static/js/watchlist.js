/* IOC watchlist — case-level indicators scanned across every table, with
   optional auto-tagging. Indicators live in the case .db (Store.watchlist)
   and are re-scanned on every import (the jobs.js source-done hook calls
   scanWatchlistForSources). Its own page tab: add/import indicators, see
   per-indicator hit counts, and drill into where each landed. See
   docs/design/analysis-suite.md. */

import { $, api, el, post, toast } from './core.js';
import { recordTabVisit } from './tabhistory.js';
import { showMainView, syncTabChrome } from './sql.js';
import { openSource, syncTabSelection } from './sources.js';
import { S } from './state.js';

const KIND_COLOR = { hash: '#7c6cf6', ip: '#39a8e8', domain: '#39e881',
                     filename: '#d9a441', other: '#8a8a90' };
const KIND_LABEL = { hash: 'HASH', ip: 'IP', domain: 'DOMAIN',
                     filename: 'FILE', other: 'IOC' };
let indicators = [];
let selected = null;

/* One CSV cell, RFC-4180-quoted only when it has to be. */
function csvCell(v) {
  const s = v == null ? '' : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/* Client-side file download — no server round trip; the watchlist is
   already in memory. */
function downloadText(text, filename, type) {
  const a = el('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* Fire-and-forget scan of just-imported sources; refresh the tab if it's
   showing. Exported for the jobs.js import hook. */
export async function scanWatchlistForSources(sourceIds) {
  if (!sourceIds || !sourceIds.length) return;
  try {
    const wl = await api('/api/watchlist');
    if (!wl.length) return;            // nothing to scan for
    for (const sid of sourceIds) await post(`/api/watchlist/scan?source_id=${sid}`, {});
    if (S.activeTab === 'watchlist') await load();
  } catch { /* best effort */ }
}

function fillAutoTag() {
  const sel = $('wlAutoTag');
  const keep = sel.value;
  sel.replaceChildren(new Option('no auto-tag', ''));
  for (const t of S.tags || []) sel.append(new Option(t.name, String(t.id)));
  if (keep) sel.value = keep;
}

async function load() {
  try { indicators = await api('/api/watchlist'); } catch { indicators = []; }
  renderList();
  if (selected != null && !indicators.some((i) => i.id === selected)) { selected = null; renderHits(); }
}

function renderList() {
  const list = $('wlList');
  list.replaceChildren();
  if (!indicators.length) {
    list.append(el('div', 'note-status', 'No indicators yet — add one above or import a list. '
      + 'New imports are scanned automatically.'));
    return;
  }
  const withHits = indicators.filter((i) => i.hit_count).length;
  const head = el('div', 'wl-listhead',
    `${indicators.length} indicator${indicators.length === 1 ? '' : 's'}`
    + (withHits ? ` · ${withHits} with hits` : ''));
  list.append(head);
  for (const ind of indicators) {
    const row = el('div', 'wl-row' + (ind.id === selected ? ' active' : ''));
    const kind = el('span', 'wl-kind', KIND_LABEL[ind.kind] || KIND_LABEL.other);
    kind.style.color = KIND_COLOR[ind.kind] || KIND_COLOR.other;
    kind.title = ind.kind;
    const val = el('span', 'wl-val', ind.value);
    if (ind.note) val.title = ind.note;
    const cnt = el('span', 'wl-count' + (ind.hit_count ? ' hot' : ''), String(ind.hit_count));
    cnt.title = `${ind.hit_count} hit${ind.hit_count === 1 ? '' : 's'}`;
    const del = el('button', 'wl-del', '✕');
    del.title = 'Remove this indicator';
    del.onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/watchlist/${ind.id}`, { method: 'DELETE' });
      if (selected === ind.id) selected = null;
      load();
    };
    row.append(kind, val, cnt, del);
    row.onclick = () => { selected = ind.id; renderList(); renderHits(); };
    list.append(row);
  }
}

async function renderHits() {
  const box = $('wlHits');
  box.replaceChildren();
  if (selected == null) { box.append(el('div', 'note-status', 'Select an indicator to see its hits.')); return; }
  const ind = indicators.find((i) => i.id === selected);
  box.append(el('div', 'wl-hits-head', `Hits for "${ind ? ind.value : ''}"`));
  let hits;
  try { hits = await api(`/api/watchlist/hits?watchlist_id=${selected}`); }
  catch (e) { box.append(el('div', 'note-status', e.message)); return; }
  if (!hits.length) { box.append(el('div', 'note-status', 'No hits — scan tables, or this indicator matched nothing.')); return; }
  for (const h of hits) {
    const r = el('div', 'wl-hit');
    r.append(el('span', 'wl-hit-src', h.source_name), el('span', 'wl-hit-rid', `row ${h.rid}`));
    r.title = 'Open this table';
    r.onclick = () => openSource(h.source_id);
    box.append(r);
  }
}

export function wireWatchlist() {
  $('tabWatchlist').onclick = showWatchlistTab;
  $('wlAdd').onclick = async () => {
    const value = $('wlValue').value.trim();
    if (!value) { toast('Enter an indicator'); return; }
    try {
      await post('/api/watchlist', { value, kind: $('wlKind').value,
        auto_tag_id: $('wlAutoTag').value ? Number($('wlAutoTag').value) : null });
      $('wlValue').value = '';
      await post('/api/watchlist/scan', {});   // scan all so the new one gets counts
      await load();
    } catch (e) { toast(e.message, 6000); }
  };
  $('wlScan').onclick = async () => {
    $('wlStatus').textContent = 'Scanning…';
    try { await post('/api/watchlist/scan', {}); await load(); $('wlStatus').textContent = 'Scanned'; }
    catch (e) { $('wlStatus').textContent = ''; toast(e.message, 6000); }
    setTimeout(() => { if ($('wlStatus').textContent === 'Scanned') $('wlStatus').textContent = ''; }, 1500);
  };
  $('wlExport').onclick = () => {
    if (!indicators.length) { toast('No indicators to export'); return; }
    const header = ['value', 'kind', 'note', 'hits'];
    const rows = indicators.map((i) => [i.value, i.kind, i.note || '', i.hit_count]);
    const csv = [header, ...rows].map((r) => r.map(csvCell).join(',')).join('\r\n') + '\r\n';
    downloadText(csv, 'watchlist.csv', 'text/csv');
  };
  $('wlImport').onclick = () => $('wlImportFile').click();
  $('wlImportFile').onchange = async () => {
    const f = $('wlImportFile').files[0];
    if (!f) return;
    const text = await f.text();
    $('wlImportFile').value = '';
    try {
      const r = await post('/api/watchlist/import', { text, kind: $('wlKind').value,
        auto_tag_id: $('wlAutoTag').value ? Number($('wlAutoTag').value) : null });
      await post('/api/watchlist/scan', {});
      await load();
      toast(`${r.added} indicator${r.added === 1 ? '' : 's'} imported`);
    } catch (e) { toast(e.message, 6000); }
  };
}

// Indicators are per-case (server-side); a case switch just refetches.
export function resetWatchlist() { selected = null; indicators = []; }

export async function showWatchlistTab() {
  recordTabVisit({ kind: 'page', key: 'watchlist' });
  S.activeTab = 'watchlist';
  showMainView('watchlistview');
  syncTabSelection();
  syncTabChrome();
  fillAutoTag();
  await load();
}

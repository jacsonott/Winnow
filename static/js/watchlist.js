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
import { modal } from './ui.js';

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
    else await refreshWatchlistBadge();  // new hits while the analyst is elsewhere → dot
  } catch { /* best effort */ }
}

/* The tab's new-hit dot: total hits vs the count last seen (case_settings,
   so it travels with the case). Showing the tab is what marks them seen. */
export async function refreshWatchlistBadge() {
  let b;
  try { b = await api('/api/watchlist/badge'); } catch { return; }
  $('tabWatchlist')?.classList.toggle('has-new-hits', b.total_hits > b.seen);
}

async function markHitsSeen() {
  const total = indicators.reduce((n, i) => n + (i.hit_count || 0), 0);
  $('tabWatchlist')?.classList.remove('has-new-hits');
  try { await post('/api/watchlist/seen', { count: total }); } catch { /* best effort */ }
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
  // Any load while the tab is showing means the analyst is looking at the
  // current counts — keep the seen high-water in step or the next badge
  // poll would light the dot for hits already on screen.
  if (S.activeTab === 'watchlist') markHitsSeen();
}

function renderList() {
  const list = $('wlList');
  list.replaceChildren();
  const withHits = indicators.filter((i) => i.hit_count).length;
  const totalHits = indicators.reduce((n, i) => n + (i.hit_count || 0), 0);
  const sum = $('wlSummary');
  if (sum) {
    sum.textContent = indicators.length
      ? `${indicators.length} indicator${indicators.length === 1 ? '' : 's'}`
        + (withHits ? ` · ${withHits} with hits · ${totalHits.toLocaleString()} hit${totalHits === 1 ? '' : 's'}` : ' · no hits yet')
      : '';
  }
  if (!indicators.length) {
    list.append(el('div', 'note-status', 'No indicators yet — add one above or import a list. '
      + 'New imports are scanned automatically.'));
    return;
  }
  for (const ind of indicators) {
    const row = el('div', 'wl-row' + (ind.id === selected ? ' active' : ''));
    const kind = el('span', 'wl-kind wl-kind-' + (KIND_COLOR[ind.kind] ? ind.kind : 'other'),
      KIND_LABEL[ind.kind] || KIND_LABEL.other);
    kind.title = ind.kind;
    const mainCol = el('span', 'wl-main');
    mainCol.append(el('span', 'wl-val', ind.value));
    if (ind.note) {
      const note = el('span', 'wl-note', ind.note);
      note.title = ind.note;
      mainCol.append(note);
    }
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
    row.append(kind, mainCol, cnt, del);
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

/* Copy indicators in from another recent case — the standing IOC set an
   analyst carries between engagements usually lives in whichever case
   they worked last. The server lists only cases that actually have
   indicators; the import dedupes by value and never carries auto-tags
   (they name the other case's tag ids). */
function openFromCasePicker() {
  modal('Watchlist from a case', async (b) => {
    b.append(el('p', 'fb-help',
      'Copy another case\u2019s indicators into this one. Duplicates are skipped; '
      + 'auto-tag settings don\u2019t carry (they belong to the other case\u2019s tags).'));
    const list = el('div', 'session-list');
    b.append(list);
    list.append(el('div', 'note-status', 'Reading recent cases\u2026'));
    let cases;
    try { cases = await api('/api/watchlist/cases'); }
    catch (e) { list.replaceChildren(el('div', 'note-status', e.message)); return; }
    list.replaceChildren();
    if (!cases.length) {
      list.append(el('div', 'note-status', 'No other recent case has watchlist indicators.'));
      return;
    }
    for (const c of cases) {
      const row = el('div', 'row-actions session-row wl-case-row');
      const name = el('span', 'session-name', c.name);
      name.title = c.path;
      row.append(name, el('span', 'count',
        `${c.indicator_count} indicator${c.indicator_count === 1 ? '' : 's'}`));
      const go = el('button', 'btn ghost', 'Import');
      go.onclick = async () => {
        go.disabled = true;
        try {
          const r = await post('/api/watchlist/import_case', { case_id: c.id });
          document.getElementById('modal').hidden = true;
          toast(`${r.added} indicator${r.added === 1 ? '' : 's'} imported`
            + (r.skipped ? ` \u00b7 ${r.skipped} already here` : ''));
          await post('/api/watchlist/scan', {});
          await load();
        } catch (e) {
          toast(e.message, 6000);
          go.disabled = false;
        }
      };
      row.append(go);
      list.append(row);
    }
  });
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
  $('wlFromCase').onclick = openFromCasePicker;
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

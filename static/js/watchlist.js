/* IOC watchlist — case-level indicators scanned across every table, with
   optional auto-tagging. Indicators live in the case .db (Store.watchlist)
   and are re-scanned on every import (the jobs.js source-done hook calls
   scanWatchlistForSources). Its own page tab: add/import indicators, see
   per-indicator hit counts, and drill into where each landed — the hits
   pane groups them by table. The scan is a background job (runScan):
   started, polled at 400 ms, shown as a jobs-panel row; a new entry is
   in the list the moment the server has it, with "…" for a count until
   its scan lands. See docs/design/analysis-suite.md. */

import { $, api, el, post, toast } from './core.js';
import { clearPageCache, render } from './grid.js';
import { clearGroupPageCache, drawRail } from './grouping.js';
import { createNotice } from './jobs.js';
import { recordTabVisit } from './tabhistory.js';
import { showMainView, syncTabChrome } from './sql.js';
import { renderSidebar, sourceLabel, syncTabSelection } from './sources.js';
import { S } from './state.js';
import { refreshTagCounts } from './tags.js';
import { modal } from './ui.js';
import { jumpToTimelineRow } from './timeline.js';

const KIND_COLOR = { hash: '#7c6cf6', ip: '#39a8e8', domain: '#39e881',
                     filename: '#d9a441', other: '#8a8a90' };
const KIND_LABEL = { hash: 'HASH', ip: 'IP', domain: 'DOMAIN',
                     filename: 'FILE', other: 'IOC' };
export const WATCHLIST_POLL_MS = 400;
let indicators = [];
let selected = null;
/* Indicators whose first scan has not landed yet — rendered as "…" in
   the count cell. Keyed by id in module state rather than flagged on the
   row object: any load() (the import hook's, a delete's) replaces
   `indicators` wholesale mid-scan and would drop a flag carried there,
   flipping "…" to "0" while the job still runs. */
const scanning = new Set();
/* Tables the analyst folded in the hits pane, by source id. */
const collapsed = new Set();
/* The scan being polled, if any: { jobId, notice, timer, label }. One at
   a time — the server keeps one live scan per case, and starting another
   supersedes it (its poller then 404s and stops). */
let scanJob = null;

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

/* Scan of just-imported sources, for the jobs.js import hook: every
   indicator against those tables only. Exported for that hook; the tab
   refreshes itself when the job lands. */
export async function scanWatchlistForSources(sourceIds) {
  if (!sourceIds || !sourceIds.length) return;
  try {
    const wl = await api('/api/watchlist');
    if (!wl.length) return;            // nothing to scan for
    await runScan({ sourceIds });
  } catch { /* best effort */ }
}

/* Whether a scan is running — the shutdown guard's list (jobs.js
   inFlightWork) names it. */
export function watchlistScanRunning() { return scanJob != null; }

function hitsLabel(n) { return `${n.toLocaleString()} hit${n === 1 ? '' : 's'}`; }
function tablesLabel(n) { return `${n.toLocaleString()} table${n === 1 ? '' : 's'}`; }

function scanDetail(job, label) {
  const progress = job.total ? `${job.scanned}/${job.total} tables` : 'starting…';
  return label ? `"${label}" · ${progress}` : progress;
}

/* The tab's own status text mirrors the job while it runs; it is only
   visible on the tab, so writing it from anywhere is harmless. */
function paintScanStatus(job) {
  const st = $('wlStatus');
  if (!st) return;
  if (job && job.status === 'running') {
    st.textContent = job.total ? `Scanning ${job.scanned}/${job.total} tables…` : 'Scanning…';
    return;
  }
  st.textContent = job && job.status === 'done' ? 'Scanned' : '';
  if (st.textContent) setTimeout(() => { if (st.textContent === 'Scanned') st.textContent = ''; }, 1500);
}

function stopScanPoll() {
  if (!scanJob) return;
  clearTimeout(scanJob.timer);
  scanJob = null;
}

/* Every scan goes through here — Add, Scan all, the file import, From a
   case, the import hook and Search-all's "Add to watchlist". Starts the
   job, stands a jobs-panel row for it (progress = tables scanned),
   polls it to its end and then refreshes whatever the analyst is
   looking at: the list if the tab is showing, else the tab's badge and
   — when it found something — the sticky "Watchlist: N hits" row with a
   way to the hits. Never rejects: callers fire it and move on. Returns
   the finished job, or null when it could not start. */
export async function runScan({ sourceIds = null, watchlistIds = null, label = null } = {}) {
  stopScanPoll();
  let job;
  try {
    job = await post('/api/watchlist/scan/start', { source_ids: sourceIds, watchlist_ids: watchlistIds });
  } catch (e) {
    // The entries marked "…" have no scan coming: back to their real
    // counts rather than a marker that never resolves.
    scanning.clear();
    renderList();
    paintScanStatus(null);
    toast('Could not start the watchlist scan: ' + e.message, 6000);
    return null;
  }
  const rec = { jobId: job.job_id, label, timer: null, notice: null, done: null };
  rec.notice = createNotice('watchlist', {
    title: 'Watchlist scan',
    detail: scanDetail(job, label),
    progress: job.total ? job.scanned / job.total : null,
    actions: [{ label: 'Cancel', onClick: () => cancelScan(rec) }],
  }, {
    // The row's ✕ cancels a running scan rather than merely hiding it:
    // a hidden scan would go on tagging rows with nothing on screen to
    // stop it from.
    onDismiss: () => cancelScan(rec),
  });
  scanJob = rec;
  paintScanStatus(job);
  if (job.status !== 'running') { await finishScan(rec, job); return job; }
  return new Promise((resolve) => { rec.done = resolve; pollScan(rec); });
}

function cancelScan(rec) {
  if (scanJob !== rec) return;
  post(`/api/watchlist/scan/cancel?job_id=${rec.jobId}`, {}).catch(() => {});
}

/* Polls the job at WATCHLIST_POLL_MS until it ends. Stops the moment its
   record is no longer the one being followed (a newer scan, a case
   switch), so a late answer never touches a row that has moved on. A 404
   is the server's word that the job is gone — superseded from another
   window, or the case closed under it — and the poller stops on it,
   dropping the "…" markers back to real counts; anything else is asked
   again. */
function pollScan(rec) {
  rec.timer = setTimeout(async () => {
    if (scanJob !== rec) return;
    let job;
    try {
      job = await api(`/api/watchlist/scan/job?job_id=${rec.jobId}`);
    } catch (e) {
      if (scanJob !== rec) return;
      if (e.status === 404 || e.status === 409) {
        await settleScan(rec, e.status === 404 ? 'replaced by a newer scan' : 'the case was closed');
        return;
      }
      pollScan(rec);
      return;
    }
    if (scanJob !== rec) return;
    if (job.status === 'running') {
      rec.notice.update({ detail: scanDetail(job, rec.label), progress: job.total ? job.scanned / job.total : null });
      paintScanStatus(job);
      pollScan(rec);
      return;
    }
    await finishScan(rec, job);
  }, WATCHLIST_POLL_MS);
}

/* A scan that ended without a result to report on. */
async function settleScan(rec, detail) {
  scanJob = null;
  scanning.clear();
  rec.notice.done({ detail, sticky: false, actions: [] });
  paintScanStatus(null);
  if (S.activeTab === 'watchlist') await load(); else renderList();
  if (rec.done) rec.done(null);
}

/* The job landed. Auto-tags on the open table mean its cached rows and
   ribbon counts are stale — the same invalidation a bulk tag does, or
   the tags would not paint until something else refreshed the grid. */
async function finishScan(rec, job) {
  if (scanJob === rec) scanJob = null;
  scanning.clear();
  const total = Object.values(job.matched || {}).reduce((a, b) => a + b, 0);
  const found = Object.entries(job.by_source || {}).filter(([, n]) => n > 0)
    .map(([sid, n]) => ({ sid: Number(sid), n }));
  const secs = `${((job.elapsed_ms || 0) / 1000).toFixed(1)} s`;
  if (job.status === 'error') {
    rec.notice.fail({ detail: job.error || 'the scan failed', actions: [] });
  } else if (job.status !== 'done') {
    rec.notice.done({ detail: `cancelled · ${job.scanned}/${job.total} tables`, sticky: false, actions: [] });
  } else if (found.length && S.activeTab !== 'watchlist') {
    announceHits(rec.notice, found);
  } else {
    rec.notice.done({ detail: `${hitsLabel(total)} · ${tablesLabel(job.scanned)} · ${secs}`, sticky: false, actions: [] });
  }
  if ((job.auto_tagged || []).includes(S.sourceId) && S.view) {
    // clearRowCaches() once PR 2 lands; both caches, for the same reason it exists.
    clearPageCache();
    clearGroupPageCache();
    refreshTagCounts();
    render();
    drawRail();
  }
  paintScanStatus(job);
  if (S.activeTab === 'watchlist') await load();
  else { renderList(); await refreshWatchlistBadge(); }
  if (rec.done) rec.done(job);
}

/* The alert itself: the scan's own row in the jobs panel — the card an
   import gets, in the corner the analyst already watches while files
   land — turned into the news, with the way to the hits on it. Sticky:
   a hit is news until it's looked at. Owned by 'watchlist' so a case
   switch clears it with the rest. */
function announceHits(notice, found) {
  const total = found.reduce((a, f) => a + f.n, 0);
  const names = found.map((f) => {
    const src = S.sources.find((s) => s.id === f.sid);
    return `${f.n} in ${src ? sourceLabel(src) : `table ${f.sid}`}`;
  });
  notice.done({
    title: `Watchlist: ${hitsLabel(total)}`,
    detail: names.join(' · '),
    sticky: true,
    actions: [{ label: 'Open watchlist', onClick: () => showWatchlistTab() }],
  });
}

/* The tab's new-hit dot: total hits vs the count last seen (case_settings,
   so it travels with the case). Showing the tab is what marks them seen. */
export async function refreshWatchlistBadge() {
  let b;
  try { b = await api('/api/watchlist/badge'); } catch { return; }
  S.watchlistNewHits = Math.max(0, (b.total_hits || 0) - (b.seen || 0));
  paintWatchlistBadge();
}

/* The new-hit indicator: a count pill on the Watchlist tab (and on the
   collapsed Pages ▾ button when the strip is a dropdown), plus the
   sidebar's Pages row — one number, painted wherever the tab is
   represented, so it can't be missed whichever way the pages are shown. */
export function paintWatchlistBadge() {
  const n = S.watchlistNewHits || 0;
  const paint = (host) => {
    if (!host) return;
    let pill = host.querySelector(':scope > .tab-badge');
    if (!n) { if (pill) pill.remove(); return; }
    if (!pill) { pill = el('span', 'tab-badge'); host.append(pill); }
    pill.textContent = n > 99 ? '99+' : String(n);
    pill.title = `${n.toLocaleString()} new watchlist hit${n === 1 ? '' : 's'} since you last looked`;
  };
  paint($('tabWatchlist'));
  const pm = $('pagesMenuBtn');
  if (pm) paint(pm);
  $('tabWatchlist')?.classList.toggle('has-new-hits', n > 0);
  renderSidebar();
}

async function markHitsSeen() {
  const total = indicators.reduce((n, i) => n + (i.hit_count || 0), 0);
  S.watchlistNewHits = 0;
  paintWatchlistBadge();
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

/* Entries the server just accepted go straight into the list, marked
   as awaiting their scan; the scan that follows fills the counts in. */
function addIndicators(added) {
  for (const ind of added) {
    if (!indicators.some((i) => i.id === ind.id)) indicators.push(ind);
    scanning.add(ind.id);
  }
  renderList();
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
    const pending = scanning.has(ind.id);
    const cnt = el('span', 'wl-count' + (pending ? ' scanning' : ind.hit_count ? ' hot' : ''),
      pending ? '…' : String(ind.hit_count));
    cnt.title = pending ? 'Scanning the tables for it…' : `${ind.hit_count} hit${ind.hit_count === 1 ? '' : 's'}`;
    const del = el('button', 'wl-del', '✕');
    del.title = 'Remove this indicator';
    del.onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/watchlist/${ind.id}`, { method: 'DELETE' });
      scanning.delete(ind.id);
      if (selected === ind.id) selected = null;
      load();
    };
    row.append(kind, mainCol, cnt, del);
    row.onclick = () => { selected = ind.id; renderList(); renderHits(); };
    list.append(row);
  }
}

function hitRow(h) {
  const r = el('div', 'wl-hit');
  const top = el('div', 'wl-hit-top');
  top.append(el('span', 'wl-hit-rid', `row ${h.rid}`));
  // Where in the row it matched: the column and that cell.
  if (h.column) {
    const where = el('span', 'wl-hit-col');
    where.append(el('span', 'wl-hit-colname', h.column + ': '), document.createTextNode(h.value || ''));
    where.title = `${h.column}: ${h.value || ''}`;
    top.append(where);
  }
  r.append(top);
  if (h.preview) {
    const pv = el('div', 'wl-hit-preview', h.preview);
    pv.title = h.preview;
    r.append(pv);
  }
  r.title = 'Open this table at the row';
  r.onclick = () => jumpToTimelineRow(h.source_id, h.rid);
  return r;
}

/* The hits pane: one collapsible group per table the indicator hit,
   headed by the table's name (the analyst's nickname, as the sidebar
   shows it) and its exact count — the server counts per table and caps
   only the rows it returns, so a hot indicator's tables are all listed
   and a group that runs past the cap ends in "…and N more". The header
   sticks while its rows scroll, like a grouped view's. Folding is local
   state (`collapsed`), no refetch. */
async function renderHits() {
  const box = $('wlHits');
  box.replaceChildren();
  if (selected == null) { box.append(el('div', 'note-status', 'Select an indicator to see its hits.')); return; }
  const want = selected;
  const ind = indicators.find((i) => i.id === selected);
  box.append(el('div', 'wl-hits-head', `Hits for "${ind ? ind.value : ''}"`));
  let res;
  try { res = await api(`/api/watchlist/hits?watchlist_id=${selected}`); }
  catch (e) { if (selected === want) box.append(el('div', 'note-status', e.message)); return; }
  if (selected !== want) return;   // another row was picked while this loaded
  if (!res.hits.length) { box.append(el('div', 'note-status', 'No hits — scan tables, or this indicator matched nothing.')); return; }
  const bySource = new Map();
  for (const h of res.hits) {
    if (!bySource.has(h.source_id)) bySource.set(h.source_id, []);
    bySource.get(h.source_id).push(h);
  }
  for (const g of res.sources) {
    const group = el('div', 'wl-hit-group');
    group.dataset.sourceId = g.source_id;
    const src = S.sources.find((s) => s.id === g.source_id);
    const head = el('div', 'wl-hit-group-head');
    const arrow = el('span', 'wl-hit-group-arrow');
    const name = el('span', 'wl-hit-group-label', (src && sourceLabel(src)) || g.source_name);
    name.title = g.source_name;
    head.append(arrow, name, el('span', 'wl-hit-group-count', hitsLabel(g.count)));
    const body = el('div', 'wl-hit-group-body');
    const paint = () => {
      const open = !collapsed.has(g.source_id);
      arrow.textContent = open ? '▾' : '▸';
      body.hidden = !open;
      head.title = open ? 'Fold this table’s hits' : 'Unfold this table’s hits';
    };
    head.onclick = () => {
      if (collapsed.has(g.source_id)) collapsed.delete(g.source_id); else collapsed.add(g.source_id);
      paint();
    };
    const hits = bySource.get(g.source_id) || [];
    for (const h of hits) body.append(hitRow(h));
    if (g.shown < g.count) {
      const more = el('div', 'note-status wl-hit-more',
        `…and ${(g.count - g.shown).toLocaleString()} more — open the table`);
      more.title = 'Open this table at its first hit';
      more.onclick = () => { if (hits.length) jumpToTimelineRow(g.source_id, hits[0].rid); };
      body.append(more);
    }
    paint();
    group.append(head, body);
    box.append(group);
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
      'Copy another case’s indicators into this one. Duplicates are skipped; '
      + 'auto-tag settings don’t carry (they belong to the other case’s tags).'));
    const list = el('div', 'session-list');
    b.append(list);
    list.append(el('div', 'note-status', 'Reading recent cases…'));
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
            + (r.skipped ? ` · ${r.skipped} already here` : ''));
          importedIndicators(r);
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

/* An import answered: the list is what the server returned, the new
   entries are marked, and the scan covers only those. */
function importedIndicators(r) {
  const addedIds = new Set(r.added_ids || []);
  indicators = r.indicators || indicators;
  addIndicators(indicators.filter((i) => addedIds.has(i.id)));
  if (addedIds.size) runScan({ watchlistIds: [...addedIds] });
}

export function wireWatchlist() {
  $('tabWatchlist').onclick = showWatchlistTab;
  $('wlAdd').onclick = async () => {
    const value = $('wlValue').value.trim();
    if (!value) { toast('Enter an indicator'); return; }
    const btn = $('wlAdd');
    btn.disabled = true;   // only while the add itself is in flight; the scan runs behind
    let ind;
    try {
      ind = await post('/api/watchlist', { value, kind: $('wlKind').value,
        auto_tag_id: $('wlAutoTag').value ? Number($('wlAutoTag').value) : null });
    } catch (e) {
      toast(e.message, 6000);   // a duplicate is the usual one: the server refuses it
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
    $('wlValue').value = '';
    // The row is on screen now; its count arrives when the scan does.
    addIndicators([ind]);
    runScan({ watchlistIds: [ind.id], label: ind.value });
  };
  $('wlScan').onclick = () => { runScan({}); };
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
      toast(`${r.added} indicator${r.added === 1 ? '' : 's'} imported`);
      importedIndicators(r);
    } catch (e) { toast(e.message, 6000); }
  };
}

// Indicators are per-case (server-side); a case switch just refetches.
// The scan being followed belonged to the Store the server just closed
// (its notice row went with resetJobState); its poll stops here.
export function resetWatchlist() {
  selected = null;
  indicators = [];
  scanning.clear();
  collapsed.clear();
  stopScanPoll();
}

export async function showWatchlistTab() {
  recordTabVisit({ kind: 'page', key: 'watchlist' });
  S.activeTab = 'watchlist';
  showMainView('watchlistview');
  syncTabSelection();
  syncTabChrome();
  fillAutoTag();
  await load();
}

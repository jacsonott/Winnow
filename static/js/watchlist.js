/* IOC watchlist — case-level indicators scanned across every table, with
   optional auto-tagging. Indicators live in the case .db (Store.watchlist)
   and are re-scanned on every import (the jobs.js source-done hook calls
   scanWatchlistForSources). Its own page tab: add/import indicators, see
   per-indicator hit counts, and drill into where each landed — the hits
   pane groups them by table. It OPENS on the latest flagged rows across
   every indicator (renderLatestHits), because a case with thousands of
   findings should not spend half the page telling you to click something;
   counts say whether a zero was scanned or never looked at, and an
   indicator whose rows are another's is marked and mergeable (see
   Store.watchlist_overview). The scan is a background job (runScan):
   started, polled at 400 ms, shown as a jobs-panel row; a newer scan
   takes over a running one's remaining work (the server folds the
   scopes), so a new entry is in the list the moment the server has it,
   with "…" for a count until the scan covering it lands. See
   docs/design/analysis-suite.md. */

import { $, api, el, post, toast } from './core.js';
import { markDashboardStale } from './dashboard.js';
import { render } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { createNotice } from './jobs.js';
import { recordTabVisit } from './tabhistory.js';
import { gridIsShowing, showMainView, syncTabChrome } from './sql.js';
import { renderSidebar, sourceLabel, syncTabSelection } from './sources.js';
import { S } from './state.js';
import { clearRowCaches, refreshTagCounts } from './tags.js';
import { modal } from './ui.js';
import { jumpToTimelineRow } from './timeline.js';

const KIND_COLOR = { hash: '#7c6cf6', ip: '#39a8e8', domain: '#39e881',
                     filename: '#d9a441', other: '#8a8a90' };
const KIND_LABEL = { hash: 'HASH', ip: 'IP', domain: 'DOMAIN',
                     filename: 'FILE', other: 'IOC' };
export const WATCHLIST_POLL_MS = 400;
let indicators = [];
/* The rest of what /api/watchlist/overview answers: how many tables a scan
   covers (`scan_targets`, the denominator of every entry's
   scanned-so-far fraction), the honest row count behind the per-indicator
   totals, and which indicators flag each other's rows. Kept beside
   `indicators` rather than folded onto each entry because two of the three
   are facts about the LIST, not about any one row. */
let overview = { scan_targets: 0, total_hits: 0, distinct_rows: null,
                 overlap_checked: false, relations: [] };
let selected = null;
/* Bumped by every renderHits() call. Both panes fetch, and the fetch that
   comes back second would otherwise paint over the pane the analyst is
   actually looking at — selecting an indicator while the latest-hits list
   is still in flight was exactly that. */
let paintToken = 0;
/* Indicators whose first scan has not landed yet — rendered as "…" in
   the count cell. Keyed by id in module state rather than flagged on the
   row object: any load() (the import hook's, a delete's) replaces
   `indicators` wholesale mid-scan and would drop a flag carried there,
   flipping "…" to "0" while the job still runs. */
const scanning = new Set();
/* Tables the analyst folded in the hits pane, by source id. */
const collapsed = new Set();
/* The scan being polled, if any: { jobId, notice, timer, label,
   watchlistIds, done, failures }. One at a time — the server keeps one live scan per case,
   and a newer start folds the running one's remaining scope into the new
   job, so the row being followed settles as folded and the new one is
   followed instead. Starts from this window go out one at a time
   (`starting`): the one in flight answers before the next leaves, so the
   server sees them in the order they were asked for and the scan a start
   displaces is always the one being followed here. */
let scanJob = null;
let starting = Promise.resolve();
/* Consecutive poll failures (not 404/409) a scan is given before it is
   taken as gone — the server exited or crashed under it. */
const SCAN_POLL_FAILURES = 3;

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

/* The scan being followed gives way to the newer one that `job` is. Its
   row settles either way — never left in the running state with a Cancel
   that reaches nothing — and whoever awaited it gets null. Which way is
   read off the new job's scope: the server widens a job to a displaced
   scan's scope only while that scan is still running, so a new job that
   covers the old one's ids folded it (its "…" markers stay for the job
   that now carries them), and one that does not means the old scan had
   already finished — its counts are on the server, nothing of it is
   left to follow, and its markers come off now, all but the ids the new
   scan is for. */
function foldScan(rec, job, keep) {
  if (scanJob !== rec) return;
  clearTimeout(rec.timer);
  scanJob = null;
  const own = rec.label ? `"${rec.label}" · ` : '';
  const covered = job.watchlist_ids == null
    || (rec.watchlistIds != null && rec.watchlistIds.every((id) => job.watchlist_ids.includes(id)));
  if (covered) {
    rec.notice.done({ detail: `${own}folded into the newer scan`, sticky: false, actions: [] });
  } else {
    const keepSet = new Set(keep || []);
    for (const id of [...scanning]) {
      if (!keepSet.has(id) && (rec.watchlistIds == null || rec.watchlistIds.includes(id))) scanning.delete(id);
    }
    rec.notice.done({ detail: `${own}finished`, sticky: false, actions: [] });
    if (S.activeTab === 'watchlist') load(); else renderList();
  }
  if (rec.done) rec.done(null);
}

/* Every scan goes through here — Add, Scan all, the file import, From a
   case, the import hook and Search-all's "Add to watchlist". Starts the
   job, stands a jobs-panel row for it (progress = tables scanned),
   polls it to its end and then refreshes whatever the analyst is
   looking at: the list if the tab is showing, else the tab's badge and
   — when it found something — the sticky "Watchlist: N hits" row with a
   way to the hits. Never rejects: callers fire it and move on. Returns
   the finished job, or null when it could not start or a later scan
   folded it in. */
export async function runScan({ sourceIds = null, watchlistIds = null, label = null } = {}) {
  const turn = starting;
  let release;
  starting = new Promise((r) => { release = r; });
  await turn;
  let rec, job;
  try {
    // The scan already being followed pauses its poll while this start is
    // in flight: once the start lands its row settles as folded (the
    // server widened the new job to its scope) or done; should the start
    // fail, its poll picks back up as if nothing had happened.
    const prev = scanJob;
    if (prev) clearTimeout(prev.timer);
    try {
      job = await post('/api/watchlist/scan/start', { source_ids: sourceIds, watchlist_ids: watchlistIds });
    } catch (e) {
      if (prev && scanJob === prev) pollScan(prev);
      // The entries this call marked have no scan coming: back to their
      // real counts rather than a marker that never resolves. Any a scan
      // still being followed covers keep theirs until it lands.
      if (scanJob) for (const id of watchlistIds || []) scanning.delete(id);
      else { scanning.clear(); paintScanStatus(null); }
      renderList();
      toast('Could not start the watchlist scan: ' + e.message, 6000);
      return null;
    }
    if (scanJob) foldScan(scanJob, job, watchlistIds);
    // The label names the one indicator a scan is for; a job the server
    // widened to cover a folded scan is no longer that.
    const single = !!(job.watchlist_ids && job.watchlist_ids.length === 1);
    rec = { jobId: job.job_id, label: single ? label : null, watchlistIds: job.watchlist_ids,
            timer: null, notice: null, done: null, failures: 0 };
    rec.notice = createNotice('watchlist', {
      title: 'Watchlist scan',
      detail: scanDetail(job, rec.label),
      progress: job.total ? job.scanned / job.total : null,
      actions: [{ label: 'Cancel', onClick: () => cancelScan(rec) }],
    }, {
      // The row's ✕ cancels a running scan rather than merely hiding it:
      // a hidden scan would go on tagging rows with nothing on screen to
      // stop it from.
      onDismiss: () => cancelScan(rec),
    });
    scanJob = rec;
  } finally {
    release();   // the next start may go out, whatever became of this one
  }
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
   dropping the "…" markers back to real counts. Anything else is asked
   again, but not forever: with the server gone (idle shutdown, a crash)
   every poll fails, and a chain that never stopped would keep the row
   running and the markers unresolved for as long as the tab was open. */
function pollScan(rec) {
  clearTimeout(rec.timer);
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
      if (++rec.failures >= SCAN_POLL_FAILURES) { await settleScan(rec, e.message, { failed: true }); return; }
      pollScan(rec);
      return;
    }
    if (scanJob !== rec) return;
    rec.failures = 0;
    if (job.status === 'running') {
      rec.notice.update({ detail: scanDetail(job, rec.label), progress: job.total ? job.scanned / job.total : null });
      paintScanStatus(job);
      pollScan(rec);
      return;
    }
    await finishScan(rec, job);
  }, WATCHLIST_POLL_MS);
}

/* A scan that ended without a result to report on. Every marker comes
   off: whatever scan they were waiting on is not one this window can
   follow any more. */
async function settleScan(rec, detail, { failed = false } = {}) {
  scanJob = null;
  scanning.clear();
  if (failed) rec.notice.fail({ detail, actions: [] });
  else rec.notice.done({ detail, sticky: false, actions: [] });
  paintScanStatus(null);
  if (S.activeTab === 'watchlist') await load(); else renderList();
  if (rec.done) rec.done(null);
}

/* Whether the open table's rows are among the ones a scan auto-tagged:
   the table itself, or — for a merge, whose rows are its members' and
   are tagged there (invariant #9) — any member. */
function openTableWasTagged(job) {
  const open = S.sources.find((s) => s.id === S.sourceId);
  if (!open) return false;
  const touched = new Set(job.auto_tagged || []);
  return touched.has(open.id)
    || !!(open.is_merge && (open.member_source_ids || []).some((id) => touched.has(id)));
}

/* The job landed. Auto-tags on the open table mean its cached rows and
   ribbon counts are stale — the same invalidation a bulk tag does, or
   the tags would not paint until something else refreshed the grid. */
async function finishScan(rec, job) {
  if (scanJob === rec) scanJob = null;
  // Only the markers this job covered come off: a scan scoped to one
  // entry says nothing about another's, whose own scan is still to come.
  if (job.watchlist_ids == null) scanning.clear();
  else for (const id of job.watchlist_ids) scanning.delete(id);
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
  // Hits written and matches auto-tagged are both things a widget counts,
  // and a scan is usually running because an import just landed — which is
  // exactly when a board is open and being read. markDashboardStale asks
  // the server whether anything actually moved, so a re-scan that found
  // the same hits leaves the board alone.
  if (job.status === 'done') markDashboardStale();
  if (S.view && openTableWasTagged(job)) {
    clearRowCaches();
    refreshTagCounts();
    // The same trio every other tag path ends in: the rows, the rail, and
    // — under a grouping BY TAG — the tree, whose buckets and counts are
    // the pre-tag ones, so the rows this scan tagged would sit in
    // "(untagged)" until the analyst regrouped by hand.
    //
    // The repaint itself waits for the grid to be showing. Against a grid
    // a page tab (or the home screen) hides, render() measures a
    // zero-height viewport and paints the first rows at the top; the
    // return then restores the real scroll position over an empty
    // viewport. The paths that only re-show the grid (Alt+1, tab history,
    // the mouse thumb buttons) pay the owed repaint on the way back —
    // showGridTab, which regroups with it.
    if (gridIsShowing()) { render(); drawRail(); regroupIfGroupedByTag(); } else S.gridRepaintPending = true;
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
  try {
    const res = await api('/api/watchlist/overview');
    indicators = res.indicators || [];
    overview = res;
  } catch { indicators = []; overview = { scan_targets: 0, total_hits: 0, distinct_rows: null,
                                          overlap_checked: false, relations: [] }; }
  if (selected != null && !indicators.some((i) => i.id === selected)) selected = null;
  renderList();
  // The hits pane is repainted on every load, not only when the selection
  // died under it: a scan that just landed changed what "latest" means and
  // what the selected indicator's hits are, and leaving the pane on the
  // pre-scan answer is the stale half-screen this page already had.
  renderHits();
  // Any load while the tab is showing means the analyst is looking at the
  // current counts — keep the seen high-water in step or the next badge
  // poll would light the dot for hits already on screen.
  if (S.activeTab === 'watchlist') markHitsSeen();
}

/* Every relation the overlap pass found for one indicator, most
   consequential first: an identical set makes this entry redundant, being
   contained in another makes it redundant, containing another makes the
   OTHER one redundant. */
function relationsFor(id) {
  const order = { same: 0, subset: 1, superset: 2 };
  return (overview.relations || []).filter((r) => r.id === id)
    .sort((a, b) => order[a.relation] - order[b.relation]);
}

function indicatorById(id) { return indicators.find((i) => i.id === id); }
function valueOf(id) { const i = indicatorById(id); return i ? i.value : `indicator ${id}`; }

/* What an indicator's count cell says, which is not always a number. A
   bare "0" meant both "every table was read and it is not in this case"
   and "nothing has looked yet" — the first is a sentence an analyst can
   put in a report, the second is not, and they were spelled the same.

   Every answer keeps the `wl-count` class, whatever it says: that class
   is the row's count SLOT, the one thing the stylesheet and the UI tests
   reach for to find the cell, and a row that happened to be unscanned
   dropping out of `.wl-count` altogether turned "read the cell" into
   "read the cell if it is a number". `wl-state` is the variant on top of
   it, for when the answer is words. */
function countCell(ind) {
  if (scanning.has(ind.id)) {
    const c = el('span', 'wl-count scanning', '…');
    c.title = 'Scanning the tables for it…';
    return c;
  }
  const done = ind.scanned_sources || 0;
  const targets = overview.scan_targets || 0;
  const covered = targets ? `scanned ${done.toLocaleString()} of ${targets.toLocaleString()} table${targets === 1 ? '' : 's'}` : 'no tables to scan';
  if (ind.hit_count) {
    const c = el('span', 'wl-count hot', ind.hit_count.toLocaleString());
    c.title = `${hitsLabel(ind.hit_count)} · ${covered}`;
    return c;
  }
  if (!done) {
    const c = el('span', 'wl-count wl-state unscanned', 'not scanned');
    c.title = 'No scan has read this indicator against any table yet — this is not "not present".';
    return c;
  }
  if (done >= targets) {
    const c = el('span', 'wl-count wl-state clean', 'clean');
    c.title = `${covered} — no row anywhere in the case matched it.`;
    return c;
  }
  const c = el('span', 'wl-count wl-state partial', `${done}/${targets} tables`);
  c.title = `${covered} — no hits in those. The rest have not been read for it.`;
  return c;
}

/* The row's overlap chip: this indicator's hits are another's hits, so one
   of the two is buying the analyst nothing. Clicking opens the merge
   dialog rather than acting, because which of the two names belongs in the
   report is the analyst's call, not ours. */
function overlapChip(ind) {
  const rels = relationsFor(ind.id);
  if (!rels.length) return null;
  const r = rels[0];
  let text;
  if (rels.length > 1) text = `⧉ overlaps ${rels.length} other indicators`;
  else if (r.relation === 'same') text = `⧉ same rows as ${valueOf(r.other_id)}`;
  else if (r.relation === 'subset') text = `⧉ all its rows are also ${valueOf(r.other_id)}’s`;
  else text = `⧉ covers every row of ${valueOf(r.other_id)}`;
  const chip = el('button', 'wl-dup', text);
  chip.title = 'These indicators report the same rows — the summary counts them once. Merge or remove one.';
  chip.onclick = (e) => { e.stopPropagation(); openOverlapDialog(ind); };
  return chip;
}

/* Merge two indicators that cover the same rows. The server refuses to
   drop one that found rows the keeper did not, so the pair on screen being
   a scan out of date is a message, not lost findings. */
async function mergeIndicators(keepId, dropId) {
  let r;
  try { r = await post('/api/watchlist/merge', { keep_id: keepId, drop_id: dropId }); }
  catch (e) { toast(e.message, 6000); return; }
  document.getElementById('modal').hidden = true;
  scanning.delete(dropId);
  if (selected === dropId) selected = keepId;
  toast(`Merged into “${r.kept.value}”`
    + (r.auto_tag_moved ? ' · its auto-tag came across' : ''));
  await load();
  await refreshWatchlistBadge();
}

function openOverlapDialog(ind) {
  modal('Overlapping indicators', (b) => {
    b.append(el('p', 'fb-help',
      'Two indicators that flag the same rows are one finding, not two. '
      + 'Keep the name you want in the report; the other is removed, and its '
      + 'auto-tag moves across if the one you keep has none.'));
    const list = el('div', 'session-list');
    b.append(list);
    for (const r of relationsFor(ind.id)) {
      const other = indicatorById(r.other_id);
      const row = el('div', 'row-actions session-row wl-dup-row');
      const what = el('span', 'session-name');
      what.append(el('span', 'wl-val', ind.value));
      const rel = r.relation === 'same' ? ' and ' : r.relation === 'subset' ? ' — every row of it is also in ' : ' — it covers every row of ';
      what.append(document.createTextNode(rel));
      what.append(el('span', 'wl-val', other ? other.value : `indicator ${r.other_id}`));
      what.append(el('span', 'count', ` ${r.shared.toLocaleString()} shared row${r.shared === 1 ? '' : 's'}`));
      row.append(what);
      // Only the merges that lose nothing are offered: dropping the
      // indicator whose rows are a strict subset is safe, dropping the
      // wider one would take rows with it and the server says no.
      if (r.relation === 'same' || r.relation === 'superset') {
        const keepThis = el('button', 'btn ghost', `Keep “${ind.value}”`);
        keepThis.onclick = () => mergeIndicators(ind.id, r.other_id);
        row.append(keepThis);
      }
      if (r.relation === 'same' || r.relation === 'subset') {
        const keepOther = el('button', 'btn ghost', `Keep “${other ? other.value : r.other_id}”`);
        keepOther.onclick = () => mergeIndicators(r.other_id, ind.id);
        row.append(keepOther);
      }
      list.append(row);
    }
  });
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

/* The summary line. Its old form added the per-indicator counts up and
   called the total "hits", which is a count of findings only while no two
   indicators match the same row — and two of them matching the same rows
   is the normal case (`mimikatz` and `mimikatz.exe` are one finding
   reported twice). When the overlap pass found fewer distinct rows than
   hits, the ROW count leads and the hit total is named for what it is. */
function paintSummary() {
  const sum = $('wlSummary');
  if (!sum) return;
  sum.title = '';
  if (!indicators.length) { sum.textContent = ''; return; }
  const withHits = indicators.filter((i) => i.hit_count).length;
  const totalHits = overview.total_hits != null
    ? overview.total_hits : indicators.reduce((n, i) => n + (i.hit_count || 0), 0);
  const rows = overview.overlap_checked ? overview.distinct_rows : null;
  let text = `${indicators.length} indicator${indicators.length === 1 ? '' : 's'}`;
  if (!withHits) { sum.textContent = text + ' · no hits yet'; return; }
  text += ` · ${withHits} with hits`;
  if (rows != null && rows < totalHits) {
    text += ` · ${rows.toLocaleString()} row${rows === 1 ? '' : 's'} flagged`
      + ` · ${totalHits.toLocaleString()} hits counted across indicators`;
    sum.title = 'Some rows matched more than one indicator, so the hit total counts them '
      + 'more than once. The row count is how many findings the case actually holds.';
  } else {
    text += ` · ${hitsLabel(totalHits)}`;
  }
  sum.textContent = text;
}

function renderList() {
  const list = $('wlList');
  list.replaceChildren();
  paintSummary();
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
    const dup = overlapChip(ind);
    if (dup) mainCol.append(dup);
    const cnt = countCell(ind);
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
    // Clicking the selected row again goes back to the whole case, which
    // is the only way back to the latest-hits pane once one is picked.
    row.onclick = () => { selected = selected === ind.id ? null : ind.id; renderList(); renderHits(); };
    row.title = ind.id === selected ? 'Show the latest hits from every indicator again'
      : 'Show only this indicator’s hits';
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

/* The pane the tab opens on: the newest flagged rows across every
   indicator, one line per ROW rather than per hit, each naming the
   indicators that matched it, the table, what the row is and when.

   It replaced "Select an indicator to see its hits", which spent half the
   page on an instruction while the case held thousands of findings — and
   which made "which of these fired most recently" a question you could
   only answer by clicking every entry in turn. Selecting an indicator
   still narrows to that one (the grouped-by-table pane below).

   The row summary and the timestamp are the columns the analyst's own
   timeline template names for that artefact, resolved server-side, so this
   pane and the Timeline describe a row the same way. */
async function renderLatestHits(box, token) {
  const head = el('div', 'wl-hits-head');
  head.append(el('span', null, 'Latest hits'));
  const sub = el('span', 'wl-hits-sub');
  head.append(sub);
  box.append(head);
  if (!indicators.length) {
    box.append(el('div', 'note-status', 'No indicators yet — add one on the left, import a list, '
      + 'or copy the set from another case.'));
    return;
  }
  let res;
  try { res = await api('/api/watchlist/latest'); }
  catch (e) { if (paintToken === token) box.append(el('div', 'note-status', e.message)); return; }
  if (paintToken !== token) return;
  if (!res.rows.length) {
    const anyScanned = indicators.some((i) => (i.scanned_sources || 0) > 0);
    box.append(el('div', 'note-status', anyScanned
      ? 'Nothing in this case matches any indicator on the list.'
      : 'Nothing scanned yet — "Scan all" reads every table for every indicator.'));
    return;
  }
  const withHits = indicators.filter((i) => i.hit_count).length;
  const rows = overview.overlap_checked && overview.distinct_rows != null
    ? overview.distinct_rows : null;
  sub.textContent = (rows != null
    ? `${rows.toLocaleString()} row${rows === 1 ? '' : 's'} flagged by ${withHits} indicator${withHits === 1 ? '' : 's'}`
    : `${withHits} indicator${withHits === 1 ? '' : 's'} with hits`) + ' · newest first';
  for (const h of res.rows) {
    const row = el('div', 'wl-latest');
    const who = el('span', 'wl-latest-who');
    for (const wid of h.watchlist_ids) {
      // The indicator's own name, and a way to narrow to it — the
      // question "what else did this one hit" is one click from here
      // rather than a hunt down the list on the left. Separated by a
      // middot: two indicator names side by side in the same colour read
      // as one long value, which is exactly the confusion this pane
      // exists to clear up.
      if (who.childNodes.length) who.append(el('span', 'wl-latest-sep', '·'));
      const ioc = el('button', 'wl-latest-ioc', valueOf(wid));
      ioc.title = 'Show only this indicator’s hits';
      ioc.onclick = (e) => { e.stopPropagation(); selected = wid; renderList(); renderHits(); };
      who.append(ioc);
    }
    const what = el('span', 'wl-latest-what');
    const src = S.sources.find((s) => s.id === h.source_id);
    what.append(el('span', 'wl-latest-table', (src && sourceLabel(src)) || h.source_name));
    what.append(document.createTextNode(' · ' + h.body));
    what.title = h.body;
    const when = el('span', 'wl-latest-when', h.ts || '—');
    if (!h.ts) when.title = 'This table has no datetime column, so the row cannot be placed in time.';
    row.append(who, what, when);
    row.title = 'Open this table at the row';
    row.onclick = () => jumpToTimelineRow(h.source_id, h.rid);
    box.append(row);
  }
  if (rows != null && rows > res.rows.length) {
    box.append(el('div', 'note-status wl-latest-more',
      `Showing the newest ${res.rows.length.toLocaleString()} of ${rows.toLocaleString()} flagged rows — `
      + 'pick an indicator to page through its own.'));
  }
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
  const token = ++paintToken;
  if (selected == null) { await renderLatestHits(box, token); return; }
  const want = selected;
  const ind = indicators.find((i) => i.id === selected);
  const head = el('div', 'wl-hits-head');
  const back = el('button', 'wl-hits-back', '← all indicators');
  back.title = 'Back to the latest hits across every indicator';
  back.onclick = () => { selected = null; renderList(); renderHits(); };
  head.append(back, el('span', null, `Hits for "${ind ? ind.value : ''}"`));
  box.append(head);
  let res;
  try { res = await api(`/api/watchlist/hits?watchlist_id=${selected}`); }
  catch (e) { if (paintToken === token) box.append(el('div', 'note-status', e.message)); return; }
  if (paintToken !== token || selected !== want) return;   // another row was picked while this loaded
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
  overview = { scan_targets: 0, total_hits: 0, distinct_rows: null,
               overlap_checked: false, relations: [] };
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

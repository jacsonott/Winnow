/* Background ingest jobs and the cancellable-op token: the jobs panel, upload
progress, and armOpCancel.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, toast } from './core.js';
import { markDashboardStale } from './dashboard.js';
import { clientLog } from './errlog.js';
import { offerTimestampColumns } from './derived.js';
import { updateSearchHint } from './filters.js';
import { scanWatchlistForSources, watchlistScanRunning } from './watchlist.js';
import { loadSources } from './sources.js';
import { S } from './state.js';
import { refreshSourcesQuietly } from './tables.js';

/* ---------------------------------------------------------- import jobs */

/* Imports run as background jobs server-side (Store.start_ingest_job) and
   the transfer phase runs as an XHR here, so the analyst keeps working —
   in this tab — while both happen. This panel (bottom-right corner) is the
   one place every phase of that reports: upload transfer (XHR progress
   events), the ingest itself (polled from /api/ingest/jobs — bytes for
   CSV, records/rows otherwise), and the background search-index builds
   (S.sources[].fts_building), which used to be completely invisible: a
   killed server took an index build down silently and nothing anywhere
   said so. Polling resumes on boot, so reloading the tab mid-import shows
   the running job again instead of losing sight of it. */
export const activeUploads = new Map();

 // clientId -> {name, loaded, total, xhr}
export let uploadSeq = 0;

export let ingestJobs = [];

export let jobsPollTimer = null;

export const seenJobStatus = new Map();

 // job_id -> last status, for transition toasts
export const dismissedJobs = new Set();

export const ftsWatch = new Set();

/* Settings → Imports → "Open new tables when an import finishes". One
   navigation per BATCH: the first table to finish opens, and nothing
   else moves the analyst until the queue has gone idle — a folder import
   is dozens of files, and being dragged to each one is the bug that made
   "never navigate" the rule in the first place. Reset when the poll finds
   nothing running (and on a case switch). */
let batchNavigated = false;

/* Job ids and source ids both restart at 1 in a new case (`_ingest_job_seq`
   is per Store; `sources.id` is a plain INTEGER PRIMARY KEY). Everything
   above is keyed by one of them, so carrying it across a case switch makes
   case B's first import inherit case A's "already dismissed" and never
   show a progress row. Called from home.js openCase. */
export function resetJobState() {
  seenJobStatus.clear();
  dismissedJobs.clear();
  ftsWatch.clear();
  ftsAsked.clear();
  batchNavigated = false;
  // A search left running in the background belongs to the Store the
  // server just closed (its job was cancelled with it); its poller stops
  // once its record is gone from here. Its notice row is one of the
  // notices cleared below.
  S.pendingViews.clear();
  // A plugin's rows were about the previous case too — and the poll that
  // would redraw the panel stops when nothing is running, so the DOM has
  // to be cleared here, not left for the next tick.
  for (const n of pluginNotices.values()) clearNoticeTimer(n);
  pluginNotices.clear();
  renderJobsPanel();
}

      // source ids seen building, for the "ready" toast

export function uploadWithProgress(url, fd, name) {
  const id = ++uploadSeq;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-Timeline-Lite-Client', '1');
    xhr.upload.onprogress = (e) => {
      const u = activeUploads.get(id);
      if (u && e.lengthComputable) { u.loaded = e.loaded; u.total = e.total; renderJobsPanel(); }
    };
    xhr.onload = () => {
      activeUploads.delete(id);
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) { startJobsPoll(); resolve(body); }
      else {
        renderJobsPanel();
        const err = new Error((body && body.detail) || xhr.statusText);
        err.status = xhr.status;
        reject(err);
      }
    };
    xhr.onerror = () => { activeUploads.delete(id); renderJobsPanel(); reject(new Error('Upload failed — is the server still up?')); };
    xhr.onabort = () => {
      activeUploads.delete(id);
      renderJobsPanel();
      const e = new Error('Upload cancelled');
      e.cancelled = true;
      reject(e);
    };
    activeUploads.set(id, { name, loaded: 0, total: 0, xhr });
    renderJobsPanel();
    xhr.send(fd);
  });
}


/* A short list of operations still running, for the shutdown guard — empty
   when the server is idle. Reads the same live state the corner panel does
   plus the search-all job and the cancellable-op token. */
export function inFlightWork() {
  const bits = [];
  const imports = ingestJobs.filter((j) => j.status === 'running' || j.status === 'queued').length
    + activeUploads.size;
  if (imports) bits.push(`${imports} import${imports === 1 ? '' : 's'} in progress`);
  if (S.searchAll && S.searchAll.running) bits.push('a Search-all sweep');
  if (watchlistScanRunning()) bits.push('a watchlist scan');
  const searching = [...S.pendingViews.values()].filter((p) => p.status === 'running').length;
  if (searching) bits.push(`${searching} search${searching === 1 ? '' : 'es'} running in the background`);
  const indexing = (S.sources || []).filter((s) => s.fts_building).length;
  if (indexing) bits.push(`${indexing} index build${indexing === 1 ? '' : 's'}`);
  if (opCancelCurrent) bits.push('a running query');
  return bits;
}

export function startJobsPoll() {
  if (!jobsPollTimer) pollJobs();
}

/* A search on a table with no trigram index is what starts the build —
   server-side, from inside build_view (Store._ensure_fts_building) — and
   nothing tells the client: the view's payload says nothing about the
   index, and the poll below only runs while it has something to watch,
   so on an idle case there is no poll to notice a new fts_building.
   rebuildView calls this once a build with a search in it lands on a
   table whose record says no index. Refetch the sources; recompute the
   open table's hint (a small table's index is ready before the search
   that started it lands, so the hint may have nothing to say); then let
   the poll take over — it watches the build (the indexing row, the hint
   while it runs, the toast when it is ready) and stops when it lands.
   `ftsAsked`: a table that answered "no index, no build" is not asked
   again — a runtime whose SQLite predates the trigram pushdown never
   builds one (store.py TRIGRAM_LIKE_MIN_SQLITE), and asking after every
   search there is three lock-taking requests for an answer that doesn't
   change. Source ids restart per case; resetJobState clears it. */
const ftsAsked = new Set();

export async function followFtsBuild(sourceId) {
  if (ftsAsked.has(sourceId)) return;
  try { await refreshSourcesQuietly(); } catch { return; }
  const src = (S.sources || []).find((s) => s.id === sourceId);
  if (src && !src.has_fts && !src.fts_building) ftsAsked.add(sourceId);
  if (sourceId === S.sourceId) updateSearchHint();
  startJobsPoll();
}

/* Jobs that finished before this page existed are history, not news.
   "Before this page existed" used to be approximated as "before this
   client ever saw a job" (seenJobStatus empty), which misfired on a
   fresh page: the very first import, failing instantly (an empty file),
   was already done by the first poll and got dismissed with NO toast —
   the analyst's failed import just silently didn't appear. The page's
   own load time is the real boundary. */
const PAGE_START = Date.now() / 1000;

export async function pollJobs() {
  jobsPollTimer = null;
  const firstPoll = seenJobStatus.size === 0;
  const finishedNow = [];
  try {
    const d = await api('/api/ingest/jobs');
    for (const j of d.jobs) {
      const done = j.status === 'done' || j.status === 'error' || j.status === 'cancelled';
      const prev = seenJobStatus.get(j.job_id);
      /* `prev === undefined` is the fast-job case, and it is not rare:
         deriving one column over a table this app considers small finishes
         well inside a poll interval, so the first time this client ever
         sees that job it is already done. Requiring a status *transition*
         meant those jobs never reached the loop below — no completion
         toast, and (the visible symptom) no auto-dismiss timer, so the
         panel row sat there reading DONE until the analyst clicked ✕.
         Jobs that were already finished before this client polled at all
         are still history rather than news; that's what firstPoll is. */
      const history = done && firstPoll && (j.started_at || 0) < PAGE_START;
      if (done && !history && prev !== j.status) finishedNow.push(j);
      // Jobs that were already finished before this page loaded (server
      // keeps the last 20) are history, not news — don't toast them and
      // don't fill the panel with them on load. A job STARTED after the
      // page loaded is this page's own work however fast it finished.
      if (history) dismissedJobs.add(j.job_id);
      seenJobStatus.set(j.job_id, j.status);
    }
    ingestJobs = d.jobs;
  } catch (e) {
    // The panel empties and the progress bar with it. Say so where it can
    // be found later — this is the trace behind "it stopped updating".
    ingestJobs = [];
    clientLog('warn', 'Job poll failed: ' + (e && e.message ? e.message : String(e)));
  }

  for (const j of finishedNow) {
    if (j.kind === 'derive') {
      // A derive job's "rows" are its columns' values, not an import — and
      // a flatten builds several columns in the one job, so the failure
      // count has to be summed across them rather than read off the first.
      const cols = j.result || [];
      const res = cols[0] || {};
      if (j.status === 'done') {
        const failed = cols.reduce((a, c) => a + (c.parse_failures || 0), 0);
        const what = cols.length > 1 ? `${cols.length} columns` : `"${j.name}"`;
        toast(`${what}: ${(res.rows || 0).toLocaleString()} rows read`
          + (failed ? ` · ${failed.toLocaleString()} value${failed === 1 ? '' : 's'} not found` : ''), failed ? 6000 : 3000);
      } else if (j.status === 'error') {
        toast(`Could not derive "${j.name}": ${j.error}`, 8000);
      }
      setTimeout(() => { dismissedJobs.add(j.job_id); renderJobsPanel(); }, 8000);
      continue;
    }
    if (j.status === 'done') {
      const total = (j.result || []).reduce((a, r) => a + (r.row_count || 0), 0);
      const ragged = (j.result || []).reduce((a, r) => a + (r.ragged_rows || 0), 0);
      const badRecs = (j.result || []).reduce((a, r) => a + (r.bad_records || 0), 0);
      const suspect = (j.result || []).reduce((a, r) => a + (r.suspect_quote_rows || 0), 0);
      const warn = ragged || badRecs || suspect;
      toast(`${j.name}: ${total.toLocaleString()} rows imported`
        + (ragged ? ` · ${ragged.toLocaleString()} ragged rows padded/trimmed` : '')
        + (badRecs ? ` · ${badRecs.toLocaleString()} unreadable record${badRecs === 1 ? '' : 's'} skipped` : '')
        // Many-newline fields are the signature of an unbalanced quote
        // swallowing the lines after it — a warning, not a verdict.
        + (suspect ? ` · ${suspect.toLocaleString()} row${suspect === 1 ? '' : 's'} with very long multi-line fields — check for a stray quote if the row count looks low` : ''),
        warn ? 8000 : 3500);
      setTimeout(() => { dismissedJobs.add(j.job_id); renderJobsPanel(); }, 8000);
      for (const sid of j.source_ids || []) offerTimestampColumns(sid);
      // A freshly imported table is scanned against the case's IOC
      // watchlist (auto-tagging matches); fire-and-forget, the watchlist
      // tab and the rail reflect it.
      scanWatchlistForSources(j.source_ids || []);
    } else if (j.status === 'error') {
      toast(`Import failed for ${j.name}: ${j.error}`, 8000);
    } else {
      toast(`Import of ${j.name} cancelled`, 3000);
      setTimeout(() => { dismissedJobs.add(j.job_id); renderJobsPanel(); }, 8000);
    }
  }
  if (!$('app').hidden) {
    // Imports only: a derive job's source_ids is the table it added a
    // column TO, and re-opening that would drop the cursor, the picks and
    // whatever page the analyst was on for a table they already have open.
    const landed = finishedNow.filter((j) => j.status === 'done' && j.kind !== 'derive' && (j.source_ids || []).length)
      .sort((a, b) => a.job_id - b.job_id);   // the server lists newest first; "first" means lowest id
    if (landed.length && S.appearance && S.appearance.openNewTables && !batchNavigated) {
      batchNavigated = true;
      try { await loadSources(landed[0].source_ids[0]); } catch {}
    } else if (finishedNow.some((j) => j.status === 'done')) {
      // navigate:false — a finished import refreshes the tab strip, the
      // sidebar and the dashboards, but never takes the analyst somewhere
      // they didn't ask to go (see loadSources). The setting above is the
      // one exception, and it spends itself on the batch's first table.
      try { await loadSources(undefined, { navigate: false }); } catch {}
    } else if (ftsWatch.size) {
      // Keep the index-build rows honest without loadSources()'s tab
      // re-select side effects (same reasoning as the Tables modal poll).
      try { await refreshSourcesQuietly(); } catch {}
    }
    // Rows just arrived, so every number on an open dashboard was worked
    // out before them. The board is only told when it LOADS otherwise,
    // which for an analyst who opened it to watch the import land is never.
    if (finishedNow.some((j) => j.status === 'done' && j.kind !== 'derive' && (j.source_ids || []).length)) {
      markDashboardStale();
    }
  }
  for (const src of S.sources || []) {
    if (src.fts_building) {
      if (ftsWatch.has(src.id)) continue;
      ftsWatch.add(src.id);
      // The open table's search hint names the build while it runs
      // (updateSearchHint) — it is only ever computed on demand, so the
      // start and the end of the build are the two moments to recompute it.
      if (src.id === S.sourceId) updateSearchHint();
    } else if (ftsWatch.has(src.id)) {
      ftsWatch.delete(src.id);
      if (src.has_fts) toast(`Search index ready for ${src.name}`, 3000);
      if (src.id === S.sourceId) updateSearchHint();
    }
  }
  renderJobsPanel();
  const batchActive = activeUploads.size > 0
    || ingestJobs.some((j) => j.status === 'running' || j.status === 'queued');
  // The batch is over once nothing is uploading or importing; the next
  // import may open its first table. An FTS build still being watched
  // keeps the poll alive but is not part of the batch.
  if (!batchActive) batchNavigated = false;
  if (batchActive || ftsWatch.size > 0) jobsPollTimer = setTimeout(pollJobs, 900);
}

/* `phase` is the badge text; `cls` is its class when the two differ (a
   plugin notice can say "thinking" while still being styled as running —
   free text never becomes a class name). `actions` is a row of buttons
   under the detail line. */
export function jobPanelRow({ label, phase, cls, pct, detail, indeterminate, done, onCancel, onDismiss, actions }) {
  const row = el('div', 'job-row');
  const head = el('div', 'job-head');
  head.append(el('span', 'job-name', label), el('span', 'job-phase ' + (cls || phase), phase));
  if (onCancel) {
    const x = el('button', 'job-x', '✕');
    x.title = 'Cancel';
    x.onclick = onCancel;
    head.append(x);
  }
  if (onDismiss) {
    const x = el('button', 'job-x', '✕');
    x.title = 'Dismiss';
    x.onclick = onDismiss;
    head.append(x);
  }
  row.append(head);
  if (!done) {
    const bar = el('div', 'job-bar' + (indeterminate ? ' indeterminate' : ''));
    const fill = el('div', 'job-bar-fill');
    if (!indeterminate) fill.style.width = `${Math.round(Math.min(1, pct || 0) * 100)}%`;
    bar.append(fill);
    row.append(bar);
  }
  if (detail) row.append(el('div', 'job-detail', detail));
  if (actions && actions.length) {
    const acts = el('div', 'job-actions');
    for (const a of actions) {
      const b = el('button', 'job-action', a.label);
      b.onclick = a.onClick;
      acts.append(b);
    }
    row.append(acts);
  }
  return row;
}

/* What a finished job's panel row says it did. An import's results carry
   `row_count` per source; a derive's carry `rows` (the length of the pass)
   and `parse_failures` per column, which is how a finished column build
   came to report "0 rows" — the sum was over a key its results never had. */
export function jobDoneDetail(j) {
  if (j.kind === 'derive') {
    const failed = (j.result || []).reduce((a, c) => a + (c.parse_failures || 0), 0);
    const rows = `${(j.rows_done || 0).toLocaleString()} rows`;
    return failed ? `${rows} · ${failed.toLocaleString()} not found` : rows;
  }
  return `${(j.result || []).reduce((a, r) => a + (r.row_count || 0), 0).toLocaleString()} rows`;
}

export function renderJobsPanel() {
  const panel = $('jobsPanel');
  if (!panel) return;
  panel.replaceChildren();
  let count = 0;
  /* The Clear all header goes first so it is the top of the scroller and
     can stick there — twenty finished rows overflow the panel's 50vh, and
     a button that scrolled away with them would be exactly as much work
     as the ✕s it replaces. It is never the only thing in the panel:
     anything it can clear is a row below it, so `count` still decides
     whether the panel shows at all. */
  if (clearableCount()) panel.append(clearAllRow());
  for (const [, u] of activeUploads) {
    panel.append(jobPanelRow({
      label: u.name, phase: 'uploading',
      pct: u.total ? u.loaded / u.total : 0,
      detail: u.total ? `${(u.loaded / 1048576).toFixed(1)} / ${(u.total / 1048576).toFixed(1)} MB` : '',
      onCancel: () => u.xhr.abort(),
    }));
    count++;
  }
  // Directory imports queue dozens of files at once. Show the ones
  // actually importing right now, roll the rest into one "N queued" row so
  // the in-progress files aren't buried below a long waiting list, and keep
  // finished/errored rows (they auto-dismiss). Running first, then the
  // queued summary, then the completed — so what's happening stays on top.
  //
  // The finished list is `clearableJob` itself, not a second copy of its
  // rule. The header above counts the rows Clear all will take; these are
  // those rows. Two spellings of one rule drift, and the drift is silent —
  // the count would describe a different set than the button removes, which
  // is the exact miscount clearAllRow says the clearable count exists to
  // avoid.
  const active = ingestJobs.filter((j) => !dismissedJobs.has(j.job_id) && j.status === 'running');
  const queued = ingestJobs.filter((j) => !dismissedJobs.has(j.job_id) && j.status === 'queued');
  const finished = ingestJobs.filter(clearableJob);
  for (const j of active) {
    const label = j.tables_total > 1
      ? `${j.name} — ${Math.min(j.tables_done + 1, j.tables_total)}/${j.tables_total}${j.current_table ? `: ${j.current_table}` : ''}`
      : j.name;
    panel.append(jobPanelRow({
      label, phase: 'importing',
      pct: j.units_total ? j.units_done / j.units_total : 0,
      indeterminate: !j.units_total,
      detail: j.rows_done ? `${j.rows_done.toLocaleString()} rows` : '',
      onCancel: () => post(`/api/ingest/jobs/${j.job_id}/cancel`, {}).then(startJobsPoll).catch(() => {}),
    }));
    count++;
  }
  if (queued.length) {
    panel.append(jobPanelRow({
      label: `${queued.length} queued`, phase: 'queued', indeterminate: false,
      detail: 'waiting to import',
      // Cancel the whole waiting batch at once — the common "I didn't mean
      // to import that many" recovery.
      onCancel: () => Promise.allSettled(
        queued.map((j) => post(`/api/ingest/jobs/${j.job_id}/cancel`, {}))).then(startJobsPoll),
    }));
    count++;
  }
  for (const j of finished) {
    panel.append(jobPanelRow({
      label: j.name, phase: j.status, done: true,
      detail: j.status === 'done' ? jobDoneDetail(j) : (j.error || ''),
      onDismiss: () => { dismissedJobs.add(j.job_id); renderJobsPanel(); },
    }));
    count++;
  }
  for (const n of pluginNotices.values()) {
    panel.append(noticeRow(n));
    count++;
  }
  for (const src of S.sources || []) {
    if (src.fts_building) {
      panel.append(jobPanelRow({ label: src.name, phase: 'indexing', indeterminate: true }));
      count++;
    }
  }
  panel.hidden = count === 0;
}

/* --------------------------------------------------------- plugin notices */

/* Rows a plugin puts in this panel through winnow.notify() — the same
   card an upload or import gets, so a plugin's background work reports
   in the one place the analyst already looks. Purely client-side: the
   plugin's mounted JS owns the row through a handle and drives it with
   update/done/fail/close. The handle goes inert (every call a no-op) once
   the row is closed, the owning mount is torn down (a reloaded plugin's
   click handlers are dead code), or the case switches — so a fetch that
   resolves late can never resurrect a row.

   `status` (running/done/error) is set by which handle method ran and is
   what the badge's colour follows; `phase` is optional free text the
   badge shows instead of the status word. A done row lingers
   NOTICE_LINGER_MS like a finished import unless it carries buttons or
   asked to be sticky — then it waits for the ✕, or for a button click,
   which also closes it. Error rows always wait.

   The third argument to createNotice is the app's own, not part of the
   plugin contract (plugins.js's `notify` passes `opts` through and
   nothing else): `onDismiss` runs after the ✕ has closed the row, for a
   row that stands for something the ✕ must act on rather than merely
   hide — a search running in the background is cancelled by it, and a
   finished one's result discarded, since a dismissed row would leave
   that search polling with nothing on screen to apply or drop it from. */
export const NOTICE_LINGER_MS = 8000;
export const pluginNotices = new Map();   // notice id -> record
let noticeSeq = 0;

function clearNoticeTimer(n) {
  if (n.timer) { clearTimeout(n.timer); n.timer = null; }
}

function applyNoticeOpts(n, o) {
  if (!o || typeof o !== 'object') return;
  if (o.title !== undefined) n.title = String(o.title);
  if (o.detail !== undefined) n.detail = o.detail == null ? '' : String(o.detail);
  if (o.phase !== undefined) n.phase = o.phase == null ? null : String(o.phase);
  // progress: a number clamps to 0..1 (a bar), null is an indeterminate
  // bar, undefined leaves it alone; `progress: false` removes the bar.
  if (o.progress !== undefined) n.progress = o.progress === null ? null : (o.progress === false ? undefined : Math.max(0, Math.min(1, Number(o.progress) || 0)));
  if (o.sticky !== undefined) n.sticky = !!o.sticky;
  if (o.actions !== undefined) {
    n.actions = (Array.isArray(o.actions) ? o.actions : [])
      .filter((a) => a && a.label)
      .map((a) => ({ label: String(a.label), onClick: typeof a.onClick === 'function' ? a.onClick : null }));
  }
}

export function closeNotice(id) {
  const n = pluginNotices.get(id);
  if (!n) return;
  clearNoticeTimer(n);
  pluginNotices.delete(id);
  renderJobsPanel();
}

/* Mount teardown (plugins.js disposePluginMount) — the owner key is the
   mount's, so a tab and a panel of the same plugin close only their own. */
export function closeNoticesOwnedBy(owner) {
  let any = false;
  for (const [id, n] of [...pluginNotices]) {
    if (n.owner === owner) { clearNoticeTimer(n); pluginNotices.delete(id); any = true; }
  }
  if (any) renderJobsPanel();
}

function noticeRow(n) {
  const row = jobPanelRow({
    label: n.title,
    phase: n.phase || n.status,
    cls: n.status,
    // The bar exists only while running with a progress value; a finished
    // row is text like a finished import's.
    done: n.status !== 'running' || n.progress === undefined,
    pct: n.progress || 0,
    indeterminate: n.progress === null,
    detail: n.detail,
    onDismiss: () => {
      closeNotice(n.id);
      if (n.onDismiss) { try { n.onDismiss(); } catch (e) { console.error(e); } }
    },
    actions: n.actions.map((a) => ({
      label: a.label,
      onClick: () => {
        closeNotice(n.id);
        if (a.onClick) { try { a.onClick(); } catch (e) { console.error(e); } }
      },
    })),
  });
  row.classList.add('job-notice');   // a plugin's row, for tests and styling alike
  return row;
}

/* A progress update rebuilds the panel like anything else — but a plugin
   reporting per-item progress calls update() in a tight loop, so those
   are coalesced to one repaint per frame. Create, done, fail and close
   stay synchronous: a caller (and a test) can look for the row at once. */
let noticeRaf = 0;
function renderJobsPanelSoon() {
  if (noticeRaf) return;
  noticeRaf = requestAnimationFrame(() => { noticeRaf = 0; renderJobsPanel(); });
}

export function createNotice(owner, opts = {}, { onDismiss = null, holdsResult = false } = {}) {
  const id = ++noticeSeq;
  const n = { id, owner, status: 'running', title: '', detail: '', phase: null, progress: undefined, actions: [], sticky: false, timer: null,
              onDismiss: typeof onDismiss === 'function' ? onDismiss : null,
              /* `holdsResult`: this row is not reporting, it is ASKING —
                 something is held for it somewhere and the row is the only
                 way to say yes or no. Clear all leaves those alone; see
                 clearFinishedNotices. Today the detached search is the only
                 one (view.js), holding a built view server-side until Apply
                 or Discard. In the third argument beside onDismiss, not in
                 opts, because that argument is the app's own — a plugin's
                 notify() passes opts through and nothing else, and a plugin
                 has no server-side result to hold. */
              holdsResult: !!holdsResult };
  applyNoticeOpts(n, opts);
  if (!n.title) n.title = String(owner).replace(/^[a-z]+:/, '');
  pluginNotices.set(id, n);
  renderJobsPanel();
  const live = () => pluginNotices.get(id) === n;
  const settle = (status, o) => {
    if (!live()) return handle;
    clearNoticeTimer(n);
    applyNoticeOpts(n, o);
    n.status = status;
    n.progress = undefined;
    if (status === 'done' && !n.sticky && !n.actions.length) n.timer = setTimeout(() => closeNotice(id), NOTICE_LINGER_MS);
    renderJobsPanel();
    return handle;
  };
  const handle = {
    get open() { return live(); },
    update(o = {}) {
      if (!live()) return handle;
      clearNoticeTimer(n);
      applyNoticeOpts(n, o);
      n.status = 'running';
      renderJobsPanelSoon();
      return handle;
    },
    done: (o = {}) => settle('done', o),
    fail: (o = {}) => settle('error', o),
    close() { closeNotice(id); },
  };
  return handle;
}

/* ------------------------------------------------------ clearing the panel */

/* Importing a folder queues one job per file, and every one of them leaves
   a finished row behind. The server keeps the last INGEST_JOB_KEEP (20)
   finished jobs, so the pile tops out at twenty rows rather than one per
   file — but twenty ✕ clicks after every folder is still twenty, and the
   errors are the ones that stay: a done row auto-dismisses after 8s and a
   failed one never does, so a folder holding a dozen files Winnow cannot
   read leaves a dozen rows waiting to be clicked away one at a time.

   It clears the rows the panel is only TELLING you about: finished
   imports, whether they landed, failed or were cancelled, and finished
   plugin notices. Two kinds are left alone.

   Anything still running or queued, because clearing notifications is not
   cancelling work. Those rows wear a ✕ that means Cancel — jobPanelRow
   gives onCancel and onDismiss the same glyph — and a Clear all that
   quietly killed a half-finished folder import would be a far worse bug
   than the one it fixes.

   And any FINISHED row that says it is holding something — `holdsResult`
   on createNotice. A search that ran in the background holds its rows
   server-side until Apply or Discard (view.js followPendingView), and
   "clear my notifications" is not an answer to that question, which is
   the same reason that row's own ✕ is a Discard rather than a plain
   dismiss. An error row is never asking, so it goes.

   `holdsResult` is a declaration and not an inference, because the
   obvious inference is wrong. "Finished and still carrying buttons" was
   tried first and it kept the wrong rows: the watchlist's hits alert
   (watchlist.js announceHits) is `done` + `sticky` + an Open watchlist
   button, and the bundled Claude plugin ends the same way — both are
   shortcuts to a tab, not questions, and neither holds anything. Worse,
   a folder import is what PRODUCES the watchlist alert (every landed
   import runs scanWatchlistForSources) and sticky-with-buttons also
   skips the linger timer, so the one row the feature exists to sweep up
   was the one row it refused to clear, while telling the analyst it was
   waiting for an answer. */
export function awaitingAnswer() {
  return [...pluginNotices.values()].filter((n) => n.status === 'done' && n.holdsResult);
}

/* Declarations, not const arrows: renderJobsPanel calls both
   clearableCount and clearableJob from 200 lines above this, and a const
   would leave these in the temporal dead zone for anything that painted
   the panel before the module finished evaluating. Nothing does today —
   every top-level side effect is main.js's, per CLAUDE.md — and this way
   nothing can. */
function clearableJob(j) {
  return !dismissedJobs.has(j.job_id) && j.status !== 'running' && j.status !== 'queued';
}

function clearableNotice(n) {
  return n.status !== 'running' && !(n.status === 'done' && n.holdsResult);
}

export function clearableCount() {
  return ingestJobs.filter(clearableJob).length
    + [...pluginNotices.values()].filter(clearableNotice).length;
}

export function clearFinishedNotices() {
  let cleared = 0;
  for (const j of ingestJobs) if (clearableJob(j)) { dismissedJobs.add(j.job_id); cleared++; }
  for (const [id, n] of [...pluginNotices]) {
    if (!clearableNotice(n)) continue;
    /* Deleted rather than closed, for two reasons.

       The repaint: once at the end instead of once per row, the same
       reason closeNoticesOwnedBy does it by hand.

       And onDismiss deliberately does NOT run. It means "the ✕ must act
       on this rather than merely hide it", which is only ever true of a
       row standing for something live — and those are exactly the rows
       kept above this line, for being `running` or `holdsResult`. What
       reaches here stands for nothing, so there is nothing to act on, and
       firing the handler anyway reaches PAST the row: the detached
       search's onDismiss is cancelPendingView(rec.sourceId), keyed by
       TABLE and not by record, so running it for a search that failed ten
       minutes ago cancels whatever search that table has in flight now.
       Clearing a stale receipt is not consent to kill live work. */
    clearNoticeTimer(n);
    pluginNotices.delete(id);
    cleared++;
  }
  if (cleared) renderJobsPanel();
  return cleared;
}

/* The panel's own header. Built only when there is something for it to
   clear: a panel showing one running import has nothing for this button
   to do, and a control that is usually a no-op is one people learn to
   ignore.

   The count is the CLEARABLE count, not the row count — the running
   import and the queued summary above it are not what the button acts
   on, and a header reading "11 finished" over a panel that only loses
   eight rows would be the button explaining itself wrongly.

   It does change as jobs land, inside #jobsPanel's aria-live region.
   That is not a new cost: renderJobsPanel replaceChildren()s the whole
   panel on every 900ms poll while a batch runs, so the region already
   re-announces everything it holds. Worth knowing before anything here
   is made quieter — the fix is at the panel level, not this line. */
function clearAllRow() {
  const row = el('div', 'jobs-clear');
  row.append(el('span', 'jobs-clear-count', `${clearableCount()} finished`));
  const btn = el('button', 'jobs-clear-btn', 'Clear all');
  btn.title = 'Dismiss the finished rows. Anything still running, and anything waiting on an answer, stays.';
  btn.onclick = () => {
    clearFinishedNotices();
    // Said only when something was left behind, since the panel emptying
    // reports the ordinary case by itself. A row that survived a button
    // labelled "Clear all" needs explaining; one that vanished does not.
    const waiting = awaitingAnswer().length;
    if (waiting) {
      toast(`Kept ${waiting} notification${waiting === 1 ? '' : 's'} `
        + `${waiting === 1 ? 'that is' : 'that are'} waiting for an answer`, 4000);
    }
  };
  row.append(btn);
  return row;
}

/* ----------------------------------------------------- cancellable ops */

/* One-shot client-generated handle for a cancellable server operation
   (view/timeline build, group summary — Store.cancel_op). The chip under
   the busy bar only appears once the op has been in flight ~1.2s: a fast
   rebuild finishing under that never flashes a cancel button at all. */
export const opToken = () => `op_${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;

export let opCancelCurrent = null;

export function armOpCancel(token, delay = 1200) {
  const btn = $('busyCancel');
  const show = () => {
    opCancelCurrent = token;
    btn.onclick = () => {
      btn.disabled = true;
      post('/api/cancel_op', { token }).catch(() => {}).finally(() => { btn.disabled = false; });
    };
    btn.disabled = false;
    btn.hidden = false;
  };
  // No delay means claim it NOW, not on the next turn of the event loop:
  // the only caller that asks for one is a build taking the chip over
  // from the build it just superseded (view.js), and that build's disarm
  // runs on the rejection its abort caused — a microtask, ahead of any
  // timer — so a zero timer here would still let the chip blink off.
  const timer = delay ? setTimeout(show, delay) : (show(), 0);
  return () => {
    clearTimeout(timer);
    if (opCancelCurrent === token) {
      $('busyCancel').hidden = true;
      opCancelCurrent = null;
    }
  };
}

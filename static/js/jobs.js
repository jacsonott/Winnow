/* Background ingest jobs and the cancellable-op token: the jobs panel, upload
progress, and armOpCancel.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, toast } from './core.js';
import { clientLog } from './errlog.js';
import { offerTimestampColumns } from './derived.js';
import { scanWatchlistForSources } from './watchlist.js';
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

/* Job ids and source ids both restart at 1 in a new case (`_ingest_job_seq`
   is per Store; `sources.id` is a plain INTEGER PRIMARY KEY). Everything
   above is keyed by one of them, so carrying it across a case switch makes
   case B's first import inherit case A's "already dismissed" and never
   show a progress row. Called from home.js openCase. */
export function resetJobState() {
  seenJobStatus.clear();
  dismissedJobs.clear();
  ftsWatch.clear();
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
  const indexing = (S.sources || []).filter((s) => s.fts_building).length;
  if (indexing) bits.push(`${indexing} index build${indexing === 1 ? '' : 's'}`);
  if (opCancelCurrent) bits.push('a running query');
  return bits;
}

export function startJobsPoll() {
  if (!jobsPollTimer) pollJobs();
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
    if (finishedNow.some((j) => j.status === 'done')) {
      // navigate:false — a finished import refreshes the tab strip, the
      // sidebar and the dashboards, but never takes the analyst somewhere
      // they didn't ask to go (see loadSources).
      try { await loadSources(undefined, { navigate: false }); } catch {}
    } else if (ftsWatch.size) {
      // Keep the index-build rows honest without loadSources()'s tab
      // re-select side effects (same reasoning as the Tables modal poll).
      try { await refreshSourcesQuietly(); } catch {}
    }
  }
  for (const src of S.sources || []) {
    if (src.fts_building) ftsWatch.add(src.id);
    else if (ftsWatch.has(src.id)) {
      ftsWatch.delete(src.id);
      if (src.has_fts) toast(`Search index ready for ${src.name}`, 3000);
    }
  }
  renderJobsPanel();
  const active = activeUploads.size > 0
    || ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')
    || ftsWatch.size > 0;
  if (active) jobsPollTimer = setTimeout(pollJobs, 900);
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
  const active = ingestJobs.filter((j) => !dismissedJobs.has(j.job_id) && j.status === 'running');
  const queued = ingestJobs.filter((j) => !dismissedJobs.has(j.job_id) && j.status === 'queued');
  const finished = ingestJobs.filter((j) => !dismissedJobs.has(j.job_id)
    && j.status !== 'running' && j.status !== 'queued');
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
   which also closes it. Error rows always wait. */
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
    onDismiss: () => closeNotice(n.id),
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

export function createNotice(owner, opts = {}) {
  const id = ++noticeSeq;
  const n = { id, owner, status: 'running', title: '', detail: '', phase: null, progress: undefined, actions: [], sticky: false, timer: null };
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
      renderJobsPanel();
      return handle;
    },
    done: (o = {}) => settle('done', o),
    fail: (o = {}) => settle('error', o),
    close() { closeNotice(id); },
  };
  return handle;
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
  const timer = setTimeout(() => {
    opCancelCurrent = token;
    btn.onclick = () => {
      btn.disabled = true;
      post('/api/cancel_op', { token }).catch(() => {}).finally(() => { btn.disabled = false; });
    };
    btn.disabled = false;
    btn.hidden = false;
  }, delay);
  return () => {
    clearTimeout(timer);
    if (opCancelCurrent === token) {
      $('busyCancel').hidden = true;
      opCancelCurrent = null;
    }
  };
}

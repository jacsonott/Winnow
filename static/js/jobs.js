/* Background ingest jobs and the cancellable-op token: the jobs panel, upload
progress, and armOpCancel.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, toast } from './core.js';
import { offerTimestampColumns } from './derived.js';
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
  } catch { ingestJobs = []; }

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
    } else if (j.status === 'error') {
      toast(`Import failed for ${j.name}: ${j.error}`, 8000);
    } else {
      toast(`Import of ${j.name} cancelled`, 3000);
      setTimeout(() => { dismissedJobs.add(j.job_id); renderJobsPanel(); }, 8000);
    }
  }
  if (!$('app').hidden) {
    if (finishedNow.some((j) => j.status === 'done')) {
      try { await loadSources(); } catch {}
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

export function jobPanelRow({ label, phase, pct, detail, indeterminate, done, onCancel, onDismiss }) {
  const row = el('div', 'job-row');
  const head = el('div', 'job-head');
  head.append(el('span', 'job-name', label), el('span', 'job-phase ' + phase, phase));
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
  for (const j of ingestJobs) {
    if (dismissedJobs.has(j.job_id)) continue;
    const running = j.status === 'running' || j.status === 'queued';
    const label = j.tables_total > 1
      ? `${j.name} — ${Math.min(j.tables_done + 1, j.tables_total)}/${j.tables_total}${j.current_table ? `: ${j.current_table}` : ''}`
      : j.name;
    if (running) {
      panel.append(jobPanelRow({
        label, phase: j.status === 'queued' ? 'queued' : 'importing',
        pct: j.units_total ? j.units_done / j.units_total : 0,
        indeterminate: !j.units_total,
        detail: j.rows_done ? `${j.rows_done.toLocaleString()} rows` : '',
        onCancel: () => post(`/api/ingest/jobs/${j.job_id}/cancel`, {}).then(startJobsPoll).catch(() => {}),
      }));
    } else {
      panel.append(jobPanelRow({
        label: j.name, phase: j.status, done: true,
        detail: j.status === 'done' ? jobDoneDetail(j) : (j.error || ''),
        onDismiss: () => { dismissedJobs.add(j.job_id); renderJobsPanel(); },
      }));
    }
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

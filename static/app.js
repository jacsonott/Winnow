/* Winnow — virtualized grid over a materialised SQLite view.
   Rows are fetched in pages of PAGE and cached; only the visible window is
   ever in the DOM, so a 20M-row view scrolls the same as a 2k-row one. */

/* Fixed per-request cost (uvicorn + JSON encode) dominates at 500 — measured:
   500 rows/request is 288ms per 10k rows scrolled, 5000 rows/request is
   103ms/10k (~2.8x), 10,000 rows/request is 79ms/10k. 5000 takes most of
   that win without pushing per-request JSON payload size as far, for a
   proportionally smaller further gain. MAX_CACHED_PAGES below is sized off
   this value — keep them paired. */
const PAGE = 5000;
const OVERSCAN = 12;
const ROW_H_COMFORTABLE = 24;
const ROW_H_COMPACT = 20;
let ROW_H = ROW_H_COMFORTABLE; // mutable — the Appearance density setting changes this at runtime, see applyDensity()

/* Ceiling on the virtualized spacer's height. The spacer (#spacerY,
   #timelineSpacerY) is what gives the scroller its scrollable height —
   row_count * ROW_H — but a DOM element can't be arbitrarily tall. Blink
   clamps at 33,554,365px (measured; 2^25 LayoutUnits) and Gecko lower still
   (~17.9M), and past the clamp the spacer stops growing while the row count
   doesn't: scrollTop stops mapping onto rows 1:1 and the tail of the view
   becomes unreachable. Measured on a 2,459,653-row $J table, which wants
   59,031,672px of spacer: scrolling bottomed out at row ~1,398,090, hiding
   43% of the evidence with no indication anything was missing.
   So the spacer is capped below every engine's limit, and above the cap every
   conversion between scrollTop and a row goes through vScroll()/rScroll()
   rather than a bare multiply or divide by ROW_H. Below the cap those are
   the identity, so nothing changes for a table that already fit.
   What the cap costs above it is scroll *granularity*, not reach: 2.46M rows
   get ~6.5px of spacer per row instead of 24, so a wheel notch travels ~3.7x
   further. Every row stays individually addressable (that needs 1px/row; the
   cap doesn't reach it until ~16M rows), and keyboard navigation moves by row
   rather than by pixel, so it's unaffected either way. */
const MAX_SPACER_PX = 16000000;
const GUTTER_W = 104; // keep in sync with `.gutter { width: ... }` in style.css
/* Default ceiling for autofit-to-content column widths, overridable (and
   removable) per browser under Settings → Appearance — see autofitMaxWidth.
   A cap exists because one pathological column can otherwise decide the
   width of the whole grid: a CommandLine full of base64 is tens of
   thousands of characters, and the rows are `width: max-content`, so an
   uncapped fit makes every horizontal scroll of every other column a
   journey. 900px is wide enough for a full Windows path, which 480 wasn't. */
const AUTOFIT_MAX_W_DEFAULT = 900;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

const S = {
  sources: [],
  sourceId: null,
  columns: [],          // [{name, type}] plus, for analyst-added derived columns, {derived, derived_from, derived_op, derived_status, derived_id, parse_failures}
  appSettings: {},      // system-wide prefs (workspace/app_settings.json) — currently default_ts_format
  caseSettings: {},     // this case's own overrides (case_settings table) — currently ts_format
  layout: {},           // name -> {w, hidden, pinned, tsFormat, durFormat, valuePicker}
  order: [],            // column names in display order
  valueFilterMode: 'auto', // 'auto' (on under VALUE_FILTER_AUTO_MAX rows) | 'on' | 'off' — this table's value-picker default, per-column overrides live in S.layout
  filters: {},          // name -> raw filter text
  sort: [],             // [{column, dir}]
  search: '',
  searchMode: 'contains', // 'contains' | 'regex' | 'advanced'
  searchTerms: [],        // advanced mode: [{term, connector: 'AND'|'OR', exclude: bool}]
  filterTree: { type: 'group', op: 'AND', children: [] }, // guided filter builder
  tagFilter: [],        // tag ids, or ['__any__'] / ['__none__']
  view: null,           // {view_id, row_count}
  jumpTs: { value: '', column: null }, // "jump to timestamp" target — deliberately NOT reset by openSource, so the same moment is reachable in every table
  lastGroupBy: null,     // {cols, sort, dir} — what toggleGrouping restores
  pages: new Map(),      // page index -> rows; capped, see MAX_CACHED_PAGES/trimPageCache
  pending: new Map(),    // page index -> in-flight fetch promise, so concurrent callers share one request
  pageGen: 0,            // bumped by clearPageCache() so an in-flight fetch issued before it can't repopulate stale rows
  tags: [],
  tagCounts: {},          // tag id -> tagged rows *in the current view* — what the ribbon shows
  tagCountsAll: {},       // tag id -> tagged rows in the whole table, for the "of N" half of the tooltip
  cursor: -1,           // position in view
  /* Row selection is a flag plus a Set, never a materialized list of every
     selected position — see the sel* helpers below for the full contract.
     selectAll=false: `selection` holds the selected positions.
     selectAll=true:  every position in the view is selected EXCEPT the ones
                      in `selection`. */
  selectAll: false,
  selection: new Set(),
  anchor: -1,
  rowsByPos: new Map(),
  reqId: 0,
  viewCache: new Map(), // source_id -> { key, view_id, row_count, elapsed_ms }
  cellAnchor: null,      // {pos, col} — drag start, col is an index into visibleCols()
  cellRange: null,       // {r0, c0, r1, c1} normalized — separate from row S.selection
  importQueue: [],       // [{file, kind: 'csv'|'json', configured, ...kind-specific settings}] — Import modal's file queue
  groupByCols: [],         // ordered column names — [] for normal flat mode, nested grouping otherwise
  groupSort: 'count',      // 'count' | 'value' — how each level's groups are ordered
  groupSortDir: 'desc',    // 'asc' | 'desc' — direction within groupSort
  preGroupOrder: null,     // S.order snapshot from just before the first column was dragged into grouping
  groups: [],              // flat array reflecting the currently-visible tree — see the group-by section below
  groupPrefix: [],         // prefix sums: virtual position where each group's header row starts
  groupTotalRows: 0,
  groupPages: new Map(),   // `${viewId}:${pageIndex}` -> rows array, for expanded leaf groups' data
  groupPending: new Map(),  // same key -> the in-flight fetch promise, so concurrent callers share one request
  groupPageGen: 0,         // bumped by clearGroupPageCache so an in-flight fetch can't repopulate stale rows
  savedFilters: [],        // cross-case cyclable filters, loaded from workspace/filters.json
  headerNicknames: [],     // [{id, col_names, nickname}] — friendly names for a header set
  timeRange: { enabled: false, column: null, start: '', end: '' }, // survives filter/preset/tab changes on purpose — see toggleTimeRange
  timelineTemplates: [], // [{id, col_names, type_label, timestamp_column, body_columns}] — workspace/timeline_templates.json
  importProfiles: [],    // [{id, name, extensions, include_patterns, exclude_patterns, recursive}] — workspace/import_profiles.json
  plugins: [],           // loaded plugin records from GET /api/plugins — name/version/error, for the Plugins modal
  pluginFormats: [],     // plugin-registered ingest formats (extensions/patterns/options) — routes files to plugin parsers
  pluginTabs: [],        // plugin-registered pinned tabs [{id, plugin, plugin_fs, label, entry, gen}] — see showPluginTab
  pluginDirs: [],        // where the server loads plugins from — shown in the Plugins modal so "drop it where?" has an answer
  sidebarFilter: '',      // substring filter typed into the sidebar's own search box
  timeline: {
    view: null, pages: new Map(), pending: new Set(), reqId: 0,
    tagFilter: null, // tag ids currently checked; null = not yet initialized (defaults to "every known tag" on first load)
  },
  savedFilterCursor: -1,   // index into filtersForCurrentSource(), for [ and ] cycling
  cases: [],               // home screen's case registry, from workspace/cases.json
  homeSearch: '',          // home screen's case/group name filter, persisted across re-renders
  homeShowOlder: false,    // reveals cases not opened in >30 days once toggled
  activeTab: 'grid',       // 'grid' or a page tab's key ('sql' | 'timeline' | 'plugin:<id>') — which of #grid/#sqlview/#timelineview/.pluginview is up
  tabOrder: [],            // source/merge ids, drag-reordered — ids not listed here sort after, in loadSources() order
  pageTabPrefs: null,      // {order, width} for the page-tab strip, from localStorage — set below, see loadPageTabPrefs
  sqlTabs: [],             // [{id, name, sql, pos}] from the case file's sql_tabs table (see showSqlTab)
  sqlTabId: null,          // which sql tab the editor/result pane is currently showing
  sqlResults: new Map(),   // sql tab id -> last {columns, rows, elapsed_ms, truncated} | {error}, in memory only
  searchAll: null,         // in-flight/finished Search-all job — see searchAllState(); survives closing the modal
};

/* ------------------------------------------------------- row selection */

/* Nothing outside this block touches S.selection/S.selectAll directly.
   "Select all" on a 1.2M-row view used to mean literally
   `for (p = 0; p < row_count; p++) S.selection.add(p)` — a multi-million
   entry Set allocated to express "everything", which then had to be
   materialized again as an array by every consumer. Inverting the Set when
   the flag is on makes the common cases O(1) and, more importantly, makes
   "the whole view is selected" something the tagging path can *recognise*
   and hand to the server as one bulk operation rather than a list of rids
   it only has page-cached a few hundred of (see applyTag). */

/* How many addressable row positions the grid has right now — the view's
   rows in flat mode, the flattened group tree's rows (data rows *and* group
   headers) in grouped mode. Every bound on the shared position space goes
   through this rather than reaching for S.view.row_count, which is only
   half the answer once a grouping is on. */
const gridRowCount = () => (S.groupByCols.length ? S.groupTotalRows : S.view ? S.view.row_count : 0);
const selViewRows = () => gridRowCount();

function selCount() {
  return S.selectAll ? Math.max(0, selViewRows() - S.selection.size) : S.selection.size;
}
const selHas = (pos) => (S.selectAll ? !S.selection.has(pos) : S.selection.has(pos));
const selAdd = (pos) => { S.selectAll ? S.selection.delete(pos) : S.selection.add(pos); };
const selRemove = (pos) => { S.selectAll ? S.selection.add(pos) : S.selection.delete(pos); };
const selToggle = (pos) => { selHas(pos) ? selRemove(pos) : selAdd(pos); };
function selClear() { S.selectAll = false; S.selection.clear(); }
function selSetAll() { S.selectAll = true; S.selection.clear(); }
function selSetRange(from, to) {
  S.selectAll = false;
  S.selection.clear();
  const lo = Math.min(from, to), hi = Math.max(from, to);
  // Grouped mode's position space interleaves group-header rows with data
  // rows, and a header isn't a row anything can tag or copy. groupCoordAt
  // answers that from the tree alone (no page fetch), so a range drag over
  // three groups selects their rows and none of their headings — and
  // selCount() stays a count of real rows.
  for (let p = lo; p <= hi; p++) {
    if (S.groupByCols.length && !groupCoordAt(p)) continue;
    S.selection.add(p);
  }
}
/* The lowest selected position, without materializing the rest. */
function selFirst() {
  if (!S.selectAll) return S.selection.size ? Math.min(...S.selection) : -1;
  for (let p = 0; p < selViewRows(); p++) if (!S.selection.has(p)) return p;
  return -1;
}
/* Materializes ascending. Only for paths that genuinely need every position
   (copy, and tagging an explicit subset), and those check selCount() first —
   the cheap answer to "how many" — rather than building this to measure it. */
function selPositions() {
  if (!S.selectAll) return [...S.selection].sort((a, b) => a - b);
  const out = [];
  for (let p = 0, n = selViewRows(); p < n; p++) if (!S.selection.has(p)) out.push(p);
  return out;
}
/* Rewrites every selected position through `fn`, dropping the ones it maps
   to null. The one mutation that isn't add/remove/clear: grouped mode's
   positions are indices into a tree whose shape changes under expand and
   collapse, and the selection has to move with it (see
   shiftGroupPositions). Lives here so the "nothing outside this block
   touches S.selection" rule keeps holding. */
function selRemap(fn) {
  const moved = new Set();
  for (const pos of S.selection) {
    const to = fn(pos);
    if (to !== null && to !== undefined) moved.add(to);
  }
  S.selection = moved;
}
/* Positions explicitly unchecked out of a select-all. Empty unless selectAll. */
const selExcludedPositions = () => (S.selectAll ? [...S.selection] : []);
/* Those same rows as [source_id, rid] pairs, for the bulk tag endpoint —
   or null if any of them isn't in the page cache. They're rows the analyst
   unchecked on screen, so they're cached by construction, but silently
   dropping one would tag a row that was explicitly deselected; the caller
   fetches and retries rather than guessing. */
function selExcludedPairs() {
  const pairs = [];
  for (const pos of S.selection) {
    const r = rowAt(pos);
    if (!r) return null;
    pairs.push([r.source_id ?? S.sourceId, r.rid]);
  }
  return pairs;
}

function cellInRange(pos, ci) {
  if (!S.cellRange) return false;
  const { r0, r1, c0, c1 } = S.cellRange;
  return pos >= r0 && pos <= r1 && ci >= c0 && ci <= c1;
}

const specKey = (spec) => JSON.stringify(spec);

/* ------------------------------------------------------------------ net */

/* Every non-GET call carries this header — it's what the server's CSRF
   middleware checks for (see server.py's require_client_header). A
   same-origin request always allows a custom header; a cross-origin one
   can't add it without triggering a CORS preflight, which fails since this
   app sends no CORS allow-headers. GETs are left alone since they're
   read-only and this header would break the plain-navigation download links
   (Export, session/filters export) that can't set custom headers at all. */
async function api(path, opts) {
  const o = { ...opts };
  if (o.method && o.method !== 'GET') {
    o.headers = { ...(o.headers || {}), 'X-Timeline-Lite-Client': '1' };
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText, detail = null;
    // FastAPI's `detail` is usually a string, but a few routes raise a
    // structured one the caller needs to branch on (case-in-use, below).
    // Keep both: `message` stays the human string every existing catch
    // prints, `detail` carries the object when there is one.
    try {
      detail = (await r.json()).detail;
      if (typeof detail === 'string') msg = detail;
      else if (detail && detail.message) msg = detail.message;
    } catch {}
    // The status rides along so callers can tell "you asked for something
    // invalid" (4xx) from "the server broke" (5xx) — the server is careful
    // to only 400 the former, and blaming an analyst's filter for a backend
    // defect sends them off fixing something that isn't wrong.
    const err = new Error(msg);
    err.status = r.status;
    err.detail = detail;
    throw err;
  }
  return r.json();
}
const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

/* Thin top-of-viewport progress indicator for anything that can take a
   real amount of time on a large source (a view rebuild — filter/sort/
   search — is the one that matters most; scroll paging is deliberately left
   out since it's normally sub-page-latency and would just flicker). A
   counter, not a boolean, so overlapping calls can't have one's `finally`
   hide the bar while another is still in flight. */
let busyCount = 0;
function setBusy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  $('busyBar').hidden = busyCount === 0;
}

function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* A toast with one thing you can do about it. Longer-lived than a plain
   toast (it's only useful if it's still there when you look up) and
   dismissed by acting on it. */
function toastAction(msg, label, onclick, ms = 12000) {
  const t = $('toast');
  t.replaceChildren(el('span', null, msg));
  const btn = el('button', 'toast-action', label);
  btn.onclick = () => { t.hidden = true; onclick(); };
  t.append(btn);
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

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
const activeUploads = new Map(); // clientId -> {name, loaded, total, xhr}
let uploadSeq = 0;
let ingestJobs = [];
let jobsPollTimer = null;
const seenJobStatus = new Map(); // job_id -> last status, for transition toasts
const dismissedJobs = new Set();
const ftsWatch = new Set();      // source ids seen building, for the "ready" toast

function uploadWithProgress(url, fd, name) {
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

/* Same-host path recovery: the browser sandbox never reveals a picked
   file's real path, but when this page is served from the same machine,
   the picked file is somewhere on the server's own disk — so send a
   fingerprint (name, size, mtime, first/last 64 KB via File.slice — two
   tiny reads even on a 50 GB file) and let the server look for it in the
   places files usually come from. A hit skips the entire upload copy; a
   miss (remote client, unguessable directory) returns null and the
   caller falls back to the normal upload. Deliberately silent either
   way — one Import button, two transports, no decision for the analyst. */
async function resolveLocalFile(file) {
  try {
    const fd = new FormData();
    fd.append('name', file.name);
    fd.append('size', String(file.size));
    fd.append('mtime_ms', String(file.lastModified));
    fd.append('head', file.slice(0, 65536));
    fd.append('tail', file.slice(Math.max(0, file.size - 65536)));
    const res = await api('/api/ingest/resolve_local', { method: 'POST', body: fd });
    return res.path || null;
  } catch {
    return null; // resolution is an optimization, never a failure mode
  }
}

function startJobsPoll() {
  if (!jobsPollTimer) pollJobs();
}

async function pollJobs() {
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
      if (done && !firstPoll && prev !== j.status) finishedNow.push(j);
      // Jobs that were already finished before this page ever polled
      // (server keeps the last 20) are history, not news — don't toast
      // them and don't fill the panel with them on load.
      if (done && firstPoll) dismissedJobs.add(j.job_id);
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
      toast(`${j.name}: ${total.toLocaleString()} rows imported${ragged ? ` · ${ragged.toLocaleString()} ragged rows padded/trimmed` : ''}`, ragged ? 6000 : 3500);
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

function jobPanelRow({ label, phase, pct, detail, indeterminate, done, onCancel, onDismiss }) {
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
function jobDoneDetail(j) {
  if (j.kind === 'derive') {
    const failed = (j.result || []).reduce((a, c) => a + (c.parse_failures || 0), 0);
    const rows = `${(j.rows_done || 0).toLocaleString()} rows`;
    return failed ? `${rows} · ${failed.toLocaleString()} not found` : rows;
  }
  return `${(j.result || []).reduce((a, r) => a + (r.row_count || 0), 0).toLocaleString()} rows`;
}

function renderJobsPanel() {
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
const opToken = () => `op_${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
let opCancelCurrent = null;

function armOpCancel(token, delay = 1200) {
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

/* -------------------------------------------------------------- filters */

/* Compact filter syntax, typed straight into the column box:
     foo      contains          !foo    does not contain
     =foo     exact             ^foo    starts with
     >10 <10 >=10 <=10          /re/    regex
     ""       empty             *       not empty
     a|b|c    any of            */
function parseFilter(raw) {
  const s = raw.trim();
  if (!s) return null;
  if (s === '""' || s === "''") return { op: 'empty', value: 'x' };
  if (s === '*') return { op: 'not_empty', value: 'x' };
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) return { op: 'regex', value: s.slice(1, -1) };
  const m = s.match(/^(!=|>=|<=|[!=^><])(.*)$/);
  if (m && m[2] !== '') {
    const map = { '!': 'not_contains', '=': 'equals', '^': 'starts', '!=': 'not_equals' };
    return { op: map[m[1]] || m[1], value: m[2].trim() };
  }
  if (s.includes('|')) return { op: 'in', value: s.split('|').map((x) => x.trim()).filter(Boolean) };
  return { op: 'contains', value: s };
}

function currentSpec() {
  const filters = [];
  for (const [column, raw] of Object.entries(S.filters)) {
    const p = parseFilter(raw);
    if (p) filters.push({ column, ...p });
  }
  return {
    source_id: S.sourceId,
    filters,
    sort: S.sort,
    search: S.searchMode === 'advanced' ? '' : S.search,
    search_mode: S.searchMode,
    search_terms: S.searchMode === 'advanced' ? S.searchTerms : [],
    filter_tree: (S.filterTree.children && S.filterTree.children.length) || S.filterTree.type === 'raw' ? S.filterTree : null,
    tags: S.tagFilter,
    time_range: S.timeRange,
  };
}

/* --------------------------------------------------------------- sources */

/* S.sources holds EVERY source/merge in the case (the merge builder, tag
   filter, etc. all need the full set) — the tab strip only renders the
   subset that's "open" (s.is_open), so a case with a dozen imported tables
   doesn't turn into a dozen permanent tabs. Closing a tab (below) just
   toggles that flag; the source/merge and its tags/notes are untouched.
   Errored merges are always shown regardless of is_open — they need
   attention (fix or remove), not a place to hide. */

/* The name a source shows everywhere: the analyst's nickname when one is
   set, else the imported file's name. s.name itself is never rewritten —
   it's the record of what was imported (session hash warnings, the Tables
   manager's identity column), where a nickname is presentation. Merges
   have no nickname field (their name is already analyst-chosen), so this
   falls through to name for them. */
function sourceLabel(s) { return (s && (s.nickname || s.name)) || ''; }

/* The hover title for a nicknamed source — keeps the real file name one
   hover away wherever the nickname replaced it. */
function sourceTitle(s, suffix) {
  const parts = [];
  if (s && s.nickname) parts.push(s.name);
  if (suffix) parts.push(suffix);
  return parts.join(' — ');
}

/* Prompt-and-save for a source's nickname (a merge's name — merges have no
   separate file name to fall back to). Returns true when a change was
   saved, so callers know to re-render whatever list they came from. */
async function editSourceNickname(s) {
  const msg = s.is_merge
    ? 'Rename this merge:'
    : `Nickname for "${s.name}" — shown in place of the file name everywhere. Leave empty to go back to the file name.`;
  const cur = s.is_merge ? s.name : (s.nickname || '');
  const v = await promptDialog(msg, cur, { okLabel: 'Save' });
  if (v === null) return false;
  try {
    await post(`/api/source/${s.id}/nickname`, { nickname: v });
  } catch (e) {
    toast('Could not save that: ' + e.message, 4000);
    return false;
  }
  await loadSources();
  return true;
}

/* Open tabs sorted per S.tabOrder (drag-reordered by the user) — ids not
   yet in tabOrder (a newly opened table) sort after the ones that are, in
   whatever order loadSources()'s API responses returned them. */
function openTabsSorted() {
  const openTabs = S.sources.filter((s) => s.error || s.is_open);
  return openTabs.slice().sort((a, b) => {
    const ia = S.tabOrder.indexOf(a.id), ib = S.tabOrder.indexOf(b.id);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

/* Shared by the tab strip's own ✕ and the tab-jump dropdown's close
   action — hides the tab (stays in the case, reopen from Tables or the
   dropdown's Closed section), doesn't delete anything. */
async function closeTab(s) {
  await post(`/api/source/${s.id}/open`, { open: false });
  if (S.sourceId === s.id) S.sourceId = null;
  await loadSources();
}

/* Moves an open tab earlier/later in S.tabOrder — the same state
   wireTabDrag's drop handler mutates, just via a menu action instead of a
   drag gesture. No-ops silently at either end (dir would move it past the
   first/last position) rather than wrapping. */
function moveTab(id, dir) {
  const ids = openTabsSorted().map((s) => s.id);
  const idx = ids.indexOf(id);
  const swapIdx = idx + dir;
  if (swapIdx < 0 || swapIdx >= ids.length) return;
  [ids[idx], ids[swapIdx]] = [ids[swapIdx], ids[idx]];
  S.tabOrder = ids;
  renderTabs();
}

/* Native HTML5 drag-and-drop, same technique as wireColumnDrag above —
   draggedTabId tracked in a closure var since dataTransfer.getData isn't
   readable during dragover in most browsers. Reordering only touches
   S.tabOrder + a re-render, no server round trip.

   Shared by the horizontal tab strip (wireTabDrag) and the sidebar's
   vertical list (wireSidebarRowDrag) — same reorder semantics, just
   measured along a different axis (tab strip: left/right of the pointer
   vs. the node's horizontal midpoint; sidebar: above/below its vertical
   midpoint). draggedTabId is deliberately one shared variable rather than
   a copy per axis: starting a drag on a tab and dropping it on a sidebar
   row (or vice versa) still reorders correctly, since both render from
   the same openTabsSorted() and mutate the same S.tabOrder.

   `currentIds`/`onReorder` default to the source-tab behaviour so the two
   original callers stay one-liners; the SQL pane's sub-tab strip passes
   its own pair to reorder S.sqlTabs (and persist to the case file)
   instead. draggedTabId staying shared across all three surfaces is
   harmless — a cross-surface drop can't resolve an id the target's own
   currentIds() doesn't contain, so it no-ops. */
let draggedTabId = null;

function wireDragReorder(node, id, {
  containerSelector, rowSelector, horizontal,
  currentIds = () => openTabsSorted().map((s) => s.id),
  onReorder = (ids) => { S.tabOrder = ids; renderTabs(); },
}) {
  node.draggable = true;
  node.addEventListener('dragstart', (e) => {
    draggedTabId = id;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(id));
    node.classList.add('dragging');
  });
  node.addEventListener('dragend', () => {
    draggedTabId = null;
    document.querySelectorAll(`${containerSelector} ${rowSelector}.dragging, ${containerSelector} ${rowSelector}.drop-before, ${containerSelector} ${rowSelector}.drop-after`)
      .forEach((n) => n.classList.remove('dragging', 'drop-before', 'drop-after'));
  });
  node.addEventListener('dragover', (e) => {
    if (draggedTabId == null || draggedTabId === id) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const r = node.getBoundingClientRect();
    const before = horizontal ? e.clientX < r.left + node.offsetWidth / 2 : e.clientY < r.top + node.offsetHeight / 2;
    node.classList.toggle('drop-before', before);
    node.classList.toggle('drop-after', !before);
  });
  node.addEventListener('dragleave', () => node.classList.remove('drop-before', 'drop-after'));
  node.addEventListener('drop', (e) => {
    e.preventDefault();
    const dragged = draggedTabId;
    const before = node.classList.contains('drop-before');
    node.classList.remove('drop-before', 'drop-after');
    if (dragged == null || dragged === id) return;
    const ids = currentIds();
    const from = ids.indexOf(dragged);
    if (from === -1) return; // dropped from a different reorderable surface
    ids.splice(from, 1);
    let idx = ids.indexOf(id);
    if (!before) idx += 1;
    ids.splice(idx, 0, dragged);
    onReorder(ids);
  });
}

function wireTabDrag(t, id) {
  wireDragReorder(t, id, { containerSelector: '#sourceTabs', rowSelector: '.tab', horizontal: true });
}

function wireSidebarRowDrag(row, id) {
  wireDragReorder(row, id, { containerSelector: '#sidebarList', rowSelector: '.sidebar-row', horizontal: false });
}

function renderTabs() {
  const openTabs = openTabsSorted();
  const tabs = $('sourceTabs');
  tabs.replaceChildren();
  for (const s of openTabs) {
    const t = el('button', 'tab' + (s.is_merge ? ' tab-merge' : ''));
    t.dataset.id = String(s.id);
    // Same condition syncTabSelection applies, for the same reason: a
    // background loadSources() (an import finishing, say) can land while a
    // page tab is showing, and S.sourceId still names a table then.
    t.setAttribute('aria-selected', String(S.activeTab === 'grid' && s.id === S.sourceId));
    if (s.error) {
      t.append(el('span', null, `⚠ ${s.name}`));
      t.title = s.error;
    } else {
      t.append(el('span', null, (s.is_merge ? '⛓ ' : '') + sourceLabel(s)), el('span', 'count', s.row_count.toLocaleString()));
    }
    const x = el('span', 'x', '✕');
    x.title = 'Close tab — stays in this case, reopen it from Tables';
    x.onclick = async (e) => { e.stopPropagation(); await closeTab(s); };
    t.append(x);
    if (!s.error) {
      t.onclick = () => openSource(s.id);
      // Right-click is where the table menu lives now (the ▦ column-chooser
      // button that used to sit here opened a strict subset of it) — same
      // menu from the tab, the sidebar row and the openTableMenu keybind.
      t.oncontextmenu = (e) => { e.preventDefault(); openTableMenu(s.id); };
      t.title = sourceTitle(s, 'Right-click for the table menu');
    }
    wireTabDrag(t, s.id);
    tabs.append(t);
  }
  renderSidebar(); // every caller here (loadSources, moveTab, the drag-drop handler) means S.sources or S.tabOrder just changed
  return openTabs;
}

/* -------------------------------------------------------------- page tabs */

/* The tabs that aren't tables — SQL, Timeline, and whatever pinned tabs
   plugins registered (plugin_api.register_tab). They have their own strip
   (#pageTabs) beside the table strip, and are reordered by the same
   gestures tabs are: drag along the strip, or drag/▲/▼ in the sidebar.
   Two things differ from S.tabOrder's table tabs, both following from what
   a page tab *is*:

   - It's identified by a string key ('sql' | 'timeline' | 'plugin:<id>'),
     not a numeric source id. That's also what keeps the shared
     wireDragReorder honest between the two strips with one draggedTabId
     between them: a table tab dropped on the page strip resolves to no
     index in that strip's currentIds() and no-ops, and vice versa.
   - Order (and the strip's width) persist in localStorage
     'winnow.pagetabs', next to winnow.sidebar/winnow.detail, instead of
     dying with the case the way S.tabOrder does. "SQL" means the same
     thing in every case, so nothing about switching cases should move it
     back — and a plugin tab belongs to this machine's plugins folder, not
     to any one case file either.

   S.activeTab holds the active page tab's key verbatim ('grid' while a
   table is showing), which is what lets syncTabSelection paint the strip
   from one comparison rather than a branch per tab. */

const PAGE_TABS_KEY = 'winnow.pagetabs';
const PAGE_TABS_MIN = 60;    // a strip narrower than one tab is still usable — it scrolls
const SOURCE_TABS_MIN = 140; // ...but not at the cost of an unreadable table strip

function loadPageTabPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PAGE_TABS_KEY) || '{}'); } catch { /* fall through to defaults */ }
  // Validated rather than spread-with-defaults (the winnow.detail idiom):
  // these two feed indexOf() and a CSS length, and a corrupt value from an
  // older/hand-edited profile should read as "no preference", not throw.
  return {
    order: Array.isArray(p.order) ? p.order.filter((k) => typeof k === 'string') : [],
    width: typeof p.width === 'number' && p.width > 0 ? p.width : null,
  };
}
S.pageTabPrefs = loadPageTabPrefs();
function savePageTabPrefs() { localStorage.setItem(PAGE_TABS_KEY, JSON.stringify(S.pageTabPrefs)); }

/* Every page tab that exists right now, in declaration order. Rebuilt per
   call because S.pluginTabs changes under it — toggling a plugin in
   Settings adds or removes one with no reload. */
function pageTabs() {
  return [
    { key: 'sql', label: 'SQL', title: 'Read-only SQL against the case file', node: () => $('tabSql'), show: showSqlTab },
    { key: 'timeline', label: 'Timeline', title: 'Unified timeline of every tagged row across the case', node: () => $('tabTimeline'), show: showTimelineTab },
    ...S.pluginTabs.map((t) => ({
      key: 'plugin:' + t.id,
      label: t.label,
      title: t.description || `${t.label} — from the ${t.plugin} plugin`,
      plugin: t,
      show: () => showPluginTab(t.id),
    })),
  ];
}

/* Same rule openTabsSorted() applies to tables: keys the user has ordered
   first, anything else after in declaration order — so a plugin installed
   after the order was saved lands at the end rather than nowhere. */
function pageTabsSorted() {
  const order = S.pageTabPrefs.order;
  return pageTabs().sort((a, b) => {
    const ia = order.indexOf(a.key), ib = order.indexOf(b.key);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

function setPageTabOrder(keys) {
  S.pageTabPrefs.order = keys;
  savePageTabPrefs();
  renderPageTabs(); // which re-renders the sidebar's Pages section in turn
}

/* Menu-driven equivalent of the drag, for the sidebar's ▲/▼ — no-ops at
   either end rather than wrapping, exactly like moveTab. */
function movePageTab(key, dir) {
  const keys = pageTabsSorted().map((t) => t.key);
  const idx = keys.indexOf(key);
  const swapIdx = idx + dir;
  if (idx === -1 || swapIdx < 0 || swapIdx >= keys.length) return;
  [keys[idx], keys[swapIdx]] = [keys[swapIdx], keys[idx]];
  setPageTabOrder(keys);
}

/* Alt + 1…0 addresses both strips as one row of keys: 1 is the table tab
   you were last in, and 2…0 are the page tabs in strip order — so the
   digits follow a drag-reorder instead of being nailed to SQL/Timeline.
   Slot 1 re-shows the grid rather than re-opening the source: clicking a
   table's own tab runs openSource(), which resets its filters/sort/search,
   and "take me back to the table I was in" means back to it as you left
   it. Slots past the last page tab are silently ignored — the row is worth
   having whether or not a plugin filled it out. */
function activateTabSlot(digit) {
  if (digit === '1') {
    const open = openTabsSorted().filter((s) => !s.error);
    // S.sourceId *is* the most recently selected table (openSource is the
    // only thing that sets it, and it survives a trip through the page
    // tabs) — unless it names one that has since been closed.
    const target = open.find((s) => s.id === S.sourceId) || open[0];
    if (!target) return;
    if (target.id === S.sourceId) { if (S.activeTab !== 'grid') showGridTab(); }
    else openSource(target.id);
    // The grid is the one tab that doesn't focus anything on arrival (a
    // click on its tab moves focus for free; a keystroke doesn't), so the
    // SQL editor would still be holding it and the next j/k would type
    // into a query.
    $('body').focus();
    return;
  }
  const t = pageTabsSorted()[(digit === '0' ? 10 : Number(digit)) - 2]; // 2→first page tab … 0→ninth
  if (t) t.show();
}

/* SQL and Timeline are markup in index.html — a dozen places reach them by
   id — so they're *moved* into position here rather than rebuilt; plugin
   tabs are built. Either way a node is wired for dragging exactly once:
   the two reused ones would otherwise collect another listener set on
   every render, and a drop would then apply the same reorder N times. */
function renderPageTabs() {
  const strip = $('pageTabs');
  const nodes = [];
  for (const t of pageTabsSorted()) {
    const btn = t.plugin ? el('button', 'tab tab-sql tab-plugin', t.label) : t.node();
    if (t.plugin) {
      btn.dataset.tabId = t.plugin.id;
      btn.title = t.title;
      btn.onclick = t.show;
    }
    btn.dataset.pageKey = t.key;
    if (!btn.dataset.dragWired) {
      wireDragReorder(btn, t.key, {
        containerSelector: '#pageTabs',
        rowSelector: '.tab',
        horizontal: true,
        currentIds: () => pageTabsSorted().map((x) => x.key),
        onReorder: setPageTabOrder,
      });
      btn.dataset.dragWired = '1';
    }
    nodes.push(btn);
  }
  strip.replaceChildren(...nodes);
  syncTabSelection();
}

/* One place paints "which tab is current". S.activeTab is either a page
   tab's key or 'grid', in which case S.sourceId names the table tab that
   owns the highlight — which is also why no source tab may look selected
   while a page tab is up: S.sourceId is never cleared on the way to
   SQL/Timeline (there's no single "a source is open" flag to unset), it
   just stops being what's on screen. The sidebar re-render lives here for
   the same reason renderTabs() ends with one — every caller has, by
   definition, just changed what's active. */
function syncTabSelection() {
  document.querySelectorAll('#pageTabs .tab').forEach((t) =>
    t.setAttribute('aria-selected', String(t.dataset.pageKey === S.activeTab)));
  document.querySelectorAll('#sourceTabs .tab').forEach((t) =>
    t.setAttribute('aria-selected', String(S.activeTab === 'grid' && Number(t.dataset.id) === S.sourceId)));
  renderSidebar();
}

/* ------------------------------------------------- page/table strip split */

/* What's stored is the width the analyst dragged to; what's applied is
   that width clamped to what the bar can currently give it. Nothing writes
   the clamped value back, so opening the same case in a narrower window
   (or with the sidebar out) squeezes the strip without amnesia — widen the
   window again and the chosen width comes back. */

/* The widest the page strip may be given `total` px to share with the
   table strip. Below the point where both minimums fit, neither gets its
   floor and the space is halved instead — starving the table strip to
   nothing to honour a 60px page strip is the worse of the two failures,
   and both strips scroll, so half of a cramped bar is still usable. */
function pageTabsMaxWidth(total) {
  return Math.max(0, total < PAGE_TABS_MIN + SOURCE_TABS_MIN ? total / 2 : total - SOURCE_TABS_MIN);
}

function clampPageTabsWidth(px) {
  // The two strips share one pool of space, so the pool is just their
  // current widths added together — true whichever of them is flexing.
  const total = $('sourceTabs').getBoundingClientRect().width + $('pageTabs').getBoundingClientRect().width;
  // ...except before a case is open: #app is [hidden], so every rect is 0
  // and there is nothing to clamp against. Take the stored width as-is and
  // let showApp() re-clamp it once the bar has a real width — clamping
  // against a zero-width bar would otherwise pin the strip at 0px for the
  // whole session, since nothing but a resize re-runs this.
  if (!total) return Math.round(px);
  const max = pageTabsMaxWidth(total);
  return Math.round(Math.min(Math.max(px, Math.min(PAGE_TABS_MIN, max)), max));
}

function applyPageTabsSize() {
  const strip = $('pageTabs');
  if (!S.pageTabPrefs.width) {
    // No preference: back to the stylesheet's content-width-with-a-60%-cap.
    strip.style.flexBasis = '';
    strip.style.maxWidth = '';
    return;
  }
  strip.style.maxWidth = 'none'; // an explicit width IS the cap now
  strip.style.flexBasis = clampPageTabsWidth(S.pageTabPrefs.width) + 'px';
}

$('tabSplit').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const strip = $('pageTabs'), handle = $('tabSplit');
  const startX = e.clientX;
  const startW = strip.getBoundingClientRect().width;
  const max = pageTabsMaxWidth(startW + $('sourceTabs').getBoundingClientRect().width);
  const min = Math.min(PAGE_TABS_MIN, max);
  handle.classList.add('dragging');
  strip.style.maxWidth = 'none';
  const move = (ev) => {
    // The handle sits on the page strip's left edge, so dragging left grows
    // it — same "toward the edge it's docked against" rule as detailResize.
    const w = Math.round(Math.min(Math.max(startW + (startX - ev.clientX), min), max));
    strip.style.flexBasis = w + 'px';
    S.pageTabPrefs.width = w;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    handle.classList.remove('dragging');
    savePageTabPrefs();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

/* Double-click clears the size rather than restoring some hardcoded px —
   "no preference" is a real state (content width), and it's the one a
   fresh profile starts in. */
$('tabSplit').ondblclick = () => {
  S.pageTabPrefs.width = null;
  applyPageTabsSize();
  savePageTabPrefs();
};

applyPageTabsSize();
renderPageTabs(); // paints the saved order onto SQL/Timeline before plugins load

async function loadSources(select) {
  const [sources, merges] = await Promise.all([api('/api/sources'), api('/api/merges')]);
  S.sources = [...sources, ...merges];
  const openTabs = renderTabs();
  // select/S.sourceId are only trustworthy if they actually name a tab
  // that's open right now — S.sourceId in particular is never reset on a
  // case switch (there's no single source-level event for "the whole case
  // changed"), so a stale id left over from the *previous* case can survive
  // here. openSource() no-ops on an id it can't find, which used to mean
  // "open a new, empty case while an old source was selected" silently
  // left the previous case's grid on screen instead of falling through to
  // the empty state below.
  const candidate = select ?? S.sourceId;
  const target = (candidate != null && openTabs.some((s) => s.id === candidate))
    ? candidate
    : (openTabs.find((s) => !s.error) || {}).id;
  if (target) await openSource(target);
  else {
    S.sourceId = null;
    $('empty').hidden = false;
    $('viewStats').textContent = '';
  }
}

async function openSource(id) {
  const src = S.sources.find((s) => s.id === id);
  if (!src) return;
  if (S.activeTab !== 'grid') showGridTab();
  S.sourceId = id;
  S.columns = src.columns;
  /* A cached row's `cells` is an array positional to S.columns, so changing
     the column set invalidates every cached page — and the two were never
     tied together. Adding or removing a derived column re-enters here with
     a different S.columns while the cache still holds rows laid out for the
     old one; rendering those would put values under the wrong headers,
     which for evidence is far worse than a blank cell. Cleared here, before
     anything can paint, rather than in each branch below (only one of which
     used to do it). */
  clearPageCache();
  S.filters = {};
  S.search = '';
  S.searchMode = 'contains';
  S.searchTerms = [];
  S.filterTree = { type: 'group', op: 'AND', children: [] };
  S.sort = [];
  S.tagFilter = [];
  S.cursor = -1;
  selClear();
  await closeAllGroupViews();
  S.groupByCols = [];
  S.preGroupOrder = null;
  S.groups = [];
  S.groupPages.clear();
  S.groupPending.clear();
  $('search').value = '';
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === 'contains')));
  syncSearchExpansion(false);
  updateSearchHint();
  updateFiltersButton();
  $('presetBanner').hidden = true;
  $('empty').hidden = true;

  const saved = await api(`/api/layout?source_id=${id}`).catch(() => ({}));
  // No per-source layout saved yet (a source opened for the first time) —
  // check for a cross-case default saved for this exact set of column
  // names (Settings > "save default layout" hotkey), same seed-once
  // pattern as the default tag template for a brand-new case.
  let defaultLayout = null;
  if (!saved.order) {
    const qp = new URLSearchParams();
    for (const c of baseColumns()) qp.append('col_names', c.name);
    const found = await api(`/api/column_layouts/find?${qp.toString()}`).catch(() => null);
    if (found && found.order) defaultLayout = found;
  }
  S.layout = saved.columns || (defaultLayout && defaultLayout.columns) || {};
  // Per-source, unlike the per-column overrides inside S.layout: this one is
  // a judgement about *this table's* size, and a cross-case default layout
  // (which is keyed by header set, not row count) has no business carrying it.
  S.valueFilterMode = saved.value_filters || 'auto';
  S.order = (saved.order && saved.order.filter((n) => S.columns.some((c) => c.name === n)))
    || (defaultLayout && defaultLayout.order.filter((n) => S.columns.some((c) => c.name === n)))
    || S.columns.map((c) => c.name);
  for (const c of S.columns) if (!S.order.includes(c.name)) S.order.push(c.name);

  // Default sort: first datetime column, ascending — a timeline wants time order.
  const dt = S.columns.find((c) => c.type === 'datetime');
  if (dt && !saved.sort) S.sort = [{ column: dt.name, dir: 'asc' }];
  if (saved.sort) S.sort = saved.sort;

  if (S.columns.some((c) => c.derived)) await derivedOps().catch(() => {});
  await loadTags();
  renderHead();

  const spec = currentSpec();
  const cached = S.viewCache.get(id);
  if (cached && cached.key === specKey(spec)) {
    // Same filter/sort/search as last time we had this source open — the
    // materialized v.view_N table is still alive server-side (Store only
    // evicts views for the SAME source on rebuild), so skip re-materializing.
    S.view = { view_id: cached.view_id, row_count: cached.row_count, elapsed_ms: cached.elapsed_ms };
    clearPageCache();
    selClear();
    S.anchor = -1;
    $('spacerY').style.height = spacerPx(cached.row_count) + 'px';
    $('viewStats').innerHTML =
      `<b>${cached.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · cached`;
    $('body').scrollTop = 0;
    render();
    drawRail();
    updateFiltersButton();
  } else {
    await rebuildView({ keepScroll: false });
  }
  syncTabSelection(); // moves the strip highlight and the sidebar's .active row onto this table
  checkPresets(id); // fire-and-forget — a suggestion banner, not core to opening the source
}

/* ----------------------------------------------------------------- view */

/* Monotonic token so an older rebuild that resolves after a newer one
   started can't swap its stale view/spec in over the newer one's — the
   race was always possible (two POSTs can complete out of order), and
   the pre-swap row prefetch below widens the in-flight window enough to
   care. */
let rebuildSeq = 0;

async function rebuildView({ keepScroll = true } = {}) {
  if (!S.sourceId) return;
  // Captured in virtual (row-space) pixels rather than as a raw scrollTop:
  // the outgoing and incoming views can have different row counts, and once
  // either is over MAX_SPACER_PX they have different spacer scales too — the
  // same scrollTop would then mean a different row on each side.
  const oldTotal = gridRowCount();
  const scroll = keepScroll ? vScroll($('body'), oldTotal, headH()) : 0;
  const spec = currentSpec();
  spec.op_token = opToken();
  const seq = ++rebuildSeq;
  let v;
  let seeded = [];
  setBusy(true);
  const disarmCancel = armOpCancel(spec.op_token);
  try {
    try {
      v = await post('/api/view', spec);
    } catch (e) {
      // 499 = the analyst cancelled this build. Server-side the transaction
      // rolled back with the previous view intact (see build_view), so the
      // rows on screen are still real — just keep them.
      if (e.status === 499) {
        toast('Cancelled — kept the previous view', 2500);
        // Repaint before leaving. The rows on screen are still real, but the
        // *column set* may have changed since they were painted (this is the
        // path a removed derived column takes when its rebuild is cancelled
        // or superseded), and skipping the paint is what left the grid
        // showing a column its own header had already dropped.
        render();
        return;
      }
      // 409 = the case/view this tab was talking to is gone (e.g. another
      // client switched cases) — show the server's message as-is rather
      // than mislabeling it a filter problem.
      toast(e.status >= 500
        ? `Couldn't build the view: ${e.message} — this is a bug, check the server console`
        : (e.status === 409 ? e.message : 'Filter error: ' + e.message), 5000);
      render(); // same reason as the 499 path above
      return;
    }
    // Fetch the page(s) covering where the grid will land BEFORE swapping
    // any state. Swapping first meant clearPageCache() + render() painted
    // every visible row as a '·' placeholder for the round-trip of the
    // first page fetch — the whole table visibly vanished on every filter
    // keystroke and sort click, which reads as sluggishness even when the
    // rebuild itself is fast. With the seed fetched up front, the old rows
    // stay on screen until the new view's rows replace them in one paint.
    // A seed failure is not an error: we fall back to exactly the old
    // pending-placeholder behaviour, and ensurePage recovers.
    if (!S.groupByCols.length && v.row_count) {
      const body = $('body');
      const target = Math.min(scroll, Math.max(0, headH() + v.row_count * ROW_H - body.clientHeight));
      const firstRow = Math.max(0, Math.floor(target / ROW_H) - OVERSCAN);
      const lastRow = Math.min(v.row_count - 1,
        firstRow + Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2);
      const pageIdxs = [...new Set([Math.floor(firstRow / PAGE), Math.floor(lastRow / PAGE)])];
      try {
        seeded = await Promise.all(pageIdxs.map(async (idx) => {
          const data = await api(`/api/rows?view_id=${v.view_id}&start=${idx * PAGE}&count=${PAGE}`);
          // An in-range page that came back empty is not a page — seeding it
          // would cache the empty array, and ensurePage short-circuits on
          // S.pages.has(), so nothing would ever refetch it. Same latch the
          // guard in ensurePage exists to prevent, one layer up: the seed
          // writes into the cache directly and so bypasses that guard.
          return (!data.rows.length && idx * PAGE < v.row_count) ? null : [idx, data.rows];
        }));
        seeded = seeded.filter(Boolean);
      } catch { seeded = []; }
    }
  } finally {
    setBusy(false);
    disarmCancel();
  }
  // A newer rebuild started while this one was in flight — its view has
  // already evicted ours server-side; let it win.
  if (seq !== rebuildSeq) return;
  S.view = v;
  S.viewCache.set(S.sourceId, { key: specKey(spec), view_id: v.view_id, row_count: v.row_count, elapsed_ms: v.elapsed_ms });
  clearPageCache();
  for (const [idx, rows] of seeded) {
    S.pages.set(idx, rows);
    for (const r of rows) S.rowsByPos.set(r.pos, r);
  }
  selClear();
  S.anchor = -1;
  S.cellRange = null;
  S.cellAnchor = null;
  const src = S.sources.find((s) => s.id === S.sourceId);
  $('spacerY').style.height = spacerPx(v.row_count) + 'px';
  $('viewStats').innerHTML =
    `<b>${v.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · ${v.elapsed_ms} ms`;
  $('body').scrollTop = rScroll($('body'), v.row_count, scroll, headH());
  if (S.groupByCols.length) {
    // The old view_id (and any expanded groups' sub-views) is gone now —
    // re-summarize against the new one, keeping the chosen grouping columns.
    await regroupAll();
  } else {
    render();
    drawRail();
  }
  refreshTagCounts(); // the scope changed, so every ribbon count did too
  updateFiltersButton();
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};
const rebuildSoon = debounce(() => rebuildView(), 220);

/* ---------------------------------------------------------------- header */

function colWidth(name) {
  const l = S.layout[name] || {};
  if (l.w) return l.w;
  const c = S.columns.find((x) => x.name === name);
  if (!c) return 140;
  if (c.type === 'datetime') return 190;
  if (c.type === 'number') return 100;
  return Math.min(360, Math.max(90, name.length * 9 + 30));
}
const visibleCols = () => S.order.filter((n) => !(S.layout[n] || {}).hidden);

/* --------------------------------------------------- timestamp display format */

/* Presentation only — the stored/exported value is always the raw text the
   CSV came with (invariant: source data is never mutated). Deliberately
   NOT `new Date(string)`: that applies the browser's local timezone to
   whatever gets parsed, which can silently shift an evidentiary timestamp.
   Instead this pulls numeric components straight out of the same two
   families store.py's DATE_RE already recognizes as "datetime" at ingest,
   then formatting is pure string padding — no Date object, no TZ math. A
   value that doesn't match either shape is left exactly as it was. */
const TS_ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?/;
const TS_US_RE = /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?)?/;

function parseTimestamp(raw) {
  const s = String(raw).trim();
  let m = TS_ISO_RE.exec(s);
  if (m) {
    return {
      y: +m[1], mo: +m[2], d: +m[3], h: +(m[4] || 0), mi: +(m[5] || 0), s: +(m[6] || 0),
      /* Sub-second digits kept as the raw string, padded/truncated only at
         format time — parsing them to a number would lose the distinction
         between .1 (100ms) and .000001, which matters for ordering
         same-second events. */
      frac: m[7] || '',
    };
  }
  m = TS_US_RE.exec(s);
  if (m) {
    let h = +(m[4] || 0);
    const ampm = (m[7] || '').toLowerCase();
    if (ampm === 'pm' && h < 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { y: +m[3], mo: +m[1], d: +m[2], h, mi: +(m[5] || 0), s: +(m[6] || 0), frac: '' };
  }
  return null;
}

const pad2 = (n) => String(n).padStart(2, '0');
const TS_FORMATS = {
  raw: 'As stored',
  iso: 'YYYY-MM-DD HH:MM:SS',
  iso_ms: 'YYYY-MM-DD HH:MM:SS.mmm',
  iso_us: 'YYYY-MM-DD HH:MM:SS.ffffff',
  date: 'YYYY-MM-DD',
  time: 'HH:MM:SS',
  us: 'MM/DD/YYYY HH:MM:SS',
  us_date: 'MM/DD/YYYY',
};
/* Sub-second digits to exactly `n` places. A value with no fraction shows
   zeros rather than being left short, so the column stays column-aligned
   and two timestamps remain visually comparable. */
function fracTo(frac, n) { return (frac || '').padEnd(n, '0').slice(0, n); }
function formatTimestamp(raw, fmt) {
  if (!fmt || fmt === 'raw' || raw == null || raw === '') return raw;
  const t = parseTimestamp(raw);
  if (!t) return raw; // doesn't match a recognized shape — show unchanged, never fabricate
  const ymd = `${t.y}-${pad2(t.mo)}-${pad2(t.d)}`;
  const hms = `${pad2(t.h)}:${pad2(t.mi)}:${pad2(t.s)}`;
  switch (fmt) {
    case 'iso': return `${ymd} ${hms}`;
    case 'iso_ms': return `${ymd} ${hms}.${fracTo(t.frac, 3)}`;
    case 'iso_us': return `${ymd} ${hms}.${fracTo(t.frac, 6)}`;
    case 'date': return ymd;
    case 'time': return hms;
    case 'us': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y} ${hms}`;
    case 'us_date': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y}`;
    default: return raw;
  }
}
/* Four layers, most specific first: this column in this table, then this
   case, then the system-wide default, then 'iso'. Before derived columns
   existed the fallback was 'raw' — analysts who prefer that set it as
   their system default (Settings > Timestamps); an explicitly-chosen
   per-column format still wins, so existing saved layouts are unaffected. */
function tsFormatFor(name) {
  return (S.layout[name] || {}).tsFormat
    || S.caseSettings.ts_format
    || S.appSettings.default_ts_format
    || 'iso';
}

/* A duration column holds seconds (see timeparse's duration_delta). Shown
   humanized by default because "1h 23m 45s" is the question the analyst
   asked; the raw seconds stay one click away in the header menu. */
function formatDuration(raw, mode) {
  if (raw == null || raw === '' || mode === 'raw') return raw;
  const total = Number(raw);
  if (!isFinite(total)) return raw;
  const sign = total < 0 ? '-' : '';
  let rest = Math.abs(total);
  const h = Math.floor(rest / 3600); rest -= h * 3600;
  const m = Math.floor(rest / 60); rest -= m * 60;
  const s = Math.round(rest * 1000) / 1000;
  const parts = [];
  if (h) parts.push(h + 'h');
  if (m || h) parts.push(m + 'm');
  parts.push(s + 's');
  return sign + parts.join(' ');
}

function columnMeta(name) { return S.columns.find((c) => c.name === name) || null; }
/* The imported file's own columns. Header-set identity (saved filters,
   cross-case layouts, nicknames, timeline templates) has to key off these
   alone — a derived column is one analyst's addition, and including it
   would stop the same file matching its own saved work elsewhere. */
function baseColumns() { return S.columns.filter((c) => !c.derived); }

/* Presentation for one cell, given its column's type. Kept in one place
   because four call sites (grid, grouped grid, detail pane, copy) have to
   agree on what the analyst is looking at. */
function displayCell(name, val) {
  const c = columnMeta(name);
  if (!c) return val;
  if (c.derived_kind === 'duration') return formatDuration(val, (S.layout[name] || {}).durFormat);
  if (c.type === 'datetime') return formatTimestamp(val, tsFormatFor(name));
  return val;
}

/* ------------------------------------------------------- derived columns */

/* Analyst-added columns computed from an existing one (see timeparse.py).
   The values are materialised server-side into a sidecar table, so a
   derived column sorts, filters, groups and exports like any other — the
   only frontend-visible difference is the marker, the management menu,
   and that its display format defaults like any datetime column. */

let DERIVED_OPS = null; // registry from /api/derived/ops, fetched once per load

async function derivedOps() {
  if (!DERIVED_OPS) DERIVED_OPS = await api('/api/derived/ops');
  return DERIVED_OPS;
}

function opLabel(opId) {
  const op = (DERIVED_OPS || []).find((o) => o.id === opId);
  return op ? op.label : opId;
}

function columnMenuItems(name) {
  const c = columnMeta(name) || {};
  const items = [];
  if (c.derived_kind === 'duration') {
    const cur = (S.layout[name] || {}).durFormat || 'human';
    for (const [key, label] of [['human', '1h 23m 45s'], ['raw', 'Seconds']]) {
      items.push({
        label,
        checked: key === cur, // the menu's own ✓ slot, rather than a padded label
        onclick: () => {
          S.layout[name] = Object.assign({}, S.layout[name] || {}, { durFormat: key });
          render();
          saveLayout();
        },
      });
    }
  } else if (c.type === 'datetime') {
    const current = tsFormatFor(name);
    for (const key of Object.keys(TS_FORMATS)) {
      items.push({
        label: TS_FORMATS[key],
        checked: key === current,
        onclick: () => {
          /* The chosen key is always stored, including 'raw'. It used to be
             stored as undefined, which was equivalent back when the
             fallback was 'raw' — with a configurable default underneath,
             that would silently mean "inherit" instead of "as stored". */
          S.layout[name] = Object.assign({}, S.layout[name] || {}, { tsFormat: key });
          render();
          saveLayout();
        },
      });
    }
  }
  if (items.length) items.push('-');
  items.push({ label: 'Add datetime column from this…', onclick: () => openDerivedColumnModal(name) });
  // Offered on any base column rather than only ones that sniff as
  // structured: the check costs a sample scan, the menu is built
  // synchronously, and a column of JSON that happens to start with a
  // non-document row would silently lose the entry. The picker itself says
  // so when there's nothing in there.
  if (!c.derived && S.sourceId >= 0) {
    items.push({ label: 'Flatten JSON/XML into columns…', onclick: () => openFlattenModal(name) });
  }
  if (c.derived) {
    if (c.parse_failures) {
      // "Unparsed" is the right word for a timestamp that didn't convert;
      // for an extracted field the same count means the document had no
      // such field, which is a different thing to go and look at.
      const extracted = c.derived_kind === 'text';
      items.push({
        label: extracted
          ? `Show ${c.parse_failures.toLocaleString()} row${c.parse_failures === 1 ? '' : 's'} without this field`
          : `Show ${c.parse_failures.toLocaleString()} unparsed row${c.parse_failures === 1 ? '' : 's'}`,
        onclick: () => showUnparsedRows(c),
      });
    }
    if (c.derived_kind === 'text') {
      items.push({ label: 'Change the field path…', onclick: () => editExtractedPath(c) });
    } else {
      items.push({ label: 'Re-derive…', onclick: () => openDerivedColumnModal(c.derived_from, c) });
    }
    items.push({ label: 'Remove derived column…', onclick: () => removeDerivedColumn(c) });
  }
  return items;
}

/* "12 failures" is only useful if you can see which 12. The fragment is
   built server-side (it has to quote two column names into SQL) and lands
   in the guided filter builder's raw slot, so it shows up as a normal
   filter the analyst can then edit or clear. */
async function showUnparsedRows(c) {
  try {
    const res = await api(`/api/derived/${c.derived_id}/unparsed_filter`);
    S.filterTree = { type: 'group', op: 'AND', children: [{ type: 'raw', sql: res.sql }] };
    updateFiltersButton();
    await rebuildView();
    toast(`Showing rows where "${c.name}" could not be parsed`);
  } catch (e) {
    toast('Could not filter: ' + e.message, 6000);
  }
}

/* An extracted column's whole definition is its path, so "re-derive" here
   is "edit the path" — the timestamp modal's format-and-parameters shape
   has nothing to offer it. Recomputes in place via the same rederive
   endpoint, so the column keeps its name, position and width. */
async function editExtractedPath(c) {
  const current = (c.derived_params || {}).path || '';
  const next = await promptDialog(
    `Field path for "${c.name}" (read from "${c.derived_from}"):`, current, { okLabel: 'Recompute' });
  if (next === null || next.trim() === current) return;
  try {
    await post(`/api/derived/${c.derived_id}/rederive`, { params: { path: next.trim() } });
    await showDerivedColumnsSoon();
    toast(`Recomputing "${c.name}"…`);
  } catch (e) {
    toast('Could not change the path: ' + e.message, 6000);
  }
}

async function removeDerivedColumn(c) {
  const ok = await confirmDialog(
    `Remove the derived column "${c.name}"? Its values are recomputed from "${c.derived_from}", so it can be added back at any time.`,
    { okLabel: 'Remove', danger: true });
  if (!ok) return;
  try {
    await api(`/api/derived/${c.derived_id}`, { method: 'DELETE' });
    delete S.layout[c.name];
    S.order = S.order.filter((n) => n !== c.name);
    await loadSources();
    await openSource(S.sourceId);
    toast(`Removed "${c.name}"`);
  } catch (e) {
    toast('Could not remove: ' + e.message, 6000);
  }
}

/* ------------------------------------------- extracted (JSON/XML) columns

   These share the derived-column machinery with the timestamp ops — same
   registry, same backfill, same sidecar, same session portability. The
   only thing that differs is the question being asked, which is why they
   are a separate `family` in the registry and a separate pair of entry
   points here rather than another row in the timestamp modal's dropdown. */

function extractOpFor(kind) { return kind === 'xml' ? 'xml_field' : 'json_field'; }

/* A name that doesn't collide with a column already on the table, since
   the obvious suggestion (a path's last component) collides constantly —
   `$.user.name` and `$.host.name` both want "name". */
function uniqueColumnName(base, taken) {
  let name = (base || 'field').trim() || 'field';
  if (!taken.has(name.toLowerCase())) return name;
  for (let i = 2; i < 500; i++) {
    const candidate = `${name} ${i}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
  return `${name} ${Date.now()}`;
}

function takenColumnNames() {
  return new Set(S.columns.map((c) => c.name.toLowerCase()));
}

/* Suggests the column name the way structparse.suggest_name does — the
   last meaningful component of the path, or an EVTX-style predicate's own
   value, which is nearly always what the analyst would have typed. */
function suggestColumnName(path, kind) {
  const s = String(path);
  const pred = s.match(/\[@[\w:.-]+='([^']*)'\]$/);
  if (pred && pred[1].trim()) return pred[1].trim();
  if (s.includes('@') && !s.trim().endsWith(']')) {
    const at = s.lastIndexOf('@');
    const attr = s.slice(at + 1);
    const tail = s.slice(0, at).replace(/\/$/, '').split('/').pop().replace(/\[[^\]]*\]$/, '');
    return (tail ? `${tail} ${attr}` : attr).trim();
  }
  if (kind === 'json') {
    const parts = s.replace(/^\$/, '').match(/\["'](?:[^"']*)["']|\[\d+\]|[^.[\]]+/g) || [];
    for (let i = parts.length - 1; i >= 0; i--) {
      const seg = parts[i];
      if (/^\[\d+\]$/.test(seg)) continue;
      return seg.replace(/^\["']|["']\]$/g, '').replace(/^\[|\]$/g, '');
    }
    return s;
  }
  return s.replace(/\/$/, '').split('/').pop().replace(/\[\d+\]$/, '') || s;
}

/* One field, one column — the "add as a column" item on a right-clicked
   node in the detail pane. No modal: the path came from a click, the name
   is derivable, and interrupting that with a dialog to confirm two things
   the analyst just expressed would be the wrong trade. The toast carries
   the undo-shaped escape hatch instead (remove it from the header menu). */
async function addExtractedColumn(column, path, kind) {
  if (S.sourceId < 0) { toast("Derived columns aren't available on merged tables", 5000); return; }
  const name = uniqueColumnName(suggestColumnName(path, kind), takenColumnNames());
  setBusy(true);
  try {
    await post('/api/derived', {
      source_id: S.sourceId, name, input_column: column,
      op_id: extractOpFor(kind), params: { path },
    });
    await showDerivedColumnsSoon();
    toast(`Adding "${name}" from ${ellipsize(path, 30)}…`);
  } catch (e) {
    toast('Could not add column: ' + e.message, 6000);
  } finally { setBusy(false); }
}

/* Brings the new columns into view without waiting for the backfill —
   same idiom the timestamp modal already uses. The columns appear
   immediately with status 'building' and fill in top-down; the jobs panel
   tracks progress and pollJobs reports the result. Blocking the UI on a
   pass over a million rows would be the wrong trade for a column the
   analyst can already see taking shape. */
async function showDerivedColumnsSoon() {
  await loadSources();
  await openSource(S.sourceId);
  startJobsPoll();
}

/* The flatten picker: every field found in a sample of the column, with
   how much of the sample carried it, ticked into columns in one pass.

   Coverage is shown and pre-selection is driven by it because that's the
   judgement the analyst is actually making — a field in 3 of 200 rows is
   usually noise from one outlier record, and a field in all 200 is a
   column. Everything at full coverage starts ticked; the rest start
   unticked and one click away. */
async function openFlattenModal(column) {
  if (S.sourceId < 0) { toast("Derived columns aren't available on merged tables", 5000); return; }
  let found;
  setBusy(true);
  try {
    found = await post('/api/derived/paths', { source_id: S.sourceId, column });
  } catch (e) {
    toast('Could not read that column: ' + e.message, 6000);
    return;
  } finally { setBusy(false); }

  if (!found.kind || !found.paths.length) {
    toast(`"${column}" doesn't look like JSON or XML`, 5000);
    return;
  }

  const taken = takenColumnNames();
  const rows = found.paths.map((p) => ({
    path: p.path,
    coverage: p.coverage,
    count: p.count,
    sample: p.sample,
    // Present in every sampled row *and* actually carrying a value. A
    // container element like <TimeCreated SystemTime="…"/> is present
    // everywhere and empty everywhere; pre-ticking it would build a column
    // of blanks (its value is on the attribute, one row down the list).
    checked: p.coverage >= 1 && p.nonempty > 0,
    name: uniqueColumnName(p.suggested_name || suggestColumnName(p.path, found.kind), taken),
  }));
  // Reserve every suggested name up front, so two paths that suggest the
  // same one get distinct defaults rather than colliding at submit time.
  rows.forEach((r) => taken.add(r.name.toLowerCase()));

  modal(`Flatten "${column}" into columns`, (body) => {
    const head = el('div', 'flatten-head');
    head.append(el('div', 'fb-help',
      `${found.kind.toUpperCase()} · ${found.paths.length} field${found.paths.length === 1 ? '' : 's'} found in ${found.sampled.toLocaleString()} sampled row${found.sampled === 1 ? '' : 's'}`));
    const bulk = el('div');
    const all = el('button', 'btn ghost', 'Select all');
    const none = el('button', 'btn ghost', 'Select none');
    bulk.append(all, none);
    head.append(bulk);
    body.append(head);

    const list = el('div', 'flatten-list');
    const count = el('div', 'fb-help');

    function updateCount() {
      const n = rows.filter((r) => r.checked).length;
      count.textContent = n ? `${n} column${n === 1 ? '' : 's'} will be added, in a single pass over the table.`
                            : 'Nothing selected.';
      addBtn.disabled = !n;
    }

    function renderRows() {
      list.replaceChildren();
      for (const r of rows) {
        const row = el('div', 'flatten-row');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = r.checked;
        box.onchange = () => { r.checked = box.checked; updateCount(); };

        // Name over path: the name is what the analyst edits, the path is
        // what the column actually means, and burying the latter in a
        // tooltip makes two similarly-named fields impossible to tell
        // apart at a glance.
        const namePart = el('div', 'flatten-name');
        const name = el('input');
        name.type = 'text';
        name.value = r.name;
        name.oninput = () => { r.name = name.value; };
        const path = el('div', 'flatten-path', r.path);
        path.title = r.path;
        namePart.append(name, path);

        const cov = el('div', 'flatten-cov', `${Math.round(r.coverage * 100)}%`);
        cov.title = `${r.count.toLocaleString()} of ${found.sampled.toLocaleString()} sampled rows have this field`;
        const sample = el('div', 'flatten-sample', r.sample || '—');
        sample.title = r.sample || '';
        row.append(box, namePart, cov, sample);
        list.append(row);
      }
      updateCount();
    }

    all.onclick = () => { rows.forEach((r) => { r.checked = true; }); renderRows(); };
    none.onclick = () => { rows.forEach((r) => { r.checked = false; }); renderRows(); };

    const addBtn = el('button', 'btn primary', 'Add columns');
    addBtn.onclick = async () => {
      const picked = rows.filter((r) => r.checked);
      const names = picked.map((r) => r.name.trim());
      if (names.some((n) => !n)) { toast('Every selected column needs a name', 4000); return; }
      const lower = names.map((n) => n.toLowerCase());
      const dupe = lower.find((n, i) => lower.indexOf(n) !== i);
      if (dupe) { toast(`Two columns are both called "${dupe}" — rename one`, 5000); return; }
      $('modal').hidden = true;
      setBusy(true);
      try {
        await post('/api/derived/batch', {
          source_id: S.sourceId,
          columns: picked.map((r, i) => ({
            name: names[i], input_column: column,
            op_id: extractOpFor(found.kind), params: { path: r.path },
          })),
        });
        await showDerivedColumnsSoon();
        toast(`Adding ${names.length} column${names.length === 1 ? '' : 's'}…`);
      } catch (e) {
        toast('Could not add columns: ' + e.message, 6000);
      } finally { setBusy(false); }
    };

    renderRows();
    body.append(list, count);
    const foot = el('div', 'row-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    foot.append(cancel, addBtn);
    body.append(foot);
  }, { wide: true });
}

/* The add/re-derive modal. `prefill` is the column to parse; `editing` is
   the existing definition when re-deriving (the column and operation are
   then fixed — only the parameters are in play, which is the actual use
   case: "I set the wrong syslog year"). */
async function openDerivedColumnModal(prefill, editing) {
  let ops;
  try {
    ops = await derivedOps();
  } catch (e) {
    toast('Could not load timestamp formats: ' + e.message, 6000);
    return;
  }
  const textCols = baseColumns();
  if (!textCols.length) return;
  const state = {
    column: editing ? editing.derived_from : (prefill || textCols[0].name),
    opId: editing ? editing.derived_op : null,
    params: {},
    name: editing ? editing.name : '',
  };

  modal(editing ? `Re-derive "${editing.name}"` : 'Add datetime column', (body) => {
    const previewBox = el('div', 'derived-preview');
    const paramBox = el('div', 'derived-params');
    const nameInput = el('input');
    nameInput.className = 'derived-name';
    const opSelect = el('select');
    const colSelect = el('select');
    const suggestNote = el('div', 'fb-help');

    for (const c of textCols) {
      const o = el('option', null, c.name);
      o.value = c.name;
      colSelect.append(o);
    }
    colSelect.value = state.column;
    colSelect.disabled = !!editing;

    for (const op of ops) {
      // A two-input operation (duration) needs a second column, not a
      // format guess, so it's offered here too — but never auto-suggested.
      const o = el('option', null, op.label);
      o.value = op.id;
      opSelect.append(o);
    }

    function currentOp() { return ops.find((o) => o.id === opSelect.value); }

    function defaultName() {
      const op = currentOp();
      if (op && op.derived_kind === 'duration') return `${state.column} elapsed`;
      return `${state.column} (parsed)`;
    }

    function buildParams() {
      paramBox.replaceChildren();
      const op = currentOp();
      if (!op) return;
      for (const spec of op.params) {
        const row = el('label', 'derived-param');
        row.append(el('span', 'derived-param-label', spec.label + (spec.required ? ' *' : '')));
        let input;
        if (spec.type === 'select') {
          input = el('select');
          for (const opt of spec.options) {
            const o = el('option', null, opt);
            o.value = opt;
            input.append(o);
          }
        } else if (spec.type === 'column') {
          input = el('select');
          for (const c of S.columns) {
            if (c.name === state.name) continue;
            const o = el('option', null, c.name);
            o.value = c.name;
            input.append(o);
          }
        } else {
          input = el('input');
          if (spec.type === 'int') input.type = 'number';
          input.placeholder = spec.type === 'offset' ? '+00:00' : '';
        }
        const existing = state.params[spec.name];
        if (existing != null && existing !== '') input.value = existing;
        else if (spec.default != null) input.value = spec.default;
        else if (spec.type === 'int' && spec.name === 'base_year') input.value = new Date().getFullYear();
        state.params[spec.name] = input.value;
        input.oninput = () => { state.params[spec.name] = input.value; refreshPreview(); };
        input.onchange = () => { state.params[spec.name] = input.value; refreshPreview(); };
        row.append(input);
        if (spec.help) row.append(el('span', 'fb-help derived-param-help', spec.help));
        paramBox.append(row);
      }
    }

    let previewSeq = 0;
    async function refreshPreview() {
      const seq = ++previewSeq;
      previewBox.replaceChildren(el('div', 'fb-help', 'Checking…'));
      let res;
      try {
        res = await post('/api/derived/preview', {
          source_id: S.sourceId, column: state.column, op_id: opSelect.value, params: state.params,
        });
      } catch (e) {
        if (seq !== previewSeq) return;
        previewBox.replaceChildren(el('div', 'fb-help bad', e.message));
        return;
      }
      if (seq !== previewSeq) return; // a later keystroke already superseded this
      previewBox.replaceChildren();
      const table = el('div', 'derived-preview-rows');
      for (const row of res.preview) {
        const r = el('div', 'derived-preview-row');
        r.append(el('span', 'derived-in', String(row.input == null ? '' : row.input)));
        r.append(el('span', 'derived-arrow', '→'));
        r.append(el('span', 'derived-out' + (row.output == null ? ' bad' : ''),
                    row.output == null ? "can't parse" : row.output));
        table.append(r);
      }
      previewBox.append(table);
      previewBox.append(el('div', 'fb-help derived-verdict' + (res.failures ? ' bad' : ''),
        res.failures
          ? `${res.failures.toLocaleString()} of ${res.sampled.toLocaleString()} sampled values can't be parsed this way.`
          : `All ${res.sampled.toLocaleString()} sampled values parse.`));
    }

    async function pickColumn(name) {
      state.column = name;
      if (!editing) {
        nameInput.value = defaultName();
        state.name = nameInput.value;
      }
      suggestNote.textContent = 'Detecting format…';
      let ranked = [];
      try {
        ranked = await post('/api/derived/detect', { source_id: S.sourceId, column: name });
      } catch { /* detection is a convenience — the picker still works */ }
      if (ranked.length) {
        const best = ranked[0];
        suggestNote.textContent =
          `Suggested: ${best.label} — ${Math.round(best.confidence * 100)}% of sampled values parse.`;
        if (!editing) {
          opSelect.value = best.op_id;
          state.params = Object.assign({}, best.params);
        }
      } else {
        suggestNote.textContent = editing ? '' : "No format detected — pick one below to see what it produces.";
      }
      buildParams();
      refreshPreview();
    }

    colSelect.onchange = () => pickColumn(colSelect.value);
    opSelect.onchange = () => { state.params = {}; buildParams(); refreshPreview(); };
    nameInput.oninput = () => { state.name = nameInput.value; };

    body.append(labeledRow('Parse column', colSelect));
    body.append(suggestNote);
    body.append(labeledRow('Format', opSelect));
    body.append(paramBox);
    if (!editing) {
      nameInput.value = defaultName();
      state.name = nameInput.value;
      body.append(labeledRow('New column name', nameInput));
    }
    body.append(el('div', 'derived-preview-title', 'Preview'));
    body.append(previewBox);

    if (editing) {
      state.opId = editing.derived_op;
      opSelect.value = editing.derived_op;
      opSelect.disabled = true;
      api(`/api/derived?source_id=${S.sourceId}`).then((defs) => {
        const d = defs.find((x) => x.id === editing.derived_id);
        if (d) { state.params = Object.assign({}, d.params); buildParams(); refreshPreview(); }
      }).catch(() => {});
      buildParams();
      refreshPreview();
    } else {
      pickColumn(state.column);
    }

    const actions = el('div', 'row-actions');
    const go = el('button', 'btn', editing ? 'Re-derive' : 'Add column');
    go.onclick = async () => {
      go.disabled = true;
      try {
        let res;
        if (editing) {
          res = await post(`/api/derived/${editing.derived_id}/rederive`, { params: state.params });
        } else {
          res = await post('/api/derived', {
            source_id: S.sourceId, name: state.name, input_column: state.column,
            op_id: opSelect.value, params: state.params,
          });
        }
        $('modal').hidden = true;
        // The column shows up immediately (status 'building') and fills in
        // top-down as the backfill runs; the jobs panel tracks it.
        await loadSources();
        await openSource(S.sourceId);
        startJobsPoll();
        toast(editing ? `Re-deriving "${editing.name}"…` : `Adding "${state.name}"…`);
      } catch (e) {
        go.disabled = false;
        toast('Could not add column: ' + e.message, 8000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(go, cancel);
    body.append(actions);
  }, { wide: true });
}

/* After an import, if a column looks strongly like a timestamp the app
   can't already read (an epoch, a syslog line), say so once. A toast with
   an action rather than a modal: the analyst asked to import a file, not
   to be interrupted — and a column that isn't converted still shows and
   searches exactly as before. */
const suggestedSources = new Set();

async function offerTimestampColumns(sourceId) {
  if (suggestedSources.has(sourceId)) return;
  suggestedSources.add(sourceId);
  let suggestions = [];
  try {
    suggestions = await api(`/api/derived/suggestions?source_id=${sourceId}`);
  } catch { return; }
  if (!suggestions.length) return;
  const s = suggestions[0];
  const src = (S.sources || []).find((x) => x.id === sourceId);
  const more = suggestions.length > 1 ? ` (and ${suggestions.length - 1} more)` : '';
  toastAction(
    `"${s.column}" in ${src ? src.name : 'the new table'} looks like ${s.label}${more}`,
    'Add datetime column',
    async () => {
      if (S.sourceId !== sourceId) await openSource(sourceId);
      openDerivedColumnModal(s.column);
    });
}

function labeledRow(label, control) {
  const row = el('label', 'derived-row');
  row.append(el('span', 'derived-row-label', label));
  row.append(control);
  return row;
}

/* ----------------------------------------------------------- column drag */

/* Native HTML5 drag-and-drop, reordering S.order directly. draggedCol is
   tracked in a closure var rather than trusted from dataTransfer alone —
   dataTransfer.getData isn't readable during dragover in most browsers
   (only on drop), but we need to know the source column during dragover
   to decide which side of the target to show the insertion indicator on. */
let draggedCol = null;

function wireColumnDrag(h, name) {
  h.addEventListener('dragstart', (e) => {
    draggedCol = name;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', name);
    h.classList.add('dragging');
  });
  h.addEventListener('dragend', () => {
    draggedCol = null;
    document.querySelectorAll('.hcell.dragging, .hcell.drop-before, .hcell.drop-after')
      .forEach((el2) => el2.classList.remove('dragging', 'drop-before', 'drop-after'));
  });
  h.addEventListener('dragover', (e) => {
    if (!draggedCol || draggedCol === name) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const before = e.clientX < h.getBoundingClientRect().left + h.offsetWidth / 2;
    h.classList.toggle('drop-before', before);
    h.classList.toggle('drop-after', !before);
  });
  h.addEventListener('dragleave', () => h.classList.remove('drop-before', 'drop-after'));
  h.addEventListener('drop', (e) => {
    e.preventDefault();
    const dragged = draggedCol;
    const before = h.classList.contains('drop-before');
    h.classList.remove('drop-before', 'drop-after');
    if (!dragged || dragged === name) return;
    S.order = S.order.filter((n) => n !== dragged);
    let idx = S.order.indexOf(name);
    if (!before) idx += 1;
    S.order.splice(idx, 0, dragged);
    renderHead();
    render();
    saveLayout();
  });
}

function renderHead() {
  S.cellRange = null; // column order/visibility/width changes invalidate cell-range column indices
  S.cellAnchor = null;
  renderGroupStrip();
  const head = $('headRow');
  const filt = $('filterRow');
  head.replaceChildren();
  filt.replaceChildren();

  // .gutter-head mirrors .gutter's three-slot grid exactly (checkbox |
  // stripes | right-aligned row number) so the select-all box sits directly
  // above the row checkboxes and "Line" sits directly above the rid digits.
  // Not sortable, unlike every other hcell — hence gutter-head's own
  // cursor/hover treatment rather than .hcell's.
  const gh = el('div', 'hcell gutter-head');
  gh.style.flexBasis = GUTTER_W + 'px';
  const selectAllCb = el('input');
  selectAllCb.type = 'checkbox';
  selectAllCb.id = 'selectAllRows';
  selectAllCb.className = 'select-all-rows';
  selectAllCb.title = 'Select every row in the current view';
  selectAllCb.onchange = () => {
    if (S.groupByCols.length || !S.view) { selectAllCb.checked = false; return; }
    selectAllCb.checked ? selSetAll() : selClear();
    S.cellRange = null;
    S.cellAnchor = null;
    render();
  };
  gh.append(selectAllCb, el('span', 'gutter-mid'), el('span', 'label', 'Line'));
  head.append(gh);

  const gf = el('div', 'fcell gutter-filter');
  gf.style.flexBasis = GUTTER_W + 'px';
  filt.append(gf);

  for (const name of visibleCols()) {
    const w = colWidth(name);
    const h = el('div', 'hcell' + ((S.layout[name] || {}).pinned ? ' pinned' : ''));
    h.style.flexBasis = w + 'px';
    h.draggable = true;
    h.dataset.col = name;
    wireColumnDrag(h, name);
    h.append(el('span', 'label', name));
    const si = S.sort.findIndex((s) => s.column === name);
    if (si >= 0) {
      h.append(el('span', 'sort', (S.sort[si].dir === 'asc' ? '▲' : '▼') + (S.sort.length > 1 ? si + 1 : '')));
    }
    const colMetaEntry = columnMeta(name);
    if (colMetaEntry && colMetaEntry.derived) {
      // Marks the column as the analyst's own addition rather than
      // something that came out of the evidence file.
      const mark = el('span', 'hcell-derived', 'ƒ');
      const dstatus = colMetaEntry.derived_status;
      mark.title = `Derived from "${colMetaEntry.derived_from}" — ${opLabel(colMetaEntry.derived_op)}`
        + (dstatus === 'building' ? ' (building…)' : '')
        + (dstatus === 'partial' ? ' (incomplete — re-derive to finish)' : '');
      if (dstatus !== 'ready') mark.classList.add('pending');
      h.append(mark);
    }
    if (colMetaEntry) {
      // Column options (display format, "Add datetime column from this…",
      // the derived-column actions) are a right-click, not a ▾ button that
      // spent a slot of every header's width forever to be used rarely —
      // same move the tab strip's ▦ made. The header's title carries the
      // discovery burden the glyph used to.
      h.oncontextmenu = (e) => {
        e.preventDefault();
        e.stopPropagation();
        contextMenu(e, columnMenuItems(name));
      };
      h.title = 'Click to sort · Shift-click to add a sort · Right-click for column options';
    }
    h.onclick = (e) => {
      const cur = S.sort.find((s) => s.column === name);
      const dir = cur && cur.dir === 'asc' ? 'desc' : 'asc';
      if (e.shiftKey) {
        if (cur) cur.dir = dir; else S.sort.push({ column: name, dir });
      } else {
        S.sort = [{ column: name, dir }];
      }
      renderHead();
      rebuildView();
    };
    const grip = el('div', 'grip');
    grip.draggable = false;
    grip.onmousedown = (e) => startResize(e, name);
    grip.onclick = (e) => e.stopPropagation();
    grip.ondblclick = (e) => { e.stopPropagation(); autofitOneColumn(name); };
    grip.title = 'Drag to resize, double-click to autofit this column';
    h.append(grip);
    head.append(h);

    const f = el('div', 'fcell');
    f.style.flexBasis = w + 'px';
    const inp = el('input');
    inp.value = S.filters[name] || '';
    inp.placeholder = 'filter';
    inp.dataset.col = name;
    if (inp.value) inp.classList.add('active');
    inp.oninput = () => {
      S.filters[name] = inp.value;
      inp.classList.toggle('active', !!inp.value);
      rebuildSoon();
    };
    inp.onkeydown = (e) => {
      if (e.key === 'Escape') { inp.value = ''; S.filters[name] = ''; inp.classList.remove('active'); rebuildView(); }
      if (e.key === 'Enter') { e.preventDefault(); rebuildView(); $('body').focus(); }
    };
    f.append(inp);
    if (valueFilterEnabled(name)) {
      // Excel's funnel, in the place the filter it writes will appear.
      // Whether it's here at all is the size rule + the table menu's
      // overrides — see valueFilterEnabled.
      const pick = el('button', 'fcell-pick', '▾');
      pick.dataset.col = name;
      pick.tabIndex = -1;
      // A selection the box couldn't spell lives in the filter tree instead
      // (see setPickerTreeNode), which would otherwise leave this column
      // looking unfiltered — the box next to it is empty.
      const inTree = !!pickerTreeNode(name);
      if (inTree) pick.classList.add('active');
      pick.title = inTree
        ? `${name} is filtered to picked values — shown under Filters ▾ because the filter box can't spell them`
        : `Pick values to filter ${name} by`;
      pick.onclick = (ev) => { ev.stopPropagation(); openValuePicker(name, pick); };
      f.append(pick);
    }
    filt.append(f);
  }
}

function startResize(e, name) {
  e.preventDefault();
  e.stopPropagation();
  const x0 = e.clientX;
  const w0 = colWidth(name);
  const move = (ev) => {
    const w = Math.max(48, w0 + ev.clientX - x0);
    S.layout[name] = { ...(S.layout[name] || {}), w };
    renderHead();
    render();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    saveLayout();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

const saveLayout = debounce(() => {
  if (!S.sourceId) return;
  post('/api/layout', {
    source_id: S.sourceId,
    payload: { columns: S.layout, order: S.order, sort: S.sort, value_filters: S.valueFilterMode },
  }).catch(() => {});
}, 400);

/* Saves the current column order/visibility/timestamp-format as the
   cross-case default for this exact header set (workspace/column_layouts.json,
   outside any single case — same home as saved filters and the default tag
   template) — so importing another file with the same headers later opens
   to it. Independent of saveLayout() above, which persists per-source
   inside this one case. */
async function saveDefaultLayout() {
  if (!S.sourceId || !S.columns.length) return;
  try {
    await post('/api/column_layouts', {
      // Keyed by the imported file's own columns: a derived column is this
      // analyst's addition, and including it would stop the same file
      // matching this layout when it's opened somewhere else.
      col_names: baseColumns().map((c) => c.name), order: S.order, columns: S.layout,
    });
    toast('Saved as the default layout for this set of columns');
  } catch (e) {
    toast('Could not save default layout: ' + e.message, 4000);
  }
}

/* ------------------------------------------------------- column autosize */

async function fetchColumnMaxLens() {
  if (!S.sourceId) return null;
  try { return await api(`/api/column_maxlen?source_id=${S.sourceId}`); }
  catch (e) { toast('Could not measure column widths: ' + e.message); return null; }
}

/* 0/null means "no cap". Stored with the other per-browser look-and-feel
   preferences rather than in the layout: it's a statement about this
   screen, not about this table's columns. */
function autofitMaxWidth() {
  const v = S.appearance.autofitMax;
  if (v === 0 || v === null) return 0;
  return Number(v) > 0 ? Number(v) : AUTOFIT_MAX_W_DEFAULT;
}

/* What the header cell actually needs, measured off the live DOM rather
   than estimated from the column name's length. The estimate ignored
   everything the header carries besides its text — the sort arrow, the ▾
   options button, the derived ƒ mark, 8px of padding either side — and the
   header font is uppercase and letter-spaced, so it isn't 7px/char either.
   That's how a fit-to-content pass could leave "EVEN…▾" sitting over a
   column of 1s. `scrollWidth` gives the label's full text width even while
   it's clipped; the difference between the cell's own clientWidth (which
   includes its padding) and the label's is everything else in the row. The
   grip is absolutely positioned, so it isn't in that difference.

   Returns 0 for a column with no header on screen (hidden, or a caller
   running before the first renderHead) — callers fall back to the estimate. */
function headerWidthFor(name) {
  const h = document.querySelector(`.hcell[data-col="${CSS.escape(name)}"]`);
  const label = h && h.querySelector('.label');
  if (!label) return 0;
  // Everything in the cell that isn't the label, measured from the siblings
  // themselves — NOT from `h.clientWidth - label.clientWidth`, which is only
  // the chrome while the cell is exactly as wide as its contents. On a column
  // that's wider than it needs to be, that difference is mostly slack, so the
  // header reported needing roughly the current width and autofit could never
  // shrink a column back to its content. (.grip is absolutely positioned and
  // occupies no track, so it isn't counted; .label can shrink but not grow,
  // so its scrollWidth is the text width whether it's clipped or not.)
  const cs = getComputedStyle(h);
  const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
  const gap = parseFloat(cs.columnGap === 'normal' ? cs.gap : cs.columnGap) || 0;
  let extras = 0;
  let siblings = 0;
  for (const child of h.children) {
    if (child === label || child.classList.contains('grip')) continue;
    extras += child.getBoundingClientRect().width;
    siblings += 1;
  }
  return Math.ceil(label.scrollWidth + extras + gap * siblings + pad) + 1;
}

function widthForLen(name, len) {
  const dataPx = Math.max(60, (len || 0) * 7 + 24);
  const headPx = headerWidthFor(name) || (name.length * 7 + 24);
  const px = Math.max(dataPx, headPx);
  const cap = autofitMaxWidth();
  if (!cap) return px;
  // The header is allowed past the cap: a column whose *name* is cut off is
  // unreadable in a way a truncated value isn't — you can widen a column you
  // can still identify. Only to 2x, so one absurd header can't defeat the
  // cap's whole purpose either.
  return Math.min(px, Math.max(cap, Math.min(headPx, cap * 2)));
}

function resetAllColumnWidths() {
  if (!S.sourceId) return;
  for (const name of visibleCols()) {
    if (S.layout[name]) delete S.layout[name].w;
  }
  renderHead(); render(); saveLayout();
  toast('Column widths reset to default');
}

async function autofitAllColumnWidths() {
  if (!S.sourceId) return;
  toast('Measuring columns…', 8000);
  const maxlens = await fetchColumnMaxLens();
  if (!maxlens) return;
  for (const name of visibleCols()) {
    S.layout[name] = { ...(S.layout[name] || {}), w: widthForLen(name, maxlens[name]) };
  }
  renderHead(); render(); saveLayout();
  toast('Columns autofit to content');
}

async function autofitOneColumn(name) {
  if (!S.sourceId) return;
  const maxlens = await fetchColumnMaxLens();
  if (!maxlens) return;
  S.layout[name] = { ...(S.layout[name] || {}), w: widthForLen(name, maxlens[name]) };
  renderHead(); render(); saveLayout();
}

/* ------------------------------------------------------------ row paging */

/* Page cache ceiling. A 500-row page of a 27-column source is on the order
   of a megabyte of JS objects, and nothing used to evict them within a
   view's lifetime — so deep-scrolling a 1.2M-row view quietly accumulated
   the entire table in the JS heap. The DOM has only ever held the visible
   window (invariant #6); this makes memory follow the same rule.
   Holds the same ~50k-row idle-scrollback budget PAGE=500/100 pages did —
   10 * PAGE(5000) = 50k — comfortably more than any viewport plus overscan,
   and enough that ordinary back-and-forth scrolling still hits the cache. */
const MAX_CACHED_PAGES = 10;

/* The page indices the grid is currently painting from. Never evicted:
   render() re-requests any page it needs, so dropping one would just be
   refetched on the very next frame — and, worse, ensurePage calls render()
   on arrival, so an eviction/refetch pair here would loop forever. */
function visiblePageRange() {
  const body = $('body');
  const total = gridRowCount();
  const first = Math.max(0, Math.floor(vScroll(body, total, headH()) / ROW_H) - OVERSCAN);
  const last = first + Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  return [Math.floor(first / PAGE), Math.floor(last / PAGE)];
}

/* Evicts the pages furthest from the viewport until the cache is back under
   the ceiling. `keep` protects pages an in-flight bulk operation still
   needs — copy and tag both walk a range of pages they've already fetched,
   and evicting one out from under them would produce exactly the silent
   blank rows waitForPages exists to prevent. Both protected sets can be
   larger than the ceiling (a 400-page bulk tag), in which case nothing is
   evicted; the cap is a cap on *idle* scrollback, not a hard limit that
   could break an operation in progress. */
function trimPageCache(keep) {
  if (S.pages.size <= MAX_CACHED_PAGES) return;
  const [visFirst, visLast] = visiblePageRange();
  const center = Math.floor((visFirst + visLast) / 2);
  const held = keep instanceof Set ? keep : new Set(keep || []);
  const evictable = [...S.pages.keys()].filter((p) => !held.has(p) && (p < visFirst || p > visLast));
  evictable.sort((a, b) => Math.abs(b - center) - Math.abs(a - center));
  for (const idx of evictable) {
    if (S.pages.size <= MAX_CACHED_PAGES) break;
    for (const r of S.pages.get(idx)) S.rowsByPos.delete(r.pos);
    S.pages.delete(idx);
  }
}

/* Drops every cached row for the *current* view — used after a bulk tag,
   where the server changed rows this client never fetched and there's
   nothing to patch up in place.

   Bumping the generation is the part that's easy to miss: a page fetch
   already in flight was issued against the pre-tag state, and without this
   it would land afterwards and repopulate the cache with stale `tags`
   arrays. ensurePage checks the generation before storing, and clearing
   S.pending lets render() start fresh fetches for whatever's on screen
   instead of waiting on the now-discarded ones. */
function clearPageCache() {
  S.pages.clear();
  S.rowsByPos.clear();
  S.pending.clear();
  S.pageGen++;
}

/* Returns a promise that resolves once the page is in S.pages, or once the
   attempt to load it has finished failing — callers that care (waitForPages)
   check S.pages afterwards. Concurrent callers for the same page share one
   request rather than the second one returning immediately as if it were
   already loaded. */
function ensurePage(idx, { keep, prefetch } = {}) {
  if (S.pages.has(idx)) return Promise.resolve();
  const inFlight = S.pending.get(idx);
  if (inFlight) return inFlight;
  const vid = S.view.view_id;
  const gen = S.pageGen;
  const p = (async () => {
    try {
      const data = await api(`/api/rows?view_id=${vid}&start=${idx * PAGE}&count=${PAGE}`);
      if (!S.view || S.view.view_id !== vid || S.pageGen !== gen) return;
      /* An in-range page that comes back empty means this client's view
         handle and the server's view disagree about how many rows there
         are. Caching the empty array would be the worst possible response:
         ensurePage short-circuits on S.pages.has(idx), so nothing would
         ever refetch it, no error would surface, and the grid would sit
         there showing '·' for every row until the analyst reloaded the
         page — which is exactly the "my data stopped loading" failure this
         guard exists to prevent.

         Rebuilding is the same recovery the expired-view path below takes,
         and it can't loop: a rebuild replaces both the view id and the row
         count, so if the view really is empty, row_count becomes 0 and
         this branch stops being reachable. */
      if (!data.rows.length && idx * PAGE < S.view.row_count) {
        rebuildView();
        return;
      }
      S.pages.set(idx, data.rows);
      for (const r of data.rows) S.rowsByPos.set(r.pos, r);
      trimPageCache(keep);
      // A prefetched page landing outside the viewport changes nothing on
      // screen, and repainting for it is pure work — but the analyst may
      // have scrolled onto it while it was in flight, in which case it's
      // exactly the page render() is waiting on. Check, don't assume.
      const [visFirst, visLast] = visiblePageRange();
      if (!prefetch || (idx >= visFirst && idx <= visLast)) {
        render();
        if (!$('detail').hidden && S.cursor >= 0 && rowAt(S.cursor)) showDetail(S.cursor);
      }
    } catch (e) {
      if (String(e.message).includes('expired')) rebuildView();
    } finally {
      S.pending.delete(idx);
    }
  })();
  S.pending.set(idx, p);
  return p;
}

/* ------------------------------------------------------------- prefetch */

/* Rows the analyst hasn't reached yet, fetched before they ask for them.

   A page is PAGE (5,000) rows, so crossing a page boundary is rare — but
   when it happens the grid paints `pending` placeholder rows until a
   5,000-row round trip completes, and that stall is the whole of what
   "scrolling feels sluggish" is. Warming the neighbouring pages turns the
   boundary into a cache hit for at most two extra requests, well inside
   MAX_CACHED_PAGES (a viewport spans one or two pages; this makes it three
   or four).

   Both directions, not just the direction of travel: scrolling back up
   through a boundary stalls exactly as badly as scrolling down through it,
   and guessing the direction wrong costs a wasted fetch while covering
   both costs one.

   Deferred to idle rather than fired inline from render(): a prefetch
   competing with the page the viewport is actually waiting on would make
   the visible case slower in order to fix the invisible one. Only one pass
   is ever pending, and it reads the viewport at fire time rather than
   closing over a range that scrolling has since invalidated. */
const PREFETCH_RADIUS = 1; // pages either side of the visible range

const whenIdle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 500 }) : setTimeout(fn, 150));

/* Never cancelled on a view rebuild or a grouping change: the callback
   reads S.view / S.groups at fire time rather than closing over them, so a
   pass scheduled against the old view simply warms the right pages of the
   new one — and ensurePage's own generation check discards anything that
   was already in flight across the change. */
let prefetchHandle = null;
function schedulePrefetch() {
  if (prefetchHandle !== null) return;
  prefetchHandle = whenIdle(() => {
    prefetchHandle = null;
    if (!S.view) return;
    if (S.groupByCols.length) prefetchGroupPages();
    else prefetchFlatPages();
  });
}

function prefetchFlatPages() {
  const maxPage = Math.floor(Math.max(0, S.view.row_count - 1) / PAGE);
  const [firstPage, lastPage] = visiblePageRange();
  for (let d = 1; d <= PREFETCH_RADIUS; d++) {
    for (const idx of [lastPage + d, firstPage - d]) {
      if (idx >= 0 && idx <= maxPage) ensurePage(idx, { prefetch: true });
    }
  }
}

/* Grouped mode's boundaries are closer together and there are two kinds:
   the next page *within* a big expanded group, and the first page of the
   *next* expanded group. Both stall the same way, so both get warmed. */
function prefetchGroupPages() {
  const body = $('body');
  const virt = vScroll(body, S.groupTotalRows, headH());
  const first = Math.max(0, Math.floor(virt / ROW_H) - OVERSCAN);
  const last = Math.min(S.groupTotalRows - 1, first + Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2);

  for (const [vpos, step] of [[last, 1], [first, -1]]) {
    const c = groupCoordAt(vpos);
    if (c) {
      const page = Math.floor(c.localIdx / PAGE) + step;
      if (page >= 0 && page * PAGE < c.g.rowCount) ensureGroupPage(c.g, page, { prefetch: true });
    }
    const gi = nextExpandedLeaf(findGroupAt(Math.max(0, Math.min(vpos, S.groupTotalRows - 1))), step);
    if (gi !== null) ensureGroupPage(S.groups[gi], step > 0 ? 0 : Math.floor((S.groups[gi].rowCount - 1) / PAGE), { prefetch: true });
  }
}

/* The next expanded leaf group in `step` direction from `gi`, skipping the
   headers of collapsed and non-leaf nodes — the next node that actually has
   rows to warm. Null when there isn't one. */
function nextExpandedLeaf(gi, step) {
  for (let i = gi + step; i >= 0 && i < S.groups.length; i += step) {
    const g = S.groups[i];
    if (g.expanded && isLeafLevel(g.level) && g.rowCount) return i;
  }
  return null;
}

/* `pos` is a view position in flat mode and a position in the flattened
   group tree in grouped mode — the two share one address space so that
   every row-level consumer (the cursor, the cell range, the row menu, copy,
   tagging, the detail pane) works in both without a second implementation.
   See the group-by block for how grouped positions stay pinned to their
   rows across an expand/collapse. Returns null for a group header row and
   for a data row whose page hasn't landed yet — both mean "no row here". */
const rowAt = (pos) => (S.groupByCols.length ? groupDataRowAt(pos) : S.rowsByPos.get(pos));

/* -------------------------------------------------------------- painting */

/* Kept in sync from render() (called after every S.selection mutation —
   row clicks, checkbox toggles, tag/copy actions that clear it, etc.)
   rather than from each of those sites individually. Disabled under a
   grouping: rows there *are* selectable, but this box means "every row in
   the view", and the flattened tree it would have to check is a mix of
   data rows and group headers whose collapsed groups aren't even loaded.
   Tag-the-whole-view (Shift + a tag hotkey) and the group menu's
   tag-this-group both do that job server-side without the ambiguity. */
function syncSelectAllCheckbox() {
  const cb = $('selectAllRows');
  if (!cb) return;
  if (S.groupByCols.length || !S.view || !S.view.row_count) {
    cb.checked = false;
    cb.indeterminate = false;
    cb.disabled = true;
    return;
  }
  cb.disabled = false;
  const n = selCount();
  cb.checked = n >= S.view.row_count;
  cb.indeterminate = n > 0 && n < S.view.row_count;
}

/* The sticky header is in-flow at the top of the scroll content, so the
   virtualized .rows block has to start below it — its height isn't a
   constant (the filter row, wrapping, zoom), so it's measured and applied
   on every paint (a no-op write when unchanged). Also the term every
   scroll-geometry calculation uses: row `pos` occupies content
   y ∈ [headH() + pos*ROW_H, headH() + (pos+1)*ROW_H). The *top*-edge
   visibility math is unchanged by the header (it overlays exactly the
   space it occupies), but anything anchoring to the viewport bottom or
   its height must subtract it. */
function headH() { return $('gridHead').offsetHeight; }

/* The spacer height to use for `total` rows — capped, see MAX_SPACER_PX. */
function spacerPx(total) { return Math.min(total * ROW_H, MAX_SPACER_PX); }

/* scrollTop as the rest of the grid means it: an offset into `total * ROW_H`
   pixels of rows. Identity below the spacer cap; a linear rescale above it.
   `head` is the in-scroller sticky header's height — headH() for the grid, 0
   for the timeline, whose header sits outside its scroller. Both ends are
   anchored (0 maps to 0, max-scroll maps to max-offset), so the last row is
   exactly reachable rather than merely nearly so. */
function vScroll(scroller, total, head = 0) {
  const want = total * ROW_H;
  if (want <= MAX_SPACER_PX) return scroller.scrollTop;
  const maxReal = Math.max(0, head + MAX_SPACER_PX - scroller.clientHeight);
  const maxWant = Math.max(0, head + want - scroller.clientHeight);
  if (maxReal <= 0) return 0;
  return Math.min(scroller.scrollTop * (maxWant / maxReal), maxWant);
}

/* Inverse of vScroll: the real scrollTop that lands on virtual offset `virt`.
   Clamps into range on the way, so callers can hand it an unbounded target
   (a row far past the end, a negative centring term) the way they used to
   hand one straight to scrollTop. */
function rScroll(scroller, total, virt, head = 0) {
  const want = total * ROW_H;
  const maxWant = Math.max(0, head + want - scroller.clientHeight);
  const target = Math.min(Math.max(0, virt), maxWant);
  if (want <= MAX_SPACER_PX) return target;
  const maxReal = Math.max(0, head + MAX_SPACER_PX - scroller.clientHeight);
  return maxWant > 0 ? target * (maxReal / maxWant) : 0;
}

/* Where the virtualized rows block has to sit for row `first` to line up
   under the row the scroll position actually points at. Below the cap this
   reduces to exactly first * ROW_H. Above it, subtracting the fractional part
   of the virtual offset is what keeps scrolling smooth — without it the top
   row snaps to the viewport edge and the whole grid moves in ROW_H steps. */
function rowsPaintY(scroller, virt, first) {
  const anchor = Math.floor(virt / ROW_H);
  return scroller.scrollTop - (virt - anchor * ROW_H) - (anchor - first) * ROW_H;
}

function syncRowsTop() {
  const t = headH() + 'px';
  const rowsEl = $('rows');
  if (rowsEl.style.top !== t) rowsEl.style.top = t;
}

/* Explicit pixel width for #rows — the exact gutter + visible-column
   total this render pass is about to lay cells out against. See the
   .rows comment in style.css for why this isn't left to intrinsic
   (max-content) sizing. */
function syncRowsWidth(widths, cols) {
  const w = GUTTER_W + cols.reduce((a, name) => a + widths[name], 0) + 'px';
  const rowsEl = $('rows');
  if (rowsEl.style.width !== w) rowsEl.style.width = w;
}

function render() {
  if (!S.view) return;
  syncSelectAllCheckbox();
  if (S.groupByCols.length) { renderGrouped(); return; }
  syncRowsTop();
  const body = $('body');
  const rowsEl = $('rows');
  const total = S.view.row_count;
  const virt = vScroll(body, total, headH());
  const first = Math.max(0, Math.floor(virt / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);

  /* Clamped to the pages the view actually has. Without the cap, a grid
     still scrolled past the end of a view that just got shorter asks for a
     page beyond the last row, which can only ever come back empty — and an
     empty page is now a desync signal (see ensurePage), so requesting one
     on purpose would spin rebuilds for no reason. */
  const lastPage = Math.floor(Math.max(0, total - 1) / PAGE);
  const wantLast = Math.min(lastPage, Math.floor(Math.max(first, last - 1) / PAGE));
  for (let p = Math.floor(first / PAGE); p <= wantLast; p++) ensurePage(p);
  schedulePrefetch();

  const ctx = rowPaintContext();

  syncRowsWidth(ctx.widths, ctx.cols);
  rowsEl.style.transform = `translateY(${rowsPaintY(body, virt, first)}px)`;
  const frag = document.createDocumentFragment();

  for (let pos = first; pos < last; pos++) frag.append(buildDataRow(pos, rowAt(pos), ctx));
  rowsEl.replaceChildren(frag);
  renderTagToolbar();
}

/* Everything a paint pass hoists out of its row loop — built once per
   render and handed to buildDataRow for every row. Shared by the flat and
   grouped painters so a change to either lands in both. */
function rowPaintContext() {
  const cols = visibleCols();
  return {
    cols,
    colMeta: Object.fromEntries(S.columns.map((c) => [c.name, c])),
    idx: Object.fromEntries(S.columns.map((c, i) => [c.name, i])),
    tagColor: Object.fromEntries(S.tags.map((t) => [t.id, t.color])),
    widths: Object.fromEntries(cols.map((name) => [name, colWidth(name)])),
    needle: S.search.trim().toLowerCase(),
  };
}

/* One data row's DOM. `pos` addresses the row the way the current mode
   does — a view position when flat, a flattened-tree position when grouped
   — and is what every delegated listener on #body reads back off
   dataset.pos. Grouped mode paints through here rather than through a
   reduced copy of it precisely so that selection, tag stripes, the note
   mark and the cell-range highlight can't be present in one mode and
   quietly missing in the other. */
function buildDataRow(pos, r, { cols, colMeta, idx, tagColor, widths, needle }) {
  const row = el('div', 'row' + (r ? '' : ' pending'));
  row.dataset.pos = pos;
  if (pos === S.cursor) row.classList.add('cursor');
  if (selHas(pos)) row.classList.add('selected');

  // Three fixed slots (see .gutter in style.css): the checkbox, a middle
  // strip for tag colors + the note mark, then the rid hard right. The
  // middle slot is always present even when empty so the checkbox and the
  // number keep the same x-position on every row regardless of whether
  // that row happens to be tagged or annotated.
  const g = el('div', 'gutter');
  g.style.flexBasis = GUTTER_W + 'px';
  const cb = el('input');
  cb.type = 'checkbox';
  cb.className = 'rowcheck';
  cb.checked = selHas(pos);
  const mid = el('div', 'gutter-mid');
  if (r) {
    for (const tid of r.tags) {
      const st = el('div', 'stripe');
      st.style.background = tagColor[tid] || '#888';
      mid.append(st);
    }
    if (r.note) mid.append(el('span', 'has-note', '✎'));
  }
  g.append(cb, mid, el('span', 'rid', r ? String(r.rid) : '·'));
  row.append(g);

  cols.forEach((name, ci) => {
    const c = el('div', 'cell' + (colMeta[name] && colMeta[name].type === 'number' ? ' num' : ''));
    c.style.flexBasis = widths[name] + 'px';
    c.dataset.col = ci;
    if (cellInRange(pos, ci)) c.classList.add('cell-selected');
    const val = r ? r.cells[idx[name]] : '';
    if (val != null && val !== '') {
      // Keep the raw value (with highlight) when it's what matched the
      // search, so the matched substring stays visible — only substitute
      // the formatted display when there's nothing to highlight.
      if (needle && String(val).toLowerCase().includes(needle)) highlight(c, String(val), needle);
      else c.textContent = displayCell(name, val);
    }
    row.append(c);
  });
  return row;
}

function renderTagToolbar() {
  const bar = $('tagToolbar');
  const count = selCount();
  if (!count) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.replaceChildren(el('span', 'tag-toolbar-count', `${count.toLocaleString()} selected`));
  for (const t of S.tags) {
    const btn = el('button', 'tag-chip');
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    btn.append(sw, el('span', null, t.name));
    btn.title = `Tag ${count.toLocaleString()} selected row(s) as ${t.name}`;
    btn.onclick = () => applyTag(t);
    bar.append(btn);
  }
  const clear = el('button', 'btn ghost', 'Clear selection');
  clear.onclick = () => { selClear(); render(); };
  bar.append(clear);
}

function highlight(node, text, needle) {
  const lower = text.toLowerCase();
  let i = 0, from = 0;
  while ((i = lower.indexOf(needle, from)) !== -1) {
    node.append(text.slice(from, i));
    const m = el('mark', null, text.slice(i, i + needle.length));
    node.append(m);
    from = i + needle.length;
  }
  node.append(text.slice(from));
}

/* ----------------------------------------------------------------- group-by */

/* Nested multi-column grouping. S.groups is a FLAT array that reflects the
   currently *visible* tree, in display order — expanding a node splices its
   children in immediately after it; collapsing removes that contiguous run.
   This is the key design choice: it lets the prefix-sum virtualization below
   stay almost identical to a single-level grouping, since it only ever needs
   "an ordered list of nodes, each contributing 1 header row + (leaf and
   expanded ? rowCount : 0) data rows" — a non-leaf expanded node contributes
   just its own header; its children are separate entries right after it
   that contribute their own spans. Only the *last* level (level ===
   S.groupByCols.length - 1) ever materializes real data rows via
   /api/group_expand; every other level's "expand" is another
   /api/group_summary call scoped by `path`.

   Data rows here are ordinary rows: they paint through the same
   buildDataRow the flat grid uses and address themselves with the same
   `pos` (a position in this flattened tree rather than in the view), so
   selection, the cursor, the cell range, the row menu, copy, tagging and
   the detail pane all work without a second implementation. The one thing
   grouped positions do that flat ones don't is *move* — expanding or
   collapsing renumbers everything below the toggled header, which
   shiftGroupPositions applies exactly to whatever was pointing at it. */

function isLeafLevel(level) { return level === S.groupByCols.length - 1; }

function rebuildGroupPrefix() {
  let pos = 0;
  S.groupPrefix = S.groups.map((g) => {
    const start = pos;
    pos += 1 + (g.expanded && isLeafLevel(g.level) ? g.rowCount : 0);
    return start;
  });
  S.groupTotalRows = pos;
}

function findGroupAt(vpos) {
  let lo = 0, hi = S.groups.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (S.groupPrefix[mid] <= vpos) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

/* Returns a promise that settles once the page is cached or has finished
   failing — the same contract ensurePage has in flat mode, and for the same
   reason: waitForGroupPages needs something to await before it can promise
   a caller that every row it asked for is really there. S.groupPending
   holds the in-flight promise (it was a bare marker Set) so concurrent
   callers for one page share the one request. */
function ensureGroupPage(g, pageIdx, { prefetch } = {}) {
  const key = `${g.viewId}:${pageIdx}`;
  if (S.groupPages.has(key)) return Promise.resolve();
  const inFlight = S.groupPending.get(key);
  if (inFlight) return inFlight;
  const gen = S.groupPageGen;
  const p = api(`/api/rows?view_id=${g.viewId}&start=${pageIdx * PAGE}&count=${PAGE}`)
    .then((data) => {
      if (!S.groupByCols.length || S.groupPageGen !== gen) return; // left group mode, or the cache was invalidated, before this resolved
      S.groupPages.set(key, data.rows);
      // A prefetched page isn't on screen — nothing to repaint for. The
      // rows that *are* on screen arrive through the unflagged path, which
      // still renders on every arrival (same reasoning as ensurePage).
      if (!prefetch) render();
    })
    .catch(() => {})
    .finally(() => { S.groupPending.delete(key); });
  S.groupPending.set(key, p);
  return p;
}

/* Grouped mode's answer to clearPageCache: after a bulk tag the server has
   changed rows this client never fetched, so every cached page's `tags`
   array is suspect. Bumping the generation is the half that's easy to miss
   — a fetch issued before the tag would otherwise land afterwards and put
   the pre-tag rows straight back. */
function clearGroupPageCache() {
  S.groupPages.clear();
  S.groupPending.clear();
  S.groupPageGen++;
}

function groupRowAt(g, localIdx) {
  const pageIdx = Math.floor(localIdx / PAGE);
  const page = S.groupPages.get(`${g.viewId}:${pageIdx}`);
  if (!page) { ensureGroupPage(g, pageIdx); return null; }
  return page[localIdx - pageIdx * PAGE] || null;
}

/* Which group, and which row inside it, a flattened-tree position lands on
   — or null when that position is a group header rather than a data row.
   Structural only: it reads the tree and the prefix sums, never a page, so
   every caller that just needs "is there a row here" (selSetRange, the
   click handlers, the context menu) can ask synchronously and for free. */
function groupCoordAt(vpos) {
  if (!S.groups.length || vpos < 0 || vpos >= S.groupTotalRows) return null;
  const gi = findGroupAt(vpos);
  const g = S.groups[gi];
  const localIdx = vpos - S.groupPrefix[gi] - 1;
  if (localIdx < 0) return null;                      // the header row itself
  if (!g.expanded || !isLeafLevel(g.level)) return null;
  return { gi, g, localIdx };
}

/* The row at a flattened-tree position, or null for a header row / a data
   row still paging in. This is what rowAt() dispatches to under grouping. */
function groupDataRowAt(vpos) {
  const c = groupCoordAt(vpos);
  return c ? groupRowAt(c.g, c.localIdx) : null;
}

/* Loads every page the given flattened positions need, or throws — the
   grouped-mode counterpart of waitForPages, and it exists for the same
   reason that one does: a bulk tag or copy that quietly skipped the rows
   it couldn't load would corrupt the analyst's record of what they've
   triaged while looking like it worked. Bounded the same way, since these
   are the same single-connection backend's pages. */
async function waitForGroupPages(positions) {
  const wanted = new Map(); // "viewId:pageIdx" -> {g, pageIdx}
  for (const pos of positions) {
    const c = groupCoordAt(pos);
    if (!c) continue; // a header row: nothing to load
    const pageIdx = Math.floor(c.localIdx / PAGE);
    wanted.set(`${c.g.viewId}:${pageIdx}`, { g: c.g, pageIdx });
  }
  const queue = [...wanted.entries()].filter(([key]) => !S.groupPages.has(key));
  let next = 0;
  const worker = async () => {
    while (next < queue.length) {
      const [key, { g, pageIdx }] = queue[next++];
      await ensureGroupPage(g, pageIdx);
      if (!S.groupPages.has(key)) throw new Error('could not load every row in the selection');
    }
  };
  await Promise.all(Array.from({ length: Math.min(PAGE_FETCH_CONCURRENCY, queue.length) }, worker));
}

/* "Load whatever these positions need," in whichever mode is current. Flat
   mode's page indices are the root view's; grouped mode's are per-group
   sub-view pages, so the two index spaces can't be shared — but every
   caller (copy, bulk tag) only ever wants "make sure these rows are here,
   or fail loudly," which is the same ask either way. */
function loadRowsForPositions(positions) {
  if (S.groupByCols.length) return waitForGroupPages(positions);
  return waitForPages([...new Set(positions.map((p) => Math.floor(p / PAGE)))]);
}

/* True when at least one of these positions still needs a fetch — drives
   the "Copying N rows…" toast, which is only worth showing for a copy that
   will actually go to the network. */
function positionsNeedLoading(positions) {
  if (!S.groupByCols.length) return positions.some((p) => !S.pages.has(Math.floor(p / PAGE)));
  return positions.some((pos) => {
    const c = groupCoordAt(pos);
    return c && !S.groupPages.has(`${c.g.viewId}:${Math.floor(c.localIdx / PAGE)}`);
  });
}

function renderGroupHeaderRow(g, gi) {
  const row = el('div', 'row group-header-row');
  row.dataset.groupIdx = gi;
  row.dataset.level = g.level;
  row.style.setProperty('--group-level', g.level);
  const arrow = g.expanded ? '▾' : '▸';
  const colName = S.groupByCols[g.level];
  const label = el('div', 'group-header-label');
  label.append(el('span', 'group-header-arrow', arrow));
  label.append(el('span', 'group-header-col', groupColLabel(colName) + ': '));
  // A tag level's swatch, so a grouped-by-tag list reads the same way the
  // tag ribbon and the row stripes do.
  const tag = isTagGroupCol(colName) ? S.tags.find((x) => x.id === g.value) : null;
  if (tag) {
    const sw = el('span', 'swatch');
    sw.style.background = tag.color;
    label.append(sw);
  }
  label.append(el('span', 'group-header-value', groupValueLabel(colName, g.value)));
  label.append(el('span', 'group-header-count', `${g.count.toLocaleString()} row${g.count === 1 ? '' : 's'}`));
  row.append(label);
  return row;
}

function renderGrouped() {
  syncRowsTop();
  const body = $('body');
  const rowsEl = $('rows');
  rebuildGroupPrefix();
  const total = S.groupTotalRows;
  $('spacerY').style.height = spacerPx(total) + 'px';

  const virt = vScroll(body, total, headH());
  const first = Math.max(0, Math.floor(virt / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);

  rowsEl.style.transform = `translateY(${rowsPaintY(body, virt, first)}px)`;
  const frag = document.createDocumentFragment();
  const ctx = rowPaintContext();
  syncRowsWidth(ctx.widths, ctx.cols);

  for (let vpos = first; vpos < last; vpos++) {
    if (!S.groups.length) break;
    const gi = findGroupAt(vpos);
    const g = S.groups[gi];
    const localOffset = vpos - S.groupPrefix[gi];
    if (localOffset === 0) frag.append(renderGroupHeaderRow(g, gi));
    else frag.append(buildDataRow(vpos, g.expanded ? groupRowAt(g, localOffset - 1) : null, ctx));
  }
  rowsEl.replaceChildren(frag);
  schedulePrefetch();
  renderTagToolbar();
}

function makeGroupNode(gr, level, path) {
  return { value: gr.value, count: gr.count, level, path, expanded: false, viewId: null, rowCount: gr.count };
}

/* Fetches one level's groups, scoped by `path` (every outer level already
   fixed) — level is implied by path.length, since groupByCols is ordered. */
async function fetchGroupLevel(path) {
  const column = S.groupByCols[path.length];
  const params = new URLSearchParams({
    view_id: S.view.view_id, column, order: S.groupSort === 'value' ? 'value' : 'count', direction: S.groupSortDir,
  });
  if (path.length) params.set('path', JSON.stringify(path));
  const token = opToken();
  params.set('op_token', token);
  const disarmCancel = armOpCancel(token);
  let res;
  try {
    res = await api(`/api/group_summary?${params.toString()}`);
  } catch (e) {
    // Cancelled: hand back an empty level rather than throwing — the
    // grouping stays on with no groups, and dropping it is one keypress.
    if (e.status === 499) { toast('Grouping cancelled', 2500); return []; }
    throw e;
  } finally {
    disarmCancel();
  }
  if (res.truncated) toast(`Showing the top ${res.groups.length.toLocaleString()} groups`, 4000);
  return res.groups;
}

/* Grouped and flat positions are the same numbers over different row sets,
   so anything that switches between them — or rebuilds the tree — has to
   drop what the old numbers were pointing at rather than let a selection
   silently land on different rows. */
function clearGroupSelectionState() {
  selClear();
  S.cursor = -1;
  S.anchor = -1;
  S.cellRange = null;
  S.cellAnchor = null;
}

/* Expanding or collapsing a group renumbers every flattened position below
   it, and selection/cursor/cell-range all live in that number space (see
   rowAt). Rather than re-resolving them through row identity — which would
   need a lookup this tree has no index for — the shift is applied exactly:
   the toggled header sits at `headerPos` and doesn't move, everything above
   it doesn't move, and everything below moves by the change in total rows.
   Positions inside a collapsed span are dropped, not shifted: those rows
   are no longer on screen, and keeping them selected would mean a tag
   landing on rows the analyst can't see.

   Call with the tree already mutated and `oldTotal` captured before it was
   — rebuildGroupPrefix() here is what makes S.groupTotalRows current. */
function shiftGroupPositions(headerPos, oldTotal) {
  rebuildGroupPrefix();
  const delta = S.groupTotalRows - oldTotal;
  if (!delta) return;
  const removedEnd = headerPos - delta; // collapse only: last position that vanished
  const remap = (pos) => {
    if (pos <= headerPos) return pos;
    if (delta < 0 && pos <= removedEnd) return null;
    return pos + delta;
  };
  selRemap(remap);
  if (S.cursor > headerPos) S.cursor = remap(S.cursor) ?? headerPos;
  if (S.anchor > headerPos) S.anchor = remap(S.anchor) ?? -1;
  // A cell range spanning the toggled group can't survive it intact —
  // its rows are no longer contiguous — so it goes rather than silently
  // covering different rows than it did a moment ago.
  if (S.cellRange && (S.cellRange.r1 > headerPos)) { S.cellRange = null; S.cellAnchor = null; }
}

/* The (headerPos, oldTotal) pair shiftGroupPositions needs, read at the
   moment of the mutation rather than at the top of toggleGroup: the expand
   paths await a fetch first, and another toggle landing during that await
   would leave both numbers describing a tree that no longer exists. */
function groupShiftAnchor(gi) {
  rebuildGroupPrefix();
  return [S.groupPrefix[gi], S.groupTotalRows];
}

async function toggleGroup(gi) {
  const g = S.groups[gi];
  if (!g) return;
  if (g.expanded) {
    // Collapse: drop every following node that's a descendant of g — a
    // contiguous run, since children are always spliced in right after
    // their parent, and grandchildren right after their own parent, etc.
    let end = gi + 1;
    while (end < S.groups.length && S.groups[end].level > g.level) end++;
    for (let k = gi + 1; k < end; k++) {
      if (S.groups[k].viewId) api(`/api/view/${S.groups[k].viewId}`, { method: 'DELETE' }).catch(() => {});
    }
    if (g.viewId) { api(`/api/view/${g.viewId}`, { method: 'DELETE' }).catch(() => {}); g.viewId = null; }
    const [headerPos, oldTotal] = groupShiftAnchor(gi);
    S.groups.splice(gi + 1, end - gi - 1);
    g.expanded = false;
    shiftGroupPositions(headerPos, oldTotal);
    render();
    return;
  }
  if (isLeafLevel(g.level)) {
    try {
      const res = await post('/api/group_expand', {
        view_id: S.view.view_id, column: S.groupByCols[g.level], value: g.value, path: g.path,
      });
      g.viewId = res.view_id;
      g.rowCount = res.row_count;
    } catch (e) {
      toast('Could not expand group: ' + e.message, 4000);
      return;
    }
    const [headerPos, oldTotal] = groupShiftAnchor(gi);
    g.expanded = true;
    shiftGroupPositions(headerPos, oldTotal);
    render();
  } else {
    const childPath = [...g.path, { column: S.groupByCols[g.level], value: g.value }];
    try {
      const children = await fetchGroupLevel(childPath);
      const [headerPos, oldTotal] = groupShiftAnchor(gi);
      S.groups.splice(gi + 1, 0, ...children.map((gr) => makeGroupNode(gr, g.level + 1, childPath)));
      g.expanded = true;
      shiftGroupPositions(headerPos, oldTotal);
      render();
    } catch (e) {
      toast('Could not expand group: ' + e.message, 4000);
    }
  }
}

async function closeAllGroupViews() {
  for (const g of S.groups) {
    if (g.viewId) { api(`/api/view/${g.viewId}`, { method: 'DELETE' }).catch(() => {}); g.viewId = null; }
  }
}

/* Rebuilds the top level of the group tree from scratch — called whenever
   the grouping columns/sort change, or the underlying view is rebuilt
   (filter/search/sort — the old group views are gone with it regardless). */
async function regroupAll() {
  await closeAllGroupViews();
  S.groups = [];
  S.groupPages.clear();
  S.groupPending.clear();
  // The tree is about to be rebuilt from scratch, so every position that
  // addressed a row in the old one now addresses something else — same rule
  // as a view rebuild in flat mode (CLAUDE.md: positions are view-specific).
  clearGroupSelectionState();
  renderGroupStrip();
  if (!S.groupByCols.length || !S.view) { render(); drawRail(); return; }
  try {
    const top = await fetchGroupLevel([]);
    S.groups = top.map((gr) => makeGroupNode(gr, 0, []));
  } catch (e) {
    toast('Group-by failed: ' + e.message, 4000);
    S.groupByCols = [];
    renderGroupStrip();
  }
  render();
  drawRail();
}

/* "Group by the tags on the row" rather than by anything in the file.
   Travels through every grouping path — S.groupByCols, the saved filter's
   group_by, /api/group_summary's `column` — as an ordinary column name, so
   nothing between here and store.py needs a second notion of what a
   grouping level is. Kept in step with store.py's TAG_GROUP_COLUMN, which
   reserves the name at ingest so a real header can never collide with it.

   A tag group's *value* is a tag id (tag names aren't unique), and the
   untagged group's value is null — both rendered through
   groupValueLabel(). */
const TAG_GROUP_COLUMN = '__tag__';
const isTagGroupCol = (c) => c === TAG_GROUP_COLUMN;

/* What a grouping level is called on screen. */
function groupColLabel(column) {
  return isTagGroupCol(column) ? 'Tag' : column;
}

/* What one group's value is called on screen: a tag's name for a tag
   level (falling back to its id if the tag was deleted underneath the
   grouping), the value itself otherwise. */
function groupValueLabel(column, value) {
  if (isTagGroupCol(column)) {
    if (value === null || value === undefined) return '(untagged)';
    const t = S.tags.find((x) => x.id === value);
    return t ? t.name : `tag ${value}`;
  }
  return value === null || value === '' ? '(empty)' : String(value);
}

/* Adds a column as the innermost (last) grouping level — the drop target
   for dragging a header into the group strip. Removes it from the normal
   column list (S.order) so it doesn't also render as a data column while
   grouped; S.preGroupOrder snapshots S.order the first time this happens,
   so dropGrouping() can restore the original layout exactly. The tag
   pseudo-column isn't in S.order to begin with, so that filter is a no-op
   for it and it stays out of the way. */
function addGroupLevel(column) {
  if (S.groupByCols.includes(column)) return;
  if (!S.preGroupOrder) S.preGroupOrder = [...S.order];
  S.groupByCols.push(column);
  S.order = S.order.filter((n) => n !== column);
  renderHead();
  regroupAll();
}

function removeGroupLevel(i) {
  const [removed] = S.groupByCols.splice(i, 1);
  if (!S.groupByCols.length) { dropGrouping(); return; }
  // The tag pseudo-column has no column to give back to the layout.
  if (!isTagGroupCol(removed) && !S.order.includes(removed)) S.order.push(removed);
  renderHead();
  regroupAll();
}

async function dropGrouping() {
  await closeAllGroupViews();
  S.groupByCols = [];
  S.groups = [];
  clearGroupSelectionState(); // grouped positions don't mean anything in the flat view

  if (S.preGroupOrder) { S.order = S.preGroupOrder; S.preGroupOrder = null; }
  renderHead();
  renderGroupStrip();
  render();
  drawRail();
  saveLayout();
}

/* Replace the whole grouping set at once (preset apply, toggle restore) —
   the incremental addGroupLevel/removeGroupLevel path exists for drag
   interactions, but swapping one saved grouping for another needs the
   pre-grouping column order restored first, or a formerly-grouped column
   leaks out of the visible layout. Caller renders + regroups (or lets
   rebuildView's own regroupAll do it). */
function setGrouping(cols, gsort, gdir) {
  if (S.preGroupOrder) { S.order = S.preGroupOrder; S.preGroupOrder = null; }
  const valid = (cols || []).filter((c) => isTagGroupCol(c) || S.columns.some((x) => x.name === c));
  S.groupByCols = [];
  if (valid.length) {
    S.preGroupOrder = [...S.order];
    S.groupByCols = valid;
    S.order = S.order.filter((n) => !valid.includes(n));
  }
  if (gsort) S.groupSort = gsort;
  if (gdir) S.groupSortDir = gdir;
}

/* One keypress parks the grouping, the next brings the same grouping back —
   independent of the filters (same independence toggleTimeRange has),
   for flipping between "the shape of the data" and "the rows themselves". */
async function toggleGrouping() {
  if (S.groupByCols.length) {
    S.lastGroupBy = { cols: [...S.groupByCols], sort: S.groupSort, dir: S.groupSortDir };
    await dropGrouping();
    toast('Grouping off — press again to restore');
  } else if (S.lastGroupBy && S.lastGroupBy.cols.some((c) => S.columns.some((x) => x.name === c))) {
    setGrouping(S.lastGroupBy.cols, S.lastGroupBy.sort, S.lastGroupBy.dir);
    renderHead();
    await regroupAll();
  } else {
    toast('No grouping to restore — drag a column header into the Group by strip');
  }
}

let draggedPillIdx = null;

function wireGroupPillDrag(pill, idx) {
  pill.addEventListener('dragstart', (e) => {
    draggedPillIdx = idx;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', S.groupByCols[idx]);
    pill.classList.add('dragging');
  });
  pill.addEventListener('dragend', () => {
    draggedPillIdx = null;
    document.querySelectorAll('.group-pill').forEach((p) => p.classList.remove('dragging'));
  });
  pill.addEventListener('dragover', (e) => {
    if (draggedPillIdx === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  });
  pill.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation(); // don't also fall through to the strip's own "add new column" drop
    if (draggedPillIdx === null || draggedPillIdx === idx) return;
    const [moved] = S.groupByCols.splice(draggedPillIdx, 1);
    S.groupByCols.splice(idx, 0, moved);
    renderGroupStrip();
    regroupAll();
  });
}

/* Both dimensions ('count' vs 'value' — the latter meaning alphabetical
   for text, numeric for a 'number' column, chronological for a bucketed
   'datetime' column, see group_summary in store.py) are independently
   sortable ascending or descending — applies to every level of a nested
   grouping, not per-level. */
const GROUP_SORT_OPTIONS = [
  { by: 'count', dir: 'desc' },
  { by: 'count', dir: 'asc' },
  { by: 'value', dir: 'asc' },
  { by: 'value', dir: 'desc' },
];
function groupSortLabel(by, dir, short) {
  if (by === 'count') {
    if (short) return `Count ${dir === 'asc' ? '↑' : '↓'}`;
    return dir === 'asc' ? 'Count — fewest first' : 'Count — most first';
  }
  if (short) return `Value ${dir === 'asc' ? '↑' : '↓'}`;
  return dir === 'asc' ? 'Value — low to high' : 'Value — high to low';
}

/* Tagging a row changes which tag group it belongs to, so a grouping that
   includes the tag pseudo-column is stale the moment a tag lands — the
   group counts and the membership of any expanded group's sub-view both.
   The tree gets rebuilt rather than patched: those sub-views live on the
   server and there is nothing here to patch them with. A grouping by an
   ordinary column is unaffected by tagging and stays exactly where it is,
   which is why this checks rather than always regrouping. */
function regroupIfGroupedByTag() {
  if (S.groupByCols.some(isTagGroupCol)) regroupAll();
}

/* The tag pseudo-column has no header to drag, so the strip carries its
   own way in. Offered as long as it isn't already a level — grouping by
   tag twice would be two identical levels. */
function groupByTagButton() {
  const btn = el('button', 'btn ghost group-tag-btn', '+ Tag');
  btn.title = 'Add a grouping level that buckets rows by the tags on them';
  btn.onclick = () => addGroupLevel(TAG_GROUP_COLUMN);
  return btn;
}

function renderGroupStrip() {
  const strip = $('groupStrip');
  strip.replaceChildren();
  strip.append(el('span', 'group-strip-label', 'Group by'));
  if (!S.groupByCols.length) {
    strip.append(el('span', 'group-strip-hint', 'drag a column header here'));
    strip.append(groupByTagButton());
    return;
  }
  S.groupByCols.forEach((name, i) => {
    const pill = el('div', 'group-pill');
    pill.draggable = true;
    pill.append(el('span', null, groupColLabel(name)));
    const rm = el('button', 'group-pill-rm', '✕');
    rm.title = 'Remove this grouping level';
    rm.onclick = (e) => { e.stopPropagation(); removeGroupLevel(i); };
    pill.append(rm);
    wireGroupPillDrag(pill, i);
    strip.append(pill);
    if (i < S.groupByCols.length - 1) strip.append(el('span', 'group-strip-arrow', '›'));
  });
  const sortBtn = el('button', 'btn ghost group-sort-btn', 'Sort: ' + groupSortLabel(S.groupSort, S.groupSortDir, true));
  sortBtn.title = 'Change how groups at every level are sorted';
  sortBtn.onclick = () => dropdownMenu(sortBtn, GROUP_SORT_OPTIONS.map((o) => ({
    label: (o.by === S.groupSort && o.dir === S.groupSortDir ? '✓ ' : '   ') + groupSortLabel(o.by, o.dir),
    onclick: () => { S.groupSort = o.by; S.groupSortDir = o.dir; regroupAll(); },
  })));
  strip.append(sortBtn);
  if (!S.groupByCols.some(isTagGroupCol)) strip.append(groupByTagButton());
  const dropAll = el('button', 'btn ghost group-drop-btn', 'Ungroup');
  dropAll.title = 'Drop all grouping — hotkey: ' + ((S.keymap.dropGrouping || [])[0] || '');
  dropAll.onclick = dropGrouping;
  strip.append(dropAll);
}

$('groupStrip').addEventListener('dragover', (e) => {
  if (!draggedCol || S.groupByCols.includes(draggedCol)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  $('groupStrip').classList.add('drag-over');
});
$('groupStrip').addEventListener('dragleave', () => $('groupStrip').classList.remove('drag-over'));
$('groupStrip').addEventListener('drop', (e) => {
  e.preventDefault();
  $('groupStrip').classList.remove('drag-over');
  if (draggedCol && !S.groupByCols.includes(draggedCol)) addGroupLevel(draggedCol);
});

async function drawRail() {
  const cv = $('rail');
  const ctx = cv.getContext('2d');
  cv.height = cv.clientHeight;
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!S.view || !S.view.row_count) return;
  let pts = [];
  try { pts = await api(`/api/tag_positions?view_id=${S.view.view_id}`); } catch { return; }
  const color = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
  for (const [pos, tid] of pts) {
    const y = Math.round((pos / S.view.row_count) * cv.height);
    ctx.fillStyle = color[tid] || '#888';
    ctx.fillRect(1, y, cv.width - 2, 2);
  }
}

/* ----------------------------------------------------------------- tags */

async function loadTags() {
  const d = await api(`/api/tags?source_id=${S.sourceId}`);
  S.tags = d.tags;
  // Whole-table counts. The ribbon shows the *view*-scoped ones the moment
  // there's a view to scope to (refreshTagCounts, below); until then these
  // are both the answer and the only answer.
  S.tagCountsAll = d.counts || {};
  S.tagCounts = d.counts || {};
  renderTagRibbon();
  renderTimelineTagFilter();
  refreshTagCounts();
  // Deleting a tag takes its history with it server-side, and opening a
  // different case swaps the Store (and so the whole stack) underneath us.
  refreshUndoState();
}

/* Re-reads the tag counts for whatever view is up. Called after every view
   rebuild and after every tagging operation, because both change the
   answer: a filter changes which rows are in scope, a tag changes which of
   them are tagged.

   Fire-and-forget on purpose — it's one aggregate over the view (the same
   join tag_positions makes, with the same untagged short-circuit) and the
   grid has no reason to wait on it. A failure leaves the previous numbers
   up rather than blanking the ribbon; a stale count for one paint is a far
   smaller lie than an empty one. */
async function refreshTagCounts() {
  const vid = S.view && S.view.view_id;
  // loadTags() runs while switching tables, when S.view can still be the
  // *previous* table's (live, not yet evicted) view — counting against that
  // would put one table's numbers under another's tags for the moment
  // before rebuildView lands.
  if (!vid || S.view.source_id !== S.sourceId) return;
  let d;
  try {
    d = await api(`/api/tag_counts?view_id=${vid}`);
  } catch {
    return; // expired view: the rebuild that replaces it calls back through here
  }
  if (!S.view || S.view.view_id !== vid) return; // superseded while in flight
  S.tagCounts = d.counts || {};
  renderTagRibbon();
}

function renderTagRibbon() {
  const rib = $('tagRibbon');
  rib.replaceChildren();
  for (const t of S.tags) {
    const chip = el('button', 'tag-chip');
    chip.setAttribute('aria-pressed', String(S.tagFilter.includes(t.id)));
    chip.style.color = S.tagFilter.includes(t.id) ? t.color : '';
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    chip.append(sw, el('span', null, t.name));
    if (t.hotkey) chip.append(el('span', 'key', t.hotkey));
    // Scoped to the current view, not the whole table: with a filter or a
    // search on, "how many of *these* are tagged" is the question the
    // ribbon is sitting next to. The whole-table number isn't dropped, it
    // moves into the tooltip — a count that silently changed meaning when a
    // filter went on would be worse than either number alone.
    const n = S.tagCounts[t.id] || 0;
    const all = S.tagCountsAll[t.id] || 0;
    if (n) chip.append(el('span', 'n', n.toLocaleString()));
    const scope = n === all
      ? `${all.toLocaleString()} tagged`
      : `${n.toLocaleString()} tagged in this view · ${all.toLocaleString()} in the table`;
    chip.title = `${scope}. Click to filter to ${t.name}. Press ${t.hotkey || '—'} to tag the selection.`;
    chip.onclick = () => {
      S.tagFilter = S.tagFilter.includes(t.id) ? [] : [t.id];
      renderTagRibbon();
      rebuildView({ keepScroll: false });
    };
    rib.append(chip);
  }
  const any = el('button', 'tag-chip');
  any.setAttribute('aria-pressed', String(S.tagFilter[0] === '__any__'));
  any.append(el('span', null, 'Any tag'));
  any.onclick = () => {
    S.tagFilter = S.tagFilter[0] === '__any__' ? [] : ['__any__'];
    renderTagRibbon();
    rebuildView({ keepScroll: false });
  };
  rib.append(any);
  const edit = el('button', 'tag-chip');
  edit.append(el('span', null, 'Edit tags'));
  edit.onclick = openTagEditor;
  rib.append(edit);
}

/* Above this many rows, tagging asks first — and, on the per-row path,
   that many rows also means a lot of pages to fetch before it can start. */
const BULK_TAG_CONFIRM_AT = 10000;

/* Tagging a selection.

   This used to be `positions.map(rowAt).filter(Boolean)` — which silently
   dropped every selected row the page cache hadn't seen. That was harmless
   while selections only came from shift-clicks inside loaded pages, and
   became a real correctness bug the moment the select-all checkbox existed:
   "select all 1.2M rows, press a tag hotkey" tagged the few hundred rows
   that happened to be cached and reported that smaller number in a toast.
   A silently partial tag is the worst failure mode this tool has — the
   analyst's own notion of what they've triaged is the thing being corrupted.

   So there are two paths and no third:
   - The whole view is selected (with, at most, a few explicitly unchecked
     rows): hand the view id and the exclusions to the server and let it do
     the set operation over the materialized view. Nothing needs fetching.
   - An explicit subset: fetch every page it spans *first* and fail loudly if
     that doesn't work, rather than tagging whatever happened to be there. */
async function applyTag(tag, on) {
  if (!S.view) return;
  if (!selCount()) {
    if (S.cursor < 0) return;
    // In grouped mode the cursor can sit on a group header, which isn't a
    // row — tag the whole group from its right-click menu instead.
    if (S.groupByCols.length && !groupCoordAt(S.cursor)) return;
    await tagRowsAtPositions(tag, [S.cursor], on);
    return;
  }
  if (S.selectAll) await tagWholeViewSelection(tag, on);
  else await tagRowsAtPositions(tag, selPositions(), on);
}

/* Resolves the toggle (`on === undefined`) the same way the old code did —
   from the first selected row — but tolerates that row not being cached,
   which "select all" makes likely. Defaulting to tagging (rather than
   untagging) when nothing's loaded matches what a bulk select-all is for. */
function resolveTagDirection(tag, on, samplePos) {
  if (on !== undefined) return on;
  const r = samplePos >= 0 ? rowAt(samplePos) : null;
  return r ? !r.tags.includes(tag.id) : true;
}

async function tagWholeViewSelection(tag, on) {
  const count = selCount();
  on = resolveTagDirection(tag, on, selFirst());
  if (count >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} ${count.toLocaleString()} selected rows as "${tag.name}"?`))) return;
  // Excluded rows are ones the analyst unchecked on screen, so they're
  // cached — but a view rebuild or cache eviction could have dropped one,
  // and guessing would tag a row that was explicitly deselected.
  let exclude = selExcludedPairs();
  if (exclude === null) {
    try {
      await waitForPages([...new Set(selExcludedPositions().map((p) => Math.floor(p / PAGE)))]);
    } catch (e) {
      toast('Could not tag: ' + e.message, 5000);
      return;
    }
    exclude = selExcludedPairs();
    if (exclude === null) { toast('Could not tag: deselected rows could not be loaded', 5000); return; }
  }
  setBusy(true);
  let res;
  try {
    res = await post('/api/row_tags/view', { view_id: S.view.view_id, tag_id: tag.id, on, exclude });
  } catch (e) {
    toast('Could not tag: ' + e.message, 5000);
    return;
  } finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  // Every cached row's `tags` array is now stale — the server changed rows
  // this client never fetched, so there's nothing to patch up in place.
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  const n = res.affected != null ? res.affected : count;
  toast(`${on ? 'Tagged' : 'Untagged'} ${n.toLocaleString()} row${n === 1 ? '' : 's'} · ${tag.name}`);
}

async function tagRowsAtPositions(tag, positions, on) {
  if (!positions.length) return;
  on = resolveTagDirection(tag, on, positions[0]);
  if (positions.length >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} ${positions.length.toLocaleString()} selected rows as "${tag.name}"?`))) return;
  if (positionsNeedLoading(positions)) {
    setBusy(true);
    try {
      await loadRowsForPositions(positions);
    } catch (e) {
      toast('Could not tag: ' + e.message, 5000);
      return;
    } finally { setBusy(false); }
  }
  const rows = positions.map((p) => rowAt(p));
  if (rows.some((r) => !r)) { toast('Could not tag: some selected rows could not be loaded', 5000); return; }
  // A merged view's selected rows can each belong to a different real
  // source — send their own (source_id, rid) pairs rather than the merge's
  // synthetic negative id, so tags land on the row's actual origin.
  const body = S.sourceId < 0
    ? { pairs: rows.map((r) => [r.source_id, r.rid]), tag_id: tag.id, on }
    : { source_id: S.sourceId, rids: rows.map((r) => r.rid), tag_id: tag.id, on };
  setBusy(true);
  let res;
  try { res = await post('/api/row_tags', body); }
  catch (e) { toast('Could not tag: ' + e.message, 5000); return; }
  finally { setBusy(false); }
  for (const r of rows) {
    r.tags = on ? [...new Set([...r.tags, tag.id])] : r.tags.filter((x) => x !== tag.id);
  }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  toast(`${on ? 'Tagged' : 'Untagged'} ${rows.length.toLocaleString()} row${rows.length === 1 ? '' : 's'} · ${tag.name}`);
}

/* ---------------------------------------------------------- undo (tags) */

/* The history itself lives server-side, for one reason: only the server
   knows which rows a change actually moved. "Tag the 171k rows in this
   view" is a set operation over a materialised view that this client never
   fetches a row of, and even on the explicit-selection path, undoing by
   re-sending the same rids with the direction flipped would strip the tag
   off rows that already carried it before. So the client tracks nothing
   but what the next undo *would* say, for the menu label. */
let UNDO_NEXT = { available: false, depth: 0 };

function setUndoState(next) {
  UNDO_NEXT = next || { available: false, depth: 0 };
}

async function refreshUndoState() {
  try { setUndoState(await api('/api/row_tags/undo')); }
  catch { setUndoState(null); }
}

async function undoLastTagChange() {
  let res;
  setBusy(true);
  try {
    res = await post('/api/row_tags/undo', {});
  } catch (e) {
    // 400 is the empty-history case, which is a normal thing to press
    // Ctrl+Z into rather than an error worth a five-second toast.
    toast(e.message === 'Nothing to undo' ? 'Nothing to undo' : 'Could not undo: ' + e.message,
          e.message === 'Nothing to undo' ? 2000 : 5000);
    await refreshUndoState();
    return;
  } finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  setUndoState(res.next);
  // Same reasoning as the bulk tag path: the server changed rows this
  // client may never have fetched, so there is nothing to patch in place.
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag(); // undo moved rows between tag groups too
  toast(`Undone: ${res.undone}`);
}

async function applyTagToView(tag) {
  if (!S.view || !S.view.row_count) return;
  if (!(await confirmDialog(`Tag all ${S.view.row_count.toLocaleString()} rows in this view as "${tag.name}"?`))) return;
  setBusy(true);
  let res;
  try { res = await post('/api/row_tags/view', { view_id: S.view.view_id, tag_id: tag.id, on: true }); }
  finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  toast(`Tagged ${res.affected.toLocaleString()} rows · ${tag.name}`);
}

/* --------------------------------------------------------------- detail */

/* Nested JSON/XML field values get pretty-printed + syntax colored in the
   detail pane (grid cells stay plain — single-line, truncated, virtualized).
   Everything here builds DOM nodes directly (never innerHTML) since field
   values are untrusted forensic data that can contain HTML-looking text. */

function tryParseJSON(v) {
  const s = v.trim();
  if (!s || (s[0] !== '{' && s[0] !== '[')) return null;
  try { return JSON.parse(s); } catch { return null; }
}

function looksLikeXml(v) {
  const s = v.trim();
  return s.startsWith('<') && s.endsWith('>') && /<\/?[a-zA-Z_][\w:.-]*[^>]*>/.test(s);
}

function prettyXml(xml) {
  // Heuristic reflow, not a real parser: forensic XML fragments are often
  // malformed/truncated, so this must degrade gracefully rather than throw.
  const tags = xml.replace(/>\s*</g, '><').split(/(?=<)/).filter(Boolean);
  let out = '', depth = 0;
  for (const tag of tags) {
    const isClosing = tag.startsWith('</');
    const isVoid = /\/>$/.test(tag) || tag.startsWith('<?') || tag.startsWith('<!');
    if (isClosing) depth = Math.max(0, depth - 1);
    out += '  '.repeat(depth) + tag + '\n';
    if (!isClosing && !isVoid) depth++;
  }
  return out.trim();
}

/* The unhighlighted-but-readable fallback for XML the browser's own
   parser rejects — truncated fragments, mismatched tags, the shapes real
   evidence actually contains. Regex-based on purpose: it has to degrade
   rather than throw, which is exactly what a parser can't do. Nothing it
   emits carries a path, because there is no trustworthy structure to
   address in a document that didn't parse. */
function appendXmlHighlighted(container, xmlText) {
  const tagRe = /<(\/?)([a-zA-Z_][\w:.-]*)((?:\s+[a-zA-Z_][\w:.-]*\s*=\s*"[^"]*")*)\s*(\/?)>/g;
  const attrRe = /([a-zA-Z_][\w:.-]*)(\s*=\s*)("[^"]*")/g;
  let last = 0, m;
  while ((m = tagRe.exec(xmlText))) {
    if (m.index > last) container.append(xmlText.slice(last, m.index));
    container.append('<' + m[1]);
    container.append(el('span', 'xtok-tag', m[2]));
    const attrs = m[3];
    if (attrs) {
      let aLast = 0, am;
      attrRe.lastIndex = 0;
      while ((am = attrRe.exec(attrs))) {
        if (am.index > aLast) container.append(attrs.slice(aLast, am.index));
        container.append(el('span', 'xtok-attr', am[1]));
        container.append(am[2]);
        container.append(el('span', 'xtok-attrval', am[3]));
        aLast = am.index + am[0].length;
      }
      container.append(attrs.slice(aLast));
    }
    container.append(m[4] + '>');
    last = m.index + m[0].length;
  }
  container.append(xmlText.slice(last));
}

/* ------------------ path-aware pretty-printing

   The pretty-printer builds from the *parsed* document rather than
   regexing over its serialized text, because every node it emits carries
   the path that addresses it (`data-path`) on the span. That path is what
   makes "right-click a field → add it as a column" a single click instead
   of a syntax the analyst has to learn and type: the same string the
   backend's json_field/xml_field operations take is already sitting on the
   node under the pointer.

   Everything appends DOM nodes and never innerHTML — field values are
   untrusted forensic data that routinely contains HTML-looking text. */

function jsonPathStep(base, seg) {
  if (typeof seg === 'number') return `${base}[${seg}]`;
  return /^[^.[\]"']+$/.test(seg) ? `${base}.${seg}` : `${base}["${String(seg).replace(/(["\\])/g, '\\$1')}"]`;
}

function jsonTokenClass(v) {
  if (v === null) return 'jtok-null';
  if (typeof v === 'boolean') return 'jtok-bool';
  if (typeof v === 'number') return 'jtok-num';
  return 'jtok-str';
}

/* A span the detail menu can act on: the path that addresses it, and the
   raw (unformatted) value it holds, so "filter to this" filters to what's
   in the data rather than to its pretty-printed rendering. */
function fieldNode(cls, text, path, value, kind) {
  const n = el('span', cls, text);
  if (path) {
    n.dataset.path = path;
    n.dataset.structKind = kind;
    if (value !== undefined) n.dataset.value = value;
    n.classList.add('struct-node');
  }
  return n;
}

function appendJsonNodes(container, value, path, indent) {
  const pad = '  '.repeat(indent);
  const padIn = '  '.repeat(indent + 1);
  if (Array.isArray(value)) {
    if (!value.length) { container.append('[]'); return; }
    container.append('[\n');
    value.forEach((v, i) => {
      container.append(padIn);
      appendJsonNodes(container, v, jsonPathStep(path, i), indent + 1);
      container.append(i < value.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + ']');
    return;
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (!keys.length) { container.append('{}'); return; }
    container.append('{\n');
    keys.forEach((k, i) => {
      const kp = jsonPathStep(path, k);
      const leaf = value[k];
      const scalar = !leaf || typeof leaf !== 'object';
      container.append(padIn);
      // The key carries the path too, so clicking either half of
      // `"user": "jacson"` means the same field.
      container.append(fieldNode('jtok-key', JSON.stringify(k), kp,
        scalar ? jsonLeafText(leaf) : undefined, 'json'));
      container.append(': ');
      appendJsonNodes(container, leaf, kp, indent + 1);
      container.append(i < keys.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + '}');
    return;
  }
  container.append(fieldNode(jsonTokenClass(value), JSON.stringify(value), path,
    jsonLeafText(value), 'json'));
}

/* Mirrors structparse._leaf_text — what the extracted column would hold,
   which is what the filter/copy actions should act on. */
function jsonLeafText(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* XML is rendered from a real parse when the browser can manage one, and
   falls back to the old heuristic reflow when it can't — forensic XML
   fragments are often malformed or truncated, and degrading to
   "unhighlighted but readable" beats showing nothing. Only the parsed path
   carries `data-path`; there is nothing trustworthy to address in a
   document that didn't parse. */
function parseXmlDoc(text) {
  const body = text.trim();
  // Same guard as structparse.load_xml: a DOCTYPE is refused rather than
  // parsed. DOMParser won't expand external entities, but keeping the two
  // sides on the same rule means the detail pane never offers a path the
  // backend would then decline to extract.
  if (/<!DOCTYPE/i.test(body)) return null;
  const parse = (t) => {
    const doc = new DOMParser().parseFromString(t, 'application/xml');
    return doc.querySelector('parsererror') ? null : doc.documentElement;
  };
  return parse(body)
    || parse(`<winnow-fragment>${body.replace(/^<\?xml[^>]*\?>/, '')}</winnow-fragment>`);
}

function xmlLocal(name) { return name.includes(':') ? name.split(':').pop() : name; }

const XML_ID_ATTRS = ['name', 'key', 'id'];

/* The frontend twin of structparse._sibling_selectors — repeated elements
   that carry an identifying attribute are addressed by it rather than by
   position, so an EVTX `<Data Name="LogonType">` offers a path that means
   the same thing in every row. Kept in step with the backend: a path this
   produces has to be one xml_field can resolve. */
function xmlSiblingSelectors(kids) {
  const groups = new Map();
  kids.forEach((c, i) => {
    const tag = xmlLocal(c.tagName);
    if (!groups.has(tag)) groups.set(tag, []);
    groups.get(tag).push(i);
  });
  const sel = new Array(kids.length).fill(0);
  for (const idxs of groups.values()) {
    let chosen = null;
    if (idxs.length > 1) {
      for (const want of XML_ID_ATTRS) {
        const real = idxs.map((i) => [...kids[i].attributes].find((a) => xmlLocal(a.name).toLowerCase() === want));
        const vals = real.map((a) => (a ? a.value : null));
        if (vals.every((v) => v !== null) && new Set(vals).size === vals.length) {
          chosen = { attr: xmlLocal(real[0].name), vals };
          break;
        }
      }
    }
    idxs.forEach((i, slot) => { sel[i] = chosen ? [chosen.attr, chosen.vals[slot]] : slot; });
  }
  return sel;
}

function xmlStep(tag, sel) {
  if (Array.isArray(sel)) return `${tag}[@${sel[0]}='${sel[1]}']`;
  return sel === 0 ? tag : `${tag}[${sel}]`;
}

function appendXmlNodes(container, node, path, indent) {
  const pad = '  '.repeat(indent);
  const tag = xmlLocal(node.tagName);
  const selector = path.endsWith(']') && /\[@[\w:.-]+='[^']*'\]$/.test(path);
  const kids = [...node.children];
  // A leaf element's text is its value, and the tag is what most people
  // aim at — so the opening tag carries it too, rather than only the text
  // run and the closing tag. Computed before anything is emitted because
  // the opening tag is written first.
  const leafText = kids.length ? undefined : (node.textContent || '').trim();
  container.append(pad + '<');
  container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
  for (const a of node.attributes) {
    const an = xmlLocal(a.name);
    container.append(' ');
    // An attribute that selected this element restates its own predicate;
    // it still renders, it just isn't offered as a separate field.
    const apath = selector && path.includes(`@${an}='${a.value}'`) ? null : `${path}@${an}`;
    container.append(fieldNode('xtok-attr', an, apath, a.value, 'xml'));
    container.append('=');
    container.append(fieldNode('xtok-attrval', `"${a.value}"`, apath, a.value, 'xml'));
  }
  if (!kids.length) {
    if (!leafText) { container.append('/>\n'); return; }
    container.append('>');
    container.append(fieldNode('xtok-text', leafText, path, leafText, 'xml'));
    container.append('</');
    container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
    container.append('>\n');
    return;
  }
  container.append('>\n');
  const sel = xmlSiblingSelectors(kids);
  kids.forEach((child, i) => {
    appendXmlNodes(container, child, `${path ? path + '/' : ''}${xmlStep(xmlLocal(child.tagName), sel[i])}`, indent + 1);
  });
  container.append(pad + '</');
  container.append(fieldNode('xtok-tag', tag, path, undefined, 'xml'));
  container.append('>\n');
}

function renderDetailContent(v) {
  const json = tryParseJSON(v);
  if (json !== null && typeof json === 'object') {
    const pre = el('pre', 'detail-pretty');
    appendJsonNodes(pre, json, '$', 0);
    return pre;
  }
  if (looksLikeXml(v)) {
    const root = parseXmlDoc(v);
    if (root) {
      const pre = el('pre', 'detail-pretty');
      // A synthetic fragment wrapper is ours, not the document's — its
      // children are what the analyst actually has, and what paths are
      // written against.
      if (root.tagName === 'winnow-fragment') {
        const kids = [...root.children];
        const sel = xmlSiblingSelectors(kids);
        kids.forEach((c, i) => appendXmlNodes(pre, c, xmlStep(xmlLocal(c.tagName), sel[i]), 0));
      } else {
        appendXmlNodes(pre, root, xmlLocal(root.tagName), 0);
      }
      return pre;
    }
    try {
      // Unparseable: the old heuristic reflow, highlighted but not
      // addressable.
      const pre = el('pre', 'detail-pretty');
      appendXmlHighlighted(pre, prettyXml(v));
      return pre;
    } catch { /* malformed fragment — fall through to plain text */ }
  }
  return document.createTextNode(v);
}

/* The detail pane only force-opens on double-click (activateRow's plain
   single-click path deliberately never calls showDetail) or the toggleDetail
   hotkey. Once it's open, though, cursor movement — click, arrow keys,
   ctrl/cmd-click — should keep it in sync with whatever row is now current,
   which is what this gates. */
function maybeShowDetail(pos) {
  if (!$('detail').hidden) showDetail(pos);
}

function showDetail(pos) {
  const r = rowAt(pos);
  const d = $('detail');
  if (!r) { d.hidden = true; $('detailResize').hidden = true; return; }
  d.hidden = false;
  $('detailResize').hidden = false;
  $('detailTitle').textContent = `Line ${r.rid}`;
  const dl = $('detailFields');
  dl.replaceChildren();
  S.columns.forEach((c, i) => {
    const v = r.cells[i];
    if (v == null || v === '') return;
    const dt = el('dt', null, c.name);
    dt.dataset.col = c.name;
    dl.append(dt);
    const dd = el('dd');
    // Which column a selection or a clicked node belongs to — the detail
    // menu's actions (filter, exclude, add as column) all need it, and the
    // <dd> is the only thing that still knows once you're deep inside a
    // pretty-printed document.
    dd.dataset.col = c.name;
    dd.append(renderDetailContent(displayCell(c.name, v)));
    dl.append(dd);
  });
  const note = $('noteInput');
  note.value = r.note || '';
  note.dataset.rid = r.rid;
  note.dataset.sourceId = r.source_id;
  $('noteStatus').textContent = '';
}

/* ------------------------------------------------- detail pane menu

   Right-clicking in the detail pane answers whichever of two questions the
   pointer is actually over, and often both at once:

   - text is selected → act on that substring (copy it, filter the column
     to it, search the table for it). A selection inside a JSON blob is a
     fragment, so it filters as *contains*, never as `=`: the cell it came
     from is a whole document and an exact match would return nothing.
   - the click landed on a node of a parsed JSON/XML document → act on that
     field (add it as a column, filter to its exact value, copy it). This
     is the path that makes extraction a click rather than a syntax to
     learn — `data-path` was put on the node by the pretty-printer.

   Both are scoped to the column the <dd> belongs to. */

function detailSelectionText() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return '';
  const node = sel.anchorNode;
  const holder = node && (node.nodeType === 1 ? node : node.parentElement);
  // A selection that started outside the detail fields isn't ours.
  if (!holder || !holder.closest('#detailFields')) return '';
  return String(sel).trim();
}

function detailMenuItems(ctx) {
  const items = [];
  const { column, selection, node } = ctx;

  if (selection) {
    const shown = ellipsize(selection);
    items.push({ header: `Selection in ${column}` });
    items.push({
      label: 'Copy',
      onclick: () => writeClipboardText(Promise.resolve(selection), 'Copied selection'),
    });
    items.push({
      label: `Filter ${column} to ${shown}`,
      title: 'Filters to rows whose value contains this text',
      onclick: () => filterByContains(column, selection),
    });
    items.push({
      label: `Filter ${column} to ${shown} only`,
      title: 'Drops every other filter and the search — the timeframe filter stays',
      onclick: () => filterByContains(column, selection, { only: true }),
    });
    items.push({
      label: `Exclude ${shown}`,
      onclick: () => filterByContains(column, selection, { exclude: true }),
    });
    items.push({
      label: `Search all columns for ${shown}`,
      onclick: () => searchForText(selection),
    });
  }

  if (node && node.dataset.path) {
    const path = node.dataset.path;
    const value = node.dataset.value;
    const kind = node.dataset.structKind;
    if (items.length) items.push('-');
    items.push({ header: ellipsize(path, 48), literal: true });
    items.push({
      label: 'Add as a column',
      title: `Adds a column holding ${path} from every row of "${column}"`,
      onclick: () => addExtractedColumn(column, path, kind),
    });
    if (value !== undefined && value !== '') {
      items.push({
        label: `Filter ${column} to this value`,
        title: 'Filters to rows whose document contains this value',
        onclick: () => filterByContains(column, value),
      });
      items.push({
        label: 'Copy value',
        onclick: () => writeClipboardText(Promise.resolve(value), 'Copied value'),
      });
    }
    items.push({ label: 'Copy path', onclick: () => writeClipboardText(Promise.resolve(path), 'Copied path') });
    items.push({
      label: 'Flatten this document into columns…',
      onclick: () => openFlattenModal(column),
    });
  }
  return items;
}

$('detail').addEventListener('contextmenu', (e) => {
  const dd = e.target.closest('#detailFields dd, #detailFields dt');
  if (!dd || !dd.dataset.col) return; // the note box and the header keep the browser's menu
  const ctx = {
    column: dd.dataset.col,
    selection: detailSelectionText(),
    node: e.target.closest('.struct-node'),
  };
  const items = detailMenuItems(ctx);
  if (!items.length) return;
  e.preventDefault();
  contextMenu(e, items);
});

/* Contains-filtering, as distinct from filterByValue's `=`. A selection or
   a field pulled out of a document is a *part* of the cell, so the filter
   that finds it again is the substring one. Reuses the same raw-filter
   syntax the header boxes take, and the same clearAllFilters seeding that
   makes `only` correct (the timeframe filter survives, grouping is
   stashed) rather than reimplementing either. */
async function filterByContains(column, text, { only = false, exclude = false } = {}) {
  const value = String(text == null ? '' : text);
  if (!value) return;
  if (/^\s|\s$/.test(value)) {
    toast('That selection starts or ends with whitespace — trim it and try again', 5000);
    return;
  }
  const raw = (exclude ? '!' : '') + value;
  const shown = ellipsize(value);
  if (only) {
    await clearAllFilters({ column, raw });
    toast(`Filtered ${column} to ${shown} · other filters cleared`);
    return;
  }
  setColumnFilter(column, raw);
  await rebuildView();
  toast(`Filtered ${column} ${exclude ? 'not ' : ''}containing ${shown}`);
}

async function searchForText(text) {
  const value = String(text || '').trim();
  if (!value) return;
  // 'contains' is the plain-substring mode (see #searchModeToggle) — a
  // selection lifted out of a document is literal text, so regex mode
  // would treat its dots and brackets as syntax.
  if (S.searchMode !== 'contains') await setSearchMode('contains');
  S.search = value;
  $('search').value = value;
  syncSearchExpansion(true);
  await rebuildView({ keepScroll: false });
  toast(`Searching for ${ellipsize(value)}`);
}

const saveNote = debounce(async () => {
  const note = $('noteInput');
  const rid = Number(note.dataset.rid);
  const sourceId = Number(note.dataset.sourceId);
  if (!rid) return;
  await post('/api/note', { source_id: sourceId, rid, note: note.value });
  const r = rowAt(S.cursor);
  if (r && r.rid === rid) r.note = note.value;
  $('noteStatus').textContent = 'Saved';
  render();
}, 500);

/* ------------------------------------------------------------- movement */

function moveCursor(to, extend) {
  const total = gridRowCount();
  if (!S.view || !total) return;
  to = Math.max(0, Math.min(total - 1, to));
  if (extend) {
    if (S.anchor < 0) S.anchor = S.cursor < 0 ? to : S.cursor;
    selSetRange(S.anchor, to);
  } else {
    S.anchor = to;
    selClear();
  }
  S.cursor = to;
  scrollIntoView(to);
  render();
  maybeShowDetail(to);
}

function scrollIntoView(pos) {
  const body = $('body');
  // Top-edge check needs no header term: the sticky header overlays
  // exactly the content space it occupies, so "row top clears the header"
  // is still pos*ROW_H >= scrollTop. The bottom edge does: the row's real
  // content y is headH() further down, and without it the target row
  // parks its last ~two-rows'-worth below the viewport.
  // Compared against the *virtual* offset, not raw scrollTop: top/bottom are
  // row-space pixels, and scrollTop stops being row-space once the spacer is
  // capped (MAX_SPACER_PX).
  const total = gridRowCount();
  const head = headH();
  const cur = vScroll(body, total, head);
  const top = pos * ROW_H;
  const bottom = top + ROW_H + head;
  if (top < cur) body.scrollTop = rScroll(body, total, top, head);
  else if (bottom > cur + body.clientHeight) body.scrollTop = rScroll(body, total, bottom - body.clientHeight, head);
}

/* ---------------------------------------------------------------- modal */

function modal(title, build, opts = {}) {
  $('modalTitle').textContent = title;
  document.querySelector('.modal-card').classList.toggle('wide', !!opts.wide);
  const b = $('modalBody');
  b.replaceChildren();
  // Any modal opening supersedes the Search-all pane's repaint hook; the
  // search-all builder re-installs its own below. (The background job keeps
  // running either way — only the painting stops.)
  searchAllRepaint = () => {};
  build(b);
  $('modal').hidden = false;
}
$('modalClose').onclick = () => ($('modal').hidden = true);
$('modal').onclick = (e) => { if (e.target === $('modal')) $('modal').hidden = true; };

/* --------------------------------------------------------- confirm/prompt */

/* Replacements for window.confirm()/window.prompt() — native browser
   dialogs can't be restyled and look jarring next to the rest of the app.
   Both build their own overlay (not the #modal singleton) so they can be
   triggered from a click handler inside an already-open modal and stack
   visibly above it, then resolve a Promise once the user answers instead
   of blocking synchronously like the native versions do. */
function _spawnDialog(build) {
  return new Promise((resolve) => {
    const overlay = el('div', 'confirm-overlay');
    const card = el('div', 'confirm-card');
    let settled = false;
    function close(result) {
      if (settled) return;
      settled = true;
      overlay.remove();
      document.removeEventListener('keydown', onKey, true);
      resolve(result);
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(build.cancelValue); }
    }
    overlay.onclick = (e) => { if (e.target === overlay) close(build.cancelValue); };
    build(card, close);
    overlay.append(card);
    document.body.append(overlay);
    document.addEventListener('keydown', onKey, true);
  });
}

function confirmDialog(message, opts = {}) {
  const build = (card, close) => {
    card.append(el('p', 'confirm-message', message));
    const acts = el('div', 'confirm-actions');
    const cancelBtn = el('button', 'btn ghost', opts.cancelLabel || 'Cancel');
    cancelBtn.onclick = () => close(false);
    const okBtn = el('button', 'btn' + (opts.danger ? ' danger' : ''), opts.okLabel || 'OK');
    okBtn.onclick = () => close(true);
    acts.append(cancelBtn, okBtn);
    card.append(acts);
    setTimeout(() => okBtn.focus(), 0);
  };
  build.cancelValue = false;
  return _spawnDialog(build);
}

function promptDialog(message, defaultValue = '', opts = {}) {
  const build = (card, close) => {
    card.append(el('p', 'confirm-message', message));
    const input = el('input');
    input.className = 'confirm-input';
    input.type = 'text';
    input.value = defaultValue || '';
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); close(input.value); } };
    card.append(input);
    const acts = el('div', 'confirm-actions');
    const cancelBtn = el('button', 'btn ghost', 'Cancel');
    cancelBtn.onclick = () => close(null);
    const okBtn = el('button', 'btn', opts.okLabel || 'OK');
    okBtn.onclick = () => close(input.value);
    acts.append(cancelBtn, okBtn);
    card.append(acts);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  };
  build.cancelValue = null;
  return _spawnDialog(build);
}

/* ------------------------------------------------------------ dropdown menu */

/* Minimal floating menu — one instance ever open at a time. Closes on
   outside click, Escape, or an item's own click (items are expected to
   open a modal/do their thing and don't need to close it themselves).

   Three surfaces share this machinery, differing only in what they're
   positioned against and what they hold: `dropdownMenu` (under a button —
   the Session menu, a column's ▾), `contextMenu` (at the pointer — the
   right-click menus on a row, a tab, a sidebar row) and `anchoredPanel` (a
   card with real controls in it — the header value picker). One
   implementation is what makes "only one of these is ever open, and Escape
   always closes it" true across all three instead of three near-copies of
   the same two listeners. */
let openMenuEl = null;
let openMenuAnchor = null;
function closeMenu() {
  if (openMenuAnchor) openMenuAnchor.setAttribute('aria-expanded', 'false');
  if (openMenuEl) openMenuEl.remove();
  openMenuEl = null;
  openMenuAnchor = null;
  document.removeEventListener('mousedown', onMenuOutsideClick, true);
  document.removeEventListener('keydown', onMenuKeydown, true);
}
function onMenuOutsideClick(e) { if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu(); }
/* Swallowed rather than left to bubble: the document-level Escape handler
   further down clears the row selection, and dismissing a menu you just
   opened shouldn't also throw away what was selected underneath it. */
function onMenuKeydown(e) {
  if (e.key !== 'Escape' || !openMenuEl) return;
  e.preventDefault();
  e.stopPropagation();
  closeMenu();
}

/* One item is {label, onclick} plus any of: `disabled`, `title`, `checked`
   (renders a ✓ column — pass false for "checkable but off", omit entirely
   for a plain item), `swatch` (a color chip, for tags), `hint` (right-aligned
   dim text, e.g. a hotkey) and `keepOpen` (re-render the menu in place
   instead of closing it, so toggling three tags is three clicks). '-' is a
   separator and {header} a section label. */
function menuItemNode(item, rerender) {
  // menu-item-flex, not plain .menu-item: the ✓/swatch/hint slots need a
  // flex row, while the sidebar's own hand-built .menu-item rows (a bare
  // text node inside the button) still rely on block-level ellipsing.
  const b = el('button', 'menu-item menu-item-flex');
  if (item.checked !== undefined) b.append(el('span', 'menu-check', item.checked ? '✓' : ''));
  if (item.swatch) {
    const sw = el('span', 'menu-swatch');
    sw.style.background = item.swatch;
    b.append(sw);
  }
  b.append(el('span', 'menu-item-text', item.label));
  if (item.hint) b.append(el('span', 'menu-item-hint', item.hint));
  b.disabled = !!item.disabled;
  if (item.title) b.title = item.title;
  b.onclick = async () => {
    if (!item.keepOpen) { closeMenu(); item.onclick(); return; }
    await item.onclick();
    rerender();
  };
  return b;
}

function fillMenuNode(menu, items, rerender) {
  menu.replaceChildren();
  for (const item of items) {
    if (!item) continue;
    if (item === '-') { menu.append(el('div', 'menu-sep')); continue; }
    if (item.header) {
      menu.append(el('div', 'menu-header' + (item.literal ? ' menu-header-literal' : ''), item.header));
      continue;
    }
    menu.append(menuItemNode(item, rerender));
  }
}

/* Positions a floating node against a rect — a button's own bounding box,
   or a zero-size rect at the pointer. Flips above the rect when there isn't
   room below (right-clicking a row near the bottom of the grid is the
   common case, not the edge case) and clamps into the viewport on both
   axes. Measured after the node is in the DOM, so callers append first. */
function placeFloating(node, rect) {
  const m = 8;
  const w = node.offsetWidth, h = node.offsetHeight;
  let top = rect.bottom + 4;
  if (top + h > window.innerHeight - m) {
    const above = rect.top - h - 4;
    top = above >= m ? above : Math.max(m, window.innerHeight - h - m);
  }
  let left = rect.left;
  if (left + w > window.innerWidth - m) left = rect.right - w;
  node.style.top = Math.max(m, top) + 'px';
  node.style.left = Math.max(m, left) + 'px';
}

function showFloating(node, rect, anchorEl) {
  document.body.append(node);
  placeFloating(node, rect);
  openMenuEl = node;
  openMenuAnchor = anchorEl || null;
  if (anchorEl) anchorEl.setAttribute('aria-expanded', 'true');
  setTimeout(() => {
    document.addEventListener('mousedown', onMenuOutsideClick, true);
    document.addEventListener('keydown', onMenuKeydown, true);
  }, 0);
}

/* `items` may be a function returning the array — that's what a keepOpen
   item re-runs to repaint itself with fresh state (a tag's ✓ after the
   tag actually landed), so callers build items from live state rather than
   patching DOM nodes by hand. */
function showMenu(items, rect, anchorEl) {
  const get = typeof items === 'function' ? items : () => items;
  const menu = el('div', 'menu');
  const rerender = () => { if (openMenuEl === menu) fillMenuNode(menu, get(), rerender); };
  fillMenuNode(menu, get(), rerender);
  showFloating(menu, rect, anchorEl);
  return menu;
}

function dropdownMenu(anchorEl, items) {
  const wasOpenForSameAnchor = openMenuAnchor === anchorEl;
  closeMenu();
  if (wasOpenForSameAnchor) return; // second click on the same anchor just toggles it shut
  showMenu(items, anchorEl.getBoundingClientRect(), anchorEl);
}

/* Right-click menus. `e` is the contextmenu event — its client coords are
   the anchor, and preventDefault() is the caller's job (some surfaces want
   the browser's own menu when the click misses anything actionable). */
function contextMenu(e, items) {
  closeMenu();
  showMenu(items, { top: e.clientY, bottom: e.clientY, left: e.clientX, right: e.clientX });
}

/* A floating card that isn't a list of buttons — same one-at-a-time,
   outside-click and Escape behaviour, arbitrary contents. `build` gets the
   panel node and the close function. */
function anchoredPanel(anchorEl, cls, build) {
  const wasOpenForSameAnchor = openMenuAnchor === anchorEl;
  closeMenu();
  if (wasOpenForSameAnchor) return null;
  const panel = el('div', 'menu ' + cls);
  build(panel, closeMenu);
  showFloating(panel, anchorEl.getBoundingClientRect(), anchorEl);
  return panel;
}

/* --------------------------------------------------------- filter builder */

/* Guided AND/OR condition tree, additive to the per-column quick filters.
   Tree shape: {type:'group', op:'AND'|'OR', children:[...]}
             | {type:'cond', column, op, value}   -- same op vocabulary as parseFilter
             | {type:'raw', sql}                  -- fallback when the SQL box can't round-trip
   The tree is compiled server-side by Store._compile_tree; 'raw' nodes are
   re-validated by Store.validate_where_fragment on every use, not just here. */

const OP_LABELS = {
  contains: 'contains', not_contains: 'does not contain',
  equals: 'equals', not_equals: 'does not equal',
  starts: 'starts with', regex: 'matches regex',
  '>': '> (numeric)', '>=': '>= (numeric)', '<': '< (numeric)', '<=': '<= (numeric)',
  empty: 'is empty', not_empty: 'is not empty', in: 'is any of (one per line)',
};
const OP_NO_VALUE = new Set(['empty', 'not_empty']);

function sqlLit(v) { return "'" + String(v).replace(/'/g, "''") + "'"; }
function sqlIdent(c) { return '"' + String(c).replace(/"/g, '""') + '"'; }

function serializeCond(node) {
  const c = sqlIdent(node.column);
  const v = node.value;
  switch (node.op) {
    case 'contains': return `${c} LIKE ${sqlLit('%' + v + '%')}`;
    case 'not_contains': return `(${c} NOT LIKE ${sqlLit('%' + v + '%')} OR ${c} IS NULL)`;
    case 'equals': return `${c} = ${sqlLit(v)}`;
    case 'not_equals': return `(${c} <> ${sqlLit(v)} OR ${c} IS NULL)`;
    case 'starts': return `${c} LIKE ${sqlLit(v + '%')}`;
    case 'regex': return `${c} REGEXP ${sqlLit(v)}`;
    case '>': case '>=': case '<': case '<=': return `${c} ${node.op} ${sqlLit(v)}`;
    case 'empty': return `(${c} IS NULL OR ${c} = '')`;
    case 'not_empty': return `(${c} IS NOT NULL AND ${c} <> '')`;
    case 'in': {
      const items = Array.isArray(v) ? v : String(v || '').split('\n').map((x) => x.trim()).filter(Boolean);
      return items.length ? `${c} IN (${items.map(sqlLit).join(', ')})` : '1';
    }
    default: return '1';
  }
}

function serializeTree(node) {
  if (!node) return '';
  if (node.type === 'raw') return node.sql || '';
  if (node.type === 'cond') return node.column ? serializeCond(node) : '';
  if (node.type === 'group') {
    const parts = (node.children || []).map(serializeTree).filter(Boolean);
    if (!parts.length) return '';
    if (parts.length === 1) return parts[0];
    return '(' + parts.join(node.op === 'OR' ? ' OR ' : ' AND ') + ')';
  }
  return '';
}

function tokenizeWhere(s) {
  const re = /"(?:[^"]|"")*"|'(?:[^']|'')*'|<>|>=|<=|[()<>=,]|\bAND\b|\bOR\b|\bIS\b|\bNOT\b|\bNULL\b|\bLIKE\b|\bREGEXP\b|\bIN\b|[A-Za-z_][A-Za-z0-9_]*/g;
  const toks = [];
  let last = 0, m;
  while ((m = re.exec(s))) {
    if (s.slice(last, m.index).trim() !== '') return null;
    toks.push(m[0]);
    last = m.index + m[0].length;
  }
  if (s.slice(last).trim() !== '') return null;
  return toks.length ? toks : null;
}

const unquoteIdent = (t) => (t[0] === '"' ? t.slice(1, -1).replace(/""/g, '"') : t);
const unquoteStr = (t) => t.slice(1, -1).replace(/''/g, "'");
const isStrLit = (t) => !!t && t[0] === "'";

/* Round-trips only the narrow subset serializeTree/serializeCond emit: the
   simple atomic ops (contains/starts/equals/regex/comparisons/in) plus
   AND/OR/paren grouping. The compound shapes for not_contains/not_equals/
   empty/not_empty — and anything else outside this subset — fall back to
   raw mode, which still works as a filter, it just won't populate the
   structured editor. */
function parseWhereFragment(text) {
  const toks = tokenizeWhere(text.trim());
  if (!toks) return null;
  let pos = 0;
  const peek = () => toks[pos];

  function parseCond() {
    const colTok = toks[pos];
    if (!colTok || !/^[A-Za-z_"]/.test(colTok)) return null;
    const column = unquoteIdent(colTok);
    const op = toks[pos + 1];
    if (op === 'LIKE') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      const val = unquoteStr(lit);
      pos += 3;
      if (val.startsWith('%') && val.endsWith('%') && val.length >= 2) return { type: 'cond', column, op: 'contains', value: val.slice(1, -1) };
      if (!val.startsWith('%') && val.endsWith('%')) return { type: 'cond', column, op: 'starts', value: val.slice(0, -1) };
      return { type: 'cond', column, op: 'equals', value: val };
    }
    if (op === 'REGEXP') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      pos += 3;
      return { type: 'cond', column, op: 'regex', value: unquoteStr(lit) };
    }
    if (op === '=' || op === '>' || op === '>=' || op === '<' || op === '<=') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      pos += 3;
      return { type: 'cond', column, op: op === '=' ? 'equals' : op, value: unquoteStr(lit) };
    }
    if (op === 'IN') {
      if (toks[pos + 2] !== '(') return null;
      let p = pos + 3;
      const items = [];
      while (toks[p] && toks[p] !== ')') {
        if (isStrLit(toks[p])) items.push(unquoteStr(toks[p]));
        p++;
        if (toks[p] === ',') p++;
      }
      if (toks[p] !== ')') return null;
      pos = p + 1;
      return { type: 'cond', column, op: 'in', value: items };
    }
    return null;
  }

  function parseAtom() {
    if (peek() === '(') {
      pos++;
      const inner = parseOr();
      if (!inner || peek() !== ')') return null;
      pos++;
      return inner;
    }
    return parseCond();
  }
  function parseAnd() {
    const first = parseAtom();
    if (!first) return null;
    const children = [first];
    while (peek() === 'AND') { pos++; const n = parseAtom(); if (!n) return null; children.push(n); }
    return children.length === 1 ? children[0] : { type: 'group', op: 'AND', children };
  }
  function parseOr() {
    const first = parseAnd();
    if (!first) return null;
    const children = [first];
    while (peek() === 'OR') { pos++; const n = parseAnd(); if (!n) return null; children.push(n); }
    return children.length === 1 ? children[0] : { type: 'group', op: 'OR', children };
  }

  const tree = parseOr();
  if (!tree || pos !== toks.length) return null;
  return tree.type === 'group' ? tree : { type: 'group', op: 'AND', children: [tree] };
}

function hasActiveFilterTree() {
  return S.filterTree.type === 'raw' ? !!(S.filterTree.sql || '').trim() : !!(S.filterTree.children || []).length;
}

function renderCondRow(node, onStructural, onPreview) {
  const row = el('div', 'fb-cond');
  if (!node.column && S.columns.length) node.column = S.columns[0].name;

  const colSel = el('select');
  for (const c of S.columns) {
    const opt = document.createElement('option');
    opt.value = c.name; opt.textContent = c.name;
    if (node.column === c.name) opt.selected = true;
    colSel.append(opt);
  }
  colSel.onchange = () => { node.column = colSel.value; onStructural(); };
  row.append(colSel);

  const opSel = el('select');
  for (const [op, label] of Object.entries(OP_LABELS)) {
    const opt = document.createElement('option');
    opt.value = op; opt.textContent = label;
    if (node.op === op) opt.selected = true;
    opSel.append(opt);
  }
  opSel.onchange = () => { node.op = opSel.value; onStructural(); };
  row.append(opSel);

  if (!OP_NO_VALUE.has(node.op)) {
    const inp = el('input');
    inp.value = Array.isArray(node.value) ? node.value.join('\n') : (node.value || '');
    inp.placeholder = node.op === 'in' ? 'one per line' : 'value';
    inp.oninput = () => {
      node.value = node.op === 'in' ? inp.value.split('\n').map((x) => x.trim()).filter(Boolean) : inp.value;
      onPreview();
    };
    row.append(inp);
  }
  return row;
}

function renderFilterGroup(node, onStructural, onPreview, isRoot) {
  const wrap = el('div', 'fb-group');
  const head = el('div', 'fb-group-head');
  const opSel = el('select', 'fb-group-op');
  for (const o of ['AND', 'OR']) {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    if (node.op === o) opt.selected = true;
    opSel.append(opt);
  }
  opSel.onchange = () => { node.op = opSel.value; onStructural(); };
  head.append(el('span', 'fb-group-label', isRoot ? 'Match' : 'Group:'), opSel, el('span', 'fb-group-label', 'of:'));
  wrap.append(head);

  const list = el('div', 'fb-children');
  (node.children || []).forEach((child, i) => {
    const row = el('div', 'fb-row');
    row.append(child.type === 'group'
      ? renderFilterGroup(child, onStructural, onPreview, false)
      : renderCondRow(child, onStructural, onPreview));
    const rm = el('button', 'btn ghost fb-rm', '✕');
    rm.title = 'Remove';
    rm.onclick = () => { node.children.splice(i, 1); onStructural(); };
    row.append(rm);
    list.append(row);
  });
  wrap.append(list);

  const actions = el('div', 'fb-actions');
  const addCond = el('button', 'btn ghost', '+ condition');
  addCond.onclick = () => {
    node.children = node.children || [];
    node.children.push({ type: 'cond', column: S.columns[0] ? S.columns[0].name : '', op: 'contains', value: '' });
    onStructural();
  };
  const addGroup = el('button', 'btn ghost', '+ group');
  addGroup.onclick = () => {
    node.children = node.children || [];
    node.children.push({ type: 'group', op: 'AND', children: [] });
    onStructural();
  };
  actions.append(addCond, addGroup);
  wrap.append(actions);
  return wrap;
}

/* `editing` is a saved-filter record when this was opened from the Saved
   filters modal's Edit button (which applied that filter to the grid
   first, so the tree/sort/search below is already its payload and the row
   count behind the modal is real feedback). It only adds an "Update
   <name>" action that writes the current state back over that record —
   everything else, including "Save filter…" as a save-as-new escape
   hatch, behaves identically to a normal open. */
function openFilterBuilder(editing = null) {
  modal(editing ? `Edit filter — ${editing.name}` : 'Filter builder', (b) => {
    const help = el('p', 'fb-help',
      'Build filters visually, or type/paste SQL directly below — edits sync both ways when the SQL is simple enough to parse back into the structured editor.');
    const treeContainer = el('div', 'fb-tree');
    const sqlLabel = el('div', 'fb-sql-label', 'Equivalent SQL (editable):');
    const sqlBox = el('textarea', 'fb-sql');
    sqlBox.rows = 3;
    sqlBox.spellcheck = false;
    const status = el('div', 'fb-status');

    function refreshPreview() {
      if (document.activeElement !== sqlBox) {
        sqlBox.value = S.filterTree.type === 'raw' ? (S.filterTree.sql || '') : serializeTree(S.filterTree);
      }
    }

    function rerenderTree() {
      treeContainer.replaceChildren();
      if (S.filterTree.type === 'raw') {
        treeContainer.append(el('div', 'fb-raw-banner',
          "Raw SQL mode — this expression doesn't match the structured editor's supported shape."));
        const startOver = el('button', 'btn ghost', 'Start over with the guided editor');
        startOver.onclick = () => { S.filterTree = { type: 'group', op: 'AND', children: [] }; rerenderTree(); };
        treeContainer.append(startOver);
      } else {
        treeContainer.append(renderFilterGroup(S.filterTree, rerenderTree, refreshPreview, true));
      }
      refreshPreview();
    }

    const validateLive = debounce((text) => {
      if (S.filterTree.type !== 'raw' || !text.trim()) { status.textContent = ''; status.className = 'fb-status'; return; }
      post('/api/filter/validate', { source_id: S.sourceId, fragment: text })
        .then((res) => {
          status.textContent = res.ok ? '✓ valid' : '✗ ' + res.error;
          status.className = 'fb-status ' + (res.ok ? 'ok' : 'err');
        })
        .catch(() => {});
    }, 400);

    sqlBox.oninput = () => {
      const text = sqlBox.value;
      const parsed = text.trim() ? parseWhereFragment(text) : { type: 'group', op: 'AND', children: [] };
      S.filterTree = parsed || { type: 'raw', sql: text };
      rerenderTree();
      validateLive(text);
    };

    b.append(help, treeContainer, sqlLabel, sqlBox, status);

    const actions = el('div', 'row-actions');
    const apply = el('button', 'btn', 'Apply');
    apply.onclick = () => {
      const doApply = () => {
        $('modal').hidden = true;
        updateFiltersButton();
        rebuildView({ keepScroll: false });
      };
      if (S.filterTree.type === 'raw' && S.filterTree.sql.trim()) {
        post('/api/filter/validate', { source_id: S.sourceId, fragment: S.filterTree.sql }).then((res) => {
          if (!res.ok) { status.textContent = '✗ ' + res.error; status.className = 'fb-status err'; return; }
          doApply();
        });
      } else doApply();
    };
    const clear = el('button', 'btn ghost', 'Clear');
    clear.onclick = () => { S.filterTree = { type: 'group', op: 'AND', children: [] }; rerenderTree(); };
    const saveFilterAs = el('button', 'btn ghost', editing ? 'Save as new…' : 'Save filter…');
    saveFilterAs.title = `Saves for these ${S.columns.length} columns — cyclable with `
      + `${S.keymap.cyclePrevFilter[0] || '['} / ${S.keymap.cycleNextFilter[0] || ']'}, and suggested `
      + `automatically next time you open a table with matching columns`;
    saveFilterAs.onclick = async () => {
      if (!hasActiveFilterTree()) { toast('Build a filter first'); return; }
      const name = await promptDialog('Filter name:');
      if (!name || !name.trim()) return;
      const rec = await post('/api/saved_filters', { name: name.trim(), col_names: baseColumns().map((c) => c.name), payload: currentFilterPayload() });
      S.savedFilters.push(rec);
      updateFiltersButton();
      checkPresets(S.sourceId); // this filter may now match the open table's banner
      toast(`Saved filter "${name.trim()}"`);
    };
    actions.append(apply, clear, saveFilterAs);

    if (editing) {
      // Deliberately doesn't send col_names: the header set is the filter's
      // identity for [ / ] cycling and the suggested-filter banner, so an
      // edit keeps it bound to the set it was saved for even if the table
      // open right now has a different one. "Save as new…" is the rebind path.
      const update = el('button', 'btn', `Update "${editing.name}"`);
      update.title = 'Overwrite this saved filter with the conditions above';
      update.onclick = async () => {
        const payload = currentFilterPayload();
        try {
          const rec = await api(`/api/saved_filters/${editing.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }),
          });
          const i = S.savedFilters.findIndex((f) => f.id === editing.id);
          if (i !== -1) S.savedFilters[i] = rec;
          $('modal').hidden = true;
          updateFiltersButton();
          if (S.sourceId) checkPresets(S.sourceId);
          toast(`Updated filter "${editing.name}"`);
        } catch (e) {
          toast('Could not update filter: ' + e.message);
        }
      };
      actions.append(update);
    }

    b.append(actions);

    rerenderTree();
  }, { wide: true });
}

/* ---------------------------------------------------------- header nicknames */

/* A friendly name for a *set* of headers (e.g. "EVTX exports" instead of a
   long raw column list) — cross-case, workspace-level, keyed by the header
   set itself (order/case-independent), same convention as ColumnLayouts.
   Several saved filters commonly share one header set and so share one
   nickname; there's no per-filter nickname field. */
function headerSig(colNames) {
  return (colNames || []).map((c) => c.trim().toLowerCase()).sort().join('\x1f');
}
function nicknameFor(colNames) {
  const sig = headerSig(colNames);
  const rec = S.headerNicknames.find((n) => headerSig(n.col_names) === sig);
  return rec ? rec.nickname : null;
}
async function loadHeaderNicknames() {
  try { S.headerNicknames = await api('/api/header_nicknames'); } catch { S.headerNicknames = []; }
}
async function setNicknameFor(colNames, currentName) {
  const name = await promptDialog('Nickname for this header set (blank to clear):', currentName || '');
  if (name == null) return; // cancelled
  const sig = headerSig(colNames);
  const existing = S.headerNicknames.find((n) => headerSig(n.col_names) === sig);
  if (!name.trim()) {
    if (existing) { await api(`/api/header_nicknames/${existing.id}`, { method: 'DELETE' }); }
    S.headerNicknames = S.headerNicknames.filter((n) => headerSig(n.col_names) !== sig);
    return null;
  }
  const rec = await post('/api/header_nicknames', { col_names: colNames, nickname: name.trim() });
  S.headerNicknames = S.headerNicknames.filter((n) => headerSig(n.col_names) !== sig);
  S.headerNicknames.push(rec);
  return rec;
}

/* ------------------------------------------------------------- presets */

/* A "preset" isn't a separate stored thing — it's just a saved filter
   whose col_names happens to match (exactly, or closely enough to be
   worth surfacing as "similar") the table that was just opened. Computed
   entirely against the already-loaded S.savedFilters, no request needed. */

const S_dismissedPresetSources = new Set();

function matchingSavedFilters(colNames) {
  const target = new Set(colNames.map((c) => c.trim().toLowerCase()));
  const exact = [];
  const similar = [];
  for (const f of S.savedFilters) {
    const cols = new Set((f.col_names || []).map((c) => c.trim().toLowerCase()));
    if (!cols.size) continue;
    if (cols.size === target.size && [...cols].every((c) => target.has(c))) { exact.push(f); continue; }
    const union = new Set([...cols, ...target]);
    const overlap = union.size ? [...cols].filter((c) => target.has(c)).length / union.size : 0;
    const isSubset = [...cols].every((c) => target.has(c)) || [...target].every((c) => cols.has(c));
    if (overlap >= 0.7 || isSubset) similar.push(f);
  }
  return { exact, similar };
}

function checkPresets(sourceId) {
  const banner = $('presetBanner');
  if (S_dismissedPresetSources.has(sourceId)) { banner.hidden = true; return; }
  const src = S.sources.find((s) => s.id === sourceId);
  if (!src) return;
  renderPresetBanner(matchingSavedFilters(src.columns.map((c) => c.name)), sourceId);
}

function renderPresetBanner(res, sourceId) {
  const banner = $('presetBanner');
  const exact = res.exact || [];
  const similar = res.similar || [];
  if (!exact.length && !similar.length) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.replaceChildren();
  banner.append(el('span', 'preset-banner-label', 'Saved filters for these columns:'));
  for (const p of exact) {
    const chip = el('button', 'tag-chip preset-chip', p.name);
    chip.title = `Apply "${p.name}" (exact column match)`;
    chip.onclick = () => applyPreset(p);
    banner.append(chip);
  }
  for (const p of similar) {
    const chip = el('button', 'tag-chip preset-chip preset-chip-fuzzy', `${p.name} (similar)`);
    const presetCols = new Set(p.col_names.map((c) => c.toLowerCase()));
    const curCols = new Set(S.columns.map((c) => c.name.toLowerCase()));
    const missing = [...curCols].filter((c) => !presetCols.has(c));
    const extra = [...presetCols].filter((c) => !curCols.has(c));
    chip.title = `Columns differ — missing: ${missing.join(', ') || 'none'}; extra: ${extra.join(', ') || 'none'}. Click to apply anyway.`;
    chip.onclick = async () => {
      if (await confirmDialog(`"${p.name}" was built for a different column set (${chip.title}). Apply anyway?`)) applyPreset(p);
    };
    banner.append(chip);
  }
  const dismiss = el('button', 'btn ghost preset-dismiss', '✕');
  dismiss.title = 'Dismiss for this source';
  dismiss.onclick = () => { S_dismissedPresetSources.add(sourceId); banner.hidden = true; };
  banner.append(dismiss);
}

function applyPreset(preset) {
  // Deliberately doesn't touch S.timeRange — applying a saved filter/
  // preset must not remove an active timeframe filter (see toggleTimeRange).
  const p = preset.payload || {};
  S.filterTree = p.filter_tree || { type: 'group', op: 'AND', children: [] };
  S.sort = p.sort || S.sort;
  S.search = p.search || '';
  S.searchMode = p.search_mode || 'contains';
  S.searchTerms = p.search_terms || [];
  $('search').value = S.searchMode === 'advanced' ? '' : S.search;
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === S.searchMode)));
  if (S.searchMode === 'advanced') renderAdvancedChips();
  syncSearchExpansion();
  updateSearchHint();
  updateFiltersButton();
  if (p.group_by && p.group_by.length) setGrouping(p.group_by, p.group_sort, p.group_sort_dir);
  renderHead();
  rebuildView({ keepScroll: false });  // regroups via regroupAll when grouping is set
  toast(`Applied preset "${preset.name}"`);
}

/* ------------------------------------------------------------ saved filters */

/* Cross-case, human-readable (workspace/filters.json), cyclable with [ and ].
   Distinct from filter_presets above (which lives in the case db and is
   scoped/matched there) but shares the exact same payload shape, so
   applyPreset() works unchanged for a saved-filter record too. */

async function loadSavedFilters() {
  try { S.savedFilters = await api('/api/saved_filters'); } catch { S.savedFilters = []; }
}

async function loadAppSettings() {
  try { S.appSettings = await api('/api/settings/app'); } catch { S.appSettings = {}; }
}

async function loadCaseSettings() {
  try { S.caseSettings = await api('/api/case_settings'); } catch { S.caseSettings = {}; }
}

function filtersForCurrentSource() {
  const cur = new Set(baseColumns().map((c) => c.name.trim().toLowerCase()));
  return S.savedFilters.filter((f) => {
    const cols = new Set((f.col_names || []).map((c) => c.trim().toLowerCase()));
    return cols.size === cur.size && [...cols].every((c) => cur.has(c));
  });
}

/* -1 is "no filter" — its own stop in the cycle, not just a resting value
   before the first real one. Cycling forward from the last saved filter,
   or backward from the first, lands there and drops the filters entirely
   (clearAllFilters) rather than wrapping straight to the opposite end, so
   "keep pressing [ " has a clean bottom instead of silently looping. */
function cycleSavedFilter(dir) {
  if (!S.sourceId) return;
  const list = filtersForCurrentSource();
  if (!list.length) { toast('No saved filters for these columns'); return; }
  S.savedFilterCursor += dir;
  if (S.savedFilterCursor >= list.length) S.savedFilterCursor = -1;
  if (S.savedFilterCursor < -1) S.savedFilterCursor = list.length - 1;
  if (S.savedFilterCursor === -1) {
    clearAllFilters();
    toast('Filters cleared');
  } else {
    applyPreset(list[S.savedFilterCursor]);
  }
}

/* The grid's current filter/sort/search, rendered server-side as a
   standalone SELECT (spec_sql — the same compiler the view build uses) and
   dropped into a new SQL pane tab. For when a filter has done all it can
   and the next question needs a JOIN or a GROUP BY. */
async function openFilterSqlTab() {
  if (!S.sourceId) { toast('Open a table first'); return; }
  try {
    const res = await post('/api/view/sql', currentSpec());
    const src = S.sources.find((x) => x.id === S.sourceId);
    const rec = await post('/api/sql_tabs', { name: `Filter: ${src ? src.name : 'table'}`, sql: res.sql });
    S.sqlTabId = rec.id;  // loadSqlTabs (via showSqlTab) keeps a still-valid selection
    showSqlTab();
  } catch (e) {
    toast('Could not open in SQL pane: ' + e.message, 5000);
  }
}

/* ------------------------------------------------------ jump to timestamp */

/* Scroll to the row whose timestamp is closest to a given moment. The
   target is saved in S.jumpTs and deliberately survives switching tables —
   the workflow this exists for is "something happened at 13:22:01; show me
   that moment in the EVTX table, now in the proxy log, now in the MFT". */
function openJumpTsModal() {
  if (!S.sourceId) { toast('Open a table first'); return; }
  const dtCols = S.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
  modal('Jump to timestamp', (b) => {
    const jumpKey = (S.keymap.repeatJumpTs || [])[0] || '(unbound)';
    b.append(el('p', null,
      'Scrolls to the row whose timestamp is closest to this moment. The value is remembered '
      + `across tables — press "${jumpKey}" in any table to jump straight to it again.`));

    b.append(el('label', null, 'Column'));
    const colSel = el('select');
    colSel.style.cssText = 'display:block;width:100%;background:var(--ink);color:var(--text);'
      + 'border:1px solid var(--line-2);padding:6px 8px;font:inherit;margin-bottom:10px';
    const allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = 'Nearest across all datetime columns';
    colSel.append(allOpt);
    for (const name of dtCols) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      colSel.append(opt);
    }
    colSel.value = dtCols.includes(S.jumpTs.column) ? S.jumpTs.column : '';
    b.append(colSel);
    if (!dtCols.length) b.append(el('p', 'fb-help', 'This table has no datetime columns — the jump will have nothing to measure against here.'));

    const row = el('div', 'row-actions');
    const input = el('input');
    input.type = 'text';
    input.placeholder = 'YYYY-MM-DD HH:MM:SS';
    input.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'padding:6px 8px;font:inherit;font-family:var(--mono)';
    input.value = S.jumpTs.value || '';
    row.append(el('span', null, 'Moment'), input);
    b.append(row);
    b.append(el('p', 'fb-help', '24-hour time — the date alone, or date plus HH:MM, also work.'));

    const go = () => {
      const v = input.value.trim();
      if (!v) { toast('Enter a timestamp'); return; }
      if (!parseTimestamp(v)) { toast('Not a recognized timestamp — try YYYY-MM-DD HH:MM:SS', 4000); return; }
      S.jumpTs = { value: v, column: colSel.value || null };
      $('modal').hidden = true;
      doJumpTs();
    };
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } };
    const actions = el('div', 'row-actions');
    const jumpBtn = el('button', 'btn', 'Jump');
    jumpBtn.onclick = go;
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(jumpBtn, cancel);
    b.append(actions);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

async function doJumpTs() {
  if (!S.jumpTs.value) { openJumpTsModal(); return; }
  if (!S.view || !S.sourceId) { toast('Open a table first'); return; }
  if (S.groupByCols.length) { toast('Jump works in the flat view — toggle grouping off first'); return; }
  // The saved column may not exist in this table — fall back to
  // nearest-across-all rather than erroring on a per-table mismatch.
  const col = S.jumpTs.column && S.columns.some((c) => c.name === S.jumpTs.column && c.type === 'datetime')
    ? S.jumpTs.column : null;
  try {
    const res = await post('/api/view/find_ts', { view_id: S.view.view_id, value: S.jumpTs.value, column: col });
    moveCursor(res.pos, false);
    toast(`Jumped to ${res.ts} (row ${(res.pos + 1).toLocaleString()})`);
  } catch (e) {
    toast((e.status === 404 ? e.message : 'Could not jump: ' + e.message), 4000);
  }
}

/* Same shape saveFilterAs/saveAs POST as a saved filter/preset's payload. */
function currentFilterPayload() {
  const p = { filter_tree: S.filterTree, sort: S.sort, search: S.search, search_mode: S.searchMode, search_terms: S.searchTerms };
  // Grouping rides along only when one is active — a filter saved without
  // grouping omits the key entirely, and applying it leaves any current
  // grouping alone (same leniency `sort: p.sort || S.sort` has).
  if (S.groupByCols.length) {
    p.group_by = [...S.groupByCols];
    p.group_sort = S.groupSort;
    p.group_sort_dir = S.groupSortDir;
  }
  return p;
}

/* No separate "which saved filter is active" flag to keep in sync — instead
   this re-derives it every time the view changes by comparing the live
   filter/sort/search state against each saved filter's stored payload.
   Applying one makes S.filterTree etc. literally the response object back
   from the API, so it matches immediately; editing anything afterward means
   it naturally stops matching, without needing to invalidate a flag by hand. */
function activeSavedFilterRecord() {
  if (!S.sourceId) return null;
  const cur = JSON.stringify(currentFilterPayload());
  return filtersForCurrentSource().find((f) => JSON.stringify(f.payload || {}) === cur) || null;
}

/* Single merged button for Filter builder + Saved filters (dropdown menu
   below) — its label/pressed-state reflects whichever is more specific:
   an exactly-matching saved filter by name, else just "filter active" when
   the tree has content the analyst built by hand, else the plain label. */
function updateFiltersButton() {
  const btn = $('btnFilters');
  const f = activeSavedFilterRecord();
  if (f) {
    btn.textContent = `★ ${f.name} ▾`;
    btn.title = `Applying saved filter "${f.name}" — click to browse filters`;
    btn.setAttribute('aria-pressed', 'true');
  } else if (hasActiveFilterTree()) {
    btn.textContent = 'Filters ● ▾';
    btn.title = 'A custom filter is active — click to edit or browse saved filters';
    btn.setAttribute('aria-pressed', 'true');
  } else {
    btn.textContent = 'Filters ▾';
    btn.title = 'Filter builder and saved filters';
    btn.setAttribute('aria-pressed', 'false');
  }
}

/* ---------------------------------------------------------- timeframe filter */

/* A start/end range that stays applied across everything that resets the
   regular filters — clearAllFilters(), applyPreset(), switching tabs (see
   openSource() — deliberately not among the fields it resets). The MFT use
   case this exists for: pin a date range, then flip through per-column
   quick filters/presets/tables without having to re-set the range each
   time. Column choice is per-invocation, not persisted structure — "all
   datetime columns" (column: null) ORs every datetime column on whichever
   table is open, so a timestomped Created date doesn't hide a row whose
   Modified date is genuinely in range; _compile_where falls back to that
   same "all columns" behavior automatically if a specifically-chosen
   column doesn't exist on the table currently open. */

function datetimeColumns() {
  return S.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
}

function updateTimeRangeButton() {
  const btn = $('btnTimeRange');
  const hasRange = !!(S.timeRange.start || S.timeRange.end);
  const active = S.timeRange.enabled && hasRange;
  btn.setAttribute('aria-pressed', String(active));
  btn.textContent = active ? `⏱ ${S.timeRange.column || 'all columns'}` : '⏱ Timeframe';
  const toggleKey = S.keymap.toggleTimeRange[0] || '';
  const openKey = S.keymap.openTimeRange[0] || '';
  btn.title = active
    ? `Timeframe filter active (${S.timeRange.column || 'all datetime columns'}): `
      + `${S.timeRange.start || '…'} → ${S.timeRange.end || '…'} — "${toggleKey}" to toggle off, "${openKey}" to edit`
    : hasRange
      ? `Timeframe filter set but off — "${toggleKey}" to toggle on, "${openKey}" to edit`
      : `Set up a timeframe filter that survives filter/preset/table changes — "${openKey}" to open, "${toggleKey}" to toggle`;
}

function toggleTimeRange() {
  if (!S.timeRange.start && !S.timeRange.end) {
    toast('Set a timeframe first, from the clock button in the toolbar');
    openTimeRangeModal();
    return;
  }
  S.timeRange.enabled = !S.timeRange.enabled;
  updateTimeRangeButton();
  toast(S.timeRange.enabled ? 'Timeframe filter on' : 'Timeframe filter off');
  if (S.sourceId) rebuildView({ keepScroll: false });
}

function openTimeRangeModal() {
  modal('Timeframe filter', (b) => {
    const toggleKey = S.keymap.toggleTimeRange[0] || '(unbound)';
    const openKey = S.keymap.openTimeRange[0] || '(unbound)';
    b.append(el('p', null,
      `Stays applied across filter/preset changes and table switches, unlike the regular filters — `
      + `toggle it on/off quickly with "${toggleKey}", or jump straight back to this dialog with "${openKey}".`));

    const enabledLabel = el('label');
    const enabledCb = el('input');
    enabledCb.type = 'checkbox';
    enabledCb.checked = S.timeRange.enabled;
    enabledLabel.append(enabledCb, document.createTextNode(' Enabled'));
    b.append(enabledLabel);

    b.append(el('label', null, 'Column'));
    const colSel = el('select');
    colSel.style.cssText = 'display:block;width:100%;background:var(--ink);color:var(--text);'
      + 'border:1px solid var(--line-2);padding:6px 8px;font:inherit;margin-bottom:10px';
    const allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = 'All datetime columns (catches a match on any of them)';
    colSel.append(allOpt);
    const cols = datetimeColumns();
    for (const name of cols) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      colSel.append(opt);
    }
    colSel.value = cols.includes(S.timeRange.column) ? S.timeRange.column : '';
    b.append(colSel);
    if (!cols.length) b.append(el('p', 'fb-help', "This table has no datetime columns yet — the range below will simply match nothing here until it's opened on one that does."));

    // Plain text, not <input type="datetime-local"> — that widget's native
    // picker renders in the browser's locale/12-hour format depending on
    // the OS, which doesn't match what TS_NORMALIZE/_ts_normalize actually
    // parse (ISO 'YYYY-MM-DD HH:MM:SS' or the US M/D/YYYY shape) and gave no
    // way to just type a known timestamp in 24-hour time. ISO with a space
    // separator is TS_ISO_RE's own shape (it accepts 'T' too, for values
    // coming from elsewhere, but the field asks for the one the rest of the
    // app displays: formatTimestamp's 'iso' option, the timeline, exports).
    const inputStyle = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'padding:6px 8px;font:inherit;font-family:var(--mono)';

    const startRow = el('div', 'row-actions');
    const startInput = el('input');
    startInput.type = 'text';
    startInput.placeholder = 'YYYY-MM-DD HH:MM:SS';
    startInput.style.cssText = inputStyle;
    startInput.value = S.timeRange.start || '';
    startRow.append(el('span', null, 'Start'), startInput);
    b.append(startRow);

    const endRow = el('div', 'row-actions');
    const endInput = el('input');
    endInput.type = 'text';
    endInput.placeholder = 'YYYY-MM-DD HH:MM:SS';
    endInput.style.cssText = inputStyle;
    endInput.value = S.timeRange.end || '';
    endRow.append(el('span', null, 'End'), endInput);
    b.append(endRow);
    b.append(el('p', 'fb-help', '24-hour time, e.g. 2024-01-05 13:22:01 — the date alone, or date plus HH:MM, also work.'));

    // Free-text, so it's validated the same way a value has to be
    // recognizable to TS_NORMALIZE server-side to be usable at all —
    // parseTimestamp is the client-side twin of that same ISO/US shape
    // check (see its own comment). Rejecting here beats silently building
    // a filter that will never match anything.
    const validateTimeInput = (input, label) => {
      const v = input.value.trim();
      if (!v) return true;
      if (!parseTimestamp(v)) { toast(`${label}: not a recognized timestamp — try YYYY-MM-DD HH:MM:SS`, 4000); return false; }
      return true;
    };

    // Fill the range from what's already been triaged: earliest/latest
    // timestamp among tagged rows — any tag, or just the ones toggled on.
    if (S.sourceId && S.tags.length) {
      b.append(el('label', null, 'From tagged rows'));
      const tagRow = el('div', 'row-actions tr-tag-row');
      const selectedTags = new Set();
      for (const t of S.tags) {
        const chip = el('button', 'tag-chip');
        chip.setAttribute('aria-pressed', 'false');
        const sw = el('span', 'swatch');
        sw.style.background = t.color;
        chip.append(sw, el('span', null, t.name));
        chip.title = 'Toggle — no tags toggled means any tag counts';
        chip.onclick = () => {
          if (selectedTags.has(t.id)) selectedTags.delete(t.id); else selectedTags.add(t.id);
          chip.setAttribute('aria-pressed', String(selectedTags.has(t.id)));
          chip.style.color = selectedTags.has(t.id) ? t.color : '';
        };
        tagRow.append(chip);
      }
      const fillBtn = el('button', 'btn ghost', 'Fill range');
      fillBtn.title = 'Set start/end to the earliest and latest timestamps among tagged rows (respects the column chosen above)';
      fillBtn.onclick = async () => {
        try {
          const res = await post('/api/tag_time_bounds', {
            source_id: S.sourceId, tag_ids: [...selectedTags], column: colSel.value || null,
          });
          if (!res.start && !res.end) { toast('No tagged rows with a usable timestamp'); return; }
          startInput.value = res.start || '';
          endInput.value = res.end || '';
          enabledCb.checked = true;
          toast('Range set from tagged rows — Apply to use it');
        } catch (e) {
          toast('Could not read tagged range: ' + e.message, 4000);
        }
      };
      tagRow.append(fillBtn);
      b.append(tagRow);
    }

    const actions = el('div', 'row-actions');
    const apply = el('button', 'btn', 'Apply');
    apply.onclick = () => {
      if (!startInput.value.trim() && !endInput.value.trim()) { toast('Set a start and/or end'); return; }
      if (!validateTimeInput(startInput, 'Start') || !validateTimeInput(endInput, 'End')) return;
      S.timeRange = {
        enabled: enabledCb.checked,
        column: colSel.value || null,
        start: startInput.value.trim(),
        end: endInput.value.trim(),
      };
      updateTimeRangeButton();
      $('modal').hidden = true;
      if (S.sourceId) rebuildView({ keepScroll: false });
    };
    const clearBtn = el('button', 'btn ghost', 'Clear');
    clearBtn.onclick = () => {
      S.timeRange = { enabled: false, column: null, start: '', end: '' };
      updateTimeRangeButton();
      $('modal').hidden = true;
      if (S.sourceId) rebuildView({ keepScroll: false });
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(apply, clearBtn, cancel);
    b.append(actions);
  });
}

/* Every saved filter sharing f's exact header set, in current cycle/list
   order — the scope moveSavedFilter's up/down reordering operates within,
   so reordering one header set's filters never disturbs another's (same
   guarantee workspace.SavedFilters.reorder makes server-side). */
function sameGroupFilterIds(colNames) {
  const sig = headerSig(colNames);
  return S.savedFilters.filter((f) => headerSig(f.col_names) === sig).map((f) => f.id);
}

async function moveSavedFilter(f, dir) {
  const ids = sameGroupFilterIds(f.col_names);
  const idx = ids.indexOf(f.id);
  const swapIdx = idx + dir;
  if (swapIdx < 0 || swapIdx >= ids.length) return;
  [ids[idx], ids[swapIdx]] = [ids[swapIdx], ids[idx]];
  S.savedFilters = await post('/api/saved_filters/reorder', { ids });
}

function openSavedFiltersModal() {
  modal('Saved filters', (b) => {
    if (!S.savedFilters.length) {
      b.append(el('div', 'note-status', 'No saved filters yet. Build one in the Filter builder, then "Save filter…".'));
      return;
    }
    const search = el('input');
    search.type = 'search';
    search.placeholder = 'Search by name, nickname, or column…';
    search.autocomplete = 'off';
    search.style.cssText = 'width:100%;background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'border-radius:var(--radius-sm);padding:6px 9px;font:inherit;font-size:12px;margin-bottom:10px';
    b.append(search);

    const list = el('div', 'session-list');
    b.append(list);

    const curCols = new Set(baseColumns().map((c) => c.name.trim().toLowerCase()));
    const active = activeSavedFilterRecord();

    function render() {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      let shown = 0;
      for (const f of S.savedFilters) {
        const nickname = nicknameFor(f.col_names);
        const colText = (f.col_names || []).join(', ');
        if (q && !f.name.toLowerCase().includes(q) && !(nickname || '').toLowerCase().includes(q)
          && !colText.toLowerCase().includes(q)) continue;
        shown++;
        const cols = new Set((f.col_names || []).map((c) => c.trim().toLowerCase()));
        const matches = cols.size === curCols.size && [...cols].every((c) => curCols.has(c));
        const row = el('div', 'row-actions session-row');
        const applyBtn = el('button', 'btn' + (matches ? '' : ' ghost'), f.name);
        applyBtn.setAttribute('aria-pressed', String(!!(active && active.id === f.id)));
        applyBtn.title = matches
          ? `Apply "${f.name}"`
          : `Built for different columns (${colText}) — click to apply anyway`;
        applyBtn.onclick = async () => {
          if (!matches && !(await confirmDialog(`"${f.name}" was built for a different column set (${colText}). Apply anyway?`))) return;
          $('modal').hidden = true;
          applyPreset(f);
        };
        const headerLabel = el('span', 'count', nickname || colText);
        headerLabel.title = nickname ? colText : '';
        // Editing routes through the real grid: apply the filter, then open
        // the builder pre-loaded with its payload, so the match count behind
        // the modal is live feedback on the change being made. Needs a table
        // open to apply against — without one there's nothing to preview.
        const editBtn = el('button', 'btn ghost', 'Edit');
        editBtn.title = S.sourceId
          ? `Apply "${f.name}" to the open table and edit its conditions`
          : 'Open a table first — editing applies the filter so you can see what it matches';
        editBtn.disabled = !S.sourceId;
        editBtn.onclick = async () => {
          if (!matches && !(await confirmDialog(
            `"${f.name}" was built for a different column set (${colText}). Edit it against the open table anyway? `
            + `It stays saved for its original columns.`))) return;
          $('modal').hidden = true;
          applyPreset(f);
          openFilterBuilder(f);
        };
        const nicknameBtn = el('button', 'btn ghost', nickname ? '🏷' : '🏷 name…');
        nicknameBtn.title = nickname
          ? `Rename this header set's nickname (used by every saved filter with these columns: ${colText})`
          : `Give this header set a nickname instead of showing its raw columns (${colText})`;
        nicknameBtn.onclick = async () => {
          await setNicknameFor(f.col_names, nickname);
          render();
        };
        const groupIds = sameGroupFilterIds(f.col_names);
        const gIdx = groupIds.indexOf(f.id);
        const upBtn = el('button', 'btn ghost', '▲');
        upBtn.title = 'Move earlier in the cycle order for this header set';
        upBtn.disabled = gIdx <= 0;
        upBtn.onclick = async () => { await moveSavedFilter(f, -1); render(); };
        const downBtn = el('button', 'btn ghost', '▼');
        downBtn.title = 'Move later in the cycle order for this header set';
        downBtn.disabled = gIdx >= groupIds.length - 1;
        downBtn.onclick = async () => { await moveSavedFilter(f, 1); render(); };
        row.append(applyBtn, headerLabel, editBtn, nicknameBtn, upBtn, downBtn);
        // Drag to reorder as well — same one DnD implementation the tab
        // strip and SQL sub-tabs use. currentIds scopes the drop to this
        // filter's own header set, so dragging across sets is a no-op
        // (wireDragReorder's own from === -1 guard).
        wireDragReorder(row, f.id, {
          containerSelector: '.session-list',
          rowSelector: '.session-row',
          horizontal: false,
          currentIds: () => sameGroupFilterIds(f.col_names),
          onReorder: async (ids) => {
            S.savedFilters = await post('/api/saved_filters/reorder', { ids });
            render();
          },
        });
        list.append(row);
      }
      if (!shown) list.append(el('div', 'note-status', 'No saved filters match that search.'));
    }
    search.oninput = render;
    render();
    setTimeout(() => search.focus(), 0);

    b.append(el('p', null,
      `Cycle filters that match the open source's columns with `
      + `${S.keymap.cyclePrevFilter[0] || '['} / ${S.keymap.cycleNextFilter[0] || ']'} — cycling past either `
      + `end drops the filters entirely rather than wrapping. ▲/▼ set the cycle order within a header set. `
      + `"Edit" applies a filter to the open table and reopens it in the Filter builder, where "Update" `
      + `saves your changes back over it. Rename, delete, import or export them from Settings.`));
  });
}

/* `refresh` re-renders whatever surface is hosting the panel after a bulk
   action ("Show all"/"Hide empty") — the per-column checkboxes repaint the
   grid directly and don't need it. */
function buildColumnsPanel(container, refresh = openTableMenu) {
  if (!S.sourceId) {
    container.append(el('p', null, 'Open a table to manage its columns.'));
    return;
  }
  const list = el('div', 'collist');
  S.order.forEach((name) => {
    const row = el('div', 'collist-row');
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !(S.layout[name] || {}).hidden;
    cb.onchange = () => {
      S.layout[name] = { ...(S.layout[name] || {}), hidden: !cb.checked };
      renderHead(); render(); saveLayout();
    };
    lab.append(cb, el('span', null, name));
    const c = columnMeta(name);
    lab.append(el('span', 'count', ' ' + (c ? c.type : '') + (c && c.derived ? ' · derived' : '')));
    row.append(lab);
    // Per-column value-picker override, in the same row as the visibility
    // box because they're the same kind of decision about the same column.
    // Three states, not two: following the table's setting is distinct from
    // being explicitly on or off, and only an explicit choice survives the
    // table default changing later.
    const override = (S.layout[name] || {}).valuePicker;
    const state = override === undefined ? 'auto' : (override ? 'on' : 'off');
    const pick = el('button', 'btn ghost collist-pick', '▾ ' + state);
    pick.setAttribute('aria-pressed', String(valueFilterEnabled(name)));
    pick.title = state === 'auto'
      ? `Value dropdown follows this table's setting (currently ${valueFilterEnabled(name) ? 'on' : 'off'}) — click to pin it on`
      : `Value dropdown pinned ${state} for this column — click to cycle`;
    pick.onclick = () => {
      // auto → on → off → auto: all three states reachable in one direction,
      // including pinning a column on while the table default is off.
      setColumnValueFilter(name, state === 'auto' ? true : (state === 'on' ? false : null));
      refresh();
    };
    row.append(pick);
    list.append(row);
  });
  container.append(list);
  container.append(el('p', 'fb-help', 'Drag a column header in the grid to reorder it.'));
  const acts = el('div', 'row-actions');
  const addDerived = el('button', 'btn ghost', 'Add datetime column…');
  addDerived.onclick = () => openDerivedColumnModal();
  acts.append(addDerived);
  const all = el('button', 'btn ghost', 'Show all');
  all.onclick = () => { for (const n of S.order) S.layout[n] = { ...(S.layout[n] || {}), hidden: false }; renderHead(); render(); saveLayout(); refresh(); };
  const none = el('button', 'btn ghost', 'Hide empty columns');
  none.onclick = async () => {
    // Hide columns with no value in the first 2000 rows of the current view.
    const sample = await api(`/api/rows?view_id=${S.view.view_id}&start=0&count=2000`);
    S.columns.forEach((c, i) => {
      const empty = sample.rows.every((r) => r.cells[i] == null || r.cells[i] === '');
      if (empty) S.layout[c.name] = { ...(S.layout[c.name] || {}), hidden: true };
    });
    renderHead(); render(); saveLayout(); refresh();
  };
  acts.append(all, none);
  container.append(acts);
}

/* The table menu's value-filter section: the table-wide default the
   per-column overrides above fall back to. */
function buildValueFilterPanel(container, refresh) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const rows = src ? src.row_count : 0;
  container.append(el('p', null,
    'A ▾ button on each column filter box lists that column’s distinct values with counts, '
    + 'so you can tick the ones to filter to. Reading those values is a scan, which is why big '
    + 'tables start with it off.'));
  const seg = el('div', 'vp-seg');
  for (const [key, label, title] of [
    ['auto', `Auto (on under ${VALUE_FILTER_AUTO_MAX.toLocaleString()} rows)`,
      `This table has ${rows.toLocaleString()} rows — auto means ${valueFilterAutoOn() ? 'on' : 'off'} here`],
    ['on', 'On for every column', 'Show the picker on every column regardless of size'],
    ['off', 'Off', 'No picker buttons — the row menu’s "Filter by values…" still opens one'],
  ]) {
    const b = el('button', 'btn ghost', label);
    b.setAttribute('aria-pressed', String(S.valueFilterMode === key));
    b.title = title;
    b.onclick = () => { setValueFilterMode(key); refresh(); };
    seg.append(b);
  }
  container.append(seg);
  const pinned = S.order.filter((n) => (S.layout[n] || {}).valuePicker !== undefined);
  if (pinned.length) {
    const clear = el('button', 'btn ghost', `Clear ${pinned.length} per-column override${pinned.length > 1 ? 's' : ''}`);
    clear.style.marginTop = '10px';
    clear.onclick = () => { for (const n of pinned) setColumnValueFilter(n, null); refresh(); };
    container.append(clear);
  }
}

function buildTableActionsPanel(container, refresh) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const acts = el('div', 'row-actions');
  const dflt = el('button', 'btn ghost', 'Save layout as default for these columns');
  dflt.title = 'Reuse this column order/visibility for any table imported with the same headers';
  dflt.onclick = () => saveDefaultLayout();
  const nick = el('button', 'btn ghost', 'Name this header set…');
  nick.onclick = () => setNicknameFor(baseColumns().map((c) => c.name), nicknameFor(baseColumns().map((c) => c.name)));
  const nickTable = el('button', 'btn ghost', src && src.is_merge ? 'Rename this merge…' : 'Nickname this table…');
  nickTable.title = src && src.is_merge
    ? 'Rename this merge'
    : 'A display name shown in place of the file name — clear it to go back';
  nickTable.onclick = async () => {
    if (!src) return;
    if (await editSourceNickname(src)) refresh();
  };
  const tables = el('button', 'btn ghost', 'Tables manager…');
  tables.title = 'Every table in the case — indexes, row counts, dropping a source';
  tables.onclick = () => openTablesManager();
  acts.append(dflt, nick, nickTable, tables);
  container.append(acts);
  if (src && src.is_open) {
    const close = el('button', 'btn ghost', 'Close this tab');
    close.title = 'Stays in the case — reopen it from the sidebar';
    close.style.marginTop = '10px';
    close.onclick = async () => { $('modal').hidden = true; await closeTab(src); };
    container.append(close);
  }
}

/* The table menu — everything that's about *this table* rather than the
   case or the app. Sections are a registry for the same reason the row
   menu's are: this is where per-table features are expected to land, and
   adding one should be adding an entry. `build` gets (container, refresh)
   and may render nothing at all.

   It lives behind a right-click on the tab or the sidebar row (and the
   openTableMenu keybind) rather than a visible ▦ button, which is the icon
   this replaced — a menu that's going to keep growing needs a home that
   doesn't cost tab-strip width per entry. */
const TABLE_MENU_SECTIONS = [
  { id: 'columns', title: 'Columns', build: buildColumnsPanel },
  { id: 'valueFilters', title: 'Value filter dropdowns', build: buildValueFilterPanel },
  { id: 'table', title: 'This table', build: buildTableActionsPanel },
];

/* `sourceId` is optional — the tab/sidebar entry points pass the table that
   was right-clicked, which may not be the one on screen. The panels all
   read S.layout/S.order/S.columns (this table's live state), so opening
   that source first isn't a convenience, it's the precondition. */
async function openTableMenu(sourceId) {
  const id = sourceId === undefined ? S.sourceId : sourceId;
  if (id == null) { toast('Open a table first'); return; }
  if (id !== S.sourceId || S.activeTab !== 'grid') await openSource(id);
  if (S.sourceId !== id) return; // openSource no-ops on an id it can't find
  const src = S.sources.find((x) => x.id === id);
  modal(`Table — ${src ? sourceLabel(src) : ''}`, (b) => {
    for (const section of TABLE_MENU_SECTIONS) {
      const wrap = el('div', 'table-menu-section');
      wrap.append(el('h4', null, section.title));
      section.build(wrap, () => openTableMenu(id));
      b.append(wrap);
    }
  });
}

function openTagEditor() {
  modal('Tags', (b) => {
    for (const t of S.tags) {
      const row = el('div', 'row-actions');
      const color = el('input'); color.type = 'color'; color.value = t.color;
      const name = el('input'); name.value = t.name;
      name.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
      const key = el('input'); key.value = t.hotkey || ''; key.maxLength = 1;
      key.style.cssText = 'width:34px;text-align:center;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px;font-family:var(--mono)';
      const save = el('button', 'btn', 'Save');
      save.onclick = async () => {
        await post('/api/tags', { id: t.id, name: name.value, color: color.value, hotkey: key.value || null });
        await loadTags(); openTagEditor();
      };
      const del = el('button', 'btn ghost', 'Delete');
      del.onclick = async () => {
        if (!(await confirmDialog(`Delete "${t.name}" and remove it from every row?`, { danger: true, okLabel: 'Delete' }))) return;
        await api(`/api/tags/${t.id}`, { method: 'DELETE' });
        await loadTags(); render(); drawRail(); openTagEditor();
      };
      row.append(color, name, key, save, del);
      b.append(row);
    }
    const add = el('button', 'btn', 'Add tag');
    add.style.marginTop = '14px';
    add.onclick = async () => {
      await post('/api/tags', { name: 'New tag', color: '#7f9bb5', hotkey: null });
      await loadTags(); openTagEditor();
    };
    const applyTemplate = el('button', 'btn ghost', 'Apply default template');
    applyTemplate.style.marginTop = '14px';
    applyTemplate.title = "Add any tags from the default template (Settings) that this case doesn't already have";
    applyTemplate.onclick = async () => {
      const template = await api('/api/settings/default_tags');
      const existing = new Set(S.tags.map((t) => t.name.toLowerCase()));
      const missing = template.filter((t) => !existing.has((t.name || '').toLowerCase()));
      if (!missing.length) { toast('This case already has every tag in the default template'); return; }
      for (const t of missing) await post('/api/tags', { name: t.name, color: t.color, hotkey: t.hotkey || null });
      await loadTags(); openTagEditor();
      toast(`Added ${missing.length} tag${missing.length > 1 ? 's' : ''} from the default template`);
    };
    b.append(add, applyTemplate);
  });
}

/* ---------------------------------------------------------- import preview */

/* ------------------------------------------------------------------ merge */

function columnGroupKey(columns) {
  return columns.map((c) => c.name.trim().toLowerCase()).sort().join('|');
}

function openMergeBuilder() {
  const real = S.sources.filter((s) => !s.is_merge && !s.error);
  const groups = new Map();
  for (const s of real) {
    const key = columnGroupKey(s.columns);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const eligible = [...groups.values()].filter((g) => g.length >= 2);

  modal('Merge sources', (b) => {
    if (!eligible.length) {
      b.append(el('p', null, 'No two open sources currently share the same columns. Import matching files first.'));
      return;
    }
    b.append(el('p', null,
      'Sources are grouped by matching columns (case-insensitive). Pick 2 or more from the same group — '
      + 'merged rows keep tagging/notes tied to their original file, nothing is copied.'));
    const selected = new Set();
    eligible.forEach((group, gi) => {
      const groupHead = el('div', 'row-actions');
      groupHead.append(el('h4', null, `Group ${gi + 1} — ${group[0].columns.map((c) => c.name).join(', ')}`));
      const list = el('div', 'collist');
      const boxes = [];
      for (const s of group) {
        const lab = el('label');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.onchange = () => { cb.checked ? selected.add(s.id) : selected.delete(s.id); };
        boxes.push(cb);
        lab.append(cb, el('span', null, `${sourceLabel(s)} (${s.row_count.toLocaleString()} rows)`));
        list.append(lab);
      }
      const selectAll = el('button', 'btn ghost', 'Select all');
      selectAll.onclick = () => {
        const allChecked = boxes.every((cb) => cb.checked);
        group.forEach((s, i) => {
          boxes[i].checked = !allChecked;
          allChecked ? selected.delete(s.id) : selected.add(s.id);
        });
        selectAll.textContent = allChecked ? 'Select all' : 'Deselect all';
      };
      groupHead.append(selectAll);
      b.append(groupHead, list);
    });

    const nameRow = el('div', 'row-actions');
    const nameInput = el('input');
    nameInput.placeholder = 'Merge name';
    nameInput.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    nameRow.append(nameInput);
    b.append(nameRow);

    const acts = el('div', 'row-actions');
    const create = el('button', 'btn', 'Create merge');
    create.onclick = async () => {
      if (selected.size < 2) { toast('Select at least 2 sources from the same group'); return; }
      const name = nameInput.value.trim() || 'Merged view';
      try {
        const rec = await post('/api/merges', { name, source_ids: [...selected] });
        $('modal').hidden = true;
        await loadSources(rec.id);
        toast(`Created merge "${rec.name}" · ${rec.row_count.toLocaleString()} rows`);
      } catch (e) {
        toast('Merge failed: ' + e.message, 6000);
      }
    };
    acts.append(create);
    b.append(acts);
  }, { wide: true });
}

function openImportPreview(file, opts = {}) {
  let preview = null;
  let columnTypes = opts.initial && opts.initial.column_types ? opts.initial.column_types.slice() : null;

  modal(`Import: ${file.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const delimSel = el('select');
    for (const [label, val] of [['Auto-detect', ''], ['Comma', ','], ['Tab', '\t'], ['Semicolon', ';'], ['Pipe', '|']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      delimSel.append(opt);
    }
    if (opts.initial && opts.initial.delimiter) delimSel.value = opts.initial.delimiter;
    const headerLabel = el('label');
    const headerCb = el('input');
    headerCb.type = 'checkbox';
    headerCb.checked = opts.initial ? opts.initial.has_header !== false : true;
    headerLabel.append(headerCb, document.createTextNode(' First row is headers'));
    controls.append(delimSel, headerLabel);
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      status.textContent = `Detected delimiter: ${JSON.stringify(preview.delimiter)} · showing first ${preview.sample_rows.length} rows`;
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c));
        const typeSel = el('select');
        for (const ty of ['text', 'number', 'datetime']) {
          const opt = document.createElement('option');
          opt.value = ty; opt.textContent = ty;
          if (columnTypes[i] === ty) opt.selected = true;
          typeSel.append(opt);
        }
        typeSel.onchange = () => { columnTypes[i] = typeSel.value; };
        th.append(typeSel);
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    let resolvedPath; // undefined = not tried yet; null = tried, not local
    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // Same invisible same-host shortcut the import itself uses: a
        // resolve hit previews by path instead of shipping a sample over.
        if (resolvedPath === undefined) resolvedPath = await resolveLocalFile(file);
        if (resolvedPath) {
          preview = await post('/api/ingest/preview/path', {
            path: resolvedPath, kind: 'csv',
            delimiter: delimSel.value || null, has_header: headerCb.checked,
          });
        } else {
          const fd = new FormData();
          fd.append('file', file);
          if (delimSel.value) fd.append('delimiter', delimSel.value);
          fd.append('has_header', headerCb.checked ? 'true' : 'false');
          preview = await api('/api/ingest/preview', { method: 'POST', body: fd });
        }
        if (!columnTypes || columnTypes.length !== preview.columns.length) columnTypes = preview.inferred_types.slice();
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    delimSel.onchange = refreshPreview;
    headerCb.onchange = refreshPreview;
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      const settings = { delimiter: delimSel.value || null, has_header: headerCb.checked, column_types: columnTypes };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (resolvedPath) { // resolved during preview — import in place
        try {
          await post('/api/ingest/jobs/path', {
            path: resolvedPath, name: file.name, kind: 'csv',
            delimiter: settings.delimiter, has_header: settings.has_header,
            column_types: settings.column_types,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', file);
      fd.append('kind', 'csv');
      if (settings.delimiter) fd.append('delimiter', settings.delimiter);
      fd.append('has_header', settings.has_header ? 'true' : 'false');
      fd.append('column_types', JSON.stringify(settings.column_types));
      // Same background pipeline as the queue: transfer with progress, then
      // an ingest job the corner panel tracks.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, file.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* JSON/JSONL import — a single file, previewed live against whichever
   flatten mode is selected (same three modes store.py's ingest_json
   supports: don't flatten nested objects at all, flatten them without
   limit, or flatten only N levels deep — an array is never index-expanded
   at any depth/mode, always kept as its raw JSON text in one column, see
   _flatten_json's docstring for why). Single-file, not a queue — a JSON
   export is usually one file, unlike the CSV queue's "several files from
   the same collection tool" use case. */
function openJsonImportPreview(file, opts = {}) {
  let preview = null;
  let flattenMode = (opts.initial && opts.initial.flatten_mode) || 'none';
  let flattenDepth = (opts.initial && opts.initial.flatten_depth) || 1;

  modal(`Import: ${file.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const modeSel = el('select');
    for (const [label, val] of [["Don't flatten", 'none'], ['Flatten completely', 'full'], ['Flatten to depth…', 'depth']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      if (val === flattenMode) opt.selected = true;
      modeSel.append(opt);
    }
    const depthInput = el('input');
    depthInput.type = 'number';
    depthInput.min = '1';
    depthInput.value = String(flattenDepth);
    depthInput.style.cssText = 'width:60px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    depthInput.hidden = flattenMode !== 'depth';
    controls.append(modeSel, depthInput);
    b.append(el('p', 'fb-help',
      'A nested object can be flattened into dotted columns (user.name); an array is always kept as its '
      + 'raw JSON text in one column, at any depth, since its length can vary record to record.'));
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      status.textContent = `${preview.record_count.toLocaleString()} record${preview.record_count === 1 ? '' : 's'} `
        + `· showing first ${preview.sample_rows.length} · ${preview.columns.length} column${preview.columns.length === 1 ? '' : 's'}`;
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c), el('div', 'count', preview.inferred_types[i]));
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    let resolvedPath; // undefined = not tried yet; null = tried, not local
    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // Resolve matters most here: a .json document can't be truncated,
        // so the upload preview round-trips the WHOLE file — a resolve hit
        // previews in place instead.
        if (resolvedPath === undefined) resolvedPath = await resolveLocalFile(file);
        if (resolvedPath) {
          preview = await post('/api/ingest/preview/path', {
            path: resolvedPath, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
        } else {
          const fd = new FormData();
          fd.append('file', file);
          fd.append('flatten_mode', flattenMode);
          fd.append('flatten_depth', String(flattenDepth));
          preview = await api('/api/ingest/json/preview', { method: 'POST', body: fd });
        }
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    modeSel.onchange = () => {
      flattenMode = modeSel.value;
      depthInput.hidden = flattenMode !== 'depth';
      refreshPreview();
    };
    depthInput.oninput = debounce(() => {
      flattenDepth = Math.max(1, parseInt(depthInput.value, 10) || 1);
      refreshPreview();
    }, 300);
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      const settings = { flatten_mode: flattenMode, flatten_depth: flattenDepth };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (resolvedPath) { // resolved during preview — import in place
        try {
          await post('/api/ingest/jobs/path', {
            path: resolvedPath, name: file.name, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', file);
      fd.append('kind', 'json');
      fd.append('flatten_mode', flattenMode);
      fd.append('flatten_depth', String(flattenDepth));
      // Same background pipeline as the queue.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, file.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* Mirrors store.py's DEFAULT_IMPORT_EXTENSIONS — one format list, two
   places it has to be spelled out (a browser can't read a Python
   constant). Shared by three things: openImportModal's file-picker accept
   attribute, the directory-import chips (order matters there — it's the
   order they render in), and wireFileDrop's own filtering below — a raw
   OS drop has no equivalent of a picker's accept attribute doing that
   filtering natively, so the drop handler has to do it itself. */
const RECOGNIZED_IMPORT_EXTENSIONS = ['.csv', '.tsv', '.txt', '.psv', '.json', '.jsonl', '.ndjson'];
/* The SQLite extension set — routed to openSqliteTablePicker's configure
   step by the unified import queue, and recognized by wireFileDrop without
   a second hand-typed copy. */
const SQLITE_IMPORT_EXTENSIONS = ['.db', '.sqlite', '.sqlite3', '.db-wal'];

function extOf(filename) {
  const i = filename.lastIndexOf('.');
  return i === -1 ? '' : filename.slice(i).toLowerCase();
}

function importKindFor(filename) {
  if (SQLITE_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'sqlite';
  const ext = extOf(filename).slice(1); // drop the leading '.' — json/jsonl/ndjson below are bare
  return ext === 'json' || ext === 'jsonl' || ext === 'ndjson' ? 'json' : 'csv';
}

/* ------------------------------------------------------------- plugins */

/* Loaded once at boot — plugins are app-level (loaded by the server at its
   own startup), not per-case. Failing the fetch just means no plugin
   routing; every built-in import path works without it. */
async function loadPlugins() {
  try {
    const r = await api('/api/plugins');
    S.plugins = r.plugins || [];
    S.pluginFormats = r.formats || [];
    S.pluginTabs = r.tabs || [];
    S.pluginDirs = r.dirs || [];
  } catch { S.plugins = []; S.pluginFormats = []; S.pluginTabs = []; S.pluginDirs = []; }
  renderPluginTabs();
}

/* fnmatch-lite for plugin filename_patterns ($MFT, *$UsnJrnl*) — the same
   case-insensitive bare-filename semantics plugin_api.IngestFormat.matches
   applies server-side, so a file routes the same way whichever side asks. */
function globMatches(pattern, name) {
  const rx = pattern.toLowerCase().replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${rx}$`).test(name.toLowerCase());
}

/* The plugin format claiming this filename, or null. Built-in extensions
   win outright — a plugin claiming .csv doesn't hijack default routing; it
   stays reachable through the Plugins modal's explicit per-format picker. */
function pluginFormatFor(filename) {
  const base = filename.split(/[\\/]/).pop();
  const ext = extOf(base);
  if (RECOGNIZED_IMPORT_EXTENSIONS.includes(ext) || SQLITE_IMPORT_EXTENSIONS.includes(ext)) return null;
  return S.pluginFormats.find((f) =>
    (ext && (f.extensions || []).includes(ext))
    || (f.filename_patterns || []).some((p) => globMatches(p, base))) || null;
}

const pluginFormatById = (id) => S.pluginFormats.find((f) => f.id === id) || null;

/* Extensions plugins add beyond the built-in list — merged into the import
   pickers' accept attributes and the directory-import chips. Extension-less
   plugin targets ($MFT, $J) can't ride an accept attribute at all; they
   arrive by drag-drop, folder import (filename_patterns), or the Plugins
   modal's own unrestricted picker. */
function pluginExtensions() {
  const out = [];
  for (const f of S.pluginFormats) {
    for (const e of f.extensions || []) {
      if (!RECOGNIZED_IMPORT_EXTENSIONS.includes(e) && !out.includes(e)) out.push(e);
    }
  }
  return out;
}

function pluginFilenamePatterns() {
  const out = [];
  for (const f of S.pluginFormats) {
    for (const p of f.filename_patterns || []) if (!out.includes(p)) out.push(p);
  }
  return out;
}

function defaultPluginOptions(fmt) {
  const out = {};
  for (const o of fmt.options || []) out[o.name] = o.default ?? (o.type === 'bool' ? false : '');
  return out;
}

/* Appends File objects to S.importQueue with each one's default settings —
   shared by openImportModal's own file-picker (addInput.onchange) and
   wireFileDrop, so a dropped file and a picked one queue identically. Not
   gated on the modal being open: S.importQueue is app-level state that
   openImportModal just happens to render, so this can be called before
   the modal even exists yet and it'll show up correctly whenever it opens. */
function queueFiles(files) {
  for (const f of files) {
    const fmt = pluginFormatFor(f.name);
    if (fmt) {
      queueFilesForFormat(fmt, [f]);
      continue;
    }
    const kind = importKindFor(f.name);
    S.importQueue.push(
      kind === 'json' ? { file: f, kind, flatten_mode: 'none', flatten_depth: 1, configured: false }
      : kind === 'sqlite' ? { file: f, kind, tables: null, configured: false }
      : { file: f, kind, delimiter: null, has_header: true, column_types: null, configured: false });
  }
}

/* Queue files against one plugin format explicitly — bypasses filename
   routing entirely, for the Plugins modal's per-format picker (the only
   picker that can reach a file the format matches by pattern rather than
   extension, since accept attributes can't express "$MFT"). */
function queueFilesForFormat(fmt, files) {
  for (const f of files) {
    S.importQueue.push({
      file: f, kind: 'plugin', format_id: fmt.id,
      options: defaultPluginOptions(fmt), configured: false,
    });
  }
}

/* The one way to bring files into a case — queue any number of CSV/TSV or
   JSON/JSONL files (kind picked per file from its extension), optionally
   preview/configure each (delimiter+header+column-types for CSV,
   flatten mode+depth for JSON — openImportPreview/openJsonImportPreview
   both take the same {initial, onConfirm, onCancel} shape so either can
   sit behind this one "Preview & configure" button), then import them all
   at once. One queue for both kinds rather than a separate "Import
   JSON…" entry point, since from the analyst's side it's the same
   workflow — pick some files, maybe tweak settings, import — regardless
   of which parser ends up handling a given one. */
function openImportModal() {
  modal('Import', (b) => {
    b.append(el('p', null,
      'Queue CSV/TSV, JSON/JSONL, or SQLite files (a SQLite file needs its tables picked first), '
      + 'then import them all — imports run in the background, so you can keep working while the '
      + 'corner panel tracks progress.'));

    const queueList = el('div', 'session-queue');

    function renderQueue() {
      queueList.replaceChildren();
      if (!S.importQueue.length) { queueList.append(el('div', 'note-status', 'No files queued.')); return; }
      S.importQueue.forEach((item, i) => {
        const row = el('div', 'row-actions session-row');
        const kindLabel = item.kind === 'plugin'
          ? (pluginFormatById(item.format_id)?.label || item.format_id)
          : item.kind;
        const stateLabel = item.kind === 'sqlite'
          ? (item.configured ? `${item.tables.length} table${item.tables.length === 1 ? '' : 's'}` : 'pick tables') + ' · sqlite'
          : (item.configured ? 'configured' : 'default settings') + ` · ${kindLabel}`;
        row.append(
          el('span', 'session-name', item.file.name),
          el('span', 'count', stateLabel),
        );
        const cfg = el('button', 'btn ghost',
          item.kind === 'sqlite' ? 'Pick tables…' : item.kind === 'plugin' ? 'Options' : 'Preview & configure');
        if (item.kind === 'plugin' && !(pluginFormatById(item.format_id)?.options || []).length) {
          // Nothing to configure — the format declared no options.
          cfg.disabled = true;
          cfg.title = 'This plugin format has no options';
        }
        cfg.onclick = () => {
          if (item.kind === 'plugin') {
            openPluginOptionsForm(item, {
              onConfirm: (options) => {
                Object.assign(item, { options, configured: true });
                openImportModal();
              },
              onCancel: () => openImportModal(),
            });
            return;
          }
          const openPreview = item.kind === 'json' ? openJsonImportPreview
            : item.kind === 'sqlite' ? openSqliteTablePicker
            : openImportPreview;
          openPreview(item.file, {
            initial: item,
            onConfirm: (settings) => {
              Object.assign(item, settings, { configured: true });
              openImportModal();
            },
            onCancel: () => openImportModal(),
          });
        };
        const rm = el('button', 'btn ghost', '✕');
        rm.onclick = () => { S.importQueue.splice(i, 1); renderQueue(); };
        row.append(cfg, rm);
        queueList.append(row);
      });
    }
    renderQueue();
    b.append(queueList);

    const queueActs = el('div', 'row-actions');
    const addLabel = el('label', 'btn ghost', 'Choose files…');
    const addInput = el('input');
    addInput.type = 'file';
    addInput.accept = [...RECOGNIZED_IMPORT_EXTENSIONS, ...SQLITE_IMPORT_EXTENSIONS, ...pluginExtensions()].join(',');
    addInput.multiple = true;
    addInput.hidden = true;
    addInput.onchange = () => {
      queueFiles(addInput.files);
      addInput.value = '';
      renderQueue();
    };
    addLabel.append(addInput);
    const folderBtn = el('button', 'btn ghost', 'Import a whole folder…');
    folderBtn.title = 'Scan a directory (e.g. KAPE output) against extension + glob patterns';
    folderBtn.onclick = () => openDirectoryImportModal();
    const importAll = el('button', 'btn', 'Import all queued');
    importAll.onclick = () => {
      if (!S.importQueue.length) return;
      const unpicked = S.importQueue.find((i) => i.kind === 'sqlite' && !i.configured);
      if (unpicked) {
        toast(`Pick which tables to import from ${unpicked.file.name} first`, 4500);
        return;
      }
      const queue = S.importQueue.slice();
      S.importQueue = [];
      renderQueue();
      $('modal').hidden = true;
      // Deliberately not awaited: uploads run sequentially (one disk, one
      // spool at a time) behind this detached chain while the analyst
      // keeps working; each upload resolves into a background ingest job
      // the corner panel is already tracking.
      (async () => {
        for (const item of queue) {
          // Same-host client: try to recover the picked file's real path
          // first — a hit imports in place with no upload at all. This is
          // deliberately the ONLY route to the no-copy transport: one
          // Import button, and the analyst never chooses (or sees) which
          // transport carried a file, beyond the upload phase not existing.
          const localPath = await resolveLocalFile(item.file);
          if (localPath) {
            try {
              await post('/api/ingest/jobs/path', {
                path: localPath, name: item.file.name, kind: item.kind,
                delimiter: item.delimiter || null,
                has_header: item.has_header !== false,
                column_types: item.column_types || null,
                flatten_mode: item.flatten_mode || 'none',
                flatten_depth: item.flatten_depth || 0,
                tables: item.tables || null,
              });
              startJobsPoll();
              continue;
            } catch { /* fall through to the upload — resolution never blocks an import */ }
          }
          const fd = new FormData();
          fd.append('file', item.file);
          fd.append('kind', item.kind);
          if (item.kind === 'json') {
            fd.append('flatten_mode', item.flatten_mode || 'none');
            fd.append('flatten_depth', String(item.flatten_depth || 1));
          } else if (item.kind === 'sqlite') {
            fd.append('tables', JSON.stringify(item.tables));
          } else {
            if (item.delimiter) fd.append('delimiter', item.delimiter);
            fd.append('has_header', item.has_header ? 'true' : 'false');
            if (item.column_types) fd.append('column_types', JSON.stringify(item.column_types));
          }
          try {
            await uploadWithProgress('/api/ingest/jobs/upload', fd, item.file.name);
          } catch (e) {
            if (!e.cancelled) toast(`Upload failed for ${item.file.name}: ` + e.message, 6000);
          }
        }
      })();
    };
    queueActs.append(addLabel, folderBtn, importAll);
    b.append(queueActs);
  }, { wide: true });
}

/* Configure which tables come out of a queued SQLite file — Chromium's
   History/Cookies/Web Data/... or any other .db. Same {initial, onConfirm,
   onCancel} shape as openImportPreview/openJsonImportPreview, so the
   unified import queue can sit one "Pick tables…" button in front of any
   of the three. Shows every table with a row count and (for any column
   that looks like a WebKit/Chrome timestamp — microseconds since
   1601-01-01, Chromium's own convention) a pre-checked option to convert
   it to a readable datetime on import rather than leaving it as an opaque
   integer. Confirm hands back {tables: [{table, timestamp_columns}]} for
   the queue item; the actual import happens later as one background job
   reading every picked table out of one uploaded spool. */
function openSqliteTablePicker(initialFile, { initial, onConfirm, onCancel } = {}) {
  let file = null;
  let tables = null; // [{name, row_count, columns, likely_timestamp_columns}]
  const selected = new Map(); // table name -> Set of timestamp columns to convert
  const included = new Set(); // table names checked for import

  modal('Pick SQLite tables', (b) => {
    b.append(el('p', null,
      'Choose which tables to import from this file — each becomes its own source.'));

    const pickRow = el('div', 'row-actions');
    const pickStatus = el('span', 'count', '');
    pickRow.append(pickStatus);
    b.append(pickRow);

    const tableList = el('div', 'session-list');
    b.append(tableList);

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', 'Use selected tables');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { if (onCancel) onCancel(); else $('modal').hidden = true; };
    actions.append(importBtn, cancel);
    b.append(actions);

    function renderTables() {
      tableList.replaceChildren();
      if (!tables) return;
      if (!tables.length) { tableList.append(el('div', 'note-status', 'No tables in this file.')); return; }
      for (const t of tables) {
        const row = el('div', 'session-row');
        row.style.flexDirection = 'column';
        row.style.alignItems = 'stretch';
        const head = el('div', 'row-actions');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = included.has(t.name);
        cb.onchange = () => { cb.checked ? included.add(t.name) : included.delete(t.name); };
        const lab = el('label');
        lab.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1';
        lab.append(cb, el('span', 'session-name', t.name), el('span', 'count', `${t.row_count.toLocaleString()} rows`));
        head.append(lab);
        row.append(head);
        if (t.likely_timestamp_columns.length) {
          const tsRow = el('div', 'row-actions');
          tsRow.style.cssText = 'flex-wrap:wrap;margin-top:4px';
          tsRow.append(el('span', 'fb-help', 'Convert to readable datetime:'));
          for (const colName of t.likely_timestamp_columns) {
            const chip = el('button', 'btn ghost', colName);
            chip.setAttribute('aria-pressed', String(selected.get(t.name).has(colName)));
            chip.title = 'Toggle converting this WebKit/Chrome-epoch column to an ISO datetime on import';
            chip.onclick = () => {
              const set = selected.get(t.name);
              if (set.has(colName)) set.delete(colName); else set.add(colName);
              chip.setAttribute('aria-pressed', String(set.has(colName)));
            };
            tsRow.append(chip);
          }
          row.append(tsRow);
        }
        tableList.append(row);
      }
    }

    // Factored out of pickInput's own onchange so a file handed in from
    // outside (wireFileDrop) previews identically to one picked by hand —
    // same request, same defaults, same failure handling.
    async function loadFile(f) {
      file = f;
      pickStatus.textContent = 'Reading…';
      try {
        // Same invisible same-host shortcut as the import: a resolve hit
        // previews the tables by path instead of uploading the whole .db.
        const localPath = await resolveLocalFile(file);
        const res = localPath
          ? await post('/api/ingest/preview/path', { path: localPath, kind: 'sqlite' })
          : await (async () => {
              const fd = new FormData();
              fd.append('file', file);
              return api('/api/ingest/sqlite/preview', { method: 'POST', body: fd });
            })();
        tables = res.tables;
      } catch (e) {
        pickStatus.textContent = '';
        toast('Could not read that file: ' + e.message, 6000);
        return;
      }
      pickStatus.textContent = file.name;
      included.clear();
      selected.clear();
      const prior = initial && initial.tables
        ? new Map(initial.tables.map((t) => [t.table, new Set(t.timestamp_columns || [])]))
        : null;
      for (const t of tables) {
        // Re-opening the picker restores the previous choices; a fresh file
        // defaults to converting every detected timestamp column.
        if (prior) {
          if (prior.has(t.name)) included.add(t.name);
          selected.set(t.name, prior.get(t.name) || new Set(t.likely_timestamp_columns));
        } else {
          selected.set(t.name, new Set(t.likely_timestamp_columns));
        }
      }
      renderTables();
    }
    loadFile(initialFile);

    importBtn.onclick = () => {
      const targets = [...included];
      if (!targets.length) { toast('Check at least one table to import'); return; }
      onConfirm({
        tables: targets.map((tableName) => ({
          table: tableName,
          timestamp_columns: [...selected.get(tableName)],
        })),
      });
    };
  }, { wide: true });
}

/* Every source/merge in the case, open or not — the counterpart to the tab
   strip's now-nondestructive ✕. Open/Close just flips visibility; Remove is
   the one place the old hard-delete-on-close behavior still lives. Also
   folds in search index status (Contains/Advanced-mode search uses a
   per-table trigram substring index built in the background — see
   CLAUDE.md's "Things that bite") as a column rather than a separate
   modal, since both are "state of every table in this case" views —
   background-polled the same way the old standalone index-status modal
   was, refetching S.sources on a plain timer rather than going through
   loadSources() (which resets the open source's filters/search/sort as a
   side effect of re-selecting its tab — fine for a real navigation, not
   something a background status poll should ever trigger). */
let tablesModalPoll = null;

async function refreshSourcesQuietly() {
  const [sources, merges] = await Promise.all([api('/api/sources'), api('/api/merges')]);
  S.sources = [...sources, ...merges];
}

function indexStatusFor(s) {
  if (s.has_fts) return { text: '✓ Ready', cls: 'ready' };
  if (s.fts_building) return { text: '⏳ Building…', cls: 'building' };
  return { text: 'Not started', cls: 'idle' };
}

/* Drag a file (or several) from the OS straight onto the window to import
   it — an alternative to "Choose files…"/"Choose a SQLite file…", not a
   replacement; both still work. Wired once, globally, at the bottom of
   this file (see wireFileDrop()'s call site) — active whenever a case is
   open ($('app') visible), regardless of which tab/modal is currently
   showing, same as "Import files…" always being reachable from the
   Session menu.

   dataTransfer.types.includes('Files') is the gate on every one of these
   listeners — an OS file drag carries a 'Files' type; an in-page drag
   (tab-strip/sidebar reordering via wireDragReorder, column-header
   reordering) only ever carries 'text/plain'. Without that check this
   would show the "drop to import" overlay while dragging a tab, and
   dragleave/drop firing on every internal drag gesture would fight with
   wireDragReorder's own handlers on the same events.

   dragenter/dragleave are tracked with a depth counter rather than a
   boolean — both fire on every element boundary a drag crosses, not just
   the window's, so a naive "show on enter, hide on leave" flickers (or
   hides too early) as the pointer passes over any child element. */
function wireFileDrop() {
  let depth = 0;
  const isFileDrag = (e) => !!(e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files'));

  window.addEventListener('dragenter', (e) => {
    if (!isFileDrag(e)) return;
    depth++;
    if ($('app').hidden) return; // no case open — nothing to import into
    $('dropOverlay').hidden = false;
  });
  window.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // required for drop to be allowed here at all
  });
  window.addEventListener('dragleave', (e) => {
    if (!isFileDrag(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) $('dropOverlay').hidden = true;
  });
  window.addEventListener('drop', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // stop the browser from navigating to the dropped file
    depth = 0;
    $('dropOverlay').hidden = true;
    if ($('app').hidden) { toast('Open or create a case first'); return; }
    handleDroppedFiles([...e.dataTransfer.files]);
  });
}

/* A single dropped file recognized as SQLite opens the table-picker flow
   (it can't just queue-and-import like CSV/JSON — which table(s) to pull
   out is a real choice the analyst has to make, same as picking one by
   hand already requires). Anything else recognized (by extension — a raw
   drop has no equivalent of a file-picker's `accept` doing this filtering
   natively, so it happens here) queues into the same CSV/JSON import
   modal "Choose files…" already uses. Unrecognized files are dropped
   silently from the queue but named in a toast — better than a mysterious
   partial import with no explanation. */
function handleDroppedFiles(files) {
  if (!files.length) return;
  const known = (f) => RECOGNIZED_IMPORT_EXTENSIONS.includes(extOf(f.name))
    || SQLITE_IMPORT_EXTENSIONS.includes(extOf(f.name))
    || !!pluginFormatFor(f.name);
  const recognized = files.filter(known);
  const skipped = files.filter((f) => !known(f));
  if (!recognized.length) {
    toast(`No recognized files in the drop (${skipped.map((f) => f.name).join(', ')})`, 5000);
    return;
  }
  queueFiles(recognized);
  openImportModal();
  if (skipped.length) {
    toast(`Skipped ${skipped.length} unrecognized file${skipped.length === 1 ? '' : 's'}: ${skipped.map((f) => f.name).join(', ')}`, 5000);
  }
}

const patternLines = (text) => text.split('\n').map((l) => l.trim()).filter(Boolean);

async function loadImportProfiles() {
  try { S.importProfiles = await api('/api/import_profiles'); } catch { S.importProfiles = []; }
}

/* Point at a folder — a KAPE triage output or any other bulk-collection
   directory — and import every file inside that matches an extension plus
   include/exclude glob pattern set, instead of picking files one at a time
   the way openImportModal's queue does. Patterns can be built ad hoc or
   loaded from (and saved back to) a named workspace/import_profiles.json
   profile, so a profile built once for "KAPE" keeps working on every future
   triage without re-typing its exclusions.

   Async at the top level (unlike every other open* modal function here,
   which stay synchronous and do async work via inner handlers) because
   S.importProfiles — unlike S.savedFilters/S.tags/etc. — has no earlier,
   source-open-triggered load point to piggyback on; this is the one place
   that ever needs it, so it's simplest to just await a fresh copy before
   building the profile <select> at all, rather than rendering once with a
   possibly-stale/empty list and re-rendering again once a background fetch
   resolves.

   `state` carries a folder-browse round trip the same way openNewCaseModal
   does: openFolderBrowser swaps the modal's content out entirely, so the
   only way to keep whatever was already typed is to snapshot it into plain
   values and re-invoke this function with them as the new starting state. */
async function openDirectoryImportModal(state = {}) {
  await loadImportProfiles();

  const st = {
    root: state.root || null,
    profileId: state.profileId || null,
    recursive: state.recursive ?? true,
    extensions: state.extensions || RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions()),
    includeText: state.includeText || '',
    excludeText: state.excludeText || '',
  };
  let scanResult = null; // {root, matched, excluded, truncated}
  let checked = new Set(); // indices into scanResult.matched
  let showExcluded = false;
  let scanSeq = 0; // guards a slow scan response from overwriting a newer one

  modal('Import a folder', (b) => {
    b.append(el('p', null,
      'Point at a folder and import every file inside that matches — built for a KAPE triage or '
      + 'similar bulk-collection output. Pick a saved profile or build patterns ad hoc; the preview '
      + 'below updates as you edit them.'));

    const resultsBox = el('div', 'search-all-results');
    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', 'Import checked (0)');
    importBtn.disabled = true;

    async function runScan() {
      const seq = ++scanSeq;
      if (!st.root) { scanResult = null; checked = new Set(); renderResults(); return; }
      resultsBox.replaceChildren(el('div', 'note-status', 'Scanning…'));
      let r;
      try {
        r = await post('/api/ingest/dir/scan', {
          root: st.root, recursive: st.recursive, extensions: st.extensions,
          include_patterns: patternLines(st.includeText), exclude_patterns: patternLines(st.excludeText),
          // Lets extension-less plugin targets ($MFT, $J) past the scan's
          // extension gate — see scan_import_directory.
          filename_patterns: pluginFilenamePatterns(),
        });
      } catch (e) {
        if (seq !== scanSeq) return; // a newer scan already started; don't clobber it with a stale error
        resultsBox.replaceChildren(el('div', 'note-status', 'Scan failed: ' + e.message));
        return;
      }
      if (seq !== scanSeq) return; // a newer scan resolved first
      scanResult = r;
      // Pre-check everything except what's already in the case — the
      // common case is "import the new stuff", and already_imported exists
      // precisely so a second pass over the same folder doesn't default to
      // silently re-importing (and duplicating tabs for) everything.
      checked = new Set();
      r.matched.forEach((m, i) => { if (!m.already_imported) checked.add(i); });
      renderResults();
    }
    const scheduleScan = debounce(runScan, 300);

    function renderResults() {
      resultsBox.replaceChildren();
      const n = checked.size;
      importBtn.disabled = n === 0;
      importBtn.textContent = `Import checked (${n})`;
      if (!st.root) { resultsBox.append(el('div', 'note-status', 'Choose a folder to preview matches.')); return; }
      if (!scanResult) return; // "Scanning…" is already showing
      if (scanResult.truncated) {
        resultsBox.append(el('div', 'note-status', `Showing the first ${scanResult.matched.length + scanResult.excluded.length} files — narrow the folder or patterns to see the rest.`));
      }
      if (!scanResult.matched.length) {
        resultsBox.append(el('div', 'note-status', 'No files match.'));
      } else {
        resultsBox.append(el('div', 'fb-help', `Will import (${scanResult.matched.length})`));
        scanResult.matched.forEach((m, i) => {
          const row = el('div', 'search-all-row');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = checked.has(i);
          cb.onchange = () => { cb.checked ? checked.add(i) : checked.delete(i); renderResults(); };
          row.append(cb, el('span', 'search-all-name', m.rel_path + (m.already_imported ? '  (already in case)' : '')));
          resultsBox.append(row);
        });
      }
      if (scanResult.excluded.length) {
        const toggle = el('button', 'btn ghost', (showExcluded ? '▾ ' : '▸ ') + `Excluded (${scanResult.excluded.length})`);
        toggle.onclick = () => { showExcluded = !showExcluded; renderResults(); };
        resultsBox.append(toggle);
        if (showExcluded) {
          for (const e of scanResult.excluded) {
            const row = el('div', 'search-all-row');
            row.append(el('span', 'search-all-name', e.rel_path), el('span', 'search-all-count', e.reason));
            resultsBox.append(row);
          }
        }
      }
    }

    // --- folder row
    const folderRow = el('div', 'row-actions');
    const folderLabel = el('span', 'note-status', st.root || 'No folder chosen');
    folderLabel.style.cssText = 'font-family:var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      openFolderBrowser(st.root || undefined, (path) => {
        openDirectoryImportModal({ ...st, root: path });
      }, () => openDirectoryImportModal(st));
    };
    folderRow.append(folderLabel, browseBtn);
    b.append(folderRow);

    // --- profile row
    const profileRow = el('div', 'row-actions');
    const profileSel = el('select');
    profileSel.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
    const customOpt = document.createElement('option');
    customOpt.value = '';
    customOpt.textContent = 'Custom (not saved)';
    profileSel.append(customOpt);
    for (const p of S.importProfiles) {
      const opt = document.createElement('option');
      opt.value = String(p.id);
      opt.textContent = p.name;
      profileSel.append(opt);
    }
    profileSel.value = st.profileId ? String(st.profileId) : '';
    profileSel.onchange = () => {
      const id = profileSel.value ? Number(profileSel.value) : null;
      const p = id ? S.importProfiles.find((x) => x.id === id) : null;
      openDirectoryImportModal({
        ...st,
        profileId: id,
        recursive: p ? p.recursive : true,
        extensions: (p && p.extensions) || RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions()),
        includeText: p ? (p.include_patterns || []).join('\n') : '',
        excludeText: p ? (p.exclude_patterns || []).join('\n') : '',
      });
    };
    profileRow.append(profileSel);
    if (st.profileId) {
      const delBtn = el('button', 'btn ghost', 'Delete profile');
      delBtn.onclick = async () => {
        const name = S.importProfiles.find((p) => p.id === st.profileId)?.name || 'this profile';
        if (!(await confirmDialog(`Delete the "${name}" profile?`, { danger: true, okLabel: 'Delete' }))) return;
        await api(`/api/import_profiles/${st.profileId}`, { method: 'DELETE' });
        openDirectoryImportModal({ ...st, profileId: null });
      };
      profileRow.append(delBtn);
    }
    b.append(profileRow);

    // --- recursive checkbox
    const recLabel = el('label');
    recLabel.style.cssText = 'display:block;margin-bottom:10px';
    const recCb = el('input');
    recCb.type = 'checkbox';
    recCb.checked = st.recursive;
    recCb.onchange = () => { st.recursive = recCb.checked; scheduleScan(); };
    recLabel.append(recCb, document.createTextNode(' Include subfolders'));
    b.append(recLabel);

    // --- extension chips
    b.append(el('label', null, 'File types'));
    const extRow = el('div', 'row-actions');
    extRow.style.flexWrap = 'wrap';
    for (const ext of RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions())) {
      const chip = el('button', 'btn ghost', ext);
      chip.setAttribute('aria-pressed', String(st.extensions.includes(ext)));
      chip.onclick = () => {
        st.extensions = st.extensions.includes(ext)
          ? st.extensions.filter((e) => e !== ext)
          : [...st.extensions, ext];
        chip.setAttribute('aria-pressed', String(st.extensions.includes(ext)));
        scheduleScan();
      };
      extRow.append(chip);
    }
    b.append(extRow);

    // --- include/exclude patterns
    const patRow = el('div', 'row-actions');
    patRow.style.alignItems = 'stretch';
    const includeCol = el('div');
    includeCol.style.cssText = 'flex:1;display:flex;flex-direction:column;gap:4px;min-width:0';
    includeCol.append(el('label', null, 'Include patterns (blank = every recognized file)'));
    const includeArea = el('textarea');
    includeArea.rows = 3;
    includeArea.spellcheck = false;
    includeArea.placeholder = 'One glob per line, e.g. *EvtxECmd*';
    includeArea.value = st.includeText;
    includeArea.oninput = () => { st.includeText = includeArea.value; scheduleScan(); };
    includeCol.append(includeArea);

    const excludeCol = el('div');
    excludeCol.style.cssText = 'flex:1;display:flex;flex-direction:column;gap:4px;min-width:0';
    excludeCol.append(el('label', null, 'Exclude patterns'));
    const excludeArea = el('textarea');
    excludeArea.rows = 3;
    excludeArea.spellcheck = false;
    excludeArea.placeholder = 'One glob per line, e.g. *_Amcache_UnassociatedFileEntries.csv';
    excludeArea.value = st.excludeText;
    excludeArea.oninput = () => { st.excludeText = excludeArea.value; scheduleScan(); };
    excludeCol.append(excludeArea);

    patRow.append(includeCol, excludeCol);
    b.append(patRow);

    // --- save-as-profile
    const saveRow = el('div', 'row-actions');
    const saveBtn = el('button', 'btn ghost', st.profileId ? 'Update profile' : 'Save as profile…');
    saveBtn.onclick = async () => {
      let name = S.importProfiles.find((p) => p.id === st.profileId)?.name;
      if (!name) {
        name = await promptDialog('Name this profile:', 'KAPE');
        if (!name || !name.trim()) return;
      }
      let rec;
      try {
        rec = await post('/api/import_profiles', {
          id: st.profileId, name: name.trim(), extensions: st.extensions,
          include_patterns: patternLines(st.includeText), exclude_patterns: patternLines(st.excludeText),
          recursive: st.recursive,
        });
      } catch (e) { toast('Could not save profile: ' + e.message, 4000); return; }
      toast(`Saved profile "${rec.name}"`);
      openDirectoryImportModal({ ...st, profileId: rec.id });
    };
    saveRow.append(saveBtn);
    b.append(saveRow);

    b.append(resultsBox);

    const cancelBtn = el('button', 'btn ghost', 'Cancel');
    cancelBtn.onclick = () => { $('modal').hidden = true; };
    importBtn.onclick = async () => {
      const toImport = scanResult.matched.filter((_, i) => checked.has(i));
      if (!toImport.length) return;
      // The files are already on the server's disk, so there's no upload
      // phase — each request just *starts* a background job (milliseconds
      // each; the semaphore in Store caps how many ingest at once) and the
      // corner panel takes over from there.
      let ok = 0;
      let failed = 0;
      let pluginOk = 0;
      for (const m of toImport) {
        // A plugin-claimed file parses server-side through the plugin's own
        // endpoint — synchronous, since the jobs pipeline only knows the
        // built-in kinds. A scan-matched extension no loaded plugin claims
        // falls through to the delimited parser, the pre-plugin behavior.
        const fmt = m.kind === 'plugin' ? pluginFormatFor(m.path) : null;
        try {
          if (fmt) {
            toast(`Importing ${m.rel_path}…`, 60000);
            await post('/api/ingest/plugin/path', {
              path: m.path, name: m.rel_path, format_id: fmt.id, options: defaultPluginOptions(fmt),
            });
            pluginOk++;
          } else {
            await post('/api/ingest/jobs/path', { path: m.path, name: m.rel_path, kind: m.kind === 'json' ? 'json' : 'csv' });
          }
          ok++;
        } catch (e) {
          failed++;
          toast(`Could not queue ${m.rel_path}: ` + e.message, 6000);
        }
      }
      if (pluginOk) await loadSources(); // sync plugin imports don't announce themselves through a job
      startJobsPoll();
      $('modal').hidden = true;
      toast(`Queued ${ok} import${ok === 1 ? '' : 's'}${failed ? ` — ${failed} failed to queue` : ''} — progress in the corner panel`, 4000);
    };
    actions.append(importBtn, cancelBtn);
    b.append(actions);

    renderResults();
    if (st.root) runScan();
  }, { wide: true });
}

/* Options form for one queued plugin-format file, rendered generically
   from the format's declared option specs (bool/text/choice — see
   plugin_api.PluginAPI.register_ingest_format). Same {onConfirm, onCancel}
   shape as openImportPreview/openJsonImportPreview so openImportModal's
   one "configure" button can sit in front of any of the three. */
function openPluginOptionsForm(item, { onConfirm, onCancel }) {
  const fmt = pluginFormatById(item.format_id);
  const values = { ...defaultPluginOptions(fmt), ...(item.options || {}) };
  modal(fmt.label, (b) => {
    if (fmt.description) b.append(el('p', null, fmt.description));
    for (const o of fmt.options || []) {
      if (o.type === 'bool') {
        const label = el('label');
        label.style.cssText = 'display:block;margin-bottom:10px';
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = !!values[o.name];
        cb.onchange = () => { values[o.name] = cb.checked; };
        label.append(cb, document.createTextNode(' ' + (o.label || o.name)));
        b.append(label);
      } else if (o.type === 'choice') {
        b.append(el('label', null, o.label || o.name));
        const sel = el('select');
        sel.style.cssText = 'display:block;margin-bottom:10px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
        for (const c of o.choices || []) {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c;
          sel.append(opt);
        }
        sel.value = values[o.name] ?? o.default ?? '';
        sel.onchange = () => { values[o.name] = sel.value; };
        b.append(sel);
      } else {
        b.append(el('label', null, o.label || o.name));
        const inp = el('input');
        inp.type = 'text';
        inp.value = values[o.name] ?? '';
        inp.oninput = () => { values[o.name] = inp.value; };
        b.append(inp);
      }
    }
    const acts = el('div', 'row-actions');
    const ok = el('button', 'btn', 'Use these options');
    ok.onclick = () => onConfirm(values);
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = onCancel;
    acts.append(ok, cancel);
    b.append(acts);
  });
}

/* Settings → Plugins: everything about drop-in extensions in one place —
   every plugin found in the plugins directory (enabled, disabled, or
   failed-to-load with why), a checkbox per plugin that takes effect
   immediately (the server rescans and reloads its registry on every
   toggle; a disabled plugin's code is never even imported), and an
   installer that copies a picked .py file or plugin folder from anywhere
   on disk into the plugins directory — the same consent model as copying
   it in by hand, minus the hand. Appends into the Settings modal body and
   re-renders itself in place, same inline pattern as buildColumnsPanel.
   Each enabled format keeps its own no-accept-attribute file picker — the
   one file-picking path that can reach a target the format matches by
   bare-name pattern ("$MFT" has no extension for an accept to allow). */
function buildPluginsPanel(b) {
  const box = el('div');
  b.append(box);

  function applyListing(r) {
    S.plugins = r.plugins || [];
    S.pluginFormats = r.formats || [];
    S.pluginTabs = r.tabs || [];
    S.pluginDirs = r.dirs || [];
    renderPluginTabs(); // a toggle/install can add or remove pinned tabs
  }

  async function installFiles(fileList, relPaths) {
    const files = [...fileList];
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('paths', JSON.stringify(relPaths));
    let r;
    try {
      r = await api('/api/plugins/install', { method: 'POST', body: fd });
    } catch (e) {
      if (e.status !== 409) { toast('Install failed: ' + e.message, 6000); return; }
      // Name taken — the server won't clobber without being told to.
      if (!(await confirmDialog(`${e.message}. Replace it?`, { danger: true, okLabel: 'Replace' }))) return;
      fd.append('overwrite', 'true');
      try {
        r = await api('/api/plugins/install', { method: 'POST', body: fd });
      } catch (e2) { toast('Install failed: ' + e2.message, 6000); return; }
    }
    applyListing(r);
    renderPanel();
    if (r.error) toast(`Installed ${r.installed}, but it failed to load: ${r.error}`, 8000);
    else toast(`Installed ${r.installed}`);
  }

  function renderPanel() {
    box.replaceChildren();
    box.append(el('p', null,
      'Drop-in extensions, Notepad++-style. Toggles and installs take effect immediately — no restart. '
      + 'A plugin runs with the same privileges as Winnow itself, so only install plugins you trust.'));
    for (const d of S.pluginDirs) {
      const dir = el('div', 'note-status', d);
      dir.style.cssText = 'font-family:var(--mono)';
      box.append(dir);
    }

    if (!S.plugins.length) {
      box.append(el('p', 'note-status',
        'No plugins installed. A ready-made example (raw NTFS $MFT / USN journal parsing) ships in '
        + 'examples/plugins/mft_usn — install it below, or see plugins/README.md.'));
    }
    for (const p of S.plugins) {
      const row = el('div', 'row-actions session-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = p.enabled;
      cb.title = p.enabled ? 'Disable — its code will no longer be loaded' : 'Enable this plugin';
      cb.onchange = async () => {
        cb.disabled = true;
        try {
          applyListing(await post('/api/plugins/toggle', { fs_name: p.fs_name, enabled: cb.checked }));
          toast(cb.checked ? `Enabled ${p.name}` : `Disabled ${p.name} — its code is no longer loaded`);
        } catch (e) {
          toast('Could not toggle plugin: ' + e.message, 5000);
        }
        renderPanel();
      };
      const parts = [];
      if ((p.formats || []).length) parts.push(`${p.formats.length} format${p.formats.length === 1 ? '' : 's'}`);
      if ((p.tabs || []).length) parts.push(`${p.tabs.length} tab${p.tabs.length === 1 ? '' : 's'}`);
      const status = p.error ? 'failed to load'
        : !p.enabled ? 'disabled'
        : (parts.join(', ') || 'loaded');
      row.append(cb, el('span', 'session-name', p.name + (p.version ? ` v${p.version}` : '')), el('span', 'count', status));
      box.append(row);
      if (p.error) {
        const err = el('div', 'note-status', p.error);
        err.style.cssText = 'color:var(--bad, #c0392b);margin:0 0 10px 24px';
        box.append(err);
        continue;
      }
      if (p.description) {
        const desc = el('div', 'note-status', p.description);
        desc.style.cssText = 'margin:0 0 6px 24px';
        box.append(desc);
      }
      for (const fid of p.formats || []) {
        const f = pluginFormatById(fid);
        if (!f) continue;
        const frow = el('div', 'row-actions session-row');
        frow.style.marginLeft = '24px';
        const matches = (f.extensions || []).concat(f.filename_patterns || []).join(', ');
        frow.append(
          el('span', 'session-name', f.label),
          el('span', 'count', matches || 'no automatic matching'),
        );
        const pickLabel = el('label', 'btn ghost', 'Import files…');
        const inp = el('input');
        inp.type = 'file';
        inp.multiple = true;
        inp.hidden = true; // no accept attribute on purpose — see the panel comment
        inp.onchange = () => {
          if (!inp.files.length) return;
          queueFilesForFormat(f, [...inp.files]);
          openImportModal();
        };
        pickLabel.append(inp);
        frow.append(pickLabel);
        box.append(frow);
      }
    }

    const acts = el('div', 'row-actions');
    const fileLabel = el('label', 'btn ghost', 'Install a plugin file…');
    const fileInput = el('input');
    fileInput.type = 'file';
    fileInput.accept = '.py';
    fileInput.hidden = true;
    fileInput.onchange = () => {
      const files = [...fileInput.files];
      fileInput.value = '';
      installFiles(files, files.map((f) => f.name));
    };
    fileLabel.append(fileInput);
    const folderLabel = el('label', 'btn ghost', 'Install a plugin folder…');
    const folderInput = el('input');
    folderInput.type = 'file';
    // Folder picker: every file inside arrives with its path relative to
    // the picked folder (webkitRelativePath), which is exactly what the
    // install route's `paths` field wants.
    folderInput.webkitdirectory = true;
    folderInput.hidden = true;
    folderInput.onchange = () => {
      const files = [...folderInput.files];
      folderInput.value = '';
      installFiles(files, files.map((f) => f.webkitRelativePath || f.name));
    };
    folderLabel.append(folderInput);
    acts.append(fileLabel, folderLabel);
    box.append(acts);
  }

  renderPanel();
  // Refresh from the server in the background — cheap, and catches a
  // plugin someone dropped into the folder by hand since boot.
  loadPlugins().then(renderPanel);
}

/* Real (non-merge) sources' schema, formatted as CREATE TABLE-ish SQL —
   meant to be pasted into an LLM prompt alongside a question, so it can
   write a query for the SQL pane (which runs arbitrary read-only SQL
   against the case file — see run_sql in store.py). Every column is
   stored as TEXT no matter what type is noted in the comment (CLAUDE.md:
   inferred from a sample at import, metadata only, not a real column
   constraint) — worth spelling out since an LLM given a bare CREATE TABLE
   would otherwise assume normal column affinity and write e.g. numeric
   comparisons that silently do string comparison instead. Merges have no
   single backing table (they're a Store-level UNION over their members,
   not a real SQLite table — see _merge_source_dict in store.py), so
   they're left out; the SQL pane couldn't query one by name anyway. */
function sqlSchemaForLLM() {
  const real = S.sources.filter((s) => !s.is_merge && !s.error);
  const lines = [
    '-- Winnow case schema, for an LLM writing a SQL pane query.',
    '-- SQLite. Every column is stored as TEXT regardless of the type noted',
    "-- in comments below (inferred from a sample at import time; it's",
    '-- metadata, not an actual column constraint) — cast numeric/datetime',
    '-- columns explicitly rather than assuming normal comparison semantics.',
    '',
  ];
  for (const s of real) {
    lines.push(`-- ${s.name} (${s.row_count.toLocaleString()} rows)`);
    lines.push(`CREATE TABLE ${s.table_name} (`);
    lines.push('  rid INTEGER PRIMARY KEY,');
    s.columns.forEach((c, i) => {
      const comma = i < s.columns.length - 1 ? ',' : '';
      lines.push(`  "${c.name}" TEXT${comma} -- ${c.type}`);
    });
    lines.push(');', '');
  }
  return lines.join('\n');
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/* VACUUM, behind a confirm. Nothing else in the app ever returns freed
   pages to the OS — dropping a table, or the startup janitor clearing out
   a stale FTS index (which on a fat-trigram case file is most of its bulk),
   frees them to SQLite's own freelist, where they stay reserved for this
   case file forever. That's the right default, but after a big cleanup it
   can be tens of GB parked on disk with no way to ask for it back. The SQL
   pane deliberately refuses VACUUM (see run_sql), so this button is it. */
async function compactCaseFile() {
  const ok = await confirmDialog(
    'Compact this case file?\n\n'
    + 'This rewrites the whole file to return space freed by removed tables and '
    + 'indexes to the operating system. On a large case it can take several minutes, '
    + 'during which the app will be unresponsive, and it needs as much free disk '
    + 'space as the case file currently uses.',
    { okLabel: 'Compact' },
  );
  if (!ok) return;
  setBusy(true);
  let res;
  try { res = await post('/api/case/compact', {}); }
  catch (e) { toast('Compact failed: ' + e.message, 6000); return; }
  finally { setBusy(false); }
  toast(res.reclaimed_bytes > 0
    ? `Compacted: ${fmtBytes(res.before_bytes)} → ${fmtBytes(res.after_bytes)}, reclaimed ${fmtBytes(res.reclaimed_bytes)}`
    : `Compacted — nothing to reclaim (${fmtBytes(res.after_bytes)})`, 6000);
}

function openTablesManager() {
  modal('Tables', (b) => {
    b.dataset.kind = 'tables';
    b.append(el('p', null,
      'Every table in this case. Closing a tab from the header only hides it here — '
      + 'reopen it below, or use Remove to delete it (and its tags/notes) for good. '
      + 'Contains/Advanced search uses a per-table substring index built in the background '
      + '(shown below) — a table without one yet still searches correctly, just via a slower full scan. '
      + 'Filtering, grouping or opening the value picker on a column also builds a small index for '
      + 'that column; those are listed per table so they can be dropped if they add up.'));

    /* source_id -> [{column, building}]. Fetched once per modal open rather
       than joined onto /api/sources: it's one sqlite_master read per table
       and this modal already re-polls /api/sources every 1.5s for the FTS
       build status, which would turn into an N+1 on every tick. */
    const indexesBySource = new Map();
    async function refreshIndexes() {
      const real = S.sources.filter((s) => !s.is_merge && !s.error);
      const results = await Promise.all(real.map((s) =>
        api(`/api/column_indexes?source_id=${s.id}`).catch(() => [])));
      real.forEach((s, i) => indexesBySource.set(s.id, results[i]));
    }

    const acts = el('div', 'row-actions');
    const openAllTagged = el('button', 'btn ghost', 'Open all tagged');
    openAllTagged.onclick = async () => {
      const targets = S.sources.filter((s) => !s.is_merge && !s.error && !s.is_open && s.tagged_row_count > 0);
      if (!targets.length) { toast('No closed tables have tagged rows'); return; }
      setBusy(true);
      try {
        for (const s of targets) await post(`/api/source/${s.id}/open`, { open: true });
      } finally { setBusy(false); }
      await loadSources();
      openTablesManager();
    };
    const copySchema = el('button', 'btn ghost', 'Copy table definitions');
    copySchema.title = 'Copy every table\'s columns as SQL — paste into an LLM prompt to help write a SQL pane query';
    copySchema.onclick = () => {
      const real = S.sources.filter((s) => !s.is_merge && !s.error);
      if (!real.length) { toast('No tables to copy'); return; }
      writeClipboardText(Promise.resolve(sqlSchemaForLLM()), `Copied ${real.length} table definition${real.length === 1 ? '' : 's'}`);
    };
    const compact = el('button', 'btn ghost', 'Compact case file…');
    compact.title = 'VACUUM — return space freed by removed tables and indexes to the operating system';
    compact.onclick = async () => { await compactCaseFile(); };
    acts.append(openAllTagged, copySchema, compact);
    b.append(acts);

    const list = el('div', 'session-list');
    b.append(list);

    function render() {
      list.replaceChildren();
      for (const s of S.sources) {
        const row = el('div', 'row-actions session-row');
        const nameSpan = el('span', 'session-name', (s.is_merge ? '⛓ ' : '') + sourceLabel(s) + (s.error ? ' ⚠' : ''));
        nameSpan.title = sourceTitle(s);
        row.append(nameSpan);
        row.append(el('span', 'count', s.error
          ? s.error
          : `${s.row_count.toLocaleString()} rows · ${s.tagged_row_count.toLocaleString()} tagged · ${s.note_count.toLocaleString()} notes`));
        if (!s.error) {
          const status = indexStatusFor(s);
          row.append(el('span', 'index-status index-status-' + status.cls, status.text));
        }
        const toggle = el('button', 'btn ghost', s.is_open ? 'Close' : 'Open');
        toggle.onclick = async () => {
          setBusy(true);
          try { await post(`/api/source/${s.id}/open`, { open: !s.is_open }); }
          finally { setBusy(false); }
          if (s.is_open && S.sourceId === s.id) S.sourceId = null;
          await loadSources();
          openTablesManager();
        };
        const nick = el('button', 'btn ghost', 'Nickname…');
        nick.title = s.is_merge ? 'Rename this merge' : 'A display name shown in place of the file name — clear it to go back';
        nick.onclick = async () => {
          if (!(await editSourceNickname(s))) return;
          openTablesManager();
        };
        const del = el('button', 'btn ghost', 'Remove…');
        del.onclick = async () => {
          const warn = s.is_merge
            ? `Delete merge "${sourceLabel(s)}"? The underlying sources are untouched.`
            : `Remove ${sourceLabel(s)} from this case? Tags and notes for it are deleted too.`;
          if (!(await confirmDialog(warn, { danger: true, okLabel: 'Remove' }))) return;
          if (s.is_merge) {
            await api(`/api/merges/${-s.id}`, { method: 'DELETE' });
          } else {
            await api(`/api/source/${s.id}`, { method: 'DELETE' });
            S.viewCache.delete(s.id); // SQLite can reuse a deleted source's row id — don't let a stale cached view leak onto it
          }
          if (S.sourceId === s.id) S.sourceId = null;
          await loadSources();
          openTablesManager();
        };
        row.append(toggle, nick, del);
        list.append(row);
        const idxs = indexesBySource.get(s.id) || [];
        if (idxs.length) list.append(columnIndexRow(s, idxs));
      }
    }

    /* One line per table that has auto-created filter indexes, with a drop
       per column. They're created silently and never expire, so on a case
       where the same headers get imported and filtered repeatedly they
       accumulate unseen; dropping one costs nothing but the next filter
       rebuilding it. */
    function columnIndexRow(s, idxs) {
      const wrap = el('div', 'row-actions source-indexes');
      wrap.append(el('span', 'fb-help', 'Filter indexes:'));
      for (const ix of idxs) {
        const chip = el('span', 'index-chip' + (ix.building ? ' building' : ''));
        chip.append(el('span', null, ix.column + (ix.building ? ' (building…)' : '')));
        if (!ix.building) {
          const drop = el('button', 'index-chip-drop', '✕');
          drop.title = `Drop the index on "${ix.column}" — searches and filters still work, just by scanning`;
          drop.onclick = async () => {
            setBusy(true);
            try {
              await api(`/api/column_indexes?source_id=${s.id}&column=${encodeURIComponent(ix.column)}`,
                        { method: 'DELETE' });
            } catch (e) { toast('Could not drop index: ' + e.message, 4000); return; }
            finally { setBusy(false); }
            await refreshIndexes();
            render();
            toast(`Dropped the filter index on "${ix.column}"`);
          };
          chip.append(drop);
        }
        wrap.append(chip);
      }
      return wrap;
    }

    render();
    refreshIndexes().then(render).catch(() => {});

    if (tablesModalPoll) clearInterval(tablesModalPoll);
    tablesModalPoll = setInterval(async () => {
      if ($('modal').hidden || $('modalBody').dataset.kind !== 'tables') {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      try {
        await refreshSourcesQuietly();
        // Only re-poll the index list while one is mid-build — otherwise
        // this tick would be an N+1 (one query per table) every 1.5s just
        // to re-read a list that only changes when the analyst acts.
        if ([...indexesBySource.values()].some((l) => l.some((ix) => ix.building))) await refreshIndexes();
      } catch {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      render();
    }, 1500);
  }, { wide: true });
}

/* Checked against every real table in the case (plain contains-mode only —
   same as the grid's default search — not regex). Clicking a result opens
   that table with the same terms already applied via Advanced search,
   rather than inventing a separate cross-table results view.

   Two ways to build the term list, sharing one results pane:
   "Paste a list" (default) — a multi-line textarea, one term per line,
   OR'd together — the list-of-IOCs/hostnames/hashes use case, and lets you
   see more than the first line unlike a single-line input. "Advanced" is
   the original AND/OR/NOT chip builder, for anything needing mixed
   connectors or an exclusion — still available, just not the default.

   Explicit "Search" button rather than live-as-you-type: this hits every
   real table in the case (COUNT(*) per table, potentially a background FTS
   build kicked off per table too — see search_all_sources), not the cheap
   single-open-table filter the main grid's search bar is. Firing that on
   every keystroke while someone's still typing a hostname is real,
   avoidable backend load, not just a UX annoyance — so nothing here runs
   until the button (or Enter, or Cmd/Ctrl+Enter in the textarea) says to.

   The sweep itself runs as a server-side job (Store.start_search_all_job),
   polled from here. Every piece of this pane's state — the typed terms, the
   mode toggle, the job id, the hits so far — lives in S.searchAll rather
   than in the modal's closure, which is what makes closing the modal
   mid-sweep safe: the poll keeps running, the results keep accumulating,
   and reopening rebuilds the pane exactly where it was. On a 42 GB merge
   this sweep is minutes long; making the analyst sit and watch it was the
   real problem, not the sweep's own cost. */

/* Lazily created so a session that never searches carries no state. */
function searchAllState() {
  if (!S.searchAll) {
    S.searchAll = {
      mode: 'paste',                                        // 'paste' | 'advanced'
      chipTerms: [{ term: '', connector: 'AND', exclude: false }],
      pasteText: '',
      jobId: null,
      running: false,
      scanned: 0,
      total: 0,
      hits: [],
      error: null,
      terms: [],       // the terms the current results were produced from
      seen: false,     // whether the analyst has looked at the finished results
    };
  }
  return S.searchAll;
}

/* Terms from whichever builder is active, in the shape the API wants. */
function searchAllTerms(st) {
  if (st.mode === 'advanced') return st.chipTerms.filter((t) => t.term.trim());
  return st.pasteText.split('\n').map((l) => l.trim()).filter(Boolean)
    .map((term) => ({ term, connector: 'OR', exclude: false }));
}

let searchAllPollTimer = null;

/* Polls the job to completion regardless of whether the modal is open —
   that's the whole point. Repaints the modal's results pane only if it
   happens to be showing (searchAllRepaint is a no-op otherwise). */
function pollSearchAll() {
  clearTimeout(searchAllPollTimer);
  searchAllPollTimer = setTimeout(async () => {
    const st = S.searchAll;
    if (!st || !st.running || st.jobId == null) return;
    let job;
    try {
      job = await api(`/api/search_all/job?job_id=${st.jobId}`);
    } catch (e) {
      // There is only ever one poll chain (the clearTimeout above cancels
      // any previous one), so a 404 here is never a stale poller being
      // superseded — it means the job genuinely no longer exists: the
      // server restarted, or the case was closed/switched underneath us.
      // This has to clear `running`, otherwise the badge sticks at
      // "Search all… n/m" forever with nothing left to advance it.
      st.running = false;
      st.error = e.status === 404
        ? 'The search job is no longer on the server (it restarted, or the case was closed). Run the search again.'
        : e.message;
      updateSearchAllButton();
      searchAllRepaint();
      return;
    }
    if (!S.searchAll || S.searchAll !== st || st.jobId !== job.job_id) return; // superseded mid-flight
    st.scanned = job.scanned;
    st.total = job.total;
    st.hits = job.hits;
    st.error = job.error;
    if (job.done || job.cancelled) {
      st.running = false;
      const open = !$('modal').hidden && $('modalTitle').textContent === 'Search all tables';
      st.seen = open;
      if (!open && !job.cancelled) {
        toast(job.hits.length
          ? `Search all finished — ${job.hits.length} table${job.hits.length === 1 ? '' : 's'} matched. Reopen "Search all" to see them.`
          : 'Search all finished — no matches.', 6000);
      }
    }
    updateSearchAllButton();
    searchAllRepaint();
    if (st.running) pollSearchAll();
  }, 400);
}

/* Badge on the toolbar button so a sweep running behind a closed modal is
   still visible, and a finished-but-unread one invites you back. */
function updateSearchAllButton() {
  const btn = $('btnSearchAll');
  if (!btn) return;
  const st = S.searchAll;
  if (st && st.running) {
    const pct = st.total ? ` ${st.scanned}/${st.total}` : '';
    btn.textContent = `Search all…${pct}`;
    btn.setAttribute('aria-busy', 'true');
    btn.title = 'Search running in the background — click to watch or refine it';
  } else if (st && !st.seen && st.hits.length) {
    btn.textContent = `Search all (${st.hits.length})`;
    btn.removeAttribute('aria-busy');
    btn.title = `${st.hits.length} table(s) matched — click to see them`;
  } else {
    btn.textContent = 'Search all';
    btn.removeAttribute('aria-busy');
    btn.title = 'Search every table in this case';
  }
}

/* Set by openSearchAllModal while its pane is on screen; cleared when the
   modal closes or is replaced. Lets the poller repaint without knowing
   anything about the modal's internals. */
let searchAllRepaint = () => {};

async function startSearchAll() {
  const st = searchAllState();
  const terms = searchAllTerms(st);
  if (!terms.length) {
    st.hits = []; st.terms = []; st.error = null; st.jobId = null; st.running = false;
    updateSearchAllButton();
    searchAllRepaint();
    return;
  }
  st.terms = terms.map((t) => ({ ...t }));
  st.hits = [];
  st.error = null;
  st.scanned = 0;
  st.total = 0;
  st.seen = true;
  try {
    const job = await post('/api/search_all/start', { terms });
    st.jobId = job.job_id;
    st.running = true;
  } catch (e) {
    st.running = false;
    // A 404 on *this* endpoint means the route doesn't exist, not that
    // something wasn't found: static/ is served from disk (no-cache, so a
    // reload picks up new JS immediately) while server.py's routes are
    // whatever was imported when the process started. A frontend newer than
    // the running server lands exactly here, and the bare "Not Found" that
    // used to surface read like "your search matched nothing".
    st.error = e.status === 404
      ? 'This build of the page needs a newer server than the one running — restart server.py and reload.'
      : e.message;
  }
  updateSearchAllButton();
  searchAllRepaint();
  if (st.running) pollSearchAll();
}

/* "1,000+" rather than a precise number the server never computed —
   `capped` means the count stopped at SEARCH_ALL_COUNT_CAP instead of
   scanning every matching row. */
function searchAllCountLabel(d) {
  return d.capped
    ? `${d.match_count.toLocaleString()}+ matches`
    : `${d.match_count.toLocaleString()} match${d.match_count === 1 ? '' : 'es'}`;
}

/* One results row. With `term`, it's that term's own count inside `hit`'s
   table and opening it searches for just that term; without, it's the
   table's total and opening it carries the whole query across. Both share
   this so the open behaviour can't drift between the two. */
function searchAllHitRow(st, hit, term) {
  // The label goes through the live source record so a nickname shows here
  // too; the job's own hit.name (the file name) is the fallback for a
  // source dropped since the sweep ran.
  const hitSrc = S.sources.find((s) => s.id === hit.source_id);
  const hitName = hitSrc ? sourceLabel(hitSrc) : hit.name;
  const r = el('div', 'search-all-row' + (term ? ' search-all-term-row' : ''));
  r.append(
    el('span', 'search-all-name', term ? term.term : hitName),
    el('span', 'search-all-count', searchAllCountLabel(term || hit)),
  );
  const openBtn = el('button', 'btn ghost', 'Open ↦');
  openBtn.title = term
    ? `Open ${hitName} filtered to "${term.term}"`
    : `Open ${hitName} filtered to every term`;
  openBtn.onclick = async () => {
    const src = S.sources.find((s) => s.id === hit.source_id);
    if (src && !src.is_open) await post(`/api/source/${hit.source_id}/open`, { open: true });
    $('modal').hidden = true;
    await loadSources(hit.source_id);
    S.searchMode = 'advanced';
    // The terms the *results* came from, not whatever's since been typed
    // into the box — those are what this row's count describes.
    S.searchTerms = term
      ? [{ term: term.term, connector: 'AND', exclude: false }]
      : st.terms.map((t) => ({ ...t }));
    document.querySelectorAll('#searchModeToggle button').forEach((btn) => btn.setAttribute('aria-pressed', String(btn.dataset.mode === 'advanced')));
    renderAdvancedChips();
    syncSearchExpansion(true);
    updateSearchHint();
    await rebuildView({ keepScroll: false });
  };
  r.append(openBtn);
  return r;
}

function openSearchAllModal() {
  const st = searchAllState();
  st.seen = true;
  updateSearchAllButton();

  modal('Search all tables', (b) => {
    b.append(el('p', 'fb-help',
      'Matches every open and closed table in this case. Runs in the background — you can close this and keep working.'));

    const modeToggle = el('div', 'search-mode-toggle');
    const pasteBtn = el('button', 'btn ghost', 'Paste a list');
    const advBtn = el('button', 'btn ghost', 'Advanced (AND / OR / NOT)');
    modeToggle.append(pasteBtn, advBtn);
    b.append(modeToggle);

    const textarea = el('textarea', 'search-all-paste');
    textarea.rows = 8;
    textarea.spellcheck = false;
    textarea.placeholder = 'One term per line — e.g. a list of hostnames, hashes or other IOCs.\nMatches any line (OR).';
    b.append(textarea);

    const chips = el('div', 'advanced-search-bar search-all-terms');
    b.append(chips);

    const searchActs = el('div', 'row-actions');
    const searchBtn = el('button', 'btn', 'Search  ⌘⏎');
    const cancelBtn = el('button', 'btn ghost', 'Stop');
    cancelBtn.title = 'Stop the sweep — tables already counted keep their results';
    cancelBtn.onclick = async () => {
      if (st.jobId == null) return;
      try { await post(`/api/search_all/cancel?job_id=${st.jobId}`, {}); } catch { /* already gone */ }
    };
    const progress = el('span', 'search-all-progress');
    searchActs.append(searchBtn, cancelBtn, progress);
    b.append(searchActs);

    const results = el('div', 'search-all-results');
    b.append(results);

    textarea.value = st.pasteText;
    textarea.oninput = () => { st.pasteText = textarea.value; };

    function paintResults() {
      // The poller holds this closure and fires whether or not the pane is
      // still on screen; once the modal body has been replaced these nodes
      // are detached and there's nothing to paint.
      if (!results.isConnected) return;
      searchBtn.disabled = st.running;
      cancelBtn.hidden = !st.running;
      progress.textContent = st.running
        ? (st.total ? `Scanning ${st.scanned} of ${st.total} tables…` : 'Starting…')
        : '';

      results.replaceChildren();
      if (st.error) { results.append(el('div', 'note-status', 'Search failed: ' + st.error)); return; }
      if (!st.hits.length) {
        results.append(el('div', 'note-status',
          st.running ? 'No matches yet…' : (st.terms.length ? 'No matches.' : '')));
        return;
      }
      // Partial results while running are worth showing (a hit on the table
      // you care about often lands early), so this renders whatever's in
      // st.hits and just keeps the progress line alongside it.
      for (const h of st.hits) {
        results.append(searchAllHitRow(st, h));
        // One row per term that matched this table, indented under it —
        // the point of a pasted IOC list is knowing *which* indicators hit
        // where, which a single summed count per table can't tell you.
        // Absent (server sends []) for an Advanced query, where the terms
        // constrain each other and a standalone per-term count would
        // describe a query nobody ran.
        for (const t of h.terms || []) {
          results.append(searchAllHitRow(st, h, t));
        }
      }
    }
    searchAllRepaint = paintResults;

    function syncMode() {
      pasteBtn.setAttribute('aria-pressed', String(st.mode === 'paste'));
      advBtn.setAttribute('aria-pressed', String(st.mode === 'advanced'));
      textarea.hidden = st.mode !== 'paste';
      chips.hidden = st.mode !== 'advanced';
    }
    // Switching builder mode no longer auto-runs: with a real background job
    // that would abandon a sweep in progress just because you glanced at the
    // other tab. The Search button is the only thing that starts one.
    pasteBtn.onclick = () => { st.mode = 'paste'; syncMode(); setTimeout(() => textarea.focus(), 0); };
    advBtn.onclick = () => { st.mode = 'advanced'; syncMode(); };
    syncMode();

    searchBtn.onclick = startSearchAll;
    textarea.onkeydown = (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startSearchAll(); }
    };
    renderTermChips(chips, st.chipTerms, startSearchAll, { liveInput: false });
    paintResults();
    setTimeout(() => textarea.focus(), 0);
  }, { wide: true });
}

function openSessionManager() {
  modal('Session', (b) => {
    b.append(el('p', null,
      "A session captures every open source's tags, notes and layout. Named saves live in a sessions/ "
      + 'folder next to the case file; the download/upload flow at the bottom is for handing a session '
      + 'to another analyst on another machine.'));

    b.append(el('h4', null, 'Recent sessions'));
    const recentList = el('div', 'session-list');
    b.append(recentList);

    async function refreshRecent() {
      recentList.replaceChildren(el('div', 'note-status', 'Loading…'));
      try {
        const sessions = await api('/api/sessions');
        recentList.replaceChildren();
        if (!sessions.length) { recentList.append(el('div', 'note-status', 'No saved sessions yet.')); return; }
        for (const s of sessions) {
          const row = el('div', 'row-actions session-row');
          row.append(
            el('span', 'session-name', s.name),
            el('span', 'count', `${s.source_count} source${s.source_count === 1 ? '' : 's'} · ${s.saved_at || ''}`),
          );
          const openBtn = el('button', 'btn ghost', 'Load');
          openBtn.onclick = async () => {
            setBusy(true);
            let res;
            try {
              res = await api(`/api/sessions/${encodeURIComponent(s.name)}/load`, { method: 'POST' });
              await loadSources();
            } finally {
              setBusy(false);
            }
            (res.warnings || []).forEach((w) => toast(w, 6000));
            $('modal').hidden = true;
            toast(`Loaded "${s.name}" · ${res.tags_applied.toLocaleString()} tag assignments across ${res.sources_restored} source(s)`);
          };
          const del = el('button', 'btn ghost', '✕');
          del.title = 'Delete this saved session';
          del.onclick = async () => {
            if (!(await confirmDialog(`Delete saved session "${s.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
            await api(`/api/sessions/${encodeURIComponent(s.name)}`, { method: 'DELETE' });
            refreshRecent();
          };
          row.append(openBtn, del);
          recentList.append(row);
        }
      } catch (e) {
        recentList.replaceChildren(el('div', 'note-status', 'Could not load sessions: ' + e.message));
      }
    }
    refreshRecent();

    const saveActs = el('div', 'row-actions');
    const saveAs = el('button', 'btn', 'Save current case as…');
    saveAs.onclick = async () => {
      const name = await promptDialog('Session name:');
      if (!name || !name.trim()) return;
      setBusy(true);
      try { await post('/api/sessions', { name: name.trim() }); }
      finally { setBusy(false); }
      toast(`Saved session "${name.trim()}"`);
      refreshRecent();
    };
    saveActs.append(saveAs);
    b.append(saveActs);

    b.append(el('h4', null, 'Share with another analyst'));
    const shareActs = el('div', 'row-actions');
    const save = el('button', 'btn ghost', 'Download session file');
    save.onclick = () => { window.location = '/api/case_session'; };
    const loadLabel = el('label', 'btn ghost', 'Load session file');
    const input = el('input');
    input.type = 'file';
    input.accept = '.json';
    input.hidden = true;
    input.onchange = async () => {
      const fd = new FormData();
      fd.append('file', input.files[0]);
      fd.append('merge', 'true');
      setBusy(true);
      let res;
      try {
        res = await api('/api/case_session', { method: 'POST', body: fd });
        await loadSources();
      } finally {
        setBusy(false);
      }
      (res.warnings || []).forEach((w) => toast(w, 6000));
      $('modal').hidden = true;
      toast(`Applied ${res.tags_applied.toLocaleString()} tag assignments across ${res.sources_restored} source(s)`);
    };
    loadLabel.append(input);
    shareActs.append(save, loadLabel);
    b.append(shareActs);
  }, { wide: true });
}


/* ------------------------------------------------------------------ sql */

/* SQL and Timeline are both pinned tabs (S.activeTab), not popups —
   switching to/from either just swaps which of #grid / #sqlview /
   #timelineview occupies the main content area, the same way opening a
   different source tab swaps the visible grid. */
function showSqlTab() {
  S.activeTab = 'sql';
  $('grid').hidden = true;
  $('timelineview').hidden = true;
  hidePluginViews();
  $('sqlview').hidden = false;
  syncTabSelection();
  // The toolbar and the "matching saved filter" banner are about a
  // specific table's grid — meaningless here (see syncTabChrome).
  syncTabChrome();
  loadSqlTabs().then(() => $('sqlText').focus());
}

/* ------------------------------------------------------ sql pane sub-tabs */

/* Several named queries in the SQL pane instead of one scratch box, stored
   in the case file's sql_tabs table (see META_SCHEMA for why there rather
   than localStorage) so a worked-out query survives a restart and travels
   with the case.

   The editor holds exactly one tab's text at a time; switching tabs flushes
   the current text first (flushSqlTabSave — the debounced autosave is not
   allowed to lose an edit just because you clicked away within its window).
   Result sets stay in memory only, in S.sqlResults keyed by tab id: they're
   re-derivable by pressing Run, can be large, and are a snapshot of the
   data rather than the analysis, so they don't belong in the case file. */

const SQL_AUTOSAVE_MS = 700;

function starterSql() {
  // Merges (negative source_id) aren't a real src_N table — there's nothing
  // to `SELECT * FROM src_${S.sourceId}` for one. Prefill against its first
  // member instead of emitting invalid SQL like `src_-3`.
  if (!S.sourceId) return '';
  const src = S.sources.find((s) => s.id === S.sourceId);
  const realId = S.sourceId > 0 ? S.sourceId : (src && src.member_source_ids && src.member_source_ids[0]);
  return realId ? `SELECT * FROM src_${realId} LIMIT 50;` : '';
}

async function loadSqlTabs() {
  try {
    S.sqlTabs = await api('/api/sql_tabs');
  } catch {
    S.sqlTabs = [];
  }
  if (!S.sqlTabs.length) {
    // First visit to the SQL pane in this case — seed one tab rather than
    // showing an empty strip with nowhere to type.
    try {
      S.sqlTabs = [await post('/api/sql_tabs', { name: 'Query 1', sql: starterSql() })];
    } catch {
      S.sqlTabs = [];
    }
  }
  // savedSql mirrors what the server currently holds, so flushSqlTabSave can
  // skip a no-op PUT (it fires on every tab switch, not just after an edit).
  for (const t of S.sqlTabs) t.savedSql = t.sql;
  if (!S.sqlTabs.some((t) => t.id === S.sqlTabId)) S.sqlTabId = S.sqlTabs.length ? S.sqlTabs[0].id : null;
  applySqlTabToEditor();
  renderSqlTabs();
}

const activeSqlTab = () => S.sqlTabs.find((t) => t.id === S.sqlTabId) || null;

/* Loads the active tab's stored text + last result into the editor/result
   pane. The reverse direction (editor -> S.sqlTabs) is the textarea's own
   oninput below. */
function applySqlTabToEditor() {
  const tab = activeSqlTab();
  $('sqlText').value = tab ? tab.sql : '';
  $('sqlText').disabled = !tab;
  const out = $('sqlResult');
  const cached = tab ? S.sqlResults.get(tab.id) : null;
  if (!cached) out.replaceChildren();
  else if (cached.error) out.replaceChildren(el('div', 'sql-error', cached.error));
  else out.replaceChildren(...sqlResultNodes(cached));
}

const scheduleSqlTabSave = debounce(() => { flushSqlTabSave(); }, SQL_AUTOSAVE_MS);

/* Persists the active tab's current editor text now. Awaited before any
   action that changes which tab the editor represents, so a pending
   debounced save can never land on the wrong tab (it captures the id it
   read the text for). */
async function flushSqlTabSave() {
  const tab = activeSqlTab();
  if (!tab) return;
  const id = tab.id;
  const text = $('sqlText').value;
  if (text === tab.savedSql) return;
  tab.sql = text;
  try {
    await api(`/api/sql_tabs/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sql: text }),
    });
    const rec = S.sqlTabs.find((t) => t.id === id);
    if (rec) rec.savedSql = text;
  } catch {
    /* Autosave is best-effort: the text stays in S.sqlTabs and the next
       edit (or tab switch) retries. Not worth a toast per keystroke. */
  }
}

function renderSqlTabs() {
  const strip = $('sqlTabs');
  strip.replaceChildren();
  for (const t of S.sqlTabs) {
    const tab = el('button', 'sql-tab');
    tab.setAttribute('aria-selected', String(t.id === S.sqlTabId));
    tab.append(el('span', 'sql-tab-name', t.name));
    tab.title = `${t.name} — double-click to rename`;
    tab.onclick = () => activateSqlTab(t.id);
    tab.ondblclick = (e) => { e.preventDefault(); renameSqlTab(t); };
    if (S.sqlTabs.length > 1) {
      const x = el('span', 'x', '✕');
      x.title = 'Close this query';
      x.onclick = (e) => { e.stopPropagation(); closeSqlTab(t); };
      tab.append(x);
    }
    wireDragReorder(tab, t.id, {
      containerSelector: '#sqlTabs',
      rowSelector: '.sql-tab',
      horizontal: true,
      currentIds: () => S.sqlTabs.map((x) => x.id),
      onReorder: async (ids) => {
        S.sqlTabs = ids.map((id) => S.sqlTabs.find((x) => x.id === id)).filter(Boolean);
        renderSqlTabs();
        try { await post('/api/sql_tabs/reorder', { ids }); } catch { /* order is cosmetic */ }
      },
    });
    strip.append(tab);
  }
  const add = el('button', 'sql-tab sql-tab-add', '+');
  add.title = 'New query';
  add.onclick = newSqlTab;
  strip.append(add);
}

async function activateSqlTab(id) {
  if (id === S.sqlTabId) return;
  await flushSqlTabSave();
  S.sqlTabId = id;
  applySqlTabToEditor();
  renderSqlTabs();
  $('sqlText').focus();
}

async function newSqlTab() {
  await flushSqlTabSave();
  // "Query N" by highest existing number, not by count — otherwise closing
  // "Query 2" of 3 makes the next new tab a duplicate "Query 3".
  const n = S.sqlTabs.reduce((max, t) => {
    const m = /^Query (\d+)$/.exec(t.name);
    return m ? Math.max(max, Number(m[1])) : max;
  }, 0) + 1;
  try {
    const rec = await post('/api/sql_tabs', { name: `Query ${n}`, sql: '' });
    rec.savedSql = rec.sql;
    S.sqlTabs.push(rec);
    S.sqlTabId = rec.id;
    applySqlTabToEditor();
    renderSqlTabs();
    $('sqlText').focus();
  } catch (e) {
    toast('Could not create query tab: ' + e.message);
  }
}

async function renameSqlTab(t) {
  const name = await promptDialog('Query name:', t.name);
  if (name == null || !name.trim()) return;
  try {
    const rec = await api(`/api/sql_tabs/${t.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }),
    });
    t.name = rec.name;
    renderSqlTabs();
  } catch (e) {
    toast('Could not rename: ' + e.message);
  }
}

async function closeSqlTab(t) {
  if (S.sqlTabs.length <= 1) return; // the strip always keeps one tab to type in
  if (t.sql.trim() && !(await confirmDialog(`Close "${t.name}"? Its query is deleted from the case file.`,
    { danger: true, okLabel: 'Close' }))) return;
  try {
    await api(`/api/sql_tabs/${t.id}`, { method: 'DELETE' });
  } catch (e) {
    toast('Could not close: ' + e.message);
    return;
  }
  const idx = S.sqlTabs.findIndex((x) => x.id === t.id);
  S.sqlTabs = S.sqlTabs.filter((x) => x.id !== t.id);
  S.sqlResults.delete(t.id);
  if (S.sqlTabId === t.id) {
    const next = S.sqlTabs[Math.min(idx, S.sqlTabs.length - 1)];
    S.sqlTabId = next ? next.id : null;
    applySqlTabToEditor();
  }
  renderSqlTabs();
}

/* The toolbar (group-by strip, tag filter ribbon, row stats, timeframe,
   filters, search) and the saved-filter banner all act on the *grid's*
   view spec, so they're meaningless on the SQL, Timeline and plugin tabs —
   those have their own controls (the Timeline its own tag filter and
   stats; a plugin whatever it built). Hidden rather than left inert:
   a row of controls that silently does nothing reads as broken, and the
   space belongs to the pane you actually switched to.

   Called by every show*Tab, so there's one place this rule lives rather
   than four copies drifting apart. Grid: showGridTab re-runs checkPresets
   afterward, which is what brings the banner back when it applies. */
function syncTabChrome() {
  const isGrid = S.activeTab === 'grid';
  $('toolbar').hidden = !isGrid;
  if (!isGrid) $('presetBanner').hidden = true;
}

function showGridTab() {
  S.activeTab = 'grid';
  $('sqlview').hidden = true;
  $('timelineview').hidden = true;
  hidePluginViews();
  $('grid').hidden = false;
  syncTabSelection();
  syncTabChrome();
  if (S.sourceId) checkPresets(S.sourceId); // restore the banner, hidden while on SQL/Timeline
}
function showTimelineTab() {
  S.activeTab = 'timeline';
  $('grid').hidden = true;
  $('sqlview').hidden = true;
  hidePluginViews();
  $('timelineview').hidden = false;
  syncTabSelection();
  syncTabChrome(); // the Timeline has its own tag filter and stats
  buildTimeline(); // always fresh — tags can change in any table while this tab isn't the active one
}
$('tabSql').onclick = showSqlTab;
$('tabTimeline').onclick = showTimelineTab;

/* ---------------------------------------------------------- plugin tabs */

/* Plugin-registered pinned tabs (plugin_api.register_tab) — SQL/Timeline
   siblings whose content is a plugin-shipped ES module, dynamically
   import()ed from /plugin_assets/ on first activation and handed an empty
   <section class="pluginview"> plus the stable context object from
   buildPluginTabContext. One mount per tab, kept alive across tab
   switches (a half-built graph shouldn't vanish because the analyst
   glanced at the grid); optional onShow/onHide exports fire on every
   switch. The mount is torn down and rebuilt when its plugin's `gen`
   changes (every registry reload bumps it — a toggle-off/on picks up
   changed JS) and on a case switch (a view built from one case's data
   has no business surviving into another). */

const pluginTabMounts = new Map(); // tab id -> {container, module, gen}

const pluginTabById = (id) => S.pluginTabs.find((t) => t.id === id) || null;

function hidePluginViews() {
  for (const m of pluginTabMounts.values()) {
    if (!m.container.hidden) {
      m.container.hidden = true;
      if (m.module && m.module.onHide) { try { m.module.onHide(m.container); } catch (e) { console.error(e); } }
    }
  }
}

function resetPluginTabMounts() {
  for (const m of pluginTabMounts.values()) m.container.remove();
  pluginTabMounts.clear();
}

/* Called whenever the plugin listing changes (boot, Settings
   toggles/installs). The strip itself is renderPageTabs' job — plugin tabs
   are ordered among SQL/Timeline, not pinned after them, so there's one
   renderer for all three rather than one that inserts around another's
   output. What's left here is the mount bookkeeping: drop a mount whose
   plugin reloaded or vanished, and if the *active* plugin tab is the one
   that vanished — its plugin was toggled off — fall back to the grid
   rather than leaving a headless view up. */
function renderPluginTabs() {
  renderPageTabs();
  for (const [id, m] of [...pluginTabMounts]) {
    const t = pluginTabById(id);
    if (!t || t.gen !== m.gen) { m.container.remove(); pluginTabMounts.delete(id); }
  }
  if (S.activeTab.startsWith('plugin:') && !pluginTabById(S.activeTab.slice(7))) showGridTab();
}

/* The stable surface a plugin tab's module gets. Versioned via apiVersion
   the same way PLUGIN_API_VERSION covers the Python side: additions are
   free, changing what's already here isn't. `sql` (read-only, own
   connection server-side — see run_sql) is the blessed way for a tab to
   query the case; `schemaText` is the same LLM-ready schema dump the SQL
   pane's copy button builds. */
function buildPluginTabContext(tab) {
  return {
    apiVersion: 1,
    plugin: tab.plugin,
    base: `/api/plugin/${tab.plugin_fs}`,      // the plugin's own register_api routes
    assets: `/plugin_assets/${tab.plugin_fs}`, // the plugin's own files (css, workers, data)
    api, post, toast, el, modal, confirmDialog, promptDialog,
    sql: (sql, limit = 5000) => post('/api/sql', { sql, limit }),
    schemaText: sqlSchemaForLLM,
    openSource,
    state: {
      get sources() { return S.sources; },
      get sourceId() { return S.sourceId; },
      get tags() { return S.tags; },
    },
  };
}

async function showPluginTab(tabId) {
  const tab = pluginTabById(tabId);
  if (!tab) return;
  S.activeTab = 'plugin:' + tabId;
  $('grid').hidden = true;
  $('sqlview').hidden = true;
  $('timelineview').hidden = true;
  hidePluginViews();
  syncTabSelection();
  syncTabChrome();

  let m = pluginTabMounts.get(tabId);
  if (m && m.gen !== tab.gen) { m.container.remove(); pluginTabMounts.delete(tabId); m = null; }
  if (m) {
    m.container.hidden = false;
  } else {
    const container = el('section', 'pluginview');
    $('grid').parentElement.append(container);
    m = { container, module: null, gen: tab.gen };
    pluginTabMounts.set(tabId, m);
    try {
      // ?v=gen: a reloaded plugin gets a fresh module even though import()
      // caches by URL — see the gen note in plugin_api.PluginRegistry.
      const mod = await import(`${buildPluginTabContext(tab).assets}/${tab.entry}?v=${tab.gen}`);
      if (typeof mod.default !== 'function') throw new Error('tab module has no default export to mount');
      await mod.default(container, buildPluginTabContext(tab));
      m.module = mod;
    } catch (e) {
      console.error(e);
      container.replaceChildren(el('p', 'note-status', `Plugin tab "${tab.label}" failed to load: ${e.message}`));
      return;
    }
  }
  if (m.module && m.module.onShow) { try { m.module.onShow(m.container); } catch (e) { console.error(e); } }
}
$('btnRunSql').onclick = runSql;
$('sqlText').onkeydown = (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runSql(); }
};
$('sqlText').oninput = () => {
  const tab = activeSqlTab();
  if (tab) tab.sql = $('sqlText').value;
  scheduleSqlTabSave();
};

/* Split out of runSql so applySqlTabToEditor can re-paint a cached result
   when you switch back to a tab, without re-running its query. */
function sqlResultNodes(r) {
  const t = el('table');
  const hr = el('tr');
  for (const c of r.columns) hr.append(el('th', null, c));
  t.append(hr);
  for (const row of r.rows) {
    const tr = el('tr');
    for (const v of row) tr.append(el('td', null, v == null ? '' : String(v)));
    t.append(tr);
  }
  return [
    el('div', 'note-status', `${r.rows.length.toLocaleString()} rows · ${r.elapsed_ms} ms${r.truncated ? ' · truncated' : ''}`),
    t,
  ];
}

async function runSql() {
  const out = $('sqlResult');
  // Captured up front: the run is awaited, and the analyst can switch tabs
  // while it's in flight. The result belongs to the tab it was started
  // from, and only paints if that tab is still the one showing.
  const tabId = S.sqlTabId;
  out.replaceChildren(el('div', null, 'Running…'));
  setBusy(true);
  try {
    const r = await post('/api/sql', { sql: $('sqlText').value });
    S.sqlResults.set(tabId, r);
    if (S.sqlTabId === tabId) out.replaceChildren(...sqlResultNodes(r));
  } catch (e) {
    S.sqlResults.set(tabId, { error: e.message });
    if (S.sqlTabId === tabId) out.replaceChildren(el('div', 'sql-error', e.message));
  } finally {
    setBusy(false);
  }
}

/* ---------------------------------------------------------- unified timeline */

/* A semi-Plaso-style merged timeline of every *tagged* row across every
   real table in the case, regardless of which tab (if any) is currently
   open for it — a case-wide view of "everything I've flagged as a
   finding," not "everything in the table I happen to have open." Server
   side does the real work (build_timeline unions each source's tagged
   rows, using workspace.timeline_templates — see loadTimelineTemplates
   below and openTimelineSourceConfig — to pick that source's timestamp
   column, body columns, and a human "source type" label); this is just
   the tab UI plus a small virtualized list, same translateY-window
   technique as the main grid's render(), simplified since a row here is
   always exactly three fixed fields (ts/type/body), never per-column. */

async function loadTimelineTemplates() {
  try { S.timelineTemplates = await api('/api/timeline_templates'); } catch { S.timelineTemplates = []; }
}

function timelineTemplateFor(colNames) {
  const sig = headerSig(colNames);
  return S.timelineTemplates.find((t) => headerSig(t.col_names) === sig) || null;
}

function renderTimelineTagFilter() {
  const wrap = $('timelineTagFilter');
  wrap.replaceChildren();
  if (S.timeline.tagFilter === null) S.timeline.tagFilter = S.tags.map((t) => t.id);
  for (const t of S.tags) {
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = S.timeline.tagFilter.includes(t.id);
    cb.onchange = () => {
      S.timeline.tagFilter = cb.checked
        ? [...S.timeline.tagFilter, t.id]
        : S.timeline.tagFilter.filter((id) => id !== t.id);
      buildTimeline();
    };
    lab.append(cb, el('span', 'swatch'), document.createTextNode(t.name));
    lab.children[1].style.background = t.color;
    wrap.append(lab);
  }
}

async function buildTimeline() {
  if (S.timeline.tagFilter === null) S.timeline.tagFilter = S.tags.map((t) => t.id);
  S.timeline.pages.clear();
  S.timeline.pending.clear();
  const reqId = ++S.timeline.reqId;
  if (!S.timeline.tagFilter.length) {
    // Every tag unchecked -> nothing can match; skip the round trip and
    // show the empty state directly rather than asking the server for
    // "no tag filter," which would mean the opposite (every tagged row).
    S.timeline.view = { view_id: null, row_count: 0 };
    renderTimelineRows();
    return;
  }
  setBusy(true);
  let v;
  try {
    const token = opToken();
    const disarmCancel = armOpCancel(token);
    try {
      v = await post('/api/timeline', { tag_ids: S.timeline.tagFilter, op_token: token });
    } finally {
      disarmCancel();
    }
  } catch (e) {
    if (e.status === 499) { toast('Timeline build cancelled', 2500); return; }
    toast('Could not build timeline: ' + e.message, 6000);
    return;
  } finally {
    setBusy(false);
  }
  if (reqId !== S.timeline.reqId) return; // a newer build superseded this one
  S.timeline.view = v;
  $('timelineSpacerY').style.height = spacerPx(v.row_count) + 'px';
  $('timelineStats').innerHTML = `<b>${v.row_count.toLocaleString()}</b> tagged row${v.row_count === 1 ? '' : 's'}`;
  $('timelineBody').scrollTop = 0;
  renderTimelineRows();
}

async function ensureTimelinePage(idx) {
  if (S.timeline.pages.has(idx) || S.timeline.pending.has(idx) || !S.timeline.view || !S.timeline.view.view_id) return;
  S.timeline.pending.add(idx);
  const vid = S.timeline.view.view_id;
  try {
    const data = await api(`/api/timeline_rows?view_id=${vid}&start=${idx * PAGE}&count=${PAGE}`);
    if (!S.timeline.view || S.timeline.view.view_id !== vid) return;
    S.timeline.pages.set(idx, data.rows);
    renderTimelineRows();
  } catch { /* view expired mid-scroll — next buildTimeline() call will recover it */ } finally {
    S.timeline.pending.delete(idx);
  }
}

function timelineRowAt(pos) {
  const page = S.timeline.pages.get(Math.floor(pos / PAGE));
  return page ? page[pos % PAGE] : undefined;
}

function renderTimelineRows() {
  const view = S.timeline.view;
  const total = view ? view.row_count : 0;
  $('timelineEmpty').hidden = total > 0;
  const body = $('timelineBody');
  const rowsEl = $('timelineRows');
  if (!total) { rowsEl.replaceChildren(); return; }

  // head = 0: the timeline's header sits outside #timelineBody, unlike the
  // grid's sticky one.
  const virt = vScroll(body, total);
  const first = Math.max(0, Math.floor(virt / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);
  for (let p = Math.floor(first / PAGE); p <= Math.floor(Math.max(first, last - 1) / PAGE); p++) ensureTimelinePage(p);

  const tagColor = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
  rowsEl.style.transform = `translateY(${rowsPaintY(body, virt, first)}px)`;
  const frag = document.createDocumentFragment();
  for (let pos = first; pos < last; pos++) {
    const r = timelineRowAt(pos);
    const row = el('div', 'timeline-row' + (r ? '' : ' pending'));
    const tsCell = el('div', 'tl-col-ts', r ? (r.ts || '—') : '');
    const typeCell = el('div', 'tl-col-type');
    if (r) {
      for (const tid of r.tags) {
        const dot = el('span', 'tl-tag-dot');
        dot.style.background = tagColor[tid] || '#888';
        dot.title = (S.tags.find((t) => t.id === tid) || {}).name || '';
        typeCell.append(dot);
      }
      const badge = el('span', 'type-badge', r.type_label);
      badge.title = r.source_name;
      typeCell.append(badge);
    }
    const bodyCell = el('div', 'tl-col-body', r ? r.body : '');
    if (r) bodyCell.title = r.body;
    row.append(tsCell, typeCell, bodyCell);
    if (r) row.onclick = () => jumpToTimelineRow(r.source_id, r.rid);
    frag.append(row);
  }
  rowsEl.replaceChildren(frag);
}

async function jumpToTimelineRow(sourceId, rid) {
  await openSource(sourceId);
  showGridTab();
  await recenterOnRow({ source_id: sourceId, rid });
}

// Guarded like #body's own scroll handler below: scroll can fire several
// times per frame, and an unguarded rAF per event runs that many full
// repaints in the same frame.
let timelineScrollRaf = null;
$('timelineBody').addEventListener('scroll', () => {
  if (!timelineScrollRaf) timelineScrollRaf = requestAnimationFrame(() => { timelineScrollRaf = null; renderTimelineRows(); });
}, { passive: true });

/* Per-source "which column is the timestamp, which columns make up the
   body, what's this source called on the timeline" editor — writes to
   workspace.timeline_templates (keyed by header set, so it's reused by
   any future case whose columns match), not anything case-scoped. Every
   real source gets a row here, whether or not it has tagged rows yet —
   configuring ahead of tagging is the normal workflow, not an edge case. */
function openTimelineSourceConfig() {
  modal('Configure timeline sources', (b) => {
    b.append(el('p', null,
      'Per header set, reused across cases: which column is the timestamp, which columns (in the order '
      + 'checked) make up the body, and what to call this source type. A table with no matching config '
      + 'here falls back to its first datetime column, every column, and its own file name.'));

    const list = el('div', 'session-list');
    b.append(list);

    const realSources = S.sources.filter((s) => !s.is_merge && !s.error);
    for (const src of realSources) {
      // Header-set key: the file's own columns (see baseColumns). A derived
      // column can still be *picked* as the timestamp below — it just isn't
      // part of what identifies this source type.
      const colNames = src.columns.filter((c) => !c.derived).map((c) => c.name);
      const dtCols = src.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
      const existing = timelineTemplateFor(colNames);

      const row = el('div', 'row-actions session-row');
      row.style.flexDirection = 'column';
      row.style.alignItems = 'stretch';
      const nameSpan = el('span', 'session-name', sourceLabel(src));
      nameSpan.title = sourceTitle(src);
      row.append(nameSpan);

      const typeInput = fieldInput(existing ? existing.type_label : '');
      typeInput.placeholder = `Source type (defaults to "${src.name}")`;
      row.append(typeInput);

      const tsSel = el('select');
      tsSel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit;margin-top:6px';
      const noneOpt = document.createElement('option');
      noneOpt.value = '';
      noneOpt.textContent = dtCols.length ? '(first datetime column)' : '(no datetime column on this table)';
      tsSel.append(noneOpt);
      for (const c of dtCols) {
        const opt = document.createElement('option');
        opt.value = c; opt.textContent = c;
        tsSel.append(opt);
      }
      tsSel.value = existing && dtCols.includes(existing.timestamp_column) ? existing.timestamp_column : '';
      row.append(tsSel);

      const bodyWrap = el('div', 'row-actions');
      bodyWrap.style.flexWrap = 'wrap';
      let bodyOrder = existing && existing.body_columns && existing.body_columns.length
        ? existing.body_columns.filter((c) => colNames.includes(c))
        : [];
      for (const c of colNames) {
        const chip = el('button', 'btn ghost', c);
        chip.setAttribute('aria-pressed', String(bodyOrder.includes(c)));
        chip.title = 'Toggle inclusion in the body — order follows the order you check them in';
        chip.onclick = () => {
          bodyOrder = bodyOrder.includes(c) ? bodyOrder.filter((x) => x !== c) : [...bodyOrder, c];
          chip.setAttribute('aria-pressed', String(bodyOrder.includes(c)));
        };
        bodyWrap.append(chip);
      }
      row.append(el('span', 'fb-help', 'Body columns (click to toggle, order = click order; none checked = every column):'), bodyWrap);

      const saveBtn = el('button', 'btn', 'Save');
      saveBtn.style.marginTop = '6px';
      saveBtn.onclick = async () => {
        await post('/api/timeline_templates', {
          col_names: colNames,
          type_label: typeInput.value.trim() || src.name,
          timestamp_column: tsSel.value || null,
          body_columns: bodyOrder,
        });
        await loadTimelineTemplates();
        toast(`Saved timeline config for ${src.name}`);
      };
      row.append(saveBtn);
      list.append(row);
    }
    if (!realSources.length) list.append(el('div', 'note-status', 'No tables in this case yet.'));
  }, { wide: true });
}
$('btnTimelineConfig').onclick = openTimelineSourceConfig;

/* -------------------------------------------------------------- wire-up */

/* No horizontal header sync here anymore: .grid-head lives inside
   .grid-body as a position:sticky element (see index.html/style.css), so
   the compositor keeps it pinned vertically and moving horizontally with
   the columns — the old translateX-on-scroll sync ran on the main thread
   and lagged composited scrolling by a frame on every fast fling. */
// A fast trackpad/wheel fling fires several 'scroll' events per animation
// frame; without this guard each one queued its own rAF, so render() — a
// full rebuild of the visible rows into a fresh DocumentFragment — ran
// several times per painted frame instead of once. Same one-rAF-in-flight
// idiom as cellDragRaf below.
let bodyScrollRaf = null;
$('body').addEventListener('scroll', () => {
  if (!bodyScrollRaf) bodyScrollRaf = requestAnimationFrame(() => { bodyScrollRaf = null; render(); });
}, { passive: true });

/* Shared by the row-click path below and the cell mousedown handler further
   down — moving/extending the cursor onto whichever row was interacted
   with, however that interaction started.

   Double-click detection is done by hand here rather than a native
   'dblclick' listener: a `.cell` mousedown already renders synchronously
   (see the cell-range comment further down) — replacing its own target's
   DOM node before mouseup — and once a mousedown's target is detached
   before mouseup, browsers don't synthesize a 'click' for it at all, let
   alone a 'dblclick' built from two of them. Tracking last-activated
   position/time ourselves sidesteps that entirely. */
let lastActivate = null; // {pos, time}
function activateRow(pos, e) {
  const now = Date.now();
  const isDoubleActivate = !e.shiftKey && !e.metaKey && !e.ctrlKey
    && lastActivate && lastActivate.pos === pos && (now - lastActivate.time) < 400;
  lastActivate = isDoubleActivate ? null : { pos, time: now };

  if (e.shiftKey) moveCursor(pos, true);
  else if (e.metaKey || e.ctrlKey) {
    selToggle(pos);
    S.cursor = pos; render(); maybeShowDetail(pos);
  } else moveCursor(pos, false);

  if (isDoubleActivate) showDetail(pos);
}

$('body').addEventListener('click', (e) => {
  const groupHeader = e.target.closest('.group-header-row');
  if (groupHeader) { toggleGroup(Number(groupHeader.dataset.groupIdx)); return; }
  if (e.target.closest('.rowcheck')) return; // owned by the delegated `change` listener below
  // .cell clicks are handled synchronously from `mousedown` below (see the
  // comment there) — this handler is left only for gutter clicks (row
  // number, note icon, blank gutter space).
  if (e.target.closest('.cell')) return;
  const row = e.target.closest('.row');
  if (!row) return;
  const pos = Number(row.dataset.pos);
  activateRow(pos, e);
  $('body').focus();
});

$('body').addEventListener('change', (e) => {
  if (!e.target.classList.contains('rowcheck')) return;
  const row = e.target.closest('.row');
  const pos = Number(row.dataset.pos);
  e.target.checked ? selAdd(pos) : selRemove(pos);
  S.cellRange = null; // checking a box is a fresh "what to copy" choice — don't let a stale cell click win
  S.cellAnchor = null;
  render();
});

/* --------------------------------------------------- cell-range selection */

/* Separate from S.selection (row positions, used for tagging). This is a
   true rectangular cell selection like a spreadsheet — its own anchor,
   its own drag state — purely for reading/copying values. */

function setCellRange(a, b) {
  S.cellRange = {
    r0: Math.min(a.pos, b.pos), r1: Math.max(a.pos, b.pos),
    c0: Math.min(a.col, b.col), c1: Math.max(a.col, b.col),
  };
}

/* Clicking a cell and checking a row's checkbox are two different ways to
   pick "what to copy," and they should stay mutually exclusive rather than
   one silently shadowing the other: clicking a cell commits a (possibly
   1-cell) range immediately AND clears row selection (via activateRow's
   plain-click -> moveCursor(pos, false) path, which already clears
   S.selection); checking a checkbox clears any active cell range instead.
   Whichever the user touched most recently is what Ctrl+C acts on.

   The cursor/selection/detail-pane update is driven from here — mousedown
   — rather than a later 'click' listener, on purpose: setCellRange()+render()
   below replace the row/cell DOM nodes (render() does a full
   rowsEl.replaceChildren()) before the mouse button ever comes back up. A
   'click' event needs its mousedown and mouseup targets to still be
   attached to the document to fire at all, so a handler relying on 'click'
   for a `.cell` target would silently stop firing the moment this handler
   re-renders — the cell-range highlight would move, but the cursor/detail
   pane wouldn't. Doing both updates in the same synchronous handler avoids
   depending on that later event entirely. */

let cellDragging = false;
let cellDragRaf = null;

$('body').addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const cell = e.target.closest('.cell');
  if (!cell) return;
  e.preventDefault(); // don't let the browser's native text-drag-select fight our highlight
  const pos = Number(cell.closest('.row').dataset.pos);
  const col = Number(cell.dataset.col);
  if (e.shiftKey && S.cellAnchor) {
    setCellRange(S.cellAnchor, { pos, col });
  } else {
    S.cellAnchor = { pos, col };
    setCellRange(S.cellAnchor, S.cellAnchor); // commit immediately so a plain click alone selects that one cell
    cellDragging = true;
  }
  activateRow(pos, e); // renders once, atomically, with the cell range above
  $('body').focus();
});

$('body').addEventListener('mousemove', (e) => {
  if (!cellDragging) return;
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const pos = Number(cell.closest('.row').dataset.pos);
  const col = Number(cell.dataset.col);
  setCellRange(S.cellAnchor, { pos, col });
  if (cellDragRaf) return;
  cellDragRaf = requestAnimationFrame(() => { render(); cellDragRaf = null; });
});

document.addEventListener('mouseup', () => {
  if (!cellDragging) return;
  cellDragging = false;
  if (cellDragRaf) { cancelAnimationFrame(cellDragRaf); cellDragRaf = null; }
  render(); // guarantee the final drag state is painted, don't rely on a pending rAF firing
});

/* --------------------------------------------------- value filter picker */

/* Excel's header dropdown: a column's distinct values with their counts, as
   a checkbox list you tick and apply. What it writes is an ordinary column
   filter (`=v`, or `a|b|c` for several) — the picker is a way to *author*
   the filter the header box already understands, not a fourth filtering
   mechanism, so everything downstream (saved filters, Q's SQL, the filter
   builder) sees exactly what typing would have produced.

   Off above VALUE_FILTER_AUTO_MAX rows by default, and the reason is the
   scan behind it: distinct-values-with-counts is one aggregate pass over
   the view (or over the source, for whole-table scope) with no index to
   lean on until the lazy per-column one exists. At a few hundred thousand
   rows that's tens of milliseconds; on a 2.4M-row $J it's the kind of
   pause a button you might click by accident shouldn't be able to cause.
   The override lives per-table and per-column in the table menu (right-
   click a tab), and the row menu's "Filter by values…" opens it regardless
   — an explicit click is consent to pay for the scan in a way an
   always-present button isn't. */
const VALUE_FILTER_AUTO_MAX = 250000;
const VALUE_PICKER_LIMIT = 1000;

function valueFilterAutoOn() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return !!src && src.row_count <= VALUE_FILTER_AUTO_MAX;
}

/* Three layers, most specific first: the column's own override (stored in
   the layout, so it travels with a saved default layout for this header
   set), the table's mode, then the row-count rule. */
function valueFilterEnabled(name) {
  const override = (S.layout[name] || {}).valuePicker;
  if (override !== undefined) return !!override;
  if (S.valueFilterMode === 'on') return true;
  if (S.valueFilterMode === 'off') return false;
  return valueFilterAutoOn();
}

function setValueFilterMode(mode) {
  S.valueFilterMode = mode;
  saveLayout();
  renderHead();
}

function setColumnValueFilter(name, enabled) {
  const layout = { ...(S.layout[name] || {}) };
  if (enabled === null) delete layout.valuePicker; else layout.valuePicker = enabled;
  S.layout[name] = layout;
  saveLayout();
  renderHead();
}

/* Which values the column's current filter already selects, or null when it
   isn't a value selection at all (a contains/regex/numeric filter, or no
   filter) — null means "nothing is excluded yet", which the picker renders
   as everything ticked, the same way Excel opens on an unfiltered column. */
function currentValueSelection(column) {
  const raw = S.filters[column];
  if (raw) {
    const p = parseFilter(raw);
    if (!p) return null;
    if (p.op === 'equals') return new Set([p.value]);
    if (p.op === 'in') return new Set(p.value);
    if (p.op === 'empty') return new Set(['']);
    return null;
  }
  const node = pickerTreeNode(column);
  return node ? new Set(valuesFromPickerNode(node)) : null;
}

/* The picker's node in the guided filter tree, for the selections the
   header box can't spell (a value containing `|`, one with whitespace at
   its edges, or `(empty)` mixed with real values — `IN ()` drops empty
   strings server-side, so that combination has to be an OR).

   Recognised structurally rather than by a marker field: openFilterBuilder
   round-trips the tree through SQL text, which would drop any marker we
   invented, so "an in/equals/empty condition on this column, or an OR of
   exactly those" is the only durable identity available. */
function isPickerNode(node, column) {
  if (!node) return false;
  if (node.type === 'cond') return node.column === column && ['in', 'equals', 'empty'].includes(node.op);
  if (node.type === 'group' && node.op === 'OR' && (node.children || []).length) {
    return node.children.every((c) => isPickerNode(c, column));
  }
  return false;
}

function pickerTreeNode(column) {
  const root = S.filterTree;
  if (!root || root.type !== 'group' || root.op !== 'AND') return null;
  return (root.children || []).find((n) => isPickerNode(n, column)) || null;
}

function valuesFromPickerNode(node) {
  if (node.type === 'cond') {
    if (node.op === 'empty') return [''];
    if (node.op === 'equals') return [node.value];
    return Array.isArray(node.value) ? node.value.slice() : [];
  }
  return node.children.flatMap(valuesFromPickerNode);
}

function makePickerNode(column, values) {
  const nonEmpty = values.filter((v) => v !== '');
  const conds = [];
  if (nonEmpty.length === 1) conds.push({ type: 'cond', column, op: 'equals', value: nonEmpty[0] });
  else if (nonEmpty.length) conds.push({ type: 'cond', column, op: 'in', value: nonEmpty });
  if (nonEmpty.length !== values.length) conds.push({ type: 'cond', column, op: 'empty', value: '' });
  return conds.length === 1 ? conds[0] : { type: 'group', op: 'OR', children: conds };
}

function setPickerTreeNode(column, values) {
  const root = S.filterTree;
  const node = values && values.length ? makePickerNode(column, values) : null;
  if (root && root.type === 'group' && root.op === 'AND') {
    root.children = (root.children || []).filter((n) => !isPickerNode(n, column));
    if (node) root.children.push(node);
    return;
  }
  // A root the analyst turned into an OR, or a bare raw fragment, keeps its
  // own meaning — AND the picker's condition onto it rather than into it.
  if (node) S.filterTree = { type: 'group', op: 'AND', children: [root, node] };
}

/* The header box's two spellings, or null when neither fits — see
   valueFilterText for why `=v` is safe with a `|` in it but a multi-value
   `a|b|c` isn't, and why edge whitespace never survives either. */
function quickFilterTextForValues(values) {
  if (values.length === 1) return valueFilterText(values[0] === '' ? '' : values[0]);
  if (values.some((v) => v === '' || String(v).includes('|') || valueFilterText(v) === null)) return null;
  return values.join('|');
}

async function applyValueSelection(column, values, { clearInstead }) {
  setPickerTreeNode(column, null); // whatever the picker last put in the tree for this column
  if (clearInstead) {
    setColumnFilter(column, '');
  } else {
    const text = quickFilterTextForValues(values);
    setColumnFilter(column, text === null ? '' : text);
    if (text === null) {
      setPickerTreeNode(column, values);
      toast(`${column}: applied under Filters ▾ — these values can't be written in the filter box`, 5000);
    }
  }
  updateFiltersButton();
  renderHead(); // repaints the ▾'s in-tree marker (and the box) for this column
  await rebuildView({ keepScroll: false });
}

/* Two sources for the list, and which one is right depends on what the
   column is already filtered by. Unfiltered: the current view, so the list
   reflects every other filter in play (Excel's behaviour, and the one that
   makes "which processes survive this timeframe" answerable). Already
   filtered on this column: the whole table, because a view narrowed to
   three values can only ever offer those three back, and widening the
   selection is the main reason to reopen the dropdown. Either way it's
   swappable from the panel — the scope is stated, never guessed at. */
async function fetchPickerValues(column, scope) {
  if (scope === 'view' && S.view) {
    const r = await api(`/api/group_summary?view_id=${encodeURIComponent(S.view.view_id)}`
      + `&column=${encodeURIComponent(column)}&limit=${VALUE_PICKER_LIMIT}&order=count&bucket_datetime=false`);
    return { values: r.groups, truncated: !!r.truncated };
  }
  const rows = await api(`/api/column_values?source_id=${S.sourceId}`
    + `&column=${encodeURIComponent(column)}&limit=${VALUE_PICKER_LIMIT + 1}`);
  return { values: rows.slice(0, VALUE_PICKER_LIMIT), truncated: rows.length > VALUE_PICKER_LIMIT };
}

/* Opens the picker against a column's own filter-row button, or against the
   header cell when that button isn't rendered (the size default is off, and
   the row menu's "Filter by values…" is how you got here). */
function openValuePickerForColumn(column) {
  const anchor = document.querySelector(`.fcell-pick[data-col="${CSS.escape(column)}"]`)
    || document.querySelector(`.hcell[data-col="${CSS.escape(column)}"]`);
  if (!anchor) { toast('Open the table first'); return; }
  openValuePicker(column, anchor);
}

function openValuePicker(column, anchorEl) {
  if (!S.sourceId || S.activeTab !== 'grid') { toast('Open a table first'); return; }
  const state = {
    // An existing selection on this column means the view can't show what
    // else is out there — start from the whole table so it can be widened.
    scope: currentValueSelection(column) ? 'table' : 'view',
    sort: 'count',
    search: '',
    values: null,
    truncated: false,
    checked: new Set(),
    loading: true,
    error: null,
  };
  let paint = () => {};
  const panel = anchoredPanel(anchorEl, 'value-picker', (p, close) => {
    paint = () => renderValuePicker(p, column, state, { reload, apply, close });
    paint();
  });
  if (!panel) return; // second click on the same button — toggled shut

  async function reload() {
    state.loading = true;
    state.error = null;
    paint();
    try {
      const res = await fetchPickerValues(column, state.scope);
      state.values = res.values;
      state.truncated = res.truncated;
      const selected = currentValueSelection(column);
      // Intersected with what's actually listed, so "apply" can never send
      // back a value the analyst was never shown — a truncated list would
      // otherwise silently carry values it never rendered.
      state.checked = new Set(res.values
        .map((v) => (v.value == null ? '' : String(v.value)))
        .filter((v) => !selected || selected.has(v)));
    } catch (e) {
      // 409 = the view was evicted (see invariant #3). Whole-table scope
      // needs no view at all, so the question is still answerable — re-ask
      // it there rather than showing an error nobody can act on.
      if (e.status === 409 && state.scope === 'view') {
        state.scope = 'table';
        toast('That view expired — showing whole-table values');
        return reload();
      }
      state.error = e.message;
      state.values = [];
    }
    state.loading = false;
    paint();
  }

  async function apply() {
    const listed = (state.values || []).map((v) => (v.value == null ? '' : String(v.value)));
    const values = listed.filter((v) => state.checked.has(v));
    // Everything ticked out of a complete list is the same statement as no
    // filter at all — and says so, rather than writing a 400-term `IN`.
    const clearInstead = !state.truncated && values.length === listed.length;
    closeMenu();
    await applyValueSelection(column, values, { clearInstead });
  }

  reload();
}

function pickerRows(state) {
  const q = state.search.trim().toLowerCase();
  const rows = (state.values || [])
    .map((v) => ({ value: v.value == null ? '' : String(v.value), count: v.count }))
    .filter((v) => !q || v.value.toLowerCase().includes(q) || (v.value === '' && '(empty)'.includes(q)));
  // Sorted client-side: the fetch always asks for the most common values
  // first (that's the right thing to keep when the list is capped), so
  // A→Z is a re-sort of what came back rather than a second round trip.
  if (state.sort === 'value') rows.sort((a, b) => a.value.localeCompare(b.value, undefined, { numeric: true }));
  return rows;
}

function renderValuePicker(panel, column, state, actions) {
  panel.replaceChildren();
  panel.append(el('div', 'menu-header', column));

  const search = el('input', 'vp-search');
  search.type = 'search';
  search.placeholder = 'Find a value…';
  search.value = state.search;
  search.oninput = () => { state.search = search.value; paintList(); };
  search.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); actions.apply(); } };
  panel.append(search);

  const toggles = el('div', 'vp-toggles');
  const scopeGroup = el('div', 'vp-seg');
  for (const [key, label, title] of [
    ['view', 'This view', 'Values among the rows the current filters leave — the other filters still apply'],
    ['table', 'Whole table', 'Every value in the column, ignoring the current filters'],
  ]) {
    const b = el('button', 'btn ghost', label);
    b.setAttribute('aria-pressed', String(state.scope === key));
    b.title = title;
    b.onclick = () => { if (state.scope !== key) { state.scope = key; actions.reload(); } };
    scopeGroup.append(b);
  }
  const sortBtn = el('button', 'btn ghost', state.sort === 'count' ? 'By count' : 'A→Z');
  sortBtn.title = 'Switch between most-common-first and alphabetical';
  sortBtn.onclick = () => {
    state.sort = state.sort === 'count' ? 'value' : 'count';
    sortBtn.textContent = state.sort === 'count' ? 'By count' : 'A→Z';
    paintList(); // not a full re-render: that would drop what's typed in the search box
  };
  toggles.append(scopeGroup, sortBtn);
  panel.append(toggles);

  const list = el('div', 'vp-list');
  panel.append(list);
  const status = el('div', 'vp-status');
  panel.append(status);

  const acts = el('div', 'vp-actions');
  const all = el('button', 'btn ghost', 'All');
  all.onclick = () => { for (const r of pickerRows(state)) state.checked.add(r.value); paintList(); };
  const none = el('button', 'btn ghost', 'None');
  none.onclick = () => { for (const r of pickerRows(state)) state.checked.delete(r.value); paintList(); };
  const cancel = el('button', 'btn ghost', 'Cancel');
  cancel.onclick = () => actions.close();
  const applyBtn = el('button', 'btn', 'Apply');
  applyBtn.onclick = () => actions.apply();
  acts.append(all, none, el('span', 'spacer'), cancel, applyBtn);
  panel.append(acts);

  function paintList() {
    list.replaceChildren();
    if (state.loading) { list.append(el('div', 'vp-note', 'Reading values…')); }
    const rows = pickerRows(state);
    for (const r of rows) {
      const lab = el('label', 'vp-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = state.checked.has(r.value);
      cb.onchange = () => {
        cb.checked ? state.checked.add(r.value) : state.checked.delete(r.value);
        paintStatus();
      };
      const text = el('span', 'vp-value' + (r.value === '' ? ' vp-empty' : ''), r.value === '' ? '(empty)' : r.value);
      text.title = r.value;
      lab.append(cb, text, el('span', 'vp-count', r.count.toLocaleString()));
      list.append(lab);
    }
    if (!state.loading && !rows.length) {
      list.append(el('div', 'vp-note', state.values && state.values.length ? 'No value matches that.' : 'No values.'));
    }
    paintStatus();
  }

  function paintStatus() {
    const rows = pickerRows(state);
    const checked = rows.filter((r) => state.checked.has(r.value)).length;
    status.replaceChildren();
    if (state.error) status.append(el('div', 'vp-warn', state.error));
    if (state.truncated) {
      status.append(el('div', 'vp-warn',
        `Showing the ${VALUE_PICKER_LIMIT.toLocaleString()} most common values — type above to find others, or filter the column first.`));
    }
    status.append(el('div', null, `${checked.toLocaleString()} of ${rows.length.toLocaleString()} shown values ticked`));
    applyBtn.disabled = state.loading || !checked;
    applyBtn.title = checked ? '' : 'Tick at least one value';
  }

  paintList();
  setTimeout(() => search.focus(), 0);
}

/* ------------------------------------------------- row context menu */

/* Right-clicking a row opens the menu built from these sections. It's a
   registry rather than one function that spells the whole list out because
   this menu is now where per-row actions are expected to land — adding one
   should mean adding an entry here (or an item to an existing section),
   never surgery on a growing if-chain. Each section gets the same ctx and
   returns menu items (see fillMenuNode for the item shape); a section that
   doesn't apply returns [] and is skipped, separator and all.

   ctx: {pos, colName, colIndex, value} — the row and, when the click
   landed on a cell rather than the gutter, that cell's column and its
   value *at click time*. `pos` is deliberately re-resolved to a row on
   every repaint (rowAt(ctx.pos)) rather than captured: a keepOpen tag item
   re-renders the menu after tagging, and on the bulk path that tagging
   clears the page cache underneath it. */
const ROW_MENU_SECTIONS = [
  { id: 'tags', build: rowMenuTagItems },
  { id: 'cell', build: rowMenuCellItems },
  { id: 'clipboard', build: rowMenuClipboardItems },
];

/* How many rows the menu's actions will hit: the selection when the
   right-clicked row is part of it, otherwise just that row (openRowContextMenu
   has already moved the cursor there). */
function rowMenuTargets(ctx) {
  const n = selCount();
  return n ? { count: n, positions: () => selPositions() } : { count: 1, positions: () => [ctx.pos] };
}

function rowMenuTagItems(ctx) {
  const { count } = rowMenuTargets(ctx);
  const scope = count > 1 ? `${count.toLocaleString()} selected rows` : 'this row';
  const items = [{ header: `Tag ${scope}` }];
  const row = rowAt(ctx.pos);
  for (const t of S.tags) {
    // The ✓ reads the right-clicked row even when a whole selection is the
    // target — same rule the hotkeys already follow (resolveTagDirection
    // resolves the toggle direction from one sample row), so the menu can't
    // claim a different outcome than pressing the tag's number would.
    const on = !!row && row.tags.includes(t.id);
    items.push({
      label: t.name,
      swatch: t.color,
      checked: on,
      hint: t.hotkey || '',
      keepOpen: true, // tagging three tags in a row shouldn't need three right-clicks
      title: `${on ? 'Remove' : 'Apply'} "${t.name}" — ${scope}`,
      onclick: () => applyTag(t, !on),
    });
  }
  if (!S.tags.length) items.push({ label: 'No tags in this case yet', disabled: true });
  if (UNDO_NEXT.available) {
    items.push({
      label: `Undo: ${UNDO_NEXT.label}`,
      hint: 'Ctrl+Z',
      onclick: () => undoLastTagChange(),
    });
  }
  items.push({ label: 'Edit tags…', onclick: openTagEditor });
  return items;
}

function rowMenuCellItems(ctx) {
  if (!ctx.colName) return [];
  const shown = ellipsize(displayValue(ctx.value));
  return [
    { header: ctx.colName },
    { label: `Filter to ${shown}`, onclick: () => filterByValue(ctx.colName, ctx.value) },
    {
      label: `Filter to ${shown} only`,
      title: 'Drops every other filter and the search — the timeframe filter stays',
      onclick: () => filterByValue(ctx.colName, ctx.value, { only: true }),
    },
    { label: `Exclude ${shown}`, onclick: () => filterByValue(ctx.colName, ctx.value, { exclude: true }) },
    {
      // The way in when the column's own picker button is switched off for
      // size (see valueFilterEnabled) — an explicit click is consent to pay
      // for the scan, which the always-visible button isn't.
      label: 'Filter by values…',
      onclick: () => openValuePickerForColumn(ctx.colName),
    },
  ];
}

function rowMenuClipboardItems(ctx) {
  const { count, positions } = rowMenuTargets(ctx);
  const rows = count > 1 ? `${count.toLocaleString()} rows` : 'row';
  return [
    '-',
    {
      label: 'Copy cell',
      disabled: !ctx.colName,
      onclick: () => writeClipboardText(Promise.resolve(String(ctx.value == null ? '' : ctx.value)), 'Copied cell'),
    },
    { label: `Copy ${rows}`, onclick: () => copyRowsAsText(positions(), false) },
    { label: `Copy ${rows} with headers`, onclick: () => copyRowsAsText(positions(), true) },
  ];
}

function rowMenuItems(ctx) {
  const out = [];
  for (const section of ROW_MENU_SECTIONS) {
    const items = section.build(ctx);
    if (!items.length) continue;
    if (out.length && items[0] !== '-') out.push('-');
    out.push(...items);
  }
  return out;
}

function openRowContextMenu(ctx, e) {
  contextMenu(e, () => rowMenuItems(ctx));
}

/* ------------------------------------------------- group header actions */

/* A view id covering exactly this group's rows. A leaf group that's already
   expanded has one; anything else gets a throwaway built the same way, since
   expand_group scopes by the group's column/value *plus* its path and so
   answers for an outer level just as well as a leaf. Returns a release()
   the caller must call — a no-op for the borrowed leaf view, a DELETE for
   the throwaway, so tagging an unexpanded group doesn't leak a v.view_N
   per right-click. */
async function groupRowsView(g) {
  if (g.viewId) return { viewId: g.viewId, release: () => {} };
  const res = await post('/api/group_expand', {
    view_id: S.view.view_id, column: S.groupByCols[g.level], value: g.value, path: g.path,
  });
  return {
    viewId: res.view_id,
    release: () => api(`/api/view/${res.view_id}`, { method: 'DELETE' }).catch(() => {}),
  };
}

/* Tags every row in a group in one server-side operation, rather than
   paging the group in to build a rid list — the same reason applyTag hands
   a whole-view selection to /api/row_tags/view instead of enumerating it.
   Works on a collapsed group and on an outer nesting level, where the
   client has never seen a single one of the rows. */
async function tagWholeGroup(g, tag, on) {
  const n = g.count;
  if (n >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} all ${n.toLocaleString()} rows in this group as "${tag.name}"?`))) return;
  setBusy(true);
  let handle = null, res;
  try {
    handle = await groupRowsView(g);
    res = await post('/api/row_tags/view', { view_id: handle.viewId, tag_id: tag.id, on });
  } catch (e) {
    toast('Could not tag: ' + e.message, 5000);
    return;
  } finally {
    setBusy(false);
    if (handle) handle.release();
  }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  clearGroupPageCache(); // the server changed rows this client may never have fetched
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  const affected = res.affected != null ? res.affected : n;
  toast(`${on ? 'Tagged' : 'Untagged'} ${affected.toLocaleString()} row${affected === 1 ? '' : 's'} · ${tag.name}`);
}

/* The data-row span a leaf group occupies, as flattened positions — what
   "select this group's rows" needs. Null for a group with no data rows on
   screen (collapsed, or an outer level). */
function groupRowSpan(gi) {
  const g = S.groups[gi];
  if (!g || !g.expanded || !isLeafLevel(g.level) || !g.rowCount) return null;
  const start = S.groupPrefix[gi] + 1;
  return { start, end: start + g.rowCount - 1 };
}

/* Flipped by the menu's own "Remove a tag instead" item, which repaints
   through fillMenuNode's rerender rather than opening a second surface. A
   group is a set of rows with mixed tags, so there's no single row to read
   a ✓ off the way rowMenuTagItems does — apply and remove have to be two
   explicit choices rather than one toggle. Module-level (not per-menu)
   because the menu is a singleton; reset every time one opens. */
let groupMenuUntagMode = false;

function groupMenuItems(gi) {
  const g = S.groups[gi];
  if (!g) return [];
  const colName = S.groupByCols[g.level];
  const label = groupValueLabel(colName, g.value);
  const scope = `${g.count.toLocaleString()} row${g.count === 1 ? '' : 's'}`;
  const items = [{ header: `${groupColLabel(colName)}: ${ellipsize(label)} — ${scope}` }];
  items.push({
    label: g.expanded ? 'Collapse' : 'Expand',
    onclick: () => toggleGroup(gi),
  });
  const span = groupRowSpan(gi);
  if (span) {
    items.push({
      label: `Select these ${scope}`,
      onclick: () => { selSetRange(span.start, span.end); S.anchor = span.start; S.cursor = span.start; render(); },
    });
  }
  const on = !groupMenuUntagMode;
  items.push({ header: `${on ? 'Tag' : 'Untag'} ${scope}` });
  for (const t of S.tags) {
    items.push({
      label: t.name,
      swatch: t.color,
      hint: t.hotkey || '',
      keepOpen: true,
      title: `${on ? 'Apply' : 'Remove'} "${t.name}" ${on ? 'to' : 'from'} every row in this group`,
      onclick: () => tagWholeGroup(g, t, on),
    });
  }
  if (!S.tags.length) items.push({ label: 'No tags in this case yet', disabled: true });
  if (S.tags.length) {
    items.push({
      label: on ? 'Remove a tag instead…' : 'Apply a tag instead…',
      keepOpen: true,
      onclick: () => { groupMenuUntagMode = !groupMenuUntagMode; },
    });
  }
  // A datetime group's value is a calendar-day bucket (see DAY_BUCKET in
  // store.py), not a value any row literally holds, so a "=value" filter
  // built from it would match nothing — offer this only where it works.
  // A tag group filters through the tag ribbon's own mechanism instead,
  // since "tagged X" was never a column filter to begin with.
  const colType = (S.columns.find((c) => c.name === colName) || {}).type;
  if (isTagGroupCol(colName)) {
    items.push('-');
    items.push({
      label: `Filter to ${ellipsize(label)}`,
      title: 'Narrows the whole view to these rows — the same thing clicking the tag in the ribbon does',
      onclick: () => {
        S.tagFilter = [g.value === null ? '__none__' : g.value];
        renderTagRibbon();
        rebuildView({ keepScroll: false });
      },
    });
  } else if (colType !== 'datetime') {
    items.push('-');
    items.push({ label: `Filter to ${ellipsize(displayValue(g.value))}`, onclick: () => filterByValue(colName, g.value) });
    items.push({ label: `Exclude ${ellipsize(displayValue(g.value))}`, onclick: () => filterByValue(colName, g.value, { exclude: true }) });
  }
  items.push('-');
  items.push({
    label: 'Copy group value',
    onclick: () => writeClipboardText(Promise.resolve(label), 'Copied group value'),
  });
  return items;
}

function openGroupContextMenu(gi, e) {
  groupMenuUntagMode = false; // every menu opens in the common (apply) direction
  contextMenu(e, () => groupMenuItems(gi));
}

$('body').addEventListener('contextmenu', (e) => {
  const groupHeader = e.target.closest('.group-header-row');
  if (groupHeader) {
    e.preventDefault();
    openGroupContextMenu(Number(groupHeader.dataset.groupIdx), e);
    return;
  }
  const rowEl = e.target.closest('.row');
  if (!rowEl) return; // header, gutter strip, empty space: leave the browser's own menu alone
  const pos = Number(rowEl.dataset.pos);
  const cellEl = e.target.closest('.cell');
  const colIndex = cellEl ? Number(cellEl.dataset.col) : null;
  const colName = colIndex == null ? null : visibleCols()[colIndex];
  e.preventDefault();
  // Right-clicking inside an existing selection acts on the whole selection
  // (tagging 200 checked rows shouldn't collapse to the one under the
  // pointer); right-clicking outside it moves there first, which is what
  // every file manager does and what makes "this row" unambiguous.
  const inSelection = selCount() && selHas(pos);
  if (colIndex != null) {
    // Highlight the cell the menu is about — and make it the thing Ctrl+C
    // and the `f` keybind act on next, so the menu and the keyboard agree.
    // Set before moveCursor so its render paints both changes at once.
    S.cellAnchor = { pos, col: colIndex };
    setCellRange(S.cellAnchor, S.cellAnchor);
  }
  if (!inSelection) moveCursor(pos, false); // renders
  else render();
  const r = rowAt(pos);
  const value = r && colName ? r.cells[S.columns.findIndex((c) => c.name === colName)] : null;
  openRowContextMenu({ pos, colName, colIndex, value }, e);
});

/* How many page fetches are allowed to be in flight at once. Unbounded, a
   select-all + Ctrl+C on a 1.2M-row view fired ~2,400 simultaneous requests
   at a single-connection SQLite backend; the browser queues most of them
   anyway, so the only thing the fan-out bought was a thundering herd.
   Re-checked against PAGE=5000 (was 500): the same 1.2M-row worst case is
   now 240 page fetches, not 2,400 — strictly less pressure, not more, so
   this and the 20,000-row copy cap below (a row-count ceiling, independent
   of PAGE) don't need to change. */
const PAGE_FETCH_CONCURRENCY = 6;

/* Loads every listed page, or throws.

   Both properties matter. The old version fired one ensurePage per missing
   page at once and then polled with a hard 8-second deadline — after which
   it returned *successfully* with pages still missing, and its callers
   happily emitted '' for every row they couldn't find. A copy that quietly
   contains blank rows is worse than one that fails: it looks right. So this
   is bounded, has no deadline (the work is proportional to what was asked
   for, and setBusy/toast already tell the analyst it's running), and
   surfaces a failed fetch as a rejection instead of a silent gap. */
async function waitForPages(pageIndices) {
  const vid = S.view && S.view.view_id;
  const keep = new Set(pageIndices); // built once, not per ensurePage call
  const queue = pageIndices.filter((p) => !S.pages.has(p));
  let next = 0;
  const worker = async () => {
    while (next < queue.length) {
      const idx = queue[next++];
      if (S.pages.has(idx)) continue;
      await ensurePage(idx, { keep });
      if (!S.view || S.view.view_id !== vid) throw new Error('the view changed while loading');
      if (!S.pages.has(idx)) throw new Error(`page ${idx} could not be loaded`);
    }
  };
  await Promise.all(Array.from({ length: Math.min(PAGE_FETCH_CONCURRENCY, queue.length) }, worker));
}

/* The Clipboard API (navigator.clipboard) only exists at all in a "secure
   context" — HTTPS, or the loopback host (127.0.0.1/localhost). Timeline
   Lite defaults to loopback, but server.py's own --host flag explicitly
   allows binding elsewhere (with a printed warning) for analysts who need
   to reach it from another machine on the case network — and on any of
   those origins, every browser sets navigator.clipboard to undefined
   outright, not just individual calls failing. There's no fixing that from
   here short of requiring HTTPS, which is a heavier ask than a local tool
   should make. document.execCommand('copy') is the pre-Clipboard-API
   mechanism — deprecated, but still implemented everywhere, and it works
   in insecure contexts precisely because it doesn't go through this API. */
function legacyCopyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0;';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

/* navigator.clipboard.writeText() has to be called synchronously within the
   user gesture that triggered it — Firefox (and Safari) silently reject it
   otherwise, with no visible error, the moment there's been any `await`
   first (Mozilla bug 1605928). waitForPages() is exactly that: an await,
   needed whenever the copied range spans pages that haven't been fetched
   yet. Chrome tolerates the gap; Firefox doesn't, which is why this was
   invisible in Chromium testing and broke on a fresh Firefox session the
   moment a copy touched an unfetched page.

   navigator.clipboard.write() with a ClipboardItem sidesteps this: the
   *call* to write() must still happen synchronously with the gesture, but
   the item's value is allowed to be a still-pending Promise — the actual
   page fetch and text-building can keep happening after that, async, and
   the write only resolves once the promise does. textPromise must already
   be a live (called, not just defined) promise by the time this runs — see
   the IIFE pattern in the two callers below.

   None of that matters if navigator.clipboard doesn't exist in the first
   place (insecure context, see above) — that path can't defer past an
   await the way the ClipboardItem trick does, so it just waits for the
   text up front and uses the synchronous legacy fallback instead. */
async function writeClipboardText(textPromise, successMsg) {
  try {
    if (!navigator.clipboard) {
      if (!legacyCopyText(await textPromise)) throw new Error('clipboard access is unavailable on this connection');
    } else if (window.ClipboardItem) {
      await navigator.clipboard.write([
        new ClipboardItem({ 'text/plain': textPromise.then((text) => new Blob([text], { type: 'text/plain' })) }),
      ]);
    } else {
      await navigator.clipboard.writeText(await textPromise);
    }
    toast(successMsg);
  } catch (e) {
    toast('Copy failed: ' + e.message, 4000);
  }
}

async function copySelectedCells(withHeaders) {
  if (!S.cellRange) return;
  const { r0, r1, c0, c1 } = S.cellRange;
  const rowCount = r1 - r0 + 1;
  if (rowCount > 20000) { toast('Selection too large to copy (max 20,000 rows)', 4000); return; }
  const cols = visibleCols().slice(c0, c1 + 1);
  const spanned = [];
  for (let pos = r0; pos <= r1; pos++) spanned.push(pos);
  if (positionsNeedLoading(spanned)) toast(`Copying ${rowCount.toLocaleString()} row${rowCount > 1 ? 's' : ''}…`, 8000);
  const textPromise = (async () => {
    await loadRowsForPositions(spanned); // no-op fast path once everything's already cached
    const colIdx = Object.fromEntries(S.columns.map((c, i) => [c.name, i]));
    const lines = [];
    if (withHeaders) lines.push(cols.join('\t'));
    for (const pos of spanned) {
      // A grouped range can span group headers, which aren't rows — skip
      // those. Anything else missing here would be a bug, not a slow fetch
      // (the load above threw if it couldn't get a page), so refuse rather
      // than emit a blank line.
      if (S.groupByCols.length && !groupCoordAt(pos)) continue;
      const r = rowAt(pos);
      if (!r) throw new Error(`row ${pos + 1} could not be loaded`);
      lines.push(cols.map((name) => (r.cells[colIdx[name]] ?? '')).join('\t'));
    }
    return lines.join('\n');
  })();
  await writeClipboardText(textPromise, `Copied ${rowCount.toLocaleString()} row${rowCount > 1 ? 's' : ''}${withHeaders ? ' with headers' : ''}`);
}

async function copyRowsAsText(positions, withHeaders) {
  // Same ceiling copySelectedCells applies to a cell range. Now that
  // "select all" is a flag rather than a materialized Set, Ctrl+C on a
  // 1.2M-row selection is one keystroke away, and it would otherwise mean
  // fetching the whole table to build a clipboard string out of it.
  if (positions.length > 20000) { toast('Selection too large to copy (max 20,000 rows)', 4000); return; }
  const cols = visibleCols();
  if (positionsNeedLoading(positions)) toast(`Copying ${positions.length.toLocaleString()} row${positions.length > 1 ? 's' : ''}…`, 8000);
  const textPromise = (async () => {
    await loadRowsForPositions(positions);
    const colIdx = Object.fromEntries(S.columns.map((c, i) => [c.name, i]));
    const lines = [];
    if (withHeaders) lines.push(cols.join('\t'));
    for (const pos of positions) {
      const r = rowAt(pos);
      if (!r) throw new Error(`row ${pos + 1} could not be loaded`);
      lines.push(cols.map((name) => (r.cells[colIdx[name]] ?? '')).join('\t'));
    }
    return lines.join('\n');
  })();
  await writeClipboardText(textPromise, `Copied ${positions.length.toLocaleString()} row${positions.length > 1 ? 's' : ''}${withHeaders ? ' with headers' : ''}`);
}

/* Ctrl+C prefers an explicit dragged/shift-clicked cell range; otherwise
   falls back to whatever rows are actually selected — checked rows first,
   then just the cursor row — so "select a row, then copy" (via checkbox or
   a plain click) copies the whole row rather than nothing or a stray cell. */
async function handleCopyShortcut(withHeaders) {
  if (S.cellRange) { await copySelectedCells(withHeaders); return; }
  const count = selCount();
  // Checked before materializing: selPositions() on a select-all would
  // allocate an array of every position in the view just to have it
  // rejected by copyRowsAsText's own ceiling on the next line.
  if (count > 20000) { toast('Selection too large to copy (max 20,000 rows)', 4000); return; }
  // The cursor fallback can be parked on a group header, which isn't a row.
  const cursorRow = S.cursor >= 0 && !(S.groupByCols.length && !groupCoordAt(S.cursor));
  const positions = count ? selPositions() : cursorRow ? [S.cursor] : [];
  if (!positions.length) return;
  await copyRowsAsText(positions, withHeaders);
}

/* ------------------------------------------------- filter by a cell value */

/* Everything that turns "this value, in this column" into a filter goes
   through here — the `f` keybind, the row menu's Filter to…/Exclude, and
   the value picker's single-value case — so the three can't drift apart on
   what an empty cell means or how a value gets escaped.

   `=value` is the spelling on purpose: it round-trips any value the box can
   hold, including one containing a `|` (parseFilter matches the `=` prefix
   before it ever looks for the any-of separator). What it can't hold is a
   value whose own edges are whitespace — the box trims — which is why
   valueFilterText() reports null rather than quietly filtering on the
   trimmed text, and the picker routes those through the filter tree. */
function valueFilterText(v) {
  if (v == null || v === '') return '""';
  const s = String(v);
  return s === s.trim() && !/[\r\n]/.test(s) ? '=' + s : null;
}

function valueExcludeText(v) {
  if (v == null || v === '') return '*'; // "not empty" is the exclusion of empty
  const s = String(v);
  return s === s.trim() && !/[\r\n]/.test(s) ? '!=' + s : null;
}

/* Writes a raw filter string into a column's header box and the state
   behind it, keeping the visible input in step without a full renderHead()
   (which would drop the cell selection the caller may still be acting on). */
function setColumnFilter(name, raw) {
  if (raw) S.filters[name] = raw; else delete S.filters[name];
  const inp = document.querySelector(`.fcell input[data-col="${CSS.escape(name)}"]`);
  if (inp) { inp.value = raw || ''; inp.classList.toggle('active', !!raw); }
}

const displayValue = (v) => (v == null || v === '' ? '(empty)' : String(v));
const ellipsize = (s, n = 42) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

/* `only: true` is the Shift+F half of the pair — filter to this value and
   drop everything else that was narrowing the view. It's clearAllFilters
   with a seed rather than its own reset, because the carve-outs that make
   clearing correct (the timeframe filter survives; grouping is stashed, not
   lost) are exactly the ones a second implementation would forget. */
async function filterByValue(column, value, { only = false, exclude = false } = {}) {
  const raw = exclude ? valueExcludeText(value) : valueFilterText(value);
  if (raw === null) {
    toast(`"${ellipsize(String(value))}" starts or ends with whitespace — use Filter by values… to select it`, 5000);
    return;
  }
  const shown = ellipsize(displayValue(value));
  if (only) {
    await clearAllFilters({ column, raw });
    toast(`Filtered ${column} ${exclude ? '≠' : '='} ${shown} · other filters cleared`);
    return;
  }
  setColumnFilter(column, raw);
  await rebuildView();
  toast(`Filtered ${column} ${exclude ? '≠' : '='} ${shown}`);
}

/* Reuses the same single-cell selection a plain click already commits to
   S.cellRange (see setCellRange above) — takes the top-left cell of
   whatever's selected and filters that column to its value, exactly as if
   "=value" had been typed into the header filter by hand. */
function selectedCellTarget() {
  if (!S.cellRange) return null;
  const column = visibleCols()[S.cellRange.c0];
  const r = rowAt(S.cellRange.r0);
  if (!column || !r) return null;
  return { column, value: r.cells[S.columns.findIndex((c) => c.name === column)] };
}

async function filterBySelectedCell({ only = false } = {}) {
  if (!S.cellRange) { toast('Click a cell first'); return; }
  const target = selectedCellTarget();
  if (!target) { toast('Row not loaded yet'); return; }
  await filterByValue(target.column, target.value, { only });
}

function currentSourceHasFts() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return !!(src && src.has_fts);
}

function updateSearchHint() {
  const hasFts = currentSourceHasFts();
  if (S.searchMode === 'regex') $('searchMode').textContent = 'regex · full scan, slow on large sources';
  else if (S.searchMode === 'advanced') $('searchMode').textContent = hasFts ? 'advanced · full-text' : 'advanced · substring chain';
  else $('searchMode').textContent = 'substring';
}

/* Generic multi-term AND/OR/NOT chip editor — shared by the toolbar's
   Advanced search mode and the Search-all-tables modal, which both need
   "a growable list of {term, connector, exclude} chips" but differ in
   what "changed" means (rebuild the grid view vs. re-run a cross-table
   count query) and how eagerly to react to keystrokes. */
function renderTermChips(container, terms, onChange, opts = {}) {
  const commit = opts.debounceMs ? debounce(onChange, opts.debounceMs) : onChange;
  container.replaceChildren();
  terms.forEach((t, i) => {
    const chip = el('div', 'adv-chip');
    if (i > 0) {
      const conn = el('select', 'adv-conn');
      for (const c of ['AND', 'OR']) {
        const optEl = document.createElement('option');
        optEl.value = c;
        optEl.textContent = c;
        if (t.connector === c) optEl.selected = true;
        conn.append(optEl);
      }
      conn.onchange = () => { t.connector = conn.value; onChange(); };
      chip.append(conn);
    }
    const notBtn = el('button', 'btn ghost adv-not' + (t.exclude ? ' active' : ''), 'NOT');
    notBtn.title = 'Exclude this term';
    notBtn.onclick = () => {
      t.exclude = !t.exclude;
      notBtn.classList.toggle('active', t.exclude);
      onChange();
    };
    chip.append(notBtn);
    const inp = el('input');
    inp.value = t.term;
    inp.placeholder = 'term';
    // opts.liveInput === false: update the term as the analyst types but
    // don't run anything expensive off every keystroke — Enter or an
    // explicit action (see the Search button in openSearchAllModal) is
    // what actually commits it. Default stays live (the main grid's
    // Advanced search wants immediate feedback as you type).
    inp.oninput = () => { t.term = inp.value; if (opts.liveInput !== false) commit(); };
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); onChange(); if (opts.blurTarget) opts.blurTarget.focus(); }
    };
    if (opts.onInputBlur) inp.addEventListener('blur', opts.onInputBlur);
    chip.append(inp);
    const rm = el('button', 'btn ghost adv-rm', '✕');
    rm.title = 'Remove term';
    rm.onclick = () => { terms.splice(i, 1); renderTermChips(container, terms, onChange, opts); onChange(); };
    chip.append(rm);
    container.append(chip);
  });
  const add = el('button', 'btn ghost', '+ term');
  add.onclick = () => {
    terms.push({ term: '', connector: 'AND', exclude: false });
    renderTermChips(container, terms, onChange, opts);
    const inputs = container.querySelectorAll('input');
    inputs[inputs.length - 1]?.focus();
  };
  container.append(add);
}

function renderAdvancedChips() {
  renderTermChips($('advancedSearchBar'), S.searchTerms, () => rebuildView({ keepScroll: false }), {
    debounceMs: 220, blurTarget: $('body'), onInputBlur: collapseSearchIfEmpty,
  });
}

function setSearchMode(mode) {
  S.searchMode = mode;
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  if (mode === 'advanced') {
    if (!S.searchTerms.length) S.searchTerms.push({ term: '', connector: 'AND', exclude: false });
    renderAdvancedChips();
  }
  syncSearchExpansion(true);
  updateSearchHint();
  return rebuildView({ keepScroll: false });
}

document.querySelectorAll('#searchModeToggle button').forEach((b) => {
  b.onclick = () => setSearchMode(b.dataset.mode);
});

$('search').oninput = (e) => { S.search = e.target.value; syncSearchExpansion(true); rebuildSoon(); };
$('search').onkeydown = (e) => {
  if (e.key === 'Escape') { e.target.value = ''; S.search = ''; rebuildView({ keepScroll: false }); $('body').focus(); }
  if (e.key === 'Enter') { rebuildView({ keepScroll: false }); $('body').focus(); }
};
$('search').addEventListener('blur', collapseSearchIfEmpty);

/* --------------------------------------------------------- collapsible search */

/* Collapsed by default: a bare icon button in place of the box, per the
   spec that Contains/Regex/Advanced mode buttons shouldn't be visible
   clutter when there's nothing to search for yet. "Expanded" is a UI
   state independent of content — clicking the icon or pressing / opens
   the box (and mode buttons) even before anything's typed, so the user
   can pick a mode first; it only auto-collapses again once both the box
   loses focus AND there's no content left to show. */
function hasSearchContent() {
  if (S.searchMode === 'advanced') return S.searchTerms.some((t) => (t.term || '').trim());
  return !!S.search;
}

function syncSearchExpansion(forceExpand = false) {
  const expanded = forceExpand || hasSearchContent();
  $('btnSearchToggle').hidden = expanded;
  $('searchModeToggle').hidden = !expanded;
  $('searchWrap').hidden = !expanded || S.searchMode === 'advanced';
  $('advancedSearchBar').hidden = !expanded || S.searchMode !== 'advanced';
}

function expandSearch() {
  syncSearchExpansion(true);
  if (S.searchMode === 'advanced') {
    const i = $('advancedSearchBar').querySelector('input');
    if (i) { i.focus(); i.select(); }
  } else {
    $('search').focus(); $('search').select();
  }
}

function collapseSearchIfEmpty() {
  setTimeout(() => {
    const active = document.activeElement;
    const within = active && (active.closest('.search-wrap') || active.closest('#searchModeToggle')
      || active.closest('#advancedSearchBar') || active === $('btnSearchToggle'));
    if (within || hasSearchContent()) return;
    syncSearchExpansion(false);
  }, 0);
}

$('btnSearchToggle').onclick = expandSearch;

$('btnFilters').onclick = () => dropdownMenu($('btnFilters'), [
  { label: 'Filter builder…', onclick: openFilterBuilder },
  { label: 'Saved filters…', onclick: openSavedFiltersModal },
]);
$('btnTimeRange').onclick = openTimeRangeModal;
$('btnSettings').onclick = openSettings;
$('btnSearchAll').onclick = openSearchAllModal;

/* ------------------------------------------------------------- sidebar */

/* Persistent, collapsible equivalent of the old "jump to a table" dropdown
   (openTabJumpMenu, removed) — every table in the case, whether or not it
   currently has a tab open on the horizontal strip up top (which only
   scrolls horizontally — see .tabs' overflow-x:auto and #app > *
   { min-width: 0 } above, which stop a long tab strip from pushing the
   rest of the header off-screen), so with many tables or long names the
   one you want may not even be scrolled into view. This is the reason it
   exists as a *persistent* panel rather than staying a menu you reopen
   for every click: a directory import can add 30+ tabs in one pass (each
   ingest auto-opens its tab), and reopening a dropdown that many times
   over doesn't scale the way clicking down a standing list does.

   Three sections. Open/Closed match the tab strip's own "shown vs not"
   rule (an errored source is always shown, bucketed with Open rather than
   a section of its own); Pages is the same standing list for the *other*
   strip — SQL, Timeline and any plugin tabs — and exists for the same
   reason the table sections do, since that strip scrolls too once a few
   plugin tabs are installed or the divider is dragged in. Rows reuse the
   dropdown's own .menu-item/.menu-item-action classes (see style.css)
   rather than a parallel set of near-identical ones. Filtered by
   S.sidebarFilter — a plain client-side substring match over both kinds,
   not a network round trip, since S.sources/S.pluginTabs are already in
   memory and this can be retyped on every keystroke.

   Open rows also get ▲/▼/✕, and page rows ▲/▼ — the same reorder/close
   each strip offers via drag-and-drop and its own ✕, kept here for when a
   strip is scrolled out of view or a standing list is just easier to act
   on. */
function renderSidebar() {
  const list = $('sidebarList');
  list.replaceChildren();
  const q = S.sidebarFilter.trim().toLowerCase();
  const match = (s) => !q || s.name.toLowerCase().includes(q);
  const openSrcs = openTabsSorted().filter(match);
  const closedSrcs = S.sources.filter((s) => !s.error && !s.is_open).filter(match);
  // Indices for ▲/▼ come from the *unfiltered* order, so the arrows move a
  // page where it actually is rather than where the filter makes it look.
  const pages = pageTabsSorted();
  const shownPages = pages.filter((t) => !q || t.label.toLowerCase().includes(q));
  if (!openSrcs.length && !closedSrcs.length) {
    list.append(el('div', 'note-status', q ? 'No matching tables.' : 'No tables in this case yet.'));
  }
  if (openSrcs.length) {
    list.append(el('div', 'menu-header', 'Open'));
    openSrcs.forEach((s, i) => list.append(sidebarRow(s, { open: true, index: i, total: openSrcs.length })));
  }
  if (closedSrcs.length) {
    list.append(el('div', 'menu-header', 'Closed'));
    for (const s of closedSrcs) list.append(sidebarRow(s, { open: false }));
  }
  if (shownPages.length) {
    list.append(el('div', 'menu-header', 'Pages'));
    for (const t of shownPages) list.append(pageSidebarRow(t, pages.indexOf(t), pages.length));
  }
}

/* A page tab's row: click to show it, ▲/▼ or drag to reorder. No ✕ —
   unlike a table tab, a page tab isn't something you can close and reopen
   (SQL and Timeline are always there; a plugin tab comes and goes with its
   plugin's checkbox in Settings), so there's nothing for one to do. */
function pageSidebarRow(t, index, total) {
  const row = el('div', 'sidebar-row' + (S.activeTab === t.key ? ' active' : ''));
  const label = el('button', 'menu-item', t.label);
  label.title = t.title || t.label;
  label.onclick = t.show;
  row.append(label);
  const acts = el('div', 'sidebar-row-actions');
  const up = el('button', 'menu-item-action', '▲');
  up.title = 'Move earlier';
  up.disabled = index === 0;
  up.onclick = () => movePageTab(t.key, -1);
  const down = el('button', 'menu-item-action', '▼');
  down.title = 'Move later';
  down.disabled = index === total - 1;
  down.onclick = () => movePageTab(t.key, 1);
  acts.append(up, down);
  row.append(acts);
  wireDragReorder(row, t.key, {
    containerSelector: '#sidebarList',
    rowSelector: '.sidebar-row',
    horizontal: false,
    currentIds: () => pageTabsSorted().map((x) => x.key),
    onReorder: setPageTabOrder,
  });
  return row;
}

function sidebarRow(s, { open, index, total }) {
  // S.activeTab !== 'grid' means SQL/Timeline is showing — S.sourceId is
  // still the last-open source in that state (nothing clears it), but
  // nothing in the sidebar represents SQL/Timeline, so no row should read
  // as active; #tabSql/#tabTimeline carry that highlight instead.
  const active = open && s.id === S.sourceId && S.activeTab === 'grid';
  const row = el('div', 'sidebar-row' + (active ? ' active' : ''));
  const label = el('button', 'menu-item', (s.is_merge ? '⛓ ' : '') + sourceLabel(s) + (s.error ? ' ⚠' : ''));
  label.disabled = !!s.error;
  if (s.error) label.title = s.error;
  label.onclick = open ? () => openSource(s.id) : async () => {
    await post(`/api/source/${s.id}/open`, { open: true });
    await loadSources(s.id);
  };
  row.append(label);
  if (!s.error) {
    // Same menu the tab strip's right-click opens, on the table's name in
    // the sidebar — including for a closed table, which openTableMenu opens
    // first (its panels all read the live S.layout/S.columns).
    row.oncontextmenu = (e) => { e.preventDefault(); openTableMenu(s.id); };
    label.title = sourceTitle(s, 'Right-click for the table menu');
    row.append(el('span', 'sidebar-row-count', s.row_count.toLocaleString()));
  }
  if (open) {
    const acts = el('div', 'sidebar-row-actions');
    const up = el('button', 'menu-item-action', '▲');
    up.title = 'Move earlier';
    up.disabled = index === 0;
    up.onclick = () => moveTab(s.id, -1);
    const down = el('button', 'menu-item-action', '▼');
    down.title = 'Move later';
    down.disabled = index === total - 1;
    down.onclick = () => moveTab(s.id, 1);
    const x = el('button', 'menu-item-action', '✕');
    x.title = 'Close tab — stays in this case, reopen it from here';
    x.onclick = async () => { await closeTab(s); };
    acts.append(up, down, x);
    row.append(acts);
    wireSidebarRowDrag(row, s.id);
  }
  return row;
}

$('sidebarFilter').oninput = () => { S.sidebarFilter = $('sidebarFilter').value; renderSidebar(); };

/* Collapse state persisted the same way S.keymap/S.appearance are
   (localStorage, not workspace/ — this is a per-browser UI preference,
   not case- or cross-case-workflow state). #btnTabJump is the same button
   that used to open the dropdown — same position, same "where users
   already look" reasoning — repurposed into a plain visibility toggle. */
const SIDEBAR_KEY = 'winnow.sidebar';
function setSidebarVisible(visible) {
  $('sidebar').hidden = !visible;
  $('btnTabJump').textContent = visible ? '◀' : '▶';
  $('btnTabJump').setAttribute('aria-pressed', String(visible));
  $('btnTabJump').title = visible ? 'Hide the table list' : 'Show every table in the case';
  localStorage.setItem(SIDEBAR_KEY, JSON.stringify({ collapsed: !visible }));
}
function initSidebar() {
  let collapsed = false;
  try { collapsed = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '{}').collapsed ?? false; } catch { /* default: visible */ }
  setSidebarVisible(!collapsed);
}
$('btnTabJump').onclick = () => setSidebarVisible($('sidebar').hidden);

$('btnSession').onclick = () => dropdownMenu($('btnSession'), [
  { label: 'Import…', onclick: openImportModal },
  { label: 'Merge sources…', onclick: openMergeBuilder },
  { label: 'Tables…', onclick: openTablesManager },
  '-',
  { label: 'Export…', onclick: openExportModal },
  { label: 'Session (save/load)…', onclick: openSessionManager },
  '-',
  { label: 'Shut down Winnow…', onclick: shutdownWinnow },
]);
/* Row identity (source_id, rid) survives a view rebuild even though pos
   doesn't (see CLAUDE.md — positions are view-specific and get wiped on
   every rebuild). Capture whichever row was under the selection/cell-range/
   cursor before the rebuild so clearAllFilters can find that same row again
   afterward and re-center the grid on it instead of dropping the analyst
   back at row 0. */
function selectedRowAnchor() {
  let pos = -1;
  if (S.cellRange) pos = S.cellRange.r0;
  else if (selCount()) pos = selFirst();
  else if (S.cursor >= 0) pos = S.cursor;
  const r = pos >= 0 ? rowAt(pos) : null;
  return r ? { source_id: r.source_id, rid: r.rid } : null;
}

async function recenterOnRow(anchor) {
  if (!anchor || !S.view || S.groupByCols.length) return;
  let pos;
  try {
    ({ pos } = await api(`/api/row_position?view_id=${S.view.view_id}&source_id=${anchor.source_id}&rid=${anchor.rid}`));
  } catch { return; }
  if (pos == null) return;
  S.cursor = pos;
  // Centers within the row band below the sticky header: the visible band
  // is [scrollTop + headH(), scrollTop + clientHeight], hence the extra
  // headH()/2 term.
  $('body').scrollTop = rScroll($('body'), S.view.row_count,
    pos * ROW_H + ROW_H / 2 + headH() / 2 - $('body').clientHeight / 2, headH());
  render();
}

/* `seed` ({column, raw}) is the one carve-out: "filter to this value and
   nothing else" (Shift+F, the row menu's "…only") is precisely this reset
   plus one filter, and spelling the reset out a second time is how the
   timeframe carve-out below gets forgotten in the copy. */
async function clearAllFilters(seed = null) {
  // Deliberately doesn't touch S.timeRange — the timeframe filter is meant
  // to survive exactly this ("apply/clear filters shouldn't lose my
  // timeframe"), same as it survives applyPreset() and a tab switch. Use
  // the Timeframe filter's own "Clear" button, or toggleTimeRange, for that.
  const anchor = selectedRowAnchor();
  // Grouping goes with the filters (stashed first, so toggleGrouping can
  // bring it back) — clearing "the filters" should land on the plain
  // table, not on an empty filter under the old grouping.
  if (S.groupByCols.length) {
    S.lastGroupBy = { cols: [...S.groupByCols], sort: S.groupSort, dir: S.groupSortDir };
    await dropGrouping();
  }
  S.filters = seed ? { [seed.column]: seed.raw } : {};
  S.search = ''; S.tagFilter = []; S.searchTerms = [];
  S.filterTree = { type: 'group', op: 'AND', children: [] };
  updateFiltersButton();
  $('search').value = '';
  renderHead(); renderTagRibbon();
  if (S.searchMode !== 'contains') await setSearchMode('contains'); // also rebuilds the view
  else await rebuildView({ keepScroll: false });
  syncSearchExpansion(false);
  await recenterOnRow(anchor);
}
// Wrapped, not passed directly: an onclick handler is called with the
// MouseEvent, which would arrive as `seed`.
$('btnReset').onclick = () => clearAllFilters();
// The empty-case state's one useful next action, right where the eye lands —
// the same openImportModal the Session menu's "Import…" entry opens.
$('emptyImportBtn').onclick = () => openImportModal();
function openExportModal() {
  modal('Export', (b) => {
    if (S.view) {
      b.append(el('p', null, 'Exports what you are looking at now — current filters, sort and search — with Line, Tags and Note columns added in front.'));
      const acts = el('div', 'row-actions');
      const all = el('button', 'btn', 'Export current view');
      all.onclick = () => { window.location = `/api/export?view_id=${S.view.view_id}`; $('modal').hidden = true; };
      const tagged = el('button', 'btn', 'Export tagged rows only');
      tagged.onclick = () => { window.location = `/api/export?view_id=${S.view.view_id}&tagged_only=true`; $('modal').hidden = true; };
      acts.append(all, tagged);
      b.append(acts);
    }
    b.append(el('p', null, 'Exports every tagged row from every table in this case — not just the one open now — one worksheet per table.'));
    const xlsxActs = el('div', 'row-actions');
    const xlsx = el('button', 'btn', 'Export tagged rows from all tables (.xlsx)');
    xlsx.onclick = () => { window.location = '/api/export/tagged_xlsx'; $('modal').hidden = true; };
    xlsxActs.append(xlsx);
    b.append(xlsxActs);
  });
}
/* ------------------------------------------------------------- detail pane */

/* Dock side + size are a browser-local UI preference, not case data — same
   rationale as the keymap/appearance blocks below (not saved in
   workspace/, doesn't travel with the case). */
const DETAIL_KEY = 'winnow.detail';
function loadDetailPrefs() {
  try { return { dock: 'bottom', size: null, ...JSON.parse(localStorage.getItem(DETAIL_KEY) || '{}') }; }
  catch { return { dock: 'bottom', size: null }; }
}
S.detailPrefs = loadDetailPrefs();
function saveDetailPrefs() { localStorage.setItem(DETAIL_KEY, JSON.stringify(S.detailPrefs)); }

function applyDetailPrefs() {
  const area = $('mainArea');
  const d = $('detail');
  area.dataset.dock = S.detailPrefs.dock;
  if (S.detailPrefs.size) {
    if (S.detailPrefs.dock === 'right') { d.style.width = S.detailPrefs.size + 'px'; d.style.height = ''; }
    else { d.style.height = S.detailPrefs.size + 'px'; d.style.width = ''; }
  } else {
    d.style.width = ''; d.style.height = '';
  }
  $('btnDetailDock').title = S.detailPrefs.dock === 'right' ? 'Dock to the bottom' : 'Dock to the right';
}
applyDetailPrefs();

function toggleDetailPane() {
  const d = $('detail');
  if (d.hidden) { if (S.cursor >= 0 && rowAt(S.cursor)) showDetail(S.cursor); }
  else { d.hidden = true; $('detailResize').hidden = true; }
}

$('btnDetailDock').onclick = () => {
  S.detailPrefs.dock = S.detailPrefs.dock === 'right' ? 'bottom' : 'right';
  S.detailPrefs.size = null; // switching axis — the old size doesn't mean anything on the new one
  applyDetailPrefs();
  saveDetailPrefs();
};

$('detailResize').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const dock = S.detailPrefs.dock;
  const d = $('detail');
  const handle = $('detailResize');
  const startPos = dock === 'right' ? e.clientX : e.clientY;
  const startSize = dock === 'right' ? d.getBoundingClientRect().width : d.getBoundingClientRect().height;
  handle.classList.add('dragging');
  const move = (ev) => {
    const pos = dock === 'right' ? ev.clientX : ev.clientY;
    // Dragged inward from the edge the pane is docked against — right dock's
    // handle sits on its left edge, bottom dock's on its top edge, so in
    // both cases moving the handle *toward* that edge should *grow* the pane.
    const delta = startPos - pos;
    const size = Math.max(200, startSize + delta);
    if (dock === 'right') d.style.width = size + 'px';
    else d.style.height = size + 'px';
    S.detailPrefs.size = size;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    handle.classList.remove('dragging');
    saveDetailPrefs();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

$('btnCloseDetail').onclick = () => { $('detail').hidden = true; $('detailResize').hidden = true; };
$('btnCopyRow').onclick = () => {
  const r = rowAt(S.cursor);
  if (!r) return;
  const text = S.columns.map((c, i) => `${c.name}: ${r.cells[i] ?? ''}`).join('\n');
  writeClipboardText(Promise.resolve(text), 'Row copied');
};
$('noteInput').oninput = () => { $('noteStatus').textContent = 'Saving…'; saveNote(); };

/* ------------------------------------------------------ keyboard shortcuts */

/* Navigation/action keys are rebindable and persisted in localStorage — a
   per-machine preference, not tied to the case file. Tag hotkeys (1-9) stay
   governed entirely by tag_defs.hotkey via the tag editor; Escape stays
   hardcoded (universal dismiss key, not worth letting users lock themselves
   out of). Neither is part of this keymap. */
const DEFAULT_KEYMAP = {
  moveDown: ['ArrowDown', 'j'],
  moveUp: ['ArrowUp', 'k'],
  pageDown: ['PageDown'],
  pageUp: ['PageUp'],
  jumpFirst: ['g'],
  jumpLast: ['G'],
  focusSearch: ['/'],
  // No default: `f` is worth more as "filter to the value I'm looking at"
  // (below) than as "focus the first column's filter box", which is a click
  // away and was the less-used of the two. Still bindable in Settings.
  focusFilter: [],
  focusNote: ['n'],
  openSettings: ['?'],
  resetColumnWidths: ['0'],
  autofitColumnWidths: ['='],
  cyclePrevFilter: ['['],
  cycleNextFilter: [']'],
  filterBySelectedCell: ['f'],
  filterBySelectedCellOnly: ['F'],
  clearFilters: ['c'],
  openTables: ['t'],
  openTableMenu: ['C'],
  openSearchAll: ['s'],
  toggleDetail: ['d'],
  dropGrouping: ['x'],
  saveDefaultLayout: ['L'],
  toggleTimeRange: ['T'],
  openTimeRange: ['R'],
  toggleGrouping: ['X'],
  openFilterSql: ['Q'],
  openJumpTs: ['J'],
  repeatJumpTs: ['.'],
};
const ACTION_LABELS = {
  moveDown: 'Move down', moveUp: 'Move up',
  pageDown: 'Page down', pageUp: 'Page up',
  jumpFirst: 'Jump to first row', jumpLast: 'Jump to last row',
  focusSearch: 'Focus search box', focusFilter: 'Focus first column filter',
  focusNote: 'Focus note field', openSettings: 'Open settings (keyboard shortcuts, filter syntax)',
  resetColumnWidths: 'Reset all column widths to default',
  autofitColumnWidths: 'Autofit all column widths to content',
  cyclePrevFilter: 'Previous saved filter', cycleNextFilter: 'Next saved filter',
  filterBySelectedCell: "Filter by selected cell's value",
  filterBySelectedCellOnly: "Filter by selected cell's value, dropping every other filter",
  clearFilters: 'Clear all filters, search and tag filter',
  openTables: 'Open Tables manager',
  openTableMenu: 'Open the table menu (columns, value dropdowns) — also right-click a tab',
  openSearchAll: 'Search all tables',
  toggleDetail: 'Open/close the detail pane',
  dropGrouping: 'Drop all grouping, restore column order',
  saveDefaultLayout: "Save this column order/visibility as the default for this header set",
  toggleTimeRange: 'Toggle the timeframe filter on/off',
  openTimeRange: 'Open the timeframe filter (set column/range)',
  toggleGrouping: 'Toggle grouping off/on (remembers the last grouping)',
  openFilterSql: 'Open the current filter as a query in the SQL pane',
  openJumpTs: 'Jump to timestamp… (set the moment and column)',
  repeatJumpTs: 'Jump again to the saved timestamp (works across tables)',
};

/* Stored keymaps are a merge over the defaults, which means a returning
   analyst's localStorage silently outranks every later change to
   DEFAULT_KEYMAP — including a rename, which would leave a binding pointing
   at an action that no longer has a handler (matchAction would resolve the
   key and nothing would happen). So the stored map is migrated on load:
   entries for actions that no longer exist are carried to their replacement
   and dropped, and a *default* binding a change is meant to move is moved.
   A binding the analyst chose themselves is never touched — the marker
   below is what keeps each migration one-shot rather than fighting them
   over it on every load. */
const KEYMAP_VERSION_KEY = 'winnow.keymap.v';
const KEYMAP_VERSION = 1;

const KEYMAP_MIGRATIONS = [
  // v1 (2026-08): the column chooser grew into the table menu, and `f`
  // moved from "focus the first filter box" to "filter by this value"
  // (Shift+F now doing that *and* clearing the other filters).
  (map) => {
    if (map.openColumns) map.openTableMenu = map.openColumns;
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    if (wasDefault('focusFilter', ['f']) && wasDefault('filterBySelectedCell', ['F'])) {
      map.focusFilter = [];
      map.filterBySelectedCell = ['f'];
      map.filterBySelectedCellOnly = ['F'];
    }
  },
];

/* A *deep* copy: the settings UI's "+ key"/"✕" handlers mutate the key
   arrays in place, and a shallow `{...DEFAULT_KEYMAP}` hands them
   DEFAULT_KEYMAP's own arrays to mutate. That's how binding a key on a
   fresh profile used to edit the defaults themselves — after which "Reset
   to defaults" copied the polluted defaults back and appeared to do
   nothing. */
const defaultKeymap = () =>
  Object.fromEntries(Object.entries(DEFAULT_KEYMAP).map(([action, keys]) => [action, [...keys]]));

function loadKeymap() {
  let stored;
  try { stored = JSON.parse(localStorage.getItem('winnow.keymap') || '{}'); }
  catch { return defaultKeymap(); }
  if (!stored || typeof stored !== 'object') return defaultKeymap();

  let from = 0;
  try { from = Number(localStorage.getItem(KEYMAP_VERSION_KEY)) || 0; } catch { /* treat as unmigrated */ }
  const pending = KEYMAP_MIGRATIONS.slice(from);
  for (const migrate of pending) migrate(stored);

  // Actions the app no longer has (renamed, removed) would otherwise keep
  // swallowing their key forever, since matchAction scans the stored map,
  // not the defaults.
  const map = defaultKeymap();
  for (const [action, keys] of Object.entries(stored)) {
    if (action in DEFAULT_KEYMAP && Array.isArray(keys)) map[action] = keys;
  }
  if (pending.length) {
    try {
      localStorage.setItem('winnow.keymap', JSON.stringify(map));
      localStorage.setItem(KEYMAP_VERSION_KEY, String(KEYMAP_VERSION));
    } catch { /* a full/blocked localStorage just means it migrates again next load */ }
  }
  return map;
}
function saveKeymap() {
  localStorage.setItem('winnow.keymap', JSON.stringify(S.keymap));
  localStorage.setItem(KEYMAP_VERSION_KEY, String(KEYMAP_VERSION));
}

/* A binding is stored as e.key, optionally prefixed with held modifiers in
   a fixed order: 'Ctrl+Alt+Meta+Shift+<key>'. Shift never appears for a
   printable key — e.key already arrives shifted (Shift+g is 'G'), so 'G'
   *is* the capital-letter binding — and appears for a non-printable key
   only when the binding asked for it, which is how an unprefixed
   'ArrowDown' keeps matching Shift+ArrowDown (the move handlers read
   e.shiftKey themselves to extend the selection). Returns null for a
   modifier pressed on its own, which is what lets the capture UI wait for
   the rest of a combination instead of binding "Control". */
const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock', 'ScrollLock']);
function keySpecFromEvent(e) {
  if (MODIFIER_KEYS.has(e.key)) return null;
  let mods = '';
  if (e.ctrlKey) mods += 'Ctrl+';
  if (e.altKey) mods += 'Alt+';
  if (e.metaKey) mods += 'Meta+';
  if (e.shiftKey && e.key.length > 1) mods += 'Shift+';
  return mods + e.key;
}

function matchAction(e) {
  const spec = keySpecFromEvent(e);
  if (spec == null) return null;
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (keys.includes(spec)) return action;
  }
  // Shift on a non-printable key falls back to the unshifted binding (an
  // explicit 'Shift+F2' binding above already won if there was one) — this
  // is what keeps Shift+ArrowDown reaching moveDown to extend the
  // selection. Modifiers other than Shift never fall back: Alt+j is not a
  // request to move the cursor.
  const bare = spec.replace('Shift+', '');
  if (bare !== spec) {
    for (const [action, keys] of Object.entries(S.keymap)) {
      if (keys.includes(bare)) return action;
    }
  }
  return null;
}

/* Returns a human-readable description of what already owns `key`, or null
   if it's free. Checked against other keymap actions, tag hotkeys (which
   can change independently at any time via the tag editor), Escape, and
   the hardcoded modifier shortcuts the keydown listener handles before
   the keymap (copy, tag undo, Alt+digit tab switching). */
function findKeyConflict(key, currentAction) {
  if (key === 'Escape') return 'the always-on close/clear action';
  if (/^[1-9]$/.test(key)) {
    const t = S.tags.find((x) => x.hotkey === key);
    return `the "${t ? t.name : 'tag'}" tag hotkey`;
  }
  if (/^(Ctrl|Meta)\+(c|C)$/.test(key)) return 'the copy shortcut';
  if (/^(Ctrl|Meta)\+z$/.test(key)) return 'the tag-undo shortcut';
  if (/^Alt\+[0-9]$/.test(key)) return 'tab switching (Alt+1–0)';
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (action !== currentAction && keys.includes(key)) return ACTION_LABELS[action] || action;
  }
  return null;
}

/* The shortcuts that still mean something when the grid isn't the active
   tab — everything else moves a cursor, edits the grid's view spec or
   tags its rows, none of which the analyst can see from the SQL, Timeline
   or a plugin tab. They used to fire anyway: a tag hotkey pressed on the
   SQL pane silently tagged whatever was selected in the grid behind it,
   which the tag ribbon at least hinted at before the toolbar started
   hiding itself there (see syncTabChrome). */
const TAB_AGNOSTIC_ACTIONS = new Set(['openSettings', 'openTables', 'openSearchAll']);

const ACTION_HANDLERS = {
  moveDown: (e, pageRows) => moveCursor(S.cursor + 1, e.shiftKey),
  moveUp: (e, pageRows) => moveCursor(S.cursor - 1, e.shiftKey),
  pageDown: (e, pageRows) => moveCursor(S.cursor + pageRows, e.shiftKey),
  pageUp: (e, pageRows) => moveCursor(S.cursor - pageRows, e.shiftKey),
  jumpFirst: () => moveCursor(0, false),
  jumpLast: () => moveCursor(Math.max(0, gridRowCount() - 1), false),
  focusSearch: () => expandSearch(),
  focusFilter: () => { const i = document.querySelector('.fcell input'); if (i) { i.focus(); i.select(); } },
  focusNote: () => { if (!$('detail').hidden) $('noteInput').focus(); },
  openSettings: () => openSettings(),
  resetColumnWidths: () => resetAllColumnWidths(),
  autofitColumnWidths: () => autofitAllColumnWidths(),
  cyclePrevFilter: () => cycleSavedFilter(-1),
  cycleNextFilter: () => cycleSavedFilter(1),
  filterBySelectedCell: () => filterBySelectedCell(),
  filterBySelectedCellOnly: () => filterBySelectedCell({ only: true }),
  clearFilters: () => clearAllFilters(),
  openTables: () => openTablesManager(),
  openTableMenu: () => openTableMenu(),
  toggleDetail: () => toggleDetailPane(),
  openSearchAll: () => openSearchAllModal(),
  dropGrouping: () => { if (S.groupByCols.length) dropGrouping(); },
  saveDefaultLayout: () => saveDefaultLayout(),
  toggleTimeRange: () => toggleTimeRange(),
  openTimeRange: () => openTimeRangeModal(),
  toggleGrouping: () => toggleGrouping(),
  openFilterSql: () => openFilterSqlTab(),
  openJumpTs: () => openJumpTsModal(),
  repeatJumpTs: () => doJumpTs(),
};

S.keymap = loadKeymap();
updateTimeRangeButton(); // reads S.keymap.toggleTimeRange for its tooltip — must come after the line above

/* ------------------------------------------------------------ appearance */

/* Persisted the same way S.keymap is — localStorage, not server-side. This
   is a browser-local UI preference (which look you like), not case data,
   so it doesn't belong in workspace/ (cross-case but still server-side
   bookkeeping) any more than the keymap does. index.html has a small
   blocking inline script that mirrors just enough of this (read
   localStorage, set data-style/data-theme/--accent) before first paint, so
   returning users don't see a flash of the default look; initAppearance()
   below re-applies the same values once app.js loads, which is harmless
   and idempotent. */
const APPEARANCE_KEY = 'winnow.appearance';
const STYLES = {
  panel:     { label: 'Panel',     desc: "Today's look.", defaultAccent: '#d2a04a', preview: ['#13161a', '#d2a04a'] },
  phosphor:  { label: 'Phosphor',  desc: 'Retro CRT terminal — glow, monospace chrome.', defaultAccent: '#39e881', preview: ['#060907', '#39e881'] },
  blueprint: { label: 'Blueprint', desc: 'Bold borders, hard offset shadows.', defaultAccent: '#ff6a1a', preview: ['#0c0d10', '#ff6a1a'] },
  studio:    { label: 'Studio',    desc: 'Rounded, soft shadows, calm motion.', defaultAccent: '#7c6cf6', preview: ['#111219', '#7c6cf6'] },
};
const ACCENT_PRESETS = ['#d2a04a', '#39e881', '#ff6a1a', '#7c6cf6', '#4a90d9', '#d9534f'];

function defaultAppearance() {
  return {
    style: 'panel', themeMode: 'dark', accent: STYLES.panel.defaultAccent, accentCustomized: false,
    density: 'comfortable', autofitMax: AUTOFIT_MAX_W_DEFAULT,
  };
}
function loadAppearance() {
  try {
    return { ...defaultAppearance(), ...JSON.parse(localStorage.getItem(APPEARANCE_KEY) || '{}') };
  } catch { return defaultAppearance(); }
}
function saveAppearance() { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(S.appearance)); }

function contrastFg(hex) {
  const c = hex.replace('#', '');
  const r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? '#14181d' : '#ffffff';
}
function resolveAutoTheme() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function paintTheme() {
  document.documentElement.setAttribute('data-theme', S.appearance.themeMode === 'auto' ? resolveAutoTheme() : S.appearance.themeMode);
}
function paintAccent() {
  document.documentElement.style.setProperty('--accent', S.appearance.accent);
  document.documentElement.style.setProperty('--accent-fg', contrastFg(S.appearance.accent));
}
/* Each style has a signature accent (the color it showed in the design
   review) — switching styles applies it, so the "vibe" actually changes,
   right up until the analyst manually picks a color themselves. After
   that, accentCustomized sticks and style switches stop touching it —
   their choice, not ours, from then on. */
function applyStyle(styleName) {
  S.appearance.style = styleName;
  document.documentElement.setAttribute('data-style', styleName);
  if (!S.appearance.accentCustomized) applyAccent(STYLES[styleName].defaultAccent, false);
  saveAppearance();
}
function applyThemeMode(mode) {
  S.appearance.themeMode = mode;
  paintTheme();
  saveAppearance();
}
function applyAccent(hex, fromUser = true) {
  S.appearance.accent = hex;
  if (fromUser) S.appearance.accentCustomized = true;
  paintAccent();
  saveAppearance();
}
/* Sets the module-level ROW_H (see top of file — every virtualized-grid
   position calculation reads it) and mirrors it into --row-h so the CSS
   .row height actually matches what the JS thinks it is; the two must never
   drift apart or scrolling math and painted rows disagree about where
   things are. Safe to call before any case is open — it just primes ROW_H
   and the CSS var, there's no grid to re-lay-out yet. */
function paintDensity() {
  ROW_H = S.appearance.density === 'compact' ? ROW_H_COMPACT : ROW_H_COMFORTABLE;
  document.documentElement.style.setProperty('--row-h', ROW_H + 'px');
}
/* Changing density mid-session means every already-fetched pixel position
   (spacerY height, scrollTop, the translateY render() applies) is stale —
   this recomputes them all against the new ROW_H, and re-anchors scroll on
   the row that was at the top rather than a raw pixel offset, so switching
   density doesn't fling the view to a random spot. */
function applyDensity(density) {
  S.appearance.density = density;
  saveAppearance();
  const body = $('body');
  // Read against the outgoing ROW_H (paintDensity hasn't run yet), written
  // back against the new one — so the anchor stays a row, not a pixel offset,
  // across a change that moves every pixel position in the grid.
  const total = gridRowCount;
  const topRow = S.view ? Math.floor(vScroll(body, total(), headH()) / ROW_H) : 0;
  paintDensity();
  if (!S.view) return;
  if (S.groupByCols.length) {
    rebuildGroupPrefix();
    $('spacerY').style.height = spacerPx(S.groupTotalRows) + 'px';
  } else {
    $('spacerY').style.height = spacerPx(S.view.row_count) + 'px';
  }
  body.scrollTop = rScroll(body, total(), topRow * ROW_H, headH());
  S.groupByCols.length ? renderGrouped() : render();
  drawRail();
}
function initAppearance() {
  S.appearance = loadAppearance();
  document.documentElement.setAttribute('data-style', S.appearance.style);
  paintTheme();
  paintAccent();
  paintDensity();
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (S.appearance.themeMode === 'auto') paintTheme();
    });
  }
}
initAppearance();
initSidebar();
wireFileDrop();

/* One collapsible section of the Settings modal. Returns the node the
   section's own code appends into — sections are otherwise written exactly
   as they were when they all ran into the modal body directly, which is
   also what keeps a section that fills itself later (buildPluginsPanel's
   async listing) landing inside its own section rather than at the end of
   the modal.

   Collapsed on open, every time: Settings had grown to seven sections and
   ~900px of scroll, so the thing you came for was rarely the thing you
   could see. Expansion state is deliberately not remembered — "open where
   I left it" and "collapsed by default" are different promises, and this
   one is the asked-for one. Several can be open at once; opening one
   doesn't close another.

   The header is a real <button> rather than a styled h4 so it's tabbable,
   keyboard-activatable and announces its aria-expanded state without any
   extra wiring. */
function settingsSection(parent, title, { open = false } = {}) {
  const wrap = el('div', 'settings-section');
  const head = el('button', 'settings-section-head');
  const caret = el('span', 'settings-section-caret', '▸');
  head.append(caret, el('span', 'settings-section-title', title));
  const body = el('div', 'settings-section-body');
  const paint = () => {
    head.setAttribute('aria-expanded', String(open));
    caret.textContent = open ? '▾' : '▸';
    body.hidden = !open;
  };
  head.onclick = () => { open = !open; paint(); };
  paint();
  wrap.append(head, body);
  parent.append(wrap);
  return body;
}

function openSettings() {
  modal('Settings', (b) => {
    const secLook = settingsSection(b, 'Appearance');
    secLook.append(el('p', null, 'Pick a look, then a theme, then (optionally) your own accent color. All three are saved on this machine.'));

    const styleGrid = el('div', 'appearance-styles');
    for (const [key, meta] of Object.entries(STYLES)) {
      const card = el('button', 'style-card');
      card.setAttribute('aria-pressed', String(S.appearance.style === key));
      const sw = el('div', 'style-swatch');
      sw.append(el('span', null, null), el('span', null, null));
      sw.children[0].style.background = meta.preview[0];
      sw.children[1].style.background = meta.preview[1];
      card.append(sw, el('span', 'style-name', meta.label), el('span', 'style-desc', meta.desc));
      card.onclick = () => {
        applyStyle(key);
        styleGrid.querySelectorAll('.style-card').forEach((c, i) => c.setAttribute('aria-pressed', String(Object.keys(STYLES)[i] === key)));
        accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === S.appearance.accent.toLowerCase())));
        customAccent.value = S.appearance.accent;
      };
      styleGrid.append(card);
    }
    secLook.append(styleGrid);

    secLook.append(el('div', 'settings-sub-label', 'Theme'));
    const themeSeg = el('div', 'segmented');
    for (const mode of ['dark', 'light', 'auto']) {
      const btn = el('button', null, mode[0].toUpperCase() + mode.slice(1));
      btn.setAttribute('aria-pressed', String(S.appearance.themeMode === mode));
      btn.onclick = () => {
        applyThemeMode(mode);
        themeSeg.querySelectorAll('button').forEach((b2) => b2.setAttribute('aria-pressed', String(b2.textContent.toLowerCase() === mode)));
      };
      themeSeg.append(btn);
    }
    secLook.append(themeSeg);

    secLook.append(el('div', 'settings-sub-label', 'Accent color'));
    const accentGrid = el('div', 'accent-picker');
    for (const hex of ACCENT_PRESETS) {
      const sw = el('button', 'accent-swatch');
      sw.dataset.accent = hex;
      sw.style.setProperty('--sw', hex);
      sw.setAttribute('aria-pressed', String(hex.toLowerCase() === S.appearance.accent.toLowerCase()));
      sw.onclick = () => {
        applyAccent(hex);
        accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === hex.toLowerCase())));
        customAccent.value = hex;
      };
      accentGrid.append(sw);
    }
    const customAccent = el('input');
    customAccent.type = 'color';
    customAccent.id = 'appearanceAccentCustom';
    customAccent.value = S.appearance.accent;
    customAccent.title = 'Custom accent color';
    customAccent.oninput = (e) => {
      applyAccent(e.target.value);
      accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === e.target.value.toLowerCase())));
    };
    accentGrid.append(customAccent);
    secLook.append(accentGrid);

    secLook.append(el('div', 'settings-sub-label', 'Row density'));
    const densitySeg = el('div', 'segmented');
    for (const [key, label] of [['comfortable', 'Comfortable'], ['compact', 'Compact']]) {
      const btn = el('button', null, label);
      btn.setAttribute('aria-pressed', String(S.appearance.density === key));
      btn.onclick = () => {
        applyDensity(key);
        densitySeg.querySelectorAll('button').forEach((b2, i) => b2.setAttribute('aria-pressed', String(['comfortable', 'compact'][i] === key)));
      };
      densitySeg.append(btn);
    }
    secLook.append(densitySeg);

    secLook.append(el('div', 'settings-sub-label', 'Autofit column width limit'));
    secLook.append(el('p', 'fb-help',
      'How wide fit-to-content (the ' + (S.keymap.autofitColumnWidths[0] || '=') + ' key, or double-clicking a column\u2019s '
      + 'right edge) may make one column. A column whose header name needs more than this still gets '
      + 'room for its name. Uncapped, a single base64 command line can make the grid enormously wide.'));
    const capRow = el('div', 'row-actions');
    const capInput = el('input');
    capInput.type = 'number';
    capInput.min = '80';
    capInput.max = '20000';
    capInput.step = '20';
    capInput.style.cssText = 'width:90px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
    capInput.value = String(autofitMaxWidth() || AUTOFIT_MAX_W_DEFAULT);
    capInput.disabled = !autofitMaxWidth();
    const commitCap = () => {
      const v = Math.max(80, Math.min(20000, Number(capInput.value) || AUTOFIT_MAX_W_DEFAULT));
      capInput.value = String(v);
      S.appearance.autofitMax = v;
      saveAppearance();
    };
    capInput.onchange = commitCap;
    const noCapLabel = el('label');
    noCapLabel.style.cssText = 'display:flex;align-items:center;gap:6px';
    const noCap = el('input');
    noCap.type = 'checkbox';
    noCap.checked = !autofitMaxWidth();
    noCap.onchange = () => {
      // 0 is the stored spelling of "no cap" — distinct from a missing key,
      // which loadAppearance fills in with the default.
      S.appearance.autofitMax = noCap.checked ? 0 : (Number(capInput.value) || AUTOFIT_MAX_W_DEFAULT);
      capInput.disabled = noCap.checked;
      saveAppearance();
    };
    noCapLabel.append(noCap, el('span', null, 'No limit'));
    capRow.append(capInput, el('span', 'count', 'px'), noCapLabel);
    secLook.append(capRow);

    const secKeys = settingsSection(b, 'Keyboard shortcuts');
    secKeys.append(el('p', null, 'Tag hotkeys (1–9) are set per-tag in Edit tags. Escape always clears the selection or closes a panel. '
      + '"+ key" waits for a full press — hold modifiers for a combination (e.g. Ctrl+Shift+K), or Shift+letter for a capital.'));
    const list = el('div', 'settings-keys');

    function renderList() {
      list.replaceChildren();
      for (const [action, keys] of Object.entries(S.keymap)) {
        const row = el('div', 'settings-key-row');
        row.append(el('span', 'settings-key-label', ACTION_LABELS[action] || action));
        const chips = el('div', 'settings-key-chips');
        keys.forEach((k, i) => {
          const chip = el('span', 'settings-key-chip');
          chip.append(el('kbd', null, k));
          const rm = el('button', 'btn ghost', '✕');
          rm.title = 'Remove this binding';
          rm.onclick = () => { keys.splice(i, 1); saveKeymap(); renderList(); };
          chip.append(rm);
          chips.append(chip);
        });
        const addBtn = el('button', 'btn ghost', '+ key');
        addBtn.onclick = () => {
          addBtn.textContent = 'Press a key…';
          addBtn.disabled = true;
          const done = () => {
            document.removeEventListener('keydown', capture, true);
            addBtn.disabled = false;
            addBtn.textContent = '+ key';
          };
          const capture = (ke) => {
            ke.preventDefault();
            ke.stopPropagation();
            if (ke.key === 'Escape') { done(); return; }
            const spec = keySpecFromEvent(ke);
            if (spec == null) {
              // A modifier on its own is the *start* of a combination, not
              // the binding — keep listening, showing what's held so far.
              // (This used to commit immediately, so pressing Ctrl for
              // Ctrl+K bound "Control" and combinations were impossible.)
              let mods = '';
              if (ke.ctrlKey) mods += 'Ctrl+';
              if (ke.altKey) mods += 'Alt+';
              if (ke.metaKey) mods += 'Meta+';
              if (ke.shiftKey) mods += 'Shift+';
              addBtn.textContent = mods ? mods + '…' : 'Press a key…';
              return;
            }
            done();
            const conflict = findKeyConflict(spec, action);
            if (conflict) { toast(`"${spec}" is already used by ${conflict}`, 4000); return; }
            keys.push(spec);
            saveKeymap();
            renderList();
          };
          document.addEventListener('keydown', capture, true);
        };
        chips.append(addBtn);
        row.append(chips);
        list.append(row);
      }
    }
    renderList();
    secKeys.append(list);

    const reset = el('button', 'btn ghost', 'Reset to defaults');
    reset.style.marginTop = '14px';
    reset.onclick = () => { S.keymap = defaultKeymap(); saveKeymap(); renderList(); };
    secKeys.append(reset);

    const fixedKeys = el('div', 'kv');
    fixedKeys.style.marginTop = '10px';
    fixedKeys.append(el('kbd', null, 'Shift + move keys'), el('span', null, 'Extend the selection'));
    fixedKeys.append(el('kbd', null, '1 – 9'), el('span', null, 'Toggle the tag with that hotkey on the selection'));
    fixedKeys.append(el('kbd', null, 'Shift + 1 – 9'), el('span', null, 'Apply that tag to every row in the current view'));
    fixedKeys.append(el('kbd', null, 'Alt + 1 – 0'), el('span', null, 'Switch tabs — 1 is the table you were last in, 2 – 0 the page tabs in strip order'));
    fixedKeys.append(el('kbd', null, 'Ctrl/⌘ + Z'), el('span', null, 'Undo the last tag applied or removed (repeat to keep stepping back)'));
    fixedKeys.append(el('kbd', null, 'Esc'), el('span', null, 'Clear selection, or close a panel'));
    fixedKeys.append(el('kbd', null, 'Right-click a row'), el('span', null, 'Tag it, filter to or exclude that cell’s value, copy'));
    fixedKeys.append(el('kbd', null, 'Right-click a tab'), el('span', null, 'That table’s menu — columns, value dropdowns, layout'));
    secKeys.append(fixedKeys);

    const secSyntax = settingsSection(b, 'Filter & search syntax');
    const filters = [
      ['svchost', 'contains'],
      ['!svchost', 'does not contain'],
      ['=4624', 'exact match'],
      ['^C:\\Users', 'starts with'],
      ['>1000', 'greater than (numeric columns)'],
      ['/regex/', 'regular expression'],
      ['""', 'empty'],
      ['*', 'not empty'],
      ['a|b|c', 'any of these values'],
    ];
    const f = el('div', 'kv');
    for (const [a, c] of filters) { f.append(el('kbd', null, a), el('span', null, c)); }
    secSyntax.append(el('p', null, 'Column filter row:'), f);
    secSyntax.append(el('p', null,
      'The ▾ on a filter box lists that column’s distinct values with counts — tick the ones to keep. '
      + `It appears automatically on tables under ${VALUE_FILTER_AUTO_MAX.toLocaleString()} rows (reading the values is a scan); `
      + 'the table menu (right-click a tab) turns it on or off per table or per column, and a row’s '
      + 'right-click menu can open it for any column.'));
    secSyntax.append(el('p', null,
      'Search box — Contains is always a true substring match; Regex is a full scan; Advanced supports '
      + 'multiple AND / OR / NOT terms and uses the FTS5 index when one was built at import.'));
    secSyntax.append(el('p', null,
      'The ⏱ Timeframe button pins a start/end range against one datetime column, or every datetime '
      + "column at once (catches a row via its Modified time even if its Created time was timestomped) — "
      + 'unlike the other filters, it stays applied when you clear filters, apply a saved filter, or switch tables.'));

    const secTs = settingsSection(b, 'Timestamps');
    secTs.append(el('p', null,
      'How datetime columns are displayed. This is presentation only — the stored and exported '
      + 'value is always the text the file came with. A format picked on an individual column '
      + '(right-click its header) beats the case setting, which beats the system-wide one.'));

    const tsSystemSel = el('select');
    for (const [key, label] of Object.entries(TS_FORMATS)) {
      const o = el('option', null, label);
      o.value = key;
      tsSystemSel.append(o);
    }
    tsSystemSel.value = S.appSettings.default_ts_format || 'iso';
    tsSystemSel.onchange = async () => {
      try {
        S.appSettings = await post('/api/settings/app', { default_ts_format: tsSystemSel.value });
        render();
        toast('Default timestamp format saved');
      } catch (e) {
        toast('Could not save: ' + e.message, 5000);
      }
    };
    secTs.append(labeledRow('Every case on this machine', tsSystemSel));

    const tsCaseSel = el('select');
    const inherit = el('option', null, 'Use the system-wide default');
    inherit.value = '';
    tsCaseSel.append(inherit);
    for (const [key, label] of Object.entries(TS_FORMATS)) {
      const o = el('option', null, label);
      o.value = key;
      tsCaseSel.append(o);
    }
    tsCaseSel.value = S.caseSettings.ts_format || '';
    tsCaseSel.disabled = !S.sources.length && !S.sourceId;
    tsCaseSel.onchange = async () => {
      try {
        S.caseSettings = await post('/api/case_settings', { ts_format: tsCaseSel.value });
        render();
        toast('Case timestamp format saved');
      } catch (e) {
        toast('Could not save: ' + e.message, 5000);
      }
    };
    secTs.append(labeledRow('This case', tsCaseSel));

    const secTags = settingsSection(b, 'Default tags for new cases');
    secTags.append(el('p', null,
      "Seeds a brand-new case's tag set when you create one from the home screen. Doesn't change tags in "
      + 'any case that already exists — use "Apply default template" in Edit tags for that.'));
    const dtList = el('div', 'settings-keys');
    let defaultTags = [];

    function renderDefaultTagRows() {
      dtList.replaceChildren();
      defaultTags.forEach((t, i) => {
        const row = el('div', 'row-actions');
        const color = el('input'); color.type = 'color'; color.value = t.color || '#8899aa';
        color.oninput = () => { t.color = color.value; };
        const name = el('input'); name.value = t.name || ''; name.placeholder = 'Tag name';
        name.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
        name.oninput = () => { t.name = name.value; };
        const key = el('input'); key.value = t.hotkey || ''; key.maxLength = 1;
        key.style.cssText = 'width:34px;text-align:center;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px;font-family:var(--mono)';
        key.oninput = () => { t.hotkey = key.value || null; };
        const rm = el('button', 'btn ghost', '✕');
        rm.onclick = () => { defaultTags.splice(i, 1); renderDefaultTagRows(); };
        row.append(color, name, key, rm);
        dtList.append(row);
      });
    }

    api('/api/settings/default_tags').then((t) => { defaultTags = t; renderDefaultTagRows(); }).catch(() => {});
    secTags.append(dtList);

    const dtActs = el('div', 'row-actions');
    const dtAdd = el('button', 'btn ghost', '+ tag');
    dtAdd.onclick = () => { defaultTags.push({ name: 'New tag', color: '#7f9bb5', hotkey: null }); renderDefaultTagRows(); };
    const dtSave = el('button', 'btn', 'Save template');
    dtSave.onclick = async () => {
      defaultTags = await post('/api/settings/default_tags', { tags: defaultTags });
      renderDefaultTagRows();
      toast('Default tag template saved');
    };
    dtActs.append(dtAdd, dtSave);
    secTags.append(dtActs);

    const secFilters = settingsSection(b, 'Saved filters');
    secFilters.append(el('p', null,
      `Cycle through filters saved for the current source's columns with `
      + `${S.keymap.cyclePrevFilter[0] || '['} / ${S.keymap.cycleNextFilter[0] || ']'}. `
      + `A table with matching columns also suggests these on open. Save one from the Filter `
      + `builder's "Save filter…" button; give a header set a nickname from the Saved filters menu.`));
    const flist = el('div', 'session-list');
    function renderFilterList() {
      flist.replaceChildren();
      if (!S.savedFilters.length) { flist.append(el('div', 'note-status', 'No saved filters yet.')); return; }
      for (const f of S.savedFilters) {
        const row = el('div', 'row-actions session-row');
        const colText = (f.col_names || []).join(', ');
        const headerLabel = el('span', 'count', nicknameFor(f.col_names) || colText);
        headerLabel.title = colText;
        row.append(el('span', 'session-name', f.name), headerLabel);
        const ren = el('button', 'btn ghost', 'Rename');
        ren.onclick = async () => {
          const name = await promptDialog('New name:', f.name);
          if (!name || !name.trim()) return;
          await api(`/api/saved_filters/${f.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }),
          });
          f.name = name.trim();
          renderFilterList();
          updateFiltersButton();
        };
        const del = el('button', 'btn ghost', '✕');
        del.title = 'Delete this saved filter';
        del.onclick = async () => {
          if (!(await confirmDialog(`Delete saved filter "${f.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
          await api(`/api/saved_filters/${f.id}`, { method: 'DELETE' });
          S.savedFilters = S.savedFilters.filter((x) => x.id !== f.id);
          renderFilterList();
          updateFiltersButton();
        };
        row.append(ren, del);
        flist.append(row);
      }
    }
    renderFilterList();
    secFilters.append(flist);

    const fActs = el('div', 'row-actions');
    const exp = el('button', 'btn ghost', 'Export filters…');
    exp.onclick = () => { window.location = '/api/saved_filters/export'; };
    const impLabel = el('label', 'btn ghost', 'Import filters…');
    const impInput = el('input');
    impInput.type = 'file';
    impInput.accept = '.json';
    impInput.hidden = true;
    impInput.onchange = async () => {
      const fd = new FormData();
      fd.append('file', impInput.files[0]);
      fd.append('merge', 'true');
      const res = await api('/api/saved_filters/import', { method: 'POST', body: fd });
      await loadSavedFilters();
      renderFilterList();
      updateFiltersButton();
      toast(`Imported ${res.added} filter${res.added === 1 ? '' : 's'}`);
    };
    impLabel.append(impInput);
    fActs.append(exp, impLabel);
    secFilters.append(fActs);

    const secPlugins = settingsSection(b, 'Plugins');
    buildPluginsPanel(secPlugins);
  });
}

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    if (!$('modal').hidden) { $('modal').hidden = true; return; }
    if (typing) { e.target.blur(); $('body').focus(); return; }
    selClear(); render(); return;
  }
  /* Everything below acts on the case UI — the grid's cursor, its tabs, its
     modals (Tables, Search all, the timeframe dialog). On the home screen
     none of that is on screen, and firing anyway meant `t`/`R`/the rest
     opened panels for a case that isn't showing. Escape (above) still
     works — home has modals of its own to close. */
  if ($('app').hidden) return;
  // e.code first because Alt+digit doesn't produce a digit in e.key on
  // every layout (macOS Alt+1 is '¡'), and the tag hotkeys below want the
  // same thing for the same reason.
  const digit = e.code && e.code.startsWith('Digit') ? e.code.slice(5) : e.key;
  /* Tab switching (Alt + 1…0), deliberately above the `typing` guard: the
     SQL pane focuses its editor on arrival, so a shortcut that gave up
     there could carry you *into* that tab and never back out. Skipped
     while a dialog is up — the #modal singleton *or* a spawned
     confirm/prompt overlay (_spawnDialog builds its own, so one check
     doesn't cover the other) — since switching the tab behind a dialog
     that's waiting on an answer isn't what anyone means. Ahead of
     matchAction and the tag hotkeys below because neither of those looks
     at modifiers — '0' is bound to resetColumnWidths and 1–9 are tag
     hotkeys, and Alt+digit is meant for neither. (Shift+digit was the obvious row and is taken: it applies a
     tag to the whole view.) */
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^[0-9]$/.test(digit)) {
    if (!$('modal').hidden || document.querySelector('.confirm-overlay')) return;
    e.preventDefault();
    activateTabSlot(digit);
    return;
  }

  if (typing) return;

  if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && (S.cellRange || selCount() || S.cursor >= 0)) {
    e.preventDefault();
    handleCopyShortcut(e.shiftKey);
    return;
  }

  /* Undo lives here rather than in S.keymap because matchAction only
     matches bare keys — the rebindable map has no notion of a modifier,
     and Ctrl+Z with no modifier check would fire on a bare 'z'. */
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
    e.preventDefault();
    undoLastTagChange();
    return;
  }

  const pageRows = Math.floor(($('body').clientHeight - headH()) / ROW_H) - 1;
  const action = matchAction(e);
  if (action && ACTION_HANDLERS[action] && (S.activeTab === 'grid' || TAB_AGNOSTIC_ACTIONS.has(action))) {
    e.preventDefault();
    ACTION_HANDLERS[action](e, pageRows);
    return;
  }
  if (/^[1-9]$/.test(digit) && S.activeTab === 'grid') {
    const t = S.tags.find((x) => x.hotkey === digit);
    if (t) { e.preventDefault(); e.shiftKey ? applyTagToView(t) : applyTag(t); }
  }
});

window.addEventListener('resize', () => { render(); drawRail(); applyPageTabsSize(); });

/* -------------------------------------------------------------- home screen */

/* Home manages "Cases" (one case.db each — recent, grouped, renamed,
   annotated). Distinct from the existing Session feature (the Session
   button above), which snapshots tags/notes/layout *within* one already-open
   case — that feature is untouched. Opening a Case from here is what
   actually swaps the server's STORE; navigating back to Home via #btnHome
   just changes what the client is looking at. */

function showApp() {
  $('home').hidden = true;
  $('app').hidden = false;
  applyPageTabsSize(); // first moment the bar has a real width to clamp against
}

/* The off switch — the server otherwise only ever stops when someone finds
   the terminal it was started in. Confirmed because it's outward-facing in
   the one way this app has (every tab pointed at this server dies), then a
   static farewell replaces the page: there is deliberately no "restart"
   affordance, because there's no server left to serve one. */
async function shutdownWinnow() {
  const go = await confirmDialog(
    'Shut down the Winnow server?\n\nEverything is already saved in the case file on disk — tags, notes and '
    + 'imports are never lost. This page (and any other tab using this server) will stop working until you '
    + 'start Winnow again.',
    { okLabel: 'Shut down', cancelLabel: 'Keep running', danger: true });
  if (!go) return;
  try { await post('/api/shutdown', {}); } catch { /* the server may drop before the response lands */ }
  const note = el('div', 'shutdown-note');
  note.append(
    el('div', 'shutdown-note-title', 'Winnow is off'),
    el('div', null, 'The server has shut down. You can close this tab — start Winnow again with "python server.py".'),
  );
  document.body.replaceChildren(note);
}
function showHome() { $('app').hidden = true; $('home').hidden = false; setBrandLabel(null); }

// The brand button doubles as "which case is this" once one's open — falls
// back to the app name on the home screen / before any case has loaded.
function setBrandLabel(name) {
  $('brandLabel').textContent = name || 'Winnow';
}

/* Text for the "already open elsewhere" prompt. Deliberately says what the
   consequence is rather than just "in use" — the analyst clicking this is
   deciding whether their colleague's afternoon survives, and "case is
   locked" doesn't give them anything to decide with. */
function describeCaseHolder(holder) {
  const who = holder.user || 'an unknown user';
  const where = holder.host || 'an unknown host';
  let when = '';
  if (holder.started_at) when = `, open since ${holder.started_at.replace('T', ' ')}`;
  if (holder.evidence === 'unreadable') {
    return 'A lock file sits next to this case but can\u2019t be read, so Winnow can\u2019t tell '
      + 'whether another server has it open.';
  }
  const age = holder.heartbeat_age_sec;
  const seen = (age === null || age === undefined) ? '' : ` (last seen ${Math.round(age)}s ago)`;
  return `This case is already open in another Winnow \u2014 ${who} on ${where}${when}${seen}.\n\n`
    + 'Opening it here too means neither server sees the other\u2019s tags, notes or imports until '
    + 'it reloads, and a long write in one (an import, or Compact case) will start failing the '
    + 'other. If the case file is on a network share, SQLite\u2019s locking does not work there '
    + 'at all and the file can be corrupted.';
}

async function openCase(path, opts = {}) {
  let res;
  try {
    res = await post('/api/case/open', { path, force: !!opts.force });
  } catch (e) {
    if (e.status === 409 && e.detail && e.detail.error === 'case_in_use') {
      const go = await confirmDialog(describeCaseHolder(e.detail.holder), {
        okLabel: 'Open anyway', cancelLabel: 'Don\u2019t open', danger: true,
      });
      if (!go) return;
      return openCase(path, { force: true });
    }
    toast('Could not open case: ' + e.message, 6000);
    return;
  }
  // Source ids are small and sequential per-case, so this case's "source 1"
  // may well collide with the id of a cached view_id from whatever case was
  // open before — that view_id belongs to a Store instance the server just
  // closed and can't possibly still exist. Drop the cache rather than let
  // openSource() try it, get a 409, and rebuild anyway.
  S.viewCache.clear();
  S.tabOrder = [];
  // Unlike a tab switch within the *same* case (where the timeframe filter
  // deliberately survives — see clearAllFilters()/applyPreset()), a
  // different case is a different investigation; a timeframe pinned in
  // the last one has no reason to silently carry over into this one.
  S.timeRange = { enabled: false, column: null, start: '', end: '' };
  updateTimeRangeButton();
  // Same reasoning for the timeline: its view_id belongs to the Store
  // instance that just closed, and its tag-id checkboxes belong to the
  // previous case's tag_defs — neither means anything here.
  S.timeline = { view: null, pages: new Map(), pending: new Set(), reqId: S.timeline.reqId + 1, tagFilter: null };
  // sql_tabs is a per-case table, so the previous case's tabs (and the
  // in-memory results keyed by their ids) don't describe this one. Left
  // empty rather than reloaded here — showSqlTab loads lazily.
  S.sqlTabs = [];
  S.sqlTabId = null;
  S.sqlResults.clear();
  // A Search-all job belongs to the Store instance the server just closed;
  // its results reference source ids from the previous case.
  S.searchAll = null;
  updateSearchAllButton();
  // A mounted plugin tab's UI was built from the previous case's data —
  // tear the mounts down so the next activation rebuilds against this one.
  resetPluginTabMounts();
  if (S.activeTab !== 'grid') showGridTab();
  setBrandLabel(res.name);
  showApp();
  // ts_format lives in the case file, so it's per-case state like sql_tabs
  // — reload it rather than carrying the last case's setting over.
  await loadCaseSettings();
  await loadSources();
}

function slugify(name) {
  return (name || 'case').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'case';
}

function fieldInput(value) {
  const inp = el('input');
  inp.value = value || '';
  inp.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
  return inp;
}

function groupOptionsDatalist(id) {
  const dl = el('datalist');
  dl.id = id;
  for (const g of new Set(S.cases.map((c) => c.group).filter(Boolean))) {
    const opt = document.createElement('option');
    opt.value = g;
    dl.append(opt);
  }
  return dl;
}

/* Browses the server machine's filesystem, one directory level at a time
   — the only way to hand back a real absolute path, since a browser's own
   folder picker (webkitdirectory / showDirectoryPicker) deliberately never
   exposes one. Assumes server and browser are the same machine, true for
   how this tool is actually run (see CLAUDE.md). onSelect gets the chosen
   directory's absolute path; nothing here touches file contents. */
function openFolderBrowser(startPath, onSelect, onCancel) {
  let current = null;
  modal('Choose a folder', (b) => {
    const pathLabel = el('div', 'note-status');
    pathLabel.style.cssText = 'font-family:var(--mono);word-break:break-all;margin-bottom:8px';
    b.append(pathLabel);
    const list = el('div', 'session-list');
    list.style.maxHeight = '46vh';
    list.style.overflow = 'auto';
    b.append(list);

    async function load(path) {
      let res;
      try {
        res = await api(`/api/browse_dir?path=${encodeURIComponent(path || '')}`);
      } catch (e) {
        toast('Could not list that folder: ' + e.message, 4000);
        return;
      }
      current = res.path;
      pathLabel.textContent = current;
      list.replaceChildren();
      if (res.parent) {
        const up = el('button', 'btn ghost', '.. (up a level)');
        up.style.cssText = 'justify-content:flex-start;text-align:left';
        up.onclick = () => load(res.parent);
        list.append(up);
      }
      if (!res.dirs.length) list.append(el('div', 'note-status', 'No subfolders here.'));
      for (const d of res.dirs) {
        const row = el('button', 'btn ghost', '📁 ' + d);
        row.style.cssText = 'justify-content:flex-start;text-align:left;width:100%';
        row.onclick = () => load(current + '/' + d);
        list.append(row);
      }
    }
    load(startPath);

    const actions = el('div', 'row-actions');
    const useBtn = el('button', 'btn', 'Use this folder');
    useBtn.onclick = () => onSelect(current);
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { if (onCancel) onCancel(); else $('modal').hidden = true; };
    actions.append(useBtn, cancel);
    b.append(actions);
  });
}

/* `state` carries everything back across a "Browse..." round trip —
   opening the folder browser swaps this modal's content out entirely
   (modal() replaces #modal's content in place rather than stacking), so
   there's no surviving DOM to read values back out of afterward; the only
   way to preserve what the analyst already typed is to snapshot it into
   plain values and re-invoke this function with them as the new initial
   state, same pattern openImportPreview's onConfirm/onCancel already use. */
function openNewCaseModal(state = {}) {
  modal('New case', (b) => {
    const nameInput = fieldInput(state.name || '');
    nameInput.placeholder = 'Case name';
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', null, 'Name'), nameRow);

    const groupInput = fieldInput(state.group || '');
    groupInput.placeholder = 'Group (optional) — e.g. an IR engagement name';
    groupInput.setAttribute('list', 'home-new-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-new-case-groups'));
    b.append(el('label', null, 'Group'), groupRow);

    let chosenDir = state.chosenDir || 'cases';
    const pathInput = fieldInput(state.path || `${chosenDir}/${slugify(state.name || '')}.db`);
    pathInput.style.fontFamily = 'var(--mono)';
    let pathTouched = state.pathTouched || false;
    pathInput.oninput = () => { pathTouched = true; };
    nameInput.oninput = () => { if (!pathTouched) pathInput.value = `${chosenDir}/${slugify(nameInput.value)}.db`; };
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      const snapshot = {
        name: nameInput.value, group: groupInput.value, path: pathInput.value,
        chosenDir, pathTouched, csvFile, csvFileName: csvFile ? csvFile.name : '',
      };
      openFolderBrowser(
        chosenDir,
        (dir) => openNewCaseModal({
          ...snapshot, chosenDir: dir, pathTouched: false, path: `${dir}/${slugify(snapshot.name)}.db`,
        }),
        () => openNewCaseModal(snapshot),
      );
    };
    const pathRow = el('div', 'row-actions');
    pathRow.append(pathInput, browseBtn);
    b.append(el('label', null, 'Case file path'), pathRow);

    let csvFile = state.csvFile || null;
    const csvRow = el('div', 'row-actions');
    const csvLabel = el('label', 'btn ghost', 'Import a CSV now (optional)…');
    const csvInput = el('input');
    csvInput.type = 'file';
    csvInput.accept = '.csv,.tsv,.txt,.psv';
    csvInput.hidden = true;
    const csvStatus = el('span', 'count', state.csvFileName || '');
    csvInput.onchange = () => { csvFile = csvInput.files[0] || null; csvStatus.textContent = csvFile ? csvFile.name : ''; };
    csvLabel.append(csvInput);
    csvRow.append(csvLabel, csvStatus);
    b.append(csvRow);

    const actions = el('div', 'row-actions');
    const create = el('button', 'btn', 'Create case');
    create.onclick = async () => {
      const name = nameInput.value.trim();
      if (!name) { toast('Name the case first'); return; }
      const path = pathInput.value.trim();
      if (!path) { toast('Give the case file a path'); return; }
      try {
        await post('/api/cases', { path, name, group: groupInput.value.trim(), notes: '' });
      } catch (e) {
        toast('Could not create case: ' + e.message, 6000);
        return;
      }
      $('modal').hidden = true;
      await openCase(path); // shared with the home screen's "open" flow — same brand-label/view-cache handling
      if (csvFile) openImportPreview(csvFile);
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(create, cancel);
    b.append(actions);
  });
}

async function openExistingCasePrompt() {
  const path = await promptDialog('Path to an existing case .db file:');
  if (!path || !path.trim()) return;
  const trimmed = path.trim();
  const name = trimmed.split(/[\\/]/).pop().replace(/\.db$/i, '');
  try {
    await post('/api/cases', { path: trimmed, name, group: '', notes: '' });
  } catch (e) {
    toast('Could not register case: ' + e.message, 6000);
    return;
  }
  await openCase(trimmed);
}

function openEditCaseModal(c) {
  modal('Edit case', (b) => {
    const nameInput = fieldInput(c.name);
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', null, 'Name'), nameRow);

    const groupInput = fieldInput(c.group || '');
    groupInput.setAttribute('list', 'home-edit-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-edit-case-groups'));
    b.append(el('label', null, 'Group'), groupRow);

    const notesArea = el('textarea');
    notesArea.rows = 4;
    notesArea.value = c.notes || '';
    notesArea.placeholder = 'Notes about this case…';
    notesArea.style.width = '100%';
    b.append(el('label', null, 'Notes'), notesArea);

    b.append(el('div', 'note-status', c.path));

    const actions = el('div', 'row-actions');
    const save = el('button', 'btn', 'Save');
    save.onclick = async () => {
      try {
        await api(`/api/cases/${c.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nameInput.value.trim() || c.name, group: groupInput.value.trim(), notes: notesArea.value }),
        });
      } catch (e) {
        toast('Could not save: ' + e.message, 6000);
        return;
      }
      $('modal').hidden = true;
      refreshCases();
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(save, cancel);
    b.append(actions);
  });
}

async function removeCaseFromList(c) {
  if (!(await confirmDialog(`Remove "${c.name}" from this list? The case file itself is left untouched on disk.`, { danger: true, okLabel: 'Remove' }))) return;
  await api(`/api/cases/${c.id}`, { method: 'DELETE' });
  refreshCases();
}

async function deleteCaseFile(c) {
  if (!(await confirmDialog(
    `Permanently delete the case file for "${c.name}"?\n\n${c.path}\n\nThis cannot be undone — all tags, notes and imported data in it will be lost.`,
    { danger: true, okLabel: 'Delete permanently' },
  ))) return;
  try {
    await api(`/api/cases/${c.id}?delete_file=true`, { method: 'DELETE' });
  } catch (e) {
    toast('Could not delete: ' + e.message, 6000);
    return;
  }
  refreshCases();
}

function renderCaseRow(c) {
  const row = el('div', 'home-case-row' + (c.exists === false ? ' missing' : ''));
  const main = el('div', 'home-case-main');
  const nameRow = el('div', 'home-case-name');
  nameRow.append(el('span', null, (c.exists === false ? '⚠ ' : '') + c.name));
  if (c.group) nameRow.append(el('span', 'home-case-group-badge', c.group));
  main.append(nameRow);
  const stats = c.exists === false
    ? `${c.path} — file not found`
    : c.error
      ? `${c.path} — ${c.error}`
      : `${c.path} · ${(c.source_count || 0).toLocaleString()} source${c.source_count === 1 ? '' : 's'} `
        + `· ${(c.row_count || 0).toLocaleString()} rows` + (c.last_opened ? ` · opened ${c.last_opened}` : '');
  main.append(el('div', 'home-case-meta', stats));
  if (c.notes) main.append(el('div', 'home-case-notes', c.notes));
  if (c.exists !== false) main.onclick = () => openCase(c.path);
  row.append(main);

  // Always four button slots, in the same order, even when an action
  // doesn't apply to this row (a missing case can't be Opened or have its
  // file Deleted) — .home-case-actions is a plain flex row with no fixed
  // column widths, so a row with fewer buttons used to pack them flush
  // right instead of leaving Edit/Remove from list in their usual columns,
  // visibly zig-zagging as soon as a missing case sat next to a real one.
  // visibility:hidden (not omitting the element) keeps the slot's width
  // reserved without making it paintable or clickable; disabled backs
  // that up and drops it from tab order.
  const actions = el('div', 'home-case-actions');
  const openBtn = el('button', 'btn ghost', 'Open');
  if (c.exists !== false) {
    openBtn.onclick = () => openCase(c.path);
  } else {
    openBtn.disabled = true;
    openBtn.style.visibility = 'hidden';
  }
  const edit = el('button', 'btn ghost', 'Edit');
  edit.onclick = () => openEditCaseModal(c);
  const remove = el('button', 'btn ghost', 'Remove from list');
  remove.onclick = () => removeCaseFromList(c);
  const del = el('button', 'btn ghost', 'Delete file…');
  if (c.exists !== false) {
    del.title = 'Permanently delete the case file from disk';
    del.onclick = () => deleteCaseFile(c);
  } else {
    del.disabled = true;
    del.style.visibility = 'hidden';
  }
  actions.append(openBtn, edit, remove, del);
  row.append(actions);
  return row;
}

const HOME_STALE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

function isStaleCase(c) {
  // Never-opened cases (just created, or from before last_opened existed)
  // aren't "stale" — they need attention, not hiding.
  if (!c.last_opened) return false;
  const t = Date.parse(c.last_opened);
  return !Number.isNaN(t) && (Date.now() - t) > HOME_STALE_MS;
}

function renderHome() {
  const home = $('home');
  home.replaceChildren();
  const inner = el('div', 'home-inner');

  const head = el('div', 'home-head');
  head.append(el('div', 'brand', 'Winnow'));
  head.append(el('div', 'home-head-spacer'));
  const newBtn = el('button', 'btn', '+ New case');
  newBtn.onclick = openNewCaseModal;
  const openBtn = el('button', 'btn ghost', 'Open existing case file…');
  openBtn.onclick = openExistingCasePrompt;
  const offBtn = el('button', 'btn ghost', '⏻ Shut down');
  offBtn.title = 'Stop the Winnow server — cases stay saved on disk';
  offBtn.onclick = shutdownWinnow;
  head.append(newBtn, openBtn, offBtn);
  inner.append(head);

  if (!S.cases.length) {
    inner.append(el('div', 'home-empty', 'No cases yet — create one to get started.'));
    home.append(inner);
    return;
  }

  const searchRow = el('div', 'home-search-row');
  const search = el('input', 'home-search');
  search.type = 'search';
  search.placeholder = 'Search cases or groups…';
  search.value = S.homeSearch;
  searchRow.append(search);
  inner.append(searchRow);

  const listWrap = el('div', 'home-case-list');
  inner.append(listWrap);

  function renderList() {
    listWrap.replaceChildren();
    const q = S.homeSearch.trim().toLowerCase();
    const matches = (c) => !q || c.name.toLowerCase().includes(q) || (c.group || '').toLowerCase().includes(q);
    // Most-recently-opened first; a case that's never been opened (no
    // last_opened) sorts after every case that has been, newest first.
    const sorted = [...S.cases].sort((a, b) => (b.last_opened || '').localeCompare(a.last_opened || ''));
    const filtered = sorted.filter(matches);
    const visible = S.homeShowOlder ? filtered : filtered.filter((c) => !isStaleCase(c));
    const hiddenCount = filtered.length - visible.length;
    if (!visible.length) {
      listWrap.append(el('div', 'home-empty', q ? 'No cases match that search.' : 'No cases to show.'));
    } else {
      for (const c of visible) listWrap.append(renderCaseRow(c));
    }
    if (hiddenCount > 0) {
      const toggle = el('button', 'btn ghost home-show-older',
        `Show ${hiddenCount.toLocaleString()} case${hiddenCount === 1 ? '' : 's'} not opened in over 30 days…`);
      toggle.onclick = () => { S.homeShowOlder = true; renderList(); };
      listWrap.append(toggle);
    }
  }
  search.oninput = () => { S.homeSearch = search.value; renderList(); };
  renderList();

  home.append(inner);
}

async function refreshCases() {
  try { S.cases = await api('/api/cases'); } catch { S.cases = []; }
  renderHome();
}

$('btnHome').onclick = () => { showHome(); refreshCases(); };

async function boot() {
  await Promise.all([loadSavedFilters(), loadHeaderNicknames(), loadTimelineTemplates(),
                     loadPlugins(), loadAppSettings()]);
  const cur = await api('/api/case/current').catch(() => ({ open: false }));
  if (cur.open) {
    setBrandLabel(cur.name);
    showApp();
    await loadCaseSettings();
    await loadSources();
    startJobsPoll(); // an import (or index build) from before a reload shows back up
  } else {
    showHome();
    await refreshCases();
  }
}

boot().catch((e) => toast('Could not start: ' + e.message, 8000));

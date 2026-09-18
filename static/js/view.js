/* Building and rebuilding the materialized view behind the grid.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { renderHead } from './columns.js';
import { $, OVERSCAN, PAGE, ROW_H, api, debounce, post, setBusy, toast, toastAction } from './core.js';
import { currentSpec, renderAdvancedChips, updateSearchHint } from './filters.js';
import { clearPageCache, headH, rScroll, render, rowAt, spacerPx, vScroll } from './grid.js';
import { drawRail, regroupAll } from './grouping.js';
import { armOpCancel, createNotice, followFtsBuild, opCancelCurrent, opToken } from './jobs.js';
import { syncSearchExpansion } from './search.js';
import { openSource } from './sources.js';
import { S, gridRowCount, selAdd, selClear, selCount, selFirst, selPositions, specKey } from './state.js';
import { refreshTagCounts, renderTagRibbon } from './tags.js';
import { updateFiltersButton, updateTimeRangeButton } from './timeframe.js';

/* ----------------------------------------------------------------- view */

/* Monotonic token so an older rebuild that resolves after a newer one
   started can't swap its stale view/spec in over the newer one's — the
   race was always possible (two POSTs can complete out of order), and
   the pre-swap row prefetch below widens the in-flight window enough to
   care. */
export let rebuildSeq = 0;

const SELECTION_REMAP_MAX = 20000;

/* The picks a rebuild is carrying across, as row ids, until it has put
   them back. A rebuild that starts while another's remap is still in
   flight finds the picks already cleared (or still in the OLD view's
   positions) — so it takes over these keys instead of reading S.selection.
   Cleared once a rebuild restores them, and on a table switch. */
let pendingKeys = null;
export function dropPendingSelection() { pendingKeys = null; }

/* The build in flight, if any: its cancel token and the controller its
   fetches hang off. A new rebuild cancels this one BEFORE it starts its
   own — both halves, and both matter. Without the server-side cancel,
   every keystroke past the debounce queued a full build on the writer
   lock, each stale one still committed (evicting the live view under the
   grid, whose next page fetch 409'd into yet another rebuild), and every
   read that takes the lock — the tab strip, the ribbon counts, tagging —
   waited behind the whole queue. Without the client-side abort, the
   superseded request kept one of the browser's six per-host connections
   (one is the presence stream) until its build reached the lock and
   failed fast there: a pre-cancelled token is only checked once the lock
   is HELD (Store._interruptible), so cancel alone shortens the work, not
   the wait. Two or three of those plus the jobs poll could use the budget
   up, and then even reader-pool page fetches would queue in the browser —
   the one way the grid itself can freeze. (Reasoning from the connection
   budget; nobody measured it.) */
let inflight = null;   // { token, controller, seq, sourceId }

/* Cancels the build in flight, if any. Returns whether its cancel chip
   was already up: the build superseding it arms its own at once then
   rather than after the usual 1.2s, since a chip that blinks off on
   every keystroke of a long search is a button the analyst reaches for
   and finds gone. */
function cancelInflight() {
  const prev = inflight;
  if (!prev) return false;
  inflight = null;
  // Fire-and-forget: a miss (the build already finished, or never
  // reached the server) is a no-op there and of no interest here.
  post('/api/cancel_op', { token: prev.token }).catch(() => {});
  prev.controller.abort();
  return opCancelCurrent === prev.token;
}

/* Whether a rebuild for the open table is in flight. The grid's expired-
   view recovery asks before starting one of its own: a build that
   finished after being superseded has evicted the live view — and the
   superseding build, when it lands, replaces it. Rebuilding from the 409
   would cancel that build and start the same spec over. A search left to
   finish in the background (S.pendingViews) is not in flight in this
   sense: it evicts nothing, so the rows on screen stay valid without it. */
export function rebuildInFlight() {
  return inflight !== null && inflight.sourceId === S.sourceId;
}

/* What #viewStats and the search box say while a build runs. The count
   the stats showed before the build is put back when the build is
   cancelled or fails (the old rows are still there — build_view rolls
   back with the previous view intact); a build that lands writes its own
   count. Owned by the newest rebuild: a superseded one's timer is
   stopped as the new one starts, and the text from before the FIRST of a
   burst is what a cancel comes back to, not "Filtering… 0.3 s". Ticks
   every BUILD_TICK_MS, so a build that finishes inside the first tick
   never shows it — the same reason the cancel chip waits 1.2s. */
export const BUILD_TICK_MS = 250;
let indicator = null;   // { seq, sourceId, before, timer }

/* What the spec is searching for, as one string: the box's text in
   contains/regex mode, the non-empty advanced terms joined. Empty when
   the build is a filter, a sort, a chip — anything but a search (an
   advanced bar holding one blank placeholder term is not a search). */
function specSearchTerm(spec) {
  return spec.search_mode === 'advanced'
    ? (spec.search_terms || []).map((t) => (t.term || '').trim()).filter(Boolean).join(', ')
    : (spec.search || '').trim();
}

/* 'Searching "term"…' when the spec searches, 'Filtering…' otherwise. */
export function buildLabel(spec) {
  const term = specSearchTerm(spec);
  return term ? `Searching "${term}"…` : 'Filtering…';
}

function startIndicator(seq, sourceId, spec) {
  // The rebuild being superseded hands its "before" text over; a rebuild
  // for another table (openSource) has no old count of its own to keep.
  const before = indicator
    ? (indicator.sourceId === sourceId ? indicator.before : '')
    : $('viewStats').innerHTML;
  if (indicator) clearInterval(indicator.timer);
  const label = buildLabel(spec);
  const started = performance.now();
  const ind = { seq, sourceId, before, timer: 0 };
  ind.timer = setInterval(() => {
    // The table changed under this build (openSource's cached path never
    // rebuilds, so nothing else stops the timer): its stats are the
    // other table's now.
    if (S.sourceId !== sourceId) { stopIndicator(seq, false); return; }
    $('viewStats').textContent = `${label} ${((performance.now() - started) / 1000).toFixed(1)} s`;
  }, BUILD_TICK_MS);
  indicator = ind;
  // The box marks itself busy only when it is what the build is doing —
  // a header-box filter pulsing the search field would point at the
  // wrong thing.
  if (specSearchTerm(spec)) $('search').setAttribute('aria-busy', 'true');
  else $('search').removeAttribute('aria-busy');
}

/* Returns the text the stats showed before the build (empty when a newer
   rebuild owns the indicator, or the table changed under it) — a build
   that detaches keeps it for its Discard. */
function stopIndicator(seq, restore) {
  if (!indicator || indicator.seq !== seq) return '';   // a newer rebuild owns it now
  clearInterval(indicator.timer);
  const { sourceId, before } = indicator;
  indicator = null;
  $('search').removeAttribute('aria-busy');
  if (restore && S.sourceId === sourceId) $('viewStats').innerHTML = before;
  return before;
}

/* The stats line a landed view writes. */
function statsLine(v) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return `<b>${v.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · ${v.elapsed_ms} ms`;
}

/* The row the analyst is at, as an identity that survives a rebuild
   (positions don't — they are wiped with the view, see CLAUDE.md
   invariant #2). Cursor first: the highlighted row is the place, and a
   few picks elsewhere in the table are not what a chip toggle should land
   on. The picks and the cell range are fallbacks for a view with no cursor
   (a Shift+F from the row menu, a range dragged without a click) — unless
   `cursorOnly`, for a rebuild that isn't going to move the viewport and
   only wants to keep the cursor honest. Null under a grouping (grouped
   positions are another address space, and regroupAll clears the cursor)
   and null once the row's page has left the cache. */
export function cursorRowAnchor({ cursorOnly = false } = {}) {
  if (S.groupByCols.length) return null;
  let pos = -1;
  if (S.cursor >= 0) pos = S.cursor;
  else if (cursorOnly) return null;
  else if (selCount()) pos = selFirst();
  else if (S.cellRange) pos = S.cellRange.r0;
  const r = pos >= 0 ? rowAt(pos) : null;
  return r ? { source_id: r.source_id, rid: r.rid } : null;
}

/* Where `anchor` sits in the view just built: a position; null when the
   view no longer has that row; undefined when the question couldn't be
   asked — the view was already gone (a 409: evicted by a newer rebuild
   before this landed) or the request itself failed. The two non-answers
   are kept apart because they mean different things to the cursor: null
   clears it, undefined leaves it alone, since the row may well still be
   there. Never rejects: the tag chips and the timeframe toggle call
   rebuildView without awaiting it, so a throw here would be an unhandled
   rejection over a grid that is otherwise fine. */
export async function rowPositionIn(v, anchor, signal) {
  try {
    const { pos } = await api(`/api/row_position?view_id=${v.view_id}&source_id=${anchor.source_id}&rid=${anchor.rid}`, { signal });
    return pos == null ? null : pos;
  } catch { return undefined; }
}

/* ------------------------------------------------- searching in the background */

/* How long a search-box build blocks the grid — busy bar, cancel chip,
   the "Searching… N s" stats — before it is left to finish in the
   background and the old rows stay on screen. Search-box rebuilds only
   (the box's debounce, Enter, Escape, the mode switch, the advanced
   chips): a filter, a sort, a tag chip or the timeframe still block with
   the chip, because the code that awaits those rebuilds acts on the NEW
   view afterwards (restores a scroll offset, recentres on a row, drills
   into a widget), and a rebuild that resolved with the old view still
   installed would run that against the wrong rows. Rebindable for the
   UI tests, which can't wait five seconds per case. */
export let SEARCH_DETACH_MS = 5000;
export function setSearchDetachMs(ms) { SEARCH_DETACH_MS = ms; }

/* Poll cadence for a background build: quick at first (most searches land
   within a few ticks) and backing off to the same interval the jobs
   panel uses. */
const VIEW_POLL_MIN_MS = 150;
const VIEW_POLL_MAX_MS = 400;

/* A setTimeout that rejects like an aborted fetch when the build it
   paces is superseded, so the poll loop below leaves through the same
   catch a cancelled request does. */
function abortableSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const cancelled = () => { const e = new Error('Cancelled'); e.aborted = true; return e; };
    if (signal.aborted) { reject(cancelled()); return; }
    const onAbort = () => { clearTimeout(t); reject(cancelled()); };
    const t = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/* Starts the build as a job (POST /api/view/start answers inline when it
   finishes within its own short wait) and polls it until it lands or
   `detachAfterMs` has passed since `t0`. Resolves the last job snapshot:
   `status: 'running'` means the deadline came first. */
async function startAndPoll(spec, signal, t0, detachAfterMs) {
  let job = await post('/api/view/start', spec, { signal });
  let wait = VIEW_POLL_MIN_MS;
  while (job.status === 'running') {
    const left = detachAfterMs - (performance.now() - t0);
    if (left <= 0) return job;
    await abortableSleep(Math.min(wait, left), signal);
    wait = Math.min(VIEW_POLL_MAX_MS, wait + 50);
    job = await api(`/api/view/job?job_id=${job.job_id}`, { signal });
  }
  return job;
}

/* The view a finished job built — or the error /api/view would have
   answered with: 499-shaped for a cancelled build (the chip, or a newer
   search superseding it server-side), the record's own status for a
   failed one (400 for a bad filter, like api_view). */
function viewOfJob(job) {
  if (job.status === 'done') return job.view;
  const e = new Error(job.status === 'cancelled' ? 'Cancelled' : (job.error || 'The search failed'));
  e.status = job.status === 'cancelled' ? 499 : (job.error_status || 500);
  throw e;
}

/* Makes a held view the table's live one. A held build evicts nothing,
   so until this the grid's old view is still what the server pages; a
   409 "expired" means a build that landed in between — a filter change,
   another search — evicted the held view (newer intent wins), and the
   honest answer is to run the same spec again, right here, inside the
   chrome the caller already has up. `onRebuild` is told before that
   build is posted — runBuild arms the cancel chip there for an adopt,
   which has nothing for a chip to cancel until it becomes a build. */
async function adoptOrRebuild(view, spec, signal, onRebuild = null) {
  try {
    return await post('/api/view/adopt', { view_id: view.view_id }, { signal });
  } catch (e) {
    if (e.status !== 409 || !/expired/i.test(e.message || '')) throw e;
    toast('That search’s view expired — running it again', 4000);
    if (onRebuild) onRebuild();
    return post('/api/view', spec, { signal });
  }
}

/* The search box, filters, sort, tag and timeframe state as S holds
   them — what a search that finishes in the background puts back when
   it is applied, since by then the analyst may have typed something
   else, changed tables, or both. The compiled spec can't do this job:
   S.filters is the header boxes' raw text and the spec its parsed form. */
function viewStateSnapshot() {
  return {
    filters: { ...S.filters },
    search: S.search,
    searchMode: S.searchMode,
    searchTerms: S.searchTerms.map((t) => ({ ...t })),
    advCollapsed: S.advCollapsed,
    filterTree: JSON.parse(JSON.stringify(S.filterTree)),
    sort: S.sort.map((x) => ({ ...x })),
    tagFilter: [...S.tagFilter],
    timeRange: { ...S.timeRange },
    hideEmptyRows: S.hideEmptyRows,
  };
}

function restoreViewState(st) {
  S.filters = { ...st.filters };
  S.search = st.search;
  S.searchMode = st.searchMode;
  S.searchTerms = st.searchTerms.map((t) => ({ ...t }));
  S.advCollapsed = st.advCollapsed;
  S.filterTree = JSON.parse(JSON.stringify(st.filterTree));
  S.sort = st.sort.map((x) => ({ ...x }));
  S.tagFilter = [...st.tagFilter];
  S.timeRange = { ...st.timeRange };
  S.hideEmptyRows = st.hideEmptyRows;
}

/* Every piece of chrome that paints from the state above. */
function repaintSearchChrome() {
  $('search').value = S.searchMode === 'advanced' ? '' : S.search;
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === S.searchMode)));
  if (S.searchMode === 'advanced') renderAdvancedChips();
  syncSearchExpansion();
  updateSearchHint();
  renderHead();
  renderTagRibbon();
  updateTimeRangeButton();
  updateFiltersButton();
}

/* Puts the stats line back to what it said before a search that never
   installed (cancelled, discarded, failed) — when the analyst is looking
   at that table AND the view on screen is still the one that text
   described (`rec.viewId`, captured as the build started). Recomputed
   from the live view otherwise: another build landed in between and
   wrote its own count, which the pre-search text would miscaption
   ("200 of 200 rows" over 37); or the text from before the build is
   gone (the table was switched under it, which stops the indicator
   without keeping it). */
function restoreStats(rec) {
  if (S.sourceId !== rec.sourceId) return;
  if (rec.before && S.view && S.view.view_id === rec.viewId) $('viewStats').innerHTML = rec.before;
  else if (S.view && S.view.source_id === S.sourceId) $('viewStats').innerHTML = statsLine(S.view);
}

function rowsLabel(n) { return `${n.toLocaleString()} row${n === 1 ? '' : 's'}`; }

/* What #viewStats says for a table whose search is in the background:
   while it runs, and once it has landed and waits for Apply. Written at
   the detach, when the result comes in, and by openSource when the
   analyst comes back to the table with that search still in the box. */
export function pendingViewStatsText(rec) {
  if (rec.status !== 'done') return 'Searching in background…';
  return `Search finished — ${rowsLabel(rec.view.row_count)} · Apply from the jobs panel`;
}

/* A search-box build still running at the detach deadline goes on in
   the background: the busy chrome comes down, the old rows stay (a held
   build evicted nothing, so they are still real), a jobs-panel row with
   a Cancel button stands for it, and followPendingView polls it to its
   end. One per table (S.pendingViews) — a newer search-box rebuild for
   the same table cancels it, see runBuild. */
function detachBuild(rec) {
  const src = S.sources.find((s) => s.id === rec.sourceId);
  const term = specSearchTerm(rec.spec);
  const name = src ? src.name : 'the table';
  rec.status = 'running';
  rec.view = null;
  rec.notice = createNotice('view', {
    title: term ? `Searching "${term}" in ${name}` : `Filtering ${name}`,
    detail: 'running in the background',
    progress: null,
    sticky: true,
    actions: [{ label: 'Cancel', onClick: () => cancelPendingView(rec.sourceId) }],
  }, {
    // The row's ✕ is this search's Cancel while it runs and its Discard
    // once it has landed — never a plain dismiss, which would leave the
    // search polling with nothing on screen to apply or drop it from.
    onDismiss: () => cancelPendingView(rec.sourceId),
  });
  S.pendingViews.set(rec.sourceId, rec);
  if (S.sourceId === rec.sourceId) $('viewStats').textContent = pendingViewStatsText(rec);
  followPendingView(rec);
}

/* Polls a detached build until it lands. A finished search never
   installs itself — the analyst may be three tables away or mid-thought
   on the rows they have — it waits for Apply (the notice's button, or
   the toast's) and offers Discard beside it. Stops the moment its record
   is no longer the table's pending one (cancelled, discarded, applied,
   replaced by a newer search, or the case switched), so a late answer
   can never resurrect a closed row. */
async function followPendingView(rec) {
  const live = () => S.pendingViews.get(rec.sourceId) === rec;
  let wait = VIEW_POLL_MIN_MS;
  for (;;) {
    await new Promise((r) => setTimeout(r, wait));
    wait = Math.min(VIEW_POLL_MAX_MS, wait + 50);
    if (!live()) return;
    let job;
    try {
      job = await api(`/api/view/job?job_id=${rec.jobId}`);
    } catch (e) {
      if (!live()) return;
      // 404: a newer job for this table replaced it server-side (another
      // window, say); 409: the case was closed under it. Either way there
      // is nothing left to wait for, and neither is this search's fault.
      // Anything else — the server not answering while the connection
      // banner is up — is asked again.
      if (e.status === 404 || e.status === 409) {
        settlePending(rec, 'cancelled', e.status === 404 ? 'replaced by a newer search' : 'the case was closed');
        return;
      }
      continue;
    }
    if (!live()) return;
    if (job.status === 'running') continue;
    if (job.status !== 'done') { settlePending(rec, job.status, job.error); return; }
    rec.status = 'done';
    rec.view = job.view;
    const rows = rowsLabel(job.view.row_count);
    rec.notice.done({
      detail: `${rows} · ${((job.elapsed_ms || 0) / 1000).toFixed(1)} s`,
      sticky: true,
      actions: [
        { label: 'Apply', onClick: () => applyPendingView(rec.sourceId) },
        { label: 'Discard', onClick: () => cancelPendingView(rec.sourceId) },
      ],
    });
    toastAction(`Search finished — ${rows}`, 'Apply', () => applyPendingView(rec.sourceId));
    // Over the count the search was started against only: a view that
    // landed since has its own line (such a build cancels the search,
    // but this poll's answer may already have been on its way).
    if (S.sourceId === rec.sourceId && S.view && S.view.view_id === rec.viewId) $('viewStats').textContent = pendingViewStatsText(rec);
    return;
  }
}

/* A detached build that ended without a view to offer. A build error
   waits for the ✕ like any failed job's row; a cancel — the chip's, or a
   newer search from another window superseding it server-side — is not
   an error, so its row finishes, lingers NOTICE_LINGER_MS and closes on
   its own. */
function settlePending(rec, status, detail) {
  S.pendingViews.delete(rec.sourceId);
  rec.status = status;
  if (status === 'error') rec.notice.fail({ detail: detail || 'the search failed' });
  else rec.notice.done({ detail: detail || 'cancelled', sticky: false, actions: [] });
  restoreStats(rec);
}

/* Cancels the search running in the background for a table (the
   notice's Cancel), or discards the result of one that finished (its
   Discard). The server cancels the build, or drops the held view — the
   rows on screen were never touched either way. Returns whether there
   was one. */
export function cancelPendingView(sourceId = S.sourceId) {
  const rec = S.pendingViews.get(sourceId);
  if (!rec) return false;
  S.pendingViews.delete(sourceId);
  rec.notice.close();
  post(`/api/view/job/cancel?job_id=${rec.jobId}`, {}).catch(() => {});
  restoreStats(rec);
  return true;
}

/* Installs the result of a search that finished in the background: the
   table it was for is opened if it isn't the open one (openSource with
   skipBuild restores that table's own stash into S FIRST, so the job's
   state has to go in after it, not before), the search box, filters,
   sort, tags and timeframe are put back to what the search was run
   with, the chrome repainted from them, and the held view adopted as
   the table's live one through the same landing every rebuild takes —
   so the cursor row, the picks and the seeded pages all resolve against
   the adopted view. */
export async function applyPendingView(sourceId = S.sourceId) {
  const rec = S.pendingViews.get(sourceId);
  if (!rec || rec.status !== 'done') return;
  if (!S.sources.some((s) => s.id === sourceId)) {
    // The table was removed while its search waited (Remove drops the
    // record too, but a toast's Apply can land in the beat before the
    // source list is refetched): nothing to open, and the held view is
    // dropped rather than left on the server for nobody.
    toast('That table is gone — the search result was discarded', 4000);
    cancelPendingView(sourceId);
    return;
  }
  S.pendingViews.delete(sourceId);
  rec.notice.close();
  if (S.sourceId !== sourceId) {
    await openSource(sourceId, { skipBuild: true });
    if (S.sourceId !== sourceId) return;   // the table is gone
  }
  restoreViewState(rec.state);
  repaintSearchChrome();
  await runBuild({
    keepScroll: false,
    keepRow: true,
    fetchView: (spec, signal, onRebuild) => adoptOrRebuild(rec.view, spec, signal, onRebuild),
  });
}

/* ------------------------------------------------------------- rebuilding */

/* One rebuild, whichever way the view arrives: POST /api/view (the
   default), a search-box build started as a job and polled until the
   detach deadline (`detachAfterMs`), or a held view adopted by
   applyPendingView (`fetchView`). Everything around the fetch — the
   supersede, the busy chrome, the seed fetch, the landing — is the same,
   which is the point of there being one of these. */
async function runBuild({ keepScroll = true, keepRow = true, detachAfterMs = null, fetchView = null } = {}) {
  if (!S.sourceId) return;
  // Captured in virtual (row-space) pixels rather than as a raw scrollTop:
  // the outgoing and incoming views can have different row counts, and once
  // either is over MAX_SPACER_PX they have different spacer scales too — the
  // same scrollTop would then mean a different row on each side.
  const oldTotal = gridRowCount();
  let scroll = keepScroll ? vScroll($('body'), oldTotal, headH()) : 0;
  // The row to come back to, captured while the old view is still there to
  // resolve it. keepRow:false is for a navigation that means "the top of a
  // fresh table" — a dashboard drill, openSource's first build — where
  // there is no place to keep. With keepScroll the viewport stays where it
  // is, and the one thing that would notice a re-pointed cursor is an open
  // detail pane (grid.js re-points it at rowAt(S.cursor) as pages land, so
  // a number left behind puts a stranger in it): the row is captured for
  // the pane's sake alone — the cursor only, and only while the pane is
  // open. Pane closed, nothing is captured and the cursor keeps its number,
  // which is what the header box always did — the highlight stays at its
  // screen spot — and typing pays for no lookup (see the one below).
  const anchor = keepRow && (!keepScroll || !$('detail').hidden) ? cursorRowAnchor({ cursorOnly: keepScroll }) : null;
  const spec = currentSpec();
  // The cache key is the spec as the analyst set it: the op_token added
  // next is fresh per rebuild, and keying on it meant no reopen ever hit.
  const cacheKey = specKey(spec);
  spec.op_token = opToken();
  const seq = ++rebuildSeq;
  // Which table this rebuild is for; checked again before it paints.
  const forSourceId = S.sourceId;
  // The view on screen for it, by id — what a detached search's stats
  // text will describe, and what restoreStats checks is still there
  // before putting that text back.
  const oldViewId = S.view && S.view.source_id === forSourceId ? S.view.view_id : null;
  const detach = detachAfterMs != null;
  // This table's search in the background, if any, cannot survive this
  // build, so it is called off first — whichever way this build lands.
  // A search-box rebuild is the newer search: its notice replaces the
  // old one. Any other rebuild (a header filter, a sort, a tag chip, the
  // timeframe, a return to the table with a different spec) lands as a
  // normal build, which evicts the held view server-side (newer intent
  // wins — store.md), and a notice left standing would offer an Apply
  // that could only 409 into a blocking re-run of the search. Cancelling
  // first also frees the writer lock a running one holds: told the
  // search was in the background, the analyst would otherwise find a
  // header-box keystroke blocked for the rest of it and then the same
  // scan run again. An adopt (fetchView) is a pending record's own
  // landing; applyPendingView has taken the record already. (The cancel
  // restores the stats text first, so the indicator below captures the
  // real count, not "Searching in background…".)
  if (!fetchView) cancelPendingView(forSourceId);
  // What this build would put back if it detaches and is applied later.
  const state = detach ? viewStateSnapshot() : null;
  // Supersede the build in flight — before this one's own work, so the
  // writer lock is asked for once, not once per keystroke — and register
  // this one for the next rebuild to do the same to. A rebuild that
  // starts during the keys lookup below finds this one registered and
  // aborts it before it ever posts: fetch rejects at once on a signal
  // that is already aborted.
  const chipUp = cancelInflight();
  const controller = new AbortController();
  inflight = { token: spec.op_token, controller, seq, sourceId: forSourceId };
  // The chip cancels a BUILD — cancel_op interrupts the statement its
  // token is registered under. An adopt registers nothing, and its wait,
  // if any, is for the writer lock, which no cancel shortens: the chip
  // stays down for one, and comes up only if the adopt 409s into a
  // rebuild (adoptOrRebuild arms it before posting /api/view). Armed
  // here, at the supersede, when the build being superseded already had
  // its chip up: that build disarms the moment its aborted fetch
  // rejects, which is long before the keys lookup below lets this one
  // reach its own arming point, and the chip a long search put up would
  // blink off on every keystroke — a button the analyst reaches for and
  // finds gone. Idempotent, so the call below is still the one that arms
  // a build nobody was cancelling yet.
  let disarmCancel = null;
  const armChip = () => { if (!disarmCancel) disarmCancel = chipUp ? armOpCancel(spec.op_token, 0) : armOpCancel(spec.op_token); };
  if (chipUp && !fetchView) armChip();
  // See the remap below: what's picked, as row ids, while the old view is
  // still there to ask. Explicit picks only — a select-all is a statement
  // about THIS view. Capped: nobody remaps a hundred thousand hand-picks.
  let keys = null;
  if (pendingKeys && pendingKeys.sourceId === forSourceId && !S.selectAll && !S.selection.size) {
    keys = pendingKeys.keys;   // a superseded rebuild's picks, not yet put back
  } else if (S.view && !S.selectAll && S.selection.size && S.selection.size <= SELECTION_REMAP_MAX && !S.groupByCols.length) {
    try {
      keys = (await post('/api/view/keys', { view_id: S.view.view_id, positions: selPositions() })).keys;
    } catch { keys = null; }
  }
  // Superseded during that lookup: the newer rebuild has already cancelled
  // this one's token and aborted its controller, and none of this one's
  // chrome — busy bar, indicator — has started, so there is nothing else
  // to undo. Starting it now would put this rebuild's indicator over the
  // newer one's and then, in the finally below, take it down and restore
  // the old count while the build that is actually running goes unmarked.
  // The chip claimed above is handed straight on: that newer rebuild
  // claimed it in turn as it superseded this one, so this disarm finds
  // the chip owned by another token and leaves it standing.
  if (seq !== rebuildSeq) { if (disarmCancel) disarmCancel(); return; }
  pendingKeys = keys ? { sourceId: forSourceId, keys } : null;
  let v;
  let seeded = [];
  let pos = null;
  const t0 = performance.now();
  setBusy(true);
  if (!fetchView) armChip();
  startIndicator(seq, forSourceId, spec);
  // The chrome comes down once, whichever way this build leaves — the
  // detach takes it down early and keeps the stats text from before the
  // build for a later Discard; the finally is for every other exit.
  let settled = false;
  const settle = (restore) => {
    if (settled) return '';
    settled = true;
    setBusy(false);
    if (disarmCancel) disarmCancel();
    if (inflight && inflight.seq === seq) inflight = null;
    return stopIndicator(seq, restore);
  };
  try {
    try {
      if (fetchView) {
        v = await fetchView(spec, controller.signal, armChip);
      } else if (detach) {
        const job = await startAndPoll(spec, controller.signal, t0, detachAfterMs);
        if (job.status === 'running') {
          // Superseded between the last poll and now: that rebuild has
          // cancelled this build's token already, so there is nothing to
          // follow.
          if (seq !== rebuildSeq) return;
          const before = settle(false);
          // The picks stay live in the old view; an apply re-reads them.
          pendingKeys = null;
          detachBuild({ jobId: job.job_id, token: spec.op_token, sourceId: forSourceId, spec, cacheKey, state, before, viewId: oldViewId });
          return;
        }
        v = await adoptOrRebuild(viewOfJob(job), spec, controller.signal);
      } else {
        v = await post('/api/view', spec, { signal: controller.signal });
      }
    } catch (e) {
      // 499 = this build was cancelled; aborted = this client dropped the
      // request itself; 404 = the job was replaced server-side. Server-side
      // the transaction rolled back with the previous view intact (see
      // build_view), so the rows on screen are still real — just keep them.
      if (e.status === 499 || e.aborted || e.status === 404) {
        // Superseded — a newer rebuild cancelled this one on its way in
        // (or the table changed under it): that one owns the screen now,
        // and a toast per superseded keystroke would be noise about work
        // the analyst never asked to see finish. The same guard the
        // success path applies before it paints.
        if (seq !== rebuildSeq || S.sourceId !== forSourceId) return;
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
    // Where the anchored row landed in the new view. Asked BEFORE the seed
    // fetch below, so that when the viewport is going to move onto that row
    // the pages seeded are the ones the grid will show — landing on page 0
    // and recentring afterwards painted the target rows as placeholders for
    // a round trip, the exact flash the seed exists to remove. With
    // keepScroll the viewport doesn't move and the answer only re-points
    // the cursor for the open pane, so the lookup is issued alongside the
    // seed and awaited after it — the paint still waits for the slower of
    // the two. That is why it isn't issued at all with the pane closed (see
    // the anchor above): on a materialised view find_position is a scan of
    // the whole view table (pos is its only key), and every debounced
    // keystroke in a header box would pay it before the grid could repaint.
    const posP = anchor && v.row_count ? rowPositionIn(v, anchor, controller.signal) : Promise.resolve(null);
    if (!keepScroll) {
      pos = await posP;
      // Centred in the band below the sticky header — the same target
      // recenterOnRow uses (the headH()/2 term is that band's offset); the
      // Math.min at the seed and rScroll at the paint clamp it to the view.
      if (pos != null) scroll = Math.max(0, pos * ROW_H + ROW_H / 2 + headH() / 2 - $('body').clientHeight / 2);
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
          const data = await api(`/api/rows?view_id=${v.view_id}&start=${idx * PAGE}&count=${PAGE}`, { signal: controller.signal });
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
    if (keepScroll) pos = await posP;
  } finally {
    // A build that landed writes its own count in installView; one that
    // didn't gets the old one back. (A superseded build owns neither —
    // no-op.)
    settle(!v);
  }
  // anchored, not the anchor itself: installView only needs to know whether
  // a row was captured to resolve (the rule the comment below spells out).
  await installView(v, { seq, forSourceId, cacheKey, seeded, keys, pos, scroll, keepRow, keepScroll, anchored: !!anchor, spec });
}

/* Makes `v` the grid's view: state, page cache, stats, the selection and
   cursor carried across, the paint, and everything that follows a new
   view — the ribbon counts, the Filters button, plugin panels via
   winnow:viewchange. The tail of every rebuild, whether the view came
   from /api/view or was adopted after finishing in the background;
   `winnow:viewchange` fires from here and nowhere else in a rebuild, so
   a search that is still running in the background has not "changed the
   view" until it is applied. `ctx` is what runBuild resolved before the
   swap: the seq/table guards, the seeded pages, the picks by id, the
   cursor row's new position and the scroll to land at. */
export async function installView(v, { seq, forSourceId, cacheKey, seeded = [], keys = null, pos = null, scroll = 0,
                                      keepRow = true, keepScroll = true, anchored = false, spec }) {
  // A newer rebuild started while this one was in flight — its view has
  // already evicted ours server-side (or this one's cancel landed first);
  // let it win.
  if (seq !== rebuildSeq) return;
  // …and the table may have changed under it without any rebuild at all:
  // openSource's cached-view path restores S.view directly and never bumps
  // rebuildSeq. Painting here would put THIS source's rows under the OTHER
  // source's headers, and the line below would cache the view under the
  // wrong id, so the poison survives the next open.
  if (S.sourceId !== forSourceId) return;
  S.view = v;
  S.viewCache.set(S.sourceId, { key: cacheKey, view_id: v.view_id, row_count: v.row_count, elapsed_ms: v.elapsed_ms });
  clearPageCache();
  for (const [idx, rows] of seeded) {
    S.pages.set(idx, rows);
    for (const r of rows) S.rowsByPos.set(r.pos, r);
  }
  // The count goes in before the selection remap below awaits. The
  // indicator, stopped in the finally, left its last "Searching… 3.2 s"
  // frozen in #viewStats, and a rebuild that started during that await
  // read the frozen label as the text to come back to on a cancel — then
  // put it back as the permanent stats line. The remap doesn't change
  // the count, so nothing here waits on it.
  const src = S.sources.find((s) => s.id === S.sourceId);
  $('viewStats').innerHTML = statsLine(v);
  // Picks are positions, and positions mean different rows now — but the
  // ROWS the analyst picked are the same rows. Carry them over by id
  // (keys captured before the rebuild, positions looked up after) so a
  // sort or filter to check something doesn't cost the selection; the
  // ones the new view no longer shows are counted, not silently lost.
  selClear();
  S.selUndo = [];   // snapshots of the old positions would land on other rows
  S.selHidden = 0;
  if (keys) {
    try {
      const r = await post('/api/view/positions', { view_id: v.view_id, keys });
      if (seq !== rebuildSeq || S.sourceId !== forSourceId) return;   // pendingKeys stays for the rebuild that won
      for (const p of r.positions) selAdd(p);
      S.selHidden = r.missing;
      pendingKeys = null;
    } catch { /* a lost selection is not worth a failed rebuild */ }
  }
  S.anchor = -1;
  S.cellRange = null;
  S.cellAnchor = null;
  // The cursor is the one piece of place state the remap above doesn't
  // carry. Left as a number it names whatever row now holds that position
  // — off-screen after a chip toggle widened the view, and with the detail
  // pane open (grid.js re-points it at rowAt(S.cursor) as pages land) a
  // silent wrong-row display. Where a row was captured: re-point the
  // cursor at it by identity, or clear it, pane and all, when this view no
  // longer has that row (null) — an honest empty is better than a
  // highlight on a stranger. A lookup that failed (undefined) is neither
  // answer; the row may well still be here, so the cursor is left as it
  // was for the next rebuild to resolve. Where no row was captured, only a
  // landing at the top with a cursor still set is acted on: its page had
  // left the cache (trimPageCache, once the analyst scrolled far from it)
  // and the number would name a stranger at the top of the new view. A
  // keepScroll rebuild that captured nothing — pane closed, or that page
  // gone — touches nothing: the viewport didn't move, and the highlight
  // stays where it was. Untouched under a grouping (regroupAll owns the
  // cursor there) and for keepRow:false.
  if (keepRow && !S.groupByCols.length) {
    const drop = () => { S.cursor = -1; $('detail').hidden = true; $('detailResize').hidden = true; };
    if (anchored) {
      if (pos != null) S.cursor = pos;
      else if (pos === null) drop();
    } else if (!keepScroll && S.cursor >= 0) {
      drop();
    }
  }
  $('spacerY').style.height = spacerPx(v.row_count) + 'px';
  $('noRows').hidden = v.row_count > 0;
  $('body').scrollTop = rScroll($('body'), v.row_count, scroll, headH());
  if (S.groupByCols.length) {
    // The old view_id (and any expanded groups' sub-views) is gone now —
    // re-summarize against the new one, keeping the chosen grouping columns.
    await regroupAll();
  } else {
    render();
    drawRail();
  }
  // Anything following the grid (plugin toolbar panels via
  // winnow.onViewChange) hears about the new view here — after the paint,
  // so a listener that reads S.view sees the settled state.
  document.dispatchEvent(new CustomEvent('winnow:viewchange',
    { detail: { sourceId: S.sourceId, viewId: v.view_id, rowCount: v.row_count } }));
  refreshTagCounts(); // the scope changed, so every ribbon count did too
  updateFiltersButton();
  // A search on a table with no trigram index is what starts the build —
  // server-side, from inside build_view's contains and advanced branches
  // (Store._ensure_fts_building; regex never indexes) — and the view's
  // payload says nothing about it. The client has to ask: followFtsBuild.
  if (!src.has_fts && spec.search_mode !== 'regex' && specSearchTerm(spec)) followFtsBuild(src.id);
}

/* `detachAfterMs`: leave the build to finish in the background once it
   has run this long (search-box callers pass SEARCH_DETACH_MS); absent,
   the build blocks with the cancel chip however long it takes. */
export async function rebuildView({ keepScroll = true, keepRow = true, detachAfterMs = null } = {}) {
  return runBuild({ keepScroll, keepRow, detachAfterMs });
}

export const rebuildSoon = debounce(() => rebuildView(), 220);

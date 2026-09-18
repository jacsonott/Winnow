/* Building and rebuilding the materialized view behind the grid.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, OVERSCAN, PAGE, ROW_H, api, debounce, post, setBusy, toast } from './core.js';
import { currentSpec } from './filters.js';
import { clearPageCache, headH, rScroll, render, rowAt, spacerPx, vScroll } from './grid.js';
import { drawRail, regroupAll } from './grouping.js';
import { armOpCancel, followFtsBuild, opCancelCurrent, opToken } from './jobs.js';
import { S, gridRowCount, selAdd, selClear, selCount, selFirst, selPositions, specKey } from './state.js';
import { refreshTagCounts } from './tags.js';
import { updateFiltersButton } from './timeframe.js';

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
   would cancel that build and start the same spec over. */
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

function stopIndicator(seq, restore) {
  if (!indicator || indicator.seq !== seq) return;   // a newer rebuild owns it now
  clearInterval(indicator.timer);
  const { sourceId, before } = indicator;
  indicator = null;
  $('search').removeAttribute('aria-busy');
  if (restore && S.sourceId === sourceId) $('viewStats').innerHTML = before;
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

/* Where `anchor` sits in the view just built — or null when the view no
   longer has that row, or the view itself is already gone (a 409: evicted
   by a newer rebuild before this asked). Never rejects: the tag chips and
   the timeframe toggle call rebuildView without awaiting it, so a throw
   here would be an unhandled rejection over a grid that is otherwise fine. */
export async function rowPositionIn(v, anchor, signal) {
  try {
    const { pos } = await api(`/api/row_position?view_id=${v.view_id}&source_id=${anchor.source_id}&rid=${anchor.rid}`, { signal });
    return pos == null ? null : pos;
  } catch { return null; }
}

export async function rebuildView({ keepScroll = true, keepRow = true } = {}) {
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
  // is and only the cursor follows the row, so only the cursor counts.
  const anchor = keepRow ? cursorRowAnchor({ cursorOnly: keepScroll }) : null;
  const spec = currentSpec();
  // The cache key is the spec as the analyst set it: the op_token added
  // next is fresh per rebuild, and keying on it meant no reopen ever hit.
  const cacheKey = specKey(spec);
  spec.op_token = opToken();
  const seq = ++rebuildSeq;
  // Which table this rebuild is for; checked again before it paints.
  const forSourceId = S.sourceId;
  // Supersede the build in flight — before this one's own work, so the
  // writer lock is asked for once, not once per keystroke — and register
  // this one for the next rebuild to do the same to. A rebuild that
  // starts during the keys lookup below finds this one registered and
  // aborts it before it ever posts: fetch rejects at once on a signal
  // that is already aborted.
  const chipUp = cancelInflight();
  const controller = new AbortController();
  inflight = { token: spec.op_token, controller, seq, sourceId: forSourceId };
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
  // chrome — busy bar, chip, indicator — has started, so there is nothing
  // to undo. Starting it now would put this rebuild's indicator over the
  // newer one's and then, in the finally below, take it down and restore
  // the old count while the build that is actually running goes unmarked.
  if (seq !== rebuildSeq) return;
  pendingKeys = keys ? { sourceId: forSourceId, keys } : null;
  let v;
  let seeded = [];
  let pos = null;
  setBusy(true);
  const disarmCancel = chipUp ? armOpCancel(spec.op_token, 0) : armOpCancel(spec.op_token);
  startIndicator(seq, forSourceId, spec);
  try {
    try {
      v = await post('/api/view', spec, { signal: controller.signal });
    } catch (e) {
      // 499 = this build was cancelled; aborted = this client dropped the
      // request itself. Server-side the transaction rolled back with the
      // previous view intact (see build_view), so the rows on screen are
      // still real — just keep them.
      if (e.status === 499 || e.aborted) {
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
    // the cursor, so the lookup rides alongside the seed instead of ahead
    // of it and costs the paint nothing.
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
    setBusy(false);
    disarmCancel();
    if (inflight && inflight.seq === seq) inflight = null;
    // A build that landed writes its own count below; one that didn't
    // gets the old one back. (A superseded build owns neither — no-op.)
    stopIndicator(seq, !v);
  }
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
  $('viewStats').innerHTML =
    `<b>${v.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · ${v.elapsed_ms} ms`;
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
  // silent wrong-row display. Re-point it at the same row by identity, or
  // clear it, pane and all, when this view no longer has that row: an
  // honest empty is better than a highlight on a stranger. Untouched under
  // a grouping (regroupAll owns the cursor there) and for keepRow:false.
  if (keepRow && !S.groupByCols.length) {
    if (pos != null) S.cursor = pos;
    else if (S.cursor >= 0) { S.cursor = -1; $('detail').hidden = true; $('detailResize').hidden = true; }
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

export const rebuildSoon = debounce(() => rebuildView(), 220);

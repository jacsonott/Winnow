/* Building and rebuilding the materialized view behind the grid.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, OVERSCAN, PAGE, ROW_H, api, debounce, post, setBusy, toast } from './core.js';
import { currentSpec } from './filters.js';
import { clearPageCache, headH, rScroll, render, rowAt, spacerPx, vScroll } from './grid.js';
import { drawRail, regroupAll } from './grouping.js';
import { armOpCancel, opToken } from './jobs.js';
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
export async function rowPositionIn(v, anchor) {
  try {
    const { pos } = await api(`/api/row_position?view_id=${v.view_id}&source_id=${anchor.source_id}&rid=${anchor.rid}`);
    return pos == null ? null : pos;
  } catch { return undefined; }
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
  pendingKeys = keys ? { sourceId: forSourceId, keys } : null;
  let v;
  let seeded = [];
  let pos = null;
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
    const posP = anchor && v.row_count ? rowPositionIn(v, anchor) : Promise.resolve(null);
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
    if (keepScroll) pos = await posP;
  } finally {
    setBusy(false);
    disarmCancel();
  }
  // A newer rebuild started while this one was in flight — its view has
  // already evicted ours server-side; let it win.
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
    if (anchor) {
      if (pos != null) S.cursor = pos;
      else if (pos === null) drop();
    } else if (!keepScroll && S.cursor >= 0) {
      drop();
    }
  }
  const src = S.sources.find((s) => s.id === S.sourceId);
  $('spacerY').style.height = spacerPx(v.row_count) + 'px';
  $('noRows').hidden = v.row_count > 0;
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
  // Anything following the grid (plugin toolbar panels via
  // winnow.onViewChange) hears about the new view here — after the paint,
  // so a listener that reads S.view sees the settled state.
  document.dispatchEvent(new CustomEvent('winnow:viewchange',
    { detail: { sourceId: S.sourceId, viewId: v.view_id, rowCount: v.row_count } }));
  refreshTagCounts(); // the scope changed, so every ribbon count did too
  updateFiltersButton();
}

export const rebuildSoon = debounce(() => rebuildView(), 220);

/* Building and rebuilding the materialized view behind the grid.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, OVERSCAN, PAGE, ROW_H, api, debounce, post, setBusy, toast } from './core.js';
import { currentSpec } from './filters.js';
import { clearPageCache, headH, rScroll, render, spacerPx, vScroll } from './grid.js';
import { drawRail, regroupAll } from './grouping.js';
import { armOpCancel, opToken } from './jobs.js';
import { S, gridRowCount, selClear, specKey } from './state.js';
import { refreshTagCounts } from './tags.js';
import { updateFiltersButton } from './timeframe.js';

/* ----------------------------------------------------------------- view */

/* Monotonic token so an older rebuild that resolves after a newer one
   started can't swap its stale view/spec in over the newer one's — the
   race was always possible (two POSTs can complete out of order), and
   the pre-swap row prefetch below widens the in-flight window enough to
   care. */
export let rebuildSeq = 0;

export async function rebuildView({ keepScroll = true } = {}) {
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
  // Anything following the grid (plugin toolbar panels via
  // winnow.onViewChange) hears about the new view here — after the paint,
  // so a listener that reads S.view sees the settled state.
  document.dispatchEvent(new CustomEvent('winnow:viewchange',
    { detail: { sourceId: S.sourceId, viewId: v.view_id, rowCount: v.row_count } }));
  refreshTagCounts(); // the scope changed, so every ribbon count did too
  updateFiltersButton();
}

export const rebuildSoon = debounce(() => rebuildView(), 220);

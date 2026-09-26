/* The virtualized grid: paging, prefetch, painting, and cell-range selection.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { applyPin, colWidth, pinnedOffsets, visibleCols } from './columns.js';
import { $, GUTTER_W, MAX_SPACER_PX, OVERSCAN, PAGE, ROW_H, api, el } from './core.js';
import { syncHistogramMarker } from './histogram.js';
import { maybeShowDetail, showDetail } from './detail.js';
import { ensureGroupPage, findGroupAt, groupCoordAt, groupDataRowAt, isLeafLevel, renderGrouped, toggleGroup } from './grouping.js';
import { S, cellInRange, cellRangeAllSelected, cellRangeRowCount, cellRangeRows, clearCellSelection, gridRowCount, selAdd, selClear, selCount, selHas, selRangeApply, selRanges, selRemove, selReplace, selSnapshot, selToggle, selUndoAvailable, selUndoLast } from './state.js';
import { applyTag } from './tags.js';
import { displayCell } from './tsformat.js';
import { rebuildInFlight, rebuildView } from './view.js';

/* ------------------------------------------------------------ row paging */

/* Page cache ceiling. A 500-row page of a 27-column source is on the order
   of a megabyte of JS objects, and nothing used to evict them within a
   view's lifetime — so deep-scrolling a 1.2M-row view quietly accumulated
   the entire table in the JS heap. The DOM has only ever held the visible
   window (invariant #6); this makes memory follow the same rule.
   Holds the same ~50k-row idle-scrollback budget PAGE=500/100 pages did —
   10 * PAGE(5000) = 50k — comfortably more than any viewport plus overscan,
   and enough that ordinary back-and-forth scrolling still hits the cache. */
export const MAX_CACHED_PAGES = 10;

/* The page indices the grid is currently painting from. Never evicted:
   render() re-requests any page it needs, so dropping one would just be
   refetched on the very next frame — and, worse, ensurePage calls render()
   on arrival, so an eviction/refetch pair here would loop forever. */
export function visiblePageRange() {
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
export function trimPageCache(keep) {
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
   instead of waiting on the now-discarded ones.

   This is only the flat half. A grouping keeps its own page cache for the
   same view id (S.groupPages) and this one stays alive underneath it, so
   a tag write reaches for tags.js's clearRowCaches, which drops both; the
   two places that call this alone from a tag or note write do so because
   they patched the group-page rows in place and only the flat copies of
   those rids are stale. */
export function clearPageCache() {
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
export function ensurePage(idx, { keep, prefetch } = {}) {
  if (S.pages.has(idx)) return Promise.resolve();
  const inFlight = S.pending.get(idx);
  if (inFlight) return inFlight;
  if (!S.view) return Promise.resolve();   // nothing to page against
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
      // Expired = evicted by a build that finished after it was superseded.
      // If the superseding build is still in flight its landing is the
      // recovery — a rebuild from here would cancel it and start the same
      // spec over. Nothing in flight means nobody else will: rebuild.
      if (String(e.message).includes('expired') && !rebuildInFlight()) rebuildView();
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
export const PREFETCH_RADIUS = 1;

 // pages either side of the visible range

export const whenIdle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 500 }) : setTimeout(fn, 150));

/* Never cancelled on a view rebuild or a grouping change: the callback
   reads S.view / S.groups at fire time rather than closing over them, so a
   pass scheduled against the old view simply warms the right pages of the
   new one — and ensurePage's own generation check discards anything that
   was already in flight across the change. */
export let prefetchHandle = null;

export function schedulePrefetch() {
  if (prefetchHandle !== null) return;
  prefetchHandle = whenIdle(() => {
    prefetchHandle = null;
    if (!S.view) return;
    if (S.groupByCols.length) prefetchGroupPages();
    else prefetchFlatPages();
  });
}

export function prefetchFlatPages() {
  if (!S.view) return;   // exported: not every caller is schedulePrefetch
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
export function prefetchGroupPages() {
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
export function nextExpandedLeaf(gi, step) {
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
export const rowAt = (pos) => (S.groupByCols.length ? groupDataRowAt(pos) : S.rowsByPos.get(pos));

/* -------------------------------------------------------------- painting */

/* Kept in sync from render() (called after every S.selection mutation —
   row clicks, checkbox toggles, tag/copy actions that clear it, etc.)
   rather than from each of those sites individually. Disabled under a
   grouping: rows there *are* selectable, but this box means "every row in
   the view", and the flattened tree it would have to check is a mix of
   data rows and group headers whose collapsed groups aren't even loaded.
   Tag-the-whole-view (Shift + a tag hotkey) and the group menu's
   tag-this-group both do that job server-side without the ambiguity. */
export function syncSelectAllCheckbox() {
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
export function headH() { return $('gridHead').offsetHeight; }

/* The spacer height to use for `total` rows — capped, see MAX_SPACER_PX. */
export function spacerPx(total) { return Math.min(total * ROW_H, MAX_SPACER_PX); }

/* scrollTop as the rest of the grid means it: an offset into `total * ROW_H`
   pixels of rows. Identity below the spacer cap; a linear rescale above it.
   `head` is the in-scroller sticky header's height — headH() for the grid, 0
   for the timeline, whose header sits outside its scroller. Both ends are
   anchored (0 maps to 0, max-scroll maps to max-offset), so the last row is
   exactly reachable rather than merely nearly so. */
export function vScroll(scroller, total, head = 0) {
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
export function rScroll(scroller, total, virt, head = 0) {
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
export function rowsPaintY(scroller, virt, first) {
  const anchor = Math.floor(virt / ROW_H);
  return scroller.scrollTop - (virt - anchor * ROW_H) - (anchor - first) * ROW_H;
}

export function syncRowsTop() {
  const t = headH() + 'px';
  const rowsEl = $('rows');
  if (rowsEl.style.top !== t) rowsEl.style.top = t;
}

/* Explicit pixel width for #rows — the exact gutter + visible-column
   total this render pass is about to lay cells out against. See the
   .rows comment in style.css for why this isn't left to intrinsic
   (max-content) sizing. */
export function syncRowsWidth(widths, cols) {
  const w = GUTTER_W + cols.reduce((a, name) => a + widths[name], 0) + 'px';
  const rowsEl = $('rows');
  if (rowsEl.style.width !== w) rowsEl.style.width = w;
}

export function render() {
  if (!S.view) return;
  syncSelectAllCheckbox();
  // Grouped mode has no honest viewport span (group headers interleave
  // with rows, and a collapsed group's rows are not loaded at all), so
  // this is the call that CLEARS the marker as well as the one that moves it.
  if (S.groupByCols.length) { renderGrouped(); syncHistogramMarker(); return; }
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
  titleClippedCells();
  syncHistogramMarker();
}

/* A cell the column cut short says what it says, on hover — and only one
   that was actually cut.

   `LEVEL` painting `Informati…` with an empty title was the measured case:
   the full value was reachable by widening the column or opening the row,
   and nowhere else, while every other truncating surface in the app (tabs,
   sidebar rows, column headers) has always carried a title.

   Two things here are deliberate. It runs AFTER the rows are in the
   document, because scrollWidth on a node still in a DocumentFragment is
   0 — there is no way to ask "did this clip" before layout. And it titles
   only the cells that clipped, rather than every cell unconditionally:
   most values fit, and a title attribute on every cell of every painted
   row is DOM string the grid does not need (invariant #6 is about what
   the grid keeps in the document). The loop reads and never writes
   geometry, so the first scrollWidth forces the one layout the frame was
   going to do anyway and the rest come out of it — setting `title` cannot
   dirty layout, so this must stay the last thing a paint does.

   The 1px of slack is not superstition: scrollWidth and clientWidth are
   rounded integers over fractional text and box widths, so an
   exactly-fitting cell (an autofit column, most obviously) reports one
   pixel of overflow it does not visibly have, and every cell in the
   column would get a tooltip repeating what is already on screen. */
export function titleClippedCells() {
  for (const c of $('rows').querySelectorAll('.cell')) {
    if (c.scrollWidth > c.clientWidth + 1) c.title = c.textContent;
  }
}

/* Everything a paint pass hoists out of its row loop — built once per
   render and handed to buildDataRow for every row. Shared by the flat and
   grouped painters so a change to either lands in both. */
export function rowPaintContext() {
  const cols = visibleCols();
  return {
    cols,
    colMeta: Object.fromEntries(S.columns.map((c) => [c.name, c])),
    idx: Object.fromEntries(S.columns.map((c, i) => [c.name, i])),
    tagColor: Object.fromEntries(S.tags.map((t) => [t.id, t.color])),
    widths: Object.fromEntries(cols.map((name) => [name, colWidth(name)])),
    // Hoisted with the widths: pinned placement is identical for every row
    // in a pass, and recomputing it per row would walk the column list once
    // per painted row.
    pins: pinnedOffsets(),
    needle: S.search.trim().toLowerCase(),
    // A pivoted session comparison's rows, when they are this table's.
    diffRows: S.diffMarks && S.diffMarks.sourceId === S.sourceId ? S.diffMarks.rows : null,
  };
}

/* A session comparison's mark on a row: A, the left session only; B, the
   right only; A→B, both with different tags (or a note that differs).
   Shared with the panel's legend and the banner, so the pill in the
   gutter and the key explaining it can't drift apart. */
const DIFF_GLYPH = { removed: 'A', added: 'B', changed: 'A→B' };
const DIFF_ROW_CLASS = { removed: 'diff-a', added: 'diff-b', changed: 'diff-ab' };
export function diffMarkNode(kind, title) {
  const m = el('span', 'diff-mark diff-mark-' + kind, DIFF_GLYPH[kind]);
  if (title) m.title = title;
  return m;
}
/* One row can differ in its tags AND its note; the tags decide the glyph
   (they are what a review is about), and the title says both. */
export function diffKind(dm) {
  if (!dm.tags) return 'changed';
  if (!dm.tags.left.length) return 'added';
  if (!dm.tags.right.length) return 'removed';
  return 'changed';
}
const tagsText = (v) => (v.length ? v.join(', ') : 'no tags');
function diffMarkTitle(dm) {
  const { left, right } = S.diffMarks;
  const lines = [];
  if (dm.tags) lines.push(`A · ${left}: ${tagsText(dm.tags.left)}`, `B · ${right}: ${tagsText(dm.tags.right)}`);
  if (dm.note) lines.push(`note A · ${left}: ${dm.note.left || 'no note'}`, `note B · ${right}: ${dm.note.right || 'no note'}`);
  return lines.join('\n');
}

/* One data row's DOM. `pos` addresses the row the way the current mode
   does — a view position when flat, a flattened-tree position when grouped
   — and is what every delegated listener on #body reads back off
   dataset.pos. Grouped mode paints through here rather than through a
   reduced copy of it precisely so that selection, tag stripes, the note
   mark and the cell-range highlight can't be present in one mode and
   quietly missing in the other. */
export function buildDataRow(pos, r, ctx) {
  const { cols, colMeta, idx, tagColor, widths, pins, needle } = ctx;
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
  // The line number says whether this row is IN the cell rectangle. It is
  // the one column that never could: the range is painted per .cell and
  // the gutter is not one, so with a rectangle two columns wide an
  // analyst reading down the line numbers had nothing to read. Rows only
  // — a rectangle spans whole rows vertically, so a per-column answer
  // here would be a lie.
  if (S.cellRange && pos >= S.cellRange.r0 && pos <= S.cellRange.r1) g.classList.add('gutter-in-range');
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
    // A session comparison's mark: which side tagged this row (see
    // session.js pivotDiff).
    const dm = ctx.diffRows && ctx.diffRows[r.rid];
    if (dm) {
      const kind = diffKind(dm);
      mid.append(diffMarkNode(kind, diffMarkTitle(dm)));
      // The whole row wears its side's wash, not just the pill.
      row.classList.add(DIFF_ROW_CLASS[kind]);
    }
  }
  g.append(cb, mid, el('span', 'rid', r ? String(r.rid) : '·'));
  // How you open a row, said on the row. The gesture (double-click, or the
  // detail hotkey) is unchanged — this is a place to point at for an
  // analyst who single-clicked, got a highlighted cell, and had nothing on
  // screen telling them the whole row was one more click away. Only on a
  // row that has landed: there is nothing to show for a page still in
  // flight. It sits in the gutter's middle slot (see style.css) rather
  // than as a fourth column, so the three-slot contract the checkbox and
  // the rid line up against is untouched — the slot is named there, row
  // and column both, so appending it after the rid here does not decide
  // where it lands (it once did, and it landed a row down). aria-hidden
  // because the keyboard already has the hotkey, and one of these per
  // painted row would otherwise be read out as a screenful of identical
  // controls.
  if (r) {
    const open = el('span', 'row-open', '⤢');
    open.title = 'Open this row (double-click, or the detail key)';
    open.setAttribute('aria-hidden', 'true');
    g.append(open);
  }
  row.append(g);

  cols.forEach((name, ci) => {
    const c = el('div', 'cell' + (colMeta[name] && colMeta[name].type === 'number' ? ' num' : ''));
    c.style.flexBasis = widths[name] + 'px';
    applyPin(c, name, pins);
    c.dataset.col = ci;
    if (cellInRange(pos, ci)) c.classList.add('cell-selected');
    // The ring is where the next arrow steps from — without it a
    // keyboard analyst cannot tell which corner of a rectangle is live.
    if (S.cellFocus && S.cellFocus.pos === pos && S.cellFocus.col === ci) c.classList.add('cell-active');
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

export function renderTagToolbar() {
  const bar = $('tagToolbar');
  const count = selCount();
  // Grid chrome, not app chrome. It is position:fixed at the bottom of the
  // viewport, so it used to follow the analyst onto SQL, Timeline, Notes,
  // the watchlist, a dashboard and every plugin tab — offering to tag "12
  // selected" rows that nothing on that page has, beside a Clear selection
  // button for a selection they could not see. The selection itself is
  // kept: coming back to the grid brings the bar back with it.
  // A cell range spanning several rows is what a tag key would hit when
  // nothing is picked (applyTag) — say so, rather than tagging silently.
  const rangeRows = !count ? cellRangeRowCount() : 0;   // headings excluded — the same rows a tag key hits
  if ((!count && rangeRows < 2) || S.activeTab !== 'grid') { bar.hidden = true; return; }
  bar.hidden = false;
  const ranges = count && !S.selectAll ? selRanges() : 0;
  const label = count
    ? `${count.toLocaleString()} selected` + (ranges > 1 ? ` · ${ranges} ranges` : '')
    : `${rangeRows.toLocaleString()} rows in the cell range`;
  const countEl = el('span', 'tag-toolbar-count', label);
  if (S.selHidden) {
    countEl.append(el('span', 'tag-toolbar-warn', ` · ${S.selHidden.toLocaleString()} filtered out`));
    countEl.title = `${S.selHidden.toLocaleString()} picked row(s) are not in the current view — a tag applies to the ${count.toLocaleString()} it shows`;
  }
  bar.replaceChildren(countEl);
  const n = count || rangeRows;
  for (const t of S.tags) {
    const btn = el('button', 'tag-chip');
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    btn.append(sw, el('span', null, t.name));
    btn.title = `Tag ${n.toLocaleString()} row(s) as ${t.name}`;
    btn.onclick = () => applyTag(t);
    bar.append(btn);
  }
  if (count && !S.groupByCols.length) {
    const inv = el('button', 'btn ghost', 'Invert');
    inv.title = 'Select every row that is not picked, and drop the ones that are';
    inv.onclick = () => { selSnapshot(); selReplace(!S.selectAll, S.selection); render(); };
    bar.append(inv);
  }
  if (selUndoAvailable()) {
    const undo = el('button', 'btn ghost', 'Undo');
    undo.title = 'Take back the last selection change';
    undo.onclick = () => { if (selUndoLast()) { S.selHidden = 0; render(); } };
    bar.append(undo);
  }
  const clear = el('button', 'btn ghost', count ? 'Clear selection' : 'Clear');
  clear.onclick = () => { selSnapshot(); selClear(); clearCellSelection(); S.selHidden = 0; render(); };
  bar.append(clear);
}

export function highlight(node, text, needle) {
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

/* ------------------------------------------------------------- movement */

/* Moves the ROW cursor. The arrow keys no longer come through here —
   they drive the cell cursor (moveCell), and Shift+Arrow grows the cell
   rectangle rather than a run of row picks. What still calls this is
   every jump that is not an arrow: a cell click, the row-open chevron,
   jump-to-timestamp, opening a watchlist hit, the right-click path.

   It took an `extend` flag while the arrow keys drove it, for the
   Shift+Arrow run that grew the row picks. Nothing produces that gesture
   any more — every remaining caller passes a plain move — so the flag and
   its run machinery are gone from here rather than left as a branch the
   next reader would assume something reaches. The gutter's shift-click
   calls selRangeApply directly and is untouched. */
export function moveCursor(to) {
  const total = gridRowCount();
  if (!S.view || !total) return;
  to = Math.max(0, Math.min(total - 1, to));
  // Moving the cursor is looking, not choosing: the picks stay. They go
  // with Escape, the chip, or a new pick.
  S.anchor = to;
  S.cursor = to;
  /* Carry the cell cursor to the row the row cursor just moved to. Every
     jump that is not an arrow key comes through here — the row-open
     chevron, jump-to-timestamp, opening a watchlist hit, the right-click
     path — and each of them used to leave S.cellFocus on whatever row was
     last arrowed to. The next ArrowDown then read that stale focus and
     teleported the viewport back to it, taking the detail pane and the
     tag keys along; with Shift held it selected everything in between.
     The column is kept and the rectangle collapses, which is what a plain
     move means everywhere else in this feature. Deliberately NOT a
     clearCellSelection(): the cell click path sets the selection and THEN
     calls this, so clearing would wipe the click's own work.

     A cursor landing INSIDE the current rectangle leaves it alone. That
     is the right-click-within-a-selection case, where the menu's scope IS
     the rectangle (rowMenuTargets) — collapsing it there silently shrank
     "these four rows" to "this one cell" and re-enabled plugin actions
     that had been disabled for exceeding their row limit. */
  const insideRange = S.cellRange && to >= S.cellRange.r0 && to <= S.cellRange.r1;
  if (S.cellFocus && S.cellFocus.pos !== to && !insideRange) {
    S.cellFocus = { ...S.cellFocus, pos: to };
    S.cellAnchor = { pos: to, col: S.cellFocus.col };
    setCellRange(S.cellAnchor, S.cellFocus);
    S.cellRangeExplicit = false;   // the jump moved the cursor, it did not choose a cell
  }
  scrollIntoView(to);
  render();
  maybeShowDetail(to);
}

export function scrollIntoView(pos) {
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
export let bodyScrollRaf = null;

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
export let lastActivate = null;

 // {pos, time}
export function activateRow(pos, e) {
  const now = Date.now();
  const isDoubleActivate = !e.shiftKey && !e.metaKey && !e.ctrlKey
    && lastActivate && lastActivate.pos === pos && (now - lastActivate.time) < 400;
  lastActivate = isDoubleActivate ? null : { pos, time: now };

  if (e.metaKey || e.ctrlKey) {
    // Ctrl+click on a cell: add or drop that row — the one modifier that
    // still means rows on the cell surface. Shift means the cell range.
    selSnapshot();
    selToggle(pos);
    S.anchor = pos;
    S.cursor = pos; render(); maybeShowDetail(pos);
  } else moveCursor(pos);

  if (isDoubleActivate) showDetail(pos);
}

/* --------------------------------------------------- cell-range selection */

/* Separate from S.selection (row positions, used for tagging). This is a
   true rectangular cell selection like a spreadsheet — its own anchor,
   its own drag state — purely for reading/copying values. */

export function setCellRange(a, b) {
  S.cellRange = {
    r0: Math.min(a.pos, b.pos), r1: Math.max(a.pos, b.pos),
    c0: Math.min(a.col, b.col), c1: Math.max(a.col, b.col),
  };
}

/* Clicking a cell and checking a row's checkbox are two different ways to
   pick "what to copy," and they should stay mutually exclusive rather than
   one silently shadowing the other: clicking a cell commits a (possibly
   1-cell) range immediately AND clears row selection (via activateRow's
   plain-click -> moveCursor(pos) path, which already clears
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


/* ----------------------------------------- keyboard cell movement */

/* Excel's model in the terms this grid already had: S.cellFocus is the
   active cell, S.cellAnchor is the corner a Shift run extends FROM, and
   S.cellRange is the rectangle between them. A plain move drags both
   corners along together, so the rectangle collapses to the one cell the
   analyst is standing on; Shift moves only the focus, so the rectangle
   grows from wherever the anchor was left — by an earlier arrow or by a
   mouse drag, since the pointer paths below set the same two fields.

   A keypress can therefore create a cell range where the mouse used to be
   the only thing that could. That is why handleCopyShortcut lets picked
   rows beat a ONE-cell range (grouping.js): a single cell is where you
   are, a rectangle is what you chose. */

/* The next position up or down that can hold a cell. A group heading owns
   a position but has no cells — grouping.js paints one spanning element —
   so an active cell parked on one would be invisible, and every consumer
   that asks which rows a range means skips it anyway (cellRangeRows).
   Arrows step over headings rather than landing on them. */
function nextCellRow(from, step) {
  const total = gridRowCount();
  let p = from + step;
  while (p >= 0 && p < total && S.groupByCols.length && !groupCoordAt(p)) p += step;
  return p >= 0 && p < total ? p : null;
}

/* The first or last position that can hold a cell — Ctrl+Up / Ctrl+Down.
   Walks inward past trailing headings for the same reason. */
function edgeCellRow(step) {
  const total = gridRowCount();
  if (!total) return null;
  let p = step > 0 ? total - 1 : 0;
  while (p >= 0 && p < total && S.groupByCols.length && !groupCoordAt(p)) p -= step;
  return p >= 0 && p < total ? p : null;
}

/* Bring a column into view horizontally — the counterpart of
   scrollIntoView(pos) above, and deliberately NOT the DOM's own
   scrollIntoView: the gutter and every pinned column are position:sticky
   over the left edge of the scroller, so a cell scrolled flush to
   scrollLeft parks UNDERNEATH them and the analyst watches the highlight
   disappear. The sticky block's width is the real left edge, and it is
   the same sum pinnedOffsets() already computes for the paint. */
export function scrollColIntoView(ci) {
  const body = $('body');
  const cols = visibleCols();
  const name = cols[ci];
  if (!name || !body) return;
  const pins = pinnedOffsets();
  if (pins[name] !== undefined) return;   // a pinned column is never off screen
  let left = GUTTER_W;
  for (let i = 0; i < ci; i++) left += colWidth(cols[i]);
  let stuck = GUTTER_W;
  for (const n of cols) if (pins[n] !== undefined) stuck += colWidth(n);
  const w = colWidth(name);
  if (left < body.scrollLeft + stuck) body.scrollLeft = left - stuck;
  else if (left + w > body.scrollLeft + body.clientWidth) body.scrollLeft = left + w - body.clientWidth;
}

/* One step of the cell cursor. dr/dc are -1, 0 or 1; `edge` turns the
   step into a jump to the far end (Ctrl); `extend` keeps the anchor where
   it is so the rectangle grows (Shift). Every keyboard movement action
   goes through here, so the active cell and the row cursor can never
   drift apart. */
export function moveCell({ dr = 0, dc = 0, extend = false, edge = false, rows = 1 } = {}) {
  const total = gridRowCount();
  const cols = visibleCols();
  if (!S.view || !total || !cols.length) return;
  const lastCol = cols.length - 1;
  /* Cold start: the arrows have to work on a grid nobody has clicked in.
     The row cursor is wherever the analyst was last put — a search hit, a
     row opened from the watchlist — so the first press continues from
     there instead of teleporting to the top of the table. */
  if (!S.cellFocus) {
    const onHeading = S.groupByCols.length && !groupCoordAt(S.cursor);
    const pos = S.cursor >= 0 && !onHeading ? S.cursor : edgeCellRow(-1);
    if (pos == null) return;
    S.cellFocus = { pos, col: 0, name: cols[0] };
    S.cellAnchor = { pos, col: 0 };
  }
  let { pos, col } = S.cellFocus;
  if (dr) {
    if (edge) {
      const to = edgeCellRow(dr);
      if (to != null) pos = to;
    } else {
      // PageUp/PageDown ask for a stride; a heading inside it is stepped
      // over one row at a time, so a page never lands on one.
      for (let i = 0; i < rows; i++) {
        const to = nextCellRow(pos, dr);
        if (to == null) break;
        pos = to;
      }
    }
  }
  if (dc) col = edge ? (dc > 0 ? lastCol : 0) : Math.max(0, Math.min(lastCol, col + dc));
  S.cellFocus = { pos, col, name: cols[col] };
  if (!extend) S.cellAnchor = { pos, col };
  setCellRange(S.cellAnchor, S.cellFocus);
  // Shift asked for a rectangle; a plain arrow only moved the cursor and
  // left a one-cell range where it stopped. Copy is the one consumer that
  // needs to tell those apart — see handleCopyShortcut.
  S.cellRangeExplicit = extend;
  /* The row cursor follows the active cell: the detail pane, the note
     box, the tag keys and the row menu all read S.cursor, and a cell
     highlight on one row while those act on another is the bug this
     feature would otherwise ship. S.anchor follows too, so a later
     shift-click in the gutter extends from where the keyboard left off,
     exactly as it did when the arrows drove the row cursor directly. */
  S.cursor = pos;
  if (!extend) S.anchor = pos;
  scrollIntoView(pos);
  scrollColIntoView(col);
  render();
  maybeShowDetail(pos);
}

export let cellDragging = false;

export let cellDragRaf = null;

/* What the two Space keys mean, across the three functions below.
   Both keys fall through to toggleCursorRow when there is no
   rectangle to speak of, which is every press before the analyst
   has touched a cell, and every press after the gutter (which
   clears the cell selection) has had one.

   Space is a toggle, and what it toggles is whatever the cell selection
   says you are pointing at: a rectangle spanning several rows is ONE
   block of rows, not the single row the active cell happens to sit on.
   All-or-nothing across that block, the way a header checkbox behaves —
   anything short of every row picked means the press picks the rest, and
   only a fully picked block is let go. Flipping each row on its own is
   the obvious alternative and is wrong: on a half-picked range it merely
   swaps which half is picked, and a second press swaps it back, so the
   block can never be made whole.

   The rectangle SURVIVES the press, which is what gives a second press
   something to act on — Shift+Space clears it instead, and if Space did
   too there would be no block left to let go of. Letting it go is not an
   undo, though: a row picked BEFORE the rectangle was drawn goes with
   the block if the rectangle covers it, because all-or-nothing means the
   block and not "the rows this press added". The toolbar's Undo, which
   the selSnapshot below feeds, is the thing that restores the state
   before a press.

   Shift+Space is the other half: it only ever adds, and it spends the
   rectangle doing it — the gesture for gathering several rectangles into
   one set of picks, where a toggle would undo the last one gathered. */
export function toggleCellRangeRows() {
  if (cellRangeRowCount() < 2) return false;   // one row IS the cursor row; leave it to the plain path
  selSnapshot();
  /* S.anchor is deliberately left alone. It is the corner a later
     Shift+click in the gutter extends FROM, and on a rectangle that is
     already the row the run started on — the Shift+Arrow origin, or a
     drag's mousedown. Moving it to r0 (what selectCellRangeRows does,
     which loses the direction of an upward run) or to the cursor would
     spend that on a gesture that chose no new origin. */
  selRangeApply(S.cellRange.r0, S.cellRange.r1, !cellRangeAllSelected());
  render();
  return true;
}
export function toggleCursorRow() {
  if (!S.view || S.cursor < 0) return;
  if (S.groupByCols.length && !groupCoordAt(S.cursor)) return;
  selSnapshot();
  selToggle(S.cursor);
  S.anchor = S.cursor;
  render();
}
export function selectCellRangeRows() {
  if (!S.cellRange) return false;
  selSnapshot();
  selRangeApply(S.cellRange.r0, S.cellRange.r1, true);
  S.anchor = S.cellRange.r0;
  clearCellSelection();
  render();
  return true;
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireGrid() {
$('body').addEventListener('scroll', () => {
  if (!bodyScrollRaf) bodyScrollRaf = requestAnimationFrame(() => { bodyScrollRaf = null; render(); });
}, { passive: true });

/* Remote session mode: replace the browser's smooth pixel scrolling with
   whole-row jumps. Smooth scrolling animates a wheel notch through a dozen
   intermediate frames, each shifting the whole viewport a few pixels —
   which a remote display protocol must re-encode as a full-region change
   per frame. One quantized jump per notch is a single repaint, the same
   thing that makes native grids feel fine over RDP. The accumulator turns
   trackpad pixel deltas into the same discrete row steps. */
let wheelAcc = 0;
const REMOTE_PX_PER_ROW = 33; // ≈ one Chrome wheel notch (100px) → 3 rows, the Windows default
$('body').addEventListener('wheel', (e) => {
  if (!S.appearance.remoteSession) return;
  if (e.ctrlKey || !e.deltaY) return; // browser zoom / horizontal-only: leave native
  e.preventDefault();
  let rows;
  if (e.deltaMode === 1) rows = Math.round(e.deltaY);                     // lines (Firefox)
  else if (e.deltaMode === 2) rows = Math.round(e.deltaY * (($('body').clientHeight / ROW_H) - 1)); // pages
  else {
    wheelAcc += e.deltaY;
    rows = Math.trunc(wheelAcc / REMOTE_PX_PER_ROW);
    wheelAcc -= rows * REMOTE_PX_PER_ROW;
  }
  if (!rows) return;
  const b = $('body');
  b.scrollTop = Math.max(0, Math.round(b.scrollTop / ROW_H) * ROW_H + rows * ROW_H);
}, { passive: false });

$('body').addEventListener('click', (e) => {
  const groupHeader = e.target.closest('.group-header-row');
  if (groupHeader) {
    // A click that ends a TEXT SELECTION is a copy gesture, not a toggle —
    // collapsing the group under someone highlighting its value rips the
    // thing they were copying off the screen.
    if (!window.getSelection().isCollapsed) return;
    toggleGroup(Number(groupHeader.dataset.groupIdx));
    return;
  }
  // The gutter is the row handle and is handled on mousedown below; the
  // checkbox's native toggle is suppressed there too (its state is
  // painted from the selection). .cell clicks are handled from mousedown
  // as well. Nothing is left for a click on a row.
  if (e.target.closest('.rowcheck')) e.preventDefault();
});

/* The gutter is the row handle — all 104 px of it.

   The 12px box and the digits beside it were the only targets, with a
   dead strip between them that CLEARED the selection when hit. Now a
   mousedown anywhere in the gutter toggles that row; Shift+click adds the
   run from the last pick (Ctrl+Shift+click removes it) without dropping
   what was picked before; and dragging selects the span from the press to
   the pointer — worked out from the pointer's y, not from whatever element
   it happens to be over, so a fast drag never skips a row and dragging
   back shrinks the span. Past the top or bottom edge the grid scrolls and
   the span follows.

   Deliberately NOT routed through activateRow/moveCursor: the cursor
   follows, so the detail pane and the keyboard stay on the row you just
   picked, but picking is never clearing. */
let gutterDrag = null;   // {from, on, base: {selectAll, selection}} while the button is down
let dragLastY = 0;
let dragRaf = null;
let autoScroll = null;
const AUTOSCROLL_EDGE = 28;

function gutterRowAt(target) {
  const g = target.closest('.gutter');
  if (!g) return -1;
  const row = g.closest('.row');
  if (!row) return -1;
  const pos = Number(row.dataset.pos);
  // Grouped mode interleaves group headings into the position space, and a
  // heading is not a row anything can select (selRangeApply skips them too).
  if (S.groupByCols.length && !groupCoordAt(pos)) return -1;
  return pos;
}

/* The position under a viewport y, from geometry: the pointer may be
   between rows, over the sticky header, or moving faster than mousemove
   samples. Clamped to the view. */
function rowAtClientY(clientY) {
  const body = $('body');
  const rect = body.getBoundingClientRect();
  const y = clientY - rect.top + body.scrollTop - headH();
  return Math.max(0, Math.min(gridRowCount() - 1, Math.floor(y / ROW_H)));
}

function applyGutterSpan(pos) {
  if (pos === gutterDrag.last) return;
  gutterDrag.last = pos;
  S.selectAll = gutterDrag.base.selectAll;
  S.selection = new Set(gutterDrag.base.selection);
  S.selVersion++;
  selRangeApply(gutterDrag.from, pos, gutterDrag.on);
  S.cursor = pos;
  if (!dragRaf) dragRaf = requestAnimationFrame(() => { dragRaf = null; render(); });
}

let autoDy = 0;
function stopAutoScroll() { if (autoScroll) { clearInterval(autoScroll); autoScroll = null; } autoDy = 0; }

$('body').addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const pos = gutterRowAt(e.target);
  if (pos < 0) return;
  // The row's own "open me" affordance is the one thing in the gutter that
  // isn't picking, so it has to be taken before the toggle below. On
  // mousedown like the rest of this handler, and for the same reason: the
  // render these two calls do replaces the pressed node before the button
  // comes back up, and a 'click' listener on a detached target never
  // fires. The cursor moves with it because the pane, its note box and
  // Copy row all read the cursor row — opening one row while another is
  // current is how "Copy row" would copy a row nobody was looking at.
  if (e.target.closest('.row-open')) {
    e.preventDefault();
    moveCursor(pos);
    showDetail(pos);
    $('body').focus();   // as every other gutter gesture does: the arrow keys keep working
    return;
  }
  e.preventDefault();   // no native text-drag off the digits, no native checkbox toggle
  selSnapshot();
  if (e.shiftKey && S.anchor >= 0) {
    selRangeApply(S.anchor, pos, !(e.ctrlKey || e.metaKey));
  } else {
    // The base is the state BEFORE this press, captured before the toggle:
    // re-applying the span from it is what lets the drag shrink again.
    const base = { selectAll: S.selectAll, selection: new Set(S.selection) };
    const on = !selHas(pos);
    on ? selAdd(pos) : selRemove(pos);
    gutterDrag = { from: pos, on, base, last: pos };
    S.anchor = pos;
  }
  // Picking rows is a fresh "what to copy" choice, so a stale cell
  // rectangle must not win over it.
  clearCellSelection();
  S.cursor = pos;
  render();
  maybeShowDetail(pos);
  $('body').focus();
});

$('body').addEventListener('mousemove', (e) => {
  if (!gutterDrag) return;
  dragLastY = e.clientY;
  const pos = rowAtClientY(e.clientY);
  if (pos !== S.cursor) applyGutterSpan(pos);
  const rect = $('body').getBoundingClientRect();
  const dy = e.clientY < rect.top + headH() + AUTOSCROLL_EDGE ? -ROW_H
    : e.clientY > rect.bottom - AUTOSCROLL_EDGE ? ROW_H : 0;
  // Restart the interval only when the direction changes: tearing it down
  // on every mousemove meant it never fired while the pointer moved, and
  // a pointer at the edge always moves a little.
  if (dy !== autoDy) {
    stopAutoScroll();
    autoDy = dy;
    if (dy) {
      autoScroll = setInterval(() => {
        if (!gutterDrag) { stopAutoScroll(); return; }
        $('body').scrollTop += dy;
        applyGutterSpan(rowAtClientY(dragLastY));
      }, 40);
    }
  }
});

document.addEventListener('mouseup', () => {
  if (!gutterDrag) return;
  gutterDrag = null;
  stopAutoScroll();
  if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = null; }
  render();
});

$('body').addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const cell = e.target.closest('.cell');
  if (!cell) return;
  e.preventDefault(); // don't let the browser's native text-drag-select fight our highlight
  const pos = Number(cell.closest('.row').dataset.pos);
  const col = Number(cell.dataset.col);
  if (e.shiftKey && S.cellAnchor) {
    // Shift on a cell is the cell rectangle and nothing else: it no longer
    // also picks rows (that ambiguity was what made Ctrl+C and a tag key
    // disagree). The toolbar says how many rows the range spans, and
    // Shift+Space turns it into row picks.
    S.cellFocus = { pos, col, name: visibleCols()[col] };
    setCellRange(S.cellAnchor, S.cellFocus);
    S.cellRangeExplicit = true;
    S.cursor = pos;
    render();
  } else {
    S.cellAnchor = { pos, col };
    S.cellFocus = { pos, col, name: visibleCols()[col] };
    setCellRange(S.cellAnchor, S.cellAnchor); // commit immediately so a plain click alone selects that one cell
    S.cellRangeExplicit = true;               // clicking a cell IS asking for it
    cellDragging = true;
    activateRow(pos, e); // renders once, atomically, with the cell range above
  }
  $('body').focus();
});


$('body').addEventListener('mousemove', (e) => {
  if (!cellDragging) return;
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const pos = Number(cell.closest('.row').dataset.pos);
  const col = Number(cell.dataset.col);
  S.cellFocus = { pos, col, name: visibleCols()[col] };
  setCellRange(S.cellAnchor, S.cellFocus);
  S.cellRangeExplicit = true;
  if (cellDragRaf) return;
  cellDragRaf = requestAnimationFrame(() => { render(); cellDragRaf = null; });
});

document.addEventListener('mouseup', () => {
  if (!cellDragging) return;
  cellDragging = false;
  if (cellDragRaf) { cancelAnimationFrame(cellDragRaf); cellDragRaf = null; }
  render(); // guarantee the final drag state is painted, don't rely on a pending rAF firing
});
}

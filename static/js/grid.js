/* The virtualized grid: paging, prefetch, painting, and cell-range selection.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { colWidth, visibleCols } from './columns.js';
import { $, GUTTER_W, MAX_SPACER_PX, OVERSCAN, PAGE, ROW_H, api, el } from './core.js';
import { maybeShowDetail, showDetail } from './detail.js';
import { ensureGroupPage, findGroupAt, groupCoordAt, groupDataRowAt, isLeafLevel, renderGrouped, toggleGroup } from './grouping.js';
import { S, cellInRange, gridRowCount, selAdd, selClear, selCount, selHas, selRemove, selSetRange, selToggle } from './state.js';
import { applyTag } from './tags.js';
import { displayCell } from './tsformat.js';
import { rebuildView } from './view.js';

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
   instead of waiting on the now-discarded ones. */
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
export function rowPaintContext() {
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
export function buildDataRow(pos, r, { cols, colMeta, idx, tagColor, widths, needle }) {
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

export function renderTagToolbar() {
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

export function moveCursor(to, extend) {
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

  if (e.shiftKey) moveCursor(pos, true);
  else if (e.metaKey || e.ctrlKey) {
    selToggle(pos);
    S.cursor = pos; render(); maybeShowDetail(pos);
  } else moveCursor(pos, false);

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

export let cellDragging = false;

export let cellDragRaf = null;

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
}

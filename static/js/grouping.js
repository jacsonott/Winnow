/* Group-by: the group strip, nested levels, and grouped-mode rendering.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { draggedCol, renderHead, saveLayout, visibleCols } from './columns.js';
import { $, OVERSCAN, PAGE, ROW_H, api, el, post, setBusy, toast } from './core.js';
import { displayValue, ellipsize, filterByValue } from './filters.js';
import { buildDataRow, ensurePage, headH, moveCursor, render, renderTagToolbar, rowAt, rowPaintContext, rowsPaintY, schedulePrefetch, setCellRange, spacerPx, syncRowsTop, syncRowsWidth, vScroll } from './grid.js';
import { armOpCancel, opToken } from './jobs.js';
import { openRowContextMenu } from './rowmenu.js';
import { S, selClear, selCount, selHas, selPositions, selRemap, selSetRange } from './state.js';
import { BULK_TAG_CONFIRM_AT, refreshTagCounts, refreshUndoState, renderTagRibbon } from './tags.js';
import { confirmDialog, contextMenu, dropdownMenu } from './ui.js';
import { rebuildView } from './view.js';

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

export function isLeafLevel(level) { return level === S.groupByCols.length - 1; }

export function rebuildGroupPrefix() {
  let pos = 0;
  S.groupPrefix = S.groups.map((g) => {
    const start = pos;
    pos += 1 + (g.expanded && isLeafLevel(g.level) ? g.rowCount : 0);
    return start;
  });
  S.groupTotalRows = pos;
}

export function findGroupAt(vpos) {
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
export function ensureGroupPage(g, pageIdx, { prefetch } = {}) {
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
export function clearGroupPageCache() {
  S.groupPages.clear();
  S.groupPending.clear();
  S.groupPageGen++;
}

export function groupRowAt(g, localIdx) {
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
export function groupCoordAt(vpos) {
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
export function groupDataRowAt(vpos) {
  const c = groupCoordAt(vpos);
  return c ? groupRowAt(c.g, c.localIdx) : null;
}

/* Loads every page the given flattened positions need, or throws — the
   grouped-mode counterpart of waitForPages, and it exists for the same
   reason that one does: a bulk tag or copy that quietly skipped the rows
   it couldn't load would corrupt the analyst's record of what they've
   triaged while looking like it worked. Bounded the same way, since these
   are the same single-connection backend's pages. */
export async function waitForGroupPages(positions) {
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
export function loadRowsForPositions(positions) {
  if (S.groupByCols.length) return waitForGroupPages(positions);
  return waitForPages([...new Set(positions.map((p) => Math.floor(p / PAGE)))]);
}

/* True when at least one of these positions still needs a fetch — drives
   the "Copying N rows…" toast, which is only worth showing for a copy that
   will actually go to the network. */
export function positionsNeedLoading(positions) {
  if (!S.groupByCols.length) return positions.some((p) => !S.pages.has(Math.floor(p / PAGE)));
  return positions.some((pos) => {
    const c = groupCoordAt(pos);
    return c && !S.groupPages.has(`${c.g.viewId}:${Math.floor(c.localIdx / PAGE)}`);
  });
}

export function renderGroupHeaderRow(g, gi) {
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

export function renderGrouped() {
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

export function makeGroupNode(gr, level, path) {
  return { value: gr.value, count: gr.count, level, path, expanded: false, viewId: null, rowCount: gr.count };
}

/* Fetches one level's groups, scoped by `path` (every outer level already
   fixed) — level is implied by path.length, since groupByCols is ordered. */
export async function fetchGroupLevel(path) {
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
export function clearGroupSelectionState() {
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
export function shiftGroupPositions(headerPos, oldTotal) {
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
export function groupShiftAnchor(gi) {
  rebuildGroupPrefix();
  return [S.groupPrefix[gi], S.groupTotalRows];
}

export async function toggleGroup(gi) {
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

export async function closeAllGroupViews() {
  for (const g of S.groups) {
    if (g.viewId) { api(`/api/view/${g.viewId}`, { method: 'DELETE' }).catch(() => {}); g.viewId = null; }
  }
}

/* A node's identity across rebuilds: its chain of group values from the
   root down. Only comparable while S.groupByCols is unchanged — the caller
   below checks that before using these keys. */
function groupKey(g) {
  return JSON.stringify([...g.path.map((p) => p.value), g.value]);
}

/* Guards the re-expand loop below: each await inside it is a window for a
   newer regroupAll to start, and re-expanding into a tree that newer call
   just replaced would splice stale children into it. */
let regroupGen = 0;

/* Rebuilds the top level of the group tree from scratch — called whenever
   the grouping columns/sort change, or the underlying view is rebuilt
   (filter/search/sort — the old group views are gone with it regardless). */
export async function regroupAll() {
  const gen = ++regroupGen;
  // Which nodes are open, so a view rebuild under the SAME grouping (sort
  // by a column, tweak a filter, type a search) reopens them instead of
  // collapsing the tree the analyst was reading. When the grouping columns
  // themselves changed, the old paths describe a different tree and the
  // snapshot is dropped.
  const prevCols = JSON.stringify(S.groupByCols);
  const wasOpen = new Set(S.groups.filter((g) => g.expanded).map(groupKey));
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
    if (gen !== regroupGen) return;
    S.groups = top.map((gr) => makeGroupNode(gr, 0, []));
  } catch (e) {
    if (gen !== regroupGen) return;
    toast('Group-by failed: ' + e.message, 4000);
    S.groupByCols = [];
    renderGroupStrip();
  }
  if (wasOpen.size && JSON.stringify(S.groupByCols) === prevCols) {
    // Walk forward so a reopened parent's freshly spliced children are
    // themselves visited (they sit right after it) — nested open levels
    // come back too. A group the new view no longer contains simply never
    // matches its key and stays gone.
    for (let gi = 0; gi < S.groups.length; gi++) {
      const g = S.groups[gi];
      if (!g.expanded && wasOpen.has(groupKey(g))) {
        await toggleGroup(gi);
        if (gen !== regroupGen) return;
      }
    }
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
export const TAG_GROUP_COLUMN = '__tag__';

export const isTagGroupCol = (c) => c === TAG_GROUP_COLUMN;

/* What a grouping level is called on screen. */
export function groupColLabel(column) {
  return isTagGroupCol(column) ? 'Tag' : column;
}

/* What one group's value is called on screen: a tag's name for a tag
   level (falling back to its id if the tag was deleted underneath the
   grouping), the value itself otherwise. */
export function groupValueLabel(column, value) {
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
export function addGroupLevel(column) {
  if (S.groupByCols.includes(column)) return;
  if (!S.preGroupOrder) S.preGroupOrder = [...S.order];
  S.groupByCols.push(column);
  S.order = S.order.filter((n) => n !== column);
  renderHead();
  regroupAll();
}

export function removeGroupLevel(i) {
  const [removed] = S.groupByCols.splice(i, 1);
  if (!S.groupByCols.length) { dropGrouping(); return; }
  // The tag pseudo-column has no column to give back to the layout.
  if (!isTagGroupCol(removed) && !S.order.includes(removed)) S.order.push(removed);
  renderHead();
  regroupAll();
}

export async function dropGrouping() {
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
export function setGrouping(cols, gsort, gdir) {
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
export async function toggleGrouping() {
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

export let draggedPillIdx = null;

export function wireGroupPillDrag(pill, idx) {
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
export const GROUP_SORT_OPTIONS = [
  { by: 'count', dir: 'desc' },
  { by: 'count', dir: 'asc' },
  { by: 'value', dir: 'asc' },
  { by: 'value', dir: 'desc' },
];

export function groupSortLabel(by, dir, short) {
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
export function regroupIfGroupedByTag() {
  if (S.groupByCols.some(isTagGroupCol)) regroupAll();
}

/* The tag pseudo-column has no header to drag, so the strip carries its
   own way in. Offered as long as it isn't already a level — grouping by
   tag twice would be two identical levels. */
export function groupByTagButton() {
  const btn = el('button', 'btn ghost group-tag-btn', '+ Tag');
  btn.title = 'Add a grouping level that buckets rows by the tags on them';
  btn.onclick = () => addGroupLevel(TAG_GROUP_COLUMN);
  return btn;
}

export function renderGroupStrip() {
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

export async function drawRail() {
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

/* ------------------------------------------------- group header actions */

/* A view id covering exactly this group's rows. A leaf group that's already
   expanded has one; anything else gets a throwaway built the same way, since
   expand_group scopes by the group's column/value *plus* its path and so
   answers for an outer level just as well as a leaf. Returns a release()
   the caller must call — a no-op for the borrowed leaf view, a DELETE for
   the throwaway, so tagging an unexpanded group doesn't leak a v.view_N
   per right-click. */
export async function groupRowsView(g) {
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
export async function tagWholeGroup(g, tag, on) {
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
export function groupRowSpan(gi) {
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
export let groupMenuUntagMode = false;

export function groupMenuItems(gi) {
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

export function openGroupContextMenu(gi, e) {
  groupMenuUntagMode = false; // every menu opens in the common (apply) direction
  contextMenu(e, () => groupMenuItems(gi));
}

/* How many page fetches are allowed to be in flight at once. Unbounded, a
   select-all + Ctrl+C on a 1.2M-row view fired ~2,400 simultaneous requests
   at a single-connection SQLite backend; the browser queues most of them
   anyway, so the only thing the fan-out bought was a thundering herd.
   Re-checked against PAGE=5000 (was 500): the same 1.2M-row worst case is
   now 240 page fetches, not 2,400 — strictly less pressure, not more, so
   this and the 20,000-row copy cap below (a row-count ceiling, independent
   of PAGE) don't need to change. */
export const PAGE_FETCH_CONCURRENCY = 6;

/* Loads every listed page, or throws.

   Both properties matter. The old version fired one ensurePage per missing
   page at once and then polled with a hard 8-second deadline — after which
   it returned *successfully* with pages still missing, and its callers
   happily emitted '' for every row they couldn't find. A copy that quietly
   contains blank rows is worse than one that fails: it looks right. So this
   is bounded, has no deadline (the work is proportional to what was asked
   for, and setBusy/toast already tell the analyst it's running), and
   surfaces a failed fetch as a rejection instead of a silent gap. */
export async function waitForPages(pageIndices) {
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
export function legacyCopyText(text) {
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
export async function writeClipboardText(textPromise, successMsg) {
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

export async function copySelectedCells(withHeaders) {
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

export async function copyRowsAsText(positions, withHeaders) {
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
export async function handleCopyShortcut(withHeaders) {
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

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireGrouping() {
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
}

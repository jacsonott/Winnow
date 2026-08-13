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
const GUTTER_W = 104; // keep in sync with `.gutter { width: ... }` in style.css
const AUTOFIT_MAX_W = 480; // upper limit for autofit-to-content column widths

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
  columns: [],          // [{name, type}]
  layout: {},           // name -> {w, hidden, pinned}
  order: [],            // column names in display order
  filters: {},          // name -> raw filter text
  sort: [],             // [{column, dir}]
  search: '',
  searchMode: 'contains', // 'contains' | 'regex' | 'advanced'
  searchTerms: [],        // advanced mode: [{term, connector: 'AND'|'OR', exclude: bool}]
  filterTree: { type: 'group', op: 'AND', children: [] }, // guided filter builder
  tagFilter: [],        // tag ids, or ['__any__'] / ['__none__']
  view: null,           // {view_id, row_count}
  pages: new Map(),      // page index -> rows; capped, see MAX_CACHED_PAGES/trimPageCache
  pending: new Map(),    // page index -> in-flight fetch promise, so concurrent callers share one request
  pageGen: 0,            // bumped by clearPageCache() so an in-flight fetch issued before it can't repopulate stale rows
  tags: [],
  tagCounts: {},
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
  groupPending: new Set(),
  savedFilters: [],        // cross-case cyclable filters, loaded from workspace/filters.json
  headerNicknames: [],     // [{id, col_names, nickname}] — friendly names for a header set
  timeRange: { enabled: false, column: null, start: '', end: '' }, // survives filter/preset/tab changes on purpose — see toggleTimeRange
  timelineTemplates: [], // [{id, col_names, type_label, timestamp_column, body_columns}] — workspace/timeline_templates.json
  importProfiles: [],    // [{id, name, extensions, include_patterns, exclude_patterns, recursive}] — workspace/import_profiles.json
  sidebarFilter: '',      // substring filter typed into the sidebar's own search box
  timeline: {
    view: null, pages: new Map(), pending: new Set(), reqId: 0,
    tagFilter: null, // tag ids currently checked; null = not yet initialized (defaults to "every known tag" on first load)
  },
  savedFilterCursor: -1,   // index into filtersForCurrentSource(), for [ and ] cycling
  cases: [],               // home screen's case registry, from workspace/cases.json
  homeSearch: '',          // home screen's case/group name filter, persisted across re-renders
  homeShowOlder: false,    // reveals cases not opened in >30 days once toggled
  activeTab: 'grid',       // 'grid' | 'sql' — which of #grid/#sqlview the pinned SQL tab has swapped in
  tabOrder: [],            // source/merge ids, drag-reordered — ids not listed here sort after, in loadSources() order
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

const selViewRows = () => (S.view ? S.view.row_count : 0);

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
  for (let p = Math.min(from, to); p <= Math.max(from, to); p++) S.selection.add(p);
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
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    // The status rides along so callers can tell "you asked for something
    // invalid" (4xx) from "the server broke" (5xx) — the server is careful
    // to only 400 the former, and blaming an analyst's filter for a backend
    // defect sends them off fixing something that isn't wrong.
    const err = new Error(msg);
    err.status = r.status;
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

function raggedNote(rec) {
  return rec.ragged_rows
    ? ` · ${rec.ragged_rows.toLocaleString()} ragged row${rec.ragged_rows === 1 ? '' : 's'} padded/trimmed to fit`
    : '';
}

function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
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
    t.setAttribute('aria-selected', String(s.id === S.sourceId));
    if (s.error) {
      t.append(el('span', null, `⚠ ${s.name}`));
      t.title = s.error;
    } else {
      t.append(el('span', null, (s.is_merge ? '⛓ ' : '') + s.name), el('span', 'count', s.row_count.toLocaleString()));
    }
    const x = el('span', 'x', '✕');
    x.title = 'Close tab — stays in this case, reopen it from Tables';
    x.onclick = async (e) => { e.stopPropagation(); await closeTab(s); };
    t.append(x);
    if (!s.error) t.onclick = () => openSource(s.id);
    wireTabDrag(t, s.id);
    tabs.append(t);
  }
  renderSidebar(); // every caller here (loadSources, moveTab, the drag-drop handler) means S.sources or S.tabOrder just changed
  return openTabs;
}

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
    for (const c of S.columns) qp.append('col_names', c.name);
    const found = await api(`/api/column_layouts/find?${qp.toString()}`).catch(() => null);
    if (found && found.order) defaultLayout = found;
  }
  S.layout = saved.columns || (defaultLayout && defaultLayout.columns) || {};
  S.order = (saved.order && saved.order.filter((n) => S.columns.some((c) => c.name === n)))
    || (defaultLayout && defaultLayout.order.filter((n) => S.columns.some((c) => c.name === n)))
    || S.columns.map((c) => c.name);
  for (const c of S.columns) if (!S.order.includes(c.name)) S.order.push(c.name);

  // Default sort: first datetime column, ascending — a timeline wants time order.
  const dt = S.columns.find((c) => c.type === 'datetime');
  if (dt && !saved.sort) S.sort = [{ column: dt.name, dir: 'asc' }];
  if (saved.sort) S.sort = saved.sort;

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
    $('spacerY').style.height = cached.row_count * ROW_H + 'px';
    $('viewStats').innerHTML =
      `<b>${cached.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · cached`;
    $('body').scrollTop = 0;
    render();
    drawRail();
    updateFiltersButton();
  } else {
    await rebuildView({ keepScroll: false });
  }
  document.querySelectorAll('#sourceTabs .tab').forEach((t) =>
    t.setAttribute('aria-selected', String(Number(t.dataset.id) === id)));
  renderSidebar(); // lightweight re-render (S.sources is already current) to move the .active row
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
  const scroll = keepScroll ? $('body').scrollTop : 0;
  const spec = currentSpec();
  const seq = ++rebuildSeq;
  let v;
  let seeded = [];
  setBusy(true);
  try {
    try {
      v = await post('/api/view', spec);
    } catch (e) {
      // 409 = the case/view this tab was talking to is gone (e.g. another
      // client switched cases) — show the server's message as-is rather
      // than mislabeling it a filter problem.
      toast(e.status >= 500
        ? `Couldn't build the view: ${e.message} — this is a bug, check the server console`
        : (e.status === 409 ? e.message : 'Filter error: ' + e.message), 5000);
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
          return [idx, data.rows];
        }));
      } catch { seeded = []; }
    }
  } finally {
    setBusy(false);
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
  $('spacerY').style.height = v.row_count * ROW_H + 'px';
  $('viewStats').innerHTML =
    `<b>${v.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · ${v.elapsed_ms} ms`;
  $('body').scrollTop = Math.min(scroll, Math.max(0, headH() + v.row_count * ROW_H - $('body').clientHeight));
  if (S.groupByCols.length) {
    // The old view_id (and any expanded groups' sub-views) is gone now —
    // re-summarize against the new one, keeping the chosen grouping columns.
    await regroupAll();
  } else {
    render();
    drawRail();
  }
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
const TS_ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/;
const TS_US_RE = /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?)?/;

function parseTimestamp(raw) {
  const s = String(raw).trim();
  let m = TS_ISO_RE.exec(s);
  if (m) {
    return { y: +m[1], mo: +m[2], d: +m[3], h: +(m[4] || 0), mi: +(m[5] || 0), s: +(m[6] || 0) };
  }
  m = TS_US_RE.exec(s);
  if (m) {
    let h = +(m[4] || 0);
    const ampm = (m[7] || '').toLowerCase();
    if (ampm === 'pm' && h < 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { y: +m[3], mo: +m[1], d: +m[2], h, mi: +(m[5] || 0), s: +(m[6] || 0) };
  }
  return null;
}

const pad2 = (n) => String(n).padStart(2, '0');
const TS_FORMATS = {
  raw: 'As stored',
  iso: 'YYYY-MM-DD HH:MM:SS',
  date: 'YYYY-MM-DD',
  time: 'HH:MM:SS',
  us: 'MM/DD/YYYY HH:MM:SS',
  us_date: 'MM/DD/YYYY',
};
function formatTimestamp(raw, fmt) {
  if (!fmt || fmt === 'raw' || raw == null || raw === '') return raw;
  const t = parseTimestamp(raw);
  if (!t) return raw; // doesn't match a recognized shape — show unchanged, never fabricate
  switch (fmt) {
    case 'iso': return `${t.y}-${pad2(t.mo)}-${pad2(t.d)} ${pad2(t.h)}:${pad2(t.mi)}:${pad2(t.s)}`;
    case 'date': return `${t.y}-${pad2(t.mo)}-${pad2(t.d)}`;
    case 'time': return `${pad2(t.h)}:${pad2(t.mi)}:${pad2(t.s)}`;
    case 'us': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y} ${pad2(t.h)}:${pad2(t.mi)}:${pad2(t.s)}`;
    case 'us_date': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y}`;
    default: return raw;
  }
}
function tsFormatFor(name) { return (S.layout[name] || {}).tsFormat || 'raw'; }

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
    const colMetaEntry = S.columns.find((x) => x.name === name);
    if (colMetaEntry && colMetaEntry.type === 'datetime') {
      const fmtBtn = el('button', 'hcell-fmt', '▾');
      fmtBtn.draggable = false;
      fmtBtn.title = 'Timestamp display format';
      fmtBtn.onclick = (e) => {
        e.stopPropagation();
        const current = tsFormatFor(name);
        dropdownMenu(fmtBtn, Object.entries(TS_FORMATS).map(([key, label]) => ({
          label: (key === current ? '✓ ' : '   ') + label,
          onclick: () => {
            S.layout[name] = { ...(S.layout[name] || {}), tsFormat: key === 'raw' ? undefined : key };
            render();
            saveLayout();
          },
        })));
      };
      h.append(fmtBtn);
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
    payload: { columns: S.layout, order: S.order, sort: S.sort },
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
      col_names: S.columns.map((c) => c.name), order: S.order, columns: S.layout,
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

function widthForLen(name, len) {
  const chars = Math.max(len || 0, name.length);
  return Math.min(AUTOFIT_MAX_W, Math.max(60, chars * 7 + 24));
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
  const first = Math.max(0, Math.floor(body.scrollTop / ROW_H) - OVERSCAN);
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
function ensurePage(idx, { keep } = {}) {
  if (S.pages.has(idx)) return Promise.resolve();
  const inFlight = S.pending.get(idx);
  if (inFlight) return inFlight;
  const vid = S.view.view_id;
  const gen = S.pageGen;
  const p = (async () => {
    try {
      const data = await api(`/api/rows?view_id=${vid}&start=${idx * PAGE}&count=${PAGE}`);
      if (!S.view || S.view.view_id !== vid || S.pageGen !== gen) return;
      S.pages.set(idx, data.rows);
      for (const r of data.rows) S.rowsByPos.set(r.pos, r);
      trimPageCache(keep);
      render();
      if (!$('detail').hidden && S.cursor >= 0 && rowAt(S.cursor)) showDetail(S.cursor);
    } catch (e) {
      if (String(e.message).includes('expired')) rebuildView();
    } finally {
      S.pending.delete(idx);
    }
  })();
  S.pending.set(idx, p);
  return p;
}

const rowAt = (pos) => S.rowsByPos.get(pos);

/* -------------------------------------------------------------- painting */

/* Kept in sync from render() (called after every S.selection mutation —
   row clicks, checkbox toggles, tag/copy actions that clear it, etc.)
   rather than from each of those sites individually. Grouped mode has no
   row-level selection yet (same "flat-mode-only for now" gate as the rest
   of it), so the checkbox is disabled there rather than lying about it. */
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
  const first = Math.max(0, Math.floor(body.scrollTop / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);

  for (let p = Math.floor(first / PAGE); p <= Math.floor(Math.max(first, last - 1) / PAGE); p++) ensurePage(p);

  const cols = visibleCols();
  const colMeta = Object.fromEntries(S.columns.map((c) => [c.name, c]));
  const idx = Object.fromEntries(S.columns.map((c, i) => [c.name, i]));
  const tagColor = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
  const widths = Object.fromEntries(cols.map((name) => [name, colWidth(name)]));
  const needle = S.search.trim().toLowerCase();

  syncRowsWidth(widths, cols);
  rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
  const frag = document.createDocumentFragment();

  for (let pos = first; pos < last; pos++) {
    const r = rowAt(pos);
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
        const s = el('div', 'stripe');
        s.style.background = tagColor[tid] || '#888';
        mid.append(s);
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
        else if (colMeta[name] && colMeta[name].type === 'datetime') c.textContent = formatTimestamp(val, tsFormatFor(name));
        else c.textContent = val;
      }
      row.append(c);
    });
    frag.append(row);
  }
  rowsEl.replaceChildren(frag);
  renderTagToolbar();
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
   /api/group_summary call scoped by `path`. Row-level selection/tagging/
   detail-pane stay flat-mode-only — group headers only toggle here. */

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

function ensureGroupPage(g, pageIdx) {
  const key = `${g.viewId}:${pageIdx}`;
  if (S.groupPages.has(key) || S.groupPending.has(key)) return;
  S.groupPending.add(key);
  api(`/api/rows?view_id=${g.viewId}&start=${pageIdx * PAGE}&count=${PAGE}`)
    .then((data) => {
      if (!S.groupByCols.length) return; // left group mode before this resolved
      S.groupPages.set(key, data.rows);
      render();
    })
    .catch(() => {})
    .finally(() => { S.groupPending.delete(key); });
}

function groupRowAt(g, localIdx) {
  const pageIdx = Math.floor(localIdx / PAGE);
  const page = S.groupPages.get(`${g.viewId}:${pageIdx}`);
  if (!page) { ensureGroupPage(g, pageIdx); return null; }
  return page[localIdx - pageIdx * PAGE] || null;
}

function renderGroupHeaderRow(g, gi) {
  const row = el('div', 'row group-header-row');
  row.dataset.groupIdx = gi;
  row.dataset.level = g.level;
  row.style.setProperty('--group-level', g.level);
  const arrow = g.expanded ? '▾' : '▸';
  const valueLabel = g.value === null || g.value === '' ? '(empty)' : g.value;
  const colName = S.groupByCols[g.level];
  const label = el('div', 'group-header-label');
  label.append(el('span', 'group-header-arrow', arrow));
  label.append(el('span', 'group-header-col', colName + ': '));
  label.append(el('span', 'group-header-value', String(valueLabel)));
  label.append(el('span', 'group-header-count', `${g.count.toLocaleString()} row${g.count === 1 ? '' : 's'}`));
  row.append(label);
  return row;
}

function renderGroupDataRow(r, cols, colMeta, idx, widths) {
  const row = el('div', 'row' + (r ? '' : ' pending'));
  const g = el('div', 'gutter');
  g.style.flexBasis = GUTTER_W + 'px';
  // No checkbox in grouped mode (no row selection there yet), so this is the
  // gutter's only child — .rid pins itself to grid column 3 rather than
  // relying on sibling order, keeping it aligned with flat mode's rids.
  g.append(el('span', 'rid', r ? String(r.rid) : '·'));
  row.append(g);
  for (const name of cols) {
    const c = el('div', 'cell' + (colMeta[name] && colMeta[name].type === 'number' ? ' num' : ''));
    c.style.flexBasis = widths[name] + 'px';
    const val = r ? r.cells[idx[name]] : '';
    if (val != null && val !== '') {
      c.textContent = colMeta[name] && colMeta[name].type === 'datetime' ? formatTimestamp(val, tsFormatFor(name)) : val;
    }
    row.append(c);
  }
  return row;
}

function renderGrouped() {
  syncRowsTop();
  const body = $('body');
  const rowsEl = $('rows');
  rebuildGroupPrefix();
  const total = S.groupTotalRows;
  $('spacerY').style.height = total * ROW_H + 'px';

  const first = Math.max(0, Math.floor(body.scrollTop / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);

  rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
  const frag = document.createDocumentFragment();
  const cols = visibleCols();
  const colMeta = Object.fromEntries(S.columns.map((c) => [c.name, c]));
  const idx = Object.fromEntries(S.columns.map((c, i) => [c.name, i]));
  const widths = Object.fromEntries(cols.map((name) => [name, colWidth(name)]));
  syncRowsWidth(widths, cols);

  for (let vpos = first; vpos < last; vpos++) {
    if (!S.groups.length) break;
    const gi = findGroupAt(vpos);
    const g = S.groups[gi];
    const localOffset = vpos - S.groupPrefix[gi];
    if (localOffset === 0) {
      frag.append(renderGroupHeaderRow(g, gi));
    } else {
      const r = g.expanded ? groupRowAt(g, localOffset - 1) : null;
      frag.append(renderGroupDataRow(r, cols, colMeta, idx, widths));
    }
  }
  rowsEl.replaceChildren(frag);
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
  const res = await api(`/api/group_summary?${params.toString()}`);
  if (res.truncated) toast(`Showing the top ${res.groups.length.toLocaleString()} groups`, 4000);
  return res.groups;
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
    S.groups.splice(gi + 1, end - gi - 1);
    g.expanded = false;
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
    g.expanded = true;
    render();
  } else {
    const childPath = [...g.path, { column: S.groupByCols[g.level], value: g.value }];
    try {
      const children = await fetchGroupLevel(childPath);
      S.groups.splice(gi + 1, 0, ...children.map((gr) => makeGroupNode(gr, g.level + 1, childPath)));
      g.expanded = true;
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

/* Adds a column as the innermost (last) grouping level — the drop target
   for dragging a header into the group strip. Removes it from the normal
   column list (S.order) so it doesn't also render as a data column while
   grouped; S.preGroupOrder snapshots S.order the first time this happens,
   so dropGrouping() can restore the original layout exactly. */
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
  if (!S.order.includes(removed)) S.order.push(removed);
  renderHead();
  regroupAll();
}

async function dropGrouping() {
  await closeAllGroupViews();
  S.groupByCols = [];
  S.groups = [];
  if (S.preGroupOrder) { S.order = S.preGroupOrder; S.preGroupOrder = null; }
  renderHead();
  renderGroupStrip();
  render();
  drawRail();
  saveLayout();
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

function renderGroupStrip() {
  const strip = $('groupStrip');
  strip.replaceChildren();
  strip.append(el('span', 'group-strip-label', 'Group by'));
  if (!S.groupByCols.length) {
    strip.append(el('span', 'group-strip-hint', 'drag a column header here'));
    return;
  }
  S.groupByCols.forEach((name, i) => {
    const pill = el('div', 'group-pill');
    pill.draggable = true;
    pill.append(el('span', null, name));
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
  S.tagCounts = d.counts || {};
  renderTagRibbon();
  renderTimelineTagFilter();
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
    const n = S.tagCounts[t.id] || 0;
    if (n) chip.append(el('span', 'n', n.toLocaleString()));
    chip.title = `Click to filter to ${t.name}. Press ${t.hotkey || '—'} to tag the selection.`;
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
  S.tagCounts = res.counts || {};
  // Every cached row's `tags` array is now stale — the server changed rows
  // this client never fetched, so there's nothing to patch up in place.
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  const n = res.affected != null ? res.affected : count;
  toast(`${on ? 'Tagged' : 'Untagged'} ${n.toLocaleString()} row${n === 1 ? '' : 's'} · ${tag.name}`);
}

async function tagRowsAtPositions(tag, positions, on) {
  if (!positions.length) return;
  on = resolveTagDirection(tag, on, positions[0]);
  if (positions.length >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} ${positions.length.toLocaleString()} selected rows as "${tag.name}"?`))) return;
  const pageIndices = [...new Set(positions.map((p) => Math.floor(p / PAGE)))];
  const missing = pageIndices.filter((p) => !S.pages.has(p));
  if (missing.length) {
    setBusy(true);
    try {
      await waitForPages(pageIndices);
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
  S.tagCounts = res.counts || {};
  renderTagRibbon();
  render();
  drawRail();
  toast(`${on ? 'Tagged' : 'Untagged'} ${rows.length.toLocaleString()} row${rows.length === 1 ? '' : 's'} · ${tag.name}`);
}

async function applyTagToView(tag) {
  if (!S.view || !S.view.row_count) return;
  if (!(await confirmDialog(`Tag all ${S.view.row_count.toLocaleString()} rows in this view as "${tag.name}"?`))) return;
  setBusy(true);
  let res;
  try { res = await post('/api/row_tags/view', { view_id: S.view.view_id, tag_id: tag.id, on: true }); }
  finally { setBusy(false); }
  S.tagCounts = res.counts || {};
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
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

function appendJsonHighlighted(container, jsonText) {
  const re = /("(?:\\.|[^"\\])*"(\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let last = 0, m;
  while ((m = re.exec(jsonText))) {
    if (m.index > last) container.append(jsonText.slice(last, m.index));
    const tok = m[0];
    let cls = 'jtok-num';
    if (tok[0] === '"') cls = m[2] ? 'jtok-key' : 'jtok-str';
    else if (tok === 'true' || tok === 'false') cls = 'jtok-bool';
    else if (tok === 'null') cls = 'jtok-null';
    container.append(el('span', cls, tok));
    last = m.index + tok.length;
  }
  container.append(jsonText.slice(last));
}

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

function renderDetailContent(v) {
  const json = tryParseJSON(v);
  if (json !== null && typeof json === 'object') {
    const pre = el('pre', 'detail-pretty');
    appendJsonHighlighted(pre, JSON.stringify(json, null, 2));
    return pre;
  }
  if (looksLikeXml(v)) {
    try {
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
    dl.append(el('dt', null, c.name));
    const dd = el('dd');
    dd.append(renderDetailContent(c.type === 'datetime' ? formatTimestamp(v, tsFormatFor(c.name)) : v));
    dl.append(dd);
  });
  const note = $('noteInput');
  note.value = r.note || '';
  note.dataset.rid = r.rid;
  note.dataset.sourceId = r.source_id;
  $('noteStatus').textContent = '';
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
  if (!S.view || !S.view.row_count) return;
  to = Math.max(0, Math.min(S.view.row_count - 1, to));
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
  const top = pos * ROW_H;
  const bottom = top + ROW_H + headH();
  if (top < body.scrollTop) body.scrollTop = top;
  else if (bottom > body.scrollTop + body.clientHeight) body.scrollTop = bottom - body.clientHeight;
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

/* Minimal anchored menu — one instance ever open at a time. Closes on
   outside click, Escape, or an item's own click (items are expected to
   open a modal/do their thing and don't need to close it themselves). */
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
function onMenuKeydown(e) { if (e.key === 'Escape') closeMenu(); }

/* items: {label, onclick, disabled}, '-' for a separator, or {header} for
   a section label. */
function dropdownMenu(anchorEl, items) {
  const wasOpenForSameAnchor = openMenuAnchor === anchorEl;
  closeMenu();
  if (wasOpenForSameAnchor) return; // second click on the same anchor just toggles it shut
  const menu = el('div', 'menu');
  for (const item of items) {
    if (item === '-') { menu.append(el('div', 'menu-sep')); continue; }
    if (item.header) { menu.append(el('div', 'menu-header', item.header)); continue; }
    const b = el('button', 'menu-item', item.label);
    b.disabled = !!item.disabled;
    b.onclick = () => { closeMenu(); item.onclick(); };
    menu.append(b);
  }
  document.body.append(menu);
  const r = anchorEl.getBoundingClientRect();
  menu.style.top = r.bottom + 4 + 'px';
  menu.style.left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 8) + 'px';
  openMenuEl = menu;
  openMenuAnchor = anchorEl;
  anchorEl.setAttribute('aria-expanded', 'true');
  setTimeout(() => {
    document.addEventListener('mousedown', onMenuOutsideClick, true);
    document.addEventListener('keydown', onMenuKeydown, true);
  }, 0);
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
      const rec = await post('/api/saved_filters', { name: name.trim(), col_names: S.columns.map((c) => c.name), payload: currentFilterPayload() });
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
  renderHead();
  rebuildView({ keepScroll: false });
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

function filtersForCurrentSource() {
  const cur = new Set(S.columns.map((c) => c.name.trim().toLowerCase()));
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

/* Same shape saveFilterAs/saveAs POST as a saved filter/preset's payload. */
function currentFilterPayload() {
  return { filter_tree: S.filterTree, sort: S.sort, search: S.search, search_mode: S.searchMode, search_terms: S.searchTerms };
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

    const curCols = new Set(S.columns.map((c) => c.name.trim().toLowerCase()));
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

/* Lives inline in Settings rather than its own modal — appends its content
   directly to whatever container it's given (called with the Settings
   modal body), and re-invokes openSettings() to refresh itself in place
   after an action instead of managing its own re-render. */
function buildColumnsPanel(container) {
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
    const c = S.columns.find((x) => x.name === name);
    lab.append(el('span', 'count', ' ' + (c ? c.type : '')));
    row.append(lab);
    list.append(row);
  });
  container.append(list);
  container.append(el('p', 'fb-help', 'Drag a column header in the grid to reorder it.'));
  const acts = el('div', 'row-actions');
  const all = el('button', 'btn ghost', 'Show all');
  all.onclick = () => { for (const n of S.order) S.layout[n] = { ...(S.layout[n] || {}), hidden: false }; renderHead(); render(); saveLayout(); openSettings(); };
  const none = el('button', 'btn ghost', 'Hide empty columns');
  none.onclick = async () => {
    // Hide columns with no value in the first 2000 rows of the current view.
    const sample = await api(`/api/rows?view_id=${S.view.view_id}&start=0&count=2000`);
    S.columns.forEach((c, i) => {
      const empty = sample.rows.every((r) => r.cells[i] == null || r.cells[i] === '');
      if (empty) S.layout[c.name] = { ...(S.layout[c.name] || {}), hidden: true };
    });
    renderHead(); render(); saveLayout(); openSettings();
  };
  acts.append(all, none);
  container.append(acts);
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
        lab.append(cb, el('span', null, `${s.name} (${s.row_count.toLocaleString()} rows)`));
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

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      const fd = new FormData();
      fd.append('file', file);
      if (delimSel.value) fd.append('delimiter', delimSel.value);
      fd.append('has_header', headerCb.checked ? 'true' : 'false');
      try {
        preview = await api('/api/ingest/preview', { method: 'POST', body: fd });
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
      const fd = new FormData();
      fd.append('file', file);
      if (settings.delimiter) fd.append('delimiter', settings.delimiter);
      fd.append('has_header', settings.has_header ? 'true' : 'false');
      fd.append('column_types', JSON.stringify(settings.column_types));
      $('modal').hidden = true;
      toast(`Importing ${file.name}…`, 60000);
      try {
        const rec = await api('/api/ingest/upload', { method: 'POST', body: fd });
        toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s${raggedNote(rec)}`, rec.ragged_rows ? 6000 : 2600);
        await loadSources(rec.id);
      } catch (e) {
        toast('Import failed: ' + e.message, 6000);
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

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      const fd = new FormData();
      fd.append('file', file);
      fd.append('flatten_mode', flattenMode);
      fd.append('flatten_depth', String(flattenDepth));
      try {
        preview = await api('/api/ingest/json/preview', { method: 'POST', body: fd });
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
      const fd = new FormData();
      fd.append('file', file);
      fd.append('flatten_mode', flattenMode);
      fd.append('flatten_depth', String(flattenDepth));
      $('modal').hidden = true;
      toast(`Importing ${file.name}…`, 60000);
      try {
        const rec = await api('/api/ingest/json/upload', { method: 'POST', body: fd });
        toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s`, 2600);
        await loadSources(rec.id);
      } catch (e) {
        toast('Import failed: ' + e.message, 6000);
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
/* openSqliteImportModal's own file-picker accept list, factored out so
   wireFileDrop can recognize the same set without a second hand-typed copy. */
const SQLITE_IMPORT_EXTENSIONS = ['.db', '.sqlite', '.sqlite3', '.db-wal'];

function extOf(filename) {
  const i = filename.lastIndexOf('.');
  return i === -1 ? '' : filename.slice(i).toLowerCase();
}

function importKindFor(filename) {
  const ext = extOf(filename).slice(1); // drop the leading '.' — json/jsonl/ndjson below are bare
  return ext === 'json' || ext === 'jsonl' || ext === 'ndjson' ? 'json' : 'csv';
}

/* Appends File objects to S.importQueue with each one's default settings —
   shared by openImportModal's own file-picker (addInput.onchange) and
   wireFileDrop, so a dropped file and a picked one queue identically. Not
   gated on the modal being open: S.importQueue is app-level state that
   openImportModal just happens to render, so this can be called before
   the modal even exists yet and it'll show up correctly whenever it opens. */
function queueFiles(files) {
  for (const f of files) {
    const kind = importKindFor(f.name);
    S.importQueue.push(kind === 'json'
      ? { file: f, kind, flatten_mode: 'none', flatten_depth: 1, configured: false }
      : { file: f, kind, delimiter: null, has_header: true, column_types: null, configured: false });
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
  modal('Import files', (b) => {
    b.append(el('p', null,
      'Queue one or more CSV/TSV or JSON/JSONL files, optionally previewing/configuring each, '
      + 'then import them all at once.'));

    const queueList = el('div', 'session-queue');

    function renderQueue() {
      queueList.replaceChildren();
      if (!S.importQueue.length) { queueList.append(el('div', 'note-status', 'No files queued.')); return; }
      S.importQueue.forEach((item, i) => {
        const row = el('div', 'row-actions session-row');
        row.append(
          el('span', 'session-name', item.file.name),
          el('span', 'count', (item.configured ? 'configured' : 'default settings') + ` · ${item.kind}`),
        );
        const cfg = el('button', 'btn ghost', 'Preview & configure');
        cfg.onclick = () => {
          const openPreview = item.kind === 'json' ? openJsonImportPreview : openImportPreview;
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
    addInput.accept = RECOGNIZED_IMPORT_EXTENSIONS.join(',');
    addInput.multiple = true;
    addInput.hidden = true;
    addInput.onchange = () => {
      queueFiles(addInput.files);
      addInput.value = '';
      renderQueue();
    };
    addLabel.append(addInput);
    const importAll = el('button', 'btn', 'Import all queued');
    importAll.onclick = async () => {
      if (!S.importQueue.length) return;
      const queue = S.importQueue.slice();
      S.importQueue = [];
      renderQueue();
      setBusy(true);
      try {
        for (const item of queue) {
          toast(`Importing ${item.file.name}…`, 60000);
          const fd = new FormData();
          fd.append('file', item.file);
          try {
            if (item.kind === 'json') {
              fd.append('flatten_mode', item.flatten_mode || 'none');
              fd.append('flatten_depth', String(item.flatten_depth || 1));
              const rec = await api('/api/ingest/json/upload', { method: 'POST', body: fd });
              toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s`, 2600);
            } else {
              if (item.delimiter) fd.append('delimiter', item.delimiter);
              fd.append('has_header', item.has_header ? 'true' : 'false');
              if (item.column_types) fd.append('column_types', JSON.stringify(item.column_types));
              const rec = await api('/api/ingest/upload', { method: 'POST', body: fd });
              toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s${raggedNote(rec)}`, rec.ragged_rows ? 6000 : 2600);
            }
          } catch (e) {
            toast(`Import failed for ${item.file.name}: ` + e.message, 6000);
          }
        }
        await loadSources();
      } finally {
        setBusy(false);
      }
      $('modal').hidden = true;
    };
    queueActs.append(addLabel, importAll);
    b.append(queueActs);
  }, { wide: true });
}

/* Import one or more tables out of an external SQLite file — Chromium's
   History/Cookies/Web Data/... or any other .db/.sqlite — as new sources.
   Two-step, mirroring the CSV preview flow: pick a file, see every table
   with a row count and (for any column that looks like a WebKit/Chrome
   timestamp — microseconds since 1601-01-01, Chromium's own convention)
   a pre-checked option to convert it to a readable datetime on import
   rather than leaving it as an opaque integer. */
function openSqliteImportModal(initialFile) {
  let file = null;
  let tables = null; // [{name, row_count, columns, likely_timestamp_columns}]
  const selected = new Map(); // table name -> Set of timestamp columns to convert
  const included = new Set(); // table names checked for import

  modal('Import SQLite tables', (b) => {
    b.append(el('p', null,
      'Pick a SQLite file — a Chromium History/Cookies/Web Data file, or any other .db — and choose '
      + 'which of its tables to import, each as its own source.'));

    const pickRow = el('div', 'row-actions');
    const pickLabel = el('label', 'btn ghost', 'Choose a SQLite file…');
    const pickInput = el('input');
    pickInput.type = 'file';
    pickInput.accept = SQLITE_IMPORT_EXTENSIONS.join(',');
    pickInput.hidden = true;
    const pickStatus = el('span', 'count', '');
    pickRow.append(pickLabel, pickStatus);
    b.append(pickRow);

    const tableList = el('div', 'session-list');
    b.append(tableList);

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', 'Import selected');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
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
      const fd = new FormData();
      fd.append('file', file);
      try {
        const res = await api('/api/ingest/sqlite/preview', { method: 'POST', body: fd });
        tables = res.tables;
      } catch (e) {
        pickStatus.textContent = '';
        toast('Could not read that file: ' + e.message, 6000);
        return;
      }
      pickStatus.textContent = file.name;
      included.clear();
      selected.clear();
      for (const t of tables) {
        selected.set(t.name, new Set(t.likely_timestamp_columns)); // default: convert every detected one
      }
      renderTables();
    }
    pickInput.onchange = () => { if (pickInput.files[0]) loadFile(pickInput.files[0]); };
    pickLabel.append(pickInput);
    if (initialFile) loadFile(initialFile);

    importBtn.onclick = async () => {
      if (!file) { toast('Choose a file first'); return; }
      const targets = [...included];
      if (!targets.length) { toast('Check at least one table to import'); return; }
      setBusy(true);
      try {
        for (const tableName of targets) {
          toast(`Importing ${tableName}…`, 60000);
          const fd = new FormData();
          fd.append('file', file);
          fd.append('table', tableName);
          fd.append('timestamp_columns', JSON.stringify([...selected.get(tableName)]));
          try {
            const rec = await api('/api/ingest/sqlite/upload', { method: 'POST', body: fd });
            toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s`, 2600);
          } catch (e) {
            toast(`Import failed for ${tableName}: ` + e.message, 6000);
          }
        }
        await loadSources();
      } finally {
        setBusy(false);
      }
      $('modal').hidden = true;
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
  if (files.length === 1 && SQLITE_IMPORT_EXTENSIONS.includes(extOf(files[0].name))) {
    openSqliteImportModal(files[0]);
    return;
  }
  const recognized = files.filter((f) => RECOGNIZED_IMPORT_EXTENSIONS.includes(extOf(f.name)));
  const skipped = files.filter((f) => !RECOGNIZED_IMPORT_EXTENSIONS.includes(extOf(f.name)));
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
    extensions: state.extensions || RECOGNIZED_IMPORT_EXTENSIONS.slice(),
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
        extensions: p ? (p.extensions || RECOGNIZED_IMPORT_EXTENSIONS.slice()) : RECOGNIZED_IMPORT_EXTENSIONS.slice(),
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
    for (const ext of RECOGNIZED_IMPORT_EXTENSIONS) {
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
      setBusy(true);
      let ok = 0;
      let failed = 0;
      try {
        for (const m of toImport) {
          toast(`Importing ${m.rel_path}…`, 60000);
          try {
            if (m.kind === 'json') {
              const rec = await post('/api/ingest/json/path', { path: m.path, name: m.rel_path });
              toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s`, 2600);
            } else {
              const rec = await post('/api/ingest/path', { path: m.path, name: m.rel_path });
              toast(`${rec.name}: ${rec.row_count.toLocaleString()} rows in ${rec.elapsed_sec}s${raggedNote(rec)}`, rec.ragged_rows ? 6000 : 2600);
            }
            ok++;
          } catch (e) {
            failed++;
            toast(`Import failed for ${m.rel_path}: ` + e.message, 6000);
          }
        }
        await loadSources();
      } finally {
        setBusy(false);
      }
      $('modal').hidden = true;
      toast(`Imported ${ok} of ${toImport.length} file${toImport.length === 1 ? '' : 's'}${failed ? ` — ${failed} failed` : ''}`, 4000);
    };
    actions.append(importBtn, cancelBtn);
    b.append(actions);

    renderResults();
    if (st.root) runScan();
  }, { wide: true });
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
        row.append(el('span', 'session-name', (s.is_merge ? '⛓ ' : '') + s.name + (s.error ? ' ⚠' : '')));
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
        const del = el('button', 'btn ghost', 'Remove…');
        del.onclick = async () => {
          const warn = s.is_merge
            ? `Delete merge "${s.name}"? The underlying sources are untouched.`
            : `Remove ${s.name} from this case? Tags and notes for it are deleted too.`;
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
        row.append(toggle, del);
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
        const r = el('div', 'search-all-row');
        // `capped` means the server stopped counting at its ceiling rather
        // than scanning every matching row (see SEARCH_ALL_COUNT_CAP) — say
        // "1,000+" rather than imply a precise number it never computed.
        const count = h.capped
          ? `${h.match_count.toLocaleString()}+ matches`
          : `${h.match_count.toLocaleString()} match${h.match_count === 1 ? '' : 'es'}`;
        r.append(
          el('span', 'search-all-name', h.name),
          el('span', 'search-all-count', count),
        );
        const openBtn = el('button', 'btn ghost', 'Open ↦');
        openBtn.onclick = async () => {
          const src = S.sources.find((s) => s.id === h.source_id);
          if (src && !src.is_open) await post(`/api/source/${h.source_id}/open`, { open: true });
          $('modal').hidden = true;
          await loadSources(h.source_id);
          S.searchMode = 'advanced';
          // The terms the *results* came from, not whatever's since been
          // typed into the box — those are what this row's count describes.
          S.searchTerms = st.terms.map((t) => ({ ...t }));
          document.querySelectorAll('#searchModeToggle button').forEach((btn) => btn.setAttribute('aria-pressed', String(btn.dataset.mode === 'advanced')));
          renderAdvancedChips();
          syncSearchExpansion(true);
          updateSearchHint();
          await rebuildView({ keepScroll: false });
        };
        r.append(openBtn);
        results.append(r);
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
  $('sqlview').hidden = false;
  $('tabSql').setAttribute('aria-selected', 'true');
  $('tabTimeline').setAttribute('aria-selected', 'false');
  document.querySelectorAll('#sourceTabs .tab').forEach((t) => t.setAttribute('aria-selected', 'false'));
  renderSidebar(); // S.sourceId is unchanged (still the last-open source) but nothing in the sidebar should read as active here
  // The "matching saved filter" banner is about a specific table's columns —
  // meaningless on a tab that isn't showing any one table's grid.
  $('presetBanner').hidden = true;
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

function showGridTab() {
  S.activeTab = 'grid';
  $('sqlview').hidden = true;
  $('timelineview').hidden = true;
  $('grid').hidden = false;
  $('tabSql').setAttribute('aria-selected', 'false');
  $('tabTimeline').setAttribute('aria-selected', 'false');
  if (S.sourceId) checkPresets(S.sourceId); // restore the banner, hidden while on SQL/Timeline
}
function showTimelineTab() {
  S.activeTab = 'timeline';
  $('grid').hidden = true;
  $('sqlview').hidden = true;
  $('timelineview').hidden = false;
  $('tabSql').setAttribute('aria-selected', 'false');
  $('tabTimeline').setAttribute('aria-selected', 'true');
  document.querySelectorAll('#sourceTabs .tab').forEach((t) => t.setAttribute('aria-selected', 'false'));
  renderSidebar(); // same reasoning as showSqlTab — S.sourceId is stale for highlighting purposes here
  $('presetBanner').hidden = true;
  buildTimeline(); // always fresh — tags can change in any table while this tab isn't the active one
}
$('tabSql').onclick = showSqlTab;
$('tabTimeline').onclick = showTimelineTab;
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
    v = await post('/api/timeline', { tag_ids: S.timeline.tagFilter });
  } catch (e) {
    toast('Could not build timeline: ' + e.message, 6000);
    return;
  } finally {
    setBusy(false);
  }
  if (reqId !== S.timeline.reqId) return; // a newer build superseded this one
  S.timeline.view = v;
  $('timelineSpacerY').style.height = v.row_count * ROW_H + 'px';
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

  const first = Math.max(0, Math.floor(body.scrollTop / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);
  for (let p = Math.floor(first / PAGE); p <= Math.floor(Math.max(first, last - 1) / PAGE); p++) ensureTimelinePage(p);

  const tagColor = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
  rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
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
      const colNames = src.columns.map((c) => c.name);
      const dtCols = src.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
      const existing = timelineTemplateFor(colNames);

      const row = el('div', 'row-actions session-row');
      row.style.flexDirection = 'column';
      row.style.alignItems = 'stretch';
      row.append(el('span', 'session-name', src.name));

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
  if (S.groupByCols.length) return; // row-level selection/cursor stays flat-mode-only for now
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
  if (S.groupByCols.length) return; // row-level selection/cursor stays flat-mode-only for now
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
  const firstPage = Math.floor(r0 / PAGE), lastPage = Math.floor(r1 / PAGE);
  const pageIndices = [];
  for (let p = firstPage; p <= lastPage; p++) pageIndices.push(p);
  if (pageIndices.some((p) => !S.pages.has(p))) toast(`Copying ${rowCount.toLocaleString()} row${rowCount > 1 ? 's' : ''}…`, 8000);
  const textPromise = (async () => {
    await waitForPages(pageIndices); // no-op fast path once everything's already cached
    const colIdx = Object.fromEntries(S.columns.map((c, i) => [c.name, i]));
    const lines = [];
    if (withHeaders) lines.push(cols.join('\t'));
    for (let pos = r0; pos <= r1; pos++) {
      // waitForPages threw if anything was missing, so a hole here would be
      // a bug, not a slow fetch — refuse rather than emit a blank line.
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
  const pageIndices = [...new Set(positions.map((p) => Math.floor(p / PAGE)))];
  if (pageIndices.some((p) => !S.pages.has(p))) toast(`Copying ${positions.length.toLocaleString()} row${positions.length > 1 ? 's' : ''}…`, 8000);
  const textPromise = (async () => {
    await waitForPages(pageIndices);
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
  const positions = count ? selPositions() : S.cursor >= 0 ? [S.cursor] : [];
  if (!positions.length) return;
  await copyRowsAsText(positions, withHeaders);
}

/* Reuses the same single-cell selection a plain click already commits to
   S.cellRange (see setCellRange above) — takes the top-left cell of
   whatever's selected, drops its value into that column's filter box as an
   exact match, and reuses the existing S.filters + rebuildView() path so it
   behaves exactly like typing "=value" into the header filter by hand. */
function filterBySelectedCell() {
  if (!S.cellRange) { toast('Click a cell first'); return; }
  const name = visibleCols()[S.cellRange.c0];
  const r = rowAt(S.cellRange.r0);
  if (!name || !r) { toast('Row not loaded yet'); return; }
  const idx = S.columns.findIndex((c) => c.name === name);
  const val = r.cells[idx];
  const empty = val == null || val === '';
  const raw = empty ? '""' : '=' + String(val);
  S.filters[name] = raw;
  const inp = document.querySelector(`.fcell input[data-col="${CSS.escape(name)}"]`);
  if (inp) { inp.value = raw; inp.classList.add('active'); }
  rebuildView();
  toast(`Filtered ${name} = ${empty ? '(empty)' : val}`);
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

   Two sections, matching the tab strip's own "shown vs not" rule (an
   errored source is always shown, bucketed with Open rather than a third
   section). Rows reuse the dropdown's own .menu-item/.menu-item-action
   classes (see style.css) rather than a parallel set of near-identical
   ones. Filtered by S.sidebarFilter — a plain client-side substring match,
   not a network round trip, since S.sources is already in memory and this
   can be retyped on every keystroke.

   Open rows also get ▲/▼/✕ — the same reorder/close the tab strip itself
   offers via drag-and-drop and its own ✕, kept here for when the strip is
   scrolled out of view or a standing list is just easier to act on. */
function renderSidebar() {
  const list = $('sidebarList');
  list.replaceChildren();
  const q = S.sidebarFilter.trim().toLowerCase();
  const match = (s) => !q || s.name.toLowerCase().includes(q);
  const openSrcs = openTabsSorted().filter(match);
  const closedSrcs = S.sources.filter((s) => !s.error && !s.is_open).filter(match);
  if (!openSrcs.length && !closedSrcs.length) {
    list.append(el('div', 'note-status', q ? 'No matching tables.' : 'No tables in this case yet.'));
    return;
  }
  if (openSrcs.length) {
    list.append(el('div', 'menu-header', 'Open'));
    openSrcs.forEach((s, i) => list.append(sidebarRow(s, { open: true, index: i, total: openSrcs.length })));
  }
  if (closedSrcs.length) {
    list.append(el('div', 'menu-header', 'Closed'));
    for (const s of closedSrcs) list.append(sidebarRow(s, { open: false }));
  }
}

function sidebarRow(s, { open, index, total }) {
  // S.activeTab !== 'grid' means SQL/Timeline is showing — S.sourceId is
  // still the last-open source in that state (nothing clears it), but
  // nothing in the sidebar represents SQL/Timeline, so no row should read
  // as active; #tabSql/#tabTimeline carry that highlight instead.
  const active = open && s.id === S.sourceId && S.activeTab === 'grid';
  const row = el('div', 'sidebar-row' + (active ? ' active' : ''));
  const label = el('button', 'menu-item', (s.is_merge ? '⛓ ' : '') + s.name + (s.error ? ' ⚠' : ''));
  label.disabled = !!s.error;
  if (s.error) label.title = s.error;
  label.onclick = open ? () => openSource(s.id) : async () => {
    await post(`/api/source/${s.id}/open`, { open: true });
    await loadSources(s.id);
  };
  row.append(label);
  if (!s.error) row.append(el('span', 'sidebar-row-count', s.row_count.toLocaleString()));
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
  { label: 'Import files…', onclick: openImportModal },
  { label: 'Import SQLite tables…', onclick: openSqliteImportModal },
  { label: 'Import a folder…', onclick: openDirectoryImportModal },
  { label: 'Merge sources…', onclick: openMergeBuilder },
  { label: 'Tables…', onclick: openTablesManager },
  '-',
  { label: 'Export…', onclick: openExportModal },
  { label: 'Session (save/load)…', onclick: openSessionManager },
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
  $('body').scrollTop = Math.max(0, pos * ROW_H + ROW_H / 2 + headH() / 2 - $('body').clientHeight / 2);
  render();
}

async function clearAllFilters() {
  // Deliberately doesn't touch S.timeRange — the timeframe filter is meant
  // to survive exactly this ("apply/clear filters shouldn't lose my
  // timeframe"), same as it survives applyPreset() and a tab switch. Use
  // the Timeframe filter's own "Clear" button, or toggleTimeRange, for that.
  const anchor = selectedRowAnchor();
  S.filters = {}; S.search = ''; S.tagFilter = []; S.searchTerms = [];
  S.filterTree = { type: 'group', op: 'AND', children: [] };
  updateFiltersButton();
  $('search').value = '';
  renderHead(); renderTagRibbon();
  if (S.searchMode !== 'contains') await setSearchMode('contains'); // also rebuilds the view
  else await rebuildView({ keepScroll: false });
  syncSearchExpansion(false);
  await recenterOnRow(anchor);
}
$('btnReset').onclick = clearAllFilters;
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
  focusFilter: ['f'],
  focusNote: ['n'],
  openSettings: ['?'],
  resetColumnWidths: ['0'],
  autofitColumnWidths: ['='],
  cyclePrevFilter: ['['],
  cycleNextFilter: [']'],
  filterBySelectedCell: ['F'],
  clearFilters: ['c'],
  openTables: ['t'],
  openSearchAll: ['s'],
  toggleDetail: ['d'],
  dropGrouping: ['x'],
  saveDefaultLayout: ['L'],
  toggleTimeRange: ['T'],
  openTimeRange: ['R'],
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
  clearFilters: 'Clear all filters, search and tag filter',
  openTables: 'Open Tables manager',
  openSearchAll: 'Search all tables',
  toggleDetail: 'Open/close the detail pane',
  dropGrouping: 'Drop all grouping, restore column order',
  saveDefaultLayout: "Save this column order/visibility as the default for this header set",
  toggleTimeRange: 'Toggle the timeframe filter on/off',
  openTimeRange: 'Open the timeframe filter (set column/range)',
};

function loadKeymap() {
  try {
    return { ...DEFAULT_KEYMAP, ...JSON.parse(localStorage.getItem('winnow.keymap') || '{}') };
  } catch { return { ...DEFAULT_KEYMAP }; }
}
function saveKeymap() { localStorage.setItem('winnow.keymap', JSON.stringify(S.keymap)); }

function matchAction(e) {
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (keys.includes(e.key)) return action;
  }
  return null;
}

/* Returns a human-readable description of what already owns `key`, or null
   if it's free. Checked against other keymap actions, tag hotkeys (which
   can change independently at any time via the tag editor), and Escape. */
function findKeyConflict(key, currentAction) {
  if (key === 'Escape') return 'the always-on close/clear action';
  if (/^[1-9]$/.test(key)) {
    const t = S.tags.find((x) => x.hotkey === key);
    return `the "${t ? t.name : 'tag'}" tag hotkey`;
  }
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (action !== currentAction && keys.includes(key)) return ACTION_LABELS[action] || action;
  }
  return null;
}

const ACTION_HANDLERS = {
  moveDown: (e, pageRows) => moveCursor(S.cursor + 1, e.shiftKey),
  moveUp: (e, pageRows) => moveCursor(S.cursor - 1, e.shiftKey),
  pageDown: (e, pageRows) => moveCursor(S.cursor + pageRows, e.shiftKey),
  pageUp: (e, pageRows) => moveCursor(S.cursor - pageRows, e.shiftKey),
  jumpFirst: () => moveCursor(0, false),
  jumpLast: () => moveCursor(S.view ? S.view.row_count - 1 : 0, false),
  focusSearch: () => expandSearch(),
  focusFilter: () => { const i = document.querySelector('.fcell input'); if (i) { i.focus(); i.select(); } },
  focusNote: () => { if (!$('detail').hidden) $('noteInput').focus(); },
  openSettings: () => openSettings(),
  resetColumnWidths: () => resetAllColumnWidths(),
  autofitColumnWidths: () => autofitAllColumnWidths(),
  cyclePrevFilter: () => cycleSavedFilter(-1),
  cycleNextFilter: () => cycleSavedFilter(1),
  filterBySelectedCell: () => filterBySelectedCell(),
  clearFilters: () => clearAllFilters(),
  openTables: () => openTablesManager(),
  toggleDetail: () => toggleDetailPane(),
  openSearchAll: () => openSearchAllModal(),
  dropGrouping: () => { if (S.groupByCols.length) dropGrouping(); },
  saveDefaultLayout: () => saveDefaultLayout(),
  toggleTimeRange: () => toggleTimeRange(),
  openTimeRange: () => openTimeRangeModal(),
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
  return { style: 'panel', themeMode: 'dark', accent: STYLES.panel.defaultAccent, accentCustomized: false, density: 'comfortable' };
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
  const oldRowH = ROW_H;
  const topRow = S.view ? Math.floor(body.scrollTop / oldRowH) : 0;
  paintDensity();
  if (!S.view) return;
  if (S.groupByCols.length) {
    rebuildGroupPrefix();
    $('spacerY').style.height = S.groupTotalRows * ROW_H + 'px';
  } else {
    $('spacerY').style.height = S.view.row_count * ROW_H + 'px';
  }
  body.scrollTop = topRow * ROW_H;
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

function openSettings() {
  modal('Settings', (b) => {
    b.append(el('h4', null, 'Appearance'));
    b.append(el('p', null, 'Pick a look, then a theme, then (optionally) your own accent color. All three are saved on this machine.'));

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
    b.append(styleGrid);

    b.append(el('div', 'settings-sub-label', 'Theme'));
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
    b.append(themeSeg);

    b.append(el('div', 'settings-sub-label', 'Accent color'));
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
    b.append(accentGrid);

    b.append(el('div', 'settings-sub-label', 'Row density'));
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
    b.append(densitySeg);

    b.append(el('h4', null, 'Columns'));
    b.append(el('p', null, 'Show, hide, or bulk-manage columns for the currently open table.'));
    buildColumnsPanel(b);

    b.append(el('h4', null, 'Keyboard shortcuts'));
    b.append(el('p', null, 'Tag hotkeys (1–9) are set per-tag in Edit tags. Escape always clears the selection or closes a panel.'));
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
          const capture = (ke) => {
            ke.preventDefault();
            ke.stopPropagation();
            document.removeEventListener('keydown', capture, true);
            addBtn.disabled = false;
            addBtn.textContent = '+ key';
            if (ke.key === 'Escape') return;
            const conflict = findKeyConflict(ke.key, action);
            if (conflict) { toast(`"${ke.key}" is already used by ${conflict}`, 4000); return; }
            keys.push(ke.key);
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
    b.append(list);

    const reset = el('button', 'btn ghost', 'Reset to defaults');
    reset.style.marginTop = '14px';
    reset.onclick = () => { S.keymap = { ...DEFAULT_KEYMAP }; saveKeymap(); renderList(); };
    b.append(reset);

    const fixedKeys = el('div', 'kv');
    fixedKeys.style.marginTop = '10px';
    fixedKeys.append(el('kbd', null, 'Shift + move keys'), el('span', null, 'Extend the selection'));
    fixedKeys.append(el('kbd', null, '1 – 9'), el('span', null, 'Toggle the tag with that hotkey on the selection'));
    fixedKeys.append(el('kbd', null, 'Shift + 1 – 9'), el('span', null, 'Apply that tag to every row in the current view'));
    fixedKeys.append(el('kbd', null, 'Esc'), el('span', null, 'Clear selection, or close a panel'));
    b.append(fixedKeys);

    b.append(el('h4', null, 'Filter & search syntax'));
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
    b.append(el('p', null, 'Column filter row:'), f);
    b.append(el('p', null,
      'Search box — Contains is always a true substring match; Regex is a full scan; Advanced supports '
      + 'multiple AND / OR / NOT terms and uses the FTS5 index when one was built at import.'));
    b.append(el('p', null,
      'The ⏱ Timeframe button pins a start/end range against one datetime column, or every datetime '
      + "column at once (catches a row via its Modified time even if its Created time was timestomped) — "
      + 'unlike the other filters, it stays applied when you clear filters, apply a saved filter, or switch tables.'));

    b.append(el('h4', null, 'Default tags for new cases'));
    b.append(el('p', null,
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
    b.append(dtList);

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
    b.append(dtActs);

    b.append(el('h4', null, 'Saved filters'));
    b.append(el('p', null,
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
    b.append(flist);

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
    b.append(fActs);
  });
}

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    if (!$('modal').hidden) { $('modal').hidden = true; return; }
    if (typing) { e.target.blur(); $('body').focus(); return; }
    selClear(); render(); return;
  }
  if (typing) return;

  if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && (S.cellRange || selCount() || S.cursor >= 0)) {
    e.preventDefault();
    handleCopyShortcut(e.shiftKey);
    return;
  }

  const pageRows = Math.floor(($('body').clientHeight - headH()) / ROW_H) - 1;
  const action = matchAction(e);
  if (action && ACTION_HANDLERS[action]) {
    e.preventDefault();
    ACTION_HANDLERS[action](e, pageRows);
    return;
  }
  const digit = e.code && e.code.startsWith('Digit') ? e.code.slice(5) : e.key;
  if (/^[1-9]$/.test(digit)) {
    const t = S.tags.find((x) => x.hotkey === digit);
    if (t) { e.preventDefault(); e.shiftKey ? applyTagToView(t) : applyTag(t); }
  }
});

window.addEventListener('resize', () => { render(); drawRail(); });

/* -------------------------------------------------------------- home screen */

/* Home manages "Cases" (one case.db each — recent, grouped, renamed,
   annotated). Distinct from the existing Session feature (the Session
   button above), which snapshots tags/notes/layout *within* one already-open
   case — that feature is untouched. Opening a Case from here is what
   actually swaps the server's STORE; navigating back to Home via #btnHome
   just changes what the client is looking at. */

function showApp() { $('home').hidden = true; $('app').hidden = false; }
function showHome() { $('app').hidden = true; $('home').hidden = false; setBrandLabel(null); }

// The brand button doubles as "which case is this" once one's open — falls
// back to the app name on the home screen / before any case has loaded.
function setBrandLabel(name) {
  $('brandLabel').textContent = name || 'Winnow';
}

async function openCase(path) {
  let res;
  try {
    res = await post('/api/case/open', { path });
  } catch (e) {
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
  if (S.activeTab === 'timeline') showGridTab();
  if (S.activeTab === 'sql') showGridTab();
  setBrandLabel(res.name);
  showApp();
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
  head.append(newBtn, openBtn);
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
  await Promise.all([loadSavedFilters(), loadHeaderNicknames(), loadTimelineTemplates()]);
  const cur = await api('/api/case/current').catch(() => ({ open: false }));
  if (cur.open) {
    setBrandLabel(cur.name);
    showApp();
    await loadSources();
  } else {
    showHome();
    await refreshCases();
  }
}

boot().catch((e) => toast('Could not start: ' + e.message, 8000));

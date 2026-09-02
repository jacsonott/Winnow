/* S — the one mutable store the whole app reads — plus the row-selection
helpers that are the only sanctioned way to touch S.selection/S.selectAll.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { rowAt } from './grid.js';
import { groupCoordAt } from './grouping.js';

/* A filter tree's root must be a group for every consumer here — the
   builder renders root.children, currentSpec gates on children.length —
   but payloads from elsewhere (seeded defaults, imports) can legally be a
   bare condition. Wrap those; leave groups and raw-SQL roots alone. Every
   boundary that accepts a tree from outside runs it through this, which is
   what keeps the three consumers from disagreeing about whether a filter
   is active (the bug where a ★ filter showed as applied but filtered
   nothing).  */
export function normalizeTree(tree) {
  if (tree && tree.type === 'cond') return { type: 'group', op: 'AND', children: [tree] };
  return tree || { type: 'group', op: 'AND', children: [] };
}

export const S = {
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
  advCollapsed: null,     // advanced bar: null = auto (collapse past a handful of terms), true/false = pinned by the ▸/▴ controls
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
  pluginRowActions: [],  // plugin-registered row-menu entries [{id, local_id, plugin, plugin_fs, label, description, max_rows}]
  pluginPanels: [],      // plugin-registered toolbar panels [{id, plugin, plugin_fs, label, entry, description, gen}] — see plugins.js
  pluginDirs: [],        // where the server loads plugins from — shown in the Plugins modal so "drop it where?" has an answer
  lastBrowsePath: null,  // last dir the "Add from this machine…" picker was in — session-only convenience, not persisted
  pluginsCaseOpen: false, // whether /api/plugins was answered with a case open — gates the per-case scope options in Settings → Plugins
  sidebarFilter: '',      // substring filter typed into the sidebar's own search box
  dashboards: [],         // [{id, name, pos, widget_count}] named dashboards, from the case file (see dashboard.js)
  dashboardId: null,      // which named dashboard is currently showing
  folders: [],            // sidebar folder tree [{id, name, parent_id, pos}], from the case file (see sources.js)
  collapsedFolders: new Set(), // folder ids collapsed in the sidebar — a per-browser view pref (localStorage)
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
  tempCase: false,         // the open case is a quick look — gates the home-navigation guard
  tabHistory: [],          // recently visited page tabs, mouse back/forward — see tabhistory.js
  tabHistoryPos: -1,
  pageTabPrefs: null,      // {order, closed, width} for the page-tab strip, from localStorage — set below, see loadPageTabPrefs
  dashboardLibrary: [],    // machine-wide saved boards [{id, name, widget_count}] — workspace/dashboards.json
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
export const gridRowCount = () => (S.groupByCols.length ? S.groupTotalRows : S.view ? S.view.row_count : 0);

export const selViewRows = () => gridRowCount();

export function selCount() {
  return S.selectAll ? Math.max(0, selViewRows() - S.selection.size) : S.selection.size;
}

export const selHas = (pos) => (S.selectAll ? !S.selection.has(pos) : S.selection.has(pos));

export const selAdd = (pos) => { S.selectAll ? S.selection.delete(pos) : S.selection.add(pos); };

export const selRemove = (pos) => { S.selectAll ? S.selection.add(pos) : S.selection.delete(pos); };

export const selToggle = (pos) => { selHas(pos) ? selRemove(pos) : selAdd(pos); };

export function selClear() { S.selectAll = false; S.selection.clear(); }

export function selSetAll() { S.selectAll = true; S.selection.clear(); }

export function selSetRange(from, to) {
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
export function selFirst() {
  if (!S.selectAll) return S.selection.size ? Math.min(...S.selection) : -1;
  for (let p = 0; p < selViewRows(); p++) if (!S.selection.has(p)) return p;
  return -1;
}

/* Materializes ascending. Only for paths that genuinely need every position
   (copy, and tagging an explicit subset), and those check selCount() first —
   the cheap answer to "how many" — rather than building this to measure it. */
export function selPositions() {
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
export function selRemap(fn) {
  const moved = new Set();
  for (const pos of S.selection) {
    const to = fn(pos);
    if (to !== null && to !== undefined) moved.add(to);
  }
  S.selection = moved;
}

/* Positions explicitly unchecked out of a select-all. Empty unless selectAll. */
export const selExcludedPositions = () => (S.selectAll ? [...S.selection] : []);

/* Those same rows as [source_id, rid] pairs, for the bulk tag endpoint —
   or null if any of them isn't in the page cache. They're rows the analyst
   unchecked on screen, so they're cached by construction, but silently
   dropping one would tag a row that was explicitly deselected; the caller
   fetches and retries rather than guessing. */
export function selExcludedPairs() {
  const pairs = [];
  for (const pos of S.selection) {
    const r = rowAt(pos);
    if (!r) return null;
    pairs.push([r.source_id ?? S.sourceId, r.rid]);
  }
  return pairs;
}

export function cellInRange(pos, ci) {
  if (!S.cellRange) return false;
  const { r0, r1, c0, c1 } = S.cellRange;
  return pos >= r0 && pos <= r1 && ci >= c0 && ci <= c1;
}

export const specKey = (spec) => JSON.stringify(spec);

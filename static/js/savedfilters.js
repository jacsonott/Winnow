/* Cross-case saved filters, header-set nicknames, and the suggestion banner.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { renderHead } from './columns.js';
import { $, api, el, post, toast } from './core.js';
import { paintRemote } from './settings.js';
import { currentSpec, renderAdvancedChips, updateSearchHint } from './filters.js';
import { setGrouping } from './grouping.js';
import { syncSearchExpansion } from './search.js';
import { clearAllFilters } from './sources.js';
import { showSqlTab } from './sql.js';
import { normalizeTree, S } from './state.js';
import { updateFiltersButton } from './timeframe.js';
import { baseColumns } from './tsformat.js';
import { promptDialog } from './ui.js';
import { rebuildView } from './view.js';

/* ---------------------------------------------------------- header nicknames */

/* A friendly name for a *set* of headers (e.g. "EVTX exports" instead of a
   long raw column list) — cross-case, workspace-level, keyed by the header
   set itself (order/case-independent), same convention as ColumnLayouts.
   Several saved filters commonly share one header set and so share one
   nickname; there's no per-filter nickname field. */
export function headerSig(colNames) {
  return (colNames || []).map((c) => c.trim().toLowerCase()).sort().join('\x1f');
}

export function nicknameFor(colNames) {
  const sig = headerSig(colNames);
  const rec = S.headerNicknames.find((n) => headerSig(n.col_names) === sig);
  return rec ? rec.nickname : null;
}

export async function loadHeaderNicknames() {
  try { S.headerNicknames = await api('/api/header_nicknames'); } catch { S.headerNicknames = []; }
}

export async function setNicknameFor(colNames, currentName) {
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

export function matchingSavedFilters(colNames) {
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

/* What the suggestion banner used to do, now a state of the Filters
   button itself: matching saved filters put an accent ring on it and the
   matches at the top of its dropdown (see wireSearch), instead of a
   whole toolbar row of chips. Kept as the same entry point every caller
   already had — "something changed that could affect the suggestions". */
export function checkPresets(sourceId) {
  if (sourceId !== S.sourceId) return;
  updateFiltersButton();
}

export function applyPreset(preset) {
  // Deliberately doesn't touch S.timeRange — applying a saved filter/
  // preset must not remove an active timeframe filter (see toggleTimeRange).
  const p = preset.payload || {};
  S.filterTree = normalizeTree(p.filter_tree);
  S.sort = p.sort || S.sort;
  S.search = p.search || '';
  S.searchMode = p.search_mode || 'contains';
  S.searchTerms = p.search_terms || [];
  S.advCollapsed = null;  // back to auto — a many-term preset lands as the one-line summary
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

export async function loadSavedFilters() {
  try { S.savedFilters = await api('/api/saved_filters'); } catch { S.savedFilters = []; }
}

export async function loadAppSettings() {
  try { S.appSettings = await api('/api/settings/app'); } catch { S.appSettings = {}; return; }
  // Remote-session mode is a MACHINE setting now (workspace/, survives
  // updates and port changes) — the server value is the truth. A value in
  // the STORED localStorage blob from before the move migrates up exactly
  // once, then the local copy is dropped so it can never fight the
  // server. Deliberately reads the raw blob, not S.appearance: the
  // defaults always contain the key, and writing the merged object back
  // on a pristine browser would make every context look like a returning
  // install (which silently killed the first-run prompt).
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem('winnow.appearance') || 'null'); } catch { stored = null; }
  if (stored && stored.remoteSession === true && S.appSettings.remote_session !== true) {
    try {
      S.appSettings = await post('/api/settings/app', { remote_session: true });
    } catch { /* keep the local value for this session; retry next boot */ }
  }
  if (stored && 'remoteSession' in stored) {
    delete stored.remoteSession;
    try { localStorage.setItem('winnow.appearance', JSON.stringify(stored)); } catch { /* full/blocked */ }
  }
  S.appearance.remoteSession = !!S.appSettings.remote_session;
  paintRemote();
}

export async function loadCaseSettings() {
  try { S.caseSettings = await api('/api/case_settings'); } catch { S.caseSettings = {}; }
}

export function filtersForCurrentSource() {
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
export function cycleSavedFilter(dir) {
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
export async function openFilterSqlTab() {
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

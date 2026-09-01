/* Sources and merges, the two tab strips, and the sidebar list of every table
in the case.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { recordTabVisit } from './tabhistory.js';
import { renderHead } from './columns.js';
import { $, ROW_H, api, el, post, toast } from './core.js';
import { derivedOps } from './derived.js';
import { currentSpec, renderAdvancedChips, setSearchMode, updateSearchHint } from './filters.js';
import { clearPageCache, headH, rScroll, render, rowAt, spacerPx } from './grid.js';
import { closeAllGroupViews, drawRail, dropGrouping, regroupAll, setGrouping } from './grouping.js';
import { shutdownWinnow } from './home.js';
import { openImportModal } from './importer.js';
import { openMergeBuilder } from './merge.js';
import { showPluginTab } from './plugins.js';
import { checkPresets } from './savedfilters.js';
import { syncSearchExpansion } from './search.js';
import { openSessionManager } from './session.js';
import { showGridTab, showSqlTab, showTimelineTab } from './sql.js';
import { showNotesTab } from './notes.js';
import { showWatchlistTab } from './watchlist.js';
import { loadDashboards, renderDashboardsInto } from './dashboard.js';
import { openCaseSettings } from './settings.js';
import { S, selClear, selCount, selFirst, specKey } from './state.js';
import { openTablesManager } from './tables.js';
import { loadTags, renderTagRibbon } from './tags.js';
import { openTableMenu, updateFiltersButton } from './timeframe.js';
import { baseColumns } from './tsformat.js';
import { confirmDialog, dropdownMenu, modal, promptDialog } from './ui.js';
import { rebuildView } from './view.js';

/* --------------------------------------------------------------- sources */

/* S.sources holds EVERY source/merge in the case (the merge builder, tag
   filter, etc. all need the full set) — the tab strip only renders the
   subset that's "open" (s.is_open), so a case with a dozen imported tables
   doesn't turn into a dozen permanent tabs. Closing a tab (below) just
   toggles that flag; the source/merge and its tags/notes are untouched.
   Errored merges are always shown regardless of is_open — they need
   attention (fix or remove), not a place to hide. */

/* The name a source shows everywhere: the analyst's nickname when one is
   set, else the imported file's name. s.name itself is never rewritten —
   it's the record of what was imported (session hash warnings, the Tables
   manager's identity column), where a nickname is presentation. Merges
   have no nickname field (their name is already analyst-chosen), so this
   falls through to name for them. */
export function sourceLabel(s) { return (s && (s.nickname || s.name)) || ''; }

/* The hover title for a nicknamed source — keeps the real file name one
   hover away wherever the nickname replaced it. */
/* The hover text for a table, wherever its name appears (tab strip,
   sidebar, Tables manager).

   It used to name the source file ONLY when a nickname was set, on the
   reasoning that the label already showed the file name otherwise — which
   left the common case (no nickname) with a tooltip that said nothing
   about the table at all. What an analyst actually wants on hover is
   which file on disk this is: names collide constantly across a triage
   set (four hosts' worth of `Amcache_UnassociatedFileEntries.csv`), and
   the label can't show a path without becoming unreadable. So: the
   nickname relationship when there is one, then the full path, then the
   import date. */
export function sourceTitle(s, suffix) {
  const parts = [];
  if (s) {
    if (s.is_merge) parts.push(`Merge of ${(s.members || []).length || 'several'} tables`);
    else if (s.nickname) parts.push(`${s.nickname} — from ${s.name}`);
    else parts.push(s.name);
    if (s.path) parts.push(s.path);
    if (s.imported_at) parts.push(`Imported ${s.imported_at.replace('T', ' ')}`);
  }
  if (suffix) parts.push(suffix);
  return parts.join('\n');
}

/* Prompt-and-save for a source's nickname (a merge's name — merges have no
   separate file name to fall back to). Returns true when a change was
   saved, so callers know to re-render whatever list they came from. */
export async function editSourceNickname(s) {
  const msg = s.is_merge
    ? 'Rename this merge:'
    : `Nickname for "${s.name}" — shown in place of the file name everywhere. Leave empty to go back to the file name.`;
  const cur = s.is_merge ? s.name : (s.nickname || '');
  const v = await promptDialog(msg, cur, { okLabel: 'Save' });
  if (v === null) return false;
  try {
    await post(`/api/source/${s.id}/nickname`, { nickname: v });
  } catch (e) {
    toast('Could not save that: ' + e.message, 4000);
    return false;
  }
  await loadSources();
  return true;
}

/* Open tabs sorted per S.tabOrder (drag-reordered by the user) — ids not
   yet in tabOrder (a newly opened table) sort after the ones that are, in
   whatever order loadSources()'s API responses returned them. */
export function openTabsSorted() {
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
export async function closeTab(s) {
  viewStateStash.delete(s.id);
  await post(`/api/source/${s.id}/open`, { open: false });
  if (S.sourceId === s.id) S.sourceId = null;
  await loadSources();
}

/* Close every open tab in one call — the tables stay in the case (and the
   folder tree); only their tabs go. The sidebar's Open-section header
   offers this once more than one tab is open. */
export async function closeAllTabs() {
  try {
    await post('/api/tabs/close_all', {});
    viewStateStash.clear();
    S.sourceId = null;
    await loadSources();
  } catch (e) { toast('Could not close tabs: ' + e.message, 6000); }
}

/* Moves an open tab earlier/later in S.tabOrder — the same state
   wireDragReorder's drop handler mutates, just via the Open section's ▲/▼
   instead of a drag. No-ops at either end rather than wrapping. */
export function moveTab(id, dir) {
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

   Used by the horizontal tab strip (wireTabDrag) and the SQL pane's
   sub-tab strip (which passes its own currentIds/onReorder to reorder
   S.sqlTabs and persist to the case file). The sidebar's own table rows
   drag differently now — dragging one files it into a folder rather than
   reordering the strip (see wireTableDrag) — so they don't go through
   here. draggedTabId staying shared across surfaces is harmless: a
   cross-surface drop can't resolve an id the target's own currentIds()
   doesn't contain, so it no-ops. */
export let draggedTabId = null;

export function wireDragReorder(node, id, {
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

export function wireTabDrag(t, id) {
  wireDragReorder(t, id, { containerSelector: '#sourceTabs', rowSelector: '.tab', horizontal: true });
}

export function renderTabs() {
  const openTabs = openTabsSorted();
  const tabs = $('sourceTabs');
  tabs.replaceChildren();
  for (const s of openTabs) {
    const t = el('button', 'tab' + (s.is_merge ? ' tab-merge' : ''));
    t.dataset.id = String(s.id);
    // Same condition syncTabSelection applies, for the same reason: a
    // background loadSources() (an import finishing, say) can land while a
    // page tab is showing, and S.sourceId still names a table then.
    t.setAttribute('aria-selected', String(S.activeTab === 'grid' && s.id === S.sourceId));
    if (s.error) {
      t.append(el('span', null, `⚠ ${s.name}`));
      t.title = s.error;
    } else {
      t.append(el('span', null, (s.is_merge ? '⛓ ' : '') + sourceLabel(s)), el('span', 'count', s.row_count.toLocaleString()));
    }
    const x = el('span', 'x', '✕');
    x.title = 'Close tab — stays in this case, reopen it from Tables';
    x.onclick = async (e) => { e.stopPropagation(); await closeTab(s); };
    t.append(x);
    if (!s.error) {
      t.onclick = () => openSource(s.id);
      // Right-click is where the table menu lives now (the ▦ column-chooser
      // button that used to sit here opened a strict subset of it) — same
      // menu from the tab, the sidebar row and the openTableMenu keybind.
      t.oncontextmenu = (e) => { e.preventDefault(); openTableMenu(s.id); };
      t.title = sourceTitle(s, 'Right-click for the table menu');
    }
    wireTabDrag(t, s.id);
    tabs.append(t);
  }
  renderSidebar(); // every caller here (loadSources, the tab drag-drop handler) means S.sources or S.tabOrder just changed
  return openTabs;
}

/* -------------------------------------------------------------- page tabs */

/* The tabs that aren't tables — SQL, Timeline, and whatever pinned tabs
   plugins registered (plugin_api.register_tab). They have their own strip
   (#pageTabs) beside the table strip, and are reordered by the same
   gestures tabs are: drag along the strip, or drag/▲/▼ in the sidebar.
   Two things differ from S.tabOrder's table tabs, both following from what
   a page tab *is*:

   - It's identified by a string key ('sql' | 'timeline' | 'plugin:<id>'),
     not a numeric source id. That's also what keeps the shared
     wireDragReorder honest between the two strips with one draggedTabId
     between them: a table tab dropped on the page strip resolves to no
     index in that strip's currentIds() and no-ops, and vice versa.
   - Order (and the strip's width) persist in localStorage
     'winnow.pagetabs', next to winnow.sidebar/winnow.detail, instead of
     dying with the case the way S.tabOrder does. "SQL" means the same
     thing in every case, so nothing about switching cases should move it
     back — and a plugin tab belongs to this machine's plugins folder, not
     to any one case file either.

   S.activeTab holds the active page tab's key verbatim ('grid' while a
   table is showing), which is what lets syncTabSelection paint the strip
   from one comparison rather than a branch per tab. */

export const PAGE_TABS_KEY = 'winnow.pagetabs';

export const PAGE_TABS_MIN = 60;

    // a strip narrower than one tab is still usable — it scrolls
export const SOURCE_TABS_MIN = 140;

 // ...but not at the cost of an unreadable table strip

export function loadPageTabPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PAGE_TABS_KEY) || '{}'); } catch { /* fall through to defaults */ }
  // Validated rather than spread-with-defaults (the winnow.detail idiom):
  // these two feed indexOf() and a CSS length, and a corrupt value from an
  // older/hand-edited profile should read as "no preference", not throw.
  return {
    order: Array.isArray(p.order) ? p.order.filter((k) => typeof k === 'string') : [],
    width: typeof p.width === 'number' && p.width > 0 ? p.width : null,
  };
}

export function savePageTabPrefs() { localStorage.setItem(PAGE_TABS_KEY, JSON.stringify(S.pageTabPrefs)); }

/* Every page tab that exists right now, in declaration order. Rebuilt per
   call because S.pluginTabs changes under it — toggling a plugin in
   Settings adds or removes one with no reload. */
export function pageTabs() {
  return [
    { key: 'sql', label: 'SQL', title: 'Read-only SQL against the case file', node: () => $('tabSql'), show: showSqlTab },
    { key: 'timeline', label: 'Timeline', title: 'Unified timeline of every tagged row across the case', node: () => $('tabTimeline'), show: showTimelineTab },
    { key: 'notes', label: 'Notes', title: 'Case narrative — a Markdown scratchpad saved in the case file', node: () => $('tabNotes'), show: showNotesTab },
    { key: 'watchlist', label: 'Watchlist', title: 'IOC watchlist — indicators scanned across every table', node: () => $('tabWatchlist'), show: showWatchlistTab },
    ...S.pluginTabs.map((t) => ({
      key: 'plugin:' + t.id,
      label: t.label,
      title: t.description || `${t.label} — from the ${t.plugin} plugin`,
      plugin: t,
      show: () => showPluginTab(t.id),
    })),
  ];
}

/* Same rule openTabsSorted() applies to tables: keys the user has ordered
   first, anything else after in declaration order — so a plugin installed
   after the order was saved lands at the end rather than nowhere. */
export function pageTabsSorted() {
  const order = S.pageTabPrefs.order;
  return pageTabs().sort((a, b) => {
    const ia = order.indexOf(a.key), ib = order.indexOf(b.key);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

export function setPageTabOrder(keys) {
  S.pageTabPrefs.order = keys;
  savePageTabPrefs();
  renderPageTabs(); // which re-renders the sidebar's Pages section in turn
}

/* Menu-driven equivalent of the drag, for the sidebar's ▲/▼ — no-ops at
   either end rather than wrapping, exactly like moveTab. */
export function movePageTab(key, dir) {
  const keys = pageTabsSorted().map((t) => t.key);
  const idx = keys.indexOf(key);
  const swapIdx = idx + dir;
  if (idx === -1 || swapIdx < 0 || swapIdx >= keys.length) return;
  [keys[idx], keys[swapIdx]] = [keys[swapIdx], keys[idx]];
  setPageTabOrder(keys);
}

/* Alt + 1…0 addresses both strips as one row of keys: 1 is the table tab
   you were last in, and 2…0 are the page tabs in strip order — so the
   digits follow a drag-reorder instead of being nailed to SQL/Timeline.
   Slot 1 re-shows the grid rather than re-opening the source: clicking a
   table's own tab runs openSource(), which resets its filters/sort/search,
   and "take me back to the table I was in" means back to it as you left
   it. Slots past the last page tab are silently ignored — the row is worth
   having whether or not a plugin filled it out. */
export function activateTabSlot(digit) {
  if (digit === '1') {
    const open = openTabsSorted().filter((s) => !s.error);
    // S.sourceId *is* the most recently selected table (openSource is the
    // only thing that sets it, and it survives a trip through the page
    // tabs) — unless it names one that has since been closed.
    const target = open.find((s) => s.id === S.sourceId) || open[0];
    if (!target) return;
    if (target.id === S.sourceId) { if (S.activeTab !== 'grid') showGridTab(); }
    else openSource(target.id);
    // The grid is the one tab that doesn't focus anything on arrival (a
    // click on its tab moves focus for free; a keystroke doesn't), so the
    // SQL editor would still be holding it and the next j/k would type
    // into a query.
    $('body').focus();
    return;
  }
  const t = pageTabsSorted()[(digit === '0' ? 10 : Number(digit)) - 2]; // 2→first page tab … 0→ninth
  if (t) t.show();
}

/* SQL and Timeline are markup in index.html — a dozen places reach them by
   id — so they're *moved* into position here rather than rebuilt; plugin
   tabs are built. Either way a node is wired for dragging exactly once:
   the two reused ones would otherwise collect another listener set on
   every render, and a drop would then apply the same reorder N times. */
export function renderPageTabs() {
  const strip = $('pageTabs');
  const nodes = [];
  for (const t of pageTabsSorted()) {
    const btn = t.plugin ? el('button', 'tab tab-sql tab-plugin', t.label) : t.node();
    if (t.plugin) {
      btn.dataset.tabId = t.plugin.id;
      btn.title = t.title;
      btn.onclick = t.show;
    }
    btn.dataset.pageKey = t.key;
    if (!btn.dataset.dragWired) {
      wireDragReorder(btn, t.key, {
        containerSelector: '#pageTabs',
        rowSelector: '.tab',
        horizontal: true,
        currentIds: () => pageTabsSorted().map((x) => x.key),
        onReorder: setPageTabOrder,
      });
      btn.dataset.dragWired = '1';
    }
    nodes.push(btn);
  }
  strip.replaceChildren(...nodes);
  syncTabSelection();
}

/* One place paints "which tab is current". S.activeTab is either a page
   tab's key or 'grid', in which case S.sourceId names the table tab that
   owns the highlight — which is also why no source tab may look selected
   while a page tab is up: S.sourceId is never cleared on the way to
   SQL/Timeline (there's no single "a source is open" flag to unset), it
   just stops being what's on screen. The sidebar re-render lives here for
   the same reason renderTabs() ends with one — every caller has, by
   definition, just changed what's active. */
export function syncTabSelection() {
  document.querySelectorAll('#pageTabs .tab').forEach((t) =>
    t.setAttribute('aria-selected', String(t.dataset.pageKey === S.activeTab)));
  document.querySelectorAll('#sourceTabs .tab').forEach((t) =>
    t.setAttribute('aria-selected', String(S.activeTab === 'grid' && Number(t.dataset.id) === S.sourceId)));
  renderSidebar();
}

/* ------------------------------------------------- page/table strip split */

/* What's stored is the width the analyst dragged to; what's applied is
   that width clamped to what the bar can currently give it. Nothing writes
   the clamped value back, so opening the same case in a narrower window
   (or with the sidebar out) squeezes the strip without amnesia — widen the
   window again and the chosen width comes back. */

/* The widest the page strip may be given `total` px to share with the
   table strip. Below the point where both minimums fit, neither gets its
   floor and the space is halved instead — starving the table strip to
   nothing to honour a 60px page strip is the worse of the two failures,
   and both strips scroll, so half of a cramped bar is still usable. */
export function pageTabsMaxWidth(total) {
  return Math.max(0, total < PAGE_TABS_MIN + SOURCE_TABS_MIN ? total / 2 : total - SOURCE_TABS_MIN);
}

export function clampPageTabsWidth(px) {
  // The two strips share one pool of space, so the pool is just their
  // current widths added together — true whichever of them is flexing.
  const total = $('sourceTabs').getBoundingClientRect().width + $('pageTabs').getBoundingClientRect().width;
  // ...except before a case is open: #app is [hidden], so every rect is 0
  // and there is nothing to clamp against. Take the stored width as-is and
  // let showApp() re-clamp it once the bar has a real width — clamping
  // against a zero-width bar would otherwise pin the strip at 0px for the
  // whole session, since nothing but a resize re-runs this.
  if (!total) return Math.round(px);
  const max = pageTabsMaxWidth(total);
  return Math.round(Math.min(Math.max(px, Math.min(PAGE_TABS_MIN, max)), max));
}

export function applyPageTabsSize() {
  const strip = $('pageTabs');
  if (!S.pageTabPrefs.width) {
    // No preference: back to the stylesheet's content-width-with-a-60%-cap.
    strip.style.flexBasis = '';
    strip.style.maxWidth = '';
    return;
  }
  strip.style.maxWidth = 'none'; // an explicit width IS the cap now
  strip.style.flexBasis = clampPageTabsWidth(S.pageTabPrefs.width) + 'px';
}

 // paints the saved order onto SQL/Timeline before plugins load

export async function loadSources(select) {
  const [sources, merges, folders] = await Promise.all([
    api('/api/sources'), api('/api/merges'), api('/api/folders'),
  ]);
  S.sources = [...sources, ...merges];
  S.folders = folders;
  await loadDashboards();   // populates S.dashboards for the sidebar's Dashboards section
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

/* "The way I left it", per table: switching tabs used to reset every
   filter, so coming back to a table lost what the analyst was looking at.
   Keyed by source id, in-memory only — a reload starts clean, which is
   also the escape hatch if a stashed filter ever misbehaves. The
   timeframe filter isn't in here because it deliberately survives
   globally (see clearAllFilters); the layout/sort aren't because the
   server already persists those per source. */
const viewStateStash = new Map();

export function clearViewStateStash() { viewStateStash.clear(); }

function stashViewState() {
  if (S.sourceId == null || !S.view) return;
  viewStateStash.set(S.sourceId, {
    filters: { ...S.filters },
    search: S.search,
    searchMode: S.searchMode,
    searchTerms: S.searchTerms.map((t) => ({ ...t })),
    advCollapsed: S.advCollapsed,
    filterTree: JSON.parse(JSON.stringify(S.filterTree)),
    sort: S.sort.map((x) => ({ ...x })),
    tagFilter: [...S.tagFilter],
    groupBy: [...S.groupByCols],
    groupSort: S.groupSort,
    groupSortDir: S.groupSortDir,
    scroll: $('body').scrollTop,
  });
}

export async function openSource(id) {
  const src = S.sources.find((s) => s.id === id);
  if (!src) return;
  recordTabVisit({ kind: 'source', id });
  stashViewState();
  if (S.activeTab !== 'grid') showGridTab();
  S.sourceId = id;
  S.columns = src.columns;
  /* A cached row's `cells` is an array positional to S.columns, so changing
     the column set invalidates every cached page — and the two were never
     tied together. Adding or removing a derived column re-enters here with
     a different S.columns while the cache still holds rows laid out for the
     old one; rendering those would put values under the wrong headers,
     which for evidence is far worse than a blank cell. Cleared here, before
     anything can paint, rather than in each branch below (only one of which
     used to do it). */
  clearPageCache();
  S.filters = {};
  S.search = '';
  S.searchMode = 'contains';
  S.searchTerms = [];
  S.advCollapsed = null;
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
  $('empty').hidden = true;

  const saved = await api(`/api/layout?source_id=${id}`).catch(() => ({}));
  // No per-source layout saved yet (a source opened for the first time) —
  // check for a cross-case default saved for this exact set of column
  // names (Settings > "save default layout" hotkey), same seed-once
  // pattern as the default tag template for a brand-new case.
  let defaultLayout = null;
  if (!saved.order) {
    const qp = new URLSearchParams();
    for (const c of baseColumns()) qp.append('col_names', c.name);
    const found = await api(`/api/column_layouts/find?${qp.toString()}`).catch(() => null);
    if (found && found.order) defaultLayout = found;
  }
  S.layout = saved.columns || (defaultLayout && defaultLayout.columns) || {};
  // Per-source, unlike the per-column overrides inside S.layout: this one is
  // a judgement about *this table's* size, and a cross-case default layout
  // (which is keyed by header set, not row count) has no business carrying it.
  S.valueFilterMode = saved.value_filters || 'auto';
  S.order = (saved.order && saved.order.filter((n) => S.columns.some((c) => c.name === n)))
    || (defaultLayout && defaultLayout.order.filter((n) => S.columns.some((c) => c.name === n)))
    || S.columns.map((c) => c.name);
  for (const c of S.columns) if (!S.order.includes(c.name)) S.order.push(c.name);

  // Default sort: first datetime column, ascending — a timeline wants time order.
  const dt = S.columns.find((c) => c.type === 'datetime');
  if (dt && !saved.sort) S.sort = [{ column: dt.name, dir: 'asc' }];
  if (saved.sort) S.sort = saved.sort;

  // Coming BACK to a table: reapply what was on screen when we left it.
  // After the reset above and the layout/sort defaults, before renderHead
  // (the header filter boxes render from S.filters).
  const stash = viewStateStash.get(id);
  if (stash) {
    S.filters = { ...stash.filters };
    S.search = stash.search;
    S.searchMode = stash.searchMode;
    S.searchTerms = stash.searchTerms.map((t) => ({ ...t }));
    S.advCollapsed = stash.advCollapsed;
    S.filterTree = JSON.parse(JSON.stringify(stash.filterTree));
    S.sort = stash.sort.map((x) => ({ ...x }));
    S.tagFilter = [...stash.tagFilter];
    $('search').value = S.searchMode === 'advanced' ? '' : S.search;
    document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === S.searchMode)));
    if (S.searchMode === 'advanced') renderAdvancedChips();
    syncSearchExpansion();
    updateSearchHint();
    if (stash.groupBy.length) setGrouping(stash.groupBy, stash.groupSort, stash.groupSortDir);
    updateFiltersButton();
  }

  if (S.columns.some((c) => c.derived)) await derivedOps().catch(() => {});
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
    $('spacerY').style.height = spacerPx(cached.row_count) + 'px';
    $('viewStats').innerHTML =
      `<b>${cached.row_count.toLocaleString()}</b> of ${src.row_count.toLocaleString()} rows · cached`;
    $('body').scrollTop = stash ? stash.scroll : 0;
    render();
    drawRail();
    updateFiltersButton();
    // The cached view is flat — a restored grouping still needs its
    // summary levels rebuilt on top of it.
    if (S.groupByCols.length) await regroupAll();
  } else {
    await rebuildView({ keepScroll: false });
    // Same view spec as when we left (the stash IS the spec), so the raw
    // scrollTop still points at the same rows.
    if (stash && !S.groupByCols.length) $('body').scrollTop = stash.scroll;
  }
  syncTabSelection(); // moves the strip highlight and the sidebar's .active row onto this table
  checkPresets(id); // fire-and-forget — a suggestion banner, not core to opening the source
}

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

   Two parts. An OPEN section at the top is the working set — the tables
   with a tab open, in tab-strip order, reorderable by ▲/▼ or drag
   (openSidebarRow); drag a table from the tree onto it to open one. Below
   it, ALL TABLES is a FOLDER TREE: every table sits at the root or inside
   a folder (created by hand, or by a directory import reproducing its
   on-disk structure — see importer.js), open or not. So an open table
   shows in both — the Open section is the tabs you're working with, the
   tree is the whole library. Pages stays its own standing list for the
   *other* strip (SQL, Timeline, plugin tabs). Rows reuse the dropdown's
   .menu-item/.menu-item-action classes. Filtered by S.sidebarFilter — a
   client-side substring match; while a filter is active, folders that
   contain a match are shown and force-expanded.

   Folders: ▸/▾ collapse (persisted per-browser in FOLDERS_KEY), ▲/▼ to
   reorder among siblings, ＋ for a subfolder, ✕ to delete (tables move to
   the parent — a folder is a label, never an owner of evidence). A table
   is filed by dragging its row onto a folder header, via the "Move to a
   folder" button on the row, and dragged back out via the root drop
   zone. */

export const FOLDERS_KEY = 'winnow.folders';   // collapsed folder ids, per browser

export function loadCollapsedFolders() {
  try { return new Set(JSON.parse(localStorage.getItem(FOLDERS_KEY) || '[]')); }
  catch { return new Set(); }
}
function saveCollapsedFolders() {
  localStorage.setItem(FOLDERS_KEY, JSON.stringify([...S.collapsedFolders]));
}

/* Within a folder, tables sort alphabetically by label — "what's open" is
   the Open section's job now, so it's no longer a sort key here. */
function cmpTables(a, b) {
  return sourceLabel(a).localeCompare(sourceLabel(b));
}

/* Folders indexed by parent (null → 'root'), each sibling list ordered by
   pos then name — the shape both the tree render and the move-menu walk. */
function foldersByParent() {
  const kids = new Map();
  for (const f of S.folders) {
    const k = f.parent_id ?? 'root';
    if (!kids.has(k)) kids.set(k, []);
    kids.get(k).push(f);
  }
  for (const arr of kids.values()) arr.sort((a, b) => a.pos - b.pos || a.name.localeCompare(b.name));
  return kids;
}

/* Every folder as a flat list with a "Parent / Child" path label, tree
   order — for the row's "Move to a folder" menu. */
function foldersFlattened() {
  const kids = foldersByParent();
  const out = [];
  (function walk(key, prefix) {
    for (const f of (kids.get(key) || [])) {
      const pathLabel = prefix ? prefix + ' / ' + f.name : f.name;
      out.push({ id: f.id, pathLabel });
      walk(f.id, pathLabel);
    }
  })('root', '');
  return out;
}

export function renderSidebar() {
  const list = $('sidebarList');
  list.replaceChildren();
  const q = S.sidebarFilter.trim().toLowerCase();
  const tableText = (s) => (sourceLabel(s) + ' ' + (s.name || '')).toLowerCase();
  const tables = S.sources.filter((s) => !q || tableText(s).includes(q));

  const tablesIn = new Map();                 // folder id (null→'root') → [tables]
  for (const s of tables) {
    const k = s.folder_id ?? 'root';
    if (!tablesIn.has(k)) tablesIn.set(k, []);
    tablesIn.get(k).push(s);
  }
  for (const arr of tablesIn.values()) arr.sort(cmpTables);
  const childFolders = foldersByParent();

  // Under a filter, keep a folder only if it (or a descendant) holds a match.
  function folderShown(fid) {
    if (!q) return true;
    if ((tablesIn.get(fid) || []).length) return true;
    return (childFolders.get(fid) || []).some((c) => folderShown(c.id));
  }
  function tableCount(fid) {
    let n = (tablesIn.get(fid) || []).length;
    for (const c of (childFolders.get(fid) || [])) n += tableCount(c.id);
    return n;
  }

  function renderInto(key, depth) {
    const folders = (childFolders.get(key) || []).filter((f) => folderShown(f.id));
    folders.forEach((f, i) => {
      list.append(folderHeaderRow(f, depth, folders, i, tableCount(f.id)));
      const collapsed = S.collapsedFolders.has(f.id) && !q;   // a filter forces open
      if (!collapsed) renderInto(f.id, depth + 1);
    });
    for (const s of (tablesIn.get(key) || [])) list.append(sidebarRow(s, { depth }));
  }

  // OPEN — the working set. Reorder within it (drag or ▲/▼) is the tab-strip
  // order; drag a table from the tree below onto it to open one.
  const openSrcs = openTabsSorted().filter((s) => !q || tableText(s).includes(q));
  if (S.sources.length) {
    const oh = el('div', 'menu-header sidebar-open-header');
    oh.append(el('span', 'sidebar-open-label', 'Open'));
    if (openSrcs.length > 1) {
      const closeAll = el('button', 'sidebar-closeall', 'close all');
      closeAll.title = 'Close every open tab — the tables stay in the case, under All tables';
      closeAll.onclick = (e) => { e.stopPropagation(); closeAllTabs(); };
      oh.append(closeAll);
    }
    wireOpenDrop(oh);
    list.append(oh);
    if (openSrcs.length) {
      openSrcs.forEach((s, i) => list.append(openSidebarRow(s, i, openSrcs.length)));
    } else {
      const z = el('div', 'sidebar-rootzone sidebar-openzone', 'drag a table here to open it');
      wireOpenDrop(z);
      list.append(z);
    }
  }

  // ALL TABLES — every table in the case, organized into folders whether or
  // not it's currently open.
  if (!tables.length && !S.folders.length) {
    list.append(el('div', 'note-status', q ? 'No matching tables.' : 'No tables in this case yet.'));
  } else {
    if (S.sources.length) list.append(el('div', 'menu-header', 'All tables'));
    renderInto('root', 0);
    if (S.folders.length) list.append(rootDropZone());  // drag a table back out of a folder
  }

  const pages = pageTabsSorted();
  const shownPages = pages.filter((t) => !q || t.label.toLowerCase().includes(q));
  if (shownPages.length) {
    list.append(el('div', 'menu-header', 'Pages'));
    for (const t of shownPages) list.append(pageSidebarRow(t, pages.indexOf(t), pages.length));
  }

  // Dashboards are a section here, not a top-strip tab — a dashboard is a
  // function (build several named boards), not one page. dashboard.js owns
  // the rows (open/rename/delete + "New dashboard").
  if (S.sources.length) renderDashboardsInto(list);
}

/* A folder in the tree: disclosure + name + recursive table count, the
   ▲/▼/＋/✕ actions, and a drop target (a table dragged onto it joins). */
export function folderHeaderRow(f, depth, siblings, index, count) {
  const collapsed = S.collapsedFolders.has(f.id) && !S.sidebarFilter.trim();
  const row = el('div', 'sidebar-folder');
  row.style.setProperty('--depth', String(depth));
  const tw = el('button', 'folder-twisty', collapsed ? '▸' : '▾');
  tw.title = collapsed ? 'Expand' : 'Collapse';
  tw.onclick = () => toggleFolderCollapsed(f.id);
  const name = el('button', 'menu-item folder-name', f.name);
  name.title = 'Click to collapse or expand · double-click to rename';
  name.onclick = () => toggleFolderCollapsed(f.id);
  name.ondblclick = (e) => { e.preventDefault(); renameFolder(f); };
  const cnt = el('span', 'sidebar-row-count', String(count));
  const acts = el('div', 'sidebar-row-actions');
  const up = el('button', 'menu-item-action', '▲');
  up.title = 'Move folder up';
  up.disabled = index === 0;
  up.onclick = () => reorderFolder(f, siblings, -1);
  const down = el('button', 'menu-item-action', '▼');
  down.title = 'Move folder down';
  down.disabled = index === siblings.length - 1;
  down.onclick = () => reorderFolder(f, siblings, 1);
  const add = el('button', 'menu-item-action', '＋');
  add.title = 'New subfolder';
  add.onclick = () => createFolder(f.id);
  const del = el('button', 'menu-item-action', '✕');
  del.title = 'Delete folder — its tables move out, nothing is deleted';
  del.onclick = () => deleteFolder(f);
  acts.append(up, down, add, del);
  row.append(tw, name, cnt, acts);
  wireFolderDrop(row, f.id);
  return row;
}

function rootDropZone() {
  const z = el('div', 'sidebar-rootzone', 'drop here to remove from folder');
  wireFolderDrop(z, null);
  return z;
}

function toggleFolderCollapsed(id) {
  if (S.collapsedFolders.has(id)) S.collapsedFolders.delete(id);
  else S.collapsedFolders.add(id);
  saveCollapsedFolders();
  renderSidebar();
}

export async function createFolder(parentId = null) {
  const name = await promptDialog(parentId ? 'New subfolder name:' : 'New folder name:', '', { okLabel: 'Create' });
  if (!name || !name.trim()) return;
  try {
    await post('/api/folders', { name: name.trim(), parent_id: parentId });
    if (parentId != null) S.collapsedFolders.delete(parentId);  // reveal the new child
    saveCollapsedFolders();
    await loadSources(S.sourceId);
  } catch (e) { toast('Could not create folder: ' + e.message, 6000); }
}

async function renameFolder(f) {
  const name = await promptDialog('Rename folder:', f.name, { okLabel: 'Rename' });
  if (!name || !name.trim() || name.trim() === f.name) return;
  try { await post(`/api/folders/${f.id}/rename`, { name: name.trim() }); await loadSources(S.sourceId); }
  catch (e) { toast('Could not rename folder: ' + e.message, 6000); }
}

async function deleteFolder(f) {
  if (!(await confirmDialog(`Delete folder “${f.name}”? Its tables move out of it; nothing is deleted.`,
    { danger: true, okLabel: 'Delete folder' }))) return;
  try { await api(`/api/folders/${f.id}`, { method: 'DELETE' }); await loadSources(S.sourceId); }
  catch (e) { toast('Could not delete folder: ' + e.message, 6000); }
}

async function reorderFolder(f, siblings, dir) {
  const ids = siblings.map((x) => x.id);
  const i = ids.indexOf(f.id);
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  try {
    await post('/api/folders/reorder', { parent_id: f.parent_id ?? null, ordered_ids: ids });
    await loadSources(S.sourceId);
  } catch (e) { toast('Could not reorder folders: ' + e.message, 6000); }
}

async function moveTableToFolder(sourceId, folderId) {
  try { await post(`/api/source/${sourceId}/folder`, { folder_id: folderId }); await loadSources(S.sourceId); }
  catch (e) { toast('Could not move table: ' + e.message, 6000); }
}

/* The row's "Move to a folder" button — the keyboard/click path to the
   same thing dragging does, and the only way to reach a collapsed or
   scrolled-away target. */
function openMoveMenu(anchor, s) {
  const items = [{ header: 'Move to a folder' }];
  if (s.folder_id != null) items.push({ label: '↥ Remove from folder', onclick: () => moveTableToFolder(s.id, null) });
  items.push({
    label: '＋ New folder…',
    onclick: async () => {
      const name = await promptDialog('New folder name:', '', { okLabel: 'Create' });
      if (!name || !name.trim()) return;
      try {
        const f = await post('/api/folders', { name: name.trim(), parent_id: null });
        await moveTableToFolder(s.id, f.id);
      } catch (e) { toast('Could not create folder: ' + e.message, 6000); }
    },
  });
  const flat = foldersFlattened().filter((f) => f.id !== s.folder_id);
  if (flat.length) items.push('-');
  for (const f of flat) items.push({ label: '🗀 ' + f.pathLabel, onclick: () => moveTableToFolder(s.id, f.id) });
  dropdownMenu(anchor, items);
}

/* Dragging a table row files it into a folder. Its own drag var, separate
   from the tab strip's draggedTabId (wireTabDrag) — the two never share a
   node, and a sidebar drag means "move to folder", not "reorder the strip". */
let draggedTableId = null;

function wireTableDrag(row, id) {
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    draggedTableId = id;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(id));   // Firefox won't start a drag without data
    row.classList.add('dragging');
  });
  row.addEventListener('dragend', () => {
    draggedTableId = null;
    document.querySelectorAll('#sidebarList .drop-into').forEach((n) => n.classList.remove('drop-into'));
    row.classList.remove('dragging');
  });
}

function wireFolderDrop(node, folderId) {
  node.addEventListener('dragover', (e) => {
    if (draggedTableId == null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.add('drop-into');
  });
  node.addEventListener('dragleave', () => node.classList.remove('drop-into'));
  node.addEventListener('drop', (e) => {
    if (draggedTableId == null) return;
    e.preventDefault();
    const id = draggedTableId;
    node.classList.remove('drop-into');
    moveTableToFolder(id, folderId);
  });
}

/* A drop target in the Open section: a table dragged out of the tree
   (draggedTableId) is opened. Kept distinct from wireFolderDrop so the same
   tree-row drag can mean "file into a folder" or "open", by where it lands. */
function wireOpenDrop(node) {
  node.addEventListener('dragover', (e) => {
    if (draggedTableId == null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.add('drop-into');
  });
  node.addEventListener('dragleave', () => node.classList.remove('drop-into'));
  node.addEventListener('drop', async (e) => {
    if (draggedTableId == null) return;
    e.preventDefault();
    const id = draggedTableId;
    node.classList.remove('drop-into');
    await post(`/api/source/${id}/open`, { open: true });
    await loadSources(id);
  });
}

/* A row in the Open section — the working set. Reorder with ▲/▼ or drag
   (that IS the horizontal tab strip's order), close with ✕. The table also
   still appears in its folder in the tree below; this is just the tabs you
   have open, in the order you want them. */
export function openSidebarRow(s, index, total) {
  const active = s.id === S.sourceId && S.activeTab === 'grid';
  const row = el('div', 'sidebar-row sidebar-openrow' + (active ? ' active' : ''));
  const label = el('button', 'menu-item', (s.is_merge ? '⛓ ' : '') + sourceLabel(s) + (s.error ? ' ⚠' : ''));
  label.disabled = !!s.error;
  label.title = s.error || sourceTitle(s, 'Right-click for the table menu');
  label.onclick = () => openSource(s.id);
  if (!s.error) row.oncontextmenu = (e) => { e.preventDefault(); openTableMenu(s.id); };
  row.append(label);
  if (!s.error) row.append(el('span', 'sidebar-row-count', s.row_count.toLocaleString()));
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
  x.title = 'Close tab — stays in the case, still listed under All tables below';
  x.onclick = async () => { await closeTab(s); };
  acts.append(up, down, x);
  row.append(acts);
  wireDragReorder(row, s.id, {
    containerSelector: '#sidebarList', rowSelector: '.sidebar-openrow', horizontal: false,
  });
  wireOpenDrop(row);   // a tree row dropped on an open row opens it too
  return row;
}

/* A page tab's row: click to show it, ▲/▼ or drag to reorder. No ✕ —
   unlike a table tab, a page tab isn't something you can close and reopen
   (SQL and Timeline are always there; a plugin tab comes and goes with its
   plugin's checkbox in Settings), so there's nothing for one to do. */
export function pageSidebarRow(t, index, total) {
  const row = el('div', 'sidebar-row' + (S.activeTab === t.key ? ' active' : ''));
  const label = el('button', 'menu-item', t.label);
  label.title = t.title || t.label;
  label.onclick = t.show;
  row.append(label);
  const acts = el('div', 'sidebar-row-actions');
  const up = el('button', 'menu-item-action', '▲');
  up.title = 'Move earlier';
  up.disabled = index === 0;
  up.onclick = () => movePageTab(t.key, -1);
  const down = el('button', 'menu-item-action', '▼');
  down.title = 'Move later';
  down.disabled = index === total - 1;
  down.onclick = () => movePageTab(t.key, 1);
  acts.append(up, down);
  row.append(acts);
  wireDragReorder(row, t.key, {
    containerSelector: '#sidebarList',
    rowSelector: '.sidebar-row',
    horizontal: false,
    currentIds: () => pageTabsSorted().map((x) => x.key),
    onReorder: setPageTabOrder,
  });
  return row;
}

export function sidebarRow(s, { depth = 0 } = {}) {
  const open = s.is_open;
  // S.activeTab !== 'grid' means SQL/Timeline is showing — S.sourceId is
  // still the last-open source in that state (nothing clears it), but
  // nothing in the sidebar represents SQL/Timeline, so no row should read
  // as active; #tabSql/#tabTimeline carry that highlight instead.
  const active = open && s.id === S.sourceId && S.activeTab === 'grid';
  const row = el('div', 'sidebar-row' + (active ? ' active' : ''));
  row.style.setProperty('--depth', String(depth));
  const label = el('button', 'menu-item', (s.is_merge ? '⛓ ' : '') + sourceLabel(s) + (s.error ? ' ⚠' : ''));
  label.disabled = !!s.error;
  if (s.error) label.title = s.error;
  label.onclick = open ? () => openSource(s.id) : async () => {
    await post(`/api/source/${s.id}/open`, { open: true });
    await loadSources(s.id);
  };
  row.append(label);
  if (!s.error) {
    // Same menu the tab strip's right-click opens, on the table's name in
    // the sidebar — including for a closed table, which openTableMenu opens
    // first (its panels all read the live S.layout/S.columns).
    row.oncontextmenu = (e) => { e.preventDefault(); openTableMenu(s.id); };
    label.title = sourceTitle(s, 'Right-click for the table menu');
    row.append(el('span', 'sidebar-row-count', s.row_count.toLocaleString()));
    const acts = el('div', 'sidebar-row-actions');
    const mv = el('button', 'menu-item-action', '🗀');
    mv.title = 'Move to a folder';
    mv.onclick = (e) => openMoveMenu(e.currentTarget, s);
    acts.append(mv);
    row.append(acts);
    wireTableDrag(row, s.id);
  }
  return row;
}

/* Collapse state persisted the same way S.keymap/S.appearance are
   (localStorage, not workspace/ — this is a per-browser UI preference,
   not case- or cross-case-workflow state). #btnTabJump is the same button
   that used to open the dropdown — same position, same "where users
   already look" reasoning — repurposed into a plain visibility toggle. */
export const SIDEBAR_KEY = 'winnow.sidebar';

export function setSidebarVisible(visible) {
  $('sidebar').hidden = !visible;
  $('btnTabJump').textContent = visible ? '◀' : '▶';
  $('btnTabJump').setAttribute('aria-pressed', String(visible));
  $('btnTabJump').title = visible ? 'Hide the table list' : 'Show every table in the case';
  saveSidebarPrefs({ collapsed: !visible });
}

export const SIDEBAR_W_DEFAULT = 220;
export const SIDEBAR_W_MIN = 140;   // narrower than this and the row counts collide with the names
export const SIDEBAR_W_MAX = 640;

export function setSidebarWidth(px) {
  const w = Math.round(Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, px)));
  // One custom property on the root, read by .sidebar's width — so nothing
  // has to touch inline styles on the element the CSS already owns.
  document.documentElement.style.setProperty('--sidebar-w', w + 'px');
  return w;
}

function saveSidebarPrefs(patch) {
  let cur = {};
  try { cur = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '{}'); } catch { /* start fresh */ }
  localStorage.setItem(SIDEBAR_KEY, JSON.stringify({ ...cur, ...patch }));
}

export function initSidebar() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '{}'); } catch { /* defaults below */ }
  setSidebarWidth(prefs.width || SIDEBAR_W_DEFAULT);
  S.collapsedFolders = loadCollapsedFolders();
  // Collapsed by DEFAULT: a case should open showing the evidence, not a
  // panel of navigation. The tab strip already names the open tables; the
  // sidebar is one ` (or the ◀ button) away when the analyst wants the
  // full list — and once they choose to keep it open, that choice
  // persists like every other panel preference here.
  setSidebarVisible(!(prefs.collapsed ?? true));
}

/* Drag the sidebar's right edge to resize it, double-click to reset —
   same shape as the detail pane's handle (see detail.js). Width is
   persisted next to the collapsed flag, so it survives a reload the way
   every other panel size here does. */
export function wireSidebarResize() {
  const handle = $('sidebarResize');
  if (!handle) return;
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = $('sidebar').getBoundingClientRect().width;
    handle.classList.add('dragging');
    let width = startW;
    const move = (ev) => { width = setSidebarWidth(startW + (ev.clientX - startX)); };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      handle.classList.remove('dragging');
      saveSidebarPrefs({ width });
      // The grid sizes itself off the space left over, so it has to be
      // told the viewport changed — the window 'resize' event never fires
      // for a layout change we made ourselves.
      window.dispatchEvent(new Event('resize'));
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
  handle.addEventListener('dblclick', () => {
    saveSidebarPrefs({ width: setSidebarWidth(SIDEBAR_W_DEFAULT) });
    window.dispatchEvent(new Event('resize'));
  });
}

/* Row identity (source_id, rid) survives a view rebuild even though pos
   doesn't (see CLAUDE.md — positions are view-specific and get wiped on
   every rebuild). Capture whichever row was under the selection/cell-range/
   cursor before the rebuild so clearAllFilters can find that same row again
   afterward and re-center the grid on it instead of dropping the analyst
   back at row 0. */
export function selectedRowAnchor() {
  let pos = -1;
  if (S.cellRange) pos = S.cellRange.r0;
  else if (selCount()) pos = selFirst();
  else if (S.cursor >= 0) pos = S.cursor;
  const r = pos >= 0 ? rowAt(pos) : null;
  return r ? { source_id: r.source_id, rid: r.rid } : null;
}

export async function recenterOnRow(anchor) {
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
  $('body').scrollTop = rScroll($('body'), S.view.row_count,
    pos * ROW_H + ROW_H / 2 + headH() / 2 - $('body').clientHeight / 2, headH());
  render();
}

/* `seed` ({column, raw}) is the one carve-out: "filter to this value and
   nothing else" (Shift+F, the row menu's "…only") is precisely this reset
   plus one filter, and spelling the reset out a second time is how the
   timeframe carve-out below gets forgotten in the copy. */
export async function clearAllFilters(seed = null) {
  // Deliberately doesn't touch S.timeRange — the timeframe filter is meant
  // to survive exactly this ("apply/clear filters shouldn't lose my
  // timeframe"), same as it survives applyPreset() and a tab switch. Use
  // the Timeframe filter's own "Clear" button, or toggleTimeRange, for that.
  const anchor = selectedRowAnchor();
  // Grouping goes with the filters (stashed first, so toggleGrouping can
  // bring it back) — clearing "the filters" should land on the plain
  // table, not on an empty filter under the old grouping.
  if (S.groupByCols.length) {
    S.lastGroupBy = { cols: [...S.groupByCols], sort: S.groupSort, dir: S.groupSortDir };
    await dropGrouping();
  }
  S.filters = seed ? { [seed.column]: seed.raw } : {};
  S.search = ''; S.tagFilter = []; S.searchTerms = []; S.advCollapsed = null;
  S.filterTree = { type: 'group', op: 'AND', children: [] };
  updateFiltersButton();
  $('search').value = '';
  renderHead(); renderTagRibbon();
  if (S.searchMode !== 'contains') await setSearchMode('contains'); // also rebuilds the view
  else await rebuildView({ keepScroll: false });
  syncSearchExpansion(false);
  await recenterOnRow(anchor);
}

export function openExportModal() {
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

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireSources() {
$('tabSplit').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const strip = $('pageTabs'), handle = $('tabSplit');
  const startX = e.clientX;
  const startW = strip.getBoundingClientRect().width;
  const max = pageTabsMaxWidth(startW + $('sourceTabs').getBoundingClientRect().width);
  const min = Math.min(PAGE_TABS_MIN, max);
  handle.classList.add('dragging');
  strip.style.maxWidth = 'none';
  const move = (ev) => {
    // The handle sits on the page strip's left edge, so dragging left grows
    // it — same "toward the edge it's docked against" rule as detailResize.
    const w = Math.round(Math.min(Math.max(startW + (startX - ev.clientX), min), max));
    strip.style.flexBasis = w + 'px';
    S.pageTabPrefs.width = w;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    handle.classList.remove('dragging');
    savePageTabPrefs();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

/* Double-click clears the size rather than restoring some hardcoded px —
   "no preference" is a real state (content width), and it's the one a
   fresh profile starts in. */
$('tabSplit').ondblclick = () => {
  S.pageTabPrefs.width = null;
  applyPageTabsSize();
  savePageTabPrefs();
};

$('sidebarFilter').oninput = () => { S.sidebarFilter = $('sidebarFilter').value; renderSidebar(); };
$('btnNewFolder').onclick = () => createFolder(null);

$('btnTabJump').onclick = () => setSidebarVisible($('sidebar').hidden);

/* Everything scoped to the OPEN CASE. Called "Session" until the word
   was needed for something else: a session is now a named snapshot of the
   analysis, stored in the case file, and this menu is one entry among
   several here rather than the thing itself. */
$('btnCase').onclick = () => dropdownMenu($('btnCase'), [
  { label: 'Import…', onclick: openImportModal },
  { label: 'Merge sources…', onclick: openMergeBuilder },
  { label: 'Tables…', onclick: openTablesManager },
  '-',
  { label: 'Export…', onclick: openExportModal },
  { label: 'Sessions…', onclick: openSessionManager },
  { label: 'Case settings…', onclick: openCaseSettings },
  '-',
  { label: 'Shut down Winnow…', onclick: shutdownWinnow },
]);

// Wrapped, not passed directly: an onclick handler is called with the
// MouseEvent, which would arrive as `seed`.
$('btnReset').onclick = () => clearAllFilters();

// The empty-case state's one useful next action, right where the eye lands —
// the same openImportModal the Case menu's "Import…" entry opens.
$('emptyImportBtn').onclick = () => openImportModal();
}

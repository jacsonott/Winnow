/* Sources and merges, the two tab strips, and the sidebar list of every table
in the case.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { renderHead } from './columns.js';
import { $, ROW_H, api, el, post, toast } from './core.js';
import { derivedOps } from './derived.js';
import { currentSpec, setSearchMode, updateSearchHint } from './filters.js';
import { clearPageCache, headH, rScroll, render, rowAt, spacerPx } from './grid.js';
import { closeAllGroupViews, drawRail, dropGrouping } from './grouping.js';
import { shutdownWinnow } from './home.js';
import { openImportModal } from './importer.js';
import { openMergeBuilder } from './merge.js';
import { showPluginTab } from './plugins.js';
import { checkPresets } from './savedfilters.js';
import { syncSearchExpansion } from './search.js';
import { openSessionManager } from './session.js';
import { showGridTab, showSqlTab, showTimelineTab } from './sql.js';
import { S, selClear, selCount, selFirst, specKey } from './state.js';
import { openTablesManager } from './tables.js';
import { loadTags, renderTagRibbon } from './tags.js';
import { openTableMenu, updateFiltersButton } from './timeframe.js';
import { baseColumns } from './tsformat.js';
import { dropdownMenu, modal, promptDialog } from './ui.js';
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
export function sourceTitle(s, suffix) {
  const parts = [];
  if (s && s.nickname) parts.push(s.name);
  if (suffix) parts.push(suffix);
  return parts.join(' — ');
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
  await post(`/api/source/${s.id}/open`, { open: false });
  if (S.sourceId === s.id) S.sourceId = null;
  await loadSources();
}

/* Moves an open tab earlier/later in S.tabOrder — the same state
   wireTabDrag's drop handler mutates, just via a menu action instead of a
   drag gesture. No-ops silently at either end (dir would move it past the
   first/last position) rather than wrapping. */
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

export function wireSidebarRowDrag(row, id) {
  wireDragReorder(row, id, { containerSelector: '#sidebarList', rowSelector: '.sidebar-row', horizontal: false });
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
  renderSidebar(); // every caller here (loadSources, moveTab, the drag-drop handler) means S.sources or S.tabOrder just changed
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

export async function openSource(id) {
  const src = S.sources.find((s) => s.id === id);
  if (!src) return;
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
    $('body').scrollTop = 0;
    render();
    drawRail();
    updateFiltersButton();
  } else {
    await rebuildView({ keepScroll: false });
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

   Three sections. Open/Closed match the tab strip's own "shown vs not"
   rule (an errored source is always shown, bucketed with Open rather than
   a section of its own); Pages is the same standing list for the *other*
   strip — SQL, Timeline and any plugin tabs — and exists for the same
   reason the table sections do, since that strip scrolls too once a few
   plugin tabs are installed or the divider is dragged in. Rows reuse the
   dropdown's own .menu-item/.menu-item-action classes (see style.css)
   rather than a parallel set of near-identical ones. Filtered by
   S.sidebarFilter — a plain client-side substring match over both kinds,
   not a network round trip, since S.sources/S.pluginTabs are already in
   memory and this can be retyped on every keystroke.

   Open rows also get ▲/▼/✕, and page rows ▲/▼ — the same reorder/close
   each strip offers via drag-and-drop and its own ✕, kept here for when a
   strip is scrolled out of view or a standing list is just easier to act
   on. */
export function renderSidebar() {
  const list = $('sidebarList');
  list.replaceChildren();
  const q = S.sidebarFilter.trim().toLowerCase();
  const match = (s) => !q || s.name.toLowerCase().includes(q);
  const openSrcs = openTabsSorted().filter(match);
  const closedSrcs = S.sources.filter((s) => !s.error && !s.is_open).filter(match);
  // Indices for ▲/▼ come from the *unfiltered* order, so the arrows move a
  // page where it actually is rather than where the filter makes it look.
  const pages = pageTabsSorted();
  const shownPages = pages.filter((t) => !q || t.label.toLowerCase().includes(q));
  if (!openSrcs.length && !closedSrcs.length) {
    list.append(el('div', 'note-status', q ? 'No matching tables.' : 'No tables in this case yet.'));
  }
  if (openSrcs.length) {
    list.append(el('div', 'menu-header', 'Open'));
    openSrcs.forEach((s, i) => list.append(sidebarRow(s, { open: true, index: i, total: openSrcs.length })));
  }
  if (closedSrcs.length) {
    list.append(el('div', 'menu-header', 'Closed'));
    for (const s of closedSrcs) list.append(sidebarRow(s, { open: false }));
  }
  if (shownPages.length) {
    list.append(el('div', 'menu-header', 'Pages'));
    for (const t of shownPages) list.append(pageSidebarRow(t, pages.indexOf(t), pages.length));
  }
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

export function sidebarRow(s, { open, index, total }) {
  // S.activeTab !== 'grid' means SQL/Timeline is showing — S.sourceId is
  // still the last-open source in that state (nothing clears it), but
  // nothing in the sidebar represents SQL/Timeline, so no row should read
  // as active; #tabSql/#tabTimeline carry that highlight instead.
  const active = open && s.id === S.sourceId && S.activeTab === 'grid';
  const row = el('div', 'sidebar-row' + (active ? ' active' : ''));
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
  }
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
  localStorage.setItem(SIDEBAR_KEY, JSON.stringify({ collapsed: !visible }));
}

export function initSidebar() {
  let collapsed = false;
  try { collapsed = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '{}').collapsed ?? false; } catch { /* default: visible */ }
  setSidebarVisible(!collapsed);
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
  S.search = ''; S.tagFilter = []; S.searchTerms = [];
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

$('btnTabJump').onclick = () => setSidebarVisible($('sidebar').hidden);

$('btnSession').onclick = () => dropdownMenu($('btnSession'), [
  { label: 'Import…', onclick: openImportModal },
  { label: 'Merge sources…', onclick: openMergeBuilder },
  { label: 'Tables…', onclick: openTablesManager },
  '-',
  { label: 'Export…', onclick: openExportModal },
  { label: 'Session (save/load)…', onclick: openSessionManager },
  '-',
  { label: 'Shut down Winnow…', onclick: shutdownWinnow },
]);

// Wrapped, not passed directly: an onclick handler is called with the
// MouseEvent, which would arrive as `seed`.
$('btnReset').onclick = () => clearAllFilters();

// The empty-case state's one useful next action, right where the eye lands —
// the same openImportModal the Session menu's "Import…" entry opens.
$('emptyImportBtn').onclick = () => openImportModal();
}

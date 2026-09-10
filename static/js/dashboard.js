/* Case dashboards — a case holds several NAMED boards ("KAPE triage", a
   lateral-movement board), each a grid of widgets. Boards live under the
   sidebar's Dashboards section, not as a top-strip tab: a dashboard is a
   function, not one page. A widget is a data source (read-only SQL, the
   watchlist, or tag totals) rendered a chosen way — number, chart, list,
   chips. A board can be saved into a PROFILE (a plugin bundle + a named
   dashboard) and reused across cases of the same type. See
   docs/design/analysis-suite.md. */

import { drawBars, drawHistogram, pickBar } from './charts.js';
import { renderHead } from './columns.js';
import { $, api, el, post, toast } from './core.js';
import { WIDGET_TEMPLATES, bucketRange, columnsForTable, recipeSql, tableOf, templateById, widgetFrom } from './dashwidgets.js';
import { currentSpec, renderAdvancedChips, updateSearchHint } from './filters.js';
import { syncSearchExpansion } from './search.js';
import { recordTabVisit } from './tabhistory.js';
import { openSource, renderPageTabs, renderSidebar, sourceLabel, syncTabSelection } from './sources.js';
import { showGridTab, showMainView, showSqlTab, syncTabChrome } from './sql.js';
import { S } from './state.js';
import { updateFiltersButton, updateTimeRangeButton } from './timeframe.js';
import { dropdownMenu, modal, promptDialog, confirmDialog } from './ui.js';
import { rebuildView } from './view.js';

let widgets = [];   // the CURRENT board's widgets (the one with S.dashboardId)
let loadError = null;   // why they aren't here, if they aren't — an empty
                        // board and an unreachable server look identical
                        // otherwise, and one of them is alarming

/* The dashboard being dragged from the sidebar, for the Pages header's
   drop target (sources.js) — a tiny shared holder rather than a
   dataTransfer read, which isn't available during dragover. */
export const dashDrag = { id: null };

const num = (v) => (typeof v === 'number' ? v : (parseFloat(String(v).replace(/,/g, '')) || 0));

/* ------------------------------------------------------------ data */

// Fetched into S.dashboards by loadSources so the sidebar can render the
// Dashboards section alongside everything else.
export async function loadDashboards() {
  try { S.dashboards = await api('/api/dashboards'); }
  catch { S.dashboards = []; }
  // The machine-wide library rides along: it's listed under the same
  // sidebar section, and doesn't depend on the case.
  try { S.dashboardLibrary = await api('/api/dashboard_library'); }
  catch { S.dashboardLibrary = []; }
  // Pinned boards are page tabs — the strip has to know about them.
  renderPageTabs();
}

async function loadWidgets(id) {
  try { widgets = (await api(`/api/dashboards/${id}`)).widgets || []; loadError = null; }
  catch (e) {
    widgets = [];
    loadError = e;
    // 404 means this board is not in this case (deleted elsewhere, or an
    // id left over from another case). Re-read the list so the sidebar
    // stops offering it rather than leaving a row that can't open.
    if (e.status === 404) { try { await loadDashboards(); } catch { /* offline */ } }
  }
}

async function persist() {
  if (S.dashboardId == null) return;
  try { await post(`/api/dashboards/${S.dashboardId}`, { widgets }); }
  catch (e) { toast('Could not save dashboard: ' + e.message, 6000); return; }
  // The sidebar's per-board count comes from the list endpoint, so it
  // showed 0 after the first widget until something else reloaded it.
  try { await loadDashboards(); renderSidebar(); } catch { /* offline — the next reload catches up */ }
}

/* --------------------------------------------------------- show a board */

export async function showDashboard(id) {
  recordTabVisit({ kind: 'page', key: 'dashboard:' + id });
  S.activeTab = 'dashboard';
  S.dashboardId = id;
  showMainView('dashboardview');
  syncTabSelection();
  syncTabChrome();
  await loadWidgets(id);
  render();
  // The sidebar carries the "which board am I looking at" highlight, and
  // it only moves when the sidebar is redrawn. Without this the highlight
  // stayed on whichever board was open when the sidebar was last built,
  // so the header named one board while the sidebar pointed at another —
  // and an empty board read as "the board I selected has no widgets".
  renderSidebar();
}

/* ---------------------------------------- sidebar "Dashboards" section */

/* Rendered into #sidebarList by renderSidebar — one row per board (click to
   open, double-click to rename, ✕ to delete) plus "+ New dashboard". */
export function renderDashboardsInto(list) {
  list.append(el('div', 'menu-header', 'Dashboards'));
  for (const d of (S.dashboards || [])) {
    const active = S.activeTab === 'dashboard' && S.dashboardId === d.id;
    const row = el('div', 'sidebar-row' + (active ? ' active' : '') + (d.pinned ? ' sidebar-dash-pinned' : ''));
    const label = el('button', 'menu-item', d.name);
    label.title = `${d.widget_count} widget${d.widget_count === 1 ? '' : 's'}`
      + (d.pinned ? ' · pinned as a page tab' : '') + ' · double-click to rename · drag onto Pages to pin';
    label.onclick = () => showDashboard(d.id);
    label.ondblclick = (e) => { e.preventDefault(); renameDashboard(d); };
    row.append(label, el('span', 'sidebar-row-count', String(d.widget_count)));
    const acts = el('div', 'sidebar-row-actions');
    const pin = el('button', 'menu-item-action', d.pinned ? '⇲' : '⇱');
    pin.title = d.pinned ? 'Unpin from the page tabs' : 'Pin as a page tab (or drag this row onto Pages)';
    pin.onclick = async (e) => {
      e.stopPropagation();
      try { await post(`/api/dashboards/${d.id}`, { pinned: !d.pinned }); await loadDashboards(); renderSidebar(); }
      catch (err) { toast('Could not change the pin: ' + err.message, 5000); }
    };
    const del = el('button', 'menu-item-action', '✕');
    del.title = 'Delete this dashboard';
    del.onclick = (e) => { e.stopPropagation(); deleteDashboard(d); };
    acts.append(pin, del);
    row.append(acts);
    // Drag source for the Pages header's drop target.
    row.draggable = true;
    row.addEventListener('dragstart', (e) => {
      dashDrag.id = d.id;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(d.id));
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => {
      dashDrag.id = null;
      row.classList.remove('dragging');
      document.querySelectorAll('#sidebarList .drop-into').forEach((n) => n.classList.remove('drop-into'));
    });
    list.append(row);
  }
  const add = el('div', 'sidebar-row sidebar-dash-new');
  const addBtn = el('button', 'menu-item', '＋ New dashboard');
  addBtn.onclick = () => createDashboard();
  add.append(addBtn);
  list.append(add);

  // The machine-wide library: boards kept across cases. ＋ copies one into
  // this case (create-or-replace by name); ✕ removes it from the library.
  const lib = S.dashboardLibrary || [];
  if (lib.length) {
    list.append(el('div', 'menu-header sidebar-subheader', 'Library'));
    for (const b of lib) {
      const row = el('div', 'sidebar-row sidebar-dash-library');
      const label = el('button', 'menu-item', b.name);
      label.title = `${b.widget_count} widget${b.widget_count === 1 ? '' : 's'} · saved on this machine — click to add to this case`;
      const addToCase = async () => {
        try {
          const rec = await post(`/api/dashboard_library/${b.id}/add`, {});
          await loadDashboards();
          renderSidebar();
          await showDashboard(rec.id);
          toast(`Added "${b.name}" to this case`);
        } catch (err) { toast('Could not add: ' + err.message, 5000); }
      };
      label.onclick = addToCase;
      row.append(label, el('span', 'sidebar-row-count', String(b.widget_count)));
      const acts = el('div', 'sidebar-row-actions');
      const plus = el('button', 'menu-item-action', '＋');
      plus.title = 'Add this board to the open case';
      plus.onclick = (e) => { e.stopPropagation(); addToCase(); };
      const del = el('button', 'menu-item-action', '✕');
      del.title = 'Remove from the library (cases that already have it keep their copy)';
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!(await confirmDialog(`Remove “${b.name}” from the dashboard library?`, { danger: true, okLabel: 'Remove' }))) return;
        try { await api(`/api/dashboard_library/${b.id}`, { method: 'DELETE' }); await loadDashboards(); renderSidebar(); }
        catch (err) { toast('Could not remove: ' + err.message, 5000); }
      };
      acts.append(plus, del);
      row.append(acts);
      list.append(row);
    }
  }

  // Boards a plugin offers (register_dashboard). Same Library section, no
  // ✕: the plugin owns them, so removing one means turning the plugin off.
  // Offered, never applied — a plugin that dropped a board into every case
  // it could see would be deciding what the analyst opened Winnow to look
  // at.
  const offered = S.pluginDashboards || [];
  if (offered.length) {
    if (!lib.length) list.append(el('div', 'menu-header sidebar-subheader', 'Library'));
    for (const b of offered) {
      const row = el('div', 'sidebar-row sidebar-dash-library');
      const label = el('button', 'menu-item', b.label);
      label.title = `${b.widget_count} widget${b.widget_count === 1 ? '' : 's'} · offered by the ${b.plugin} plugin — click to add to this case`;
      const addToCase = async () => {
        try {
          const rec = await post(`/api/plugin_dashboards/${encodeURIComponent(b.plugin_fs)}/${encodeURIComponent(b.local_id)}/add`, {});
          await loadDashboards();
          renderSidebar();
          await showDashboard(rec.id);
          toast(`Added "${b.label}" to this case`);
        } catch (err) { toast('Could not add: ' + err.message, 5000); }
      };
      label.onclick = addToCase;
      row.append(label, el('span', 'sidebar-row-count', String(b.widget_count)));
      const acts = el('div', 'sidebar-row-actions');
      const plus = el('button', 'menu-item-action', '＋');
      plus.title = `Add this board to the open case (from ${b.plugin})`;
      plus.onclick = (e) => { e.stopPropagation(); addToCase(); };
      acts.append(plus);
      row.append(acts);
      list.append(row);
    }
  }
}

/* Save the current board machine-wide (workspace/dashboards.json), so
   the next case of the same kind can pick it up from the sidebar's
   Library rows without re-applying a whole profile. Upserts by name. */
async function saveToLibrary() {
  const d = (S.dashboards || []).find((x) => x.id === S.dashboardId);
  const name = await promptDialog('Save this dashboard to the library as:', d ? d.name : '', { okLabel: 'Save' });
  if (!name || !name.trim()) return;
  try {
    await post('/api/dashboard_library', { name: name.trim(), widgets });
    await loadDashboards();
    renderSidebar();
    toast(`Saved "${name.trim()}" to the library — it's under Dashboards → Library in every case`);
  } catch (e) { toast('Could not save: ' + e.message, 6000); }
}

/* New board: a name and a starting point. Blank is one choice among
   several — a starter built from the open table, one of the shipped
   boards, or a library board — because an empty grid with instructions
   in it was where most boards stopped. */
export async function createDashboard() {
  const bundles = await api('/api/plugin_bundles').catch(() => []);
  const src = S.sourceId != null ? (S.sources || []).find((s) => s.id === S.sourceId && !s.error && !s.is_merge) : null;
  modal('New dashboard', (b) => {
    const form = el('div', 'dash-form');
    const name = el('input'); name.className = 'confirm-input'; name.placeholder = 'e.g. Overview';
    const from = el('select'); from.className = 'dash-start-from';
    from.append(new Option('Blank', 'blank'));
    if (src) from.append(new Option(`Starter for ${sourceLabel(src)} — row count, activity window, top values`, 'starter'));
    for (const p of bundles.filter((x) => x.shipped && (x.dashboard || []).length)) {
      from.append(new Option(`Shipped: ${p.name} (${p.dashboard.length} widgets)`, 'shipped:' + p.id));
    }
    for (const l of S.dashboardLibrary || []) from.append(new Option(`Library: ${l.name} (${l.widget_count} widgets)`, 'library:' + l.id));
    if (src) from.value = 'starter';
    const f1 = el('div', 'dash-field'); f1.append(el('label', null, 'Name'), name);
    const f2 = el('div', 'dash-field'); f2.append(el('label', null, 'Start from'), from);
    form.append(f1, f2);
    b.append(form);
    b.append(el('p', 'fb-help', 'A starter reads the open table: a row count, its activity window and '
      + 'events over time (when it has a timestamp), and the top values of its low-cardinality columns. '
      + 'Every widget it makes opens the rows behind it when clicked, and can be edited or removed.'));
    const acts = el('div', 'row-actions');
    const create = el('button', 'btn', 'Create');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    create.onclick = async () => {
      if (!name.value.trim()) { toast('Give the dashboard a name'); return; }
      create.disabled = true;
      // The starting widgets first, then ONE create carrying them: a board
      // that exists before its widgets can't be re-created by a retry.
      let start;
      try { start = await startingWidgets(from.value, bundles); }
      catch (e) { toast('Could not build the starting widgets: ' + e.message, 6000); create.disabled = false; return; }
      try {
        const d = await post('/api/dashboards', { name: name.value.trim(), widgets: start });
        $('modal').hidden = true;
        await loadDashboards();
        renderSidebar();
        await showDashboard(d.id);
        if (start.length) toast(`“${d.name}” starts with ${start.length} widget${start.length === 1 ? '' : 's'} — click any to open its rows`, 5000);
      } catch (e) { toast('Could not create dashboard: ' + e.message, 6000); create.disabled = false; }
    };
    name.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); create.click(); } };
    acts.append(create, cancel);
    b.append(acts);
  }, { focus: 'input' });
}

async function startingWidgets(choice, bundles) {
  if (choice === 'starter') return buildStarter(S.sourceId);
  if (choice.startsWith('shipped:')) {
    const p = bundles.find((x) => String(x.id) === choice.slice('shipped:'.length));
    return p ? JSON.parse(JSON.stringify(p.dashboard)) : [];
  }
  if (choice.startsWith('library:')) {
    const rec = await api(`/api/dashboard_library/${choice.slice('library:'.length)}`);
    return rec.widgets || [];
  }
  return [];
}

/* A first board for a table, from its shape alone: the count, the time
   span and rhythm if it has a timestamp, and the top values of up to four
   columns that have few enough distinct values to chart (2–12). Every
   widget is a recipe, so it is editable and drills into its rows. */
export async function buildStarter(sourceId) {
  const src = (S.sources || []).find((s) => s.id === sourceId);
  if (!src) return [];
  const t = tableOf(sourceId);
  const out = [widgetFrom({ template: 'count', table: t }, { title: `Rows in ${sourceLabel(src)}` })];
  const dt = src.columns.find((c) => c.type === 'datetime');
  if (dt) {
    const win = widgetFrom({ template: 'window', table: t, column: dt.name });
    out.push(win);
    // Per day is one solid block when the whole table is a single day's
    // triage; read the window first and bucket by the hour for short spans.
    let bucket = 'day';
    try {
      const rows = (await post('/api/dashboard/widget/preview', { source: 'sql', query: win.query })).rows || [];
      const first = Date.parse(String(rows[0] && rows[0][1] || '').replace(' ', 'T'));
      const last = Date.parse(String(rows[1] && rows[1][1] || '').replace(' ', 'T'));
      if (Number.isFinite(first) && Number.isFinite(last) && last - first <= 2 * 86400e3) bucket = 'hour';
    } catch { /* unknown span — per day is the safe default */ }
    out.push(widgetFrom({ template: 'time', table: t, column: dt.name, bucket }));
  }
  const picked = [];
  for (const c of src.columns.filter((x) => x.type !== 'datetime' && !x.derived).slice(0, 12)) {
    if (picked.length >= 4) break;
    try {
      const vals = await api(`/api/column_values?source_id=${sourceId}&column=${encodeURIComponent(c.name)}&limit=13`);
      const n = (Array.isArray(vals) ? vals : (vals.values || [])).length;
      if (n >= 2 && n <= 12) picked.push(c.name);
    } catch { /* an unreadable column is just not charted */ }
  }
  for (const col of picked) out.push(widgetFrom({ template: 'top', table: t, column: col }));
  return out.filter(Boolean);
}

/* ------------------------------------------- one-click from the grid */

/* Add a ready-made widget to a board without the editor: the column
   header menu, the row menu and the Filters menu all land here. One
   board means no question; none means name the first; several means a
   pick. Returns the board id, or null if the analyst backed out. */
export async function quickAddWidget(w) {
  if (!w) { toast('Nothing to add'); return null; }
  const id = await pickBoard();
  if (id == null) return null;
  try {
    await addWidgetTo(id, w);
  } catch (e) { toast('Could not add the widget: ' + e.message, 5000); return null; }
  const d = (S.dashboards || []).find((x) => x.id === id);
  toast(`Added “${w.title}” to ${d ? d.name : 'the dashboard'} — click it there to open its rows`, 4500);
  return id;
}

async function addWidgetTo(id, w) {
  if (id === S.dashboardId && S.activeTab === 'dashboard') {
    widgets.push(w);
    await persist();
    render();
    return;
  }
  const cur = (await api(`/api/dashboards/${id}`)).widgets || [];
  cur.push(w);
  await post(`/api/dashboards/${id}`, { widgets: cur });
  await loadDashboards();
  renderSidebar();
}

function pickBoard() {
  const boards = S.dashboards || [];
  if (boards.length === 1) return Promise.resolve(boards[0].id);
  if (!boards.length) {
    return (async () => {
      const name = await promptDialog('No dashboard in this case yet — name the first one:', 'Overview', { okLabel: 'Create' });
      if (!name || !name.trim()) return null;
      const d = await post('/api/dashboards', { name: name.trim() });
      await loadDashboards();
      renderSidebar();
      return d.id;
    })();
  }
  return new Promise((resolve) => {
    let done = false;
    let watch = null;
    const finish = (v) => { if (!done) { done = true; clearInterval(watch); resolve(v); } };
    modal('Add to which dashboard?', (b) => {
      const sel = el('select'); sel.className = 'dash-pick-board';
      for (const d of boards) sel.append(new Option(`${d.name} (${d.widget_count} widget${d.widget_count === 1 ? '' : 's'})`, String(d.id)));
      if (S.dashboardId != null && boards.some((d) => d.id === S.dashboardId)) sel.value = String(S.dashboardId);
      b.append(sel);
      const acts = el('div', 'row-actions');
      const ok = el('button', 'btn', 'Add');
      ok.onclick = () => { $('modal').hidden = true; finish(Number(sel.value)); };
      const cancel = el('button', 'btn ghost', 'Cancel');
      cancel.onclick = () => { $('modal').hidden = true; finish(null); };
      acts.append(ok, cancel);
      b.append(acts);
    }, { focus: 'select' });
    // Escape and the Close button hide the modal without telling us.
    watch = setInterval(() => { if ($('modal').hidden) finish(null); }, 150);
  });
}

/* The Filters menu's "count of this view": whatever the grid is showing
   right now — header boxes, search, builder tree, timeframe, tag filter —
   as one number, whose drill reopens the table in exactly this state. */
export async function addViewCountWidget() {
  if (S.sourceId == null) { toast('Open a table first'); return null; }
  const src = (S.sources || []).find((s) => s.id === S.sourceId);
  const spec = currentSpec();
  let sqlText;
  try { sqlText = (await post('/api/view/sql', spec)).sql; }
  catch (e) { toast('Could not build the query: ' + e.message, 5000); return null; }
  const parts = [];
  for (const [c, raw] of Object.entries(S.filters)) if (raw) parts.push(`${c}: ${raw}`);
  if (spec.search) parts.push(`search “${spec.search}”`);
  if ((spec.search_terms || []).length) parts.push(`${spec.search_terms.length} search terms`);
  if (spec.filter_tree) parts.push('filter builder');
  if (spec.time_range && spec.time_range.enabled) parts.push('timeframe');
  if ((spec.tags || []).length) parts.push('tag filter');
  const summary = parts.length ? parts.join(', ') : 'all rows';
  const w = {
    title: `${sourceLabel(src)} — ${summary}`, source: 'sql', render: 'stat', span: 1, sub: 'rows matching this view',
    query: { sql: `SELECT COUNT(*) AS n FROM (\n${sqlText}\n)` },
    drill: { table: tableOf(S.sourceId), spec: {
      filters: { ...S.filters }, filter_tree: spec.filter_tree, search: spec.search, search_mode: spec.search_mode,
      search_terms: spec.search_terms, tags: spec.tags, time_range: spec.time_range } },
  };
  return quickAddWidget(w);
}

/* ------------------------------------------------------- drilldown */

/* The grid state a drill starts from: nothing left over from the last
   visit to this table — openSource restores per-table header filters,
   search and tag filter from its stash, and never touches the timeframe,
   and any of those ANDed onto the widget's condition would show fewer
   rows than the widget counted. */
function resetGridState() {
  S.filters = {};
  S.filterTree = { type: 'group', op: 'AND', children: [] };
  S.search = '';
  S.searchMode = 'contains';
  S.searchTerms = [];
  S.tagFilter = [];
  // A widget's number is computed over the whole table, so the rows behind
  // it are too. The timeframe is global state, so say so rather than
  // turning it off under the analyst.
  if (S.timeRange && S.timeRange.enabled) {
    toast('Timeframe filter turned off — these are all the rows behind that number', 4000);
  }
  S.timeRange = { enabled: false, column: null, start: '', end: '' };
  $('search').value = '';
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === 'contains')));
  syncSearchExpansion(false);
  updateSearchHint();
}

/* Which of several tables a {{all:…}} widget should open: its SQL counted
   across all of them, so the analyst picks. */
function pickTable(ids) {
  return new Promise((resolve) => {
    let done = false;
    let watch = null;
    const finish = (v) => { if (!done) { done = true; clearInterval(watch); resolve(v); } };
    modal('This widget counts across several tables — open which?', (b) => {
      const sel = el('select'); sel.className = 'dash-pick-table';
      for (const id of ids) {
        const s = (S.sources || []).find((x) => x.id === id);
        sel.append(new Option(s ? sourceLabel(s) : `table ${id}`, String(id)));
      }
      b.append(sel);
      const acts = el('div', 'row-actions');
      const ok = el('button', 'btn', 'Open');
      ok.onclick = () => { $('modal').hidden = true; finish(Number(sel.value)); };
      const cancel = el('button', 'btn ghost', 'Cancel');
      cancel.onclick = () => { $('modal').hidden = true; finish(null); };
      acts.append(ok, cancel);
      b.append(acts);
    }, { focus: 'select' });
    watch = setInterval(() => { if ($('modal').hidden) finish(null); }, 150);
  });
}

const deepCopy = (x) => JSON.parse(JSON.stringify(x));

/* From a widget to its rows. `extra.value` is a clicked bar or list row
   (a value of the widget's pivot column); `extra.bucket` a clicked
   histogram bucket. A widget with no drill but its own SQL opens as a
   query in the SQL pane instead — never a dead click. A drill carries
   either `where` (conditions ANDed) or `tree` (a whole filter-tree node,
   for the OR-shaped ones); `column` is what a clicked value filters on. */
export async function drillInto(w, extra = {}) {
  const drill = w.drill;
  if (!drill) {
    if (w.source === 'sql' && w.query && w.query.sql) return openAsQuery(w);
    toast('This widget has no rows to open');
    return;
  }
  // A bucket the timeframe can't express is refused up front, not opened
  // as the whole table under a toast naming the bucket.
  const range = extra.bucket != null ? bucketRange(extra.bucket) : null;
  if (extra.bucket != null && !range) { toast(`Can't open the "${extra.bucket}" bucket as a time range`, 5000); return; }
  let sourceId;
  const m = /^src_(\d+)$/.exec(drill.table || '');
  if (m) sourceId = Number(m[1]);
  else {
    let r;
    try { r = await post('/api/dashboard/resolve', { table: drill.table }); }
    catch (e) { toast(e.message, 5000); return; }
    const ids = (r.source_ids || [r.source_id]).filter((id) => (S.sources || []).some((s) => s.id === id));
    if (!ids.length) { toast('That table is no longer in this case'); return; }
    sourceId = ids.length === 1 ? ids[0] : await pickTable(ids);
    if (sourceId == null) return;
  }
  if (!(S.sources || []).some((s) => s.id === sourceId)) { toast('That table is no longer in this case'); return; }
  await openSource(sourceId, { skipBuild: true });
  resetGridState();
  if (drill.spec) {
    const sp = drill.spec;
    S.filters = { ...(sp.filters || {}) };
    S.filterTree = sp.filter_tree ? deepCopy(sp.filter_tree) : { type: 'group', op: 'AND', children: [] };
    S.search = sp.search || '';
    S.searchMode = sp.search_mode || 'contains';
    S.searchTerms = (sp.search_terms || []).map((x) => ({ ...x }));
    S.tagFilter = [...(sp.tags || [])];
    S.timeRange = sp.time_range ? { ...sp.time_range } : S.timeRange;
    $('search').value = S.searchMode === 'advanced' ? '' : S.search;
    document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === S.searchMode)));
    if (S.searchMode === 'advanced') renderAdvancedChips();
    syncSearchExpansion(!!(S.search || S.searchTerms.length));
    updateSearchHint();
  } else {
    const conds = (drill.where || []).map((c) => ({ type: 'cond', column: c.column, op: c.op, value: c.value }));
    if (drill.tree) conds.push(deepCopy(drill.tree));
    if (extra.value != null && drill.column) {
      const v = String(extra.value);
      conds.push(v === '' || v === '(empty)'
        ? { type: 'cond', column: drill.column, op: 'empty', value: '' }
        : { type: 'cond', column: drill.column, op: 'equals', value: v });
    }
    if (range && drill.column) {
      // The timeframe needs a column the case typed as datetime; for a
      // text column that merely holds dates, the bucket's own prefix is
      // the filter ('2026-03-14 08' matches every row in that hour).
      const col = (S.columns || []).find((c) => c.name === drill.column);
      if (col && col.type === 'datetime') S.timeRange = { enabled: true, column: drill.column, start: range.start, end: range.end };
      else conds.push({ type: 'cond', column: drill.column, op: 'starts', value: String(extra.bucket).replace('T', ' ') });
    }
    S.filterTree = { type: 'group', op: 'AND', children: conds };
  }
  updateTimeRangeButton();
  updateFiltersButton();
  renderHead();
  await rebuildView({ keepScroll: false });
  toast(`Rows behind “${w.title}”` + (extra.value != null && drill.column ? ` · ${drill.column} = ${extra.value}` : '')
    + (extra.bucket != null ? ` · ${extra.bucket}` : ''), 3500);
}

async function openAsQuery(w) {
  try {
    const r = await post('/api/dashboard/resolve', { sql: w.query.sql });
    const name = w.title || 'Widget';
    // Reuse the tab this widget already opened rather than creating one
    // per click. SQL tabs live in the case file, so a pass over a board
    // used to leave a permanent row of identically named duplicates —
    // worst on a board whose widgets have no drill, where this button is
    // the only thing that responds.
    const mine = (S.sqlTabs || []).find((t) => t.name === name && t.sql === r.sql);
    const rec = mine || await post('/api/sql_tabs', { name, sql: r.sql });
    S.sqlTabId = rec.id;
    showSqlTab();
  } catch (e) { toast('Could not open as a query: ' + e.message, 5000); }
}

const drillable = (w) => !!(w.drill || (w.source === 'sql' && w.query && w.query.sql));

async function renameDashboard(d) {
  const name = await promptDialog('Rename dashboard:', d.name, { okLabel: 'Rename' });
  if (!name || !name.trim() || name.trim() === d.name) return;
  try {
    await post(`/api/dashboards/${d.id}`, { name: name.trim() });
    await loadDashboards();
    renderSidebar();
    if (S.dashboardId === d.id) renderBar();
  } catch (e) { toast('Could not rename dashboard: ' + e.message, 6000); }
}

async function deleteDashboard(d) {
  if (!(await confirmDialog(`Delete dashboard “${d.name}”? Its widgets go with it; the case data is untouched.`,
    { danger: true, okLabel: 'Delete dashboard' }))) return;
  try {
    await api(`/api/dashboards/${d.id}`, { method: 'DELETE' });
    const wasShowing = S.dashboardId === d.id;
    await loadDashboards();
    renderSidebar();
    if (wasShowing) {
      const other = (S.dashboards || [])[0];
      if (other) await showDashboard(other.id);
      else { S.dashboardId = null; widgets = []; showGridTab(); }   // nothing left to show
    }
  } catch (e) { toast('Could not delete dashboard: ' + e.message, 6000); }
}

/* --------------------------------------------------------------- render */

function renderBar() {
  const bar = $('dashBar');
  bar.replaceChildren();
  const d = (S.dashboards || []).find((x) => x.id === S.dashboardId);
  const title = el('span', 'dash-title', d ? d.name : 'Dashboard');
  title.title = 'Double-click to rename';
  if (d) title.ondblclick = () => renameDashboard(d);
  bar.append(title, el('div', 'spacer'));
  const add = el('button', 'btn', '＋ Add widget');
  add.onclick = () => openWidgetEditor(null);
  const lib = el('button', 'btn ghost', 'Save to library…');
  lib.title = 'Keep this dashboard on this machine, for any case — adds it under Dashboards → Library';
  lib.onclick = saveToLibrary;
  const prof = el('button', 'btn ghost', 'Save as profile…');
  prof.title = 'Save this dashboard + the enabled plugins as a reusable profile';
  prof.onclick = saveAsProfile;
  bar.append(add, lib, prof);
}

function render() {
  renderBar();
  const grid = $('dashGrid');
  grid.replaceChildren();
  if (!widgets.length && loadError) {
    // Say the widgets could not be FETCHED. Offering "＋ Add widget" here
    // invites someone to rebuild a board that is still perfectly fine.
    const e = el('div', 'dash-empty');
    const gone = loadError.status === 404;
    e.append(el('p', null, gone
      ? 'This dashboard is no longer in this case — it was deleted, or it belongs to another case.'
      : `Could not load this dashboard — ${loadError.message}.`));
    e.append(el('p', null, gone
      ? 'Pick one from the sidebar’s Dashboards section, or make a new one.'
      : loadError.offline
        ? 'Its widgets are safe in the case file; they will be here once Winnow is running again.'
        : 'Its widgets are safe in the case file.'));
    if (!gone) {
      const retry = el('button', 'btn ghost', 'Try again');
      retry.onclick = () => showDashboard(S.dashboardId);
      e.append(retry);
    }
    grid.append(e);
    return;
  }
  if (!widgets.length) {
    const e = el('div', 'dash-empty');
    e.append(el('p', null, 'No widgets yet. A dashboard is a grid of small summaries of the case.'));
    e.append(el('p', null, 'Click “＋ Add widget”, pick a template (row count, top values, events '
      + 'over time…) and a table, and it writes the query for you — tweak it, preview it, choose how '
      + 'it renders (number, chart, list, chips), and save.'));
    e.append(el('p', null, 'Built one you like? “Save as profile…” keeps this dashboard (plus the '
      + 'enabled plugins) to apply on the next case of the same type — the shipped KAPE triage '
      + 'profile is exactly that.'));
    grid.append(e);
  }
  widgets.forEach((w, i) => grid.append(card(w, i)));
  const add = el('div', 'dash-card dash-add', '＋ Add widget');
  add.onclick = () => openWidgetEditor(null);
  grid.append(add);
}

let dragIdx = null;
function wireWidgetDrag(cardEl, i, grip) {
  // The grip is the drag source, not the whole card — a fully-draggable
  // card makes every text selection inside a widget start a drag instead.
  // The card stays the drop target either way.
  grip.draggable = true;
  grip.addEventListener('dragstart', (e) => {
    dragIdx = i;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(i));
    cardEl.classList.add('dragging');
  });
  grip.addEventListener('dragend', () => {
    dragIdx = null;
    document.querySelectorAll('.dash-card.dragging, .dash-card.drop-target')
      .forEach((n) => n.classList.remove('dragging', 'drop-target'));
  });
  cardEl.addEventListener('dragover', (e) => {
    if (dragIdx == null || dragIdx === i) return;
    e.preventDefault();
    cardEl.classList.add('drop-target');
  });
  cardEl.addEventListener('dragleave', () => cardEl.classList.remove('drop-target'));
  cardEl.addEventListener('drop', async (e) => {
    if (dragIdx == null || dragIdx === i) return;
    e.preventDefault();
    cardEl.classList.remove('drop-target');
    const [moved] = widgets.splice(dragIdx, 1);
    widgets.splice(i, 0, moved);
    await persist();
    render();
  });
}

function card(w, i) {
  const c = el('div', 'dash-card' + (w.span ? ` span${Math.min(4, w.span)}` : ''));
  const head = el('div', 'dash-head');
  const grip = el('span', 'dash-grip', '⠿');
  grip.title = 'Drag to reorder';
  head.append(grip, el('h4', null, w.title || '(untitled)'));
  // ONE button per card — everything about the widget, removal included,
  // lives in the editor it opens.
  if (drillable(w)) {
    const open = el('button', 'dash-drill', '⤴');
    open.title = w.drill ? 'Open the rows behind this widget' : 'Open this widget’s query in the SQL pane';
    open.onclick = (e) => { e.stopPropagation(); drillInto(w); };
    head.append(open);
  }
  const edit = el('button', 'dash-edit', '✎');
  edit.title = 'Edit widget';
  edit.onclick = () => openWidgetEditor(w);
  head.append(edit);
  c.append(head);
  wireWidgetDrag(c, i, grip);   // drag the grip to reorder
  const body = el('div', 'dash-widget-body');
  if (w.drill && ['stat', 'kv', 'chips'].includes(w.render)) {
    // The number IS the link. Bars and list rows carry their own (finer)
    // targets in runWidget; the whole body stays clickable for the rest.
    body.classList.add('drillable');
    body.title = 'Open these rows';
    body.onclick = () => drillInto(w);
  }
  c.append(body);
  runWidget(w, body);
  return c;
}

async function runWidget(w, body) {
  body.replaceChildren(el('div', 'note-status', 'Loading…'));
  let data;
  try { data = await post('/api/dashboard/widget/preview', { source: w.source, query: w.query || {} }); }
  catch (e) { body.replaceChildren(el('div', 'note-status', e.message)); return; }
  body.replaceChildren();
  const rows = data.rows || [];
  switch (w.render) {
    case 'stat': {
      // First numeric cell, or the total for watchlist/tags.
      const val = data.total != null ? data.total : (rows[0] ? num(rows[0][rows[0].length - 1]) : 0);
      const s = el('div', 'dash-stat' + (w.tone ? ` ${w.tone}` : ''), val.toLocaleString());
      body.append(s);
      if (w.sub) body.append(el('div', 'dash-sub', w.sub));
      break;
    }
    case 'kv':
      for (const r of rows) {
        const row = el('div', 'dash-kv');
        row.append(el('span', 'k', String(r[0])), el('span', 'v', String(r[1] == null ? '' : r[1])));
        body.append(row);
      }
      break;
    case 'chips':
      for (const r of rows) {
        const on = num(r[1]) > 0 || r[1] === true || String(r[1]).toLowerCase() === 'true';
        body.append(el('span', 'dash-chip ' + (on ? 'on' : 'off'), `${r[0]} ${on ? '✓' : '✗'}`));
      }
      break;
    case 'list':
      for (const r of rows.slice(0, 12)) {
        const row = el('div', 'dash-list-row');
        row.append(el('span', 't', String(r[0])), el('span', 'c', String(r[r.length - 1])));
        if (w.drill && w.drill.column) {
          row.classList.add('drillable');
          row.title = `Open rows where ${w.drill.column} = ${r[0]}`;
          row.onclick = (e) => { e.stopPropagation(); drillInto(w, { value: r[0] }); };
        }
        body.append(row);
      }
      break;
    case 'bar': {
      const canvas = el('canvas'); canvas.style.cssText = 'width:100%;height:100%;display:block';
      body.style.height = '160px'; body.append(canvas);
      let boxes = [];
      requestAnimationFrame(() => {
        boxes = drawBars(canvas, {
          rows: rows.map((r) => ({ label: String(r[0]), value: num(r[r.length - 1]) })),
          label: 'label', value: 'value' }).boxes;
      });
      if (w.drill && w.drill.column) {
        canvas.classList.add('drillable');
        canvas.title = `Click a bar to open those rows (${w.drill.column})`;
        canvas.onclick = (e) => {
          e.stopPropagation();
          const r = pickBar(boxes, e.offsetX, e.offsetY);
          if (r) drillInto(w, { value: r.label });
        };
      }
      break;
    }
    case 'histogram': {
      const canvas = el('canvas'); canvas.style.cssText = 'width:100%;height:100%;display:block';
      body.style.height = '120px'; body.append(canvas);
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      const labels = rows.map((r) => String(r[0]));
      requestAnimationFrame(() => drawHistogram(canvas, {
        buckets: rows.map((r) => [String(r[0]), [num(r[r.length - 1])]]), colors: [accent] }));
      if (w.drill && w.drill.column && w.drill.bucket) {
        canvas.classList.add('drillable');
        canvas.title = 'Click a bar to open that period';
        canvas.onclick = (e) => {
          e.stopPropagation();
          if (!labels.length) return;
          const idx = Math.min(labels.length - 1, Math.max(0, Math.floor(e.offsetX / (canvas.clientWidth / labels.length))));
          drillInto(w, { bucket: labels[idx] });
        };
      }
      break;
    }
    default:
      body.append(el('div', 'note-status', `Unknown render "${w.render}"`));
  }
}


/* The FROM options: this case's own tables (src_<id>), plus portable
   header-set placeholders that resolve on any case — so a widget built
   here survives being saved into a profile and applied elsewhere. */
function dashTableOptions() {
  const opts = [];
  for (const s of (S.sources || [])) {
    if (s.is_merge || s.error) continue;
    opts.push({ value: `src_${s.id}`, label: (s.nickname || s.name) });
  }
  for (const [ph, lbl] of [['{{evtx}}', 'Event logs (EvtxECmd)'], ['{{mft}}', 'MFT (MFTECmd)'],
    ['{{registry}}', 'Registry (RECmd)'], ['{{amcache}}', 'Amcache'], ['{{prefetch}}', 'Prefetch']]) {
    opts.push({ value: ph, label: `${lbl} — any case (portable)` });
  }
  return opts;
}

async function ensureHeaderSets() {
  if (S.headerSets) return;
  try { S.headerSets = await api('/api/header_sets'); }
  catch { S.headerSets = { shorthands: {}, sets: [] }; }
}

/* The widget editor, guided: pick what to show (a template), where from
   (a table), and the column or value it needs, and the SQL is written —
   and shown, under Advanced, for anyone who wants to change it. A widget
   built this way carries its recipe (`build`) so it reopens guided, and a
   drill so clicking it opens its rows. Hand-edited SQL is respected as
   is: the recipe and the drill are dropped rather than left describing a
   query they no longer match. */
function openWidgetEditor(existing, prefill = null) {
  modal(existing ? 'Edit widget' : 'Add widget', (b) => {
    const form = el('div', 'dash-form');
    const mk = (label, node, into = form) => {
      const field = el('div', 'dash-field');
      field.append(el('label', null, label), node);
      into.append(field);
      return field;
    };
    const title = el('input'); title.className = 'confirm-input'; title.value = existing?.title || prefill?.title || '';
    const source = el('select');
    for (const o of ['sql', 'watchlist', 'tags']) source.append(new Option(o, o));
    source.value = existing?.source || 'sql';
    const templ = el('select'); templ.className = 'dash-template';
    for (const t of WIDGET_TEMPLATES) templ.append(new Option(t.label, t.id));
    const tableSel = el('select'); tableSel.className = 'dash-table';
    for (const o of dashTableOptions()) tableSel.append(new Option(o.label, o.value));
    const colSel = el('select'); colSel.className = 'dash-column';
    const valueIn = el('input'); valueIn.className = 'confirm-input dash-value'; valueIn.placeholder = 'value';
    const matchSel = el('select'); matchSel.className = 'dash-match';
    matchSel.append(new Option('equals', 'equals'), new Option('contains', 'contains'));
    const bucketSel = el('select'); bucketSel.className = 'dash-bucket';
    bucketSel.append(new Option('per day', 'day'), new Option('per hour', 'hour'));
    const renderSel = el('select');
    for (const o of ['stat', 'kv', 'chips', 'list', 'bar', 'histogram']) renderSel.append(new Option(o, o));
    renderSel.value = existing?.render || prefill?.render || 'stat';
    const span = el('select');
    for (const o of [1, 2, 3, 4]) span.append(new Option(`${o} column${o === 1 ? '' : 's'}`, String(o)));
    span.value = String(existing?.span || 1);
    const sql = el('textarea'); sql.rows = 5; sql.className = 'dash-sql';
    sql.value = existing?.query?.sql || prefill?.sql || '';
    sql.placeholder = 'SELECT COUNT(DISTINCT RemoteHost) AS hosts FROM src_1';
    const sub = el('input'); sub.className = 'confirm-input'; sub.value = existing?.sub || '';

    // The recipe this widget was built from, if it was.
    const build0 = existing?.build || prefill?.build || null;
    templ.value = build0 ? build0.template : 'blank';
    if (build0 && build0.table && [...tableSel.options].some((o) => o.value === build0.table)) tableSel.value = build0.table;
    if (build0 && build0.value) valueIn.value = build0.value;
    if (build0 && build0.match) matchSel.value = build0.match;
    if (build0 && build0.bucket) bucketSel.value = build0.bucket;

    let autoTitle = build0 ? templateById(build0.template).title(build0) : '';
    const picks = () => ({ template: templ.value, table: tableSel.value, column: colSel.value,
      value: valueIn.value.trim(), match: matchSel.value, bucket: bucketSel.value });
    const fillColumns = (want) => {
      const t = templateById(templ.value);
      const cols = columnsForTable(tableSel.value);
      const timeOnly = t.id === 'time' || t.id === 'window';
      const dts = cols.filter((c) => c.type === 'datetime');
      const list = timeOnly && dts.length ? dts : cols;
      colSel.replaceChildren();
      for (const c of list) colSel.append(new Option(c.name, c.name));
      if (want && [...colSel.options].some((o) => o.value === want)) colSel.value = want;
    };
    const syncFields = () => {
      const t = templateById(templ.value);
      const needs = new Set(t.needs || []);
      fColumn.hidden = !needs.has('column');
      fValue.hidden = !needs.has('value');
      fMatch.hidden = !needs.has('match');
      fBucket.hidden = !needs.has('bucket');
      advanced.open = t.id === 'blank' || advanced.dataset.custom === '1';
    };
    // Rewrite the SQL from the picks — never over a hand edit.
    const regenerate = () => {
      const t = templateById(templ.value);
      if (t.id === 'blank') { advanced.open = true; return; }
      const s = recipeSql(picks());
      // A pick still missing (no column yet, header sets not loaded): no
      // query — Save then says so — rather than the previous template's.
      if (s == null) { sql.value = ''; return; }
      sql.value = s;
      advanced.dataset.custom = '';
      renderSel.value = t.render;
      if (t.span) span.value = String(t.span);
      const nt = t.title(picks());
      if (!title.value.trim() || title.value === autoTitle) title.value = nt;
      autoTitle = nt;
    };
    templ.onchange = () => { fillColumns(colSel.value); syncFields(); regenerate(); };
    tableSel.onchange = () => { fillColumns(colSel.value); regenerate(); };
    colSel.onchange = regenerate;
    valueIn.oninput = regenerate;
    matchSel.onchange = regenerate;
    bucketSel.onchange = regenerate;
    sql.oninput = () => { advanced.dataset.custom = '1'; };

    // What Preview and Save both build — so they can't disagree.
    const draft = () => {
      const w = {
        title: title.value.trim() || '(untitled)', source: source.value, render: renderSel.value,
        span: Number(span.value), sub: sub.value.trim() || undefined,
        query: source.value === 'sql' ? { sql: sql.value.trim() } : {},
      };
      if (source.value === 'sql') {
        const gen = templ.value === 'blank' ? null : widgetFrom(picks());
        if (gen && gen.query.sql === sql.value.trim()) { w.build = gen.build; w.drill = gen.drill; }
        else if (existing && existing.drill && (existing.query?.sql || '').trim() === sql.value.trim()) {
          w.drill = existing.drill;   // shipped widgets: a drill written by hand, SQL untouched
          if (existing.build) w.build = existing.build;
        }
      }
      return w;
    };

    b.append(el('p', 'fb-help', 'Pick what to show and which table it comes from; the query is written for you '
      + 'and sits under Advanced if you want to change it. A widget built this way opens the rows behind it when clicked.'));
    b.append(form);
    const top = el('div', 'dash-form-row');
    mk('Title', title, top); mk('Data source', source, top);
    form.append(top);
    const sqlWrap = el('div');
    const pick = el('div', 'dash-form-row');
    mk('What to show', templ, pick); mk('Table', tableSel, pick);
    sqlWrap.append(pick);
    const detail = el('div', 'dash-form-flex');
    const fColumn = mk('Column', colSel, detail);
    const fMatch = mk('Match', matchSel, detail);
    const fValue = mk('Value', valueIn, detail);
    const fBucket = mk('Bucket', bucketSel, detail);
    sqlWrap.append(detail);
    const advanced = el('details', 'dash-advanced');
    advanced.append(el('summary', null, 'Advanced — the SQL'));
    const sqlField = el('div', 'dash-field');
    sqlField.append(sql);
    advanced.append(sqlField);
    advanced.append(el('p', 'fb-help', 'Read-only against the case. Edit it and the widget is yours: the recipe '
      + 'above stops applying, and the drilldown with it. A {{…}} table is portable — it resolves on any case, '
      + 'so the widget still works when this dashboard is saved as a profile.'));
    sqlWrap.append(advanced);
    form.append(sqlWrap);
    const look = el('div', 'dash-form-row dash-form-row-3');
    mk('Render as', renderSel, look); mk('Sub-label (optional, for stat)', sub, look); mk('Width', span, look);
    form.append(look);
    const syncSql = () => { sqlWrap.style.display = source.value === 'sql' ? '' : 'none'; };
    source.onchange = syncSql; syncSql();

    fillColumns(build0 ? build0.column : null);
    if (!build0 && sql.value.trim()) advanced.dataset.custom = '1';
    syncFields();
    // A portable {{…}} table's columns come from the header sets, fetched once.
    ensureHeaderSets().then(() => {
      if (colSel.options.length) return;
      fillColumns(build0 ? build0.column : null);
      if (!build0) regenerate();
    });

    // Live preview — see the widget's output before committing it.
    const previewWrap = el('div', 'dash-preview-wrap');
    previewWrap.append(el('label', null, 'Preview'));
    const previewCard = el('div', 'dash-card dash-preview');
    const previewBody = el('div', 'dash-widget-body');
    previewCard.append(previewBody);
    previewWrap.append(previewCard);
    b.append(previewWrap);

    const acts = el('div', 'row-actions');
    const previewBtn = el('button', 'btn ghost', 'Preview');
    previewBtn.onclick = () => {
      if (source.value === 'sql' && !sql.value.trim()) { toast('SQL widget needs a query'); return; }
      runWidget(draft(), previewBody);
    };
    const save = el('button', 'btn', 'Save widget');
    save.onclick = async () => {
      if (!title.value.trim()) { toast('Give the widget a title'); return; }
      if (source.value === 'sql' && !sql.value.trim()) { toast('SQL widget needs a query'); return; }
      const w = draft();
      w.title = title.value.trim();   // draft() defaulted to "(untitled)"; keep the real one on save
      if (existing) {
        delete existing.build;        // a stale recipe or drill must not outlive a hand edit
        delete existing.drill;
        Object.assign(existing, w);
      } else widgets.push(w);
      await persist();
      document.getElementById('modal').hidden = true;
      render();
    };
    acts.append(previewBtn, save);
    if (existing) {
      const rm = el('button', 'btn ghost dash-remove', 'Remove widget…');
      rm.onclick = async () => {
        if (!(await confirmDialog(`Remove "${existing.title || '(untitled)'}" from this dashboard?`,
          { danger: true, okLabel: 'Remove' }))) return;
        widgets.splice(widgets.indexOf(existing), 1);
        await persist();
        document.getElementById('modal').hidden = true;
        render();
      };
      acts.append(rm);
    }
    b.append(acts);
  }, { wide: true });
}

async function saveAsProfile() {
  const name = await promptDialog('Save the current plugins + this dashboard as a profile named:');
  if (!name || !name.trim()) return;
  // The profile's plugins = whatever's enabled for this case now.
  const enabled = (S.pluginTabs || []).map((t) => t.plugin_fs);
  const plugins = [...new Set((S.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name).concat(enabled))];
  // Variable DEFINITIONS travel with the profile (name/description/required),
  // never the values — a profile is a template for the next case.
  const variables = (S.caseVariables || []).map((v) => ({
    name: v.name, description: v.description || '', required: !!v.required }));
  try {
    await post('/api/plugin_bundles', { name: name.trim(), plugins, dashboard: widgets, variables });
    toast(`Profile "${name.trim()}" saved — apply it from the Plugin bundles menu on a new case`, 7000);
  } catch (e) { toast(e.message, 6000); }
}

/* "To dashboard…" in the SQL pane: pick a board (or make one), land on it,
   and open the widget editor pre-filled with the editor's current query —
   a query that earned a place on a board shouldn't need retyping. */
async function sqlToWidget(anchor) {
  const sqlText = $('sqlText').value.trim();
  if (!sqlText) { toast('Write a query first'); return; }
  const tab = S.sqlTabs.find((t) => t.id === S.sqlTabId);
  const openOn = async (id) => {
    await showDashboard(id);
    openWidgetEditor(null, { sql: sqlText, title: tab ? tab.name : '', render: 'kv' });
  };
  const boards = S.dashboards || [];
  if (!boards.length) {
    const name = await promptDialog('New dashboard name:', '', { okLabel: 'Create' });
    if (!name || !name.trim()) return;
    const d = await post('/api/dashboards', { name: name.trim() });
    await loadDashboards();
    renderSidebar();
    await openOn(d.id);
    return;
  }
  dropdownMenu(anchor, [
    ...boards.map((d) => ({ label: d.name, onclick: () => openOn(d.id) })),
    '-',
    { label: '＋ New dashboard…',
      onclick: async () => {
        const name = await promptDialog('New dashboard name:', '', { okLabel: 'Create' });
        if (!name || !name.trim()) return;
        const d = await post('/api/dashboards', { name: name.trim() });
        await loadDashboards();
        renderSidebar();
        await openOn(d.id);
      } },
  ]);
}

// The dashboard bar itself is built per-board in renderBar (its buttons
// carry their own handlers); the one piece of static chrome is the SQL
// pane's "To dashboard…" button.
export function wireDashboard() {
  const btn = $('btnSqlWidget');
  if (btn) btn.onclick = () => sqlToWidget(btn);
}

// Dashboards live in the case; a case switch reloads them.
export function resetDashboard() { widgets = []; S.dashboardId = null; S.dashboards = []; }

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
import { openImportModal } from './importer.js';
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

/* What each widget last returned, keyed by its id — handed over by
   GET /api/dashboards/<id> from the case file, so it survives closing the
   case, and topped up here as widgets run. A board paints from this and
   runs only the widgets marked `live`; ↻ Refresh is how everything else
   gets re-run. Entries are {payload, ran_at, elapsed_ms, stale}. */
let cache = {};

/* Bumped whenever the open board changes. A board open fires one request
   per widget that has to run, and switching boards mid-flight leaves those
   in the air: without a generation to compare against, the answer to a
   question about the PREVIOUS board lands in this one's cache under a
   widget id that may well exist here too. The page cache learned this the
   hard way (grid.js clearPageCache) — same guard, same reason. */
let boardGen = 0;

/* How many widgets ↻ Refresh is still waiting on. The bar is rebuilt
   every time a widget lands (the "as of" moves), which would otherwise
   hand the refresh back its enabled label halfway through the run. */
let refreshing = 0;

/* Widget ids whose last re-run did not come back, while a cached result
   was already painted on the card. The number stays — it is still the
   last true answer this widget gave — but its age mark says the re-run
   failed, or a server that blinked during ↻ Refresh would be indis-
   tinguishable from a board that is up to date. Cleared by the next
   successful run and by opening any board (two boards can hold the same
   widget id: an id is derived from the widget's index and question). */
const failedRuns = new Set();

/* Cards that resolved to nothing, keyed by the card ELEMENT they were
   painted into — not by widget id (a widget has none until the store
   mints one) and not by index (the answers arrive out of order, so there
   is no one moment to build this at). render() builds new elements, so
   clearing there empties this by construction.

   Why the board keeps this at all: on a real collection two cards on the
   shipped KAPE board read "(no host facts in this RECmd output)" and
   "(no Defender alert events in the logs)" and each took a full-width
   card on the first screen. Nothing was wrong — those artefacts were not
   in the collection — but the board spent its best space saying so and
   never said what would change it. */
const empties = new Map();

/* Whether the folded cards are ALSO shown in place. Off, which is the
   whole point, and deliberately not a stored preference: it is a look at
   what is behind one line, not a setting, and a board that remembered it
   would quietly be the old board again on the next case. Reset on every
   board load for the same reason. */
let showEmpties = false;

/* One strip repaint per frame. Every widget that lands may add or remove
   a line, so a ten-widget board would otherwise rebuild the strip ten
   times in one open — and rebuilding it under a mousedown loses the
   click, exactly as renderBar() did before scheduleBar(). */
let emptiesRaf = 0;
function scheduleEmpties() {
  if (emptiesRaf) return;
  emptiesRaf = requestAnimationFrame(() => { emptiesRaf = 0; renderEmpties(); });
}

/* One bar repaint per frame. renderBar() tears the whole bar down
   (replaceChildren) and every widget that lands moves the "as of", so a
   cold open of a 26-widget board rebuilt it 27 times — and a mousedown on
   "＋ Add widget" that lands before a rebuild with its mouseup after it
   produces no click at all. Same coalesce as the jobs panel's. */
let barRaf = 0;
function scheduleBar() {
  if (barRaf) return;
  barRaf = requestAnimationFrame(() => { barRaf = 0; renderBar(); });
}

/* The dashboard being dragged from the sidebar, for the Pages header's
   drop target (sources.js) — a tiny shared holder rather than a
   dataTransfer read, which isn't available during dragover. */
export const dashDrag = { id: null };

const num = (v) => (typeof v === 'number' ? v : (parseFloat(String(v).replace(/,/g, '')) || 0));

/* What a widget ASKS — its source and its query, and nothing else. The
   same pair the store fingerprints a cached result with, so the client and
   the case file agree about when an edit throws the old answer away. A
   retitled card keeps its number; a rewritten query does not.

   A `signals` card asks in its CELLS, and a cell's label is part of the
   answer (the payload is label/value pairs and the card reads each drill
   back off it by label), so a relabelled cell does throw its number away
   — which is the store's rule too, in _widget_fingerprint. */
const questionOf = (w) => JSON.stringify([w.source || 'sql', w.query || {},
  (w.cells || []).map((c) => [c.label || '', c.source || 'sql', c.query || {}])]);

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
  boardGen++;
  try {
    const d = await api(`/api/dashboards/${id}`);
    widgets = d.widgets || [];
    cache = d.cache || {};
    failedRuns.clear();
    showEmpties = false;
    loadError = null;
  }
  catch (e) {
    widgets = [];
    cache = {};
    failedRuns.clear();
    showEmpties = false;
    loadError = e;
    // 404 means this board is not in this case (deleted elsewhere, or an
    // id left over from another case). Re-read the list so the sidebar
    // stops offering it rather than leaving a row that can't open.
    if (e.status === 404) { try { await loadDashboards(); } catch { /* offline */ } }
  }
}

async function persist() {
  if (S.dashboardId == null) return;
  let saved;
  try { saved = await post(`/api/dashboards/${S.dashboardId}`, { widgets }); }
  catch (e) { toast('Could not save dashboard: ' + e.message, 6000); return; }
  // A widget added here has no id until the store mints one, and its
  // cached result is filed under that id — so take the ids back, or the
  // new card re-runs its query on every open for the rest of its life.
  for (const [i, sw] of ((saved && saved.widgets) || []).entries()) {
    if (widgets[i] && !widgets[i].id && sw && sw.id) widgets[i].id = sw.id;
  }
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
      const addToCase = async (replace = false) => {
        const route = `/api/plugin_dashboards/${encodeURIComponent(b.plugin_fs)}/${encodeURIComponent(b.local_id)}/add`;
        try {
          const rec = await post(route, { replace });
          await loadDashboards();
          renderSidebar();
          await showDashboard(rec.id);
          toast(`Added "${b.label}" to this case`);
        } catch (err) {
          // The name is the plugin's, so it can collide with a board the
          // analyst built. The server refuses rather than choosing for
          // them; this is where they choose.
          const taken = err.detail && err.detail.error === 'name_taken' ? err.detail : null;
          if (!taken) { toast('Could not add: ' + err.message, 5000); return; }
          const go = await confirmDialog(
            `This case already has a dashboard called “${taken.name}” with `
            + `${taken.widget_count} widget${taken.widget_count === 1 ? '' : 's'}.\n\n`
            + 'Replacing it discards those widgets — there is no undo.',
            { danger: true, okLabel: 'Replace it', cancelLabel: 'Keep mine' });
          if (go) addToCase(true);
        }
      };
      // Not `= addToCase`: onclick hands the handler the click event, which
      // would arrive as `replace`. It survives JSON.stringify as {}, so the
      // body says replace: {} and the route 422s — clicking the name failed
      // while clicking ＋ beside it worked.
      label.onclick = () => addToCase();
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
  // A drill is a navigation — the rows behind a number, from the top —
  // not a filter change on a table the analyst was reading.
  await rebuildView({ keepScroll: false, keepRow: false });
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

/* ---------------------------------------------------- freshness chrome */

/* A cached number with no date on it reads exactly like a live one, and
   DFIR conclusions get drawn from these cards ("only 3 failed logons").
   So every cached card says how old it is, and the bar says it for the
   board. Short form on the card (no room), long form in the bar. */
const WHEN = [[60, 's', 1], [3600, 'm', 60], [86400, 'h', 3600], [Infinity, 'd', 86400]];

export function ranAtAge(ranAt) {
  const t = Date.parse(String(ranAt || '').replace(' ', 'T'));
  if (!Number.isFinite(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

export function shortAge(ranAt) {
  const secs = ranAtAge(ranAt);
  if (secs == null) return '';
  if (secs < 45) return 'now';
  const [, unit, div] = WHEN.find(([lim]) => secs < lim);
  return `${Math.round(secs / div)}${unit}`;
}

export function longAge(ranAt) {
  const secs = ranAtAge(ranAt);
  if (secs == null) return '';
  if (secs < 45) return 'just now';
  if (secs < 3600) { const m = Math.round(secs / 60); return `${m} minute${m === 1 ? '' : 's'} ago`; }
  if (secs < 86400) { const h = Math.round(secs / 3600); return `${h} hour${h === 1 ? '' : 's'} ago`; }
  const d = Math.round(secs / 86400);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

/* Milliseconds the way a person reads them: "0.0 s" for a query that took
   40 ms is a worse answer than the number itself. */
export function runtimeLabel(ms) {
  const n = Number(ms) || 0;
  return n < 1000 ? `${n} ms` : `${(n / 1000).toFixed(1)} s`;
}

export function clockOf(ranAt) {
  const t = Date.parse(String(ranAt || '').replace(' ', 'T'));
  if (!Number.isFinite(t)) return '';
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* The board's own "as of": the OLDEST run on it, not the newest. The
   number an analyst mistrusts is the one that has been sitting longest,
   and a bar quoting the freshest card would be reassuring about exactly
   the wrong thing. */
function boardStamp() {
  // Only results that can say WHEN. A cache entry with no readable
  // ran_at has no age to compare or to print, and one of those landing
  // as the oldest rendered the bar as "As of  · " — two empty spans.
  const hits = widgets.map((w) => w.id && cache[w.id])
    .filter((h) => h && ranAtAge(h.ran_at) != null);
  if (!hits.length) return null;
  let oldest = hits[0];
  let stale = false;
  for (const h of hits) {
    if (h.stale) stale = true;
    if ((ranAtAge(h.ran_at) || 0) > (ranAtAge(oldest.ran_at) || 0)) oldest = h;
  }
  return { ran_at: oldest.ran_at, stale, cached: hits.length, total: widgets.length };
}

/* Rewrites the per-card corner marks in place. Not a re-render: render()
   would re-run the live widgets, and this is called every time one
   finishes. */
function paintAges() {
  const cards = [...$('dashGrid').querySelectorAll('.dash-card:not(.dash-add)')];
  widgets.forEach((w, i) => {
    const mark = cards[i] && cards[i].querySelector('.dash-mark');
    if (mark) fillMark(mark, w);
  });
}

function fillMark(mark, w) {
  const hit = w.id ? cache[w.id] : null;
  const bad = !!(w.id && failedRuns.has(w.id));
  mark.className = 'dash-mark';
  // A live widget that just failed to run is showing its PREVIOUS answer,
  // so it says how old that is rather than wearing the "runs every time"
  // dot, which would be a promise it did not keep.
  if (w.live && !bad) {
    mark.classList.add('dash-live-dot');
    mark.textContent = '';
    mark.title = 'Runs every time this dashboard opens';
    return;
  }
  mark.textContent = hit ? (bad ? '!' : '') + shortAge(hit.ran_at) : '';
  if (!hit) { mark.title = ''; return; }
  mark.classList.add('dash-age');
  if (hit.stale) mark.classList.add('stale');
  if (bad) mark.classList.add('failed');
  mark.title = `Last run ${clockOf(hit.ran_at)} · ${longAge(hit.ran_at)}`
    + (hit.stale ? ' — the case has changed since. ↻ Refresh re-runs it.' : '')
    + (bad ? ' — the last attempt to re-run it did not come back, so this is still the older result.' : '')
    + ` · took ${runtimeLabel(hit.elapsed_ms)}`;
}

/* Something that could have moved these numbers just finished — an
   import landing, a watchlist scan tagging rows. Staleness is only
   computed when a board is LOADED, so a board left open through a
   twenty-minute import went on saying "As of 09:12" in green over numbers
   the import had already invalidated, and only owned up when the analyst
   navigated away and back. Called from the jobs poll and the watchlist
   scan, which are where the app learns those finished.

   It ASKS rather than assumes: the flags come from the same
   Store.data_generation comparison the next open would make, so the board
   never reddens over a re-scan that found exactly what it found last time
   and then quietly greens again on the next open. Only the flag is taken
   from the answer — the payloads on screen are left alone, and a result
   that landed while this was in flight (a newer ran_at) is not touched. */
export async function markDashboardStale() {
  if (S.activeTab !== 'dashboard' || S.dashboardId == null) return;
  const id = S.dashboardId;
  const gen = boardGen;
  let fresh;
  try { fresh = await api(`/api/dashboards/${id}`); } catch { return; }
  if (gen !== boardGen || S.dashboardId !== id) return;
  let moved = false;
  for (const [wid, hit] of Object.entries(fresh.cache || {})) {
    const mine = cache[wid];
    if (mine && hit.stale && !mine.stale && mine.ran_at === hit.ran_at) { mine.stale = true; moved = true; }
  }
  if (!moved) return;
  renderBar();
  paintAges();
}

function renderBar() {
  const bar = $('dashBar');
  bar.replaceChildren();
  const d = (S.dashboards || []).find((x) => x.id === S.dashboardId);
  const title = el('span', 'dash-title', d ? d.name : 'Dashboard');
  title.title = 'Double-click to rename';
  if (d) title.ondblclick = () => renameDashboard(d);
  bar.append(title);
  const st = widgets.length ? boardStamp() : null;
  if (st) {
    const stamp = el('span', 'dash-stamp' + (st.stale ? ' stale' : ''));
    stamp.append('As of ', el('b', null, clockOf(st.ran_at)), ' · ' + longAge(st.ran_at)
      + (st.stale ? ' · the case has changed since' : ''));
    stamp.title = st.stale
      ? 'Rows were imported or tagged after these numbers were worked out. ↻ Refresh re-runs them.'
      : 'The oldest result on this board — the rest are at least this fresh.';
    bar.append(stamp);
  }
  const liveCount = widgets.filter((w) => w.live).length;
  if (widgets.length) {
    const note = el('span', 'dash-live-note');
    note.append(el('span', 'dash-dot'),
      liveCount ? `${liveCount} widget${liveCount === 1 ? ' runs' : 's run'} every time`
        : 'no widget set to run every time');
    note.title = 'A widget set to "run every time the dashboard opens" re-runs on every '
      + 'open; the rest paint their last result. Set it per widget in ✎.';
    bar.append(note);
  }
  bar.append(el('div', 'spacer'));
  if (widgets.length) {
    const refresh = el('button', 'btn dash-refresh', refreshing ? '↻ Refreshing…' : '↻ Refresh all');
    refresh.title = 'Re-run every widget on this board now';
    refresh.disabled = refreshing > 0;
    refresh.onclick = () => refreshBoard();
    bar.append(refresh);
  }
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

/* Re-run everything on the board, keeping the numbers that are already up
   while it happens. Widget by widget rather than through the board-level
   refresh route on purpose: the cards fill in as answers arrive, the way
   an open used to, and one widget that fails leaves the other 25 alone. */
export async function refreshBoard() {
  const grid = $('dashGrid');
  const cards = [...grid.querySelectorAll('.dash-card:not(.dash-add)')];
  const id = S.dashboardId;
  const jobs = [];
  widgets.forEach((w, i) => {
    const body = cards[i] && cards[i].querySelector('.dash-widget-body');
    if (body) jobs.push(runWidget(w, body, { boardId: id, quiet: true }));
  });
  refreshing += 1;
  renderBar();
  try {
    const bad = (await Promise.all(jobs)).filter((r) => r && r.error).length;
    // One toast for the board, not one per card: the cards keep their
    // numbers (see runWidget), so without this a refresh that reached
    // nothing would look exactly like one that changed nothing.
    if (bad) {
      toast(`${bad} of ${jobs.length} widget${bad === 1 ? '' : 's'} could not be re-run — `
        + 'those cards still show their previous results', 6000);
    }
  } finally {
    refreshing -= 1;
    renderBar();
  }
}

function render() {
  renderBar();
  const grid = $('dashGrid');
  grid.replaceChildren();
  // Every card element this Map keyed has just been thrown away.
  empties.clear();
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
  // First child, spanning the grid: the empty cards fold UP, to one line
  // each, rather than down into a footnote nobody scrolls to. Hidden
  // until something lands in it, which on most boards is never.
  const strip = el('div', 'dash-empties');
  strip.hidden = true;
  grid.append(strip);
  widgets.forEach((w, i) => grid.append(card(w, i)));
  const add = el('div', 'dash-card dash-add', '＋ Add widget');
  add.onclick = () => openWidgetEditor(null);
  grid.append(add);
  renderEmpties();
}


/* ------------------------------------------------- cards that said nothing */

/* Why this card has nothing to show, or null when it has something.

   TWO causes, which paint identically on a blank card and are not the
   same answer. Either the artefact is not in this case — import a RECmd
   batch and the card fills in, so it is a gap in the collection — or the
   table IS here and the query matched nothing, which is a finding ("no
   Defender alerts in these logs") and has nothing to import. The strip
   says which, because an analyst about to write "not present" in a
   report needs to know whether anything looked.

   A `stat` is never empty: 0 is an answer, and folding "0 failed logons"
   away would hide the reassuring half of a triage board. Neither is a
   `signals` card that answered any of its cells — a cell that could not
   answer already reports in its own place (paintSignals), beside the
   neighbours that did, and folding the card would take those with it. */
export function emptyStateOf(w, data, err) {
  // Only a missing table folds. A SQL error or a server that blinked is
  // something to fix, not an absence, and it keeps saying so on the card.
  if (err) {
    const d = err.detail;
    return d && typeof d === 'object' && 'missing_table' in d
      ? { missing: true, table: d.missing_table || null } : null;
  }
  if (!data || w.render === 'stat') return null;
  return (data.rows || []).length ? null : { missing: false, table: null };
}

/* The one line. Written for somebody deciding whether the absence is
   theirs to fix, so it says what is missing rather than that something
   is — and it names what the widget actually asked, since "the query
   matched nothing" is a sentence about a table and three of the four
   sources are not tables. */
export function emptyReason(w, state) {
  if (state.missing) {
    return state.table
      ? `no “${state.table}” table in this case yet`
      : 'the table this card read is no longer in this case';
  }
  if (w.source === 'watchlist') return 'nothing on this case’s watchlist yet';
  if (w.source === 'tags') return 'no tags defined in this case yet';
  if (w.source === 'cells') return 'this card has no signals on it';
  return 'the table is in the case; the query matched nothing';
}

/* One card just answered, or failed: file it in the strip, or take it
   out. Called from every place a card body is painted — the cache paint
   on open, a run landing, the editor's Run now — so a card can never be
   folded away on the strength of a stale answer.

   It also writes the reason into the card's own body, which matters for
   the one moment the card is visible: "Show them" puts it back on the
   board, and the shipped queries no longer UNION in a sentence of their
   own (that sentence is what held the full-width card this replaces). */
function noteEmpty(w, cardEl, data, err) {
  // Cards on the BOARD only. The editor's Preview runs an unsaved draft
  // through this same runWidget, into a `.dash-card.dash-preview` inside
  // the modal — which wants its own error text, not a line in a strip it
  // is not on. Tested by the class rather than by the parent node: the
  // cached paint happens while the card is still being built, before
  // render() has appended it to the grid.
  if (!cardEl || cardEl.classList.contains('dash-preview')) return;
  const state = emptyStateOf(w, data, err);
  if (state) {
    empties.set(cardEl, { w, state });
    const body = cardEl.querySelector('.dash-widget-body');
    if (body) {
      body.style.height = '';
      body.replaceChildren(el('div', 'dash-empty-note', emptyReason(w, state)));
    }
  } else empties.delete(cardEl);
  scheduleEmpties();
}

/* The strip: one line per card that resolved to nothing, in BOARD order
   (read off the grid, since the answers arrive in whatever order the
   queries finish), plus the toggle that puts the cards back. Rebuilt
   whole — it is a handful of rows, and rebuilding is what keeps it in
   order as answers land. */
function renderEmpties() {
  const grid = $('dashGrid');
  const strip = grid.querySelector('.dash-empties');
  if (!strip) return;
  const cards = [...grid.querySelectorAll('.dash-card:not(.dash-add)')];
  const folded = cards.filter((c) => empties.has(c));
  // Hidden, never removed: paintAges(), refreshBoard() and the editor's
  // Run now all reach a card by its INDEX among the cards on the grid.
  cards.forEach((c) => c.classList.toggle('is-empty', !showEmpties && empties.has(c)));
  strip.replaceChildren();
  strip.hidden = !folded.length;
  if (!folded.length) return;
  const head = el('div', 'dash-empties-head');
  head.append(el('span', 'n', `${folded.length} empty card${folded.length === 1 ? '' : 's'}`));
  const toggle = el('button', 'dash-empties-act', showEmpties ? 'Fold them back' : 'Show them');
  toggle.title = showEmpties
    ? 'Collapse these back to one line each'
    : 'Put these cards back on the board, so they can be edited or removed';
  toggle.onclick = () => { showEmpties = !showEmpties; renderEmpties(); };
  head.append(toggle);
  strip.append(head);
  for (const cardEl of folded) {
    const { w, state } = empties.get(cardEl);
    const row = el('div', 'dash-empties-row');
    row.append(el('span', 't', w.title || '(untitled)'),
      el('span', 'r', emptyReason(w, state)));
    // The fix, where there is one. A deleted src_N has no artefact to
    // import — a new file would be a new table with a new id — so that
    // case gets the pencil and nothing else.
    if (state.missing && state.table) {
      const imp = el('button', 'dash-empties-act', 'Import one ▸');
      imp.title = `Import a “${state.table}” file into this case`;
      imp.onclick = () => openImportModal();
      row.append(imp);
    }
    const edit = el('button', 'dash-empties-act dash-empties-edit', '✎');
    edit.title = 'Edit widget';
    edit.onclick = () => openWidgetEditor(w);
    row.append(edit);
    strip.append(row);
  }
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
  // How old this card's number is, or a dot if it re-runs on every open.
  // In the head rather than floating over the body because a bar and a
  // histogram give their body a fixed height and a canvas that fills it.
  const mark = el('span', 'dash-mark');
  fillMark(mark, w);
  head.append(mark);
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
  // Paint what it said last time FIRST, then re-run only if it has to.
  // This is the whole point of the change: a 26-widget board used to be 26
  // queries on every open, every card drag and every edit of any other
  // card, and an analyst reopening a case in the morning paid for all of
  // them before a single number appeared.
  const hit = w.id ? cache[w.id] : null;
  if (hit) { paintWidget(w, body, hit.payload); noteEmpty(w, c, hit.payload, null); }
  if (!hit || w.live) runWidget(w, body, { boardId: S.dashboardId, quiet: !!hit });
  return c;
}

/* Run one widget and paint it. `boardId` files the result in the case
   file under this widget — omitted (the editor's Preview, which runs an
   unsaved draft) it caches nothing, because a Preview that answered from
   the cache would not be previewing anything. `quiet` keeps whatever is
   already on the card while the query runs, instead of blanking a good
   number to say "Loading…". */
async function runWidget(w, body, opts = {}) {
  const gen = boardGen;
  const req = { source: w.source, query: w.query || {} };
  if (w.cells) req.cells = w.cells;   // a signals card: one request, one answer per cell
  if (opts.boardId != null && w.id) { req.dashboard_id = opts.boardId; req.widget_id = w.id; }
  if (!opts.quiet) body.replaceChildren(el('div', 'note-status', 'Loading…'));
  const mine = () => gen === boardGen && S.dashboardId === opts.boardId;
  let data;
  try { data = await post('/api/dashboard/widget/preview', req); }
  catch (e) {
    // `quiet` means a good number is already on this card. Replacing 26 of
    // them with 26 identical grey error lines because the server blinked
    // throws away the only thing the cache was for — and throws away
    // nothing real, since the payloads are still here and a reopen brings
    // them back, which makes a transient failure read as a lost cache.
    if (opts.quiet && w.id && cache[w.id]) {
      failedRuns.add(w.id);
      if (mine()) paintAges();
      return { error: e };
    }
    body.replaceChildren(el('div', 'note-status', e.message));
    // A missing table is an absence, not a breakage — noteEmpty folds the
    // card into the strip and replaces the sentence above with one that
    // says what to import.
    noteEmpty(w, body.closest('.dash-card'), null, e);
    return { error: e };
  }
  if (req.dashboard_id != null && w.id) failedRuns.delete(w.id);
  // The board may have been closed or swapped while this was in flight;
  // filing the answer would file it against another board's widget. No
  // ran_at means the server did not file it either (the widget left the
  // board while this was out), so neither does this — caching it here
  // would paint an age-less card the bar cannot date.
  if (req.dashboard_id != null && data.ran_at && mine()) {
    cache[w.id] = { payload: data, ran_at: data.ran_at,
      elapsed_ms: data.elapsed_ms || 0, stale: false };
    scheduleBar();
    paintAges();
  }
  paintWidget(w, body, data);
  noteEmpty(w, body.closest('.dash-card'), data, null);
  return {};
}

export function paintWidget(w, body, data) {
  body.replaceChildren();
  body.style.height = '';
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
    case 'signals':
      paintSignals(w, body, data);
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


/* ------------------------------------------------------------ signals */

/* A grid of labelled numbers where EVERY CELL KEEPS ITS OWN DRILL.

   This is the render kind that let eleven single-number cards become one:
   folding them into a `kv` card would have folded eleven working
   drill-throughs into one OR-of-everything, and a click that opens a
   superset of the rows you were reading is worse than one that opens
   nothing. A cell is a small widget — its own source, its own query, its
   own drill — so the number and the rows behind it stay the pair they
   were on the card it came from.

   Cells with `chip: true` draw as yes/no pills above the numbers, which
   is what lets one card answer "is anything logging?" and "how much was
   tampered with?" together. The server answers in cell order; the
   metadata is looked up by LABEL so a payload cached before the card was
   edited still lines its drills up with its numbers (and a cell it no
   longer has simply renders without one). */
function paintSignals(w, body, data) {
  const rows = data.rows || [];
  const errs = data.cell_errors || [];
  const meta = new Map((w.cells || []).map((c) => [String(c.label == null ? '' : c.label), c]));
  const chips = [], nums = [];
  rows.forEach((r, i) => {
    const label = String(r[0] == null ? '' : r[0]);
    const cell = meta.get(label) || {};
    const item = { label, value: r[r.length - 1], cell, err: errs[i] || null };
    (cell.chip ? chips : nums).push(item);
  });
  if (!rows.length) { body.append(el('div', 'note-status', 'No signals on this card')); return; }
  if (chips.length) {
    const line = el('div', 'dash-chipline');
    for (const s of chips) {
      const on = num(s.value) > 0 || s.value === true || String(s.value).toLowerCase() === 'true';
      // "?" rather than ✗: a cell whose table is not in this case has not
      // answered "no", it has not answered.
      const chip = el('span', 'dash-chip ' + (s.err ? 'unknown' : on ? 'on' : 'off'),
        `${s.label} ${s.err ? '?' : on ? '✓' : '✗'}`);
      wireSignal(chip, w, s);
      line.append(chip);
    }
    body.append(line);
  }
  if (nums.length) {
    const grid = el('div', 'dash-signals');
    for (const s of nums) {
      const cellEl = el('div', 'dash-signal' + (s.cell.tone ? ` ${s.cell.tone}` : ''));
      cellEl.append(el('div', 'n', s.err || s.value == null ? '—' : num(s.value).toLocaleString()),
        el('div', 'l', s.label));
      wireSignal(cellEl, w, s);
      grid.append(cellEl);
    }
    body.append(grid);
  }
  // The card's sub-label, under the grid rather than under a number —
  // the same `.dash-sub` a stat card carries, which is the only other
  // render kind that shows one. A card of ten numbers is exactly the one
  // that needs a line saying what they are numbers OF.
  if (w.sub) body.append(el('div', 'dash-sub', w.sub));
}

/* One cell, as the drilldown sees it: a widget of its own. Keeping the
   card's title in front of the label is what makes the toast the drill
   lands with ("Rows behind …") name the number that was clicked rather
   than the card it lives on. */
export function signalWidget(w, cell, label) {
  return { title: `${w.title || 'Signals'} · ${label}`, source: cell.source || 'sql',
    render: 'stat', query: cell.query || {}, drill: cell.drill };
}

function wireSignal(node, w, s) {
  const target = signalWidget(w, s.cell, s.label);
  if (!drillable(target)) { if (s.err) node.title = s.err; return; }
  node.classList.add('drillable');
  node.title = s.err ? s.err
    : (s.cell.drill ? `Open the rows behind “${s.label}”` : `Open “${s.label}” as a query`);
  node.onclick = (e) => { e.stopPropagation(); drillInto(target); };
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
    for (const o of ['sql', 'watchlist', 'tags', 'cells']) source.append(new Option(o, o));
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
    for (const o of ['stat', 'kv', 'chips', 'list', 'bar', 'histogram', 'signals']) renderSel.append(new Option(o, o));
    renderSel.value = existing?.render || prefill?.render || 'stat';
    const span = el('select');
    for (const o of [1, 2, 3, 4]) span.append(new Option(`${o} column${o === 1 ? '' : 's'}`, String(o)));
    span.value = String(existing?.span || 1);
    const sql = el('textarea'); sql.rows = 5; sql.className = 'dash-sql';
    sql.value = existing?.query?.sql || prefill?.sql || '';
    sql.placeholder = 'SELECT COUNT(DISTINCT RemoteHost) AS hosts FROM src_1';
    const sub = el('input'); sub.className = 'confirm-input'; sub.value = existing?.sub || '';
    const live = el('input'); live.type = 'checkbox'; live.className = 'dash-live-box';
    live.checked = !!existing?.live;

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
      // A signals card's questions live in its cells, which this editor
      // shows and does not rewrite — one question per widget is its whole
      // shape. Carrying them through is what keeps Save from turning a
      // twelve-number card into an empty one.
      if (source.value === 'cells') w.cells = (existing && existing.cells) || [];
      if (live.checked) w.live = true;   // absent means "use the cache" — see the save handler
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
    // What a signals card holds, read-only: the editor asks one question
    // per widget, and this card asks several. Saying so beats a form that
    // silently drops them.
    const cellsNote = el('div', 'dash-cells-note');
    const cells0 = (existing && existing.cells) || [];
    cellsNote.append(el('p', 'fb-help', cells0.length
      ? `${cells0.length} signals on this card, each with its own drill: `
        + cells0.map((c) => c.label).join(' · ')
        + '. Title, width and “run every time” are edited here; the signals themselves '
        + 'are part of the profile or board this card came from.'
      : 'A signals card is a grid of labelled numbers, each keeping its own drill. '
        + 'This editor writes one question per widget, so a new one is built by saving '
        + 'a board that already has one — or by hand in the profile JSON.'));
    form.append(cellsNote);
    const look = el('div', 'dash-form-row dash-form-row-3');
    mk('Render as', renderSel, look); mk('Sub-label (optional, for stat)', sub, look); mk('Width', span, look);
    form.append(look);
    // Run-every-time, and what it costs. The number beside it is this
    // widget's own last runtime, because "is this one expensive?" is the
    // only question that decides the checkbox, and nothing else in the app
    // was answering it.
    const hit = existing && existing.id ? cache[existing.id] : null;
    const liveRow = el('label', 'check-row dash-live-row');
    const liveText = el('span', 'dash-live-text');
    liveText.append(el('span', 't', 'Run every time the dashboard opens'));
    liveText.append(el('span', 'd', 'Off by default: the last result is shown with its age, and '
      + '↻ Refresh re-runs it. Turn this on for a widget whose number has to be current the '
      + 'moment the board is opened — a watchlist or tag count, say.'
      + (hit ? ` This one took ${runtimeLabel(hit.elapsed_ms)} last run.` : '')));
    liveRow.append(live, liveText);
    // Outside .dash-form: its `label` rule is the uppercase field caption,
    // and this label is a checkbox row, not a caption.
    b.append(liveRow);
    const liveNow = widgets.filter((w) => w.live).length;
    const st = boardStamp();
    const cost = el('div', 'dash-cost');
    cost.append('This dashboard: ', el('b', null, `${widgets.length} widget${widgets.length === 1 ? '' : 's'}`),
      ' · ', el('b', null, String(liveNow)), ' run every time',
      st ? ` · oldest result ${clockOf(st.ran_at)} (${longAge(st.ran_at)})` : ' · nothing cached yet');
    b.append(cost);
    const syncSql = () => {
      sqlWrap.style.display = source.value === 'sql' ? '' : 'none';
      cellsNote.style.display = source.value === 'cells' ? '' : 'none';
    };
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
        const asked = questionOf(existing);
        delete existing.build;        // a stale recipe or drill must not outlive a hand edit
        delete existing.drill;
        delete existing.live;         // Object.assign can't clear a key draft() leaves out
        delete existing.cells;        // ditto: a card moved off "cells" keeps none of them
        Object.assign(existing, w);
        // The server drops a cached result whose question changed; do the
        // same here, or the repaint below shows the old answer under the
        // new SQL until something else reloads the board.
        if (asked !== questionOf(existing)) delete cache[existing.id];
      } else widgets.push(w);
      await persist();
      document.getElementById('modal').hidden = true;
      render();
    };
    acts.append(previewBtn, save);
    if (existing && existing.id) {
      // The per-card ↻. It lives here rather than as a third icon on the
      // card head, which already carries ⠿, ⤴ and ✎ — the editor is
      // where everything about one widget is, by the same rule that put
      // Remove here.
      const now = el('button', 'btn ghost dash-run-now', 'Run now');
      now.title = 'Re-run this widget against the case and keep the result';
      now.onclick = async () => {
        now.disabled = true;
        try {
          const r = await post(`/api/dashboards/${S.dashboardId}/refresh`, { widget_id: existing.id });
          const res = (r.results || {})[existing.id] || {};
          if (res.error) { toast(res.error, 6000); return; }
          // No ran_at means the server did not file it — a signals card
          // one of whose cells failed — so neither does this, the same
          // rule runWidget applies. Filing it would mark the card with an
          // age the case file does not have and the next open re-runs.
          if (res.ran_at) {
            cache[existing.id] = { payload: res.payload, ran_at: res.ran_at,
              elapsed_ms: res.elapsed_ms, stale: false };
          }
          paintWidget(existing, previewBody, res.payload);
          const cards = [...$('dashGrid').querySelectorAll('.dash-card:not(.dash-add)')];
          const onBoard = cards[widgets.indexOf(existing)];
          if (onBoard) {
            paintWidget(existing, onBoard.querySelector('.dash-widget-body'), res.payload);
            // An edit that gave the card rows takes it back out of the
            // strip, and one that took them away puts it in.
            noteEmpty(existing, onBoard, res.payload, null);
          }
          renderBar();
          paintAges();
        } catch (e) { toast('Could not run this widget: ' + e.message, 6000); }
        finally { now.disabled = false; }
      };
      acts.append(now);
    }
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
    toast(`Profile "${name.trim()}" saved — apply it from the profile manager (M) on a new case`, 7000);
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

// Dashboards live in the case; a case switch reloads them — and the
// cached results go with them, since they are that case's numbers. The
// generation bump is what stops a widget request still in the air from
// the old case landing in the new one's cache (see boardGen).
export function resetDashboard() {
  widgets = [];
  cache = {};
  boardGen++;
  S.dashboardId = null;
  S.dashboards = [];
}

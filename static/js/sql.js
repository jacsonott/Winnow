/* The SQL pane and its named sub-tabs.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { recordTabVisit } from './tabhistory.js';
import { $, api, debounce, el, post, toast } from './core.js';
import { hidePluginViews, sqlResultNodes } from './plugins.js';
import { setActiveSqlResult } from './sqlassist.js';
import { checkPresets } from './savedfilters.js';
import { syncTabSelection, wireDragReorder } from './sources.js';
import { S } from './state.js';
import { buildTimeline } from './timeline.js';
import { confirmDialog, promptDialog } from './ui.js';

/* ------------------------------------------------------------------ sql */

/* SQL and Timeline are both pinned tabs (S.activeTab), not popups —
   switching to/from either just swaps which of #grid / #sqlview /
   #timelineview occupies the main content area, the same way opening a
   different source tab swaps the visible grid. */
export function showSqlTab() {
  recordTabVisit({ kind: 'page', key: 'sql' });
  S.activeTab = 'sql';
  showMainView('sqlview');
  syncTabSelection();
  // The toolbar and the "matching saved filter" banner are about a
  // specific table's grid — meaningless here (see syncTabChrome).
  syncTabChrome();
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

export const SQL_AUTOSAVE_MS = 700;

export function starterSql() {
  // Merges (negative source_id) aren't a real src_N table — there's nothing
  // to `SELECT * FROM src_${S.sourceId}` for one. Prefill against its first
  // member instead of emitting invalid SQL like `src_-3`.
  if (!S.sourceId) return '';
  if (S.sourceId < 0) return `SELECT * FROM merge_${-S.sourceId} LIMIT 50;`;
  return `SELECT * FROM src_${S.sourceId} LIMIT 50;`;
}

export async function loadSqlTabs() {
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

export const activeSqlTab = () => S.sqlTabs.find((t) => t.id === S.sqlTabId) || null;

/* Loads the active tab's stored text + last result into the editor/result
   pane. The reverse direction (editor -> S.sqlTabs) is the textarea's own
   oninput below. */
export function applySqlTabToEditor() {
  const tab = activeSqlTab();
  $('sqlText').value = tab ? tab.sql : '';
  $('sqlText').disabled = !tab;
  const out = $('sqlResult');
  const cached = tab ? S.sqlResults.get(tab.id) : null;
  setActiveSqlResult(cached && !cached.error ? cached : null);
  if (!cached) out.replaceChildren();
  else if (cached.error) out.replaceChildren(el('div', 'sql-error', cached.error));
  else out.replaceChildren(...sqlResultNodes(cached));
}

export const scheduleSqlTabSave = debounce(() => { flushSqlTabSave(); }, SQL_AUTOSAVE_MS);

/* Persists the active tab's current editor text now. Awaited before any
   action that changes which tab the editor represents, so a pending
   debounced save can never land on the wrong tab (it captures the id it
   read the text for). */
export async function flushSqlTabSave() {
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

export function renderSqlTabs() {
  const strip = $('sqlTabs');
  strip.replaceChildren();
  for (const t of S.sqlTabs) {
    const tab = el('button', 'sql-tab');
    tab.setAttribute('aria-selected', String(t.id === S.sqlTabId));
    tab.append(el('span', 'sql-tab-name', t.name));
    tab.title = `${t.name} — right-click or double-click to rename`;
    tab.onclick = () => activateSqlTab(t.id);
    tab.ondblclick = (e) => { e.preventDefault(); renameSqlTab(t); };
    tab.oncontextmenu = (e) => { e.preventDefault(); renameSqlTab(t); };
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

export async function activateSqlTab(id) {
  if (id === S.sqlTabId) return;
  await flushSqlTabSave();
  S.sqlTabId = id;
  applySqlTabToEditor();
  renderSqlTabs();
  $('sqlText').focus();
}

export async function newSqlTab() {
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

export async function renameSqlTab(t) {
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

export async function closeSqlTab(t) {
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

/* The toolbar (group-by strip, tag filter ribbon, row stats, timeframe,
   filters, search) and the saved-filter banner all act on the *grid's*
   view spec, so they're meaningless on the SQL, Timeline and plugin tabs —
   those have their own controls (the Timeline its own tag filter and
   stats; a plugin whatever it built). Hidden rather than left inert:
   a row of controls that silently does nothing reads as broken, and the
   space belongs to the pane you actually switched to.

   Called by every show*Tab, so there's one place this rule lives rather
   than four copies drifting apart. Grid: showGridTab re-runs checkPresets
   afterward, which is what brings the banner back when it applies. */
export function syncTabChrome() {
  const isGrid = S.activeTab === 'grid';
  $('toolbar').hidden = !isGrid;
}

/* The mutually-exclusive main content views (grid / SQL / Timeline / and
   the analysis-suite tabs). Each new page tab adds its view id here and
   routes through showMainView, so no show-function has to know about the
   others — the trap that made adding a tab an N-place edit. */
export const MAIN_VIEWS = ['grid', 'sqlview', 'timelineview', 'notesview', 'watchlistview'];
export function hideMainViews() {
  for (const v of MAIN_VIEWS) { const e = $(v); if (e) e.hidden = true; }
}
export function showMainView(id) {
  hideMainViews();
  hidePluginViews();
  const e = $(id);
  if (e) e.hidden = false;
}

export function showGridTab() {
  S.activeTab = 'grid';
  showMainView('grid');
  syncTabSelection();
  syncTabChrome();
  if (S.sourceId) checkPresets(S.sourceId); // refresh the Filters button's suggestion state
}

export function showTimelineTab() {
  recordTabVisit({ kind: 'page', key: 'timeline' });
  S.activeTab = 'timeline';
  showMainView('timelineview');
  syncTabSelection();
  syncTabChrome(); // the Timeline has its own tag filter and stats
  buildTimeline(); // always fresh — tags can change in any table while this tab isn't the active one
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireSql() {
$('tabSql').onclick = showSqlTab;

$('tabTimeline').onclick = showTimelineTab;
}

/* The SQL pane and its named sub-tabs.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { recordTabVisit } from './tabhistory.js';
import { renderFilterBar } from './columns.js';
import { $, MOD_ENTER, api, debounce, el, post, toast } from './core.js';
import { hideDetailPane } from './detail.js';
import { render, renderTagToolbar } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { hidePluginViews, runSql, sqlResultNodes, syncPluginPanels } from './plugins.js';
import { syncHistogramPanel } from './histogram.js';
import { quoteIdent, setActiveSqlResult } from './sqlassist.js';
import { checkPresets } from './savedfilters.js';
import { syncDiffBanner } from './session.js';
import { paneTable, sourceLabel, syncTabSelection, wireDragReorder } from './sources.js';
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
  // Returned so a caller (winnow.showPage('sql')) can await the tabs.
  return loadSqlTabs().then(() => $('sqlText').focus());
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

/* A SQLite string literal, for the table names the starters below embed as
   data rather than as identifiers. A file called `O'Brien.csv` is not
   hypothetical on a real case. */
const sqlLiteral = (v) => `'${String(v).replace(/'/g, "''")}'`;

/* "SELECT * FROM src_1" with the first line saying which file that is.
   `src_1` is a name the analyst has never seen anywhere else — the tab
   strip, the sidebar and the dashboards all call this table Security.csv —
   so the query has to answer "which table is this?" before it answers
   anything else, and a comment is the one place to say it that survives
   being edited into a real query. */
function browseSql(s) {
  const table = paneTable(s);
  return `-- ${sourceLabel(s)} (${table})\nSELECT * FROM ${table} LIMIT 50;`;
}

export function starterSql() {
  if (!S.sourceId) return '';
  const s = S.sources.find((x) => x.id === S.sourceId);
  if (s) return browseSql(s);
  // The source list hasn't landed — no caller does this today, but emitting
  // a bare statement is better than naming the table wrongly. Merges
  // (negative source_id) aren't a real src_N table: there's nothing to
  // `SELECT * FROM src_${S.sourceId}` for one, and `src_-3` is a syntax
  // error rather than a wrong answer.
  const table = S.sourceId < 0 ? `merge_${-S.sourceId}` : `src_${S.sourceId}`;
  return `SELECT * FROM ${table} LIMIT 50;`;
}

/* Two or three queries worth clicking, built from the tables THIS case
   actually holds. Hardcoded examples were rejected outright: a starter
   naming a table the analyst doesn't have teaches nothing and errors on
   click, which is a worse first impression than the blank pane this
   replaces.

   Each one is offered only when it would return something — a "rows you
   tagged" starter on an untagged case is a scan that ends in "0 rows" —
   and each is written the way it is for a measured reason:
     - the tagged-rows query drives off `row_tags` via the `rid IN (...)`
       subquery rather than `WHERE Tags IS NOT NULL`, which cannot use an
       index and scanned 200k rows in 32ms to find five (the subquery form
       is 0.4ms on the same data, SEARCH by INTEGER PRIMARY KEY);
     - the per-table counts read the `src_N` views, not `main.src_N`,
       because SQLite flattens the view and never evaluates the Tags/Note
       correlated subselects for a COUNT (measured: 1.4ms over 200k rows,
       against 1.0ms for the raw table) — so the pane's own vocabulary
       costs nothing here;
     - a merge carries `source_id` and its rids are only unique per
       member, so its tagged-rows form is the row-value `(source_id, rid)
       IN (...)`. Invariant #9: the merge path ships with the feature. */
export function sqlStarters() {
  const usable = S.sources.filter((s) => !s.error);
  if (!usable.length) return [];
  const here = usable.find((s) => s.id === S.sourceId) || usable[0];
  const table = paneTable(here);
  const label = sourceLabel(here);
  // `name` is what the query becomes if it opens in a tab of its own:
  // short, because it has to fit the sub-tab strip beside the others.
  const out = [{ label: `First 50 rows of ${label}`, name: label, sql: browseSql(here) }];

  if (here.tagged_row_count > 0) {
    const key = here.is_merge || here.id < 0
      ? '(source_id, rid) IN (SELECT source_id, rid FROM row_tags)'
      : `rid IN (SELECT rid FROM row_tags WHERE source_id = ${here.id})`;
    out.push({
      label: `Rows you tagged in ${label}`,
      name: 'Tagged rows',
      sql: `-- Tagged rows in ${label} — the Tags column is the pane's, not the file's.\n`
        + `SELECT * FROM ${table} WHERE ${key} LIMIT 50;`,
    });
  }

  // Merges are left out of the inventory on purpose: every merge row is
  // also a member row, so counting both reports the case as larger than
  // it is (the same reason _sources_for_header_set skips them).
  const real = usable.filter((s) => !s.is_merge && s.id > 0);
  if (real.length > 1) {
    out.push({
      label: 'Rows per table',
      name: 'Rows per table',
      sql: '-- How many rows each table in this case holds.\n'
        + real.map((s, i) => `SELECT ${sqlLiteral(sourceLabel(s))}${i ? '' : ' AS "Table"'}, `
            + `COUNT(*)${i ? '' : ' AS "Rows"'} FROM ${paneTable(s)}`).join('\nUNION ALL ')
        + '\nORDER BY "Rows" DESC;',
    });
  }

  // Timestamps are the one column shape where a value count is never the
  // interesting question, so the pick skips them; whichever column it
  // lands on is named in the label, so the analyst can see it was a guess
  // and change it.
  const col = (here.columns || []).find((c) => c.type !== 'datetime');
  if (col) {
    out.push({
      label: `Most common ${col.name} in ${label}`,
      name: `Top ${col.name}`,
      sql: `-- Value counts for one column of ${label}.\n`
        + `SELECT ${quoteIdent(col.name)} AS "Value", COUNT(*) AS "Rows"\n`
        + `FROM ${table} GROUP BY 1 ORDER BY 2 DESC LIMIT 20;`,
    });
  }
  return out.slice(0, 3);
}

/* The results area before anything has run in this query tab. It used to
   be blank — about four fifths of the pane saying nothing at all, with no
   sign that Ctrl+Enter is what runs a query (the Run button says so, but
   only in a tooltip). */
export function sqlEmptyState() {
  const wrap = el('div', 'sql-empty');
  wrap.append(el('p', 'empty-title', 'Nothing run yet'));
  wrap.append(el('p', null,
    `${MOD_ENTER} runs the query. Results become a table you can sort, tag and export.`));
  const starters = sqlStarters();
  if (!starters.length) return wrap;
  const row = el('div', 'sql-starters');
  for (const st of starters) {
    const b = el('button', 'btn ghost sql-starter', st.label);
    b.title = st.sql; // the query itself, so a click is never a surprise
    b.onclick = () => openStarter(st);
    row.append(b);
  }
  wrap.append(row);
  return wrap;
}

/* Clicking a starter must not cost the analyst a draft. The editor holds
   one tab's text and autosaves it into the case file, so overwriting a
   half-written query is a real deletion — but demanding a new tab for the
   very first click, when the editor holds nothing but the seeded starter,
   would leave an abandoned "Query 1" behind in every case. So: fill this
   tab when its text is blank or is itself a starter, and open a named tab
   otherwise. */
export async function openStarter(st) {
  const cur = $('sqlText').value.trim();
  const disposable = !cur || sqlStarters().some((x) => x.sql.trim() === cur);
  if (!disposable) {
    await newSqlTab(st.name, st.sql);
  } else {
    $('sqlText').value = st.sql;
    // Through the editor's own input handler, so the tab record, the
    // autosave and the box's auto-grow all see it the way they would a
    // keystroke.
    $('sqlText').dispatchEvent(new Event('input', { bubbles: true }));
    await flushSqlTabSave();
  }
  await runSql();
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
  if (!cached) out.replaceChildren(sqlEmptyState());
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
  add.onclick = () => newSqlTab();
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

/* A new query tab: the "+" button's (unnamed, empty) and a plugin's
   (sqlPage.setText with newTab — named, filled). One path, so both land
   the same way: created, selected, in the editor. */
export async function newSqlTab(name = null, sql = '') {
  await flushSqlTabSave();
  // "Query N" by highest existing number, not by count — otherwise closing
  // "Query 2" of 3 makes the next new tab a duplicate "Query 3".
  const n = S.sqlTabs.reduce((max, t) => {
    const m = /^Query (\d+)$/.exec(t.name);
    return m ? Math.max(max, Number(m[1])) : max;
  }, 0) + 1;
  try {
    const rec = await post('/api/sql_tabs', { name: name || `Query ${n}`, sql });
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

   The row detail pane is the same kind of thing: it shows a grid row, and
   it sits beside .main-content rather than inside the grid, so the view
   swap the pages do never reaches it (docs/notes/ui.md).

   Called by every writer of S.activeTab — including showPluginTab, which
   swaps views itself rather than through showMainView — so there's one
   place this rule lives rather than copies drifting apart. Grid:
   showGridTab re-runs checkPresets afterward, which is what brings the
   banner back when it applies. */
export function syncTabChrome() {
  const isGrid = S.activeTab === 'grid';
  $('toolbar').hidden = !isGrid;
  // The detail pane reads a grid row, and no page shows the grid — but a
  // one-way hide only: `hidden = !isGrid` would force it open, possibly
  // empty, on every return to the grid.
  if (!isGrid) hideDetailPane();
  syncPluginPanels();   // plugin toolbar panels live and die with the toolbar
  syncHistogramPanel(); // as does the built-in histogram strip beside them
  syncDiffBanner();     // as does a session comparison's banner
  renderFilterBar();    // and the filter bar, which describes the grid's rows
  renderTagToolbar();   // and the "N selected" tagging bar
}

/* The mutually-exclusive main content views (grid / SQL / Timeline / and
   the analysis-suite tabs). Each new page tab adds its view id here and
   routes through showMainView, so no show-function has to know about the
   others — the trap that made adding a tab an N-place edit. */
export const MAIN_VIEWS = ['grid', 'sqlview', 'timelineview', 'notesview', 'watchlistview', 'dashboardview'];
export function hideMainViews() {
  for (const v of MAIN_VIEWS) { const e = $(v); if (e) e.hidden = true; }
}
export function showMainView(id) {
  hideMainViews();
  hidePluginViews();
  const e = $(id);
  if (e) e.hidden = false;
}

/* Whether the grid is actually on screen, rather than merely the tab the
   app would return to. S.activeTab stays 'grid' on the home screen, which
   hides #app wholesale (#home and #app are siblings, only one visible), so
   a background job that painted on "the grid tab is active" would measure
   a zero-height viewport there exactly as it would behind a page tab.
   One predicate, so the two answers can't drift apart. */
export function gridIsShowing() {
  return S.activeTab === 'grid' && !$('app').hidden;
}

/* Arriving at the grid, from a page tab or from a table that was opened
   while one was showing. One paint on the way in, at most, and only for a
   caller that wants this function to do the painting.

   Two things owe that paint. Tags can change while the grid isn't
   showing — the SQL pane's tag hotkey drops the row caches but can't
   paint a hidden grid — so the rows on screen are whatever was painted
   before leaving; and a background job (the watchlist scan's auto-tags)
   that dropped the caches with the grid hidden left `S.gridRepaintPending`
   behind rather than painting into nothing. The owed repaint carries the
   rest of what a tag write does: the rail, and a regroup when the
   grouping is BY TAG, since the rows the scan tagged changed bucket and
   the tree still holds its pre-tag counts.

   openSource and openCase come through here BEFORE swapping
   S.view/S.sourceId and paint for themselves next; with the caches just
   dropped, a render() here would fetch a page of the previous table's
   view (or, on a case switch, ask the new Store for the old case's view
   id and spin a spurious rebuild off the 409). Those two pass
   repaint:false — for the owed repaint as much as the ordinary one, which
   is why the flag is consumed either way: they satisfy it themselves
   (installView renders, draws the rail and regroups; openSource's
   cached-view path does the same), and honouring it here is exactly what
   repaint:false exists to prevent. render() is a no-op with no S.view at
   all. */
export function showGridTab({ repaint = true } = {}) {
  S.activeTab = 'grid';
  showMainView('grid');
  syncTabSelection();
  syncTabChrome();
  const owed = S.gridRepaintPending;
  S.gridRepaintPending = false;
  if (repaint) {
    render();
    if (owed && S.view) { drawRail(); regroupIfGroupedByTag(); }
  }
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

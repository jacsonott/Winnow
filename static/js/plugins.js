/* The plugin host's frontend: the Plugins settings panel and plugin tabs.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { recordTabVisit } from './tabhistory.js';
import { $, MOD_ENTER, OVERSCAN, api, el, post, setBusy, toast } from './core.js';
import { clearAllFilters, loadSources, openSource, pageTabs, renderPageTabs, renderSidebar, reopenPageTab, syncTabSelection } from './sources.js';
import { armOpCancel, closeNoticesOwnedBy, createNotice, opToken } from './jobs.js';
import { ensureNotesLoaded, insertAtCursor, showNotesTab } from './notes.js';
import { showWatchlistTab } from './watchlist.js';
import { setColumnFilter, valueFilterText } from './filters.js';
import { rebuildView } from './view.js';
import { activeSqlTab, flushSqlTabSave, hideMainViews, loadSqlTabs, newSqlTab, scheduleSqlTabSave, showGridTab, showSqlTab, showTimelineTab, syncTabChrome } from './sql.js';
import { releaseSqlResults, setActiveSqlResult, sqlCopyResult, sqlDownloadCsv, sqlDownloadXlsx, sqlRefQuery, sqlResultUrl, sqlRowKey, sqlSelectedRows, sqlSortQuery, sqlTagsFor, tagChips, wireSqlAssist } from './sqlassist.js';
import { moveCursor, rowsPaintY, spacerPx, vScroll } from './grid.js';
import { loadCaseVariables } from './savedfilters.js';
import { S } from './state.js';
import { alertDialog, closeModal, confirmDialog, dropdownMenu, modal, promptDialog } from './ui.js';
import { updateTimeRangeButton } from './timeframe.js';
import { histogramOpen } from './histogram.js';

/* Installing a plugin: copying a picked .py file or plugin folder from
   anywhere on disk into the plugins directory — the same consent model as
   copying it in by hand, minus the hand. The listing these routes answer
   with is applied by applyPluginListing, which every caller shares so a
   toggle and an install can't drift on what they repaint.

   The panel that used to live here is now two pieces: a summary line in
   Settings (buildPluginsPanel, in pluginmanager.js) and the manager it
   opens. */

/* Applies a fresh /api/plugins response to app state and repaints
   everything a plugin toggle or install can change. Exported because the
   plugins manager (pluginmanager.js) POSTs the toggle and install routes,
   both of which answer with this same listing shape. */
export function applyPluginListing(r) {
  S.plugins = r.plugins || [];
  S.pluginFormats = r.formats || [];
  S.pluginTabs = r.tabs || [];
  S.pluginDirs = r.dirs || [];
  S.pluginApiVersion = r.api_version ?? null;
  // A toggle takes a plugin's row actions with it — and a pinned one
  // out of the row menu, which promises to hide it while its plugin is off.
  S.pluginRowActions = r.row_actions || [];
  S.pluginDashboards = r.dashboards || [];
  S.pluginPanels = r.panels || [];
  S.pluginPagePanels = r.page_panels || [];
  renderPluginTabs(); // a toggle/install can add or remove pinned tabs
  renderPluginPanelButtons(); // …and toolbar / page panels
  // …and can add or remove offered boards. Without this the Dashboards
  // section keeps whatever it last drew: a board that is not offered
  // yet, or a phantom row whose ＋ 404s because its plugin is off.
  renderSidebar();
}

async function installFiles(fileList, relPaths, onDone) {
  const files = [...fileList];
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('paths', JSON.stringify(relPaths));
  let r;
  try {
    r = await api('/api/plugins/install', { method: 'POST', body: fd });
  } catch (e) {
    if (e.status !== 409) { toast('Install failed: ' + e.message, 6000); return; }
    // Name taken — the server won't clobber without being told to.
    if (!(await confirmDialog(`${e.message}. Replace it?`, { danger: true, okLabel: 'Replace' }))) return;
    fd.append('overwrite', 'true');
    try {
      r = await api('/api/plugins/install', { method: 'POST', body: fd });
    } catch (e2) { toast('Install failed: ' + e2.message, 6000); return; }
  }
  applyPluginListing(r);
  if (onDone) onDone(r.installed);
  if (r.error) toast(`Installed ${r.installed}, but it failed to load: ${r.error}`, 8000);
  else toast(`Installed ${r.installed}`);
}

/* "File or folder?" was the question the two old side-by-side buttons
   made the analyst answer blind — the browser can't offer one picker
   that takes either, so the dialog states the rule the pickers can't:
   which one you need is decided by how the plugin arrived on disk. If a
   folder pick contains no __init__.py, or a file pick isn't a .py, that's
   said here rather than left to a failed install.

   `onDone(installedName)` fires after a successful install, so whatever
   opened this dialog can repaint. It replaces the modal it was opened
   from, so the caller re-opens itself rather than being restored. */
export function openInstallDialog(onDone) {
  modal('Install a plugin', (b) => {
    b.append(el('p', null,
      'A plugin is either a single Python file or a folder — which one is decided by '
      + 'how it arrived, not by preference:'));
    const kv = el('div', 'kv');
    kv.append(el('kbd', null, 'One .py file'), el('span', null, 'Pick the file itself.'));
    kv.append(el('kbd', null, 'A folder'), el('span', null,
      'Pick the folder that directly contains __init__.py (plus any ui/, README, data it ships). '
      + 'Everything inside is copied.'));
    b.append(kv);
    b.append(el('p', 'note-status',
      'Either way it lands in the first plugins directory listed in the manager, enabled '
      + 'immediately. The bundled examples are already listed — no install needed, just switch '
      + 'them on.'));

    const acts = el('div', 'row-actions');
    const fileLabel = el('label', 'btn', 'Pick a .py file…');
    const fileInput = el('input');
    fileInput.type = 'file';
    fileInput.accept = '.py';
    fileInput.hidden = true;
    fileInput.onchange = () => {
      const files = [...fileInput.files];
      fileInput.value = '';
      if (!files.length) return;
      if (!files[0].name.endsWith('.py')) { toast('That isn\'t a .py file — for a folder plugin, use the folder button', 5000); return; }
      $('modal').hidden = true;
      installFiles(files, files.map((f) => f.name), onDone);
    };
    fileLabel.append(fileInput);
    const folderLabel = el('label', 'btn', 'Pick a plugin folder…');
    const folderInput = el('input');
    // Folder picker: every file inside arrives with its path relative to
    // the picked folder (webkitRelativePath), which is exactly what the
    // install route's `paths` field wants.
    folderInput.webkitdirectory = true;
    folderInput.hidden = true;
    folderInput.onchange = () => {
      const files = [...folderInput.files];
      folderInput.value = '';
      if (!files.length) return;
      // The rule stated above, enforced before any bytes move: a plugin
      // folder is one whose top level has __init__.py.
      const hasInit = files.some((f) => {
        const rel = f.webkitRelativePath || f.name;
        const parts = rel.split('/');
        return parts.length === 2 && parts[1] === '__init__.py';
      });
      if (!hasInit) {
        toast('That folder has no __init__.py at its top level — pick the plugin folder itself, not its parent or a subfolder', 6500);
        return;
      }
      $('modal').hidden = true;
      installFiles(files, files.map((f) => f.webkitRelativePath || f.name), onDone);
    };
    folderLabel.append(folderInput);
    acts.append(fileLabel, folderLabel);
    b.append(acts);
  });
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
   they're left out. (The pane itself CAN query a merge — _pane_connection
   makes a merge_<id> TEMP VIEW for each — so this is a shape the schema
   dump has not caught up with, not a limit of the pane.) */
export function sqlSchemaForLLM() {
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

/* ---------------------------------------------------------- plugin tabs */

/* Plugin-registered pinned tabs (plugin_api.register_tab) — SQL/Timeline
   siblings whose content is a plugin-shipped ES module, dynamically
   import()ed from /plugin_assets/ on first activation and handed an empty
   <section class="pluginview"> plus the stable context object from
   buildPluginTabContext. One mount per tab, kept alive across tab
   switches (a half-built graph shouldn't vanish because the analyst
   glanced at the grid); optional onShow/onHide exports fire on every
   switch. The mount is torn down and rebuilt when its plugin's `gen`
   changes (every registry reload bumps it — a toggle-off/on picks up
   changed JS) and on a case switch (a view built from one case's data
   has no business surviving into another). */

export const pluginTabMounts = new Map();

 // tab id -> {container, module, gen}

export const pluginTabById = (id) => S.pluginTabs.find((t) => t.id === id) || null;

export function hidePluginViews() {
  for (const m of pluginTabMounts.values()) {
    if (!m.container.hidden) {
      m.container.hidden = true;
      if (m.module && m.module.onHide) { try { m.module.onHide(m.container); } catch (e) { console.error(e); } }
    }
  }
}

export function resetPluginTabMounts() {
  // Anything a mount was about to save describes the case being left, and
  // the server has already swapped stores by the time this runs — see
  // dropPendingTabState.
  dropPendingTabState();
  for (const [id, m] of pluginTabMounts) { disposePluginMount(mountKey('tab', id)); m.container.remove(); }
  pluginTabMounts.clear();
}

/* Called whenever the plugin listing changes (boot, Settings
   toggles/installs). The strip itself is renderPageTabs' job — plugin tabs
   are ordered among SQL/Timeline, not pinned after them, so there's one
   renderer for all three rather than one that inserts around another's
   output. What's left here is the mount bookkeeping: drop a mount whose
   plugin reloaded or vanished, and if the *active* plugin tab is the one
   that vanished — its plugin was toggled off — fall back to the grid
   rather than leaving a headless view up. */
export function renderPluginTabs() {
  renderPageTabs();
  for (const [id, m] of [...pluginTabMounts]) {
    const t = pluginTabById(id);
    if (!t || t.gen !== m.gen) { disposePluginMount(mountKey('tab', id)); m.container.remove(); pluginTabMounts.delete(id); }
  }
  if (S.activeTab.startsWith('plugin:') && !pluginTabById(S.activeTab.slice(7))) showGridTab();
}

/* The stable surface a plugin tab's module gets. Versioned via apiVersion
   the same way PLUGIN_API_VERSION covers the Python side: additions are
   free, changing what's already here isn't. `sql` (read-only, own
   connection server-side — see run_sql) is the blessed way for a tab to
   query the case; `schemaText` is the same LLM-ready schema dump the SQL
   pane's copy button builds. */
/* Document-level listeners a mounted plugin registered, per mount id.

   The context hands out onViewChange/onAppearanceChange with an
   unsubscribe, but a mount is torn down by removing its container — the
   plugin never gets a chance to call it, and there is no onDestroy in the
   contract. Opening a case reloads every plugin, which bumps its gen and
   drops every mount, so the listeners accumulated one set per case switch:
   each still firing on every grid rebuild, painting into detached DOM and
   re-issuing the panel's fetch. Tracked here and cut in disposePluginMount,
   which every teardown path calls.

   Keyed by MOUNT, not by registered id: a tab and a panel share the
   `<plugin>.<id>` namespace, so a plugin that names both "copilot" would
   otherwise have one teardown cut the other's listeners. */
const mountListeners = new Map();   // mount key -> [unsubscribe]

export const mountKey = (kind, id) => `${kind}:${id}`;

function trackMountListener(key, off) {
  if (key == null) return off;
  const list = mountListeners.get(key) || [];
  list.push(off);
  mountListeners.set(key, list);
  return off;
}

export function disposePluginMount(key) {
  for (const off of mountListeners.get(key) || []) {
    try { off(); } catch { /* a plugin's own teardown must not block ours */ }
  }
  mountListeners.delete(key);
  closeNoticesOwnedBy(key);   // its rows' click handlers point at code that's gone
}

/* ------------------------------------------------- winnow.tabState */

/* A mount's own scrap of the case file, written as the analyst works.

   A plugin tab is built from nothing every time its plugin's gen changes
   — a case switch, a Settings toggle, a profile apply, F5 — and there is
   no onDestroy in the contract (see mountListeners above), so there is no
   moment at which a plugin could flush what somebody spent ten minutes
   building. Eager is therefore the only shape that works: set() debounces,
   the host owns the timer, and the row lands under this mount's key.

   Three rules, all load-bearing, all said again in docs/writing-plugins.md:
   - SPEC ONLY. Save which fields sit in which well, never the rows they
     produced — rows belong to the case and are re-run on restore. The cap
     is what keeps that from being a suggestion.
   - VALIDATE ON RESTORE. A source id is reused after a drop and a column
     can be gone; a payload records what somebody wanted, not what the case
     still holds.
   - LAST WRITE WINS. Two Winnows on one case do not merge.

   Nothing here rejects. An unhandled rejection from a background save
   would surface as a page error in whatever the analyst was doing at the
   time, which is both useless and alarming; failures are logged, said out
   loud once per mount, and reported through the resolved value — which is
   why get() resolves {error: true} rather than null when the read fails.
   Those two are the same picture to a mount that cannot tell them apart,
   and the wrong one of them ends with a save over unread state. */
const TAB_STATE_DEBOUNCE_MS = 500;
// store.PLUGIN_UI_STATE_MAX_BYTES. Counted in characters here, which only
// ever under-counts UTF-8 — the server holds the real line and answers 400.
const TAB_STATE_MAX_CHARS = 64 * 1024;
const pendingTabState = new Map();      // mount key -> {payload, timer, waiters}
const tabStateComplained = new Set();   // mounts whose failure has been said once

function flushTabState(key) {
  const p = pendingTabState.get(key);
  if (!p) return;
  clearTimeout(p.timer);
  pendingTabState.delete(key);
  post('/api/plugin_state', { key, payload: p.payload }).then(
    () => { for (const w of p.waiters) w(true); },
    (e) => {
      console.error(`winnow.tabState: ${key} was not saved`, e);
      if (!tabStateComplained.has(key)) {
        tabStateComplained.add(key);
        toast('A plugin tab could not save its state — see the console', 5000);
      }
      for (const w of p.waiters) w(false);
    });
}

/* Everything still inside its debounce, dropped rather than written.

   openCase POSTs /api/case/open FIRST and the server swaps stores there,
   so a save that fired a moment later would land the previous case's
   grouping in the file that just opened — the same trap the note binding
   at the top of openCase is blanked for. Dropping costs at most half a
   second of edits in the case being left; writing would put them in the
   wrong case file. */
export function dropPendingTabState() {
  for (const p of pendingTabState.values()) {
    clearTimeout(p.timer);
    for (const w of p.waiters) w(false);
  }
  pendingTabState.clear();
}

function tabStateApi(key) {
  return {
    // null = nothing saved here; {payload, savedAt} = what was saved;
    // {error: true} = the read FAILED, which is a different thing and has
    // to stay different: a mount that reads a failure as "nothing saved"
    // starts empty and writes that emptiness over a row it never saw.
    get: () => api(`/api/plugin_state?key=${encodeURIComponent(key)}`).then(
      (r) => (r && r.payload != null ? { payload: r.payload, savedAt: r.saved_at } : null),
      (e) => {
        console.error(`winnow.tabState: ${key} could not be read`, e);
        return { error: true };
      }),
    set: (value) => {
      let json;
      try {
        json = JSON.stringify(value === undefined ? null : value);
      } catch (e) {
        console.error(`winnow.tabState: ${key} is not JSON`, e);
        return Promise.resolve(false);
      }
      if (json.length > TAB_STATE_MAX_CHARS) {
        console.error(`winnow.tabState: ${key} is ${json.length} characters, over the `
          + `${TAB_STATE_MAX_CHARS} cap — save the definition, not the rows`);
        return Promise.resolve(false);
      }
      const p = pendingTabState.get(key) || { waiters: [], timer: null };
      // A snapshot, not the plugin's object: it goes on being edited while
      // this sits in the debounce, and what is saved must be what was set.
      p.payload = JSON.parse(json);
      clearTimeout(p.timer);
      p.timer = setTimeout(() => flushTabState(key), TAB_STATE_DEBOUNCE_MS);
      pendingTabState.set(key, p);
      return new Promise((resolve) => p.waiters.push(resolve));
    },
    clear: () => {
      const p = pendingTabState.get(key);
      if (p) {
        clearTimeout(p.timer);
        pendingTabState.delete(key);
        for (const w of p.waiters) w(false);
      }
      return post('/api/plugin_state', { key, payload: null }).then(
        () => true,
        (e) => { console.error(`winnow.tabState: ${key} was not cleared`, e); return false; });
    },
  };
}

/* winnow.showTab: back to one of the plugin's OWN pages — from a
   notification's button, typically. Reopens the page tab if the analyst
   had closed it from the strip, the way the sidebar's Pages rows do;
   showPluginTab alone would show a view with no tab to stand on. */
async function showOwnTab(owner, localId) {
  const mine = S.pluginTabs.filter((t) => t.plugin_fs === owner.plugin_fs);
  let target;
  if (localId != null) {
    target = mine.find((t) => t.id === `${owner.plugin}.${localId}`);
    if (!target) throw new Error(`${owner.plugin} registers no tab "${localId}"`);
  } else if (mine.length === 1) {
    target = mine[0];
  } else {
    throw new Error(mine.length ? `${owner.plugin} registers ${mine.length} tabs — showTab(id) needs the id` : `${owner.plugin} registers no tab`);
  }
  reopenPageTab('plugin:' + target.id);
  await showPluginTab(target.id);
}

/* winnow.showPage: the built-in pages by name. `pageTabs()` is the app's
   own registry of them (plugin tabs and pinned boards ride in it too, so
   those keys work as well, though the documented names are the five). */
export async function showPage(name) {
  if (name === 'grid') { showGridTab(); return; }
  const t = pageTabs().find((p) => p.key === name);
  if (!t) throw new Error(`unknown page "${name}"`);
  reopenPageTab(t.key);
  await t.show();
}

export function buildPluginTabContext(tab, kind = 'tab') {
  return {
    apiVersion: 7,
    plugin: tab.plugin,
    base: `/api/plugin/${tab.plugin_fs}`,      // the plugin's own register_api routes
    assets: `/plugin_assets/${tab.plugin_fs}`, // the plugin's own files (css, workers, data)
    api, post, toast, el, modal, confirmDialog, promptDialog, alertDialog, closeModal,
    // The app's own small menu, anchored under an element — the one behind
    // Filters ▾ and Case ▾, so a plugin's menu IS one of the app's: one open
    // at a time, Escape and an outside click close it, the arrows walk it.
    // A choice of three or four things is a menu, not a modal over the
    // board the analyst is arranging. (API 13 / apiVersion 7.)
    dropdownMenu,
    // A row in the bottom-right jobs panel — the card an import gets —
    // driven through the returned handle (update/done/fail/close). Dies
    // with this mount; see jobs.js createNotice for the lifecycle.
    notify: (opts) => createNotice(mountKey(kind, tab.id), opts),
    showTab: (localId) => showOwnTab(tab, localId),
    showPage,
    // The built-in pages, driven from a plugin — a page panel's reason to
    // exist, but any tab can use them. Everything that may need a lazy
    // load (the SQL sub-tabs, the notes body) is async.
    sqlPage: sqlPageApi(mountKey(kind, tab.id)),
    notesPage: notesPageApi(mountKey(kind, tab.id)),
    // This mount's own state in the case file — get/set/clear, scoped by
    // mount key so a plugin's tab and its panel cannot overwrite each
    // other. Spec only, and validated against the case on the way back in;
    // see the tabStateApi comment and docs/writing-plugins.md.
    tabState: tabStateApi(mountKey(kind, tab.id)),
    sql: (sql, limit = 5000) => post('/api/sql', { sql, limit }),
    schemaText: sqlSchemaForLLM,
    openSource,
    // For plugins that CREATE sources (via their own routes + ingest_rows):
    // a synchronous server-side ingest announces itself through no job, so
    // nothing else refreshes the app's source list — the tab must, or its
    // freshly created table is invisible until a reload. Await this, then
    // openSource(new_id).
    refreshSources: () => loadSources(),
    state: {
      get sources() { return S.sources; },
      get sourceId() { return S.sourceId; },
      get tags() { return S.tags; },
      // The case timeframe filter, verbatim — {enabled, column, start, end}.
      // A plugin honouring it is what makes "the timeframe applies
      // everywhere" true for plugin tabs too.
      get timeRange() { return S.timeRange; },
      // The grid's current view — what the table is showing right now,
      // filters/search/timeframe applied — or null before a table is open.
      // Hand view_id to a plugin route that reads THROUGH the view (the
      // shape Store.time_histogram gives the built-in histogram strip,
      // static/js/histogram.js) to describe exactly the rows on screen.
      get view() { return S.view ? { view_id: S.view.view_id, row_count: S.view.row_count } : null; },
      // The case's variables as {name: value} — engagement name, API base
      // URL, a document link. Case data, never secrets.
      get variables() { return Object.fromEntries((S.caseVariables || []).map((v) => [v.name, v.value])); },
    },
    // Write a case variable (upsert) and refresh the local copy.
    setVariable: async (name, value) => {
      await post('/api/case/variables', { name, value: String(value ?? '') });
      await loadCaseVariables();
    },
    // Fires after every grid rebuild — filter, sort, search, timeframe,
    // table switch — with {sourceId, viewId, rowCount}. Returns an
    // unsubscribe. This is what lets a panel follow the grid.
    onViewChange: (cb) => {
      const h = (e) => cb(e.detail);
      document.addEventListener('winnow:viewchange', h);
      return trackMountListener(mountKey(kind, tab.id), () => document.removeEventListener('winnow:viewchange', h));
    },
    // Fires after every skin / theme / accent change with {style, themeMode,
    // accent}. Canvases don't inherit CSS — a panel that painted with the
    // accent redraws here, or it keeps the old colour until the next view
    // change. Returns an unsubscribe.
    onAppearanceChange: (cb) => {
      const h = (e) => cb(e.detail);
      document.addEventListener('winnow:appearance', h);
      return trackMountListener(mountKey(kind, tab.id), () => document.removeEventListener('winnow:appearance', h));
    },
    // Drive the case timeframe filter (the toolbar's ⏱) from a plugin —
    // the same object the Timeframe dialog writes, so the button, the
    // toggle key and every other consumer see it as if typed there.
    setTimeRange: ({ column = null, start = '', end = '', enabled = true } = {}) => {
      S.timeRange = { enabled: !!enabled, column: column || null, start: start || '', end: end || '' };
      updateTimeRangeButton();
      if (S.sourceId) rebuildView({ keepScroll: false });
    },
    clearTimeRange: () => {
      S.timeRange = { enabled: false, column: null, start: '', end: '' };
      updateTimeRangeButton();
      if (S.sourceId) rebuildView({ keepScroll: false });
    },
    // Jump from a plugin's visualization to the EVIDENCE: open the source
    // and exact-filter it to the given column values (clearing whatever
    // filters were there — this is a navigation, not a refinement).
    openFiltered: async (sourceId, pairs) => {
      await openSource(sourceId);
      await clearAllFilters();
      let applied = 0;
      for (const { column, value } of pairs || []) {
        const raw = valueFilterText(value);
        if (raw !== null) { setColumnFilter(column, raw); applied++; }
      }
      if (applied) await rebuildView({ keepScroll: false });
    },
  };
}

export async function showPluginTab(tabId) {
  recordTabVisit({ kind: 'page', key: tabId });
  const tab = pluginTabById(tabId);
  if (!tab) return;
  S.activeTab = 'plugin:' + tabId;
  hideMainViews();
  hidePluginViews();
  syncTabSelection();
  syncTabChrome();

  let m = pluginTabMounts.get(tabId);
  if (m && m.gen !== tab.gen) { disposePluginMount(mountKey('tab', tabId)); m.container.remove(); pluginTabMounts.delete(tabId); m = null; }
  if (m) {
    m.container.hidden = false;
  } else {
    const container = el('section', 'pluginview');
    $('grid').parentElement.append(container);
    m = { container, module: null, gen: tab.gen };
    pluginTabMounts.set(tabId, m);
    try {
      // ?v=gen: a reloaded plugin gets a fresh module even though import()
      // caches by URL — see the gen note in plugin_api.PluginRegistry.
      const ctx = buildPluginTabContext(tab, 'tab');
      const mod = await import(`${ctx.assets}/${tab.entry}?v=${tab.gen}`);
      if (typeof mod.default !== 'function') throw new Error('tab module has no default export to mount');
      await mod.default(container, ctx);
      m.module = mod;
    } catch (e) {
      console.error(e);
      container.replaceChildren(el('p', 'note-status', `Plugin tab "${tab.label}" failed to load: ${e.message}`));
      return;
    }
  }
  if (m.module && m.module.onShow) { try { m.module.onShow(m.container); } catch (e) { console.error(e); } }
}

/* ------------------------------------------------- the built-in pages */

/* winnow.sqlPage: the SQL pane as a plugin sees it. The sub-tabs load
   lazily with the page, so every method that touches the editor loads
   them first — otherwise the page's own first visit would repaint the
   editor from the server copy over whatever the plugin wrote. setText
   persists at once (the tab-switch flush) for the same reason: showSqlTab
   re-fetches the tabs. */
function sqlPageApi(key) {
  const ready = async () => { if (!(S.sqlTabs || []).length) await loadSqlTabs(); };
  return {
    show: () => showSqlTab(),
    text: async () => { await ready(); return $('sqlText').value; },
    setText: async (sql, { newTab } = {}) => {
      await ready();
      const text = sql == null ? '' : String(sql);
      if (newTab) {
        await newSqlTab(String(newTab), text);   // the "+" tab's own path; showSqlTab keeps the selection
      } else {
        const tab = activeSqlTab();
        $('sqlText').value = text;
        if (tab) tab.sql = text;
        await flushSqlTabSave();
      }
      await showSqlTab();
    },
    run: async () => {
      await ready();
      const r = await runSql();
      if (r && r.error) throw new Error(r.error);
      return publicSqlResult(r);
    },
    result: () => { const r = S.sqlResults.get(S.sqlTabId); return r && !r.error ? publicSqlResult(r) : null; },
    selectedRows: () => sqlSelectedRows(),
    onRun: (cb) => {
      const h = (e) => cb(e.detail);
      document.addEventListener('winnow:sqlrun', h);
      return trackMountListener(key, () => document.removeEventListener('winnow:sqlrun', h));
    },
  };
}

/* The result shape a plugin gets: the run's columns and first page of
   rows, how many rows the result has in all (the server holds the rest —
   the pane pages them in), and whether a Stop or a full disk cut it
   short. Not the pane's own tag decoration (a Map and a selection Set),
   nor the id of the held result, which is the pane's to release. */
function publicSqlResult(r) {
  if (!r) return null;
  if (r.error) return { error: r.error };
  return {
    columns: r.columns, rows: r.rows, total: r.total, elapsed_ms: r.elapsed_ms,
    truncated: r.total > r.rows.length, stopped: r.stopped || null,
  };
}

/* winnow.notesPage: the case notes. The body loads lazily with the page;
   ensureNotesLoaded first, always — an insert into an editor that was
   never seeded would autosave plugin text over the case's narrative. */
function notesPageApi(key) {
  return {
    show: () => showNotesTab(),
    text: async () => { await ensureNotesLoaded(); return $('notesEditor').value; },
    setText: async (md) => {
      await ensureNotesLoaded();
      const ed = $('notesEditor');
      ed.value = md == null ? '' : String(md);
      ed.dispatchEvent(new Event('input'));   // autosave sees it like typing
    },
    insert: async (text) => { await ensureNotesLoaded(); insertAtCursor(text == null ? '' : String(text)); },
    onChange: (cb) => {
      const ed = $('notesEditor');
      const h = () => cb({ text: ed.value });
      ed.addEventListener('input', h);
      return trackMountListener(key, () => ed.removeEventListener('input', h));
    },
  };
}

/* ------------------------------------------------ toolbar and page panels */

/* Plugin panels: a toggle button per panel in some toolbar, and — while
   toggled on and that toolbar's page is showing — the panel's own UI in
   a host next to it. Three hosts, one code path: the grid's
   (register_toolbar_panel — a strip between the toolbar and the grid)
   and the SQL and Notes pages' (register_page_panel — a side column).
   Mounted once per plugin gen like a tab; hidden/shown after that, with
   onShow/onHide. The toggle persists per browser, keyed by the
   namespaced panel id, so an analyst who keeps a panel open gets it
   back on the next case. The grid host is shared with the built-in
   histogram strip (histogram.js, its own pref and section): this module
   still owns the host's `hidden`, and asks histogramOpen() so the strip
   shows for either kind. */
const PANEL_PREFS_KEY = 'winnow.panels';
const PAGE_PANEL_W_KEY = 'winnow.pagepanels.width';
const PAGE_PANEL_MIN_W = 260;
const pluginPanelMounts = new Map();   // panel id -> {container, module, gen, host}

const PANEL_HOSTS = {
  grid: {
    kind: 'panel', buttons: 'pluginToolbarButtons', host: 'pluginPanels',
    list: () => S.pluginPanels || [],
    visible: () => S.activeTab === 'grid',
  },
  sql: {
    kind: 'page', buttons: 'sqlPluginButtons', host: 'sqlPluginPanels', resize: 'sqlPanelResize',
    list: () => (S.pluginPagePanels || []).filter((p) => p.page === 'sql'),
    visible: () => S.activeTab === 'sql',
  },
  notes: {
    kind: 'page', buttons: 'notesPluginButtons', host: 'notesPluginPanels', resize: 'notesPanelResize',
    list: () => (S.pluginPagePanels || []).filter((p) => p.page === 'notes'),
    visible: () => S.activeTab === 'notes',
  },
};

function panelHostFor(id) {
  for (const host of Object.values(PANEL_HOSTS)) {
    const p = host.list().find((x) => x.id === id);
    if (p) return { host, panel: p };
  }
  return null;
}

function panelPrefs() {
  try { return JSON.parse(localStorage.getItem(PANEL_PREFS_KEY) || '{}'); } catch { return {}; }
}
export function pluginPanelOpen(id) { return !!panelPrefs()[id]; }
function setPanelPref(id, on) {
  const p = panelPrefs();
  if (on) p[id] = true; else delete p[id];
  localStorage.setItem(PANEL_PREFS_KEY, JSON.stringify(p));
}

export function renderPluginPanelButtons() {
  // A panel whose plugin was disabled or reloaded loses its mount.
  for (const [id, m] of [...pluginPanelMounts]) {
    const found = panelHostFor(id);
    if (!found || found.panel.gen !== m.gen) {
      disposePluginMount(mountKey(m.host.kind, id));
      m.container.remove();
      pluginPanelMounts.delete(id);
    }
  }
  for (const host of Object.values(PANEL_HOSTS)) {
    const buttons = $(host.buttons);
    if (!buttons) continue;
    buttons.replaceChildren();
    for (const p of host.list()) {
      const b = el('button', 'btn ghost plugin-panel-btn', p.label);
      b.dataset.panelId = p.id;
      b.title = (p.description || `${p.label} — from the ${p.plugin} plugin`) + ' (click to toggle)';
      b.setAttribute('aria-pressed', String(pluginPanelOpen(p.id)));
      b.onclick = () => togglePluginPanel(p.id);
      buttons.append(b);
      if (pluginPanelOpen(p.id)) mountPluginPanel(p.id);
    }
  }
  syncPluginPanels();
}

export async function togglePluginPanel(id, on = !pluginPanelOpen(id)) {
  setPanelPref(id, on);
  const btn = document.querySelector(`.plugin-panel-btn[data-panel-id="${CSS.escape(id)}"]`);
  if (btn) btn.setAttribute('aria-pressed', String(on));
  if (on) await mountPluginPanel(id);
  syncPluginPanels();
}

async function mountPluginPanel(id) {
  const found = panelHostFor(id);
  if (!found) return;
  const { host, panel } = found;
  let m = pluginPanelMounts.get(id);
  if (m) return;
  const container = el('section', 'plugin-panel');
  container.dataset.panelId = id;
  $(host.host).append(container);
  m = { container, module: null, gen: panel.gen, host };
  pluginPanelMounts.set(id, m);
  try {
    const ctx = buildPluginTabContext(panel, host.kind);
    const mod = await import(`${ctx.assets}/${panel.entry}?v=${panel.gen}`);
    if (typeof mod.default !== 'function') throw new Error('panel module has no default export to mount');
    await mod.default(container, ctx);
    m.module = mod;
  } catch (e) {
    console.error(e);
    container.replaceChildren(el('p', 'note-status', `Plugin panel "${panel.label}" failed to load: ${e.message}`));
  }
  syncPluginPanels();
}

/* Panels show only with their page (the grid's with the toolbar, which
   hides on page tabs; a page's with that page) and only while toggled
   on; a host collapses to nothing when no panel is visible, so the page
   gets the space back. Called from syncTabChrome on every switch. */
export function syncPluginPanels() {
  for (const host of Object.values(PANEL_HOSTS)) {
    const hostEl = $(host.host);
    if (!hostEl) continue;
    const isPage = host.visible();
    // The built-in histogram counts as an open panel in the grid host —
    // without this a plugin toggled off would hide an open histogram.
    let any = host === PANEL_HOSTS.grid && isPage && histogramOpen();
    for (const [id, m] of pluginPanelMounts) {
      if (m.host !== host) continue;
      const show = isPage && pluginPanelOpen(id);
      const was = !m.container.hidden;
      m.container.hidden = !show;
      if (show) any = true;
      if (m.module) {
        try {
          if (show && !was && m.module.onShow) m.module.onShow(m.container);
          if (!show && was && m.module.onHide) m.module.onHide(m.container);
        } catch (e) { console.error(e); }
      }
    }
    hostEl.hidden = !any;
    if (host.resize) { const r = $(host.resize); if (r) r.hidden = !any; }
  }
}

/* The side column's drag handle. One width for both pages — it is the
   same "how much room does the assistant get" preference — remembered
   per browser like the panel toggles. */
function wirePagePanelResize() {
  const stored = Number(localStorage.getItem(PAGE_PANEL_W_KEY));
  for (const host of Object.values(PANEL_HOSTS)) {
    if (!host.resize) continue;
    const handle = $(host.resize);
    const aside = $(host.host);
    if (!handle || !aside) continue;
    if (stored >= PAGE_PANEL_MIN_W) aside.style.width = `${stored}px`;
    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add('dragging');
      const startX = e.clientX;
      const startW = aside.getBoundingClientRect().width;
      const max = Math.max(PAGE_PANEL_MIN_W, aside.parentElement.getBoundingClientRect().width * 0.7);
      const move = (ev) => {
        const w = Math.min(max, Math.max(PAGE_PANEL_MIN_W, startW + (startX - ev.clientX)));
        aside.style.width = `${Math.round(w)}px`;
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.classList.remove('dragging');
        const w = Math.round(aside.getBoundingClientRect().width);
        try { localStorage.setItem(PAGE_PANEL_W_KEY, String(w)); } catch { /* private mode */ }
        // Both pages share the width: apply it to the other column too.
        for (const other of Object.values(PANEL_HOSTS)) {
          const o = other.resize && $(other.host);
          if (o && o !== aside) o.style.width = `${w}px`;
        }
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  }
}

/* Split out of runSql so applySqlTabToEditor can re-paint a cached result
   when you switch back to a tab, without re-running its query. */
/* Which result column is the pane view's own Tags column, if any — the
   GROUP_CONCAT of tag names Store._annotation_cols appends to every
   src_N/merge_N view. When the result carries one AND the rid join
   resolved, the chips go INTO that column instead of a second "Tags"
   header being appended beside it: the two said the same thing twice.
   The view names it "Winnow Tags" when the file has its own Tags column,
   so that name is unambiguous; a bare "Tags" is only the view's if the
   single source behind the result has no column of that name — a file's
   own Tags text must keep showing as text. -1 when there is no such
   column, and the chips get their own column as before. */
function annotationTagsIdx(r) {
  const lower = r.columns.map((c) => String(c).toLowerCase());
  const wt = lower.indexOf('winnow tags');
  if (wt !== -1) return wt;
  const t = lower.indexOf('tags');
  if (t === -1) return -1;
  // Every source the rows came from: the one src_N, or — for a merge, or
  // any result carrying source_id — each id the rows actually name (the
  // server reads those off the held result, sqlTagsFor's `sids`). If ANY
  // of them has a file column called Tags, the bare "Tags" here is (at
  // least sometimes) the file's text, and text it stays.
  const { sidIdx, sid } = r.tags.ref;
  const sids = sidIdx === -1 ? [sid] : (r.tags.sids || []);
  const own = sids.some((id) => {
    const src = S.sources.find((s) => s.id === id);
    return src && src.columns.some((c) => String(c.name).toLowerCase() === 'tags');
  });
  return own ? -1 : t;
}

/* The result table is virtualized the way the grid is (invariant #6),
   over a result the SERVER holds (winnow/sqlresults.py): the pane has no
   row cap, and an answer of millions of rows is neither one response nor
   a million <tr>s. The run hands back the first page; the rest is fetched
   by position as it scrolls into view, into a cache on the result keyed
   by the sort it was fetched under. Only the rows in view, plus OVERSCAN
   either side, are in the DOM; a spacer row above and one below stand in
   for the rest and give the scroller its full height — capped and
   rescaled past MAX_SPACER_PX through the grid's own
   spacerPx/vScroll/rowsPaintY. A row not fetched yet paints as an empty
   row of the same height until it lands. A spacer with no height is left
   out, so a result that fits on screen is the plain table it always was.

   Every row is the same height (`.sql-result tbody tr` sets it, so a row
   of empty cells can't collapse, and the tag chips fit inside it),
   because the window arithmetic counts rows by height; a painted row is
   measured once the table is on screen, and wins if a skin disagrees.
   Column widths are the longest value in each column across EVERY row —
   the server measures them as the run lands — set on the header cell:
   auto table layout would otherwise size the columns to whichever rows
   happen to be painted, and they would jump as you scroll.

   Sort, scroll position, widths and the row cache live on the result
   (`r.pane`), so the repaint a tab switch or a return to the page does
   comes back where the analyst left it, not unsorted at the top. A click
   sort is the server's (the result is there, not here); it reproduces the
   order the pane used to sort in.

   Columns are windowed the same way, because a wide result is the other
   half of the same problem: `SELECT *` on a 150-column table built 150
   cells per row, and a paint of a screenful was tens of thousands of
   them. The header always holds every column — it is what sizes them —
   but a body row builds only the columns in view plus SQL_OVERSCAN_PX
   either way, with a `td.sql-gap` at its head spanning the columns left
   of the window so every built cell keeps its x. The rows' cells past the
   window are simply not there: off screen, and nothing reads the DOM for
   them. Copy, the exports, Save as table and the tag hotkeys all read the
   server's rows, never the table. */
export const SQL_ROW_H = 20;
const SQL_PAGE = 500;            // rows per fetch — about ten screens
const SQL_CACHE_MAX = 30000;     // rows kept per result before the far ones are dropped
const SQL_OVERSCAN_PX = 600;     // columns built past each edge of the pane
/* A 12px monospace glyph, low on purpose: for the one paint that runs
   before the table is in the document and has no geometry to read, an
   estimate that comes out small builds MORE columns than the pane shows,
   never fewer — and the first resize-observer paint measures. */
const SQL_EST_CH_PX = 6;

/* Each column's widest value, in characters (the table is monospace),
   with room in the header for its sort arrow. */
function sqlColumnChars(r) {
  return r.columns.map((c, i) => Math.max(String(c).length + 2, (r.widths && r.widths[i]) || 0));
}

/* The widest cell of chips in a Tags column, in the table's ch: a chip's
   text is 10px against the table's 12px, plus its padding and the gap. */
function sqlTagsChars(map) {
  const names = new Map(S.tags.map((t) => [t.id, t.name]));
  let best = 6;
  for (const ids of Object.values(map)) {
    let w = 0;
    for (const id of ids) w += (names.get(id) || `tag ${id}`).length * 0.85 + 2.3;
    if (w > best) best = w;
  }
  return Math.ceil(best);
}

const sqlSortKey = (sort) => (sort.idx == null ? 'none' : `${sort.idx}:${sort.dir}`);

/* A result's rows for one sort, by position. The run's own first page
   seeds the unsorted one. */
function sqlRowCache(r, sort) {
  const rows = new Map();
  if (sort.idx == null) (r.rows || []).forEach((row, i) => rows.set(i, row));
  return { key: sqlSortKey(sort), rows, loading: false, failed: null };
}

export function sqlResultNodes(r) {
  const pane = r.pane || (r.pane = { sort: { idx: null, dir: 1 }, top: 0, left: 0, ch: null, cache: null });
  if (!pane.ch) pane.ch = sqlColumnChars(r);
  if (!pane.cache) pane.cache = sqlRowCache(r, pane.sort);
  const { sort } = pane;
  const scroller = el('div', 'sql-result-scroll');
  const t = el('table');
  const thead = el('thead');
  const tbody = el('tbody');
  t.append(thead, tbody);
  scroller.append(t);
  // The shift-range anchor — must survive repaints. A position as well as
  // a key, because the rows between it and the click may never have been
  // fetched; only good for the sort it was clicked under.
  let anchor = null;
  let rowH = SQL_ROW_H;
  let painted = '';          // the window last drawn — a scroll inside it is free
  const annIdx = r.tags ? annotationTagsIdx(r) : -1;
  const colW = (n) => `calc(${n}ch + 18px)`;  // + padding and borders (border-box)
  // The chips' own column, after the result's, when the view's Tags text
  // isn't there to hold them.
  const tagsCol = r.tags && annIdx === -1 ? r.columns.length : -1;
  const width = r.columns.length + (tagsCol === -1 ? 0 : 1);
  const keyOf = (row) => (row && r.tags ? sqlRowKey(r.tags.ref, row) : null);
  let ths = [];
  // Each column's width in characters, as its header cell is set — what
  // the window is estimated from before there is a layout to read.
  const colChars = (ci) => (ci === tagsCol ? sqlTagsChars(r.tags.map)
    : ci === annIdx ? Math.max(pane.ch[ci], sqlTagsChars(r.tags.map)) : pane.ch[ci]);

  const paintHead = () => {
    const hr = el('tr');
    r.columns.forEach((c, i) => {
      const th = el('th', 'sql-th-sort', c);
      th.title = 'Click to sort the result by this column';
      // The view's own Tags column shows chips, which run wider than its text.
      th.style.width = colW(colChars(i));
      if (sort.idx === i) th.append(el('span', 'sort', sort.dir === 1 ? ' ▲' : ' ▼'));
      th.onclick = () => {
        sort.dir = sort.idx === i ? -sort.dir : 1;
        sort.idx = i;
        // Positions mean something else now: a new cache, and no anchor.
        pane.cache = sqlRowCache(r, sort);
        anchor = null;
        paintHead();
        paintBody(true);
      };
      hr.append(th);
    });
    if (tagsCol !== -1) {
      const th = el('th', null, 'Tags');
      th.style.width = colW(colChars(tagsCol));
      hr.append(th);
    }
    ths = [...hr.children];
    thead.replaceChildren(hr);
  };

  /* The columns to build: those in view plus SQL_OVERSCAN_PX either way,
     as [c0, c1). Read off the header cells, which hold every column at its
     full width (the table starts at the scroller's left edge, so a cell's
     offset in the table is its x in the scroller); estimated from the
     widths in characters until the table is laid out. */
  const colWindow = () => {
    const live = t.isConnected && scroller.clientWidth > 0;
    const left = live ? scroller.scrollLeft : pane.left || 0;
    const x0 = left - SQL_OVERSCAN_PX;
    const x1 = left + (live ? scroller.clientWidth : window.innerWidth) + SQL_OVERSCAN_PX;
    const tl = live ? t.getBoundingClientRect().left : 0;
    let c0 = 0, c1 = width, x = 0;
    for (let ci = 0; ci < width; ci++) {
      let cl = x, cr;
      if (live) {
        const b = ths[ci].getBoundingClientRect();
        cl = b.left - tl;
        cr = b.right - tl;
      } else {
        cr = cl + colChars(ci) * SQL_EST_CH_PX + 18;
      }
      x = cr;
      if (cr <= x0) c0 = ci + 1;
      if (cl >= x1) { c1 = ci; break; }
    }
    return { c0: Math.min(c0, c1), c1 };
  };

  // The columns left of the window, as one empty cell that keeps every
  // built cell at its own column's x.
  const gapCell = (cols) => {
    const td = el('td', 'sql-gap');
    td.colSpan = cols.c0;
    return td;
  };

  const rowNode = (row, i, cols) => {
    const tr = el('tr');
    tr.dataset.i = i;
    const key = keyOf(row);
    if (cols.c0 > 0) tr.append(gapCell(cols));
    for (let ci = cols.c0; ci < cols.c1; ci++) {
      const td = el('td');
      // The view's Tags text is replaced by the live chips, in the chips'
      // own column when it isn't there; a copy and the exports still read
      // the text, which the server holds.
      if (ci === annIdx || ci === tagsCol) td.append(tagChips(r.tags.map[key]));
      else td.textContent = row[ci] == null ? '' : String(row[ci]);
      tr.append(td);
    }
    if (key) {
      tr.classList.toggle('sql-row-sel', r.tags.sel.has(key));
      tr.style.cursor = 'pointer';
      tr.title = 'Click to select · Ctrl toggles · Shift ranges — tag hotkeys and Ctrl+C act on the selection. Double-click opens the row in its table.';
    }
    return tr;
  };

  // A row whose page hasn't landed: the same height, nothing in it.
  const pendingRow = (i, cols) => {
    const tr = el('tr', 'sql-row-pending');
    tr.dataset.i = i;
    if (cols.c0 > 0) tr.append(gapCell(cols));
    for (let k = cols.c0; k < cols.c1; k++) tr.append(el('td'));
    return tr;
  };

  const spacerRow = (h) => {
    const tr = el('tr', 'sql-spacer');
    const td = el('td');
    td.colSpan = width;
    td.style.height = h + 'px';
    tr.append(td);
    return tr;
  };

  const status = el('span', 'note-status');
  const statusText = () => {
    const stopped = r.stopped === 'cancelled' ? ' · stopped — these are the rows it had returned'
      : r.stopped === 'disk' ? ' · stopped — the disk Winnow keeps results on is nearly full'
        : '';
    const cache = pane.cache;
    const loading = cache.failed ? ` · ${cache.failed}` : cache.loading ? ' · loading rows…' : '';
    status.textContent = `${r.total.toLocaleString()} rows · ${r.elapsed_ms} ms${stopped}`
      + (r.tags ? ' · tags joined via rid' : '') + loading;
  };

  /* Fetch the first stretch of [first, last) the cache doesn't have, a
     page centred on it — one request in flight per result, and a page
     that lands repaints whichever build of this table is current
     (pane.repaint: a tab switch rebuilds the nodes while a fetch is out). */
  const ensureRows = (first, last) => {
    const cache = pane.cache;
    if (cache.loading || cache.failed) return;
    let a = first;
    while (a < last && cache.rows.has(a)) a++;
    if (a >= last) return;
    let b = last;
    while (b > a && cache.rows.has(b - 1)) b--;
    const count = Math.min(5000, Math.max(SQL_PAGE, b - a));
    const start = Math.max(0, Math.min(a - Math.floor((count - (b - a)) / 2), r.total - count));
    cache.loading = true;
    statusText();
    api(sqlResultUrl(r, `rows?start=${start}&count=${count}${sqlSortQuery(r)}`))
      .then((res) => {
        if (pane.cache !== cache) return;   // re-sorted while it was out
        res.rows.forEach((row, k) => cache.rows.set(res.start + k, row));
        // A page that brought nothing would be asked for again on every
        // paint — stop instead.
        if (!res.rows.length) cache.failed = 'some rows could not be loaded';
        if (cache.rows.size > SQL_CACHE_MAX) {
          const mid = res.start + res.rows.length / 2;
          for (const k of cache.rows.keys()) if (Math.abs(k - mid) > SQL_CACHE_MAX / 2) cache.rows.delete(k);
        }
      })
      .catch((e) => {
        if (pane.cache === cache) {
          cache.failed = e.status === 404 ? 'this result is no longer held — run the query again'
            : `rows could not be loaded: ${e.message}`;
        }
      })
      .finally(() => {
        cache.loading = false;
        if (pane.repaint) pane.repaint();
      });
  };

  const paintBody = (force = false) => {
    const total = r.total;
    const head = thead.offsetHeight;
    // Not laid out yet (built before it is attached, or on a hidden
    // page): paint a screenful, and the resize observer below repaints
    // with the real height the moment there is one.
    const viewH = scroller.clientHeight || 800;
    const virt = vScroll(scroller, total, head, rowH);
    const anchorRow = Math.floor(virt / rowH);
    const first = Math.max(0, Math.min(total, anchorRow - OVERSCAN));
    const last = Math.min(total, anchorRow + Math.ceil(viewH / rowH) + OVERSCAN);
    const top = total ? rowsPaintY(scroller, virt, first, rowH) : 0;
    const cache = pane.cache;
    const cols = colWindow();
    const sig = `${first}:${last}:${top}:${cols.c0}:${cols.c1}:${cache.key}:${cache.rows.size}`;
    if (!force && sig === painted) return;
    painted = sig;
    const bottom = spacerPx(total, rowH) - top - (last - first) * rowH;
    const out = [];
    if (top > 0.5) out.push(spacerRow(top));
    for (let i = first; i < last; i++) {
      const row = cache.rows.get(i);
      out.push(row ? rowNode(row, i, cols) : pendingRow(i, cols));
    }
    if (bottom > 0.5) out.push(spacerRow(bottom));
    tbody.replaceChildren(...out);
    ensureRows(first, last);
    statusText();
  };

  const posAt = (e) => {
    const tr = e.target.closest('tr[data-i]');
    return tr ? Number(tr.dataset.i) : null;
  };
  const keyAt = (e) => {
    const i = posAt(e);
    return i == null ? null : keyOf(pane.cache.rows.get(i));
  };
  const markSelection = () => {
    // Marked in place, not repainted: a double-click's second click lands
    // here too, and replacing the row under the pointer would hand the
    // dblclick that follows to a detached node, which never bubbles back
    // up to tbody's handler below.
    for (const tr of tbody.querySelectorAll('tr[data-i]')) {
      const k = keyOf(pane.cache.rows.get(Number(tr.dataset.i)));
      tr.classList.toggle('sql-row-sel', !!k && r.tags.sel.has(k));
    }
  };
  /* Shift+click: the anchor→here range in displayed order. From the cache
     when every row of it is there, else from the server, which has them
     all — a range can run through rows nobody scrolled past. */
  const selectRange = async (lo, hi) => {
    const cache = pane.cache;
    let keys = [];
    let complete = true;
    for (let i = lo; i <= hi && complete; i++) {
      const row = cache.rows.get(i);
      if (!row) complete = false;
      else keys.push(keyOf(row));
    }
    if (!complete) {
      try {
        const res = await api(sqlResultUrl(r,
          `keys?start=${lo}&count=${hi - lo + 1}&${sqlRefQuery(r.tags.ref)}${sqlSortQuery(r)}`));
        if (pane.cache !== cache) return;
        keys = res.keys.map((k) => (k ? `${k[0]}:${k[1]}` : null));
      } catch (e) {
        toast('Could not select that range: ' + e.message, 5000);
        return;
      }
    }
    r.tags.sel.clear();
    for (const k of keys) if (k) r.tags.sel.add(k);
    markSelection();
  };
  // Shift+mousedown would EXTEND the browser's text selection, which then
  // trips the copy-gesture guard below — suppress the native behavior so
  // Shift means "range of rows" here.
  tbody.onmousedown = (e) => { if (e.shiftKey && keyAt(e)) e.preventDefault(); };
  tbody.onclick = (e) => {
    const key = keyAt(e);
    if (!key) return;
    const pos = posAt(e);
    // Grid parity: plain click selects THIS row alone, Ctrl/Cmd toggles
    // it in place, Shift replaces with the anchor→here range in displayed
    // order. (A click ending a text selection is a copy gesture — leave
    // it alone.)
    if (!e.shiftKey && !window.getSelection().isCollapsed) return;
    if (e.shiftKey && anchor && anchor.cacheKey === pane.cache.key) {
      selectRange(Math.min(anchor.pos, pos), Math.max(anchor.pos, pos));
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      if (r.tags.sel.has(key)) r.tags.sel.delete(key);
      else r.tags.sel.add(key);
    } else {
      const only = r.tags.sel.size === 1 && r.tags.sel.has(key);
      r.tags.sel.clear();
      if (!only) r.tags.sel.add(key); // clicking the lone selected row deselects, like the grid's cursor toggle feel
    }
    anchor = { pos, cacheKey: pane.cache.key };
    markSelection();
  };
  tbody.ondblclick = async (e) => {
    const key = keyAt(e);
    if (!key) return;
    const [sid, rid] = key.split(':').map(Number);
    await openSource(sid);
    try {
      const res = await api(`/api/row_position?view_id=${S.view.view_id}&source_id=${sid}&rid=${rid}`);
      if (res.pos != null) moveCursor(res.pos);
    } catch { /* the row may be filtered out of the default view */ }
  };

  let raf = 0;
  scroller.addEventListener('scroll', () => {
    // Only while it is on screen: hiding the page is not the analyst
    // scrolling back to the top.
    if (scroller.clientHeight) { pane.top = scroller.scrollTop; pane.left = scroller.scrollLeft; }
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; paintBody(); });
  });
  let restored = false;
  const ro = new ResizeObserver(() => {
    if (!scroller.isConnected) { ro.disconnect(); return; }
    if (!scroller.clientHeight) return;
    const tr = tbody.querySelector('tr[data-i]');
    const h = tr ? tr.getBoundingClientRect().height : 0;
    if (h && Math.abs(h - rowH) > 0.25) rowH = h;
    paintBody(true);
    if (!restored) {
      restored = true;
      if (pane.top || pane.left) {
        scroller.scrollTop = pane.top;
        scroller.scrollLeft = pane.left;
        paintBody(true);
      }
    }
  });
  ro.observe(scroller);

  // The latest build of this result's table is the one a landing page
  // repaints; an older one (a tab switch rebuilt the nodes) is detached.
  pane.repaint = () => { if (scroller.isConnected) paintBody(true); };
  paintHead();
  paintBody(true);
  if (r.tags) r.tags.repaint = () => { paintHead(); paintBody(true); };
  // Status on the left, actions pushed to the right edge — the same shape
  // as the toolbar above the editor (Copy schema / Tables ▾ / Run), so the
  // two ends can never collide however long the status line gets. The bar
  // sits OUTSIDE the scroller, so the actions stay in view however far
  // down or across the result has been scrolled.
  const bar = el('div', 'sql-result-bar');
  statusText();
  bar.append(status);
  const acts = el('div', 'sql-result-actions');
  const copyBtn = el('button', 'btn ghost', 'Copy');
  copyBtn.title = 'Copy to the clipboard with a header row — the selected rows, '
    + 'or the whole result when nothing is selected';
  copyBtn.onclick = () => sqlCopyResult(r);
  const csvBtn = el('button', 'btn ghost', 'CSV…');
  csvBtn.title = 'Save this result as a CSV file, in the displayed order';
  csvBtn.onclick = () => sqlDownloadCsv(r);
  const xlsxBtn = el('button', 'btn ghost', 'Excel…');
  xlsxBtn.title = 'Save this result as an .xlsx workbook, in the displayed order';
  xlsxBtn.onclick = () => sqlDownloadXlsx(r);
  const saveBtn = el('button', 'btn ghost', 'Save as table…');
  saveBtn.title = 'Run the query again and land the result as a new table in the case';
  saveBtn.onclick = () => saveResultAsTable(r.sql);
  acts.append(copyBtn, csvBtn, xlsxBtn, saveBtn);
  bar.append(acts);
  return [bar, scroller];
}

/* "Save as table…": rerun the query server-side and land it as an
   ordinary source. Over the soft cap the server answers needs_confirm and
   we ask "you're about to save X rows" before resending with force.

   `sql` is the query behind the result on screen, which runSql keeps on
   the result. The editor's current text is only the fallback: an analyst
   who runs a query and then edits it is looking at the old result, and
   the button sits on that result — saving the edited text landed a table
   that matched neither what they saw nor what they had checked. */
export async function saveResultAsTable(sql) {
  if (typeof sql !== 'string') sql = $('sqlText').value;
  if (!sql.trim()) { toast('Nothing to save — run a query first'); return; }
  const name = await promptDialog('New table name:');
  if (!name || !name.trim()) return;
  let res;
  try {
    res = await post('/api/sql/to_table', { sql, name: name.trim() });
    if (res.needs_confirm) {
      const n = res.rows == null ? 'more than 500,000' : res.rows.toLocaleString();
      if (!(await confirmDialog(`You're about to save ${n} rows as a table. Are you sure?`,
        { okLabel: 'Save them' }))) return;
      res = await post('/api/sql/to_table', { sql, name: name.trim(), force: true });
    }
  } catch (e) {
    toast('Could not save: ' + e.message, 6000);
    return;
  }
  toast(`Created "${res.source.name}" · ${res.source.row_count.toLocaleString()} rows`);
  await loadSources();
  openSource(res.source.id);
}

/* What a running query shows in place of its result: how many rows have
   landed so far, and a Stop that keeps them. */
export function sqlRunningNodes(token) {
  const wrap = el('div', 'sql-running');
  const text = el('span', 'note-status sql-running-count', 'Running…');
  text.dataset.token = token;
  const stop = el('button', 'btn ghost', 'Stop');
  stop.title = 'Stop the query — the rows it has already returned are kept';
  stop.onclick = async () => {
    stop.disabled = true;
    stop.textContent = 'Stopping…';
    try { await post('/api/cancel_op', { token }); } catch { /* it finished first */ }
  };
  wrap.append(text, stop);
  return [wrap];
}

/* Polls a run's landed-row count into its "Running…" line, wherever that
   line is currently drawn (a tab switch draws a new one). Returns a stop. */
function pollSqlProgress(token) {
  let busy = false;
  const id = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      const p = await api(`/api/sql/run/progress?token=${encodeURIComponent(token)}`);
      for (const n of document.querySelectorAll('.sql-running-count')) {
        if (n.dataset.token === token && p.running) n.textContent = `Running… ${p.rows.toLocaleString()} rows so far`;
      }
    } catch { /* the run's own request reports anything that matters */ }
    busy = false;
  }, 400);
  return () => clearInterval(id);
}

/* The run in flight per SQL tab is S.sqlRuns — tab id → the run's op
   token, one at most — and this is each token's AbortController. The tab
   id alone can't say whether a result still belongs where it was
   started: a re-run in the same tab, closing the tab, or opening another
   case (whose tabs are numbered from 1 too) all leave the old run's
   answer pointing at a tab id that now means something else, and it used
   to paint there — over a newer result, or under case B's "Query 1",
   where tagging its rows wrote tags into the wrong case. So each run
   checks its token is still the tab's before it touches anything, and
   whatever ends a run's claim stops its query too. */
const sqlRunAborts = new Map();

/* Drop the run in flight for one tab, or for every tab (no argument —
   a case switch). Its request is aborted, which stops the query on the
   server (_run_pane_query notices the hang-up), and its token is
   cancelled as well, for a server that hasn't noticed yet; its result,
   if one arrives, is released rather than painted. */
export function abandonSqlRuns(tabId) {
  for (const [id, token] of [...S.sqlRuns]) {
    if (tabId !== undefined && id !== tabId) continue;
    S.sqlRuns.delete(id);
    const controller = sqlRunAborts.get(token);
    if (controller) controller.abort();
    post('/api/cancel_op', { token }).catch(() => {});
  }
}

export async function runSql() {
  // Captured up front: the run is awaited, and the analyst can switch tabs
  // while it's in flight. The result belongs to the tab it was started
  // from, and only paints if that tab is still the one showing.
  const tabId = S.sqlTabId;
  const sql = $('sqlText').value;
  abandonSqlRuns(tabId);   // a re-run supersedes the run still going in this tab
  const token = opToken();
  const controller = new AbortController();
  S.sqlRuns.set(tabId, token);
  sqlRunAborts.set(token, controller);
  const current = () => S.sqlRuns.get(tabId) === token;
  const superseded = { error: 'Superseded — the query was run again, its tab closed, or another case opened' };
  const prev = S.sqlResults.get(tabId);
  if (S.sqlTabId === tabId) {
    // The last result is going (the run releases it), so nothing acts on it.
    setActiveSqlResult(null);
    $('sqlResult').replaceChildren(...sqlRunningNodes(token));
  }
  setBusy(true);
  // The SQL is whatever was typed, and some of it never finishes — a
  // recursive CTE with no stop condition is one typo away — so a run has
  // to be stoppable, and what it stops is the query on the server, not
  // just this wait. The Stop beside the row count and the busy chip both
  // cancel this token.
  const disarmCancel = armOpCancel(token);
  const stopPolling = pollSqlProgress(token);
  let r;
  try {
    r = await post('/api/sql/run', { sql, op_token: token, release: prev && prev.id ? [prev.id] : [] },
      { signal: controller.signal });
    // The query that produced this result, kept with it: "Save as table…"
    // on the result re-runs THIS, not whatever the editor holds by then.
    r.sql = sql;
    // Decorate with a Tags column when the result can carry one (single
    // src_N + a rid column) — cached WITH the result so tab switches
    // repaint it without refetching.
    if (current()) r.tags = await sqlTagsFor(r, sql);
  } catch (e) {
    r = { error: e.status === 499 ? 'Query cancelled' : e.message };
  } finally {
    stopPolling();
    disarmCancel();
    sqlRunAborts.delete(token);
    setBusy(false);
  }
  if (!current()) {
    if (r.id) releaseSqlResults([r.id]);
    return superseded;
  }
  S.sqlRuns.delete(tabId);
  S.sqlResults.set(tabId, r);
  if (S.sqlTabId === tabId) {
    const out = $('sqlResult');
    if (r.error) {
      setActiveSqlResult(null);
      out.replaceChildren(el('div', 'sql-error', r.error));
    } else {
      setActiveSqlResult(r);
      out.replaceChildren(...sqlResultNodes(r));
    }
  }
  announceSqlRun(tabId, sql, r);
  return r;
}

/* winnow.sqlPage.onRun — every run, success or error, with the public
   result shape (no tag decoration). */
function announceSqlRun(tabId, sql, r) {
  document.dispatchEvent(new CustomEvent('winnow:sqlrun', { detail: { tabId, sql, result: publicSqlResult(r) } }));
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wirePlugins() {
$('btnRunSql').onclick = runSql;

$('btnRunSql').textContent = `Run  ${MOD_ENTER}`;
wirePagePanelResize();

wireSqlAssist();

$('sqlText').onkeydown = (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runSql(); }
};

$('sqlText').oninput = () => {
  const tab = activeSqlTab();
  if (tab) tab.sql = $('sqlText').value;
  scheduleSqlTabSave();
};
}

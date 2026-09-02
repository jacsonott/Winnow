/* The plugin host's frontend: the Plugins settings panel and plugin tabs.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { recordTabVisit } from './tabhistory.js';
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadPlugins, openImportModal, pluginFormatById, queueFilesForFormat } from './importer.js';
import { clearAllFilters, loadSources, openSource, renderPageTabs, syncTabSelection } from './sources.js';
import { setColumnFilter, valueFilterText } from './filters.js';
import { rebuildView } from './view.js';
import { activeSqlTab, hideMainViews, scheduleSqlTabSave, showGridTab, syncTabChrome } from './sql.js';
import { setActiveSqlResult, sqlCopyResult, sqlDownloadCsv, sqlRowKey, sqlTagsFor, tagChips, wireSqlAssist } from './sqlassist.js';
import { moveCursor } from './grid.js';
import { S } from './state.js';
import { confirmDialog, modal, promptDialog } from './ui.js';
import { updateTimeRangeButton } from './timeframe.js';

/* Settings → Plugins: everything about drop-in extensions in one place —
   every plugin found in the plugins directory (enabled, disabled, or
   failed-to-load with why), a checkbox per plugin that takes effect
   immediately (the server rescans and reloads its registry on every
   toggle; a disabled plugin's code is never even imported), and an
   installer that copies a picked .py file or plugin folder from anywhere
   on disk into the plugins directory — the same consent model as copying
   it in by hand, minus the hand. Appends into the Settings modal body and
   re-renders itself in place, same inline pattern as buildColumnsPanel.
   Each enabled format keeps its own no-accept-attribute file picker — the
   one file-picking path that can reach a target the format matches by
   bare-name pattern ("$MFT" has no extension for an accept to allow). */
export function buildPluginsPanel(b) {
  const box = el('div');
  b.append(box);

  function applyListing(r) {
    S.plugins = r.plugins || [];
    S.pluginFormats = r.formats || [];
    S.pluginTabs = r.tabs || [];
    S.pluginDirs = r.dirs || [];
    renderPluginTabs(); // a toggle/install can add or remove pinned tabs
  }

  async function installFiles(fileList, relPaths) {
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
    applyListing(r);
    renderPanel();
    if (r.error) toast(`Installed ${r.installed}, but it failed to load: ${r.error}`, 8000);
    else toast(`Installed ${r.installed}`);
  }

  /* "File or folder?" was the question the two old side-by-side buttons
     made the analyst answer blind — the browser can't offer one picker
     that takes either, so the dialog states the rule the pickers can't:
     which one you need is decided by how the plugin arrived on disk. If a
     folder pick contains no __init__.py, or a file pick isn't a .py, that's
     said here rather than left to a failed install. */
  function openInstallDialog() {
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
        'Either way it lands in the first plugins directory listed above, enabled immediately. '
        + 'The bundled examples are already listed — no install needed, just switch them on.'));

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
        installFiles(files, files.map((f) => f.name));
      };
      fileLabel.append(fileInput);
      const folderLabel = el('label', 'btn', 'Pick a plugin folder…');
      const folderInput = el('input');
      folderInput.type = 'file';
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
        installFiles(files, files.map((f) => f.webkitRelativePath || f.name));
      };
      folderLabel.append(folderInput);
      acts.append(fileLabel, folderLabel);
      b.append(acts);
    });
  }

  function renderPanel() {
    box.replaceChildren();
    box.append(el('p', null,
      'Drop-in extensions, Notepad++-style. Changes take effect immediately — no restart. '
      + 'A plugin runs with the same privileges as Winnow itself, so only install plugins you trust. '
      + 'The bundled examples ship with Winnow and start switched off.'));
    for (const d of S.pluginDirs) {
      const dir = el('div', 'note-status', d);
      dir.style.cssText = 'font-family:var(--mono)';
      box.append(dir);
    }
    for (const p of S.plugins) {
      const row = el('div', 'row-actions session-row');
      // Four scopes, not a checkbox: machine default ("everywhere") plus a
      // per-case override that lives in the case file and travels with it.
      // The select's value is the current state's provenance, so what it
      // shows is why the plugin is on/off, not just whether.
      const scopeSel = el('select');
      scopeSel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
        + 'padding:3px 6px;font:inherit;font-size:12px';
      const OPTIONS = [
        ['on_all', 'On — all cases'],
        ['off_all', 'Off — all cases'],
        ...(S.pluginsCaseOpen ? [
          ['on_case', 'On — this case only'],
          ['off_case', 'Off — this case only'],
        ] : []),
      ];
      for (const [v, label] of OPTIONS) {
        const o = el('option', null, label);
        o.value = v;
        scopeSel.append(o);
      }
      scopeSel.value = p.case_override === true ? 'on_case'
        : p.case_override === false ? 'off_case'
        : p.machine_enabled ? 'on_all' : 'off_all';
      scopeSel.title = p.case_override != null
        ? 'This case overrides the everywhere setting; other cases follow it'
        : 'Applies to every case on this machine';
      scopeSel.onchange = async () => {
        scopeSel.disabled = true;
        try {
          applyListing(await post('/api/plugins/toggle', { fs_name: p.fs_name, scope: scopeSel.value }));
        } catch (e) {
          toast('Could not change the plugin: ' + e.message, 5000);
        }
        renderPanel();
      };
      const parts = [];
      if ((p.formats || []).length) parts.push(`${p.formats.length} format${p.formats.length === 1 ? '' : 's'}`);
      if ((p.tabs || []).length) parts.push(`${p.tabs.length} tab${p.tabs.length === 1 ? '' : 's'}`);
      const status = p.error ? 'failed to load'
        : !p.enabled ? 'off'
        : (parts.join(', ') || 'loaded');
      const nameSpan = el('span', 'session-name', p.name + (p.version ? ` v${p.version}` : ''));
      row.append(scopeSel, nameSpan);
      if (p.bundled) {
        const badge = el('span', 'count', 'example — ships with Winnow');
        badge.style.cssText = 'border:1px solid var(--line-2);border-radius:var(--radius-sm);padding:0 5px';
        row.append(badge);
      }
      row.append(el('span', 'count', status));
      box.append(row);
      if (p.error) {
        const err = el('div', 'note-status', p.error);
        err.style.cssText = 'color:var(--bad, #c0392b);margin:0 0 10px 24px';
        box.append(err);
        continue;
      }
      if (p.description) {
        const desc = el('div', 'note-status', p.description);
        desc.style.cssText = 'margin:0 0 6px 24px';
        box.append(desc);
      }
      for (const fid of p.formats || []) {
        const f = pluginFormatById(fid);
        if (!f) continue;
        const frow = el('div', 'row-actions session-row');
        frow.style.marginLeft = '24px';
        const matches = (f.extensions || []).concat(f.filename_patterns || []).join(', ');
        frow.append(
          el('span', 'session-name', f.label),
          el('span', 'count', matches || 'no automatic matching'),
        );
        const pickLabel = el('label', 'btn ghost', 'Import files…');
        const inp = el('input');
        inp.type = 'file';
        inp.multiple = true;
        inp.hidden = true; // no accept attribute on purpose — see the panel comment
        inp.onchange = () => {
          if (!inp.files.length) return;
          queueFilesForFormat(f, [...inp.files]);
          openImportModal();
        };
        pickLabel.append(inp);
        frow.append(pickLabel);
        box.append(frow);
      }
    }

    const acts = el('div', 'row-actions');
    const installBtn = el('button', 'btn ghost', 'Install a plugin…');
    installBtn.onclick = openInstallDialog;
    acts.append(installBtn);
    box.append(acts);
  }

  renderPanel();
  // Refresh from the server in the background — cheap, and catches a
  // plugin someone dropped into the folder by hand since boot.
  loadPlugins().then(renderPanel);
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
  for (const m of pluginTabMounts.values()) m.container.remove();
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
    if (!t || t.gen !== m.gen) { m.container.remove(); pluginTabMounts.delete(id); }
  }
  if (S.activeTab.startsWith('plugin:') && !pluginTabById(S.activeTab.slice(7))) showGridTab();
}

/* The stable surface a plugin tab's module gets. Versioned via apiVersion
   the same way PLUGIN_API_VERSION covers the Python side: additions are
   free, changing what's already here isn't. `sql` (read-only, own
   connection server-side — see run_sql) is the blessed way for a tab to
   query the case; `schemaText` is the same LLM-ready schema dump the SQL
   pane's copy button builds. */
export function buildPluginTabContext(tab) {
  return {
    apiVersion: 2,
    plugin: tab.plugin,
    base: `/api/plugin/${tab.plugin_fs}`,      // the plugin's own register_api routes
    assets: `/plugin_assets/${tab.plugin_fs}`, // the plugin's own files (css, workers, data)
    api, post, toast, el, modal, confirmDialog, promptDialog,
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
      // table_histogram example's Store.time_histogram) to describe
      // exactly the rows on screen.
      get view() { return S.view ? { view_id: S.view.view_id, row_count: S.view.row_count } : null; },
    },
    // Fires after every grid rebuild — filter, sort, search, timeframe,
    // table switch — with {sourceId, viewId, rowCount}. Returns an
    // unsubscribe. This is what lets a panel follow the grid.
    onViewChange: (cb) => {
      const h = (e) => cb(e.detail);
      document.addEventListener('winnow:viewchange', h);
      return () => document.removeEventListener('winnow:viewchange', h);
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
  if (m && m.gen !== tab.gen) { m.container.remove(); pluginTabMounts.delete(tabId); m = null; }
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
      const mod = await import(`${buildPluginTabContext(tab).assets}/${tab.entry}?v=${tab.gen}`);
      if (typeof mod.default !== 'function') throw new Error('tab module has no default export to mount');
      await mod.default(container, buildPluginTabContext(tab));
      m.module = mod;
    } catch (e) {
      console.error(e);
      container.replaceChildren(el('p', 'note-status', `Plugin tab "${tab.label}" failed to load: ${e.message}`));
      return;
    }
  }
  if (m.module && m.module.onShow) { try { m.module.onShow(m.container); } catch (e) { console.error(e); } }
}

/* ---------------------------------------------------- toolbar panels */

/* Plugin toolbar panels (PluginAPI.register_toolbar_panel): a toggle
   button per panel in the table toolbar, and — while toggled on and the
   grid is showing — the panel's own UI in the #pluginPanels strip between
   the toolbar and the grid. Mounted once per plugin gen like a tab;
   hidden/shown after that, with onShow/onHide. The toggle persists per
   browser, keyed by the namespaced panel id, so an analyst who keeps the
   histogram open gets it back on the next case. */
const PANEL_PREFS_KEY = 'winnow.panels';
const pluginPanelMounts = new Map();   // panel id -> {container, module, gen}

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
  const host = $('pluginToolbarButtons');
  if (!host) return;
  host.replaceChildren();
  const live = new Set((S.pluginPanels || []).map((p) => p.id));
  // A panel whose plugin was disabled or reloaded loses its mount.
  for (const [id, m] of [...pluginPanelMounts]) {
    const p = (S.pluginPanels || []).find((x) => x.id === id);
    if (!p || p.gen !== m.gen) { m.container.remove(); pluginPanelMounts.delete(id); }
  }
  for (const p of S.pluginPanels || []) {
    const b = el('button', 'btn ghost plugin-panel-btn', p.label);
    b.dataset.panelId = p.id;
    b.title = (p.description || `${p.label} — from the ${p.plugin} plugin`) + ' (click to toggle)';
    b.setAttribute('aria-pressed', String(pluginPanelOpen(p.id)));
    b.onclick = () => togglePluginPanel(p.id);
    host.append(b);
    if (pluginPanelOpen(p.id) && live.has(p.id)) mountPluginPanel(p.id);
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
  const panel = (S.pluginPanels || []).find((p) => p.id === id);
  if (!panel) return;
  let m = pluginPanelMounts.get(id);
  if (m) return;
  const container = el('section', 'plugin-panel');
  container.dataset.panelId = id;
  $('pluginPanels').append(container);
  m = { container, module: null, gen: panel.gen };
  pluginPanelMounts.set(id, m);
  try {
    const ctx = buildPluginTabContext(panel);
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

/* Panels show only with the grid (the toolbar hides on page tabs, and so
   does the strip) and only while toggled on; the host collapses to
   nothing when no panel is visible, so the grid gets the row back. */
export function syncPluginPanels() {
  const host = $('pluginPanels');
  if (!host) return;
  const isGrid = S.activeTab === 'grid';
  let any = false;
  for (const [id, m] of pluginPanelMounts) {
    const show = isGrid && pluginPanelOpen(id);
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
  host.hidden = !any;
}

/* Split out of runSql so applySqlTabToEditor can re-paint a cached result
   when you switch back to a tab, without re-running its query. */
export function sqlResultNodes(r) {
  const t = el('table');
  const sort = { idx: null, dir: 1 };
  let lastClickedKey = null; // shift-range anchor — must survive repaints
  let displayedRows = r.rows; // what paint() last drew, for the CSV export
  const sortedRows = () => displayedRows;
  const paint = () => {
    t.replaceChildren();
    const hr = el('tr');
    r.columns.forEach((c, i) => {
      const th = el('th', 'sql-th-sort', c);
      th.title = 'Click to sort the result by this column';
      if (sort.idx === i) th.append(el('span', 'sort', sort.dir === 1 ? ' ▲' : ' ▼'));
      th.onclick = () => { sort.dir = sort.idx === i ? -sort.dir : 1; sort.idx = i; paint(); };
      hr.append(th);
    });
    if (r.tags) hr.append(el('th', null, 'Tags'));
    t.append(hr);
    let rows = r.rows;
    if (sort.idx != null) {
      // Client-side over the (already capped) result set — numeric when
      // both sides read as numbers, text otherwise, NULLs first.
      const { idx, dir } = sort;
      rows = [...rows].sort((a, b) => {
        const x = a[idx], y = b[idx];
        if (x == null && y == null) return 0;
        if (x == null) return -dir;
        if (y == null) return dir;
        const nx = Number(x), ny = Number(y);
        if (Number.isFinite(nx) && Number.isFinite(ny) && String(x).trim() !== '' && String(y).trim() !== '') {
          return (nx - ny) * dir;
        }
        return String(x).localeCompare(String(y)) * dir;
      });
    }
    displayedRows = rows;
    for (const row of rows) {
      const tr = el('tr');
      for (const v of row) tr.append(el('td', null, v == null ? '' : String(v)));
      if (r.tags) {
        const key = sqlRowKey(r.tags.ref, row);
        const td = el('td');
        td.append(tagChips(r.tags.map[key]));
        tr.append(td);
        if (key) {
          tr.classList.toggle('sql-row-sel', r.tags.sel.has(key));
          tr.style.cursor = 'pointer';
          tr.title = 'Click to select · Ctrl toggles · Shift ranges — tag hotkeys and Ctrl+C act on the selection. Double-click opens the row in its table.';
          // Shift+mousedown would EXTEND the browser's text selection,
          // which then trips the copy-gesture guard below — suppress the
          // native behavior so Shift means "range of rows" here.
          tr.onmousedown = (e) => { if (e.shiftKey) e.preventDefault(); };
          tr.onclick = (e) => {
            // Grid parity: plain click selects THIS row alone, Ctrl/Cmd
            // toggles it in place, Shift replaces with the anchor→here
            // range in displayed order. (A click ending a text selection
            // is a copy gesture — leave it alone.)
            if (!e.shiftKey && !window.getSelection().isCollapsed) return;
            if (e.shiftKey && lastClickedKey) {
              const keys = rows.map((rw) => sqlRowKey(r.tags.ref, rw)).filter(Boolean);
              const a = keys.indexOf(lastClickedKey), b2 = keys.indexOf(key);
              if (a !== -1 && b2 !== -1) {
                r.tags.sel.clear();
                for (let k = Math.min(a, b2); k <= Math.max(a, b2); k++) r.tags.sel.add(keys[k]);
              }
            } else if (e.ctrlKey || e.metaKey) {
              if (r.tags.sel.has(key)) r.tags.sel.delete(key);
              else r.tags.sel.add(key);
              lastClickedKey = key;
            } else {
              const only = r.tags.sel.size === 1 && r.tags.sel.has(key);
              r.tags.sel.clear();
              if (!only) r.tags.sel.add(key); // clicking the lone selected row deselects, like the grid's cursor toggle feel
              lastClickedKey = key;
            }
            paint();
          };
          tr.ondblclick = async () => {
            const [sid, rid] = key.split(':').map(Number);
            await openSource(sid);
            try {
              const res = await api(`/api/row_position?view_id=${S.view.view_id}&source_id=${sid}&rid=${rid}`);
              if (res.pos != null) moveCursor(res.pos, false);
            } catch { /* the row may be filtered out of the default view */ }
          };
        }
      }
      t.append(tr);
    }
  };
  paint();
  if (r.tags) r.tags.repaint = paint;
  // Status on the left, actions pushed to the right edge — the same shape
  // as the toolbar above the editor (Copy schema / Tables ▾ / Run), so the
  // two ends can never collide however long the status line gets.
  const bar = el('div', 'sql-result-bar');
  bar.append(el('span', 'note-status',
    `${r.rows.length.toLocaleString()} rows · ${r.elapsed_ms} ms${r.truncated ? ' · truncated' : ''}`
    + (r.tags ? ' · tags joined via rid' : '')));
  const acts = el('div', 'sql-result-actions');
  const copyBtn = el('button', 'btn ghost', 'Copy');
  copyBtn.title = 'Copy to the clipboard with a header row — the selected rows, '
    + 'or the whole result when nothing is selected';
  copyBtn.onclick = () => sqlCopyResult(r.columns, sortedRows());
  const csvBtn = el('button', 'btn ghost', 'CSV…');
  csvBtn.title = 'Save this result as a CSV file, in the displayed order';
  csvBtn.onclick = () => sqlDownloadCsv(r.columns, sortedRows());
  const saveBtn = el('button', 'btn ghost', 'Save as table…');
  saveBtn.title = "Run the query in FULL (this preview may be truncated) and land the result as a new table in the case";
  saveBtn.onclick = () => saveResultAsTable();
  acts.append(copyBtn, csvBtn, saveBtn);
  bar.append(acts);
  return [bar, t];
}

/* "Save as table…": rerun the tab's query in FULL server-side (the pane
   preview is capped) and land it as an ordinary source. Over the soft cap
   the server answers needs_confirm and we ask "you're about to save X
   rows" before resending with force. */
export async function saveResultAsTable() {
  const sql = $('sqlText').value;
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

export async function runSql() {
  const out = $('sqlResult');
  // Captured up front: the run is awaited, and the analyst can switch tabs
  // while it's in flight. The result belongs to the tab it was started
  // from, and only paints if that tab is still the one showing.
  const tabId = S.sqlTabId;
  out.replaceChildren(el('div', null, 'Running…'));
  setBusy(true);
  try {
    const sql = $('sqlText').value;
    const r = await post('/api/sql', { sql });
    // Decorate with a Tags column when the result can carry one (single
    // src_N + a rid column) — cached WITH the result so tab switches
    // repaint it without refetching.
    r.tags = await sqlTagsFor(r, sql);
    S.sqlResults.set(tabId, r);
    if (S.sqlTabId === tabId) {
      setActiveSqlResult(r);
      out.replaceChildren(...sqlResultNodes(r));
    }
  } catch (e) {
    S.sqlResults.set(tabId, { error: e.message });
    if (S.sqlTabId === tabId) out.replaceChildren(el('div', 'sql-error', e.message));
  } finally {
    setBusy(false);
  }
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wirePlugins() {
$('btnRunSql').onclick = runSql;

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

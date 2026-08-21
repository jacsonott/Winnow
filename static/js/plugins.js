/* The plugin host's frontend: the Plugins settings panel and plugin tabs.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadPlugins, openImportModal, pluginFormatById, queueFilesForFormat } from './importer.js';
import { openSource, renderPageTabs, syncTabSelection } from './sources.js';
import { activeSqlTab, scheduleSqlTabSave, showGridTab, syncTabChrome } from './sql.js';
import { S } from './state.js';
import { confirmDialog, modal, promptDialog } from './ui.js';

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

  function renderPanel() {
    box.replaceChildren();
    box.append(el('p', null,
      'Drop-in extensions, Notepad++-style. Toggles and installs take effect immediately — no restart. '
      + 'A plugin runs with the same privileges as Winnow itself, so only install plugins you trust.'));
    for (const d of S.pluginDirs) {
      const dir = el('div', 'note-status', d);
      dir.style.cssText = 'font-family:var(--mono)';
      box.append(dir);
    }

    if (!S.plugins.length) {
      box.append(el('p', 'note-status',
        'No plugins installed. A ready-made example (raw NTFS $MFT / USN journal parsing) ships in '
        + 'examples/plugins/mft_usn — install it below, or see plugins/README.md.'));
    }
    for (const p of S.plugins) {
      const row = el('div', 'row-actions session-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = p.enabled;
      cb.title = p.enabled ? 'Disable — its code will no longer be loaded' : 'Enable this plugin';
      cb.onchange = async () => {
        cb.disabled = true;
        try {
          applyListing(await post('/api/plugins/toggle', { fs_name: p.fs_name, enabled: cb.checked }));
          toast(cb.checked ? `Enabled ${p.name}` : `Disabled ${p.name} — its code is no longer loaded`);
        } catch (e) {
          toast('Could not toggle plugin: ' + e.message, 5000);
        }
        renderPanel();
      };
      const parts = [];
      if ((p.formats || []).length) parts.push(`${p.formats.length} format${p.formats.length === 1 ? '' : 's'}`);
      if ((p.tabs || []).length) parts.push(`${p.tabs.length} tab${p.tabs.length === 1 ? '' : 's'}`);
      const status = p.error ? 'failed to load'
        : !p.enabled ? 'disabled'
        : (parts.join(', ') || 'loaded');
      row.append(cb, el('span', 'session-name', p.name + (p.version ? ` v${p.version}` : '')), el('span', 'count', status));
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
    const fileLabel = el('label', 'btn ghost', 'Install a plugin file…');
    const fileInput = el('input');
    fileInput.type = 'file';
    fileInput.accept = '.py';
    fileInput.hidden = true;
    fileInput.onchange = () => {
      const files = [...fileInput.files];
      fileInput.value = '';
      installFiles(files, files.map((f) => f.name));
    };
    fileLabel.append(fileInput);
    const folderLabel = el('label', 'btn ghost', 'Install a plugin folder…');
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
      installFiles(files, files.map((f) => f.webkitRelativePath || f.name));
    };
    folderLabel.append(folderInput);
    acts.append(fileLabel, folderLabel);
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
    apiVersion: 1,
    plugin: tab.plugin,
    base: `/api/plugin/${tab.plugin_fs}`,      // the plugin's own register_api routes
    assets: `/plugin_assets/${tab.plugin_fs}`, // the plugin's own files (css, workers, data)
    api, post, toast, el, modal, confirmDialog, promptDialog,
    sql: (sql, limit = 5000) => post('/api/sql', { sql, limit }),
    schemaText: sqlSchemaForLLM,
    openSource,
    state: {
      get sources() { return S.sources; },
      get sourceId() { return S.sourceId; },
      get tags() { return S.tags; },
    },
  };
}

export async function showPluginTab(tabId) {
  const tab = pluginTabById(tabId);
  if (!tab) return;
  S.activeTab = 'plugin:' + tabId;
  $('grid').hidden = true;
  $('sqlview').hidden = true;
  $('timelineview').hidden = true;
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

/* Split out of runSql so applySqlTabToEditor can re-paint a cached result
   when you switch back to a tab, without re-running its query. */
export function sqlResultNodes(r) {
  const t = el('table');
  const hr = el('tr');
  for (const c of r.columns) hr.append(el('th', null, c));
  t.append(hr);
  for (const row of r.rows) {
    const tr = el('tr');
    for (const v of row) tr.append(el('td', null, v == null ? '' : String(v)));
    t.append(tr);
  }
  return [
    el('div', 'note-status', `${r.rows.length.toLocaleString()} rows · ${r.elapsed_ms} ms${r.truncated ? ' · truncated' : ''}`),
    t,
  ];
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
    const r = await post('/api/sql', { sql: $('sqlText').value });
    S.sqlResults.set(tabId, r);
    if (S.sqlTabId === tabId) out.replaceChildren(...sqlResultNodes(r));
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

$('sqlText').onkeydown = (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runSql(); }
};

$('sqlText').oninput = () => {
  const tab = activeSqlTab();
  if (tab) tab.sql = $('sqlText').value;
  scheduleSqlTabSave();
};
}

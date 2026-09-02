/* Case dashboards — a case holds several NAMED boards ("KAPE triage", a
   lateral-movement board), each a grid of widgets. Boards live under the
   sidebar's Dashboards section, not as a top-strip tab: a dashboard is a
   function, not one page. A widget is a data source (read-only SQL, the
   watchlist, or tag totals) rendered a chosen way — number, chart, list,
   chips. A board can be saved into a PROFILE (a plugin bundle + a named
   dashboard) and reused across cases of the same type. See
   docs/design/analysis-suite.md. */

import { drawBars, drawHistogram } from './charts.js';
import { $, api, el, post, toast } from './core.js';
import { recordTabVisit } from './tabhistory.js';
import { renderPageTabs, renderSidebar, syncTabSelection } from './sources.js';
import { showGridTab, showMainView, syncTabChrome } from './sql.js';
import { S } from './state.js';
import { dropdownMenu, modal, promptDialog, confirmDialog } from './ui.js';

let widgets = [];   // the CURRENT board's widgets (the one with S.dashboardId)

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
  try { widgets = (await api(`/api/dashboards/${id}`)).widgets || []; }
  catch { widgets = []; }
}

async function persist() {
  if (S.dashboardId == null) return;
  try { await post(`/api/dashboards/${S.dashboardId}`, { widgets }); }
  catch (e) { toast('Could not save dashboard: ' + e.message, 6000); }
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

export async function createDashboard() {
  const name = await promptDialog('New dashboard name:', '', { okLabel: 'Create' });
  if (!name || !name.trim()) return;
  try {
    const d = await post('/api/dashboards', { name: name.trim() });
    await loadDashboards();
    renderSidebar();
    await showDashboard(d.id);
  } catch (e) { toast('Could not create dashboard: ' + e.message, 6000); }
}

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
  const edit = el('button', 'dash-edit', '✎');
  edit.title = 'Edit widget';
  edit.onclick = () => openWidgetEditor(w);
  head.append(edit);
  c.append(head);
  wireWidgetDrag(c, i, grip);   // drag the grip to reorder
  const body = el('div', 'dash-widget-body');
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
        body.append(row);
      }
      break;
    case 'bar': {
      const canvas = el('canvas'); canvas.style.cssText = 'width:100%;height:100%;display:block';
      body.style.height = '160px'; body.append(canvas);
      requestAnimationFrame(() => drawBars(canvas, {
        rows: rows.map((r) => ({ label: String(r[0]), value: num(r[r.length - 1]) })),
        label: 'label', value: 'value' }));
      break;
    }
    case 'histogram': {
      const canvas = el('canvas'); canvas.style.cssText = 'width:100%;height:100%;display:block';
      body.style.height = '120px'; body.append(canvas);
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      requestAnimationFrame(() => drawHistogram(canvas, {
        buckets: rows.map((r) => [String(r[0]), [num(r[r.length - 1])]]), colors: [accent] }));
      break;
    }
    default:
      body.append(el('div', 'note-status', `Unknown render "${w.render}"`));
  }
}

/* Starting points so a widget isn't a blank SQL box. Each fills the query
   (against whichever table is picked) and the render kind; the ⟨column⟩ /
   ⟨value⟩ placeholders are what the analyst then edits. */
const WIDGET_TEMPLATES = [
  { id: 'blank', label: 'Blank — write my own SQL' },
  { id: 'count', label: 'Total row count', render: 'stat', title: 'Row count',
    sql: (t) => `SELECT COUNT(*) AS n FROM ${t}` },
  { id: 'countwhere', label: 'Count matching a condition', render: 'stat', title: 'Matches',
    sql: (t) => `SELECT COUNT(*) AS n FROM ${t}\nWHERE ⟨column⟩ = '⟨value⟩'` },
  { id: 'distinct', label: 'Distinct count', render: 'stat', title: 'Distinct',
    sql: (t) => `SELECT COUNT(DISTINCT ⟨column⟩) AS n FROM ${t}` },
  { id: 'top', label: 'Top values of a column (bar chart)', render: 'bar', title: 'Top values',
    sql: (t) => `SELECT ⟨column⟩ AS label, COUNT(*) AS n\nFROM ${t}\nGROUP BY ⟨column⟩ ORDER BY n DESC LIMIT 10` },
  { id: 'rare', label: 'Rarest values / long tail (list)', render: 'list', title: 'Rarest values',
    sql: (t) => `SELECT ⟨column⟩ AS label, COUNT(*) AS n\nFROM ${t}\nGROUP BY ⟨column⟩ ORDER BY n ASC LIMIT 10` },
  { id: 'time', label: 'Events over time (histogram)', render: 'histogram', title: 'Over time',
    sql: (t) => `SELECT strftime('%Y-%m-%d', TS_NORMALIZE(⟨timestamp⟩)) AS day, COUNT(*) AS n\nFROM ${t}\nGROUP BY day ORDER BY day` },
];

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

function openWidgetEditor(existing, prefill = null) {
  modal(existing ? 'Edit widget' : 'Add widget', (b) => {
    const mk = (label, node) => { b.append(el('label', null, label)); b.append(node); return node; };
    const title = el('input'); title.className = 'confirm-input'; title.value = existing?.title || prefill?.title || '';
    const source = el('select');
    for (const o of ['sql', 'watchlist', 'tags']) source.append(new Option(o, o));
    source.value = existing?.source || 'sql';
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
    // Template + table pickers that write a starting query for you.
    const templ = el('select');
    for (const t of WIDGET_TEMPLATES) templ.append(new Option(t.label, t.id));
    const tableSel = el('select');
    for (const o of dashTableOptions()) tableSel.append(new Option(o.label, o.value));
    const applyTemplate = () => {
      const t = WIDGET_TEMPLATES.find((x) => x.id === templ.value);
      if (!t || t.id === 'blank' || !tableSel.value) return;
      sql.value = t.sql(tableSel.value);
      renderSel.value = t.render;
      if (!title.value.trim()) title.value = t.title;
    };
    templ.onchange = applyTemplate;
    tableSel.onchange = () => { if (templ.value !== 'blank') applyTemplate(); };

    // Build the widget object from the current field values — shared by
    // Preview and Save so they can't disagree about what "this widget" is.
    const draft = () => ({
      title: title.value.trim() || '(untitled)', source: source.value, render: renderSel.value,
      span: Number(span.value), sub: sub.value.trim() || undefined,
      query: source.value === 'sql' ? { sql: sql.value.trim() } : {},
    });

    b.append(el('p', 'fb-help', 'A widget is a data source rendered a chosen way. '
      + 'SQL is read-only against the case; watchlist and tags read case state. '
      + 'stat/kv/chips/list/bar/histogram interpret the returned columns.'));
    mk('Title', title); mk('Data source', source);
    const sqlWrap = el('div');
    sqlWrap.append(el('label', null, 'Start from a template'), templ);
    sqlWrap.append(el('label', null, 'Table'), tableSel);
    sqlWrap.append(el('label', null, 'SQL query'), sql);
    sqlWrap.append(el('p', 'fb-help', 'Pick a template and a table to get a working query, then '
      + 'edit the ⟨column⟩ / ⟨value⟩ parts. A {{…}} table is portable — it resolves on any case, '
      + 'so the widget still works when this dashboard is saved as a profile.'));
    b.append(sqlWrap);
    mk('Render as', renderSel); mk('Sub-label (optional, for stat)', sub); mk('Width', span);
    const syncSql = () => { sqlWrap.style.display = source.value === 'sql' ? '' : 'none'; };
    source.onchange = syncSql; syncSql();

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
      if (existing) Object.assign(existing, w); else widgets.push(w);
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
  try {
    await post('/api/plugin_bundles', { name: name.trim(), plugins, dashboard: widgets });
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

/* Case dashboard — a grid of widgets that summarize the open case. Every
   widget is a saved query + a render kind: SQL runs through the read-only
   run_sql path, watchlist/tags read case state. That makes a dashboard
   DATA, not code — build a query, pick a render, done. The layout lives
   in the case .db and can be saved into a PROFILE (a plugin bundle + a
   dashboard) to reuse across cases of the same type. See
   docs/design/analysis-suite.md. */

import { drawBars, drawHistogram } from './charts.js';
import { $, api, el, post, toast } from './core.js';
import { recordTabVisit } from './tabhistory.js';
import { showMainView, syncTabChrome } from './sql.js';
import { syncTabSelection } from './sources.js';
import { S } from './state.js';
import { modal, promptDialog, confirmDialog } from './ui.js';

let widgets = [];
let loaded = false;

const num = (v) => (typeof v === 'number' ? v : (parseFloat(String(v).replace(/,/g, '')) || 0));

async function load() {
  try { widgets = (await api('/api/dashboard')).widgets || []; } catch { widgets = []; }
  loaded = true;
  render();
}

async function persist() {
  try { await post('/api/dashboard', { widgets }); } catch (e) { toast('Could not save dashboard: ' + e.message, 6000); }
}

function render() {
  const grid = $('dashGrid');
  grid.replaceChildren();
  if (!widgets.length) {
    const e = el('div', 'dash-empty');
    e.append(el('p', null, 'No widgets yet. A dashboard is a grid of small summaries of the case.'));
    e.append(el('p', null, 'Click “＋ Add widget”, pick a template (row count, top values, events '
      + 'over time…) and a table, and it writes the query for you — tweak it, choose how it '
      + 'renders (number, chart, list, chips), and save.'));
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

function card(w, i) {
  const c = el('div', 'dash-card' + (w.span ? ` span${Math.min(4, w.span)}` : ''));
  const head = el('div', 'dash-head');
  head.append(el('h4', null, w.title || '(untitled)'));
  const rm = el('button', 'dash-rm', '✕');
  rm.onclick = async () => { widgets.splice(i, 1); await persist(); render(); };
  head.append(rm);
  c.append(head);
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

function openWidgetEditor(existing) {
  modal(existing ? 'Edit widget' : 'Add widget', (b) => {
    const mk = (label, node) => { b.append(el('label', null, label)); b.append(node); return node; };
    const title = el('input'); title.className = 'confirm-input'; title.value = existing?.title || '';
    const source = el('select');
    for (const o of ['sql', 'watchlist', 'tags']) source.append(new Option(o, o));
    source.value = existing?.source || 'sql';
    const renderSel = el('select');
    for (const o of ['stat', 'kv', 'chips', 'list', 'bar', 'histogram']) renderSel.append(new Option(o, o));
    renderSel.value = existing?.render || 'stat';
    const span = el('select');
    for (const o of [1, 2, 3, 4]) span.append(new Option(`${o} column${o === 1 ? '' : 's'}`, String(o)));
    span.value = String(existing?.span || 1);
    const sql = el('textarea'); sql.rows = 5; sql.className = 'dash-sql';
    sql.value = existing?.query?.sql || '';
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
    const acts = el('div', 'row-actions');
    const save = el('button', 'btn', 'Save widget');
    save.onclick = async () => {
      if (!title.value.trim()) { toast('Give the widget a title'); return; }
      if (source.value === 'sql' && !sql.value.trim()) { toast('SQL widget needs a query'); return; }
      const w = { title: title.value.trim(), source: source.value, render: renderSel.value,
                  span: Number(span.value), sub: sub.value.trim() || undefined,
                  query: source.value === 'sql' ? { sql: sql.value.trim() } : {} };
      if (existing) Object.assign(existing, w); else widgets.push(w);
      await persist();
      document.getElementById('modal').hidden = true;
      render();
    };
    acts.append(save); b.append(acts);
  }, { wide: true });
}

export function wireDashboard() {
  $('tabDashboard').onclick = showDashboardTab;
  $('dashAddTop').onclick = () => openWidgetEditor(null);
  $('dashSaveProfile').onclick = async () => {
    const name = await promptDialog('Save the current plugins + this dashboard as a profile named:');
    if (!name || !name.trim()) return;
    // The profile's plugins = whatever's enabled for this case now.
    const enabled = (S.pluginTabs || []).map((t) => t.plugin_fs);
    const plugins = [...new Set((S.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name).concat(enabled))];
    try {
      await post('/api/plugin_bundles', { name: name.trim(), plugins, dashboard: widgets });
      toast(`Profile "${name.trim()}" saved — apply it from the ⚙ Plugins menu on a new case`, 7000);
    } catch (e) { toast(e.message, 6000); }
  };
}

// Dashboard lives in the case; a switch reloads it.
export function resetDashboard() { loaded = false; widgets = []; }

export async function showDashboardTab() {
  recordTabVisit({ kind: 'page', key: 'dashboard' });
  S.activeTab = 'dashboard';
  showMainView('dashboardview');
  syncTabSelection();
  syncTabChrome();
  if (!loaded) await load(); else render();
}

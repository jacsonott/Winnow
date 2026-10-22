/* The Tables manager — every table in the case, its merges and saved
   tables, and the indexes the case grows; plus removing a table and
   compacting the case file, which other menus call too.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { tableMenuSeg, tableMenuStat } from './timeframe.js';
import { $, api, el, post, setBusy, toast } from './core.js';
import { writeClipboardText } from './grouping.js';
import { openImportModal } from './importer.js';
import { columnGroupKey, openMergeBuilder } from './merge.js';
import { sqlSchemaForLLM } from './plugins.js';
import { nicknameFor } from './savedfilters.js';
import { closeAllTabs, closeTab, dropViewStateFor, foldersFlattened, loadSources, openSource, openTables, sourceGlyph, sourceLabel, sourceTitle, subsetParentLabel, tableMenuItems, taggedTablesToOpen } from './sources.js';
import { S } from './state.js';
import { alertDialog, closeModal, confirmDialog, dropdownMenu, markModalAction, modal, openMenuAnchor, sectionPanes } from './ui.js';
import { cancelPendingView } from './view.js';

/* The manager's background poll (openTablesManager, at the bottom), kept
   here so a second opening replaces the first's rather than adding one. */
export let tablesModalPoll = null;

export async function refreshSourcesQuietly() {
  const [sources, merges] = await Promise.all([api('/api/sources'), api('/api/merges')]);
  S.sources = [...sources, ...merges];
}

/* The search index's state, as the chip beside a table's counts says it.
   Worded to stand alone: the chip has no column heading over it, and the
   old bare "Not started" in a bordered box read as a button that would
   start something. */
export function indexStatusFor(s) {
  if (s.has_fts) return { text: 'index ready', cls: 'ready' };
  if (s.fts_building) return { text: 'indexing…', cls: 'building' };
  return { text: 'not indexed yet', cls: 'idle' };
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/* VACUUM, behind a confirm. Nothing else in the app ever returns freed
   pages to the OS — dropping a table, or the startup janitor clearing out
   a stale FTS index (which on a fat-trigram case file is most of its bulk),
   frees them to SQLite's own freelist, where they stay reserved for this
   case file forever. That's the right default, but after a big cleanup it
   can be tens of GB parked on disk with no way to ask for it back. The SQL
   pane deliberately refuses VACUUM (see run_sql), so this button is it. */
export async function compactCaseFile() {
  const ok = await confirmDialog(
    'Compact this case file?\n\n'
    + 'This rewrites the whole file to return space freed by removed tables and '
    + 'indexes to the operating system. On a large case it can take several minutes, '
    + 'during which the app will be unresponsive, and it needs as much free disk '
    + 'space as the case file currently uses.',
    { okLabel: 'Compact' },
  );
  if (!ok) return;
  setBusy(true);
  let res;
  try { res = await post('/api/case/compact', {}); }
  catch (e) { toast('Compact failed: ' + e.message, 6000); return; }
  finally { setBusy(false); }
  // before/after_bytes are already whole-footprint (main + -wal) — the
  // store owns that definition so this can't drift from reclaimed_bytes.
  // A checkpoint the readers wouldn't let finish leaves the freed bytes
  // in the WAL until a later passive one collects them; reclaimed_bytes
  // already counts them as not-reclaimed, so the toast just says why.
  const pending = res.wal_checkpointed === false && res.wal_pending_bytes > 0
    ? ` · ${fmtBytes(res.wal_pending_bytes)} still in the write-ahead log (a read was holding it open); it frees up on its own shortly`
    : '';
  toast((res.reclaimed_bytes > 0
    ? `Compacted: ${fmtBytes(res.before_bytes)} → ${fmtBytes(res.after_bytes)} on disk, reclaimed ${fmtBytes(res.reclaimed_bytes)}`
    : `Compacted — nothing to reclaim (${fmtBytes(res.after_bytes)} on disk)`) + pending, 8000);
}

/* The merges built on a table — what stops it being removed (removeSource
   says why). One rule for every Remove: the manager's button refuses with
   it and a table's right-click menu greys its entry out with it, so the two
   can't disagree about which tables can go. A merge itself is never in
   one: merges aren't merged into merges. */
export function mergesUsing(s) {
  if (!s || s.is_merge) return [];
  return (S.sources || []).filter((m) => m.is_merge && (m.member_source_ids || []).includes(s.id));
}

/* Why a table can't be removed, as a sentence — or null when it can. */
export function removeRefusal(s) {
  const usedBy = mergesUsing(s);
  if (!usedBy.length) return null;
  const names = usedBy.map((m) => `"${sourceLabel(m)}"`).join(', ');
  return `${sourceLabel(s)} is merged into ${names}. Delete `
    + (usedBy.length === 1 ? 'that merge' : 'those merges')
    + ' first — a merge is only a view over its tables, so deleting it keeps them.';
}

/* Remove a table or a merge from the case, behind its confirm. Shared by
   the Tables manager's Remove… and a table's right-click menu; true when
   it was removed, so a caller showing a list knows to repaint it. */
export async function removeSource(s) {
  /* A table a merge is built on is refused, not warned about: dropping it
     would leave the merge naming a member that is not there (see
     Store.drop_source). The server refuses this too (409); asking here as
     well is so the analyst gets the reason and the merge's name without a
     round trip, and so the button never looks like it did nothing. */
  const refusal = removeRefusal(s);
  if (refusal) {
    await alertDialog(refusal);
    return false;
  }
  const warn = s.is_merge
    ? `Delete merge "${sourceLabel(s)}"? The tables under it are untouched.`
    : `Remove ${sourceLabel(s)} from this case? Tags and notes for it are deleted too.`;
  if (!(await confirmDialog(warn, { danger: true, okLabel: 'Remove' }))) return false;
  // A search running in the background for it, or landed and
  // waiting for Apply, is for a table that is about to not exist:
  // its row goes, and the server drops the build or the held view.
  cancelPendingView(s.id);
  try {
    await api(s.is_merge ? `/api/merges/${-s.id}` : `/api/source/${s.id}`, { method: 'DELETE' });
  } catch (e) {
    // A refusal (the server's own "merged into" 409, for a merge
    // another window just made) has a reason worth reading; without
    // this it was an unhandled rejection and the button looked dead.
    toast(`Could not remove ${sourceLabel(s)}: ${e.message}`, 6000);
    return false;
  }
  if (!s.is_merge) {
    S.viewCache.delete(s.id); // SQLite can reuse a deleted source's row id — don't let a stale cached view leak onto it
    dropViewStateFor(s.id);   // nor the filters/sort stashed for it
    if (S.diffMarks && S.diffMarks.sourceId === s.id) S.diffMarks = null; // nor a comparison's marks
  }
  if (S.sourceId === s.id) S.sourceId = null;
  await loadSources();
  return true;
}

/* How the Every table pane is set out, kept for the page rather than per
   opening: a filter typed, a grouping picked or a sort clicked is still
   there when the manager comes back from the table menu it opened. */
export const tablesView = { query: '', groupBy: 'folder', sort: 'name' };

export const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
export const realTables = () => (S.sources || []).filter((s) => !s.is_merge && !s.error);

/* What a grouping calls a table's set of columns: the header set's name
   when it has one (headers.json, or the analyst's), else its first
   columns — the merge builder's wording, so a set reads the same in both. */
export function kindLabel(s) {
  const cols = (s.columns || []).filter((c) => !c.derived).map((c) => c.name);
  return nicknameFor(cols) || cols.slice(0, 3).join(', ') + (cols.length > 3 ? ` +${cols.length - 3} more` : '');
}

/* The tables in each group, in the order the pane lists them. By folder
   is the sidebar's tree (a folder's path, then the tables in no folder);
   by kind is one group per header set, biggest first; merges are their
   own group either way, since they have neither. */
export function groupedTables(list) {
  const merges = list.filter((s) => s.is_merge);
  const rest = list.filter((s) => !s.is_merge);
  const groups = [];
  if (tablesView.groupBy === 'kind') {
    const byKey = new Map();
    for (const s of rest) {
      const key = s.error ? '\u0000error' : columnGroupKey(s.columns || []);
      if (!byKey.has(key)) byKey.set(key, { key, label: s.error ? 'Could not be read' : kindLabel(s), tables: [], kind: true });
      byKey.get(key).tables.push(s);
    }
    groups.push(...[...byKey.values()].sort((a, b) => b.tables.length - a.tables.length || a.label.localeCompare(b.label)));
  } else {
    const flat = foldersFlattened();
    const byFolder = new Map(flat.map((f) => [f.id, { key: `f${f.id}`, label: f.pathLabel, tables: [] }]));
    const loose = { key: 'root', label: flat.length ? 'Not in a folder' : '', tables: [] };
    for (const s of rest) (byFolder.get(s.folder_id) || loose).tables.push(s);
    groups.push(...[...byFolder.values()].filter((g) => g.tables.length), loose);
  }
  if (merges.length) groups.push({ key: 'merges', label: 'Merges', tables: merges });
  const by = tablesView.sort;
  const cmp = by === 'name' ? (a, b) => sourceLabel(a).localeCompare(sourceLabel(b))
    : (a, b) => ((b[by] || 0) - (a[by] || 0)) || sourceLabel(a).localeCompare(sourceLabel(b));
  for (const g of groups) g.tables.sort(cmp);
  return groups.filter((g) => g.tables.length);
}

/* A repaint replaces every row; the control that had the focus gets it
   back in its new row, so the 1.5 s poll never drops a keyboard user to
   the page. */
export function keepingFocus(list, paint) {
  const had = list.contains(document.activeElement) ? document.activeElement : null;
  const row = had && had.closest('[data-id]');
  const id = row ? row.dataset.id : null;
  const role = had ? had.dataset.role : null;
  paint();
  if (id == null) return;
  const again = list.querySelector(`[data-id="${CSS.escape(id)}"] [data-role="${role}"]`);
  if (again) again.focus();
}

/* A table's name goes to it, opening its tab first when it is closed
   (the sidebar's closed rows do the same): openSource alone would show
   the grid with no tab on the strip for it. */
export async function goTo(s) {
  closeModal();
  if (s.is_open) await openSource(s.id);
  else await openTables([s]);
}

/* A count in one of the list's number columns: dim when there is none,
   so the tables that hold tagged rows or notes are the ones that read. */
export function countCell(n, cls) {
  const c = el('span', 'tables-num ' + cls + (n ? ' some' : ''), n ? n.toLocaleString() : '—');
  return c;
}

/* "Every table": the case's tables in one list, ticked when open as a tab
   — the Columns pane's shape, a filter above a box that is the one thing
   that scrolls. Grouped by folder (the sidebar's tree) or by kind (the
   header set, which is how a KAPE import is read: every host's EvtxECmd
   output together, a Merge… on the group). */
export function buildTablesPane(pane) {
  const changed = () => pane.dispatchEvent(new CustomEvent('tables:changed', { bubbles: true }));
  const top = el('div', 'tm-cols-top');
  const search = el('input', 'side-search');
  search.type = 'search';
  search.placeholder = 'Filter tables…';
  search.setAttribute('aria-label', 'Filter tables');
  search.value = tablesView.query;
  const groupBy = tableMenuSeg([
    ['folder', 'Folders', 'Grouped as the sidebar files them'],
    ['kind', 'Kinds', 'Grouped by their set of columns — every host’s EvtxECmd output together'],
  ], () => tablesView.groupBy, (key) => { tablesView.groupBy = key; groupBy.paint(); paint(); }, 'tables-groupby');
  const count = el('span', 'tm-cols-count');
  top.append(search, groupBy.seg, count);
  pane.append(top);

  const box = el('div', 'side-preview tm-cols-box tables-box');
  const head = el('div', 'tables-head');
  const sortBtn = (key, label, cls) => {
    const b = el('button', 'tables-sort ' + cls, label);
    b.type = 'button';
    b.dataset.sort = key;
    b.title = key === 'name' ? 'Sort by name' : `Most ${label.toLowerCase()} first`;
    b.onclick = () => { tablesView.sort = key; paint(); };
    return b;
  };
  head.append(sortBtn('name', 'Table', 'tables-name-h'), sortBtn('row_count', 'Rows', 'tables-num'),
    sortBtn('tagged_row_count', 'Tagged', 'tables-num'), sortBtn('note_count', 'Notes', 'tables-num'),
    el('span', 'tables-more-h'));
  const scroller = el('div', 'side-preview-rows');
  const list = el('div', 'tables-list');
  scroller.append(list);
  const foot = el('div', 'side-preview-foot');
  foot.append(el('p', 'fb-help', 'Tick a table to open it as a tab. Unticking closes the tab; the table stays in the case.'));
  box.append(head, scroller, foot);
  pane.append(box);

  const acts = el('div', 'row-actions tables-acts-bar');
  const openTagged = el('button', 'btn ghost');
  openTagged.onclick = async () => {
    // Which tables count as tagged is decided in one place (sources.js),
    // shared with the sidebar's button. navigate:false — the manager is
    // where the analyst is; the list ticks them as they open.
    const targets = taggedTablesToOpen();
    if (!targets.length) return;
    setBusy(true);
    try { await openTables(targets, { navigate: false }); }
    finally { setBusy(false); }
    paint();
    changed();
  };
  const closeAll = el('button', 'btn ghost', 'Close all tabs');
  closeAll.title = 'Close every open tab — the tables stay in the case';
  closeAll.onclick = async () => { await closeAllTabs(); paint(); changed(); };
  acts.append(openTagged, closeAll);
  pane.append(acts);

  async function setOpen(s, open) {
    setBusy(true);
    try {
      if (open) await openTables([s], { navigate: false });
      else await closeTab(s);
    } finally { setBusy(false); }
    paint();
    changed();
  }

  function row(s) {
    const r = el('div', 'tables-row' + (s.is_open ? ' open' : '') + (s.error ? ' error' : ''));
    r.dataset.id = String(s.id);
    const tick = el('input');
    tick.type = 'checkbox';
    tick.dataset.role = 'open';
    tick.checked = !!s.is_open;
    tick.disabled = !!s.error;
    tick.setAttribute('aria-label', `${s.is_open ? 'Close' : 'Open'} ${sourceLabel(s)} as a tab`);
    tick.onchange = () => setOpen(s, tick.checked);
    const name = el('button', 'tables-name', sourceGlyph(s) + sourceLabel(s));
    name.type = 'button';
    name.dataset.role = 'name';
    name.title = sourceTitle(s, s.error ? undefined : 'Click to go to it');
    name.disabled = !!s.error;
    name.onclick = () => goTo(s);
    const cell = el('span', 'tables-name-cell');
    cell.append(name);
    if (s.error) cell.append(el('span', 'tables-sub warn', s.error));
    else if (s.origin === 'subset') cell.append(el('span', 'tables-sub', subsetParentLabel(s).replace(/^Subset of /, 'from ')));
    else if (s.is_merge) cell.append(el('span', 'tables-sub', plural((s.member_source_ids || []).length, 'table')));
    r.append(tick, cell);
    if (s.error) r.append(el('span', 'tables-num'), el('span', 'tables-num'), el('span', 'tables-num'));
    else r.append(countCell(s.row_count, 'tables-rows'), countCell(s.tagged_row_count, 'tables-tagged'), countCell(s.note_count, 'tables-notes'));
    const more = el('button', 'btn ghost row-more tables-more', '⋯');
    more.dataset.role = 'more';
    more.title = 'Close, nickname, columns, merge, remove';
    more.setAttribute('aria-label', `Actions for ${sourceLabel(s)}`);
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-expanded', 'false');
    // The table's right-click menu, item for item, so the manager and the
    // tab strip can't disagree about what a table offers or when Remove
    // is refused. Errored rows (a broken merge) get Remove alone.
    more.onclick = () => dropdownMenu(more, () => (s.error
      ? [{ label: 'Remove…', onclick: () => removeSource(s) }]
      : tableMenuItems(s.id)));
    r.append(more);
    return r;
  }

  function groupHead(g) {
    const h = el('div', 'tables-group');
    h.append(el('span', 'tables-group-name', g.label));
    const open = g.tables.filter((s) => s.is_open).length;
    const rows = g.tables.reduce((n, s) => n + (s.row_count || 0), 0);
    h.append(el('span', 'tables-group-meta', `${plural(g.tables.length, 'table')} · ${rows.toLocaleString()} rows · ${open} open`));
    // A header set held by several tables is what the merge builder is
    // for; the group offers it with those tables ticked — the imported
    // ones, since a saved table's rows are already in the table it came from.
    const real = g.tables.filter((s) => !s.error && !s.origin);
    if (g.kind && real.length > 1) {
      const m = el('button', 'btn ghost mb-small', 'Merge…');
      m.title = `Merge these ${real.length} tables into one`;
      m.onclick = () => openMergeBuilder({ preselect: real.map((s) => s.id) });
      h.append(m);
    }
    return h;
  }

  function paint() {
    const q = tablesView.query.trim().toLowerCase();
    const all = S.sources || [];
    const shown = all.filter((s) => !q || `${sourceLabel(s)} ${s.name || ''}`.toLowerCase().includes(q));
    const open = all.filter((s) => s.is_open).length;
    count.textContent = q ? `${shown.length} of ${all.length} tables` : `${open} of ${all.length} open`;
    for (const b of head.querySelectorAll('.tables-sort')) b.setAttribute('aria-pressed', String(b.dataset.sort === tablesView.sort));
    keepingFocus(list, () => {
      const nodes = [];
      for (const g of groupedTables(shown)) {
        if (g.label) nodes.push(groupHead(g));
        for (const s of g.tables) nodes.push(row(s));
      }
      if (!nodes.length) nodes.push(el('p', 'fb-help tables-empty', all.length ? `No table matches “${tablesView.query.trim()}”.` : 'No tables in this case yet — Import… adds some.'));
      list.replaceChildren(...nodes);
    });
    const tagged = taggedTablesToOpen().length;
    openTagged.textContent = tagged ? `Open all tagged · ${tagged}` : 'Open all tagged';
    openTagged.disabled = !tagged;
    openTagged.title = tagged ? `Open the ${plural(tagged, 'closed table')} with tagged rows` : 'Every table with tagged rows is open';
    closeAll.disabled = !open;
  }
  search.oninput = () => { tablesView.query = search.value; paint(); };
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ''; tablesView.query = ''; paint(); }
  });
  paint();
  return { update: paint };
}

/* "Merges & saved tables": the tables made out of other tables, with what
   each is made of. A merge lists its tables (which is also why they can't
   be removed while it exists); a saved table says what it was saved from
   — a view of a table, or a SQL pane query. The Indexes pane's shape:
   figures and what they mean on top, then the box that scrolls. */
export function buildMergesPane(pane) {
  const summary = el('div', 'mb-summary');
  const stats = el('div', 'mb-stats');
  summary.append(stats);
  const notes = el('ul', 'mb-notes');
  notes.append(
    el('li', 'mb-note', 'A merge reads several tables as one grid, matched by column name. Removing it leaves its tables as they are; a table can\u2019t be removed while a merge is built on it.'),
    el('li', 'mb-note', 'A saved table keeps the rows a filter or a SQL pane query found, as a table of its own. Tags and notes on it are its own: none were copied, and none write back.'));
  pane.append(summary, notes);

  const box = el('div', 'side-preview tm-cols-box tables-box tables-made');
  const scroller = el('div', 'side-preview-rows');
  const list = el('div', 'tables-list');
  scroller.append(list);
  const foot = el('div', 'side-preview-foot tm-vf-foot');
  const footText = el('p', 'fb-help');
  const newMerge = el('button', 'btn ghost', 'Merge tables…');
  newMerge.onclick = () => openMergeBuilder();
  foot.append(footText, newMerge);
  box.append(scroller, foot);
  pane.append(box);

  const madeRow = (s, sub, details = []) => {
    const r = el('div', 'tables-made-row');
    r.dataset.id = String(s.id);
    const line = el('div', 'tables-made-line');
    const name = el('button', 'tables-name', sourceGlyph(s) + sourceLabel(s));
    name.type = 'button';
    name.dataset.role = 'name';
    name.title = sourceTitle(s, s.error ? undefined : 'Click to go to it');
    name.disabled = !!s.error;
    name.onclick = () => goTo(s);
    line.append(name, el('span', 'tables-made-sub' + (s.error ? ' warn' : ''), sub));
    const more = el('button', 'btn ghost row-more tables-more', '⋯');
    more.dataset.role = 'more';
    more.setAttribute('aria-label', `Actions for ${sourceLabel(s)}`);
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-expanded', 'false');
    more.onclick = () => dropdownMenu(more, () => (s.error
      ? [{ label: 'Remove…', onclick: () => removeSource(s) }] : tableMenuItems(s.id)));
    line.append(more);
    r.append(line);
    if (details.length) {
      const d = el('div', 'tables-made-details');
      d.append(...details);
      r.append(d);
    }
    return r;
  };
  const member = (t, right) => {
    const line = el('div', 'tables-member');
    line.append(el('span', 'tables-member-name', sourceGlyph(t) + sourceLabel(t)), el('span', 'tables-member-rows', right));
    return line;
  };
  const groupHead = (label, meta) => {
    const h = el('div', 'tables-group');
    h.append(el('span', 'tables-group-name', label), el('span', 'tables-group-meta', meta));
    return h;
  };

  function paint() {
    const all = S.sources || [];
    const merges = all.filter((s) => s.is_merge).sort((a, b) => sourceLabel(a).localeCompare(sourceLabel(b)));
    const saved = all.filter((s) => !s.is_merge && (s.origin === 'subset' || s.origin === 'sql'))
      .sort((a, b) => sourceLabel(a).localeCompare(sourceLabel(b)));
    const inMerges = new Set(merges.flatMap((m) => m.member_source_ids || []));
    stats.replaceChildren(tableMenuStat(merges.length, merges.length === 1 ? 'merge' : 'merges'),
      tableMenuStat(inMerges.size, 'tables in them'),
      tableMenuStat(saved.length, saved.length === 1 ? 'saved table' : 'saved tables'));
    keepingFocus(list, () => {
      const nodes = [groupHead('Merges', merges.length ? plural(merges.length, 'merge') : '')];
      if (!merges.length) nodes.push(el('p', 'fb-help tables-empty', 'None yet. Merge tables… below makes one — every host\u2019s event logs in one grid, say.'));
      for (const m of merges) {
        const members = (m.member_source_ids || []).map((id) => all.find((s) => s.id === id)).filter(Boolean);
        const sub = m.error ? m.error
          : `${(m.row_count || 0).toLocaleString()} rows · ${plural(members.length, 'table')}`
            + (m.tagged_row_count ? ` · ${m.tagged_row_count.toLocaleString()} tagged` : '');
        nodes.push(madeRow(m, sub, members.map((t) => member(t, `${(t.row_count || 0).toLocaleString()} rows`))));
      }
      nodes.push(groupHead('Saved tables', saved.length ? plural(saved.length, 'table') : ''));
      if (!saved.length) nodes.push(el('p', 'fb-help tables-empty', 'None yet. Filters ▾ → Save this view as a table… keeps the rows a filter has found.'));
      for (const s of saved) {
        const meta = s.origin_meta || {};
        if (s.origin === 'sql') {
          const q = el('code', 'tables-made-sql', String(meta.sql || '').replace(/\s+/g, ' ').trim());
          q.title = meta.sql || '';
          nodes.push(madeRow(s, `${(s.row_count || 0).toLocaleString()} rows · from a SQL pane query`, [q]));
          continue;
        }
        const how = meta.selection === 'picks' ? 'picked rows' : 'a filtered view';
        const of = meta.parent_row_count != null ? ` of ${Number(meta.parent_row_count).toLocaleString()}` : '';
        const parent = all.find((t) => t.id === meta.parent_source_id && t.name === meta.parent_name);
        const from = parent ? member(parent, `${(parent.row_count || 0).toLocaleString()} rows`)
          : el('div', 'tables-member', subsetParentLabel(s));
        nodes.push(madeRow(s, `${(s.row_count || 0).toLocaleString()}${of} rows · saved from ${how}`
          + (meta.created_at ? ` · ${meta.created_at.replace('T', ' ')}` : ''), [from]));
      }
      list.replaceChildren(...nodes);
    });
    footText.textContent = merges.length || saved.length
      ? `${plural(merges.length + saved.length, 'table')} made from other tables.` : 'Nothing made from other tables yet.';
  }
  paint();
  return { update: paint };
}

/* source_id -> [{column, building}], for the Indexes pane. Fetched when the
   manager opens rather than joined onto /api/sources: it's one
   sqlite_master read per table, and the manager re-polls /api/sources
   every 1.5 s, which would make that an N+1 on every tick. */
export const indexesBySource = new Map();

export async function refreshColumnIndexes() {
  const real = realTables();
  const results = await Promise.all(real.map((s) =>
    api(`/api/column_indexes?source_id=${s.id}`).catch(() => [])));
  indexesBySource.clear();
  real.forEach((s, i) => indexesBySource.set(s.id, results[i]));
}

/* What the server says about search indexes on this machine: whether its
   SQLite can use one at all (store.py TRIGRAM_LIKE_MIN_SQLITE). Asked once
   per page; the answer is the Python the server runs on. */
export let searchIndexSupport = null;

/* "Indexes": both kinds a case grows without being asked, one table a row.
   The search index is what makes Contains and Advanced searches quick;
   filter indexes are what filtering a column leaves behind. Each was a
   chip on every row of the old one-list manager, which said "index ready"
   thirteen times to tell the analyst nothing. */
export function buildIndexesPane(pane) {
  const changed = () => pane.dispatchEvent(new CustomEvent('tables:changed', { bubbles: true }));
  const summary = el('div', 'mb-summary');
  const stats = el('div', 'mb-stats');
  summary.append(stats);
  const notes = el('ul', 'mb-notes');
  pane.append(summary, notes);

  const box = el('div', 'side-preview tm-cols-box tables-box tables-indexes');
  const head = el('div', 'tables-head');
  head.append(el('span', 'tables-name-h', 'Table'), el('span', 'tables-si-h', 'Search index'), el('span', 'tables-fi-h', 'Filter indexes'));
  const scroller = el('div', 'side-preview-rows');
  const list = el('div', 'tables-list');
  scroller.append(list);
  const foot = el('div', 'side-preview-foot tm-vf-foot');
  const footText = el('p', 'fb-help');
  const dropAll = el('button', 'btn ghost', 'Drop all filter indexes');
  foot.append(footText, dropAll);
  box.append(head, scroller, foot);
  pane.append(box);

  const acts = el('div', 'row-actions');
  const buildAll = el('button', 'btn ghost', 'Build missing search indexes');
  acts.append(buildAll);
  pane.append(acts);

  async function build(targets) {
    setBusy(true);
    try {
      for (const s of targets) await post(`/api/source/${s.id}/search_index`, {});
      await refreshSourcesQuietly();
    } catch (e) { toast('Could not start the search index: ' + e.message, 6000); }
    finally { setBusy(false); }
    paint();
    changed();
  }
  async function drop(s, columns) {
    setBusy(true);
    try {
      for (const column of columns) {
        await api(`/api/column_indexes?source_id=${s.id}&column=${encodeURIComponent(column)}`, { method: 'DELETE' });
      }
    } catch (e) { toast('Could not drop index: ' + e.message, 4000); }
    finally { setBusy(false); }
    await refreshColumnIndexes();
    paint();
    changed();
  }
  buildAll.onclick = () => build(realTables().filter((s) => !s.has_fts && !s.fts_building));
  dropAll.onclick = async () => {
    const withIdx = realTables().filter((s) => (indexesBySource.get(s.id) || []).some((ix) => !ix.building));
    const n = withIdx.reduce((k, s) => k + (indexesBySource.get(s.id) || []).filter((ix) => !ix.building).length, 0);
    if (!(await confirmDialog(`Drop all ${plural(n, 'filter index', 'filter indexes')}? Filters still work; the next filter on each column builds its index again.`, { okLabel: 'Drop all' }))) return;
    for (const s of withIdx) await drop(s, (indexesBySource.get(s.id) || []).filter((ix) => !ix.building).map((ix) => ix.column));
    toast(`Dropped ${plural(n, 'filter index', 'filter indexes')}`);
  };

  function row(s) {
    const r = el('div', 'tables-row tables-ix-row');
    r.dataset.id = String(s.id);
    const name = el('span', 'tables-name-cell');
    const label = el('span', 'tables-name static', sourceGlyph(s) + sourceLabel(s));
    label.title = sourceTitle(s);
    name.append(label);
    const si = el('span', 'tables-si');
    const status = indexStatusFor(s);
    si.append(el('span', 'index-status index-status-' + status.cls, status.text.replace(' yet', '')));
    if (status.cls === 'idle' && searchIndexSupport && searchIndexSupport.supported) {
      const b = el('button', 'btn ghost mb-small tables-build', 'Build');
      b.dataset.role = 'build';
      b.title = 'Build it now in the background, rather than on the first search that needs it';
      b.onclick = () => build([s]);
      si.append(b);
    }
    const fi = el('span', 'tables-fi');
    const idxs = indexesBySource.get(s.id) || [];
    if (!idxs.length) fi.append(el('span', 'tables-none', '—'));
    for (const ix of idxs) {
      const chip = el('span', 'index-chip' + (ix.building ? ' building' : ''));
      chip.append(el('span', null, ix.column + (ix.building ? ' (building…)' : '')));
      if (!ix.building) {
        const x = el('button', 'index-chip-drop', '✕');
        x.dataset.role = `drop:${ix.column}`;
        x.title = `Drop the index on "${ix.column}" — filters still work, just by scanning`;
        x.setAttribute('aria-label', `Drop the filter index on ${ix.column}`);
        x.onclick = async () => { await drop(s, [ix.column]); toast(`Dropped the filter index on "${ix.column}"`); };
        chip.append(x);
      }
      fi.append(chip);
    }
    r.append(name, si, fi);
    return r;
  }

  function paint() {
    const real = realTables();
    const ready = real.filter((s) => s.has_fts).length;
    const building = real.filter((s) => !s.has_fts && s.fts_building).length;
    const idxCount = real.reduce((n, s) => n + (indexesBySource.get(s.id) || []).length, 0);
    const idxTables = real.filter((s) => (indexesBySource.get(s.id) || []).length).length;
    stats.replaceChildren(tableMenuStat(`${ready} of ${real.length}`, 'searchable'),
      ...(building ? [tableMenuStat(building, 'indexing now')] : []),
      tableMenuStat(idxCount, idxCount === 1 ? 'filter index' : 'filter indexes'));
    const unsupported = searchIndexSupport && !searchIndexSupport.supported;
    notes.replaceChildren(
      el('li', 'mb-note', unsupported
        ? `This Winnow runs on SQLite ${searchIndexSupport.sqlite}, which can't use a search index (it needs 3.45 or later, which Python 3.12 and later ship). Contains and Advanced searches scan every row instead: the same results, more slowly.`
        : 'Contains and Advanced searches use a table’s search index once it is ready. Until then they scan the table: the same results, more slowly. The first search that needs one starts it.'),
      el('li', 'mb-note', 'Filtering, grouping or opening the value picker on a column builds a small filter index for it. Drop them if they add up; the next filter on that column builds it again.'));
    keepingFocus(list, () => {
      list.replaceChildren(...(real.length ? real.slice().sort((a, b) => sourceLabel(a).localeCompare(sourceLabel(b))).map(row)
        : [el('p', 'fb-help tables-empty', 'No tables in this case yet.')]));
    });
    footText.textContent = idxCount ? `${plural(idxCount, 'filter index', 'filter indexes')} on ${plural(idxTables, 'table')}.` : 'No filter indexes.';
    dropAll.disabled = !real.some((s) => (indexesBySource.get(s.id) || []).some((ix) => !ix.building));
    const missing = real.filter((s) => !s.has_fts && !s.fts_building).length;
    buildAll.hidden = !missing || unsupported;
    buildAll.textContent = `Build ${missing === 1 ? 'the missing search index' : `${missing} missing search indexes`}`;
  }
  paint();
  if (!searchIndexSupport) {
    api('/api/search_index').then((r) => { searchIndexSupport = r; if (pane.isConnected) paint(); }).catch(() => {});
  }
  return { update: paint };
}

export function tablesHint() {
  const all = S.sources || [];
  return `${all.filter((s) => s.is_open).length} of ${plural(all.length, 'table')} open`;
}

export function mergesHint() {
  const merges = (S.sources || []).filter((s) => s.is_merge).length;
  const saved = (S.sources || []).filter((s) => !s.is_merge && (s.origin === 'subset' || s.origin === 'sql')).length;
  if (!merges && !saved) return 'None yet';
  return [merges && plural(merges, 'merge'), saved && `${saved} saved`].filter(Boolean).join(' · ');
}

export function indexesHint() {
  const real = realTables();
  const ready = real.filter((s) => s.has_fts).length;
  const idx = real.reduce((n, s) => n + (indexesBySource.get(s.id) || []).length, 0);
  return `${ready} of ${real.length} searchable` + (idx ? ` · ${idx} filter` : '');
}

/* The manager's sections: a registry like the table menu's, for the same
   reason — case-wide table features are expected to land here. */
export const TABLES_MANAGER_SECTIONS = [
  { id: 'tables', group: 'Tables', title: 'Every table', hint: tablesHint, build: buildTablesPane },
  { id: 'merges', group: 'Tables', title: 'Merges & saved tables', hint: mergesHint, build: buildMergesPane },
  { id: 'indexes', group: 'Upkeep', title: 'Indexes', hint: indexesHint, build: buildIndexesPane },
];

/* Every table in the case, open or closed — the counterpart to the tab
   strip's nondestructive ✕, in the shape of the table menu: its sections
   down a side list, the chosen one on the right in a frame of a fixed
   height, and along the foot what the manager is opened for besides
   (importing more, the case's columns as SQL). `t` opens it, and the
   table menu's foot has a way back to it.

   It polls: a search index builds in the background, and a table
   imported, opened or closed elsewhere should show up here. It refetches
   S.sources on a plain timer rather than through loadSources() (which
   re-selects the open tab, resetting its filters, as a side effect), and
   repaints the pane in place — never under an open ⋯ menu, which a
   repaint would leave hanging off a button that is gone. */
export function openTablesManager(section = 'tables') {
  markModalAction('openTables');
  modal('Tables', (b) => {
    b.dataset.kind = 'tables';
    const frame = sectionPanes({
      sections: TABLES_MANAGER_SECTIONS, label: 'Tables manager sections',
      cls: 'tables-manager', paneCls: 'tables-manager-pane', changedEvent: 'tables:changed',
    });
    b.append(frame.panes);

    const foot = el('div', 'table-menu-foot table-menu-strip');
    const imp = el('button', 'btn ghost', 'Import…');
    imp.onclick = () => openImportModal();
    const copySchema = el('button', 'btn ghost table-menu-close', 'Copy table definitions');
    copySchema.title = 'Copy every table’s columns as SQL — paste into an LLM prompt to help write a SQL pane query';
    copySchema.onclick = () => {
      const real = realTables();
      if (!real.length) { toast('No tables to copy'); return; }
      writeClipboardText(Promise.resolve(sqlSchemaForLLM()), `Copied ${real.length} table definition${real.length === 1 ? '' : 's'}`);
    };
    foot.append(imp, copySchema);
    b.append(foot);
    frame.show(section);

    // What the panes draw from, as one string, so the poll can tell a tick
    // that changed nothing from one that did.
    const paintedSig = () => JSON.stringify([S.sources, S.folders, [...indexesBySource]]);
    let lastSig = paintedSig();
    refreshColumnIndexes().then(() => { lastSig = paintedSig(); frame.update(); }).catch(() => {});

    if (tablesModalPoll) clearInterval(tablesModalPoll);
    tablesModalPoll = setInterval(async () => {
      if ($('modal').hidden || $('modalBody').dataset.kind !== 'tables') {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      try {
        // The folders too: Folders groups by the sidebar's tree, which
        // the analyst can rearrange with the manager open beside it.
        const [, folders] = await Promise.all([refreshSourcesQuietly(), api('/api/folders')]);
        S.folders = folders;
        // Only re-read the filter indexes while one is mid-build — every
        // tick would otherwise be one query per table to re-read a list
        // that only changes when the analyst acts.
        if ([...indexesBySource.values()].some((l) => l.some((ix) => ix.building))) await refreshColumnIndexes();
      } catch {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      if (openMenuAnchor && frame.panes.contains(openMenuAnchor)) return;
      const sig = paintedSig();
      if (sig === lastSig) return;
      lastSig = sig;
      frame.update();
    }, 1500);
  }, { wide: 'x', tall: true, focus: (b) => b.querySelector('.side-rail-item[aria-selected="true"]') });
}

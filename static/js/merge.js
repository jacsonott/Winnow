/* The merge builder — one table across several, matched by column name.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, el, post, toast } from './core.js';
import { startJobsPoll, uploadWithProgress } from './jobs.js';
import { loadSources, sourceLabel, sourceTitle } from './sources.js';
import { S } from './state.js';
import { nicknameFor } from './savedfilters.js';
import { modal, oneAtATime } from './ui.js';

/* ------------------------------------------------------------------ merge */

/* A table's header set as the builder groups by it: base column names,
   case-folded, sorted. Derived columns are left out because the server
   leaves them out of matching too — adding one to a table used to move it
   into a group of its own, and the builder then said nothing matched. */
export function columnGroupKey(columns) {
  return columns.filter((c) => !c.derived).map((c) => c.name.trim().toLowerCase()).sort().join('|');
}

const baseCols = (s) => s.columns.filter((c) => !c.derived);
const colKeys = (s) => new Set(baseCols(s).map((c) => c.name.trim().toLowerCase()));
const fmt = (n) => n.toLocaleString();
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const listNames = (arr) => (arr.length <= 1 ? (arr[0] || '') : `${arr.slice(0, -1).join(', ')} and ${arr[arr.length - 1]}`);

/* The merge builder: tables on the left, grouped by header set, each with
   a chip saying how it fits what's ticked; the merge it would make on the
   right. The right side is the server's own preview (/api/merges/preview
   runs the engine's column rule), always asked for with every column kept:
   "only shared columns" is that list minus the partial ones, which lets
   the Columns tab show what the switch leaves out.

   `opts.preselect` ticks tables up front (the table menu's "Merge with
   other tables…"), and the groups that fit them best are listed first.
   `opts.edit` is a merge to change in place — PUT keeps its id, so its
   tab, layout and saved views stay with it. */
export function openMergeBuilder(opts = {}) {
  const edit = opts.edit || null;
  const real = S.sources.filter((s) => !s.is_merge && !s.error);
  const byId = new Map(real.map((s) => [s.id, s]));
  const state = {
    sel: [...new Set(edit ? edit.member_source_ids : opts.preselect || [])].filter((id) => byId.has(id)),
    mode: edit && edit.col_mode === 'shared' ? 'shared' : 'all',
    srcCol: edit ? !!edit.source_column : true,
    filter: '',
    nameTouched: !!edit,
    preview: null,
    previewError: '',
    seq: 0,
  };
  // Lowercased, like the server's case-insensitive uniqueness rule — the
  // clash is a hint under the box rather than a failed request.
  const takenNames = new Set(S.sources.filter((s) => s.is_merge && (!edit || s.id !== edit.id))
    .map((s) => s.name.toLowerCase()));

  /* Header-set groups, ordered once: re-sorting on every tick would move
     the row under the pointer. */
  const groupList = (() => {
    const m = new Map();
    for (const s of real) {
      const key = columnGroupKey(s.columns);
      if (!m.has(key)) m.set(key, { key, tables: [], cols: baseCols(s).map((c) => c.name) });
      m.get(key).tables.push(s);
    }
    const out = [...m.values()];
    if (state.sel.length) {
      const anchor = new Set();
      for (const id of state.sel) for (const k of colKeys(byId.get(id))) anchor.add(k);
      const score = (g) => (g.tables.some((t) => state.sel.includes(t.id)) ? Infinity
        : g.cols.filter((c) => anchor.has(c.trim().toLowerCase())).length / Math.max(g.cols.length, anchor.size));
      out.sort((a, b) => score(b) - score(a));
    }
    return out;
  })();

  const groupLabel = (g) => nicknameFor(g.cols)
    || g.cols.slice(0, 3).join(', ') + (g.cols.length > 3 ? ` +${g.cols.length - 3} more` : '');

  /* How a table fits the others ticked — computed here, from S.sources,
     so the chips answer instantly while the preview is on its way. */
  function fitChip(t) {
    const others = state.sel.filter((id) => id !== t.id).map((id) => byId.get(id));
    const tk = colKeys(t);
    if (!others.length) return [plural(tk.size, 'column'), 'dim'];
    const union = new Set();
    for (const o of others) for (const k of colKeys(o)) union.add(k);
    const common = [...tk].filter((k) => union.has(k)).length;
    const extra = tk.size - common;
    if (!common) return ['no shared columns', 'warn'];
    if (state.sel.includes(t.id)) {
      const all = new Set([...union, ...tk]);
      return tk.size === all.size ? ['all columns', 'ok'] : [`${tk.size} of ${all.size} columns`, 'info'];
    }
    if (!extra && common === union.size) return ['same columns', 'ok'];
    if (!extra) return [`${common} of ${union.size} columns`, 'info'];
    if (common === union.size) return [`+${plural(extra, 'column')}`, 'info'];
    return [`${common} shared · ${extra} new`, 'info'];
  }

  /* The preview, reshaped for rendering: `cols` is every column with who
     has it, `kept` the ones this mode keeps (indices into the preview's
     row cells). */
  function result() {
    const pv = state.preview;
    if (!pv) return null;
    const n = pv.members.length;
    const cols = pv.columns.map((c, i) => {
      const owners = pv.members.filter((m) => c.in[m.id]);
      const partial = !c.source_column && owners.length < n;
      const types = new Set(owners.map((m) => c.in[m.id].type));
      return { ...c, i, owners, partial, conflict: types.size > 1, dropped: state.mode === 'shared' && partial };
    });
    return { members: pv.members, cols, kept: cols.filter((c) => !c.dropped), rows: pv.rows };
  }

  /* Re-ask the server, then render. The list and the row/table counts
     don't wait for it; everything that reads columns does. */
  const fetchPreview = debounce(async () => {
    const seq = ++state.seq;
    try {
      const pv = await post('/api/merges/preview', { source_ids: state.sel, col_mode: 'all', source_column: state.srcCol });
      if (seq !== state.seq) return;
      state.preview = pv;
      state.previewError = '';
    } catch (e) {
      if (seq !== state.seq) return;
      state.preview = null;
      state.previewError = e.message;
    }
    render();
  }, 120);

  let list, statsBox, seg, notesBox, panelCols, panelRows, nameInput, nameHint, createBtn;

  modal(edit ? `Edit merge — ${edit.name}` : 'Merge tables', (b) => {
    if (real.length < 2) {
      b.append(el('p', null, 'Import at least two tables to merge them.'));
      return;
    }
    const root = el('div', 'mb');
    const panes = el('div', 'mb-panes');

    const left = el('div', 'mb-pane');
    const filter = el('input', 'mb-filter');
    filter.type = 'search';
    filter.placeholder = 'Filter tables…';
    filter.setAttribute('aria-label', 'Filter tables');
    filter.oninput = () => { state.filter = filter.value; renderList(); };
    list = el('div', 'mb-list');
    left.append(el('div', 'mb-head', 'Tables in this case'), filter, list);

    const right = el('div', 'mb-pane mb-result');
    const summary = el('div', 'mb-summary');
    statsBox = el('div', 'mb-stats');
    seg = el('div', 'mb-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Which columns to keep');
    for (const [mode, label] of [['all', 'Keep every column'], ['shared', 'Only shared columns']]) {
      const btn = el('button', null, label);
      btn.type = 'button';
      btn.dataset.mode = mode;
      btn.onclick = () => { state.mode = mode; render(); };
      seg.append(btn);
    }
    summary.append(statsBox, seg);
    notesBox = el('ul', 'mb-notes');
    const tabs = el('div', 'mb-tabs');
    tabs.setAttribute('role', 'tablist');
    panelCols = el('div', 'mb-scroll');
    panelRows = el('div', 'mb-scroll');
    panelRows.hidden = true;
    const tabBtns = [['Columns', panelCols], ['Sample rows', panelRows]].map(([label, panel]) => {
      const t = el('button', null, label);
      t.type = 'button';
      t.setAttribute('role', 'tab');
      t.onclick = () => {
        for (const [tb, p] of tabBtns) { tb.setAttribute('aria-selected', String(p === panel)); p.hidden = p !== panel; }
      };
      tabs.append(t);
      return [t, panel];
    });
    tabBtns[0][0].setAttribute('aria-selected', 'true');
    tabBtns[1][0].setAttribute('aria-selected', 'false');
    right.append(summary, notesBox, tabs, panelCols, panelRows);
    panes.append(left, right);

    const foot = el('div', 'mb-foot');
    const field = el('div', 'mb-field');
    const nameLabel = el('label', null, 'Name');
    nameInput = el('input');
    nameInput.type = 'text';
    nameInput.id = 'mbName';
    nameInput.autocomplete = 'off';
    nameLabel.htmlFor = 'mbName';
    if (edit) {
      nameInput.value = edit.name;
      // A name the builder suggested ("… · 3 tables") keeps following the
      // tables; one the analyst typed stays put.
      state.nameTouched = edit.name !== suggestedName();
    }
    nameInput.oninput = () => { state.nameTouched = nameInput.value.trim() !== ''; renderFoot(); };
    nameInput.onkeydown = (e) => { if (e.key === 'Enter' && !createBtn.disabled) createBtn.click(); };
    nameHint = el('div', 'mb-hint');
    field.append(nameLabel, nameInput, nameHint);
    createBtn = el('button', 'btn mb-create');
    createBtn.type = 'button';
    // One request per click: a double-click (or Enter twice) must not
    // create the merge twice and then fail on its own name.
    createBtn.onclick = oneAtATime(createBtn, submit);
    const check = el('label', 'mb-check');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = state.srcCol;
    cb.onchange = () => { state.srcCol = cb.checked; refresh(); };
    const srcText = el('span', null, 'Add a ');
    srcText.append(el('b', null, 'Source table'), document.createTextNode(' column that says which table each row came from'));
    check.append(cb, srcText);
    foot.append(field, createBtn, check);

    root.append(panes, foot);
    b.append(root);
    refresh();
  }, { wide: 'x', focus: '.mb-filter' });

  function renderList() {
    list.replaceChildren();
    const q = state.filter.trim().toLowerCase();
    for (const g of groupList) {
      const tables = g.tables.filter((t) => !q || sourceLabel(t).toLowerCase().includes(q) || t.name.toLowerCase().includes(q));
      if (!tables.length) continue;
      const wrap = el('div', 'mb-group');
      const head = el('div', 'mb-ghead');
      const gn = el('div', 'mb-gname', groupLabel(g));
      gn.title = g.cols.join(', ');
      gn.append(el('span', 'mb-gmeta', plural(g.cols.length, 'col')));
      head.append(gn);
      if (tables.length > 1) {
        const all = tables.every((t) => state.sel.includes(t.id));
        const btn = el('button', 'btn ghost mb-small', all ? 'Clear' : 'Select all');
        btn.type = 'button';
        btn.onclick = () => {
          for (const t of tables) {
            const at = state.sel.indexOf(t.id);
            if (all && at >= 0) state.sel.splice(at, 1);
            else if (!all && at < 0) state.sel.push(t.id);
          }
          refresh();
        };
        head.append(btn);
      }
      wrap.append(head);
      for (const t of tables) {
        const row = el('label', 'mb-trow');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = state.sel.includes(t.id);
        box.onchange = () => {
          // Tick order is member order: the first table ticked leads the
          // merge's column order (the engine's rule — see _merge_columns).
          const at = state.sel.indexOf(t.id);
          if (box.checked && at < 0) state.sel.push(t.id);
          if (!box.checked && at >= 0) state.sel.splice(at, 1);
          refresh();
        };
        const cell = el('span', 'mb-tcell');
        const nm = el('span', 'mb-tname', sourceLabel(t));
        cell.title = sourceTitle(t);
        cell.append(nm, el('span', 'mb-tsub', `${fmt(t.row_count)} rows`));
        const [txt, kind] = fitChip(t);
        row.append(box, cell, el('span', `mb-chip ${kind}`, txt));
        wrap.append(row);
      }
      list.append(wrap);
    }
    if (!list.childElementCount) list.append(el('p', 'mb-empty', 'No table matches that filter.'));
  }

  function refresh() {
    if (state.sel.length) fetchPreview();
    else { state.seq++; state.preview = null; state.previewError = ''; }
    render();
  }

  function render() {
    const res = state.sel.length && state.preview
      && state.preview.members.map((m) => m.id).join() === state.sel.join() ? result() : null;
    // The switch only means something when some columns are partial.
    if (res && !res.cols.some((c) => c.partial)) state.mode = 'all';
    renderList();
    renderStats(res);
    renderNotes(res);
    renderMatrix(res);
    renderRows(res);
    renderFoot(res);
  }

  function renderStats(res) {
    statsBox.replaceChildren();
    const stat = (v, l) => {
      const d = el('div', 'mb-stat');
      d.append(el('b', null, v), el('span', null, l));
      statsBox.append(d);
    };
    const n = state.sel.length;
    stat(String(n), n === 1 ? 'table' : 'tables');
    stat(fmt(state.sel.reduce((sum, id) => sum + byId.get(id).row_count, 0)), 'rows');
    stat(res ? String(res.kept.length) : '–', 'columns');
    seg.hidden = !res || !res.cols.some((c) => c.partial);
    for (const btn of seg.children) btn.setAttribute('aria-pressed', String(btn.dataset.mode === state.mode));
  }

  function renderNotes(res) {
    notesBox.replaceChildren();
    const add = (text, kind) => notesBox.append(el('li', kind || null, text));
    if (state.sel.length < 2) { add('Tick at least two tables to merge.'); return; }
    if (state.previewError) { add(`Couldn't preview this merge: ${state.previewError}`, 'warn'); return; }
    if (!res) return;
    const label = new Map(res.members.map((m) => [m.id, m.label]));
    for (const id of state.sel) {
      const union = new Set();
      for (const o of state.sel) if (o !== id) for (const k of colKeys(byId.get(o))) union.add(k);
      if (![...colKeys(byId.get(id))].some((k) => union.has(k))) {
        add(`${label.get(id)} shares no columns with the other tables, so its rows would sit in columns of their own. Check it belongs in this merge.`, 'warn');
      }
    }
    const partial = res.cols.filter((c) => c.partial);
    if (!partial.length) {
      add('Every table has the same columns.', 'ok');
    } else if (state.mode === 'all') {
      // Grouped by which tables have them: "HiddenRecord and SourceFile
      // are only in WS03" reads; one line per column wouldn't.
      const byOwners = new Map();
      for (const c of partial) {
        const k = c.owners.map((m) => m.id).join();
        if (!byOwners.has(k)) byOwners.set(k, { owners: c.owners, cols: [] });
        byOwners.get(k).cols.push(c.name);
      }
      // Said from whichever side is shorter: "only in WS03", or "not in
      // FS01" when it's most of the tables that have them.
      const parts = [...byOwners.values()].sort((a, b) => b.cols.length - a.cols.length);
      for (const p of parts.slice(0, 3)) {
        const cols = p.cols.length > 3 ? `${p.cols.slice(0, 3).join(', ')} and ${p.cols.length - 3} more` : listNames(p.cols);
        const many = p.cols.length > 1;
        const lacking = res.members.filter((m) => !p.owners.includes(m));
        add(p.owners.length <= lacking.length
          ? `${cols} ${many ? 'are' : 'is'} only in ${listNames(p.owners.map((m) => m.label))}. `
            + `Rows from the other tables leave ${many ? 'them' : 'it'} empty.`
          : `${listNames(lacking.map((m) => m.label))} ${lacking.length > 1 ? 'have' : 'has'} no ${cols}, `
            + `so ${lacking.length > 1 ? 'their' : 'its'} rows leave ${many ? 'them' : 'it'} empty.`);
      }
      if (parts.length > 3) {
        add(`${parts.slice(3).reduce((n, p) => n + p.cols.length, 0)} more columns are in only some tables. The Columns tab shows which.`);
      }
    } else {
      const names = partial.map((c) => c.name);
      add(`Leaves out ${plural(partial.length, 'column')} not every table has: `
        + `${names.length > 4 ? `${names.slice(0, 4).join(', ')} and ${names.length - 4} more` : listNames(names)}.`);
      if (!res.kept.some((c) => !c.source_column)) add('No column is in every table, so nothing would be left. Keep every column instead.', 'warn');
    }
    const respelled = new Map();
    for (const c of res.cols) {
      for (const m of c.owners) {
        if (c.in[m.id].name !== c.name) {
          if (!respelled.has(m.id)) respelled.set(m.id, []);
          respelled.get(m.id).push([c.in[m.id].name, c.name]);
        }
      }
    }
    for (const [id, pairs] of respelled) {
      add(pairs.length === 1
        ? `${label.get(id)}'s ${pairs[0][0]} is matched to ${pairs[0][1]}. Names are compared ignoring case.`
        : `${label.get(id)} spells ${pairs.length} columns differently (${listNames(pairs.slice(0, 3).map((p) => p[0]))}`
          + `${pairs.length > 3 ? ' and more' : ''}). They're matched ignoring case.`);
    }
    const conflicts = res.kept.filter((c) => c.conflict);
    for (const c of conflicts.slice(0, 3)) {
      add(`${c.name} isn't the same type in every table. The merge treats it as ${c.type}, as ${c.owners[0].label} does.`);
    }
    if (conflicts.length > 3) add(`${conflicts.length - 3} more columns differ in type between tables.`);
  }

  function emptyPanel(panel, text) {
    panel.replaceChildren(el('p', 'mb-empty', text));
  }

  function renderMatrix(res) {
    if (state.sel.length < 2) { emptyPanel(panelCols, 'Tick at least two tables to see the merged columns.'); return; }
    if (!res) { emptyPanel(panelCols, state.previewError ? '' : 'Loading…'); return; }
    const tbl = el('table', 'mb-mx');
    const hr = el('tr');
    hr.append(el('th', 'cn', 'Column'));
    for (const m of res.members) {
      const th = el('th', 'mb-mx-t');
      th.append(el('span', null, m.label));
      th.title = m.name;
      hr.append(th);
    }
    hr.append(el('th', null, 'In'));
    const thead = el('thead');
    thead.append(hr);
    const tb = el('tbody');
    for (const c of res.cols) {
      const tr = el('tr', c.source_column ? 'virtual' : (c.dropped ? 'dropped' : null));
      const cn = el('td', 'cn', c.name);
      if (c.source_column) cn.append(el('span', 'mb-added', 'added'));
      else cn.append(el('span', 'mb-ty', c.type));
      if (c.dropped) cn.title = 'Left out: not every table has this column';
      tr.append(cn);
      for (const m of res.members) {
        if (c.source_column) {
          tr.append(el('td', 'src', m.label));
          continue;
        }
        const have = c.in[m.id];
        const td = el('td', have ? 'dot' : 'gap', have ? (have.name !== c.name ? `● ${have.name}` : '●') : '—');
        td.title = have
          ? (have.name !== c.name ? `${m.label} calls it ${have.name}` : `${m.label} has ${c.name}`)
          : `${m.label} has no ${c.name}; its rows are empty here`;
        tr.append(td);
      }
      tr.append(el('td', c.partial ? 'cov part' : 'cov', c.partial ? `${c.owners.length}/${res.members.length}` : 'all'));
      tb.append(tr);
    }
    tbl.append(thead, tb);
    panelCols.replaceChildren(tbl);
  }

  function renderRows(res) {
    if (state.sel.length < 2) { emptyPanel(panelRows, 'Tick tables to see sample rows.'); return; }
    if (!res) { emptyPanel(panelRows, state.previewError ? '' : 'Loading…'); return; }
    if (!res.kept.length) { emptyPanel(panelRows, 'No columns left to show.'); return; }
    const label = new Map(res.members.map((m) => [m.id, m.label]));
    const tbl = el('table', 'mb-grid');
    const hr = el('tr');
    for (const c of res.kept) {
      const th = el('th', c.source_column ? 'src' : null, c.name);
      if (c.partial) {
        th.append(el('span', 'mb-part', `${c.owners.length}/${res.members.length}`));
        th.title = `${c.name}: only in ${listNames(c.owners.map((m) => m.label))}`;
      }
      hr.append(th);
    }
    const thead = el('thead');
    thead.append(hr);
    const tb = el('tbody');
    for (const r of res.rows) {
      const tr = el('tr');
      for (const c of res.kept) {
        const v = r.cells[c.i];
        const td = el('td', c.source_column ? 'src' : (v == null ? 'e' : null), v == null ? '' : String(v));
        if (v == null) td.title = `${label.get(r.source_id)} has no ${c.name}`;
        tr.append(td);
      }
      tb.append(tr);
    }
    tbl.append(thead, tb);
    panelRows.replaceChildren(el('div', 'mb-caption', 'The first rows of each table, as the merge would show them.'), tbl);
  }

  /* "Event logs (EvtxECmd) · 3 tables" when most of the ticked tables are
     one named header set, else "Merged view" — numbered past any name
     already taken, so leaving the box alone can't clash. */
  function suggestedName() {
    const counts = new Map();
    for (const id of state.sel) {
      const nick = nicknameFor(baseCols(byId.get(id)).map((c) => c.name));
      if (nick) counts.set(nick, (counts.get(nick) || 0) + 1);
    }
    let best = null;
    let n = 0;
    for (const [nick, c] of counts) if (c > n) { best = nick; n = c; }
    const base = best && n * 2 > state.sel.length ? `${best} · ${state.sel.length} tables` : 'Merged view';
    let name = base;
    for (let i = 2; takenNames.has(name.toLowerCase()); i++) name = `${base} ${i}`;
    return name;
  }

  function renderFoot(res = state.preview && result()) {
    const sug = suggestedName();
    nameInput.placeholder = sug;
    if (!state.nameTouched) nameInput.value = sug;
    const clash = takenNames.has(nameInput.value.trim().toLowerCase());
    nameHint.textContent = clash ? 'A merge with this name already exists. Pick another.' : '';
    const rows = state.sel.reduce((sum, id) => sum + byId.get(id).row_count, 0);
    // Not held for the preview: a slow or failed one shouldn't block the
    // merge, and the server checks everything the preview would.
    const nothingLeft = res && !res.kept.some((c) => !c.source_column);
    createBtn.disabled = state.sel.length < 2 || clash || nothingLeft;
    createBtn.textContent = state.sel.length < 2 ? 'Pick at least 2 tables'
      : `${edit ? 'Save changes' : 'Create merged table'} · ${fmt(rows)} rows`;
  }

  async function submit() {
    const name = nameInput.value.trim() || nameInput.placeholder;
    const body = { name, source_ids: state.sel, col_mode: state.mode, source_column: state.srcCol };
    try {
      const rec = edit
        ? await api(`/api/merges/${-edit.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        })
        : await post('/api/merges', body);
      $('modal').hidden = true;
      await loadSources(rec.id);
      toast(`${edit ? 'Updated' : 'Created'} merge "${rec.name}" · ${fmt(rec.row_count)} rows`);
    } catch (e) {
      toast(`${edit ? 'Saving the merge' : 'Merge'} failed: ${e.message}`, 6000);
    }
  }
}

/* ---------------------------------------------------------- import preview */

/* `src` is a queue item's transport: {file, name} for a browser pick, or
   {path, name} for one added from the server's own disk — previews and the
   direct-import button branch on which field is set, never on a guess.
   `name` is always present (queueItem and every direct caller set it), so
   nothing here reads src.file.name — one contract, one spelling. */
export function openImportPreview(src, opts = {}) {
  let preview = null;
  let columnTypes = opts.initial && opts.initial.column_types ? opts.initial.column_types.slice() : null;

  modal(`Import: ${src.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const delimSel = el('select');
    // 'lines' is not a delimiter: it switches the item to the raw-text
    // importer (one line per row, nothing split, no header) — the way to
    // force a .txt log, or a mis-sniffed .csv, into that path.
    for (const [label, val] of [['Auto-detect', ''], ['Comma', ','], ['Tab', '\t'], ['Semicolon', ';'], ['Pipe', '|'],
      ['Lines — one line per row (logs)', 'lines']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      delimSel.append(opt);
    }
    const initialKind = (opts.initial && opts.initial.kind) || src.kind;
    if (initialKind === 'text') delimSel.value = 'lines';
    else if (opts.initial && opts.initial.delimiter) delimSel.value = opts.initial.delimiter;
    const isLines = () => delimSel.value === 'lines';
    const headerLabel = el('label');
    const headerCb = el('input');
    headerCb.type = 'checkbox';
    headerCb.checked = opts.initial ? opts.initial.has_header !== false : true;
    headerLabel.append(headerCb, document.createTextNode(' First row is headers'));
    const syncHeader = () => { headerCb.disabled = isLines(); headerLabel.title = isLines() ? 'Raw text has no header row — every line is data' : ''; };
    syncHeader();
    controls.append(delimSel, headerLabel);
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      status.textContent = (isLines()
        ? `One line per row, nothing split · showing first ${preview.sample_rows.length} lines`
        : `Detected delimiter: ${JSON.stringify(preview.delimiter)} · showing first ${preview.sample_rows.length} rows`)
        + (preview.encoding === 'cp1252' ? ' · not UTF-8 — read as Windows-1252' : '');
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c));
        const typeSel = el('select');
        for (const ty of ['text', 'number', 'datetime']) {
          const opt = document.createElement('option');
          opt.value = ty; opt.textContent = ty;
          if (columnTypes[i] === ty) opt.selected = true;
          typeSel.append(opt);
        }
        typeSel.onchange = () => { columnTypes[i] = typeSel.value; };
        th.append(typeSel);
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // A path-queued item (the "Add from this machine…" picker) previews
        // in place; a browser-picked File uploads its sample. Which one is
        // a fact about how the item arrived, not a guess about the disk.
        const kind = isLines() ? 'text' : 'csv';
        if (src.path) {
          preview = await post('/api/ingest/preview/path', {
            path: src.path, kind,
            delimiter: isLines() ? null : (delimSel.value || null), has_header: headerCb.checked,
          });
        } else {
          const fd = new FormData();
          fd.append('file', src.file);
          fd.append('kind', kind);
          if (!isLines() && delimSel.value) fd.append('delimiter', delimSel.value);
          fd.append('has_header', headerCb.checked ? 'true' : 'false');
          preview = await api('/api/ingest/preview', { method: 'POST', body: fd });
        }
        if (!columnTypes || columnTypes.length !== preview.columns.length) columnTypes = preview.inferred_types.slice();
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    delimSel.onchange = () => { syncHeader(); columnTypes = null; refreshPreview(); };
    headerCb.onchange = refreshPreview;
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      // `kind` rides in the settings: choosing Lines turns a csv queue item
      // into a text one (and back), which is what the queue row shows.
      const settings = isLines()
        ? { kind: 'text', delimiter: null, has_header: false, column_types: null }
        : { kind: 'csv', delimiter: delimSel.value || null, has_header: headerCb.checked, column_types: columnTypes };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (src.path) { // added by path — import in place, no upload leg
        try {
          await post('/api/ingest/jobs/path', {
            path: src.path, name: src.name, kind: settings.kind,
            delimiter: settings.delimiter, has_header: settings.has_header,
            column_types: settings.column_types,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', src.file);
      fd.append('kind', settings.kind);
      if (settings.delimiter) fd.append('delimiter', settings.delimiter);
      fd.append('has_header', settings.has_header ? 'true' : 'false');
      if (settings.column_types) fd.append('column_types', JSON.stringify(settings.column_types));
      // Same background pipeline as the queue: transfer with progress, then
      // an ingest job the corner panel tracks.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, src.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* JSON/JSONL import — a single file, previewed live against whichever
   flatten mode is selected (same three modes store.py's ingest_json
   supports: don't flatten nested objects at all, flatten them without
   limit, or flatten only N levels deep — an array is never index-expanded
   at any depth/mode, always kept as its raw JSON text in one column, see
   _flatten_json's docstring for why). Single-file, not a queue — a JSON
   export is usually one file, unlike the CSV queue's "several files from
   the same collection tool" use case. */
export function openJsonImportPreview(src, opts = {}) {
  let preview = null;
  let flattenMode = (opts.initial && opts.initial.flatten_mode) || 'none';
  let flattenDepth = (opts.initial && opts.initial.flatten_depth) || 1;

  modal(`Import: ${src.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const modeSel = el('select');
    for (const [label, val] of [["Don't flatten", 'none'], ['Flatten completely', 'full'], ['Flatten to depth…', 'depth']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      if (val === flattenMode) opt.selected = true;
      modeSel.append(opt);
    }
    const depthInput = el('input');
    depthInput.type = 'number';
    depthInput.min = '1';
    depthInput.value = String(flattenDepth);
    depthInput.style.cssText = 'width:60px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    depthInput.hidden = flattenMode !== 'depth';
    controls.append(modeSel, depthInput);
    b.append(el('p', 'fb-help',
      'A nested object can be flattened into dotted columns (user.name); an array is always kept as its '
      + 'raw JSON text in one column, at any depth, since its length can vary record to record.'));
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      const clashes = preview.key_collisions || 0;
      status.textContent = `${preview.record_count.toLocaleString()} record${preview.record_count === 1 ? '' : 's'} `
        + `· showing first ${preview.sample_rows.length} · ${preview.columns.length} column${preview.columns.length === 1 ? '' : 's'}`
        // Two values for one column (a duplicated key, or a literal "a.b"
        // beside a nested a.b): both are kept, the later one suffixed.
        + (clashes ? ` · ${clashes.toLocaleString()} record${clashes === 1 ? ' has' : 's have'} two values for one key — the later kept as <key>_1` : '')
        + (preview.encoding === 'cp1252' ? ' · not UTF-8 — read as Windows-1252' : '');
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c), el('div', 'count', preview.inferred_types[i]));
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // The path branch matters most here: a .json document can't be
        // truncated, so the upload preview round-trips the WHOLE file — a
        // path-queued item previews in place instead.
        if (src.path) {
          preview = await post('/api/ingest/preview/path', {
            path: src.path, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
        } else {
          const fd = new FormData();
          fd.append('file', src.file);
          fd.append('flatten_mode', flattenMode);
          fd.append('flatten_depth', String(flattenDepth));
          preview = await api('/api/ingest/json/preview', { method: 'POST', body: fd });
        }
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    modeSel.onchange = () => {
      flattenMode = modeSel.value;
      depthInput.hidden = flattenMode !== 'depth';
      refreshPreview();
    };
    depthInput.oninput = debounce(() => {
      flattenDepth = Math.max(1, parseInt(depthInput.value, 10) || 1);
      refreshPreview();
    }, 300);
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      const settings = { flatten_mode: flattenMode, flatten_depth: flattenDepth };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (src.path) { // added by path — import in place, no upload leg
        try {
          await post('/api/ingest/jobs/path', {
            path: src.path, name: src.name, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', src.file);
      fd.append('kind', 'json');
      fd.append('flatten_mode', flattenMode);
      fd.append('flatten_depth', String(flattenDepth));
      // Same background pipeline as the queue.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, src.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* Mirrors store.py's DEFAULT_IMPORT_EXTENSIONS — one format list, two
   places it has to be spelled out (a browser can't read a Python
   constant). Shared by three things: openImportModal's file-picker accept
   attribute, the directory-import chips (order matters there — it's the
   order they render in), and wireFileDrop's own filtering below — a raw
   OS drop has no equivalent of a picker's accept attribute doing that
   filtering natively, so the drop handler has to do it itself. */
export const RECOGNIZED_IMPORT_EXTENSIONS = ['.csv', '.tsv', '.txt', '.psv', '.json', '.jsonl', '.ndjson'];

/* The SQLite extension set — routed to openSqliteTablePicker's configure
   step by the unified import queue, and recognized by wireFileDrop without
   a second hand-typed copy. */
export const SQLITE_IMPORT_EXTENSIONS = ['.db', '.sqlite', '.sqlite3', '.db-wal'];
/* Excel workbooks route the same way — which sheets to import is a
   per-file choice, so the queue sends these through a picker step too.
   .xlsm is the same zip container (macros are never executed); legacy
   binary .xls is out of scope (see xlsxread.py). */
export const XLSX_IMPORT_EXTENSIONS = ['.xlsx', '.xlsm'];
/* Plaso storage files import as one flat timeline table (no picker, no
   options) — see winnow/plasoread.py. */
export const PLASO_IMPORT_EXTENSIONS = ['.plaso'];
/* Evidence archives — ESXi support bundles, UAC collections, zipped log
   dirs. Routed to the expand-then-directory-import flow (server-side
   winnow/archive.py); .gz covers .tar.gz and bare rotated logs alike. */
export const ARCHIVE_IMPORT_EXTENSIONS = ['.zip', '.tar', '.tgz', '.gz', '.bz2', '.xz', '.tbz2', '.txz'];

export function extOf(filename) {
  const i = filename.lastIndexOf('.');
  return i === -1 ? '' : filename.slice(i).toLowerCase();
}

export function importKindFor(filename) {
  if (SQLITE_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'sqlite';
  if (XLSX_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'xlsx';
  if (PLASO_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'plaso';
  if (ARCHIVE_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'archive';
  const ext = extOf(filename).slice(1); // drop the leading '.' — json/jsonl/ndjson below are bare
  if (ext === 'json' || ext === 'jsonl' || ext === 'ndjson') return 'json';
  // Anything no built-in claims is raw text — one line per row (see
  // Store.ingest_text): auth.log.1, hostd.log, an extensionless dump.
  // Plugin formats are consulted before this by queueItem.
  return RECOGNIZED_IMPORT_EXTENSIONS.includes(extOf(filename)) ? 'csv' : 'text';
}

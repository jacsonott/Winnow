/* The guided AND/OR filter tree and its SQL round-trip.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, el, post, toast } from './core.js';
import { makePickerNode, parseFilter, setColumnFilter } from './filters.js';
import { checkPresets } from './savedfilters.js';
import { setGrouping } from './grouping.js';
import { S, normalizeTree } from './state.js';
import { currentFilterPayload, openSavedFiltersModal, tableMenuSeg, tableMenuStat, updateFiltersButton } from './timeframe.js';
import { baseColumns } from './tsformat.js';
import { datePickerButton, markModalAction, modal, promptDialog } from './ui.js';
import { rebuildView } from './view.js';

/* --------------------------------------------------------- filter builder */

/* Guided AND/OR condition tree, additive to the per-column quick filters.
   Tree shape: {type:'group', op:'AND'|'OR', children:[...]}
             | {type:'cond', column, op, value}   -- same op vocabulary as parseFilter
             | {type:'raw', sql}                  -- fallback when the SQL box can't round-trip
   The tree is compiled server-side by Store._compile_tree; 'raw' nodes are
   re-validated by Store.validate_where_fragment on every use, not just here. */

export const OP_LABELS = {
  contains: 'contains', not_contains: 'does not contain',
  equals: 'equals', not_equals: 'does not equal',
  starts: 'starts with', regex: 'matches regex',
  '>': '> (numeric)', '>=': '>= (numeric)', '<': '< (numeric)', '<=': '<= (numeric)',
  empty: 'is empty', not_empty: 'is not empty', in: 'is any of (one per line)',
};

export const OP_NO_VALUE = new Set(['empty', 'not_empty']);

export function sqlLit(v) { return "'" + String(v).replace(/'/g, "''") + "'"; }

export function sqlIdent(c) { return '"' + String(c).replace(/"/g, '""') + '"'; }

/* A LIKE pattern for contains/starts text, as the server compiles it
   (Store._compile_condition: LIKE ... ESCAPE '\'). Text holding a `%` or
   `_` has to escape them, or the SQL shown says something else — a contains
   on `a%b` read as `LIKE '%a%b%'`, which also matches `axxb`. Text without
   them is shown bare, the way it always was: there a backslash is already
   literal, as it is to SQLite with no ESCAPE clause. */
export function likeSql(c, text, { lead, not = false }) {
  const kw = not ? 'NOT LIKE' : 'LIKE';
  const s = String(text);
  if (!/[%_]/.test(s)) return `${c} ${kw} ${sqlLit((lead ? '%' : '') + s + '%')}`;
  const esc = s.replace(/[\\%_]/g, '\\$&');
  return `${c} ${kw} ${sqlLit((lead ? '%' : '') + esc + '%')} ESCAPE '\\'`;
}

export function serializeCond(node) {
  const c = sqlIdent(node.column);
  const v = node.value;
  // An empty contains/starts compiles to no clause at all server-side, so
  // it says nothing here either: `LIKE '%%'` would claim it drops NULLs,
  // and would not parse back as the blank condition it came from.
  if (['contains', 'not_contains', 'starts'].includes(node.op) && (v == null || v === '')) return '';
  switch (node.op) {
    case 'contains': return likeSql(c, v, { lead: true });
    case 'not_contains': return `(${likeSql(c, v, { lead: true, not: true })} OR ${c} IS NULL)`;
    case 'equals': return `${c} = ${sqlLit(v)}`;
    case 'not_equals': return `(${c} <> ${sqlLit(v)} OR ${c} IS NULL)`;
    case 'starts': return likeSql(c, v, { lead: false });
    case 'regex': return `${c} REGEXP ${sqlLit(v)}`;
    case '>': case '>=': case '<': case '<=': return `${c} ${node.op} ${sqlLit(v)}`;
    case 'empty': return `(${c} IS NULL OR ${c} = '')`;
    case 'not_empty': return `(${c} IS NOT NULL AND ${c} <> '')`;
    case 'in': {
      const items = Array.isArray(v) ? v : String(v || '').split('\n').map((x) => x.trim()).filter(Boolean);
      // An empty string in a list is the empty cell, NULL included, as the
      // server compiles it (Store._compile_condition) — the value picker
      // and the box's `a|""` put one there.
      const values = items.filter((x) => x !== '');
      const empty = values.length !== items.length ? `${c} IS NULL OR ${c} = ''` : '';
      const inList = values.length ? `${c} IN (${values.map(sqlLit).join(', ')})` : '';
      if (inList && empty) return `(${inList} OR ${empty})`;
      return inList || (empty ? `(${empty})` : '1');
    }
    default: return '1';
  }
}

export function serializeTree(node) {
  if (!node) return '';
  if (node.type === 'raw') return node.sql || '';
  if (node.type === 'cond') return node.column ? serializeCond(node) : '';
  if (node.type === 'group') {
    const parts = (node.children || []).map(serializeTree).filter(Boolean);
    if (!parts.length) return '';
    if (parts.length === 1) return parts[0];
    return '(' + parts.join(node.op === 'OR' ? ' OR ' : ' AND ') + ')';
  }
  return '';
}

export function tokenizeWhere(s) {
  const re = /"(?:[^"]|"")*"|'(?:[^']|'')*'|<>|>=|<=|[()<>=,]|\bAND\b|\bOR\b|\bIS\b|\bNOT\b|\bNULL\b|\bLIKE\b|\bREGEXP\b|\bIN\b|[A-Za-z_][A-Za-z0-9_]*/g;
  const toks = [];
  let last = 0, m;
  while ((m = re.exec(s))) {
    if (s.slice(last, m.index).trim() !== '') return null;
    toks.push(m[0]);
    last = m.index + m[0].length;
  }
  if (s.slice(last).trim() !== '') return null;
  return toks.length ? toks : null;
}

export const unquoteIdent = (t) => (t[0] === '"' ? t.slice(1, -1).replace(/""/g, '"') : t);

export const unquoteStr = (t) => t.slice(1, -1).replace(/''/g, "'");

export const isStrLit = (t) => !!t && t[0] === "'";

/* What a LIKE pattern means as a guided condition, or null when no guided
   condition means exactly that — the caller then keeps the SQL as raw,
   which runs as typed. Two shapes qualify, because they are what the
   server's own contains and starts-with compile to (Store._compile_condition:
   LIKE with `%` around the escaped text, ESCAPE '\', so the same
   ASCII-only case folding as any LIKE): `%text%` and `text%`, with text
   holding no wildcard of its own. A `_`, a `%` anywhere else (`%.exe`,
   `p%.exe`, `%p%exe%`) or no `%` at all — LIKE 'P1.EXE' is a
   case-insensitive equality, and equals is case-sensitive — has no guided
   twin. With an ESCAPE character, it and the character after it are that
   character, literally, as SQLite reads them. */
export function likePatternCond(column, pattern, escape = null) {
  const segs = []; // literal runs as strings, each unescaped `%` as null
  for (let i = 0; i < pattern.length; i++) {
    let ch = pattern[i];
    if (escape !== null && ch === escape) {
      if (++i >= pattern.length) return null; // a dangling escape matches nothing
      ch = pattern[i];
    } else if (ch === '_') {
      return null;
    } else if (ch === '%') {
      segs.push(null);
      continue;
    }
    if (typeof segs[segs.length - 1] === 'string') segs[segs.length - 1] += ch;
    else segs.push(ch);
  }
  const [a, b, c] = segs;
  if (segs.length === 3 && a === null && typeof b === 'string' && c === null) return { type: 'cond', column, op: 'contains', value: b };
  if (segs.length === 2 && typeof a === 'string' && b === null) return { type: 'cond', column, op: 'starts', value: a };
  return null;
}

/* Round-trips only the narrow subset serializeTree/serializeCond emit: the
   simple atomic ops (contains/starts/equals/regex/comparisons/in) plus
   AND/OR/paren grouping. The compound shapes for not_contains/not_equals/
   empty/not_empty — and anything else outside this subset — fall back to
   raw mode, which still works as a filter, it just won't populate the
   structured editor. A LIKE is in the subset only in the shapes
   likePatternCond accepts. */
export function parseWhereFragment(text) {
  const toks = tokenizeWhere(text.trim());
  if (!toks) return null;
  let pos = 0;
  const peek = () => toks[pos];

  function parseCond() {
    const colTok = toks[pos];
    if (!colTok || !/^[A-Za-z_"]/.test(colTok)) return null;
    const column = unquoteIdent(colTok);
    const op = toks[pos + 1];
    if (op === 'LIKE') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      let escape = null;
      let next = pos + 3;
      if (toks[next] === 'ESCAPE') {
        const e = toks[next + 1];
        if (!isStrLit(e) || unquoteStr(e).length !== 1) return null;
        escape = unquoteStr(e);
        next += 2;
      }
      const cond = likePatternCond(column, unquoteStr(lit), escape);
      if (!cond) return null;
      pos = next;
      return cond;
    }
    if (op === 'REGEXP') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      pos += 3;
      return { type: 'cond', column, op: 'regex', value: unquoteStr(lit) };
    }
    if (op === '=' || op === '>' || op === '>=' || op === '<' || op === '<=') {
      const lit = toks[pos + 2];
      if (!isStrLit(lit)) return null;
      pos += 3;
      return { type: 'cond', column, op: op === '=' ? 'equals' : op, value: unquoteStr(lit) };
    }
    if (op === 'IN') {
      if (toks[pos + 2] !== '(') return null;
      let p = pos + 3;
      const items = [];
      while (toks[p] && toks[p] !== ')') {
        if (isStrLit(toks[p])) items.push(unquoteStr(toks[p]));
        p++;
        if (toks[p] === ',') p++;
      }
      if (toks[p] !== ')') return null;
      pos = p + 1;
      return { type: 'cond', column, op: 'in', value: items };
    }
    return null;
  }

  function parseAtom() {
    if (peek() === '(') {
      pos++;
      const inner = parseOr();
      if (!inner || peek() !== ')') return null;
      pos++;
      return inner;
    }
    return parseCond();
  }
  function parseAnd() {
    const first = parseAtom();
    if (!first) return null;
    const children = [first];
    while (peek() === 'AND') { pos++; const n = parseAtom(); if (!n) return null; children.push(n); }
    return children.length === 1 ? children[0] : { type: 'group', op: 'AND', children };
  }
  function parseOr() {
    const first = parseAnd();
    if (!first) return null;
    const children = [first];
    while (peek() === 'OR') { pos++; const n = parseAnd(); if (!n) return null; children.push(n); }
    return children.length === 1 ? children[0] : { type: 'group', op: 'OR', children };
  }

  const tree = parseOr();
  if (!tree || pos !== toks.length) return null;
  return tree.type === 'group' ? tree : normalizeTree(tree);
}

export function hasActiveFilterTree() {
  if (S.filterTree.type === 'raw') return !!(S.filterTree.sql || '').trim();
  if (S.filterTree.type === 'cond') return true; // a bare-condition root IS a filter
  return !!(S.filterTree.children || []).length;
}

export function renderCondRow(node, onStructural, onPreview) {
  const row = el('div', 'fb-cond');
  if (!node.column && S.columns.length) node.column = S.columns[0].name;

  const colSel = el('select');
  for (const c of S.columns) {
    const opt = document.createElement('option');
    opt.value = c.name; opt.textContent = c.name;
    if (node.column === c.name) opt.selected = true;
    colSel.append(opt);
  }
  colSel.onchange = () => { node.column = colSel.value; onStructural(); };
  row.append(colSel);

  const opSel = el('select');
  for (const [op, label] of Object.entries(OP_LABELS)) {
    const opt = document.createElement('option');
    opt.value = op; opt.textContent = label;
    if (node.op === op) opt.selected = true;
    opSel.append(opt);
  }
  opSel.onchange = () => { node.op = opSel.value; onStructural(); };
  row.append(opSel);

  if (!OP_NO_VALUE.has(node.op)) {
    // 'is any of' really is one-per-line, so it needs a textarea — a
    // single-line input collapses the newlines and four EventIds render
    // as one run-together number.
    const inp = node.op === 'in' ? el('textarea') : el('input');
    if (node.op === 'in') {
      inp.rows = Math.min(6, Math.max(2, Array.isArray(node.value) ? node.value.length : 2));
      inp.spellcheck = false;
    }
    inp.value = Array.isArray(node.value) ? node.value.join('\n') : (node.value || '');
    inp.placeholder = node.op === 'in' ? 'one per line' : 'value';
    inp.oninput = () => {
      node.value = node.op === 'in' ? inp.value.split('\n').map((x) => x.trim()).filter(Boolean) : inp.value;
      onPreview();
    };
    row.append(inp);
    // A datetime column's value is a date string — offer the calendar
    // (datePickerButton dispatches 'input', so oninput above still fires).
    const colType = S.columns.find((c) => c.name === node.column)?.type;
    if (node.op !== 'in' && colType === 'datetime') row.append(datePickerButton(inp));
  }
  return row;
}

export function renderFilterGroup(node, onStructural, onPreview, isRoot) {
  const wrap = el('div', 'fb-group');
  const head = el('div', 'fb-group-head');
  // All / Any as the builders' segment, in words: "Match [AND] of:" read
  // as a sentence only to someone who already knew it meant AND.
  const op = tableMenuSeg([['AND', 'All', 'Every condition here has to hold (AND)'], ['OR', 'Any', 'One condition here is enough (OR)']],
    () => node.op, (key) => { node.op = key; onStructural(); }, 'fb-group-op');
  head.append(el('span', 'fb-group-label', isRoot ? 'Match' : 'A group matching'), op.seg,
    el('span', 'fb-group-label', isRoot ? 'of these' : 'of'));
  wrap.append(head);

  const list = el('div', 'fb-children');
  (node.children || []).forEach((child, i) => {
    const row = el('div', 'fb-row');
    row.append(child.type === 'group'
      ? renderFilterGroup(child, onStructural, onPreview, false)
      : renderCondRow(child, onStructural, onPreview));
    const rm = el('button', 'btn ghost fb-rm', '✕');
    rm.title = 'Remove';
    rm.onclick = () => { node.children.splice(i, 1); onStructural(); };
    row.append(rm);
    list.append(row);
  });
  wrap.append(list);

  const actions = el('div', 'fb-actions');
  const addCond = el('button', 'btn ghost mb-small', '+ Condition');
  addCond.onclick = () => {
    node.children = node.children || [];
    node.children.push({ type: 'cond', column: S.columns[0] ? S.columns[0].name : '', op: 'contains', value: '' });
    onStructural();
  };
  const addGroup = el('button', 'btn ghost mb-small', '+ Group');
  addGroup.onclick = () => {
    node.children = node.children || [];
    node.children.push({ type: 'group', op: 'AND', children: [] });
    onStructural();
  };
  actions.append(addCond, addGroup);
  wrap.append(actions);
  return wrap;
}

/* `editing` is a saved-filter record when this was opened from the Saved
   filters modal's Edit button (which applied that filter to the grid
   first, so the tree/sort/search below is already its payload and the row
   count behind the modal is real feedback). It only adds an "Update
   <name>" action that writes the current state back over that record —
   everything else, including "Save filter…" as a save-as-new escape
   hatch, behaves identically to a normal open. */
export function openFilterBuilder(editing = null) {
  markModalAction('openFilterBuilder');
  S.filterTree = normalizeTree(S.filterTree); // a cond root would render as an empty editor
  // Absorb the header boxes' quick filters into the tree, so the builder
  // opens showing EVERYTHING currently narrowing the grid — an applied
  // saved filter already lives in S.filterTree, but a `=H1` typed into a
  // column box (or written by the value picker) lived only in S.filters
  // and the builder opened looking empty. The condition MOVES rather than
  // copies (the absorbed box is cleared via setColumnFilter, which keeps
  // the visible input in step): both states describe the same rows, so
  // nothing needs rebuilding until Apply, and a copy would double every
  // condition on the next open. parseFilter's op vocabulary is a subset of
  // the builder's, so the translation is 1:1; raw-SQL mode can't hold a
  // structured condition, so quick filters stay put there.
  if (S.filterTree.type !== 'raw') {
    for (const [column, raw] of Object.entries(S.filters)) {
      const parsed = parseFilter(raw || '');
      if (!parsed) continue;
      S.filterTree.children = S.filterTree.children || [];
      // A list holding the empty value (`a|""`) arrives as the picker's
      // OR of `in` and `is empty`: the builder's one-per-line box drops a
      // blank line the moment it is edited.
      S.filterTree.children.push(parsed.op === 'in' && parsed.value.includes('')
        ? makePickerNode(column, parsed.value)
        : { type: 'cond', column, op: parsed.op, value: OP_NO_VALUE.has(parsed.op) ? '' : parsed.value });
      setColumnFilter(column, '');
    }
  }
  modal(editing ? `Edit filter — ${editing.name}` : 'Filter builder', (b) => {
    /* The builders' shape (the merge builder's .mb-*): the conditions on
       the left; on the right what they come to — the rows they match in
       this table, counted as they change, and the same filter as SQL,
       editable, with the grouping under it. */
    const root = el('div', 'mb fb-builder');
    const panes = el('div', 'mb-panes fb-panes');
    const left = el('div', 'mb-pane fb-left');
    const right = el('div', 'mb-pane mb-result fb-right');
    const treeContainer = el('div', 'fb-tree');
    left.append(el('div', 'mb-head', 'Conditions'), treeContainer);

    const summary = el('div', 'mb-summary');
    const stats = el('div', 'mb-stats');
    summary.append(stats);
    const countNote = el('ul', 'mb-notes');
    const sqlHead = el('div', 'fb-sql-head');
    const status = el('span', 'fb-status');
    sqlHead.append(el('span', 'mb-head', 'SQL'), status);
    const sqlBox = el('textarea', 'fb-sql');
    sqlBox.rows = 4;
    sqlBox.spellcheck = false;
    sqlBox.setAttribute('aria-label', 'The filter as SQL (editable)');
    right.append(summary, countNote, sqlHead, sqlBox);
    panes.append(left, right);
    root.append(panes);

    const src = S.sources.find((s) => s.id === S.sourceId);
    // Counted with a plain COUNT over the table, which a merge has no
    // table of its own for; its grid's count is the one to read there.
    const countable = !!(src && src.table_name && !src.is_merge);
    let countSeq = 0;
    const paintStats = (n, note) => {
      stats.replaceChildren(tableMenuStat(n == null ? '—' : n, 'rows match'),
        tableMenuStat(src ? src.row_count : 0, 'rows in the table'),
        tableMenuStat(conditionCount(S.filterTree), 'conditions'));
      countNote.replaceChildren(el('li', 'mb-note' + (note && note.warn ? ' warn' : ''), note ? note.text
        : 'Counted over the whole table. The search box, the tag filter and the timeframe narrow the grid further.'));
    };
    const recount = debounce(async () => {
      if (!countable) { paintStats(null, { text: 'A merge isn’t counted here: apply the filter and read the grid’s count.' }); return; }
      const where = S.filterTree.type === 'raw' ? (S.filterTree.sql || '').trim() : serializeTree(S.filterTree);
      const seq = ++countSeq;
      try {
        const res = await post('/api/sql', { sql: `SELECT COUNT(*) AS n FROM ${src.table_name}` + (where ? ` WHERE ${where}` : ''), limit: 1 });
        if (seq === countSeq) paintStats(res.rows[0][0]);
      } catch (e) {
        if (seq === countSeq) paintStats(null, { warn: true, text: 'Not counted: ' + e.message });
      }
    }, 300);

    function refreshPreview() {
      if (document.activeElement !== sqlBox) {
        sqlBox.value = S.filterTree.type === 'raw' ? (S.filterTree.sql || '') : serializeTree(S.filterTree);
      }
      recount();
    }

    function rerenderTree() {
      treeContainer.replaceChildren();
      if (S.filterTree.type === 'raw') {
        treeContainer.append(el('div', 'fb-raw-banner',
          "Raw SQL mode — this expression doesn't match the structured editor's supported shape."));
        const startOver = el('button', 'btn ghost', 'Start over with the guided editor');
        startOver.onclick = () => { S.filterTree = { type: 'group', op: 'AND', children: [] }; rerenderTree(); };
        treeContainer.append(startOver);
      } else {
        treeContainer.append(renderFilterGroup(S.filterTree, rerenderTree, refreshPreview, true));
      }
      refreshPreview();
    }

    const validateLive = debounce((text) => {
      if (S.filterTree.type !== 'raw' || !text.trim()) { status.textContent = ''; status.className = 'fb-status'; return; }
      post('/api/filter/validate', { source_id: S.sourceId, fragment: text })
        .then((res) => {
          status.textContent = res.ok ? '✓ valid' : '✗ ' + res.error;
          status.className = 'fb-status ' + (res.ok ? 'ok' : 'err');
        })
        .catch(() => {});
    }, 400);

    sqlBox.oninput = () => {
      const text = sqlBox.value;
      const parsed = text.trim() ? parseWhereFragment(text) : { type: 'group', op: 'AND', children: [] };
      S.filterTree = parsed || { type: 'raw', sql: text };
      rerenderTree();
      validateLive(text);
    };

    // Group by — editable here so a filter can carry a grouping the way the
    // shipped defaults do (currentFilterPayload serializes S.groupByCols when
    // set; applying a saved filter restores it via setGrouping). Local until
    // Apply, then setGrouping + the doApply rebuild lay it on the grid.
    let gbCols = [...S.groupByCols];
    let gbSort = S.groupSort;
    let gbDir = S.groupSortDir;
    const gbWrap = el('div', 'fb-groupby');
    function renderGroupBy() {
      gbWrap.replaceChildren();
      gbWrap.append(el('div', 'mb-head', 'Group by'));
      const chips = el('div', 'fb-groupby-chips');
      gbCols.forEach((name, i) => {
        const chip = el('span', 'fb-groupby-chip', name);
        const rm = el('button', 'fb-groupby-rm', '✕');
        rm.title = 'Remove from grouping';
        rm.onclick = () => { gbCols.splice(i, 1); renderGroupBy(); };
        chip.append(rm);
        chips.append(chip);
      });
      const add = el('select', 'fb-groupby-add');
      add.append(new Option(gbCols.length ? '+ nested level…' : '(no grouping)', ''));
      for (const c of S.columns) if (!gbCols.includes(c.name)) add.append(new Option(c.name, c.name));
      add.onchange = () => { if (add.value) { gbCols.push(add.value); renderGroupBy(); } };
      chips.append(add);
      gbWrap.append(chips);
      if (gbCols.length) {
        const sortRow = el('div', 'fb-groupby-sort');
        sortRow.append(el('span', 'fb-groupby-sublabel', 'Order groups'));
        const by = tableMenuSeg([['count', 'By count'], ['value', 'By value']], () => gbSort, (k) => { gbSort = k; by.paint(); });
        const dir = tableMenuSeg([['desc', 'High → low'], ['asc', 'Low → high']], () => gbDir, (k) => { gbDir = k; dir.paint(); });
        sortRow.append(by.seg, dir.seg);
        gbWrap.append(sortRow);
      }
    }
    renderGroupBy();
    right.append(gbWrap);

    // Along the foot: where filters are kept on the left, then saving,
    // and Apply — the main one — on the right.
    const actions = el('div', 'table-menu-foot table-menu-strip fb-foot');
    const apply = el('button', 'btn primary', 'Apply');
    apply.onclick = () => {
      const doApply = () => {
        // Grouping first — setGrouping only updates state; the rebuild below
        // (its regroupAll) is what lays it on the grid.
        setGrouping(gbCols, gbSort, gbDir);
        $('modal').hidden = true;
        updateFiltersButton();
        rebuildView({ keepScroll: false });
      };
      if (S.filterTree.type === 'raw' && S.filterTree.sql.trim()) {
        post('/api/filter/validate', { source_id: S.sourceId, fragment: S.filterTree.sql }).then((res) => {
          if (!res.ok) { status.textContent = '✗ ' + res.error; status.className = 'fb-status err'; return; }
          doApply();
        });
      } else doApply();
    };
    const clear = el('button', 'btn ghost', 'Clear');
    clear.title = 'Remove every condition (Apply to see the table unfiltered)';
    clear.onclick = () => { S.filterTree = { type: 'group', op: 'AND', children: [] }; rerenderTree(); };
    const browse = el('button', 'btn ghost', 'Saved filters…');
    browse.title = 'Browse, apply and manage saved filters (the builder\'s state stays as-is)';
    browse.onclick = () => openSavedFiltersModal();
    const saveFilterAs = el('button', 'btn ghost table-menu-close', editing ? 'Save as new…' : 'Save filter…');
    saveFilterAs.title = `Saves for these ${S.columns.length} columns — cyclable with `
      + `${S.keymap.cyclePrevFilter[0] || '['} / ${S.keymap.cycleNextFilter[0] || ']'}, and suggested `
      + `automatically next time you open a table with matching columns`;
    saveFilterAs.onclick = async () => {
      if (!hasActiveFilterTree()) { toast('Build a filter first'); return; }
      const name = await promptDialog('Filter name:');
      if (!name || !name.trim()) return;
      const rec = await post('/api/saved_filters', { name: name.trim(), col_names: baseColumns().map((c) => c.name), payload: currentFilterPayload() });
      S.savedFilters.push(rec);
      updateFiltersButton();
      checkPresets(S.sourceId); // this filter may now match the open table's banner
      toast(`Saved filter "${name.trim()}"`);
    };
    actions.append(browse, clear, saveFilterAs);

    if (editing) {
      // Deliberately doesn't send col_names: the header set is the filter's
      // identity for [ / ] cycling and the suggested-filter banner, so an
      // edit keeps it bound to the set it was saved for even if the table
      // open right now has a different one. "Save as new…" is the rebind path.
      const update = el('button', 'btn ghost', `Update "${editing.name}"`);
      update.title = 'Overwrite this saved filter with the conditions above';
      update.onclick = async () => {
        const payload = currentFilterPayload();
        try {
          const rec = await api(`/api/saved_filters/${editing.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }),
          });
          const i = S.savedFilters.findIndex((f) => f.id === editing.id);
          if (i !== -1) S.savedFilters[i] = rec;
          $('modal').hidden = true;
          updateFiltersButton();
          if (S.sourceId) checkPresets(S.sourceId);
          toast(`Updated filter "${editing.name}"`);
        } catch (e) {
          toast('Could not update filter: ' + e.message);
        }
      };
      actions.append(update);
    }
    actions.append(apply);
    root.append(actions);
    b.append(root);

    paintStats(null);
    rerenderTree();
  }, { wide: 'x', tall: true });
}

/* Conditions in a tree, counted as the builder shows them: a raw SQL
   expression is one. */
function conditionCount(node) {
  if (!node) return 0;
  if (node.type === 'cond' || node.type === 'raw') return node.type === 'raw' && !(node.sql || '').trim() ? 0 : 1;
  return (node.children || []).reduce((n, c) => n + conditionCount(c), 0);
}

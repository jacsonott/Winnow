/* The guided AND/OR filter tree and its SQL round-trip.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, el, post, toast } from './core.js';
import { checkPresets } from './savedfilters.js';
import { S } from './state.js';
import { currentFilterPayload, updateFiltersButton } from './timeframe.js';
import { baseColumns } from './tsformat.js';
import { modal, promptDialog } from './ui.js';
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

export function serializeCond(node) {
  const c = sqlIdent(node.column);
  const v = node.value;
  switch (node.op) {
    case 'contains': return `${c} LIKE ${sqlLit('%' + v + '%')}`;
    case 'not_contains': return `(${c} NOT LIKE ${sqlLit('%' + v + '%')} OR ${c} IS NULL)`;
    case 'equals': return `${c} = ${sqlLit(v)}`;
    case 'not_equals': return `(${c} <> ${sqlLit(v)} OR ${c} IS NULL)`;
    case 'starts': return `${c} LIKE ${sqlLit(v + '%')}`;
    case 'regex': return `${c} REGEXP ${sqlLit(v)}`;
    case '>': case '>=': case '<': case '<=': return `${c} ${node.op} ${sqlLit(v)}`;
    case 'empty': return `(${c} IS NULL OR ${c} = '')`;
    case 'not_empty': return `(${c} IS NOT NULL AND ${c} <> '')`;
    case 'in': {
      const items = Array.isArray(v) ? v : String(v || '').split('\n').map((x) => x.trim()).filter(Boolean);
      return items.length ? `${c} IN (${items.map(sqlLit).join(', ')})` : '1';
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

/* Round-trips only the narrow subset serializeTree/serializeCond emit: the
   simple atomic ops (contains/starts/equals/regex/comparisons/in) plus
   AND/OR/paren grouping. The compound shapes for not_contains/not_equals/
   empty/not_empty — and anything else outside this subset — fall back to
   raw mode, which still works as a filter, it just won't populate the
   structured editor. */
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
      const val = unquoteStr(lit);
      pos += 3;
      if (val.startsWith('%') && val.endsWith('%') && val.length >= 2) return { type: 'cond', column, op: 'contains', value: val.slice(1, -1) };
      if (!val.startsWith('%') && val.endsWith('%')) return { type: 'cond', column, op: 'starts', value: val.slice(0, -1) };
      return { type: 'cond', column, op: 'equals', value: val };
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
  return tree.type === 'group' ? tree : { type: 'group', op: 'AND', children: [tree] };
}

export function hasActiveFilterTree() {
  return S.filterTree.type === 'raw' ? !!(S.filterTree.sql || '').trim() : !!(S.filterTree.children || []).length;
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
    const inp = el('input');
    inp.value = Array.isArray(node.value) ? node.value.join('\n') : (node.value || '');
    inp.placeholder = node.op === 'in' ? 'one per line' : 'value';
    inp.oninput = () => {
      node.value = node.op === 'in' ? inp.value.split('\n').map((x) => x.trim()).filter(Boolean) : inp.value;
      onPreview();
    };
    row.append(inp);
  }
  return row;
}

export function renderFilterGroup(node, onStructural, onPreview, isRoot) {
  const wrap = el('div', 'fb-group');
  const head = el('div', 'fb-group-head');
  const opSel = el('select', 'fb-group-op');
  for (const o of ['AND', 'OR']) {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    if (node.op === o) opt.selected = true;
    opSel.append(opt);
  }
  opSel.onchange = () => { node.op = opSel.value; onStructural(); };
  head.append(el('span', 'fb-group-label', isRoot ? 'Match' : 'Group:'), opSel, el('span', 'fb-group-label', 'of:'));
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
  const addCond = el('button', 'btn ghost', '+ condition');
  addCond.onclick = () => {
    node.children = node.children || [];
    node.children.push({ type: 'cond', column: S.columns[0] ? S.columns[0].name : '', op: 'contains', value: '' });
    onStructural();
  };
  const addGroup = el('button', 'btn ghost', '+ group');
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
  modal(editing ? `Edit filter — ${editing.name}` : 'Filter builder', (b) => {
    const help = el('p', 'fb-help',
      'Build filters visually, or type/paste SQL directly below — edits sync both ways when the SQL is simple enough to parse back into the structured editor.');
    const treeContainer = el('div', 'fb-tree');
    const sqlLabel = el('div', 'fb-sql-label', 'Equivalent SQL (editable):');
    const sqlBox = el('textarea', 'fb-sql');
    sqlBox.rows = 3;
    sqlBox.spellcheck = false;
    const status = el('div', 'fb-status');

    function refreshPreview() {
      if (document.activeElement !== sqlBox) {
        sqlBox.value = S.filterTree.type === 'raw' ? (S.filterTree.sql || '') : serializeTree(S.filterTree);
      }
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

    b.append(help, treeContainer, sqlLabel, sqlBox, status);

    const actions = el('div', 'row-actions');
    const apply = el('button', 'btn', 'Apply');
    apply.onclick = () => {
      const doApply = () => {
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
    clear.onclick = () => { S.filterTree = { type: 'group', op: 'AND', children: [] }; rerenderTree(); };
    const saveFilterAs = el('button', 'btn ghost', editing ? 'Save as new…' : 'Save filter…');
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
    actions.append(apply, clear, saveFilterAs);

    if (editing) {
      // Deliberately doesn't send col_names: the header set is the filter's
      // identity for [ / ] cycling and the suggested-filter banner, so an
      // edit keeps it bound to the set it was saved for even if the table
      // open right now has a different one. "Save as new…" is the rebind path.
      const update = el('button', 'btn', `Update "${editing.name}"`);
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

    b.append(actions);

    rerenderTree();
  }, { wide: true });
}

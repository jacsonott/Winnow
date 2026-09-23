/* The per-column quick filters: parsing the header box's syntax, the value
picker behind each ▾, and filtering by a cell's value.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { renderFilterBar, renderHead, saveLayout, visibleCols } from './columns.js';
import { $, api, debounce, el, toast } from './core.js';
import { rowAt } from './grid.js';
import { collapseSearchIfEmpty, syncSearchExpansion } from './search.js';
import { clearAllFilters } from './sources.js';
import { S, normalizeTree } from './state.js';
import { updateFiltersButton } from './timeframe.js';
import { anchoredPanel, closeMenu } from './ui.js';
import { SEARCH_DETACH_MS, rebuildView } from './view.js';

/* -------------------------------------------------------------- filters */

/* The search box's rebuilds — the debounce below, Enter, Escape, the
   mode switch, the advanced chips — are the ones that go to the
   background after SEARCH_DETACH_MS (view.js): the old rows stay on
   screen and the result waits for Apply. A header-box filter uses
   view.js's rebuildSoon and blocks with the cancel chip like every
   other rebuild — see SEARCH_DETACH_MS for why the two differ. */
const searchSoon = debounce(() => rebuildView({ detachAfterMs: SEARCH_DETACH_MS }), 220);

/* Compact filter syntax, typed straight into the column box:
     foo      contains          !foo    does not contain
     =foo     exact             ^foo    starts with
     >10 <10 >=10 <=10          /re/    regex
     ""       empty             *       not empty
     a|b|c    any of            */
export function parseFilter(raw) {
  const s = raw.trim();
  if (!s) return null;
  if (s === '""' || s === "''") return { op: 'empty', value: 'x' };
  if (s === '*') return { op: 'not_empty', value: 'x' };
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) return { op: 'regex', value: s.slice(1, -1) };
  const m = s.match(/^(!=|>=|<=|[!=^><])(.*)$/);
  if (m && m[2] !== '') {
    const map = { '!': 'not_contains', '=': 'equals', '^': 'starts', '!=': 'not_equals' };
    return { op: map[m[1]] || m[1], value: m[2].trim() };
  }
  if (s.includes('|')) return { op: 'in', value: s.split('|').map((x) => x.trim()).filter(Boolean) };
  return { op: 'contains', value: s };
}

/* How one column's filter reads on a chip in the filter bar. Short enough
   for a strip, and honest about which of the box's operators is in play —
   a chip that showed the bare text would make `!powershell` and
   `powershell` look like the same filter, which is the opposite of what a
   summary is for. `contains` is the one op with no marker, because it is
   what typing a word into the box means and a `~` on nine chips out of ten
   is noise. */
export function describeFilter(raw) {
  const p = parseFilter(raw);
  if (!p) return '';
  const v = (n = 30) => ellipsize(String(p.value), n);
  switch (p.op) {
    case 'contains': return v();
    case 'not_contains': return 'not ' + v(26);
    case 'equals': return '= ' + v(28);
    case 'not_equals': return '\u2260 ' + v(28);
    case 'starts': return 'starts ' + v(24);
    case 'regex': return '/' + v(26) + '/';
    case 'empty': return 'is empty';
    case 'not_empty': return 'is not empty';
    case 'in': return ellipsize(p.value.join(' | '), 34);
    default: return p.op + ' ' + v(26); // the numeric comparisons: > >= < <=
  }
}

/* Every per-column filter currently narrowing the table, in display order,
   as {column, text, full} — what the filter bar draws chips from.

   Two kinds, because a column can be filtered two ways and a bar that
   showed only one would leave the other invisible: the header box's own
   text, and the value picker's node in the guided filter tree (the
   selections the box can't spell — see setPickerTreeNode). The tree's OTHER
   conditions are the filter builder's and stay under Filters ▾, which is
   where they can be edited.

   Hidden and grouped-away columns are included deliberately: a filter on a
   column you cannot see is exactly the one worth naming. */
export function columnFilterChips() {
  const seen = new Set();
  const names = [...S.order, ...S.columns.map((c) => c.name)].filter((n) => {
    if (seen.has(n)) return false;
    seen.add(n);
    return true;
  });
  const out = [];
  for (const column of names) {
    const raw = S.filters[column];
    if (raw && parseFilter(raw)) {
      out.push({ column, text: describeFilter(raw), full: raw });
      continue;
    }
    const node = pickerTreeNode(column);
    if (node) {
      const values = valuesFromPickerNode(node).map(displayValue);
      out.push({ column, text: ellipsize(values.join(' | '), 34), full: values.join(' | ') });
    }
  }
  return out;
}

/* Taking one filter off, from the chip's ✕. Both spellings go, because
   the chip names the column rather than the mechanism and removing "the
   filter on Provider" has to mean all of it. */
export async function removeColumnFilter(column) {
  delete S.filters[column];
  setPickerTreeNode(column, null);
  S.filterOpen = S.filterOpen.filter((n) => n !== column);
  updateFiltersButton();
  renderHead();
  await rebuildView();
}

export function currentSpec() {
  const filters = [];
  for (const [column, raw] of Object.entries(S.filters)) {
    const p = parseFilter(raw);
    if (p) filters.push({ column, ...p });
  }
  return {
    source_id: S.sourceId,
    filters,
    sort: S.sort,
    search: S.searchMode === 'advanced' ? '' : S.search,
    search_mode: S.searchMode,
    search_terms: S.searchMode === 'advanced' ? S.searchTerms : [],
    // normalizeTree: a cond-root payload (seeded/imported) must not read
    // as "no filter" here while the ★ button says it's applied.
    filter_tree: (() => {
      const t = normalizeTree(S.filterTree);
      return t.type === 'raw' || (t.children && t.children.length) ? t : null;
    })(),
    tags: S.tagFilter,
    time_range: S.timeRange,
    hide_empty_rows: !!S.hideEmptyRows,
  };
}

/* --------------------------------------------------- value filter picker */

/* Excel's header dropdown: a column's distinct values with their counts, as
   a checkbox list you tick and apply. What it writes is an ordinary column
   filter (`=v`, or `a|b|c` for several) — the picker is a way to *author*
   the filter the header box already understands, not a fourth filtering
   mechanism, so everything downstream (saved filters, Q's SQL, the filter
   builder) sees exactly what typing would have produced.

   Off above VALUE_FILTER_AUTO_MAX rows by default, and the reason is the
   scan behind it: distinct-values-with-counts is one aggregate pass over
   the view (or over the source, for whole-table scope) with no index to
   lean on until the lazy per-column one exists. At a few hundred thousand
   rows that's tens of milliseconds; on a 2.4M-row $J it's the kind of
   pause a button you might click by accident shouldn't be able to cause.
   The override lives per-table and per-column in the table menu (right-
   click a tab), and the row menu's "Filter by values…" opens it regardless
   — an explicit click is consent to pay for the scan in a way an
   always-present button isn't. */
export const VALUE_FILTER_AUTO_MAX = 250000;

export const VALUE_PICKER_LIMIT = 1000;

export function valueFilterAutoOn() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return !!src && src.row_count <= VALUE_FILTER_AUTO_MAX;
}

/* Three layers, most specific first: the column's own override (stored in
   the layout, so it travels with a saved default layout for this header
   set), the table's mode, then the row-count rule. */
export function valueFilterEnabled(name) {
  const override = (S.layout[name] || {}).valuePicker;
  if (override !== undefined) return !!override;
  if (S.valueFilterMode === 'on') return true;
  if (S.valueFilterMode === 'off') return false;
  return valueFilterAutoOn();
}

export function setValueFilterMode(mode) {
  S.valueFilterMode = mode;
  saveLayout();
  renderHead();
}

export function setColumnValueFilter(name, enabled) {
  const layout = { ...(S.layout[name] || {}) };
  if (enabled === null) delete layout.valuePicker; else layout.valuePicker = enabled;
  S.layout[name] = layout;
  saveLayout();
  renderHead();
}

/* Which values the column's current filter already selects, or null when it
   isn't a value selection at all (a contains/regex/numeric filter, or no
   filter) — null means "nothing is excluded yet", which the picker renders
   as everything ticked, the same way Excel opens on an unfiltered column. */
export function currentValueSelection(column) {
  const raw = S.filters[column];
  if (raw) {
    const p = parseFilter(raw);
    if (!p) return null;
    if (p.op === 'equals') return new Set([p.value]);
    if (p.op === 'in') return new Set(p.value);
    if (p.op === 'empty') return new Set(['']);
    return null;
  }
  const node = pickerTreeNode(column);
  return node ? new Set(valuesFromPickerNode(node)) : null;
}

/* The picker's node in the guided filter tree, for the selections the
   header box can't spell (a value containing `|`, one with whitespace at
   its edges, or `(empty)` mixed with real values — `IN ()` drops empty
   strings server-side, so that combination has to be an OR).

   Recognised structurally rather than by a marker field: openFilterBuilder
   round-trips the tree through SQL text, which would drop any marker we
   invented, so "an in/equals/empty condition on this column, or an OR of
   exactly those" is the only durable identity available. */
export function isPickerNode(node, column) {
  if (!node) return false;
  if (node.type === 'cond') return node.column === column && ['in', 'equals', 'empty'].includes(node.op);
  if (node.type === 'group' && node.op === 'OR' && (node.children || []).length) {
    return node.children.every((c) => isPickerNode(c, column));
  }
  return false;
}

export function pickerTreeNode(column) {
  const root = S.filterTree;
  if (!root || root.type !== 'group' || root.op !== 'AND') return null;
  return (root.children || []).find((n) => isPickerNode(n, column)) || null;
}

export function valuesFromPickerNode(node) {
  if (node.type === 'cond') {
    if (node.op === 'empty') return [''];
    if (node.op === 'equals') return [node.value];
    return Array.isArray(node.value) ? node.value.slice() : [];
  }
  return node.children.flatMap(valuesFromPickerNode);
}

export function makePickerNode(column, values) {
  const nonEmpty = values.filter((v) => v !== '');
  const conds = [];
  if (nonEmpty.length === 1) conds.push({ type: 'cond', column, op: 'equals', value: nonEmpty[0] });
  else if (nonEmpty.length) conds.push({ type: 'cond', column, op: 'in', value: nonEmpty });
  if (nonEmpty.length !== values.length) conds.push({ type: 'cond', column, op: 'empty', value: '' });
  return conds.length === 1 ? conds[0] : { type: 'group', op: 'OR', children: conds };
}

export function setPickerTreeNode(column, values) {
  const root = S.filterTree;
  const node = values && values.length ? makePickerNode(column, values) : null;
  if (root && root.type === 'group' && root.op === 'AND') {
    root.children = (root.children || []).filter((n) => !isPickerNode(n, column));
    if (node) root.children.push(node);
    return;
  }
  // A root the analyst turned into an OR, or a bare raw fragment, keeps its
  // own meaning — AND the picker's condition onto it rather than into it.
  if (node) S.filterTree = { type: 'group', op: 'AND', children: [root, node] };
}

/* The header box's two spellings, or null when neither fits — see
   valueFilterText for why `=v` is safe with a `|` in it but a multi-value
   `a|b|c` isn't, and why edge whitespace never survives either. */
export function quickFilterTextForValues(values) {
  if (values.length === 1) return valueFilterText(values[0] === '' ? '' : values[0]);
  if (values.some((v) => v === '' || String(v).includes('|') || valueFilterText(v) === null)) return null;
  return values.join('|');
}

export async function applyValueSelection(column, values, { clearInstead }) {
  setPickerTreeNode(column, null); // whatever the picker last put in the tree for this column
  if (clearInstead) {
    setColumnFilter(column, '');
  } else {
    const text = quickFilterTextForValues(values);
    setColumnFilter(column, text === null ? '' : text);
    if (text === null) {
      setPickerTreeNode(column, values);
      toast(`${column}: applied under Filters ▾ — these values can't be written in the filter box`, 5000);
    }
  }
  updateFiltersButton();
  renderHead(); // repaints the ▾'s in-tree marker (and the box) for this column
  await rebuildView({ keepScroll: false });
}

/* Two sources for the list, and which one is right depends on what the
   column is already filtered by. Unfiltered: the current view, so the list
   reflects every other filter in play (Excel's behaviour, and the one that
   makes "which processes survive this timeframe" answerable). Already
   filtered on this column: the whole table, because a view narrowed to
   three values can only ever offer those three back, and widening the
   selection is the main reason to reopen the dropdown. Either way it's
   swappable from the panel — the scope is stated, never guessed at. */
export async function fetchPickerValues(column, scope) {
  if (scope === 'view' && S.view) {
    const r = await api(`/api/group_summary?view_id=${encodeURIComponent(S.view.view_id)}`
      + `&column=${encodeURIComponent(column)}&limit=${VALUE_PICKER_LIMIT}&order=count&bucket_datetime=false`);
    return { values: r.groups, truncated: !!r.truncated };
  }
  const rows = await api(`/api/column_values?source_id=${S.sourceId}`
    + `&column=${encodeURIComponent(column)}&limit=${VALUE_PICKER_LIMIT + 1}`);
  return { values: rows.slice(0, VALUE_PICKER_LIMIT), truncated: rows.length > VALUE_PICKER_LIMIT };
}

/* Opens the picker against a column's own filter-row button, or against the
   header cell when that button isn't rendered (the size default is off, and
   the row menu's "Filter by values…" is how you got here). */
export function openValuePickerForColumn(column) {
  const anchor = document.querySelector(`.fcell-pick[data-col="${CSS.escape(column)}"]`)
    || document.querySelector(`.hcell[data-col="${CSS.escape(column)}"]`);
  if (!anchor) { toast('Open the table first'); return; }
  openValuePicker(column, anchor);
}

export function openValuePicker(column, anchorEl) {
  if (!S.sourceId || S.activeTab !== 'grid') { toast('Open a table first'); return; }
  const state = {
    // An existing selection on this column means the view can't show what
    // else is out there — start from the whole table so it can be widened.
    scope: currentValueSelection(column) ? 'table' : 'view',
    sort: 'count',
    search: '',
    values: null,
    truncated: false,
    checked: new Set(),
    loading: true,
    error: null,
  };
  let paint = () => {};
  const panel = anchoredPanel(anchorEl, 'value-picker', (p, close) => {
    paint = () => renderValuePicker(p, column, state, { reload, apply, close });
    paint();
  });
  if (!panel) return; // second click on the same button — toggled shut

  async function reload() {
    state.loading = true;
    state.error = null;
    paint();
    try {
      const res = await fetchPickerValues(column, state.scope);
      state.values = res.values;
      state.truncated = res.truncated;
      const selected = currentValueSelection(column);
      // Intersected with what's actually listed, so "apply" can never send
      // back a value the analyst was never shown — a truncated list would
      // otherwise silently carry values it never rendered.
      state.checked = new Set(res.values
        .map((v) => (v.value == null ? '' : String(v.value)))
        .filter((v) => !selected || selected.has(v)));
    } catch (e) {
      // 409 = the view was evicted (see invariant #3). Whole-table scope
      // needs no view at all, so the question is still answerable — re-ask
      // it there rather than showing an error nobody can act on.
      if (e.status === 409 && state.scope === 'view') {
        state.scope = 'table';
        toast('That view expired — showing whole-table values');
        return reload();
      }
      state.error = e.message;
      state.values = [];
    }
    state.loading = false;
    paint();
  }

  async function apply() {
    const listed = (state.values || []).map((v) => (v.value == null ? '' : String(v.value)));
    const values = listed.filter((v) => state.checked.has(v));
    // Everything ticked out of a complete list is the same statement as no
    // filter at all — and says so, rather than writing a 400-term `IN`.
    const clearInstead = !state.truncated && values.length === listed.length;
    closeMenu();
    await applyValueSelection(column, values, { clearInstead });
  }

  reload();
}

export function pickerRows(state) {
  const q = state.search.trim().toLowerCase();
  const rows = (state.values || [])
    .map((v) => ({ value: v.value == null ? '' : String(v.value), count: v.count }))
    .filter((v) => !q || v.value.toLowerCase().includes(q) || (v.value === '' && '(empty)'.includes(q)));
  // Sorted client-side: the fetch always asks for the most common values
  // first (that's the right thing to keep when the list is capped), so
  // A→Z is a re-sort of what came back rather than a second round trip.
  if (state.sort === 'value') rows.sort((a, b) => a.value.localeCompare(b.value, undefined, { numeric: true }));
  return rows;
}

export function renderValuePicker(panel, column, state, actions) {
  panel.replaceChildren();
  panel.append(el('div', 'menu-header', column));

  const search = el('input', 'vp-search');
  search.type = 'search';
  search.placeholder = 'Find a value…';
  search.value = state.search;
  search.oninput = () => { state.search = search.value; paintList(); };
  search.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); actions.apply(); } };
  panel.append(search);

  const toggles = el('div', 'vp-toggles');
  const scopeGroup = el('div', 'vp-seg');
  for (const [key, label, title] of [
    ['view', 'This view', 'Values among the rows the current filters leave — the other filters still apply'],
    ['table', 'Whole table', 'Every value in the column, ignoring the current filters'],
  ]) {
    const b = el('button', 'btn ghost', label);
    b.setAttribute('aria-pressed', String(state.scope === key));
    b.title = title;
    b.onclick = () => { if (state.scope !== key) { state.scope = key; actions.reload(); } };
    scopeGroup.append(b);
  }
  const sortBtn = el('button', 'btn ghost', state.sort === 'count' ? 'By count' : 'A→Z');
  sortBtn.title = 'Switch between most-common-first and alphabetical';
  sortBtn.onclick = () => {
    state.sort = state.sort === 'count' ? 'value' : 'count';
    sortBtn.textContent = state.sort === 'count' ? 'By count' : 'A→Z';
    paintList(); // not a full re-render: that would drop what's typed in the search box
  };
  toggles.append(scopeGroup, sortBtn);
  panel.append(toggles);

  const list = el('div', 'vp-list');
  panel.append(list);
  const status = el('div', 'vp-status');
  panel.append(status);

  const acts = el('div', 'vp-actions');
  const all = el('button', 'btn ghost', 'All');
  all.onclick = () => { for (const r of pickerRows(state)) state.checked.add(r.value); paintList(); };
  const none = el('button', 'btn ghost', 'None');
  none.onclick = () => { for (const r of pickerRows(state)) state.checked.delete(r.value); paintList(); };
  const cancel = el('button', 'btn ghost', 'Cancel');
  cancel.onclick = () => actions.close();
  const applyBtn = el('button', 'btn', 'Apply');
  applyBtn.onclick = () => actions.apply();
  acts.append(all, none, el('span', 'spacer'), cancel, applyBtn);
  panel.append(acts);

  function paintList() {
    list.replaceChildren();
    if (state.loading) { list.append(el('div', 'vp-note', 'Reading values…')); }
    const rows = pickerRows(state);
    for (const r of rows) {
      const lab = el('label', 'vp-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = state.checked.has(r.value);
      cb.onchange = () => {
        cb.checked ? state.checked.add(r.value) : state.checked.delete(r.value);
        paintStatus();
      };
      const text = el('span', 'vp-value' + (r.value === '' ? ' vp-empty' : ''), r.value === '' ? '(empty)' : r.value);
      text.title = r.value;
      lab.append(cb, text, el('span', 'vp-count', r.count.toLocaleString()));
      list.append(lab);
    }
    if (!state.loading && !rows.length) {
      list.append(el('div', 'vp-note', state.values && state.values.length ? 'No value matches that.' : 'No values.'));
    }
    paintStatus();
  }

  function paintStatus() {
    const rows = pickerRows(state);
    const checked = rows.filter((r) => state.checked.has(r.value)).length;
    status.replaceChildren();
    if (state.error) status.append(el('div', 'vp-warn', state.error));
    if (state.truncated) {
      status.append(el('div', 'vp-warn',
        `Showing the ${VALUE_PICKER_LIMIT.toLocaleString()} most common values — type above to find others, or filter the column first.`));
    }
    status.append(el('div', null, `${checked.toLocaleString()} of ${rows.length.toLocaleString()} shown values ticked`));
    applyBtn.disabled = state.loading || !checked;
    applyBtn.title = checked ? '' : 'Tick at least one value';
  }

  paintList();
  setTimeout(() => search.focus(), 0);
}

/* ------------------------------------------------- filter by a cell value */

/* Everything that turns "this value, in this column" into a filter goes
   through here — the `f` keybind, the row menu's Filter to…/Exclude, and
   the value picker's single-value case — so the three can't drift apart on
   what an empty cell means or how a value gets escaped.

   `=value` is the spelling on purpose: it round-trips any value the box can
   hold, including one containing a `|` (parseFilter matches the `=` prefix
   before it ever looks for the any-of separator). What it can't hold is a
   value whose own edges are whitespace — the box trims — which is why
   valueFilterText() reports null rather than quietly filtering on the
   trimmed text, and the picker routes those through the filter tree. */
export function valueFilterText(v) {
  if (v == null || v === '') return '""';
  const s = String(v);
  return s === s.trim() && !/[\r\n]/.test(s) ? '=' + s : null;
}

export function valueExcludeText(v) {
  if (v == null || v === '') return '*'; // "not empty" is the exclusion of empty
  const s = String(v);
  return s === s.trim() && !/[\r\n]/.test(s) ? '!=' + s : null;
}

/* Writes a raw filter string into a column's header box and the state
   behind it, keeping the visible input in step without a full renderHead()
   (which would drop the cell selection the caller may still be acting on).

   The bar IS repainted, on its own: under it most columns have no box, so
   the chip is the only place the filter this just wrote shows up, and one
   that appeared a rebuild later would be a filter bar that lies about what
   is narrowing the table. renderFilterBar touches nothing but the strip. */
export function setColumnFilter(name, raw) {
  if (raw) S.filters[name] = raw; else delete S.filters[name];
  const inp = document.querySelector(`.fcell input[data-col="${CSS.escape(name)}"]`);
  if (inp) { inp.value = raw || ''; inp.classList.toggle('active', !!raw); }
  renderFilterBar();
}

export const displayValue = (v) => (v == null || v === '' ? '(empty)' : String(v));

export const ellipsize = (s, n = 42) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

/* `only: true` is the Shift+F half of the pair — filter to this value and
   drop everything else that was narrowing the view. It's clearAllFilters
   with a seed rather than its own reset, because the carve-outs that make
   clearing correct (the timeframe filter survives; grouping is stashed, not
   lost) are exactly the ones a second implementation would forget. */
export async function filterByValue(column, value, { only = false, exclude = false } = {}) {
  const raw = exclude ? valueExcludeText(value) : valueFilterText(value);
  if (raw === null) {
    toast(`"${ellipsize(String(value))}" starts or ends with whitespace — use Filter by values… to select it`, 5000);
    return;
  }
  const shown = ellipsize(displayValue(value));
  if (only) {
    await clearAllFilters({ column, raw });
    toast(`Filtered ${column} ${exclude ? '≠' : '='} ${shown} · other filters cleared`);
    return;
  }
  setColumnFilter(column, raw);
  await rebuildView();
  toast(`Filtered ${column} ${exclude ? '≠' : '='} ${shown}`);
}

/* Reuses the same single-cell selection a plain click already commits to
   S.cellRange (see setCellRange above) — takes the top-left cell of
   whatever's selected and filters that column to its value, exactly as if
   "=value" had been typed into the header filter by hand. */
export function selectedCellTarget() {
  if (!S.cellRange) return null;
  const column = visibleCols()[S.cellRange.c0];
  const r = rowAt(S.cellRange.r0);
  if (!column || !r) return null;
  return { column, value: r.cells[S.columns.findIndex((c) => c.name === column)] };
}

export async function filterBySelectedCell({ only = false } = {}) {
  if (!S.cellRange) { toast('Click a cell first'); return; }
  const target = selectedCellTarget();
  if (!target) { toast('Row not loaded yet'); return; }
  await filterByValue(target.column, target.value, { only });
}

export function currentSourceHasFts() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return !!(src && src.has_fts);
}

export function updateSearchHint() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const hasFts = currentSourceHasFts();
  // The first search on an unindexed table starts the trigram build in
  // the background (Store._ensure_fts_building) and scans the whole table
  // until it lands. A slow first search that says why is a wait; one
  // that doesn't is a hang. Regex never uses the index, so it stays a
  // full scan whatever the index is doing.
  const building = !!(src && src.fts_building && !hasFts);
  const hint = $('searchMode');
  if (S.searchMode === 'regex') hint.textContent = 'regex · full scan';
  else if (S.searchMode === 'advanced') {
    hint.textContent = hasFts ? 'advanced · full-text' : (building ? 'advanced · index building' : 'advanced · substring chain');
  } else hint.textContent = building ? 'substring · index building' : 'substring';
  // The hint sits inside the box's right padding, which was a fixed 74px —
  // the longer hints painted straight over the typed text. Size the padding
  // to the hint; while the box is hidden (no layout yet) estimate from the
  // 10px uppercase face and let the next call correct it.
  const w = hint.offsetWidth || Math.ceil(hint.textContent.length * 6.7);
  $('search').style.paddingRight = (w + 16) + 'px';
  // The padding it just claimed is how much room the placeholder has left.
  syncSearchKeyCopy();
}

/* The key that opens the search box is advertised in two places — the
   magnifier's tooltip and the box's own placeholder — and the chord is
   dispatched only while `focusSearch` still holds it (keymap.js's
   pre-gate). So both strings are built from the binding rather than
   written into index.html: take Ctrl+F off search in Settings, or move
   search to another key, and the copy stops naming a key that now does
   something else. It is the same reason updateTimeRangeButton spells its
   tooltip out of S.keymap. Keys are printed exactly as the Settings chips
   spell them, so the tooltip and the chip agree character for character.

   The placeholder is the string with a width limit: #search is a fixed
   320px and the mode hint above just claimed the right padding, which in
   regex and advanced mode leaves too little for the keys. Measure the
   text against what is left and fall back to the bare "All columns"
   rather than let it clip mid-chord — a hint that names half a key is
   worse than one that names none. The tooltip has no width limit and
   always names them. */
let placeholderMetrics = null;

export function syncSearchKeyCopy() {
  const keys = (S.keymap.focusSearch || []).filter(Boolean);
  const spelled = keys.join(' or ');
  $('btnSearchToggle').title = spelled ? `Search  —  press ${spelled}` : 'Search';
  const box = $('search');
  const withKeys = `All columns  —  ${spelled}`;
  const cs = getComputedStyle(box);
  placeholderMetrics = placeholderMetrics || document.createElement('canvas').getContext('2d');
  placeholderMetrics.font = `${cs.fontSize} ${cs.fontFamily}`;
  // clientWidth is 0 while the wrap is hidden — the CSS width is what the
  // box will be when it opens, and it is border-box, so the padding is
  // already inside it.
  const room = (box.clientWidth || parseFloat(cs.width) || 320)
    - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  box.placeholder = spelled && placeholderMetrics.measureText(withKeys).width <= room
    ? withKeys : 'All columns';
}

/* Generic multi-term AND/OR/NOT chip editor — shared by the toolbar's
   Advanced search mode and the Search-all-tables modal, which both need
   "a growable list of {term, connector, exclude} chips" but differ in
   what "changed" means (rebuild the grid view vs. re-run a cross-table
   count query) and how eagerly to react to keystrokes. */
export function renderTermChips(container, terms, onChange, opts = {}) {
  const commit = opts.debounceMs ? debounce(onChange, opts.debounceMs) : onChange;
  container.replaceChildren();
  terms.forEach((t, i) => {
    const chip = el('div', 'adv-chip');
    if (i > 0) {
      const conn = el('select', 'adv-conn');
      for (const c of ['AND', 'OR']) {
        const optEl = document.createElement('option');
        optEl.value = c;
        optEl.textContent = c;
        if (t.connector === c) optEl.selected = true;
        conn.append(optEl);
      }
      conn.onchange = () => { t.connector = conn.value; onChange(); };
      chip.append(conn);
    }
    const notBtn = el('button', 'btn ghost adv-not' + (t.exclude ? ' active' : ''), 'NOT');
    notBtn.title = 'Exclude this term';
    notBtn.onclick = () => {
      t.exclude = !t.exclude;
      notBtn.classList.toggle('active', t.exclude);
      onChange();
    };
    chip.append(notBtn);
    const inp = el('input');
    inp.value = t.term;
    inp.placeholder = 'term';
    // opts.liveInput === false: update the term as the analyst types but
    // don't run anything expensive off every keystroke — Enter or an
    // explicit action (see the Search button in openSearchAllModal) is
    // what actually commits it. Default stays live (the main grid's
    // Advanced search wants immediate feedback as you type).
    inp.oninput = () => { t.term = inp.value; if (opts.liveInput !== false) commit(); };
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); onChange(); if (opts.blurTarget) opts.blurTarget.focus(); }
    };
    if (opts.onInputBlur) inp.addEventListener('blur', opts.onInputBlur);
    chip.append(inp);
    const rm = el('button', 'btn ghost adv-rm', '✕');
    rm.title = 'Remove term';
    rm.onclick = () => { terms.splice(i, 1); renderTermChips(container, terms, onChange, opts); onChange(); };
    chip.append(rm);
    container.append(chip);
  });
  const add = el('button', 'btn ghost', '+ term');
  add.onclick = () => {
    terms.push({ term: '', connector: 'AND', exclude: false });
    renderTermChips(container, terms, onChange, opts);
    const inputs = container.querySelectorAll('input');
    inputs[inputs.length - 1]?.focus();
  };
  container.append(add);
  // opts.trailing appends controls after "+ term" — it has to be a hook
  // rather than something the caller tacks on afterwards, because the
  // add/remove handlers above re-render by calling renderTermChips
  // directly (replaceChildren wipes the container each time).
  if (opts.trailing) opts.trailing(container);
}

/* A saved filter like the shipped tool sweep carries 26 terms; rendered
   as editor rows they stack taller than the viewport and the grid gets
   nothing. Past this many terms the bar collapses to a one-line summary
   (S.advCollapsed = null means "auto"; the ▸/▴ controls pin it). */
const ADV_COLLAPSE_AT = 6;

function advBarCollapsed() {
  if (S.advCollapsed !== null) return S.advCollapsed;
  return S.searchTerms.length > ADV_COLLAPSE_AT;
}

export function renderAdvancedChips() {
  const bar = $('advancedSearchBar');
  if (advBarCollapsed()) {
    bar.replaceChildren();
    const labels = S.searchTerms.map((t) => (t.exclude ? '-' : '') + (t.term || '').trim()).filter(Boolean);
    const head = labels.slice(0, 3).join(', ');
    const rest = labels.length > 3 ? `, +${labels.length - 3} more` : '';
    const btn = el('button', 'btn ghost adv-summary', `${labels.length} terms: ${head}${rest} ▸`);
    btn.title = `Expand to edit — ${labels.join(', ')}`;
    btn.onclick = () => { S.advCollapsed = false; renderAdvancedChips(); };
    bar.append(btn);
    return;
  }
  renderTermChips(bar, S.searchTerms, () => rebuildView({ keepScroll: false, detachAfterMs: SEARCH_DETACH_MS }), {
    debounceMs: 220, blurTarget: $('body'), onInputBlur: collapseSearchIfEmpty,
    trailing: (container) => {
      if (S.searchTerms.length <= ADV_COLLAPSE_AT) return;
      const min = el('button', 'btn ghost adv-summary', '▴ minimize');
      min.title = 'Collapse the term list to one line';
      min.onclick = () => { S.advCollapsed = true; renderAdvancedChips(); };
      container.append(min);
    },
  });
}

/* Switching the box between substring / regex / advanced, and rebuilding
   under the new mode. The rebuild detaches like every other search-box
   one — the analyst changed the mode to run a search, and a slow one
   belongs in the background.

   `detach: false` is for a caller that is not the search box: landing on
   a set of filters (Reset, Shift+F, the row menu's "…only", a session
   comparison's pivot) normalises the mode back to contains on its way,
   and then goes on to act on the view that build produces — expanding
   the search bar, recentring on the row it kept. A build left to finish
   in the background resolves with the OLD view still installed, so that
   work would land on the rows the analyst just cleared away
   (docs/notes/ui.md: only search-box rebuilds detach). */
export function setSearchMode(mode, { detach = true } = {}) {
  S.searchMode = mode;
  document.querySelectorAll('#searchModeToggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  if (mode === 'advanced') {
    if (!S.searchTerms.length) S.searchTerms.push({ term: '', connector: 'AND', exclude: false });
    renderAdvancedChips();
  }
  syncSearchExpansion(true);
  updateSearchHint();
  return rebuildView({ keepScroll: false, detachAfterMs: detach ? SEARCH_DETACH_MS : null });
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireFilters() {
document.querySelectorAll('#searchModeToggle button').forEach((b) => {
  b.onclick = () => setSearchMode(b.dataset.mode);
});

$('search').oninput = (e) => { S.search = e.target.value; syncSearchExpansion(true); searchSoon(); };

$('search').onkeydown = (e) => {
  // Escape is also how a search left running in the background is
  // called off from the box: a search-box rebuild cancels this table's
  // pending search before it starts its own (view.js runBuild).
  if (e.key === 'Escape') { e.target.value = ''; S.search = ''; rebuildView({ keepScroll: false, detachAfterMs: SEARCH_DETACH_MS }); $('body').focus(); }
  if (e.key === 'Enter') { rebuildView({ keepScroll: false, detachAfterMs: SEARCH_DETACH_MS }); $('body').focus(); }
};

$('search').addEventListener('blur', collapseSearchIfEmpty);
}

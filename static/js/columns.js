/* Column layout: header rendering, drag-to-reorder, widths and autofit.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, AUTOFIT_MAX_W_DEFAULT, GUTTER_W, api, debounce, el, post, toast } from './core.js';
import { columnMenuItems, opLabel } from './derived.js';
import { columnFilterChips, openValuePicker, pickerTreeNode, removeColumnFilter, valueFilterEnabled } from './filters.js';
import { render } from './grid.js';
import { renderGroupStrip } from './grouping.js';
import { S, selClear, selSetAll, selSnapshot } from './state.js';
import { baseColumns, columnMeta } from './tsformat.js';
import { anchoredPanel, contextMenu } from './ui.js';
import { rebuildSoon, rebuildView } from './view.js';

/* ---------------------------------------------------------------- header */

export function colWidth(name) {
  const l = S.layout[name] || {};
  if (l.w) return l.w;
  const c = S.columns.find((x) => x.name === name);
  if (!c) return 140;
  if (c.type === 'datetime') return 190;
  if (c.type === 'number') return 100;
  if (fillsGrid(c)) return fillWidth();
  return Math.min(360, Math.max(90, name.length * 9 + 30));
}

/* A one-column table IS its column: a log imported one line per row has
   nothing but Message. Sized from that header it came out 93px wide with
   every line cut to "2026-03-1…" and the rest of the grid empty, so the
   lone base column takes the viewport instead (the gutter and a little
   for the scrollbar excepted). A dragged width still wins (S.layout), and
   columns the analyst derives from it keep their own defaults. */
export function fillsGrid(c) { return !!c && !c.derived && baseColumns().length === 1; }

export function fillWidth() {
  const body = $('body');
  return Math.max(360, (body ? body.clientWidth : 0) - GUTTER_W - 20);
}

export const visibleCols = () => S.order.filter((n) => !(S.layout[n] || {}).hidden);

export const isPinned = (name) => !!(S.layout[name] || {}).pinned;

/* Where each pinned column sticks: the gutter's width plus the widths of
   the pinned columns before it. Returns {name: leftPx} for pinned columns
   only, so the three render sites (header, filter row, body cells) all
   place them identically.

   Columns are NOT reordered to pin them. `position: sticky` keeps an
   element in flow until it would scroll past its offset, so an unpinned
   column between two pinned ones simply slides underneath — which is the
   behaviour you want and costs nothing. Reordering would also have
   silently changed export column order, which follows the arrangement. */
export function pinnedOffsets() {
  const out = {};
  let left = GUTTER_W;
  for (const name of visibleCols()) {
    if (!isPinned(name)) continue;
    out[name] = left;
    left += colWidth(name);
  }
  return out;
}

/* Applies (or clears) the sticky placement on one rendered cell. z-index
   sits above ordinary cells so the columns scrolling under a pin are
   covered, and below the gutter, which pins to the left of everything. */
export function applyPin(node, name, offsets) {
  const left = offsets[name];
  if (left === undefined) return false;
  node.classList.add('pinned');
  node.style.position = 'sticky';
  node.style.left = left + 'px';
  return true;
}

/* Pin or unpin, then re-render. Pinning is a layout property, so it is
   saved with the rest of the layout and comes back with the table. */
export function togglePin(name) {
  const now = !isPinned(name);
  S.layout[name] = { ...(S.layout[name] || {}), pinned: now };
  renderHead();
  render();
  saveLayout();
  return now;
}

/* Send a column to either end of the order. Drag covers every other
   position, but "put this first" on a 200-column table is a drag across
   the whole list — and a hidden column can't be dragged in the grid at
   all. Same save path as a drag, so the layout remembers it. */
export function moveColumn(name, where) {
  if (!S.order.includes(name)) return;
  const rest = S.order.filter((n) => n !== name);
  S.order = where === 'top' ? [name, ...rest] : [...rest, name];
  renderHead();
  render();
  saveLayout();
}

/* ----------------------------------------------------------- column drag */

/* Native HTML5 drag-and-drop, reordering S.order directly. draggedCol is
   tracked in a closure var rather than trusted from dataTransfer alone —
   dataTransfer.getData isn't readable during dragover in most browsers
   (only on drop), but we need to know the source column during dragover
   to decide which side of the target to show the insertion indicator on. */
export let draggedCol = null;

export function wireColumnDrag(h, name) {
  h.addEventListener('dragstart', (e) => {
    draggedCol = name;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', name);
    h.classList.add('dragging');
  });
  h.addEventListener('dragend', () => {
    draggedCol = null;
    document.querySelectorAll('.hcell.dragging, .hcell.drop-before, .hcell.drop-after')
      .forEach((el2) => el2.classList.remove('dragging', 'drop-before', 'drop-after'));
  });
  h.addEventListener('dragover', (e) => {
    if (!draggedCol || draggedCol === name) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const before = e.clientX < h.getBoundingClientRect().left + h.offsetWidth / 2;
    h.classList.toggle('drop-before', before);
    h.classList.toggle('drop-after', !before);
  });
  h.addEventListener('dragleave', () => h.classList.remove('drop-before', 'drop-after'));
  h.addEventListener('drop', (e) => {
    e.preventDefault();
    const dragged = draggedCol;
    const before = h.classList.contains('drop-before');
    h.classList.remove('drop-before', 'drop-after');
    if (!dragged || dragged === name) return;
    S.order = S.order.filter((n) => n !== dragged);
    let idx = S.order.indexOf(name);
    if (!before) idx += 1;
    S.order.splice(idx, 0, dragged);
    renderHead();
    render();
    saveLayout();
  });
}

/* ------------------------------------------------------- filter surface */

/* Two surfaces, one setting. The classic always-on filter ROW — a box
   under every column, forever — is what ships: typing straight into a
   column box without looking is the Timeline Explorer reflex, and the
   analysts who have it are not wrong. Settings → Appearance switches to
   the filter BAR: a strip above the grid carrying only the filters
   actually set, as chips, plus the way to add one — and there a column's
   box appears under its header only when the header's ⌕ (or a chip, or
   "+ filter a column…") asks for it.

   The bar exists because the row was measured on a real seven-table case:
   27 boxes on screen, 0 in use, and they are the heaviest thing in the
   viewport after the data. Which of the two a fresh install gets lives in
   one place (FILTER_UI_DEFAULT in settings.js), not in a comparison
   spelled out here — flipping it must be one edit, and the sentence above
   naming the shipped side is the only thing in this file that has to
   follow it. */
export const classicFilterRow = () => S.appearance.filterUi === 'row';

/* Whether this column's box is on screen. Under the classic row every
   column's is; under the bar only the ones the analyst opened. */
export const filterBoxOpen = (name) => classicFilterRow() || S.filterOpen.includes(name);

/* renderHead() for the paths that change the head's HEIGHT rather than only
   what is drawn in it — revealing or folding a box, and the Appearance
   switch between the two surfaces, all add or remove the whole filter row.

   #rows is positioned absolutely at headH(), and that top is written in one
   place only: syncRowsTop(), inside a paint. Repaint the head without
   repainting the rows and the two disagree by the row's ~29px — the first
   data row is drawn underneath the sticky header, a blank strip is left at
   the bottom, and rowAtClientY (which subtracts headH()) hands the gutter
   drag and the autoscroll a row that isn't the one under the pointer.
   Nothing repairs it by itself: a column already in view scrolls nowhere,
   so no scroll event fires, and folding a box away rebuilds nothing at all.
   Every other caller that resizes the head — a width drag, a pin, hiding a
   column — has always paired the two; this is that pair, named, so the
   filter surface cannot drift back out of it. */
export function renderHeadResized() {
  renderHead();
  render();
}

/* Reveal a column's box under its header and put the cursor in it. The
   header ⌕, a chip's label and the column picker all come through here, so
   the three cannot disagree about what "open" means or forget the scroll.

   Under the classic row there is nothing to reveal — the box is already
   there — so this is only the focus half. */
export function openColumnFilter(name, { focus = true } = {}) {
  // A hidden or grouped-away column has no header to reveal a box under.
  // Its filter is still real and its chip's ✕ still takes it off — this is
  // only the edit-in-place half, and saying why beats a click that does
  // nothing.
  if (!visibleCols().includes(name)) {
    toast(`"${name}" is not on screen — show it from the Columns panel to edit its filter here`);
    return;
  }
  if (!classicFilterRow() && !S.filterOpen.includes(name)) {
    S.filterOpen = [...S.filterOpen, name];
    renderHeadResized();
  }
  if (!focus) return;
  const inp = document.querySelector(`.fcell input[data-col="${CSS.escape(name)}"]`);
  if (!inp) return;
  // A column off the right edge has a box nobody can see, and focusing it
  // would scroll the grid there with no warning. Bring it in deliberately,
  // by the nearest amount, so the columns either side stay in frame.
  inp.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  inp.focus();
  inp.select();
}

/* Put a revealed box away again. Not a filter change: an open box with
   text in it folds back into its chip, which is where a set filter lives
   when it is not being edited. */
export function closeColumnFilter(name) {
  if (!S.filterOpen.includes(name)) return;
  S.filterOpen = S.filterOpen.filter((n) => n !== name);
  renderHeadResized();
}

export function toggleColumnFilter(name) {
  if (S.filterOpen.includes(name)) closeColumnFilter(name); else openColumnFilter(name);
}

/* The bar, painted on its own. Separate from renderHead because
   setColumnFilter (the row menu's Filter to…, the `f` keybind, the value
   picker's single-value case) deliberately avoids a full head render —
   that would drop the cell selection the caller is still acting on — and
   the chip is the only place those writes show up once the boxes are gone.

   Owns its own `hidden`, all three reasons for it: the classic row is on,
   no table is open, or a page tab is up (syncTabChrome calls this for the
   same reason it hides the toolbar — the bar describes the grid). */
export function renderFilterBar() {
  const bar = $('filterBar');
  if (!bar) return;
  bar.replaceChildren();
  bar.hidden = classicFilterRow() || !S.sourceId || S.activeTab !== 'grid';
  if (bar.hidden) return;

  const label = el('span', 'filter-bar-label', 'Filters');
  // The scope, said once here rather than guessed at: the guided tree, the
  // tag filter and the timeframe narrow the table too and have their own
  // controls, so "Filters — none" must not read as "nothing is hidden".
  label.title = 'The per-column filters on this table. The filter builder, tags and the timeframe have their own controls in the toolbar.';
  bar.append(label);

  // A column being edited in place is not also a chip: the box IS that
  // filter while it is open, and two copies of one filter a keystroke
  // apart is exactly the confusion the bar exists to remove.
  const chips = columnFilterChips().filter((c) => !S.filterOpen.includes(c.column));
  for (const c of chips) bar.append(filterChip(c));
  if (!chips.length && !S.filterOpen.length) bar.append(el('span', 'filter-bar-none', 'none'));

  const add = el('button', 'btn ghost filter-add', '+ filter a column…');
  add.id = 'filterAdd';
  add.title = 'Open a column’s filter box — including one scrolled off the right edge';
  add.onclick = (e) => { e.stopPropagation(); openFilterColumnPicker(add); };
  bar.append(add);
}

/* One chip: the column, what it is filtered to, and the way off. The label
   is a button rather than text because "click the filter to change it" is
   the gesture people try first, and it lands on the same reveal the header
   ⌕ does. */
function filterChip(c) {
  const chip = el('span', 'filter-chip');
  chip.dataset.col = c.column;
  const label = el('button', 'filter-chip-label');
  label.append(el('span', 'filter-chip-col', c.column), el('span', 'filter-chip-val', c.text));
  label.title = `${c.column}: ${c.full}\n\nClick to edit this filter under its column`;
  label.onclick = () => openColumnFilter(c.column);
  const rm = el('button', 'filter-chip-rm', '✕');
  rm.title = `Remove the filter on ${c.column}`;
  rm.setAttribute('aria-label', `Remove the filter on ${c.column}`);
  rm.onclick = () => removeColumnFilter(c.column);
  chip.append(label, rm);
  return chip;
}

/* "+ filter a column…": the way to filter a column a long way off the right
   edge, and the answer to "where did the boxes go" for anyone meeting the
   bar for the first time. Searchable because a KAPE EVTX table carries
   thirty-odd columns and reading the list is slower than typing three
   letters. Visible columns only — the others have no header to open a box
   under (see openColumnFilter). */
export function openFilterColumnPicker(anchorEl) {
  const cols = visibleCols();
  return anchoredPanel(anchorEl, 'filter-col-picker', (p, close) => {
    const search = el('input', 'vp-search');
    search.type = 'search';
    search.placeholder = 'Find a column…';
    const list = el('div', 'vp-list');
    const paint = () => {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      const hits = cols.filter((n) => !q || n.toLowerCase().includes(q));
      for (const n of hits) {
        const b = el('button', 'menu-item', n);
        if (S.filters[n]) b.classList.add('filtered');
        b.onclick = () => { close(); openColumnFilter(n); };
        list.append(b);
      }
      if (!hits.length) list.append(el('div', 'vp-note', 'No column matches that.'));
    };
    search.oninput = paint;
    // Enter takes the first hit: typing three letters and pressing Enter is
    // the whole gesture for someone who knows which column they want.
    search.onkeydown = (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const first = list.querySelector('.menu-item');
      if (first) first.click();
    };
    p.append(search, list);
    paint();
    setTimeout(() => search.focus(), 0);
  });
}

export function renderHead() {
  S.cellRange = null; // column order/visibility/width changes invalidate cell-range column indices
  S.cellAnchor = null;
  renderGroupStrip();
  const head = $('headRow');
  const filt = $('filterRow');
  head.replaceChildren();
  filt.replaceChildren();

  // .gutter-head mirrors .gutter's three-slot grid exactly (checkbox |
  // stripes | right-aligned row number) so the select-all box sits directly
  // above the row checkboxes and "Line" sits directly above the rid digits.
  // Not sortable, unlike every other hcell — hence gutter-head's own
  // cursor/hover treatment rather than .hcell's.
  const gh = el('div', 'hcell gutter-head');
  gh.style.flexBasis = GUTTER_W + 'px';
  const selectAllCb = el('input');
  selectAllCb.type = 'checkbox';
  selectAllCb.id = 'selectAllRows';
  selectAllCb.className = 'select-all-rows';
  selectAllCb.title = 'Select every row in the current view';
  selectAllCb.onchange = () => {
    if (S.groupByCols.length || !S.view) { selectAllCb.checked = false; return; }
    selSnapshot();
    selectAllCb.checked ? selSetAll() : selClear();
    S.cellRange = null;
    S.cellAnchor = null;
    render();
  };
  // The Line label is the way BACK from any sort: original file order,
  // click again for reverse. An empty sort IS line order (the view's
  // rid-ascending default), so that state shows the ▲.
  const lineLabel = el('span', 'label line-sort', 'Line');
  const lineDesc = S.sort.length === 1 && S.sort[0].column === '__line__' && S.sort[0].dir === 'desc';
  if (!S.sort.length) lineLabel.append(el('span', 'sort', '▲'));
  else if (lineDesc) lineLabel.append(el('span', 'sort', '▼'));
  lineLabel.title = 'Sort by original line order — click again for reverse';
  lineLabel.style.cursor = 'pointer';
  lineLabel.onclick = () => {
    S.sort = S.sort.length ? [] : [{ column: '__line__', dir: 'desc' }];
    renderHead();
    saveLayout();
    rebuildView({ keepScroll: false });
  };
  gh.append(selectAllCb, el('span', 'gutter-mid'), lineLabel);
  head.append(gh);

  // A box can only be open on a column that is still on screen: hiding a
  // column from the columns panel, grouping by it, or removing a derived
  // column would otherwise leave a name in S.filterOpen that nothing ever
  // renders and nothing ever drops.
  const shown = visibleCols();
  S.filterOpen = S.filterOpen.filter((n) => shown.includes(n));

  const gf = el('div', 'fcell gutter-filter');
  gf.style.flexBasis = GUTTER_W + 'px';
  filt.append(gf);

  const pins = pinnedOffsets();
  for (const name of visibleCols()) {
    const w = colWidth(name);
    const h = el('div', 'hcell');
    h.style.flexBasis = w + 'px';
    applyPin(h, name, pins);
    h.draggable = true;
    h.dataset.col = name;
    wireColumnDrag(h, name);
    h.append(el('span', 'label', name));
    const si = S.sort.findIndex((s) => s.column === name);
    if (si >= 0) {
      h.append(el('span', 'sort', (S.sort[si].dir === 'asc' ? '▲' : '▼') + (S.sort.length > 1 ? si + 1 : '')));
    }
    const colMetaEntry = columnMeta(name);
    if (colMetaEntry && colMetaEntry.derived) {
      // Marks the column as the analyst's own addition rather than
      // something that came out of the evidence file.
      const mark = el('span', 'hcell-derived', 'ƒ');
      const dstatus = colMetaEntry.derived_status;
      mark.title = `Derived from "${colMetaEntry.derived_from}" — ${opLabel(colMetaEntry.derived_op)}`
        + (dstatus === 'building' ? ' (building…)' : '')
        + (dstatus === 'partial' ? ' (incomplete — re-derive to finish)' : '');
      if (dstatus !== 'ready') mark.classList.add('pending');
      h.append(mark);
    }
    if (!classicFilterRow()) {
      // The one-click way in, in the place the box it opens will appear.
      // Laid out at all times rather than appearing on hover, for the same
      // reason .fcell-pick is: a header row that reflows as the pointer
      // crosses it is worse than a quiet glyph. It does cost ~13px of every
      // header — the charge that got the column-options ▾ removed — and the
      // trade is different here: that one was a rarely-used menu, this one
      // replaces a whole row of boxes with a row of nothing, and filtering
      // a column is the everyday verb it pays for.
      const fo = el('button', 'hcell-filter', '⌕');
      fo.dataset.col = name;
      fo.tabIndex = -1;
      const open = S.filterOpen.includes(name);
      const filtered = !!(S.filters[name] || pickerTreeNode(name));
      if (open) fo.classList.add('open');
      if (filtered) fo.classList.add('active');
      fo.setAttribute('aria-pressed', String(open));
      fo.title = filtered
        ? `${name} is filtered — click to edit the filter here`
        : `Filter ${name}`;
      fo.onclick = (ev) => {
        // A modified click belongs to the header, not to this button:
        // Alt-click pins and Shift-click adds a sort, and landing one of
        // those on the glyph of a narrow column must not mean something
        // else. Falling through (no stopPropagation) is what delivers it.
        if (ev.altKey || ev.shiftKey || ev.ctrlKey || ev.metaKey) return;
        ev.stopPropagation();
        toggleColumnFilter(name);
      };
      h.append(fo);
    }
    if (colMetaEntry) {
      // Column options (display format, "Derive a column from this…",
      // the derived-column actions) are a right-click, not a ▾ button that
      // spent a slot of every header's width forever to be used rarely —
      // same move the tab strip's ▦ made. The header's title carries the
      // discovery burden the glyph used to.
      h.oncontextmenu = (e) => {
        e.preventDefault();
        e.stopPropagation();
        contextMenu(e, columnMenuItems(name));
      };
      h.title = 'Click to sort · Shift-click to add a sort · Alt-click to pin · Right-click for column options';
    }
    h.onclick = (e) => {
      // Alt-click pins rather than sorts. Sorting is the header's primary
      // job and keeps the bare click; pinning is the same "about this
      // column" gesture with a modifier, like Shift-click for multi-sort.
      if (e.altKey) {
        e.preventDefault();
        toast(togglePin(name) ? `Pinned "${name}"` : `Unpinned "${name}"`);
        return;
      }
      const cur = S.sort.find((s) => s.column === name);
      const dir = cur && cur.dir === 'asc' ? 'desc' : 'asc';
      if (e.shiftKey) {
        if (cur) cur.dir = dir; else S.sort.push({ column: name, dir });
      } else {
        S.sort = [{ column: name, dir }];
      }
      renderHead();
      rebuildView();
    };
    const grip = el('div', 'grip');
    grip.draggable = false;
    grip.onmousedown = (e) => startResize(e, name);
    grip.onclick = (e) => e.stopPropagation();
    grip.ondblclick = (e) => { e.stopPropagation(); autofitOneColumn(name); };
    grip.title = 'Drag to resize, double-click to autofit this column';
    h.append(grip);
    head.append(h);

    // Under the bar the row is mostly empty cells: a column whose box is
    // not open still needs one, at its own width, or the open box stops
    // sitting under the header it belongs to.
    const f = el('div', 'fcell');
    applyPin(f, name, pins);
    f.style.flexBasis = w + 'px';
    if (!filterBoxOpen(name)) { filt.append(f); continue; }
    const inp = el('input');
    inp.value = S.filters[name] || '';
    inp.placeholder = 'filter';
    inp.dataset.col = name;
    if (inp.value) inp.classList.add('active');
    inp.oninput = () => {
      S.filters[name] = inp.value;
      inp.classList.toggle('active', !!inp.value);
      rebuildSoon();
    };
    inp.onkeydown = (e) => {
      // Escape clears; Enter commits. Under the bar both also put the box
      // away — it was revealed for one edit, and a box left open with its
      // filter in it is the row this replaced, rebuilt one column at a
      // time. What the filter says then is the chip's job. Under the
      // classic row there is nothing to put away.
      if (e.key === 'Escape') {
        inp.value = ''; S.filters[name] = ''; inp.classList.remove('active');
        closeColumnFilter(name);
        rebuildView();
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        closeColumnFilter(name);
        rebuildView();
        $('body').focus();
      }
    };
    f.append(inp);
    if (valueFilterEnabled(name)) {
      // Excel's funnel, in the place the filter it writes will appear.
      // Whether it's here at all is the size rule + the table menu's
      // overrides — see valueFilterEnabled.
      const pick = el('button', 'fcell-pick', '▾');
      pick.dataset.col = name;
      pick.tabIndex = -1;
      // A selection the box couldn't spell lives in the filter tree instead
      // (see setPickerTreeNode), which would otherwise leave this column
      // looking unfiltered — the box next to it is empty.
      const inTree = !!pickerTreeNode(name);
      if (inTree) pick.classList.add('active');
      pick.title = inTree
        ? `${name} is filtered to picked values — shown under Filters ▾ because the filter box can't spell them`
        : `Pick values to filter ${name} by`;
      pick.onclick = (ev) => { ev.stopPropagation(); openValuePicker(name, pick); };
      f.append(pick);
    }
    filt.append(f);
  }

  // Nothing open means the row is a line of empty cells across the whole
  // table — which is the thing the bar exists to stop drawing.
  filt.hidden = !classicFilterRow() && !S.filterOpen.length;
  renderFilterBar();
}

export function startResize(e, name) {
  e.preventDefault();
  e.stopPropagation();
  const x0 = e.clientX;
  const w0 = colWidth(name);
  const move = (ev) => {
    const w = Math.max(48, w0 + ev.clientX - x0);
    S.layout[name] = { ...(S.layout[name] || {}), w };
    renderHead();
    render();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    saveLayout();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

export const saveLayout = debounce(() => {
  if (!S.sourceId) return;
  post('/api/layout', {
    source_id: S.sourceId,
    payload: { columns: S.layout, order: S.order, sort: S.sort, value_filters: S.valueFilterMode, hide_empty_rows: S.hideEmptyRows },
  }).catch(() => {});
}, 400);

/* Saves the current column order/visibility/timestamp-format as the
   cross-case default for this exact header set (workspace/column_layouts.json,
   outside any single case — same home as saved filters and the default tag
   template) — so importing another file with the same headers later opens
   to it. Independent of saveLayout() above, which persists per-source
   inside this one case. */
export async function saveDefaultLayout() {
  if (!S.sourceId || !S.columns.length) return;
  try {
    await post('/api/column_layouts', {
      // Keyed by the imported file's own columns: a derived column is this
      // analyst's addition, and including it would stop the same file
      // matching this layout when it's opened somewhere else.
      col_names: baseColumns().map((c) => c.name), order: S.order, columns: S.layout,
    });
    toast('Saved as the default layout for this set of columns');
  } catch (e) {
    toast('Could not save default layout: ' + e.message, 4000);
  }
}

/* ------------------------------------------------------- column autosize */

export async function fetchColumnMaxLens() {
  if (!S.sourceId) return null;
  try { return await api(`/api/column_maxlen?source_id=${S.sourceId}`); }
  catch (e) { toast('Could not measure column widths: ' + e.message); return null; }
}

/* 0/null means "no cap". Stored with the other per-browser look-and-feel
   preferences rather than in the layout: it's a statement about this
   screen, not about this table's columns. */
export function autofitMaxWidth() {
  const v = S.appearance.autofitMax;
  if (v === 0 || v === null) return 0;
  return Number(v) > 0 ? Number(v) : AUTOFIT_MAX_W_DEFAULT;
}

/* What the header cell actually needs, measured off the live DOM rather
   than estimated from the column name's length. The estimate ignored
   everything the header carries besides its text — the sort arrow, the ▾
   options button, the derived ƒ mark, 8px of padding either side — and the
   header font is uppercase and letter-spaced, so it isn't 7px/char either.
   That's how a fit-to-content pass could leave "EVEN…▾" sitting over a
   column of 1s. `scrollWidth` gives the label's full text width even while
   it's clipped; the difference between the cell's own clientWidth (which
   includes its padding) and the label's is everything else in the row. The
   grip is absolutely positioned, so it isn't in that difference.

   Returns 0 for a column with no header on screen (hidden, or a caller
   running before the first renderHead) — callers fall back to the estimate. */
export function headerWidthFor(name) {
  const h = document.querySelector(`.hcell[data-col="${CSS.escape(name)}"]`);
  const label = h && h.querySelector('.label');
  if (!label) return 0;
  // Everything in the cell that isn't the label, measured from the siblings
  // themselves — NOT from `h.clientWidth - label.clientWidth`, which is only
  // the chrome while the cell is exactly as wide as its contents. On a column
  // that's wider than it needs to be, that difference is mostly slack, so the
  // header reported needing roughly the current width and autofit could never
  // shrink a column back to its content. (.grip is absolutely positioned and
  // occupies no track, so it isn't counted; .label can shrink but not grow,
  // so its scrollWidth is the text width whether it's clipped or not.)
  const cs = getComputedStyle(h);
  const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
  const gap = parseFloat(cs.columnGap === 'normal' ? cs.gap : cs.columnGap) || 0;
  let extras = 0;
  let siblings = 0;
  for (const child of h.children) {
    if (child === label || child.classList.contains('grip')) continue;
    extras += child.getBoundingClientRect().width;
    siblings += 1;
  }
  return Math.ceil(label.scrollWidth + extras + gap * siblings + pad) + 1;
}

export function widthForLen(name, len) {
  const dataPx = Math.max(60, (len || 0) * 7 + 24);
  const headPx = headerWidthFor(name) || (name.length * 7 + 24);
  const px = Math.max(dataPx, headPx);
  const cap = autofitMaxWidth();
  if (!cap) return px;
  // The header is allowed past the cap: a column whose *name* is cut off is
  // unreadable in a way a truncated value isn't — you can widen a column you
  // can still identify. Only to 2x, so one absurd header can't defeat the
  // cap's whole purpose either.
  return Math.min(px, Math.max(cap, Math.min(headPx, cap * 2)));
}

export function resetAllColumnWidths() {
  if (!S.sourceId) return;
  for (const name of visibleCols()) {
    if (S.layout[name]) delete S.layout[name].w;
  }
  renderHead(); render(); saveLayout();
  toast('Column widths reset to default');
}

export async function autofitAllColumnWidths() {
  if (!S.sourceId) return;
  toast('Measuring columns…', 8000);
  const maxlens = await fetchColumnMaxLens();
  if (!maxlens) return;
  for (const name of visibleCols()) {
    S.layout[name] = { ...(S.layout[name] || {}), w: widthForLen(name, maxlens[name]) };
  }
  renderHead(); render(); saveLayout();
  toast('Columns autofit to content');
}

export async function autofitOneColumn(name) {
  if (!S.sourceId) return;
  const maxlens = await fetchColumnMaxLens();
  if (!maxlens) return;
  S.layout[name] = { ...(S.layout[name] || {}), w: widthForLen(name, maxlens[name]) };
  renderHead(); render(); saveLayout();
}

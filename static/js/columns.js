/* Column layout: header rendering, drag-to-reorder, widths and autofit.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, AUTOFIT_MAX_W_DEFAULT, GUTTER_W, api, debounce, el, post, toast } from './core.js';
import { columnMenuItems, opLabel } from './derived.js';
import { openValuePicker, pickerTreeNode, valueFilterEnabled } from './filters.js';
import { render } from './grid.js';
import { renderGroupStrip } from './grouping.js';
import { S, selClear, selSetAll } from './state.js';
import { baseColumns, columnMeta } from './tsformat.js';
import { contextMenu } from './ui.js';
import { rebuildSoon, rebuildView } from './view.js';

/* ---------------------------------------------------------------- header */

export function colWidth(name) {
  const l = S.layout[name] || {};
  if (l.w) return l.w;
  const c = S.columns.find((x) => x.name === name);
  if (!c) return 140;
  if (c.type === 'datetime') return 190;
  if (c.type === 'number') return 100;
  return Math.min(360, Math.max(90, name.length * 9 + 30));
}

export const visibleCols = () => S.order.filter((n) => !(S.layout[n] || {}).hidden);

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
    selectAllCb.checked ? selSetAll() : selClear();
    S.cellRange = null;
    S.cellAnchor = null;
    render();
  };
  gh.append(selectAllCb, el('span', 'gutter-mid'), el('span', 'label', 'Line'));
  head.append(gh);

  const gf = el('div', 'fcell gutter-filter');
  gf.style.flexBasis = GUTTER_W + 'px';
  filt.append(gf);

  for (const name of visibleCols()) {
    const w = colWidth(name);
    const h = el('div', 'hcell' + ((S.layout[name] || {}).pinned ? ' pinned' : ''));
    h.style.flexBasis = w + 'px';
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
    if (colMetaEntry) {
      // Column options (display format, "Add datetime column from this…",
      // the derived-column actions) are a right-click, not a ▾ button that
      // spent a slot of every header's width forever to be used rarely —
      // same move the tab strip's ▦ made. The header's title carries the
      // discovery burden the glyph used to.
      h.oncontextmenu = (e) => {
        e.preventDefault();
        e.stopPropagation();
        contextMenu(e, columnMenuItems(name));
      };
      h.title = 'Click to sort · Shift-click to add a sort · Right-click for column options';
    }
    h.onclick = (e) => {
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

    const f = el('div', 'fcell');
    f.style.flexBasis = w + 'px';
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
      if (e.key === 'Escape') { inp.value = ''; S.filters[name] = ''; inp.classList.remove('active'); rebuildView(); }
      if (e.key === 'Enter') { e.preventDefault(); rebuildView(); $('body').focus(); }
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
    payload: { columns: S.layout, order: S.order, sort: S.sort, value_filters: S.valueFilterMode },
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

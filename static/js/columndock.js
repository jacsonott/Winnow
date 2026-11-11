/* The column list, pinned to the bottom of the sidebar (#527).

   The table menu's Show, hide & order pane is the full column chooser,
   but it is a modal: it covers the grid it is changing, and it has to be
   opened again for every column. An analyst going through a wide table
   one column at a time ("is CommandLine in this one? show it, put it
   next to the user, hide the payload") wants the list on screen beside
   the grid, the way EDR and SIEM consoles keep their field list. This is
   that list, docked under the table tree in the sidebar: a filter box,
   a tick per column, drag to reorder, pin and ⤒ ⤓.

   It holds no state of its own. Every control writes S.layout / S.order
   through the same calls the table menu and the grid header use
   (setColumnHidden, togglePin, moveColumn, setColumnOrder), and it is
   repainted FROM that state by syncColumnDock, which renderHead calls at
   its end — every layout change already ends in renderHead, so a column
   hidden from the header, pinned with Alt-click or reordered by dragging
   a header shows here without a second set of call sites to keep in
   step. syncTabChrome calls it too, for the page tabs, which change what
   is on screen without touching the layout, and so does loadSources when
   no table is left to open (the last tab closed, the open table removed,
   a case with nothing open), which never reaches renderHead.

   Whether it is open, and how much of the sidebar it takes, are
   per-browser preferences in winnow.sidebar, beside the sidebar's own
   width and collapsed flag. Which columns it lists (All / Shown) is the
   table menu's preference, one setting for both lists. */
import { $, el } from './core.js';
import { S } from './state.js';
import { isPinned, moveColumn, setColumnHidden, setColumnOrder, showAllColumns, togglePin } from './columns.js';
import { columnMeta } from './tsformat.js';
import { loadSidebarPrefs, saveSidebarPrefs, setSidebarVisible, wireDragReorder } from './sources.js';
import { columnsListMode, openTableMenu, saveColumnsListMode, tableMenuSeg } from './timeframe.js';
import { closeModal } from './ui.js';

export const COLDOCK_RATIO_DEFAULT = 0.45;   // of the sidebar's height
export const COLDOCK_RATIO_MIN = 0.15;
export const COLDOCK_RATIO_MAX = 0.85;

/* The pane's live parts, built once by wireColumnDock. `rows` maps a
   column name to its row's nodes for the table now listed; `listedKey`
   is that table and its set of names, so syncColumnDock can tell a
   repaint (same columns, new ticks or order) from a different table
   (rows rebuilt). `keepListed` holds the columns unticked HERE while
   Shown is picked: they stay listed until the filter or the switch next
   changes, the table menu's rule, so a misclick is one click back. */
export const dock = {
  open: false, built: false, rows: new Map(), listedKey: null, keepListed: new Set(),
  root: null, search: null, seg: null, count: null, list: null, more: null, moreText: null, empty: null, body: null,
};

export function columnDockOpen() { return dock.open; }

function dockAvailable() {
  return S.activeTab === 'grid' && S.sourceId != null && !!(S.order && S.order.length);
}

/* The columns listed: the table's order, less any it is grouped by —
   those are in the group strip, not the grid, as in the table menu. */
function listedColumns() {
  return S.order.filter((n) => !S.groupByCols.includes(n));
}

export function setColumnDockOpen(open, { focus = false } = {}) {
  dock.open = !!open;
  saveSidebarPrefs({ columns: dock.open });
  // Pinning a list to a hidden sidebar would pin it nowhere.
  if (dock.open && $('sidebar').hidden) setSidebarVisible(true);
  $('colDock').hidden = !dock.open;
  $('colDockSplit').hidden = !dock.open;
  paintDockToggles();
  dock.listedKey = null;   // repaint from scratch: the table may have changed while it was shut
  syncColumnDock();
  if (dock.open && focus && dock.search && !dock.search.closest('[hidden]')) dock.search.focus();
  // The sidebar may just have been shown, which takes width from the
  // grid, and the grid only re-measures on 'resize' (wireSidebarResize
  // says why it has to be told).
  window.dispatchEvent(new Event('resize'));
}

export function toggleColumnDock() {
  setColumnDockOpen(!dock.open, { focus: !dock.open });
}

/* The sidebar head's toggle and the table menu's button say the same
   thing about the same state. */
export function paintDockToggles() {
  const b = $('btnColDock');
  if (b) {
    b.setAttribute('aria-pressed', String(dock.open));
    b.title = dock.open
      ? 'Unpin the column list from the bottom of this sidebar'
      : 'Pin the open table’s column list to the bottom of this sidebar — search, show, hide and reorder columns without opening the table menu';
  }
  for (const t of document.querySelectorAll('.tm-coldock-toggle')) {
    t.textContent = dock.open ? 'Unpin from sidebar' : 'Pin to sidebar';
    t.setAttribute('aria-pressed', String(dock.open));
  }
}

/* The table menu's button: pinning moves the list out of the dialog and
   into the sidebar, so the dialog closes and the pinned list takes the
   caret. Unpinning leaves the dialog where it is. */
export function pinFromTableMenu() {
  if (dock.open) { setColumnDockOpen(false); return; }
  closeModal();
  setColumnDockOpen(true, { focus: true });
}

export function setColumnDockRatio(r) {
  const ratio = Math.min(COLDOCK_RATIO_MAX, Math.max(COLDOCK_RATIO_MIN, Number(r) || COLDOCK_RATIO_DEFAULT));
  // A share of the sidebar, not pixels: the window and the sidebar's
  // other contents change height, and a fixed 400px list in a 500px
  // window would leave the table tree a sliver.
  $('colDock').style.flexBasis = `${(ratio * 100).toFixed(2)}%`;
  return ratio;
}

/* Called once from main.js, after initSidebar (which owns winnow.sidebar's
   other keys and the sidebar's visibility). */
export function initColumnDock() {
  const prefs = loadSidebarPrefs();
  setColumnDockRatio(prefs.columnsRatio ?? COLDOCK_RATIO_DEFAULT);
  dock.open = !!prefs.columns;
  $('colDock').hidden = !dock.open;
  $('colDockSplit').hidden = !dock.open;
  paintDockToggles();
}

function buildDock() {
  const root = $('colDock');
  root.replaceChildren();
  const head = el('div', 'coldock-head');
  const title = el('span', 'coldock-title', 'Columns');
  dock.count = el('span', 'coldock-count');
  const menu = el('button', 'coldock-headbtn', '⋯');
  menu.type = 'button';
  menu.title = 'Open the table menu, for value dropdowns, layouts and the rest of the column settings';
  menu.setAttribute('aria-label', 'Table menu');
  menu.onclick = () => openTableMenu();
  const close = el('button', 'coldock-headbtn coldock-close', '✕');
  close.type = 'button';
  close.title = 'Unpin the column list from the sidebar';
  close.setAttribute('aria-label', 'Unpin the column list');
  close.onclick = () => setColumnDockOpen(false);
  head.append(title, dock.count, menu, close);

  const body = el('div', 'coldock-body');
  const top = el('div', 'coldock-top');
  const search = el('input', 'panel-search coldock-search');
  search.type = 'search';
  search.placeholder = 'Filter columns…';
  search.autocomplete = 'off';
  search.spellcheck = false;
  search.setAttribute('aria-label', 'Filter columns');
  search.oninput = () => { dock.keepListed.clear(); applyDockFilter(); };
  const seg = tableMenuSeg([
    ['all', 'All', 'List every column, ticked or not'],
    ['shown', 'Shown', 'List only the columns the grid shows (the same switch as in the table menu)'],
  ], () => columnsListMode(), (key) => {
    saveColumnsListMode(key);
    dock.keepListed.clear();
    seg.paint();
    applyDockFilter();
  }, 'coldock-seg');
  seg.seg.setAttribute('aria-label', 'Columns to list');
  top.append(search, seg.seg);

  const list = el('div', 'coldock-list');
  list.setAttribute('role', 'list');
  list.setAttribute('aria-label', 'Columns of the open table');
  const more = el('div', 'collist-more coldock-more');
  const moreText = el('span', 'collist-more-text');
  const listAll = el('button', 'btn ghost mb-small', 'List all');
  listAll.type = 'button';
  listAll.onclick = () => { saveColumnsListMode('all'); seg.paint(); dock.keepListed.clear(); applyDockFilter(); };
  more.append(moreText, listAll);
  more.hidden = true;
  const scroller = el('div', 'coldock-scroll');
  scroller.append(list, more);

  const foot = el('div', 'coldock-foot');
  const showAll = el('button', 'btn ghost mb-small', 'Show all');
  showAll.type = 'button';
  showAll.title = 'Tick every column, so the grid shows them all';
  showAll.onclick = () => showAllColumns();
  foot.append(el('span', 'coldock-hint', 'Drag a row to reorder'), showAll);
  body.append(top, scroller, foot);

  const empty = el('div', 'coldock-empty', 'Open a table to list its columns here.');
  root.append(head, body, empty);
  Object.assign(dock, { root, search, seg, list, more, moreText, empty, body, built: true });
}

function rowFor(name) {
  const row = el('div', 'coldock-row');
  row.dataset.col = name;
  row.setAttribute('role', 'listitem');
  const lab = el('label', 'coldock-label');
  const cb = el('input');
  cb.type = 'checkbox';
  cb.onchange = () => {
    if (!cb.checked) dock.keepListed.add(name);
    setColumnHidden(name, !cb.checked);
  };
  const nm = el('span', 'coldock-name', name);
  const drv = el('span', 'coldock-derived', 'ƒ');
  lab.append(cb, nm, drv);
  const acts = el('span', 'coldock-acts');
  const pin = el('button', 'coldock-btn coldock-pin');
  pin.type = 'button';
  pin.onclick = () => togglePin(name);
  const top = el('button', 'coldock-btn coldock-move', '⤒');
  top.type = 'button';
  top.title = `Move "${name}" to the start`;
  top.onclick = () => moveColumn(name, 'top');
  const bottom = el('button', 'coldock-btn coldock-move', '⤓');
  bottom.type = 'button';
  bottom.title = `Move "${name}" to the end`;
  bottom.onclick = () => moveColumn(name, 'bottom');
  acts.append(pin, top, bottom);
  row.append(lab, acts);
  // Same drag vocabulary as the table menu's list and the tab strip. The
  // order is read at drop time from S.order, so the closure never holds
  // a stale one, and a drop between two rows the filter left showing
  // lands where the list shows it: the hidden rows are still in S.order.
  wireDragReorder(row, name, {
    containerSelector: '.coldock-list',
    rowSelector: '.coldock-row',
    horizontal: false,
    currentIds: () => [...S.order],
    onReorder: (order) => setColumnOrder(order),
  });
  return { row, cb, nm, drv, pin, lab };
}

function paintRow(name, r) {
  const hidden = !!(S.layout[name] || {}).hidden;
  r.cb.checked = !hidden;
  r.row.classList.toggle('off', hidden);
  const pinned = isPinned(name);
  r.row.classList.toggle('pinned', pinned);
  r.pin.textContent = pinned ? 'pinned' : 'pin';
  r.pin.setAttribute('aria-pressed', String(pinned));
  r.pin.title = pinned
    ? `"${name}" stays put when you scroll sideways — click to unpin`
    : `Keep "${name}" visible while scrolling sideways`;
  const c = columnMeta(name);
  const type = c ? c.type + (c.derived ? ' · derived' : '') : '';
  r.drv.hidden = !(c && c.derived);
  r.lab.title = type ? `${name} (${type})` : name;
}

/* Which rows the filter box and the All / Shown switch leave listed,
   and the line under the list that says how many Shown leaves out. */
export function applyDockFilter() {
  if (!dock.built) return;
  const q = dock.search.value.trim().toLowerCase();
  const mode = columnsListMode();
  let left = 0, listedNow = 0;
  for (const [name, r] of dock.rows) {
    const match = !q || name.toLowerCase().includes(q);
    const out = mode === 'shown' && !!(S.layout[name] || {}).hidden && !dock.keepListed.has(name);
    r.row.hidden = !match || out;
    if (match && out) left++;
    if (!r.row.hidden) listedNow++;
  }
  dock.more.hidden = !left;
  dock.more.classList.toggle('alone', !listedNow);
  const n = `${left} hidden column${left === 1 ? '' : 's'}`;
  dock.moreText.textContent = q
    ? `${n} ${left === 1 ? 'matches' : 'match'} “${dock.search.value.trim()}”`
    : `${n} not listed`;
}

/* Repaint the pane from S. Cheap when nothing it shows has changed, and
   in place when only the ticks, pins or order have: the filter typed,
   the scroll position and the focused control all survive a tick, a
   pin or a drop. Only a different table (or a column added or taken
   away) rebuilds the rows, and even then the filter text stays, so
   looking for "User" in one table and switching tabs keeps looking. */
export function syncColumnDock() {
  if (!dock.open) return;
  if (!dock.built) buildDock();
  const available = dockAvailable();
  dock.body.hidden = !available;
  dock.empty.hidden = available;
  // "Go back to a table" only when there is one to go back to: with the
  // last tab closed or removed, a page tab has none behind it either.
  dock.empty.textContent = S.activeTab === 'grid' || S.sourceId == null
    ? 'Open a table to list its columns here.'
    : 'The columns of the table you are looking at are listed here. Go back to a table to see them.';
  if (!available) { dock.count.textContent = ''; return; }
  const listed = listedColumns();
  const key = `${S.sourceId}\u0000${[...listed].sort().join('\u0000')}`;
  if (key !== dock.listedKey) {
    const scrollTop = dock.list.parentNode.scrollTop;
    const sameTable = dock.listedKey && dock.listedKey.split('\u0000')[0] === String(S.sourceId);
    dock.rows.clear();
    dock.keepListed.clear();
    dock.list.replaceChildren();
    for (const name of listed) {
      const r = rowFor(name);
      dock.rows.set(name, r);
      dock.list.append(r.row);
    }
    dock.listedKey = key;
    // A derived column added or removed keeps the place; another table
    // starts at its top.
    dock.list.parentNode.scrollTop = sameTable ? scrollTop : 0;
  } else {
    // Same columns: put the rows in S.order's order, moving only when it
    // changed — re-appending a row takes the focus off a control in it.
    const current = [...dock.list.children].map((n) => n.dataset.col);
    if (current.join('\u0000') !== listed.join('\u0000')) {
      for (const name of listed) dock.list.append(dock.rows.get(name).row);
    }
  }
  for (const [name, r] of dock.rows) paintRow(name, r);
  const shown = listed.filter((n) => !(S.layout[n] || {}).hidden).length;
  // "shown" is left to the tooltip: at the sidebar's default width the
  // header has room for the figures and not the word.
  dock.count.textContent = `${shown} of ${listed.length}`;
  dock.count.title = `${shown} of ${listed.length} columns shown in the grid`;
  dock.seg.paint();
  applyDockFilter();
}

/* The split between the table tree and the list: drag it, double-click
   to reset — the same handle idiom as the sidebar's own right edge. */
export function wireColumnDock() {
  const btn = $('btnColDock');
  if (btn) btn.onclick = () => toggleColumnDock();
  const split = $('colDockSplit');
  if (!split) return;
  split.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const box = $('sidebar').getBoundingClientRect();
    split.classList.add('dragging');
    let ratio = null;
    const move = (ev) => { ratio = setColumnDockRatio((box.bottom - ev.clientY) / box.height); };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      split.classList.remove('dragging');
      if (ratio != null) saveSidebarPrefs({ columnsRatio: ratio });
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
  split.addEventListener('dblclick', () => {
    saveSidebarPrefs({ columnsRatio: setColumnDockRatio(COLDOCK_RATIO_DEFAULT) });
  });
}

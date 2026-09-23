/* Keybindings: the default map, its migrations, and the action handlers.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { autofitAllColumnWidths, openColumnFilter, resetAllColumnWidths, saveDefaultLayout, visibleCols } from './columns.js';
import { openFilterBuilder } from './filterbuilder.js';
import { $, ROW_H } from './core.js';
import { currentModalAction, repaintOpenMenus, closeMenu, closeModal } from './ui.js';
import { toggleDetailPane } from './detail.js';
import { toggleHistogram } from './histogram.js';
import { filterBySelectedCell, openValuePickerForColumn, selectedCellTarget } from './filters.js';
import { headH, moveCell, render, selectCellRangeRows, toggleCursorRow } from './grid.js';
import { dropGrouping, handleCopyShortcut, toggleGrouping } from './grouping.js';
import { openPluginBundlesModal } from './bundles.js';
import { cycleSavedFilter, openFilterSqlTab } from './savedfilters.js';
import { expandSearch, openSearchAllModal } from './search.js';
import { applySqlTabToEditor, showGridTab } from './sql.js';
import { sqlClearSelection, sqlCopySelection, sqlSelectionCount, sqlTagHotkey } from './sqlassist.js';
import { openSettings } from './settings.js';
import { activateTabSlot, clearAllFilters, setSidebarVisible } from './sources.js';
import { S, clearCellSelection, selClear, selCount, selSetAll, selSnapshot } from './state.js';
import { openTablesManager } from './tables.js';
import { applyTag, applyTagToView, undoLastTagChange } from './tags.js';
import { doJumpTs, openJumpTsModal, openTableMenu, openTimeRangeModal, toggleTimeRange } from './timeframe.js';

/* ------------------------------------------------------ keyboard shortcuts */

/* Navigation/action keys are rebindable and persisted in localStorage — a
   per-machine preference, not tied to the case file. Tag hotkeys (1-9) stay
   governed entirely by tag_defs.hotkey via the tag editor; Escape stays
   hardcoded (universal dismiss key, not worth letting users lock themselves
   out of). Neither is part of this keymap. */
export const DEFAULT_KEYMAP = {
  moveDown: ['ArrowDown', 'j'],
  moveUp: ['ArrowUp', 'k'],
  // Left/right and the Ctrl jumps are NEW action names on purpose, not
  // extra keys on the two above. loadKeymap lets a stored array replace a
  // default one wholesale, so adding a chord to an action an analyst has
  // already used reaches nobody without a migration — while an action
  // name their keymap has never seen takes its default for free. Same
  // reasoning the v2 entry records for openFilterBuilder/openValuePicker.
  moveLeft: ['ArrowLeft'],
  moveRight: ['ArrowRight'],
  // Ctrl+Shift+Arrow needs no binding of its own: matchAction strips
  // Shift from a chord that did not match and tries again, so it lands
  // here and the handler reads e.shiftKey — the same route Shift+ArrowDown
  // takes to moveDown.
  jumpEdgeUp: ['Ctrl+ArrowUp'],
  jumpEdgeDown: ['Ctrl+ArrowDown'],
  jumpEdgeLeft: ['Ctrl+ArrowLeft'],
  jumpEdgeRight: ['Ctrl+ArrowRight'],
  pageDown: ['PageDown'],
  pageUp: ['PageUp'],
  jumpFirst: ['g'],
  jumpLast: ['G'],
  // '/' and the chord every analyst's hands already do. Ctrl+F is worth
  // taking rather than leaving to the browser: find-in-page can only
  // see the rows the virtualised grid has in the DOM (invariant #6), so
  // it answers "not found" on data that is right there. ⌘+F comes with
  // it, spelled in the dispatcher's pre-gate rather than as a second
  // binding here — see wireKeymap.
  focusSearch: ['/', 'Ctrl+f'],
  // No default: `f` is worth more as "filter to the value I'm looking at"
  // (below) than as "focus the first column's filter box", which is a click
  // away and was the less-used of the two. Still bindable in Settings.
  focusFilter: [],
  focusNote: ['n'],
  openSettings: ['?'],
  resetColumnWidths: ['0'],
  autofitColumnWidths: ['='],
  // q/w beside [/]: cycling saved filters is the highest-traffic key in a
  // triage pass, and it belongs under the resting left hand. The bracket
  // keys stay as aliases — muscle memory is never punished.
  cyclePrevFilter: ['[', 'q'],
  cycleNextFilter: [']', 'w'],
  openFilterBuilder: ['e'],
  openValuePicker: ['v'],
  filterBySelectedCell: ['f'],
  filterBySelectedCellOnly: ['F'],
  clearFilters: ['c'],
  // Ctrl+A, like everywhere else. The typing guard above the dispatcher
  // keeps it native inside inputs; grouped mode no-ops (the header
  // checkbox is disabled there for the same reason).
  selectAllRows: ['Ctrl+a'],
  openTables: ['t'],
  toggleSidebar: ['`'],
  openTableMenu: ['C'],
  openSearchAll: ['s'],
  toggleDetail: ['d'],
  dropGrouping: ['x'],
  saveDefaultLayout: ['L'],
  toggleTimeRange: ['r'],
  openTimeRange: ['R'],
  toggleHistogram: ['h'],
  toggleGrouping: ['X'],
  openFilterSql: ['Q'],
  openJumpTs: ['J', 'a'],
  repeatJumpTs: ['.'],
  openPluginBundles: ['M'],
};

export const ACTION_LABELS = {
  moveDown: 'Move the cell cursor down', moveUp: 'Move the cell cursor up',
  moveLeft: 'Move the cell cursor left', moveRight: 'Move the cell cursor right',
  jumpEdgeUp: 'Jump to the first row, keeping the column',
  jumpEdgeDown: 'Jump to the last row, keeping the column',
  jumpEdgeLeft: 'Jump to the first column', jumpEdgeRight: 'Jump to the last column',
  pageDown: 'Page down', pageUp: 'Page up',
  jumpFirst: 'Jump to first row', jumpLast: 'Jump to last row',
  focusSearch: 'Focus search box', focusFilter: 'Filter the column under the cursor',
  focusNote: 'Focus note field', openSettings: 'Open settings (keyboard shortcuts, filter syntax)',
  resetColumnWidths: 'Reset all column widths to default',
  autofitColumnWidths: 'Autofit all column widths to content',
  cyclePrevFilter: 'Previous saved filter', cycleNextFilter: 'Next saved filter',
  openFilterBuilder: 'Open the Filter builder (guided AND/OR conditions)',
  openValuePicker: "Open the value picker for the selected cell's column",
  filterBySelectedCell: "Filter by selected cell's value",
  filterBySelectedCellOnly: "Filter by selected cell's value, dropping every other filter",
  clearFilters: 'Clear all filters, search and tag filter',
  selectAllRows: 'Select every row in the current view (same as the header checkbox)',
  openTables: 'Open Tables manager',
  toggleSidebar: 'Show/hide the table sidebar',
  openTableMenu: 'Open the table menu (columns, value dropdowns) — also right-click a tab',
  openSearchAll: 'Search all tables',
  toggleDetail: 'Open/close the detail pane',
  dropGrouping: 'Drop all grouping, restore column order',
  saveDefaultLayout: "Save this column order/visibility as the default for this header set",
  toggleTimeRange: 'Toggle the timeframe filter on/off',
  openTimeRange: 'Open the timeframe filter (set column/range)',
  toggleHistogram: 'Show/hide the histogram of when the rows in this view happened',
  toggleGrouping: 'Toggle grouping off/on (remembers the last grouping)',
  openFilterSql: 'Open the current filter as a query in the SQL pane',
  openJumpTs: 'Jump to timestamp… (set the moment and column)',
  repeatJumpTs: 'Jump again to the saved timestamp (works across tables)',
  openPluginBundles: 'Profiles — the plugins, boards, watchlist and variables a kind of case carries',
};

/* Stored keymaps are a merge over the defaults, which means a returning
   analyst's localStorage silently outranks every later change to
   DEFAULT_KEYMAP — including a rename, which would leave a binding pointing
   at an action that no longer has a handler (matchAction would resolve the
   key and nothing would happen). So the stored map is migrated on load:
   entries for actions that no longer exist are carried to their replacement
   and dropped, and a *default* binding a change is meant to move is moved.
   A binding the analyst chose themselves is never touched — the marker
   below is what keeps each migration one-shot rather than fighting them
   over it on every load. */
export const KEYMAP_VERSION_KEY = 'winnow.keymap.v';

export const KEYMAP_VERSION = 5;

/* The four spec strings one Ctrl/⌘+F press can produce. keySpecFromEvent
   spells the plain chord 'Ctrl+f'; ⌘ makes it 'Meta+f', and Shift — or Caps
   Lock, which is the one that bites — makes e.key 'F'. It is the same chord
   under the analyst's fingers in all four cases, so the dispatcher matches
   the set rather than the one spelling the keymap stores, the way the copy
   handler matches 'c' or 'C'. Declared up here because the v5 migration
   below has to know the whole set too. */
const SEARCH_CHORD_SPECS = ['Ctrl+f', 'Ctrl+F', 'Meta+f', 'Meta+F'];

export const KEYMAP_MIGRATIONS = [
  // v1 (2026-08): the column chooser grew into the table menu, and `f`
  // moved from "focus the first filter box" to "filter by this value"
  // (Shift+F now doing that *and* clearing the other filters).
  (map) => {
    if (map.openColumns) map.openTableMenu = map.openColumns;
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    if (wasDefault('focusFilter', ['f']) && wasDefault('filterBySelectedCell', ['F'])) {
      map.focusFilter = [];
      map.filterBySelectedCell = ['f'];
      map.filterBySelectedCellOnly = ['F'];
    }
  },
  // v2 (2026-08): the left-hand pass. Saved-filter cycling gains q/w, the
  // timeframe gains a/A — additive aliases, applied only where the analyst
  // still had the old default so a deliberate rebinding is never touched.
  // (openFilterBuilder/openValuePicker are new actions; loadKeymap's
  // defaults-first merge supplies e/v without migration.)
  (map) => {
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    if (wasDefault('cyclePrevFilter', ['['])) map.cyclePrevFilter = ['[', 'q'];
    if (wasDefault('cycleNextFilter', [']'])) map.cycleNextFilter = [']', 'w'];
    if (wasDefault('toggleTimeRange', ['T'])) map.toggleTimeRange = ['T', 'a'];
    if (wasDefault('openTimeRange', ['R'])) map.openTimeRange = ['R', 'A'];
  },
  // v3 (2026-08): toggle moves T → r so the timeframe pair sits on one
  // letter — r toggles it, Shift+R opens the dialog. Same key, shift is
  // "the bigger version of the action", which is how the pair reads
  // naturally. `a`/`A` aliases stay.
  (map) => {
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    if (wasDefault('toggleTimeRange', ['T', 'a'])) map.toggleTimeRange = ['r', 'a'];
  },
  // v4 (2026-09): `a`/`A` move OFF the timeframe filter (it already lives on
  // r/R) and ONTO jump-to-timestamp, so `a` jumps to a moment rather than
  // toggling a filter. Only rewrites keymaps still on the v3 defaults —
  // a customised binding is left alone.
  (map) => {
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    if (wasDefault('toggleTimeRange', ['r', 'a'])) map.toggleTimeRange = ['r'];
    if (wasDefault('openTimeRange', ['R', 'A'])) map.openTimeRange = ['R'];
    if (wasDefault('openJumpTs', ['J'])) map.openJumpTs = ['J', 'a'];
  },
  // v5 (2026-09): Ctrl+F joins '/' on the search box. Additive, and only
  // where '/' is still the whole binding — an analyst who moved search
  // somewhere else keeps what they chose, and loses nothing, since the
  // pre-gate only claims the chord while focusSearch still holds it.
  // The second condition is the one this chord needs and the others
  // didn't: Ctrl+F matched nothing in Winnow until now, so Settings
  // accepted it for any action, and handing focusSearch the chord would
  // put the pre-gate in front of a binding someone chose — shadowed with
  // the chip still sitting in Settings. Where that has happened the
  // migration does nothing and their binding keeps working, since the
  // gate is off while focusSearch does not hold the chord.
  (map) => {
    const wasDefault = (action, keys) =>
      JSON.stringify((map[action] || []).slice().sort()) === JSON.stringify(keys.slice().sort());
    const chordTaken = Object.entries(map).some(([action, keys]) => action !== 'focusSearch'
      && Array.isArray(keys) && keys.some((k) => SEARCH_CHORD_SPECS.includes(k)));
    if (!chordTaken && wasDefault('focusSearch', ['/'])) map.focusSearch = ['/', 'Ctrl+f'];
  },
];

/* A *deep* copy: the settings UI's "+ key"/"✕" handlers mutate the key
   arrays in place, and a shallow `{...DEFAULT_KEYMAP}` hands them
   DEFAULT_KEYMAP's own arrays to mutate. That's how binding a key on a
   fresh profile used to edit the defaults themselves — after which "Reset
   to defaults" copied the polluted defaults back and appeared to do
   nothing. */
export const defaultKeymap = () =>
  Object.fromEntries(Object.entries(DEFAULT_KEYMAP).map(([action, keys]) => [action, [...keys]]));

export function loadKeymap() {
  let stored;
  try { stored = JSON.parse(localStorage.getItem('winnow.keymap') || '{}'); }
  catch { return defaultKeymap(); }
  if (!stored || typeof stored !== 'object') return defaultKeymap();

  let from = 0;
  try { from = Number(localStorage.getItem(KEYMAP_VERSION_KEY)) || 0; } catch { /* treat as unmigrated */ }
  const pending = KEYMAP_MIGRATIONS.slice(from);
  for (const migrate of pending) migrate(stored);

  // Actions the app no longer has (renamed, removed) would otherwise keep
  // swallowing their key forever, since matchAction scans the stored map,
  // not the defaults.
  const map = defaultKeymap();
  for (const [action, keys] of Object.entries(stored)) {
    if (action in DEFAULT_KEYMAP && Array.isArray(keys)) map[action] = keys;
  }
  if (pending.length) {
    try {
      localStorage.setItem('winnow.keymap', JSON.stringify(map));
      localStorage.setItem(KEYMAP_VERSION_KEY, String(KEYMAP_VERSION));
    } catch { /* a full/blocked localStorage just means it migrates again next load */ }
  }
  return map;
}

export function saveKeymap() {
  localStorage.setItem('winnow.keymap', JSON.stringify(S.keymap));
  localStorage.setItem(KEYMAP_VERSION_KEY, String(KEYMAP_VERSION));
}

/* A binding is stored as e.key, optionally prefixed with held modifiers in
   a fixed order: 'Ctrl+Alt+Meta+Shift+<key>'. Shift never appears for a
   printable key — e.key already arrives shifted (Shift+g is 'G'), so 'G'
   *is* the capital-letter binding — and appears for a non-printable key
   only when the binding asked for it, which is how an unprefixed
   'ArrowDown' keeps matching Shift+ArrowDown (the move handlers read
   e.shiftKey themselves to extend the selection). Returns null for a
   modifier pressed on its own, which is what lets the capture UI wait for
   the rest of a combination instead of binding "Control". */
export const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock', 'ScrollLock']);

export function keySpecFromEvent(e) {
  if (MODIFIER_KEYS.has(e.key)) return null;
  let mods = '';
  if (e.ctrlKey) mods += 'Ctrl+';
  if (e.altKey) mods += 'Alt+';
  if (e.metaKey) mods += 'Meta+';
  if (e.shiftKey && e.key.length > 1) mods += 'Shift+';
  return mods + e.key;
}

export function matchAction(e) {
  const spec = keySpecFromEvent(e);
  if (spec == null) return null;
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (keys.includes(spec)) return action;
  }
  // Shift on a non-printable key falls back to the unshifted binding (an
  // explicit 'Shift+F2' binding above already won if there was one) — this
  // is what keeps Shift+ArrowDown reaching moveDown to extend the
  // selection. Modifiers other than Shift never fall back: Alt+j is not a
  // request to move the cursor.
  const bare = spec.replace('Shift+', '');
  if (bare !== spec) {
    for (const [action, keys] of Object.entries(S.keymap)) {
      if (keys.includes(bare)) return action;
    }
  }
  return null;
}

/* Whether the search box still owns the chord. It is dispatched by hand,
   above the typing guard where matchAction never looks — but not
   unconditionally: take the binding off focusSearch in Settings and the
   browser's find-in-page comes back, which is what keeps the chip in
   Settings a statement about the app rather than decoration. */
const searchChordBound = () => (S.keymap.focusSearch || []).some((k) => SEARCH_CHORD_SPECS.includes(k));

/* Returns a human-readable description of what already owns `key`, or null
   if it's free. Checked against other keymap actions, tag hotkeys (which
   can change independently at any time via the tag editor), Escape, and
   the hardcoded modifier shortcuts the keydown listener handles before
   the keymap (copy, tag undo, Alt+digit tab switching). */
export function findKeyConflict(key, currentAction) {
  if (key === 'Escape') return 'the always-on close/clear action';
  if (/^[1-9]$/.test(key)) {
    const t = S.tags.find((x) => x.hotkey === key);
    return `the "${t ? t.name : 'tag'}" tag hotkey`;
  }
  if (/^(Ctrl|Meta)\+(c|C)$/.test(key)) return 'the copy shortcut';
  if (/^(Ctrl|Meta)\+z$/.test(key)) return 'the tag-undo shortcut';
  if (/^Alt\+[0-9]$/.test(key)) return 'tab switching (Alt+1–0)';
  // The search chord is dispatched before matchAction and in every
  // spelling of itself, so binding any of the four to another action
  // would silently do nothing — including the two the keymap has no way
  // to store on focusSearch ('Meta+f' and 'Meta+F'). Answered here rather
  // than left to the loop below, which only knows the one spelling
  // focusSearch actually holds.
  if (currentAction !== 'focusSearch' && SEARCH_CHORD_SPECS.includes(key) && searchChordBound()) {
    return ACTION_LABELS.focusSearch;
  }
  for (const [action, keys] of Object.entries(S.keymap)) {
    if (action !== currentAction && keys.includes(key)) return ACTION_LABELS[action] || action;
  }
  return null;
}

/* The shortcuts that still mean something when the grid isn't the active
   tab — everything else moves a cursor, edits the grid's view spec or
   tags its rows, none of which the analyst can see from the SQL, Timeline
   or a plugin tab. They used to fire anyway: a tag hotkey pressed on the
   SQL pane silently tagged whatever was selected in the grid behind it,
   which the tag ribbon at least hinted at before the toolbar started
   hiding itself there (see syncTabChrome). */
export const TAB_AGNOSTIC_ACTIONS = new Set(['openSettings', 'openTables', 'openSearchAll', 'openPluginBundles', 'toggleSidebar']);

export const ACTION_HANDLERS = {
  /* Every movement goes through moveCell, so the active cell and the row
     cursor can never drift apart. Shift extends the rectangle from the
     anchor (what a mouse drag does); Ctrl jumps to the far end. Shift and
     Ctrl together do both, which is how Ctrl+Shift+Arrow gets to be "grab
     everything from here to the edge" without a binding of its own. */
  moveDown: (e) => moveCell({ dr: 1, extend: e.shiftKey }),
  moveUp: (e) => moveCell({ dr: -1, extend: e.shiftKey }),
  moveLeft: (e) => moveCell({ dc: -1, extend: e.shiftKey }),
  moveRight: (e) => moveCell({ dc: 1, extend: e.shiftKey }),
  jumpEdgeUp: (e) => moveCell({ dr: -1, edge: true, extend: e.shiftKey }),
  jumpEdgeDown: (e) => moveCell({ dr: 1, edge: true, extend: e.shiftKey }),
  jumpEdgeLeft: (e) => moveCell({ dc: -1, edge: true, extend: e.shiftKey }),
  jumpEdgeRight: (e) => moveCell({ dc: 1, edge: true, extend: e.shiftKey }),
  pageDown: (e, pageRows) => moveCell({ dr: 1, rows: pageRows, extend: e.shiftKey }),
  pageUp: (e, pageRows) => moveCell({ dr: -1, rows: pageRows, extend: e.shiftKey }),
  jumpFirst: (e) => moveCell({ dr: -1, edge: true, extend: e.shiftKey }),
  jumpLast: (e) => moveCell({ dr: 1, edge: true, extend: e.shiftKey }),
  focusSearch: () => expandSearch(),
  // "Let me type a filter" — which under the filter bar means revealing a
  // box before there is one to focus. It goes through openColumnFilter, the
  // one place that knows how to reveal, scroll to and focus a column's box,
  // so the key means the same thing on both surfaces. It aims at the column
  // the cell cursor is in rather than the first on screen: pressing it with
  // a cell selected in the seventh column and landing in the first one's
  // box is how it used to behave and it was never what anyone wanted.
  focusFilter: () => {
    const cols = visibleCols();
    const name = cols[(S.cellRange && S.cellRange.c0) || 0] || cols[0];
    if (name) openColumnFilter(name);
  },
  focusNote: () => { if (!$('detail').hidden) $('noteInput').focus(); },
  openSettings: () => openSettings(),
  resetColumnWidths: () => resetAllColumnWidths(),
  autofitColumnWidths: () => autofitAllColumnWidths(),
  selectAllRows: () => {
    if (S.groupByCols.length || !S.view || !S.view.row_count) return;
    selSnapshot();
    selSetAll();
    clearCellSelection();
    render();
  },
  cyclePrevFilter: () => cycleSavedFilter(-1),
  cycleNextFilter: () => cycleSavedFilter(1),
  openFilterBuilder: () => openFilterBuilder(),
  // The selected cell's column, else the first visible one — `v` should
  // always land somewhere useful, not demand a cell click first.
  openValuePicker: () => {
    const target = selectedCellTarget();
    const column = target ? target.column : visibleCols()[0];
    if (column) openValuePickerForColumn(column);
  },
  filterBySelectedCell: () => filterBySelectedCell(),
  filterBySelectedCellOnly: () => filterBySelectedCell({ only: true }),
  clearFilters: () => clearAllFilters(),
  openTables: () => openTablesManager(),
  toggleSidebar: () => setSidebarVisible($('sidebar').hidden),
  openTableMenu: () => openTableMenu(),
  toggleDetail: () => toggleDetailPane(),
  openSearchAll: () => openSearchAllModal(),
  dropGrouping: () => { if (S.groupByCols.length) dropGrouping(); },
  saveDefaultLayout: () => saveDefaultLayout(),
  toggleTimeRange: () => toggleTimeRange(),
  openTimeRange: () => openTimeRangeModal(),
  toggleHistogram: () => toggleHistogram(),
  toggleGrouping: () => toggleGrouping(),
  openFilterSql: () => openFilterSqlTab(),
  openJumpTs: () => openJumpTsModal(),
  openPluginBundles: () => openPluginBundlesModal(),
  repeatJumpTs: () => doJumpTs(),
};

/* DOM wiring for this module, called once by main.js. The document-level
   key dispatcher lives here rather than where the old single file happened
   to put it (mid-way through the appearance section) — this is the module
   anyone changing a keybinding opens. */
export function wireKeymap() {
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    // A confirm/prompt overlay owns this Escape: its capture-phase
    // listener has already closed it (so the overlay is gone from the DOM
    // by the time this bubble listener runs) and called preventDefault,
    // which is the trace it leaves. The modal underneath must not close
    // too, or a declined confirm inside Saved filters would also fire the
    // return-to-Settings hook.
    if (e.defaultPrevented) return;
    // closeModal, not a bare hide: whatever armed itself for the close
    // (a return-to-Settings hook, the keybinding capture) has to hear it.
    if (!$('modal').hidden) { closeModal(); return; }
    if (typing) { e.target.blur(); $('body').focus(); return; }
    if (S.activeTab === 'sql' && sqlClearSelection()) return;
    // Escape lets go of everything picked — rows and the cell range — and
    // the chip's Undo brings it back if it was a slip.
    if (selCount() || S.cellRange) selSnapshot();
    selClear(); clearCellSelection(); S.selHidden = 0; render(); return;
  }
  // Space toggles the cursor row; Shift+Space turns a cell range into row
  // picks. Not in the rebindable map: a bare space is what the map can't
  // spell, and the grid is the only place it means anything.
  // Only when the grid itself has focus: a focused button, menu item or a
  // confirm dialog's OK gets its native Space, not a row toggle.
  const gridFocused = e.target === document.body || e.target === $('body') || $('body').contains(e.target);
  if (e.key === ' ' && S.activeTab === 'grid' && !typing && gridFocused && $('modal').hidden && !document.querySelector('.confirm-overlay')) {
    e.preventDefault();
    if (e.shiftKey) { if (!selectCellRangeRows()) toggleCursorRow(); } else toggleCursorRow();
    return;
  }
  /* Everything below acts on the case UI — the grid's cursor, its tabs, its
     modals (Tables, Search all, the timeframe dialog). On the home screen
     none of that is on screen, and firing anyway meant `t`/`R`/the rest
     opened panels for a case that isn't showing. Escape (above) still
     works — home has modals of its own to close. */
  if ($('app').hidden) return;
  // e.code first because Alt+digit doesn't produce a digit in e.key on
  // every layout (macOS Alt+1 is '¡'), and the tag hotkeys below want the
  // same thing for the same reason.
  const digit = e.code && e.code.startsWith('Digit') ? e.code.slice(5) : e.key;
  /* Tab switching (Alt + 1…0), deliberately above the `typing` guard: the
     SQL pane focuses its editor on arrival, so a shortcut that gave up
     there could carry you *into* that tab and never back out. Skipped
     while a dialog is up — the #modal singleton *or* a spawned
     confirm/prompt overlay (_spawnDialog builds its own, so one check
     doesn't cover the other) — since switching the tab behind a dialog
     that's waiting on an answer isn't what anyone means. Ahead of
     matchAction and the tag hotkeys below because neither of those looks
     at modifiers — '0' is bound to resetColumnWidths and 1–9 are tag
     hotkeys, and Alt+digit is meant for neither. (Shift+digit was the obvious row and is taken: it
     tags the whole view, or untags it when the whole view already
     carries that tag.) */
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^[0-9]$/.test(digit)) {
    if (!$('modal').hidden || document.querySelector('.confirm-overlay')) return;
    e.preventDefault();
    activateTabSlot(digit);
    return;
  }

  /* Ctrl/⌘+F — the find chord, taken rather than left to the browser.
     Chromium's find-in-page searches the DOM, and invariant #6 keeps only
     the visible window of rows in it, so on a two-million-row table it
     reports "not found" for a value that is certainly there. A wrong
     answer is worse than no answer in a tool people write reports from,
     so Winnow answers instead and preventDefault stops the native bar.

     Above the `typing` guard for the same reason Alt+digit is: the search
     box is exactly what you want from a filter cell or the SQL editor,
     and a chord that gave up there would hand back the bar that lies.
     Below the $('app').hidden gate, though — the home screen has no
     search box, and it is small enough to be entirely in the DOM, which
     is the one place find-in-page tells the truth.

     The match is the copy handler's shape — (ctrl || meta) on 'f' or 'F',
     so ⌘ arrives here rather than as a second binding, and Caps Lock
     (which makes e.key 'F') still opens the box — plus one term neither
     the copy nor the undo handler has: `!e.altKey`. Ctrl+Alt is AltGr on
     a European layout, and AltGr+F there is a character somebody is
     trying to type, not a chord.

     A dialog keeps its own find. #modal and a spawned confirm overlay own
     the keyboard (the gate below says so for every other key, and Ctrl+C
     already falls through to the native copy there), and a dialog's text
     really is all in the DOM. A dropdown menu is neither of those and is
     not a dialog — it is transient chrome that any other interaction
     dismisses — so it is dismissed and the chord taken, rather than left
     floating over a search box that just opened behind it.

     Off the grid, switch first: syncTabChrome hides the whole toolbar on
     a page tab, so focusing #search there would put the caret in a
     display:none input and read as a keystroke that did nothing. */
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'f' || e.key === 'F') && searchChordBound()) {
    if (!$('modal').hidden || document.querySelector('.confirm-overlay')) return;
    e.preventDefault();
    closeMenu();
    if (S.activeTab !== 'grid') showGridTab();
    expandSearch();
    return;
  }

  if (typing) return;

  /* A dialog owns the keyboard. With the filter builder (or any modal /
     confirm overlay) up, q/w kept cycling saved filters underneath it,
     digits kept tagging, Ctrl+C hijacked copying the dialog's own text.
     Escape already closed things above; everything else stops here — with
     ONE opening: the keybind that opens a dialog also closes it, so C
     toggles the table menu, e the builder, ? settings (the openers mark
     which action owns the showing modal). The settings pane's key-capture
     listener is separate and unaffected. */
  if (!$('modal').hidden || document.querySelector('.confirm-overlay')) {
    if (!$('modal').hidden && !document.querySelector('.confirm-overlay')) {
      const toggling = matchAction(e);
      if (toggling && toggling === currentModalAction()) {
        e.preventDefault();
        $('modal').hidden = true;
      }
    }
    return;
  }

  if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C')) {
    // SQL tab first: a selected result set copies as TSV. Otherwise the
    // grid's own copy — and if neither claims it, the browser's native
    // copy of whatever text is highlighted proceeds untouched.
    if (S.activeTab === 'sql' && window.getSelection().isCollapsed && sqlCopySelection()) {
      e.preventDefault();
      return;
    }
    if (S.activeTab === 'grid' && (S.cellRange || selCount() || S.cursor >= 0)) {
      e.preventDefault();
      handleCopyShortcut(e.shiftKey);
      return;
    }
  }

  /* Undo lives here rather than in S.keymap because matchAction only
     matches bare keys — the rebindable map has no notion of a modifier,
     and Ctrl+Z with no modifier check would fire on a bare 'z'. */
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
    e.preventDefault();
    undoLastTagChange();
    return;
  }

  const pageRows = Math.floor(($('body').clientHeight - headH()) / ROW_H) - 1;
  const action = matchAction(e);
  if (action && ACTION_HANDLERS[action] && (S.activeTab === 'grid' || TAB_AGNOSTIC_ACTIONS.has(action))) {
    e.preventDefault();
    ACTION_HANDLERS[action](e, pageRows);
    return;
  }
  if (/^[1-9]$/.test(digit) && S.activeTab === 'grid') {
    const t = S.tags.find((x) => x.hotkey === digit);
    // The row menu's tag flyout may be open (its Tag entry advertises
    // these keys); it repaints so its ✓ reads what just happened.
    if (t) { e.preventDefault(); Promise.resolve(e.shiftKey ? applyTagToView(t) : applyTag(t)).then(repaintOpenMenus); }
  }
  // The SQL pane's result rows are taggable too, when the query resolves
  // real rows and some are selected (invariant #9's spirit: same
  // operation, same keys).
  if (/^[1-9]$/.test(digit) && S.activeTab === 'sql' && sqlSelectionCount()) {
    const t = S.tags.find((x) => x.hotkey === digit);
    if (t) { e.preventDefault(); sqlTagHotkey(t, () => applySqlTabToEditor()); }
  }
});
}

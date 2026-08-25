/* Keybindings: the default map, its migrations, and the action handlers.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { autofitAllColumnWidths, resetAllColumnWidths, saveDefaultLayout, visibleCols } from './columns.js';
import { openFilterBuilder } from './filterbuilder.js';
import { $, ROW_H } from './core.js';
import { toggleDetailPane } from './detail.js';
import { filterBySelectedCell, openValuePickerForColumn, selectedCellTarget } from './filters.js';
import { headH, moveCursor, render } from './grid.js';
import { dropGrouping, handleCopyShortcut, toggleGrouping } from './grouping.js';
import { cycleSavedFilter, openFilterSqlTab } from './savedfilters.js';
import { expandSearch, openSearchAllModal } from './search.js';
import { openSettings } from './settings.js';
import { activateTabSlot, clearAllFilters } from './sources.js';
import { S, gridRowCount, selClear, selCount } from './state.js';
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
  pageDown: ['PageDown'],
  pageUp: ['PageUp'],
  jumpFirst: ['g'],
  jumpLast: ['G'],
  focusSearch: ['/'],
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
  openTables: ['t'],
  openTableMenu: ['C'],
  openSearchAll: ['s'],
  toggleDetail: ['d'],
  dropGrouping: ['x'],
  saveDefaultLayout: ['L'],
  toggleTimeRange: ['T', 'a'],
  openTimeRange: ['R', 'A'],
  toggleGrouping: ['X'],
  openFilterSql: ['Q'],
  openJumpTs: ['J'],
  repeatJumpTs: ['.'],
};

export const ACTION_LABELS = {
  moveDown: 'Move down', moveUp: 'Move up',
  pageDown: 'Page down', pageUp: 'Page up',
  jumpFirst: 'Jump to first row', jumpLast: 'Jump to last row',
  focusSearch: 'Focus search box', focusFilter: 'Focus first column filter',
  focusNote: 'Focus note field', openSettings: 'Open settings (keyboard shortcuts, filter syntax)',
  resetColumnWidths: 'Reset all column widths to default',
  autofitColumnWidths: 'Autofit all column widths to content',
  cyclePrevFilter: 'Previous saved filter', cycleNextFilter: 'Next saved filter',
  openFilterBuilder: 'Open the Filter builder (guided AND/OR conditions)',
  openValuePicker: "Open the value picker for the selected cell's column",
  filterBySelectedCell: "Filter by selected cell's value",
  filterBySelectedCellOnly: "Filter by selected cell's value, dropping every other filter",
  clearFilters: 'Clear all filters, search and tag filter',
  openTables: 'Open Tables manager',
  openTableMenu: 'Open the table menu (columns, value dropdowns) — also right-click a tab',
  openSearchAll: 'Search all tables',
  toggleDetail: 'Open/close the detail pane',
  dropGrouping: 'Drop all grouping, restore column order',
  saveDefaultLayout: "Save this column order/visibility as the default for this header set",
  toggleTimeRange: 'Toggle the timeframe filter on/off',
  openTimeRange: 'Open the timeframe filter (set column/range)',
  toggleGrouping: 'Toggle grouping off/on (remembers the last grouping)',
  openFilterSql: 'Open the current filter as a query in the SQL pane',
  openJumpTs: 'Jump to timestamp… (set the moment and column)',
  repeatJumpTs: 'Jump again to the saved timestamp (works across tables)',
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

export const KEYMAP_VERSION = 2;

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
export const TAB_AGNOSTIC_ACTIONS = new Set(['openSettings', 'openTables', 'openSearchAll']);

export const ACTION_HANDLERS = {
  moveDown: (e, pageRows) => moveCursor(S.cursor + 1, e.shiftKey),
  moveUp: (e, pageRows) => moveCursor(S.cursor - 1, e.shiftKey),
  pageDown: (e, pageRows) => moveCursor(S.cursor + pageRows, e.shiftKey),
  pageUp: (e, pageRows) => moveCursor(S.cursor - pageRows, e.shiftKey),
  jumpFirst: () => moveCursor(0, false),
  jumpLast: () => moveCursor(Math.max(0, gridRowCount() - 1), false),
  focusSearch: () => expandSearch(),
  focusFilter: () => { const i = document.querySelector('.fcell input'); if (i) { i.focus(); i.select(); } },
  focusNote: () => { if (!$('detail').hidden) $('noteInput').focus(); },
  openSettings: () => openSettings(),
  resetColumnWidths: () => resetAllColumnWidths(),
  autofitColumnWidths: () => autofitAllColumnWidths(),
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
  openTableMenu: () => openTableMenu(),
  toggleDetail: () => toggleDetailPane(),
  openSearchAll: () => openSearchAllModal(),
  dropGrouping: () => { if (S.groupByCols.length) dropGrouping(); },
  saveDefaultLayout: () => saveDefaultLayout(),
  toggleTimeRange: () => toggleTimeRange(),
  openTimeRange: () => openTimeRangeModal(),
  toggleGrouping: () => toggleGrouping(),
  openFilterSql: () => openFilterSqlTab(),
  openJumpTs: () => openJumpTsModal(),
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
    if (!$('modal').hidden) { $('modal').hidden = true; return; }
    if (typing) { e.target.blur(); $('body').focus(); return; }
    selClear(); render(); return;
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
     hotkeys, and Alt+digit is meant for neither. (Shift+digit was the obvious row and is taken: it applies a
     tag to the whole view.) */
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^[0-9]$/.test(digit)) {
    if (!$('modal').hidden || document.querySelector('.confirm-overlay')) return;
    e.preventDefault();
    activateTabSlot(digit);
    return;
  }

  if (typing) return;

  if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && (S.cellRange || selCount() || S.cursor >= 0)) {
    e.preventDefault();
    handleCopyShortcut(e.shiftKey);
    return;
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
    if (t) { e.preventDefault(); e.shiftKey ? applyTagToView(t) : applyTag(t); }
  }
});
}

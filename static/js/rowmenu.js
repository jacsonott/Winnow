/* The row right-click menu — a section registry, not a fixed list.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { displayValue, ellipsize, filterByValue, openValuePickerForColumn } from './filters.js';
import { rowAt } from './grid.js';
import { copyRowsAsText, writeClipboardText } from './grouping.js';
import { S, selCount, selPositions } from './state.js';
import { UNDO_NEXT, applyTag, undoLastTagChange } from './tags.js';
import { openTagEditor } from './timeframe.js';
import { contextMenu } from './ui.js';

/* ------------------------------------------------- row context menu */

/* Right-clicking a row opens the menu built from these sections. It's a
   registry rather than one function that spells the whole list out because
   this menu is now where per-row actions are expected to land — adding one
   should mean adding an entry here (or an item to an existing section),
   never surgery on a growing if-chain. Each section gets the same ctx and
   returns menu items (see fillMenuNode for the item shape); a section that
   doesn't apply returns [] and is skipped, separator and all.

   ctx: {pos, colName, colIndex, value} — the row and, when the click
   landed on a cell rather than the gutter, that cell's column and its
   value *at click time*. `pos` is deliberately re-resolved to a row on
   every repaint (rowAt(ctx.pos)) rather than captured: a keepOpen tag item
   re-renders the menu after tagging, and on the bulk path that tagging
   clears the page cache underneath it. */
export const ROW_MENU_SECTIONS = [
  { id: 'tags', build: rowMenuTagItems },
  { id: 'cell', build: rowMenuCellItems },
  { id: 'clipboard', build: rowMenuClipboardItems },
];

/* How many rows the menu's actions will hit: the selection when the
   right-clicked row is part of it, otherwise just that row (openRowContextMenu
   has already moved the cursor there). */
export function rowMenuTargets(ctx) {
  const n = selCount();
  return n ? { count: n, positions: () => selPositions() } : { count: 1, positions: () => [ctx.pos] };
}

export function rowMenuTagItems(ctx) {
  const { count } = rowMenuTargets(ctx);
  const scope = count > 1 ? `${count.toLocaleString()} selected rows` : 'this row';
  const items = [{ header: `Tag ${scope}` }];
  const row = rowAt(ctx.pos);
  for (const t of S.tags) {
    // The ✓ reads the right-clicked row even when a whole selection is the
    // target — same rule the hotkeys already follow (resolveTagDirection
    // resolves the toggle direction from one sample row), so the menu can't
    // claim a different outcome than pressing the tag's number would.
    const on = !!row && row.tags.includes(t.id);
    items.push({
      label: t.name,
      swatch: t.color,
      checked: on,
      hint: t.hotkey || '',
      keepOpen: true, // tagging three tags in a row shouldn't need three right-clicks
      title: `${on ? 'Remove' : 'Apply'} "${t.name}" — ${scope}`,
      onclick: () => applyTag(t, !on),
    });
  }
  if (!S.tags.length) items.push({ label: 'No tags in this case yet', disabled: true });
  if (UNDO_NEXT.available) {
    items.push({
      label: `Undo: ${UNDO_NEXT.label}`,
      hint: 'Ctrl+Z',
      onclick: () => undoLastTagChange(),
    });
  }
  items.push({ label: 'Edit tags…', onclick: openTagEditor });
  return items;
}

export function rowMenuCellItems(ctx) {
  if (!ctx.colName) return [];
  const shown = ellipsize(displayValue(ctx.value));
  return [
    { header: ctx.colName },
    { label: `Filter to ${shown}`, onclick: () => filterByValue(ctx.colName, ctx.value) },
    {
      label: `Filter to ${shown} only`,
      title: 'Drops every other filter and the search — the timeframe filter stays',
      onclick: () => filterByValue(ctx.colName, ctx.value, { only: true }),
    },
    { label: `Exclude ${shown}`, onclick: () => filterByValue(ctx.colName, ctx.value, { exclude: true }) },
    {
      // The way in when the column's own picker button is switched off for
      // size (see valueFilterEnabled) — an explicit click is consent to pay
      // for the scan, which the always-visible button isn't.
      label: 'Filter by values…',
      onclick: () => openValuePickerForColumn(ctx.colName),
    },
  ];
}

export function rowMenuClipboardItems(ctx) {
  const { count, positions } = rowMenuTargets(ctx);
  const rows = count > 1 ? `${count.toLocaleString()} rows` : 'row';
  return [
    '-',
    {
      label: 'Copy cell',
      disabled: !ctx.colName,
      onclick: () => writeClipboardText(Promise.resolve(String(ctx.value == null ? '' : ctx.value)), 'Copied cell'),
    },
    { label: `Copy ${rows}`, onclick: () => copyRowsAsText(positions(), false) },
    { label: `Copy ${rows} with headers`, onclick: () => copyRowsAsText(positions(), true) },
  ];
}

export function rowMenuItems(ctx) {
  const out = [];
  for (const section of ROW_MENU_SECTIONS) {
    const items = section.build(ctx);
    if (!items.length) continue;
    if (out.length && items[0] !== '-') out.push('-');
    out.push(...items);
  }
  return out;
}

export function openRowContextMenu(ctx, e) {
  contextMenu(e, () => rowMenuItems(ctx));
}

/* The row right-click menu — a section registry, not a fixed list.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { post, toast } from './core.js';
import { quickAddWidget } from './dashboard.js';
import { tableOf, widgetFrom } from './dashwidgets.js';
import { displayValue, ellipsize, filterByValue, openValuePickerForColumn } from './filters.js';
import { rowAt } from './grid.js';
import { copyRowsAsText, loadRowsForPositions, writeClipboardText } from './grouping.js';
import { showPluginTab } from './plugins.js';
import { S, selCount, selPositions } from './state.js';
import { UNDO_NEXT, applyTag, undoLastTagChange } from './tags.js';
import { openTagEditor } from './timeframe.js';
import { displayCell } from './tsformat.js';
import { contextMenu } from './ui.js';

/* ------------------------------------------------- row context menu */

/* Right-clicking a row opens the menu built from these sections. It's a
   registry rather than one function that spells the whole list out because
   this menu is now where per-row actions are expected to land — adding one
   should mean adding an entry here (or an item to an existing section),
   never surgery on a growing if-chain. Each section gets the same ctx and
   returns menu items (see fillMenuNode for the item shape); a section that
   doesn't apply returns [] and is skipped. Rules are placed by shape, not
   by section name: folded entries (submenus) read as one short list, and
   anything broken out beside them — the clicked column's filters, Undo —
   gets a rule on the side that meets a fold.

   ctx: {pos, colName, colIndex, value} — the row and, when the click
   landed on a cell rather than the gutter, that cell's column and its
   value *at click time*. `pos` is deliberately re-resolved to a row on
   every repaint (rowAt(ctx.pos)) rather than captured: a keepOpen tag item
   re-renders the menu after tagging, and on the bulk path that tagging
   clears the page cache underneath it. */
export const ROW_MENU_SECTIONS = [
  { id: 'tags', build: rowMenuTagItems },
  { id: 'cell', build: rowMenuCellItems },
  { id: 'dashboard', build: rowMenuDashboardItems },
  { id: 'clipboard', build: rowMenuClipboardItems },
  { id: 'plugins', build: rowMenuPluginItems },
];

/* The menu is one level deep at the top: the clicked column's filters
   stay broken out (they are what a right-click on a cell is usually for),
   and everything else folds into a submenu — Tag, Add to dashboard, Copy,
   Plugins — so the list stays short as plugins and tags grow. Items in
   the submenus that declare a pinId can be dragged (or starred) onto the
   top of the menu, where they stay, per machine: an analyst who runs one
   plugin's lookup fifty times a day keeps it one click away. */
export const ROW_MENU_PINS = 'row';

/* Plugin-registered row actions (PluginAPI.register_row_action) — the
   extension point for "do X with these rows": a VT lookup on the selected
   hashes, an enrichment that lands a table. The entry is disabled past
   the action's max_rows rather than hidden, so the analyst learns the
   limit instead of wondering where the item went. */
export function rowMenuPluginItems(ctx) {
  const actions = S.pluginRowActions || [];
  if (!actions.length) return [];
  const { count, positions, scope } = rowMenuTargets(ctx);
  const items = [];
  // The pin key is the action's filesystem identity (plugin folder +
  // local id), the one the dispatch route uses — published to plugin
  // authors in docs/writing-plugins.md, so it is spelled out once, here.
  for (const a of actions) {
    const tooMany = count > a.max_rows;
    items.push({
      label: a.label,
      note: a.plugin,
      pinId: `plugin:${a.plugin_fs}:${a.local_id}`,
      disabled: tooMany,
      title: tooMany
        ? `${a.label} takes at most ${a.max_rows.toLocaleString()} rows`
        : `${a.description || a.label} — ${scope} (${a.plugin} plugin)`,
      onclick: () => runPluginRowAction(a, positions(), ctx),
    });
  }
  return [{
    label: 'Plugins',
    hint: String(actions.length),
    title: 'Row actions the enabled plugins registered — drag one to the top of this menu to keep it there',
    submenu: items,
  }];
}

export async function runPluginRowAction(action, positions, ctx) {
  if (positions.length > 20000) { toast('Selection too large (max 20,000 rows)', 4000); return; }
  toast(`${action.label}…`, 8000);
  try {
    await loadRowsForPositions(positions);   // a selection can span unloaded pages
    const merged = S.sourceId < 0;
    const pairs = [];
    for (const pos of positions) {
      const r = rowAt(pos);
      if (r) pairs.push([merged ? r.source_id : S.sourceId, r.rid]);
    }
    const res = await post(`/api/plugins/row_action/${action.plugin_fs}/${action.local_id}`, {
      source_id: S.sourceId, pairs,
      column: ctx.colName || null,
      value: ctx.value == null ? null : String(ctx.value),
    });
    if (res && res.open_url) window.open(res.open_url, '_blank', 'noopener');
    if (res && res.show_tab) showPluginTab(res.show_tab);
    toast(res && res.message ? res.message : `${action.label}: done`, 6000);
  } catch (e) {
    toast(`${action.label} failed: ` + e.message, 6000);
  }
}

/* How many rows the menu's actions will hit: the selection when the
   right-clicked row is part of it, otherwise just that row (openRowContextMenu
   has already moved the cursor there). */
export function rowMenuTargets(ctx) {
  const n = selCount();
  const count = n || 1;
  return {
    count,
    positions: n ? () => selPositions() : () => [ctx.pos],
    // The wording that tells the analyst how many rows an action hits —
    // every section reads it from here, and the UI tests assert on it.
    scope: count > 1 ? `${count.toLocaleString()} selected rows` : 'this row',
    rows: count > 1 ? `${count.toLocaleString()} rows` : 'row',
  };
}

/* The tag list is a function, not an array: a keepOpen tag item repaints
   the flyout after tagging, and the ✓ has to read the row as it is now. */
export function rowMenuTagList(ctx) {
  const { scope } = rowMenuTargets(ctx);
  const items = [];
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
      // By name, not id: tag ids are per case file and reused, pins are
      // per machine — "the tag called Malicious" is what was pinned.
      pinId: `tag:${t.name}`,
      keepOpen: true, // tagging three tags in a row shouldn't need three right-clicks
      title: `${on ? 'Remove' : 'Apply'} "${t.name}" — ${scope}`,
      onclick: () => applyTag(t, !on),
    });
  }
  if (!S.tags.length) items.push({ label: 'No tags in this case yet', disabled: true });
  items.push('-', { label: 'Edit tags…', onclick: openTagEditor });
  return items;
}

/* The keys the tags actually carry (keymap.js dispatches 1–9 to
   tag_defs.hotkey), so the entry advertises what pressing them does here
   and says nothing when no tag has one. */
function tagHotkeyHint() {
  const keys = S.tags.map((t) => t.hotkey).filter(Boolean).sort();
  if (!keys.length) return '';
  return keys.length === 1 ? keys[0] : `${keys[0]}–${keys[keys.length - 1]}`;
}

export function rowMenuTagItems(ctx) {
  const { scope } = rowMenuTargets(ctx);
  const items = [{
    label: `Tag ${scope}`,
    hint: tagHotkeyHint(),
    title: 'The tags, with their hotkeys — pin the ones you use to the top of this menu',
    submenu: () => rowMenuTagList(ctx),
  }];
  // Undo sits at the top level, beside whatever just tagged — a pinned
  // tag included — rather than inside a flyout the tagging closed.
  if (UNDO_NEXT.available) {
    items.push({
      label: `Undo: ${UNDO_NEXT.label}`,
      hint: 'Ctrl+Z',
      onclick: () => undoLastTagChange(),
    });
  }
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

/* The value under the cursor as a number on a board — "how many rows
   have this?" — whose drill is exactly the filter the item above applies.
   Always offered, disabled when it can't apply (a gutter click has no
   value; a merged view is not one table a widget can query), so a pinned
   copy stays where the analyst put it rather than coming and going with
   where they right-clicked. */
export function rowMenuDashboardItems(ctx) {
  const merged = S.sourceId == null || S.sourceId < 0;
  const ok = !!ctx.colName && !merged;
  const shown = ok ? ellipsize(displayValue(ctx.value)) : '';
  return [{
    label: 'Add to dashboard',
    submenu: [{
      label: ok ? `Count of ${ctx.colName} = ${shown}` : 'Count of this value',
      pinId: 'dash:count-of-value',
      disabled: !ok,
      title: !ctx.colName ? 'Right-click a cell to count its value'
        : merged ? 'A merged view is not one table a widget can count'
        : 'A number on a dashboard that opens these rows when clicked',
      onclick: () => quickAddWidget(widgetFrom({
        template: 'countwhere', table: tableOf(S.sourceId), column: ctx.colName,
        value: ctx.value == null ? '' : String(ctx.value), match: 'equals' })),
    }],
  }];
}

export function rowMenuClipboardItems(ctx) {
  const { positions, rows } = rowMenuTargets(ctx);
  return [{
    label: 'Copy',
    submenu: [
      {
        label: 'Copy cell',
        pinId: 'copy:cell',
        disabled: !ctx.colName,
        onclick: () => writeClipboardText(Promise.resolve(String(displayCell(ctx.colName, ctx.value == null ? '' : ctx.value))), 'Copied cell'),
      },
      { label: `Copy ${rows}`, pinId: 'copy:rows', onclick: () => copyRowsAsText(positions(), false) },
      { label: `Copy ${rows} with headers`, pinId: 'copy:rows-headers', onclick: () => copyRowsAsText(positions(), true) },
    ],
  }];
}

const folded = (item) => !!(item && item !== '-' && !item.header && item.submenu);

export function rowMenuItems(ctx) {
  const out = [];
  for (const section of ROW_MENU_SECTIONS) {
    const items = section.build(ctx);
    if (!items.length) continue;
    // A rule wherever a section boundary has something broken out on
    // either side — the column's filter block, an Undo row — and none
    // between two folds, so Tag ▸ / Add to dashboard ▸ / Copy ▸ read as
    // one short list. No section is named here: a new one lands in the
    // registry and the rules follow from its shape.
    if (out.length && !(folded(out[out.length - 1]) && folded(items[0]))) out.push('-');
    out.push(...items);
  }
  return out;
}

export function openRowContextMenu(ctx, e) {
  contextMenu(e, () => rowMenuItems(ctx), { pins: ROW_MENU_PINS });
}

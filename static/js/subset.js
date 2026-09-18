/* Saving a view — or a hand-picked set of its rows — as a new table in the
   case: "keep these 300 rows where I can find them". The server does the
   copy (Store.save_view_as_source: view order, the parent's columns and
   types, tags and notes seeded, a subset_rids map back to the parent) and
   the result is an ordinary table badged ⊂ as a subset of its parent —
   removed like any other from the Tables manager, never auto-deleted.

   Two entry points share this module: the row menu's "Save as table"
   fold (the selection, or the clicked row, or the whole view) and
   Filters ▾ (the whole view). The shape rule is applyTag's: a select-all
   is sent as the view id plus the rows unchecked out of it, never
   materialised into positions (selPositions() on a 2M-row view is a
   2M-entry array); explicit picks are fetched first and refused past
   SUBSET_PICK_CAP with a pointer at the view route, which has no cap.

   Declarations only; wired through main.js. See docs/notes/ui.md. */
import { PAGE, post, toast } from './core.js';
import { currentSpec } from './filters.js';
import { rowAt } from './grid.js';
import { loadRowsForPositions, waitForPages } from './grouping.js';
import { loadSources, openSource, sourceLabel } from './sources.js';
import { S, selExcludedPairs, selExcludedPositions } from './state.js';
import { confirmDialog, promptDialog } from './ui.js';

/* Explicit picks travel as [source_id, rid] pairs, and the server's
   selection-remap ceiling (SELECTION_REMAP_MAX) is the honest bound for
   rows named one by one — past it the analyst is filtering, not picking. */
export const SUBSET_PICK_CAP = 20000;

export function subsetDefaultName() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  return `${sourceLabel(src) || 'table'} — subset`;
}

/* The shared tail: name it, POST, answer the soft-cap question, open the
   new table — saveResultAsTable's shape (an explicit save may navigate;
   a background refresh may not). Resolves to the new source, or null. */
export async function saveViewAsTable({ viewId, keys = null, exclude = [], spec = null }) {
  if (!viewId) return null;
  const name = await promptDialog('Name for the new table:', subsetDefaultName());
  if (name === null || !name.trim()) return null;
  const body = { view_id: viewId, name: name.trim(), keys, exclude, spec };
  let res;
  try {
    res = await post('/api/view/save_as_table', body);
    if (res.needs_confirm) {
      const n = res.rows == null ? 'more than 500,000' : res.rows.toLocaleString();
      if (!(await confirmDialog(`You're about to save ${n} rows as a table. Are you sure?`,
        { okLabel: 'Save them' }))) return null;
      res = await post('/api/view/save_as_table', { ...body, force: true });
    }
  } catch (e) {
    toast('Could not save: ' + e.message, 6000);
    return null;
  }
  toast(`Created "${res.source.name}" · ${res.source.row_count.toLocaleString()} rows`);
  await loadSources();
  openSource(res.source.id);
  return res.source;
}

/* Rows unchecked out of a select-all, as pairs — their pages fetched
   first if one was evicted, because guessing would save a row the analyst
   explicitly deselected (tagWholeViewSelection's rule). */
export async function subsetExcludedPairs() {
  if (!S.selectAll || !S.selection.size) return [];
  let exclude = selExcludedPairs();
  if (exclude === null) {
    await waitForPages([...new Set(selExcludedPositions().map((p) => Math.floor(p / PAGE)))]);
    exclude = selExcludedPairs();
  }
  if (exclude === null) throw new Error('deselected rows could not be loaded');
  return exclude;
}

/* The whole view — filters, search and timeframe applied — minus any rows
   unchecked out of a select-all. Filters ▾ lands here, and so does the
   row menu when the right-clicked row is part of a select-all. */
export async function saveCurrentViewAsTable() {
  if (!S.view) return null;
  let exclude;
  try { exclude = await subsetExcludedPairs(); }
  catch (e) { toast('Could not save: ' + e.message, 5000); return null; }
  return saveViewAsTable({ viewId: S.view.view_id, exclude, spec: currentSpec() });
}

/* Explicit picks (or a cell range, or the clicked row) as [source_id, rid]
   pairs — every page they span fetched first, a hole refused rather than
   papered over (the copy paths' rule). Flat and grouped alike: rowAt
   resolves a tree position to its row either way, and the keys are
   resolved server-side against the ROOT view, which holds every row of
   every group. */
export async function saveRowsAsTable(positions) {
  if (!S.view || !positions.length) return null;
  if (positions.length > SUBSET_PICK_CAP) {
    toast(`Too many rows to save by hand (max ${SUBSET_PICK_CAP.toLocaleString()}) — `
      + 'filter the view down to them, then save the view as a table', 6000);
    return null;
  }
  try { await loadRowsForPositions(positions); }
  catch (e) { toast('Could not save: ' + e.message, 5000); return null; }
  const keys = [];
  for (const pos of positions) {
    const r = rowAt(pos);
    if (!r) { toast('Could not save: a selected row is no longer loaded — try again', 5000); return null; }
    keys.push([r.source_id ?? S.sourceId, r.rid]);
  }
  return saveViewAsTable({ viewId: S.view.view_id, keys, spec: currentSpec() });
}

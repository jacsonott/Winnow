/* Undoing a filter change — `z`, and again for the one before it.

   Recorded in one place rather than at the forty-odd call sites that
   change a filter (a header box, the value picker, the builder, a tag
   chip, the search box, the timeframe, Clear filters, a saved filter, a
   dashboard drill…): every one of them ends in a rebuild, and runBuild
   hands each table's state to recordFilterState as it starts. What the
   table was last built with is its baseline; a build whose filters
   differ from it pushes what changed, and the baseline moves on.

   Per table, because filters are: the stash in sources.js puts a table's
   own filters back when the analyst returns to it, so `z` on one table
   must never reach into another. Opening a table is not a change either
   — openSource re-baselines before it builds — which is what keeps a
   case-wide timeframe (timeframeAcrossTables) set on one table from
   turning up as an undo step on the next one opened.

   An entry holds only the PARTS that changed, before and after, and undo
   puts back exactly those. Sort, grouping and Hide empty rows are not
   filters and are left where they are, so undoing a filter typed before
   a re-sort does not also undo the sort. History dies with the stash
   (a case switch, closing or removing the table): source ids restart in
   a new case and SQLite reuses a removed table's id, so an entry kept
   past either would put one table's filters on another. */
import { nameMap, toast } from './core.js';
import { parseFilter } from './filters.js';
import { S, normalizeTree } from './state.js';
import { rebuildView, repaintSearchChrome } from './view.js';

/* The steps kept per table. A triage pass is a long run of small
   narrowings, and an entry is a few short strings. */
export const FILTER_UNDO_LIMIT = 100;

/* Typing into a header box rebuilds on every pause past the 220ms
   debounce, so one word can land as three builds. Consecutive builds
   that only edit the same box (or the search text) this close together
   are one change; undo takes the whole word back, not its last letter. */
export const FILTER_UNDO_COALESCE_MS = 1500;

const clone = (v) => JSON.parse(JSON.stringify(v));

/* Own properties only: column names are user data, and the snapshot's
   JSON copy is a plain object, so a column called `toString` read as
   `before.toString` is Object.prototype's function, not a filter. */
const rawOf = (filters, c) => (Object.prototype.hasOwnProperty.call(filters, c) ? filters[c] || '' : '');

/* Each part of a table's filters: how to read it out of S, how to put it
   back, and the key two states are compared by. The key is what the view
   is BUILT from rather than the raw text, so a trailing space in a box,
   an emptied box left as '' rather than deleted, or switching the search
   mode with nothing typed is not a step anyone has to undo through. */
const PARTS = {
  filters: {
    label: 'column filters',
    take: () => ({ ...S.filters }),
    // A column that has gone since (a removed derived column) is not put
    // back: a filter on it would fail the rebuild outright.
    put: (v) => { S.filters = nameMap(Object.fromEntries(Object.entries(v).filter(([c]) => S.columns.some((x) => x.name === c)))); },
    key: (v) => JSON.stringify(Object.keys(v).sort().map((c) => [c, parseFilter(rawOf(v, c))]).filter(([, p]) => p)),
  },
  search: {
    label: 'search',
    take: () => ({ search: S.search, searchMode: S.searchMode, searchTerms: S.searchTerms }),
    put: (v) => { S.search = v.search; S.searchMode = v.searchMode; S.searchTerms = v.searchTerms; },
    // currentSpec's own split: advanced mode sends the terms and no text,
    // the other two the text and no terms.
    key: (v) => {
      if (v.searchMode === 'advanced') {
        const terms = v.searchTerms.filter((t) => t.term);
        return terms.length ? JSON.stringify(terms) : '';
      }
      return v.search ? JSON.stringify([v.searchMode, v.search]) : '';
    },
  },
  filterTree: {
    label: 'filter builder',
    take: () => S.filterTree,
    put: (v) => { S.filterTree = v; },
    key: (v) => {
      const t = normalizeTree(v);
      return JSON.stringify(t.type === 'raw' || (t.children && t.children.length) ? t : null);
    },
  },
  tagFilter: {
    label: 'tag filter',
    take: () => S.tagFilter,
    put: (v) => { S.tagFilter = v; },
    key: (v) => JSON.stringify([...v].sort()),
  },
  timeRange: {
    label: 'timeframe',
    take: () => S.timeRange,
    put: (v) => { S.timeRange = v; },
    key: (v) => (v.enabled && (v.start || v.end) ? JSON.stringify([v.column, v.start, v.end]) : ''),
  },
};

// source id -> { base, entries: [{ at, target, before, after }] }
const histories = new Map();

/* Deep copies throughout: setPickerTreeNode edits S.filterTree's children
   in place, so a baseline holding the live tree would change with it and
   the next build would find nothing different. */
function snapshot() {
  return Object.fromEntries(Object.entries(PARTS).map(([k, p]) => [k, clone(p.take())]));
}

const pick = (st, keys) => Object.fromEntries(keys.map((k) => [k, st[k]]));

function changedColumns(before, after) {
  const cols = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...cols].filter((c) => rawOf(before, c).trim() !== rawOf(after, c).trim());
}

/* What a change was typed into, when it was one box's text and nothing
   else — the only kind of change that coalesces. The box has to have the
   focus too: Clear filters (`c`) or the value picker straight after
   typing changes the same one column, and is a step of its own, not the
   word typed back out. */
function typingTarget(changed, before, after) {
  if (changed.length !== 1) return null;
  const el = document.activeElement;
  if (changed[0] === 'filters') {
    const cols = changedColumns(before.filters, after.filters);
    return cols.length === 1 && el && el.matches('.fcell input') && el.dataset.col === cols[0]
      ? `filter:${cols[0]}` : null;
  }
  if (changed[0] === 'search' && before.search.searchMode === after.search.searchMode
      && after.search.searchMode !== 'advanced' && el && el.id === 'search') return 'search';
  return null;
}

/* Called by runBuild for the table it is building, before anything is
   sent. A table seen for the first time only takes its baseline. */
export function recordFilterState(sourceId) {
  if (sourceId == null) return;
  const now = snapshot();
  const h = histories.get(sourceId);
  if (!h) { histories.set(sourceId, { base: now, entries: [] }); return; }
  const changed = Object.keys(PARTS).filter((k) => PARTS[k].key(h.base[k]) !== PARTS[k].key(now[k]));
  if (changed.length) {
    const at = performance.now();
    const target = typingTarget(changed, h.base, now);
    const top = h.entries[h.entries.length - 1];
    if (top && target && top.target === target && at - top.at < FILTER_UNDO_COALESCE_MS) {
      top.after = pick(now, changed);
      top.at = at;
      // Typed back to where it started: nothing left to undo.
      if (changed.every((k) => PARTS[k].key(top.before[k]) === PARTS[k].key(top.after[k]))) h.entries.pop();
    } else {
      h.entries.push({ at, target, before: pick(h.base, changed), after: pick(now, changed) });
      if (h.entries.length > FILTER_UNDO_LIMIT) h.entries.shift();
    }
  }
  h.base = now;
}

/* openSource's: whatever the table opens with is where it starts, not a
   change to it. Entries already recorded for it stay. */
export function syncFilterBaseline(sourceId = S.sourceId) {
  if (sourceId == null) return;
  const h = histories.get(sourceId);
  if (h) h.base = snapshot();
  else histories.set(sourceId, { base: snapshot(), entries: [] });
}

export function dropFilterHistory(sourceId) { histories.delete(sourceId); }

/* Every state a table's history holds, its baseline and both sides of
   each entry, for sources.js renameColumnInStash to move a renamed
   derived column through: undo past the rename then puts a filter back
   on the column under the name it has now, rather than dropping it as a
   column that has gone. */
export function filterHistoryStates(sourceId) {
  const h = histories.get(sourceId);
  return h ? [h.base, ...h.entries.flatMap((e) => [e.before, e.after])] : [];
}
export function clearFilterHistory() { histories.clear(); }

export function filterUndoDepth(sourceId = S.sourceId) {
  const h = histories.get(sourceId);
  return h ? h.entries.length : 0;
}

/* The toast's half-sentence: the columns a filter change touched, by
   name while there are few enough to read, then the other parts. */
function describe(entry) {
  const what = [];
  for (const k of Object.keys(entry.before)) {
    if (k !== 'filters') { what.push(PARTS[k].label); continue; }
    const cols = changedColumns(entry.before.filters, entry.after.filters);
    if (cols.length && cols.length <= 3) what.push(...cols.map((c) => `${c} filter`));
    else what.push(PARTS.filters.label);
  }
  return what.join(', ');
}

export async function undoFilterChange() {
  const sourceId = S.sourceId;
  const h = sourceId == null ? null : histories.get(sourceId);
  const entry = h && h.entries.pop();
  if (!entry) { toast('No filter change to undo'); return; }
  for (const [k, v] of Object.entries(entry.before)) PARTS[k].put(clone(v));
  // A pivoted session comparison is the filter tree plus marks; the marks
  // go with the tree, as they do on Clear filters.
  if ('filterTree' in entry.before && S.diffMarks && S.diffMarks.sourceId === sourceId) S.diffMarks = null;
  // What is about to be built is the state being returned to, not a new
  // change for the next press to undo.
  h.base = snapshot();
  repaintSearchChrome();
  const more = h.entries.length;
  toast(`Filter change undone: ${describe(entry)}${more ? ` (${more} more to undo)` : ''}`, 3000);
  await rebuildView();
}

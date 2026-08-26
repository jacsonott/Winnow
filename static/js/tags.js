/* Tagging rows, the tag ribbon and toolbar, and the tag-change undo stack.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, PAGE, api, el, post, setBusy, toast } from './core.js';
import { clearPageCache, render, rowAt } from './grid.js';
import { drawRail, groupCoordAt, loadRowsForPositions, positionsNeedLoading, regroupIfGroupedByTag, waitForPages } from './grouping.js';
import { S, selCount, selExcludedPairs, selExcludedPositions, selFirst, selPositions } from './state.js';
import { openTagEditor } from './timeframe.js';
import { renderTimelineTagFilter } from './timeline.js';
import { confirmDialog } from './ui.js';
import { rebuildView } from './view.js';

/* ----------------------------------------------------------------- tags */

export async function loadTags() {
  const d = await api(`/api/tags?source_id=${S.sourceId}`);
  S.tags = d.tags;
  // Whole-table counts. The ribbon shows the *view*-scoped ones the moment
  // there's a view to scope to (refreshTagCounts, below); until then these
  // are both the answer and the only answer.
  S.tagCountsAll = d.counts || {};
  S.tagCounts = d.counts || {};
  renderTagRibbon();
  renderTimelineTagFilter();
  refreshTagCounts();
  // Deleting a tag takes its history with it server-side, and opening a
  // different case swaps the Store (and so the whole stack) underneath us.
  refreshUndoState();
}

/* Re-reads the tag counts for whatever view is up. Called after every view
   rebuild and after every tagging operation, because both change the
   answer: a filter changes which rows are in scope, a tag changes which of
   them are tagged.

   Fire-and-forget on purpose — it's one aggregate over the view (the same
   join tag_positions makes, with the same untagged short-circuit) and the
   grid has no reason to wait on it. A failure leaves the previous numbers
   up rather than blanking the ribbon; a stale count for one paint is a far
   smaller lie than an empty one. */
export async function refreshTagCounts() {
  const vid = S.view && S.view.view_id;
  // loadTags() runs while switching tables, when S.view can still be the
  // *previous* table's (live, not yet evicted) view — counting against that
  // would put one table's numbers under another's tags for the moment
  // before rebuildView lands.
  if (!vid || S.view.source_id !== S.sourceId) return;
  let d;
  try {
    d = await api(`/api/tag_counts?view_id=${vid}`);
  } catch {
    return; // expired view: the rebuild that replaces it calls back through here
  }
  if (!S.view || S.view.view_id !== vid) return; // superseded while in flight
  S.tagCounts = d.counts || {};
  renderTagRibbon();
}

export function renderTagRibbon() {
  const rib = $('tagRibbon');
  rib.replaceChildren();
  for (const t of S.tags) {
    const chip = el('button', 'tag-chip');
    chip.setAttribute('aria-pressed', String(S.tagFilter.includes(t.id)));
    chip.style.color = S.tagFilter.includes(t.id) ? t.color : '';
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    chip.append(sw, el('span', null, t.name));
    if (t.hotkey) chip.append(el('span', 'key', t.hotkey));
    // Scoped to the current view, not the whole table: with a filter or a
    // search on, "how many of *these* are tagged" is the question the
    // ribbon is sitting next to. The whole-table number isn't dropped, it
    // moves into the tooltip — a count that silently changed meaning when a
    // filter went on would be worse than either number alone.
    const n = S.tagCounts[t.id] || 0;
    const all = S.tagCountsAll[t.id] || 0;
    if (n) chip.append(el('span', 'n', n.toLocaleString()));
    const scope = n === all
      ? `${all.toLocaleString()} tagged`
      : `${n.toLocaleString()} tagged in this view · ${all.toLocaleString()} in the table`;
    chip.title = `${scope}. Click to filter to ${t.name}. Press ${t.hotkey || '—'} to tag the selection.`;
    chip.onclick = () => {
      S.tagFilter = S.tagFilter.includes(t.id) ? [] : [t.id];
      renderTagRibbon();
      rebuildView({ keepScroll: false });
    };
    rib.append(chip);
  }
  const any = el('button', 'tag-chip');
  any.setAttribute('aria-pressed', String(S.tagFilter[0] === '__any__'));
  any.append(el('span', null, 'Any tag'));
  any.onclick = () => {
    S.tagFilter = S.tagFilter[0] === '__any__' ? [] : ['__any__'];
    renderTagRibbon();
    rebuildView({ keepScroll: false });
  };
  rib.append(any);
  const edit = el('button', 'tag-chip');
  edit.append(el('span', null, 'Edit tags'));
  edit.onclick = openTagEditor;
  rib.append(edit);
}

/* Above this many rows, tagging asks first — and, on the per-row path,
   that many rows also means a lot of pages to fetch before it can start. */
export const BULK_TAG_CONFIRM_AT = 10000;

/* Tagging a selection.

   This used to be `positions.map(rowAt).filter(Boolean)` — which silently
   dropped every selected row the page cache hadn't seen. That was harmless
   while selections only came from shift-clicks inside loaded pages, and
   became a real correctness bug the moment the select-all checkbox existed:
   "select all 1.2M rows, press a tag hotkey" tagged the few hundred rows
   that happened to be cached and reported that smaller number in a toast.
   A silently partial tag is the worst failure mode this tool has — the
   analyst's own notion of what they've triaged is the thing being corrupted.

   So there are two paths and no third:
   - The whole view is selected (with, at most, a few explicitly unchecked
     rows): hand the view id and the exclusions to the server and let it do
     the set operation over the materialized view. Nothing needs fetching.
   - An explicit subset: fetch every page it spans *first* and fail loudly if
     that doesn't work, rather than tagging whatever happened to be there. */
export async function applyTag(tag, on) {
  if (!S.view) return;
  if (!selCount()) {
    // A rectangular cell range is also a statement of which ROWS are
    // meant: highlighting cells across four rows and pressing a tag key
    // should tag those four rows, not just the anchor row the cursor
    // happens to sit on. Group headers inside the span are skipped.
    if (S.cellRange) {
      let positions = [];
      for (let p = S.cellRange.r0; p <= S.cellRange.r1; p++) positions.push(p);
      if (S.groupByCols.length) positions = positions.filter((p) => groupCoordAt(p));
      if (positions.length) { await tagRowsAtPositions(tag, positions, on); return; }
    }
    if (S.cursor < 0) return;
    // In grouped mode the cursor can sit on a group header, which isn't a
    // row — tag the whole group from its right-click menu instead.
    if (S.groupByCols.length && !groupCoordAt(S.cursor)) return;
    await tagRowsAtPositions(tag, [S.cursor], on);
    return;
  }
  if (S.selectAll) await tagWholeViewSelection(tag, on);
  else await tagRowsAtPositions(tag, selPositions(), on);
}

/* Resolves the toggle (`on === undefined`) the same way the old code did —
   from the first selected row — but tolerates that row not being cached,
   which "select all" makes likely. Defaulting to tagging (rather than
   untagging) when nothing's loaded matches what a bulk select-all is for. */
export function resolveTagDirection(tag, on, samplePos) {
  if (on !== undefined) return on;
  const r = samplePos >= 0 ? rowAt(samplePos) : null;
  return r ? !r.tags.includes(tag.id) : true;
}

export async function tagWholeViewSelection(tag, on) {
  const count = selCount();
  on = resolveTagDirection(tag, on, selFirst());
  if (count >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} ${count.toLocaleString()} selected rows as "${tag.name}"?`))) return;
  // Excluded rows are ones the analyst unchecked on screen, so they're
  // cached — but a view rebuild or cache eviction could have dropped one,
  // and guessing would tag a row that was explicitly deselected.
  let exclude = selExcludedPairs();
  if (exclude === null) {
    try {
      await waitForPages([...new Set(selExcludedPositions().map((p) => Math.floor(p / PAGE)))]);
    } catch (e) {
      toast('Could not tag: ' + e.message, 5000);
      return;
    }
    exclude = selExcludedPairs();
    if (exclude === null) { toast('Could not tag: deselected rows could not be loaded', 5000); return; }
  }
  setBusy(true);
  let res;
  try {
    res = await post('/api/row_tags/view', { view_id: S.view.view_id, tag_id: tag.id, on, exclude });
  } catch (e) {
    toast('Could not tag: ' + e.message, 5000);
    return;
  } finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  // Every cached row's `tags` array is now stale — the server changed rows
  // this client never fetched, so there's nothing to patch up in place.
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  const n = res.affected != null ? res.affected : count;
  toast(`${on ? 'Tagged' : 'Untagged'} ${n.toLocaleString()} row${n === 1 ? '' : 's'} · ${tag.name}`);
}

export async function tagRowsAtPositions(tag, positions, on) {
  if (!positions.length) return;
  on = resolveTagDirection(tag, on, positions[0]);
  if (positions.length >= BULK_TAG_CONFIRM_AT
      && !(await confirmDialog(`${on ? 'Tag' : 'Untag'} ${positions.length.toLocaleString()} selected rows as "${tag.name}"?`))) return;
  if (positionsNeedLoading(positions)) {
    setBusy(true);
    try {
      await loadRowsForPositions(positions);
    } catch (e) {
      toast('Could not tag: ' + e.message, 5000);
      return;
    } finally { setBusy(false); }
  }
  const rows = positions.map((p) => rowAt(p));
  if (rows.some((r) => !r)) { toast('Could not tag: some selected rows could not be loaded', 5000); return; }
  // A merged view's selected rows can each belong to a different real
  // source — send their own (source_id, rid) pairs rather than the merge's
  // synthetic negative id, so tags land on the row's actual origin.
  const body = S.sourceId < 0
    ? { pairs: rows.map((r) => [r.source_id, r.rid]), tag_id: tag.id, on }
    : { source_id: S.sourceId, rids: rows.map((r) => r.rid), tag_id: tag.id, on };
  setBusy(true);
  let res;
  try { res = await post('/api/row_tags', body); }
  catch (e) { toast('Could not tag: ' + e.message, 5000); return; }
  finally { setBusy(false); }
  for (const r of rows) {
    r.tags = on ? [...new Set([...r.tags, tag.id])] : r.tags.filter((x) => x !== tag.id);
  }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  toast(`${on ? 'Tagged' : 'Untagged'} ${rows.length.toLocaleString()} row${rows.length === 1 ? '' : 's'} · ${tag.name}`);
}

/* ---------------------------------------------------------- undo (tags) */

/* The history itself lives server-side, for one reason: only the server
   knows which rows a change actually moved. "Tag the 171k rows in this
   view" is a set operation over a materialised view that this client never
   fetches a row of, and even on the explicit-selection path, undoing by
   re-sending the same rids with the direction flipped would strip the tag
   off rows that already carried it before. So the client tracks nothing
   but what the next undo *would* say, for the menu label. */
export let UNDO_NEXT = { available: false, depth: 0 };

export function setUndoState(next) {
  UNDO_NEXT = next || { available: false, depth: 0 };
}

export async function refreshUndoState() {
  try { setUndoState(await api('/api/row_tags/undo')); }
  catch { setUndoState(null); }
}

export async function undoLastTagChange() {
  let res;
  setBusy(true);
  try {
    res = await post('/api/row_tags/undo', {});
  } catch (e) {
    // 400 is the empty-history case, which is a normal thing to press
    // Ctrl+Z into rather than an error worth a five-second toast.
    toast(e.message === 'Nothing to undo' ? 'Nothing to undo' : 'Could not undo: ' + e.message,
          e.message === 'Nothing to undo' ? 2000 : 5000);
    await refreshUndoState();
    return;
  } finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  setUndoState(res.next);
  // Same reasoning as the bulk tag path: the server changed rows this
  // client may never have fetched, so there is nothing to patch in place.
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag(); // undo moved rows between tag groups too
  toast(`Undone: ${res.undone}`);
}

export async function applyTagToView(tag) {
  if (!S.view || !S.view.row_count) return;
  if (!(await confirmDialog(`Tag all ${S.view.row_count.toLocaleString()} rows in this view as "${tag.name}"?`))) return;
  setBusy(true);
  let res;
  try { res = await post('/api/row_tags/view', { view_id: S.view.view_id, tag_id: tag.id, on: true }); }
  finally { setBusy(false); }
  S.tagCountsAll = res.counts || {};  // whole-table; refreshTagCounts re-reads the view-scoped half
  refreshTagCounts();
  clearPageCache();
  renderTagRibbon();
  render();
  drawRail();
  regroupIfGroupedByTag();
  refreshUndoState();
  toast(`Tagged ${res.affected.toLocaleString()} rows · ${tag.name}`);
}

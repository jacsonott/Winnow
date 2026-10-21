/* The timeframe filter and jump-to-timestamp — the two pieces of time
navigation that survive everything else being cleared.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { applyDefaultLayout, isPinned, moveColumn, openCopyLayoutModal, renderHead, saveDefaultLayout, saveLayout, togglePin } from './columns.js';
import { $, api, el, post, toast } from './core.js';
import { openDerivedColumnModal } from './derived.js';
import { hasActiveFilterTree, openFilterBuilder } from './filterbuilder.js';
import { VALUE_FILTER_AUTO_MAX, setColumnValueFilter, setValueFilterMode, valueFilterAutoOn, valueFilterEnabled } from './filters.js';
import { moveCursor, render } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { openMergeBuilder } from './merge.js';
import { applyPreset, filtersForCurrentSource, headerSig, loadSavedFilters, matchingSavedFilters, nicknameFor, setNicknameFor } from './savedfilters.js';
import { saveAppearance } from './settings.js';
import { clearAllFilters, closeTab, editSourceNickname, openSource, sourceLabel, wireDragReorder } from './sources.js';
import { normalizeTree, S } from './state.js';
import { openTablesManager } from './tables.js';
import { clearRowCaches, loadTags, newTagDefaults, tagEditRow } from './tags.js';
import { baseColumns, columnMeta, parseTimestamp } from './tsformat.js';
import { datePickerButton, dropdownMenu, markModalAction, confirmDialog, modal, oneAtATime, promptDialog } from './ui.js';
import { rebuildView } from './view.js';

/* ------------------------------------------------------ jump to timestamp */

/* A timestamp box with its label above and the 📅 picker beside it — the
   builders' field shape (.dlg-field), shared by Jump and Timeframe so the
   two dialogs' boxes look and line up the same. */
function timestampField(label, input) {
  const field = el('div', 'dlg-field');
  const row = el('div', 'dlg-input');
  row.append(input, datePickerButton(input));
  field.append(el('label', null, label), row);
  return field;
}

/* Scroll to the row whose timestamp is closest to a given moment. The
   target is saved in S.jumpTs and deliberately survives switching tables —
   the workflow this exists for is "something happened at 13:22:01; show me
   that moment in the EVTX table, now in the proxy log, now in the MFT". */
export function openJumpTsModal() {
  markModalAction('openJumpTs');
  if (!S.sourceId) { toast('Open a table first'); return; }
  const dtCols = S.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
  modal('Jump to timestamp', (b) => {
    const jumpKey = (S.keymap.repeatJumpTs || [])[0] || '(unbound)';
    b.append(el('p', null,
      'Scrolls to the row whose timestamp is closest to this moment. The value is remembered '
      + `across tables — press "${jumpKey}" in any table to jump straight to it again.`));

    const colField = el('div', 'dlg-field');
    colField.append(el('label', null, 'Column'));
    const colSel = el('select');
    const allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = 'Nearest across all datetime columns';
    colSel.append(allOpt);
    for (const name of dtCols) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      colSel.append(opt);
    }
    colSel.value = dtCols.includes(S.jumpTs.column) ? S.jumpTs.column : '';
    colField.append(colSel);
    b.append(colField);
    if (!dtCols.length) b.append(el('p', 'fb-help', 'This table has no datetime columns — the jump will have nothing to measure against here.'));

    const input = el('input', 'confirm-input mono');
    input.type = 'text';
    input.placeholder = 'YYYY-MM-DD HH:MM:SS';
    input.value = S.jumpTs.value || '';
    b.append(timestampField('Moment', input));
    b.append(el('p', 'fb-help', '24-hour time — the date alone, or date plus HH:MM, also work.'));

    const go = () => {
      const v = input.value.trim();
      if (!v) { toast('Enter a timestamp'); return; }
      if (!parseTimestamp(v)) { toast('Not a recognized timestamp — try YYYY-MM-DD HH:MM:SS', 4000); return; }
      S.jumpTs = { value: v, column: colSel.value || null };
      $('modal').hidden = true;
      doJumpTs();
    };
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } };
    const actions = el('div', 'row-actions');
    const jumpBtn = el('button', 'btn primary', 'Jump');
    jumpBtn.onclick = go;
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(jumpBtn, cancel);
    b.append(actions);

  }, { focus: 'input' });
}

export async function doJumpTs() {
  if (!S.jumpTs.value) { openJumpTsModal(); return; }
  if (!S.view || !S.sourceId) { toast('Open a table first'); return; }
  if (S.groupByCols.length) { toast('Jump works in the flat view — toggle grouping off first'); return; }
  // The saved column may not exist in this table — fall back to
  // nearest-across-all rather than erroring on a per-table mismatch.
  const col = S.jumpTs.column && S.columns.some((c) => c.name === S.jumpTs.column && c.type === 'datetime')
    ? S.jumpTs.column : null;
  try {
    const res = await post('/api/view/find_ts', { view_id: S.view.view_id, value: S.jumpTs.value, column: col });
    moveCursor(res.pos);
    toast(`Jumped to ${res.ts} (row ${(res.pos + 1).toLocaleString()})`);
  } catch (e) {
    toast((e.status === 404 ? e.message : 'Could not jump: ' + e.message), 4000);
  }
}

/* Same shape saveFilterAs/saveAs POST as a saved filter/preset's payload. */
export function currentFilterPayload() {
  const p = { filter_tree: S.filterTree, sort: S.sort, search: S.search, search_mode: S.searchMode, search_terms: S.searchTerms };
  // Grouping rides along only when one is active — a filter saved without
  // grouping omits the key entirely, and applying it leaves any current
  // grouping alone (same leniency `sort: p.sort || S.sort` has).
  if (S.groupByCols.length) {
    p.group_by = [...S.groupByCols];
    p.group_sort = S.groupSort;
    p.group_sort_dir = S.groupSortDir;
  }
  return p;
}

/* No separate "which saved filter is active" flag to keep in sync — instead
   this re-derives it every time the view changes by comparing the live
   filter/sort/search state against each saved filter's stored payload.
   Applying one makes S.filterTree etc. literally the response object back
   from the API, so it matches immediately; editing anything afterward means
   it naturally stops matching, without needing to invalidate a flag by hand. */
export function activeSavedFilterRecord() {
  if (!S.sourceId) return null;
  const cur = currentFilterPayload();
  // The same leniency applyPreset gives on the way in, applied on the way
  // back: a payload without a `sort` key leaves the current sort alone when
  // applied, so the current sort must not stop it matching (the shipped
  // TLE defaults are exactly this shape — a filter, not a sort opinion).
  // Compared on an explicit key list so key insertion order can't matter.
  const KEYS = ['filter_tree', 'sort', 'search', 'search_mode', 'search_terms',
                'group_by', 'group_sort', 'group_sort_dir'];
  const matches = (p) => KEYS.every((k) => {
    if (k === 'sort' && p.sort === undefined) return true;
    if (k.startsWith('group_') && p.group_by === undefined) return cur.group_by === undefined;
    // Trees compare normalized: a stored cond-root payload and the
    // group-wrapped tree it becomes on apply are the same filter.
    if (k === 'filter_tree') return JSON.stringify(normalizeTree(p[k])) === JSON.stringify(normalizeTree(cur[k]));
    return JSON.stringify(p[k]) === JSON.stringify(cur[k]);
  });
  return filtersForCurrentSource().find((f) => matches(f.payload || {})) || null;
}

/* Single merged button for Filter builder + Saved filters (dropdown menu
   below) — its label/pressed-state reflects whichever is more specific:
   an exactly-matching saved filter by name, else just "filter active" when
   the tree has content the analyst built by hand, else the plain label. */
export function updateFiltersButton() {
  const btn = $('btnFilters');
  const f = activeSavedFilterRecord();
  if (f) {
    btn.textContent = `★ ${f.name} ▾`;
    btn.title = `Applying saved filter "${f.name}" — click to browse filters`;
    btn.setAttribute('aria-pressed', 'true');
  } else if (hasActiveFilterTree()) {
    btn.textContent = 'Filters ● ▾';
    btn.title = 'A custom filter is active — click to edit or browse saved filters';
    btn.setAttribute('aria-pressed', 'true');
  } else {
    btn.textContent = 'Filters ▾';
    btn.title = 'Filter builder and saved filters';
    btn.setAttribute('aria-pressed', 'false');
  }
  // The accent ring that replaced the suggestion banner: saved filters
  // exist for exactly this table's columns and none is applied yet — the
  // dropdown lists them (wireSearch). Quiet once something IS applied.
  const src = S.sources.find((x) => x.id === S.sourceId);
  const m = src && !f && !hasActiveFilterTree()
    ? matchingSavedFilters(src.columns.map((c) => c.name)) : null;
  const n = m ? m.exact.length : 0;
  btn.classList.toggle('suggest', n > 0);
  if (n > 0) btn.title = `${n} saved filter${n === 1 ? '' : 's'} match${n === 1 ? 'es' : ''} this table's columns — click to apply one`;
}

/* ---------------------------------------------------------- timeframe filter */

/* A start/end range that stays applied across everything that resets the
   regular filters — clearAllFilters(), applyPreset(), switching tabs (see
   openSource() — deliberately not among the fields it resets). The MFT use
   case this exists for: pin a date range, then flip through per-column
   quick filters/presets/tables without having to re-set the range each
   time. Switching tables is the one carry-over the analyst can turn off
   (timeframeAcrossTables, below): off, each table keeps its own timeframe
   the way it keeps its own filters. Column choice is per-invocation, not persisted structure — "all
   datetime columns" (column: null) ORs every datetime column on whichever
   table is open, so a timestomped Created date doesn't hide a row whose
   Modified date is genuinely in range; _compile_where falls back to that
   same "all columns" behavior automatically if a specifically-chosen
   column doesn't exist on the table currently open. */

export function datetimeColumns() {
  return S.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
}

/* Whether the timeframe follows the analyst from table to table. On (the
   default, and what every install did before this was a choice) it is one
   case-wide range; off, openSource stashes it with the table being left
   and puts back the one the arriving table had — none, for a table it
   has not been set on. A machine preference beside openNewTables, not
   case data: it is about how this analyst works, not about the evidence.
   `!== false` so an appearance blob from before the key existed reads as
   the behaviour it was saved under. S.appearance itself is guarded:
   updateTimeRangeButton asks this during boot, before initAppearance. */
export function timeframeAcrossTables() {
  return !(S.appearance && S.appearance.timeframeAcrossTables === false);
}

export function setTimeframeAcrossTables(on) {
  S.appearance.timeframeAcrossTables = !!on;
  saveAppearance();
  updateTimeRangeButton();
}

export function updateTimeRangeButton() {
  const btn = $('btnTimeRange');
  const hasRange = !!(S.timeRange.start || S.timeRange.end);
  const active = S.timeRange.enabled && hasRange;
  btn.setAttribute('aria-pressed', String(active));
  btn.textContent = active ? `⏱ ${S.timeRange.column || 'all columns'}` : '⏱ Timeframe';
  const toggleKey = S.keymap.toggleTimeRange[0] || '';
  const openKey = S.keymap.openTimeRange[0] || '';
  btn.title = active
    ? `Timeframe filter active (${S.timeRange.column || 'all datetime columns'}): `
      + `${S.timeRange.start || '…'} → ${S.timeRange.end || '…'} — "${toggleKey}" to toggle off, "${openKey}" to edit`
    : hasRange
      ? `Timeframe filter set but off — "${toggleKey}" to toggle on, "${openKey}" to edit`
      : `Set up a timeframe filter that survives ${timeframeAcrossTables() ? 'filter/preset/table changes' : 'filter/preset changes'}`
        + ` — "${openKey}" to open, "${toggleKey}" to toggle`;
}

export function toggleTimeRange() {
  if (!S.timeRange.start && !S.timeRange.end) {
    toast('Set a timeframe first, from the clock button in the toolbar');
    openTimeRangeModal();
    return;
  }
  S.timeRange.enabled = !S.timeRange.enabled;
  updateTimeRangeButton();
  toast(S.timeRange.enabled ? 'Timeframe filter on' : 'Timeframe filter off');
  if (S.sourceId) rebuildView({ keepScroll: false });
}

export function openTimeRangeModal() {
  markModalAction('openTimeRange');
  modal('Timeframe filter', (b) => {
    // No Enabled box. Apply always turns the filter on, and the two ways
    // off are Clear (forget the range) and the toggle key (keep it): with
    // a box as well, a range typed in with the box left unticked was
    // saved but not applied, and Apply looked as if it had done nothing.
    const toggleKey = S.keymap.toggleTimeRange[0] || '(unbound)';
    // One line, and it follows the across-tables switch below: with it
    // off the range is per table, and the line must not say otherwise.
    const intro = el('p');
    const paintIntro = () => {
      intro.textContent = (timeframeAcrossTables()
        ? 'Survives filters and table switches'
        : 'Survives filters; each table keeps its own')
        + ` — "${toggleKey}" turns it off without clearing it.`;
    };
    paintIntro();
    b.append(intro);
    if (!S.timeRange.enabled && (S.timeRange.start || S.timeRange.end)) {
      b.append(el('p', 'fb-help tf-off', 'Off right now — Apply turns it back on.'));
    }

    // A preference rather than part of the range, so it saves as it is
    // clicked — Cancel below discards the range edits, not this. The same
    // switch is in Settings → Filter & search syntax.
    const acrossLabel = el('label', 'tf-check');
    const acrossCb = el('input');
    acrossCb.type = 'checkbox';
    acrossCb.id = 'tfAcrossTables';
    acrossCb.checked = timeframeAcrossTables();
    acrossCb.onchange = () => { setTimeframeAcrossTables(acrossCb.checked); paintIntro(); };
    acrossLabel.title = 'On: one timeframe for the whole case, applied to whichever table you open. '
      + 'Off: each table keeps its own timeframe, and a table you have not set one on opens without one.';
    acrossLabel.append(acrossCb, document.createTextNode(' Keep applied when switching tables'));
    b.append(acrossLabel);

    const colField = el('div', 'dlg-field');
    colField.append(el('label', null, 'Column'));
    const colSel = el('select');
    const allOpt = document.createElement('option');
    allOpt.value = '';
    allOpt.textContent = 'All datetime columns (catches a match on any of them)';
    colSel.append(allOpt);
    const cols = datetimeColumns();
    for (const name of cols) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      colSel.append(opt);
    }
    colSel.value = cols.includes(S.timeRange.column) ? S.timeRange.column : '';
    colField.append(colSel);
    b.append(colField);
    if (!cols.length) b.append(el('p', 'fb-help', "This table has no datetime columns yet, so the range below doesn't narrow it — the timeframe hides none of its rows, and applies on the tables that have one."));

    // Plain text, not <input type="datetime-local"> — that widget's native
    // picker renders in the browser's locale/12-hour format depending on
    // the OS, which doesn't match what TS_NORMALIZE/_ts_normalize actually
    // parse (ISO 'YYYY-MM-DD HH:MM:SS' or the US M/D/YYYY shape) and gave no
    // way to just type a known timestamp in 24-hour time. ISO with a space
    // separator is TS_ISO_RE's own shape (it accepts 'T' too, for values
    // coming from elsewhere, but the field asks for the one the rest of the
    // app displays: formatTimestamp's 'iso' option, the timeline, exports).
    const startInput = el('input', 'confirm-input mono');
    startInput.type = 'text';
    startInput.placeholder = 'YYYY-MM-DD HH:MM:SS';
    startInput.value = S.timeRange.start || '';
    const endInput = el('input', 'confirm-input mono');
    endInput.type = 'text';
    endInput.placeholder = 'YYYY-MM-DD HH:MM:SS';
    endInput.value = S.timeRange.end || '';
    // Side by side, so the two boxes line up (a label beside each put them
    // at different x positions, "Start" and "End" being different widths).
    const range = el('div', 'dlg-pair');
    range.append(timestampField('Start', startInput), timestampField('End', endInput));
    b.append(range);
    b.append(el('p', 'fb-help', '24-hour time, e.g. 2024-01-05 13:22:01 — the date alone, or date plus HH:MM, also work.'));

    // Free-text, so it's validated the same way a value has to be
    // recognizable to TS_NORMALIZE server-side to be usable at all —
    // parseTimestamp is the client-side twin of that same ISO/US shape
    // check (see its own comment). Rejecting here beats silently building
    // a filter that will never match anything.
    const validateTimeInput = (input, label) => {
      const v = input.value.trim();
      if (!v) return true;
      if (!parseTimestamp(v)) { toast(`${label}: not a recognized timestamp — try YYYY-MM-DD HH:MM:SS`, 4000); return false; }
      return true;
    };

    // Fill the range from what's already been triaged: earliest/latest
    // timestamp among tagged rows — any tag, or just the ones toggled on.
    if (S.sourceId && S.tags.length) {
      const tagField = el('div', 'dlg-field');
      tagField.append(el('label', null, 'From tagged rows'));
      const tagRow = el('div', 'dlg-chips tr-tag-row');
      const selectedTags = new Set();
      for (const t of S.tags) {
        const chip = el('button', 'tag-chip');
        chip.setAttribute('aria-pressed', 'false');
        const sw = el('span', 'swatch');
        sw.style.background = t.color;
        chip.append(sw, el('span', null, t.name));
        chip.title = 'Toggle — no tags toggled means any tag counts';
        chip.onclick = () => {
          if (selectedTags.has(t.id)) selectedTags.delete(t.id); else selectedTags.add(t.id);
          chip.setAttribute('aria-pressed', String(selectedTags.has(t.id)));
          chip.style.color = selectedTags.has(t.id) ? t.color : '';
        };
        tagRow.append(chip);
      }
      const fillBtn = el('button', 'btn ghost', 'Fill range');
      fillBtn.title = 'Set start/end to the earliest and latest timestamps among tagged rows (respects the column chosen above)';
      fillBtn.onclick = async () => {
        try {
          const res = await post('/api/tag_time_bounds', {
            source_id: S.sourceId, tag_ids: [...selectedTags], column: colSel.value || null,
          });
          if (!res.start && !res.end) { toast('No tagged rows with a usable timestamp'); return; }
          startInput.value = res.start || '';
          endInput.value = res.end || '';
          toast('Range set from tagged rows — Apply to use it');
        } catch (e) {
          toast('Could not read tagged range: ' + e.message, 4000);
        }
      };
      tagRow.append(fillBtn);
      tagField.append(tagRow);
      b.append(tagField);
    }

    const actions = el('div', 'row-actions');
    const apply = el('button', 'btn primary', 'Apply');
    apply.onclick = () => {
      if (!startInput.value.trim() && !endInput.value.trim()) { toast('Set a start and/or end'); return; }
      if (!validateTimeInput(startInput, 'Start') || !validateTimeInput(endInput, 'End')) return;
      S.timeRange = {
        enabled: true,
        column: colSel.value || null,
        start: startInput.value.trim(),
        end: endInput.value.trim(),
      };
      updateTimeRangeButton();
      $('modal').hidden = true;
      if (S.sourceId) rebuildView({ keepScroll: false });
    };
    const clearBtn = el('button', 'btn ghost', 'Clear');
    clearBtn.onclick = () => {
      S.timeRange = { enabled: false, column: null, start: '', end: '' };
      updateTimeRangeButton();
      $('modal').hidden = true;
      if (S.sourceId) rebuildView({ keepScroll: false });
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    const jumpBtn = el('button', 'btn ghost', 'Jump to timestamp…');
    jumpBtn.title = 'Scroll the grid to a moment instead of filtering to a range';
    jumpBtn.onclick = () => openJumpTsModal();
    actions.append(apply, clearBtn, jumpBtn, cancel);
    b.append(actions);
  });
}

/* Every saved filter sharing f's exact header set, in current cycle/list
   order — the scope moveSavedFilter's up/down reordering operates within,
   so reordering one header set's filters never disturbs another's (same
   guarantee workspace.SavedFilters.reorder makes server-side). */
export function sameGroupFilterIds(colNames) {
  const sig = headerSig(colNames);
  return S.savedFilters.filter((f) => headerSig(f.col_names) === sig).map((f) => f.id);
}

export async function moveSavedFilter(f, dir) {
  const ids = sameGroupFilterIds(f.col_names);
  const idx = ids.indexOf(f.id);
  const swapIdx = idx + dir;
  if (swapIdx < 0 || swapIdx >= ids.length) return;
  [ids[idx], ids[swapIdx]] = [ids[swapIdx], ids[idx]];
  S.savedFilters = await post('/api/saved_filters/reorder', { ids });
}

/* `returnTo`: where the × (and Escape) should land. Settings opens this
   in the same shared modal, so closing it used to drop the analyst on the
   grid — one level up, not back where they were. A one-shot listener on
   the close event, guarded by the title so that a filter applied from
   here (which hides the modal without the event) or a builder opened
   from here (which replaces the title) doesn't bounce back to Settings
   at the wrong moment. */
export function openSavedFiltersModal({ returnTo = null } = {}) {
  if (returnTo) {
    const back = () => {
      document.removeEventListener('winnow:modalclose', back);
      if ($('modalTitle').textContent === 'Saved filters' && $('modalBody').dataset.sfReturn === '1') returnTo();
    };
    document.addEventListener('winnow:modalclose', back);
  }
  const backBtn = () => {
    if (!returnTo) return null;
    const btn = el('button', 'btn ghost sf-back', '‹ Settings');
    btn.style.marginLeft = 'auto';
    btn.onclick = () => returnTo();
    return btn;
  };
  modal('Saved filters', (b) => {
    // Marks THIS opening as the one with a way back, so a listener left
    // armed by a close that bypassed the event can't fire for a later
    // Saved filters opened from Filters ▾.
    b.dataset.sfReturn = returnTo ? '1' : '';
    if (!S.savedFilters.length) {
      b.append(el('div', 'note-status', 'No saved filters yet. Build one in the Filter builder, then "Save filter…".'));
      const back = backBtn();
      if (back) { const row = el('div', 'row-actions'); row.append(back); b.append(row); }
      return;
    }
    // One line on what the list is for; the rest (Edit's round trip
    // through the builder, cycling past the ends) is on the wiki.
    b.append(el('p', 'fb-help',
      `Click a name to apply it. Drag to set the ${S.keymap.cyclePrevFilter[0] || '['} / `
      + `${S.keymap.cycleNextFilter[0] || ']'} cycle order.`));
    const search = el('input');
    search.type = 'search';
    search.placeholder = 'Search by name, nickname, or column…';
    search.autocomplete = 'off';
    search.style.cssText = 'width:100%;background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'border-radius:var(--radius-sm);padding:6px 9px;font:inherit;font-size:12px;margin-bottom:10px';
    b.append(search);

    const list = el('div', 'sf-list');
    b.append(list);

    const curCols = new Set(baseColumns().map((c) => c.name.trim().toLowerCase()));
    const active = activeSavedFilterRecord();

    const groupMatchesCur = (colNames) => {
      const cols = new Set((colNames || []).map((c) => c.trim().toLowerCase()));
      return cols.size === curCols.size && [...cols].every((c) => curCols.has(c));
    };

    // A name is text that applies the filter on click (Enter or Space
    // from the keyboard); everything else about the filter is in its ⋯.
    async function applyFilter(f, matches, colText) {
      if (!matches && !(await confirmDialog(`"${f.name}" was built for a different column set (${colText}). Apply anyway?`))) return;
      $('modal').hidden = true;
      applyPreset(f);
    }

    // Editing routes through the real grid: apply the filter, then open
    // the builder pre-loaded with its payload, so the match count behind
    // the modal is live feedback on the change being made. Needs a table
    // open to apply against — without one there's nothing to preview.
    async function editFilter(f, matches, colText) {
      if (!matches && !(await confirmDialog(
        `"${f.name}" was built for a different column set (${colText}). Edit it against the open table anyway? `
        + `It stays saved for its original columns.`))) return;
      $('modal').hidden = true;
      applyPreset(f);
      openFilterBuilder(f);
    }

    async function renameFilter(f) {
      const name = await promptDialog('New name:', f.name);
      if (!name || !name.trim()) return;
      try {
        await api(`/api/saved_filters/${f.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }),
        });
      } catch (e) { toast(`Could not rename "${f.name}": ${e.message}`, 6000); return; }
      f.name = name.trim();
      render();
      focusMore(f.id);
      updateFiltersButton();
    }

    async function moveFilter(f, dir) {
      try { await moveSavedFilter(f, dir); } catch (e) { toast(`Could not move "${f.name}": ${e.message}`, 6000); return; }
      render();
      // Back on the same filter's ⋯, so a second Move up is Enter and
      // an arrow away — reordering without a mouse is a row of keys.
      focusMore(f.id);
    }

    async function deleteFilter(f) {
      if (!(await confirmDialog(`Delete saved filter "${f.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
      try { await api(`/api/saved_filters/${f.id}`, { method: 'DELETE' }); }
      catch (e) { toast(`Could not delete "${f.name}": ${e.message}`, 6000); return; }
      S.savedFilters = S.savedFilters.filter((x) => x.id !== f.id);
      render();
      updateFiltersButton();
    }

    function focusMore(id) {
      const btn = list.querySelector(`.sf-row[data-id="${id}"] .sf-more`);
      if (btn) btn.focus();
    }

    // Built from live state each time it opens: the move entries read the
    // filter's place in its header set as it is now, not as it was drawn.
    function rowMenu(f, matches, colText) {
      const ids = sameGroupFilterIds(f.col_names);
      const at = ids.indexOf(f.id);
      return [
        {
          label: 'Edit…',
          disabled: !S.sourceId,
          desc: S.sourceId ? null : 'Open a table first',
          title: S.sourceId
            ? `Apply "${f.name}" to the open table and edit its conditions`
            : 'Editing applies the filter so you can see what it matches, which needs a table open',
          onclick: () => editFilter(f, matches, colText),
        },
        { label: 'Rename…', onclick: () => renameFilter(f) },
        '-',
        {
          label: 'Move up', disabled: at <= 0,
          title: 'Earlier in the cycle order for this header set',
          onclick: () => moveFilter(f, -1),
        },
        {
          label: 'Move down', disabled: at < 0 || at >= ids.length - 1,
          title: 'Later in the cycle order for this header set',
          onclick: () => moveFilter(f, 1),
        },
        '-',
        { label: 'Delete…', onclick: () => deleteFilter(f) },
      ];
    }

    function filterRow(f, matches, colText) {
      const row = el('div', 'sf-row' + (matches ? '' : ' sf-other'));
      row.dataset.id = String(f.id);
      const grip = el('span', 'sf-grip', '⠿');
      grip.title = 'Drag to change the cycle order';
      grip.setAttribute('aria-hidden', 'true');
      const name = el('span', 'sf-name', f.name);
      name.setAttribute('role', 'button');
      name.tabIndex = 0;
      if (active && active.id === f.id) name.setAttribute('aria-current', 'true');
      name.title = matches
        ? `Apply "${f.name}"`
        : `Built for different columns (${colText}) — click to apply anyway`;
      name.onclick = () => applyFilter(f, matches, colText);
      name.onkeydown = (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        applyFilter(f, matches, colText);
      };
      const more = el('button', 'btn ghost row-more sf-more', '⋯');
      more.title = 'Edit, rename, move or delete';
      more.setAttribute('aria-label', `Actions for "${f.name}"`);
      more.setAttribute('aria-haspopup', 'menu');
      more.setAttribute('aria-expanded', 'false');
      more.onclick = () => dropdownMenu(more, () => rowMenu(f, matches, colText));
      row.append(grip, name, more);
      // Drag to reorder — same one DnD implementation the tab strip and
      // SQL sub-tabs use. currentIds scopes the drop to this filter's own
      // header set, so dragging across sets is a no-op (wireDragReorder's
      // own from === -1 guard).
      wireDragReorder(row, f.id, {
        containerSelector: '.sf-list',
        rowSelector: '.sf-row',
        horizontal: false,
        currentIds: () => sameGroupFilterIds(f.col_names),
        onReorder: async (ids) => {
          try { S.savedFilters = await post('/api/saved_filters/reorder', { ids }); }
          catch (e) { toast(`Could not reorder: ${e.message}`, 6000); return; }
          render();
        },
      });
      return row;
    }

    function render() {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      // One section per header set — the filters under a set cycle
      // together, so they read together. The open table's set leads.
      const groups = new Map();
      for (const f of S.savedFilters) {
        const sig = headerSig(f.col_names);
        if (!groups.has(sig)) groups.set(sig, { colNames: f.col_names, filters: [] });
        groups.get(sig).filters.push(f);
      }
      const entries = [...groups.values()];
      entries.sort((a, z) => Number(groupMatchesCur(z.colNames)) - Number(groupMatchesCur(a.colNames)));
      let shown = 0;
      for (const g of entries) {
        const nickname = nicknameFor(g.colNames);
        const colText = (g.colNames || []).join(', ');
        const hits = g.filters.filter((f) => !q || f.name.toLowerCase().includes(q)
          || (nickname || '').toLowerCase().includes(q) || colText.toLowerCase().includes(q));
        if (!hits.length) continue;
        shown += hits.length;
        const matches = groupMatchesCur(g.colNames);
        const head = el('div', 'sf-group-head');
        const title = el('span', 'sf-group-name', nickname || colText);
        title.title = colText;
        const nickBtn = el('button', 'btn ghost sf-mini', nickname ? '🏷' : '🏷 name…');
        nickBtn.title = nickname
          ? `Rename this header set's nickname (used by every filter in this section: ${colText})`
          : `Give this header set a nickname instead of showing its raw columns (${colText})`;
        nickBtn.onclick = async () => { await setNicknameFor(g.colNames, nickname); render(); };
        head.append(title, nickBtn);
        if (matches) head.append(el('span', 'count', 'open table'));
        list.append(head);
        for (const f of hits) list.append(filterRow(f, matches, colText));
      }
      if (!shown) list.append(el('div', 'note-status', 'No saved filters match that search.'));
    }
    search.oninput = render;
    render();
    setTimeout(() => search.focus(), 0);

    const acts = el('div', 'row-actions');
    const exp = el('button', 'btn ghost', 'Export filters…');
    exp.onclick = () => { window.location = '/api/saved_filters/export'; };
    const impLabel = el('label', 'btn ghost', 'Import filters…');
    const impInput = el('input');
    impInput.type = 'file';
    impInput.accept = '.json';
    impInput.hidden = true;
    impInput.onchange = async () => {
      const fd = new FormData();
      fd.append('file', impInput.files[0]);
      fd.append('merge', 'true');
      const res = await api('/api/saved_filters/import', { method: 'POST', body: fd });
      await loadSavedFilters();
      render();
      updateFiltersButton();
      toast(`Imported ${res.added} filter${res.added === 1 ? '' : 's'}`);
    };
    impLabel.append(impInput);
    acts.append(exp, impLabel);
    const back = backBtn();
    if (back) acts.append(back);
    b.append(acts);
  });
}

/* `refresh` re-renders whatever surface is hosting the panel after a bulk
   action ("Show all"/"Hide empty") — the per-column checkboxes repaint the
   grid directly and don't need it. */
export function buildColumnsPanel(container, refresh = openTableMenu) {
  if (!S.sourceId) {
    container.append(el('p', null, 'Open a table to manage its columns.'));
    return;
  }
  // A filter box first: a KAPE table has a few hundred columns, and the
  // one to hide or pin is somewhere in the middle. Rows not matching are
  // hidden, not removed — the drag order still runs over all of S.order,
  // so a drop between two visible rows lands where the list shows it.
  const search = el('input', 'panel-search collist-search');
  search.type = 'search';
  search.placeholder = 'Filter columns…';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', 'Filter columns');
  container.append(search);
  const list = el('div', 'collist');
  const rows = new Map();
  const applySearch = () => {
    const q = search.value.trim().toLowerCase();
    for (const [name, row] of rows) row.hidden = !!q && !name.toLowerCase().includes(q);
  };
  search.oninput = applySearch;
  // A column grouped on is in the group strip, not the grid, so it is left
  // out here as it is from the header (visibleCols). It is still in S.order
  // — the drag order below runs over that — and is listed again on Ungroup.
  S.order.filter((n) => !S.groupByCols.includes(n)).forEach((name) => {
    const row = el('div', 'collist-row');
    rows.set(name, row);
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !(S.layout[name] || {}).hidden;
    cb.onchange = () => {
      S.layout[name] = { ...(S.layout[name] || {}), hidden: !cb.checked };
      renderHead(); render(); saveLayout();
    };
    lab.append(cb, el('span', null, name));
    // Pinning sits beside visibility: both are decisions about where a
    // column is, rather than what it contains.
    // Its own class, not collist-pick: that one is the value-picker
    // override, and sharing it made "the row's pick control" ambiguous to
    // anything selecting by class.
    const pin = el('button', 'btn ghost collist-pin');
    const paintPin = () => {
      const on = isPinned(name);
      pin.textContent = on ? '📌 pinned' : 'pin';
      pin.setAttribute('aria-pressed', String(on));
      pin.title = on
        ? `"${name}" stays put when you scroll sideways — click to unpin`
        : `Keep "${name}" visible while scrolling sideways`;
    };
    paintPin();
    pin.onclick = () => { togglePin(name); paintPin(); };
    const c = columnMeta(name);
    lab.append(el('span', 'collist-type', ' ' + (c ? c.type : '') + (c && c.derived ? ' · derived' : '')));
    row.append(lab, pin);
    // Per-column value-picker override, in the same row as the visibility
    // box because they're the same kind of decision about the same column.
    // Three states, not two: following the table's setting is distinct from
    // being explicitly on or off, and only an explicit choice survives the
    // table default changing later.
    const override = (S.layout[name] || {}).valuePicker;
    const state = override === undefined ? 'auto' : (override ? 'on' : 'off');
    // 'auto' alone lied when the table setting was "on for every column" —
    // every row read auto while every dropdown was actually on. Show what
    // auto currently RESOLVES to.
    const label = state === 'auto' ? `auto·${valueFilterEnabled(name) ? 'on' : 'off'}` : state;
    const pick = el('button', 'btn ghost collist-pick', '▾ ' + label);
    pick.setAttribute('aria-pressed', String(valueFilterEnabled(name)));
    pick.title = state === 'auto'
      ? `Value dropdown follows this table's setting (currently ${valueFilterEnabled(name) ? 'on' : 'off'}) — click to pin it on`
      : `Value dropdown pinned ${state} for this column — click to cycle`;
    pick.onclick = () => {
      // auto → on → off → auto: all three states reachable in one direction,
      // including pinning a column on while the table default is off.
      setColumnValueFilter(name, state === 'auto' ? true : (state === 'on' ? false : null));
      refresh();
    };
    row.append(pick);
    // To either end in one click. Drag handles the rest; these handle the
    // case drag is worst at — the column that belongs first on a table
    // with two hundred of them.
    const top = el('button', 'btn ghost collist-move', '⤒');
    top.title = `Move "${name}" to the top`;
    // The row moves in place rather than the panel re-rendering, so a
    // filter typed into the box above survives the click.
    top.onclick = () => { moveColumn(name, 'top'); list.prepend(row); };
    const bottom = el('button', 'btn ghost collist-move', '⤓');
    bottom.title = `Move "${name}" to the bottom`;
    bottom.onclick = () => { moveColumn(name, 'bottom'); list.append(row); };
    row.append(top, bottom);
    // Reorder from here too — the grid's header drag is invisible to
    // anyone working from this panel, and hidden columns can ONLY be
    // repositioned here. Same DnD vocabulary as the tab strip.
    wireDragReorder(row, name, {
      containerSelector: '.collist',
      rowSelector: '.collist-row',
      horizontal: false,
      currentIds: () => [...S.order],
      onReorder: (order) => {
        S.order = order;
        renderHead(); render(); saveLayout();
        // Rows follow the new order in place — like ⤒ ⤓, so a filter
        // typed above survives the drop instead of the panel re-rendering
        // with an empty box and every column back.
        for (const n of order) { const rw = rows.get(n); if (rw) list.append(rw); }
      },
    });
    list.append(row);
  });
  container.append(list);
  container.append(el('p', 'fb-help', 'Drag rows here — or the column headers in the grid — to reorder columns. ⤒ ⤓ send one straight to an end.'));
  const acts = el('div', 'row-actions');
  const addDerived = el('button', 'btn ghost', 'Add derived column…');
  addDerived.title = 'Parse a timestamp, or extract part of a value — JSON/XML field or a regex capture — into its own sortable, filterable column';
  addDerived.onclick = () => openDerivedColumnModal();
  acts.append(addDerived);
  const all = el('button', 'btn ghost', 'Show all');
  all.onclick = () => { for (const n of S.order) S.layout[n] = { ...(S.layout[n] || {}), hidden: false }; renderHead(); render(); saveLayout(); refresh(); };
  const none = el('button', 'btn ghost', 'Hide empty columns');
  none.onclick = async () => {
    // Hide columns with no value in the first 2000 rows of the current view.
    const sample = await api(`/api/rows?view_id=${S.view.view_id}&start=0&count=2000`);
    S.columns.forEach((c, i) => {
      const empty = sample.rows.every((r) => r.cells[i] == null || r.cells[i] === '');
      if (empty) S.layout[c.name] = { ...(S.layout[c.name] || {}), hidden: true };
    });
    renderHead(); render(); saveLayout(); refresh();
  };
  // Rows, not columns: a toggle, since it's a view predicate the server
  // applies (currentSpec's hide_empty_rows) rather than a one-shot edit
  // to S.layout — the row count in the toolbar follows it. Remembered per
  // table in the layout, like the value-filter default.
  const hideRows = el('button', 'btn ghost', 'Hide empty rows');
  hideRows.title = 'Drop rows whose every column is empty — blank lines in a raw-text import, padding rows in an export';
  const paintHideRows = () => hideRows.setAttribute('aria-pressed', String(!!S.hideEmptyRows));
  paintHideRows();
  hideRows.onclick = async () => {
    S.hideEmptyRows = !S.hideEmptyRows;
    paintHideRows();
    saveLayout();
    await rebuildView({ keepScroll: false });
  };
  acts.append(all, none, hideRows);
  container.append(acts);
}

/* The table menu's value-filter section: the table-wide default the
   per-column overrides above fall back to. */
export function buildValueFilterPanel(container, refresh) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const rows = src ? src.row_count : 0;
  container.append(el('p', null,
    'A ▾ button on each column filter box lists that column’s distinct values with counts, '
    + 'so you can tick the ones to filter to. Reading those values is a scan, which is why big '
    + 'tables start with it off.'));
  const seg = el('div', 'vp-seg');
  for (const [key, label, title] of [
    ['auto', `Auto (on under ${VALUE_FILTER_AUTO_MAX.toLocaleString()} rows)`,
      `This table has ${rows.toLocaleString()} rows — auto means ${valueFilterAutoOn() ? 'on' : 'off'} here`],
    ['on', 'On for every column', 'Show the picker on every column regardless of size'],
    ['off', 'Off', 'No picker buttons — the row menu’s "Filter by values…" still opens one'],
  ]) {
    const b = el('button', 'btn ghost', label);
    b.setAttribute('aria-pressed', String(S.valueFilterMode === key));
    b.title = title;
    b.onclick = () => { setValueFilterMode(key); refresh(); };
    seg.append(b);
  }
  container.append(seg);
  const pinned = S.order.filter((n) => (S.layout[n] || {}).valuePicker !== undefined);
  if (pinned.length) {
    const clear = el('button', 'btn ghost', `Clear ${pinned.length} per-column override${pinned.length > 1 ? 's' : ''}`);
    clear.style.marginTop = '10px';
    clear.onclick = () => { for (const n of pinned) setColumnValueFilter(n, null); refresh(); };
    container.append(clear);
  }
}

export function buildTableActionsPanel(container, refresh) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const acts = el('div', 'row-actions');
  // Four long labels don't fit one row of the dialog; squeezed, each one
  // folded onto two lines. Wrap the row instead of the labels.
  acts.style.flexWrap = 'wrap';
  const dflt = el('button', 'btn ghost', 'Save layout as default for these columns');
  dflt.title = 'Reuse this column order/visibility for any table imported with the same headers';
  dflt.onclick = () => saveDefaultLayout();
  // A table opens to the default only until it has a layout of its own,
  // so one that was open before the default was saved needs telling.
  const useDflt = el('button', 'btn ghost', 'Apply default layout for these columns');
  useDflt.title = 'Replace this table\'s column order/visibility with the default saved for these headers';
  useDflt.onclick = () => { $('modal').hidden = true; applyDefaultLayout(); };
  const copyFrom = el('button', 'btn ghost', 'Copy layout from another table…');
  copyFrom.title = 'Give this table the column order, visibility and widths of another table in the case, for the columns they share';
  copyFrom.onclick = () => openCopyLayoutModal();
  const nick = el('button', 'btn ghost', 'Name this header set…');
  nick.onclick = () => setNicknameFor(baseColumns().map((c) => c.name), nicknameFor(baseColumns().map((c) => c.name)));
  const nickTable = el('button', 'btn ghost', src && src.is_merge ? 'Rename this merge…' : 'Nickname this table…');
  nickTable.title = src && src.is_merge
    ? 'Rename this merge'
    : 'A display name shown in place of the file name — clear it to go back';
  nickTable.onclick = async () => {
    if (!src) return;
    if (await editSourceNickname(src)) refresh();
  };
  // The merge builder, started from this table: ticked, with the tables
  // that fit it listed first — or, on a merge, opened on its own tables
  // to change them in place.
  const merge = el('button', 'btn ghost', src && src.is_merge ? 'Edit merge…' : 'Merge with other tables…');
  merge.title = src && src.is_merge
    ? 'Add or remove tables, or change which columns this merge keeps. Its layout stays.'
    : 'One table across this and others, matched by column name';
  merge.onclick = () => {
    if (!src) return;
    openMergeBuilder(src.is_merge ? { edit: src } : { preselect: [src.id] });
  };
  acts.append(dflt, useDflt, copyFrom, nick, nickTable, merge);
  container.append(acts);
}

/* The strip along the top of the table menu: the three things this menu
   is opened FOR most of the time. They used to sit last, under the column
   list — on a wide table that meant scrolling past two hundred rows to
   close a tab. Naming and layout defaults stay at the bottom; those are
   set once per table, not once per hour. */
export function buildTableStripPanel(container) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const strip = el('div', 'table-menu-strip');
  const tables = el('button', 'btn ghost', 'Tables manager…');
  tables.title = 'Every table in the case — indexes, row counts, dropping a table';
  tables.onclick = () => openTablesManager();
  const reset = el('button', 'btn ghost', 'Reset view');
  reset.title = 'Back to the just-opened state: clear filters, search, tag filter and grouping, and restore the default sort. Column layout and the timeframe filter stay (Apply default layout for these columns, below, resets the layout).';
  reset.onclick = async () => {
    $('modal').hidden = true;
    const dt = S.columns.find((c) => c.type === 'datetime');
    S.sort = dt ? [{ column: dt.name, dir: 'asc' }] : [];
    saveLayout();
    await clearAllFilters();
  };
  strip.append(tables, reset);
  if (src && src.is_open) {
    const close = el('button', 'btn ghost table-menu-close', 'Close this tab');
    close.title = 'Stays in the case — reopen it from the sidebar';
    close.onclick = async () => { $('modal').hidden = true; await closeTab(src); };
    strip.append(close);
  }
  container.append(strip);
}

/* The table menu — everything that's about *this table* rather than the
   case or the app. Sections are a registry for the same reason the row
   menu's are: this is where per-table features are expected to land, and
   adding one should be adding an entry. `build` gets (container, refresh)
   and may render nothing at all.

   It lives behind the openTableMenu keybind, the Tables manager's
   Settings… and Columns… on a table's right-click menu (tableMenuItems in
   sources.js) rather than a visible ▦ button, which is the icon this
   replaced — a menu that's going to keep growing needs a home that doesn't
   cost tab-strip width per entry. The right-click opened this dialog
   directly until it got a short menu of its own: a full-height modal was a
   lot to put up for closing or renaming a tab. */
export const TABLE_MENU_SECTIONS = [
  { id: 'strip', title: null, build: buildTableStripPanel },
  { id: 'columns', title: 'Columns', build: buildColumnsPanel },
  { id: 'valueFilters', title: 'Value filter dropdowns', build: buildValueFilterPanel },
  { id: 'table', title: 'This table', build: buildTableActionsPanel },
];

/* `sourceId` is optional — the right-click menu's Columns… and the Tables
   manager pass the table they were opened on, which may not be the one on
   screen. The panels all
   read S.layout/S.order/S.columns (this table's live state), so opening
   that source first isn't a convenience, it's the precondition. */
export async function openTableMenu(sourceId) {
  markModalAction('openTableMenu');
  const id = sourceId === undefined ? S.sourceId : sourceId;
  if (id == null) { toast('Open a table first'); return; }
  if (id !== S.sourceId || S.activeTab !== 'grid') await openSource(id);
  if (S.sourceId !== id) return; // openSource no-ops on an id it can't find
  const src = S.sources.find((x) => x.id === id);
  modal(`Table — ${src ? sourceLabel(src) : ''}`, (b) => {
    for (const section of TABLE_MENU_SECTIONS) {
      const wrap = el('div', 'table-menu-section');
      if (section.title) wrap.append(el('h4', null, section.title));
      section.build(wrap, () => openTableMenu(id));
      b.append(wrap);
    }
  });
}

/* The case's tags, saved as they change (tagEditRow, in tags.js — the
   template editor in Settings is the same row). `focusId` puts the caret
   in that tag's name, selected: Add tag reopens the editor on the tag it
   just made, so "New tag" is typed over rather than clicked into. */
export function openTagEditor({ focusId = null } = {}) {
  let focusRow = null;
  modal('Tags', (b) => {
    b.append(el('p', 'fb-help', 'Changes save as you make them. Keys 1–9 tag the selection.'));
    const list = el('div', 'tag-edit-list');
    b.append(list);
    const rows = [];
    // Anything that redraws the dialog sends a name still waiting on its
    // pause first, so the redraw shows it and a later edit builds on it.
    const settle = () => Promise.all(rows.map((r) => r.flush()));
    for (const t of S.tags) {
      const r = tagEditRow(t, {
        save: async (want) => {
          await post('/api/tags', { id: t.id, ...want });
          await loadTags();
          // A recolour shows on the rows' stripes and the rail at once,
          // not on the next scroll.
          render(); drawRail();
        },
        removeLabel: 'Delete',
        removeTitle: 'Delete this tag and take it off every row',
        remove: async () => {
          if (!(await confirmDialog(`Delete "${t.name}" and remove it from every row?`, { danger: true, okLabel: 'Delete' }))) return;
          await settle();
          try { await api(`/api/tags/${t.id}`, { method: 'DELETE' }); }
          catch (e) { toast(`Could not delete "${t.name}": ${e.message}`, 6000); return; }
          await loadTags();
          // The server took the tag off every row, but the cached rows (flat
          // and grouped) still carry its id, which buildDataRow paints as a
          // grey stripe for a tag it can't name. Same rule as a bulk tag —
          // drop both — and a grouping by tag has just lost a bucket.
          clearRowCaches();
          regroupIfGroupedByTag();
          render(); drawRail(); openTagEditor();
        },
      });
      rows.push(r);
      list.append(r.row);
      if (t.id === focusId) focusRow = r;
    }
    const acts = el('div', 'row-actions');
    const add = el('button', 'btn', 'Add tag');
    add.onclick = oneAtATime(add, async () => {
      await settle();
      let made;
      try { made = await post('/api/tags', newTagDefaults(S.tags)); }
      catch (e) { toast(`Could not add a tag: ${e.message}`, 6000); return; }
      await loadTags();
      openTagEditor({ focusId: made.id });
    });
    const applyTemplate = el('button', 'btn ghost', 'Apply default template');
    applyTemplate.title = "Add any tags from the default template (Settings) that this case doesn't already have";
    applyTemplate.onclick = oneAtATime(applyTemplate, async () => {
      await settle();
      const template = await api('/api/settings/default_tags');
      const existing = new Set(S.tags.map((t) => t.name.toLowerCase()));
      const missing = template.filter((t) => !existing.has((t.name || '').toLowerCase()));
      if (!missing.length) { toast('This case already has every tag in the default template'); return; }
      for (const t of missing) await post('/api/tags', { name: t.name, color: t.color, hotkey: t.hotkey || null });
      await loadTags(); openTagEditor();
      toast(`Added ${missing.length} tag${missing.length > 1 ? 's' : ''} from the default template`);
    });
    acts.append(add, applyTemplate);
    b.append(acts);
  }, { focus: () => (focusRow ? focusRow.name : null) });
}

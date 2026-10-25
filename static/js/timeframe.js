/* The timeframe filter and jump-to-timestamp — the two pieces of time
navigation that survive everything else being cleared.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { copiedColumns, copiedOrder, defaultLayoutResult, findDefaultLayout, isPinned, moveColumn, renderHead, replaceLayout, saveDefaultLayout, saveLayout, togglePin } from './columns.js';
import { $, api, el, nameMap, post, toast } from './core.js';
import { openDerivedColumnModal } from './derived.js';
import { hasActiveFilterTree, openFilterBuilder } from './filterbuilder.js';
import { VALUE_FILTER_AUTO_MAX, setColumnValueFilter, setValueFilterMode, valueFilterAutoOn, valueFilterEnabled } from './filters.js';
import { moveCursor, render } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { openMergeBuilder } from './merge.js';
import { applyPreset, filtersForCurrentSource, headerSig, loadSavedFilters, matchingSavedFilters, nicknameFor, saveHeaderNickname, setNicknameFor } from './savedfilters.js';
import { saveAppearance } from './settings.js';
import { clearAllFilters, closeTab, loadSources, openSource, sourceLabel, wireDragReorder } from './sources.js';
import { normalizeTree, S } from './state.js';
import { countCell, keepingFocus, mergesUsing, openTablesManager, plural } from './tables.js';
import { clearRowCaches, loadTags, newTagDefaults, tagEditRow } from './tags.js';
import { baseColumns, columnMeta, parseTimestamp } from './tsformat.js';
import { closeModal, contextMenu, datePickerButton, dropdownMenu, markModalAction, confirmDialog, modal, oneAtATime, promptDialog, sectionPanes } from './ui.js';
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
export function openSavedFiltersModal({ returnTo = null, section = null, rearm = true } = {}) {
  if (returnTo && rearm) {
    const back = () => {
      document.removeEventListener('winnow:modalclose', back);
      if ($('modalTitle').textContent === 'Saved filters' && $('modalBody').dataset.sfReturn === '1') returnTo();
    };
    document.addEventListener('winnow:modalclose', back);
  }
  // Redrawn whole when its sections change (a set's last filter deleted,
  // an import bringing a new one); the listener above stays armed for it.
  const reopen = (at) => openSavedFiltersModal({ returnTo, section: at, rearm: false });
  const curCols = new Set(baseColumns().map((c) => c.name.trim().toLowerCase()));
  const groupMatchesCur = (colNames) => {
    const cols = new Set((colNames || []).map((c) => c.trim().toLowerCase()));
    return cols.size === curCols.size && [...cols].every((c) => curCols.has(c));
  };
  /* One section per header set — the filters under a set cycle together,
     so they read together. The open table's set leads, and is where the
     dialog opens; All lists every set under its own heading. */
  const setsNow = () => {
    const groups = new Map();
    for (const f of S.savedFilters) {
      const sig = headerSig(f.col_names);
      if (!groups.has(sig)) groups.set(sig, { sig, colNames: f.col_names, filters: [] });
      groups.get(sig).filters.push(f);
    }
    return [...groups.values()].sort((a, z) => Number(groupMatchesCur(z.colNames)) - Number(groupMatchesCur(a.colNames)));
  };
  const sets = setsNow();
  const setLabel = (g) => nicknameFor(g.colNames) || (g.colNames || []).slice(0, 3).join(', ') + ((g.colNames || []).length > 3 ? ` +${g.colNames.length - 3}` : '');
  let query = '';

  modal('Saved filters', (b) => {
    // Marks THIS opening as the one with a way back, so a listener left
    // armed by a close that bypassed the event can't fire for a later
    // Saved filters opened from Filters ▾.
    b.dataset.sfReturn = returnTo ? '1' : '';
    const active = activeSavedFilterRecord();

    async function applyFilter(f, matches) {
      if (!matches && !(await confirmDialog(`"${f.name}" was built for a different column set (${f.col_names.join(', ')}). Apply anyway?`))) return;
      $('modal').hidden = true;
      applyPreset(f);
    }

    // Editing routes through the real grid: apply the filter, then open
    // the builder pre-loaded with its payload, so the match count behind
    // the modal is live feedback on the change being made.
    async function editFilter(f, matches) {
      if (!matches && !(await confirmDialog(
        `"${f.name}" was built for a different column set (${f.col_names.join(', ')}). Edit it against the open table anyway? `
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
      frame.update();
      focusMore(f.id);
      updateFiltersButton();
    }

    async function moveFilter(f, dir) {
      try { await moveSavedFilter(f, dir); } catch (e) { toast(`Could not move "${f.name}": ${e.message}`, 6000); return; }
      frame.update();
      // Back on the same filter's ⋯, so a second Move up is Enter and
      // an arrow away — reordering without a mouse is a row of keys.
      focusMore(f.id);
    }

    async function deleteFilter(f) {
      if (!(await confirmDialog(`Delete saved filter "${f.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
      try { await api(`/api/saved_filters/${f.id}`, { method: 'DELETE' }); }
      catch (e) { toast(`Could not delete "${f.name}": ${e.message}`, 6000); return; }
      S.savedFilters = S.savedFilters.filter((x) => x.id !== f.id);
      updateFiltersButton();
      // The last filter of a set takes its section with it.
      if (setsNow().length !== sets.length) { reopen(frame.current() === 'all' ? 'all' : null); return; }
      frame.update();
    }

    function focusMore(id) {
      const btn = frame.pane.querySelector(`.sf-row[data-id="${id}"] .sf-more`);
      if (btn) btn.focus();
    }

    // Built from live state each time it opens: the move entries read the
    // filter's place in its header set as it is now, not as it was drawn.
    function rowMenu(f, matches) {
      const ids = sameGroupFilterIds(f.col_names);
      const at = ids.indexOf(f.id);
      return [
        { label: 'Apply', onclick: () => applyFilter(f, matches) },
        {
          label: 'Edit…',
          disabled: !S.sourceId,
          desc: S.sourceId ? null : 'Open a table first',
          title: S.sourceId
            ? `Apply "${f.name}" to the open table and edit its conditions`
            : 'Editing applies the filter so you can see what it matches, which needs a table open',
          onclick: () => editFilter(f, matches),
        },
        { label: 'Rename…', onclick: () => renameFilter(f) },
        '-',
        { label: 'Move up', disabled: at <= 0, title: 'Earlier in the cycle order for this header set', onclick: () => moveFilter(f, -1) },
        { label: 'Move down', disabled: at < 0 || at >= ids.length - 1, title: 'Later in the cycle order for this header set', onclick: () => moveFilter(f, 1) },
        '-',
        { label: 'Delete…', onclick: () => deleteFilter(f) },
      ];
    }

    /* What a filter does, in a few words: the columns its conditions are
       on, and how many conditions — the old list showed the name alone,
       and twenty "EVTX — …" names tell each other apart by their ends. */
    function conditionsOf(tree) {
      const cols = [];
      let n = 0;
      const walk = (node) => {
        if (!node) return;
        if (node.type === 'cond') { n += 1; if (!cols.includes(node.column)) cols.push(node.column); }
        else if (node.type === 'raw') { n += 1; if (!cols.includes('SQL')) cols.push('SQL'); }
        else for (const c of node.children || []) walk(c);
      };
      walk(tree);
      return { n, cols };
    }

    function filterRow(f, matches) {
      const r = el('div', 'tables-row sf-row' + (matches ? ' open' : ' sf-other'));
      r.dataset.id = String(f.id);
      const grip = el('span', 'sf-grip', '⠿');
      grip.title = 'Drag to change the cycle order';
      grip.setAttribute('aria-hidden', 'true');
      const cell = el('span', 'tables-name-cell');
      const name = el('button', 'tables-name sf-name', f.name);
      name.type = 'button';
      name.dataset.role = 'name';
      if (active && active.id === f.id) name.setAttribute('aria-current', 'true');
      name.title = matches ? `Apply "${f.name}"` : `Built for different columns — click to apply anyway`;
      name.onclick = () => applyFilter(f, matches);
      cell.append(name);
      if (active && active.id === f.id) cell.append(el('span', 'tables-sub sf-on', 'applied'));
      const p = f.payload || {};
      const { n, cols } = conditionsOf(p.filter_tree);
      const extra = [p.search && 'search', (p.group_by || []).length && `grouped by ${p.group_by.join(', ')}`].filter(Boolean);
      const on = el('span', 'sf-on-cols', cols.join(', ') + (extra.length ? (cols.length ? ' · ' : '') + extra.join(' · ') : ''));
      on.title = on.textContent;
      const more = el('button', 'btn ghost row-more tables-more sf-more', '⋯');
      more.dataset.role = 'more';
      more.title = 'Apply, edit, rename, move or delete';
      more.setAttribute('aria-label', `Actions for "${f.name}"`);
      more.setAttribute('aria-haspopup', 'menu');
      more.setAttribute('aria-expanded', 'false');
      more.onclick = () => dropdownMenu(more, () => rowMenu(f, matches));
      r.append(grip, cell, on, countCell(n, 'tables-notes'), more);
      r.addEventListener('contextmenu', (e) => { e.preventDefault(); contextMenu(e, rowMenu(f, matches)); });
      // Drag to reorder — the one DnD implementation the tab strip and SQL
      // sub-tabs use, scoped to this filter's own header set.
      wireDragReorder(r, f.id, {
        containerSelector: '.sf-list',
        rowSelector: '.sf-row',
        horizontal: false,
        currentIds: () => sameGroupFilterIds(f.col_names),
        onReorder: async (ids) => {
          try { S.savedFilters = await post('/api/saved_filters/reorder', { ids }); }
          catch (e) { toast(`Could not reorder: ${e.message}`, 6000); return; }
          frame.update();
        },
      });
      return r;
    }

    function groupHead(g, filters) {
      const h = el('div', 'tables-group');
      const name = el('span', 'tables-group-name', setLabel(g));
      name.title = (g.colNames || []).join(', ');
      h.append(name, el('span', 'tables-group-meta', plural(filters.length, 'filter') + (groupMatchesCur(g.colNames) ? ' · open table' : '')));
      return h;
    }

    /* A set's pane, or All's: a filter over names, nicknames and columns on
       top, the set's columns under it, then the box that scrolls. */
    const buildPane = (sig) => (pane) => {
      const top = el('div', 'tm-cols-top');
      const search = el('input', 'side-search');
      search.type = 'search';
      search.placeholder = 'Filter by name, nickname or column…';
      search.setAttribute('aria-label', 'Filter saved filters');
      search.autocomplete = 'off';
      search.value = query;
      const count = el('span', 'tm-cols-count');
      top.append(search, count);
      pane.append(top);
      const g0 = sig ? sets.find((g) => g.sig === sig) : null;
      if (g0) {
        const about = el('div', 'sf-set');
        const cols = el('span', 'sf-set-cols', `${plural(g0.colNames.length, 'column')}: ${g0.colNames.join(', ')}`);
        cols.title = g0.colNames.join(', ');
        const nick = el('button', 'btn ghost mb-small', nicknameFor(g0.colNames) ? 'Rename nickname…' : 'Nickname…');
        nick.title = 'What this header set is called here, in the Filters ▾ menu and in the Tables manager';
        nick.onclick = async () => { await setNicknameFor(g0.colNames, nicknameFor(g0.colNames)); reopen(sig); };
        about.append(cols, nick);
        pane.append(about);
      }
      const box = el('div', 'side-preview tm-cols-box tables-box sf-box');
      const head = el('div', 'tables-head sf-head');
      head.append(el('span'), el('span', 'tables-name-h', 'Filter'), el('span', null, 'Filters on'), el('span', 'tables-num', 'Conds'), el('span'));
      const scroller = el('div', 'side-preview-rows');
      const list = el('div', 'tables-list sf-list');
      scroller.append(list);
      const foot = el('div', 'side-preview-foot');
      foot.append(el('p', 'fb-help',
        `Click a name to apply it. Drag a row to set the ${S.keymap.cyclePrevFilter[0] || '['} / `
        + `${S.keymap.cycleNextFilter[0] || ']'} cycle order.`));
      box.append(head, scroller, foot);
      pane.append(box);

      function paint() {
        const q = query.trim().toLowerCase();
        const groups = setsNow().filter((g) => !sig || g.sig === sig);
        let all = 0, shown = 0;
        const nodes = [];
        for (const g of groups) {
          const nickname = nicknameFor(g.colNames) || '';
          const colText = (g.colNames || []).join(', ').toLowerCase();
          all += g.filters.length;
          const hits = g.filters.filter((f) => !q || f.name.toLowerCase().includes(q) || nickname.toLowerCase().includes(q) || colText.includes(q));
          if (!hits.length) continue;
          shown += hits.length;
          if (!sig) nodes.push(groupHead(g, hits));
          const matches = groupMatchesCur(g.colNames);
          nodes.push(...hits.map((f) => filterRow(f, matches)));
        }
        if (!all) nodes.push(el('p', 'fb-help tables-empty', 'No saved filters yet. Build one in the Filter builder, then Save filter….'));
        else if (!shown) nodes.push(el('p', 'fb-help tables-empty', `No saved filter matches “${query.trim()}”.`));
        count.textContent = q ? `${shown} of ${plural(all, 'filter')}` : plural(all, 'filter');
        keepingFocus(list, () => list.replaceChildren(...nodes));
      }
      search.oninput = () => { query = search.value; paint(); };
      search.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ''; query = ''; paint(); }
      });
      paint();
      return { update: paint };
    };

    const frame = sectionPanes({
      sections: [
        { id: 'all', group: 'Saved filters', title: 'Every saved filter', build: buildPane(null),
          hint: () => `${plural(S.savedFilters.length, 'filter')} · ${plural(setsNow().length, 'header set')}` },
        ...sets.map((g) => ({
          id: 'set:' + g.sig, group: 'Header sets', title: setLabel(g), build: buildPane(g.sig),
          hint: () => {
            const n = S.savedFilters.filter((f) => headerSig(f.col_names) === g.sig).length;
            return plural(n, 'filter') + (groupMatchesCur(g.colNames) ? ' · open table' : '');
          },
        })),
      ],
      label: 'Saved filter sections', cls: 'list-panes sf-manager', paneCls: 'list-pane sf-pane', changedEvent: 'savedfilters:changed',
    });
    b.append(frame.panes);

    const foot = el('div', 'table-menu-foot table-menu-strip');
    if (returnTo) {
      const back = el('button', 'btn ghost sf-back', '‹ Settings');
      back.onclick = () => returnTo();
      foot.append(back);
    }
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
      updateFiltersButton();
      toast(`Imported ${res.added} filter${res.added === 1 ? '' : 's'}`);
      reopen(frame.current());   // an import can bring header sets of its own
    };
    impLabel.append(impInput);
    const exp = el('button', 'btn ghost', 'Export filters…');
    exp.onclick = () => { window.location = '/api/saved_filters/export'; };
    const builder = el('button', 'btn ghost table-menu-close', 'Filter builder…');
    builder.disabled = !S.sourceId;
    builder.title = S.sourceId ? 'Build a filter on the open table, then Save filter…' : 'Open a table first';
    builder.onclick = () => openFilterBuilder();
    foot.append(impLabel, exp, builder);
    b.append(foot);

    const mine = sets.find((g) => groupMatchesCur(g.colNames));
    const want = section && (section === 'all' || sets.some((g) => 'set:' + g.sig === section)) ? section
      : mine ? 'set:' + mine.sig : 'all';
    frame.show(want);
  }, { wide: true, tall: true, focus: (b) => b.querySelector('.sf-pane .side-search') });
}

/* The Columns pane of the table menu. `refresh` repaints the pane after a
   bulk action ("Show all"/"Hide empty") — the per-column checkboxes
   repaint the grid directly and don't need it. Anything that changes what
   the side list says about this pane (how many columns are shown) sends
   `tablemenu:changed` up from the container instead. */
export function buildColumnsPanel(container, refresh = () => openTableMenu()) {
  if (!S.sourceId) {
    container.append(el('p', null, 'Open a table to manage its columns.'));
    return;
  }
  const changed = () => container.dispatchEvent(new CustomEvent('tablemenu:changed', { bubbles: true }));
  // A filter box first: a KAPE table has a few hundred columns, and the
  // one to hide or pin is somewhere in the middle. Rows not matching are
  // hidden, not removed — the drag order still runs over all of S.order,
  // so a drop between two visible rows lands where the list shows it.
  const top = el('div', 'tm-cols-top');
  const search = el('input', 'side-search collist-search');
  search.type = 'search';
  search.placeholder = 'Filter columns…';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', 'Filter columns');
  const count = el('span', 'tm-cols-count');
  top.append(search, count);
  container.append(top);
  // The list is the one part of the pane that takes what is left and
  // scrolls, the way the derive dialog's settings and the folder import's
  // file list do, so the filter box and the buttons under it stay put.
  const box = el('div', 'side-preview tm-cols-box');
  const scroller = el('div', 'side-preview-rows');
  const list = el('div', 'collist');
  scroller.append(list);
  const foot = el('div', 'side-preview-foot');
  foot.append(el('p', 'fb-help', 'Drag a row, or a column header in the grid, to reorder. ⤒ ⤓ send one straight to an end.'));
  box.append(scroller, foot);
  const rows = new Map();
  const applySearch = () => {
    const q = search.value.trim().toLowerCase();
    for (const [name, row] of rows) row.hidden = !!q && !name.toLowerCase().includes(q);
  };
  search.oninput = applySearch;
  // A column grouped on is in the group strip, not the grid, so it is left
  // out here as it is from the header (visibleCols). It is still in S.order
  // — the drag order below runs over that — and is listed again on Ungroup.
  const listed = S.order.filter((n) => !S.groupByCols.includes(n));
  const paintCount = () => {
    const shown = listed.filter((n) => !(S.layout[n] || {}).hidden).length;
    count.textContent = `${shown} of ${listed.length} shown`;
  };
  paintCount();
  listed.forEach((name) => {
    const row = el('div', 'collist-row');
    rows.set(name, row);
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !(S.layout[name] || {}).hidden;
    cb.onchange = () => {
      S.layout[name] = { ...(S.layout[name] || {}), hidden: !cb.checked };
      renderHead(); render(); saveLayout();
      paintCount(); changed();
    };
    lab.append(cb, el('span', 'collist-name', name));
    // Pinning sits beside visibility: both are decisions about where a
    // column is, rather than what it contains.
    // Its own class, not collist-pick: that one is the value-picker
    // override, and sharing it made "the row's pick control" ambiguous to
    // anything selecting by class.
    const pin = el('button', 'btn ghost collist-pin');
    const paintPin = () => {
      const on = isPinned(name);
      pin.textContent = on ? 'pinned' : 'pin';
      pin.setAttribute('aria-pressed', String(on));
      pin.title = on
        ? `"${name}" stays put when you scroll sideways — click to unpin`
        : `Keep "${name}" visible while scrolling sideways`;
    };
    paintPin();
    pin.onclick = () => { togglePin(name); paintPin(); changed(); };
    const c = columnMeta(name);
    const type = (c ? c.type : '') + (c && c.derived ? ' · derived' : '');
    lab.append(el('span', 'collist-type', ' ' + type));
    // A narrow window cuts a long name short; hovering shows it whole.
    lab.title = `${name} ${type}`;
    row.append(lab, pin);
    // Per-column value-picker override, in the same row as the visibility
    // box because they're the same kind of decision about the same column.
    // Three states, not two: following the table's setting is distinct from
    // being explicitly on or off, and only an explicit choice survives the
    // table default changing later.
    const pick = el('button', 'btn ghost collist-pick');
    const pickState = () => {
      const override = (S.layout[name] || {}).valuePicker;
      return override === undefined ? 'auto' : (override ? 'on' : 'off');
    };
    const paintPick = () => {
      const state = pickState();
      // 'auto' alone lied when the table setting was "on for every column" —
      // every row read auto while every dropdown was actually on. Show what
      // auto currently RESOLVES to.
      const on = valueFilterEnabled(name);
      pick.textContent = '▾ ' + (state === 'auto' ? `auto·${on ? 'on' : 'off'}` : state);
      // Only a choice made for this column is drawn at full strength; a row
      // following the table's setting says the same as every other row, so
      // it stays faint until the row is under the pointer, like ⤒ ⤓.
      pick.classList.toggle('set', state !== 'auto');
      pick.setAttribute('aria-pressed', String(on));
      pick.title = state === 'auto'
        ? `Value dropdown follows this table's setting (currently ${on ? 'on' : 'off'}) — click to pin it on`
        : `Value dropdown pinned ${state} for this column — click to cycle`;
    };
    paintPick();
    pick.onclick = () => {
      // auto → on → off → auto: all three states reachable in one direction,
      // including pinning a column on while the table default is off. The
      // button repaints in place, so the filter and the scroll stay put.
      const state = pickState();
      setColumnValueFilter(name, state === 'auto' ? true : (state === 'on' ? false : null));
      paintPick(); changed();
    };
    row.append(pick);
    // To either end in one click. Drag handles the rest; these handle the
    // case drag is worst at — the column that belongs first on a table
    // with two hundred of them.
    const topBtn = el('button', 'btn ghost collist-move', '⤒');
    topBtn.title = `Move "${name}" to the top`;
    // The row moves in place rather than the panel re-rendering, so a
    // filter typed into the box above survives the click.
    topBtn.onclick = () => { moveColumn(name, 'top'); list.prepend(row); };
    const bottom = el('button', 'btn ghost collist-move', '⤓');
    bottom.title = `Move "${name}" to the bottom`;
    bottom.onclick = () => { moveColumn(name, 'bottom'); list.append(row); };
    row.append(topBtn, bottom);
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
  container.append(box);
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

/* A figure over a dim caps label, the merge builder's (.mb-stat). */
export function tableMenuStat(value, label) {
  const s = el('div', 'mb-stat');
  s.append(el('b', null, typeof value === 'number' ? value.toLocaleString() : value), el('span', null, label));
  return s;
}

/* A segmented control in the builders' tint (.mb-seg): `options` is
   [key, label, title], `current()` the pressed key, `pick(key)` what a
   click does. Returns the node and a paint() for after a change. */
export function tableMenuSeg(options, current, pick, cls = '') {
  const seg = el('div', 'mb-seg ' + cls);
  const buttons = options.map(([key, label, title]) => {
    const b = el('button', null, label);
    b.type = 'button';
    b.dataset.key = key;
    if (title) b.title = title;
    b.onclick = () => pick(key);
    seg.append(b);
    return b;
  });
  const paint = () => { for (const b of buttons) b.setAttribute('aria-pressed', String(b.dataset.key === current())); };
  paint();
  return { seg, buttons, paint };
}

/* A field in the builders' shape (.mb-field): a dim caps label above
   whatever the field holds. */
export function tableMenuField(label, ...nodes) {
  const f = el('div', 'mb-field tm-field');
  f.append(el('label', null, label), ...nodes);
  return f;
}

/* The table menu's value-dropdown section, in the merge builder's shape:
   what the table's setting comes to on top with the setting itself
   beside it, then every column with its own Auto / On / Off. The list is
   the Columns pane's order, and a click changes the row in place rather
   than re-sorting it, so the row under the pointer stays there. */
export function buildValueFilterPanel(container) {
  const changed = () => container.dispatchEvent(new CustomEvent('tablemenu:changed', { bubbles: true }));
  const src = S.sources.find((s) => s.id === S.sourceId);
  const rows = src ? src.row_count : 0;
  const listed = S.order.filter((n) => !S.groupByCols.includes(n));
  const setByHand = () => listed.filter((n) => (S.layout[n] || {}).valuePicker !== undefined);

  const summary = el('div', 'mb-summary');
  const stats = el('div', 'mb-stats');
  const table = tableMenuSeg([
    ['auto', `Auto · under ${VALUE_FILTER_AUTO_MAX.toLocaleString()} rows`, `This table has ${rows.toLocaleString()} rows`],
    ['on', 'On', 'A ▾ on every column, whatever the table’s size'],
    ['off', 'Off', 'No ▾ buttons — the row menu’s "Filter by values…" still opens one'],
  ], () => S.valueFilterMode, (key) => { setValueFilterMode(key); paint(); changed(); });
  summary.append(stats, table.seg);
  const notes = el('ul', 'mb-notes');
  container.append(summary, notes);

  const top = el('div', 'tm-cols-top');
  const search = el('input', 'side-search');
  search.type = 'search';
  search.placeholder = 'Filter columns…';
  search.setAttribute('aria-label', 'Filter columns');
  top.append(search);
  container.append(top);
  const box = el('div', 'side-preview tm-cols-box');
  const scroller = el('div', 'side-preview-rows');
  const list = el('div', 'tm-vf-list');
  scroller.append(list);
  const foot = el('div', 'side-preview-foot tm-vf-foot');
  const footText = el('p', 'fb-help');
  const clear = el('button', 'btn ghost');
  clear.onclick = () => { for (const n of setByHand()) setColumnValueFilter(n, null); paint(); changed(); };
  foot.append(footText, clear);
  box.append(scroller, foot);
  container.append(box);

  const rowsByName = new Map();
  for (const name of listed) {
    const row = el('div', 'tm-vf-row');
    row.dataset.col = name;
    const c = columnMeta(name);
    const label = el('span', 'tm-vf-name');
    label.append(el('span', 'collist-name', name), el('span', 'collist-type', ' ' + (c ? c.type : '')));
    label.title = name;
    const state = () => {
      const v = (S.layout[name] || {}).valuePicker;
      return v === undefined ? 'auto' : (v ? 'on' : 'off');
    };
    const seg = tableMenuSeg([
      ['auto', 'Auto', 'Follow this table’s setting'],
      ['on', 'On', `Always a ▾ on "${name}"`],
      ['off', 'Off', `Never a ▾ on "${name}"`],
    ], state, (key) => {
      setColumnValueFilter(name, key === 'auto' ? null : key === 'on');
      paint(); changed();
    }, 'tm-vf-seg');
    row.append(label, seg.seg);
    list.append(row);
    rowsByName.set(name, { row, seg, state });
  }
  search.oninput = () => {
    const q = search.value.trim().toLowerCase();
    for (const [name, r] of rowsByName) r.row.hidden = !!q && !name.toLowerCase().includes(q);
  };

  function paint() {
    table.paint();
    const withPicker = listed.filter((n) => valueFilterEnabled(n)).length;
    const byHand = setByHand();
    stats.replaceChildren(
      tableMenuStat(withPicker, 'with a ▾'),
      tableMenuStat(byHand.length, 'set by hand'),
      tableMenuStat(rows, 'rows'));
    const on = valueFilterAutoOn();
    const line = S.valueFilterMode === 'auto'
      ? `Auto is ${on ? 'on' : 'off'} here: ${rows.toLocaleString()} rows is ${on ? 'under' : 'over'} the ${VALUE_FILTER_AUTO_MAX.toLocaleString()} where listing a column’s values gets slow.`
      : S.valueFilterMode === 'on'
        ? 'Every column gets a ▾, whatever the table’s size. On a big table the first open of one can take a while.'
        : 'No column gets a ▾ unless it is set On below. The row menu’s "Filter by values…" still opens one.';
    const li = el('li', null, line);
    notes.replaceChildren(li);
    for (const [name, { row, seg, state }] of rowsByName) {
      seg.paint();
      const auto = state() === 'auto';
      row.classList.toggle('set', !auto);
      // Auto's own button says what auto comes to for this column.
      seg.buttons[0].textContent = auto ? `Auto · ${valueFilterEnabled(name) ? 'on' : 'off'}` : 'Auto';
    }
    footText.textContent = `${withPicker} of ${listed.length} columns get a ▾${byHand.length ? `, ${byHand.length} of them set by hand` : ''}.`;
    clear.textContent = byHand.length > 1 ? `Set all ${byHand.length} back to Auto` : 'Set back to Auto';
    clear.hidden = !byHand.length;
  }
  paint();
}

/* The tables the Layout pane can take a layout from, after the default
   saved for this header set: every other table sharing a column with this
   one, the same header set first, then by how many columns they share. */
const LAYOUT_SAME = 'Same columns';
const LAYOUT_SOME = 'Some columns in common';
function layoutSources() {
  const id = S.sourceId;
  const mine = new Set(S.columns.map((c) => c.name));
  const headerSet = (cols) => cols.filter((c) => !c.derived).map((c) => c.name.toLowerCase()).sort().join('\n');
  const myHeaders = headerSet(S.columns);
  const base = baseColumns().length;
  return S.sources.filter((s) => s.id !== id && !s.error).map((s) => {
    const cols = s.columns || [];
    const shared = cols.filter((c) => mine.has(c.name)).length;
    return {
      key: String(s.id), label: sourceLabel(s), src: s, shared,
      group: headerSet(cols) === myHeaders ? LAYOUT_SAME : LAYOUT_SOME,
      hint: shared >= base ? `all ${shared} columns` : `${shared} of ${base} columns`,
    };
  }).filter((e) => e.shared > 0)
    .sort((a, b) => (a.group === b.group ? 0 : a.group === LAYOUT_SAME ? -1 : 1)
      || b.shared - a.shared || a.label.localeCompare(b.label));
}

/* The columns a new order actually moves. Comparing positions marks
   every column after one that moved to the top as "moved" too; what
   moved is what falls outside the longest run of columns still in their
   old relative order. */
function movedColumns(before, after) {
  const at = after.map((n) => before.indexOf(n));
  const tails = [];   // tails[k]: index into `after` ending the best run of length k+1
  const prev = new Array(after.length).fill(-1);
  at.forEach((pos, i) => {
    if (pos < 0) return;
    let lo = 0, hi = tails.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (at[tails[mid]] < pos) lo = mid + 1; else hi = mid; }
    if (lo > 0) prev[i] = tails[lo - 1];
    tails[lo] = i;
  });
  const kept = new Set();
  for (let i = tails.length ? tails[tails.length - 1] : -1; i >= 0; i = prev[i]) kept.add(after[i]);
  return new Set(after.filter((n, i) => at[i] >= 0 && !kept.has(n)));
}

/* The table menu's layout section: where to take a layout from, the
   columns as they would be after it with what changes marked, and one
   button that applies it. The default for this header set and the other
   tables are places in one list, so applying the default gets the same
   preview copying another table's layout had in its own dialog (Copy
   layout from another table…, which this replaced). Saving this table's
   layout as that default sits on the left of the foot, the way Import a
   folder keeps Save as profile… */
export function buildLayoutPanel(container) {
  const id = S.sourceId;
  const changed = () => container.dispatchEvent(new CustomEvent('tablemenu:changed', { bubbles: true }));
  const others = layoutSources();
  const from = el('select', 'tm-layout-from');
  from.setAttribute('aria-label', 'Take the layout from');
  const dfltOpt = new Option('The default for these columns — looking…', 'default');
  dfltOpt.disabled = true;
  from.append(dfltOpt);
  for (const group of [LAYOUT_SAME, LAYOUT_SOME]) {
    const members = others.filter((e) => e.group === group);
    if (!members.length) continue;
    const og = el('optgroup');
    og.label = group;
    for (const e of members) og.append(new Option(`${e.label} — ${e.hint}`, e.key));
    from.append(og);
  }
  container.append(tableMenuField('Take the layout from', from));
  const notes = el('ul', 'mb-notes');
  container.append(notes);

  const head = el('div', 'side-preview-title', 'Columns after applying');
  const box = el('div', 'side-preview tm-layout-preview');
  const rows = el('div', 'side-preview-rows');
  const foot = el('div', 'side-preview-foot');
  box.append(rows, foot);
  container.append(head, box);

  const acts = el('div', 'row-actions tm-foot-actions');
  const save = el('button', 'btn ghost', 'Save this layout as the default');
  save.title = 'Reuse this table’s column order and visibility for any table imported with the same headers';
  save.onclick = async () => { await saveDefaultLayout(); picked = false; loadDefault(); changed(); };
  const apply = el('button', 'btn primary', 'Apply layout');
  apply.disabled = true;
  acts.append(save, apply);
  container.append(acts);

  let found;          // the saved default once asked: an object, or null for none
  let result = null;  // { order, layout, message } for what is chosen
  const layouts = new Map();   // source id -> its saved layout, asked once

  function loadDefault() {
    findDefaultLayout().then((f) => {
      found = f;
      if (!box.isConnected) return;
      const name = nicknameFor(baseColumns().map((c) => c.name));
      dfltOpt.disabled = !f;
      dfltOpt.textContent = f
        ? `The default saved for ${name || 'these columns'}`
        : `The default for ${name || 'these columns'} — none saved yet`;
      // The saved default is the first place to look, until the analyst
      // has picked another.
      if (f && !picked) from.value = 'default';
      else if (!f && from.value === 'default' && others.length) from.value = others[0].key;
      preview();
    }).catch(() => { found = null; if (box.isConnected) preview(); });
  }

  async function preview() {
    const key = from.value;
    result = null;
    apply.disabled = true;
    box.classList.add('stale');
    let order, layout, line, message;
    if (key === 'default' && found) {
      ({ order, layout } = defaultLayoutResult(found));
      line = `Saved for every table imported with these ${baseColumns().length} headers. A table that already has a layout keeps it until one is applied here.`;
      message = 'Applied the default layout for these columns';
    } else if (key && key !== 'default') {
      const entry = others.find((e) => e.key === key);
      if (!layouts.has(entry.src.id)) {
        layouts.set(entry.src.id, api(`/api/layout?source_id=${entry.src.id}`).catch(() => ({})));
      }
      const saved = await layouts.get(entry.src.id);
      if (from.value !== key || !box.isConnected || S.sourceId !== id) return;
      // A table never laid out shows its columns as the file had them.
      const fromOrder = saved.order || (entry.src.columns || []).map((c) => c.name);
      const copied = copiedOrder(fromOrder);
      order = copied.order;
      layout = copiedColumns(nameMap(saved.columns), copied.shared);
      const own = order.length - copied.shared.size;
      line = `The order, visibility and widths of ${entry.label}, for the ${copied.shared.size} columns the two share.`
        + (own ? ` ${own} only this table has ${own === 1 ? 'stays beside the column it follows' : 'stay beside the columns they follow'}.` : '');
      message = `Copied the layout of ${entry.label}`;
    } else {
      notes.replaceChildren(el('li', null, 'Nothing to take a layout from yet: no default is saved for these columns, and no other table shares any of them.'));
      rows.replaceChildren();
      foot.replaceChildren();
      box.classList.remove('stale');
      return;
    }
    notes.replaceChildren(el('li', null, line));
    // What applying would change, row by row, against the table now.
    const before = S.order.filter((n) => !S.groupByCols.includes(n));
    const after = order.filter((n) => !S.groupByCols.includes(n));
    const moved = movedColumns(before, after);
    const list = el('div', 'copy-layout-cols');
    let changes = 0;
    after.forEach((n) => {
      const now = S.layout[n] || {};
      const then = layout[n] || {};
      const what = [];
      if (!!now.hidden !== !!then.hidden) what.push(then.hidden ? 'hidden' : 'shown');
      if (!!now.pinned !== !!then.pinned) what.push(then.pinned ? 'pinned' : 'unpinned');
      if (moved.has(n)) what.push('moved');
      if ((now.w || null) !== (then.w || null)) what.push('width');
      const r = el('div', 'copy-layout-col' + (then.hidden ? ' hidden-col' : '') + (what.length ? ' tm-changed' : ''));
      r.dataset.col = n;
      r.append(el('span', 'copy-layout-name', n));
      const notesFor = [];
      if (then.hidden && !what.includes('hidden')) notesFor.push('hidden');
      if (then.pinned && !what.includes('pinned')) notesFor.push('pinned');
      if (notesFor.length) r.append(el('span', 'copy-layout-note', notesFor.join(' · ')));
      if (what.length) r.append(el('span', 'tm-change', what.join(' · ')));
      if (what.length) changes++;
      list.append(r);
    });
    rows.replaceChildren(list);
    const verdict = changes
      ? `${changes} of ${after.length} columns change.`
      : 'Nothing changes: this table already has this layout.';
    const p = el('p', 'fb-help', verdict);
    foot.replaceChildren(p);
    box.classList.remove('stale');
    result = { order, layout, message };
    apply.disabled = !changes;
  }

  let picked = false;
  from.onchange = () => { picked = true; preview(); };
  apply.onclick = () => {
    if (!result || S.sourceId !== id) return;
    closeModal();
    replaceLayout(result.order, result.layout, result.message);
  };
  if (others.length) from.value = others[0].key;
  loadDefault();
}

/* The table menu's name-and-merge section: the names saved as they are
   typed, the way the tag editor saves, what the table holds, and the
   merge builder with what it would find. */
export function buildNamePanel(container, refresh) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  if (!src) return;
  const merge = !!src.is_merge;
  const changed = () => container.dispatchEvent(new CustomEvent('tablemenu:changed', { bubbles: true }));

  /* A text box that saves on Enter or on leaving it, and says so. */
  function savingInput(value, placeholder, saveFn) {
    const wrap = el('div', 'tm-input');
    const input = el('input');
    input.value = value;
    input.placeholder = placeholder;
    input.spellcheck = false;
    const said = el('span', 'tm-saved');
    let last = value;
    const commit = async () => {
      const v = input.value.trim();
      if (v === last) return;
      try {
        await saveFn(v);
        last = v;
        said.textContent = 'Saved';
        setTimeout(() => { said.textContent = ''; }, 1600);
        changed();
      } catch (e) {
        toast('Could not save that: ' + e.message, 4000);
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape' && input.value !== last) { e.preventDefault(); e.stopPropagation(); input.value = last; }
    });
    input.addEventListener('blur', commit);
    wrap.append(input, said);
    return wrap;
  }

  const stats = el('div', 'mb-stats tm-about');
  const cols = baseColumns();
  const derived = S.columns.length - cols.length;
  stats.append(
    tableMenuStat(src.row_count, 'rows'),
    tableMenuStat(cols.length, derived ? `columns + ${derived} derived` : 'columns'),
    tableMenuStat(src.tagged_row_count || 0, 'tagged'),
    tableMenuStat(src.note_count || 0, 'notes'));
  const fileLine = el('div', 'tm-file');
  if (!merge && src.path) fileLine.append(el('span', 'tm-path', src.path));
  if (src.imported_at) fileLine.append(el('span', null, (merge ? 'Made ' : 'Imported ') + src.imported_at.replace('T', ' ')));
  fileLine.title = src.path || '';
  container.append(stats, fileLine);
  const name = savingInput(merge ? src.name : (src.nickname || ''), merge ? 'Name this merge' : src.name, async (v) => {
    if (merge && !v) throw new Error('a merge needs a name');
    await post(`/api/source/${src.id}/nickname`, { nickname: v });
    await loadSources();
    const fresh = S.sources.find((s) => s.id === src.id);
    if (fresh) $('modalTitle').textContent = `Table — ${sourceLabel(fresh)}`;
  });
  container.append(tableMenuField(merge ? 'Name' : 'Nickname', name,
    el('small', null, merge ? 'On its tab and in the sidebar.' : 'Shown on its tab and in the sidebar in place of the file name. Blank goes back to the file name.')));


  if (!merge) {
    const names = cols.map((c) => c.name);
    const sig = headerSig(names);
    const alike = S.sources.filter((s) => !s.is_merge && !s.error
      && headerSig((s.columns || []).filter((c) => !c.derived).map((c) => c.name)) === sig);
    const setName = savingInput(nicknameFor(names) || '', `A name for these ${names.length} columns`, async (v) => {
      await saveHeaderNickname(names, v);
    });
    container.append(tableMenuField(`These ${names.length} columns`, setName,
      el('small', null, `${alike.length === 1 ? 'Only this table has' : `${alike.length} tables have`} them. The name groups them in the merge builder, Saved filters and the Timeline.`)));
  }

  const mergeBtn = el('button', 'btn ghost', merge ? 'Edit merge…' : 'Merge with other tables…');
  mergeBtn.title = merge
    ? 'Add or remove tables, or change which columns this merge keeps. Its layout stays.'
    : 'One table across this and others, matched by column name';
  mergeBtn.onclick = () => openMergeBuilder(merge ? { edit: src } : { preselect: [src.id] });
  // What the merge builder would offer, best fit first: the tables
  // sharing columns with this one, or a merge's own tables.
  const mergeHead = el('div', 'tm-merge-head');
  const mergeList = el('div', 'tm-merge-list');
  const mergeRow = (s, chipText, chipKind) => {
    const r = el('div', 'tm-merge-row');
    r.append(el('span', 'tm-merge-name', sourceLabel(s)), el('span', 'tm-merge-rows', `${(s.row_count || 0).toLocaleString()} rows`));
    if (chipText) r.append(el('span', 'mb-chip ' + chipKind, chipText));
    mergeList.append(r);
  };
  if (merge) {
    const members = (src.member_source_ids || []).map((mid) => S.sources.find((s) => s.id === mid)).filter(Boolean);
    mergeHead.append(el('span', null, `A merge of ${members.length} tables.`));
    for (const m of members) mergeRow(m, null);
  } else {
    const usedBy = mergesUsing(src);
    const mine = new Set(cols.map((c) => c.name));
    const fits = S.sources.filter((s) => s.id !== src.id && !s.is_merge && !s.error).map((s) => ({
      s, shared: (s.columns || []).filter((c) => !c.derived && mine.has(c.name)).length,
    })).filter((f) => f.shared > 0).sort((a, b) => b.shared - a.shared || sourceLabel(a.s).localeCompare(sourceLabel(b.s)));
    mergeHead.append(el('span', null, usedBy.length
      ? `In ${usedBy.map((m) => `“${sourceLabel(m)}”`).join(', ')}. Tables that share its columns:`
      : fits.length ? 'Not in a merge. Tables that share its columns, best fit first:'
        : 'Not in a merge, and no other table shares any of its columns.'));
    for (const f of fits) {
      const all = f.shared === mine.size;
      mergeRow(f.s, `${f.shared} of ${mine.size} columns`, all ? 'ok' : 'info');
    }
  }
  mergeHead.append(mergeBtn);
  container.append(tableMenuField('Merge', mergeHead, mergeList));
}

/* Along the foot of the table menu: the three things this menu is opened
   FOR most of the time, under whichever section is showing. They sat last,
   under the column list, until a two-hundred-column table put closing a
   tab a long scroll away. */
export function buildTableStripPanel(container) {
  const src = S.sources.find((s) => s.id === S.sourceId);
  const strip = el('div', 'table-menu-strip');
  const tables = el('button', 'btn ghost', 'Tables manager…');
  tables.title = 'Every table in the case — indexes, row counts, dropping a table';
  tables.onclick = () => openTablesManager();
  const reset = el('button', 'btn ghost', 'Reset view');
  reset.title = 'Back to the just-opened state: clear filters, search, tag filter and grouping, and restore the default sort. Column layout and the timeframe filter stay (Layout, taking the default for these columns, resets the layout).';
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

/* What the side list says under each section's name: its state at a
   glance, so the list answers "is anything set here?" without opening
   each one. */
function columnsHint() {
  const listed = S.order.filter((n) => !S.groupByCols.includes(n));
  const shown = listed.filter((n) => !(S.layout[n] || {}).hidden).length;
  const pinned = listed.filter((n) => isPinned(n)).length;
  return `${shown} of ${listed.length} shown` + (pinned ? ` · ${pinned} pinned` : '');
}

function valueFilterHint() {
  const mode = S.valueFilterMode === 'auto' ? `Auto, ${valueFilterAutoOn() ? 'on' : 'off'} here`
    : S.valueFilterMode === 'on' ? 'On for every column' : 'Off';
  const set = S.order.filter((n) => !S.groupByCols.includes(n) && (S.layout[n] || {}).valuePicker !== undefined).length;
  return mode + (set ? ` · ${set} set by hand` : '');
}

function nameHint() {
  const src = S.sources.find((s) => s.id === S.sourceId);
  if (!src) return '';
  if (src.is_merge) return `${(src.member_source_ids || []).length} tables`;
  const usedBy = mergesUsing(src);
  return (src.nickname ? 'Nicknamed' : 'No nickname') + (usedBy.length ? ' · in a merge' : '');
}

/* The table menu — everything that's about *this table* rather than the
   case or the app. Sections are a registry for the same reason the row
   menu's are: this is where per-table features are expected to land, and
   adding one should be adding an entry. `build` gets (container, refresh);
   `hint` says what the side list shows under its name, and a section with
   `asyncHint` fills that in once it has asked the server.

   It lives behind the openTableMenu keybind, the Tables manager's
   Settings… and Columns… on a table's right-click menu (tableMenuItems in
   sources.js) rather than a visible ▦ button, which is the icon this
   replaced — a menu that's going to keep growing needs a home that doesn't
   cost tab-strip width per entry. The right-click opened this dialog
   directly until it got a short menu of its own: a full-height modal was a
   lot to put up for closing or renaming a tab. */
export const TABLE_MENU_SECTIONS = [
  { id: 'columns', group: 'Columns', title: 'Show, hide & order', hint: columnsHint, build: buildColumnsPanel },
  { id: 'valueFilters', group: 'Columns', title: 'Value dropdowns', hint: valueFilterHint, build: buildValueFilterPanel },
  { id: 'layout', group: 'This table', title: 'Layout', build: buildLayoutPanel,
    asyncHint: async () => ((await findDefaultLayout()) ? 'Default saved' : 'No default saved') },
  { id: 'name', group: 'This table', title: 'Name & merge', hint: nameHint, build: buildNamePanel },
];

/* `sourceId` is optional — the right-click menu's Columns… and the Tables
   manager pass the table they were opened on, which may not be the one on
   screen. The panels all
   read S.layout/S.order/S.columns (this table's live state), so opening
   that source first isn't a convenience, it's the precondition. `section`
   picks the pane it opens on; Columns otherwise, since that is what `C`
   and Columns… are pressed for.

   Two panes, the shape of Settings and the side-list dialogs: the sections
   down the left, the chosen one on the right, in a frame of a FIXED height
   so going from the column list to a pane of three rows moves nothing.
   Choosing a section builds its pane fresh (they read live state that
   another dialog opened from here, like the merge builder, may change),
   and the everyday actions sit along the foot under every one of them. */
export async function openTableMenu(sourceId, section = 'columns') {
  markModalAction('openTableMenu');
  const id = sourceId === undefined ? S.sourceId : sourceId;
  if (id == null) { toast('Open a table first'); return; }
  if (id !== S.sourceId || S.activeTab !== 'grid') await openSource(id);
  if (S.sourceId !== id) return; // openSource no-ops on an id it can't find
  const src = S.sources.find((x) => x.id === id);
  modal(`Table — ${src ? sourceLabel(src) : ''}`, (b) => {
    const frame = sectionPanes({
      sections: TABLE_MENU_SECTIONS, label: 'Table menu sections',
      cls: 'table-menu', paneCls: 'table-menu-pane', changedEvent: 'tablemenu:changed',
    });
    b.append(frame.panes);
    const foot = el('div', 'table-menu-foot');
    buildTableStripPanel(foot);
    b.append(foot);
    frame.show(section);
    // The side list, like Settings: arrow keys move between sections from
    // the start, and C still closes the menu it opened (a caret in the
    // filter box would take the C as typing).
  }, { wide: true, tall: true, focus: (b) => b.querySelector('.side-rail-item[aria-selected="true"]') });
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

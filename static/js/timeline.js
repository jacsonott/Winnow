/* The unified timeline tab.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, OVERSCAN, PAGE, ROW_H, api, debounce, el, post, setBusy, toast } from './core.js';
import { rowsPaintY, spacerPx, vScroll } from './grid.js';
import { armOpCancel, opToken } from './jobs.js';
import { headerSig, nicknameFor } from './savedfilters.js';
import { loadSources, openSource, recenterOnRow, sourceLabel } from './sources.js';
import { showGridTab } from './sql.js';
import { S } from './state.js';
import { confirmDialog, modal, oneAtATime } from './ui.js';

/* ---------------------------------------------------------- unified timeline */

/* A semi-Plaso-style merged timeline of every *tagged* row across every
   real table in the case, regardless of which tab (if any) is currently
   open for it — a case-wide view of "everything I've flagged as a
   finding," not "everything in the table I happen to have open." Server
   side does the real work (build_timeline unions each source's tagged
   rows, using workspace.timeline_templates — see loadTimelineTemplates
   below and openTimelineSourceConfig — to pick that source's timestamp
   column, body columns, and a human "source type" label, and
   defaults/headers.json to summarise the row for its artefact shape);
   this is just the tab UI plus a small virtualized list, same
   translateY-window technique as the main grid's render(), simplified
   since a row here is always exactly three fixed fields (ts/type/body),
   never per-column. */

export async function loadTimelineTemplates() {
  try { S.timelineTemplates = await api('/api/timeline_templates'); } catch { S.timelineTemplates = []; }
}

export function timelineTemplateFor(colNames) {
  const sig = headerSig(colNames);
  return S.timelineTemplates.find((t) => headerSig(t.col_names) === sig) || null;
}

/* The tag filter, as the grid's own tag chips (.tag-chip, renderTagRibbon
   in tags.js) rather than as a row of checkboxes. It asks the same
   question the ribbon asks — which tags am I looking at — in the same
   place on the screen, and it was the one tag control in the app that
   answered it with a form instead: a checkbox and a bare swatch, with
   none of the pressed-state, hover or per-skin treatment every other tag
   control has.

   Two things the ribbon's chips carry are deliberately NOT here. The
   COUNT, because the ribbon's is scoped to the open table's view while
   this list is case-wide — the open table's number under a case-wide chip
   would be a wrong answer rather than a missing one. The HOTKEY, because
   the digits tag the grid's selection and there is nothing to tag on this
   tab, so printing the key would promise a keystroke that does nothing
   here.

   Pressed means "on the timeline", and every tag starts pressed: a
   timeline is every tagged row until it is narrowed. That includes a tag
   created after the case was opened, which is why what is remembered is
   the tags switched OFF (S.timeline.tagsOff) rather than the ones on: a
   list of the tags that were on, snapshotted when the case opened, left
   every later tag off — its rows missing from the timeline, with nothing
   to say why, until its chip was clicked or the case reopened. */
export function timelineTagIds() {
  return S.tags.filter((t) => !S.timeline.tagsOff.has(t.id)).map((t) => t.id);
}

export function renderTimelineTagFilter() {
  const wrap = $('timelineTagFilter');
  wrap.replaceChildren();
  for (const t of S.tags) {
    const on = !S.timeline.tagsOff.has(t.id);
    const chip = el('button', 'tag-chip');
    chip.setAttribute('aria-pressed', String(on));
    // Inline, over the stylesheet's pressed colour, exactly as the ribbon
    // does it — the border follows through currentColor.
    chip.style.color = on ? t.color : '';
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    chip.append(sw, el('span', null, t.name));
    chip.title = on
      ? `On the timeline. Click to take ${t.name} off it.`
      : `Off the timeline. Click to put ${t.name} back on it.`;
    chip.onclick = () => {
      if (on) S.timeline.tagsOff.add(t.id); else S.timeline.tagsOff.delete(t.id);
      // Repainted here, not by the DOM: a chip's state is an attribute
      // this function writes, where a checkbox kept its own.
      renderTimelineTagFilter();
      buildTimeline();
    };
    wrap.append(chip);
  }
}

export async function buildTimeline() {
  const tagIds = timelineTagIds();
  S.timeline.pages.clear();
  S.timeline.pending.clear();
  // Which rows are showing their raw text is per-build state: the rows a
  // rebuild brings back are a different set, and a Set that outlived every
  // build would grow for the life of the tab.
  S.timeline.raw.clear();
  const reqId = ++S.timeline.reqId;
  if (!tagIds.length) {
    // Every tag unchecked -> nothing can match; skip the round trip and
    // show the empty state directly rather than asking the server for
    // "no tag filter," which would mean the opposite (every tagged row).
    S.timeline.view = { view_id: null, row_count: 0 };
    renderTimelineRows();
    return;
  }
  setBusy(true);
  let v;
  try {
    const token = opToken();
    const disarmCancel = armOpCancel(token);
    try {
      v = await post('/api/timeline', { tag_ids: tagIds, op_token: token });
    } finally {
      disarmCancel();
    }
  } catch (e) {
    if (e.status === 499) { toast('Timeline build cancelled', 2500); return; }
    toast('Could not build timeline: ' + e.message, 6000);
    return;
  } finally {
    setBusy(false);
  }
  if (reqId !== S.timeline.reqId) return; // a newer build superseded this one
  S.timeline.view = v;
  $('timelineSpacerY').style.height = spacerPx(v.row_count) + 'px';
  $('timelineStats').innerHTML = `<b>${v.row_count.toLocaleString()}</b> tagged row${v.row_count === 1 ? '' : 's'}`;
  // A table too wide to join into one body keeps its first N columns
  // (Store._fit_body_cols). Said here, so the columns past N aren't taken
  // for columns that were empty.
  const trimmed = v.body_trimmed || [];
  if (trimmed.length) {
    const label = (t) => sourceLabel(S.sources.find((s) => s.id === t.source_id)) || t.source_name;
    const note = el('span', null, trimmed.length === 1
      ? ` · ${label(trimmed[0])}: body shows ${trimmed[0].shown} of ${trimmed[0].total} columns`
      : ` · ${trimmed.length} tables: body shows their first ${trimmed[0].shown} columns`);
    note.title = trimmed.map((t) => `${label(t)}: first ${t.shown} of ${t.total} columns`).join('\n')
      + '\nPick body columns in Configure tables… to choose which ones.';
    $('timelineStats').append(note);
  }
  $('timelineBody').scrollTop = 0;
  renderTimelineRows();
}

export async function ensureTimelinePage(idx) {
  if (S.timeline.pages.has(idx) || S.timeline.pending.has(idx) || !S.timeline.view || !S.timeline.view.view_id) return;
  S.timeline.pending.add(idx);
  const vid = S.timeline.view.view_id;
  try {
    const data = await api(`/api/timeline_rows?view_id=${vid}&start=${idx * PAGE}&count=${PAGE}`);
    if (!S.timeline.view || S.timeline.view.view_id !== vid) return;
    S.timeline.pages.set(idx, data.rows);
    renderTimelineRows();
  } catch { /* view expired mid-scroll — next buildTimeline() call will recover it */ } finally {
    S.timeline.pending.delete(idx);
  }
}

export function timelineRowAt(pos) {
  const page = S.timeline.pages.get(Math.floor(pos / PAGE));
  return page ? page[pos % PAGE] : undefined;
}

/* Why the timeline is empty, which is not always "nothing is tagged".
   The chips make switching a tag off a single click, so the two narrowed
   cases are now easy to reach — and "tag some rows in any table" over a
   case full of findings sends the analyst looking for work they have
   already done. Said in terms of the control above, since that is where
   the answer is. */
function emptyReason() {
  const on = timelineTagIds().length;
  if (S.tags.length && !on) return 'Every tag is switched off — turn one back on above to see its rows.';
  if (on && on < S.tags.length) return 'No rows carry the tags switched on above.';
  return 'No tagged rows yet — tag some rows in any table, then come back here.';
}

export function renderTimelineRows() {
  const view = S.timeline.view;
  const total = view ? view.row_count : 0;
  const empty = $('timelineEmpty');
  empty.hidden = total > 0;
  if (!total) empty.textContent = emptyReason();
  const body = $('timelineBody');
  const rowsEl = $('timelineRows');
  if (!total) { rowsEl.replaceChildren(); return; }

  // head = 0: the timeline's header sits outside #timelineBody, unlike the
  // grid's sticky one.
  const virt = vScroll(body, total);
  const first = Math.max(0, Math.floor(virt / ROW_H) - OVERSCAN);
  const visible = Math.ceil(body.clientHeight / ROW_H) + OVERSCAN * 2;
  const last = Math.min(total, first + visible);
  for (let p = Math.floor(first / PAGE); p <= Math.floor(Math.max(first, last - 1) / PAGE); p++) ensureTimelinePage(p);

  const tagColor = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
  rowsEl.style.transform = `translateY(${rowsPaintY(body, virt, first)}px)`;
  const frag = document.createDocumentFragment();
  for (let pos = first; pos < last; pos++) {
    const r = timelineRowAt(pos);
    const row = timelineRowNode(r, tagColor, fillTimelineBody);
    if (r) row.onclick = () => jumpToTimelineRow(r.source_id, r.rid);
    frag.append(row);
  }
  rowsEl.replaceChildren(frag);
}

/* One Timeline line — timestamp, tag dots and type badge, body — or the
   empty placeholder for a row whose page hasn't arrived. `fillBody`
   paints the Body cell: fillTimelineBody on the Timeline, with its raw /
   summary toggle; Configure tables…' preview passes one without it. */
export function timelineRowNode(r, tagColor, fillBody) {
  const row = el('div', 'timeline-row' + (r ? '' : ' pending'));
  const tsCell = el('div', 'tl-col-ts', r ? (r.ts || '—') : '');
  const typeCell = el('div', 'tl-col-type');
  if (r) {
    for (const tid of r.tags) {
      const dot = el('span', 'tl-tag-dot');
      dot.style.background = tagColor[tid] || '#888';
      dot.title = (S.tags.find((t) => t.id === tid) || {}).name || '';
      typeCell.append(dot);
    }
    const badge = el('span', 'type-badge', r.type_label);
    badge.title = r.source_name;
    typeCell.append(badge);
  }
  const bodyCell = el('div', 'tl-col-body');
  if (r) fillBody(bodyCell, r);
  row.append(tsCell, typeCell, bodyCell);
  return row;
}

/* The Body cell. The server sends both halves of the row (see
   build_timeline): `lead`/`detail` are the summary for this artefact
   shape — "Special privileges assigned  svc_backup · WKSTN-4471 · id
   4672" — and `body` is the whole source row pipe-joined, which is all
   there is for a CSV no shipped shape describes.

   The raw row is never more than a gesture away: it's the cell's title
   on every row, "raw" swaps the cell's text for it, and clicking the row
   still opens that row in its own table. What "raw" deliberately does
   NOT do is expand the row — every row in this list is exactly ROW_H
   tall and the virtualized window's arithmetic (first, visible,
   translateY) is built on that, so a row that grew would paint over its
   neighbour and put every row below it at the wrong offset. */
export function fillTimelineBody(cell, r) {
  const summarised = !!(r.lead || r.detail);
  const key = `${r.source_id}:${r.rid}`;
  const showRaw = !summarised || S.timeline.raw.has(key);
  paintTimelineBody(cell, r, showRaw);
  if (!summarised) return; // nothing to swap to — the body already IS the raw row
  const btn = el('button', 'tl-raw-toggle', showRaw ? 'summary' : 'raw');
  btn.title = showRaw ? 'Show the summary' : 'Show the raw row';
  btn.setAttribute('aria-pressed', String(showRaw));
  btn.onclick = (e) => {
    e.stopPropagation(); // clicking the row itself jumps to it in its table
    if (showRaw) S.timeline.raw.delete(key); else S.timeline.raw.add(key);
    renderTimelineRows();
  };
  cell.append(btn);
}

/* The text half of the Body cell: the summary's lead and details, or the
   raw row. Shared with Configure tables…' preview, so a previewed row is
   painted by the same code as a row on the Timeline. */
export function paintTimelineBody(cell, r, showRaw) {
  cell.title = r.body;
  if (showRaw) {
    cell.append(el('span', 'tl-raw', r.body));
  } else {
    if (r.lead) cell.append(el('span', 'tl-lead', r.lead));
    if (r.detail) cell.append(el('span', 'tl-detail', r.detail));
  }
}

/* Open the row's own table at the row — from a timeline line, and from
   every hit in the watchlist's two panes.

   A CLOSED table needs marking open first. openSource() switches the grid
   to it, but "open" is server-side state (sources.is_open) and nothing
   here was writing it: the analyst landed in a table with no tab, absent
   from the sidebar's Open section, and still listed under the closed ones
   — nothing on screen to come back to, and nothing to close. The two
   steps are the ones search-all's "Open ↦" already takes: POST the
   source open, then reload the listing so the tab strip and sidebar are
   built from the new answer. loadSources() opens the table itself (it is
   in the open tabs by then), so this is not an extra switch.

   Every caller is a bare click handler, so a failure is caught here or
   nowhere — and it can fail for a reason nobody did wrong: the hit was
   listed before its table was removed (another window, or a moment ago
   in this one). That answers 404; the listing is reloaded so the grid
   lands on a table that exists rather than the half-opened ghost. */
export async function jumpToTimelineRow(sourceId, rid) {
  try {
    const src = S.sources.find((s) => s.id === sourceId);
    if (src && !src.is_open) {
      // A failure here is not a reason to refuse the navigation — the grid
      // can still show the row, which is what was asked for.
      try { await post(`/api/source/${sourceId}/open`, { open: true }); } catch { /* best effort */ }
      await loadSources(sourceId);
    } else {
      await openSource(sourceId);
    }
    showGridTab();
    await recenterOnRow({ source_id: sourceId, rid });
  } catch (e) {
    if (e && e.aborted) return;
    if (e && e.status === 404) {
      toast('That row\'s table is no longer in this case', 6000);
      await loadSources().catch(() => {});
      return;
    }
    toast('Could not open that row: ' + ((e && e.message) || e), 6000);
  }
}

// Guarded like #body's own scroll handler below: scroll can fire several
// times per frame, and an unguarded rAF per event runs that many full
// repaints in the same frame.
export let timelineScrollRaf = null;

/* ------------------------------------------------------ Configure tables… */

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const listNames = (arr) => (arr.length <= 1 ? (arr[0] || '')
  : `${arr.slice(0, -1).join(', ')} and ${arr[arr.length - 1]}`);

/* The case's real tables grouped by header set: their own columns, derived
   ones left out — the key Save writes under and server.py's
   _resolve_timeline_configs looks up under (the two disagreeing is how
   adding a derived column once dropped a table's config without a word).
   One template serves every table in a group, so the dialog lists groups:
   two EvtxECmd tables are one entry, and saving it changes both — which
   the one-block-per-table dialog this replaced did too, without showing
   it. A table that failed, or is still importing (no columns yet), has no
   header set to list. */
export function timelineHeaderSets() {
  const bySig = new Map();
  for (const s of S.sources) {
    if (s.is_merge || s.error || !s.columns) continue;
    const cols = s.columns.filter((c) => !c.derived).map((c) => c.name);
    if (!cols.length) continue;
    const sig = headerSig(cols);
    if (!bySig.has(sig)) bySig.set(sig, { sig, cols, tables: [] });
    bySig.get(sig).tables.push(s);
  }
  return [...bySig.values()];
}

/* The template the server applies to a header set, found the way
   _resolve_timeline_configs finds it: the one saved under the set's own
   columns, else one an older release saved under a table's every column,
   derived ones included. */
export function headerSetTemplate(set) {
  return timelineTemplateFor(set.cols)
    || set.tables.map((t) => timelineTemplateFor(t.columns.map((c) => c.name))).find(Boolean)
    || null;
}

/* The three fields the dialog edits, as a template holds them — or as the
   shipped default has them: nothing set. A saved timestamp the tables
   here don't have is kept, not blanked: the template is shared across
   cases, and a derived column another case has is no reason to lose it. */
function timelineDraft(set, tmpl) {
  return {
    type: (tmpl && tmpl.type_label) || '',
    ts: (tmpl && tmpl.timestamp_column) || '',
    body: ((tmpl && tmpl.body_columns) || []).filter((c) => set.cols.includes(c)),
  };
}

const sameDraft = (a, b) => a.type.trim() === b.type.trim() && a.ts === b.ts
  && a.body.join('\x1f') === b.body.join('\x1f');

/* The Timestamp column choices for a set: every datetime column any of its
   tables has, its own first and then derived ones (a derived datetime
   column can be the timestamp — build_timeline resolves the name against
   every column), each saying so when it isn't on every table, since a
   table without it falls back to its first datetime column. */
function timestampChoices(set) {
  const by = new Map();
  for (const t of set.tables) {
    for (const c of t.columns) {
      if (c.type !== 'datetime') continue;
      if (!by.has(c.name)) by.set(c.name, { name: c.name, derived: !!c.derived, tables: [] });
      by.get(c.name).tables.push(t);
    }
  }
  return [...by.values()].sort((a, b) => Number(a.derived) - Number(b.derived)).map((c) => {
    const notes = [];
    if (c.derived) notes.push('derived');
    if (c.tables.length < set.tables.length) notes.push(`only on ${listNames(c.tables.map(sourceLabel))}`);
    return { value: c.name, label: notes.length ? `${c.name} (${notes.join(', ')})` : c.name };
  });
}

/* What "no timestamp picked" means for this set, named: build_timeline's
   fallback is each table's first datetime column. */
function defaultTimestampLabel(set) {
  const firsts = new Set(set.tables.map((t) => (t.columns.find((c) => c.type === 'datetime') || {}).name || null));
  if (firsts.size > 1) return 'Each table\'s first datetime column';
  const [only] = firsts;
  return only ? `First datetime column (${only})` : 'None (no datetime column, so its rows go last)';
}

function addOption(sel, value, label) {
  const opt = document.createElement('option');
  opt.value = value;
  opt.textContent = label;
  sel.append(opt);
}

/* Configure tables… — how each kind of table reads on the Timeline: its
   type badge, which column is its timestamp, which columns make its Body.
   Saved to workspace.timeline_templates, keyed by header set and reused
   by every case whose tables have the same columns, so the dialog is
   built around header sets rather than tables: the case's sets on the
   left (with the tables each one covers, and whether it has a saved
   setting or the shipped default), the selected one on the right with a
   live preview of real rows from one of its tables — rendered by the
   server's own timeline code (/api/timeline/preview), so what it shows is
   what the Timeline will show.

   Edits are kept per header set until that set is saved: switching to
   another set and back finds them still there, the list marks each set
   with unsaved changes, and the footer names the others when the one
   shown is saved. Save writes the set shown, nothing else. Every real
   table is listed whether or not it has tagged rows yet — configuring
   ahead of tagging is the normal workflow, not an edge case. */
export function openTimelineSourceConfig() {
  const sets = timelineHeaderSets();
  const state = {
    sel: null,               // sig of the set on the right
    drafts: new Map(),       // sig -> its edits, kept until it is saved
    previewTable: new Map(), // sig -> id of the table its preview reads
    setFilter: '',
    colFilter: '',
    preview: null,
    previewKey: '',
    previewError: '',
    seq: 0,
    busy: false,             // a Save or Back to default in flight
  };
  const open = sets.find((g) => g.tables.some((t) => t.id === S.sourceId));
  state.sel = (open || sets[0] || {}).sig || null;

  const setOf = (sig) => sets.find((g) => g.sig === sig) || null;
  const saved = (g) => timelineDraft(g, headerSetTemplate(g));
  const draftOf = (g) => {
    if (!state.drafts.has(g.sig)) state.drafts.set(g.sig, saved(g));
    return state.drafts.get(g.sig);
  };
  const dirty = (g) => state.drafts.has(g.sig) && !sameDraft(state.drafts.get(g.sig), saved(g));
  const setName = (g) => {
    const t = headerSetTemplate(g);
    return (t && t.type_label) || nicknameFor(g.cols) || sourceLabel(g.tables[0]);
  };
  const previewTableOf = (g) => {
    if (!state.previewTable.has(g.sig)) {
      state.previewTable.set(g.sig, (g.tables.find((t) => t.tagged_row_count > 0) || g.tables[0]).id);
    }
    return state.previewTable.get(g.sig);
  };
  const previewRequest = (g) => {
    const d = draftOf(g);
    return { source_id: previewTableOf(g), timestamp_column: d.ts || null, body_columns: d.body, type_label: d.type.trim() };
  };

  let listEl, paneEl, chipsEl, clearBtn, bodyHelpEl, previewEl, captionEl, hintEl, saveBtn, resetBtn;

  const fetchPreview = debounce(async () => {
    const g = setOf(state.sel);
    if (!g) return;
    const req = previewRequest(g);
    const seq = ++state.seq;
    try {
      const pv = await post('/api/timeline/preview', req);
      if (seq !== state.seq) return;
      state.preview = pv;
      state.previewKey = JSON.stringify(req);
      state.previewError = '';
    } catch (e) {
      if (seq !== state.seq) return;
      state.preview = null;
      state.previewError = e.message;
    }
    renderPreview();
    renderBodyHelp();
  }, 120);

  modal('Configure timeline tables', (b) => {
    b.append(el('p', 'tlc-intro',
      'How each kind of table reads on the Timeline — set once per header set, reused by every case with the same columns.'));
    if (!sets.length) {
      b.append(el('p', 'tlc-empty', 'No tables in this case yet.'));
      return;
    }
    const root = el('div', 'tlc');
    const panes = el('div', 'tlc-panes');

    const left = el('div', 'tlc-pane');
    left.append(el('div', 'tlc-head', 'Header sets in this case'));
    if (sets.length > 6) {
      const filter = el('input', 'tlc-filter');
      filter.type = 'search';
      filter.placeholder = 'Filter header sets…';
      filter.setAttribute('aria-label', 'Filter header sets');
      filter.oninput = () => { state.setFilter = filter.value; renderList(); };
      left.append(filter);
    }
    listEl = el('div', 'tlc-sets');
    left.append(listEl);

    paneEl = el('div', 'tlc-pane');
    panes.append(left, paneEl);

    const foot = el('div', 'tlc-foot');
    hintEl = el('div', 'tlc-hint');
    resetBtn = el('button', 'btn ghost', 'Back to default');
    resetBtn.type = 'button';
    resetBtn.title = 'Remove the saved setting for this header set, so its tables read the way Winnow does by default';
    saveBtn = el('button', 'btn primary', 'Save');
    saveBtn.type = 'button';
    // One request per click (oneAtATime), and the buttons' enabled state
    // put back from the dialog's own state once it settles — oneAtATime
    // restores what it found, which was "enabled" when the click landed.
    // A click it drops returns at once, so renderFoot also has to know a
    // request is still in flight (state.busy) or it re-enables Save early.
    const busy = (fn) => async () => {
      state.busy = true;
      try { await fn(); } finally { state.busy = false; }
    };
    const guardedSave = oneAtATime(saveBtn, busy(save));
    const guardedReset = oneAtATime(resetBtn, busy(resetToDefault));
    saveBtn.onclick = async (e) => { await guardedSave(e); renderFoot(); };
    resetBtn.onclick = async (e) => { await guardedReset(e); renderFoot(); };
    foot.append(hintEl, resetBtn, saveBtn);

    root.append(panes, foot);
    b.append(root);
    renderList();
    renderPane();
  }, { wide: 'x' });

  function renderList() {
    listEl.replaceChildren();
    const q = state.setFilter.trim().toLowerCase();
    for (const g of sets) {
      const name = setName(g);
      const labels = g.tables.map(sourceLabel);
      if (q && !name.toLowerCase().includes(q) && !labels.some((l) => l.toLowerCase().includes(q))) continue;
      const btn = el('button', 'tlc-set');
      btn.type = 'button';
      btn.setAttribute('aria-pressed', String(g.sig === state.sel));
      btn.title = `${plural(g.cols.length, 'column')}: ${g.cols.join(', ')}`;
      const marks = el('span', 'tlc-marks');
      if (dirty(g)) marks.append(el('span', 'tlc-mark unsaved', 'unsaved'));
      marks.append(savedMark(g));
      const tables = el('span', 'tlc-set-tables',
        (labels.length > 1 ? `${plural(labels.length, 'table')} · ` : '') + labels.join(' · '));
      btn.append(el('span', 'tlc-set-name', name), marks, tables);
      btn.onclick = () => {
        if (state.sel === g.sig) return;
        state.sel = g.sig;
        state.colFilter = '';
        renderList();
        renderPane();
      };
      listEl.append(btn);
    }
    if (!listEl.childElementCount) listEl.append(el('p', 'tlc-empty', 'No header set matches that filter.'));
  }

  function savedMark(g) {
    const has = !!headerSetTemplate(g);
    const mark = el('span', `tlc-mark ${has ? 'saved' : 'default'}`, has ? 'saved' : 'default');
    mark.title = has ? 'Has a saved setting' : 'Reads the way Winnow does by default — nothing saved';
    return mark;
  }

  function renderPane() {
    paneEl.replaceChildren();
    const g = setOf(state.sel);
    if (!g) return;
    const d = draftOf(g);

    const title = el('div', 'tlc-title');
    title.append(el('b', null, setName(g)), savedMark(g));
    const scope = el('p', 'tlc-scope');
    scope.append('Applies to ', el('b', null, listNames(g.tables.map(sourceLabel))),
      ` — and to any table with these ${plural(g.cols.length, 'column')}, in this case or another.`);

    const pair = el('div', 'dlg-pair');
    const typeField = el('div', 'dlg-field');
    const typeLabel = el('label', null, 'Type');
    const typeInput = el('input', 'confirm-input');
    typeInput.type = 'text';
    typeInput.id = 'tlcType';
    typeInput.autocomplete = 'off';
    typeLabel.htmlFor = 'tlcType';
    typeInput.value = d.type;
    typeInput.placeholder = g.tables.length > 1 ? 'Each table\'s file name' : g.tables[0].name;
    typeInput.oninput = () => { d.type = typeInput.value; edited(); };
    typeInput.onkeydown = (e) => { if (e.key === 'Enter' && !saveBtn.disabled) saveBtn.click(); };
    typeField.append(typeLabel, typeInput, el('small', null, 'The badge on each line. Blank: the table\'s file name.'));

    const tsField = el('div', 'dlg-field');
    const tsLabel = el('label', null, 'Timestamp column');
    const tsSel = el('select');
    tsSel.id = 'tlcTs';
    tsLabel.htmlFor = 'tlcTs';
    const choices = timestampChoices(g);
    addOption(tsSel, '', defaultTimestampLabel(g));
    for (const c of choices) addOption(tsSel, c.value, c.label);
    if (d.ts && !choices.some((c) => c.value === d.ts)) addOption(tsSel, d.ts, `${d.ts} (not a datetime column here)`);
    tsSel.value = d.ts;
    tsSel.onchange = () => { d.ts = tsSel.value; edited(); };
    tsField.append(tsLabel, tsSel);
    pair.append(typeField, tsField);

    const bodyField = el('div', 'dlg-field');
    const bodyHead = el('div', 'tlc-body-head');
    bodyHead.append(el('span', 'tlc-label', 'Body columns'));
    clearBtn = el('button', 'btn ghost tlc-small', 'Clear');
    clearBtn.type = 'button';
    clearBtn.title = 'Pick none: the Body goes back to what Winnow shows by default';
    clearBtn.onclick = () => { d.body = []; renderChips(); edited(); };
    bodyHead.append(clearBtn);
    bodyField.append(bodyHead);
    if (g.cols.length > 24) {
      const filter = el('input', 'tlc-filter');
      filter.type = 'search';
      filter.placeholder = 'Filter columns…';
      filter.setAttribute('aria-label', 'Filter columns');
      filter.oninput = () => { state.colFilter = filter.value; renderChips(); };
      bodyField.append(filter);
    }
    chipsEl = el('div', 'tlc-chips');
    chipsEl.setAttribute('role', 'group');
    chipsEl.setAttribute('aria-label', 'Body columns');
    bodyHelpEl = el('small');
    bodyField.append(chipsEl, bodyHelpEl);

    const prev = el('div', 'tlc-prev');
    const prevHead = el('div', 'tlc-prev-head');
    prevHead.append(el('div', 'tlc-head', 'Preview'));
    if (g.tables.length > 1) {
      const from = el('label', 'tlc-prev-from');
      const tSel = el('select');
      for (const t of g.tables) addOption(tSel, String(t.id), sourceLabel(t));
      tSel.value = String(previewTableOf(g));
      tSel.onchange = () => { state.previewTable.set(g.sig, Number(tSel.value)); fetchPreview(); };
      from.append('Rows from ', tSel);
      prevHead.append(from);
    }
    previewEl = el('div', 'tlc-preview');
    captionEl = el('div', 'tlc-caption');
    prev.append(prevHead, previewEl, captionEl);

    paneEl.append(title, scope, pair, bodyField, prev);
    // Drop whatever the previous set's preview was, including an answer
    // still in flight for it.
    state.seq++;
    state.preview = null;
    state.previewError = '';
    renderChips();
    renderBodyHelp();
    renderPreview();
    renderFoot();
    fetchPreview();
  }

  function renderChips(focusCol) {
    const g = setOf(state.sel);
    const d = draftOf(g);
    chipsEl.replaceChildren();
    const q = state.colFilter.trim().toLowerCase();
    let focus = null;
    for (const c of g.cols) {
      const at = d.body.indexOf(c);
      if (q && at < 0 && !c.toLowerCase().includes(q)) continue;
      const chip = el('button', 'tlc-chip');
      chip.type = 'button';
      chip.setAttribute('aria-pressed', String(at >= 0));
      if (at >= 0) chip.append(el('b', 'tlc-ord', String(at + 1)));
      chip.append(c);
      chip.title = at >= 0
        ? `Number ${at + 1} in the Body. Click to take it out.`
        : 'Click to add it to the Body, after the ones already picked.';
      chip.onclick = () => {
        d.body = at >= 0 ? d.body.filter((x) => x !== c) : [...d.body, c];
        renderChips(c);
        edited();
      };
      if (c === focusCol) focus = chip;
      chipsEl.append(chip);
    }
    if (!chipsEl.childElementCount) chipsEl.append(el('span', 'tlc-empty', 'No column matches that filter.'));
    clearBtn.hidden = !d.body.length;
    // Rebuilt on every click, so the keyboard stays on the chip it pressed.
    if (focus) focus.focus();
  }

  /* What the Body is made of, in words: the picks, or what the Timeline
     falls back to with none — the shipped summary for a shape Winnow
     knows, else every column. Read off the preview's answer for exactly
     these settings when it has one; until then from the shape alone. */
  function renderBodyHelp() {
    const g = setOf(state.sel);
    const d = draftOf(g);
    const pv = state.preview;
    const current = pv && state.previewKey === JSON.stringify(previewRequest(g));
    const shape = pv ? pv.shape : null;
    let text;
    if (d.body.length) {
      text = 'Joined with " | " in the order picked. Click one again to take it out.'
        + (shape ? ` Picking replaces the summary Winnow ships for ${shape}.` : '');
    } else if (!pv) {
      text = 'None picked.';
    } else if (current ? pv.body === 'summary' : !!shape) {
      text = `None picked, so the Body is the summary Winnow ships for ${current ? pv.summary_shape : shape}.`;
    } else {
      text = 'None picked, so the Body is every column, joined with " | ".';
    }
    bodyHelpEl.textContent = text;
  }

  function renderPreview() {
    previewEl.replaceChildren();
    captionEl.textContent = '';
    if (state.previewError) {
      previewEl.append(el('p', 'tlc-empty', `Couldn't preview this table: ${state.previewError}`));
      return;
    }
    const pv = state.preview;
    if (!pv) { previewEl.append(el('p', 'tlc-empty', 'Loading…')); return; }
    const head = el('div', 'timeline-cols');
    head.append(el('div', 'tl-col-ts', 'Timestamp'), el('div', 'tl-col-type', 'Type'), el('div', 'tl-col-body', 'Body'));
    previewEl.append(head);
    if (!pv.rows.length) { previewEl.append(el('p', 'tlc-empty', 'This table has no rows.')); return; }
    const tagColor = Object.fromEntries(S.tags.map((t) => [t.id, t.color]));
    // No raw/summary toggle here: the toggle is the Timeline's own state,
    // and these lines are a sample of it, not lines on it.
    const paintBody = (cell, r) => paintTimelineBody(cell, r, !(r.lead || r.detail));
    const rows = el('div', 'tlc-prev-rows');
    for (const r of pv.rows) rows.append(timelineRowNode(r, tagColor, paintBody));
    previewEl.append(rows);

    const label = sourceLabel(S.sources.find((s) => s.id === pv.source_id)) || pv.source_name;
    const tagged = pv.rows.filter((r) => r.tagged).length;
    const rest = pv.rows.length - tagged;
    let caption;
    if (!tagged) caption = `${label} has no tagged rows yet, so these are its first rows. A row is on the Timeline once it is tagged.`;
    else if (!rest) caption = `${plural(tagged, 'tagged row')} from ${label}.`;
    else caption = `${plural(tagged, 'tagged row')} from ${label}, plus ${rest} untagged (no tag dot) to fill the preview.`;
    if (pv.body_shown < pv.body_total) caption += ` The Body shows its first ${pv.body_shown} of ${pv.body_total} columns.`;
    captionEl.textContent = caption;
  }

  function renderFoot() {
    const g = setOf(state.sel);
    saveBtn.disabled = state.busy || !g || !dirty(g);
    saveBtn.title = saveBtn.disabled ? 'Nothing changed for this header set' : `Save the settings for ${setName(g)}`;
    resetBtn.hidden = !g || !headerSetTemplate(g);
    const others = sets.filter((o) => o !== g && dirty(o)).map(setName);
    hintEl.textContent = others.length
      ? `Not saved yet: ${listNames(others)}. Save writes the header set shown.`
      : '';
  }

  function edited() {
    renderList();
    renderFoot();
    renderBodyHelp();
    fetchPreview();
  }

  // Rebuilt behind the dialog, so what was just saved is what the
  // Timeline shows on closing it; elsewhere the next visit rebuilds it.
  function refreshTimeline() {
    if (S.activeTab === 'timeline') buildTimeline();
  }

  async function save() {
    const g = setOf(state.sel);
    if (!g || !dirty(g)) return;
    const d = draftOf(g);
    const name = setName(g);
    try {
      await post('/api/timeline_templates', {
        col_names: g.cols,
        // Blank stays blank: build_timeline gives each table its own file
        // name. Writing the first table's name here would put it on every
        // other table's lines too, the template being shared.
        type_label: d.type.trim(),
        timestamp_column: d.ts || null,
        body_columns: d.body,
      });
    } catch (e) {
      toast(`Could not save the Timeline settings for ${name}: ${e.message}`, 6000);
      return;
    }
    await loadTimelineTemplates();
    state.drafts.delete(g.sig);
    toast(`Saved the Timeline settings for ${setName(g)}`);
    refreshTimeline();
    renderList();
    if (state.sel === g.sig) renderPane();
  }

  async function resetToDefault() {
    const g = setOf(state.sel);
    const tmpl = g && headerSetTemplate(g);
    if (!tmpl) return;
    const name = setName(g);
    const ok = await confirmDialog(
      `Put ${name} back to the default? Its saved type, timestamp and body columns are removed — `
      + 'for every table with these columns, in this case and in others.',
      { okLabel: 'Back to default' });
    if (!ok) return;
    try {
      await api(`/api/timeline_templates/${tmpl.id}`, { method: 'DELETE' });
    } catch (e) {
      toast(`Could not put ${name} back to the default: ${e.message}`, 6000);
      return;
    }
    await loadTimelineTemplates();
    state.drafts.delete(g.sig);
    toast(`${name} is back to the default`);
    refreshTimeline();
    renderList();
    if (state.sel === g.sig) renderPane();
  }
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireTimeline() {
$('timelineBody').addEventListener('scroll', () => {
  if (!timelineScrollRaf) timelineScrollRaf = requestAnimationFrame(() => { timelineScrollRaf = null; renderTimelineRows(); });
}, { passive: true });

$('btnTimelineConfig').onclick = openTimelineSourceConfig;
}

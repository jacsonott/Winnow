/* The Timeline's detail pane, and the rows around a finding.

   Reading the Timeline used to mean leaving it: a line was one summarised
   line, and the rest of the row, its note and its tags were in its table.
   The pane beside the list shows the selected line in full — kind and
   host, its time, the summary, every column, the note (editable) — and
   tags it in place, from its chips or the tag keys, so a finding can be
   confirmed or dismissed where it was read.

   "What else happened then" is the other half. A finding is rarely the
   whole story: the rows next to it in time, in every table, are where the
   rest of it is, and nobody has tagged those yet. ± 1 min … ± 1 h
   rebuilds the Timeline with every untagged row in that window added
   (build_timeline's `context`), drawn hatched with ○ in the gutter, so the
   finding reads in its surroundings. Tagging one keeps it on the Timeline
   once the window is closed. The window is S.timeline.context, said in
   the head as a chip with ✕.

   The pane follows the selection (S.timeline.sel): picking a line shows
   it, Esc lets go of it. The pane itself stays put whether a line is
   picked or not, and is put away with Details in the head (remembered per
   browser, as Overview is): one that opened on a click narrowed the list
   under the pointer, and the second click of a double-click — open this
   row in its table — landed on whatever had moved there. The row is asked
   for once per line picked (GET /api/timeline_row), debounced so holding
   j does not ask for every line it passes. */
import { $, api, debounce, el, post, toast } from './core.js';
import { copyFieldButton, renderDetailContent, shownAsText } from './detail.js';
import { regroupIfGroupedByTag, writeClipboardText } from './grouping.js';
import { sourceLabel } from './sources.js';
import { S } from './state.js';
import { clearRowCaches, refreshUndoState } from './tags.js';
import { buildTimeline, jumpToTimelineRow, renderTimelineRows, timelineDayLabel,
  timelineRowAt, typeParts } from './timeline.js';
import { timelineEpoch, timelineIso } from './timelineoverview.js';

// How far either side of a line "What else happened then" reaches, minutes.
export const TIMELINE_CONTEXT_MINUTES = [1, 5, 15, 60];
export const TIMELINE_PANE_KEY = 'winnow.timelinePane';
const EMPTY = 'empty';

let shownKey = '';      // "<source_id>:<rid>" of the row the pane is for, EMPTY, or '' while it is put away
let row = null;         // that row, from /api/timeline_row
let line = null;        // that row as the list has it: kind, summary, ctx
let asking = 0;
let noteTimer = null;
let notePending = null; // {source_id, rid, note} typed and not yet saved

const keyOf = (sel) => (sel && sel.rid != null ? `${sel.source_id}:${sel.rid}` : '');
const reach = (m) => (m === 60 ? '1 h' : `${m} min`);

/* The selected line as the list holds it, or null while its page is not
   here (a rebuild, or End on a long Timeline). */
function lineNow() {
  const sel = S.timeline.sel;
  if (!sel || sel.rid == null) return null;
  const r = timelineRowAt(sel.pos);
  return r && r.source_id === sel.source_id && r.rid === sel.rid ? r : null;
}

export function timelinePaneOpen() {
  try { return (JSON.parse(localStorage.getItem(TIMELINE_PANE_KEY) || '{}') || {}).open !== false; } catch { return true; }
}

export function toggleTimelinePane(on = !timelinePaneOpen()) {
  try { localStorage.setItem(TIMELINE_PANE_KEY, JSON.stringify({ open: on })); } catch { /* private window */ }
  refreshTimelinePane();
}

/* Run whenever the selection may have moved (selectTimelineLine, every
   renderTimelineRows): refills the pane, or empties it. Cheap when
   nothing changed, since that is every scroll frame. */
export function syncTimelinePane() {
  const pane = $('timelinePane');
  const open = timelinePaneOpen();
  pane.hidden = !open;
  $('btnTimelinePane').setAttribute('aria-pressed', String(open));
  const key = open ? keyOf(S.timeline.sel) : '';
  if (!key) {
    if (shownKey && shownKey !== EMPTY) flushTimelineNote();
    row = null;
    line = null;
    if (!open) shownKey = '';
    else if (shownKey !== EMPTY) { shownKey = EMPTY; drawEmpty(); }
    return;
  }
  if (key !== shownKey) { show(key, null); return; }
  // The line's page landed after the pane was drawn without it, or was
  // fetched again by a rebuild: what the list says of it may have moved.
  const now = lineNow();
  if (now && now !== line) { line = now; drawPane(); }
}

/* After the Timeline is rebuilt: the row is asked for again (its tags may
   be what changed), and shown as it was until the answer is here, rather
   than blinking to "Loading…" on every tag. */
export function refreshTimelinePane() {
  const key = timelinePaneOpen() ? keyOf(S.timeline.sel) : '';
  if (!key) { syncTimelinePane(); return; }
  $('timelinePane').hidden = false;
  $('btnTimelinePane').setAttribute('aria-pressed', 'true');
  show(key, key === shownKey ? row : null);
}

function show(key, keep) {
  if (key !== shownKey) flushTimelineNote();
  const same = key === shownKey;
  shownKey = key;
  row = keep;
  line = lineNow() || (same ? line : null);
  drawPane();
  askRow();
}

const askRow = debounce(async () => {
  const sel = S.timeline.sel;
  const key = keyOf(sel);
  if (!key || key !== shownKey) return;
  const mine = ++asking;
  let got = null;
  let err = null;
  try {
    got = await api(`/api/timeline_row?source_id=${sel.source_id}&rid=${sel.rid}`);
  } catch (e) { err = e; }
  if (mine !== asking || key !== shownKey) return;
  row = got;
  drawPane(err);
}, 60);

/* ---------------------------------------------------------- the pane */

function paneHead(label) {
  const head = el('div', 'detail-head tl-pane-head');
  head.append(el('span', null, label));
  const close = el('button', 'btn ghost', '✕');
  close.type = 'button';
  close.title = 'Put this pane away (Details, above, brings it back)';
  close.setAttribute('aria-label', 'Put this pane away');
  close.onclick = () => toggleTimelinePane(false);
  head.append(close);
  return head;
}

function drawEmpty() {
  const body = el('div', 'tl-pane-body');
  body.append(el('p', 'tl-pane-empty',
    'Pick a line to read it here in full: every column, its tags and note, and what else happened around it.'));
  $('timelinePane').classList.remove('is-ctx');
  $('timelinePane').replaceChildren(paneHead('Details'), body);
}

function drawPane(err = null) {
  const pane = $('timelinePane');
  // A redraw while the note is being typed in keeps the typing where it
  // was: the box is rebuilt, the caret is put back.
  const a = document.activeElement;
  const typing = a && a.id === 'timelineNote' ? [a.selectionStart, a.selectionEnd] : null;
  pane.replaceChildren();
  const ctx = !!(line && line.ctx);
  pane.classList.toggle('is-ctx', ctx);
  // A row around a finding can carry a tag that is switched off.
  const head = paneHead(!ctx ? 'Finding' : line.tags.length ? 'Around a finding' : 'Untagged row');
  const body = el('div', 'tl-pane-body');
  pane.append(head, body);
  if (err) {
    body.append(el('p', 'tl-pane-empty', err.status === 404
      ? 'This row is no longer in the case.' : `Could not read this row: ${err.message}`));
    return;
  }
  if (line) {
    const kind = el('div', 'tl-pane-kind');
    kind.append(...typeParts(line));
    body.append(kind);
  }
  const sel = S.timeline.sel;
  const ts = (line && line.ts) || (sel && sel.ts) || '';
  const when = el('div', 'tl-pane-when');
  when.append(el('b', null, ts ? ts.slice(11) : '—'),
    el('span', null, ts ? `${timelineDayLabel(ts)} ${ts.slice(0, 4)}` : 'No timestamp'));
  body.append(when);
  if (line && line.lead) body.append(el('div', 'tl-pane-lead', line.lead));
  if (line && line.detail) body.append(el('div', 'tl-pane-detail', line.detail));
  if (!row) {
    body.append(el('p', 'tl-pane-empty', 'Loading…'));
    return;
  }
  body.append(tagChips());
  const c = S.timeline.context;
  if (ctx && c) {
    body.append(el('p', 'tl-pane-hint',
      `On the Timeline because it is within ${reach(c.minutes)} of ${c.around.slice(11, 19)}, `
      + 'not because of a tag that is switched on. Tag it to keep it here.'));
  }
  if (line && line.n > 1) {
    const span = line.ts && line.ts_last ? `, ${line.ts.slice(11, 19)} to ${line.ts_last.slice(11, 19)}` : '';
    body.append(el('p', 'tl-pane-hint',
      `The first of ${line.n} rows folded into this line${span}. The tags and note here are this row's; `
      + `×${line.n} on the line shows each one.`));
  }
  body.append(noteBox());
  const acts = el('div', 'tl-pane-acts');
  const open = el('button', 'btn primary', 'Open in table ↦');
  open.type = 'button';
  open.title = 'Open this row in its table (or double-click the line, or Enter)';
  open.onclick = () => jumpToTimelineRow(row.source_id, row.rid);
  acts.append(open);
  body.append(acts, aroundBox(ts), fields());
  if (typing) {
    const area = $('timelineNote');
    area.focus({ preventScroll: true });
    area.setSelectionRange(typing[0], typing[1]);
  }
}

/* A chip per tag, pressed while the row carries it; a click puts the tag
   on or takes it off, as the tag's key does while the Timeline is up. */
function tagChips() {
  const box = el('div', 'tl-pane-tags');
  for (const t of S.tags) {
    const on = row.tags.includes(t.id);
    const chip = el('button', 'tag-chip');
    chip.type = 'button';
    chip.dataset.tagId = t.id;
    chip.setAttribute('aria-pressed', String(on));
    chip.style.color = on ? t.color : '';
    const sw = el('span', 'swatch');
    sw.style.background = t.color;
    chip.append(sw, el('span', 'name', t.name));
    if (t.hotkey) chip.append(el('span', 'key', t.hotkey));
    chip.title = (on ? `Take ${t.name} off this row` : `Tag this row ${t.name}`)
      + (t.hotkey ? ` (or press ${t.hotkey})` : '');
    chip.onclick = () => tagTimelineLine(t);
    box.append(chip);
  }
  return box;
}

/* The tag `t` on the selected line, or off it when it has it: one pair
   through /api/row_tags, which journals it for undo (invariant #7) like
   every other tag write, then the Timeline rebuilt on the same line — a
   row tagged may join it for good, one untagged may leave it. False when
   there is no line the pane has read, so the key goes on elsewhere. */
export async function tagTimelineLine(t) {
  const sel = S.timeline.sel;
  if (!sel || sel.rid == null || !row || keyOf(sel) !== shownKey) return false;
  const on = !row.tags.includes(t.id);
  try {
    await post('/api/row_tags', { pairs: [[sel.source_id, sel.rid]], tag_id: t.id, on });
  } catch (e) {
    toast('Could not tag: ' + e.message, 5000);
    return true;
  }
  // The grid's caches hold this row as it was (sqlTagHotkey's reasoning).
  clearRowCaches();
  regroupIfGroupedByTag();
  refreshUndoState();
  toast(`${on ? 'Tagged' : 'Untagged'} 1 row · ${t.name}`);
  await buildTimeline();
  return true;
}

function noteBox() {
  const box = el('div', 'note-box tl-pane-note');
  const label = el('label', null, 'Note');
  label.htmlFor = 'timelineNote';
  const area = el('textarea');
  area.id = 'timelineNote';
  area.rows = 3;
  area.placeholder = 'Why does this row matter?';
  const ids = { source_id: row.source_id, rid: row.rid };
  const mine = notePending && notePending.source_id === ids.source_id && notePending.rid === ids.rid;
  area.value = mine ? notePending.note : (row.note || '');
  const status = el('div', 'note-status', mine ? 'Saving…' : '');
  status.id = 'timelineNoteStatus';
  area.oninput = () => {
    status.textContent = 'Saving…';
    notePending = { ...ids, note: area.value };
    clearTimeout(noteTimer);
    noteTimer = setTimeout(flushTimelineNote, 500);
  };
  area.onblur = () => flushTimelineNote();
  box.append(label, area, status);
  return box;
}

/* Saves a note typed and not yet saved: on a pause, on leaving the box,
   and before the pane takes another row, so moving on never drops one.
   The line's ✎ and the grid's copy of the row follow. */
export async function flushTimelineNote() {
  clearTimeout(noteTimer);
  noteTimer = null;
  const p = notePending;
  notePending = null;
  if (!p) return;
  try {
    await post('/api/note', p);
  } catch (e) {
    toast('Could not save the note: ' + e.message, 5000);
    return;
  }
  const same = (r) => r && r.source_id === p.source_id && r.rid === p.rid;
  for (const page of S.timeline.pages.values()) for (const r of page) if (same(r)) r.note = p.note;
  if (same(row)) row.note = p.note;
  clearRowCaches();
  renderTimelineRows();
  const status = $('timelineNoteStatus');
  if (status && same(row) && !notePending) status.textContent = 'Saved';
}

/* "What else happened then": a button per reach, pressed for the window
   the Timeline is built with when it is around this line's time. */
function aroundBox(ts) {
  const box = el('div', 'tl-pane-around');
  box.append(el('div', 'tl-pane-sub', 'What else happened then'));
  const seg = el('div', 'tl-pane-seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', 'Add the untagged rows around this line');
  const ep = timelineEpoch(ts);
  const c = S.timeline.context;
  for (const m of TIMELINE_CONTEXT_MINUTES) {
    const b = el('button', 'btn ghost', `± ${reach(m)}`);
    b.type = 'button';
    const on = !!(c && c.minutes === m && ep != null && c.around === timelineIso(ep));
    b.setAttribute('aria-pressed', String(on));
    b.disabled = ep == null;
    b.title = on ? 'Take the untagged rows off the Timeline again'
      : `Add every untagged row from ${reach(m)} before this line to ${reach(m)} after it, from every table`;
    b.onclick = () => setTimelineContext(on ? null : { minutes: m, around: ts });
    seg.append(b);
  }
  box.append(seg, el('small', null, ep == null
    ? 'This line has no timestamp, so nothing is around it in time.'
    : 'Adds the untagged rows from every table in that window to the Timeline, hatched, '
      + 'so you can read the finding in its surroundings.'));
  return box;
}

/* Every column the row has, laid out as the grid's detail pane lays them
   (JSON and XML pretty-printed), each with its copy button. The values go
   in as stored: the grid's per-column timestamp formats belong to the
   table that is open, which is not this row's. */
function fields() {
  const box = el('div', 'tl-pane-cols');
  const src = S.sources.find((s) => s.id === row.source_id);
  box.append(el('div', 'tl-pane-sub', `${src ? sourceLabel(src) : row.source_name} · line ${row.rid}`));
  const dl = el('dl');
  row.columns.forEach((c, i) => {
    const v = row.values[i];
    if (v == null || v === '') return;
    dl.append(el('dt', null, c.name));
    const dd = el('dd');
    dd.dataset.col = c.name;
    let content;
    try {
      content = renderDetailContent(String(v));
    } catch (e) {
      console.error(e);
      content = shownAsText(String(v), 'Could not be laid out, so it is shown as text.');
    }
    dd.append(copyFieldButton(c.name), content);
    dl.append(dd);
  });
  box.append(dl);
  return box;
}

/* ----------------------------------------------- rows around a finding */

/* The window the Timeline is built with, `{minutes, around}` about a
   line's timestamp or null, and the rebuild that takes. */
export function setTimelineContext(want) {
  if (!want) {
    S.timeline.context = null;
  } else {
    const ep = timelineEpoch(want.around);
    if (ep == null) return;
    S.timeline.context = {
      minutes: want.minutes, around: timelineIso(ep),
      start: timelineIso(ep - want.minutes * 60),
      // Half-open, and the line's own second is in it whatever its
      // fraction.
      end: timelineIso(ep + want.minutes * 60 + 1),
    };
  }
  buildTimeline();
}

/* The chip in the head while a window is on: what it is, how many rows it
   brought in, and ✕. */
export function renderTimelineContextChip() {
  const box = $('timelineContext');
  box.replaceChildren();
  const c = S.timeline.context;
  if (!c) return;
  const v = S.timeline.view;
  const n = (v && v.context_rows) || 0;
  const chip = el('span', 'tl-range-chip tl-context-chip');
  chip.append(el('span', null, `± ${reach(c.minutes)} around ${c.around.slice(11, 19)}`),
    el('span', 'tl-context-n', `· ${n.toLocaleString()} untagged`));
  chip.title = `Every untagged row from ${c.start} up to ${c.end}, from every table, is on the Timeline`;
  const x = el('button', 'tl-range-x', '✕');
  x.type = 'button';
  x.title = 'Take the untagged rows off the Timeline again';
  x.setAttribute('aria-label', 'Take the untagged rows off the Timeline again');
  x.onclick = () => setTimelineContext(null);
  chip.append(x);
  box.append(chip);
}

/* DOM wiring, called once by main.js. */
export function wireTimelinePane() {
  $('btnTimelinePane').onclick = () => toggleTimelinePane();
  // Delegated: the field list is rebuilt for every row the pane shows.
  $('timelinePane').addEventListener('click', (e) => {
    const btn = e.target.closest('.dfield-copy');
    if (!btn || !row) return;
    const column = btn.closest('dd').dataset.col;
    const i = row.columns.findIndex((c) => c.name === column);
    if (i < 0) return;
    writeClipboardText(Promise.resolve(String(row.values[i] ?? '')), `Copied ${column}`);
  });
}

/* The unified timeline tab.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, OVERSCAN, PAGE, ROW_H, api, el, post, setBusy, toast } from './core.js';
import { rowsPaintY, spacerPx, vScroll } from './grid.js';
import { fieldInput } from './home.js';
import { armOpCancel, opToken } from './jobs.js';
import { headerSig } from './savedfilters.js';
import { loadSources, openSource, recenterOnRow, sourceLabel, sourceTitle } from './sources.js';
import { showGridTab } from './sql.js';
import { S } from './state.js';
import { modal } from './ui.js';

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
   timeline is every tagged row until it is narrowed. */
export function renderTimelineTagFilter() {
  const wrap = $('timelineTagFilter');
  wrap.replaceChildren();
  if (S.timeline.tagFilter === null) S.timeline.tagFilter = S.tags.map((t) => t.id);
  for (const t of S.tags) {
    const on = S.timeline.tagFilter.includes(t.id);
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
      S.timeline.tagFilter = on
        ? S.timeline.tagFilter.filter((id) => id !== t.id)
        : [...S.timeline.tagFilter, t.id];
      // Repainted here, not by the DOM: a chip's state is an attribute
      // this function writes, where a checkbox kept its own.
      renderTimelineTagFilter();
      buildTimeline();
    };
    wrap.append(chip);
  }
}

export async function buildTimeline() {
  if (S.timeline.tagFilter === null) S.timeline.tagFilter = S.tags.map((t) => t.id);
  S.timeline.pages.clear();
  S.timeline.pending.clear();
  // Which rows are showing their raw text is per-build state: the rows a
  // rebuild brings back are a different set, and a Set that outlived every
  // build would grow for the life of the tab.
  S.timeline.raw.clear();
  const reqId = ++S.timeline.reqId;
  if (!S.timeline.tagFilter.length) {
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
      v = await post('/api/timeline', { tag_ids: S.timeline.tagFilter, op_token: token });
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
  const on = (S.timeline.tagFilter || []).length;
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
    if (r) fillTimelineBody(bodyCell, r);
    row.append(tsCell, typeCell, bodyCell);
    if (r) row.onclick = () => jumpToTimelineRow(r.source_id, r.rid);
    frag.append(row);
  }
  rowsEl.replaceChildren(frag);
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
  cell.title = r.body;
  const summarised = !!(r.lead || r.detail);
  const key = `${r.source_id}:${r.rid}`;
  const showRaw = !summarised || S.timeline.raw.has(key);
  if (showRaw) {
    cell.append(el('span', 'tl-raw', r.body));
  } else {
    if (r.lead) cell.append(el('span', 'tl-lead', r.lead));
    if (r.detail) cell.append(el('span', 'tl-detail', r.detail));
  }
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
   in the open tabs by then), so this is not an extra switch. */
export async function jumpToTimelineRow(sourceId, rid) {
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
}

// Guarded like #body's own scroll handler below: scroll can fire several
// times per frame, and an unguarded rAF per event runs that many full
// repaints in the same frame.
export let timelineScrollRaf = null;

/* Per-source "which column is the timestamp, which columns make up the
   body, what's this source called on the timeline" editor — writes to
   workspace.timeline_templates (keyed by header set, so it's reused by
   any future case whose columns match), not anything case-scoped. Every
   real source gets a row here, whether or not it has tagged rows yet —
   configuring ahead of tagging is the normal workflow, not an edge case. */
export function openTimelineSourceConfig() {
  modal('Configure timeline tables', (b) => {
    b.append(el('p', null,
      'Per header set, reused across cases: which column is the timestamp, which columns (in the order '
      + 'checked) make up the body, and what this kind of table is called on the timeline. A table with no '
      + 'matching config here falls back to its first datetime column and its own file name, and its body '
      + 'reads as the summary Winnow ships for that artefact shape — or, for a shape it does not know, '
      + 'every column joined. Checking body columns here replaces that summary with exactly what you check.'));

    const list = el('div', 'session-list');
    b.append(list);

    const realSources = S.sources.filter((s) => !s.is_merge && !s.error);
    for (const src of realSources) {
      // Header-set key: the file's own columns (see baseColumns). A derived
      // column can still be *picked* as the timestamp below — it just isn't
      // part of what identifies this source type.
      const colNames = src.columns.filter((c) => !c.derived).map((c) => c.name);
      const dtCols = src.columns.filter((c) => c.type === 'datetime').map((c) => c.name);
      const existing = timelineTemplateFor(colNames);

      const row = el('div', 'row-actions session-row');
      row.style.flexDirection = 'column';
      row.style.alignItems = 'stretch';
      const nameSpan = el('span', 'session-name', sourceLabel(src));
      nameSpan.title = sourceTitle(src);
      row.append(nameSpan);

      const typeInput = fieldInput(existing ? existing.type_label : '');
      typeInput.placeholder = `Type (defaults to "${src.name}")`;
      row.append(typeInput);

      const tsSel = el('select');
      tsSel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit;margin-top:6px';
      const noneOpt = document.createElement('option');
      noneOpt.value = '';
      noneOpt.textContent = dtCols.length ? '(first datetime column)' : '(no datetime column on this table)';
      tsSel.append(noneOpt);
      for (const c of dtCols) {
        const opt = document.createElement('option');
        opt.value = c; opt.textContent = c;
        tsSel.append(opt);
      }
      tsSel.value = existing && dtCols.includes(existing.timestamp_column) ? existing.timestamp_column : '';
      row.append(tsSel);

      const bodyWrap = el('div', 'row-actions');
      bodyWrap.style.flexWrap = 'wrap';
      let bodyOrder = existing && existing.body_columns && existing.body_columns.length
        ? existing.body_columns.filter((c) => colNames.includes(c))
        : [];
      for (const c of colNames) {
        const chip = el('button', 'btn ghost', c);
        chip.setAttribute('aria-pressed', String(bodyOrder.includes(c)));
        chip.title = 'Toggle inclusion in the body — order follows the order you check them in';
        chip.onclick = () => {
          bodyOrder = bodyOrder.includes(c) ? bodyOrder.filter((x) => x !== c) : [...bodyOrder, c];
          chip.setAttribute('aria-pressed', String(bodyOrder.includes(c)));
        };
        bodyWrap.append(chip);
      }
      row.append(el('span', 'fb-help', 'Body columns (click to toggle, order = click order; none checked = every column):'), bodyWrap);

      const saveBtn = el('button', 'btn', 'Save');
      saveBtn.style.marginTop = '6px';
      saveBtn.onclick = async () => {
        await post('/api/timeline_templates', {
          col_names: colNames,
          type_label: typeInput.value.trim() || src.name,
          timestamp_column: tsSel.value || null,
          body_columns: bodyOrder,
        });
        await loadTimelineTemplates();
        toast(`Saved timeline config for ${src.name}`);
      };
      row.append(saveBtn);
      list.append(row);
    }
    if (!realSources.length) list.append(el('div', 'note-status', 'No tables in this case yet.'));
  }, { wide: true });
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

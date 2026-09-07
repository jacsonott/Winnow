/* Sessions: named snapshots of the analysis layer, stored IN the case file.

   A session is every open source's tags and notes at a moment in time.
   They live in the case's own `sessions` table, so work an analyst saved —
   or received from a colleague and adopted — travels with the .db instead
   of being left behind in a folder beside it. A FILE is produced only to
   hand work to someone else.

   The diff view is why this earns a panel rather than two menu entries:
   comparing a handed-over session against the live case is how a review
   gets QC'd, and it answers the three questions a reviewer actually has —
   what did they add, what did they drop, and where did we disagree.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { renderHead } from './columns.js';
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadSources, openSource } from './sources.js';
import { S } from './state.js';
import { updateFiltersButton } from './timeframe.js';
import { confirmDialog, modal, promptDialog } from './ui.js';
import { rebuildView } from './view.js';

const LIVE = '__live__';

function fmtWhen(iso) {
  return (iso || '').replace('T', ' ').slice(0, 16);
}

export function openSessionManager() {
  modal('Sessions', (b) => {
    b.append(el('p', null,
      "A session is every open table's tags and notes at a point in time. They're stored "
      + 'inside this case file, so they travel with it — save one before changing direction, '
      + 'start a fresh pass without losing what you had, or compare two to see what changed. '
      + 'Download one only when you need to hand your work to another analyst.'));

    const list = el('div', 'session-list');
    b.append(list);

    let sessions = [];
    let compare = null;   // assigned below; refresh() repopulates its options

    async function refresh() {
      list.replaceChildren(el('div', 'note-status', 'Loading…'));
      try {
        sessions = (await api('/api/case_sessions')).sessions;
      } catch (e) {
        list.replaceChildren(el('div', 'note-status', 'Could not load sessions: ' + e.message));
        return;
      }
      list.replaceChildren();
      if (!sessions.length) {
        list.append(el('div', 'note-status',
          'No sessions saved in this case yet — "Save current work" below makes one.'));
      } else {
        for (const s of sessions) list.append(sessionRow(s));
      }
      // The compare dropdowns are built before this async fetch lands, so
      // they start empty — repopulate them whenever the list changes, or
      // they'd only ever offer sessions that existed last time the panel
      // was opened.
      if (compare) compare.refreshOptions();
    }

    function sessionRow(s) {
      const row = el('div', 'row-actions session-row');
      const name = el('span', 'session-name', s.name);
      if (s.origin === 'imported') {
        // Worth marking: it is someone else's conclusions, not yours.
        const badge = el('span', 'session-badge', 'received');
        badge.title = 'Adopted from a session file another analyst sent';
        name.append(' ', badge);
      }
      row.append(name, el('span', 'count',
        `${s.tagged_rows.toLocaleString()} tagged · ${s.notes.toLocaleString()} notes · `
        + `${s.source_count} table${s.source_count === 1 ? '' : 's'} · ${fmtWhen(s.saved_at)}`));

      const load = el('button', 'btn ghost', 'Load');
      load.title = 'Replace the current tags and notes with this session';
      load.onclick = async () => {
        if (!(await confirmDialog(
          `Load "${s.name}"?\n\nThis replaces the tags and notes currently in the case. `
          + 'Save your current work first if you want to keep it.',
          { okLabel: 'Load' }))) return;
        setBusy(true);
        let res;
        try {
          res = await api(`/api/case_sessions/${encodeURIComponent(s.name)}/load?merge=false`,
                          { method: 'POST' });
          await loadSources();
        } finally { setBusy(false); }
        (res.warnings || []).forEach((w) => toast(w, 6000));
        $('modal').hidden = true;
        toast(`Loaded "${s.name}" · ${res.tags_applied.toLocaleString()} tag assignments`);
      };

      const dl = el('button', 'btn ghost', 'Download');
      dl.title = 'Save as a file to send to another analyst';
      dl.onclick = () => { window.location = `/api/case_sessions/${encodeURIComponent(s.name)}/download`; };

      const ren = el('button', 'btn ghost', 'Rename');
      ren.onclick = async () => {
        const n = await promptDialog('New name:', s.name);
        if (!n || !n.trim() || n.trim() === s.name) return;
        try {
          await post(`/api/case_sessions/${encodeURIComponent(s.name)}/rename`, { name: n.trim() });
        } catch (e) { toast(e.message, 5000); return; }
        refresh();
      };

      const del = el('button', 'btn ghost', '✕');
      del.title = 'Delete this session from the case';
      del.onclick = async () => {
        if (!(await confirmDialog(`Delete session "${s.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
        await api(`/api/case_sessions/${encodeURIComponent(s.name)}`, { method: 'DELETE' });
        refresh();
      };
      row.append(load, dl, ren, del);
      return row;
    }

    const acts = el('div', 'row-actions');
    const save = el('button', 'btn', 'Save current work…');
    save.onclick = async () => {
      const name = await promptDialog('Name this session:');
      if (!name || !name.trim()) return;
      setBusy(true);
      try { await post('/api/case_sessions', { name: name.trim() }); }
      catch (e) { toast(e.message, 5000); return; }
      finally { setBusy(false); }
      toast(`Saved "${name.trim()}" into this case`);
      refresh();
    };

    const fresh = el('button', 'btn ghost', 'Start a fresh pass…');
    fresh.title = 'Save what you have, then clear tags and notes to review the evidence again';
    fresh.onclick = async () => {
      const name = await promptDialog(
        'Save the current work as — then start with no tags or notes:',
        `pass ${sessions.length + 1}`);
      if (name === null) return;
      if (!name.trim() && !(await confirmDialog(
        'Clear all tags and notes WITHOUT saving them first?\n\nThis cannot be undone.',
        { danger: true, okLabel: 'Clear anyway' }))) return;
      setBusy(true);
      let res;
      try { res = await post('/api/case_sessions/new', { save_as: name.trim() || null }); }
      finally { setBusy(false); }
      await loadSources();
      $('modal').hidden = true;
      toast(`Fresh pass started — ${res.tags_cleared.toLocaleString()} tags cleared`
            + (res.saved ? `, saved as "${res.saved.name}"` : ''), 8000);
    };

    const adopt = el('label', 'btn ghost', 'Receive a session file…');
    adopt.title = "Store another analyst's session in this case without applying it yet";
    const input = el('input');
    input.type = 'file';
    input.accept = '.json';
    input.hidden = true;
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      let data;
      try { data = JSON.parse(await file.text()); }
      catch { toast('That file is not valid JSON', 5000); return; }
      const name = await promptDialog('Store it in this case as:',
                                      file.name.replace(/\.winnow_case\.json$|\.json$/, ''));
      if (!name || !name.trim()) return;
      try { await post('/api/case_sessions/adopt', { name: name.trim(), session: data }); }
      catch (e) { toast(e.message, 6000); return; }
      input.value = '';
      toast(`Stored "${name.trim()}" — load it, or compare it against your work`);
      refresh();
    };
    adopt.append(input);
    acts.append(save, fresh, adopt);
    b.append(acts);

    b.append(el('h4', null, 'Compare two sessions'));
    b.append(el('p', 'fb-help',
      'What one has that the other does not — for reviewing an analyst\'s work, or checking '
      + 'what a second pass changed. Tags are matched by NAME, so a session from another '
      + "analyst's case compares correctly even though their tag numbering differs."));
    compare = diffPanel(() => sessions);
    b.append(compare);
    refresh();   // after `compare` exists, so the dropdowns get filled
  }, { wide: true });
}

/* The QC view. Left is usually what was handed over, right what the
   reviewer has now — so "added" reads as the reviewer's new findings. */
function diffPanel(getSessions) {
  const wrap = el('div', 'session-compare');
  const controls = el('div', 'row-actions');
  const left = el('select');
  const right = el('select');
  const go = el('button', 'btn ghost', 'Compare');
  const out = el('div', 'session-diff');

  function fill() {
    for (const sel of [left, right]) {
      const keep = sel.value;
      sel.replaceChildren();
      const live = el('option', null, 'Current work (live)');
      live.value = LIVE;
      sel.append(live);
      for (const s of getSessions()) {
        const o = el('option', null, s.name);
        o.value = s.name;
        sel.append(o);
      }
      if (keep) sel.value = keep;
    }
    if (!right.value || right.value === left.value) right.value = LIVE;
  }

  controls.append(el('span', 'fb-help', 'From'), left,
                  el('span', 'fb-help', 'to'), right, go);
  wrap.append(controls, out);

  go.onclick = async () => {
    if (left.value === right.value) { toast('Pick two different sessions'); return; }
    out.replaceChildren(el('div', 'note-status', 'Comparing…'));
    let d;
    try {
      d = await api(`/api/case_sessions/diff?left=${encodeURIComponent(left.value)}`
                    + `&right=${encodeURIComponent(right.value)}`);
    } catch (e) {
      out.replaceChildren(el('div', 'note-status', 'Could not compare: ' + e.message));
      return;
    }
    renderDiff(out, d);
  };

  // Populate on open and whenever the caller's list changes underneath us.
  fill();
  wrap.refreshOptions = fill;
  return wrap;
}

/* A tag row's side is a list of tag names; a note row's is the note text
   or null. One cell renderer for both — "(none)" rather than an empty cell,
   so a removal reads as a removal instead of a rendering glitch. */
function side(v) {
  if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
  return v ? String(v) : '(none)';
}

/* Open a table on exactly these rows: the grid, filtered to `rid IN (…)`
   through the raw filter-tree node, so the analyst reads the evidence
   with every other tool — sort, search, the detail panel — and the
   Filters button shows how to get back out. */
export async function openDiffRows(sourceId, rids, what) {
  if (!(S.sources || []).some((s) => s.id === sourceId)) { toast('That table is no longer in this case'); return; }
  const ids = [...new Set(rids.map(Number).filter(Number.isFinite))];
  if (!ids.length) return;
  $('modal').hidden = true;
  await openSource(sourceId);
  S.filterTree = { type: 'raw', sql: `rid IN (${ids.join(', ')})` };
  updateFiltersButton();
  renderHead();
  await rebuildView({ keepScroll: false });
  toast(`${what || 'Rows'} — ${ids.length.toLocaleString()} row${ids.length === 1 ? '' : 's'}; Clear filters brings the table back`, 5000);
}

/* Which columns make a one-line preview of a row: the first timestamp-
   looking column, then the first few others with something in them. */
function previewColumns(columns, cells) {
  const idxs = [];
  const ts = columns.findIndex((c) => /time|date|created|modified/i.test(c));
  if (ts >= 0 && cells[ts] !== '' && cells[ts] != null) idxs.push(ts);
  for (let i = 0; i < columns.length && idxs.length < 4; i++) {
    if (i === ts) continue;
    const v = cells[i];
    if (v !== '' && v != null) idxs.push(i);
  }
  return idxs;
}

function rowPreview(columns, cells) {
  const wrap = el('div', 'diff-preview');
  if (!columns || !cells) {
    wrap.append(el('span', 'diff-preview-none', 'row not in this case'));
    return wrap;
  }
  for (const i of previewColumns(columns, cells)) {
    const cell = el('span', 'diff-cell');
    cell.append(el('span', 'diff-cell-k', columns[i]), el('span', 'diff-cell-v', String(cells[i])));
    wrap.append(cell);
  }
  wrap.title = columns.map((c, i) => `${c}: ${cells[i] == null ? '' : cells[i]}`).join('\n');
  return wrap;
}

function fullRow(columns, cells) {
  const dl = el('div', 'diff-full');
  columns.forEach((c, i) => {
    const v = cells[i];
    if (v === '' || v == null) return;
    const kv = el('div', 'diff-full-kv');
    kv.append(el('span', 'diff-cell-k', c), el('span', 'diff-cell-v', String(v)));
    dl.append(kv);
  });
  return dl;
}

const GROUPS = [
  ['added', 'Added on the right', 'diff-added'],
  ['removed', 'Removed on the right', 'diff-removed'],
  ['changed', 'Tagged differently', 'diff-changed'],
  ['note_changes', 'Notes changed', 'diff-note'],
];

/* The diff, as rows: every entry is the row itself (a one-line preview,
   click for all of it) beside what each side said about it, with a way
   to open it — or the whole group — in the table. The chips at the top
   narrow the list to one kind of change; the tag picker to one tag. */
function renderDiff(out, d) {
  out.replaceChildren();
  const c = d.counts;
  if (!c.added && !c.removed && !c.changed && !c.note_changes) {
    out.append(el('div', 'note-status', 'No differences — the two agree on every row.'));
    return;
  }
  if (d.only_left_sources.length || d.only_right_sources.length) {
    // Not a like-for-like comparison; say so rather than letting the
    // numbers imply the analysts disagreed about rows nobody looked at.
    out.append(el('div', 'note-status',
      'These sessions cover different tables — '
      + `${d.only_left_sources.length} only on the left, ${d.only_right_sources.length} only on the right. `
      + 'Counts below cover what they share.'));
  }
  const state = { group: 'all', tag: '' };
  const tagNames = new Set();
  for (const [key] of GROUPS) {
    if (key === 'note_changes') continue;
    for (const r of d[key]) { for (const n of r.left) tagNames.add(n); for (const n of r.right) tagNames.add(n); }
  }

  const bar = el('div', 'diff-bar');
  const chips = el('div', 'diff-chips');
  const chip = (key, label, n) => {
    const b = el('button', 'btn ghost diff-chip', `${label} ${n.toLocaleString()}`);
    b.dataset.group = key;
    b.setAttribute('aria-pressed', String(state.group === key));
    b.onclick = () => { state.group = state.group === key ? 'all' : key; paint(); };
    return b;
  };
  const tagSel = el('select', 'diff-tag');
  tagSel.append(new Option('any tag', ''));
  for (const n of [...tagNames].sort()) tagSel.append(new Option(n, n));
  tagSel.onchange = () => { state.tag = tagSel.value; paint(); };
  bar.append(chips, tagSel);
  const body = el('div', 'diff-body');
  out.append(bar, body);

  const matchesTag = (r) => !state.tag || (r.left || []).includes(state.tag) || (r.right || []).includes(state.tag);

  function paint() {
    chips.replaceChildren(
      chip('added', '+', c.added), chip('removed', '−', c.removed),
      chip('changed', '±', c.changed), chip('notes', '✎', c.note_changes));
    body.replaceChildren();
    for (const [key, label, cls] of GROUPS) {
      const gkey = key === 'note_changes' ? 'notes' : key;
      if (state.group !== 'all' && state.group !== gkey) continue;
      const rows = (key === 'note_changes' ? d[key] : d[key].filter(matchesTag));
      if (!rows.length) continue;
      const head = el('div', 'diff-group-head');
      head.append(el('h4', null, `${label} (${rows.length.toLocaleString()})`));
      // One "open" per table the group touches: the grid shows one table.
      const bySource = new Map();
      for (const r of rows) if (r.source_id != null) bySource.set(r.source_id, (bySource.get(r.source_id) || []).concat(r.rid));
      for (const [sid, rids] of bySource) {
        const src = (S.sources || []).find((s) => s.id === sid);
        const open = el('button', 'btn ghost diff-open-all', `Open ${rids.length.toLocaleString()} in ${src ? (src.nickname || src.name) : 'table'}`);
        open.title = 'Show exactly these rows in the table';
        open.onclick = () => openDiffRows(sid, rids, label);
        head.append(open);
      }
      body.append(head);
      const tbl = el('table', 'diff-table');
      for (const r of rows.slice(0, 200)) {
        const columns = r.source_id != null ? d.columns[String(r.source_id)] : null;
        const tr = el('tr', cls);
        const rid = el('td', 'diff-rid', `${r.source} · row ${r.rid.toLocaleString()}`);
        const prev = el('td', 'diff-row');
        prev.append(rowPreview(columns, r.cells));
        const openOne = el('button', 'btn ghost diff-open', '⤴');
        openOne.title = 'Open this row in the table';
        openOne.disabled = r.source_id == null;
        openOne.onclick = (e) => { e.stopPropagation(); openDiffRows(r.source_id, [r.rid], `Row ${r.rid}`); };
        const act = el('td', 'diff-act');
        act.append(openOne);
        tr.append(rid, prev, el('td', 'diff-side', side(r.left)), el('td', 'diff-arrow', '→'),
                  el('td', 'diff-side', side(r.right)), act);
        if (columns && r.cells) {
          tr.classList.add('diff-expandable');
          tr.title = 'Click to see the whole row';
          tr.onclick = () => {
            const next = tr.nextElementSibling;
            if (next && next.classList.contains('diff-full-row')) { next.remove(); return; }
            const full = el('tr', 'diff-full-row');
            const td = el('td'); td.colSpan = 6; td.append(fullRow(columns, r.cells));
            full.append(td);
            tr.after(full);
          };
        }
        tbl.append(tr);
      }
      body.append(tbl);
      if (rows.length > 200) body.append(el('div', 'fb-help', `Showing the first 200 of ${rows.length.toLocaleString()}.`));
    }
    if (!body.children.length) body.append(el('div', 'note-status', 'Nothing matches that tag in this group.'));
    if (d.truncated) {
      body.append(el('div', 'note-status',
        'The comparison hit its row cap — these sessions differ on more rows than are listed.'));
    }
  }
  paint();
}

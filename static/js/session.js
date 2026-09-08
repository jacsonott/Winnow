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

const LIVE_LABEL = 'Current work';
const KINDS = [
  ['removed', (l, r) => `Only in ${l}`, 'tagged on the left, not on the right'],
  ['added', (l, r) => `Only in ${r}`, 'tagged on the right, not on the left'],
  ['changed', () => 'Tagged differently', 'tagged on both sides, with different tags'],
  ['note_changes', () => 'Notes changed', 'a note that differs'],
];
const KIND_OF = { removed: 'removed', added: 'added', changed: 'changed', note_changes: 'note' };

const nameOf = (v) => (v === LIVE ? LIVE_LABEL : v);

/* The comparison as counts, one line per table. A count is a button:
   the rows behind it open in the grid, marked with which session tagged
   them — the panel never lists rows itself. */
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
  const L = nameOf(d.left), R = nameOf(d.right);
  // Per table: the rows of each kind, keyed by live source id (a table
  // this case lacks gets a line with no way in).
  const tables = new Map();
  for (const [kind] of KINDS) {
    for (const r of d[kind]) {
      const key = r.source_id != null ? `id:${r.source_id}` : `name:${r.source}`;
      const rec = tables.get(key) || { sourceId: r.source_id, name: r.source, rows: { removed: [], added: [], changed: [], note_changes: [] } };
      rec.rows[kind].push(r);
      tables.set(key, rec);
    }
  }
  const tbl = el('table', 'diff-stats');
  const head = el('tr');
  head.append(el('th', null, 'Table'));
  for (const [kind, label, why] of KINDS) {
    const th = el('th', 'n', label(L, R));
    th.title = why;
    head.append(th);
  }
  head.append(el('th', 'n', 'All'));
  tbl.append(head);
  for (const rec of tables.values()) {
    const tr = el('tr');
    const nameTd = el('td', null, rec.name);
    if (rec.sourceId == null) nameTd.append(el('span', 'fb-help', ' — not in this case'));
    tr.append(nameTd);
    const all = [];
    for (const [kind] of KINDS) {
      const rows = rec.rows[kind];
      all.push(...rows);
      const td = el('td', 'n diff-n-' + kind);
      if (rows.length && rec.sourceId != null) {
        const b = el('button', 'btn ghost', rows.length.toLocaleString());
        b.title = `Open these ${rows.length.toLocaleString()} rows in the table, marked`;
        b.onclick = () => pivotDiff(d, rec, kind);
        td.append(b);
      } else td.append(el('span', 'zero', rows.length ? rows.length.toLocaleString() : '·'));
      tr.append(td);
    }
    const allTd = el('td', 'n');
    if (all.length && rec.sourceId != null) {
      const b = el('button', 'btn', all.length.toLocaleString());
      b.title = 'Open every differing row in this table';
      b.onclick = () => pivotDiff(d, rec, null);
      allTd.append(b);
    } else allTd.append(el('span', 'zero', '·'));
    tr.append(allTd);
    tbl.append(tr);
  }
  out.append(tbl);
  const legend = el('div', 'diff-legend');
  legend.append(legendMark('removed', 'A', `${L} only`), legendMark('added', 'B', `${R} only`),
    legendMark('changed', 'A→B', 'both, differently'));
  legend.append(el('span', null, 'A = ' + L + ', B = ' + R + '. Marks appear on the rows when you open them.'));
  out.append(legend);
  if (d.truncated) {
    out.append(el('div', 'note-status',
      'The comparison hit its row cap — these sessions differ on more rows than can be opened at once.'));
  }
}

function legendMark(kind, glyph, text) {
  const s = el('span');
  s.append(el('span', 'diff-mark diff-mark-' + kind, glyph), ' ', el('b', null, text));
  return s;
}

/* Into the grid: the table filtered to the differing rows (all kinds, or
   one), every one of them marked with which session tagged it. The
   filter is an ordinary raw `rid IN (…)` so sort, search and the detail
   panel apply, and the banner's Done — or Clear filters — is the way out. */
export async function pivotDiff(d, rec, kind) {
  const rows = {};
  for (const [k] of KINDS) {
    for (const r of rec.rows[k]) rows[r.rid] = { kind: KIND_OF[k], left: r.left, right: r.right };
  }
  const rids = (kind ? rec.rows[kind] : Object.values(KINDS).flatMap(([k]) => rec.rows[k])).map((r) => r.rid);
  S.diffMarks = { sourceId: rec.sourceId, left: nameOf(d.left), right: nameOf(d.right), rows, kind,
    count: Object.keys(rows).length };
  await openDiffRows(rec.sourceId, rids, kind ? KINDS.find(([k]) => k === kind)[1](nameOf(d.left), nameOf(d.right)) : 'All differences');
}

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
  syncDiffBanner(what, ids.length);
}

/* The banner above the grid while a comparison is pivoted in. Follows the
   view: it shows on the compared table and hides on any other, and Done
   drops the marks and the filter together. */
function syncDiffBanner(what, n) {
  const b = $('diffBanner');
  const dm = S.diffMarks;
  if (!b) return;
  if (!dm || dm.sourceId !== S.sourceId) { b.hidden = true; return; }
  b.replaceChildren();
  b.append(el('span', null, 'Comparing sessions — '), legendMark('removed', 'A', dm.left), legendMark('added', 'B', dm.right));
  if (what) b.append(el('span', 'fb-help', `showing ${what.toLowerCase()} (${(n || 0).toLocaleString()} row${n === 1 ? '' : 's'})`));
  const acts = el('span', 'diff-banner-actions');
  const all = el('button', 'btn ghost', `All differences (${dm.count.toLocaleString()})`);
  all.onclick = () => openDiffRows(dm.sourceId, Object.keys(dm.rows).map(Number), 'All differences');
  const done = el('button', 'btn ghost', 'Done');
  done.title = 'Drop the marks and the row filter';
  done.onclick = async () => {
    S.diffMarks = null;
    S.filterTree = { type: 'group', op: 'AND', children: [] };
    updateFiltersButton();
    renderHead();
    b.hidden = true;
    await rebuildView({ keepScroll: false });
  };
  acts.append(all, done);
  b.append(acts);
  b.hidden = false;
}

// Switching tables hides the banner; coming back shows it again.
document.addEventListener('winnow:viewchange', () => syncDiffBanner());

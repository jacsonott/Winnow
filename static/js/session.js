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
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadSources } from './sources.js';
import { confirmDialog, modal, promptDialog } from './ui.js';

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


function renderDiff(out, d) {
  out.replaceChildren();
  const c = d.counts;
  if (!c.added && !c.removed && !c.changed && !c.note_changes) {
    out.append(el('div', 'note-status', 'No differences — the two agree on every row.'));
  }
  if (d.only_left_sources.length || d.only_right_sources.length) {
    // Not a like-for-like comparison; say so rather than letting the
    // numbers imply the analysts disagreed about rows nobody looked at.
    out.append(el('div', 'note-status',
      'These sessions cover different tables — '
      + `${d.only_left_sources.length} only on the left, ${d.only_right_sources.length} only on the right. `
      + 'Counts below cover what they share.'));
  }
  const groups = [
    ['Added on the right', d.added, 'diff-added'],
    ['Removed on the right', d.removed, 'diff-removed'],
    ['Tagged differently', d.changed, 'diff-changed'],
    ['Notes changed', d.note_changes, 'diff-note'],
  ];
  for (const [label, rows, cls] of groups) {
    if (!rows.length) continue;
    out.append(el('h4', null, `${label} (${rows.length.toLocaleString()})`));
    const tbl = el('table', 'diff-table');
    for (const r of rows.slice(0, 200)) {
      const tr = el('tr', cls);
      tr.append(el('td', null, r.source), el('td', 'diff-rid', `row ${r.rid.toLocaleString()}`),
                el('td', null, side(r.left)),
                el('td', 'diff-arrow', '→'),
                el('td', null, side(r.right)));
      tbl.append(tr);
    }
    out.append(tbl);
    if (rows.length > 200) {
      out.append(el('div', 'fb-help', `Showing the first 200 of ${rows.length.toLocaleString()}.`));
    }
  }
  if (d.truncated) {
    out.append(el('div', 'note-status',
      'The comparison hit its row cap — these sessions differ on more rows than are listed.'));
  }
}

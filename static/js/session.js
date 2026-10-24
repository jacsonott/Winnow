/* Sessions: named snapshots of the analysis layer, stored IN the case file.

   A session is every table's tags and notes at a moment in time — open in
   a tab or not (Store.export_case_session walks every source).
   They live in the case's own `sessions` table, so work an analyst saved —
   or received from a colleague and adopted — travels with the .db instead
   of being left behind in a folder beside it. A FILE is produced only to
   hand work to someone else.

   The diff view is why this earns a panel rather than two menu entries:
   comparing a handed-over session against the live case is how a review
   gets QC'd, and it answers the three questions a reviewer actually has —
   what did they add, what did they drop, and where did we disagree.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, caseUrl, el, post, setBusy, toast } from './core.js';
import { diffMarkNode } from './grid.js';
import { loadSources, openSource, replaceFilters } from './sources.js';
import { S } from './state.js';
import { countCell, keepingFocus, plural } from './tables.js';
import { tableMenuField, tableMenuStat } from './timeframe.js';
import { confirmDialog, contextMenu, dropdownMenu, modal, promptDialog, sectionPanes } from './ui.js';

const LIVE = '__live__';

function fmtWhen(iso) {
  return (iso || '').replace('T', ' ').slice(0, 16);
}

/* The Sessions dialog, in the Tables manager's frame: the case's saved
   sessions and the comparison down a side list, the chosen one on the
   right, and along the foot the ways a session is made, Save current
   work the main one. A session's actions are in its ⋯ (they were four
   buttons on every row); its name opens the comparison against the
   current work, which is the question a saved session is usually opened
   with, and the one click that changes nothing. */
export function openSessionManager(section = null) {
  // What both panes draw from. `left`/`right` are the comparison's sides:
  // null until chosen, then a session name or LIVE.
  const st = { sessions: [], loaded: false, error: '', query: '', left: null, right: null, diff: null, diffError: '' };

  modal('Sessions', (b) => {
    b.dataset.kind = 'sessions';
    const changed = () => frame.pane.dispatchEvent(new CustomEvent('sessions:changed', { bubbles: true }));

    async function refresh() {
      try {
        st.sessions = (await api('/api/case_sessions')).sessions;
        st.error = '';
      } catch (e) { st.error = e.message; }
      st.loaded = true;
      // A side that was renamed or deleted meanwhile falls back to the
      // defaults, rather than comparing against a session that is gone.
      const have = (v) => v === LIVE || st.sessions.some((s) => s.name === v);
      if (st.left && !have(st.left)) st.left = null;
      if (st.right && !have(st.right)) st.right = null;
      frame.update();
    }

    /* The comparison this is usually opened for, when nothing is chosen:
       a comparison already pivoted in keeps its two sides; otherwise the
       newest session (the list comes newest first) against the live work. */
    function sides() {
      const dm = S.diffMarks;
      const toValue = (name) => (name === LIVE_LABEL ? LIVE : name);
      if (!st.left && dm) { st.left = toValue(dm.left); st.right = toValue(dm.right); }
      if (!st.left) st.left = st.sessions[0] ? st.sessions[0].name : LIVE;
      if (!st.right || st.right === st.left) st.right = st.left === LIVE ? (st.sessions[0] || {}).name || LIVE : LIVE;
      return [st.left, st.right];
    }

    let diffSeq = 0;
    async function runCompare() {
      const [l, r] = sides();
      if (l === r) { st.diff = null; st.diffError = ''; frame.update(); return; }
      const seq = ++diffSeq;
      // The last answer is about the sides that were: not shown under these.
      st.diff = null;
      st.diffError = 'Comparing…';
      frame.update();
      let d;
      try {
        d = await api(`/api/case_sessions/diff?left=${encodeURIComponent(l)}&right=${encodeURIComponent(r)}`);
      } catch (e) {
        if (seq !== diffSeq) return;
        st.diff = null;
        st.diffError = 'Could not compare: ' + e.message;
        frame.update();
        return;
      }
      if (seq !== diffSeq) return;
      st.diff = d;
      st.diffError = '';
      frame.update();
    }

    function compareWith(name) {
      st.left = name;
      st.right = LIVE;
      st.diff = null;
      frame.show('compare');   // the pane compares as it is built
    }

    async function loadSession(s) {
      if (!(await confirmDialog(
        `Load "${s.name}"?\n\nThis replaces the tags and notes currently in the case. `
        + 'Save your current work first if you want to keep it.',
        { okLabel: 'Load' }))) return;
      setBusy(true);
      let res;
      try {
        res = await api(`/api/case_sessions/${encodeURIComponent(s.name)}/load?merge=false`, { method: 'POST' });
      } catch (e) {
        toast(`Could not load "${s.name}": ${e.message}`, 6000);
        refresh();
        return;
      } finally { setBusy(false); }
      await loadSources();
      (res.warnings || []).forEach((w) => toast(w, 6000));
      $('modal').hidden = true;
      toast(`Loaded "${s.name}" · ${res.tags_applied.toLocaleString()} tag assignments`);
    }

    async function renameSession(s) {
      const n = await promptDialog('New name:', s.name);
      if (!n || !n.trim() || n.trim() === s.name) return;
      try {
        await post(`/api/case_sessions/${encodeURIComponent(s.name)}/rename`, { name: n.trim() });
      } catch (e) { toast(e.message, 5000); return; }
      if (st.left === s.name) st.left = n.trim();
      if (st.right === s.name) st.right = n.trim();
      refresh();
    }

    async function deleteSession(s) {
      if (!(await confirmDialog(`Delete session "${s.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
      try {
        await api(`/api/case_sessions/${encodeURIComponent(s.name)}`, { method: 'DELETE' });
      } catch (e) { toast(`Could not delete "${s.name}": ${e.message}`, 6000); return; }
      refresh();
    }

    const sessionMenu = (s) => [
      { label: 'Compare with current work', onclick: () => compareWith(s.name) },
      { label: 'Load…', title: 'Replace the current tags and notes with this session', onclick: () => loadSession(s) },
      { label: 'Download', title: 'Save as a file to send to another analyst',
        onclick: () => { window.location = caseUrl(`/api/case_sessions/${encodeURIComponent(s.name)}/download`); } },
      '-',
      { label: 'Rename…', onclick: () => renameSession(s) },
      { label: 'Delete…', onclick: () => deleteSession(s) },
    ];

    /* Saved sessions: the Every table pane's shape — a filter on top, then
       the box that scrolls, with the Tables manager's number columns. Work
       received from another analyst is listed under its own heading: it is
       someone else's conclusions, not yours (it was a "received" badge). */
    function buildSavedPane(pane) {
      const top = el('div', 'tm-cols-top');
      const search = el('input', 'side-search');
      search.type = 'search';
      search.placeholder = 'Filter sessions…';
      search.setAttribute('aria-label', 'Filter sessions');
      search.value = st.query;
      const count = el('span', 'tm-cols-count');
      top.append(search, count);

      const box = el('div', 'side-preview tm-cols-box tables-box sessions-box');
      const head = el('div', 'tables-head sessions-head');
      head.append(el('span', 'tables-name-h', 'Session'), el('span', 'tables-num', 'Tagged'),
        el('span', 'tables-num', 'Notes'), el('span', 'tables-num', 'Tables'), el('span', 'sessions-saved-h', 'Saved'), el('span'));
      const scroller = el('div', 'side-preview-rows');
      const list = el('div', 'tables-list');
      scroller.append(list);
      const foot = el('div', 'side-preview-foot');
      // What a session holds, which the list can't show: every table's
      // tags and notes, not only the tables open as tabs.
      foot.append(el('p', 'fb-help', 'A session is every table’s tags and notes, open or not, kept in the case file.'),
        el('p', 'fb-help', 'Click a name to compare it with your current work. Load, in its ⋯, replaces the tags and notes.'));
      box.append(head, scroller, foot);
      pane.append(top, box);

      function row(s) {
        const r = el('div', 'tables-row sessions-row');
        r.dataset.id = s.name;
        const cell = el('span', 'tables-name-cell');
        const name = el('button', 'tables-name', s.name);
        name.type = 'button';
        name.dataset.role = 'name';
        name.title = `Compare “${s.name}” with your current work`;
        name.onclick = () => compareWith(s.name);
        cell.append(name);
        r.append(cell, countCell(s.tagged_rows, 'tables-tagged'), countCell(s.notes, 'tables-notes'),
          countCell(s.source_count, 'tables-rows'), el('span', 'sessions-saved', fmtWhen(s.saved_at)));
        const more = el('button', 'btn ghost row-more tables-more', '⋯');
        more.dataset.role = 'more';
        more.title = 'Compare, load, download, rename, delete';
        more.setAttribute('aria-label', `Actions for ${s.name}`);
        more.setAttribute('aria-haspopup', 'menu');
        more.setAttribute('aria-expanded', 'false');
        more.onclick = () => dropdownMenu(more, () => sessionMenu(s));
        r.append(more);
        r.addEventListener('contextmenu', (e) => { e.preventDefault(); contextMenu(e, sessionMenu(s)); });
        return r;
      }
      const groupHead = (label, list) => {
        const h = el('div', 'tables-group');
        const tagged = list.reduce((n, s) => n + (s.tagged_rows || 0), 0);
        h.append(el('span', 'tables-group-name', label),
          el('span', 'tables-group-meta', `${plural(list.length, 'session')} · ${tagged.toLocaleString()} tagged`));
        return h;
      };

      function paint() {
        const q = st.query.trim().toLowerCase();
        const shown = st.sessions.filter((s) => !q || s.name.toLowerCase().includes(q));
        count.textContent = q ? `${shown.length} of ${plural(st.sessions.length, 'session')}` : plural(st.sessions.length, 'session');
        keepingFocus(list, () => {
          const empty = (text) => [el('p', 'fb-help tables-empty', text)];
          let nodes;
          if (!st.loaded) nodes = empty('Loading…');
          else if (st.error) nodes = empty('Could not load sessions: ' + st.error);
          else if (!st.sessions.length) nodes = empty('No sessions saved in this case yet. Save current work… below makes one.');
          else if (!shown.length) nodes = empty(`No session matches “${st.query.trim()}”.`);
          else {
            const mine = shown.filter((s) => s.origin !== 'imported');
            const received = shown.filter((s) => s.origin === 'imported');
            nodes = [];
            if (received.length && mine.length) nodes.push(groupHead('Saved here', mine));
            nodes.push(...mine.map(row));
            if (received.length) nodes.push(groupHead('Received from another analyst', received), ...received.map(row));
          }
          list.replaceChildren(...nodes);
        });
      }
      search.oninput = () => { st.query = search.value; paint(); };
      search.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ''; st.query = ''; paint(); }
      });
      paint();
      return { update: paint };
    }

    /* Compare: the two sides as fields on top, compared as soon as either
       changes; the totals as the builders' figures; then one line per
       table, its counts buttons that open those rows in the grid, marked.
       A comparison already pivoted in is said first, with the way out. */
    function buildComparePane(pane) {
      const top = el('div', 'sessions-compare-top');
      const left = el('select');
      const right = el('select');
      left.setAttribute('aria-label', 'Compare from');
      right.setAttribute('aria-label', 'Compare to');
      const swap = el('button', 'btn ghost sessions-swap', '⇄');
      swap.type = 'button';
      swap.title = 'Swap the two sides';
      top.append(tableMenuField('From · A', left), swap, tableMenuField('To · B', right));
      const applied = el('ul', 'mb-notes sessions-applied');
      const summary = el('div', 'mb-summary');
      const stats = el('div', 'mb-stats');
      summary.append(stats);
      const notes = el('ul', 'mb-notes');
      const box = el('div', 'side-preview tm-cols-box tables-box sessions-diff-box');
      const head = el('div', 'tables-head sessions-diff-head');
      const scroller = el('div', 'side-preview-rows');
      const list = el('div', 'tables-list');
      scroller.append(list);
      const foot = el('div', 'side-preview-foot');
      box.append(head, scroller, foot);
      pane.append(top, applied, summary, notes, box);

      const pick = () => {
        st.left = left.value;
        st.right = right.value;
        changed();
        runCompare();
      };
      left.onchange = pick;
      right.onchange = pick;
      swap.onclick = () => { [st.left, st.right] = [st.right, st.left]; paint(); changed(); runCompare(); };

      function fillSides() {
        const [l, r] = sides();
        for (const [sel, value] of [[left, l], [right, r]]) {
          const opts = [new Option(`${LIVE_LABEL} (live)`, LIVE), ...st.sessions.map((s) => new Option(s.name, s.name))];
          sel.replaceChildren(...opts);
          sel.value = value;
        }
      }

      function paintApplied() {
        const dm = S.diffMarks;
        applied.replaceChildren();
        applied.hidden = !dm;
        if (!dm) return;
        const src = (S.sources || []).find((x) => x.id === dm.sourceId);
        const li = el('li', 'mb-note ok sessions-applied-line');
        const text = el('span', 'sessions-applied-text');
        text.append(el('b', null, 'Comparison applied'), ` — ${dm.what.toLowerCase()} between `,
          diffMarkNode('removed'), ' ', el('b', null, dm.left), ' and ', diffMarkNode('added'), ' ', el('b', null, dm.right),
          src ? ` in ${src.name}` : '', ` · ${plural(dm.n, 'row')}`);
        const all = el('button', 'btn ghost mb-small', `All differences · ${Object.keys(dm.rows).length.toLocaleString()}`);
        all.onclick = () => openDiffRows(dm.sourceId, Object.keys(dm.rows).map(Number),
          { left: dm.left, right: dm.right, rows: dm.rows, what: 'All differences' });
        const clear = el('button', 'btn ghost mb-small', 'Clear');
        clear.title = 'Drop the marks and the row filter — the same as the banner’s Done';
        clear.onclick = async () => { const done = clearDiff(); paint(); changed(); await done; };
        li.append(text, all, clear);
        applied.append(li);
      }

      function paint() {
        fillSides();
        paintApplied();
        const d = st.diff;
        const [l, r] = sides();
        const L = nameOf(l), R = nameOf(r);
        // The heads say A and B, as the marks on the rows will; the
        // figures above them spell the two names out.
        head.replaceChildren(el('span', 'tables-name-h', 'Table'),
          ...KINDS.map(([kind, label, why]) => { const h = el('span', 'tables-num', HEADS[kind]); h.title = `${label(L, R)}: ${why}`; return h; }),
          el('span', 'tables-num', 'All'));
        const total = (k) => (d ? d.counts[k] : null);
        stats.replaceChildren(...KINDS.map(([kind, label]) => tableMenuStat(total(kind) == null ? '—' : total(kind), label(L, R))));
        notes.replaceChildren();
        foot.replaceChildren();
        if (!d) {
          list.replaceChildren(el('p', 'fb-help tables-empty', l === r ? 'Pick two different sides.' : (st.diffError || 'Comparing…')));
          foot.append(el('p', 'fb-help', 'Tags are matched by name, so a session from another analyst’s case compares correctly.'));
          return;
        }
        if (d.only_left_sources.length || d.only_right_sources.length) {
          notes.append(el('li', 'mb-note warn',
            `These cover different tables — ${d.only_left_sources.length} only in ${L}, ${d.only_right_sources.length} only in ${R}. `
            + 'Every tagged row and note in a table only one side has counts as a difference.'));
        }
        const dup = d.duplicate_tag_names || [];
        if (dup.length) {
          notes.append(el('li', 'mb-note warn',
            `${dup.map((n) => `“${n}”`).join(', ')} ${dup.length === 1 ? 'is the name' : 'are names'} of more than one tag `
            + 'on one side. A row moved between tags of the same name doesn’t show as a difference.'));
        }
        if (d.truncated) notes.append(el('li', 'mb-note', 'The comparison hit its row cap: a count can open only its first rows.'));
        const c = d.counts;
        if (!c.added && !c.removed && !c.changed && !c.note_changes) {
          list.replaceChildren(el('p', 'fb-help tables-empty', dup.length ? 'No differences between tag names.' : 'No differences — the two agree on every row.'));
        } else list.replaceChildren(...diffRows(d, L, R));
        const legend = el('div', 'diff-legend sessions-legend');
        legend.append(legendMark('removed', `${L} only`), legendMark('added', `${R} only`), legendMark('changed', 'both, differently'));
        foot.append(legend);
      }
      paint();
      // After the build returns: runCompare repaints through the frame,
      // which can't update a pane it is still building.
      if (!st.diff && st.loaded) queueMicrotask(runCompare);
      return { update: paint };
    }

    const frame = sectionPanes({
      sections: [
        { id: 'saved', group: 'In this case', title: 'Saved sessions', build: buildSavedPane,
          hint: () => (!st.loaded ? 'Loading…' : st.sessions.length
            ? `${plural(st.sessions.length, 'session')} · newest ${fmtWhen(st.sessions[0].saved_at).slice(5, 10)}` : 'None saved yet') },
        { id: 'compare', group: 'Review', title: 'Compare', build: buildComparePane,
          hint: () => (S.diffMarks ? `Applied · ${plural(S.diffMarks.n, 'row')}`
            : st.left && st.right ? `${shortName(nameOf(st.left))} → ${shortName(nameOf(st.right))}` : 'Two sessions, or one and your work') },
      ],
      label: 'Sessions sections', cls: 'list-panes sessions-manager', paneCls: 'list-pane sessions-pane', changedEvent: 'sessions:changed',
    });
    b.append(frame.panes);

    // The ways a session is made: receiving one, starting over, and
    // saving — the main one, on the right.
    const foot = el('div', 'table-menu-foot table-menu-strip');
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

    const fresh = el('button', 'btn ghost', 'Start a fresh pass…');
    fresh.title = 'Save what you have, then clear tags and notes to review the evidence again';
    fresh.onclick = async () => {
      // Saving under a name that is taken REPLACES that session (by design
      // — see Store.save_session), so the suggestion has to be a free one.
      const taken = new Set(st.sessions.map((x) => x.name));
      let n = st.sessions.length + 1;
      while (taken.has(`pass ${n}`)) n += 1;
      const name = await promptDialog(
        'Save the current work as — then start with no tags or notes:', `pass ${n}`);
      if (name === null) return;
      if (!name.trim() && !(await confirmDialog(
        'Clear all tags and notes WITHOUT saving them first?\n\nThis cannot be undone.',
        { danger: true, okLabel: 'Clear anyway' }))) return;
      if (taken.has(name.trim()) && !(await confirmDialog(
        `A session named "${name.trim()}" already exists. Replace it with the current work, `
        + 'then clear the case?\n\nWhat it holds now is lost.',
        { danger: true, okLabel: 'Replace' }))) return;
      setBusy(true);
      let res;
      try { res = await post('/api/case_sessions/new', { save_as: name.trim() || null }); }
      catch (e) { toast('Could not start a fresh pass: ' + e.message, 6000); return; }
      finally { setBusy(false); }
      await loadSources();
      $('modal').hidden = true;
      toast(`Fresh pass started — ${res.tags_cleared.toLocaleString()} tags cleared`
            + (res.saved ? `, saved as "${res.saved.name}"` : ''), 8000);
    };

    const save = el('button', 'btn primary table-menu-close', 'Save current work…');
    save.onclick = async () => {
      const name = await promptDialog('Name this session:');
      if (!name || !name.trim()) return;
      setBusy(true);
      try { await post('/api/case_sessions', { name: name.trim() }); }
      catch (e) { toast(e.message, 5000); return; }
      finally { setBusy(false); }
      toast(`Saved "${name.trim()}" into this case`);
      frame.show('saved');
      refresh();
    };
    foot.append(adopt, fresh, save);
    b.append(foot);
    // A comparison pivoted into the grid opens on Compare, where it is said.
    frame.show(section || (S.diffMarks ? 'compare' : 'saved'));
    refresh().then(() => { if (frame.current() === 'compare' && !st.diff) runCompare(); });
  }, { wide: true, tall: true, focus: (b) => b.querySelector('.side-rail-item[aria-selected="true"]') });
}

/* A side's name in a column head or the side list's line: a session name
   runs long ("Before review 10/22"), and the head has 70px. */
const shortName = (name) => (name.length > 14 ? name.slice(0, 13) + '…' : name);

/* The comparison's lines, one per table. A count is a button: the rows
   behind it open in the grid, marked with which session tagged them —
   the pane never lists rows itself. The counts come from the server's
   per-table tally, which is not capped; the rows a count opens are. */
function diffRows(d, L, R) {
  const keyOf = (r) => (r.source_id != null ? `id:${r.source_id}` : `name:${r.source}`);
  const rowsOf = new Map();
  for (const [kind] of KINDS) {
    for (const r of d[kind]) {
      const rec = rowsOf.get(keyOf(r)) || { removed: [], added: [], changed: [], note_changes: [] };
      rec[kind].push(r);
      rowsOf.set(keyOf(r), rec);
    }
  }
  const cell = (n, cls, onclick, title) => {
    if (!n || !onclick) return countCell(n, cls);
    const b = el('button', 'tables-num some sessions-n ' + cls, n.toLocaleString());
    b.type = 'button';
    b.title = title;
    b.onclick = onclick;
    return b;
  };
  return d.sources.map((src) => {
    const rec = { sourceId: src.source_id, name: src.source,
      rows: rowsOf.get(src.source_id != null ? `id:${src.source_id}` : `name:${src.source}`)
        || { removed: [], added: [], changed: [], note_changes: [] } };
    const r = el('div', 'tables-row sessions-diff-row');
    const name = el('span', 'tables-name-cell');
    name.append(el('span', 'tables-name static', src.source));
    if (src.source_id == null) name.append(el('span', 'tables-sub', 'not in this case'));
    r.append(name);
    const openable = (n, have) => (n === have ? `Open these ${n.toLocaleString()} rows in the table, marked`
      : `Open the first ${have.toLocaleString()} of these ${n.toLocaleString()} rows — the comparison hit its row cap`);
    let all = 0;
    for (const [kind, label] of KINDS) {
      const n = src.counts[kind];
      all += n;
      const have = rec.rows[kind].length;
      r.append(cell(n, 'diff-n-' + kind, have && src.source_id != null ? () => pivotDiff(d, rec, kind, label(L, R)) : null,
        openable(n, have)));
    }
    const haveAll = new Set(Object.values(rec.rows).flat().map((x) => x.rid)).size;
    r.append(cell(all, 'diff-n-all', haveAll && src.source_id != null ? () => pivotDiff(d, rec, null, 'All differences') : null,
      'Open every differing row in this table'));
    return r;
  });
}

const LIVE_LABEL = 'Current work';
const KINDS = [
  ['removed', (l, r) => `Only in ${l}`, 'tagged on the left, not on the right'],
  ['added', (l, r) => `Only in ${r}`, 'tagged on the right, not on the left'],
  ['changed', () => 'Tagged differently', 'tagged on both sides, with different tags'],
  ['note_changes', () => 'Notes changed', 'a note that differs'],
];

const nameOf = (v) => (v === LIVE ? LIVE_LABEL : v);
const HEADS = { removed: 'A only', added: 'B only', changed: 'Differ', note_changes: 'Notes' };

function legendMark(kind, text) {
  const s = el('span');
  s.append(diffMarkNode(kind), ' ', el('b', null, text));
  return s;
}

/* Into the grid: the table filtered to the differing rows (all kinds, or
   one), every one of them marked with which session tagged it. One row
   can differ in its tags and its note; the mark carries both. The filter
   is an ordinary `rid IN` condition, so sort, search and the detail panel
   apply, and the banner's Done — or Clear filters — is the way out. */
export async function pivotDiff(d, rec, kind, what) {
  const rows = {};
  const facet = (k) => (k === 'note_changes' ? 'note' : 'tags');
  for (const [k] of KINDS) {
    for (const r of rec.rows[k]) {
      const m = rows[r.rid] || (rows[r.rid] = { tags: null, note: null });
      m[facet(k)] = { left: r.left, right: r.right };
    }
  }
  const rids = (kind ? rec.rows[kind] : KINDS.flatMap(([k]) => rec.rows[k])).map((r) => r.rid);
  await openDiffRows(rec.sourceId, rids, { left: nameOf(d.left), right: nameOf(d.right), rows, what });
}

export async function openDiffRows(sourceId, rids, marks) {
  if (!(S.sources || []).some((s) => s.id === sourceId)) { toast('That table is no longer in this case'); return; }
  const ids = [...new Set(rids.map(Number).filter(Number.isFinite))];
  if (!ids.length) return;
  $('modal').hidden = true;
  // Already on the table: no need to re-open it (and re-apply what it had).
  if (S.sourceId !== sourceId || S.activeTab !== 'grid') await openSource(sourceId);
  // What Done lands on — the filter the table had before the pivot.
  const prevTree = (S.diffMarks && S.diffMarks.sourceId === sourceId) ? S.diffMarks.prevTree : S.filterTree;
  S.diffMarks = { sourceId, ...marks, n: ids.length, prevTree };
  // The counts this came from are computed over the whole table and know
  // nothing about the timeframe, so leaving it on would show fewer rows
  // than the number promised. Cleared, and said out loud: it is global.
  const hadTimeframe = S.timeRange && S.timeRange.enabled;
  await replaceFilters({ type: 'cond', column: 'rid', op: 'in', value: ids.map(String) },
                       { clearTimeframe: true });
  if (hadTimeframe) toast('Timeframe filter turned off — these are all the differing rows', 4000);
}

/* The one way out of a pivoted comparison, shared by the banner's Done
   and the Sessions panel's Clear: drop the marks, land on the filter the
   table had before. */
export async function clearDiff() {
  const dm = S.diffMarks;
  if (!dm) return;
  // The Sessions panel's Clear can be pressed from any table; the filter
  // being restored belongs to the COMPARED one, so land there first (the
  // same step openDiffRows takes), or another table's filters would be
  // overwritten and the compared table left on its rid filter with no
  // marks and no banner.
  // Marks go first, synchronously: anything repainting from S.diffMarks
  // (the Sessions panel's status line) sees the cleared state before the
  // view rebuild lands, rather than racing it.
  S.diffMarks = null;
  if (!(S.sources || []).some((s) => s.id === dm.sourceId)) return;
  if (S.sourceId !== dm.sourceId || S.activeTab !== 'grid') {
    $('modal').hidden = true;
    await openSource(dm.sourceId);
  }
  await replaceFilters(dm.prevTree || { type: 'group', op: 'AND', children: [] });
}

/* The banner above the grid while a comparison is pivoted in. Drawn from
   S.diffMarks alone, so every caller shows the same thing: it appears on
   the compared table's grid and hides anywhere else (another table, the
   SQL tab), and Done drops the marks and lands on the filter the table
   had before. */
let bannerFor = null;   // the S.diffMarks the banner was last drawn from
export function syncDiffBanner() {
  const b = $('diffBanner');
  const dm = S.diffMarks;
  if (!b) return;
  if (!dm || dm.sourceId !== S.sourceId || S.activeTab !== 'grid') { b.hidden = true; return; }
  if (bannerFor === dm) { b.hidden = false; return; }   // same pivot: nothing to redraw
  bannerFor = dm;
  b.replaceChildren();
  const side = (kind, name) => { const s = el('span'); s.append(diffMarkNode(kind), ' ', el('b', null, name)); return s; };
  b.append(el('span', null, 'Comparing sessions — '), side('removed', dm.left), side('added', dm.right));
  b.append(el('span', 'fb-help', `showing ${dm.what.toLowerCase()} (${dm.n.toLocaleString()} row${dm.n === 1 ? '' : 's'})`));
  const acts = el('span', 'diff-banner-actions');
  const total = Object.keys(dm.rows).length;
  const all = el('button', 'btn ghost', `All differences (${total.toLocaleString()})`);
  all.onclick = () => openDiffRows(dm.sourceId, Object.keys(dm.rows).map(Number),
    { left: dm.left, right: dm.right, rows: dm.rows, what: 'All differences' });
  const done = el('button', 'btn ghost', 'Done');
  done.title = 'Drop the marks and the row filter';
  done.onclick = () => clearDiff();
  acts.append(all, done);
  b.append(acts);
  b.hidden = false;
}

/* Wired from main.js: the banner follows the view — a rebuild, a cached
   re-open — so it shows over the compared table and nowhere else. */
export function wireSession() {
  document.addEventListener('winnow:viewchange', () => syncDiffBanner());
}

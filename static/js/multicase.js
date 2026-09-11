/* Across cases — one modal over the read-only cross-case routes.

   An intrusion spans hosts and a case is one host, so the questions that
   matter most are the ones a single case cannot answer: where else did
   this IOC land, show me these machines on one timeline, query two
   collections at once. Everything here is READ-ONLY by construction
   (winnow/multicase.py): the open case keeps its lock and its writer, the
   others are opened mode=ro. Acting on a hit means opening it in the case
   that owns it, which is what every row here offers.

   WITHDRAWN FROM THE HEADER, September 2026. The feature isn't finished
   enough to sit beside Search all: the three panes answer three
   different questions with three different result shapes, and the
   sweep's output stops short of the thing an analyst does next (tag the
   hits, pivot to the case). The routes, this module and its UI test all
   stay — openMultiCase() is still reachable via __winnow for whoever
   picks the redesign up — but nothing in index.html points here until
   it earns the space back. */
import { $, api, el, post, toast } from './core.js';
import { openCase } from './home.js';
import { markModalAction, modal } from './ui.js';

let picked = new Set();        // case paths, remembered while the modal is open
let mode = 'sweep';

function caseRow(c) {
  const row = el('label', 'mc-case');
  const cb = el('input');
  cb.type = 'checkbox';
  cb.checked = picked.has(c.path);
  cb.disabled = !c.exists;
  cb.onchange = () => { cb.checked ? picked.add(c.path) : picked.delete(c.path); };
  const name = el('span', 'mc-case-name', c.name);
  const note = el('span', 'mc-case-note',
    !c.exists ? 'file missing' : (c.is_open ? 'open here — read from this session' : (c.group || '')));
  row.append(cb, name, note);
  return row;
}

/* A hit or a timeline row: the case, where in it, and the way back. */
function resultRow(r, { time = false } = {}) {
  const row = el('div', 'mc-hit' + (time ? ' mc-hit-time' : ''));
  if (time) row.append(el('span', 'mc-when', r.when || ''));
  row.append(el('span', 'mc-case-tag', r.case));
  row.append(el('span', 'mc-src', `${r.source} · rid ${r.rid}`));
  const summary = time ? r.summary : Object.entries(r.cells || {})
    .map(([k, v]) => `${k}=${v}`).join('  ');
  row.append(el('span', 'mc-summary', summary));
  const go = el('button', 'btn ghost mc-open', 'Open');
  go.title = `Open ${r.case} at this row — tags and notes live in the case that owns it`;
  go.onclick = async () => {
    try {
      $('modal').hidden = true;
      await openCase(r.path);
      await api(`/api/source/${r.source_id}/open`, { method: 'POST' }).catch(() => {});
      toast(`Opened ${r.case} — ${r.source}, row ${r.rid}`, 5000);
    } catch (e) { toast('Could not open that case: ' + e.message, 6000); }
  };
  row.append(go);
  return row;
}

export function openMultiCase() {
  markModalAction('openMultiCase');
  modal('Across cases', async (b) => {
    b.append(el('p', 'fb-help',
      'Reads your other cases without opening them — they stay untouched, and a case another '
      + 'Winnow has open is still readable. Everything here is read-only: to tag or annotate a '
      + 'row, open it in the case that owns it.'));

    const caseBox = el('div', 'mc-cases');
    b.append(el('div', 'settings-sub-label', 'Cases'), caseBox);

    let budget = 8;
    try {
      const info = await api('/api/multicase/cases');
      budget = info.attach_budget;
      if (!info.cases.length) caseBox.append(el('div', 'note-status', 'No cases registered yet.'));
      // Default to everything that exists, which is the sweep's best default.
      if (!picked.size) for (const c of info.cases) if (c.exists) picked.add(c.path);
      for (const c of info.cases) caseBox.append(caseRow(c));
    } catch (e) {
      caseBox.append(el('div', 'note-status', 'Could not read the case list: ' + e.message));
    }

    const tabs = el('div', 'row-actions mc-tabs');
    const out = el('div', 'mc-out');
    const controls = el('div', 'mc-controls');
    b.append(tabs, controls, out);

    const status = (msg) => out.replaceChildren(el('div', 'note-status', msg));

    // ---------------------------------------------------------- A: sweep
    async function runSweep(values) {
      status('Looking…');
      try {
        const r = await post('/api/multicase/sweep',
          { paths: [...picked], values: values || null });
        out.replaceChildren();
        out.append(el('div', 'fb-help',
          `${r.total_hits} hit${r.total_hits === 1 ? '' : 's'} for ${r.values.length} value`
          + `${r.values.length === 1 ? '' : 's'} across ${r.cases.length} case`
          + `${r.cases.length === 1 ? '' : 's'}`));
        for (const c of r.cases) {
          if (c.error) { out.append(el('div', 'mc-case-err', `${c.case}: ${c.error}`)); continue; }
          if (!c.hits.length) continue;
          out.append(el('div', 'mc-case-head',
            `${c.case} — ${c.hits.length}${c.truncated ? '+' : ''}`));
          for (const h of c.hits) out.append(resultRow(h));
        }
        if (!r.total_hits) out.append(el('div', 'note-status', 'Nothing found in the other cases.'));
      } catch (e) { status('Could not sweep: ' + e.message); }
    }

    // ------------------------------------------------------------- B: sql
    async function runSql(sql) {
      status('Running…');
      try {
        const r = await post('/api/multicase/sql', { paths: [...picked], sql });
        out.replaceChildren();
        out.append(el('div', 'fb-help',
          r.cases.map((c) => `${c.alias} = ${c.name}`).join(' · ')
          + ` — ${r.rows.length}${r.truncated ? '+' : ''} rows in ${r.elapsed_ms}ms`));
        const table = el('table', 'mc-table');
        const head = el('tr');
        for (const c of r.columns) head.append(el('th', null, c));
        table.append(head);
        for (const row of r.rows) {
          const tr = el('tr');
          for (const cell of row) tr.append(el('td', null, cell == null ? '' : String(cell)));
          table.append(tr);
        }
        out.append(table);
      } catch (e) { status(e.message); }
    }

    // -------------------------------------------------------- C: timeline
    async function runTimeline(start, end) {
      status('Building…');
      try {
        const r = await post('/api/multicase/timeline', { paths: [...picked], start, end });
        out.replaceChildren();
        out.append(el('div', 'fb-help',
          `${r.rows.length}${r.truncated ? ` of ${r.total_seen}+` : ''} rows`
          + (r.errors.length ? ` · ${r.errors.length} case(s) unreadable` : '')));
        for (const row of r.rows) out.append(resultRow(row, { time: true }));
        if (!r.rows.length) out.append(el('div', 'note-status',
          'Nothing in that window — these cases may have no datetime columns.'));
      } catch (e) { status('Could not build it: ' + e.message); }
    }

    function paint() {
      controls.replaceChildren();
      out.replaceChildren();
      if (mode === 'sweep') {
        controls.append(el('p', 'fb-help',
          'Blank uses this case’s watchlist — the IOCs you have already written down.'));
        const box = el('textarea', 'mc-values');
        box.rows = 2;
        box.placeholder = 'One value per line (blank = this case’s watchlist)';
        const go = el('button', 'btn', 'Sweep');
        go.onclick = () => runSweep(box.value.split('\n').map((v) => v.trim()).filter(Boolean));
        controls.append(box, go);
        controls.dataset.mode = 'sweep';
      } else if (mode === 'sql') {
        const box = el('textarea', 'mc-sql');
        box.rows = 4;
        box.placeholder = 'SELECT * FROM c1.src_1 LIMIT 50';
        const go = el('button', 'btn', 'Run');
        go.onclick = () => runSql(box.value);
        const schema = el('button', 'btn ghost', 'Show schema');
        schema.title = 'Cases attach as c1, c2… — they are schema names, so tables still need their own alias';
        schema.onclick = async () => {
          try {
            const r = await post('/api/multicase/schema', { paths: [...picked] });
            out.replaceChildren(el('pre', 'mc-schema', r.schema));
          } catch (e) { status(e.message); }
        };
        const acts = el('div', 'row-actions');
        acts.append(go, schema);          // el(tag, cls, TEXT) — children get appended
        const note = el('p', 'fb-help',
          `Up to ${budget} cases at once, attached as c1, c2… in the order listed above.`);
        controls.append(note, box, acts);
        controls.dataset.mode = 'sql';
        // SQLite attaches at most 10 databases, so this is the one panel
        // with a case limit. Say so before the query fails, and say which
        // ones would be used rather than making them count checkboxes.
        if (picked.size > budget) {
          note.classList.add('mc-over-budget');
          note.textContent = `${picked.size} cases selected — a query can attach at most ${budget}. `
            + 'Deselect some above, or the run will be refused.';
        }
      } else {
        const from = el('input', 'confirm-input');
        from.placeholder = 'from  YYYY-MM-DD HH:MM:SS';
        const to = el('input', 'confirm-input');
        to.placeholder = 'to';
        const go = el('button', 'btn', 'Build');
        go.onclick = () => runTimeline(from.value.trim(), to.value.trim());
        const acts = el('div', 'row-actions');
        acts.append(from, to, go);
        controls.append(el('p', 'fb-help',
          'Every case’s first datetime column, interleaved in time order.'), acts);
        controls.dataset.mode = 'timeline';
      }
    }

    // Switching tabs repaints the controls in place. Re-opening the modal
    // would re-fetch the case list and lose the analyst's selection, for a
    // change that only affects the panel below it.
    const tabButtons = [];
    for (const [id, label] of [['sweep', 'IOC sweep'], ['sql', 'Query'], ['timeline', 'Timeline']]) {
      const t = el('button', 'btn', label);
      t.dataset.mode = id;
      t.onclick = () => {
        mode = id;
        for (const b2 of tabButtons) b2.className = 'btn' + (b2.dataset.mode === mode ? '' : ' ghost');
        paint();
      };
      tabButtons.push(t);
      tabs.append(t);
    }
    for (const t of tabButtons) t.className = 'btn' + (t.dataset.mode === mode ? '' : ' ghost');
    paint();
  }, { wide: 'x' });
}

/* Across cases — the questions one case can't answer, asked of every case
   the analyst works, on the backend the open case already uses.

   An intrusion spans hosts and a case is one host. Three questions need
   several at once: where else did these values land (Where else), what
   happened on these machines in what order (Findings), and the one a
   query can ask (Query). The server reads every other case as a VISITOR —
   a Store opened read-only (winnow/acrosscases.py) — so each case answers
   the way its own Search all and Timeline would: its own trigram index,
   the Timeline's configured types and shipped summaries, its tags by name.

   ONE RESULT SHAPE. Every answer is a list of Timeline lines carrying the
   case they live in, painted by acrossLine() with the Timeline's own row
   classes, so a sweep hit, a finding and a query row read the same. And
   every line leads somewhere: click it for the whole row and its case's
   tags (a chip tags it IN ITS OWN CASE — /api/across/tag, refused by name
   when another Winnow has that case open), or Open ↦ to land on the row in
   its case. This replaces the withdrawn multicase.js (#228), which still
   answers __winnow.openMultiCase() until it is removed. */
import { $, api, el, post, toast } from './core.js';
import { renderAdvancedChips, updateSearchHint } from './filters.js';
import { render } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { openCase } from './home.js';
import { announceChange } from './popout.js';
import { syncSearchExpansion } from './search.js';
import { clearViewNarrowing, loadSources } from './sources.js';
import { gridIsShowing } from './sql.js';
import { S } from './state.js';
import { clearRowCaches, refreshTagCounts } from './tags.js';
import { TIMELINE_LONG_GAP, jumpToTimelineRow, timelineGap } from './timeline.js';
import { markModalAction, modal, sectionPanes } from './ui.js';
import { rebuildView } from './view.js';

export const ACROSS_POLL_MS = 400;
// Lines a pane paints before "Show N more" — a dialog list, not the grid.
export const ACROSS_PAINT_STEP = 300;

/* Module state, so the answers outlive the dialog AND a case switch:
   Open ↦ lands in another case, and coming back to Across cases finds
   the same results waiting. `picked` null means every case. */
export const AC = {
  cases: [], picked: null, section: 'sweep',
  values: '', job: null, pollTimer: null,
  tlStart: '', tlEnd: '', timeline: null, tlBusy: false,
  sql: '', query: null, queryAsTable: false, queryBusy: false,
  openKey: null, shown: {}, frame: null,
};

export const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
export const lineKey = (ln) => `${ln.path}|${ln.source_id}|${ln.rid}`;

export function pickedPaths() {
  const live = AC.cases.filter((c) => c.exists).map((c) => c.path);
  return AC.picked ? live.filter((p) => AC.picked.has(p)) : live;
}

export function hereCase() { return AC.cases.find((c) => c.is_open) || null; }

/* A case's color: the --series-N of its place in the case list, so
   WKSTN-014 is the same color on every pane and in every line. */
export function caseSlot(path) {
  const i = AC.cases.findIndex((c) => c.path === path);
  return i < 0 ? 8 : (i % 8) + 1;
}

/* ------------------------------------------------------------ one line */

/* A line, in the Timeline's own row classes (style.css .timeline-row),
   with a column for its case. `story` adds the Gap column, for lines
   that are in time order. */
export function acrossLine(ln, { story = false } = {}) {
  const row = el('div', 'timeline-row ac-line' + (story ? ' ac-story' : ''));
  row.dataset.key = lineKey(ln);
  if (story && ln.new_day) row.classList.add('day-start');
  const gutter = el('div', 'tl-col-gutter');
  for (const t of ln.tags) {
    const stripe = el('span', 'stripe');
    stripe.style.background = t.color || '#888';
    stripe.title = t.name;
    gutter.append(stripe);
  }
  if (ln.note) {
    const mark = el('span', 'tl-note-mark', '✎');
    mark.title = ln.note;
    gutter.append(mark);
  }
  const ts = el('div', 'tl-col-ts', ln.ts || '—');
  ts.title = ln.ts || 'No timestamp';
  const cells = [gutter, ts];
  if (story) {
    const gap = el('div', 'tl-col-gap');
    if (ln.gap != null) {
      gap.textContent = timelineGap(ln.gap);
      if (ln.gap >= TIMELINE_LONG_GAP) { gap.classList.add('long'); if (!ln.new_day) row.classList.add('gap'); }
    }
    cells.push(gap);
  }
  const caseCell = el('div', 'ac-col-case');
  const pill = el('span', 'ac-case-pill', ln.case);
  pill.style.setProperty('--k', `var(--series-${caseSlot(ln.path)})`);
  pill.title = ln.path;
  caseCell.append(pill);
  const typeCell = el('div', 'tl-col-type');
  const badge = el('span', 'type-badge', ln.kind);
  badge.title = ln.source_name;
  typeCell.append(badge);
  const body = el('div', 'tl-col-body');
  body.title = ln.body;
  if (ln.lead || ln.detail) {
    if (ln.lead) body.append(el('span', 'tl-lead', ln.lead));
    if (ln.note) body.append(el('span', 'tl-note', `✎ ${ln.note}`));
    if (ln.detail) body.append(el('span', 'tl-detail', ln.detail));
  } else {
    if (ln.note) body.append(el('span', 'tl-note', `✎ ${ln.note}`));
    body.append(el('span', 'tl-raw', ln.body));
  }
  for (const m of ln.matched || []) body.append(el('span', 'ac-matched', m));
  const go = el('button', 'btn ghost ac-open', 'Open ↦');
  go.type = 'button';
  go.title = `Open ${ln.case} at this row (${ln.source_name}, line ${ln.rid})`;
  go.onclick = (e) => { e.stopPropagation(); openLineInCase(ln); };
  body.append(go);
  row.append(...cells, caseCell, typeCell, body);
  row.onclick = () => toggleDetail(ln, row);
  return row;
}

/* A list of lines with a header row, painted ACROSS_PAINT_STEP at a time. */
export function lineList(lines, { story = false, id = 'list' } = {}) {
  const wrap = el('div', 'ac-lines' + (story ? ' ac-story' : ''));
  const head = el('div', 'timeline-cols ac-cols');
  const cols = [['tl-col-gutter', ''], ['tl-col-ts', 'Timestamp']];
  if (story) cols.push(['tl-col-gap', 'Gap']);
  cols.push(['ac-col-case', 'Case'], ['tl-col-type', 'Type'], ['tl-col-body', 'Body']);
  for (const [cls, label] of cols) head.append(el('div', cls, label));
  wrap.append(head);
  const body = el('div', 'ac-lines-body');
  wrap.append(body);
  const shown = Math.min(lines.length, AC.shown[id] || ACROSS_PAINT_STEP);
  for (const ln of lines.slice(0, shown)) {
    body.append(acrossLine(ln, { story }));
    if (AC.openKey === lineKey(ln)) body.append(detailNode(ln, body.lastChild));
  }
  if (shown < lines.length) {
    const more = el('button', 'btn ghost ac-more', `Show ${plural(Math.min(ACROSS_PAINT_STEP, lines.length - shown), 'more line')} (${(lines.length - shown).toLocaleString()} not shown)`);
    more.onclick = () => { AC.shown[id] = shown + ACROSS_PAINT_STEP; AC.frame && AC.frame.update(); };
    body.append(more);
  }
  return wrap;
}

/* ------------------------------------------------- a line's whole row */

export function toggleDetail(ln, row) {
  const key = lineKey(ln);
  const open = row.nextElementSibling && row.nextElementSibling.classList.contains('ac-detail');
  // One line open in the whole pane, not one per table's list.
  const scope = row.closest('.ac-out') || row.parentElement;
  for (const d of scope.querySelectorAll('.ac-detail')) d.remove();
  for (const r of scope.querySelectorAll('.ac-line.selected')) r.classList.remove('selected');
  if (open && AC.openKey === key) { AC.openKey = null; return; }
  AC.openKey = key;
  row.after(detailNode(ln, row));
}

/* Under a line: its case's tags as chips (pressed when the row carries
   one; a click writes it in the case that owns the row), the way into
   that case, and every column. */
export function detailNode(ln, row) {
  row.classList.add('selected');
  const box = el('div', 'ac-detail');
  box.append(el('div', 'note-status', 'Reading the row…'));
  api(`/api/across/row?path=${encodeURIComponent(ln.path)}&source_id=${ln.source_id}&rid=${ln.rid}`)
    .then((d) => paintDetail(box, ln, row, d))
    .catch((e) => box.replaceChildren(el('div', 'note-status', 'Could not read that row: ' + e.message)));
  return box;
}

export function paintDetail(box, ln, row, d) {
  const held = (AC.cases.find((c) => c.path === ln.path) || {}).held_by;
  const top = el('div', 'ac-detail-top');
  const tags = el('div', 'ac-detail-tags');
  tags.append(el('span', 'ac-detail-label', held ? `Tags in ${ln.case} (read only — open in another Winnow)` : `Tag in ${ln.case}`));
  const have = new Set(d.tags.map((t) => t.id));
  const msg = el('span', 'ac-detail-msg');
  for (const t of d.case_tags) {
    const chip = el('button', 'ac-tag', t.name);
    chip.type = 'button';
    chip.style.setProperty('--tag', t.color || '#888');
    chip.setAttribute('aria-pressed', String(have.has(t.id)));
    chip.disabled = !!held;
    chip.title = held ? `${ln.case} is open in another Winnow (${held}) — tag it there`
      : `${have.has(t.id) ? 'Take' : 'Put'} ${t.name} ${have.has(t.id) ? 'off' : 'on'} this row, in ${ln.case}`;
    chip.onclick = async (e) => {
      e.stopPropagation();
      chip.disabled = true;
      try {
        const after = await post('/api/across/tag', { path: ln.path, source_id: ln.source_id, rid: ln.rid,
                                                       tag_id: t.id, on: !have.has(t.id) });
        ln.tags = after.tags;
        afterTagged(ln);
        const fresh = acrossLine(ln, { story: row.classList.contains('ac-story') });
        fresh.classList.add('selected');
        row.replaceWith(fresh);
        paintDetail(box, ln, fresh, after);
      } catch (err) {
        chip.disabled = false;
        msg.textContent = (err.detail && err.detail.message) || err.message;
      }
    };
    tags.append(chip);
  }
  tags.append(msg);
  const go = el('button', 'btn primary ac-detail-open', `Open in ${ln.case} ↦`);
  go.onclick = (e) => { e.stopPropagation(); openLineInCase(ln); };
  top.append(tags, go);
  const where = el('div', 'fb-help ac-detail-where', `${ln.case} · ${d.source_name} · line ${d.rid}`);
  where.title = ln.path;
  const grid = el('div', 'ac-detail-cols');
  d.columns.forEach((c, i) => {
    const v = d.values[i];
    if (v == null || v === '') return;
    grid.append(el('div', 'ac-detail-name', c.name), el('div', 'ac-detail-value', String(v)));
  });
  box.replaceChildren(top, where, grid);
  box.onclick = (e) => e.stopPropagation();
}

/* A tag written in the OPEN case changes what the grid shows; the same
   trio every other tag path ends in. Another case's tags are news to
   nobody here. */
export function afterTagged(ln) {
  const here = hereCase();
  if (!here || here.path !== ln.path) return;
  announceChange(['tags']);
  if (S.view) {
    clearRowCaches();
    refreshTagCounts();
    if (gridIsShowing()) { render(); drawRail(); regroupIfGroupedByTag(); } else S.gridRepaintPending = true;
  }
}

/* --------------------------------------------------- the ways out */

/* Into the line's case (when it isn't the open one), then onto its row:
   the case switch is the app's own, prompt for a held case included. */
export async function landInCase(path) {
  $('modal').hidden = true;
  const here = hereCase();
  if (here && here.path === path) return true;
  await openCase(path);
  const now = await api('/api/case/current').catch(() => null);
  return !!(now && now.open && AC.cases.some((c) => c.path === path) && sameCase(now.path, path));
}

export function sameCase(a, b) { return (a || '').replace(/\\/g, '/') === (b || '').replace(/\\/g, '/'); }

export async function openLineInCase(ln) {
  if (!(await landInCase(ln.path))) return;
  await jumpToTimelineRow(ln.source_id, ln.rid);
}

/* Search all's Open ↦, one case further: the table, cleared of whatever
   narrowed it, with the terms in its Advanced search. */
export async function openTableWithTerms(path, sourceId, terms, label) {
  if (!(await landInCase(path))) return;
  await post(`/api/source/${sourceId}/open`, { open: true }).catch(() => {});
  await loadSources(sourceId, { openOpts: { skipBuild: true } });
  if (S.sourceId !== sourceId) return;
  const cleared = clearViewNarrowing();
  S.searchMode = 'advanced';
  S.searchTerms = terms.map((t, i) => ({ term: t, connector: i ? 'OR' : 'AND', exclude: false }));
  S.advCollapsed = null;
  document.querySelectorAll('#searchModeToggle button')
    .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === 'advanced')));
  renderAdvancedChips();
  syncSearchExpansion(true);
  updateSearchHint();
  await rebuildView({ keepScroll: false, keepRow: false });
  if (cleared.length) toast(`Cleared the filters on ${label} to show every match`, 6000);
}

/* ------------------------------------------------------------ Where else */

export async function startSweep() {
  const values = AC.values.split('\n').map((v) => v.trim()).filter(Boolean);
  try {
    AC.job = await post('/api/across/sweep', { paths: pickedPaths(), values });
  } catch (e) { toast('Could not sweep: ' + e.message, 6000); return; }
  AC.shown = {};
  AC.openKey = null;
  pollSweep();
}

export function pollSweep() {
  clearTimeout(AC.pollTimer);
  paintAcrossButton();
  if (AC.frame) AC.frame.update();
  if (!AC.job || AC.job.done) {
    if (AC.job && AC.job.done && $('modal').hidden) {
      const cases = AC.job.cases.filter((c) => c.tables.length).length;
      toast(`Across cases: ${plural(AC.job.hits, 'hit')} in ${plural(cases, 'case')}`, 5000);
    }
    return;
  }
  AC.pollTimer = setTimeout(async () => {
    try { AC.job = await api(`/api/across/sweep/${AC.job.job_id}`); } catch { AC.job = null; }
    pollSweep();
  }, ACROSS_POLL_MS);
}

export function paintAcrossButton() {
  const b = $('btnAcrossCases');
  if (!b) return;
  const j = AC.job;
  b.textContent = !j ? 'Across cases' : !j.done ? `Across cases… ${j.read}/${j.total}` : `Across cases (${j.hits.toLocaleString()})`;
}

export function buildSweepPane(pane) {
  pane.append(el('p', 'fb-help',
    'Every table in every chosen case, searched the way Search all searches this one — '
    + 'through a case’s own search index when it has one. Blank uses this case’s watchlist.'));
  const box = el('textarea', 'mc-values ac-values');
  box.rows = 3;
  box.placeholder = 'One value per line — hashes, IPs, accounts, file names (blank = this case’s watchlist)';
  box.value = AC.values;
  box.oninput = () => { AC.values = box.value; };
  box.onkeydown = (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startSweep(); } };
  const go = el('button', 'btn primary', 'Search the cases');
  go.onclick = startSweep;
  const stop = el('button', 'btn ghost', 'Stop');
  stop.onclick = () => { if (AC.job) post(`/api/across/sweep/${AC.job.job_id}/cancel`, {}).catch(() => {}); };
  const status = el('span', 'note-status ac-status');
  const acts = el('div', 'row-actions ac-acts');
  acts.append(go, stop, status);
  const out = el('div', 'ac-out');
  pane.append(box, acts, out);

  function paint() {
    const j = AC.job;
    stop.hidden = !j || j.done;
    if (!j) {
      status.textContent = `${plural(pickedPaths().length, 'case')} chosen`;
      out.replaceChildren(el('div', 'note-status', 'Nothing searched yet.'));
      return;
    }
    const withHits = j.cases.filter((c) => c.tables.length);
    status.textContent = `${j.done ? (j.cancelled ? 'Stopped' : 'Read') : 'Reading'} ${j.read} of ${plural(j.total, 'case')} · `
      + `${plural(j.hits, 'hit')} in ${plural(withHits.length, 'case')} · ${plural(j.values.length, 'value')}`;
    // A case that could not be read leads, as a table Search all could
    // not count does: "no hits" is the answer an analyst moves on from.
    const nodes = j.cases.filter((c) => c.error).map((c) => el('div', 'mc-case-err', `${c.case}: ${c.error}`));
    for (const c of j.cases) {
      if (c.error || !c.tables.length) continue;
      const head = el('div', 'ac-case-head');
      const pill = el('span', 'ac-case-pill', c.case);
      pill.style.setProperty('--k', `var(--series-${caseSlot(c.path)})`);
      const n = c.tables.reduce((a, t) => a + t.match_count, 0);
      head.append(pill, el('span', 'ac-case-count',
        `${plural(c.tables.length, 'table')} · ${n.toLocaleString()}${c.tables.some((t) => t.capped) ? '+' : ''} ${n === 1 ? 'hit' : 'hits'}`
        + (c.is_here ? ' · this case' : '')));
      nodes.push(head);
      for (const t of c.tables) {
        const th = el('div', 'ac-table-head');
        const badge = el('span', 'type-badge', t.kind || t.name);
        badge.title = t.name;
        th.append(badge, el('span', 'ac-table-name', t.name),
          el('span', 'search-all-count', `${t.capped ? '1,000+' : t.match_count.toLocaleString()} ${t.match_count === 1 ? 'match' : 'matches'}`));
        for (const term of t.terms || []) {
          th.append(el('span', 'ac-term', `${term.term} ${term.capped ? '1,000+' : term.match_count.toLocaleString()}`));
        }
        const open = el('button', 'btn ghost', 'Open table ↦');
        open.title = `Open ${t.name} in ${c.case} with these values in its Advanced search`;
        open.onclick = () => openTableWithTerms(c.path, t.source_id, j.values, t.name);
        th.append(open);
        nodes.push(th, lineList(t.lines, { id: `${c.path}|${t.source_id}` }));
        if (t.match_count > t.lines.length) {
          const more = el('button', 'btn ghost ac-more',
            `…and ${t.capped ? 'more' : (t.match_count - t.lines.length).toLocaleString() + ' more'} — open the table`);
          more.onclick = open.onclick;
          nodes.push(more);
        }
      }
    }
    const quiet = j.cases.filter((c) => !c.error && !c.tables.length && c.status === 'done').map((c) => c.case);
    if (quiet.length) nodes.push(el('div', 'note-status', `No hits in ${quiet.join(', ')}.`));
    const waiting = j.cases.filter((c) => c.status === 'waiting' || c.status === 'reading').map((c) => c.case);
    if (waiting.length) nodes.push(el('div', 'note-status', `Still to read: ${waiting.join(', ')}`));
    out.replaceChildren(...nodes);
  }
  paint();
  return { update: paint };
}

/* ------------------------------------------------------------ Findings */

export async function buildTimeline() {
  AC.tlBusy = true;
  if (AC.frame) AC.frame.update();
  try {
    AC.timeline = await post('/api/across/timeline', { paths: pickedPaths(), start: AC.tlStart, end: AC.tlEnd });
    AC.shown.timeline = 0;
    AC.openKey = null;
  } catch (e) {
    AC.timeline = { error: e.message };
  }
  AC.tlBusy = false;
  if (AC.frame) AC.frame.update();
}

export function buildTimelinePane(pane) {
  pane.append(el('p', 'fb-help',
    'Every tagged row of every chosen case, in time order — each case read the way its own Timeline reads it.'));
  const from = el('input', 'confirm-input');
  from.placeholder = 'From  YYYY-MM-DD HH:MM:SS';
  from.value = AC.tlStart;
  from.oninput = () => { AC.tlStart = from.value.trim(); };
  const to = el('input', 'confirm-input');
  to.placeholder = 'To';
  to.value = AC.tlEnd;
  to.oninput = () => { AC.tlEnd = to.value.trim(); };
  const go = el('button', 'btn primary', 'Build');
  go.onclick = buildTimeline;
  const acts = el('div', 'row-actions ac-acts');
  acts.append(from, to, go);
  const head = el('div', 'ac-tl-head');
  const out = el('div', 'ac-out');
  pane.append(acts, head, out);
  function paint() {
    const r = AC.timeline;
    go.disabled = AC.tlBusy;
    if (AC.tlBusy) { head.replaceChildren(); out.replaceChildren(el('div', 'note-status', 'Reading the cases…')); return; }
    if (!r) { head.replaceChildren(); out.replaceChildren(el('div', 'note-status', 'Build it to see the findings of the chosen cases together.')); return; }
    if (r.error) { head.replaceChildren(); out.replaceChildren(el('div', 'mc-case-err', r.error)); return; }
    const chips = r.per_case.map((c) => {
      const chip = el('span', 'ac-case-pill', `${c.case} ${c.findings.toLocaleString()}`);
      chip.style.setProperty('--k', `var(--series-${caseSlot(c.path)})`);
      return chip;
    });
    head.replaceChildren(el('span', 'ac-tl-count',
      `${plural(r.lines.length, 'finding')}${r.truncated ? ` of ${r.total.toLocaleString()}` : ''} from `
      + `${plural(r.per_case.filter((c) => c.findings).length, 'case')}`), ...chips);
    for (const e of r.errors) head.append(el('span', 'mc-case-err', `${e.case}: ${e.error}`));
    out.replaceChildren(r.lines.length ? lineList(r.lines, { story: true, id: 'timeline' })
      : el('div', 'note-status', 'No tagged rows in these cases' + (AC.tlStart || AC.tlEnd ? ' in that window.' : '.')));
  }
  paint();
  return { update: paint };
}

/* --------------------------------------------------------------- Query */

export async function runQuery() {
  AC.queryBusy = true;
  if (AC.frame) AC.frame.update();
  try {
    AC.query = await post('/api/across/sql', { paths: pickedPaths(), sql: AC.sql });
    AC.shown.query = 0;
    AC.openKey = null;
  } catch (e) { AC.query = { error: e.message }; }
  AC.queryBusy = false;
  if (AC.frame) AC.frame.update();
}

export function buildQueryPane(pane) {
  const paths = pickedPaths();
  const aliases = paths.map((p, i) => `c${i + 1} = ${(AC.cases.find((c) => c.path === p) || {}).name || p}`);
  pane.append(el('p', 'fb-help',
    `One read-only SELECT, with the chosen cases attached as ${aliases.slice(0, 4).join(' · ')}${aliases.length > 4 ? ' …' : ''}. `
    + 'Rows come back as lines when the query names them: rid from one cN.src_M table, '
    + 'or "case" (c1…), source_id and rid columns.'));
  const box = el('textarea', 'mc-sql ac-sql');
  box.rows = 4;
  box.value = AC.sql;
  box.placeholder = "SELECT rid, UserName FROM c1.src_1 WHERE UserName = 'svc_backup'";
  box.oninput = () => { AC.sql = box.value; };
  box.onkeydown = (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runQuery(); } };
  const go = el('button', 'btn primary', 'Run');
  go.onclick = runQuery;
  const schema = el('button', 'btn ghost', 'Show schema');
  schema.onclick = async () => {
    try { AC.query = { schema: (await post('/api/across/schema', { paths: pickedPaths() })).schema }; }
    catch (e) { AC.query = { error: e.message }; }
    AC.frame.update();
  };
  const asTable = el('button', 'btn ghost', 'As a table');
  asTable.onclick = () => { AC.queryAsTable = !AC.queryAsTable; AC.frame.update(); };
  const status = el('span', 'note-status ac-status');
  const acts = el('div', 'row-actions ac-acts');
  acts.append(go, schema, asTable, status);
  const out = el('div', 'ac-out');
  pane.append(box, acts, out);
  if (paths.length > 8) status.textContent = `${paths.length} cases chosen — a query can attach 8 at most`;
  function paint() {
    const r = AC.query;
    go.disabled = AC.queryBusy;
    asTable.hidden = !(r && r.lines);
    asTable.textContent = AC.queryAsTable ? 'As lines' : 'As a table';
    if (AC.queryBusy) { out.replaceChildren(el('div', 'note-status', 'Running…')); return; }
    if (!r) { out.replaceChildren(); return; }
    if (r.error) { out.replaceChildren(el('div', 'mc-case-err', r.error)); return; }
    if (r.schema) { out.replaceChildren(el('pre', 'mc-schema', r.schema)); return; }
    status.textContent = `${plural(r.rows.length, 'row')}${r.truncated ? '+' : ''} in ${r.elapsed_ms} ms`
      + (r.lines ? ` · ${plural(r.lines.length, 'line')}` : ' · not rows of a table, shown as a table');
    if (r.lines && !AC.queryAsTable) { out.replaceChildren(lineList(r.lines, { id: 'query' })); return; }
    const table = el('table', 'mc-table');
    const head = el('tr');
    for (const c of r.columns) head.append(el('th', null, c));
    table.append(head);
    for (const row of r.rows.slice(0, 1000)) {
      const tr = el('tr');
      for (const cell of row) tr.append(el('td', null, cell == null ? '' : String(cell)));
      table.append(tr);
    }
    out.replaceChildren(table);
  }
  paint();
  return { update: paint };
}

/* --------------------------------------------------------------- Cases */

export function buildCasesPane(pane) {
  pane.append(el('p', 'fb-help',
    'The cases on this machine’s case list. Others are read without being opened: nothing in them changes '
    + 'unless you tag a row, and a case another Winnow has open is read but never written.'));
  const acts = el('div', 'row-actions ac-acts');
  const all = el('button', 'btn ghost', 'All');
  all.onclick = () => { AC.picked = null; AC.frame.update(); };
  const none = el('button', 'btn ghost', 'None');
  none.onclick = () => { AC.picked = new Set(); AC.frame.update(); };
  acts.append(all, none);
  const list = el('div', 'ac-cases');
  pane.append(acts, list);
  function paint() {
    const chosen = new Set(pickedPaths());
    list.replaceChildren(...AC.cases.map((c) => {
      const row = el('label', 'ac-case-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = chosen.has(c.path);
      cb.disabled = !c.exists;
      cb.onchange = () => {
        if (!AC.picked) AC.picked = new Set(pickedPaths());
        if (cb.checked) AC.picked.add(c.path); else AC.picked.delete(c.path);
        pane.dispatchEvent(new Event('across:changed'));
      };
      const pill = el('span', 'ac-case-pill', c.name);
      pill.style.setProperty('--k', `var(--series-${caseSlot(c.path)})`);
      const note = !c.exists ? 'file missing' : c.is_open ? 'open here'
        : c.held_by ? `open in another Winnow (${c.held_by}) — read, not tagged` : (c.group || '');
      row.append(cb, pill, el('span', 'ac-case-note', note));
      return row;
    }));
  }
  paint();
  return { update: paint };
}

/* -------------------------------------------------------------- dialog */

export async function openAcrossCases(section = null) {
  markModalAction('openAcrossCases');
  try { AC.cases = (await api('/api/across/cases')).cases; }
  catch (e) { toast('Could not read the case list: ' + e.message, 6000); return; }
  if (section) AC.section = section;
  modal('Across cases', (b) => {
    const total = () => AC.cases.filter((c) => c.exists).length;
    const frame = sectionPanes({
      sections: [
        { id: 'sweep', group: 'Ask', title: 'Where else', build: buildSweepPane,
          hint: () => (!AC.job ? 'Values in every case' : !AC.job.done ? `Reading ${AC.job.read}/${AC.job.total}…`
            : `${plural(AC.job.hits, 'hit')} · ${plural(AC.job.cases.filter((c) => c.tables.length).length, 'case')}`) },
        { id: 'timeline', group: 'Ask', title: 'Findings', build: buildTimelinePane,
          hint: () => (AC.timeline && AC.timeline.lines ? `${plural(AC.timeline.lines.length, 'tagged row')} in time order`
            : 'Tagged rows, in time order') },
        { id: 'query', group: 'Ask', title: 'Query', build: buildQueryPane,
          hint: () => (AC.query && AC.query.rows ? plural(AC.query.rows.length, 'row') : 'SQL over c1, c2…') },
        { id: 'cases', group: 'Read', title: 'Cases', build: buildCasesPane,
          hint: () => (AC.picked ? `${pickedPaths().length} of ${total()}` : `All ${total()}`) },
      ],
      label: 'Across cases', cls: 'list-panes ac-panes', paneCls: 'list-pane ac-pane', changedEvent: 'across:changed',
    });
    AC.frame = frame;
    b.append(frame.panes);
    frame.show(AC.section);
    for (const it of frame.panes.querySelectorAll('.side-rail-item')) {
      it.addEventListener('click', () => { AC.section = it.dataset.section; });
    }
  }, { wide: 'x', tall: true });
}

export function wireAcross() {
  $('btnAcrossCases').onclick = () => openAcrossCases();
}

/* Derived datetime columns and extracted (JSON/XML) columns — the analyst's
own additions to a source's column set.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { openStack } from './stack.js';
import { renderHead, saveLayout, saveLayoutNow } from './columns.js';
import { quickAddWidget } from './dashboard.js';
import { tableOf, widgetFrom } from './dashwidgets.js';
import { $, api, el, nameMap, post, setBusy, toast, toastAction } from './core.js';
import { currentSpec, ellipsize } from './filters.js';
import { render } from './grid.js';
import { copyUniqueValues } from './grouping.js';
import { startJobsPoll } from './jobs.js';
import { loadSources, openSource, renameColumnInStash } from './sources.js';
import { gridIsShowing } from './sql.js';
import { S, dashboardCreatorMode } from './state.js';
import { clearRowCaches } from './tags.js';
import { updateFiltersButton, updateTimeRangeButton } from './timeframe.js';
import { TS_FORMATS, baseColumns, columnMeta, parseTimestamp, tsFormatFor } from './tsformat.js';
import { confirmDialog, modal, promptDialog, sideRail } from './ui.js';
import { rebuildView } from './view.js';

/* ------------------------------------------------------- derived columns */

/* Analyst-added columns computed from an existing one (see timeparse.py).
   The values are materialised server-side into a sidecar table, so a
   derived column sorts, filters, groups and exports like any other — the
   only frontend-visible difference is the marker, the management menu,
   and that its display format defaults like any datetime column. */

export let DERIVED_OPS = null;

 // registry from /api/derived/ops, fetched once per load

export async function derivedOps() {
  if (!DERIVED_OPS) DERIVED_OPS = await api('/api/derived/ops');
  return DERIVED_OPS;
}

export function opLabel(opId) {
  const op = (DERIVED_OPS || []).find((o) => o.id === opId);
  return op ? op.label : opId;
}

/* Does this column hold JSON/XML documents? Judged from rows already in
   the page cache (the menu builds synchronously, so no fetch): any sampled
   non-empty cell that opens like a document counts. A column whose first
   cached rows are all blank simply doesn't offer Flatten until some are on
   screen — an honest "nothing to flatten here" rather than a stale menu
   entry on every plain-text column. */
export function columnLooksStructured(name) {
  const idx = S.columns.findIndex((c) => c.name === name);
  if (idx === -1) return false;
  let seen = 0;
  for (const r of S.rowsByPos.values()) {
    const v = r && r.cells ? r.cells[idx] : null;
    if (v == null || v === '') continue;
    const t = String(v).trimStart();
    if (t[0] === '{' || t[0] === '[' || t[0] === '<') return true;
    if (++seen >= 200) break;
  }
  return false;
}

export function columnMenuItems(name) {
  const c = columnMeta(name) || {};
  const items = [];
  if (c.derived_kind === 'duration') {
    const cur = (S.layout[name] || {}).durFormat || 'human';
    for (const [key, label] of [['human', '1h 23m 45s'], ['raw', 'Seconds']]) {
      items.push({
        label,
        checked: key === cur, // the menu's own ✓ slot, rather than a padded label
        onclick: () => {
          S.layout[name] = Object.assign({}, S.layout[name] || {}, { durFormat: key });
          render();
          saveLayout();
        },
      });
    }
  } else if (c.type === 'datetime') {
    const current = tsFormatFor(name);
    for (const key of Object.keys(TS_FORMATS)) {
      items.push({
        label: TS_FORMATS[key],
        checked: key === current,
        onclick: () => {
          /* The chosen key is always stored, including 'raw'. It used to be
             stored as undefined, which was equivalent back when the
             fallback was 'raw' — with a configurable default underneath,
             that would silently mean "inherit" instead of "as stored". */
          S.layout[name] = Object.assign({}, S.layout[name] || {}, { tsFormat: key });
          render();
          saveLayout();
        },
      });
    }
  }
  if (items.length) items.push('-');
  // The verbs, in the order they get used: Stack (and its clipboard
  // twin), Derive, and — only when the column actually holds documents —
  // Flatten. The derive modal
  // is type-first (timestamp / extract / join / compare), so one entry
  // covers what used to be spelled out as "add a datetime column".
  items.push({ label: 'Stack values (rarest first)…', onclick: () => openStack(name) });
  // Stack's list with the counts taken off, onto the clipboard: every
  // distinct value the view holds, sorted. Always the whole view from
  // here — a header is about the column, not about which rows are picked
  // (the row menu's copy of this item is the one that follows the picks).
  items.push({
    label: 'Copy unique values',
    title: `Every distinct ${name} value the current filters show, one per line, sorted`,
    onclick: () => copyUniqueValues(name, null),
  });
  items.push({ label: 'Derive a column from this…', onclick: () => openDerivedColumnModal(name) });
  if (!c.derived && S.sourceId >= 0 && columnLooksStructured(name)) {
    items.push({ label: 'Flatten JSON/XML into columns…', onclick: () => openFlattenModal(name) });
  }
  if (c.derived) {
    if (c.parse_failures) {
      // "Unparsed" is the right word for a timestamp that didn't convert;
      // for an extracted field the same count means the document had no
      // such field, which is a different thing to go and look at.
      const extracted = c.derived_kind === 'text';
      items.push({
        label: extracted
          ? `Show ${c.parse_failures.toLocaleString()} row${c.parse_failures === 1 ? '' : 's'} without this field`
          : `Show ${c.parse_failures.toLocaleString()} unparsed row${c.parse_failures === 1 ? '' : 's'}`,
        onclick: () => showUnparsedRows(c),
      });
    }
    items.push({ label: 'Rename derived column…', onclick: () => renameDerivedColumn(c) });
    if (hasFieldPath(c)) {
      items.push({ label: 'Change the field path…', onclick: () => editExtractedPath(c) });
    } else {
      items.push({ label: 'Re-derive…', onclick: () => openDerivedColumnModal(c.derived_from, c) });
    }
    items.push({ label: 'Remove derived column…', onclick: () => removeDerivedColumn(c) });
  }
  // Straight onto a board, no editor: the column is the pick, the
  // widget writes its own query and knows how to open these rows.
  // Not for a derived column itself: boards don't offer derived columns
  // anywhere (dashwidgets.js's widgetable), though a widget's own SQL can
  // read one — run_sql's src_<id> view joins the drv_<id> sidecar.
  if (dashboardCreatorMode() && S.sourceId != null && S.sourceId >= 0 && !c.derived) {
    const table = tableOf(S.sourceId);
    items.push('-', { header: 'Add to dashboard' });
    items.push({ label: `Top values of ${name}`, onclick: () => quickAddWidget(widgetFrom({ template: 'top', table, column: name })) });
    items.push({ label: `Distinct count of ${name}`, onclick: () => quickAddWidget(widgetFrom({ template: 'distinct', table, column: name })) });
    if (c.type === 'datetime') {
      items.push({ label: `Events over time by ${name}`, onclick: () => quickAddWidget(widgetFrom({ template: 'time', table, column: name, bucket: 'day' })) });
    }
  }
  return items;
}

/* "12 failures" is only useful if you can see which 12. The fragment is
   built server-side (it has to quote two column names into SQL) and lands
   in the guided filter builder's raw slot, so it shows up as a normal
   filter the analyst can then edit or clear. */
export async function showUnparsedRows(c) {
  try {
    const res = await api(`/api/derived/${c.derived_id}/unparsed_filter`);
    S.filterTree = { type: 'group', op: 'AND', children: [{ type: 'raw', sql: res.sql }] };
    updateFiltersButton();
    await rebuildView();
    toast(`Showing rows where "${c.name}" could not be parsed`);
  } catch (e) {
    toast('Could not filter: ' + e.message, 6000);
  }
}

/* Does this column's operation have a field path to change? Only the JSON
   and XML field ops do — their path is the whole definition. A regex or a
   lookup column is derived_kind 'text' too, and was offered "Change the
   field path…" on that alone: the rederive it sent ({path}) is not a
   param either op has, so it failed every time and their settings could
   not be edited at all. They take Re-derive…, the derive modal prefilled
   with what the column has now. Read from the op registry (loaded with
   any table that has a derived column, sources.js); the column's own
   params answer the same question if it isn't here yet. */
export function hasFieldPath(c) {
  const op = (DERIVED_OPS || []).find((o) => o.id === c.derived_op);
  if (op) return op.params.some((p) => p.name === 'path');
  return Object.prototype.hasOwnProperty.call(c.derived_params || {}, 'path');
}

/* An extracted column's whole definition is its path, so "re-derive" here
   is "edit the path" — the timestamp modal's format-and-parameters shape
   has nothing to offer it. Recomputes in place via the same rederive
   endpoint, so the column keeps its name, position and width.

   Every edit to a derived column (this, Re-derive…, Remove) sends the
   table it was made FROM. On a merge the column is one definition per
   member, and the merge's id is what makes the server act on all of them
   (Store._derived_scope) — `derived_id` alone is the first member's, and
   used to be all that changed. */
export async function editExtractedPath(c) {
  const current = (c.derived_params || {}).path || '';
  const next = await promptDialog(
    `Field path for "${c.name}" (read from "${c.derived_from}"):`, current, { okLabel: 'Recompute' });
  if (next === null || next.trim() === current) return;
  try {
    const res = await post(`/api/derived/${c.derived_id}/rederive`,
      { params: { path: next.trim() }, source_id: S.sourceId });
    await showDerivedColumnsSoon();
    const chain = res.cascades_to || [];
    toast(`Recomputing "${c.name}"…`
      + (chain.length ? ` — ${chain.join(', ')} ${chain.length === 1 ? 'is' : 'are'} derived from it and will recompute too` : ''), chain.length ? 7000 : 4000);
  } catch (e) {
    toast('Could not change the path: ' + e.message, 6000);
  }
}

/* The file's own columns are evidence and keep their names; a derived
   column's name is the analyst's, so it can change. The server moves
   everything in the case file that names it (columns computed from it,
   lookups on other tables, the saved layouts), and this moves what is on
   screen before the re-open, so a filter, sort, grouping or timeframe on
   the column carries over under the new name instead of falling off.
   Sends the table it was renamed from, as Re-derive and Remove do: on a
   merge that renames every member's copy, whose own stashed views move
   with it. */
export async function renameDerivedColumn(c) {
  const next = await promptDialog(`New name for the derived column "${c.name}":`, c.name, { okLabel: 'Rename' });
  if (next === null || next.trim() === c.name) return;
  let res;
  try {
    res = await post(`/api/derived/${c.derived_id}/rename`, { name: next, source_id: S.sourceId });
  } catch (e) {
    toast('Could not rename: ' + e.message, 6000);
    return;
  }
  const name = res.definition.name;
  renameColumnIn(S, c.name, name);
  const tables = (res.member_definitions || [res.definition]).map((d) => d.source_id);
  renameColumnInStash(tables, c.name, name);
  updateTimeRangeButton();
  await saveLayoutNow();
  await loadSources();
  await openSource(S.sourceId);
  toast(`Renamed "${c.name}" to "${name}"`);
}

/* Every place a table's view state names a column, moved to a new name:
   the live state (S) or one stashed for a table not on screen
   (sources.js viewStateStash), which spell grouping differently but are
   otherwise the same shape. In place where the state is an object other
   code keeps a hold of; names compare without case, as the server's
   collision check does, so the one column is the only match. */
export function renameColumnIn(st, from, to) {
  const swap = (n) => (typeof n === 'string' && n.toLowerCase() === from.toLowerCase() ? to : n);
  const rekey = (m) => nameMap(Object.fromEntries(Object.entries(m).map(([k, v]) => [swap(k), v])));
  if (st.filters) st.filters = rekey(st.filters);
  if (st.layout) st.layout = rekey(st.layout);
  for (const key of ['filterOpen', 'groupBy', 'groupByCols', 'order']) {
    if (Array.isArray(st[key])) st[key] = st[key].map(swap);
  }
  for (const s of st.sort || []) s.column = swap(s.column);
  (function walk(node) {
    if (!node) return;
    if (node.type === 'cond') node.column = swap(node.column);
    for (const child of node.children || []) walk(child);
  })(st.filterTree);
  if (st.timeRange && st.timeRange.column) st.timeRange.column = swap(st.timeRange.column);
  if (st.cellFocus && st.cellFocus.name) st.cellFocus.name = swap(st.cellFocus.name);
}

export async function removeDerivedColumn(c) {
  const src = S.sources.find((x) => x.id === S.sourceId);
  const members = src && src.is_merge ? (src.member_source_ids || []).length : 0;
  const ok = await confirmDialog(
    `Remove the derived column "${c.name}"${members ? ` from all ${members} tables in this merge` : ''}? `
    + `Its values are recomputed from "${c.derived_from}", so it can be added back at any time.`,
    { okLabel: 'Remove', danger: true });
  if (!ok) return;
  try {
    await api(`/api/derived/${c.derived_id}?source_id=${encodeURIComponent(S.sourceId)}`, { method: 'DELETE' });
    delete S.layout[c.name];
    S.order = S.order.filter((n) => n !== c.name);
    await loadSources();
    await openSource(S.sourceId);
    toast(`Removed "${c.name}"`);
  } catch (e) {
    toast('Could not remove: ' + e.message, 6000);
  }
}

/* ------------------------------------------- extracted (JSON/XML) columns

   These share the derived-column machinery with the timestamp ops — same
   registry, same backfill, same sidecar, same session portability. The
   only thing that differs is the question being asked, which is why they
   are a separate `family` in the registry and a separate pair of entry
   points here rather than another row in the timestamp modal's dropdown. */

export function extractOpFor(kind) { return kind === 'xml' ? 'xml_field' : 'json_field'; }

/* A name that doesn't collide with a column already on the table, since
   the obvious suggestion (a path's last component) collides constantly —
   `$.user.name` and `$.host.name` both want "name". */
export function uniqueColumnName(base, taken) {
  let name = (base || 'field').trim() || 'field';
  if (!taken.has(name.toLowerCase())) return name;
  for (let i = 2; i < 500; i++) {
    const candidate = `${name} ${i}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
  return `${name} ${Date.now()}`;
}

export function takenColumnNames() {
  return new Set(S.columns.map((c) => c.name.toLowerCase()));
}

/* A predicate's value as a path writes it — structparse._quote_predicate:
   single quotes, double when the value holds an apostrophe, and a
   doubled quote when it holds both. */
export function quotePredicate(value) {
  const v = String(value);
  if (!v.includes("'")) return `'${v}'`;
  if (!v.includes('"')) return `"${v}"`;
  return `'${v.replace(/'/g, "''")}'`;
}

/* structparse.parse_json_path's twin, for naming only: segments are
   numbers (an index), [key, value] pairs (a `[Name='X']` predicate) and
   strings (a key). null for a path it can't read. */
const JSON_PATH_TOKEN = new RegExp([
  /\.\s*([^.[\]"']+)/.source,
  /\[\s*(-?\d+)\s*\]/.source,
  /\[\s*(@?[A-Za-z_]\w*)\s*=\s*(?:'((?:''|[^'])*)'|"((?:""|[^"])*)")\s*\]/.source,
  /\[\s*"((?:\\.|[^"\\])*)"\s*\]/.source,
  /\[\s*'((?:\\.|[^'\\])*)'\s*\]/.source,
  /([^.[\]"']+)/.source,
].join('|'), 'y');

export function parseJsonPath(text) {
  let s = String(text || '').trim();
  if (s.startsWith('$')) s = s.slice(1);
  const out = [];
  let pos = 0;
  while (pos < s.length) {
    if (/\s/.test(s[pos])) { pos++; continue; }
    JSON_PATH_TOKEN.lastIndex = pos;
    const m = JSON_PATH_TOKEN.exec(s);
    if (!m || JSON_PATH_TOKEN.lastIndex === pos) return null;
    if (m[2] !== undefined) out.push(Number(m[2]));
    else if (m[3] !== undefined) out.push([m[3], m[4] !== undefined ? m[4].replace(/''/g, "'") : m[5].replace(/""/g, '"')]);
    else if (m[1] !== undefined || m[8] !== undefined) out.push((m[1] !== undefined ? m[1] : m[8]).trim());
    else {
      out.push((m[6] !== undefined ? m[6] : m[7]).replace(/\\(u[0-9a-fA-F]{4}|[\s\S])/g,
        (all, e) => (e.length === 5 ? String.fromCharCode(parseInt(e.slice(1), 16)) : e)));
    }
    pos = JSON_PATH_TOKEN.lastIndex;
  }
  return out.length ? out : null;
}

/* Suggests the column name the way structparse.suggest_name does — for
   JSON the full dotted path (target.ip, items[0].id: two fields with the
   same leaf get names that say which is which) unless the path picks a
   Name/Value entry, which is named by its Name (UserAgent, or
   X.NewValue beside an OldValue); for XML the last meaningful component
   or an EVTX-style predicate's own value. */
export function suggestColumnName(path, kind) {
  const s = String(path);
  // JSON first: the "@" rules below are XML's (Provider/@Name), and an
  // EvtxECmd payload is full of keys that merely START with "@" — the
  // XML-to-JSON convention for attributes. Sent down the XML branch,
  // $.EventData.Data[0].@Name came out as "$.EventData.Data[0]. Name"
  // beside a clean "EventData.Data[0].#text" from the sibling key.
  if (kind === 'json') {
    const segs = parseJsonPath(s);
    if (!segs) return s;
    if (segs.every((seg) => typeof seg === 'number')) return s.replace(/^[$.]+/, '');
    let last = -1;
    segs.forEach((seg, i) => { if (Array.isArray(seg)) last = i; });
    let out = '';
    let rest = segs;
    if (last >= 0 && String(segs[last][1]).trim()) {
      out = String(segs[last][1]).trim();
      rest = segs.slice(last + 1);
      if (rest.length && typeof rest[0] === 'string'
          && ['value', 'text'].includes(rest[0].toLowerCase().replace(/^[@#]+/, ''))) rest = rest.slice(1);
    }
    for (const seg of rest) {
      if (typeof seg === 'number') out += `[${seg}]`;
      else if (Array.isArray(seg)) out += `[${seg[0]}=${quotePredicate(seg[1])}]`;
      else out += (out ? '.' : '') + seg;
    }
    return out;
  }
  const pred = s.match(/\[@[\w:.-]+='([^']*)'\]$/);
  if (pred && pred[1].trim()) return pred[1].trim();
  if (s.includes('@') && !s.trim().endsWith(']')) {
    const at = s.lastIndexOf('@');
    const attr = s.slice(at + 1);
    const tail = s.slice(0, at).replace(/\/$/, '').split('/').pop().replace(/\[[^\]]*\]$/, '');
    return (tail ? `${tail} ${attr}` : attr).trim();
  }
  return s.replace(/\/$/, '').split('/').pop().replace(/\[\d+\]$/, '') || s;
}

/* One field, one column — the "add as a column" item on a right-clicked
   node in the detail pane. No modal: the path came from a click, the name
   is derivable, and interrupting that with a dialog to confirm two things
   the analyst just expressed would be the wrong trade. The toast carries
   the undo-shaped escape hatch instead (remove it from the header menu). */
export async function addExtractedColumn(column, path, kind) {
  const name = uniqueColumnName(suggestColumnName(path, kind), takenColumnNames());
  setBusy(true);
  try {
    await post('/api/derived', {
      source_id: S.sourceId, name, input_column: column,
      op_id: extractOpFor(kind), params: { path },
    });
    await showDerivedColumnsSoon();
    toast(`Adding "${name}" from ${ellipsize(path, 30)}…`);
  } catch (e) {
    toast('Could not add column: ' + e.message, 6000);
  } finally { setBusy(false); }
}

/* Brings the new columns into view without waiting for the backfill —
   same idiom the timestamp modal already uses. The columns appear
   immediately with status 'building'; the jobs panel tracks progress,
   and when the job lands pollJobs reports it and refreshAfterDerive puts
   the values on screen. Blocking the UI on a pass over a million rows
   would be the wrong trade for a column the analyst can already see. */
export async function showDerivedColumnsSoon() {
  await loadSources();
  await openSource(S.sourceId);
  startJobsPoll();
}

/* What a derived column's header and management menu read off it — the
   fields a backfill's landing (or failing) changes. */
export function derivedShape(c) {
  return JSON.stringify([c.derived_id, c.derived_status, c.parse_failures, c.derived_op, c.derived_params]);
}

/* The other half of showDerivedColumnsSoon: the backfill has landed, and
   the grid on screen was painted before it. Its cached rows were fetched
   while the column was 'building' — blank, or filled only as far as the
   pass had got — and S.columns still says "(building…)" in the header.
   pollJobs refreshes S.sources when a derive job finishes, but with
   loadSources(navigate:false), which leaves a live grid alone on purpose;
   so the new column sat empty until the analyst switched tables and back.

   `jobs` are the derive jobs pollJobs just saw finish, done or not, with
   S.sources already fresh. The table on screen is affected when a done
   job names it (or a member of the merge on screen), or when its derived
   columns now read differently from S.columns — the second is what
   catches a failure or a cancel, whose job names no table. Updated in
   place rather than re-opened: the pass took long enough for the analyst
   to have moved on, and a re-open would drop their picks and cursor (and,
   from a page tab, drag them back to the grid). The cached rows go,
   since they predate the values; and a view sorted, filtered, grouped or
   timeframed by a column that just changed was ordered by the old (or
   missing) values, so it is rebuilt. A cancelled or failed create is
   removed outright, and leaves the column order with it. */
export async function refreshAfterDerive(jobs) {
  if (S.sourceId == null || !S.view) return;
  const src = (S.sources || []).find((s) => s.id === S.sourceId);
  if (!src) return;
  const fresh = src.columns || [];
  const tables = new Set([src.id, ...(src.member_source_ids || [])]);
  const changed = new Set();
  for (const j of jobs) {
    if (j.status !== 'done' || !(j.source_ids || []).some((id) => tables.has(id))) continue;
    for (const c of j.result || []) changed.add(c.name);
  }
  const before = new Map(S.columns.map((c) => [c.name, c]));
  for (const c of fresh) {
    const was = before.get(c.name);
    if (c.derived && (!was || derivedShape(c) !== derivedShape(was))) changed.add(c.name);
  }
  const kept = new Set(fresh.map((c) => c.name));
  const gone = S.columns.some((c) => !kept.has(c.name));
  if (!changed.size && !gone) return;
  // A cached row's cells are positional to S.columns (openSource says
  // why), so the two change together, and the caches go below.
  S.columns = fresh;
  if (gone) S.order = S.order.filter((n) => kept.has(n));
  renderHead();
  if (viewReadsColumns(changed)) {
    await rebuildView();
    return;
  }
  clearRowCaches();
  if (gridIsShowing()) render(); else S.gridRepaintPending = true;
}

/* Does the view on screen depend on any of these columns' values? A raw
   filter fragment is SQL the analyst (or "Show N unparsed rows") wrote,
   which this cannot read reliably — so it counts as yes, and the cost of
   being wrong is one rebuild. */
export function viewReadsColumns(names) {
  const spec = currentSpec();
  const read = [
    ...spec.filters.map((f) => f.column),
    ...spec.sort.map((s) => s.column),
    ...S.groupByCols,
  ];
  if (spec.time_range && spec.time_range.enabled) read.push(spec.time_range.column);
  let raw = false;
  (function walk(node) {
    if (!node) return;
    if (node.type === 'raw') raw = true;
    else if (node.type === 'cond') read.push(node.column);
    else for (const child of node.children || []) walk(child);
  })(spec.filter_tree);
  return raw || read.some((n) => names.has(n));
}

/* The flatten picker: every field found in a sample of the column, with
   how much of the sample carried it, ticked into columns in one pass.

   Coverage is shown and pre-selection is driven by it because that's the
   judgement the analyst is actually making — a field in 3 of 200 rows is
   usually noise from one outlier record, and a field in all 200 is a
   column. Everything at full coverage starts ticked; the rest start
   unticked and one click away. */
export async function openFlattenModal(column) {
  let found;
  setBusy(true);
  try {
    found = await post('/api/derived/paths', { source_id: S.sourceId, column });
  } catch (e) {
    toast('Could not read that column: ' + e.message, 6000);
    return;
  } finally { setBusy(false); }

  if (!found.kind || !found.paths.length) {
    toast(`"${column}" doesn't look like JSON or XML`, 5000);
    return;
  }

  const taken = takenColumnNames();
  const rows = found.paths.map((p) => ({
    path: p.path,
    coverage: p.coverage,
    count: p.count,
    sample: p.sample,
    // Present in every sampled row *and* actually carrying a value. A
    // container element like <TimeCreated SystemTime="…"/> is present
    // everywhere and empty everywhere; pre-ticking it would build a column
    // of blanks (its value is on the attribute, one row down the list).
    checked: p.coverage >= 1 && p.nonempty > 0,
    name: uniqueColumnName(p.suggested_name || suggestColumnName(p.path, found.kind), taken),
  }));
  // Reserve every suggested name up front, so two paths that suggest the
  // same one get distinct defaults rather than colliding at submit time.
  rows.forEach((r) => taken.add(r.name.toLowerCase()));

  modal(`Flatten "${column}" into columns`, (body) => {
    const head = el('div', 'flatten-head');
    head.append(el('div', 'fb-help',
      `${found.kind.toUpperCase()} · ${found.paths.length} field${found.paths.length === 1 ? '' : 's'} found in ${found.sampled.toLocaleString()} sampled row${found.sampled === 1 ? '' : 's'}`));
    const bulk = el('div');
    const all = el('button', 'btn ghost', 'Select all');
    const none = el('button', 'btn ghost', 'Select none');
    bulk.append(all, none);
    head.append(bulk);
    body.append(head);

    const list = el('div', 'flatten-list');
    const count = el('div', 'fb-help');

    function updateCount() {
      const n = rows.filter((r) => r.checked).length;
      count.textContent = n ? `${n} column${n === 1 ? '' : 's'} will be added, in a single pass over the table.`
                            : 'Nothing selected.';
      addBtn.disabled = !n;
    }

    function renderRows() {
      list.replaceChildren();
      for (const r of rows) {
        const row = el('div', 'flatten-row');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = r.checked;
        box.onchange = () => { r.checked = box.checked; updateCount(); };

        // Name over path: the name is what the analyst edits, the path is
        // what the column actually means, and burying the latter in a
        // tooltip makes two similarly-named fields impossible to tell
        // apart at a glance.
        const namePart = el('div', 'flatten-name');
        const name = el('input');
        name.type = 'text';
        name.value = r.name;
        name.oninput = () => { r.name = name.value; };
        const path = el('div', 'flatten-path', r.path);
        path.title = r.path;
        namePart.append(name, path);

        const cov = el('div', 'flatten-cov', `${Math.round(r.coverage * 100)}%`);
        cov.title = `${r.count.toLocaleString()} of ${found.sampled.toLocaleString()} sampled rows have this field`;
        const sample = el('div', 'flatten-sample', r.sample || '—');
        sample.title = r.sample || '';
        row.append(box, namePart, cov, sample);
        list.append(row);
      }
      updateCount();
    }

    all.onclick = () => { rows.forEach((r) => { r.checked = true; }); renderRows(); };
    none.onclick = () => { rows.forEach((r) => { r.checked = false; }); renderRows(); };

    const addBtn = el('button', 'btn primary', 'Add columns');
    addBtn.onclick = async () => {
      const picked = rows.filter((r) => r.checked);
      const names = picked.map((r) => r.name.trim());
      if (names.some((n) => !n)) { toast('Every selected column needs a name', 4000); return; }
      const lower = names.map((n) => n.toLowerCase());
      const dupe = lower.find((n, i) => lower.indexOf(n) !== i);
      if (dupe) { toast(`Two columns are both called "${dupe}" — rename one`, 5000); return; }
      $('modal').hidden = true;
      setBusy(true);
      try {
        await post('/api/derived/batch', {
          source_id: S.sourceId,
          columns: picked.map((r, i) => ({
            name: names[i], input_column: column,
            op_id: extractOpFor(found.kind), params: { path: r.path },
          })),
        });
        await showDerivedColumnsSoon();
        toast(`Adding ${names.length} column${names.length === 1 ? '' : 's'}…`);
      } catch (e) {
        toast('Could not add columns: ' + e.message, 6000);
      } finally { setBusy(false); }
    };

    renderRows();
    body.append(list, count);
    const foot = el('div', 'row-actions');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    foot.append(cancel, addBtn);
    body.append(foot);
  }, { wide: true });
}

/* Which column the add dialog starts on when nothing named one — the
   Table dialog's "Add derived column…". It used to be the first column,
   which on an EvtxECmd table is RecordNumber: a counter, which the format
   guess then called Mac absolute time (timeparse's _MAC_DETECT_FLOOR is
   the other half of that fix). Opened from a table rather than from a
   column, the dialog is most often after a timestamp, so it starts on the
   first column that looks like one: typed datetime at import, else one
   whose values (the rows already in the page cache — no fetch) or whose
   name say time. The first column only when nothing does. */
export const TIME_NAME_WORDS = new Set(['time', 'timestamp', 'date', 'datetime', 'epoch', 'ts', 'when',
  'created', 'modified', 'accessed', 'written']);
// A word that turns a time-ish name into something else: TimeZone,
// UtcOffset, DateFormat, ModifiedBy.
export const NOT_TIME_WORDS = new Set(['zone', 'tz', 'offset', 'bias', 'format', 'by']);

export function columnNameLooksLikeTime(name) {
  // Words out of camelCase, snake_case and anything else that separates
  // them — TimeCreated, visit_time, @timestamp, Created0x10.
  const words = String(name).replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z]+/);
  return words.some((w) => TIME_NAME_WORDS.has(w)) && !words.some((w) => NOT_TIME_WORDS.has(w));
}

export function cachedValuesLookLikeTime(name) {
  const idx = S.columns.findIndex((c) => c.name === name);
  if (idx === -1) return false;
  let seen = 0;
  let hits = 0;
  for (const r of S.rowsByPos.values()) {
    const v = r && r.cells ? r.cells[idx] : null;
    if (v == null || v === '') continue;
    if (parseTimestamp(v)) hits++;
    if (++seen >= 50) break;
  }
  return seen > 0 && hits * 2 >= seen;
}

export function defaultParseColumn(cols) {
  const pick = cols.find((c) => c.type === 'datetime')
    || cols.find((c) => cachedValuesLookLikeTime(c.name) || columnNameLooksLikeTime(c.name))
    || cols[0];
  return pick ? pick.name : null;
}

/* What the Add derived column dialog's side list offers, by group. Every
   operation is an entry of its own except the timestamp formats, which
   are one entry, "Parse a timestamp", with a Format picker inside it:
   there are more than a dozen, detection picks the right one most of the
   time, and listed one by one they filled the list and buried the other
   kinds of column under it. A family that arrives with no group here is
   listed under Other rather than vanishing. */
export const DERIVE_GROUPS = [
  ['Time', (op) => op.family === 'datetime'],
  ['Extract', (op) => op.family === 'extract'],
  ['Decode', (op) => op.family === 'decode'],
  ['Combine', (op) => op.family === 'combine'],
  ['Join', (op) => op.family === 'lookup'],
];
export const TIMESTAMP_ENTRY = 'timestamp';
// The menuPins() key the side list's pinned entries are kept under.
export const DERIVE_PINS = 'derive';

export function isTimestampOp(op) {
  return op.family === 'datetime' && op.derived_kind === 'datetime';
}

/* The side list's entries in display order: {key, label, group,
   description} plus `formats` (the timestamp ops) on the timestamp entry
   and `op` on every other. */
export function deriveEntries(ops) {
  const groupOf = (op) => { for (const [g, m] of DERIVE_GROUPS) if (m(op)) return g; return 'Other'; };
  const entries = [];
  for (const op of ops) {
    if (!isTimestampOp(op)) {
      entries.push({ key: op.id, label: op.label, group: groupOf(op), description: op.description, op });
      continue;
    }
    let ts = entries.find((e) => e.key === TIMESTAMP_ENTRY);
    if (!ts) {
      ts = { key: TIMESTAMP_ENTRY, label: 'Parse a timestamp', group: groupOf(op), formats: [],
             description: 'A date and time read out of this column, in UTC, that sorts, filters and lines up on the Timeline.' };
      entries.push(ts);
    }
    ts.formats.push(op);
  }
  const order = DERIVE_GROUPS.map(([g]) => g).concat('Other');
  return entries.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));   // stable: registry order within a group
}

/* Does the search box's text find this entry? Every word has to appear
   somewhere in its name, group, id or description. The timestamp entry
   also answers for its formats — "filetime" finds it — and says which
   one matched, so picking that hit can pick the format too. null for no
   match, else {format}: the format's op id, or null when the entry
   matched on its own words. */
export function matchesDeriveSearch(entry, query) {
  const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return { format: null };
  const has = (...parts) => {
    const text = parts.join(' ').toLowerCase();
    return terms.every((t) => text.includes(t));
  };
  const own = [entry.label, entry.group, entry.op ? entry.op.id.replace(/_/g, ' ') : '', entry.description || ''];
  if (has(...own)) return { format: null };
  for (const f of entry.formats || []) {
    if (has(...own, f.label, f.id.replace(/_/g, ' '), f.description || '')) return { format: f.id };
  }
  return null;
}

/* The add/re-derive modal. `prefill` is the column to parse; `editing` is
   the existing definition when re-deriving (the column and operation are
   then fixed — only the parameters are in play, which is the actual use
   case: "I set the wrong syslog year").

   Two panes: what to make, listed down the left (searchable, with the
   analyst's pinned favourites on top), and everything about it on the
   right. The frame is a fixed height and so is every part of the right
   pane but the settings box, which takes what is left and scrolls — so a
   different entry, a format with more settings, or a regex sprouting
   named groups changes what is in the boxes and moves none of them. It
   used to be a column of rows sized to its content and centred on the
   screen, so every one of those changes resized the card and slid its
   header up or down. */
export async function openDerivedColumnModal(prefill, editing) {
  let ops;
  try {
    ops = await derivedOps();
  } catch (e) {
    toast('Could not load derived-column formats: ' + e.message, 6000);
    return;
  }
  // Ready derived columns are inputs too — chains ("the JSON field holds
  // XML; now take the XML apart") are a first-class shape.
  const textCols = [...baseColumns(),
    ...S.columns.filter((c) => c.derived && c.derived_status === 'ready')];
  if (!textCols.length) return;
  const entries = deriveEntries(ops);
  const tsEntry = entries.find((e) => e.key === TIMESTAMP_ENTRY);
  const entryOf = (opId) => {
    const op = ops.find((o) => o.id === opId);
    return op && isTimestampOp(op) ? TIMESTAMP_ENTRY : opId;
  };
  const state = {
    column: editing ? editing.derived_from : (prefill || defaultParseColumn(textCols)),
    // Which side-list entry is chosen, and — while that is the timestamp
    // entry — which format. A new column starts on the timestamp entry
    // and lets detection pick its format.
    entry: editing ? entryOf(editing.derived_op) : (tsEntry ? TIMESTAMP_ENTRY : entries[0].key),
    format: editing && entryOf(editing.derived_op) === TIMESTAMP_ENTRY ? editing.derived_op
      : (tsEntry ? tsEntry.formats[0].id : null),
    // Set once the analyst picks a format by hand; detection then
    // suggests rather than overrides, until the column changes.
    formatTouched: false,
    params: {},
    name: editing ? editing.name : '',
  };

  /* Which rows the preview samples. The head of the file is the one
     region a triage filter exists to escape, so a preview taken from it
     can report that every sampled value parses while every row on screen
     fails — which is the whole complaint this control answers.

     Defaults to the view when the analyst has narrowed one, because then
     they have already said which rows they mean; to the whole table
     otherwise, where the two are the same thing anyway. Same two words
     and the same segmented control as the value picker's scope, which is
     the same question asked about a different thing. */
  const narrowed = () => !!(S.view && S.view.row_count != null && S.view.row_count
    !== (S.sources.find((x) => x.id === S.sourceId) || {}).row_count);
  state.scope = narrowed() ? 'view' : 'table';

  modal(editing ? `Re-derive "${editing.name}"` : 'Add derived column', (body) => {
    const paramBox = el('div', 'derived-params');
    const nameInput = el('input');
    nameInput.className = 'derived-name';
    const colSelect = el('select');
    const formatSelect = el('select');
    const detectNote = el('div', 'fb-help derived-detect');
    const descBox = el('p', 'derived-desc');
    const settingsBox = el('div', 'side-settings derived-settings');
    const previewBox = el('div', 'side-preview derived-preview');
    const previewRows = el('div', 'side-preview-rows');
    const previewFoot = el('div', 'side-preview-foot');
    previewBox.append(previewRows, previewFoot);

    for (const c of textCols) {
      const o = el('option', null, c.derived ? `${c.name} · derived` : c.name);
      o.value = c.name;
      colSelect.append(o);
    }
    colSelect.value = state.column;
    colSelect.disabled = !!editing;
    for (const f of (tsEntry ? tsEntry.formats : [])) formatSelect.append(new Option(f.label, f.id));
    formatSelect.disabled = !!editing;
    // Tests address these by role rather than by position.
    colSelect.dataset.role = 'column';
    formatSelect.dataset.role = 'format';
    const formatRow = labeledRow('Format', formatSelect);

    function currentOpId() { return state.entry === TIMESTAMP_ENTRY ? state.format : state.entry; }
    function currentOp() { return ops.find((o) => o.id === currentOpId()); }

    function defaultName() {
      const op = currentOp();
      if (op && op.derived_kind === 'duration') return `${state.column} elapsed`;
      if (op && op.family === 'decode') return `${state.column} (decoded)`;
      if (op && op.derived_kind === 'text') return `${state.column} (extract)`;
      if (op && op.id === 'row_json') return 'Row as JSON';
      if (op && op.derived_kind === 'combine') return `${state.column} (combined)`;
      return `${state.column} (parsed)`;
    }
    // "(parsed)" vs "(extract)" tracks the op kind — the suggestion stays
    // current until the analyst has typed a name of their own.
    let nameTouched = false;
    function suggestName() {
      if (editing || nameTouched) return;
      state.name = defaultName();
      nameInput.value = state.name;
    }

    function buildParams() {
      paramBox.replaceChildren();
      const op = currentOp();
      if (!op) return;
      for (const spec of op.params) {
        // A chips widget can't live in a <label>: a label forwards a click
        // on its text (or on a chip's name) to its first labelable
        // descendant, which is the first chip's ✕ — clicking "Then try"
        // removed a column. A div row for that kind, a label for the rest.
        const row = el(spec.type === 'columns' ? 'div' : 'label', 'derived-param');
        row.append(el('span', 'derived-param-label', spec.label + (spec.required ? ' *' : '')));
        let input;
        if (spec.type === 'select') {
          input = el('select');
          for (const opt of spec.options) {
            const o = el('option', null, opt);
            o.value = opt;
            input.append(o);
          }
        } else if (spec.type === 'lookup_source') {
          // The table to look values up in — real tables only (a merge has
          // no single table behind it), the open one excluded.
          input = el('select');
          for (const src of S.sources.filter((x) => !x.is_merge && !x.error && x.id > 0 && x.id !== S.sourceId)) {
            const o = el('option', null, `${src.name} (${src.row_count.toLocaleString()})`);
            o.value = String(src.id);
            input.append(o);
          }
          // Changing the table invalidates the dependent column picks.
          input.addEventListener('change', () => {
            state.params[spec.name] = input.value;
            for (const other of currentOp().params) {
              if (other.type === 'lookup_column') delete state.params[other.name];
            }
            buildParams();
            refreshBoth();
          });
        } else if (spec.type === 'lookup_column') {
          // A column OF THE CHOSEN LOOKUP TABLE, not of the open one.
          input = el('select');
          const chosen = S.sources.find((x) => String(x.id) === String(state.params.other_source_id));
          for (const c of (chosen ? chosen.columns : [])) {
            const o = el('option', null, c.name);
            o.value = c.name;
            input.append(o);
          }
        } else if (spec.type === 'column') {
          input = el('select');
          for (const c of S.columns) {
            if (c.name === state.name) continue;
            const o = el('option', null, c.name);
            o.value = c.name;
            input.append(o);
          }
        } else if (spec.type === 'bool') {
          // A checkbox; the state holds a real boolean (the server's bool
          // param type), so the scalar `.value` sync below is skipped.
          input = el('input');
          input.type = 'checkbox';
          const existingBool = state.params[spec.name];
          input.checked = existingBool === true || existingBool === 'true' || (existingBool == null && spec.default === true);
          state.params[spec.name] = input.checked;
          input.onchange = () => { state.params[spec.name] = input.checked; refreshBoth(); };
          row.classList.add('derived-param-bool');
          row.append(input);
          if (spec.help) row.append(el('span', 'fb-help derived-param-help', spec.help));
          paramBox.append(row);
          continue;
        } else if (spec.type === 'columns') {
          // An ORDERED list of columns — chips plus an add-one select, the
          // shape the filter builder's group-by uses. Order is the
          // meaning (coalesce tries them in turn), so it isn't a
          // multi-select, whose order is the option order. The state
          // holds an array, not a string, so the scalar `.value` sync
          // below is skipped for this kind.
          // any_type: every column may go in (row_json), not just text
          // ones; prefill "all": start with every other column listed,
          // the analyst removes rather than adds.
          const pool = spec.any_type ? S.columns : textCols;
          const chosen = Array.isArray(state.params[spec.name]) ? [...state.params[spec.name]]
            : spec.prefill === 'all' ? pool.filter((c) => c.name !== state.column && c.name !== state.name).map((c) => c.name)
              : [];
          state.params[spec.name] = chosen;
          input = el('div', 'fb-groupby-chips derived-columns');
          const paint = () => {
            input.replaceChildren();
            chosen.forEach((name, i) => {
              const chip = el('span', 'fb-groupby-chip', name);
              const rm = el('button', 'fb-groupby-rm', '✕');
              rm.type = 'button';
              rm.title = `Stop reading ${name}`;
              rm.onclick = () => { chosen.splice(i, 1); paint(); refreshBoth(); };
              chip.append(rm);
              input.append(chip);
            });
            const add = el('select', 'fb-groupby-add');
            add.append(new Option(chosen.length ? '+ then…' : '+ column…', ''));
            for (const c of pool) {
              if (c.name === state.column || c.name === state.name || chosen.includes(c.name)) continue;
              add.append(new Option(c.name, c.name));
            }
            add.onchange = () => { if (add.value) { chosen.push(add.value); paint(); refreshBoth(); } };
            input.append(add);
          };
          paint();
          // Its help goes on a line of its own: beside a row of chips in
          // the narrower right-hand pane it was squeezed to a word a line.
          row.classList.add('derived-param-wrap');
          row.append(input);
          if (spec.help) row.append(el('span', 'fb-help derived-param-help', spec.help));
          paramBox.append(row);
          continue;
        } else {
          input = el('input');
          if (spec.type === 'int') input.type = 'number';
          input.placeholder = spec.type === 'offset' ? '+00:00' : '';
        }
        const existing = state.params[spec.name];
        if (existing != null && existing !== '') input.value = existing;
        else if (spec.default != null) input.value = spec.default;
        else if (spec.type === 'int' && spec.name === 'base_year') input.value = new Date().getFullYear();
        else if (spec.type === 'lookup_source' && input.options.length) input.value = input.options[0].value;
        state.params[spec.name] = input.value;
        input.oninput = () => { state.params[spec.name] = input.value; refreshBoth(); };
        input.onchange = () => { state.params[spec.name] = input.value; refreshBoth(); };
        row.append(input);
        if (spec.help) row.append(el('span', 'fb-help derived-param-help', spec.help));
        paramBox.append(row);
      }
    }

    /* The settings box's bottom edge shades while more of it is out of
       sight below — a fixed box that scrolls has to say that it does. */
    function syncMore() {
      requestAnimationFrame(() => settingsBox.classList.toggle('more',
        settingsBox.scrollTop + settingsBox.clientHeight < settingsBox.scrollHeight - 2));
    }
    settingsBox.addEventListener('scroll', syncMore);

    /* Everything in the settings box for the chosen entry: the Format
       picker and what detection made of the column (the timestamp entry
       only), what the operation does, then its parameters. */
    function renderSettings() {
      settingsBox.replaceChildren();
      if (state.entry === TIMESTAMP_ENTRY) {
        formatSelect.value = state.format;
        settingsBox.append(formatRow, detectNote);
      }
      const op = currentOp();
      descBox.textContent = op ? op.description : '';
      settingsBox.append(descBox, paramBox, groupsBox, groupsNote);
      settingsBox.scrollTop = 0;
      buildParams();
      syncMore();
    }

    /* ------------------------------------------- regex named groups

       One pattern over one column is usually several columns of intent —
       an IIS line is a method, a path, a status and a time. Deriving them
       one at a time meant writing the same regex once per field, each
       with a different group number, and a full scan of the table for
       each. When the pattern names its groups, they are offered as a
       batch: one scan, N columns, all-or-nothing (the same
       /api/derived/batch the JSON flatten picker uses).

       Each column is an ordinary regex_extract definition carrying
       `group_name`, not a slice of some new multi-output op — so it
       re-derives, exports and lives in a session exactly like a
       hand-made one, and a name survives the pattern being edited
       around it in a way a group number does not. */
    const groupsBox = el('div', 'derived-groups');
    const groupsNote = el('div', 'fb-help bad');
    let groups = [];          // [{name, samples, use, colName}]
    let groupsSeq = 0;

    function groupsWanted() {
      return groups.filter((g) => g.use);
    }
    function takenNames() {
      return new Set(S.columns.map((c) => c.name.toLowerCase()));
    }
    function groupsProblem() {
      const wanted = groupsWanted();
      const taken = takenNames();
      const seen = new Set();
      for (const g of wanted) {
        const n = (g.colName || '').trim();
        if (!n) return 'Every column needs a name.';
        if (taken.has(n.toLowerCase())) return `This table already has a column called “${n}”.`;
        if (seen.has(n.toLowerCase())) return `Two of these are both called “${n}”.`;
        seen.add(n.toLowerCase());
      }
      return null;
    }

    function renderGroups(note) {
      groupsBox.replaceChildren();
      if (note) groupsBox.append(el('div', 'fb-help', note));
      if (!groups.length) { syncGo(); return; }
      const n = groups.length;
      groupsBox.append(el('div', 'derived-groups-title',
        `This pattern names ${n} group${n === 1 ? '' : 's'} — make a column for each?`));
      for (const g of groups) {
        const row = el('div', 'derived-group-row');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = g.use;
        cb.onchange = () => { g.use = cb.checked; syncGo(); };
        const name = el('input', 'derived-group-name');
        name.value = g.colName;
        name.oninput = () => { g.colName = name.value; syncGo(); };
        const lab = el('label', 'derived-group-pick');
        lab.append(cb, el('span', 'derived-group-key', g.name));
        row.append(lab, name);
        row.append(el('span', 'derived-group-samples',
          g.samples.length ? g.samples.join(' · ') : '(nothing matched)'));
        groupsBox.append(row);
      }
      syncGo();
    }

    /* The offer lands below the pattern's own settings, which in a box of
       fixed height can be out of sight. Bring it up the first time it
       appears — but never so far that the field being typed in scrolls
       away above. */
    function revealGroups() {
      const want = groupsBox.offsetTop + groupsBox.offsetHeight - settingsBox.clientHeight + 6;
      const active = document.activeElement;
      const cap = active && settingsBox.contains(active) ? active.offsetTop - 6 : want;
      const top = Math.min(want, cap);
      if (top > settingsBox.scrollTop) settingsBox.scrollTop = top;
    }

    async function refreshGroups() {
      // Not while re-deriving: that edits one existing column, and Re-derive
      // never sends a batch (syncGo), so offering "a column for each"
      // would be a set of checkboxes that do nothing. Returning before
      // renderGroups matters as well: the editing branch below calls this
      // before the Re-derive button exists, and renderGroups' syncGo reads
      // it — every Re-derive used to throw "Cannot access 'go' before
      // initialization" into the console that way.
      if (editing) return;
      const seq = ++groupsSeq;
      if (currentOpId() !== 'regex_extract' || !(state.params.pattern || '').trim()) {
        groups = [];
        renderGroups(null);
        syncMore();
        return;
      }
      let res;
      try {
        res = await post('/api/derived/regex_groups', {
          source_id: S.sourceId, column: state.column, pattern: state.params.pattern,
        });
      } catch {
        if (seq !== groupsSeq) return;
        groups = [];
        renderGroups(null);   // the preview below already says what is wrong with the pattern
        syncMore();
        return;
      }
      if (seq !== groupsSeq) return;
      const wasEmpty = !groups.length;
      const before = new Map(groups.map((g) => [g.name, g]));
      groups = res.groups.map((g) => {
        const had = before.get(g.name);
        return { name: g.name, samples: g.samples, use: had ? had.use : true,
                 colName: had ? had.colName : g.name };
      });
      renderGroups(groups.length && res.matched === 0
        ? 'The pattern matched none of the sampled values — the columns would all be empty.'
        : null);
      if (wasEmpty && groups.length) revealGroups();
      syncMore();
    }

    /* Declared before refreshPreview so its 409 fallback can call it —
       the two are one control: the buttons say what the sample is, and
       the fallback changes what the sample is. */
    function syncScope() {
      for (const b of body.querySelectorAll('.derived-scope .btn')) {
        b.setAttribute('aria-pressed', String(b.dataset.scope === state.scope));
      }
    }

    /* While a new answer is on its way, the last one stays on screen,
       dimmed, rather than the box emptying to a "Checking…" line: an
       emptied box that refills is a box that changes height, and it read
       as a flicker on every keystroke in a pattern. aria-busy is how a
       test (or a screen reader) knows the rows on screen are the old
       answer's. */
    function previewBusy(on) {
      previewBox.classList.toggle('stale', on);
      previewBox.setAttribute('aria-busy', String(on));
    }
    function previewError(message) {
      previewRows.replaceChildren(el('div', 'fb-help bad', message));
      previewFoot.replaceChildren();
      previewBusy(false);
    }

    let previewSeq = 0;
    async function refreshPreview() {
      const seq = ++previewSeq;
      previewBusy(true);
      let res;
      const ask = (scope) => post('/api/derived/preview', {
        source_id: S.sourceId, column: state.column, op_id: currentOpId(), params: state.params,
        view_id: scope === 'view' && S.view ? S.view.view_id : undefined,
      });
      try {
        res = await ask(state.scope);
      } catch (e) {
        /* Before anything else: a superseded request may not touch the
           modal at all. The fallback below repaints the scope buttons and
           raises a toast, and doing that on behalf of a request nobody is
           waiting on leaves the pressed button, the verdict sentence and
           the rows under it describing three different samples. The other
           branches each guarded for themselves; this one has to guard
           first, because its first act is already a visible one. */
        if (seq !== previewSeq) return;
        // A 409 is the view going out from under the modal mid-rebuild.
        // Fall back to the table rather than showing an error for a
        // question the analyst can still have answered — the value
        // picker does the same, and says so.
        if (e.status === 409 && state.scope === 'view') {
          state.scope = 'table';
          syncScope();
          toast('That view was rebuilt — previewing against the whole table');
          try { res = await ask('table'); } catch (e2) {
            if (seq !== previewSeq) return;   // the retry itself can be overtaken
            previewError(e2.message);
            return;
          }
        } else {
          previewError(e.message);
          return;
        }
      }
      if (seq !== previewSeq) return; // a later keystroke already superseded this
      const table = el('div', 'derived-preview-rows-list');
      for (const row of res.preview) {
        const r = el('div', 'derived-preview-row');
        r.append(el('span', 'derived-in', String(row.input == null ? '' : row.input)));
        r.append(el('span', 'derived-arrow', '→'));
        r.append(el('span', 'derived-out' + (row.output == null ? ' bad' : ''),
                    row.output == null ? "can't parse" : row.output));
        table.append(r);
      }
      previewRows.replaceChildren(table);
      /* The verdict names the sample it is a verdict about. "All 200
         sampled values parse" is a very different claim depending on
         which 200, and it used to be silent about that. The name comes
         from the answer's own `scope`, never from the button that was
         pressed: an operation that reads several columns of a row at once
         is not scoped server-side, so asking for the view still gets the
         table — and the sentence has to say the table. */
      const where = res.scope === 'view' ? 'in this view' : 'in the whole table';
      const verdict = res.failures
        ? `${res.failures.toLocaleString()} of ${res.sampled.toLocaleString()} sampled values ${where} can't be parsed this way.`
        : `All ${res.sampled.toLocaleString()} sampled values ${where} parse.`;
      const v = el('div', 'fb-help derived-verdict' + (res.failures ? ' bad' : ''), verdict);
      v.title = verdict;
      previewFoot.replaceChildren(v);
      if (state.scope === 'view' && res.scope !== 'view') {
        // Otherwise the pressed button and the verdict disagree with
        // nothing on screen to explain which one to believe.
        const note = el('div', 'fb-help derived-scope-note',
          'This reads more than one column of each row, so the preview samples the whole table.');
        note.title = note.textContent;
        previewFoot.append(note);
      }
      previewBusy(false);
    }

    /* The preview and the named-group offer answer the same edits — the
       pattern, the column, the operation — so they refresh together. The
       group probe no-ops unless the operation is the regex one. */
    function refreshBoth() { refreshPreview(); refreshGroups(); syncMore(); }

    /* ----------------------------------------- the format suggestion

       Detection ranks timestamp formats for a column. It runs whenever
       the column changes, whatever entry is chosen, and is remembered per
       column, so going to the timestamp entry later costs no second
       round trip. It picks the format only while the timestamp entry is
       chosen and the analyst hasn't picked one by hand since the column
       last changed; otherwise it is shown, not applied. The note's text
       also goes on the body as data-detected, which is what a test waits
       on to know the answer has landed. */
    const detected = new Map();   // column -> Promise of the ranked list
    let detectSeq = 0;
    let ranked = [];
    let rankedFor = null;         // the column `ranked` is about
    function detectFor(column) {
      if (!detected.has(column)) {
        detected.set(column, post('/api/derived/detect', { source_id: S.sourceId, column })
          .catch(() => []));   // detection is a convenience — the picker still works
      }
      return detected.get(column);
    }
    function applyDetection() {
      if (state.entry !== TIMESTAMP_ENTRY || state.formatTouched || rankedFor !== state.column || !ranked.length) return;
      const best = ranked[0];
      if (!tsEntry.formats.some((f) => f.id === best.op_id)) return;
      state.format = best.op_id;
      state.params = Object.assign({}, best.params);
    }

    async function pickColumn(name) {
      state.column = name;
      state.formatTouched = false;
      suggestName();
      const seq = ++detectSeq;
      delete body.dataset.detected;
      detectNote.textContent = 'Detecting format…';
      const waitForFormat = state.entry === TIMESTAMP_ENTRY;
      if (waitForFormat) {
        // The preview would otherwise answer for whatever format was
        // showing, then again for the detected one.
        previewBusy(true);
      } else {
        buildParams();
        refreshBoth();
      }
      const got = await detectFor(name);
      if (seq !== detectSeq) return;
      ranked = got;
      rankedFor = name;
      if (ranked.length) {
        const best = ranked[0];
        detectNote.textContent =
          `Suggested: ${best.label} — ${Math.round(best.confidence * 100)}% of sampled values parse.`;
      } else {
        detectNote.textContent = 'No format detected — pick one to see what it produces.';
      }
      body.dataset.detected = detectNote.textContent;
      // Shown, not applied: another entry is chosen, or the analyst picked
      // a format while this was on its way — and either already refreshed
      // the preview for what they chose.
      if (state.entry !== TIMESTAMP_ENTRY || state.formatTouched) return;
      applyDetection();
      suggestName();
      renderSettings();
      refreshBoth();
    }

    /* ---------------------------------------------- the side list */
    function setEntry(key, format) {
      if (editing) return;
      if (key === state.entry && !format) return;
      state.entry = key;
      state.params = {};
      groups = [];
      groupsSeq++;
      renderGroups(null);
      if (key === TIMESTAMP_ENTRY) {
        if (format) { state.format = format; state.formatTouched = true; }
        applyDetection();
      }
      suggestName();
      side.sync();
      renderSettings();
      refreshBoth();
    }

    // The shared side list (ui.js), with the search reaching into the
    // timestamp entry's formats: a hit found through one names it, and
    // picking the hit picks it.
    const side = sideRail({
      entries: () => entries,
      groups: DERIVE_GROUPS.map(([name]) => name).concat('Other'),
      selected: () => state.entry,
      onPick: (entry, hit) => setEntry(entry.key, hit.format),
      match: (entry, q) => {
        const hit = matchesDeriveSearch(entry, q);
        if (!hit || !hit.format) return hit;
        const f = entry.formats.find((x) => x.id === hit.format);
        return { ...hit, hint: f ? f.label : hit.format };
      },
      pinsKey: DERIVE_PINS,
      locked: !!editing,
      label: 'What to make',
    });

    colSelect.onchange = () => pickColumn(colSelect.value);
    formatSelect.onchange = () => {
      state.format = formatSelect.value;
      state.formatTouched = true;
      state.params = {};
      suggestName();
      renderSettings();
      refreshBoth();
    };
    nameInput.oninput = () => { nameTouched = true; state.name = nameInput.value; };

    /* Left: the search box and the list. Right, top to bottom: the column
       it reads, the settings box, the new column's name, the preview. */
    const pane = el('div', 'side-pane');
    pane.append(labeledRow('Parse column', colSelect), settingsBox);
    // The name row keeps its place when a regex's named groups take over
    // (syncGo): the input steps aside for a line saying why, rather than
    // the row folding away and everything under it moving up.
    let nameRow = null;
    const nameAside = el('span', 'derived-name-aside', 'Each ticked group above becomes a column of its own.');
    if (!editing) {
      nameRow = labeledRow('New column name', nameInput);
      nameRow.classList.add('derived-name-row');
      nameRow.append(nameAside);
      pane.append(nameRow);
    }
    /* The preview's own scope control, beside the word Preview, because
       what it changes is which rows the answer below is about. Same
       shape and the same two labels as the value picker's (filters.js),
       so an analyst who has met one has met both. */
    const previewHead = el('div', 'side-preview-title', 'Preview');
    const scopeSeg = el('div', 'vp-seg derived-scope');
    for (const [key, label, title] of [
      ['view', 'This view', 'Sample the rows the current filters leave — what you are actually looking at'],
      ['table', 'Whole table', 'Sample from the top of the table, ignoring the current filters'],
    ]) {
      const b = el('button', 'btn ghost', label);
      b.dataset.scope = key;
      b.title = title;
      b.onclick = () => { if (state.scope !== key) { state.scope = key; syncScope(); refreshPreview(); } };
      scopeSeg.append(b);
    }
    previewHead.append(scopeSeg);
    pane.append(previewHead, previewBox);
    const panes = el('div', 'side-panes');
    panes.append(side.rail, pane);
    body.append(panes);
    syncScope();

    const actions = el('div', 'row-actions');
    const go = el('button', 'btn primary', editing ? 'Re-derive' : 'Add column');
    /* The one place the button's label, its enabled state and the single
       name field agree about what is being made: one column, or the
       ticked named groups. */
    function syncGo() {
      const wanted = editing ? [] : groupsWanted();
      const problem = wanted.length ? groupsProblem() : null;
      go.textContent = editing ? 'Re-derive'
        : (wanted.length ? `Add ${wanted.length} column${wanted.length === 1 ? '' : 's'}` : 'Add column');
      go.disabled = !!problem;
      go.title = problem || '';
      // One column's name is beside the point when several are being made.
      if (nameRow) nameRow.classList.toggle('by-group', wanted.length > 0);
      groupsNote.textContent = problem || '';
    }
    go.onclick = async () => {
      go.disabled = true;
      try {
        let res;
        const batch = editing ? [] : groupsWanted();
        if (batch.length) {
          // One scan, N columns, all-or-nothing — a name collision in the
          // fourth spec fails before the first column exists, rather than
          // leaving three behind to clean up (add_derived_columns).
          await post('/api/derived/batch', {
            source_id: S.sourceId,
            columns: batch.map((g) => ({
              name: g.colName.trim(), input_column: state.column, op_id: 'regex_extract',
              params: { pattern: state.params.pattern, group_name: g.name },
            })),
          });
          $('modal').hidden = true;
          await loadSources();
          await openSource(S.sourceId);
          startJobsPoll();
          toast(`Adding ${batch.length} column${batch.length === 1 ? '' : 's'}…`);
          return;
        }
        if (editing) {
          res = await post(`/api/derived/${editing.derived_id}/rederive`,
            { params: state.params, source_id: S.sourceId });
        } else {
          res = await post('/api/derived', {
            source_id: S.sourceId, name: state.name, input_column: state.column,
            op_id: currentOpId(), params: state.params,
          });
        }
        $('modal').hidden = true;
        // The column shows up immediately (status 'building'); the jobs
        // panel tracks the backfill and refreshAfterDerive fills it in.
        await loadSources();
        await openSource(S.sourceId);
        startJobsPoll();
        const chain = (editing && res.cascades_to) || [];
        toast((editing ? `Re-deriving "${editing.name}"…` : `Adding "${state.name}"…`)
          + (chain.length ? ` — ${chain.join(', ')} ${chain.length === 1 ? 'is' : 'are'} derived from it and will recompute too` : ''), chain.length ? 7000 : 4000);
      } catch (e) {
        go.disabled = false;
        toast('Could not add column: ' + e.message, 8000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(go, cancel);
    body.append(actions);

    suggestName();
    side.paint();
    renderSettings();
    if (editing) {
      api(`/api/derived?source_id=${S.sourceId}`).then((defs) => {
        const d = defs.find((x) => x.id === editing.derived_id);
        if (d) { state.params = Object.assign({}, d.params); buildParams(); refreshBoth(); }
      }).catch(() => {});
      refreshBoth();
    } else {
      pickColumn(state.column);
    }
  }, { wide: true, tall: true, focus: editing ? null : '.side-search' });
}

/* After an import, if a column looks strongly like a timestamp the app
   can't already read (an epoch, a syslog line), say so once. A toast with
   an action rather than a modal: the analyst asked to import a file, not
   to be interrupted — and a column that isn't converted still shows and
   searches exactly as before. */
/* Source ids restart per case, so this has to be cleared with one —
   otherwise case B's source 1 never gets its "looks like an epoch
   timestamp" offer because case A's source 1 already consumed it. */
export const suggestedSources = new Set();

export function resetDerivedSuggestions() { suggestedSources.clear(); }

export async function offerTimestampColumns(sourceId) {
  if (suggestedSources.has(sourceId)) return;
  suggestedSources.add(sourceId);
  let suggestions = [];
  try {
    suggestions = await api(`/api/derived/suggestions?source_id=${sourceId}`);
  } catch { return; }
  if (!suggestions.length) return;
  const s = suggestions[0];
  const src = (S.sources || []).find((x) => x.id === sourceId);
  const more = suggestions.length > 1 ? ` (and ${suggestions.length - 1} more)` : '';
  toastAction(
    `"${s.column}" in ${src ? src.name : 'the new table'} looks like ${s.label}${more}`,
    'Add datetime column',
    async () => {
      if (S.sourceId !== sourceId) await openSource(sourceId);
      openDerivedColumnModal(s.column);
    });
}

export function labeledRow(label, control) {
  const row = el('label', 'derived-row');
  row.append(el('span', 'derived-row-label', label));
  row.append(control);
  return row;
}

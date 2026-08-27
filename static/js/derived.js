/* Derived datetime columns and extracted (JSON/XML) columns — the analyst's
own additions to a source's column set.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { saveLayout } from './columns.js';
import { $, api, el, post, setBusy, toast, toastAction } from './core.js';
import { ellipsize } from './filters.js';
import { render } from './grid.js';
import { startJobsPoll } from './jobs.js';
import { loadSources, openSource } from './sources.js';
import { S } from './state.js';
import { updateFiltersButton } from './timeframe.js';
import { TS_FORMATS, baseColumns, columnMeta, tsFormatFor } from './tsformat.js';
import { confirmDialog, modal, promptDialog } from './ui.js';
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
  items.push({ label: 'Add datetime column from this…', onclick: () => openDerivedColumnModal(name) });
  // Offered on any base column rather than only ones that sniff as
  // structured: the check costs a sample scan, the menu is built
  // synchronously, and a column of JSON that happens to start with a
  // non-document row would silently lose the entry. The picker itself says
  // so when there's nothing in there.
  if (!c.derived && S.sourceId >= 0) {
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
    if (c.derived_kind === 'text') {
      items.push({ label: 'Change the field path…', onclick: () => editExtractedPath(c) });
    } else {
      items.push({ label: 'Re-derive…', onclick: () => openDerivedColumnModal(c.derived_from, c) });
    }
    items.push({ label: 'Remove derived column…', onclick: () => removeDerivedColumn(c) });
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

/* An extracted column's whole definition is its path, so "re-derive" here
   is "edit the path" — the timestamp modal's format-and-parameters shape
   has nothing to offer it. Recomputes in place via the same rederive
   endpoint, so the column keeps its name, position and width. */
export async function editExtractedPath(c) {
  const current = (c.derived_params || {}).path || '';
  const next = await promptDialog(
    `Field path for "${c.name}" (read from "${c.derived_from}"):`, current, { okLabel: 'Recompute' });
  if (next === null || next.trim() === current) return;
  try {
    const res = await post(`/api/derived/${c.derived_id}/rederive`, { params: { path: next.trim() } });
    await showDerivedColumnsSoon();
    const chain = res.cascades_to || [];
    toast(`Recomputing "${c.name}"…`
      + (chain.length ? ` — ${chain.join(', ')} ${chain.length === 1 ? 'is' : 'are'} derived from it and will recompute too` : ''), chain.length ? 7000 : 4000);
  } catch (e) {
    toast('Could not change the path: ' + e.message, 6000);
  }
}

export async function removeDerivedColumn(c) {
  const ok = await confirmDialog(
    `Remove the derived column "${c.name}"? Its values are recomputed from "${c.derived_from}", so it can be added back at any time.`,
    { okLabel: 'Remove', danger: true });
  if (!ok) return;
  try {
    await api(`/api/derived/${c.derived_id}`, { method: 'DELETE' });
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

/* Suggests the column name the way structparse.suggest_name does — the
   last meaningful component of the path, or an EVTX-style predicate's own
   value, which is nearly always what the analyst would have typed. */
export function suggestColumnName(path, kind) {
  const s = String(path);
  const pred = s.match(/\[@[\w:.-]+='([^']*)'\]$/);
  if (pred && pred[1].trim()) return pred[1].trim();
  if (s.includes('@') && !s.trim().endsWith(']')) {
    const at = s.lastIndexOf('@');
    const attr = s.slice(at + 1);
    const tail = s.slice(0, at).replace(/\/$/, '').split('/').pop().replace(/\[[^\]]*\]$/, '');
    return (tail ? `${tail} ${attr}` : attr).trim();
  }
  if (kind === 'json') {
    const parts = s.replace(/^\$/, '').match(/\["'](?:[^"']*)["']|\[\d+\]|[^.[\]]+/g) || [];
    for (let i = parts.length - 1; i >= 0; i--) {
      const seg = parts[i];
      if (/^\[\d+\]$/.test(seg)) continue;
      return seg.replace(/^\["']|["']\]$/g, '').replace(/^\[|\]$/g, '');
    }
    return s;
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
   immediately with status 'building' and fill in top-down; the jobs panel
   tracks progress and pollJobs reports the result. Blocking the UI on a
   pass over a million rows would be the wrong trade for a column the
   analyst can already see taking shape. */
export async function showDerivedColumnsSoon() {
  await loadSources();
  await openSource(S.sourceId);
  startJobsPoll();
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

/* The add/re-derive modal. `prefill` is the column to parse; `editing` is
   the existing definition when re-deriving (the column and operation are
   then fixed — only the parameters are in play, which is the actual use
   case: "I set the wrong syslog year"). */
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
  const state = {
    column: editing ? editing.derived_from : (prefill || textCols[0].name),
    opId: editing ? editing.derived_op : null,
    params: {},
    name: editing ? editing.name : '',
  };

  modal(editing ? `Re-derive "${editing.name}"` : 'Add derived column', (body) => {
    const previewBox = el('div', 'derived-preview');
    const paramBox = el('div', 'derived-params');
    const nameInput = el('input');
    nameInput.className = 'derived-name';
    const opSelect = el('select');
    const colSelect = el('select');
    const suggestNote = el('div', 'fb-help');

    for (const c of textCols) {
      const o = el('option', null, c.derived ? `${c.name} · derived` : c.name);
      o.value = c.name;
      colSelect.append(o);
    }
    colSelect.value = state.column;
    colSelect.disabled = !!editing;

    // Grouped, not one flat list: with timestamps, extraction and
    // comparisons all in the registry, thirteen timestamp formats drowned
    // the other kinds. A two-input operation (duration) still needs a
    // second column rather than a format guess, so it's offered but never
    // auto-suggested.
    const OP_GROUPS = [
      ['Timestamps', (op) => op.family === 'datetime' && op.derived_kind !== 'duration'],
      ['Extract part of a value', (op) => op.family === 'extract'],
      ['Comparisons', (op) => op.derived_kind === 'duration'],
    ];
    const grouped = new Set();
    for (const [label, match] of OP_GROUPS) {
      const members = ops.filter((op) => match(op) && !grouped.has(op.id));
      if (!members.length) continue;
      const g = document.createElement('optgroup');
      g.label = label;
      for (const op of members) {
        grouped.add(op.id);
        const o = el('option', null, op.label);
        o.value = op.id;
        g.append(o);
      }
      opSelect.append(g);
    }
    for (const op of ops) {
      if (grouped.has(op.id)) continue; // a future family lands ungrouped rather than invisible
      const o = el('option', null, op.label);
      o.value = op.id;
      opSelect.append(o);
    }

    function currentOp() { return ops.find((o) => o.id === opSelect.value); }

    function defaultName() {
      const op = currentOp();
      if (op && op.derived_kind === 'duration') return `${state.column} elapsed`;
      if (op && op.derived_kind === 'text') return `${state.column} (extract)`;
      return `${state.column} (parsed)`;
    }

    function buildParams() {
      paramBox.replaceChildren();
      const op = currentOp();
      if (!op) return;
      for (const spec of op.params) {
        const row = el('label', 'derived-param');
        row.append(el('span', 'derived-param-label', spec.label + (spec.required ? ' *' : '')));
        let input;
        if (spec.type === 'select') {
          input = el('select');
          for (const opt of spec.options) {
            const o = el('option', null, opt);
            o.value = opt;
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
        } else {
          input = el('input');
          if (spec.type === 'int') input.type = 'number';
          input.placeholder = spec.type === 'offset' ? '+00:00' : '';
        }
        const existing = state.params[spec.name];
        if (existing != null && existing !== '') input.value = existing;
        else if (spec.default != null) input.value = spec.default;
        else if (spec.type === 'int' && spec.name === 'base_year') input.value = new Date().getFullYear();
        state.params[spec.name] = input.value;
        input.oninput = () => { state.params[spec.name] = input.value; refreshPreview(); };
        input.onchange = () => { state.params[spec.name] = input.value; refreshPreview(); };
        row.append(input);
        if (spec.help) row.append(el('span', 'fb-help derived-param-help', spec.help));
        paramBox.append(row);
      }
    }

    let previewSeq = 0;
    async function refreshPreview() {
      const seq = ++previewSeq;
      previewBox.replaceChildren(el('div', 'fb-help', 'Checking…'));
      let res;
      try {
        res = await post('/api/derived/preview', {
          source_id: S.sourceId, column: state.column, op_id: opSelect.value, params: state.params,
        });
      } catch (e) {
        if (seq !== previewSeq) return;
        previewBox.replaceChildren(el('div', 'fb-help bad', e.message));
        return;
      }
      if (seq !== previewSeq) return; // a later keystroke already superseded this
      previewBox.replaceChildren();
      const table = el('div', 'derived-preview-rows');
      for (const row of res.preview) {
        const r = el('div', 'derived-preview-row');
        r.append(el('span', 'derived-in', String(row.input == null ? '' : row.input)));
        r.append(el('span', 'derived-arrow', '→'));
        r.append(el('span', 'derived-out' + (row.output == null ? ' bad' : ''),
                    row.output == null ? "can't parse" : row.output));
        table.append(r);
      }
      previewBox.append(table);
      previewBox.append(el('div', 'fb-help derived-verdict' + (res.failures ? ' bad' : ''),
        res.failures
          ? `${res.failures.toLocaleString()} of ${res.sampled.toLocaleString()} sampled values can't be parsed this way.`
          : `All ${res.sampled.toLocaleString()} sampled values parse.`));
    }

    async function pickColumn(name) {
      state.column = name;
      if (!editing) {
        nameInput.value = defaultName();
        state.name = nameInput.value;
      }
      suggestNote.textContent = 'Detecting format…';
      let ranked = [];
      try {
        ranked = await post('/api/derived/detect', { source_id: S.sourceId, column: name });
      } catch { /* detection is a convenience — the picker still works */ }
      if (ranked.length) {
        const best = ranked[0];
        suggestNote.textContent =
          `Suggested: ${best.label} — ${Math.round(best.confidence * 100)}% of sampled values parse.`;
        if (!editing) {
          opSelect.value = best.op_id;
          state.params = Object.assign({}, best.params);
        }
      } else {
        suggestNote.textContent = editing ? '' : "No format detected — pick one below to see what it produces.";
      }
      buildParams();
      refreshPreview();
    }

    colSelect.onchange = () => pickColumn(colSelect.value);
    let nameTouched = false;
    opSelect.onchange = () => {
      state.params = {};
      // "(parsed)" vs "(extract)" tracks the op kind — keep the suggestion
      // current until the analyst has typed a name of their own.
      if (!editing && !nameTouched) { nameInput.value = defaultName(); state.name = nameInput.value; }
      buildParams();
      refreshPreview();
    };
    nameInput.oninput = () => { nameTouched = true; state.name = nameInput.value; };

    body.append(labeledRow('Parse column', colSelect));
    body.append(suggestNote);
    body.append(labeledRow('Format', opSelect));
    body.append(paramBox);
    if (!editing) {
      nameInput.value = defaultName();
      state.name = nameInput.value;
      body.append(labeledRow('New column name', nameInput));
    }
    body.append(el('div', 'derived-preview-title', 'Preview'));
    body.append(previewBox);

    if (editing) {
      state.opId = editing.derived_op;
      opSelect.value = editing.derived_op;
      opSelect.disabled = true;
      api(`/api/derived?source_id=${S.sourceId}`).then((defs) => {
        const d = defs.find((x) => x.id === editing.derived_id);
        if (d) { state.params = Object.assign({}, d.params); buildParams(); refreshPreview(); }
      }).catch(() => {});
      buildParams();
      refreshPreview();
    } else {
      pickColumn(state.column);
    }

    const actions = el('div', 'row-actions');
    const go = el('button', 'btn', editing ? 'Re-derive' : 'Add column');
    go.onclick = async () => {
      go.disabled = true;
      try {
        let res;
        if (editing) {
          res = await post(`/api/derived/${editing.derived_id}/rederive`, { params: state.params });
        } else {
          res = await post('/api/derived', {
            source_id: S.sourceId, name: state.name, input_column: state.column,
            op_id: opSelect.value, params: state.params,
          });
        }
        $('modal').hidden = true;
        // The column shows up immediately (status 'building') and fills in
        // top-down as the backfill runs; the jobs panel tracks it.
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
  }, { wide: true });
}

/* After an import, if a column looks strongly like a timestamp the app
   can't already read (an epoch, a syslog line), say so once. A toast with
   an action rather than a modal: the analyst asked to import a file, not
   to be interrupted — and a column that isn't converted still shows and
   searches exactly as before. */
export const suggestedSources = new Set();

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

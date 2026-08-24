/* The merge builder — one view across several sources with the same columns.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, el, post, toast } from './core.js';
import { startJobsPoll, uploadWithProgress } from './jobs.js';
import { loadSources, sourceLabel } from './sources.js';
import { S } from './state.js';
import { modal } from './ui.js';

/* ---------------------------------------------------------- import preview */

/* ------------------------------------------------------------------ merge */

export function columnGroupKey(columns) {
  return columns.map((c) => c.name.trim().toLowerCase()).sort().join('|');
}

export function openMergeBuilder() {
  const real = S.sources.filter((s) => !s.is_merge && !s.error);
  const groups = new Map();
  for (const s of real) {
    const key = columnGroupKey(s.columns);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const eligible = [...groups.values()].filter((g) => g.length >= 2);

  modal('Merge sources', (b) => {
    if (!eligible.length) {
      b.append(el('p', null, 'No two open sources currently share the same columns. Import matching files first.'));
      return;
    }
    b.append(el('p', null,
      'Sources are grouped by matching columns (case-insensitive). Pick 2 or more from the same group — '
      + 'merged rows keep tagging/notes tied to their original file, nothing is copied.'));
    const selected = new Set();
    eligible.forEach((group, gi) => {
      const groupHead = el('div', 'row-actions');
      groupHead.append(el('h4', null, `Group ${gi + 1} — ${group[0].columns.map((c) => c.name).join(', ')}`));
      const list = el('div', 'collist');
      const boxes = [];
      for (const s of group) {
        const lab = el('label');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.onchange = () => { cb.checked ? selected.add(s.id) : selected.delete(s.id); };
        boxes.push(cb);
        lab.append(cb, el('span', null, `${sourceLabel(s)} (${s.row_count.toLocaleString()} rows)`));
        list.append(lab);
      }
      const selectAll = el('button', 'btn ghost', 'Select all');
      selectAll.onclick = () => {
        const allChecked = boxes.every((cb) => cb.checked);
        group.forEach((s, i) => {
          boxes[i].checked = !allChecked;
          allChecked ? selected.delete(s.id) : selected.add(s.id);
        });
        selectAll.textContent = allChecked ? 'Select all' : 'Deselect all';
      };
      groupHead.append(selectAll);
      b.append(groupHead, list);
    });

    const nameRow = el('div', 'row-actions');
    const nameInput = el('input');
    nameInput.placeholder = 'Merge name';
    nameInput.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    nameRow.append(nameInput);
    b.append(nameRow);

    const acts = el('div', 'row-actions');
    const create = el('button', 'btn', 'Create merge');
    create.onclick = async () => {
      if (selected.size < 2) { toast('Select at least 2 sources from the same group'); return; }
      const name = nameInput.value.trim() || 'Merged view';
      try {
        const rec = await post('/api/merges', { name, source_ids: [...selected] });
        $('modal').hidden = true;
        await loadSources(rec.id);
        toast(`Created merge "${rec.name}" · ${rec.row_count.toLocaleString()} rows`);
      } catch (e) {
        toast('Merge failed: ' + e.message, 6000);
      }
    };
    acts.append(create);
    b.append(acts);
  }, { wide: true });
}

/* `src` is a queue item's transport: {file, name} for a browser pick, or
   {path, name} for one added from the server's own disk — previews and the
   direct-import button branch on which field is set, never on a guess.
   `name` is always present (queueItem and every direct caller set it), so
   nothing here reads src.file.name — one contract, one spelling. */
export function openImportPreview(src, opts = {}) {
  let preview = null;
  let columnTypes = opts.initial && opts.initial.column_types ? opts.initial.column_types.slice() : null;

  modal(`Import: ${src.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const delimSel = el('select');
    for (const [label, val] of [['Auto-detect', ''], ['Comma', ','], ['Tab', '\t'], ['Semicolon', ';'], ['Pipe', '|']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      delimSel.append(opt);
    }
    if (opts.initial && opts.initial.delimiter) delimSel.value = opts.initial.delimiter;
    const headerLabel = el('label');
    const headerCb = el('input');
    headerCb.type = 'checkbox';
    headerCb.checked = opts.initial ? opts.initial.has_header !== false : true;
    headerLabel.append(headerCb, document.createTextNode(' First row is headers'));
    controls.append(delimSel, headerLabel);
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      status.textContent = `Detected delimiter: ${JSON.stringify(preview.delimiter)} · showing first ${preview.sample_rows.length} rows`;
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c));
        const typeSel = el('select');
        for (const ty of ['text', 'number', 'datetime']) {
          const opt = document.createElement('option');
          opt.value = ty; opt.textContent = ty;
          if (columnTypes[i] === ty) opt.selected = true;
          typeSel.append(opt);
        }
        typeSel.onchange = () => { columnTypes[i] = typeSel.value; };
        th.append(typeSel);
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // A path-queued item (the "Add from this machine…" picker) previews
        // in place; a browser-picked File uploads its sample. Which one is
        // a fact about how the item arrived, not a guess about the disk.
        if (src.path) {
          preview = await post('/api/ingest/preview/path', {
            path: src.path, kind: 'csv',
            delimiter: delimSel.value || null, has_header: headerCb.checked,
          });
        } else {
          const fd = new FormData();
          fd.append('file', src.file);
          if (delimSel.value) fd.append('delimiter', delimSel.value);
          fd.append('has_header', headerCb.checked ? 'true' : 'false');
          preview = await api('/api/ingest/preview', { method: 'POST', body: fd });
        }
        if (!columnTypes || columnTypes.length !== preview.columns.length) columnTypes = preview.inferred_types.slice();
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    delimSel.onchange = refreshPreview;
    headerCb.onchange = refreshPreview;
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      const settings = { delimiter: delimSel.value || null, has_header: headerCb.checked, column_types: columnTypes };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (src.path) { // added by path — import in place, no upload leg
        try {
          await post('/api/ingest/jobs/path', {
            path: src.path, name: src.name, kind: 'csv',
            delimiter: settings.delimiter, has_header: settings.has_header,
            column_types: settings.column_types,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', src.file);
      fd.append('kind', 'csv');
      if (settings.delimiter) fd.append('delimiter', settings.delimiter);
      fd.append('has_header', settings.has_header ? 'true' : 'false');
      fd.append('column_types', JSON.stringify(settings.column_types));
      // Same background pipeline as the queue: transfer with progress, then
      // an ingest job the corner panel tracks.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, src.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* JSON/JSONL import — a single file, previewed live against whichever
   flatten mode is selected (same three modes store.py's ingest_json
   supports: don't flatten nested objects at all, flatten them without
   limit, or flatten only N levels deep — an array is never index-expanded
   at any depth/mode, always kept as its raw JSON text in one column, see
   _flatten_json's docstring for why). Single-file, not a queue — a JSON
   export is usually one file, unlike the CSV queue's "several files from
   the same collection tool" use case. */
export function openJsonImportPreview(src, opts = {}) {
  let preview = null;
  let flattenMode = (opts.initial && opts.initial.flatten_mode) || 'none';
  let flattenDepth = (opts.initial && opts.initial.flatten_depth) || 1;

  modal(`Import: ${src.name}`, (b) => {
    const controls = el('div', 'row-actions');
    const modeSel = el('select');
    for (const [label, val] of [["Don't flatten", 'none'], ['Flatten completely', 'full'], ['Flatten to depth…', 'depth']]) {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      if (val === flattenMode) opt.selected = true;
      modeSel.append(opt);
    }
    const depthInput = el('input');
    depthInput.type = 'number';
    depthInput.min = '1';
    depthInput.value = String(flattenDepth);
    depthInput.style.cssText = 'width:60px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    depthInput.hidden = flattenMode !== 'depth';
    controls.append(modeSel, depthInput);
    b.append(el('p', 'fb-help',
      'A nested object can be flattened into dotted columns (user.name); an array is always kept as its '
      + 'raw JSON text in one column, at any depth, since its length can vary record to record.'));
    b.append(controls);

    const status = el('div', 'note-status', 'Loading preview…');
    b.append(status);
    const tableWrap = el('div', 'preview-table-wrap');
    b.append(tableWrap);

    function renderTable() {
      tableWrap.replaceChildren();
      status.textContent = `${preview.record_count.toLocaleString()} record${preview.record_count === 1 ? '' : 's'} `
        + `· showing first ${preview.sample_rows.length} · ${preview.columns.length} column${preview.columns.length === 1 ? '' : 's'}`;
      const t = el('table', 'preview-tbl');
      const hr = el('tr');
      preview.columns.forEach((c, i) => {
        const th = el('th');
        th.append(el('div', 'preview-colname', c), el('div', 'count', preview.inferred_types[i]));
        hr.append(th);
      });
      t.append(hr);
      for (const row of preview.sample_rows.slice(0, 20)) {
        const tr = el('tr');
        for (const v of row) tr.append(el('td', null, v));
        t.append(tr);
      }
      tableWrap.append(t);
    }

    async function refreshPreview() {
      status.textContent = 'Loading preview…';
      try {
        // The path branch matters most here: a .json document can't be
        // truncated, so the upload preview round-trips the WHOLE file — a
        // path-queued item previews in place instead.
        if (src.path) {
          preview = await post('/api/ingest/preview/path', {
            path: src.path, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
        } else {
          const fd = new FormData();
          fd.append('file', src.file);
          fd.append('flatten_mode', flattenMode);
          fd.append('flatten_depth', String(flattenDepth));
          preview = await api('/api/ingest/json/preview', { method: 'POST', body: fd });
        }
        renderTable();
      } catch (e) {
        status.textContent = 'Preview failed: ' + e.message;
      }
    }

    modeSel.onchange = () => {
      flattenMode = modeSel.value;
      depthInput.hidden = flattenMode !== 'depth';
      refreshPreview();
    };
    depthInput.oninput = debounce(() => {
      flattenDepth = Math.max(1, parseInt(depthInput.value, 10) || 1);
      refreshPreview();
    }, 300);
    refreshPreview();

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', opts.onConfirm ? 'Use these settings' : 'Import');
    importBtn.onclick = async () => {
      const settings = { flatten_mode: flattenMode, flatten_depth: flattenDepth };
      if (opts.onConfirm) {
        $('modal').hidden = true;
        opts.onConfirm(settings);
        return;
      }
      $('modal').hidden = true;
      if (src.path) { // added by path — import in place, no upload leg
        try {
          await post('/api/ingest/jobs/path', {
            path: src.path, name: src.name, kind: 'json',
            flatten_mode: flattenMode, flatten_depth: flattenDepth,
          });
          startJobsPoll();
        } catch (e) {
          toast('Import failed: ' + e.message, 6000);
        }
        return;
      }
      const fd = new FormData();
      fd.append('file', src.file);
      fd.append('kind', 'json');
      fd.append('flatten_mode', flattenMode);
      fd.append('flatten_depth', String(flattenDepth));
      // Same background pipeline as the queue.
      try {
        await uploadWithProgress('/api/ingest/jobs/upload', fd, src.name);
      } catch (e) {
        if (!e.cancelled) toast('Import failed: ' + e.message, 6000);
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; if (opts.onCancel) opts.onCancel(); };
    actions.append(importBtn, cancel);
    b.append(actions);
  }, { wide: true });
}

/* Mirrors store.py's DEFAULT_IMPORT_EXTENSIONS — one format list, two
   places it has to be spelled out (a browser can't read a Python
   constant). Shared by three things: openImportModal's file-picker accept
   attribute, the directory-import chips (order matters there — it's the
   order they render in), and wireFileDrop's own filtering below — a raw
   OS drop has no equivalent of a picker's accept attribute doing that
   filtering natively, so the drop handler has to do it itself. */
export const RECOGNIZED_IMPORT_EXTENSIONS = ['.csv', '.tsv', '.txt', '.psv', '.json', '.jsonl', '.ndjson'];

/* The SQLite extension set — routed to openSqliteTablePicker's configure
   step by the unified import queue, and recognized by wireFileDrop without
   a second hand-typed copy. */
export const SQLITE_IMPORT_EXTENSIONS = ['.db', '.sqlite', '.sqlite3', '.db-wal'];

export function extOf(filename) {
  const i = filename.lastIndexOf('.');
  return i === -1 ? '' : filename.slice(i).toLowerCase();
}

export function importKindFor(filename) {
  if (SQLITE_IMPORT_EXTENSIONS.includes(extOf(filename))) return 'sqlite';
  const ext = extOf(filename).slice(1); // drop the leading '.' — json/jsonl/ndjson below are bare
  return ext === 'json' || ext === 'jsonl' || ext === 'ndjson' ? 'json' : 'csv';
}

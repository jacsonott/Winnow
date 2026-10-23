/* Every way a file becomes a source: the import modal and its queue, the
SQLite table picker, folder import, and OS drag-and-drop.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, dragHas, el, post, toast } from './core.js';
import { maybeOfferAssociation } from './assoc.js';
import { openFolderBrowser } from './home.js';
import { startJobsPoll, uploadWithProgress } from './jobs.js';
import { ARCHIVE_IMPORT_EXTENSIONS, IIS_FORMAT_LABELS, IIS_IMPORT_TYPE, KV_PROBE_BYTES, PLASO_IMPORT_EXTENSIONS, RECOGNIZED_IMPORT_EXTENSIONS, SQLITE_IMPORT_EXTENSIONS, XLSX_IMPORT_EXTENSIONS, extOf, iisLogFormat, importKindFor, kvCandidate, looksLikeKvLog } from './merge.js';
import { renderPluginTabs, renderPluginPanelButtons } from './plugins.js';
import { fmtBytes } from './tables.js';
import { loadSources } from './sources.js';
import { S } from './state.js';
import { tableMenuSeg } from './timeframe.js';
import { confirmDialog, modal, oneAtATime, promptDialog, sideRail, sideRailMatch } from './ui.js';

/* ------------------------------------------------------------- plugins */

/* Loaded once at boot — plugins are app-level (loaded by the server at its
   own startup), not per-case. Failing the fetch just means no plugin
   routing; every built-in import path works without it. */
export async function loadPlugins() {
  try {
    const r = await api('/api/plugins');
    S.plugins = r.plugins || [];
    S.pluginFormats = r.formats || [];
    S.pluginTabs = r.tabs || [];
    S.pluginRowActions = r.row_actions || [];
    S.pluginDashboards = r.dashboards || [];
    S.pluginPanels = r.panels || [];
    S.pluginPagePanels = r.page_panels || [];
    S.pluginDirs = r.dirs || [];
    S.pluginsCaseOpen = !!r.case_open;
    S.pluginApiVersion = r.api_version ?? null;
  } catch { S.plugins = []; S.pluginFormats = []; S.pluginTabs = []; S.pluginRowActions = []; S.pluginDashboards = []; S.pluginPanels = []; S.pluginPagePanels = []; S.pluginDirs = []; S.pluginsCaseOpen = false; S.pluginApiVersion = null; }
  renderPluginTabs();
  renderPluginPanelButtons();
}

/* fnmatch-lite for plugin filename_patterns ($MFT, *$UsnJrnl*) — the same
   case-insensitive bare-filename semantics plugin_api.IngestFormat.matches
   applies server-side, so a file routes the same way whichever side asks. */
export function globMatches(pattern, name) {
  const rx = pattern.toLowerCase().replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${rx}$`).test(name.toLowerCase());
}

/* The plugin format claiming this filename, or null. Built-in extensions
   win outright — a plugin claiming .csv doesn't hijack default routing; it
   stays reachable through the Plugins modal's explicit per-format picker.
   So do built-in names: an IIS log is .log, which esxi_logs claims. */
export function pluginFormatFor(filename) {
  const base = filename.split(/[\\/]/).pop();
  const ext = extOf(base);
  if (RECOGNIZED_IMPORT_EXTENSIONS.includes(ext) || SQLITE_IMPORT_EXTENSIONS.includes(ext) || XLSX_IMPORT_EXTENSIONS.includes(ext) || PLASO_IMPORT_EXTENSIONS.includes(ext) || ARCHIVE_IMPORT_EXTENSIONS.includes(ext)) return null;
  if (iisLogFormat(base)) return null;
  return S.pluginFormats.find((f) =>
    (ext && (f.extensions || []).includes(ext))
    || (f.filename_patterns || []).some((p) => globMatches(p, base))) || null;
}

export const pluginFormatById = (id) => S.pluginFormats.find((f) => f.id === id) || null;

/* Extensions plugins add beyond the built-in list — merged into the import
   pickers' accept attributes and the directory-import chips. Extension-less
   plugin targets ($MFT, $J) can't ride an accept attribute at all; they
   arrive by drag-drop, folder import (filename_patterns), or the Plugins
   modal's own unrestricted picker. */
export function pluginExtensions() {
  const out = [];
  for (const f of S.pluginFormats) {
    for (const e of f.extensions || []) {
      if (!RECOGNIZED_IMPORT_EXTENSIONS.includes(e) && !out.includes(e)) out.push(e);
    }
  }
  return out;
}

export function pluginFilenamePatterns() {
  const out = [];
  for (const f of S.pluginFormats) {
    for (const p of f.filename_patterns || []) if (!out.includes(p)) out.push(p);
  }
  return out;
}

export function defaultPluginOptions(fmt) {
  const out = {};
  for (const o of fmt.options || []) out[o.name] = o.default ?? (o.type === 'bool' ? false : '');
  return out;
}

/* Appends File objects to S.importQueue with each one's default settings —
   shared by openImportModal's own file-picker (addInput.onchange) and
   wireFileDrop, so a dropped file and a picked one queue identically. Not
   gated on the modal being open: S.importQueue is app-level state that
   openImportModal just happens to render, so this can be called before
   the modal even exists yet and it'll show up correctly whenever it opens. */
/* Files this large are cheaper to add by path: the browser upload copies
   every byte through a multipart POST and a server-side spool before the
   import even starts, where a path import reads the file in place. */
export const UPLOAD_ADVISORY_BYTES = 1 << 30; // 1 GB

/* The ONE place a queue item's shape is built. transport is {file: File} or
   {path: string}; routing (plugin format by name first, then extension) and
   the per-kind defaults live here so the entry points can't drift — a
   default that differs between a dropped file and a path-picked one is the
   kind of bug nothing surfaces until the imports disagree. `fmt` overrides
   name routing for the Plugins panel's explicit per-format picker, and
   `kind` for a file recognised by its content (a key=value log, which
   queueFiles and queuePaths ask about before they get here). */
export let queueSeq = 0;

export function queueItem(transport, name, fmt = pluginFormatFor(name), kind = importKindFor(name)) {
  // qid: which item the Import dialog has chosen, kept across a reorder,
  // a removal and the dialog reopening.
  const qid = ++queueSeq;
  if (fmt) {
    return { ...transport, qid, name, kind: 'plugin', format_id: fmt.id,
             options: defaultPluginOptions(fmt), configured: false };
  }
  transport = { ...transport, qid };
  return kind === 'json' ? { ...transport, name, kind, flatten_mode: 'none', flatten_depth: 1, configured: false }
    : kind === 'sqlite' || kind === 'xlsx' ? { ...transport, name, kind, tables: null, configured: false }
    // A .plaso is one file → one timeline table with nothing to configure.
    : kind === 'plaso' ? { ...transport, name, kind, configured: true }
    // An IIS log names its own columns; the preview can still pick another
    // of the three formats (log_format, null = the name's) and set types.
    : kind === 'iis' ? { ...transport, name, kind, log_format: null, column_types: null, configured: false }
    // A key=value log names its own columns too, one per key; the preview
    // can set their types, by name as an IIS log's.
    : kind === 'kv' ? { ...transport, name, kind, column_types: null, configured: false }
    // An archive expands on import, then you pick files from its contents.
    : kind === 'archive' ? { ...transport, name, kind, configured: true }
    // Raw text (a log with an extension nothing else claims) is the delimited
    // shape with no header — one line per row, nothing to configure; the
    // preview can still switch it to csv.
    : { ...transport, name, kind, delimiter: null, has_header: kind !== 'text', column_types: null, configured: false };
}

/* The server's looks_binary, on the client: a NUL in the first 8 KB, a
   UTF-16 BOM exempt. Checked BEFORE a raw-text upload, so an .exe or an
   .evtx picked by mistake is refused here rather than copied up in full
   and refused on arrival. Unreadable here means "let the server say". */
const BINARY_PROBE_BYTES = 8192;
const PROBE_BYTES = Math.max(BINARY_PROBE_BYTES, KV_PROBE_BYTES);
async function fileHead(f) {
  try { return new Uint8Array(await f.slice(0, PROBE_BYTES).arrayBuffer()); } catch { return null; }
}
const utf16Bom = (head) => head.length >= 2 && ((head[0] === 0xFF && head[1] === 0xFE) || (head[0] === 0xFE && head[1] === 0xFF));
function headLooksBinary(head) {
  if (!head || utf16Bom(head)) return false;
  return head.subarray(0, BINARY_PROBE_BYTES).includes(0);
}
/* A file's first bytes as text, for looksLikeKvLog: UTF-16 by its BOM,
   else UTF-8 (a Windows-1252 byte the decoder can't read is not in the
   ASCII the first line is recognised by). */
function headText(head) {
  if (!head) return '';
  const bytes = head.subarray(0, KV_PROBE_BYTES);
  const enc = utf16Bom(bytes) ? (bytes[0] === 0xFF ? 'utf-16le' : 'utf-16be') : 'utf-8';
  return new TextDecoder(enc).decode(bytes);
}

export async function queueFiles(files) {
  let bigNamed = null, bigCount = 0;
  const skipped = [];
  for (const f of [...files]) {
    // A name nothing claims could be a key=value log, which only its first
    // line says (looks_like_kv_log server-side reads the same bytes).
    const head = kvCandidate(f.name) ? await fileHead(f) : null;
    const item = head && looksLikeKvLog(headText(head))
      ? queueItem({ file: f }, f.name, null, 'kv') : queueItem({ file: f }, f.name);
    if (item.kind === 'text' && headLooksBinary(head || await fileHead(f))) { skipped.push(f.name); continue; }
    if (f.size >= UPLOAD_ADVISORY_BYTES) { bigCount++; bigNamed = bigNamed || f; }
    S.importQueue.push(item);
  }
  if (skipped.length) {
    const names = skipped.slice(0, 3).join(', ') + (skipped.length > 3 ? ', …' : '');
    toast(`Skipped ${skipped.length} binary file${skipped.length === 1 ? '' : 's'} (not text): ${names}`, 6000);
  }
  if (bigCount) {
    // Advisory, never a gate — the upload still works, it's just the slow
    // way to move a file the server can already reach.
    const what = bigCount === 1 ? `${bigNamed.name} is ${fmtBytes(bigNamed.size)}`
      : `${bigCount} of these files are over ${fmtBytes(UPLOAD_ADVISORY_BYTES)}`;
    toast(`${what} — "Add from this machine…" imports by path with no upload copy, which is much faster.`, 9000);
  }
}

/* Queue files already on the server's disk, by absolute path — the "Add
   from this machine…" picker's entry point, and the fast transport: a path
   item imports in place (jobs/path) with no upload leg at all. Same routing
   rules as queueFiles (plugin format by name first, then extension), same
   queue, same configure/preview steps — the transport is the only
   difference, carried as {path} instead of {file}. */
export async function queuePaths(entries) {
  // No extension gate any more: a name nothing else claims queues as raw
  // text (importKindFor). A binary file is refused by the importer itself,
  // as a job error — the server has the bytes, the browser doesn't. Which
  // is also why the server is asked which of those are key=value logs:
  // only the first line says. Asked once for the lot, and only when one
  // could be; a failed ask leaves them as their names say.
  const ask = entries.filter((e) => kvCandidate(e.name));
  const kv = new Set();
  if (ask.length) {
    try {
      const r = await post('/api/ingest/recognise', { paths: ask.map((e) => e.path) });
      ask.forEach((e, i) => { if (r.kinds[i] === 'kv') kv.add(e); });
    } catch { /* read as their names say */ }
  }
  for (const e of entries) {
    const transport = { path: e.path, size: e.size };
    S.importQueue.push(kv.has(e) ? queueItem(transport, e.name, null, 'kv') : queueItem(transport, e.name));
  }
}

/* Queue files against one plugin format explicitly — bypasses filename
   routing entirely, for the Plugins modal's per-format picker (the only
   picker that can reach a file the format matches by pattern rather than
   extension, since accept attributes can't express "$MFT"). */
export function queueFilesForFormat(fmt, files) {
  for (const f of files) {
    S.importQueue.push(queueItem({ file: f }, f.name, fmt));
  }
}

/* The one way to bring files into a case, in the two panes of the
   Tables manager and Import a folder: the queued files down the left,
   what each holds and how it will be read on the right, and the ways to
   add more along the foot beside the one import. Any number of files of
   any kind queue together (kind picked per file by importKindFor, or a
   plugin's format by name), because from the analyst's side it is one
   workflow whichever parser ends up reading a given file.

   A file's settings are set in its pane and kept as they change — there
   is no "Use these settings" step, so moving to the next file never loses
   the last one's. The preview under them answers for what is set now:
   the first rows split as they will be, or a SQLite file's tables. The
   frame is .side-panes', a fixed height; only the preview box scrolls,
   so going from a two-column CSV to a forty-table SQLite file moves
   nothing on screen. It used to be a column of rows, one per file, with a
   button on each that swapped the whole dialog out for that file's
   preview and back. */
export const IMPORT_QUEUE_GROUPS = ['Queued'];

/* Which queued file the dialog shows, by qid — kept across its reopening
   (a drop while it is open, the server-disk picker coming back). */
export let importSel = null;

export const UNIT_KINDS = ['sqlite', 'xlsx'];
export const needsChoice = (item) => UNIT_KINDS.includes(item.kind) && !item.configured;
export const itemSize = (item) => (item.file ? item.file.size : item.size);

/* A JSON, SQLite or Excel upload's preview sends the whole file up to read
   it (a JSON document can't be cut short, and sqlite3 wants a real file);
   past this a pane asks before doing that, rather than starting a large
   copy because a row in the list was clicked. A path item previews in
   place, at any size. */
export const AUTO_PREVIEW_UPLOAD_BYTES = 32 << 20;

/* A delimited, text or IIS preview reads a bounded head of the file
   (server.py's PREVIEW_SAMPLE_BYTES, 512 KB), so only a head is sent:
   more than the server reads, so what it sees is the same. */
export const PREVIEW_HEAD_BYTES = 1 << 20;
export const headOf = (file) => (file.size > PREVIEW_HEAD_BYTES
  ? new File([file.slice(0, PREVIEW_HEAD_BYTES)], file.name) : file);

/* A queued file's preview, by the transport it arrived by — a path item
   previews in place, an upload sends the file (or its head) — and by
   kind, which picks the upload route. Never a guess about the disk. */
export function fetchPreview(item, fields) {
  if (item.path) return post('/api/ingest/preview/path', { path: item.path, ...fields });
  const url = { json: '/api/ingest/json/preview', sqlite: '/api/ingest/sqlite/preview',
                xlsx: '/api/ingest/xlsx/preview' }[fields.kind] || '/api/ingest/preview';
  const fd = new FormData();
  fd.append('file', url === '/api/ingest/preview' ? headOf(item.file) : item.file);
  for (const [k, v] of Object.entries(fields)) if (v != null) fd.append(k, String(v));
  return api(url, { method: 'POST', body: fd });
}

export const DELIMITERS = [['auto', 'Auto-detect', null], ['comma', 'Comma', ','], ['tab', 'Tab', '\t'],
                    ['semicolon', 'Semicolon', ';'], ['pipe', 'Pipe', '|']];
export const delimKey = (d) => (DELIMITERS.find((x) => x[2] === d) || DELIMITERS[0])[0];
export const delimName = (d) => (DELIMITERS.find((x) => x[2] === d) || ['', JSON.stringify(d)])[1];

/* What the side list says under a file's name: how it will be read, as
   the section lists say what is set in a section. */
export function queueHint(item) {
  const units = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;
  switch (item.kind) {
    case 'plugin': return (pluginFormatById(item.format_id)?.label || item.format_id) + (item.configured ? ' · options set' : '');
    case 'text': return 'Text · one line per row';
    case 'iis': return `IIS log · ${IIS_FORMAT_LABELS[item.log_format || iisLogFormat(item.name)] || 'W3C'}`;
    case 'kv': return 'Key=value log · a column per key';
    case 'json': return 'JSON · ' + (item.flatten_mode === 'full' ? 'flattened'
      : item.flatten_mode === 'depth' ? `flattened ${item.flatten_depth} deep` : 'nested kept as JSON');
    case 'sqlite': return 'SQLite · ' + (item.configured ? units(item.tables.length, 'table') : 'pick tables');
    case 'xlsx': return 'Excel · ' + (item.configured ? units(item.tables.length, 'sheet') : 'pick sheets');
    case 'plaso': return 'Plaso · one timeline table';
    case 'archive': return 'Archive · expands on import';
    default: return 'Delimited · ' + (item.delimiter ? delimName(item.delimiter).toLowerCase() : 'auto-detect')
      + (item.has_header === false ? ' · no header' : '');
  }
}

export function openImportModal() {
  modal('Import', (b) => {
    b.dataset.kind = 'import';
    const queue = () => S.importQueue;
    const byKey = (key) => queue().find((i) => String(i.qid) === key) || null;
    if (!byKey(importSel)) {
      const first = queue().find(needsChoice) || queue()[0];
      importSel = first ? String(first.qid) : null;
    }

    const side = sideRail({
      entries: () => queue().map((item) => ({
        key: String(item.qid), label: item.name, group: 'Queued', item,
        title: item.path || item.name, hint: queueHint(item), warn: needsChoice(item),
      })),
      groups: IMPORT_QUEUE_GROUPS,
      selected: () => importSel,
      onPick: (entry) => { if (importSel !== entry.key) { importSel = entry.key; side.sync(); showItem(); } },
      label: 'Queued files',
      notes: { Queued: 'Nothing queued yet. Drop files on the window, or add them with the buttons below.' },
      action: {
        glyph: '✕', title: 'Take this file out of the queue',
        label: (entry) => `Take ${entry.label} out of the queue`,
        run: (entry) => removeItem(entry.item),
      },
    });
    side.search.placeholder = 'Filter queued files…';

    const pane = el('div', 'side-pane import-pane');
    const panes = el('div', 'side-panes import-queue');
    panes.append(side.rail, pane);
    b.append(panes);

    // A setting changed in the pane: the side list's line for the file
    // and the import button say so.
    const changed = () => { side.paint(); syncImport(); };

    function removeItem(item) {
      const i = queue().indexOf(item);
      if (i < 0) return;
      queue().splice(i, 1);
      if (importSel === String(item.qid)) {
        const next = queue()[Math.min(i, queue().length - 1)];
        importSel = next ? String(next.qid) : null;
        showItem();
      }
      side.paint();
      syncImport();
    }

    function showItem() {
      pane.replaceChildren();
      const item = byKey(importSel);
      if (!item) buildEmptyQueuePane(pane);
      else buildQueueItemPane(pane, item, changed);
    }

    /* ---------------------------------------------- the foot */
    // The ways in first, the import last and on the far side. Path before
    // upload, deliberately: browser and server are the same machine here,
    // so "Add from this machine…" reads the file in place (no upload copy,
    // no spool), which is the transport you want for anything large; the
    // browser picker stays for convenience and for the odd tunneled or
    // remote client where it's the only one that works.
    const foot = el('div', 'table-menu-foot table-menu-strip import-foot');
    const pathBtn = el('button', 'btn ghost', 'Add from this machine…');
    pathBtn.title = 'Browse the server\'s own disk and import in place — no upload copy, fastest for big files';
    pathBtn.onclick = () => {
      openFolderBrowser(S.lastBrowsePath || undefined, async (sel) => {
        S.lastBrowsePath = sel.dir;
        const before = queue().length;
        await queuePaths(sel.files);
        if (queue()[before]) importSel = String(queue()[before].qid);
        openImportModal();
      }, () => openImportModal(), { mode: 'files', title: 'Add files from this machine' });
    };
    const addLabel = el('label', 'btn ghost', 'Upload from browser…');
    addLabel.title = 'A regular browser file picker — the file is copied up to the server before importing';
    const addInput = el('input');
    addInput.type = 'file';
    // No `accept`: every file is importable now (raw text is the
    // catch-all), and the other two entry points — a drop, the server-disk
    // picker — never filtered by extension. An accept list here would make
    // the browser picker the one place a .log.1 could not be chosen.
    addInput.multiple = true;
    addInput.hidden = true;
    addInput.onchange = async () => {
      const files = [...addInput.files];
      addInput.value = '';
      const before = queue().length;
      await queueFiles(files);
      if (queue()[before]) importSel = String(queue()[before].qid);
      side.paint();
      showItem();
      syncImport();
    };
    addLabel.append(addInput);
    const folderBtn = el('button', 'btn ghost', 'Import a whole folder…');
    folderBtn.title = 'Scan a directory (e.g. KAPE output) against extension + glob patterns';
    folderBtn.onclick = () => openDirectoryImportModal();
    const importAll = el('button', 'btn primary table-menu-close');
    function syncImport() {
      const n = queue().length;
      importAll.textContent = n ? `Import ${n} file${n === 1 ? '' : 's'}` : 'Import';
      importAll.disabled = !n;
      const waiting = queue().filter(needsChoice).length;
      importAll.title = waiting ? `${waiting} file${waiting === 1 ? '' : 's'} still need${waiting === 1 ? 's' : ''} tables or sheets picked` : '';
    }
    importAll.onclick = () => {
      if (!queue().length) return;
      const unpicked = queue().find(needsChoice);
      if (unpicked) {
        // Take the analyst to it: the pane is where the choice is made.
        importSel = String(unpicked.qid);
        side.paint();
        showItem();
        toast(`Pick which ${unpicked.kind === 'xlsx' ? 'sheets' : 'tables'} to import from ${unpicked.name} first`, 4500);
        return;
      }
      const items = queue().slice();
      S.importQueue = [];
      importSel = null;
      $('modal').hidden = true;
      importQueued(items);
    };
    foot.append(pathBtn, addLabel, folderBtn, importAll);
    b.append(foot);

    side.paint();
    showItem();
    syncImport();
  }, { wide: 'x', tall: true, focus: (b) => b.querySelector('.side-rail-item[aria-selected="true"]') || b.querySelector('.side-search') });
}

/* Nothing queued: the pane says what the dialog takes and how to give it
   some, where a file's settings would be. */
export function buildEmptyQueuePane(pane) {
  const box = el('div', 'import-empty');
  box.append(
    el('div', 'import-empty-title', 'Drop files here'),
    el('p', 'fb-help', 'or anywhere on the window — or add them with the buttons below. “Add from this machine…” reads a file where it is, which is the fast way for anything large.'),
    el('p', 'fb-help', 'CSV and other delimited text, JSON and JSON Lines, SQLite, Excel, Plaso, IIS logs, key=value logs such as FortiGate’s, and zip or tar archives each import their own way; any other text file imports one line per row.'));
  pane.append(box);
}

/* What a queued file can be read as, by what it was queued as. A file
   named like an IIS log can be read as delimited text too, for the log
   that was renamed or isn't what its name says; anything text-shaped can
   be read one line per row, the way out for a file that some reader
   claims and gets wrong. Delimited and other text can be read as
   key=value pairs, for a key=value log whose first line didn't say so
   (only Fortinet's is recognised), and one that did can be read the other
   two ways. Kinds with no other reading (SQLite, Excel, Plaso, archives)
   have no switch. */
export function readAsOptions(item) {
  const base = item.queuedAs || item;
  const delimited = ['csv', 'Delimited', 'Split into columns on a delimiter'];
  const lines = ['text', 'One line per row', 'Every line becomes one row in one column, nothing split'];
  const kv = ['kv', 'Key=value', 'A column per key, each line’s values under its own keys (FortiGate and other Fortinet logs)'];
  switch (base.kind) {
    case 'csv': case 'text': return [delimited, lines, kv];
    case 'kv': return [kv, delimited, lines];
    case 'json': return [['json', 'JSON', 'One row per record, its keys as columns'], lines];
    case 'iis': return [['iis', 'IIS log', 'Columns named by the log itself'], delimited, lines];
    // A plugin-claimed upload only, as the queue row's "As text" was.
    case 'plugin': return item.path ? null
      : [['plugin', pluginFormatById(base.format_id)?.label || base.format_id, 'Read by the plugin that claims this file'], lines];
    default: return null;
  }
}

/* Read a queued file as another kind: the Read as switch. What it was
   queued as is kept (queuedAs) so the switch keeps offering its way back,
   and a plugin's format and options come back with it. Each kind starts
   from its own defaults, as a freshly queued file of that kind would. */
export function switchKind(item, to) {
  if (item.kind === to) return;
  if (!item.queuedAs) item.queuedAs = { kind: item.kind, format_id: item.format_id, options: item.options };
  const reset = { delimiter: null, has_header: true, column_types: null, log_format: null, format_id: null, options: null };
  if (to === 'text') Object.assign(item, reset, { kind: 'text', has_header: false, configured: true });
  else if (to === 'csv') Object.assign(item, reset, { kind: 'csv', configured: true });
  else if (to === 'iis') Object.assign(item, reset, { kind: 'iis', configured: false });
  else if (to === 'kv') Object.assign(item, reset, { kind: 'kv', configured: false });
  else if (to === 'json') Object.assign(item, reset, { kind: 'json', flatten_mode: 'none', flatten_depth: 1, configured: false });
  else if (to === 'plugin') {
    Object.assign(item, reset, { kind: 'plugin', format_id: item.queuedAs.format_id, options: item.queuedAs.options, configured: false });
  }
}

export function buildQueueItemPane(pane, item, changed) {
  // The name, then the folder it is in, cut short from its start: the
  // end of a path is the part that tells two triage folders apart.
  const cut = item.path ? Math.max(item.path.lastIndexOf('/'), item.path.lastIndexOf('\\')) : -1;
  const where = el('span', 'import-file-where');
  where.append(el('span', 'import-file-name', item.name));
  if (cut > 0) where.append(el('span', 'import-file-dir', '\u200E' + item.path.slice(0, cut) + '\u200E'));
  where.title = item.path || item.name;
  const size = itemSize(item);
  const how = el('span', 'import-file-how',
    (size != null ? fmtBytes(size) + ' · ' : '') + (item.path ? 'read in place' : 'uploaded when it imports'));
  const fileRow = paneRow('File', where, how);
  fileRow.classList.add('import-folder-row');
  pane.append(fileRow);

  const opts = readAsOptions(item);
  if (opts) {
    const seg = tableMenuSeg(opts, () => item.kind, (key) => {
      if (key === item.kind) return;
      switchKind(item, key);
      changed();
      pane.replaceChildren();
      buildQueueItemPane(pane, item, changed);
    }, 'import-readas');
    pane.append(paneRow('Read as', seg.seg));
  }

  const settings = el('div', 'side-settings import-settings');
  const previewHead = el('div', 'side-preview-title', 'Preview');
  const preview = el('div', 'side-preview import-preview');
  const rows = el('div', 'side-preview-rows');
  const foot = el('div', 'side-preview-foot');
  preview.append(rows, foot);
  pane.append(settings, previewHead, preview);
  const footLine = (text, bad) => {
    const line = el('div', 'fb-help' + (bad ? ' bad' : ''), text);
    line.title = text;
    foot.replaceChildren(line);
  };
  const note = (text) => settings.append(el('p', 'fb-help import-note', text));
  const ctx = { item, settings, previewHead, preview, rows, foot, footLine, note, changed };

  if (item.kind === 'csv') buildDelimitedSettings(ctx);
  else if (item.kind === 'text') buildLinesSettings(ctx);
  else if (item.kind === 'json') buildJsonSettings(ctx);
  else if (item.kind === 'iis') buildIisSettings(ctx);
  else if (item.kind === 'kv') buildKvSettings(ctx);
  else if (UNIT_KINDS.includes(item.kind)) buildUnitSettings(ctx);
  else if (item.kind === 'plugin') buildPluginSettings(ctx);
  else buildFixedSettings(ctx);
}

/* Fetch a preview for what is set now; the last answer stays on screen,
   dimmed, while the next is on its way (the derive preview's rule), and
   an answer that arrives after a newer question is dropped. */
export function previewRunner(ctx, fetcher, render, { wholeFile = false } = {}) {
  let seq = 0;
  const run = async () => {
    const mine = ++seq;
    ctx.preview.classList.add('stale');
    ctx.preview.setAttribute('aria-busy', 'true');
    if (!ctx.rows.childElementCount) ctx.footLine('Loading preview…');
    let res;
    try { res = await fetcher(); } catch (e) {
      if (mine !== seq || !ctx.preview.isConnected) return;
      ctx.preview.classList.remove('stale');
      ctx.preview.setAttribute('aria-busy', 'false');
      ctx.rows.replaceChildren();
      ctx.footLine('Preview failed: ' + e.message, true);
      return;
    }
    if (mine !== seq || !ctx.preview.isConnected) return;
    ctx.preview.classList.remove('stale');
    ctx.preview.setAttribute('aria-busy', 'false');
    render(res);
  };
  const size = itemSize(ctx.item);
  if (wholeFile && !ctx.item.path && size > AUTO_PREVIEW_UPLOAD_BYTES) {
    // Asked, not started: reading it means sending it up.
    const go = el('button', 'btn ghost', `Load preview (uploads ${fmtBytes(size)})`);
    go.onclick = () => { go.remove(); run(); };
    ctx.rows.append(go);
    ctx.footLine('A browser-picked file is sent to the server to be read.');
    return () => { if (!go.isConnected) run(); };
  }
  run();
  return run;
}

export function previewTable(columns, sampleRows, headExtra) {
  const t = el('table', 'preview-tbl');
  const hr = el('tr');
  columns.forEach((c, i) => {
    const th = el('th');
    th.append(el('div', 'preview-colname', c));
    const extra = headExtra && headExtra(i);
    if (extra) th.append(extra);
    hr.append(th);
  });
  t.append(hr);
  for (const row of sampleRows.slice(0, 50)) {
    const tr = el('tr');
    for (const v of row) tr.append(el('td', null, v));
    t.append(tr);
  }
  return t;
}

export const encodingNote = (res) => (res.encoding === 'cp1252' ? ' · not UTF-8, read as Windows-1252' : '');

export function buildDelimitedSettings(ctx) {
  const { item, settings } = ctx;
  const delim = tableMenuSeg(DELIMITERS.map(([k, l]) => [k, l]), () => delimKey(item.delimiter), (key) => {
    item.delimiter = (DELIMITERS.find((x) => x[0] === key) || DELIMITERS[0])[2];
    item.column_types = null;
    item.configured = true;
    delim.paint();
    ctx.changed();
    reload();
  }, 'import-delims');
  const headerLabel = el('label', 'import-check');
  const headerCb = el('input');
  headerCb.type = 'checkbox';
  headerCb.checked = item.has_header !== false;
  headerCb.onchange = () => { item.has_header = headerCb.checked; item.column_types = null; item.configured = true; ctx.changed(); reload(); };
  headerLabel.append(headerCb, document.createTextNode(' First row is headers'));
  settings.append(paneRow('Delimiter', delim.seg), paneRow('Header', headerLabel));
  ctx.previewHead.append(el('span', 'import-preview-aside', 'Set a column’s type in its heading'));

  const reload = previewRunner(ctx, () => fetchPreview(item, { kind: 'csv', delimiter: item.delimiter || null, has_header: item.has_header !== false }), (res) => {
    if (!item.column_types || item.column_types.length !== res.columns.length) item.column_types = null;
    const types = item.column_types || res.inferred_types.slice();
    ctx.rows.replaceChildren(previewTable(res.columns, res.sample_rows, (i) => typeSelect(types[i], (ty) => {
      types[i] = ty;
      item.column_types = types.slice();
      item.configured = true;
      ctx.changed();
    })));
    const n = res.sample_rows.length;
    ctx.footLine(`${delimName(res.delimiter)}-delimited${item.delimiter ? '' : ' (detected)'} · ${res.columns.length} columns · first ${n} row${n === 1 ? '' : 's'}${encodingNote(res)}`);
  });
}

export function buildLinesSettings(ctx) {
  const { item } = ctx;
  ctx.note('Every line becomes one row, in a single column. Nothing is split, and there is no header row.');
  previewRunner(ctx, () => fetchPreview(item, { kind: 'text', has_header: false }), (res) => {
    ctx.rows.replaceChildren(previewTable(res.columns, res.sample_rows));
    const n = res.sample_rows.length;
    ctx.footLine(`One line per row · first ${n} line${n === 1 ? '' : 's'}${encodingNote(res)}`);
  });
}

/* An IIS log in one of its three formats: the name picks one, and the
   others are here for the log that was renamed or isn't what its name
   says. Types are kept by column NAME ({column: type}, not the delimited
   list): the preview reads the start of a W3C log, and a #Fields: line
   further in can put columns ahead of the ones it saw (store.ingest_iis). */
export const IIS_FORMATS = [
  ['w3c', 'W3C extended', 'Columns named by the log’s own #Fields: lines'],
  ['ncsa', 'NCSA common', 'The fixed NCSA common log layout'],
  ['native', 'IIS native', 'IIS’s own fixed, comma-separated layout, in the server’s local time'],
];

export function buildIisSettings(ctx) {
  const { item, settings } = ctx;
  const named = iisLogFormat(item.name) || 'w3c';
  const format = () => item.log_format || named;
  const seg = tableMenuSeg(IIS_FORMATS, format, (key) => {
    if (key === format()) return;
    item.log_format = key;
    item.column_types = null;
    item.configured = true;
    seg.paint();
    ctx.changed();
    reload();
  }, 'import-iis-format');
  settings.append(paneRow('Format', seg.seg));
  ctx.note(`The name says ${IIS_FORMAT_LABELS[named]}. A Timestamp (UTC) column is added, and a line that isn’t in the format is kept whole.`);
  ctx.previewHead.append(el('span', 'import-preview-aside', 'Set a column’s type in its heading'));

  const reload = previewRunner(ctx, () => fetchPreview(item, { kind: 'iis', log_format: format() }), (res) => {
    const given = item.column_types && !Array.isArray(item.column_types) ? item.column_types : {};
    const types = res.columns.map((c, i) => given[c] || res.inferred_types[i]);
    ctx.rows.replaceChildren(previewTable(res.columns, res.sample_rows, (i) => typeSelect(types[i], (ty) => {
      types[i] = ty;
      item.column_types = { ...given, ...Object.fromEntries(res.columns.map((c, j) => [c, types[j]])) };
      item.configured = true;
      ctx.changed();
    })));
    const n = res.sample_rows.length;
    ctx.footLine(`IIS log, ${IIS_FORMAT_LABELS[format()]} · ${res.columns.length} columns · first ${n} row${n === 1 ? '' : 's'}`
      // Only W3C's columns can grow: NCSA's and native's are fixed.
      + (format() === 'w3c' && !res.complete ? ' · a #Fields: line further in can add columns' : '')
      + encodingNote(res));
  });
}

/* A key=value log: nothing to choose but the column types, which are kept
   by name as an IIS log's are — a key first seen further into the file
   adds a column ahead of Line prefix and Unparsed text (store.ingest_kv). */
export function buildKvSettings(ctx) {
  const { item } = ctx;
  ctx.note('Each key becomes a column, in the order keys first appear; a value’s quotes come off. date and time are joined into a Timestamp column: UTC when every line carries a tz offset, the device’s own clock when not. Text before the first pair is kept in Line prefix, and anything else that isn’t a pair in Unparsed text.');
  ctx.previewHead.append(el('span', 'import-preview-aside', 'Set a column’s type in its heading'));
  previewRunner(ctx, () => fetchPreview(item, { kind: 'kv' }), (res) => {
    const given = item.column_types && !Array.isArray(item.column_types) ? item.column_types : {};
    const types = res.columns.map((c, i) => given[c] || res.inferred_types[i]);
    ctx.rows.replaceChildren(previewTable(res.columns, res.sample_rows, (i) => typeSelect(types[i], (ty) => {
      types[i] = ty;
      item.column_types = { ...given, ...Object.fromEntries(res.columns.map((c, j) => [c, types[j]])) };
      item.configured = true;
      ctx.changed();
    })));
    const n = res.sample_rows.length;
    ctx.footLine(`Key=value log · ${res.columns.length} columns · first ${n} row${n === 1 ? '' : 's'}`
      + (res.complete ? '' : ' · a key further in can add columns')
      + encodingNote(res));
  });
}

/* The type picker in a preview column's heading. */
export function typeSelect(value, onPick) {
  const sel = el('select');
  for (const ty of ['text', 'number', 'datetime']) {
    const o = document.createElement('option');
    o.value = ty; o.textContent = ty;
    sel.append(o);
  }
  sel.value = value;
  sel.onchange = () => onPick(sel.value);
  return sel;
}

export function buildJsonSettings(ctx) {
  const { item, settings } = ctx;
  const depthInput = el('input', 'import-depth');
  depthInput.type = 'number';
  depthInput.min = '1';
  depthInput.value = String(item.flatten_depth || 1);
  depthInput.setAttribute('aria-label', 'Levels to flatten');
  const mode = tableMenuSeg([
    ['none', 'Keep as JSON', 'A nested object stays as its JSON text in one column'],
    ['full', 'Flatten', 'Every nested object becomes dotted columns (user.name)'],
    ['depth', 'Flatten to depth', 'Flatten only this many levels; deeper objects stay as JSON'],
  ], () => item.flatten_mode || 'none', (key) => {
    item.flatten_mode = key;
    item.configured = true;
    mode.paint();
    depthInput.hidden = key !== 'depth';
    ctx.changed();
    reload();
  }, 'import-flatten');
  depthInput.hidden = (item.flatten_mode || 'none') !== 'depth';
  depthInput.oninput = debounce(() => {
    item.flatten_depth = Math.max(1, parseInt(depthInput.value, 10) || 1);
    item.configured = true;
    ctx.changed();
    reload();
  }, 300);
  settings.append(paneRow('Nested objects', mode.seg, depthInput));
  ctx.note('An array always stays as its JSON text in one column, at any depth, since its length varies record to record.');

  const reload = previewRunner(ctx, () => fetchPreview(item, { kind: 'json', flatten_mode: item.flatten_mode || 'none', flatten_depth: item.flatten_depth || 1 }), (res) => {
    ctx.rows.replaceChildren(previewTable(res.columns, res.sample_rows, (i) => el('div', 'count', res.inferred_types[i])));
    const clashes = res.key_collisions || 0;
    ctx.footLine(`${res.record_count.toLocaleString()} record${res.record_count === 1 ? '' : 's'} · ${res.columns.length} columns · first ${res.sample_rows.length}`
      + (clashes ? ` · ${clashes.toLocaleString()} with two values for one key, the later kept as <key>_1` : '')
      + encodingNote(res));
  }, { wholeFile: true });
}

/* A SQLite file's tables or a workbook's sheets, ticked to import; the
   preview box is the list. Chromium-epoch columns in a table get a chip
   each, on to convert (microseconds since 1601 — Chrome's own
   convention) to a readable datetime as it imports. */
export function buildUnitSettings(ctx) {
  const { item } = ctx;
  const sheets = item.kind === 'xlsx';
  const unit = sheets ? 'sheet' : 'table';
  ctx.note(sheets
    ? 'Each sheet you tick becomes a table of its own. Date-formatted cells become readable timestamps.'
    : 'Each table you tick becomes a table of its own. Chrome-style timestamp columns are converted to readable dates; untick a chip to keep the number.');
  ctx.previewHead.firstChild.textContent = sheets ? 'Sheets' : 'Tables';
  const pick = el('div', 'vp-seg');
  const all = el('button', 'btn ghost', 'All');
  const none = el('button', 'btn ghost', 'None');
  pick.append(all, none);
  ctx.previewHead.append(pick);

  let units = null;
  const chosen = new Set((item.tables || []).map((t) => t.table));
  const convert = new Map();
  function commit() {
    item.tables = units.filter((t) => chosen.has(t.name)).map((t) => ({ table: t.name, timestamp_columns: [...convert.get(t.name)] }));
    item.configured = item.tables.length > 0;
    ctx.changed();
    sum();
  }
  function sum() {
    const picked = units.filter((t) => chosen.has(t.name));
    const rowsPicked = picked.reduce((n, t) => n + (t.row_count || 0), 0);
    ctx.footLine(picked.length
      ? `${picked.length} of ${units.length} ${unit}s ticked · ${rowsPicked.toLocaleString()} rows`
      : `Tick the ${unit}s to import · ${units.length} in this file`);
  }
  function paint() {
    const list = el('div', 'import-units');
    if (!units.length) list.append(el('p', 'fb-help', sheets ? 'No data sheets in this workbook.' : 'No tables in this file.'));
    for (const t of units) {
      const row = el('div', 'import-unit' + (t.error ? ' error' : ''));
      const lab = el('label', 'import-unit-pick');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = chosen.has(t.name);
      cb.disabled = !!t.error;
      cb.onchange = () => { if (cb.checked) chosen.add(t.name); else chosen.delete(t.name); commit(); };
      lab.append(cb, el('span', 'import-unit-name', t.name));
      row.append(lab, el('span', 'import-unit-count', t.error ? `can’t be read: ${t.error}` : `${t.row_count.toLocaleString()} rows`));
      if (t.likely_timestamp_columns.length) {
        const chips = el('div', 'import-unit-chips');
        for (const col of t.likely_timestamp_columns) {
          const chip = el('button', 'btn ghost', col);
          chip.setAttribute('aria-pressed', String(convert.get(t.name).has(col)));
          chip.title = 'Convert this Chrome-epoch column to a readable datetime as it imports';
          chip.onclick = () => {
            const set = convert.get(t.name);
            if (set.has(col)) set.delete(col); else set.add(col);
            chip.setAttribute('aria-pressed', String(set.has(col)));
            if (chosen.has(t.name)) commit();
          };
          chips.append(chip);
        }
        row.append(chips);
      }
      list.append(row);
    }
    ctx.rows.replaceChildren(list);
    sum();
  }
  all.onclick = () => { if (!units) return; for (const t of units) if (!t.error) chosen.add(t.name); commit(); paint(); };
  none.onclick = () => { if (!units) return; chosen.clear(); commit(); paint(); };
  previewRunner(ctx, () => fetchPreview(item, { kind: item.kind }), (res) => {
    units = res.tables;
    for (const t of units) {
      // Back on a file already picked: its choices; fresh: every detected
      // timestamp column converts.
      const prior = (item.tables || []).find((x) => x.table === t.name);
      convert.set(t.name, prior ? new Set(prior.timestamp_columns || []) : new Set(t.likely_timestamp_columns));
    }
    paint();
  }, { wholeFile: true });
}

export function buildPluginSettings(ctx) {
  const { item, settings } = ctx;
  const fmt = pluginFormatById(item.format_id);
  if (!fmt) { ctx.note('The plugin that claimed this file is no longer loaded.'); return; }
  if (fmt.description) ctx.note(fmt.description);
  item.options = { ...defaultPluginOptions(fmt), ...(item.options || {}) };
  const set = (name, v) => { item.options[name] = v; item.configured = true; ctx.changed(); };
  for (const o of fmt.options || []) {
    if (o.type === 'bool') {
      const lab = el('label', 'import-check');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = !!item.options[o.name];
      cb.onchange = () => set(o.name, cb.checked);
      lab.append(cb, document.createTextNode(' ' + (o.label || o.name)));
      settings.append(paneRow('', lab));
    } else if (o.type === 'choice') {
      const sel = el('select');
      for (const c of o.choices || []) {
        const opt = document.createElement('option');
        opt.value = c; opt.textContent = c;
        sel.append(opt);
      }
      sel.value = item.options[o.name] ?? o.default ?? '';
      sel.onchange = () => set(o.name, sel.value);
      settings.append(paneRow(o.label || o.name, sel));
    } else {
      const inp = el('input');
      inp.type = 'text';
      inp.value = item.options[o.name] ?? '';
      inp.oninput = () => set(o.name, inp.value);
      settings.append(paneRow(o.label || o.name, inp));
    }
  }
  if (!(fmt.options || []).length) ctx.note('This format has no options.');
  ctx.rows.append(el('p', 'fb-help', 'The plugin reads the file when it imports; there is no preview before then.'));
  ctx.footLine(`Read by ${fmt.label}`);
}

/* Kinds with nothing to set: what they become, said where the settings
   would be. */
export function buildFixedSettings(ctx) {
  const { item } = ctx;
  if (item.kind === 'plaso') {
    ctx.note('A Plaso storage file imports as one timeline table: a fixed set of columns, plus an Attributes column holding each event’s other fields as JSON.');
    ctx.rows.append(el('p', 'fb-help', 'Plaso files are read in full when they import; there is no preview before then.'));
    ctx.footLine('One timeline table');
  } else if (item.kind === 'archive') {
    ctx.note('Expands on import, nested archives included. Import a folder then opens on what it held, so you choose the files from there.');
    ctx.rows.append(el('p', 'fb-help', 'The archive is opened when it imports; its files are listed then.'));
    ctx.footLine('Expands on import');
  }
}

/* Every queued file, in order, through the transport it arrived by.
   Deliberately not awaited by the dialog: uploads run sequentially (one
   disk, one spool at a time) behind this detached chain while the analyst
   keeps working; each upload resolves into a background ingest job the
   corner panel is already tracking. */
export async function importQueued(queue) {
  // Plugin ingests are synchronous routes that create no background
  // job, so nothing announces the finished source — the jobs poll
  // only refreshes on a job transition. Counted here, refreshed after
  // the loop; the directory-import loop does the same, for the same
  // reason.
  let pluginOk = 0;
  const expanded = [];   // archive expansion reports, handed to directory import below
  for (const item of queue) {
    if (item.kind === 'archive') {
      toast(`Expanding ${item.name}…`, 60000);
      try {
        const rep = item.path
          ? await post('/api/ingest/archive/expand', { path: item.path })
          : await (() => {
            const fd = new FormData();
            fd.append('file', item.file);
            return uploadWithProgress('/api/ingest/archive/upload', fd, item.name);
          })();
        expanded.push({ name: item.name, ...rep });
        // Skipped members (damaged, unwritable, refused as unsafe) and
        // renamed ones (two wanted one name) are things the analyst
        // needs to know before trusting the tree; the first of each is
        // named, the full list is in the log.
        const skipped = rep.skipped || [];
        const renamed = rep.renamed || [];
        toast(`Expanded ${item.name}: ${rep.files.toLocaleString()} file${rep.files === 1 ? '' : 's'}`
          + (rep.archives > 1 ? ` from ${rep.archives} nested archives` : '')
          + (renamed.length ? ` · ${renamed.length} renamed, the name was taken (${renamed[0].path} → ${renamed[0].as})` : '')
          + (skipped.length ? ` · ${skipped.length} skipped (${skipped[0].path}: ${skipped[0].reason})` : '')
          + (rep.truncated ? ' (stopped at a safety cap — partial)' : ''),
        skipped.length || renamed.length ? 12000 : 6000);
      } catch (e) {
        if (!e.cancelled) toast(`Could not expand ${item.name}: ` + e.message, 8000);
      }
      continue;
    }
    // Two transports, chosen by how the item arrived — a path item
    // (the "Add from this machine…" picker, directory import) reads
    // in place with no upload leg; a browser-picked File uploads.
    // Never a guess: the old content-fingerprint resolver that tried
    // to recover a picked file's path is deliberately gone.
    if (item.path) {
      try {
        if (item.kind === 'plugin') {
          // 60s, not the default: this awaits the whole parse (see
          // the directory-import loop's identical choice).
          toast(`Importing ${item.name}…`, 60000);
          await post('/api/ingest/plugin/path', {
            path: item.path, name: item.name, format_id: item.format_id,
            options: item.options || {},
          });
          pluginOk++;
        } else {
          await post('/api/ingest/jobs/path', {
            path: item.path, name: item.name, kind: item.kind,
            delimiter: item.delimiter || null,
            has_header: item.has_header !== false,
            column_types: item.column_types || null,
            log_format: item.log_format || null,
            flatten_mode: item.flatten_mode || 'none',
            flatten_depth: item.flatten_depth || 0,
            tables: item.tables || null,
          });
          startJobsPoll();
        }
      } catch (e) {
        toast(`Import failed for ${item.name}: ` + e.message, 6000);
      }
      continue;
    }
    const fd = new FormData();
    fd.append('file', item.file);
    if (item.kind === 'plugin') {
      // jobs/upload's start_ingest_job knows csv/json/sqlite only —
      // plugin uploads have their own route, which parses via the
      // registered format and ingests synchronously (no job).
      fd.append('format_id', item.format_id);
      fd.append('options', JSON.stringify(item.options || {}));
      try {
        await uploadWithProgress('/api/ingest/plugin/upload', fd, item.name);
        pluginOk++;
      } catch (e) {
        if (!e.cancelled) toast(`Upload failed for ${item.name}: ` + e.message, 6000);
      }
      continue;
    }
    fd.append('kind', item.kind);
    if (item.kind === 'json') {
      fd.append('flatten_mode', item.flatten_mode || 'none');
      fd.append('flatten_depth', String(item.flatten_depth || 1));
    } else if (item.kind === 'sqlite' || item.kind === 'xlsx') {
      fd.append('tables', JSON.stringify(item.tables));
    } else {
      if (item.delimiter) fd.append('delimiter', item.delimiter);
      if (item.log_format) fd.append('log_format', item.log_format);
      fd.append('has_header', item.has_header ? 'true' : 'false');
      if (item.column_types) fd.append('column_types', JSON.stringify(item.column_types));
    }
    try {
      await uploadWithProgress('/api/ingest/jobs/upload', fd, item.name);
    } catch (e) {
      if (!e.cancelled) toast(`Upload failed for ${item.name}: ` + e.message, 6000);
    }
  }
  // Same reason as the directory-import loop's identical line: a
  // sync plugin ingest creates no job for the poll to notice.
  if (pluginOk) await loadSources();
  // The analyst just opened these types with Winnow — the moment
  // the one-time Open With offer is actually justified.
  maybeOfferAssociation(queue.map((i) => i.name));
  // Expanded archives hand off to directory import — the flow that
  // already owns extension gates, include/exclude and the sidebar
  // folder mirror. One modal, rooted at the first expansion; other
  // roots are announced for a follow-up pass.
  if (expanded.length) {
    for (const r of expanded.slice(1)) toast(`Also expanded to ${r.root} — import it via Case → Import → folder`, 10000);
    openDirectoryImportModal({ root: expanded[0].root });
  }
}

/* Drag a file (or several) from the OS straight onto the window to import
   it — an alternative to "Choose files…"/"Choose a SQLite file…", not a
   replacement; both still work. Wired once, globally, at the bottom of
   this file (see wireFileDrop()'s call site) — active whenever a case is
   open ($('app') visible), regardless of which tab/modal is currently
   showing, same as "Import files…" always being reachable from the
   Case menu.

   dataTransfer.types.includes('Files') is the gate on every one of these
   listeners — an OS file drag carries a 'Files' type; an in-page drag
   (tab-strip/sidebar reordering via wireDragReorder, column-header
   reordering) only ever carries 'text/plain'. Without that check this
   would show the "drop to import" overlay while dragging a tab, and
   dragleave/drop firing on every internal drag gesture would fight with
   wireDragReorder's own handlers on the same events.

   dragenter/dragleave are tracked with a depth counter rather than a
   boolean — both fire on every element boundary a drag crosses, not just
   the window's, so a naive "show on enter, hide on leave" flickers (or
   hides too early) as the pointer passes over any child element. */
export function wireFileDrop() {
  let depth = 0;
  const isFileDrag = (e) => dragHas(e, 'Files');

  window.addEventListener('dragenter', (e) => {
    if (!isFileDrag(e)) return;
    depth++;
    if ($('app').hidden) return; // no case open — nothing to import into
    $('dropOverlay').hidden = false;
  });
  window.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // required for drop to be allowed here at all
  });
  window.addEventListener('dragleave', (e) => {
    if (!isFileDrag(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) $('dropOverlay').hidden = true;
  });
  window.addEventListener('drop', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // stop the browser from navigating to the dropped file
    depth = 0;
    $('dropOverlay').hidden = true;
    if ($('app').hidden) { toast('Open or create a case first'); return; }
    handleDroppedFiles([...e.dataTransfer.files]);
  });
}

/* Windows refuses a drag from File Explorer into a window running as
   administrator before the page sees any event at all, so nothing above
   can tell the analyst why the drop won't land: they just get a no-entry
   cursor. The server can tell (winnow/elevation.py), so the window says so
   as it loads, and keeps saying so until it's dismissed. "May", because a
   browser that was already open runs the window with its own rights. */
export async function maybeWarnElevated() {
  let res;
  try { res = await api('/api/elevation'); } catch { return; }
  if (!res.drops_blocked) return;
  const box = $('elevNotice');
  const ok = el('button', 'btn ghost', 'Got it');
  ok.onclick = () => { box.hidden = true; };
  box.replaceChildren(el('span', 'elev-msg',
    'Winnow is running as administrator, so Windows may refuse files dragged onto this '
    + 'window from File Explorer. To drag files in, close Winnow and start it from a normal '
    + 'window, not an administrator one. Case ▾ → Import… works either way.'), ok);
  box.hidden = false;
}

/* A single dropped file recognized as SQLite opens the table-picker flow
   (it can't just queue-and-import like CSV/JSON — which table(s) to pull
   out is a real choice the analyst has to make, same as picking one by
   hand already requires). Anything else recognized (by extension — a raw
   drop has no equivalent of a file-picker's `accept` doing this filtering
   natively, so it happens here) queues into the same CSV/JSON import
   modal "Choose files…" already uses. Unrecognized files are dropped
   silently from the queue but named in a toast — better than a mysterious
   partial import with no explanation. */
/* Every filename is importable now — a name nothing else claims imports
   one line per row — so the only refusal left is the binary check in
   queueFiles (and the importer's own, at ingest). */
export async function handleDroppedFiles(files) {
  if (!files.length) return;
  await queueFiles(files);
  openImportModal();
}

export const patternLines = (text) => text.split('\n').map((l) => l.trim()).filter(Boolean);

export async function loadImportProfiles() {
  try { S.importProfiles = await api('/api/import_profiles'); } catch { S.importProfiles = []; }
}

/* What the Import a folder dialog's side list offers: no profile at all
   (patterns built here and not saved), then the saved profiles by name.
   Pins are menuPins(IMPORT_PINS), keyed by profile id. */
export const IMPORT_PINS = 'import';
export const CUSTOM_IMPORT = 'custom';
export const IMPORT_GROUPS = ['Ad hoc', 'Profiles'];

export const defaultImportExtensions = () =>
  RECOGNIZED_IMPORT_EXTENSIONS.concat(PLASO_IMPORT_EXTENSIONS, [IIS_IMPORT_TYPE], pluginExtensions());

/* How a folder-import file type reads on its chip and in a profile's
   search hint: the two that aren't extensions get words. */
export const importTypeLabel = (x) => (x === '*' ? 'other text files' : x === IIS_IMPORT_TYPE ? 'IIS logs' : x);

export function importProfileEntries(profiles) {
  const entries = [{
    key: CUSTOM_IMPORT, label: 'Custom (not saved)', group: 'Ad hoc',
    title: 'Every file type Winnow reads and no patterns, to build your own here',
  }];
  const byName = [...profiles].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
  for (const p of byName) {
    const inc = p.include_patterns || [];
    const exc = p.exclude_patterns || [];
    const title = [inc.length ? `Includes ${inc.join(', ')}` : 'Every file its types let in',
                   exc.length ? `excludes ${exc.join(', ')}` : ''].filter(Boolean).join('; ');
    entries.push({ key: String(p.id), label: p.name, group: 'Profiles', profile: p, title });
  }
  return entries;
}

/* Does the search box's text find this entry? By its name, or — for a
   profile — by one of its patterns or file types, which the hit then
   names: "amcache" finds a KAPE profile through the pattern that leaves
   out Amcache's unassociated entries, the way "filetime" finds Parse a
   timestamp in the derive dialog. */
export function matchesImportSearch(entry, query) {
  if (sideRailMatch(entry, query)) return {};
  const p = entry.profile;
  if (!p) return null;
  const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  for (const [verb, list] of [['includes', p.include_patterns], ['excludes', p.exclude_patterns], ['reads', p.extensions]]) {
    for (const x of list || []) {
      const shown = importTypeLabel(x);
      const text = `${entry.label} ${entry.group} ${shown}`.toLowerCase();
      if (terms.every((t) => text.includes(t))) return { hint: `${verb} ${shown}` };
    }
  }
  return null;
}

/* A label and its controls on one line, the derive dialog's row — a div
   rather than labeledRow's <label>, because a label passes its clicks to
   the first control inside it, and here that would be Browse… or the
   .csv chip. */
function paneRow(label, ...controls) {
  const row = el('div', 'derived-row');
  row.append(el('span', 'derived-row-label', label), ...controls);
  return row;
}

/* Point at a folder — a KAPE triage output or any other bulk-collection
   directory — and import every file inside that matches an extension plus
   include/exclude glob pattern set, instead of picking files one at a time
   the way openImportModal's queue does. Patterns can be built ad hoc or
   loaded from (and saved back to) a named workspace/import_profiles.json
   profile, so a profile built once for "KAPE" keeps working on every future
   triage without re-typing its exclusions.

   The derive dialog's shape (sideRail, the .side-* rules): the profiles
   down the left, searchable, with pinned ones on top; on the right, the
   folder, the settings a profile holds, and a preview of what the scan
   found; the buttons under both. The frame is a fixed height and so is
   every part of the right pane but the preview's file list, which takes
   what is left and scrolls — so picking a profile, a rescan finding three
   files or three thousand, or a profile's buttons coming and going moves
   nothing. It used to be a centred column sized to the scan: the card
   grew with the list until the buttons left the screen, and re-centred
   on every rescan.

   Async at the top level (unlike every other open* modal function here,
   which stay synchronous and do async work via inner handlers) because
   S.importProfiles — unlike S.savedFilters/S.tags/etc. — has no earlier,
   source-open-triggered load point to piggyback on; this is the one place
   that ever needs it, so it's simplest to just await a fresh copy before
   building the side list at all, rather than rendering once with a
   possibly-stale/empty list and re-rendering again once a background fetch
   resolves.

   `state` carries a folder-browse round trip the same way openNewCaseModal
   does: openFolderBrowser swaps the modal's content out entirely, so the
   only way to keep whatever was already typed is to snapshot it into plain
   values and re-invoke this function with them as the new starting state. */
export async function openDirectoryImportModal(state = {}) {
  await loadImportProfiles();

  const st = {
    root: state.root || null,
    profileId: state.profileId || null,
    recursive: state.recursive ?? true,
    extensions: state.extensions || defaultImportExtensions(),
    includeText: state.includeText || '',
    excludeText: state.excludeText || '',
  };
  // Deleted from another window since the state was taken: the list has
  // no row for it, so start from Custom with what was set.
  if (st.profileId && !S.importProfiles.some((p) => p.id === st.profileId)) st.profileId = null;
  let scanResult = null; // {root, matched, excluded, truncated}
  let scanError = null;
  let checked = new Set(); // indices into scanResult.matched
  let showing = 'matched'; // which of the scan's two lists the preview shows
  let scanSeq = 0; // guards a slow scan response from overwriting a newer one
  let pending = false; // a scan has been asked for and its answer isn't in

  modal('Import a folder', (b) => {
    const profileOf = (id) => S.importProfiles.find((p) => p.id === id) || null;

    /* ---------------------------------------------- the side list */
    const side = sideRail({
      entries: () => importProfileEntries(S.importProfiles),
      groups: IMPORT_GROUPS,
      selected: () => (st.profileId ? String(st.profileId) : CUSTOM_IMPORT),
      onPick: (entry) => pickProfile(entry.profile || null),
      match: matchesImportSearch,
      pinsKey: IMPORT_PINS,
      label: 'Profiles',
      notes: { Profiles: 'None saved yet. Save as profile… keeps the file types and patterns for every case on this machine.' },
    });

    // Custom starts from every file type and no patterns, as it always
    // has; a profile brings its own.
    function pickProfile(p) {
      if ((p ? p.id : null) === st.profileId) return;
      st.profileId = p ? p.id : null;
      st.recursive = p ? (p.recursive ?? true) : true;
      st.extensions = (p && p.extensions) || defaultImportExtensions();
      st.includeText = p ? (p.include_patterns || []).join('\n') : '';
      st.excludeText = p ? (p.exclude_patterns || []).join('\n') : '';
      side.sync();
      syncSettings();
      syncProfileButtons();
      scheduleScan();
    }

    /* ---------------------------------------------- the right pane */
    const folderPath = el('span', 'import-folder-path');
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      openFolderBrowser(st.root || undefined, (path) => {
        openDirectoryImportModal({ ...st, root: path });
      }, () => openDirectoryImportModal(st));
    };
    const folderRow = paneRow('Folder', folderPath, browseBtn);
    folderRow.classList.add('import-folder-row');

    const recLabel = el('label', 'import-subfolders');
    const recCb = el('input');
    recCb.type = 'checkbox';
    recCb.onchange = () => { st.recursive = recCb.checked; scheduleScan(); };
    recLabel.append(recCb, document.createTextNode(' Include subfolders'));

    // '*' is the raw-text catch-all: any other file that isn't binary,
    // one line per row. Off by default — a triage tree is full of text
    // nobody wants as a table — and remembered by profiles like an
    // extension (it rides in the same list).
    const extRow = el('div', 'import-types');
    for (const ext of defaultImportExtensions().concat(['*'])) {
      const chip = el('button', 'btn ghost', importTypeLabel(ext));
      chip.dataset.ext = ext;
      if (ext === '*') chip.title = 'Any other file that is not binary, imported as raw text — one line per row';
      if (ext === IIS_IMPORT_TYPE) chip.title = 'IIS web server logs, recognised by name: u_ex…, ex…, nc…, in….log';
      chip.onclick = () => {
        st.extensions = st.extensions.includes(ext)
          ? st.extensions.filter((e) => e !== ext)
          : [...st.extensions, ext];
        chip.setAttribute('aria-pressed', String(st.extensions.includes(ext)));
        scheduleScan();
      };
      extRow.append(chip);
    }

    const patternBox = (caption, placeholder, onText) => {
      const col = el('label', 'import-pattern');
      const area = el('textarea');
      area.rows = 3;
      area.spellcheck = false;
      area.wrap = 'off'; // a glob per line; one broken across two reads as two
      area.placeholder = placeholder;
      area.oninput = () => { onText(area.value); scheduleScan(); };
      col.append(el('span', 'import-pattern-caption', caption), area);
      return [col, area];
    };
    const [includeCol, includeArea] = patternBox('Include (blank = every file the types let in)',
      'One glob per line,\ne.g. *EvtxECmd*', (v) => { st.includeText = v; });
    const [excludeCol, excludeArea] = patternBox('Exclude',
      'One glob per line,\ne.g. *_Amcache_Unassociated*', (v) => { st.excludeText = v; });
    const patterns = el('div', 'import-patterns');
    patterns.append(includeCol, excludeCol);

    const settingsBox = el('div', 'side-settings');
    const typesRow = paneRow('File types', extRow);
    const patternRow = paneRow('Patterns', patterns);
    // Labels on the first line of what they label, which is two lines.
    typesRow.classList.add('import-top');
    patternRow.classList.add('import-top');
    settingsBox.append(typesRow, recLabel, patternRow);

    // What a profile holds, put on screen — after a pick, and once to start.
    function syncSettings() {
      recCb.checked = st.recursive;
      for (const chip of extRow.children) chip.setAttribute('aria-pressed', String(st.extensions.includes(chip.dataset.ext)));
      includeArea.value = st.includeText;
      excludeArea.value = st.excludeText;
    }

    /* The preview: the scan's two lists, one at a time, chosen beside the
       word Preview the way the derive dialog chooses its sample, and a
       verdict line under them that stays put. */
    const previewBox = el('div', 'side-preview import-preview');
    const previewRows = el('div', 'side-preview-rows');
    const previewFoot = el('div', 'side-preview-foot');
    previewBox.append(previewRows, previewFoot);
    const previewHead = el('div', 'side-preview-title', 'Preview');
    const listSeg = el('div', 'vp-seg import-lists');
    const listBtns = {};
    for (const [key, title] of [
      ['matched', 'The files the scan matched; untick any you want left out'],
      ['excluded', 'The files the scan passed over, and why'],
    ]) {
      const btn = el('button', 'btn ghost');
      btn.dataset.list = key;
      btn.title = title;
      btn.onclick = () => { if (showing !== key) { showing = key; renderResults(); } };
      listBtns[key] = btn;
      listSeg.append(btn);
    }
    previewHead.append(listSeg);

    const pane = el('div', 'side-pane');
    pane.append(folderRow, settingsBox, previewHead, previewBox);
    const panes = el('div', 'side-panes');
    panes.append(side.rail, pane);
    b.append(panes);

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn primary', 'Import checked (0)');
    importBtn.disabled = true;

    /* While a new scan is on its way, the last answer stays on screen,
       dimmed — the derive preview's rule, for the same reason: a list
       that empties to "Scanning…" and refills on every keystroke in a
       pattern is a flicker. aria-busy says the rows are the old answer's. */
    function previewBusy(on) {
      previewBox.classList.toggle('stale', on);
      previewBox.setAttribute('aria-busy', String(on));
      syncCount();
    }

    async function runScan() {
      const seq = ++scanSeq;
      if (!st.root) { pending = false; scanResult = null; scanError = null; checked = new Set(); renderResults(); return; }
      pending = true;
      previewBusy(true);
      if (!scanResult && !scanError) renderResults(); // the first scan says so; later ones dim the last
      let r;
      try {
        r = await post('/api/ingest/dir/scan', {
          root: st.root, recursive: st.recursive, extensions: st.extensions,
          include_patterns: patternLines(st.includeText), exclude_patterns: patternLines(st.excludeText),
          // Lets extension-less plugin targets ($MFT, $J) past the scan's
          // extension gate — see scan_import_directory.
          filename_patterns: pluginFilenamePatterns(),
        });
      } catch (e) {
        if (seq !== scanSeq) return; // a newer scan already started; don't clobber it with a stale error
        pending = false;
        scanResult = null;
        scanError = e.message;
        checked = new Set();
        renderResults();
        return;
      }
      if (seq !== scanSeq) return; // a newer scan resolved first
      pending = false;
      scanResult = r;
      scanError = null;
      // Pre-check everything except what's already in the case — the
      // common case is "import the new stuff", and already_imported exists
      // precisely so a second pass over the same folder doesn't default to
      // silently re-importing (and duplicating tabs for) everything.
      checked = new Set();
      r.matched.forEach((m, i) => { if (!m.already_imported) checked.add(i); });
      renderResults();
    }
    // Dimmed at once rather than after the debounce, so a keystroke
    // never leaves an answer on screen looking current.
    const scheduleScan = () => {
      if (st.root) { pending = true; previewBusy(true); }
      debouncedScan();
    };
    const debouncedScan = debounce(runScan, 300);

    const foot = (text, bad) => {
      const line = el('div', 'fb-help' + (bad ? ' bad' : ''), text);
      line.title = text;
      return line;
    };

    function renderResults() {
      const r = scanResult;
      listBtns.matched.textContent = r ? `Will import (${r.matched.length})` : 'Will import';
      listBtns.excluded.textContent = r ? `Excluded (${r.excluded.length})` : 'Excluded';
      for (const [key, btn] of Object.entries(listBtns)) btn.setAttribute('aria-pressed', String(key === showing));
      const rows = el('div', 'import-files');
      previewRows.replaceChildren(rows);
      previewFoot.replaceChildren();
      previewRows.scrollTop = 0;
      previewBusy(pending);
      if (!st.root) {
        previewFoot.append(foot('Choose a folder to see what it would import.'));
        return;
      }
      if (scanError) {
        previewFoot.append(foot('Scan failed: ' + scanError, true));
        return;
      }
      if (!r) { previewFoot.append(foot('Scanning…')); return; }
      if (showing === 'matched') {
        if (!r.matched.length) rows.append(el('div', 'fb-help', 'No files match.'));
        r.matched.forEach((m, i) => {
          const row = el('label', 'import-file');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = checked.has(i);
          cb.onchange = () => { cb.checked ? checked.add(i) : checked.delete(i); syncCount(); };
          const name = el('span', 'import-file-path', m.rel_path);
          name.title = m.rel_path;
          row.append(cb, name);
          if (m.already_imported) row.append(el('span', 'import-file-note', 'already in case'));
          rows.append(row);
        });
      } else {
        if (!r.excluded.length) rows.append(el('div', 'fb-help', 'Nothing was passed over.'));
        for (const e of r.excluded) {
          const row = el('div', 'import-file excluded');
          const name = el('span', 'import-file-path', e.rel_path);
          name.title = e.rel_path;
          const why = el('span', 'import-file-note', e.reason);
          why.title = e.reason;
          row.append(name, why);
          rows.append(row);
        }
      }
      const total = r.matched.length + r.excluded.length;
      previewFoot.append(foot(total
        ? `${r.matched.length.toLocaleString()} of ${total.toLocaleString()} file${total === 1 ? '' : 's'} ${r.matched.length === 1 ? 'matches' : 'match'}.`
        : 'There are no files in this folder.'));
      const already = r.matched.filter((m) => m.already_imported).length;
      const more = [];
      if (already) more.push(`${already.toLocaleString()} already in the case ${already === 1 ? 'starts' : 'start'} unticked.`);
      if (r.truncated) more.push(`The scan stopped at the first ${total.toLocaleString()} files: narrow the folder or patterns to see the rest.`);
      if (more.length) previewFoot.append(foot(more.join(' ')));
      previewBusy(pending);
    }

    // A tick changes the count, not the list. Nothing imports while the
    // list on screen is the last answer, dimmed: what it would import is
    // about to change.
    function syncCount() {
      importBtn.disabled = checked.size === 0 || pending;
      importBtn.textContent = `Import checked (${checked.size})`;
    }

    function syncFolder() {
      folderPath.textContent = st.root || 'No folder chosen';
      folderPath.title = st.root || '';
      folderPath.classList.toggle('unset', !st.root);
    }

    /* ---------------------------------------------- the profile's buttons */
    // On the bottom row, left of the import: the profile is the last thing
    // done with a set of choices, the import the first.
    const saveBtn = el('button', 'btn ghost');
    const deleteBtn = el('button', 'btn ghost', 'Delete profile');
    // Delete is in the row only while a profile is chosen — taken out,
    // not hidden, so the row reads the same to a test as on screen.
    function syncProfileButtons() {
      saveBtn.textContent = st.profileId ? 'Update profile' : 'Save as profile…';
      if (!st.profileId) deleteBtn.remove();
      else if (!deleteBtn.isConnected) saveBtn.after(deleteBtn);
    }
    saveBtn.onclick = async () => {
      let name = profileOf(st.profileId)?.name;
      if (!name) {
        name = await promptDialog('Name this profile:', 'KAPE');
        if (!name || !name.trim()) return;
      }
      let rec;
      try {
        rec = await post('/api/import_profiles', {
          id: st.profileId, name: name.trim(), extensions: st.extensions,
          include_patterns: patternLines(st.includeText), exclude_patterns: patternLines(st.excludeText),
          recursive: st.recursive,
        });
      } catch (e) { toast('Could not save profile: ' + e.message, 4000); return; }
      toast(`Saved profile "${rec.name}"`);
      await loadImportProfiles();
      st.profileId = rec.id;
      side.paint();
      syncProfileButtons();
    };
    deleteBtn.onclick = async () => {
      const id = st.profileId;
      const name = profileOf(id)?.name || 'this profile';
      if (!(await confirmDialog(`Delete the "${name}" profile?`, { danger: true, okLabel: 'Delete' }))) return;
      try {
        await api(`/api/import_profiles/${id}`, { method: 'DELETE' });
      } catch (e) { toast(`Could not delete the "${name}" profile: ${e.message}`, 6000); return; }
      await loadImportProfiles();
      side.pins.remove(String(id));
      // What it held stays on screen, as Custom, to tweak or save again.
      st.profileId = null;
      side.paint();
      syncProfileButtons();
    };

    const cancelBtn = el('button', 'btn ghost', 'Cancel');
    cancelBtn.onclick = () => { $('modal').hidden = true; };
    importBtn.onclick = oneAtATime(importBtn, async () => {
      const toImport = scanResult.matched.filter((_, i) => checked.has(i));
      if (!toImport.length) return;
      // The files are already on the server's disk, so there's no upload
      // phase — each request just *starts* a background job (milliseconds
      // each; the semaphore in Store caps how many ingest at once) and the
      // corner panel takes over from there.
      let ok = 0;
      let failed = 0;
      let pluginOk = 0;
      for (const m of toImport) {
        // A plugin-claimed file parses server-side through the plugin's own
        // endpoint — synchronous, since the jobs pipeline only knows the
        // built-in kinds. A scan-matched extension no loaded plugin claims
        // falls through to the delimited parser, the pre-plugin behavior.
        const fmt = m.kind === 'plugin' ? pluginFormatFor(m.path) : null;
        // Reproduce the on-disk tree in the sidebar: the file's basename is
        // the table name, and its nested directory (posix, "" at the scan
        // root) becomes the folder the server files it under. This replaces
        // the old behaviour of stuffing the whole rel_path into the name.
        const slash = m.rel_path.lastIndexOf('/');
        const base = slash === -1 ? m.rel_path : m.rel_path.slice(slash + 1);
        const folder_path = slash === -1 ? '' : m.rel_path.slice(0, slash);
        try {
          if (fmt) {
            toast(`Importing ${m.rel_path}…`, 60000);
            await post('/api/ingest/plugin/path', {
              path: m.path, name: base, folder_path, format_id: fmt.id, options: defaultPluginOptions(fmt),
            });
            pluginOk++;
          } else {
            await post('/api/ingest/jobs/path', { path: m.path, name: base, folder_path, kind: ['json', 'plaso', 'text', 'iis', 'kv'].includes(m.kind) ? m.kind : 'csv' });
          }
          ok++;
        } catch (e) {
          failed++;
          toast(`Could not queue ${m.rel_path}: ` + e.message, 6000);
        }
      }
      if (pluginOk) await loadSources(); // sync plugin imports don't announce themselves through a job
      startJobsPoll();
      $('modal').hidden = true;
      toast(`Queued ${ok} import${ok === 1 ? '' : 's'}${failed ? ` — ${failed} failed to queue` : ''} — progress in the corner panel`, 4000);
    });
    importBtn.style.marginLeft = 'auto';
    actions.append(saveBtn, deleteBtn, importBtn, cancelBtn);
    b.append(actions);

    syncFolder();
    syncSettings();
    syncProfileButtons();
    side.paint();
    renderResults();
    if (st.root) runScan();
  }, { wide: true, tall: true, focus: '.side-search' });
}

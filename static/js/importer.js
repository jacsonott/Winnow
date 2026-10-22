/* Every way a file becomes a source: the import modal and its queue, the
SQLite table picker, folder import, and OS drag-and-drop.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, dragHas, el, post, toast } from './core.js';
import { maybeOfferAssociation } from './assoc.js';
import { openFolderBrowser } from './home.js';
import { startJobsPoll, uploadWithProgress } from './jobs.js';
import { ARCHIVE_IMPORT_EXTENSIONS, IIS_FORMAT_LABELS, IIS_IMPORT_TYPE, PLASO_IMPORT_EXTENSIONS, RECOGNIZED_IMPORT_EXTENSIONS, SQLITE_IMPORT_EXTENSIONS, XLSX_IMPORT_EXTENSIONS, extOf, iisLogFormat, importKindFor, openImportPreview, openJsonImportPreview } from './merge.js';
import { renderPluginTabs, renderPluginPanelButtons } from './plugins.js';
import { fmtBytes } from './tables.js';
import { loadSources } from './sources.js';
import { S } from './state.js';
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
   name routing for the Plugins panel's explicit per-format picker. */
export function queueItem(transport, name, fmt = pluginFormatFor(name)) {
  if (fmt) {
    return { ...transport, name, kind: 'plugin', format_id: fmt.id,
             options: defaultPluginOptions(fmt), configured: false };
  }
  const kind = importKindFor(name);
  return kind === 'json' ? { ...transport, name, kind, flatten_mode: 'none', flatten_depth: 1, configured: false }
    : kind === 'sqlite' || kind === 'xlsx' ? { ...transport, name, kind, tables: null, configured: false }
    // A .plaso is one file → one timeline table with nothing to configure.
    : kind === 'plaso' ? { ...transport, name, kind, configured: true }
    // An IIS log names its own columns; the preview can still pick another
    // of the three formats (log_format, null = the name's) and set types.
    : kind === 'iis' ? { ...transport, name, kind, log_format: null, column_types: null, configured: false }
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
async function looksBinaryFile(f) {
  try {
    const head = new Uint8Array(await f.slice(0, BINARY_PROBE_BYTES).arrayBuffer());
    if (head.length >= 2 && ((head[0] === 0xFF && head[1] === 0xFE) || (head[0] === 0xFE && head[1] === 0xFF))) return false;
    return head.includes(0);
  } catch { return false; }
}

export async function queueFiles(files) {
  let bigNamed = null, bigCount = 0;
  const skipped = [];
  for (const f of [...files]) {
    const item = queueItem({ file: f }, f.name);
    if (item.kind === 'text' && await looksBinaryFile(f)) { skipped.push(f.name); continue; }
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
export function queuePaths(entries) {
  // No extension gate any more: a name nothing else claims queues as raw
  // text (importKindFor). A binary file is refused by the importer itself,
  // as a job error — the server has the bytes, the browser doesn't.
  for (const e of entries) S.importQueue.push(queueItem({ path: e.path }, e.name));
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

/* The one way to bring files into a case — queue any number of CSV/TSV or
   JSON/JSONL files (kind picked per file from its extension), optionally
   preview/configure each (delimiter+header+column-types for CSV,
   flatten mode+depth for JSON — openImportPreview/openJsonImportPreview
   both take the same {initial, onConfirm, onCancel} shape so either can
   sit behind this one "Preview & configure" button), then import them all
   at once. One queue for both kinds rather than a separate "Import
   JSON…" entry point, since from the analyst's side it's the same
   workflow — pick some files, maybe tweak settings, import — regardless
   of which parser ends up handling a given one. */
export function openImportModal() {
  modal('Import', (b) => {
    // One line, and it still names every kind the queue takes (a .plaso or
    // a support bundle must not read as unwelcome). Which ones need tables
    // or sheets picked first, and that imports run in the background, are
    // on the wiki's Cases and importing page.
    b.append(el('p', null, 'Queue CSV, JSON, SQLite, Excel, Plaso, archives or any text log to import.'));

    const queueList = el('div', 'session-queue');

    function renderQueue() {
      queueList.replaceChildren();
      if (!S.importQueue.length) { queueList.append(el('div', 'note-status', 'No files queued.')); return; }
      S.importQueue.forEach((item, i) => {
        const row = el('div', 'row-actions session-row');
        const kindLabel = item.kind === 'plugin'
          ? (pluginFormatById(item.format_id)?.label || item.format_id)
          : item.kind === 'text' ? 'text · one line per row'
          : item.kind === 'iis' ? `IIS log · ${IIS_FORMAT_LABELS[item.log_format || iisLogFormat(item.name)] || 'W3C'}`
          : item.kind;
        const unitNoun = item.kind === 'xlsx' ? 'sheet' : 'table';
        const stateLabel = item.kind === 'sqlite' || item.kind === 'xlsx'
          ? (item.configured ? `${item.tables.length} ${unitNoun}${item.tables.length === 1 ? '' : 's'}` : `pick ${unitNoun}s`) + ` · ${kindLabel}`
          : (item.configured ? 'configured' : 'default settings') + ` · ${kindLabel}`;
        row.append(
          el('span', 'session-name', item.name),
          el('span', 'count', (item.path ? 'by path · ' : '') + stateLabel),
        );
        const cfg = el('button', 'btn ghost',
          item.kind === 'sqlite' ? 'Pick tables…' : item.kind === 'xlsx' ? 'Pick sheets…'
            : item.kind === 'plugin' ? 'Options' : 'Preview & configure');
        if (item.kind === 'plugin' && !(pluginFormatById(item.format_id)?.options || []).length) {
          // Nothing to configure — the format declared no options.
          cfg.disabled = true;
          cfg.title = 'This plugin format has no options';
        }
        if (item.kind === 'plaso') {
          cfg.disabled = true;
          cfg.title = 'A Plaso storage file imports as one flat timeline table — nothing to configure';
        }
        if (item.kind === 'archive') {
          cfg.disabled = true;
          cfg.title = 'Expands on import (nested archives included) — you pick the files from its contents next';
        }
        cfg.onclick = () => {
          if (item.kind === 'plugin') {
            openPluginOptionsForm(item, {
              onConfirm: (options) => {
                Object.assign(item, { options, configured: true });
                openImportModal();
              },
              onCancel: () => openImportModal(),
            });
            return;
          }
          const openPreview = item.kind === 'json' ? openJsonImportPreview
            : item.kind === 'sqlite' ? openSqliteTablePicker
            : item.kind === 'xlsx' ? openXlsxSheetPicker
            : openImportPreview;
          openPreview(item, {
            initial: item,
            onConfirm: (settings) => {
              Object.assign(item, settings, { configured: true });
              openImportModal();
            },
            onCancel: () => openImportModal(),
          });
        };
        const rm = el('button', 'btn ghost', '✕');
        rm.onclick = () => { S.importQueue.splice(i, 1); renderQueue(); };
        if ((item.kind === 'plugin' && !item.path) || item.kind === 'iis') {
          // A plugin claimed this by extension (.log is ESXi's, say), or
          // its name reads like an IIS log's — the way out when it is some
          // other program's log: one line per row.
          const asText = el('button', 'btn ghost', 'As text');
          asText.title = item.kind === 'iis'
            ? 'Import this file as raw text, one line per row, instead of as an IIS log'
            : 'Skip the plugin and import this file as raw text, one line per row';
          asText.onclick = () => {
            Object.assign(item, { kind: 'text', format_id: null, options: null, delimiter: null, has_header: false, column_types: null, log_format: null, configured: false });
            renderQueue();
          };
          row.append(cfg, asText, rm);
        } else {
          row.append(cfg, rm);
        }
        queueList.append(row);
      });
    }
    renderQueue();
    b.append(queueList);

    const queueActs = el('div', 'row-actions');
    // Path first, upload second — deliberate order. Browser and server are
    // the same machine here, so "Add from this machine…" reads the file in
    // place (no upload copy, no spool), which is the transport you want for
    // anything large; the browser picker stays for convenience and for the
    // odd tunneled/remote client where it's the only one that works.
    const pathBtn = el('button', 'btn', 'Add from this machine…');
    pathBtn.title = 'Browse the server\'s own disk and import in place — no upload copy, fastest for big files';
    pathBtn.onclick = () => {
      openFolderBrowser(S.lastBrowsePath || undefined, (sel) => {
        S.lastBrowsePath = sel.dir;
        queuePaths(sel.files);
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
      await queueFiles(files);
      renderQueue();
    };
    addLabel.append(addInput);
    const folderBtn = el('button', 'btn ghost', 'Import a whole folder…');
    folderBtn.title = 'Scan a directory (e.g. KAPE output) against extension + glob patterns';
    folderBtn.onclick = () => openDirectoryImportModal();
    const importAll = el('button', 'btn primary', 'Import all queued');
    // The one primary action in the row, pushed to the far right so it
    // isn't the neighbour of "Import a whole folder…".
    importAll.style.marginLeft = 'auto';
    importAll.onclick = () => {
      if (!S.importQueue.length) { toast('No files queued — add some first.', 3500); return; }
      const unpicked = S.importQueue.find((i) => (i.kind === 'sqlite' || i.kind === 'xlsx') && !i.configured);
      if (unpicked) {
        toast(`Pick which ${unpicked.kind === 'xlsx' ? 'sheets' : 'tables'} to import from ${unpicked.name} first`, 4500);
        return;
      }
      const queue = S.importQueue.slice();
      S.importQueue = [];
      renderQueue();
      $('modal').hidden = true;
      // Deliberately not awaited: uploads run sequentially (one disk, one
      // spool at a time) behind this detached chain while the analyst
      // keeps working; each upload resolves into a background ingest job
      // the corner panel is already tracking.
      (async () => {
        // Plugin ingests are synchronous routes that create no background
        // job, so nothing announces the finished source — the jobs poll
        // only refreshes on a job transition. Counted here, refreshed after
        // the loop; the directory-import loop does the same, for the same
        // reason.
        let pluginOk = 0;
        const expanded = [];   // archive expansion reports, handed to directory import below
        for (const item of queue) {
          if (item.kind === 'archive') {
            toast(`Expanding ${item.name}\u2026`, 60000);
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
          for (const r of expanded.slice(1)) toast(`Also expanded to ${r.root} — import it via Case \u2192 Import \u2192 folder`, 10000);
          openDirectoryImportModal({ root: expanded[0].root });
        }
      })();
    };
    queueActs.append(pathBtn, addLabel, folderBtn, importAll);
    b.append(queueActs);
  }, { wide: true });
}

/* Configure which tables come out of a queued SQLite file — Chromium's
   History/Cookies/Web Data/... or any other .db. Same {initial, onConfirm,
   onCancel} shape as openImportPreview/openJsonImportPreview, so the
   unified import queue can sit one "Pick tables…" button in front of any
   of the three. Shows every table with a row count and (for any column
   that looks like a WebKit/Chrome timestamp — microseconds since
   1601-01-01, Chromium's own convention) a pre-checked option to convert
   it to a readable datetime on import rather than leaving it as an opaque
   integer. Confirm hands back {tables: [{table, timestamp_columns}]} for
   the queue item; the actual import happens later as one background job
   reading every picked table out of one uploaded spool. */
export function openSqliteTablePicker(src, hooks = {}) {
  openUnitPicker(src, hooks, {
    title: 'Pick SQLite tables',
    intro: 'Choose which tables to import from this file — each becomes its own table in the case.',
    empty: 'No tables in this file.',
    confirm: 'Use selected tables',
    checkFirst: 'Check at least one table to import',
    previewKind: 'sqlite',
    uploadUrl: '/api/ingest/sqlite/preview',
  });
}

/* The same picker over an Excel workbook's sheets — a sheet is to a
   workbook what a table is to a SQLite file, so the two share one
   implementation (openUnitPicker) and one queue-item shape ({tables}).
   No timestamp chips here: date-styled cells convert to ISO text
   unconditionally (the day-serial Excel stores means nothing in a filter
   or an export — see xlsxread.py), which the intro line states instead
   of asking. */
export function openXlsxSheetPicker(src, hooks = {}) {
  openUnitPicker(src, hooks, {
    title: 'Pick Excel sheets',
    intro: 'Choose which sheets to import from this workbook — each becomes its own table in the case. '
      + 'Date-formatted cells are converted to readable timestamps on import.',
    empty: 'No data sheets in this workbook.',
    confirm: 'Use selected sheets',
    checkFirst: 'Check at least one sheet to import',
    previewKind: 'xlsx',
    uploadUrl: '/api/ingest/xlsx/preview',
  });
}

function openUnitPicker(src, { initial, onConfirm, onCancel } = {}, cfg) {
  // src is a queue item's transport ({file} or {path, name}) — same contract
  // as openImportPreview/openJsonImportPreview.
  let tables = null; // [{name, row_count, columns, likely_timestamp_columns}]
  const selected = new Map(); // table name -> Set of timestamp columns to convert
  const included = new Set(); // table names checked for import

  modal(cfg.title, (b) => {
    b.append(el('p', null, cfg.intro));

    const pickRow = el('div', 'row-actions');
    const pickStatus = el('span', 'count', '');
    pickRow.append(pickStatus);
    b.append(pickRow);

    const tableList = el('div', 'session-list');
    b.append(tableList);

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn primary', cfg.confirm);
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { if (onCancel) onCancel(); else $('modal').hidden = true; };
    actions.append(importBtn, cancel);
    b.append(actions);

    function renderTables() {
      tableList.replaceChildren();
      if (!tables) return;
      if (!tables.length) { tableList.append(el('div', 'note-status', cfg.empty)); return; }
      for (const t of tables) {
        const row = el('div', 'session-row');
        row.style.flexDirection = 'column';
        row.style.alignItems = 'stretch';
        const head = el('div', 'row-actions');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = included.has(t.name);
        cb.onchange = () => { cb.checked ? included.add(t.name) : included.delete(t.name); };
        const lab = el('label');
        lab.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1';
        // A table the server could not read (a virtual table whose module
        // this SQLite lacks) is shown, with why, but can't be picked —
        // the file's other tables still import.
        if (t.error) cb.disabled = true;
        lab.append(cb, el('span', 'session-name', t.name),
          el('span', 'count', t.error ? `can't be read: ${t.error}` : `${t.row_count.toLocaleString()} rows`));
        head.append(lab);
        row.append(head);
        if (t.likely_timestamp_columns.length) {
          const tsRow = el('div', 'row-actions');
          tsRow.style.cssText = 'flex-wrap:wrap;margin-top:4px';
          tsRow.append(el('span', 'fb-help', 'Convert to readable datetime:'));
          for (const colName of t.likely_timestamp_columns) {
            const chip = el('button', 'btn ghost', colName);
            chip.setAttribute('aria-pressed', String(selected.get(t.name).has(colName)));
            chip.title = 'Toggle converting this WebKit/Chrome-epoch column to an ISO datetime on import';
            chip.onclick = () => {
              const set = selected.get(t.name);
              if (set.has(colName)) set.delete(colName); else set.add(colName);
              chip.setAttribute('aria-pressed', String(set.has(colName)));
            };
            tsRow.append(chip);
          }
          row.append(tsRow);
        }
        tableList.append(row);
      }
    }

    // Factored out of pickInput's own onchange so a file handed in from
    // outside (wireFileDrop) previews identically to one picked by hand —
    // same request, same defaults, same failure handling.
    async function loadFile(f) {
      pickStatus.textContent = 'Reading…';
      try {
        // A path item previews the tables in place; a File uploads the .db
        // once to enumerate them. Which is a fact, not a fingerprint guess.
        const res = f.path
          ? await post('/api/ingest/preview/path', { path: f.path, kind: cfg.previewKind })
          : await (async () => {
              const fd = new FormData();
              fd.append('file', f.file);
              return api(cfg.uploadUrl, { method: 'POST', body: fd });
            })();
        tables = res.tables;
      } catch (e) {
        pickStatus.textContent = '';
        toast('Could not read that file: ' + e.message, 6000);
        return;
      }
      pickStatus.textContent = f.name;
      included.clear();
      selected.clear();
      const prior = initial && initial.tables
        ? new Map(initial.tables.map((t) => [t.table, new Set(t.timestamp_columns || [])]))
        : null;
      for (const t of tables) {
        // Re-opening the picker restores the previous choices; a fresh file
        // defaults to converting every detected timestamp column.
        if (prior) {
          if (prior.has(t.name) && !t.error) included.add(t.name);
          selected.set(t.name, prior.get(t.name) || new Set(t.likely_timestamp_columns));
        } else {
          selected.set(t.name, new Set(t.likely_timestamp_columns));
        }
      }
      renderTables();
    }
    loadFile(src);

    importBtn.onclick = () => {
      const targets = [...included];
      if (!targets.length) { toast(cfg.checkFirst); return; }
      onConfirm({
        tables: targets.map((tableName) => ({
          table: tableName,
          timestamp_columns: [...selected.get(tableName)],
        })),
      });
    };
  }, { wide: true });
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
            await post('/api/ingest/jobs/path', { path: m.path, name: base, folder_path, kind: ['json', 'plaso', 'text', 'iis'].includes(m.kind) ? m.kind : 'csv' });
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

/* Options form for one queued plugin-format file, rendered generically
   from the format's declared option specs (bool/text/choice — see
   plugin_api.PluginAPI.register_ingest_format). Same {onConfirm, onCancel}
   shape as openImportPreview/openJsonImportPreview so openImportModal's
   one "configure" button can sit in front of any of the three. */
export function openPluginOptionsForm(item, { onConfirm, onCancel }) {
  const fmt = pluginFormatById(item.format_id);
  const values = { ...defaultPluginOptions(fmt), ...(item.options || {}) };
  modal(fmt.label, (b) => {
    if (fmt.description) b.append(el('p', null, fmt.description));
    for (const o of fmt.options || []) {
      if (o.type === 'bool') {
        const label = el('label');
        label.style.cssText = 'display:block;margin-bottom:10px';
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = !!values[o.name];
        cb.onchange = () => { values[o.name] = cb.checked; };
        label.append(cb, document.createTextNode(' ' + (o.label || o.name)));
        b.append(label);
      } else if (o.type === 'choice') {
        b.append(el('label', null, o.label || o.name));
        const sel = el('select');
        sel.style.cssText = 'display:block;margin-bottom:10px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
        for (const c of o.choices || []) {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c;
          sel.append(opt);
        }
        sel.value = values[o.name] ?? o.default ?? '';
        sel.onchange = () => { values[o.name] = sel.value; };
        b.append(sel);
      } else {
        b.append(el('label', null, o.label || o.name));
        const inp = el('input');
        inp.type = 'text';
        inp.value = values[o.name] ?? '';
        inp.oninput = () => { values[o.name] = inp.value; };
        b.append(inp);
      }
    }
    const acts = el('div', 'row-actions');
    const ok = el('button', 'btn', 'Use these options');
    ok.onclick = () => onConfirm(values);
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = onCancel;
    acts.append(ok, cancel);
    b.append(acts);
  });
}

/* Every way a file becomes a source: the import modal and its queue, the
SQLite table picker, folder import, and OS drag-and-drop.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, debounce, el, post, toast } from './core.js';
import { openFolderBrowser } from './home.js';
import { startJobsPoll, uploadWithProgress } from './jobs.js';
import { RECOGNIZED_IMPORT_EXTENSIONS, SQLITE_IMPORT_EXTENSIONS, extOf, importKindFor, openImportPreview, openJsonImportPreview } from './merge.js';
import { renderPluginTabs } from './plugins.js';
import { fmtBytes } from './tables.js';
import { loadSources } from './sources.js';
import { S } from './state.js';
import { confirmDialog, modal, promptDialog } from './ui.js';

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
    S.pluginDirs = r.dirs || [];
  } catch { S.plugins = []; S.pluginFormats = []; S.pluginTabs = []; S.pluginDirs = []; }
  renderPluginTabs();
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
   stays reachable through the Plugins modal's explicit per-format picker. */
export function pluginFormatFor(filename) {
  const base = filename.split(/[\\/]/).pop();
  const ext = extOf(base);
  if (RECOGNIZED_IMPORT_EXTENSIONS.includes(ext) || SQLITE_IMPORT_EXTENSIONS.includes(ext)) return null;
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
    : kind === 'sqlite' ? { ...transport, name, kind, tables: null, configured: false }
    : { ...transport, name, kind, delimiter: null, has_header: true, column_types: null, configured: false };
}

export function queueFiles(files) {
  let bigNamed = null, bigCount = 0;
  for (const f of files) {
    if (f.size >= UPLOAD_ADVISORY_BYTES) { bigCount++; bigNamed = bigNamed || f; }
    S.importQueue.push(queueItem({ file: f }, f.name));
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
  // Same gate the drop handler applies (recognizedImportFile) — the server
  // lists every file on purpose, so the filter has to live here: an
  // unrecognized extension would otherwise fall through importKindFor's
  // default and ingest a .zip as a CSV.
  const known = entries.filter((e) => recognizedImportFile(e.name));
  const skipped = entries.filter((e) => !recognizedImportFile(e.name));
  for (const e of known) S.importQueue.push(queueItem({ path: e.path }, e.name));
  if (skipped.length) {
    toast(`Skipped ${skipped.length} file${skipped.length === 1 ? '' : 's'} no importer recognizes: ${skipped.map((e) => e.name).join(', ')}`, 6000);
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
    b.append(el('p', null,
      'Queue CSV/TSV, JSON/JSONL, or SQLite files (a SQLite file needs its tables picked first), '
      + 'then import them all — imports run in the background, so you can keep working while the '
      + 'corner panel tracks progress.'));

    const queueList = el('div', 'session-queue');

    function renderQueue() {
      queueList.replaceChildren();
      if (!S.importQueue.length) { queueList.append(el('div', 'note-status', 'No files queued.')); return; }
      S.importQueue.forEach((item, i) => {
        const row = el('div', 'row-actions session-row');
        const kindLabel = item.kind === 'plugin'
          ? (pluginFormatById(item.format_id)?.label || item.format_id)
          : item.kind;
        const stateLabel = item.kind === 'sqlite'
          ? (item.configured ? `${item.tables.length} table${item.tables.length === 1 ? '' : 's'}` : 'pick tables') + ' · sqlite'
          : (item.configured ? 'configured' : 'default settings') + ` · ${kindLabel}`;
        row.append(
          el('span', 'session-name', item.name),
          el('span', 'count', (item.path ? 'by path · ' : '') + stateLabel),
        );
        const cfg = el('button', 'btn ghost',
          item.kind === 'sqlite' ? 'Pick tables…' : item.kind === 'plugin' ? 'Options' : 'Preview & configure');
        if (item.kind === 'plugin' && !(pluginFormatById(item.format_id)?.options || []).length) {
          // Nothing to configure — the format declared no options.
          cfg.disabled = true;
          cfg.title = 'This plugin format has no options';
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
        row.append(cfg, rm);
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
    addInput.accept = [...RECOGNIZED_IMPORT_EXTENSIONS, ...SQLITE_IMPORT_EXTENSIONS, ...pluginExtensions()].join(',');
    addInput.multiple = true;
    addInput.hidden = true;
    addInput.onchange = () => {
      queueFiles(addInput.files);
      addInput.value = '';
      renderQueue();
    };
    addLabel.append(addInput);
    const folderBtn = el('button', 'btn ghost', 'Import a whole folder…');
    folderBtn.title = 'Scan a directory (e.g. KAPE output) against extension + glob patterns';
    folderBtn.onclick = () => openDirectoryImportModal();
    const importAll = el('button', 'btn', 'Import all queued');
    importAll.onclick = () => {
      if (!S.importQueue.length) return;
      const unpicked = S.importQueue.find((i) => i.kind === 'sqlite' && !i.configured);
      if (unpicked) {
        toast(`Pick which tables to import from ${unpicked.name} first`, 4500);
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
        for (const item of queue) {
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
          } else if (item.kind === 'sqlite') {
            fd.append('tables', JSON.stringify(item.tables));
          } else {
            if (item.delimiter) fd.append('delimiter', item.delimiter);
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
export function openSqliteTablePicker(src, { initial, onConfirm, onCancel } = {}) {
  // src is a queue item's transport ({file} or {path, name}) — same contract
  // as openImportPreview/openJsonImportPreview.
  let tables = null; // [{name, row_count, columns, likely_timestamp_columns}]
  const selected = new Map(); // table name -> Set of timestamp columns to convert
  const included = new Set(); // table names checked for import

  modal('Pick SQLite tables', (b) => {
    b.append(el('p', null,
      'Choose which tables to import from this file — each becomes its own source.'));

    const pickRow = el('div', 'row-actions');
    const pickStatus = el('span', 'count', '');
    pickRow.append(pickStatus);
    b.append(pickRow);

    const tableList = el('div', 'session-list');
    b.append(tableList);

    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', 'Use selected tables');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { if (onCancel) onCancel(); else $('modal').hidden = true; };
    actions.append(importBtn, cancel);
    b.append(actions);

    function renderTables() {
      tableList.replaceChildren();
      if (!tables) return;
      if (!tables.length) { tableList.append(el('div', 'note-status', 'No tables in this file.')); return; }
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
        lab.append(cb, el('span', 'session-name', t.name), el('span', 'count', `${t.row_count.toLocaleString()} rows`));
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
          ? await post('/api/ingest/preview/path', { path: f.path, kind: 'sqlite' })
          : await (async () => {
              const fd = new FormData();
              fd.append('file', f.file);
              return api('/api/ingest/sqlite/preview', { method: 'POST', body: fd });
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
          if (prior.has(t.name)) included.add(t.name);
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
      if (!targets.length) { toast('Check at least one table to import'); return; }
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
   Session menu.

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
  const isFileDrag = (e) => !!(e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files'));

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

/* A single dropped file recognized as SQLite opens the table-picker flow
   (it can't just queue-and-import like CSV/JSON — which table(s) to pull
   out is a real choice the analyst has to make, same as picking one by
   hand already requires). Anything else recognized (by extension — a raw
   drop has no equivalent of a file-picker's `accept` doing this filtering
   natively, so it happens here) queues into the same CSV/JSON import
   modal "Choose files…" already uses. Unrecognized files are dropped
   silently from the queue but named in a toast — better than a mysterious
   partial import with no explanation. */
/* Whether ANY importer will take this filename — built-in extensions or a
   loaded plugin format. The shared gate for the two entry points that see
   unfiltered listings: OS drops and the server-disk picker. */
export function recognizedImportFile(name) {
  return RECOGNIZED_IMPORT_EXTENSIONS.includes(extOf(name))
    || SQLITE_IMPORT_EXTENSIONS.includes(extOf(name))
    || !!pluginFormatFor(name);
}

export function handleDroppedFiles(files) {
  if (!files.length) return;
  const known = (f) => recognizedImportFile(f.name);
  const recognized = files.filter(known);
  const skipped = files.filter((f) => !known(f));
  if (!recognized.length) {
    toast(`No recognized files in the drop (${skipped.map((f) => f.name).join(', ')})`, 5000);
    return;
  }
  queueFiles(recognized);
  openImportModal();
  if (skipped.length) {
    toast(`Skipped ${skipped.length} unrecognized file${skipped.length === 1 ? '' : 's'}: ${skipped.map((f) => f.name).join(', ')}`, 5000);
  }
}

export const patternLines = (text) => text.split('\n').map((l) => l.trim()).filter(Boolean);

export async function loadImportProfiles() {
  try { S.importProfiles = await api('/api/import_profiles'); } catch { S.importProfiles = []; }
}

/* Point at a folder — a KAPE triage output or any other bulk-collection
   directory — and import every file inside that matches an extension plus
   include/exclude glob pattern set, instead of picking files one at a time
   the way openImportModal's queue does. Patterns can be built ad hoc or
   loaded from (and saved back to) a named workspace/import_profiles.json
   profile, so a profile built once for "KAPE" keeps working on every future
   triage without re-typing its exclusions.

   Async at the top level (unlike every other open* modal function here,
   which stay synchronous and do async work via inner handlers) because
   S.importProfiles — unlike S.savedFilters/S.tags/etc. — has no earlier,
   source-open-triggered load point to piggyback on; this is the one place
   that ever needs it, so it's simplest to just await a fresh copy before
   building the profile <select> at all, rather than rendering once with a
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
    extensions: state.extensions || RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions()),
    includeText: state.includeText || '',
    excludeText: state.excludeText || '',
  };
  let scanResult = null; // {root, matched, excluded, truncated}
  let checked = new Set(); // indices into scanResult.matched
  let showExcluded = false;
  let scanSeq = 0; // guards a slow scan response from overwriting a newer one

  modal('Import a folder', (b) => {
    b.append(el('p', null,
      'Point at a folder and import every file inside that matches — built for a KAPE triage or '
      + 'similar bulk-collection output. Pick a saved profile or build patterns ad hoc; the preview '
      + 'below updates as you edit them.'));

    const resultsBox = el('div', 'search-all-results');
    const actions = el('div', 'row-actions');
    const importBtn = el('button', 'btn', 'Import checked (0)');
    importBtn.disabled = true;

    async function runScan() {
      const seq = ++scanSeq;
      if (!st.root) { scanResult = null; checked = new Set(); renderResults(); return; }
      resultsBox.replaceChildren(el('div', 'note-status', 'Scanning…'));
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
        resultsBox.replaceChildren(el('div', 'note-status', 'Scan failed: ' + e.message));
        return;
      }
      if (seq !== scanSeq) return; // a newer scan resolved first
      scanResult = r;
      // Pre-check everything except what's already in the case — the
      // common case is "import the new stuff", and already_imported exists
      // precisely so a second pass over the same folder doesn't default to
      // silently re-importing (and duplicating tabs for) everything.
      checked = new Set();
      r.matched.forEach((m, i) => { if (!m.already_imported) checked.add(i); });
      renderResults();
    }
    const scheduleScan = debounce(runScan, 300);

    function renderResults() {
      resultsBox.replaceChildren();
      const n = checked.size;
      importBtn.disabled = n === 0;
      importBtn.textContent = `Import checked (${n})`;
      if (!st.root) { resultsBox.append(el('div', 'note-status', 'Choose a folder to preview matches.')); return; }
      if (!scanResult) return; // "Scanning…" is already showing
      if (scanResult.truncated) {
        resultsBox.append(el('div', 'note-status', `Showing the first ${scanResult.matched.length + scanResult.excluded.length} files — narrow the folder or patterns to see the rest.`));
      }
      if (!scanResult.matched.length) {
        resultsBox.append(el('div', 'note-status', 'No files match.'));
      } else {
        resultsBox.append(el('div', 'fb-help', `Will import (${scanResult.matched.length})`));
        scanResult.matched.forEach((m, i) => {
          const row = el('div', 'search-all-row');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = checked.has(i);
          cb.onchange = () => { cb.checked ? checked.add(i) : checked.delete(i); renderResults(); };
          row.append(cb, el('span', 'search-all-name', m.rel_path + (m.already_imported ? '  (already in case)' : '')));
          resultsBox.append(row);
        });
      }
      if (scanResult.excluded.length) {
        const toggle = el('button', 'btn ghost', (showExcluded ? '▾ ' : '▸ ') + `Excluded (${scanResult.excluded.length})`);
        toggle.onclick = () => { showExcluded = !showExcluded; renderResults(); };
        resultsBox.append(toggle);
        if (showExcluded) {
          for (const e of scanResult.excluded) {
            const row = el('div', 'search-all-row');
            row.append(el('span', 'search-all-name', e.rel_path), el('span', 'search-all-count', e.reason));
            resultsBox.append(row);
          }
        }
      }
    }

    // --- folder row
    const folderRow = el('div', 'row-actions');
    const folderLabel = el('span', 'note-status', st.root || 'No folder chosen');
    folderLabel.style.cssText = 'font-family:var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      openFolderBrowser(st.root || undefined, (path) => {
        openDirectoryImportModal({ ...st, root: path });
      }, () => openDirectoryImportModal(st));
    };
    folderRow.append(folderLabel, browseBtn);
    b.append(folderRow);

    // --- profile row
    const profileRow = el('div', 'row-actions');
    const profileSel = el('select');
    profileSel.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
    const customOpt = document.createElement('option');
    customOpt.value = '';
    customOpt.textContent = 'Custom (not saved)';
    profileSel.append(customOpt);
    for (const p of S.importProfiles) {
      const opt = document.createElement('option');
      opt.value = String(p.id);
      opt.textContent = p.name;
      profileSel.append(opt);
    }
    profileSel.value = st.profileId ? String(st.profileId) : '';
    profileSel.onchange = () => {
      const id = profileSel.value ? Number(profileSel.value) : null;
      const p = id ? S.importProfiles.find((x) => x.id === id) : null;
      openDirectoryImportModal({
        ...st,
        profileId: id,
        recursive: p ? p.recursive : true,
        extensions: (p && p.extensions) || RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions()),
        includeText: p ? (p.include_patterns || []).join('\n') : '',
        excludeText: p ? (p.exclude_patterns || []).join('\n') : '',
      });
    };
    profileRow.append(profileSel);
    if (st.profileId) {
      const delBtn = el('button', 'btn ghost', 'Delete profile');
      delBtn.onclick = async () => {
        const name = S.importProfiles.find((p) => p.id === st.profileId)?.name || 'this profile';
        if (!(await confirmDialog(`Delete the "${name}" profile?`, { danger: true, okLabel: 'Delete' }))) return;
        await api(`/api/import_profiles/${st.profileId}`, { method: 'DELETE' });
        openDirectoryImportModal({ ...st, profileId: null });
      };
      profileRow.append(delBtn);
    }
    b.append(profileRow);

    // --- recursive checkbox
    const recLabel = el('label');
    recLabel.style.cssText = 'display:block;margin-bottom:10px';
    const recCb = el('input');
    recCb.type = 'checkbox';
    recCb.checked = st.recursive;
    recCb.onchange = () => { st.recursive = recCb.checked; scheduleScan(); };
    recLabel.append(recCb, document.createTextNode(' Include subfolders'));
    b.append(recLabel);

    // --- extension chips
    b.append(el('label', null, 'File types'));
    const extRow = el('div', 'row-actions');
    extRow.style.flexWrap = 'wrap';
    for (const ext of RECOGNIZED_IMPORT_EXTENSIONS.concat(pluginExtensions())) {
      const chip = el('button', 'btn ghost', ext);
      chip.setAttribute('aria-pressed', String(st.extensions.includes(ext)));
      chip.onclick = () => {
        st.extensions = st.extensions.includes(ext)
          ? st.extensions.filter((e) => e !== ext)
          : [...st.extensions, ext];
        chip.setAttribute('aria-pressed', String(st.extensions.includes(ext)));
        scheduleScan();
      };
      extRow.append(chip);
    }
    b.append(extRow);

    // --- include/exclude patterns
    const patRow = el('div', 'row-actions');
    patRow.style.alignItems = 'stretch';
    const includeCol = el('div');
    includeCol.style.cssText = 'flex:1;display:flex;flex-direction:column;gap:4px;min-width:0';
    includeCol.append(el('label', null, 'Include patterns (blank = every recognized file)'));
    const includeArea = el('textarea');
    includeArea.rows = 3;
    includeArea.spellcheck = false;
    includeArea.placeholder = 'One glob per line, e.g. *EvtxECmd*';
    includeArea.value = st.includeText;
    includeArea.oninput = () => { st.includeText = includeArea.value; scheduleScan(); };
    includeCol.append(includeArea);

    const excludeCol = el('div');
    excludeCol.style.cssText = 'flex:1;display:flex;flex-direction:column;gap:4px;min-width:0';
    excludeCol.append(el('label', null, 'Exclude patterns'));
    const excludeArea = el('textarea');
    excludeArea.rows = 3;
    excludeArea.spellcheck = false;
    excludeArea.placeholder = 'One glob per line, e.g. *_Amcache_UnassociatedFileEntries.csv';
    excludeArea.value = st.excludeText;
    excludeArea.oninput = () => { st.excludeText = excludeArea.value; scheduleScan(); };
    excludeCol.append(excludeArea);

    patRow.append(includeCol, excludeCol);
    b.append(patRow);

    // --- save-as-profile
    const saveRow = el('div', 'row-actions');
    const saveBtn = el('button', 'btn ghost', st.profileId ? 'Update profile' : 'Save as profile…');
    saveBtn.onclick = async () => {
      let name = S.importProfiles.find((p) => p.id === st.profileId)?.name;
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
      openDirectoryImportModal({ ...st, profileId: rec.id });
    };
    saveRow.append(saveBtn);
    b.append(saveRow);

    b.append(resultsBox);

    const cancelBtn = el('button', 'btn ghost', 'Cancel');
    cancelBtn.onclick = () => { $('modal').hidden = true; };
    importBtn.onclick = async () => {
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
        try {
          if (fmt) {
            toast(`Importing ${m.rel_path}…`, 60000);
            await post('/api/ingest/plugin/path', {
              path: m.path, name: m.rel_path, format_id: fmt.id, options: defaultPluginOptions(fmt),
            });
            pluginOk++;
          } else {
            await post('/api/ingest/jobs/path', { path: m.path, name: m.rel_path, kind: m.kind === 'json' ? 'json' : 'csv' });
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
    };
    actions.append(importBtn, cancelBtn);
    b.append(actions);

    renderResults();
    if (st.root) runScan();
  }, { wide: true });
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

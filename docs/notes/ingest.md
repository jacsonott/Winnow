# Ingest: CSV, JSON, SQLite, folders, drops, jobs

Every path that turns a file on disk into a `src_<id>` table, and the
background-job machinery all of them run through. Invariant #1 (source
tables are never mutated) is what they all exist to preserve.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

- Ingesting a table out of an *external* SQLite file (Chromium's History/
  Cookies/Web Data/..., or any other .db — `ingest_sqlite_table`/
  `preview_sqlite_tables`, "Import SQLite tables…") opens that file with
  `mode=ro` — a second, separate connection from `self.db`, never touched —
  and copies rows into a normal `src_<id>` table with the same all-TEXT
  convention as CSV ingest. A column gets converted from a WebKit/Chrome
  timestamp (microseconds since 1601-01-01 — Chromium's own convention for
  every `*_time`/`*_utc` column) to a readable ISO datetime only if the
  analyst opts in per column (`timestamp_columns`, pre-checked by
  `preview_sqlite_tables`' heuristic when a column's name and sampled
  values look like one, via `_webkit_to_iso`) — never automatically, and
  never in place in the source file. A BLOB value becomes `<N bytes>`
  rather than attempting to stringify binary data.
- JSON/JSONL ingest (`ingest_json`/`preview_json_file`/`_flatten_json`) has
  no fixed header row the way CSV's first line is one, so it can't type
  columns from row 1 alone — it makes **two full passes** over the file:
  pass 1 flattens every record just far enough to collect the union of
  column keys (in first-seen order), pass 2 re-reads and inserts against
  that now-fixed column set, padding "" for any key a given record doesn't
  have (same convention as a short CSV row). `_flatten_json`'s `max_depth`
  unfolds nested **objects** into dotted columns (`user.name`) but never
  arrays, at any depth — an array is always JSON-stringified into one
  column as-is, since its length can vary record to record and
  index-expanding it would make the column set itself unstable the way a
  nested object's key set usually isn't. `.jsonl`/`.ndjson` streams line by
  line (each pass); a single `.json` document has to be `json.load()`ed
  whole (no generic streaming parser in the standard library) — memory use
  scales with file size for that shape specifically.
- Ragged rows are padded/trimmed to the header width, never dropped, and
  counted — `ingest_csv`'s return dict has a `ragged_rows` count surfaced to
  the analyst (toast in the UI, printed in the CLI path). If that first row
  (header, or first data row when `has_header=False`) happens to be short,
  every other row gets trimmed to match it; there's no whole-file pre-scan to
  pick a "correct" width, so this count is the only signal something's off.
- **Directory import** (`Store.scan_import_directory`, `POST
  /api/ingest/dir/scan`, the "Import a folder…" modal) is a preview-then-
  commit design, not a bulk-ingest endpoint — the scan step never touches
  `self.db` beyond one cheap `SELECT path FROM sources` (to flag
  `already_imported`), so it's safe to re-run live on every pattern edit.
  The frontend decides what actually gets imported and does it by looping
  over `/api/ingest/path` / the new `/api/ingest/json/path` sibling, one
  file at a time — same per-file, try/catch, toast-per-file loop
  `openImportModal`'s "Import all queued" already used, just fed by the
  scan's matches instead of a manual file picker. A pattern containing `/`
  matches the file's path relative to the scan root; one without matches
  the bare filename anywhere in the tree — this is what lets
  `*_Amcache_UnassociatedFileEntries.csv` (KAPE/Amcache's list of
  executables that merely shipped with Windows, not triage signal) work as
  a simple glob while `RegistryHives/*` can still exclude a whole
  subfolder. Matching is case-insensitive (EZTools output casing isn't
  consistent enough to make an analyst type it exactly). `already_imported`
  never blocks a re-import (no hard skip server-side) — it's only the
  frontend's default-uncheck signal, so pointing the same import at the
  same folder twice doesn't silently duplicate every table, but re-
  importing on purpose (e.g. the source file changed) is still one click
  away. Saved patterns (`workspace.ImportProfiles`, `import_profiles.json`)
  are named and cross-case like `TimelineTemplates`/`ColumnLayouts`, but
  upsert-by-id like tags rather than keyed by an implicit natural key —
  there's no "header set" to key on before anything's been scanned yet.
  `openDirectoryImportModal` is the one `open*` modal function that's
  `async` at its own top level (every other one stays synchronous and does
  async work via inner handlers) — `S.importProfiles`, unlike
  `S.savedFilters`/`S.tags`/etc., has no earlier source-open-triggered load
  point to piggyback on, so it just awaits a fresh copy before building the
  profile `<select>` at all.
- **There is one import entry point** — the Session menu's "Import…" →
  `openImportModal`, whose queue now takes CSV/TSV, JSON/JSONL *and*
  SQLite files (`importKindFor` routes by extension; a sqlite item's
  "Pick tables…" opens `openSqliteTablePicker`, the old standalone
  sqlite modal reshaped into the same `{initial, onConfirm, onCancel}`
  contract the CSV/JSON previews already had, storing
  `{tables: [{table, timestamp_columns}]}` on the queue item). The
  directory-import modal is reached from inside it ("Import a whole
  folder…") and both flows start background jobs rather than awaiting
  sync uploads — "Import all queued" closes the modal immediately and a
  detached async chain uploads sequentially (one disk, one spool at a
  time) while the jobs panel tracks everything. Don't add a second
  menu entry per format again; the queue is the router.
  Every queue item is a picked/dropped `{file: File}`, and the no-copy
  transport is chosen **invisibly**: before uploading, each item tries
  `resolveLocalFile` → `POST /api/ingest/resolve_local`, and a hit
  imports via `/api/ingest/jobs/path` reading the file **in place** — no
  upload leg, no tempfile spool, no 50 GB copied to produce a file that
  was already on the disk. The three configure previews run the same
  resolve first and, on a hit, preview by path too (`POST
  /api/ingest/preview/path` — bounded CSV sample / the path-based
  json/sqlite store previews; this matters most for JSON, whose upload
  preview round-trips the whole file). There is deliberately **no
  visible control** for any of this: a server-disk file picker ("Add
  from this machine…", `openServerFileBrowser`, `browse_dir?files=true`)
  was built and then removed on request — one Import button, two
  transports, and the only visible difference is the upload phase not
  existing. Don't reintroduce a picker; directory import remains the
  explicit path route, for folders. The
  sandbox can't be asked for the path, but a same-host client's picked
  file necessarily exists on the server's own disk, so the frontend sends
  a fingerprint — name, size, mtime, first/last 64 KB via `File.slice`
  (two tiny reads even on 50 GB) — and the server looks for it in a fixed
  handful of candidate dirs (recently browsed/scanned dirs, dirs of
  previous imports, registered cases' dirs, Downloads/Desktop/Documents/
  home — stat calls, never a disk search). A hit imports by path with no
  upload; a miss falls back to the upload silently — resolution is an
  optimization, never a failure mode, and never a user decision. The
  match is deliberately strict (exact name+size, mtime ±2s, byte-equal
  head *and* tail), which is also the answer to the loopback check's one
  hole: an SSH-tunneled remote client looks local (`request.client` is
  the socket peer — header-spoof-proof, but a tunnel terminates locally),
  and strict content equality means the only file it can ever be handed
  is byte-identical at both ends anyway. Names are `basename()`d before
  joining, so a crafted name can't traverse out of a candidate dir.
  `_is_loopback` also admits Starlette's literal "testclient" peer —
  never a real IP, so it can't admit a network peer, and it keeps the
  TestClient suite honest without monkeypatching.
- **Dragging a file from the OS onto the window** (`wireFileDrop`,
  `handleDroppedFiles`) is an alternative entry point into the *existing*
  import flows, not a new one — every dropped file (CSV/JSON *and*
  SQLite) queues into the same `S.importQueue`/`openImportModal` a picked
  file does (via `queueFiles`, factored out of the file `<input>`'s own
  `onchange`); a queued SQLite item still has to go through "Pick
  tables…" (`openSqliteTablePicker`) before it can import — which
  table(s) to pull out is a real choice, so it can't just auto-import the
  way CSV/JSON does. The one genuinely new piece is recognizing what was
  dropped at all: a raw OS drop has no equivalent of a `<input accept>`
  filtering what's offered, so `handleDroppedFiles` filters by extension
  itself, against `RECOGNIZED_IMPORT_EXTENSIONS`/
  `SQLITE_IMPORT_EXTENSIONS` — the same lists the import modal's own
  `accept` attribute is built from, so there's one true list per format
  instead of three hand-typed copies. Every listener in `wireFileDrop`
  gates on `e.dataTransfer.types.includes('Files')` — an OS file drag
  carries a `'Files'` type; every *internal* drag (`wireDragReorder`,
  column-header reorder, the group-by pill drag) only ever carries
  `'text/plain'` — so dragging a tab or a sidebar row never triggers the
  drop overlay or fights with those handlers' own dragover/drop listeners
  on the same window-level events. The overlay's shown/hidden state is a
  depth counter, not a boolean: `dragenter`/`dragleave` fire on every
  element boundary a drag crosses, not just the window's, so a naive
  enter-shows/leave-hides flickers as the pointer crosses any child
  element underneath.
- **Every import runs as a background job** (`Store.start_ingest_job` /
  `_ingest_job_worker`, `POST /api/ingest/jobs/{path,upload}`, `GET
  /api/ingest/jobs`, per-job cancel) — the search-all job pattern made
  plural, since a directory import legitimately starts many at once (a
  semaphore caps concurrent parses at `MAX_CONCURRENT_INGESTS`; the rest
  sit `queued`). The three `ingest_*` paths' per-BATCH `progress` callback
  (the one backlog item 5 said nothing consumed) feeds the job record —
  and consuming it exposed that it never worked: `fh.tell()` on a text
  file being iterated raises "telling position disabled by next() call"
  the moment csv.reader drives the iterator. `fh.buffer.tell()` (the
  BufferedReader's byte position, ahead by at most the 1 MB read buffer)
  is the legal spelling; CSV progress is therefore bytes/size — a
  percentage with no pre-scan — while JSON reports records (pass 1 already
  counts them) and SQLite rows (`COUNT(*)` known up front). Cancellation
  is cooperative per BATCH, and **a cancelled ingest drops its partial
  source** — the deliberate opposite of a mid-file *error*, which keeps
  what committed: the analyst asked for the source not to exist, and a
  half-table looks exactly like a complete import in every list.
  `Store.close()` cancels and joins running jobs so a case switch can't
  strand a worker on a closed connection. One sqlite job takes N tables
  from one spooled upload (`options["tables"]`) rather than re-uploading
  the file per table. Upload spools are deleted when the job ends —
  the old sync upload endpoints (kept for compat, same `finally` added)
  leaked the full file size in the OS tempdir on every upload, found as
  a stray 50 GB tempfile. Frontend: `uploadWithProgress` (XHR — fetch
  can't report upload progress) plus `pollJobs`/`renderJobsPanel`, the
  bottom-right panel that also surfaces `fts_building` — background index
  builds used to be invisible, and a server restart kills one silently
  (`_build_fts_worker` swallows everything; the next search retries).
  `boot()` restarts the poll so a reload mid-import picks the job back up.

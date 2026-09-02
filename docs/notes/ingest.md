# Ingest: CSV, JSON, SQLite, Excel, folders, drops, jobs

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
- Excel ingest (`ingest_xlsx_sheet`/`preview_xlsx_sheets`, xlsxread.py) is
  the sheet-shaped sibling of the SQLite path — one workbook, N picked
  sheets, one background job, the same `{tables: [{table, name?}]}` option
  shape and the same frontend picker (`openUnitPicker` serves both). It
  reads through **openpyxl's read-only mode** (already a runtime dependency
  — the XLSX *export* writes through it), not a hand-rolled parser; what
  xlsxread.py owns is the typed-cell→TEXT conversion. Three traps worth
  knowing: date-styled cells convert to ISO text *unconditionally* (no
  per-column opt-in like SQLite's WebKit chips — a day-serial means
  nothing in any downstream surface), the header is the first *non-empty*
  row (hand-made workbooks put titles above it) with the table sized to
  max(header, declared width) so extra data lands in `col_N` instead of
  being cut, and a formula cell in a workbook that was never opened in
  Excel imports as "" (data_only=True reads the cached result, and a
  file written by a tool has no cache). Rows shorter than the header are
  normal Excel storage (trailing empties aren't stored) and pad silently;
  only wider rows count as ragged. `.xlsm` imports (same container,
  macros never executed); legacy binary `.xls` is deliberately out of
  scope. Like SQLite files, workbooks are excluded from directory import
  — which sheets to pull is a per-file choice.
- Plaso ingest (`ingest_plaso`, plasoread.py) reads a `.plaso` storage
  file — a SQLite db of serialized attribute containers — as ONE flat
  timeline table, no plaso install involved. Both on-disk generations are
  handled by looking at the `event` table's columns (`_data` JSON blobs,
  zlib-compressed when the metadata says so, vs the acstore era's real
  schema columns), never by trusting `format_version`. Fixed columns
  (Datetime/Timestamp desc/Data type/Parser/Source file/Host/User) plus
  one `Attributes (JSON)` cell for everything else — event_data's
  attribute set varies per data_type, and the JSON-extraction derived
  columns are the tool for the long tail. Events stream out ORDER BY
  timestamp, so rid order is chronological and the unsorted view stays on
  the root_virtual fast path. Unlike SQLite files and workbooks there is
  no picker — one file is one table — so `.plaso` participates in
  directory import (kind "plaso") and drag-drop directly. A malformed
  event_data row degrades to an error note in its Attributes cell; cancel
  drops the partial source like every other ingest.
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
- **There is one import entry point** — the Case menu's "Import…" →
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
  Every queue item carries its transport explicitly — `{file: File}` for
  a browser pick/drop, `{path}` for one added from the server's own disk —
  and the import loop, the three configure previews and the SQLite table
  picker all branch on which field is set. A path item reads **in place**
  (`/api/ingest/jobs/path`, `/api/ingest/preview/path` — no upload leg, no
  tempfile spool, no 50 GB copied to produce a file already on the disk;
  the preview half matters most for JSON, whose upload preview round-trips
  the whole file). The visible door to the path transport is the import
  modal's **"Add from this machine…"** (`openFolderBrowser` in files
  mode → `browse_dir?files=true` → `queuePaths`), deliberately listed
  first: browser and server are the same machine here, so the path
  transport is the right default and the upload is the convenience.
  files=true is loopback-gated (a disk-wide name+size enumeration is a
  bigger gift to a LAN scanner than the folder listing ever was), lists
  dot entries on purpose (on a mounted *nix image, `.bash_history` IS the
  evidence), answers a typed FILE path with a `picked` stat instead of a
  400 (server-side os.path is what makes pasted Windows paths and
  files past the listing cap work), and caps both lists at
  `BROWSE_LIST_CAP`, returned in the response so the truncation notice
  can't lie. Plugin-format items route per transport: path →
  `/api/ingest/plugin/path`, browser file → `/api/ingest/plugin/upload`
  (jobs/upload knows csv/json/sqlite only) — and both are *synchronous*
  ingests with no job record, so the import loop counts them and calls
  `loadSources()` after, exactly like the directory-import loop; miss
  that and a successful plugin import looks like a silent no-op. The
  queue-item shape itself has one producer, `queueItem()` — per-kind
  defaults live there and nowhere else.
  Files over `UPLOAD_ADVISORY_BYTES` (1 GB) queued through the browser
  get a toast pointing at the path route — advisory, never a gate.

  **History, because this reversed twice and the next session shouldn't
  re-reverse it**: the first design was a visible server-disk picker;
  it was replaced on request by an *invisible* resolver
  (`/api/ingest/resolve_local`) that fingerprinted every browser-picked
  file (name+size+mtime+head/tail bytes) and searched candidate dirs for
  a same-content file to import in place — "one Import button, two
  transports, no user decision." That resolver is now deliberately
  **removed**, also on request: its hit rate depended on the file
  happening to sit in a guessable directory, and a transport that
  silently works only sometimes made the upload path untrustworthy ("did
  it copy or not?"). The lesson recorded here is the *pair*: implicit
  transport selection traded a click for uncertainty, and the click won.
  If a resolver-like idea comes back, it must be visible in the queue row
  the way `by path ·` is now.
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

- **Keep-what-committed is really keep-what-PARSED.** When a bad line
  hard-stops the csv reader mid-file, the rows sitting in the pending
  batch parsed fine — the failure is the line *after* them — so the error
  path commits them before re-raising (best-effort: if the failure *was*
  the commit, what already landed still stands). Before this, an error at
  row N silently discarded up to BATCH−1 (19,999) good rows immediately
  before it. The raised error names the file line and the kept count
  ("Line 1,010,002: … — the 1,010,000 rows before it were kept"), and if
  *nothing* committed the source row is dropped entirely — a 0-row husk
  in the table list reads as a real empty import. Pinned by
  tests/test_ingest_broken.py.

- **UTF-16 is a routine input, not an edge case.** Windows PowerShell
  5.1's `Out-File`/`>` write UTF-16LE by default. Decoded as
  utf-8-sig-with-replacement, the header becomes NUL-riddled garbage and
  CREATE TABLE aborts with "the query contains a null character".
  `sniff_text_encoding` checks the two-byte BOM and opens UTF-16 files
  properly (csv, jsonl, and both server preview paths via
  `_decode_preview_bytes`); `sanitize_columns` also strips C0 control
  characters from header NAMES as a backstop — cells keep whatever bytes
  they had, names must be quotable. BOM-less UTF-16 is deliberately left
  alone: guessing without a BOM mislabels legitimate files.

- **An unbalanced quote swallows lines silently, and ragged can't see
  it.** A stray `"` folds every following line into one field until the
  next quote (or the 128KB field-limit error). The rows vanish from the
  grid, `ragged_rows` reads 0, and nothing errored. `suspect_quote_rows`
  counts rows where a single field holds ≥10 embedded newlines — the
  swallow's signature — and the jobs toast / CLI print a check-your-file
  warning. Legitimate multi-line payloads (EVTX XML) sit under the
  threshold. It's a heuristic on purpose: csv's parse is *correct* for
  properly-quoted multi-line fields, so this can only ever be a warning.

- **One broken JSONL line costs one line, not the file.** ingest_json's
  two-pass shape used to make any malformed line an all-or-nothing
  failure — a million good lines refused because line 1,000,001 was cut
  off mid-write — with json's own "line 1" (the position inside the one
  line) as the reported location. `_iter_json_records` now takes a `bad`
  collector: malformed lines are skipped and counted (`bad_records`,
  `first_bad_line`, surfaced in the jobs toast), and when a caller passes
  no collector the raise carries the real FILE line number. A truncated
  `.json` *document* is still a hard error — half of one JSON value is
  not a partial table.

# Ingest: CSV, raw text, JSON, SQLite, Excel, Plaso, IIS logs, key=value logs, archives, folders, drops, jobs

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
  rather than attempting to stringify binary data. A TEXT value that is
  not valid UTF-8 is decoded with U+FFFD (`_lenient_text_factory`) and
  counted (`undecodable_values`, in the jobs toast and the log): sqlite3's
  default factory raised "Could not decode to UTF-8" mid-read, and the
  batch it was in plus every row after it were gone. And a table that
  fails before its first batch commits is dropped rather than left with
  0 rows, which is indistinguishable from a real empty import. The
  preview reads each table on its own: one this SQLite can't read (a
  virtual table whose module it lacks — Thunderbird's message index)
  comes back with an `error` and `row_count: null`, shown but not
  pickable, instead of failing the preview of the whole file. Columns come from
  `PRAGMA table_xinfo`, not `table_info` (`_sqlite_table_columns`):
  `table_info` omits GENERATED columns, so those were silently missing
  from every import; a virtual table's hidden columns stay out.
  **`mode=ro` still writes beside a WAL database** — its `-shm`, and a
  `-wal` when the copy came without one — which put files in the
  analyst's evidence folder and littered the temp folder beside every
  uploaded spool. `_open_evidence_sqlite` opens a WAL-mode file with no
  `-wal` as `immutable=1`, appended to the `sqlite_ro_uri` URI below
  (nothing to replay, and no live writer can be holding it), and one WITH
  a `-wal` by copying the pair into a private temp directory first —
  those un-checkpointed rows are evidence, so
  `immutable` (which ignores the WAL) is not an option there. Plaso
  storage still opens plain `mode=ro`; plaso writes its files closed.
- **Every read-only open is a URI, and the URI comes from
  `paths.sqlite_ro_uri` — never `f"file:{path}?mode=ro"`.** `mode=ro`
  only exists in URI form, and in a URI `#` starts a fragment, `?` the
  query and `%` an escape, so a pasted-in path does not fail on those
  characters, it opens a different file: `History#1.db` was opened as
  `History`, read-WRITE (the `?mode=ro` went with the fragment), which
  created an empty file of that name in the evidence folder and listed
  no tables; `a%41.db` opened a real neighbour called `aA.db`. Same for
  the case itself (a case under `Case #5/` read every table as missing),
  the Plaso reader, the multi-case readers and server.py's peeks at
  case files. The helper percent-encodes the path and spells Windows
  drive and UNC paths the way SQLite reads them (`file:///C:/...`,
  `file:////server/share/...` — not `Path.as_uri()`, whose
  `file://server/` SQLite refuses). An ATTACH of a URI also needs the
  ATTACHing connection opened with `uri=True` (multicase's `:memory:`
  one included): whether SQLite reads ATTACH names as URIs otherwise is
  a compile option the Linux CI build has and a Windows Python's need
  not. `tests/test_path_characters.py`, which also fails on any new
  pasted-in `file:{` in the tree.
- Excel ingest (`ingest_xlsx_sheet`/`preview_xlsx_sheets`, xlsxread.py) is
  the sheet-shaped sibling of the SQLite path — one workbook, N picked
  sheets, one background job, the same `{tables: [{table, name?}]}` option
  shape and the same frontend list (`buildUnitSettings` serves both). It
  reads through **openpyxl's read-only mode** (already a runtime dependency
  — the XLSX *export* writes through it), not a hand-rolled parser; what
  xlsxread.py owns is the typed-cell→TEXT conversion. Three traps worth
  knowing: date-styled cells convert to ISO text *unconditionally* (no
  per-column opt-in like SQLite's WebKit chips — a day-serial means
  nothing in any downstream surface) — except a serial past year 9999,
  which keeps its number: openpyxl's parser would have replaced it with
  the string "#VALUE!", so xlsxread takes the conversion over from it
  (`_own_date_conversion`, reading cells rather than values_only), the header is the first *non-empty*
  row (hand-made workbooks put titles above it) with the table sized to
  max(header, declared width) so extra data lands in `col_N` instead of
  being cut, and a formula cell in a workbook that was never opened in
  Excel imports as "" (data_only=True reads the cached result, and a
  file written by a tool has no cache). And the sheet's declared
  `<dimension>` is reset before reading (`reset_dimensions()`, in the
  reader and the picker): read-only mode treats it as the edge of the
  data, and a stale A1:B10 over 3 columns and 101 rows imported 2
  columns and 9 rows with ragged 0. The declared size is still the
  picker's row estimate and a floor on the width — so the picker can
  under-count a sheet whose declaration is stale, but the import can't.
  A sheet that declares no size at all (openpyxl's write-only mode and
  many tools' writers leave it out) is counted off its `<row>` tags in
  the raw XML (`_count_rows`), not through `iter_rows`: openpyxl builds
  every cell to be counted and thrown away, 16 of the 20 s a 33 MB
  workbook took to list, holding the GIL so a 1 s CSV import beside it
  took 18. Note openpyxl's own `load_workbook` still parses the whole of
  such a sheet once looking for the `<dimension>` (its
  `parse_dimensions` waits for `</sheetData>`), and a workbook's shared
  strings are read in full on open — the rest of the listing's time,
  out of reach without bypassing openpyxl. Rows shorter than the header are
  normal Excel storage (trailing empties aren't stored) and pad silently;
  only wider rows count as ragged. `.xlsm` imports (same container,
  macros never executed); legacy binary `.xls` is deliberately out of
  scope. Like SQLite files, workbooks are excluded from directory import
  — which sheets to pull is a per-file choice.
- Archive expansion (`expand_archive`, winnow/archive.py) is how support
  bundles and UAC collections come in: zip/tar/tgz/gz expand RECURSIVELY
  (per-subsystem tgz inside the bundle, gzipped rotated logs) into a
  fresh `<name>-extracted` directory beside the archive (uploads: beside
  the case file), then the DIRECTORY-IMPORT flow takes over — it already
  owns the extension/pattern gates and the sidebar folder mirror, so
  nothing is ingested by the expansion itself. Safety lives in
  archive.py, not in callers: zip-slip refused, absolute member paths
  defanged (leading slash stripped), tar non-regular members skipped,
  byte budget enforced DURING decompression (headers are never trusted),
  file-count and nesting-depth caps → `truncated: true` with a valid
  partial tree. Inner archives are deleted after expanding — the tree
  holds only real files; the original archive is never touched.
  **Damage is per member, never the whole expansion.** What a damaged
  archive raises — EOFError (a cut-off .tgz), zlib.error (a corrupt
  inner .gz; it is not an OSError), BadZipFile/TarError, LZMAError,
  NotImplementedError (Deflate64, which Windows Explorer writes for big
  members) — is none of it an ArchiveError, so each used to escape as a
  500 with half the tree on disk and every later member unextracted.
  Now a member that cannot be read or written (name too long, a file
  where a directory must go) is skipped with a reason and the rest go
  on; a partial member is deleted (a truncated file would import as if
  whole); a tar whose stream ends early keeps every member before the
  cut; an inner archive that cannot even be opened stays in the tree
  unexpanded and is not retried; only an archive that yields NOTHING is
  an ArchiveError (400). Disk-full stops the expansion rather than
  failing every later member. **And nothing is overwritten**: two
  members with one name, or `messages` beside `messages.gz`, used to
  leave whichever came last — the attacker-activity copy gone without a
  trace. The later one takes `name (2).ext` (`_Budget.free_path`,
  numbered per path so 200,000 repeats of one name stay linear) and is
  listed under `renamed`. The import toast names the first skipped and
  renamed member and the server log keeps them all.
  Member names are made to fit the OS they land on
  (`_member_name_for_os`): a no-op on POSIX, but on Windows a `:`
  would write an alternate data stream of the file before it and
  `?*<>|"` raise, so those, trailing dots/spaces and reserved device
  names (`aux.log` included) are rewritten with `_` — two that come
  out the same are kept apart by `free_path` like any other clash.
  Tested with the Windows rules forced, since the suite runs on POSIX.
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
  event_data row degrades to an error note in its Attributes cell, and so
  does a malformed (or NULL) EVENT row — it lands as a row with the event
  table's own timestamp and an `_winnow_error` note, so the table always
  holds every event in the file (it used to be skipped uncounted, and a
  NULL `_data` failed the whole import with a TypeError); cancel
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
  **The column set is capped at `JSON_MAX_COLUMNS` (1,000).** SQLite
  refuses a table past 2,000 columns, and ONE record with more keys than
  that (attacker-chosen HTTP header names, an environment dump) failed
  the whole import. Past the cap, the first 999 keys (first-seen order)
  stay columns and every later key folds into one `Other fields (JSON)`
  cell per record — a JSON object of just that record's overflow keys,
  empty when it has none — so nothing is dropped and the structparse
  ops lift any of them back out. `_json_column_plan` builds the same
  plan for preview and ingest, so the preview shows the real table.
- **A JSON cell holds what the file said, all of it.** Every evidence
  read goes through `_json_loads`/`_json_load`, never a bare
  `json.loads`, for two reasons. *Numbers* are parsed to `_JsonNumber`,
  a str holding the literal: through float() 1.10 became 1.1, 1e5
  became 100000.0, 1E400 became inf, and an integer past Python's
  4,300-digit limit raised and failed the file. Inside a container that
  is stored as JSON text a number must still be written unquoted, and
  json.dumps cannot write a value raw, so `_json_text` is a writer of
  our own — byte-for-byte json.dumps(ensure_ascii=False) otherwise,
  pinned by a test, because every existing import's cells have that
  shape. It recurses (measured twice as fast as an explicit stack, and
  faster than the per-call json.dumps it replaced) for 200 levels, then
  hands off to an iterative copy, since json accepts documents thousands
  of levels deep. *Two values for one column* — a key named twice in one
  object (json.loads keeps the last), or a literal `"process.name"` key
  beside a nested `process.name` — used to keep whichever came last, so
  whoever wrote the record chose what the analyst saw. The first keeps
  the name, the next takes `_1`, `_2`… (sanitize_columns' suffix), and
  `key_collisions` counts the records it happened in (preview status
  line, jobs toast). A duplicated key inside a container stored as text
  is written twice, as the file had it.
- **IIS web server logs are recognised by name, not by extension**
  (`iisread.log_format`, `Store.ingest_iis`, kind `iis`). Their `.log` is
  every log's, and the name is the one thing IIS makes distinctive:
  `u_ex240501.log` (W3C extended, daily; `u_` = UTF-8), `ex24050113.log`
  (hourly), `ex2405.log` (monthly), `u_extend1.log` (by size), `nc…`
  (NCSA), `in…` (native). The dated stem must be a date, so `in1234.log`
  is not one. Every router asks the name FIRST — `_ingest_kind_for_path`,
  `importKindFor` (merge.js keeps a twin of the regex and the date
  check), `_guess_session_reader`, the folder scan — and asks it before
  plugin formats too (`pluginFormatFor`, `_assoc_ingest`): esxi_logs
  claims `.log`, and an IIS log routed to it came out as one ESXi
  `Message` per line. In the folder scan the `iis` token
  (`IIS_IMPORT_TYPE`) is the chip that admits them by name, on in the
  defaults; a file named like one that got in any other way (a `.log`
  chip, `*`) is still kind `iis`. A profile saved before the chip lacks
  the token, so its scan excludes them as "IIS logs not selected".
  **W3C columns are the file's `#Fields:` lines, never a fixed list.** A
  site logs the fields it is configured to, IIS writes a new header on
  every restart, and a changed setting changes the fields mid-file. So
  `ingest_iis` makes two passes, JSON's shape: `plan` collects the union
  of every directive's fields (case-insensitive, first-seen order) before
  the source exists — a cancel there has nothing to drop — and `rows`
  reads each line against the directive in effect where it sits. The
  second pass reads exactly as many lines as the first (`islice` on the
  first pass's count), because a live server's current log grows while
  it is imported in place, and a header the plan never saw would have
  nowhere to go; a last line that grew between the passes raises
  `FileChanged` instead of landing in the wrong column. Values the
  header in effect doesn't name (more values than fields, or a line
  before any header) go to an `Unnamed values` column that exists only
  when a file needs it — NCSA/native's equivalent is `Unparsed line`,
  the whole line — and count as `ragged_rows`. Values stay as logged:
  "-" for empty and "+" for a space are IIS's substitutions, which the
  file cannot tell from a literal "-" or "+", so decoding them would
  invent certainty. The one added column is the timestamp
  (`Timestamp (UTC)` for W3C's always-UTC date+time and NCSA's
  offset-bearing time, `Timestamp (local)` for native, which logs no
  offset) — declared, not sampled, and the raw date column of native
  format is pinned to text so it is not offered beside it. **NUL runs
  are gaps**: a server that loses power leaves zeros where its unflushed
  tail was and resumes writing after them, so a run can sit mid-line,
  glued to the restart's header block; `logical_lines` splits there,
  drops the zeros, and the import logs that it happened. There is no
  binary refusal for this kind, for the same reason. The three formats'
  header sets in defaults/headers.json give them Timeline summaries
  (W3C's by containment, so any site that logs the default fields).
  **The preview's column types are a `{column: type}` map, not
  ingest_csv's list.** The preview reads only the start of the log, and
  a `#Fields:` line further in can add columns ahead of the ones it saw
  (`Timestamp (UTC)` arrives at position 0 once any header has date and
  time), so a list by position would type the wrong columns. The map
  rides through the jobs routes and the ingest record (`column_types`,
  with the picked `log_format`), so a session re-import types the same.
  tests/test_iis_logs.py.
- **Key=value logs (FortiGate, the other Fortinet products) are
  recognised by their first line** (`kvread.looks_like`,
  `store.looks_like_kv_log`, `Store.ingest_kv`, kind `kv`). They have no
  name of their own — `fortigate.log`, `export.txt`, `syslog-10.0.0.1` —
  so the rule is content: the first non-blank line starts, after an
  optional syslog `<PRI>` and any words without `=` (a syslog header),
  `date=YYYY-MM-DD`, then `time=HH:MM:SS`, then another pair; spaces or
  FortiOS's `set format csv` commas between them. Requiring the third
  pair keeps a CSV header and an auth.log out. Content means opening the
  file, so only a file no reader claims by NAME is asked: one that would
  be raw text, or a `.txt` (which would be sniffed as delimited) —
  `_ingest_kind_for_path`, the folder scan (only among files the chips
  already let in: `.txt`, other text, a plugin's extension; it never opens
  a file just to see), `kvCandidate` + `looksLikeKvLog` in merge.js (a
  twin of the regex) over an upload's first `KV_PROBE_BYTES`, and
  `POST /api/ingest/recognise` for path items, so the queue shows the
  kind before the import rather than routing invisibly. Recognised, it
  wins over a plugin's claim on the extension, as an IIS name does
  (`_assoc_ingest`, queueFiles/queuePaths pass `fmt=null`): esxi_logs
  claims `.log`. **Columns are the union of every line's keys**, never
  the first line's: a traffic line and an event line carry different
  keys, and a reader that took the first line as a header put the event
  line's values under the traffic line's names. Two passes through
  `_ingest_planned_lines`, the body `ingest_iis` and `ingest_kv` share:
  `plan` collects keys case-insensitively in first-seen order (first
  spelling kept; a key repeated within a line takes `_1`), `rows` places
  each line's values by key. **Quoting** is the load-bearing part: FortiOS
  quotes string values that may hold spaces (`msg="Administrator admin
  logged in"`), so the split is pair-by-pair, never on spaces; `\"`
  inside a quoted value is an escaped quote only when that reading closes
  the value at a pair boundary, and an unterminated quote takes the rest
  of the line. Clean lines take a fast path (one fullmatch + findall,
  and `_Splitter` caching a compiled regex per line shape), anything
  else the careful scanner. Text before the first pair goes to `Line
  prefix`, words that aren't pairs (or a line with no pairs) to
  `Unparsed text`, both only when a file needs them and counted as
  `ragged_rows`; past `MAX_COLUMNS` keys the rest go to `Other fields
  (JSON)`. A file with no pair anywhere is refused, not imported as one
  Unparsed text column. **Timestamp**: `date` + `time` become the first
  column, `Timestamp (UTC)` only when every dated line also has a valid
  `tz` (FortiOS 6.2+ writes `tz="+0100"`; 6.0 writes none) and
  `Timestamp (local)` — the device clock, unconverted — when any line
  lacks one, because a column that is UTC on some rows and local on
  others can't be ordered. `tz` is pinned to text (it infers as a
  number). NUL runs, the live-file second pass (`FileChanged`) and the
  `{column: type}` map are IIS's, for the same reasons. Folder imports
  and session re-import (`_SESSION_READERS`, the ingest record's
  `column_types`) carry kind `kv`. tests/test_kv_logs.py,
  tests/ui/test_kv_queue.py.

- Ragged rows are padded/trimmed to the header width, never dropped, and
  counted — `ingest_csv`'s return dict has a `ragged_rows` count surfaced to
  the analyst (toast in the UI, printed in the CLI path). If that first row
  (header, or first data row when `has_header=False`) happens to be short,
  every other row gets trimmed to match it; there's no whole-file pre-scan to
  pick a "correct" width, so this count is the only signal something's off.
  "First row" means first NON-BLANK row (`_first_nonblank_row`, ingest and
  preview alike): a leading blank line used to be read as a header of zero
  columns, and the import died on `CREATE TABLE ... (rid ..., )`. Blank
  rows after it are still kept as padded rows.
- **Directory import** (`Store.scan_import_directory`, `POST
  /api/ingest/dir/scan`, the "Import a folder…" modal) is a preview-then-
  commit design, not a bulk-ingest endpoint — the scan step never touches
  `self.db` beyond one cheap `SELECT path FROM sources` (to flag
  `already_imported`), so it's safe to re-run live on every pattern edit.
  The frontend decides what actually gets imported and does it by looping
  over `/api/ingest/path` / the new `/api/ingest/json/path` sibling, one
  file at a time — same per-file, try/catch, toast-per-file loop
  `openImportModal`'s import (`importQueued`) already used, just fed by the
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
  profile list at all. The dialog is the derive dialog's frame (`sideRail`,
  `.side-*`; see ui.md): the profiles down the left, and a pick fills the
  settings in place rather than reopening the dialog, so the list keeps
  the focus and the arrow keys can walk it. Only Browse… still reopens it,
  because the folder browser borrows the modal — that round trip is what
  `state` carries.
- **Raw text is the catch-all importer, and it is a different reader.**
  `Store.ingest_text` (kind `text`): one row per physical line, the whole
  line in the one `Message` column (`TEXT_COLUMN`), no header, nothing
  split — a line iterator, not the csv module, so there is no field
  limit and a stray quote swallows nothing. Blank lines are kept
  as empty rows so `rid` is the line number — and for the same reason a
  line ends at `\n` and nowhere else (`_text_line`; the file is opened
  with `newline="\n"`). Python's default universal newlines, and
  `str.splitlines()` in the preview, also break at a lone `\r`, so a CR
  inside a logged value (an attacker-chosen username) forged a row that
  read like a real entry and shifted every rid after it. One `\r` before
  the `\n` is a CRLF's and comes off; any other is the line's own text.
  The preview splits the same way. `_ingest_kind_for_path`
  (server) and `importKindFor` (client) send any name no built-in claims
  here — `hostd.log`, `auth.log.1`, an extensionless dump — *after* plugin
  formats have had their say (`queueItem` / `_assoc_ingest` consult them
  first). `.txt` keeps the delimited path on purpose (KAPE emits
  delimited `.txt`); the CSV preview's **Lines** option forces any file
  into raw text, and that choice rides on the queue item as `kind`.
  The only refusal left is the binary check, and it stands on both sides.
  A file the browser hands over is probed before anything is uploaded —
  `looksBinaryFile` in importer.js, the same rule as `looks_binary`: a NUL
  in the first 8 KB, a UTF-16 BOM exempt — and one that trips it is dropped
  from the queue under a "Skipped N binary files (not text)" toast, so an
  .exe or an .evtx picked or dropped by mistake is never copied up at all.
  A path item has no bytes to probe — the server has them, the browser
  doesn't — so there, and for a binary that routes to some kind other than
  `text` (one named `.csv`, or one a plugin format claims), `looks_binary`
  at ingest makes it a job error instead, not a silent skip. Folder
  import stays extension-gated: the `*` chip ("other text files", off by
  default) admits everything non-binary as kind `text`, with binaries
  excluded under their own reason; profiles carry `*` in their extension
  list.
- **The folder scan never opens anything that isn't a regular file.**
  `os.walk` lists FIFOs, sockets and device nodes among the files, and a
  mounted *nix image is full of them. `open()` on a FIFO blocks until a
  writer appears, which on an image is never: with `*` on, the binary
  probe hung the scan for good, and a FIFO named `*.csv` was matched and
  hung the import instead. The scan checks `S_ISREG` on the stat it
  already makes, before any gate that reads the file, and excludes the
  rest as "not a regular file". It uses `os.stat`, not `lstat`, so a
  symlink to a real file still imports and one to a FIFO does not.
- **Include patterns narrow; they never widen.** The folder scan's
  extension gate runs first, and a file it drops is gone before
  `include_patterns` are consulted — so `*.log` in the include box cannot
  admit a `.log` the chips don't carry. The two ways past the gate are a
  chip (built-in extensions plus every extension a loaded plugin format
  claims) and a plugin's bare-name `filename_patterns`. That is why the
  esxi_logs plugin claims `.log` by extension as well as its fourteen
  stems by name: patterns alone left `vmkwarning.log`, `clomd.log` and
  the rest of a bundle behind, "unrecognized" with the plugin enabled,
  while the parser had an "other" type for them all along. A plugin that
  parses a whole family of files claims the extension; patterns are for
  the extensionless ones. Also: `extensions=[]` on the scan means no
  chips (nothing but patterns gets through), `None` means the defaults —
  `or` used to conflate them.
- **There is one import entry point** — the Case menu's "Import…" →
  `openImportModal`, whose queue now takes CSV/TSV, JSON/JSONL *and*
  SQLite files (`importKindFor` routes by extension; a sqlite item's
  pane lists its tables to tick, `buildUnitSettings`, storing
  `{tables: [{table, timestamp_columns}]}` on the queue item). The
  directory-import modal is reached from inside it ("Import a whole
  folder…") and both flows start background jobs rather than awaiting
  sync uploads — "Import N files" closes the modal immediately and a
  detached async chain uploads sequentially (one disk, one spool at a
  time) while the jobs panel tracks everything. Don't add a second
  menu entry per format again; the queue is the router.
  Every queue item carries its transport explicitly — `{file: File}` for
  a browser pick/drop, `{path}` for one added from the server's own disk —
  and the import loop and the pane's previews (`fetchPreview`) all branch
  on which field is set. A path item reads **in place**
  (`/api/ingest/jobs/path`, `/api/ingest/preview/path` — no upload leg, no
  tempfile spool, no 50 GB copied to produce a file already on the disk;
  the preview half matters most for JSON, whose upload preview round-trips
  the whole file). An upload's delimited, text or IIS preview sends only
  its first `PREVIEW_HEAD_BYTES` (1 MB, more than the server's 512 KB
  sample); a JSON, SQLite or Excel one needs the whole file, and past
  `AUTO_PREVIEW_UPLOAD_BYTES` (32 MB) the pane asks before sending it,
  since the preview loads when a file is chosen, not on a button. The visible door to the path transport is the import
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
  If a resolver-like idea comes back, it must be visible in the queue
  the way the pane's *read in place* is now.
- **Dragging a file from the OS onto the window** (`wireFileDrop`,
  `handleDroppedFiles`) is an alternative entry point into the *existing*
  import flows, not a new one — every dropped file (CSV/JSON *and*
  SQLite) queues into the same `S.importQueue`/`openImportModal` a picked
  file does (via `queueFiles`, factored out of the file `<input>`'s own
  `onchange`); a queued SQLite item still has to have its tables ticked
  in its pane before it can import — which
  table(s) to pull out is a real choice, so it can't just auto-import the
  way CSV/JSON does. There is nothing for the drop itself to
  recognize: `handleDroppedFiles` puts every dropped file through `queueFiles`
  and opens the modal, and what each one *is* gets decided later, per queue
  item, by `importKindFor` — sqlite, xlsx, plaso, archive, json, csv, and raw
  `text` for any name no built-in or plugin format claims. The extension lists
  (`RECOGNIZED_IMPORT_EXTENSIONS`/`SQLITE_IMPORT_EXTENSIONS`/`XLSX_`/
  `PLASO_`/`ARCHIVE_`) drive that routing and the directory-import chips,
  but nothing gates a drop; the import modal's own picker carries no
  `accept` either, deliberately, so the picker isn't the one entry point
  where a `.log.1` can't be chosen. Every listener in `wireFileDrop`
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
- **A drop Windows refuses never reaches the page.** Started from an
  administrator PowerShell, Winnow and the app window it opens run
  elevated, and Windows (UIPI) won't let a normal File Explorer drag into
  an elevated window: a no-entry cursor, and not one drag event for
  `wireFileDrop` to see, so no handler change can detect or fix it.
  `winnow/elevation.py` asks Windows instead (this process elevated, the
  desktop shell not), and the server warns at startup while
  `GET /api/elevation` drives `maybeWarnElevated`'s notice in the window.
  The shell half is what keeps it quiet with UAC off, where Explorer is
  elevated too and drops work. It's only "may": a browser that was
  already open runs the window with its own rights, not Winnow's.
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
  strand a worker on a closed connection — and, in the same locked step
  as that snapshot, sets `_closing`, which `start_ingest_job` reads:
  a job asked for after the snapshot is refused with `OpCancelled` (499
  on a route) instead of started, since nothing would ever cancel or
  join it. The caller that makes this real is
  `_cascade_dependent_derives`, which starts a child backfill from
  inside a derive worker close() is already draining; it treats the
  refusal like any other failure — the child is marked `partial` (its
  values predate the parent's change, and the header says "incomplete —
  re-derive to finish") and the reason is written to the log, rather
  than being left stuck `building`. The three paths that commit a
  definition *before* asking for the job — `add_derived_column`,
  `add_derived_columns`, `rederive_column` — roll that definition back
  instead and let the refusal out: a create drops the column (what its
  `drop_on_cancel` already promises), a re-derive restores the
  definition it had just overwritten. One sqlite job takes N tables
  from one spooled upload (`options["tables"]`) rather than re-uploading
  the file per table. Upload spools are deleted when the job ends —
  the old sync upload endpoints (kept for compat, same `finally` added)
  leaked the full file size in the OS tempdir on every upload, found as
  a stray 50 GB tempfile. `/api/ingest/plugin/upload` was the one that
  still leaked (no `finally` at all), and it recorded the spool's
  `tmpXXXX` path as the source's path. The archive upload had the
  opposite problem: it renamed its spool to `<tempdir>/<client
  filename>`, so two uploads of `bundle.tgz` at once shared one file,
  and a same-named file already in the tempdir was overwritten and then
  deleted. Every upload that imports now goes through `server._upload_spool`: a private
  `mkdtemp` per upload holding the file under its uploaded basename,
  removed in `finally` by `_remove_spool` (the job route hands both to
  the job — `delete_after` + `spool_dir` — which removes them when it
  ends). That is an unlink and an
  rmdir, not `rmtree`, because the routes are async and a tree walk is
  blocking work (test_event_loop_blocking). That basename is load-bearing for plugins,
  whose parsers read meaning from the file name (esxi_logs' `Log` column
  came out `other` for every uploaded file) — and for the built-in
  readers too: a SQLite table's and a workbook sheet's default name is
  `<file stem>.<table>`, built from the path, so while `jobs/upload`
  still spooled through `mkstemp` an uploaded History's `urls` came in
  as `tmp3z2vauq7.urls` beside the `History.urls` a path import makes.
  The path the source records is the uploaded name, never the spool's:
  `_ingest_via_plugin`'s `recorded_path` for plugin uploads, and
  `Store.record_upload_path` for the built-in ones (the job's
  `recorded_path`, and `_ingest_spooled` for the sync routes). It used to
  be the spool's `tmpXXXX` path for every kind, so a session saved
  against an uploaded table sent its re-import looking for a temp file
  that was gone; now it names the file, which the re-import refuses as
  not a local path and asks the analyst to open. `record_upload_path`
  updates by the spool's path rather than by id so it also reaches the
  rows a failed upload kept and the tables a multi-table job finished
  before one failed (tests/test_upload_names.py). Frontend: `uploadWithProgress` (XHR — fetch
  can't report upload progress) plus `pollJobs`/`renderJobsPanel`, the
  bottom-right panel that also surfaces `fts_building` — background index
  builds used to be invisible, and a server restart kills one silently
  (`_build_fts_worker` swallows everything; the next search retries).
  `boot()` restarts the poll so a reload mid-import picks the job back up.
  **A failed poll changes nothing.** It used to empty the jobs list, so
  nothing read as running and the poll's own reschedule stopped it for
  good — one 500 mid-import and the finished table never reached the tab
  strip until a reload. Now the panel keeps the last good answer and the
  poll retries with back-off (900ms doubling to 15s, logged once per run
  of failures); `startJobsPoll` cuts a back-off short, since whoever calls
  it just started work. Only the case list (no `#app`) lets it lapse.
  `tests/ui/test_jobs_panel.py`.

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

- **A source's row count is final before its columns are.** Each batch
  commits `sources.row_count` with its rows, so the count a source list
  reads grows while the file is read. The typed column list is the import's last write
  (`_finish_source_columns`), in a transaction of its own that waits for
  the writer lock again, and until it lands the row holds the
  `columns = '[]'` placeholder. So for a moment `list_sources` reports
  every row and no columns, and a table opened then has an empty header.
  The job reaching `done` is what says the table is complete. A UI test
  that queues an import from outside the page and polls the source list
  waits for `columns.length` as well as the count:
  test_value_picker_fills_the_box.py waited on the count alone, and
  opened `picks.csv` in that gap once the browser shards were dealt
  differently.

- **The file's extension names the delimiter, and it is read before the
  content is sniffed** (`EXTENSION_DELIMITERS`, `Store._sniff`). `.tsv`,
  `.tab`, `.csv` and `.psv` each say what they are; `.txt` and an
  extensionless dump say nothing and are sniffed as before.

  `csv.Sniffer` alone got real exports wrong. It scores each candidate by
  how consistently it appears per line, and when two of them tie it breaks
  the tie with **CPython's own `preferred` list, which starts with the
  comma**. A single comma on every line is enough to tie — and a column
  named `Last Modified (UTC, local)` puts one on the header as well as the
  body, so a 3-column `.tsv` imported as 2 columns with the header split
  down the middle of a column name. Analysts were setting Tab by hand in
  the preview on every import.

  The name is **evidence, not an instruction**: `_looks_delimited_by`
  requires the named character on every sampled line with a consistent
  count (90%, for the ragged rows this importer pads rather than refuses),
  so a `.csv` or `.psv` whose bytes don't agree with its name still falls
  through to the sniffer. An explicit `delimiter=` from the preview still
  beats both.

  **A name that says tab is held to a looser test**
  (`_splits_most_records`): the tab has to split at least half of the
  first 20 *records*, parsed the way the import reads them. "Every line,
  the same number of times" sent real `.tsv` files straight back to the
  comma — an exporter that leaves empty trailing fields off, a title line
  above the header, a quoted cell running over two lines — and the
  analyst saw *Comma-delimited (detected)* again. The asymmetry is the
  point: a comma inside a TSV's text is routine, a tab inside a CSV's is
  not. A CSV saved as `.tsv` has no tab to split on, so it still fails and
  is sniffed. For the same reason, on content alone (`.txt`, no
  extension) a tab passing `_looks_delimited_by` is taken before
  `csv.Sniffer` is asked — Excel's *Text (Tab delimited)* is a `.txt`.

  **The preview and the import read the same head** (`SNIFF_HEAD_CHARS`,
  64K characters). The preview used to sniff its first 8 KB, so with wide
  rows its 20-record sample was cut short where the import's was not.

  **The name has to reach both ends.** `ingest_csv` passes `path` rather
  than `name` — the display name is the caller's to override and often
  carries no extension — and both preview routes pass theirs
  (`file.filename`, `body.path`), because a preview that sniffed without
  the name would show a table the import then would not build.
  tests/test_tsv_delimiter.py.

- **UTF-16 is a routine input, not an edge case.** Windows PowerShell
  5.1's `Out-File`/`>` write UTF-16LE by default. Decoded as
  utf-8-sig-with-replacement, the header becomes NUL-riddled garbage and
  CREATE TABLE aborts with "the query contains a null character".
  `sniff_text_encoding` checks the two-byte BOM and opens UTF-16 files
  properly (csv, text, jsonl, and both server preview paths via
  `_decode_preview_bytes`); `sanitize_columns` also strips C0 control
  characters from header NAMES as a backstop — cells keep whatever bytes
  they had, names must be quotable. BOM-less UTF-16 is deliberately left
  alone: guessing without a BOM mislabels legitimate files.

- **So is Windows-1252, and a file that isn't UTF-8 says so.** Excel on
  Windows saves "CSV" in the ANSI code page (cp1252 in the west), and so
  do older tools; decoded as UTF-8-with-replacement, "José" imported as
  "Jos\ufffd" with no warning and the byte was gone. Two parts, both in
  store.py:
  - `sniff_text_encoding` probes the first `ENCODING_PROBE_BYTES` (1 MB).
    Valid UTF-8 is UTF-8; *invalid* UTF-8 that holds no well-formed
    multi-byte sequence at all is cp1252, read with Python's own codec.
    Invalid UTF-8 that *also* holds real multi-byte UTF-8 stays UTF-8 —
    it is a UTF-8 file with a few bad bytes, and reading it all as cp1252
    would turn every genuine "é" in it into "Ã©".
  - Every text evidence read opens with `errors=TEXT_DECODE_ERRORS`, a
    registered handler (`_cp1252_fallback`) that reads an undecodable
    byte as its cp1252 character instead of U+FFFD — so a stray byte past
    the probe, or in a mostly-UTF-8 file, keeps its meaning — and maps
    the five bytes cp1252 leaves undefined to the Latin-1 controls of the
    same value, so no byte is ever lost. It counts per THREAD
    (decoding happens on the thread iterating the file), which is how an
    import reports `cp1252_bytes`; take the snapshot after any
    `seek(0)` re-read or the head counts twice (ingest_csv does; JSON
    counts pass 2 only).
  An import reports `encoding` (utf-8 / utf-16 / cp1252) and
  `cp1252_bytes`, the jobs toast and the CLI say so, the previews decode
  through `decode_text_sample` (the path preview with the file's own
  sniff) and say so, and the source keeps it in `origin_meta`
  (`{encoding}` — origin_meta is NULL for a clean import) where the
  table's hover text reads it: how evidence bytes were interpreted
  belongs with the table they became, not only in a toast.

- **An unbalanced quote swallows lines silently, and ragged can't see
  it.** A stray `"` folds every following line into one field until the
  next quote (or the end of the file, or `CSV_FIELD_LIMIT` — see the next
  entry). The rows vanish from the grid, `ragged_rows` reads 0, and
  nothing errored. `suspect_quote_rows`
  counts rows that consumed ≥10 PHYSICAL file lines — `reader.line_num`
  minus the previous row's, one int compare per row, because a per-cell
  newline scan here benchmarked ingest 11% slower — which is the swallow's
  signature, and the jobs toast / CLI print a check-your-file warning. The
  count is per ROW across all of its fields rather than per field, so two
  fields holding five newlines each trip it and a legitimately quoted
  record spanning ten lines is counted too. Ordinary multi-line payloads
  (EVTX XML) sit under the threshold. It's a heuristic on purpose: csv's
  parse is *correct* for properly-quoted multi-line fields, so this can
  only ever be a warning.

- **The csv field limit is raised, not removed** (`CSV_FIELD_LIMIT`,
  set process-wide when store.py is imported). csv's own default is
  131072 characters, and one field past it stopped the import at that
  line with "field larger than field limit" — an EvtxECmd payload with a
  PowerShell script block in it, or a base64 blob in a column, is bigger
  than that. The cap (64 Mi characters) stays because it is also the
  only bound on a stray quote with no partner: csv folds the rest of the
  file into one field, built at four bytes a character, and on a 20 GB
  export unlimited means running the machine out of memory. Two
  consequences: a swallow under the cap no longer errors out, so
  `suspect_quote_rows` above is now the signal for it rather than a
  backstop; and the obvious `field_size_limit(sys.maxsize)` is wrong —
  the limit is a C long, 32 bits on Windows, and that call raises
  OverflowError there. tests/test_ingest_broken.py's poison-line tests
  lower the limit for their duration rather than write a 64 MB line.

- **One broken JSONL line costs one line, not the file.** ingest_json's
  two-pass shape used to make any malformed line an all-or-nothing
  failure — a million good lines refused because line 1,000,001 was cut
  off mid-write — with json's own "line 1" (the position inside the one
  line) as the reported location. `_iter_json_records` now takes a `bad`
  collector: malformed lines are skipped and counted (`bad_records`,
  `first_bad_line`, surfaced in the jobs toast), and when a caller passes
  no collector the raise carries the real FILE line number. A truncated
  `.json` *document* is still a hard error — half of one JSON value is
  not a partial table. A lone surrogate escape (`"\udc80"`, legal JSON,
  and how Plaso's serializer writes an NTFS name holding one) is not a
  broken line either: it decodes to a string with no UTF-8 form, whose
  batch insert raised "surrogates not allowed" and stopped the import —
  in a key, before the first row. `structparse.utf8_text` (store's
  `_utf8_text`, and a twin in plasoread, which imports nothing from the
  app) writes it back as the six-character escape the file held, so the
  value is recoverable and a JSON-text cell stays valid JSON. The same
  escape in a JSON *column* reaches the flatten picker as a real
  surrogate again (json.loads undoes it): discovery's samples and
  suggested names go through `utf8_text`, and a key holding one is
  spelled `["\uXXXX"]` in its path — `parse_json_path` reads that back
  as the same key — because the path is stored in a column definition
  and the picker's route could not encode its response at all.

- **Python 3.10 reads the same evidence 3.12 does only because store.py
  works around two stdlib differences.** 3.10's csv refuses any line
  holding a NUL ("line contains NUL"), so a file with one NUL failed at
  that line; `_csv_rows` escapes NULs past the parser there and
  unescapes the cells, exactly (the escape characters themselves round
  trip). And before 3.12 json's C scanner spends the interpreter's
  recursion limit on nesting, so a document ~1,000 deep raised
  RecursionError; `_json_loads` retries once under a raised limit
  (`_JSON_DEEP_RECURSION_LIMIT`, sized against the smallest thread
  stack). Past what any Python can nest it raises JSONDecodeError, so a
  JSONL import counts the line as bad instead of losing the file. 3.11+
  skips the csv escape and 3.12+ the retry; the backend suite runs on
  3.10 in CI so a third difference shows up there.

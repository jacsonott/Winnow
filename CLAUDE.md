# Winnow — working notes

Local web app for reading very large delimited files out of SQLite, with the
row-tagging workflow Timeline Explorer gets right. FastAPI + SQLite backend,
dependency-free vanilla JS frontend.

Built for DFIR analysts. Assume the target machine may be **airgapped**: no CDN,
no npm, no build step, no web fonts. If a change would add a network dependency
at runtime, it's the wrong change.

## Layout

```
server.py          FastAPI routes, CLI entrypoint. Thin — logic lives in store.
store.py           All SQLite: ingest, view materialisation, tags, sessions, export.
workspace.py       Cross-case JSON state (case registry, saved filters, default
                    tag template) — human-readable files in workspace/, outside
                    any single case.db so they survive switching cases.
static/index.html  App shell. No framework. #home and #app are siblings; only
                    one is ever visible.
static/app.js      Virtualized grid, filters, tagging, detail pane, SQL pane,
                    home screen.
bench/             Performance suite — stdlib-only timing harness, seeded
                    fixtures, baseline/`--vs-ref` comparison. Separate from
                    tests/ on purpose; see the Performance section below.
static/style.css   Token-driven theming: 4 styles (panel/phosphor/blueprint/
                    studio) x dark/light, selected via data-style/data-theme
                    on <html>, plus a user accent color. See the file's own
                    header comment for the token contract before adding
                    hardcoded colors.
```

Run: `python server.py` opens the home screen (recent cases, no case loaded
yet). `python server.py --case case.db --open sample.csv` still jumps
straight into a case, unchanged — that's the documented smoke-test flow below.

## Invariants — don't break these

1. **Source tables are never mutated.** Each import is `src_<id>` with an
   explicit `rid INTEGER PRIMARY KEY`. Tags, notes, layouts and saved views live
   in sidecar tables keyed by `(source_id, rid)`. The CSV on disk is never
   written to. This is what makes a session portable and re-import non-destructive.

2. **Never page with `LIMIT/OFFSET` over a filtered sort.** Filter/sort changes
   materialise once into a temp-attached `v.view_N(pos, rid)` table; the grid
   pages with `WHERE pos BETWEEN ? AND ?`. This is the difference between 1 ms
   and multi-second scrolling at depth. `Store.build_view` / `fetch_rows`.

   **Carve-out:** an unfiltered view with no sort at all is not a filtered
   sort — its "order" is just `rid`, which `INTEGER PRIMARY KEY` already
   gives for free (verified: `EXPLAIN QUERY PLAN ... ORDER BY rid ASC` is a
   bare `SCAN`, no temp b-tree). `build_view` skips materialisation
   entirely for this one case (`kind: "root_virtual"`, no `v.view_N`
   table), and `fetch_rows` pages the source table directly by
   `rid`/`LIMIT`/`OFFSET` instead — still cheap at depth precisely because
   there's no filter and no sort to redo, not because `LIMIT/OFFSET` is
   safe in general. Never on a merge (no single source table to page).
   Deliberately narrower than "no filter, sort is absent or index-served":
   any sort at all — even one an index can serve — still materialises
   (see `Store._build_virtual_root_view`'s docstring for why extending it
   to a sorted virtual view is a real materialise-on-tag_positions cost,
   not a free extension). `fetch_rows`/`tag_positions`/`find_position`/
   `tag_view`/`export_view_csv`/`group_summary`/`expand_group` all handle
   `kind == "root_virtual"` directly against the source table — `pos = rid
   - 1` throughout, exact (not a stub) because ingest assigns `rid`
   contiguously from 1 on every ingest path and no source table is ever
   mutated after ingest (invariant #1).

3. **Views live in the temp-attached `v` database** (`ATTACH DATABASE '' AS v`),
   which SQLite deletes on disconnect. Case files stay clean. Views die with the
   process — the frontend handles a 409 "view expired" by rebuilding.

4. **One shared connection guarded by `Store.lock`.** The temp attach only
   exists on that connection, so per-request connections would break views. The
   SQL pane is the exception: it opens its own read-only connection.
   `ingest_csv` takes this seriously at scale: it acquires the lock per
   `BATCH`-sized chunk (commit, release, re-acquire for the next chunk)
   rather than once for the whole file, so a multi-minute import on a huge
   CSV doesn't freeze every other request for its entire duration. Follow
   this pattern for any other operation that might run long — hold the lock
   for one unit of committed work, not for a whole loop over the file.

5. **Column names are user data.** Always run them through `store.q()` for
   quoting; never f-string a raw header into SQL. Headers get sanitised and
   deduped at ingest by `sanitize_columns`.

6. **Only the visible window is ever in the DOM.** `render()` in app.js builds
   rows for the scroll window plus overscan and positions them with a single
   `translateY`. Don't introduce per-row listeners; the grid uses event
   delegation on `#body`.

7. **`workspace/` is not a case file.** It holds UI/workflow bookkeeping only
   (case registry, saved filters, default-tag template) as human-readable
   JSON, never evidence data. Case files stay fully self-contained and
   portable on their own — deleting `workspace/` loses convenience state, not
   analysis.

## Things that bite

- Static assets (`/`, `/static/*`) get an explicit `Cache-Control: no-cache`
  from a middleware in server.py — FastAPI's `StaticFiles` only sends
  `ETag`/`Last-Modified`, and without an explicit `Cache-Control` the
  browser is free to serve a stale `style.css`/`app.js`/`index.html` from
  its own disk cache indefinitely, surviving even a normal reload (only a
  hard refresh forces a re-check). Bit us for real: a reported layout bug
  turned out to be a stale cached stylesheet, not the actual CSS on disk.
  `no-cache` still means a cheap `304` when nothing changed (browser
  revalidates via the existing ETag every load, doesn't re-download) — this
  isn't a `no-store`/always-refetch tradeoff, just "always double-check,"
  which is the right default given there's no build step or content-hashed
  filename to cache-bust with (and these files change often here). API
  routes are untouched — no reason to force revalidation on data responses.
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
- The **unified Timeline tab** (`build_timeline`/`fetch_timeline_rows` in
  store.py, a pinned tab like SQL) unions every *tagged* row across every
  real source in the case — open or closed, since it's "every finding in
  the case," not "every finding in an open tab" — into one chronological
  list: timestamp (via `TS_NORMALIZE`, same function the timeframe filter
  uses, so tables with different timestamp formats still sort correctly
  against each other), a "source type" label, and a body joined from a
  configurable subset of that source's columns with `" | "`. Per-source
  config (which column is the timestamp, which columns make the body, what
  to call the type) lives in `workspace.timeline_templates` — keyed by
  header set like `ColumnLayouts`/`HeaderNicknames`, cross-case on purpose
  (the "database of headers" that maps a header shape to a source type).
  store.py can't resolve these itself (can't import workspace.py — see
  `pop_legacy_presets`), so server.py's `_resolve_timeline_configs` does
  the header-set matching and hands `build_timeline` a plain `{source_id:
  {...}}` dict; a source with no matching template still works, falling
  back to its first datetime column, every column, and its own file name.
  Same materialize-into-`v.`-then-page-by-pos pattern as `build_view`
  (invariant #2) — only one timeline view is ever alive at a time, rebuilt
  (and the old one evicted) on every tag-filter change.
- The **timeframe filter** (`S.timeRange` in app.js, `time_range` on
  `ViewSpec`, compiled in `_compile_where` via the registered SQL function
  `TS_NORMALIZE`) is deliberately a separate piece of state from every
  other filter mechanism, and every place that resets "the filters" —
  `clearAllFilters()`, `applyPreset()`, opening a different source — is
  written to skip it on purpose (see the comments at each site). It's
  meant to stay pinned while everything else changes underneath it, and
  toggles on/off via its own keybind (`toggleTimeRange`) rather than
  needing its config modal reopened. `column: null` means "every datetime
  column on whichever table is open, OR'd together" — the MFT case this
  exists for: a timestomped Created date shouldn't hide a row whose
  Modified date is genuinely in range. `TS_NORMALIZE(x)` (a zero-padded
  `"YYYY-MM-DD HH:MM:SS"`, same ISO/US shapes as `DAY_BUCKET`/
  `parseTimestamp`) is what both the column values and the start/end
  bounds get compared through — a bare text/numeric comparison on the raw
  stored value sorts the US `M/D/YYYY` shape wrong.
- The substring-search index (`fts_<id>`) is FTS5 `tokenize='trigram'` over a
  **single `doc` column** — every column concatenated via the `src_<id>_doc`
  view (same `_blob_expr` the LIKE fallback scans, so indexed and fallback
  results are identical by construction) — with `detail=none, columnsize=0`,
  and it is queried with a bare `doc LIKE ?`, **never MATCH**. Each piece of
  that shape is measured, not stylistic (332K-row/285MB EvtxECmd source):
  `detail=none` drops per-occurrence position lists nothing here uses,
  cutting the index 892MB→143MB (–84%; case files were ~4.2x their source
  CSVs, now ~1.6x) at the cost of verifying candidates against the real
  text — so query time scales with *result count*: unchanged for the
  rare-IOC case, ~0.8s worst-case for a term matching ~all rows. Under
  `detail=none` a multi-trigram MATCH is a *phrase* query and errors
  outright ("phrase queries are not supported"); SQLite ≥3.45's trigram
  LIKE pushdown is the query form instead. Three traps around that
  pushdown, all verified: it's per-column — a table-level `fts LIKE ?`
  runs against the hidden table-name column (NULL outside MATCH) and
  silently returns **0 rows** (why the index is one `doc` column); it only
  fires for a *bare* `LIKE ?` — adding `ESCAPE` reverts to a full scan —
  so the pushdown pattern is deliberately unescaped and
  `_fts_like_pattern()` refuses any term containing `%`/`_` (would match
  as wildcards = silently-wrong superset), routing it to the escaped
  blob-LIKE fallback (backslash needs no escape in bare LIKE — Windows
  paths search fine); and pre-3.45 SQLite has no pushdown at all, so
  `_ensure_fts_building` no-ops there (LIKE still returns correct rows by
  scanning — an index would be pure wasted disk). A term under
  `TRIGRAM_MIN_LEN` (3 chars) or a source whose index isn't built yet
  falls back the same way, in `_compile_where`/`_advanced_fts_clause`/
  `search_all_sources`. Building isn't free — `build_fts` is
  `_ensure_fts_building`'d on a background daemon thread (batched the same
  BATCH-sized-chunk way `ingest_csv` commits, so it never holds
  `self.lock` for the whole build) rather than inline, both right after
  ingest and lazily on a source's first Contains/Advanced search — so
  browsing and searching both work immediately via the LIKE fallback, and
  get fast once the build catches up. A case file from an earlier build
  may have `has_fts=1` pointing at a stale-shape table — the original
  word-tokenized one, or the first-generation trigram (multi-column,
  `detail=full`; the doc-LIKE query form would be a SQL error against it);
  `Store.__init__` detects both from `sqlite_master`'s own CREATE VIRTUAL
  TABLE text (`'detail=none'` only appears in current-shape DDL), resets
  `has_fts=0` for the lazy re-upgrade, and drops the stale tables on a
  background janitor thread (`wait_for_fts_maintenance` in tests) — freed
  pages go to the freelist for reuse; the file only shrinks under a VACUUM
  nothing runs automatically.
- A per-column filter with a **sargable op** (`equals`/`in` — see `SARGABLE_OPS`)
  lazily gets a plain B-tree index on that `(source_id, column)`, same
  fire-and-forget background pattern as FTS (`_ensure_column_index_building`
  parallels `_ensure_fts_building`; the query being compiled still runs the
  current scan, the *next* application of that filter gets the indexed
  path). Only `equals`/`in` trigger it — `contains`/`starts` are LIKE
  patterns a plain index can't accelerate (trigram FTS is the answer for
  substring search instead), and numeric `>`/`<` go through `_numeric_expr`,
  a functional expression a plain index on the raw column wouldn't match.
  This matters a lot more for a **merge**: `build_view` compiles the filter
  once per member and `UNION ALL`s the results (see `_resolve_members`), so
  an unindexed sargable filter is a full scan repeated across every member,
  serially, on the one shared connection. Measured on an 11-member/42 GB
  merged case where the working set exceeds available RAM: an `EventId`-
  equals filter went from 6–8s *every* application (never stays cached —
  each scan touches more distinct pages than fit in the OS/SQLite cache) to
  ~50ms once indexed. Index name is `idx_<table>_<md5(column)[:12]>` — hashed
  rather than the raw column name, since a column name is arbitrary CSV-
  header text and this sidesteps identifier-safety entirely rather than
  leaning on `q()` for a human-readable name nobody needs to read.
- Column types are **inferred from a 500-row sample and stored as metadata only**;
  every value is stored TEXT. Don't "fix" this by typing the columns — mixed-type
  forensic CSVs then silently coerce, and evidence fidelity matters more than
  sort elegance. Numeric sorts/filters go through `_numeric_expr()`, not a bare
  `CAST(... AS REAL)` — SQLite's own CAST silently turns non-numeric text into
  `0.0`, which is indistinguishable from a genuine zero. `_numeric_expr` gates
  the cast behind the same regex the ingest-time sampler uses, so a value that
  doesn't actually look numeric (a stray "N/A", a blank from a ragged row) comes
  out `NULL` instead — it sorts to one edge and drops out of `>`/`<` filters
  instead of quietly blending in as real data. Any new numeric comparison/sort
  should go through this helper, not a raw CAST.
- Grouping (`group_summary`) buckets a `datetime` column by calendar day via
  a registered SQL function, `DAY_BUCKET(x)` (Python `_day_bucket`, same
  ISO/US shapes `DATE_RE` and app.js's `parseTimestamp` already recognize) —
  otherwise grouping by a full timestamp puts nearly every row in its own
  group of one. Everywhere a group's *value* gets compared back against raw
  rows (`expand_group`, `_virtual_group_where`, and therefore tag/export on
  a group) has to wrap the column in the same `DAY_BUCKET(...)` — that's
  `_eq_condition`'s `is_datetime` flag, threaded through `_path_where` too
  for nested-grouping's outer levels. `_eq_condition`/`_path_where` build
  the alias (`s.`) *inside* the returned fragment now rather than letting
  the caller string-prepend it — `s.DAY_BUCKET(...)` isn't valid SQL the
  way `s."col"` is, so a caller that goes back to prepending `s.` onto the
  result will get a syntax error the moment it hits a datetime column.
- There's no separate "preset" concept anymore — a preset is just a saved
  filter (`workspace.SavedFilters`, cross-case) whose `col_names` happens to
  match (exactly, or "similar" per the same Jaccard/subset heuristic the old
  case-scoped `filter_presets` table used) the table just opened. The banner
  (`checkPresets`/`matchingSavedFilters` in app.js) computes this entirely
  client-side against the already-loaded `S.savedFilters` — no request. A
  case file saved before this change may still have rows in the old
  `filter_presets` SQLite table; `Store.pop_legacy_presets()` reads and
  clears it once on open, and server.py folds whatever it finds into
  `WS.filters`. Nothing writes to `filter_presets` anymore — it stays in the
  schema purely as a one-way migration source for old case files.
- A header-set **nickname** (`workspace.HeaderNicknames`, `header_nicknames.json`)
  is a separate tiny store from `SavedFilters`, not a field on it — several
  saved filters commonly share one header set (e.g. five different EVTX
  filters), and should all pick up the same nickname rather than needing it
  set per-filter. Keyed the same way as `ColumnLayouts` (sorted, lowercased
  column names) — saving again for the same set overwrites in place.
- Ragged rows are padded/trimmed to the header width, never dropped, and
  counted — `ingest_csv`'s return dict has a `ragged_rows` count surfaced to
  the analyst (toast in the UI, printed in the CLI path). If that first row
  (header, or first data row when `has_header=False`) happens to be short,
  every other row gets trimmed to match it; there's no whole-file pre-scan to
  pick a "correct" width, so this count is the only signal something's off.
- `_quick_hash` is size + first/last 1 MB, not a full digest. It identifies a
  file for session matching; it is **not** an evidence integrity hash and must
  not be presented as one.
- Session import remaps tag IDs **by name**, creating missing tags. Two analysts
  with their own "Lateral movement" tag merge rather than duplicate.
- **Row selection in app.js is a flag plus a Set, never a list of every
  selected position** — `S.selectAll` off means `S.selection` holds the
  selected view *positions*; on means it holds the *exclusions* (everything
  else in the view is selected). Nothing outside the `sel*` helper block at
  the top of app.js touches either field directly. Positions, not rids, so
  it's still cleared on every view rebuild (positions no longer mean the
  same rows). The inversion isn't just to avoid allocating a 1.2M-entry Set
  for "everything": it's what lets `applyTag` *recognise* a whole-view
  selection and hand it to `/api/row_tags/view` as one server-side set
  operation. That matters because the page cache only ever holds the pages
  you've scrolled through — the old `positions.map(rowAt).filter(Boolean)`
  silently dropped every selected row that wasn't cached, so "select all
  1.2M rows, press a tag hotkey" tagged a few hundred of them and reported
  that smaller number in a toast. **A partial tag must never be silent** —
  the analyst's record of what they've triaged is the thing being
  corrupted. So there are exactly two paths: whole-view (bulk endpoint,
  with any unchecked rows passed as `exclude` — server-side so an excluded
  row that legitimately already had the tag keeps it), or an explicit
  subset (every page it spans is fetched *first*, and a failure is a toast,
  not a gap).
- `waitForPages` has **no deadline and bounded concurrency**
  (`PAGE_FETCH_CONCURRENCY`), and throws rather than returning short. It
  used to fire one `ensurePage` per missing page at once — ~2,400
  simultaneous requests at a single-connection SQLite backend for a
  select-all copy — and give up after 8s, after which its callers emitted
  `''` for every row still missing. A clipboard full of correct-looking
  rows with quiet blanks in it is worse than a copy that fails. Both copy
  paths now cap at 20,000 rows and refuse a hole rather than papering over
  one.
- The page cache is capped at `MAX_CACHED_PAGES` (~100 pages / 50k rows),
  evicted furthest-from-viewport first by `trimPageCache`. Deep-scrolling a
  1.2M-row × 27-column view used to accumulate the whole table in the JS
  heap — the DOM has always held only the visible window (invariant #6),
  and memory now follows the same rule. Two sets are never evicted: the
  pages currently being painted (render() would refetch them on the next
  frame, and ensurePage calls render() on arrival — that pair loops
  forever), and any `keep` set an in-flight bulk copy/tag still needs. Both
  can exceed the cap, in which case nothing is evicted; it's a cap on idle
  scrollback, not a hard limit that could break an operation mid-flight.
- There's no auth — this is a local, single-analyst tool. The one guard against
  a malicious page on another tab silently triggering side effects (e.g. via an
  unpreflighted `multipart/form-data` upload) is server.py's
  `require_client_header` middleware, which 403s any non-GET `/api/*` request
  missing an `X-Timeline-Lite-Client` header. `app.js`'s `api()` sets it on
  every non-GET call automatically — a raw `fetch()` that bypasses `api()`/
  `post()` won't have it and will get 403'd. GET stays exempt on purpose (the
  Export/session/filters download links are plain navigations, which can't set
  custom headers at all).
- `search_all_sources` takes `self.lock` **per source's count**, not once
  around the loop — invariant #4's "one unit of committed work" applied to a
  read sweep. It's N full LIKE scans back to back on a case whose indexes
  aren't built; wrapping the loop froze every other request (paging,
  tagging, view builds) for minutes on a 42 GB merge. Each count also stops
  at `SEARCH_ALL_COUNT_CAP` (`SELECT COUNT(*) FROM (… LIMIT cap+1)`) and
  reports `capped: True`, which the modal renders as "1,000+" — that pane
  only ranks which tables hit and roughly how hard, and an exact count is a
  scan of every matching row to produce a number nobody reads.
- `group_summary` skips the view join and aggregates member tables directly
  when `_grouping_covers_whole_source` proves the view holds every row
  (row counts alone are proof: a view's rows are distinct (source_id, rid)
  pairs drawn from its members, and a filter matching every row gives the
  same answer anyway). The joined shape **cannot** use a column index —
  verified with EXPLAIN QUERY PLAN, adding one leaves the plan
  byte-identical, because it's driven from the view table and reaches the
  source by rid — while the direct shape gets a covering-index scan with no
  temp b-tree, same as `column_values`. Measured on 120k rows: 56ms → 30ms.
  Both that check and `_ensure_column_index_building` take the caller's
  already-resolved `src`/`table_name` rather than looking them up:
  `get_source` runs a `COUNT(DISTINCT rid)` over `row_tags` (~12ms on a
  heavily tagged source), and the first cut of this was exactly break-even
  because the extra lookups ate the whole saving.
- **Not** followed by an FTS5 `optimize` after `build_fts`'s chunked
  inserts, despite that being the standard advice — measured, and it
  doesn't pay here. See `build_fts`'s docstring for the numbers: segments do
  consolidate, but query time was unchanged on every shape (rare term,
  matches-everything, miss) while the file grew ~7%, because FTS5 automerges
  during the build and `detail=none` makes candidate verification, not
  segment walking, the cost.
- `compact()` (`POST /api/case/compact`, the Tables modal's "Compact case
  file…") is the only thing that returns freed pages to the OS. Everything
  else — dropping a source, the startup stale-FTS janitor, dropping a column
  index — frees to SQLite's freelist, which is the right default but can
  park tens of GB. Explicit and confirmed because it rewrites the whole file
  (minutes of held lock on a big case) and needs a full second copy free on
  disk, checked up front. It forces `temp_store=FILE` for the duration:
  `_tune` sets MEMORY, and VACUUM's scratch copy of the entire database
  obeys that pragma. WAL is checkpoint-truncated on **both** sides — after,
  so the reclaimed bytes don't just move into a `-wal`; before, so the two
  sizes are comparable at all.
- The auto-created per-column filter indexes are surfaced per table in the
  Tables modal, with a drop. They're created behind the analyst's back and
  never expire. `list_column_indexes` works by hashing each *known* column
  and looking for that index name — the name deliberately carries only an
  md5 (`_column_index_name`), so there's no way to read a column back out of
  one.
- `api_view` maps only `ValueError`/`KeyError` to 400; everything else is a
  500. It used to catch bare `Exception`, so an internal defect surfaced as
  "Filter error: …" — blaming the analyst's filter for something they can't
  fix and hiding the traceback. `app.js`'s `api()` attaches `err.status` so
  callers can tell the two apart. Nothing that a user can actually type
  reaches the 500 path: `validate_where_fragment` converts SQL errors to
  `ValueError`, and `_regexp` swallows `re.error`.
- `run_sql` (the SQL pane) allows arbitrary SELECT/EXPLAIN on purpose, but
  blacklists `ATTACH`/`DETACH`/`PRAGMA`/`VACUUM` as defense-in-depth — none of
  those serve a read-only ad-hoc query pane. CSV export runs every cell through
  `_csv_safe()` (OWASP formula-injection prefixing for `=+-@`/tab/CR-leading
  values) since exports are explicitly meant to be opened in Excel and handed
  to other analysts; this only touches the exported copy, never the stored
  case-file value.
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
- **The sidebar** (`renderSidebar`, replacing the old `openTabJumpMenu`
  dropdown) is a *persistent* list of every table, open or closed — the
  horizontal tab strip (`.tabs`/`renderTabs`) is untouched and still the
  primary way to switch between what's currently open; the sidebar exists
  for the same reason the dropdown used to (reaching a table that isn't
  open, or is scrolled out of the strip's view), just without having to
  reopen a menu for every click — the case that actually forced this: a
  directory import can open 30+ tabs in one pass (every ingest auto-opens
  its tab), and a dropdown you reopen per click doesn't scale to that.
  `#app`'s CSS grid grew a column rather than a wrapper div — `#sidebar` is
  `grid-column: 1; grid-row: 1 / -1`, the four rows that used to be `#app`'s
  only direct children (`.bar`/`.toolbar`/`#presetBanner`/`.main-area`) all
  moved to `grid-column: 2` — so hiding it (`[hidden]`) collapses that
  column to zero width for free, nothing else occupies it. `renderSidebar`
  is called from inside `renderTabs()` itself (every one of `renderTabs`'s
  three callers — `loadSources`, `moveTab`, the tab-strip's own drag-drop
  handler — means `S.sources`/`S.tabOrder` just changed), not from a
  parallel set of call sites that could drift out of sync. Its active-row
  highlight isn't simply `s.id === S.sourceId`: `S.sourceId` is never
  cleared while the SQL or Timeline pinned tab is showing (there's no
  single "a source is open" flag to unset), so `sidebarRow` also requires
  `S.activeTab === 'grid'` — the same reason `showSqlTab`/`showTimelineTab`
  already force every `#sourceTabs .tab` to `aria-selected="false"` even
  though the underlying source hasn't changed. Collapse state persists in
  `localStorage` (`winnow.sidebar`) like `winnow.keymap`/`winnow.appearance` — a
  per-browser UI preference, not `workspace/` state. `dropdownMenu` lost
  its `actions`/`forceReopen` support in the same change — `openTabJumpMenu`
  was their only caller, and dead generic capability isn't worth carrying;
  its rows' `.menu-item`/`.menu-item-action` classes live on, reused as-is
  by the sidebar's own rows. Drag-to-reorder (`wireDragReorder`, factored
  out of what used to be `wireTabDrag` alone) is shared by the horizontal
  strip and the sidebar's vertical list — same native-HTML5-DnD technique,
  same `S.tabOrder`, just measured along a different axis (tab strip:
  pointer left/right of the dragged node's horizontal midpoint; sidebar:
  above/below its vertical midpoint). `draggedTabId` is one shared closure
  variable rather than one per axis on purpose: a drag started on a tab and
  dropped on a sidebar row (or vice versa) still reorders correctly, since
  both surfaces render from the same `openTabsSorted()`.
- **Dragging a file from the OS onto the window** (`wireFileDrop`,
  `handleDroppedFiles`) is an alternative entry point into the *existing*
  import flows, not a new one — a dropped CSV/JSON queues into the same
  `S.importQueue`/`openImportModal` a picked file does (via `queueFiles`,
  factored out of the file `<input>`'s own `onchange`), and a single
  dropped SQLite file opens `openSqliteImportModal` pre-loaded (it takes an
  optional `initialFile` now and extracts `loadFile` so a handed-in file
  previews identically to a picked one — which table(s) to pull out is a
  real choice, so it still can't just auto-import the way CSV/JSON does).
  The one genuinely new piece is recognizing what was dropped at all: a
  raw OS drop has no equivalent of a `<input accept>` filtering what's
  offered, so `handleDroppedFiles` filters by extension itself, against
  `RECOGNIZED_IMPORT_EXTENSIONS`/`SQLITE_IMPORT_EXTENSIONS` — the same
  lists `openImportModal`'s/`openSqliteImportModal`'s own `accept`
  attributes are now built from, so there's one true list per format
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
- **Editing a saved filter** goes through the real grid, not a
  self-contained dialog: the Saved filters modal's "Edit" applies that
  filter (`applyPreset`) and *then* opens `openFilterBuilder(f)` with the
  record, so the row count behind the modal is live feedback on the change
  being made. The only thing the `editing` argument adds is an `Update
  "<name>"` button; everything else, including "Save as new…", is the
  normal builder. That button deliberately sends **only `payload`** —
  never `col_names`. A filter's header set is its identity for `[` / `]`
  cycle order and the suggested-filter banner (see the saved-filters
  entries above), so re-binding it to whatever table happened to be open
  during an edit would silently move it out of the group it was saved
  for; "Save as new…" is the rebind path. `workspace.SavedFilters.update`
  replaced the old name-only `rename` with the same
  None-means-leave-alone partial-update convention `CaseRegistry.update`
  already used, so one method serves both a rename and a conditions
  re-save. Edit needs a table open (it applies the filter to preview it),
  hence the disabled button and its explanatory title when none is.
- **The row gutter and its header share one three-slot CSS grid**
  (`.gutter` / `.gutter-head` / `.gutter-filter`: checkbox | tag stripes +
  note mark | row number). The gutter used to be `justify-content:
  flex-end` over a variable child list, so the checkbox's x-position
  shifted from row to row depending on whether that row happened to be
  tagged or annotated, and the header's checkbox — left-aligned in a
  plain flex `.hcell` — sat above none of them. Only the middle slot
  flexes (`minmax(0, 1fr)`, so a long stripe run clips rather than
  pushing the number out of alignment); both edge slots are content-sized
  and therefore fixed down the column. **The three selectors must keep
  the same `grid-template-columns`, `gap` and horizontal padding** — that's
  the whole contract. `.rid` sets `grid-column: 3` explicitly rather than
  relying on sibling order, because grouped mode's gutter
  (`renderGroupDataRow`) has no checkbox or stripe slot and a rid placed
  by flow would land in column 1. `.gutter-head` also opts out of
  `.hcell`'s `cursor: pointer` and hover tint — it's the one header cell
  that doesn't sort. The select-all box's indeterminate state was already
  handled by `syncSelectAllCheckbox`.
- **The SQL pane has named sub-tabs** (`sql_tabs`, a per-case sidecar
  table; `list/create/update/delete/reorder_sql_tabs`, `/api/sql_tabs`,
  `renderSqlTabs` and friends in app.js). Stored in the **case file**, not
  `localStorage` like `winnow.sidebar` and not `workspace/` like a saved
  filter: a worked-out query is analysis *about this evidence* ("the join
  that pulls 4624s against the RDP source"), so it should travel with the
  case when it's handed to another analyst — and it's still only SELECTs
  the analyst typed, so invariant #1 holds (no source table is touched).
  The editor holds one tab's text at a time; every action that changes
  *which* tab that is `await`s `flushSqlTabSave()` first, so the debounced
  autosave can't lose an edit because you clicked away inside its window
  (and it captures the tab id it read the text for, so a late PUT can't
  land on the wrong tab). `savedSql` mirrors what the server holds so that
  flush is a no-op when nothing changed — it fires on every tab switch,
  not just after an edit. Result sets live in `S.sqlResults` keyed by tab
  id, **in memory only**: they're re-derivable by pressing Run, can be
  large, and are a snapshot of the data rather than the analysis.
  `runSql` captures `S.sqlTabId` up front and only paints if that tab is
  still showing, since you can switch tabs while a query is in flight.
  `wireDragReorder` grew optional `currentIds`/`onReorder` callbacks
  (defaulting to the source-tab behaviour) so the sub-tab strip reuses the
  one DnD implementation; its `drop` handler now *returns* when the
  dragged id isn't in the target surface's own id list, which is what
  stops a SQL sub-tab dropped on the source strip from being spliced into
  `S.tabOrder`.
- **"Search all tables" runs as a background job** rather than one long
  POST (`start_search_all_job` / `get_search_all_job` /
  `cancel_search_all_job`, `POST /api/search_all/start`, `GET
  /api/search_all/job`). The sweep was *already* careful not to hold
  `self.lock` across the whole loop, so the server was never really
  blocked — but the single request still took as long as the sweep, which
  meant one modal the analyst had to sit in front of, no results until the
  very end, and nothing to come back to if they closed it. Measured on
  8×60k rows with no FTS built (the full-LIKE-scan shape): `start`
  returns in **7ms** against a **2.8s** sweep, and an unrelated
  `/api/sources` during the sweep takes ~370ms vs ~3ms idle — i.e. it
  waits out at most *one* source's scan, not the whole sweep. That
  remaining latency is inherent to the one-shared-connection design
  (invariant #4), not something the job layer can remove.
  `search_all_sources` is now a thin collect-it-all wrapper over
  `_iter_search_all_sources`, a generator yielding `(scanned, total,
  hit_or_None)` per source — **one implementation, two callers**, so the
  structural per-source-lock test in `test_search.py` still covers the
  path the UI actually uses. Every source yields, matching or not, so
  `scanned` tracks real progress rather than counting hits. Only one job
  is alive at a time: starting another marks the old one cancelled and
  replaces it, and polling a superseded id returns None → **404**, which
  the frontend treats as "stop polling" (distinct from "no matches" — a
  stale poller rendering an empty result set would be a lie). Cancellation
  is cooperative and only checked between sources, so cancelling during
  one huge table's count still waits that count out. The job record has
  **its own lock**, not `self.lock` — making the worker's between-sources
  progress updates wait on the connection lock would reintroduce exactly
  the contention the per-source scoping exists to avoid. On the frontend,
  all of the pane's state lives in `S.searchAll` (terms, mode, job id,
  hits) instead of the modal's closure, which is what makes closing it
  mid-sweep safe; `searchAllRepaint` is a hook the poller calls that
  no-ops once the pane's nodes are detached (and `modal()` clears it, so
  opening any other modal supersedes it). Switching builder mode no longer
  auto-runs a search — with a real job that would abandon a sweep in
  progress just because you glanced at the other tab.

## Backlog, roughly in order

1. **DuckDB ingest path.** Current import is single-threaded Python `csv` at
   ~150k rows/s. `read_csv_auto` into DuckDB then copy to SQLite should be 5–10×
   faster with better type sniffing. Keep SQLite as the store — the sidecar and
   session model depend on it.
2. **Multiple live views per source** (filter tabs). `Store._views` currently
   evicts any other view for the same source on rebuild.
3. **Merged multi-source timeline** — one view across several `src_` tables with
   a normalised timestamp column. The big one for real triage.
4. **Drag-to-reorder columns.** `S.order` is already persisted in the layout;
   only the drag handler is missing.
5. **Import progress streaming.** `ingest_csv` takes a `progress` callback that
   nothing currently consumes; wire it to SSE or a websocket. `ingest_csv` now
   commits (and calls `progress`, when something consumes it) per `BATCH`-sized
   chunk rather than once for the whole file, which is exactly the boundary an
   SSE progress tick would want — the remaining work is wiring, not restructuring
   ingest itself.
6. **Saved views UI.** Endpoints (`/api/saved_views`) exist and work; nothing in
   the frontend calls them yet.
7. `.tle_sess` import, so existing Timeline Explorer sessions carry over.

## Testing

`tests/` is a `pytest` suite against the backend (`store.py`, `server.py`,
`workspace.py`) — deliberately backend-only, since `static/app.js` has no
build step or module exports to test against. Run it with:

```
pip install -r requirements-dev.txt
pytest
```

`requirements-dev.txt` (pytest, httpx2 for FastAPI's `TestClient`) is
separate from `requirements.txt` on purpose — it's dev-time-only and must
never become a runtime dependency the airgapped target machine needs.
Every test gets its own `tmp_path`-backed case file and a monkeypatched
`workspace.WORKSPACE_DIR` (see `tests/conftest.py`'s autouse
`isolate_workspace` fixture) — nothing in the suite touches the repo's real
`case.db`, `sample.csv`, or `workspace/`.

Coverage is organized one file per concern (`test_ingest.py`,
`test_views.py`, `test_search.py`, `test_grouping.py`, ...) and is weighted
toward the invariants and "things that bite" documented above — numeric
NULL-not-zero, ragged-row padding, the contains-mode substring fix, nested
grouping's `path` threading through the virtual small-group fast path,
session import's tag-remap-by-name, CSV formula-injection prefixing not
touching the stored value, and so on. `test_api_routes.py` covers the HTTP
layer itself (the CSRF header gate, request parsing, 400-vs-500) rather
than re-testing logic the `Store`-level tests already cover directly.
`test_maintenance.py` covers the things that only exist because the case
file otherwise only ever grows: the auto-created column indexes
(listing/dropping) and `compact()`. `test_sql_tabs.py` covers the SQL
pane's sub-tabs, including that they actually survive closing and
reopening the case file (the whole reason they live there rather than in
`localStorage`) and that `reorder` keeps tabs it wasn't given.

There's no browser-side test runner, but `static/app.js` can at least be
checked for syntax errors without one — the repo has `esprima` (the Python
port) available. It's ES2017, so the three newer syntaxes this codebase
uses (`??`, `?.`, bare `catch {`) have to be rewritten to older equivalents
before parsing; that's enough to catch an unbalanced brace or a broken
arrow body, which is the realistic failure mode when editing a 6k-line
file.

A note on the lock-scope test in `test_search.py`: it asserts *structurally*
(a `_CountingLock` counting how many times the lock goes fully unheld)
rather than by racing a competing thread, because a racing thread also
passes against the broken implementation — it can grab the lock in the
window before the sweep ever takes it. If you write another concurrency
test here, check it actually fails against the shape it's meant to catch.

For manual/perf testing outside the suite, generate fixtures rather than
committing large CSVs:

```python
# 1.2M rows ≈ 169 MB; imports in ~8s, trigram FTS build ~30s (backgrounded —
# ingest itself still returns in ~8s; see "Things that bite" above)
```

Smoke path that exercised everything during the first build: ingest → build a
filtered+sorted view → fetch a page 150k rows deep → FTS search → tag rows →
tag a whole view → export session → export CSV → run a SQL query.

## Performance

`bench/` is a separate suite from `tests/` — `pytest` stays a correctness
run measured in seconds (`pytest.ini`'s `testpaths` only covers `tests/`),
and a perf run needs minutes and a fixture two orders of magnitude bigger.
Full docs in `bench/README.md`; the short version:

```
python3 -m bench --vs-ref HEAD --only-changed   # did my change cost anything?
python3 -m bench --size quick                   # ~2 min sanity run
python3 -m bench --save-baseline                # record; later runs diff against it
```

Stdlib only, same rule `requirements-dev.txt` follows — no pytest-benchmark,
nothing new to install. Points worth knowing before editing it:

- **`--vs-ref` copies today's `bench/` into the worktree it checks out.**
  Only `store.py`/`server.py`/`workspace.py` come from the ref. Comparing
  HEAD's benchmarks against whatever benchmarks existed at the ref would be
  comparing two workloads and calling the difference a regression.
- **A result is only called a regression if it clears 7%, 0.5ms *and* 2x the
  larger run's stdev.** All three, deliberately conservative — two
  back-to-back runs of identical code flag nothing, which is the property
  that makes the output worth reading. Verified by injecting a `sleep` into
  `fetch_rows`: every paging benchmark flagged, a real +22% on a 176µs
  `get_source` correctly suppressed by the absolute floor.
- **Several benchmarks are only meaningful as a pair**, where the *gap* is
  the measurement: `paging/fetch_rows.head` vs `.deep` (invariant #2 —
  depth must not matter), `search/contains.*_fts` vs `.*_fallback` (what
  the trigram index buys), `search/contains.rare_fts` vs `.common_fts`
  (`detail=none` makes query time scale with result count, so those two can
  be traded against each other), `views/filter.equals_indexed` vs
  `.equals_unindexed`, `grouping/summary.covers_source` vs `.via_view_join`,
  `api/rows.page` vs `paging/fetch_rows.head` (serialisation cost alone).
  Don't delete one half.
- **`footprint/` measures bytes, not time**, and is compared the same way.
  It's in the suite because the index-shape decisions documented above are
  recorded as sizes ("892MB → 143MB", "~1.6x their source CSVs") — a change
  that buys query speed by tripling the index is a regression every timing
  in the suite would miss.
- **The fixture store has background FTS/column-index builds disabled**
  (`fixtures._quiet_background`) after setup. Fire-and-forget threads are
  right in the app and wrong here: the LIKE-fallback benchmarks would build
  the very index they exist to measure the absence of, and anything running
  alongside a build is timed against a busy core. `fixtures.ensure_column_index`
  is how a benchmark asks for one on purpose.
- Generated input files are cached in `bench/.cache` (pure data, independent
  of `store.py` — safe to reuse across code changes and across `--vs-ref`
  worktrees); the **case file is always rebuilt**, since building it is what
  the ingest benchmarks measure. Both `bench/.cache/` and `bench/baselines/`
  are gitignored — a baseline from another machine is worse than none.
- `tests/test_bench_harness.py` covers the harness itself (hook exclusion,
  the significance rules, fixture determinism, an end-to-end run on 400 rows)
  and runs in the normal `pytest` sweep, so the perf suite can't rot into
  reporting "no change" forever without anyone noticing.

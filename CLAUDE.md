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
timeparse.py       Timestamp-parsing operations for derived datetime columns —
                    and, since structparse.py registers into it, the registry
                    of derived-column operations generally. `family` splits
                    them ("datetime" vs "extract") so each picker offers only
                    the ops that answer its question. Stdlib only, imports
                    nothing from the app.
structparse.py     JSON/XML field-extraction operations, registered into
                    timeparse.OPERATIONS via register_op (its documented
                    seam) rather than a parallel registry — a derived column
                    needs the same things either way. Also owns the two path
                    syntaxes and the sample-and-enumerate discovery behind the
                    flatten picker. Stdlib only; imports timeparse and nothing
                    else from the app. store.py imports it for the
                    registration side effect.
workspace.py       Cross-case JSON state (case registry, saved filters, default
                    tag template) — human-readable files in workspace/, outside
                    any single case.db so they survive switching cases.
plugin_api.py      Plugin host: discovery/loading from plugins/, PluginAPI
                    (register_ingest_format / register_tab / register_api),
                    the registry server.py's /api/plugins, /plugin_assets/*
                    and /api/plugin/* routes read. The authoring contract
                    lives in its module docstring; docs/writing-plugins.md
                    is the long-form guide built on top of it (keep the two
                    in sync when the contract changes).
docs/              Long-form documentation. writing-plugins.md — the plugin
                    developer guide.
plugins/           Analyst-installed plugins (gitignored except its README).
                    Managed from Settings → Plugins: per-plugin on/off
                    toggles and a copy-from-disk installer, no restart —
                    dropping a folder/.py here by hand works too.
examples/plugins/  Committed example plugins, one per extension point — treat
                    them as the reference for writing new ones. mft_usn: raw
                    NTFS $MFT/$J parsing (ingest formats, stdlib-only).
                    lateral_movement: a pinned graph tab (register_tab +
                    register_api + a canvas ES module, offline).
                    claude_assistant: a Claude chat tab (external service from
                    a plugin route; needs network + `pip install anthropic` —
                    deliberately NOT airgap-compatible, which is why it's a
                    plugin). Install via Settings → Plugins or cp -r into
                    plugins/.
static/index.html  App shell. No framework. #home and #app are siblings; only
                    one is ever visible.
static/app.js      Virtualized grid, filters, tagging, detail pane, SQL pane,
                    home screen. The right-click surfaces (row menu, table
                    menu, detail-pane menu) and the header value picker all
                    ride one floating-menu implementation — see "Things that
                    bite". The detail pane's JSON/XML pretty-printer builds
                    from the parsed document rather than regexing its text,
                    because every node it emits carries the path that
                    addresses it; xmlSiblingSelectors is a deliberate twin of
                    structparse._sibling_selectors and has to stay in step
                    with it, since a path the UI offers must be one the
                    backend can resolve.
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
   table), and `fetch_rows` pages the source table directly by `rid`
   *range* (`WHERE rid >= ? AND rid < ?` — an O(log n) seek; contiguous
   rids make the window at `start` exactly rids start+1..start+count).
   Not `LIMIT/OFFSET`, which walks and discards `start` rows first even
   with no filter — measured 26ms vs 0.1ms for a window at the far end
   of 2M rows, a gap that grows linearly with depth. Never on a merge
   (no single source table to page).
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

3. **Views live in the `v` database — a named temp file, not the anonymous
   `ATTACH DATABASE ''` it used to be.** Created per Store in `/dev/shm`
   when available (else the platform tempdir), WAL-journalled,
   `synchronous=OFF` (views are re-derivable scratch, never evidence — see
   the comment in `Store.__init__` for why each of those settings is
   load-bearing), deleted in `close()`. Named so that the reader pool
   (invariant #4) can attach it too — that's the entire reason it changed.
   Case files stay clean. Views still die with the process — the frontend
   handles a 409 "view expired" by rebuilding.
   **Two things make "deleted in `close()`" actually true**, and neither is
   optional: server.py's `_lifespan` shutdown hook calls `Store.close()` on
   the way out (nothing did before — `main()` blocks in `uvicorn.run` and
   the only other caller is a case switch, so every Ctrl+C, SIGTERM and
   crash stranded one file per session, permanently, in `/dev/shm` or
   `C:\Windows\Temp`), and `sweep_orphan_views()` runs at startup to
   collect whatever a *hard* kill still left behind. The sweep can delete
   another process's file only because each Store holds an exclusive
   `flock` on its own for its whole life (`Store.__init__` keeps the
   mkstemp fd open for exactly this), so a live second Winnow is never
   swept — if you change how that file is created or named, keep the lock
   and the `winnow-views-` prefix.

4. **One writer connection guarded by `Store.lock`; reads go through the
   `Store._reader()` pool.** Everything that mutates — ingest, view
   materialisation, tags/notes, FTS/index builds, compact — runs on the one
   shared `self.db` under `self.lock`, exactly as before. But the pure-read
   paths (`fetch_rows` in all its kinds, `tag_positions`, `find_position`,
   `column_values`, `column_max_lengths`, `group_summary`'s aggregate,
   `fetch_timeline_rows`, the CSV/XLSX export streams, the search-all
   counts) run on pooled read-only connections that never take `self.lock`
   at all — WAL (already on for the case file, set on the views db at
   attach) is what makes that safe, and it's what keeps paging at ~1ms
   while a multi-second `build_view` or ingest holds the writer lock
   (measured: 638 page fetches at idle-baseline speed during a 545ms
   build, vs exactly 1 blocked call before). Two rules when touching this:
   **never use `_reader()` inside an open writer transaction** (a reader
   sees committed data only — code that needs to read back its own
   uncommitted writes stays on `self.db`, e.g. ingest's `get_source`), and
   a read that can race view eviction wraps its queries in
   `_dropped_view_is_expired()` so a dropped `v.view_N` surfaces as the
   KeyError → 409 contract, not a 500. The `_ensure_*` fire-and-forget
   helpers keep their thread registries under `_threads_lock` (not
   `self.lock`) for the same reason `_search_job_lock` exists — a "pure
   read" that brushed the writer lock for bookkeeping would stall behind a
   long build after all.
   `ingest_csv` still takes the writer lock per `BATCH`-sized chunk
   (commit, release, re-acquire) rather than once for the whole file, so a
   multi-minute import doesn't freeze the *write* paths (tagging, other
   imports) for its entire duration. Follow that pattern for any new
   long-running writer — hold the lock for one unit of committed work, not
   for a whole loop over the file.

5. **Column names are user data.** Always run them through `store.q()` for
   quoting; never f-string a raw header into SQL. Headers get sanitised and
   deduped at ingest by `sanitize_columns`.

6. **Only the visible window is ever in the DOM.** `render()` in app.js builds
   rows for the scroll window plus overscan and positions them with a single
   `translateY`. Don't introduce per-row listeners; the grid uses event
   delegation on `#body`.

7. **A tag write records the rows it actually changed, and undo replays
   that.** Every apply/remove — the selection paths, `tag_view`, both
   virtual-view paths — goes through `Store._apply_tag_change`, which
   materialises the *delta* (target rows minus the ones already in the
   wanted state) into a `v.undo_<n>` table and then applies the change
   **from that table**, so the rows recorded and the rows written are the
   same set by construction. Undo is `_write_tag_delta` over that same
   table with the direction flipped.
   The reason this isn't just "re-send the rids with `on` inverted": tags
   overlap. Tagging 200 rows when 40 already carried the tag moves 160; an
   undo that deleted the tag from all 200 would strip it off the 40, and
   nothing would ever tell the analyst. Silent corruption of triage state
   is the worst failure this tool has (see the two-paths-no-third comment
   on `applyTag` for the same reasoning applied one level up). If you add a
   new tag write path, route it through `_apply_tag_change` — don't
   INSERT/DELETE `row_tags` directly.
   History lives in the views database (invariant #3), so it's scratch that
   dies with the process, and it's bounded by both an entry count and a row
   budget (`UNDO_LIMIT` / `UNDO_ROW_BUDGET`) because "tag every row in a
   1.2M-row view" records 1.2M pairs. `delete_tag` drops the entries naming
   it — undoing one would reinsert rows pointing at a dead tag_id.
   `affected` stays "rows targeted" for backwards compatibility; `changed`
   is the delta count.

8. **`workspace/` is not a case file.** It holds UI/workflow bookkeeping only
   (case registry, saved filters, default-tag template) as human-readable
   JSON, never evidence data. Case files stay fully self-contained and
   portable on their own — deleting `workspace/` loses convenience state, not
   analysis.

## Things that bite

- **One case file, one Winnow.** SQLite's WAL keeps the *file* consistent
  across processes, but nothing in this app invalidates a second process's
  caches or its frontend's row counts, `compact()` holds the writer for
  minutes (past the 5s busy timeout every write in the other process
  fails), and on a network share WAL doesn't work at all — which is
  precisely the setup two analysts would collide on. So each Store drops a
  `<case>.winnow-lock` marker beside its case file (`_CaseLock`, modelled
  on the views file's flock) and `probe_case_lock` reports on it.
  **Two signals, either one enough to report a conflict**: the flock
  (exact, local filesystems) and a 30s heartbeat written into the marker
  (the only half that survives a filesystem where flock does nothing — do
  not "simplify" it away, the share is the case that matters). Free flock
  *and* a heartbeat older than `CASE_LOCK_STALE_AFTER_SEC` means a killed
  process; that reads as free and the next Store overwrites the marker in
  place. Rewritten **in place** on the held fd, never write-temp-and-rename
  — a rename moves the flock onto an unlinked inode and silently un-holds
  it, and a torn read is why the probe tolerates a corrupt record.
  All of it is advisory: `Store` never refuses to open, `server.py` decides
  (`/api/case/open` → 409 `case_in_use` → "Open anyway"; the CLI refuses
  and names the holder, `--force` overrides). Biased toward *reporting* a
  conflict, the opposite of `_views_file_is_orphaned`'s bias — there a
  wrong answer deletes a live process's file, here it costs one click.
- `/api/case/open` short-circuits when the requested path is **already the
  open case** — necessary, because the open-before-close ordering would
  otherwise make the process probe its own lock and refuse itself. Guarded
  by `not STORE.closed`, and that guard is load-bearing: `STORE` outlives
  `Store.close()` on both the case-switch path and the legacy-preset
  migration, so "same path" alone is not "already open" and short-cutting
  there serves rows off a closed connection.

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
- **The spacer that gives the grid its scroll height is capped**
  (`MAX_SPACER_PX`, 16M px) and above that cap `scrollTop` is no longer a
  row offset. A DOM element can't be arbitrarily tall — Blink clamps at
  33,554,365px (measured; 2^25 LayoutUnits), Gecko at ~17.9M — so
  `row_count * ROW_H` stops growing somewhere around 1.4M rows at 24px
  while the row count keeps going, and the tail of the view becomes
  unreachable: a 2,459,653-row `$J` table scrolled only to row ~1,398,090,
  hiding 43% of the evidence with nothing on screen to say so. So every
  conversion between `scrollTop` and a row goes through `vScroll()` (real →
  virtual) / `rScroll()` (virtual → real), and the rows block is positioned
  by `rowsPaintY()`, never by `first * ROW_H`. **Below the cap all three are
  exactly the arithmetic they replaced**, which is what makes them safe to
  apply everywhere — `render`, `renderGrouped`, `renderTimelineRows`,
  `visiblePageRange`, `scrollIntoView`, `recenterOnRow`, `applyDensity`, and
  `rebuildView` (whose kept scroll position is captured in *virtual* pixels,
  since the outgoing and incoming views can have different row counts and so
  different spacer scales). The non-obvious part is `rowsPaintY` subtracting
  the fractional part of the virtual offset: drop it and the top row snaps
  to the viewport edge, so the grid moves in whole-`ROW_H` steps instead of
  scrolling smoothly. Above the cap the cost is granularity, not reach —
  2.46M rows get ~6.5px of spacer per row instead of 24, so a wheel notch
  travels ~3.7x further; every row stays addressable (that needs 1px/row,
  which the cap doesn't reach until ~16M rows) and keyboard nav moves by row.
  Related, and the reason this was found at all: `#app`'s four grid children
  each pin their own `grid-row`. `#presetBanner` is `hidden` by default and a
  `display:none` item isn't placed in the grid at all, so under
  auto-placement `.main-area` slid into the 3rd (`auto`) track — harmless
  until the spacer passed the browser's ceiling, at which point that track's
  intrinsic size resolved to 0 and collapsed `.main-area`/`#grid`/`#body` to
  zero height. Correct row count, sticky header painted, not one data row.
- There's no auth — this is a local, single-analyst tool. The one guard against
  a malicious page on another tab silently triggering side effects (e.g. via an
  unpreflighted `multipart/form-data` upload) is server.py's
  `require_client_header` middleware, which 403s any non-GET `/api/*` request
  missing an `X-Timeline-Lite-Client` header. `app.js`'s `api()` sets it on
  every non-GET call automatically — a raw `fetch()` that bypasses `api()`/
  `post()` won't have it and will get 403'd. GET stays exempt on purpose (the
  Export/session/filters download links are plain navigations, which can't set
  custom headers at all).
- `search_all_sources` runs each source's count on a `_reader()`
  connection — the sweep never touches `self.lock` at all now (it used to
  take it per source's count, which still stalled a concurrent request for
  up to one source's scan; the history matters because it's N full LIKE
  scans back to back on a case whose indexes aren't built, minutes on a
  42 GB merge). The structural tests in `test_search.py` still assert the
  lock is never held across the sweep. Each count also stops at
  `SEARCH_ALL_COUNT_CAP` (`SELECT COUNT(*) FROM (… LIMIT cap+1)`) and
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
  parallel set of call sites that could drift out of sync. It lists page tabs
  too, in a third section (Pages) under Open/Closed — same rows, same
  drag/▲/▼ reorder, just against the other strip; see the two-strips entry
  below. Its active-row highlight isn't simply `s.id === S.sourceId`:
  `S.sourceId` is never cleared while a page tab is showing (there's no
  single "a source is open" flag to unset), so `sidebarRow` also requires
  `S.activeTab === 'grid'` — the same condition `syncTabSelection` applies
  to the strip itself, which is why both now live in that one function
  rather than as a block repeated in every `show*Tab`. Collapse state persists in
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
- **Two tab strips share the header bar**: `#sourceTabs` (tables, ordered
  by `S.tabOrder`) and `#pageTabs` (SQL, Timeline and plugin tabs, ordered
  by `S.pageTabPrefs.order`), split by the `#tabSplit` divider. Page tabs
  were three loose `.tab-sql` buttons sitting directly in `.bar` before —
  fixed order, no scrolling of their own, and free to squeeze the table
  strip to nothing once a couple of plugin tabs existed. Now:
  - A page tab is identified by a **string key** — `'sql'`, `'timeline'`,
    `'plugin:<id>'` — where a table tab is a numeric source id. That's
    what lets the one shared `wireDragReorder` (and its one shared
    `draggedTabId`) span both strips safely: a tab dragged from one strip
    to the other resolves to no index in the target's own `currentIds()`
    and the drop no-ops — the same guard the SQL sub-tabs already relied
    on. `S.activeTab` holds that key verbatim (or `'grid'`), which is what
    makes `syncTabSelection` one comparison per node rather than a branch
    per tab. It is now the only thing that writes `aria-selected` on
    either strip, and it ends with the sidebar re-render for the same
    reason `renderTabs()` does — every caller has just changed what's
    active.
  - `renderPageTabs` **moves** `#tabSql`/`#tabTimeline` into place rather
    than rebuilding them (a dozen places reach them by id) and builds the
    plugin ones. Each node is drag-wired exactly once
    (`dataset.dragWired`): the two reused ones would otherwise accumulate
    a listener set per render, and one drop would then apply the same
    reorder once per set.
  - Order and divider position persist in `localStorage`
    (`winnow.pagetabs`), unlike `S.tabOrder`, which is in-memory and
    resets per case. "SQL" means the same thing in every case, and a
    plugin tab belongs to this machine's `plugins/` rather than to any one
    case file — neither has a reason to jump back on a case switch.
  - **`Alt`+`1`–`0`** (`activateTabSlot`) addresses both strips as one key
    row: 1 is the last-selected table, 2…0 the page tabs *in strip order*,
    so the digits follow a reorder. Slot 1 calls `showGridTab()` rather
    than `openSource()` when the target is already `S.sourceId` —
    re-opening resets that table's filters/sort/search, which is not what
    "back to where I was" means. It's handled before `matchAction` and the
    tag hotkeys in the keydown listener because neither of those checks
    modifiers (`'0'` is `resetColumnWidths`, `1`–`9` are tag hotkeys), and
    it reads `e.code` rather than `e.key` since `Alt`+digit isn't a digit
    in `e.key` on every layout. `Shift`+digit — the obvious row — was
    already taken by apply-tag-to-view. Sitting above that pair also puts
    it outside their `S.activeTab === 'grid'` gate, which is correct and
    not incidental: switching tabs is the one thing that has to work
    *from* a non-grid tab, the same carve-out `TAB_AGNOSTIC_ACTIONS`
    makes for Settings/Tables/Search-all. It's above the `typing` guard
    for the same reason — the SQL pane focuses its editor on arrival, so a
    shortcut that stopped at that guard could get you into that tab and
    never back out — but below a check for an open dialog (`#modal` *or* a
    spawned `.confirm-overlay`; `_spawnDialog` builds its own, so one
    check doesn't cover the other).
  - The divider stores **the width the analyst dragged to** and applies
    that width *clamped* to what the bar can currently give it
    (`clampPageTabsWidth`); the clamped value is never written back, so a
    narrower window squeezes the strip without forgetting the setting.
    Below the width where both strips' minimums fit, the space is halved
    rather than honouring either — starving the table strip to hold a
    60px page strip is the worse failure, and both strips scroll. The
    clamp is deliberately a no-op while `#app` is `[hidden]`: every rect
    is 0 before a case is open, which would otherwise pin the strip at 0px
    for the whole session, since only `showApp()` and the window `resize`
    handler re-run it.

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
  returns in **7ms** against a **2.8s** sweep. (When this was first
  built, an unrelated `/api/sources` during the sweep still took ~370ms
  vs ~3ms idle — waiting out one source's scan on the then-shared
  connection; the reader pool in invariant #4 has since removed that too,
  the counts don't touch `self.lock` at all.)
  `search_all_sources` is now a thin collect-it-all wrapper over
  `_iter_search_all_sources`, a generator yielding `(scanned, total,
  hit_or_None)` per source — **one implementation, two callers**, so the
  structural lock tests in `test_search.py` still cover the
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
- **Long view work is cancellable via a client-generated `op_token`**
  (`Store.cancel_op` / `_interruptible`, `POST /api/cancel_op`;
  `build_view`, `build_timeline`, `group_summary`). cancel_op marks the
  token cancelled *first*, then `interrupt()`s the registered connection —
  so a cancel that lands before the op registers still takes effect at
  registration, and one that lands after unregistration is a no-op. The
  discipline making `interrupt()` safe on the shared writer: a writer op
  registers while *holding* `self.lock` and unregisters before releasing
  it, so the only interruptible statements under a token are that op's
  own; a cancel between statements is a missed cancel ("click again"),
  never a mis-aimed kill. Reader ops (group_summary) stack
  `_interruptible` innermost so the interrupt becomes `OpCancelled` before
  `_dropped_view_is_expired` can misread it, and `_reader()` closes — not
  repools — the interrupted connection. **Builds evict the previous view
  *after* the new INSERT, inside the same transaction** — a cancelled
  build rolls back to a world where the old view still exists, which is
  why the frontend can keep its rows on a 499 instead of 409-rebuilding.
  server.py maps `OpCancelled` → HTTP 499 in one exception handler; app.js
  arms a cancel chip (`armOpCancel`) only after ~1.2s in flight, so fast
  rebuilds never flash it.
- **The 2026-08 hot-path perf pass** (validated with `python3 -m bench
  --vs-ref` at both the 200k and 1.2M tiers — 0 slower, footprint
  unchanged), the shapes and their reasons:
  - `Store._source_lite()` (whose query logic now lives in the
    connection-parameterised `_source_lite_on`, shared with the reader
    pool) is what internal hot paths use instead of `get_source` —
    everything that only needs
    `table_name`/`columns`/`row_count`/`has_fts` to run a query:
    `fetch_rows` (was paying get_source's COUNT(DISTINCT rid) over
    row_tags — ~12ms on a heavily tagged source — at least twice per
    scroll page), `_resolve_members`, view builds, grouping, exports
    (which paid it per 5000-row chunk), FTS/index ensure-helpers.
    `get_source` stays for anything that actually surfaces
    tagged_row_count/note_count/is_open/fts_building to the UI.
  - View materialisation is an **ORDER BY-fed `INSERT ... SELECT`** with
    `pos` absent from the column list (rowids assign 1..N in insertion =
    sorted order), not `ROW_NUMBER() OVER` — measured ~35% faster for
    byte-identical content, and every such ORDER BY ends in a unique key
    (`rid ASC`/`root_pos`/`(source_id, rid)`) so pos is fully determined,
    not sorter-dependent. Row counts come from `cursor.rowcount` (works
    for INSERT..SELECT, 0 on empty) instead of a count(*) re-scan of the
    table just written. Same shape in `build_view`, `expand_group`'s
    materialised branch, and `build_timeline`. Net: view builds 21–84%
    faster across every filter/sort shape in the suite.
  - The hot, large-payload GET endpoints (`/api/rows`, timeline rows,
    tag_positions, group_summary, column_values) return
    `fastapi.JSONResponse` directly — returning a Response makes FastAPI
    skip `jsonable_encoder`'s per-value walk over every cell (pure
    overhead on payloads that are already plain str/int/None) and go
    straight to json.dumps. `/api/rows` dropped ~62–77%. Anything these
    endpoints return must stay JSON-native types.
  - `tag_positions` fast-exits via one indexed `row_tags` probe per
    member before the join — the join's only plan is a full scan of the
    view probing row_tags per row (~115ms per 2M view rows even with
    zero tags), it runs after every view build, and an untagged source
    is the common case.
  - `ingest_csv`'s per-row append loop is deliberate: a chunked
    `list(islice(reader, BATCH))` rewrite measured ~7% *slower* (islice's
    per-item indirection costs more than the tuple() copy it saves, and
    executemany binds tuples slightly faster than lists). The 1 MB read
    buffer on the file handle is the part that helps.
  - `_tune`: `PRAGMA threads=4` (parallelises the sorter — the one knob
    that helps a multi-million-row view-build sort), mmap_size at 1 GB
    (file-backed, shared with the OS page cache; costs address space,
    not RAM; SQLite clamps it to its compile-time max on old builds).
- **The 2026-08 reader-pool split** (invariants #3/#4 carry the rules;
  this entry carries the numbers and the traps that were checked):
  - Headline, via `bench/probe_concurrency.py` (a standalone before/after
    probe, separate from the `@benchmark` harness because it times one op
    *during* another): during a ~545ms `build_view`, `fetch_rows` on
    another source went from **1 completed call (455ms — blocked for the
    build's whole duration)** to **638 calls at 0.84ms mean**, which is
    the idle baseline. An earlier per-call-connection prototype measured
    1.61ms here; pooling (`READER_POOL_CAP` = app.js's
    `PAGE_FETCH_CONCURRENCY`) recovered the difference.
  - `bench --vs-ref main`: paging unchanged, `grouping/summary.*` and
    `column_values` 10–30% **faster** (their aggregate no longer queues
    behind the writer, and the ensure-index `sqlite_master` probe moved
    off it too). The tagging benchmarks flagged +9–14% SLOWER on three
    harness runs — chased and **not real**: an interleaved single-process
    A/B of `set_tags` (10k rows, 40 reps alternating main/current)
    measured 22.63ms vs 22.77ms (+0.6%), matching a ~0.2ms/commit cost of
    the second attached WAL db, isolated with a pure-sqlite3 probe. The
    harness's sequential ref-then-current runs pick up machine load drift
    as systematic bias on a shared box; interleave before believing a
    flag on a write path this change doesn't touch.
  - The views db moving out of `temp_store=MEMORY` (a *named* attach
    can't ride that pragma) was a real 20–100% regression on every
    views/paging/timeline benchmark until `/dev/shm` + `v.synchronous=OFF`
    recovered it — that's why those two settings in `Store.__init__` are
    not optional decoration. macOS/Windows fall back to the tempdir file
    (page-cache-backed, synchronous=OFF): fine for correctness,
    unmeasured for speed on those platforms.
  - `sqlite3.connect` is ~0.3–0.5ms with the attach + function
    registration — why `_reader()` pools instead of opening per call, and
    why the pool returns connections on the happy path but *closes* them
    on any exception (a connection whose last statement died mid-view-drop
    is cheaper to replace than to prove clean).

- **Plugins** (plugin_api.py, `plugins/`, Settings → Plugins) are
  Notepad++-style drop-in extensions, first loaded at server *import* (so
  `uvicorn server:app` gets them, not just `python server.py`;
  `--plugins-dir` / `$WINNOW_PLUGINS_DIR` add directories) and reloaded
  live by `_reload_plugins()` whenever Settings → Plugins toggles or
  installs one — `PluginRegistry.load` is written to be safely re-run on
  a live server (registry rebuilt wholesale; superseded modules linger in
  sys.modules under unique per-load names because Python can't truly
  unload code, but nothing references them again). Enabled/disabled state
  is `workspace/plugins.json` (`PluginPrefs`) — machine-level workflow
  state, stored as a *disabled* list so presence in plugins/ means on by
  default, keyed by filesystem name because that's the only identity that
  exists without importing (a disabled plugin is discovered for the
  listing but its code never runs — the point of the off switch).
  `POST /api/plugins/install` copies an uploaded .py or folder (from the
  panel's webkitdirectory picker) into `PLUGIN_DIRS[0]`, rejecting
  absolute/`..` paths so an upload can't write outside the plugins dir; a
  name collision is a 409 the frontend confirms into `overwrite=true`,
  and an install whose *load* then fails still keeps the files and
  reports the error (same standing as a hand-copied broken plugin).
  Three extension points on PluginAPI:
  `register_ingest_format` parses a file into columns + a row iterable
  that feeds `Store.ingest_rows` — the generic sibling of `ingest_csv`
  with every one of its conventions (all-TEXT via `sanitize_columns`,
  contiguous rid from 1, per-BATCH lock/commit, ragged pad-and-count,
  sampled types with an explicit `column_types` override, background FTS)
  — so invariants #1/#2 hold for plugin sources with no extra work, and a
  plugin source is a completely normal source afterward.
  `register_tab` adds a page tab (a true SQL/Timeline sibling — ordered
  among them by the analyst, not pinned after them; see the two-strips
  entry above): the entry is
  an ES module in the plugin folder, served via `/plugin_assets/<fs>/…`
  (enabled folder plugins only; resolved-path containment blocks
  traversal; same no-cache middleware as /static/) and dynamically
  import()ed by `showPluginTab` in app.js with a `?v=<gen>` cache-buster
  — `gen` is the registry load sequence, bumped on every reload, so a
  toggle-off/on picks up changed JS. One mount per tab, kept across tab
  switches, torn down on case switch (`resetPluginTabMounts`) and gen
  change; the module's default export gets `(container, winnow)` where
  `winnow` is `buildPluginTabContext`'s stable surface (api/post/el/
  modal helpers, `sql()` → run_sql's own RO connection, `schemaText()`,
  live state getters); optional onShow/onHide exports fire per switch.
  `register_api` registers backend routes dispatched at request time by
  the one catch-all `/api/plugin/{fs}/{route}` handler — deliberately
  not real FastAPI routes, so a Settings toggle's registry reload is
  instantly authoritative with no stale route objects. Handlers get a
  plain `PluginRequest` (method/route/query/body/store — None when no
  case is open) and return JSON-ables; ValueError → 400, same split as
  api_view; the CSRF middleware already covers non-GET. Plugin backends
  should read via `req.store.run_sql` (own RO connection — never holds
  invariant #4's lock). A plugin that
  fails to import/register is recorded with its error and skipped, never
  fatal; `GET /api/plugins` carries the reason to the Plugins modal.
  Format matching is extension OR bare-filename fnmatch — the latter
  because the files plugins exist for ("$MFT", "$J") *have* no extension;
  that's also why `scan_import_directory` grew `filename_patterns` (a
  second way past its extension gate, marked kind `"plugin"`; kinds the
  frontend can't resolve to a loaded format fall back to the CSV path,
  the pre-plugin behavior for analyst-added extensions) and why the
  Plugins modal's per-format picker sets no `accept` attribute. Routing
  precedence in app.js (`pluginFormatFor`): a built-in extension always
  wins over a plugin claiming the same one. Parse errors are 400s like
  every other ingest route. Security model is Notepad++'s: a plugin is
  arbitrary local Python with the app's privileges, installed by the
  analyst physically placing it — never fetched, so the airgap rule holds.
- **Derived columns** (`timeparse.py`, `derived_columns`/`drv_<id>` tables,
  `/api/derived/*`, the column header's right-click menu's "Add datetime column from
  this…") let an analyst add a *computed* datetime column from one that's
  already there — a Unix epoch, a BSD syslog line, a FILETIME, whatever
  the tool that produced the file happened to emit. Shape, and why each
  piece is where it is:
  - **Definitions in `derived_columns`, values materialised in a per-source
    `drv_<id>` sidecar** (`rid INTEGER PRIMARY KEY`, one TEXT column per
    derived column) — the row_tags/row_notes pattern, so invariant #1
    holds literally: `src_<id>` is never touched. Materialised rather than
    computed per query because the whole point is that these sort, filter,
    group and export like any other column, and all of that is server-side
    SQL over a column that either exists or doesn't.
  - **`sources.columns` in the DB stays base-only**; derived entries are
    merged into `src["columns"]` at read time (`_derived_col_entry`, in
    `get_source`/`list_sources`/`_source_lite_on`) with `derived: true`.
    That's deliberately fail-open: the ~15 read paths that iterate
    `src["columns"]` pick derived columns up for free, and only the
    handful that must see *the evidence file's own shape* filter them back
    out via `_base_cols` — the FTS doc view and its LIKE-fallback twin
    (identical by construction, so a derived value is **not** searchable —
    it's computed from text that already is), `column_signature` (adding a
    derived column must not change merge eligibility), `_iter_search_all_sources`,
    and the session file's column list. Merges get base columns only: each
    member has its own `drv_` table and its own definitions, and a UNION
    ALL across mismatched sets would need per-member NULL padding nothing
    does yet.
  - **`_from_clause(src)` is the one place the join is spelled** —
    `LEFT JOIN drv_<id> USING(rid)`, so an unqualified `rid` stays legal
    on both sides. `drv`'s rid is an INTEGER PRIMARY KEY, so the join
    matches at most one row and can't change the row count or order: that
    is what keeps `_fetch_virtual_root_rows`' `pos = rid - 1` exact
    (invariant #2), pinned by an EXPLAIN QUERY PLAN test in
    `test_derived.py`. A source with no derived columns compiles
    byte-identical SQL, which is why `bench --vs-ref` is flat.
  - **Two join shapes, not one.** `group_summary`/`expand_group`'s
    view-join branch already chains `JOIN … ON`, and an `ON` after a
    `USING` join binds to the wrong join — so those use `_derived_join`
    (an explicit aliased `LEFT JOIN … ON d.rid = s.rid`) and refer to
    derived columns as `d."col"`. `_col_ref(src, col, alias, derived_alias)`
    is what picks the right spelling; `_path_where` takes `src` for the
    same reason. This is the `s.DAY_BUCKET(...)` alias trap one level
    further in — a derived column can't take the source table's alias at
    all, because it isn't in the source table.
  - **Backfill is a `derive` ingest job** (`backfill_derived_column`,
    `start_ingest_job(kind='derive')` with no file) — per-BATCH lock
    discipline, progress, cancel, the jobs panel, and `close()`'s
    cancel-and-join, all for free. Rows are parsed **in rid order** because
    the syslog operation is *stateful*: BSD syslog carries no year, so the
    analyst supplies the first line's year and the parser rolls it forward
    every time the month decreases. That assumes syslog files are appended
    chronologically — an out-of-order line whose month is lower than its
    predecessor's is attributed to the next year, documented in the op's
    own description. Cancelling an *add* drops the column (mirror of
    cancel-drops-the-partial-source: a half-filled column is
    indistinguishable from a finished one in the grid); cancelling a
    *re-derive* keeps it and marks it `partial`, since that wasn't a
    request to delete the analyst's column.
  - **Canonical output is `YYYY-MM-DD HH:MM:SS[.ffffff]`**, sub-second only
    when the source has that resolution. That exact shape is why there are
    **no new regexes to hand-sync**: `_TS_ISO_RE`/`TS_NORMALIZE`/`DAY_BUCKET`
    and app.js's `parseTimestamp` already prefix-match it. Timezones:
    epoch-family values are UTC by definition, values carrying an explicit
    offset (ISO `Z`/`±HH:MM`, CLF, RFC 2822) are converted to UTC, and
    naive text is kept exactly as written unless the analyst sets the
    op's optional fixed `utc_offset` — never a TZ database, never DST
    inference. Unparseable input is NULL, not `''`: it drops out of
    comparisons and the timeframe filter instead of pretending to be data,
    and the count of non-empty inputs that produced NULL is surfaced per
    column ("Show N unparsed rows" builds a raw-filter fragment
    server-side, so the UI never quotes a column name into SQL itself).
  - **Display format layers**, most specific first: the column's own
    `tsFormat` in the layout → this case (`case_settings`, in the case
    file so it travels with the evidence) → system-wide
    (`workspace/app_settings.json`, machine-level workflow state) →
    `'iso'`. The old hard default was `'raw'`; analysts who want that set
    it as their system default. Note `tsFormatFor`'s menu now always
    stores the chosen key — it used to store `undefined` for `'raw'`,
    which was equivalent only while `'raw'` *was* the fallback.
  - Cost: two new tables ≈ **3 pages / 192KB fixed** per case file at the
    64KB page size `_tune` sets (measured), independent of row count.
    `derived_columns` deliberately has no index on `source_id` — it holds
    one row per derived column in the whole case, so a scan is one page
    either way and an index would cost more file than it could save.
- **The 2026-08 navigation batch** — five smaller features, and the traps
  each one carries:
  - **"Open filter in SQL pane"** (`Store.spec_sql`, `POST /api/view/sql`,
    keybind `Q`) renders the live spec through the *same*
    `_compile_where`/`_compile_order` the view build uses — never a
    parallel SQL generator that could drift — then inlines bound params as
    literals via `_inline_sql_params`, which walks string literals with
    SQLite's own ''-doubling rule so a `?` *inside* an analyst's raw
    filter fragment is not mistaken for a placeholder. `run_sql`'s
    connection now registers TS_NORMALIZE/DAY_BUCKET alongside REGEXP —
    a compiled timeframe filter contains them, and the pane erroring on
    its own generated SQL was the bug that surfaced this.
  - **Grouping travels with saved filters**: `currentFilterPayload()` adds
    `group_by`/`group_sort`/`group_sort_dir` *only when a grouping is
    active*, so filters saved without one keep byte-identical payloads —
    that's what keeps `activeSavedFilterRecord()`'s JSON-stringify
    matching honest, and it gives apply-time the same leniency `sort:
    p.sort || S.sort` has (a payload without the key leaves the current
    grouping alone; setGrouping() replaces wholesale, restoring
    `S.preGroupOrder` first so a formerly-grouped column doesn't leak out
    of the visible layout). `clearAllFilters()` now drops grouping too —
    stashed in `S.lastGroupBy` first, which is also what the `X`
    toggleGrouping keybind restores (the deliberate contrast with lowercase
    `x` dropGrouping, which just drops).
  - **Jump to timestamp** (`Store.find_nearest_timestamp`,
    `POST /api/view/find_ts`, keybinds `J`/`.`) measures closeness by
    `ABS(julianday(TS_NORMALIZE(col)) - julianday(target))` — string order
    can rank timestamps but can't measure *between* them — which makes it
    a scan of the view; that's the same cost shape as group_summary's
    aggregate and it runs on a pooled reader. Returns a pos each view kind
    computes its own way (root_virtual: rid-1; materialized: vv.pos-1;
    group_virtual: COUNT of group rows with a smaller rid, matching that
    path's rid-order paging). `S.jumpTs` deliberately survives
    `openSource()` — the workflow is "show me 13:22:01 in *each* table".
  - **Timeframe-from-tags** (`Store.tag_time_bounds`,
    `POST /api/tag_time_bounds`, the timeframe modal's "Fill range")
    returns TS_NORMALIZE'd bounds — the exact shape the timeframe filter
    compares through — over any-tag or a tag subset, honoring the modal's
    column choice with the same all-datetime-columns fallback the filter
    itself has, so the filled range always covers the rows it came from.
  - **Saved-filter reordering** predates this batch (▲/▼ +
    `SavedFilters.reorder`); the addition is drag-to-reorder on the modal
    rows via the one shared `wireDragReorder`, scoped by
    `currentIds: sameGroupFilterIds(...)` so a drag across header sets is
    a structural no-op rather than a rule someone has to remember.
- **The right-click surfaces** (row menu, column-header menu, table menu,
  header value picker) all hang off one floating-menu implementation in app.js —
  `showFloating`/`placeFloating` plus the single `openMenuEl`/`openMenuAnchor`
  pair, with `dropdownMenu` (anchored under a button), `contextMenu`
  (positioned at the pointer) and `anchoredPanel` (a card with real
  controls in it) as the three entry points. That's what makes "only one
  of these is open at a time, and Escape closes it" true across all of
  them rather than four near-copies of the same two listeners. The
  column-header menu is the one that *replaced* a visible control rather
  than adding a surface: its `▾` (`.hcell-fmt`) cost a slot of every
  header's width, on every table, forever, to be opened rarely — the same
  trade the tab strip's `▦` lost. Both handed their discovery burden to a
  title attribute. Two
  details are load-bearing: `onMenuKeydown` now `stopPropagation()`s its
  Escape (the document-level handler underneath clears the row selection,
  and dismissing a menu shouldn't throw away what was selected under it),
  and a menu's `items` may be a *function* — which is what `keepOpen`
  items re-run to repaint themselves, so toggling three tags from the row
  menu is three clicks instead of three right-clicks. `placeFloating`
  flips above the anchor rect when there's no room below; right-clicking
  a row near the bottom of the grid is the common case, not the edge one.
- **The row context menu is a section registry** (`ROW_MENU_SECTIONS`,
  `rowMenuItems`), not one function that spells the list out, because it's
  now the place per-row features are expected to land — a new action
  should be an entry, never surgery on a growing if-chain. Sections get
  `{pos, colName, colIndex, value}` and return items; an empty return is
  skipped, separator and all. The row is re-resolved (`rowAt(ctx.pos)`) on
  every repaint rather than captured, because a keepOpen tag item
  re-renders after tagging and the bulk tag path clears the page cache
  underneath it. Scope follows the selection: right-clicking *inside* one
  acts on the whole selection (tagging 200 checked rows shouldn't collapse
  to the row under the pointer), right-clicking outside it moves the
  cursor there first. Flat mode only, same as the click/mousedown handlers
  next to it — grouped mode has no row selection to act on. A tag's ✓
  reads the clicked row even when the target is a whole selection, which
  is deliberately the same sample-one-row rule `resolveTagDirection`
  already uses for the number hotkeys, so the menu can't promise a
  different outcome than pressing `2` would.
- **The header value picker** (`openValuePicker`, the `▾` in each filter
  cell) is an *author* for the filter the header box already understands —
  it writes `=v` or `a|b|c` into `S.filters` and nothing downstream knows
  it exists. Four things about it are decisions, not accidents:
  - **Which values it lists.** Unfiltered column: the current view, via
    `group_summary` — so the list reflects every *other* filter in play,
    which is Excel's behaviour and the one that answers "which processes
    survive this timeframe". Already filtered on this column: the whole
    table, via `column_values` — a view narrowed to three values can only
    offer those three back, and widening is the main reason to reopen the
    dropdown. Both are swappable from the panel, because a guess about
    scope that isn't visible is a lie. Building a *second* view with just
    this column's filter removed would be the truly Excel-exact answer and
    is not available: `Store._views` evicts any other view for the same
    source (backlog item 2).
  - **`bucket_datetime=False`.** `group_summary` day-buckets datetime
    columns, and a `2024-01-05` bucket matches no stored value, so an
    `=`/`in` filter built from one selects nothing. The flag turns the
    bucketing off for this one caller. It relaxes nothing about grouping's
    contract — the picker returns values, never groups anything gets
    expanded against, so there's no `_eq_condition` on the other side to
    keep in step — and raw values make the column index worth building
    again, hence the `whole_source and not is_datetime` gate admitting them.
  - **The size gate.** Distinct-values-with-counts is an aggregate pass
    with no index to lean on until the lazy per-column one exists, so the
    button is only rendered under `VALUE_FILTER_AUTO_MAX` (250k) rows by
    default. Overrides are three-layer, most specific first: the column's
    own pin (in the layout, so it travels with a saved default layout for
    the header set) → the table's `value_filters` mode (per-source, in the
    layout payload — it's a judgement about *this table's* size, which a
    header-set-keyed cross-case layout has no business carrying) → the row
    count. The row menu's "Filter by values…" opens it regardless: an
    explicit click is consent to pay for the scan in a way an
    always-present button isn't.
  - **What the filter box can't spell.** `=v` round-trips any value
    including one containing `|` (parseFilter matches the `=` prefix
    first), but the box trims, `a|b|c` is its only multi-value spelling,
    and `IN ()` drops empty strings server-side. So a selection with edge
    whitespace, a `|` in a multi-selection, or `(empty)` mixed with real
    values goes into the guided filter tree instead (`setPickerTreeNode`),
    with a toast saying so. That node is recognised *structurally* on the
    way back (an in/equals/empty cond on the column, or an OR of exactly
    those) rather than by a marker field, because `openFilterBuilder`
    round-trips the tree through SQL text and would drop any marker we
    invented.
- **Settings' sections are collapsed on open, every time**
  (`settingsSection`, one wrapper per `h4` the modal used to append
  straight into its body). Seven sections had grown to ~900px of scroll,
  so the setting you came for was rarely the one on screen. Two
  deliberate non-features: state isn't remembered between opens ("open
  where I left it" and "collapsed by default" are different promises, and
  the second is the one that was asked for), and opening one doesn't
  close the others. A section's own code is unchanged apart from what it
  appends into — which is also what keeps a section that fills itself
  later (`buildPluginsPanel`'s async listing) landing inside its own
  section rather than at the end of the modal.
- **`S.keymap` must hold its own key arrays, not `DEFAULT_KEYMAP`'s.**
  The settings UI's "+ key"/"✕" handlers splice and push those arrays in
  place, so the old shallow `{...DEFAULT_KEYMAP}` handed them the
  defaults' own arrays: on a profile with nothing stored yet, adding a
  binding edited `DEFAULT_KEYMAP` itself, and "Reset to defaults" then
  copied the polluted defaults back and looked like it did nothing.
  `defaultKeymap()` (a per-action `[...keys]`) is what `loadKeymap` and
  the reset button both go through now.
- **`.fcell` needs its `min-width: 0`, and it's not tidying.** Giving the
  filter cell `display: flex` (to seat the value picker's ▾ next to the
  input) also made its own automatic minimum size content-based — and a
  text input's intrinsic width is ~177px, so every filter cell silently
  floored at 177px while its header stayed at the column's real
  flex-basis. Measured: a 90px column had a 179px filter cell under it,
  and the two rows stopped lining up from the first narrow column
  onward. `.hcell` has never had the problem because the `overflow:
  hidden` it already carries suppresses the same automatic minimum.
  Anything else in this file that becomes a flex container while sitting
  in the `.head-row`/`.filter-row` flex line needs one or the other.
- **Autofit measures the header, it doesn't estimate it.** `widthForLen`
  used `max(dataChars, name.length) * 7 + 24`, which ignored everything
  the header cell carries besides its text — the sort arrow, the ▾
  options button, the derived `ƒ` mark, 8px of padding either side — and
  the header font is uppercase and letter-spaced, so it was never 7px per
  character either. Result: a fit-to-content pass could leave `EVEN…▾`
  sitting over a column of `1`s. `headerWidthFor` now reads the live DOM
  instead: the label's `scrollWidth` (its full text, even while clipped)
  plus `hcell.clientWidth - label.clientWidth` (padding, gaps and every
  non-label child; the grip is absolutely positioned, so it isn't in
  that difference). It's idempotent by construction — once the label
  isn't clipped, both terms stop changing — and returns 0 for a column
  with no header on screen, where callers fall back to the old estimate.
- **The autofit cap is a user setting** (`S.appearance.autofitMax`,
  Settings → Appearance, default `AUTOFIT_MAX_W_DEFAULT` = 900px, `0`
  meaning uncapped), not the old hardcoded 480. A cap still exists by
  default because the rows are `width: max-content`: one column of
  base64 command lines fits to ~3,600px uncapped (measured) and every
  horizontal scroll of every other column then goes through it. Two
  rules inside `widthForLen`: the header may exceed the cap (a column
  whose *name* is cut off can't be identified, while a truncated value
  can still be read in the detail pane) but only to 2x, so one absurd
  header can't defeat the cap either. Stored with the other per-browser
  look-and-feel prefs rather than in the layout — it's a statement about
  this screen, not about this table's columns.
- **The table menu replaced the tab strip's `▦` column-chooser button**
  (`TABLE_MENU_SECTIONS`/`openTableMenu`, right-click a tab or a sidebar
  row, or press `C`). Same registry reasoning as the row menu: it's where
  per-table features land, and the tab strip can't grow an icon per
  feature. `openTableMenu(sourceId)` opens that source first when it isn't
  the one on screen — not a convenience, a precondition, since every panel
  reads the live `S.layout`/`S.order`/`S.columns` rather than the record it
  was handed.
- **`clearAllFilters(seed)`** — Shift+F ("filter to this value and drop the
  rest") is that reset plus one filter, so it goes through the same
  function rather than a second implementation that would forget the
  carve-outs: the timeframe filter survives, grouping is stashed into
  `S.lastGroupBy` rather than lost. Note `$('btnReset').onclick` is now a
  wrapper — passing `clearAllFilters` directly would hand it the MouseEvent
  as `seed`.
- **Stored keymaps are migrated on load, not merged blindly.**
  `loadKeymap` used to be `{...DEFAULT_KEYMAP, ...stored}`, which means a
  returning analyst's localStorage outranks every later change to the
  defaults — including a *rename*, where the stored entry keeps swallowing
  its key while pointing at an action that no longer has a handler (
  `matchAction` scans the stored map, so the key resolves and nothing
  happens). So there's a `KEYMAP_MIGRATIONS` list with a version counter in
  `winnow.keymap.v`, and unknown actions are dropped on the way through.
  The v1 migration carries `openColumns` → `openTableMenu` and moves the
  `f`/`Shift+F` pair (focus-first-filter → filter-by-this-value, plus the
  new drop-the-others variant) *only* for analysts still on the old
  defaults — a binding someone chose themselves is never touched.
- The example mft_usn plugin's fixup handling encodes a real-world trap:
  extraction tools disagree about whether $MFT records arrive with NTFS's
  multi-sector fixups still stamped (KAPE/icat/RawCopy: yes; ntfscat:
  already un-applied — verified against a real mkntfs volume, where the
  strict all-stamped check silently produced 0 rows). `_apply_fixups`
  therefore distinguishes all-stamped (un-stamp), none-stamped (parse
  as-is), and mixed (genuinely torn → skip the record).

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
5. **Saved views UI.** Endpoints (`/api/saved_views`) exist and work; nothing in
   the frontend calls them yet.
6. `.tle_sess` import, so existing Timeline Explorer sessions carry over.

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
`test_maintenance.py` covers the things that only exist because everything
else only ever grows: the auto-created column indexes (listing/dropping),
`compact()`, and `sweep_orphan_views()` — whose load-bearing test is that a
*live* Store's views database survives a sweep, since that's the assumption
that makes deleting another process's file safe at all. `test_ingest_jobs.py` covers the
background ingest jobs (lifecycle, byte-progress, the queued-cancel path,
cancel-drops-the-partial-source — driven through `ingest_csv`'s own
`cancel` hook so the cancellation point is deterministic, not a race —
multi-table sqlite jobs, spool deletion, close-with-running-job).
`test_cancel_op.py` covers cancellable builds, using a
catastrophic-backtracking regex filter as a reliably-slow build to
interrupt; its key regression assert is that the *previous* view still
pages after a cancelled rebuild. `test_concurrency.py` pins the
reader-pool split (invariant #4) structurally: each test holds the writer
lock for the *entire duration* of a read and asserts the read completes —
a deterministic deadlock against a single-connection implementation, not a
race — plus the dropped-view → KeyError mapping, pool recycling/drain on
close, and read-your-committed-writes across the connection split.
`test_plugins.py` covers the plugin
system end to end — loader isolation (a broken plugin never takes the
rest down; a disabled one is discovered but never imported, proved with a
plugin whose module body raises), `ingest_rows`' conventions, the HTTP
routes (toggle persistence, install path-traversal rejection, the
409-then-overwrite flow), tabs/assets/API dispatch (register_tab entry
validation, asset containment, the gen cache-buster changing per reload,
405/404/400 splits, disabled = no assets + no routes + no tabs), the
mft_usn example parsed against synthetic NTFS fixtures built byte-by-byte
in the test file (both fixup states; no real evidence files in the repo),
lateral_movement's edge aggregation, and claude_assistant against a fake
`anthropic` module injected into sys.modules (asserts the request shape —
model, fallbacks, the schema cache breakpoint — with no network). `test_sql_tabs.py` covers the SQL
pane's sub-tabs, including that they actually survive closing and
reopening the case file (the whole reason they live there rather than in
`localStorage`) and that `reorder` keeps tabs it wasn't given.
`test_timeparse.py` covers every timestamp operation as pure functions —
epoch auto-ranging boundaries, hex-vs-decimal FILETIME, the syslog year
rollover (including across a batch boundary and Feb 29 in a non-leap
year), offset-to-UTC conversion — plus the guarantee that every parser's
output is accepted by `_TS_ISO_RE`/`TS_NORMALIZE`/`DAY_BUCKET`, which is
what makes derived columns need no new regexes on either side.
`test_tag_undo.py` covers the undo stack, and its load-bearing test is the
one that tags an overlapping selection and asserts the *pre-existing*
assignments survive the undo — a naive re-send-inverted implementation
passes every other test in that file and fails only that one.
`test_structparse.py` covers the JSON/XML path syntaxes and extraction as
pure functions (same shape as `test_timeparse.py`), including the round-trip
property that makes paths safe to write into a column definition, the
EVTX `[@Name='…']` predicate form, and the DOCTYPE refusal.
`test_derived_extract.py` covers the store-level integration: that an
extracted column is an ordinary derived column everywhere, that discovery
only ever offers paths that actually extract, and that a batch is
all-or-nothing (a name collision in the fifth spec must not leave four
columns behind).
`test_derived.py` covers the column lifecycle and, more importantly, the
integration choices that are easy to reverse by accident: the
virtual-root EXPLAIN plan (invariant #2), that derived values are *not*
searchable, that merge eligibility is unaffected, and both cancel
semantics (driven through the backfill's own cancel hook, so the
cancellation point is deterministic rather than a race — same technique
as `test_ingest_jobs.py`).

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

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
                    developer guide. notes/ — the per-subsystem working
                    notes ("things that bite"), one file per part of the
                    app; read the one covering what you're about to touch.
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

Split by subsystem under **[docs/notes/](docs/notes/)** — same entries, same
text, one file per part of the app instead of 1,300 lines every session had
to page through to find the three that applied to it. Read the one covering
what you're about to touch:

| note | covers |
| --- | --- |
| [docs/notes/store.md](docs/notes/store.md) | view building and paging, FTS/trigram and column indexes, grouping and tag counts, search-all, the reader pool, cancellable ops, `compact()` |
| [docs/notes/ingest.md](docs/notes/ingest.md) | CSV/JSON/SQLite/folder ingest, drag-and-drop, ragged rows, background import jobs |
| [docs/notes/grid.md](docs/notes/grid.md) | the virtualized grid: DOM window, page cache and prefetch, row selection, the spacer cap, the gutter grid |
| [docs/notes/ui.md](docs/notes/ui.md) | right-click menus, the filter row and value picker, saved filters, the timeframe filter, tab strips and sidebar, Settings, keybindings |
| [docs/notes/server.md](docs/notes/server.md) | routes and middleware, one-case-one-Winnow, CSRF header, 400-vs-500, shutdown |
| [docs/notes/derived.md](docs/notes/derived.md) | derived datetime columns end to end |
| [docs/notes/plugins.md](docs/notes/plugins.md) | the plugin host and the example plugins |

A new trap goes in the file for its subsystem, not back here — see
[docs/notes/README.md](docs/notes/README.md).

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

`tests/` is a `pytest` suite in three layers, and **CI
(`.github/workflows/ci.yml`) runs all of it on every PR into main** — the
whole suite used to depend on somebody remembering to run it before merging,
which on a repo where changes arrive as whole features is a lot to leave to
memory.

- **Backend** (`tests/test_*.py`) — `store.py`, `server.py`, `workspace.py`.
  The bulk of it; see the coverage notes below.
- **`tests/test_static_syntax.py`** — parses `static/app.js` with `esprima`
  (the Python port). There's no build step between an editor and the browser,
  so an unbalanced brace ships as a blank page and every backend test still
  passes. esprima is ES2017, so `??`/`?.`/bare `catch {` are rewritten to
  older equivalents before the parse; a newer syntax means a line in that
  file's rewrite table.
- **`tests/ui/`** — browser-driven, against a real uvicorn and a real
  Chromium via Playwright. This exists because the class of bug it catches is
  invisible to everything above it: a CSS change that unpinned the filter row
  from its columns, an autofit that measured the wrong thing and could only
  ever grow a column, a "Reset to defaults" button that had never worked.
  Every one of those shipped past a green backend suite and was found by an
  analyst instead. Playwright is `requirements-dev.txt` only and the fixtures
  **skip** (never fail) without it or without a browser, so the airgapped
  target still installs nothing extra — but don't lean on that skip in CI,
  which is why the fast job `--ignore=tests/ui` explicitly and a second job
  installs the browser. Marker: `-m ui` / `-m "not ui"`.

Run it with:

```
pip install -r requirements-dev.txt
playwright install chromium   # once, for tests/ui
pytest
```

`requirements-dev.txt` (pytest, httpx2 for FastAPI's `TestClient`, esprima,
playwright) is separate from `requirements.txt` on purpose — it's
dev-time-only and must never become a runtime dependency the airgapped
target machine needs.
Every test gets its own `tmp_path`-backed case file and a monkeypatched
`workspace.WORKSPACE_DIR` (see `tests/conftest.py`'s autouse
`isolate_workspace` fixture) — nothing in the suite touches the repo's real
`case.db`, `sample.csv`, or `workspace/`.

Coverage is organized one file per concern (`test_ingest.py`,
`test_views.py`, `test_search.py`, `test_grouping.py`, ...) and is weighted
toward the invariants above and the traps in [docs/notes/](docs/notes/) — numeric
NULL-not-zero, ragged-row padding, the contains-mode substring fix, nested
grouping's `path` threading through the virtual small-group fast path,
that same fast path refusing a *filtered* parent (counts stayed right while
the rows went wrong, so only an assertion on the rows catches it), grouping
by tag — including two EXPLAIN assertions, since the wrong join order there
returns the right numbers and takes minutes — tag counts scoped to a view,
search-all's per-term breakdown, session import's tag-remap-by-name, CSV
formula-injection prefixing not touching the stored value, and so on. `test_api_routes.py` covers the HTTP
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

A UI test earns its place by pinning something that *actually broke*, in the
terms it broke in — assert the symptom (a filter cell wider than its header,
a header label whose `scrollWidth` exceeds its `clientWidth`), not the fix,
so it fails again if the fix is refactored away or re-broken from a different
direction. Keep them deterministic: a flaky required check is worse than no
check, because it teaches everyone to re-run until green.

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
# ingest itself still returns in ~8s; see docs/notes/ingest.md)
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

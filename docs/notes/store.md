# Store: views, paging, search, grouping, maintenance

`store.py` — everything that materialises a view, pages it, searches it,
groups it, or maintains the case file. Read this before touching
`build_view`/`fetch_rows`, the FTS or column indexes, `group_summary`, the
reader pool, or anything that holds the writer lock.

Invariants #2, #3 and #4 in [CLAUDE.md](../../CLAUDE.md) are the rules;
these are the traps and the measurements behind them.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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
  ISO/US shapes `DATE_RE` and `tsformat.js`'s `parseTimestamp` already recognize) —
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
- **`expand_group`'s virtual fast path only applies to an unfiltered
  parent.** `_virtual_group_where` reads straight off the member table with
  nothing but `column = value` (+ the nested path) — it has no view to join
  and so no way to express the parent's filters, search or timeframe. The
  gate is `_grouping_covers_whole_source`; anything else materialises, same
  as a merge or an over-threshold group already did. The bug this closed
  was quiet in exactly the way that costs you: the *counts* come from the
  other side (`group_summary` and `expand_group`'s own `total` both join the
  view and stayed correct), so the grid asked for `row_count` rows and got
  the first `row_count` of a longer, unfiltered list — and tag/export on the
  group read the same way, which made it an over-tagging bug and not just a
  display one.
- **Grouping by tag** is a pseudo-column, `TAG_GROUP_COLUMN` (`"__tag__"`),
  carried through every grouping path as an ordinary column name so nothing
  between the frontend and `group_summary` needs a second notion of what a level
  is. It's in `RESERVED_COLUMN_NAMES`, so a CSV with a literal `__tag__`
  header gets renamed at ingest and the sentinel can never be ambiguous.
  Three things about it are decisions:
  - **One group per tag, not one per tag-set.** A row with two tags is
    counted under both, so the counts can sum to more than the view holds.
    That's the only reading that answers "how much of this have I marked,
    and as what"; a partition into `"Lateral movement, Persistence"`
    combinations is combinatorial and useless.
  - **A group's value is a tag *id*, not its name.** `tag_defs` has no
    unique constraint on `name`, so grouping by name would silently merge
    two tags an analyst deliberately kept apart. `groupValueLabel()` in
    the frontend renders the name from `S.tags`; the untagged group's value is
    `NULL`.
  - **The join order is pinned, and that's load-bearing.** `v.view_N` is
    indexed on `pos` and nothing else, so reaching a view row by `rid` is a
    full scan of it — and given a `WHERE vv.source_id = ?` to work with,
    SQLite drives from `row_tags`' covering index and re-scans the entire
    view once per tagged row (measured: 150k x 300k row visits, minutes,
    where the right plan is 200ms). `_tag_group_branches` therefore uses
    `CROSS JOIN` (which SQLite documents as suppressing reordering), drops
    the per-member `source_id` restriction when there's no nested path, and
    reaches the source table only through a self-contained `EXISTS`. The
    whole-table case never touches the source at all — per-tag counts are
    `row_tags`' own aggregate and the untagged remainder is arithmetic on
    the member's `row_count`. `test_grouping.py` pins both with EXPLAIN.
- **`tag_counts_in_view`** is what the tag ribbon shows once a filter or
  search is on: the same shape `tag_counts` returns, counted over one view.
  Scope is the view exactly as built, tag filter included — a ribbon that
  quietly dropped one of the filters in play would be reporting on a view
  nobody is looking at. The frontend keeps the whole-table counts in
  `S.tagCountsAll` alongside and puts them in the chip's tooltip rather than
  picking one number and hoping the analyst infers which it meant. Same
  join `tag_positions` makes, same untagged-source short-circuit in front
  of it, since this runs after every view build.
- **Search-all breaks a pasted list down per term.** `_or_of_positive_terms`
  recognises the one shape where a per-term count means anything on its own
  — the "Paste a list" mode's OR of positive terms — and
  `_search_all_term_counts` then runs one capped count per term, but *only*
  on a source the union count already proved matched. That ordering is the
  whole design: sources that miss (the majority of a big case) cost exactly
  what they cost before the feature existed. A folded-in
  `SUM(CASE WHEN … )`-per-term single query would instead lose the OR's
  short-circuit on every source, matching or not.
  `SEARCH_ALL_TERM_BREAKDOWN_MAX` bounds the accident (a whole wordlist
  pasted in), not normal use. Mixed AND/NOT from the Advanced builder gets
  no breakdown: those terms constrain each other, so a count for one alone
  describes a query nobody ran.
- `_quick_hash` is size + first/last 1 MB, not a full digest. It identifies a
  file for session matching; it is **not** an evidence integrity hash and must
  not be presented as one.
- Session import remaps tag IDs **by name**, creating missing tags. Two analysts
  with their own "Lateral movement" tag merge rather than duplicate.
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
  so the reclaimed bytes don't just move into a `-wal`; before, so VACUUM
  starts from a folded-in file.
- **`wal_checkpoint(TRUNCATE)` reports failure in its RESULT ROW (busy=1),
  not by raising** — so the obvious one-liner silently no-ops whenever a
  reader holds an older snapshot, which is exactly why compact "didn't
  shrink" cases with live readers. Always go through
  `_checkpoint_truncate()`, which retries within a budget and returns
  whether it actually completed (the two `bench/` size-measuring call
  sites go through it too — a silent no-op there yields a size missing
  the WAL bytes, in the one suite whose job is detecting size
  regressions).
- **Qualify it `main.`** — an unqualified `wal_checkpoint` checkpoints
  *every attached* database and ORs their busy flags together, including
  the scratch views db (invariant #3) that every pooled reader attaches.
  Unqualified, a reader merely paging a view reports the case file's
  checkpoint as blocked (verified: `(1,0,0)` unqualified vs `(0,0,0)` for
  `main.` with only `v` pinned).
- **TRUNCATE invokes the connection's busy handler**, so one blocked
  attempt sits *inside* the pragma for the full `busy_timeout` (measured
  5.03s at the default 5s). `_checkpoint_truncate` scopes the timeout down
  per attempt so its own budget is the real wall-clock bound, and uses a
  monotonic deadline — it runs under `self.lock`, so overshooting stalls
  every writer.
- **compact's sizes are `main + -wal` on both sides, measured before any
  checkpoint**, which is what makes `reclaimed_bytes` honest without
  depending on a checkpoint succeeding. A checkpoint that can't complete
  is reported (`wal_checkpointed`, `wal_pending_bytes`), never raised:
  the bytes are simply counted as not-reclaimed and a later passive
  checkpoint collects them. Raising after the VACUUM would throw away
  minutes of completed work on a large case.
- The auto-created per-column filter indexes are surfaced per table in the
  Tables modal, with a drop. They're created behind the analyst's back and
  never expire. `list_column_indexes` works by hashing each *known* column
  and looking for that index name — the name deliberately carries only an
  md5 (`_column_index_name`), so there's no way to read a column back out of
  one.
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
  server.py maps `OpCancelled` → HTTP 499 in one exception handler; the frontend
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
    1.61ms here; pooling (`READER_POOL_CAP` = the frontend's
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

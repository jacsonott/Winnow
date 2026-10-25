# Server: HTTP layer, case lifecycle, safety rails

`server.py` — routes, middleware, and the handful of guards that exist
because this is a local single-analyst tool with no auth.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

- **Across cases: one writable case, N read-only readers.**
  `winnow/multicase.py` reads OTHER case files (`mode=ro`, no lock, no
  views database, no heartbeat) while the open case keeps its exclusive
  lock and its writer — so every invariant in CLAUDE.md holds unchanged
  and a case another Winnow has open is still readable (verified: WAL
  and all). Three things sit on it: an IOC sweep, a cross-case SELECT,
  and a unified timeline.
  Two constraints worth knowing. SQLite attaches at most **10**
  databases (`MAX_ATTACHED`), and `v` already uses one — so the query
  path caps at 8 cases and everything else opens one reader at a time,
  which is why the sweep and timeline have no case limit. Two more that
  each produced a confident wrong answer: the timeline orders and
  windows by `TS_NORMALIZE`, like the case's own timeframe filter (raw
  text put `1/5/2024` before `12/31/2023`), and every SOURCE gets the
  whole `limit + 1`, merged as they arrive — reading each with "the
  limit minus rows so far" let the first table spend it all on its
  latest rows while reporting `truncated: False`. The sweep's values are
  LIKE-escaped: `svc_backup` matched `svcXbackup`. And the routes
  only read **registered** cases: a path arrives from the browser, and
  the case list is the analyst's own statement of what they work on.
  These views are read-only BY DESIGN, not pending work: tags and notes
  live in the case that owns the row, so writing across cases means
  opening those cases read-write and fighting the one-Winnow-per-case
  lock. The supported move is "open it in its own case", which every row
  offers.
  **The header button is withdrawn** (September 2026) until the feature
  is redesigned — three panes with three result shapes, and a sweep that
  stops short of what an analyst does with a hit. Routes, `multicase.py`,
  `multicase.js` and `tests/ui/test_multicase_ui.py` all stay;
  `__winnow.openMultiCase()` still opens it.

- **An `async def` route runs ON the event loop; a plain `def` route does
  not.** FastAPI threadpools sync handlers for you, so most of this file is
  safe by default. The async ones — which have to be async, because they
  `await file.read()` or the request body, which is exactly where the heavy
  work lives — must hand blocking work to `run_in_threadpool` themselves.
  This has bitten twice: the plugin dispatcher called plugin handlers
  directly (one LLM call froze Winnow for its duration), and
  `/api/ingest/upload` called `ingest_csv` directly (a 130MB CSV made a
  trivial `/api/version` take **2.4s**; 21ms after the fix). Every ingest,
  preview, import and plugin-install route is threadpooled now, and
  `tests/test_event_loop_blocking.py` fails the build if a new async route
  calls a Store method — or `_reload_plugins`, which imports arbitrary
  plugin code — on the loop. Pass the bound method as an argument
  (`run_in_threadpool(store().ingest_csv, tmp, ...)`), don't call it.
  Two consequences worth knowing. The worker pool is bounded (anyio's
  default is 40 threads) and shared by every sync route, so enough
  simultaneous slow handlers would fill it and stall the app anyway —
  which is why PLUGIN handlers run under a limiter of their own
  (`PLUGIN_THREADS`, 8) rather than the default one: a plugin can queue
  behind other plugins, never in front of Winnow's own routes. And
  threadpooling does not make anything take the writer lock less — a long
  write still serialises other writes, which is invariant #4's job, not
  this one's.

- **A stale id must not act on something else.** Two windows on one
  server see the same store, and either can delete what the other is
  showing. Two rules keep the slower window honest. Case-file writes that
  name a table, tag or row this case no longer has raise `RecordGone`
  inside their transaction, before writing, and the global handler answers
  **404** (store.md has the store side; ids are never reused there either).
  And the case REGISTRY never reuses an id (`CaseRegistry` keeps `next_id`
  in cases.json), while `DELETE /api/cases/{id}` takes the `path` the
  client's list showed and refuses with **409**, touching nothing, when the
  id now names another file — a home screen painted before the server
  restarted can hold an id from the old numbering, and "delete with file"
  on a bare id is the one step here that cannot be undone.
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
  The marker is named from the case's **realpath**, not its abspath: one
  marker per file however the path was spelled, or a case opened through
  a symlink holds a marker that nobody opening it by its real name ever
  probes (`_same_case_path` is the matching comparison for "is this the
  case that's already open"). **Deleting** a case file (`DELETE
  /api/cases/{id}?delete_file=true`) is the one place the probe is a
  refusal rather than a prompt — 409 `case_in_use`, no "anyway" — since
  there is no version of deleting a case somebody has open that they
  would want; it takes the -wal/-shm and a stale marker with it. The
  case THIS server has open is the exception, and is closed first
  (`_close_case`, under `_CASE_SWITCH`): going back to the case list
  doesn't close a case, and nothing there does, so refusing it ("Close
  this case before deleting its file") left the case an analyst had just
  come from undeletable until they opened another one. The probe runs
  after that close, not before — this process's own flock reads as a
  holder until its Store lets go. And the registry entry only goes once
  the file has: on Windows a file something still holds can't be
  removed, and dropping the entry anyway hid a case still on disk.
- **A Winnow binds its port before it does anything else, and its window
  waits for its own pid.** main() used to open the case, import `--open`
  files and start the browser thread, and only then have uvicorn bind;
  the thread waited for "something answers on the port". A second Winnow
  started while the first held 8777 got that answer from the FIRST one,
  opened its window there, failed to bind and exited — silently, under the
  no-console launchers — and the analyst worked in the first Winnow's case
  through a window they believed was the second's. Reported as tags from
  one case showing up in another. Now `_claim_port` binds first (the next
  free port after `DEFAULT_PORT` when none was asked for; an explicit
  `--port` is that port or exit 3, before any case is touched), uvicorn
  serves on that socket (`server.run(sockets=[...])`), and
  `browser.open_when_ready(expect_pid=...)` opens nothing until
  `/api/version`'s `pid` is this process. Keep both: binding is what makes
  the port ours, the pid check is what catches anything that ever makes it
  not. `tests/test_second_winnow.py`.
- **Every page request names its case, and a request about any other case
  is refused.** One server, one open case, any number of windows: when one
  window opened another case, the rest went on sending requests whose
  source ids, rids and tag ids every case reuses, so a window left on A
  painted B's rows under A's tabs and wrote its tags onto B's rows at the
  same rid. core.js's `api()` (and the upload XHR in jobs.js) sends
  `X-Winnow-Case` — the token `/api/case/current` and every case-switching
  route return as `case_token` — and `require_same_case` answers a
  mismatch with 409 `case_changed`, touching nothing. Plain-navigation
  downloads carry it as `?case_token=` and get an HTML page back instead,
  since the refusal replaces the app. No token, no check: scripts, the
  launcher and old pages are let through. `_CASE_FREE_PATHS` is what a
  stale window can still do (switch, list cases, presence, version,
  shutdown, log); a new route is guarded unless it is added there, which is
  the safe default. The token names the case FILE (realpath + normcase,
  like `_same_case_path`), so a window outliving a restart on the same case
  carries on, and a quick-look save-as (the file moves) hands the saving
  page a new one. A route that swaps `STORE` returns `case_token` and calls
  `_announce_case()`; the client wraps the request in `switchCase()`, which
  is what keeps its own switch from reading as another window's.
  `tests/test_case_guard.py`, `tests/ui/test_two_windows_one_case.py`.
- **The presence stream says which case is open.** A `case` event when the
  stream opens and whenever `_announce_case()` wakes it (a sync route on a
  worker thread, so the wake goes through `loop.call_soon_threadsafe`):
  how a window learns another switched the server before it asks for
  anything, and how one reconnecting to a server restarted on another case
  finds out at once. The page covers itself (`#caseGone`) and offers a
  reload; on the home screen it just drops its token.
- `/api/case/open` short-circuits when the requested path is **already the
  open case** — necessary, because the open-before-close ordering would
  otherwise make the process probe its own lock and refuse itself. Guarded
  by `not STORE.closed`, and that guard is load-bearing: `STORE` outlives
  `Store.close()` on both the case-switch path and the legacy-preset
  migration, so "same path" alone is not "already open" and short-cutting
  there serves rows off a closed connection.
- **Every route that replaces `STORE` holds `_CASE_SWITCH` for the whole
  swap** — case open, the quick-look save-as and discard, the association
  open (each is a thin wrapper around a private `_case_open` & co.). A
  double-click on a case card is two `/api/case/open` POSTs on two worker
  threads; unserialised, both read the same `old` and both opened, and the
  Store whose assignment lost was never closed — its lock held for the
  life of the process, so the case read as "open in another Winnow"
  naming this one — and on one path both passed the already-open check
  and the case got two writer Stores. A new route that swaps `STORE` takes
  the lock too. The client has its own guard (`openCase` hands a second
  call the in-flight one's promise), but the server cannot rely on it: two
  windows, or a script, race just the same. `tests/test_case_lock.py`.
- Static assets (`/`, `/static/*`) get an explicit `Cache-Control: no-cache`
  from a middleware in server.py — FastAPI's `StaticFiles` only sends
  `ETag`/`Last-Modified`, and without an explicit `Cache-Control` the
  browser is free to serve a stale `style.css`/`js/*.js`/`index.html` from
  its own disk cache indefinitely, surviving even a normal reload (only a
  hard refresh forces a re-check). Bit us for real: a reported layout bug
  turned out to be a stale cached stylesheet, not the actual CSS on disk.
  `no-cache` still means a cheap `304` when nothing changed (browser
  revalidates via the existing ETag every load, doesn't re-download) — this
  isn't a `no-store`/always-refetch tradeoff, just "always double-check,"
  which is the right default given there's no build step or content-hashed
  filename to cache-bust with (and these files change often here). API
  routes are untouched — no reason to force revalidation on data responses.
- There's no auth — this is a local, single-analyst tool. The one guard against
  a malicious page on another tab silently triggering side effects (e.g. via an
  unpreflighted `multipart/form-data` upload) is server.py's
  `require_client_header` middleware, which 403s any non-GET `/api/*` request
  missing an `X-Timeline-Lite-Client` header. `core.js`'s `api()` sets it on
  every non-GET call automatically — a raw `fetch()` that bypasses `api()`/
  `post()` won't have it and will get 403'd. GET stays exempt on purpose (the
  Export/session/filters download links are plain navigations, which can't set
  custom headers at all).
- **A name the analyst typed cannot be a plain `{name}` path segment.** The
  ASGI path is percent-decoded before routing, so `encodeURIComponent`'s
  `%2F` arrives as a real `/` and a one-segment parameter never matches:
  a case session saved as "before 12/05" could never be loaded, renamed,
  downloaded or deleted. The case-session routes take `{name:path}`
  (greedy, so a name ending in "/load" still resolves); the plugin-state
  key went the other way and travels as a parameter. Either works — what
  does not is a segment. The one residue is "." and "..": a browser
  resolves those (and their `%2E` spellings) out of the URL before it is
  sent, so `Store._session_name` refuses them at save, rename and adopt
  rather than storing a session nothing can reach.
- `api_view` maps only `ValueError`/`KeyError` to 400; everything else is a
  500. It used to catch bare `Exception`, so an internal defect surfaced as
  "Filter error: …" — blaming the analyst's filter for something they can't
  fix and hiding the traceback. `core.js`'s `api()` attaches `err.status` so
  callers can tell the two apart. Nothing that a user can actually type
  reaches the 500 path: `validate_where_fragment` converts SQL errors to
  `ValueError`, and `_regexp` swallows `re.error`.
- **A 500 means Winnow is broken; input it can't act on is a 4xx.** A
  route fuzz found ~50 routes answering 500 for the request's own fault,
  and the fixes follow three rules. An int past SQLite's 64-bit range
  (any path/query/body id) is ONE handler, `int_out_of_range_handler`,
  keyed on sqlite3's own OverflowError message rather than a bound on
  every int parameter — any other OverflowError is still the bug it
  would be. A structured parameter is checked at the door: a typed
  pydantic shape where one fits (`tag_view`'s `exclude` pairs → 422),
  an explicit check where it arrives as text (`group_summary`'s JSON
  `path`) or as a file (`session_document_problem` /
  `case_session_document_problem` in store.py, `_filters_file_problem`
  here) — and a whole case session is checked before its FIRST table is
  applied, so a bad tenth table can't leave nine written. A missing
  table is a 404, a table still importing is the 400
  `Store._require_columns` raises, and no open case is a 400 — nothing
  is broken, there's just no case yet. `tests/test_api_routes.py`'s
  last section pins each one through a client that doesn't re-raise.
- **The background-search routes** — `POST /api/view/start`, `GET
  /api/view/job`, `POST /api/view/job/cancel`, `POST /api/view/adopt`
  (the search box's build, run as a job that builds a *held* view; see
  [store.md](store.md)) — are plain `def` like `/api/view`. The build
  runs on a Store thread; the start route only waits for it, up to
  `wait_ms`, clamped to `Store.VIEW_JOB_INLINE_WAIT_MAX_MS` because that
  wait parks one of the 40 shared workers and the client never sends the
  parameter. A build error is a field of the job record (`error_status`
  400 for the analyst-fixable kind), never the start route's status;
  `/api/view/job` 404s for a superseded id, which is the poller's cue to
  stop; adopt's 409 says "expired" — the word the client keys on to run
  the search again — and the closed-case 409 from
  `closed_database_handler` deliberately still does not. `_busy_reason`
  counts running view jobs, so idle shutdown cannot reap a search whose
  window was closed. `tests/test_view_jobs.py`.
- **The watchlist scan routes** — `POST /api/watchlist/scan/start` (body
  `{source_ids?, watchlist_ids?}`, both optional), `GET
  /api/watchlist/scan/job?job_id=`, `POST /api/watchlist/scan/cancel?job_id=`
  — are plain `def` like the search-all job's; the scan runs on a Store
  thread and start answers with the record at once (see
  [store.md](store.md)). A start that displaces a running scan folds
  its remaining scope into the new job and answers with the widened
  `source_ids`/`watchlist_ids`; `/api/watchlist/scan/job` 404s for a
  superseded id (the poller's cue to stop), cancel's miss is
  `cancelled: false`, and `_busy_reason` counts a running scan. The synchronous `POST
  /api/watchlist/scan?source_id=&watchlist_id=` stays: profile apply
  scans inline while it seeds a watchlist, and tests use it. `POST
  /api/watchlist` answers 400 for an exact duplicate value; the two
  import routes return `added_ids` so the client scans for the new
  entries only; `GET /api/watchlist/hits` answers `{sources, hits}`. `GET
  /api/watchlist/overview` is what the tab loads — the indicators plus
  `scanned_sources`/`scan_targets` and the overlap picture (see
  [store.md](store.md)); `/api/watchlist` still answers the plain list,
  because the badge poll and the import hook only need that. `GET
  /api/watchlist/latest?limit=` resolves the timeline templates the same
  way `/api/timeline` does before calling the store, which is why it lives
  on a route rather than in `Store.latest_hits`. `POST
  /api/watchlist/merge` is 400 when the keeper does not cover every row the
  dropped indicator found, and the message says how many would go.
- **`POST /api/timeline/preview` is a read that takes a body** — the
  Configure tables… dialog's unsaved settings (`source_id`,
  `timestamp_column`, `body_columns`, `type_label`, optional `limit`
  clamped to 1–10), POSTed like `/api/derived/preview` and so behind the
  client-header gate like any other POST. It writes nothing: no template,
  no view. A merge or a table still importing is a 400 (the Timeline is
  configured per real table; `_require_columns`), and a table that is gone
  falls through to `gone_table_handler`'s 404 rather than being caught
  here — catching every KeyError would turn a defect into a 404.
  Unlike `/api/timeline` it does NOT resolve the saved templates: the
  dialog sends what is on screen, saved or not.
- **The profile routes** (`/api/plugin_bundles/*`) are all plain `def`:
  every one of them touches `WS.plugin_bundles` (a JSON file under a
  process-wide lock) or the Store, so none belongs on the event loop.
  Two shapes worth knowing. `POST .../{id}/apply` takes an OPTIONAL body
  (`BundleApplyBody | None = None`) because the request that has always
  existed sends none — the new-case dialog's Profile select, tests,
  scripts — and that has to keep meaning "all four parts"; a part name
  outside `APPLY_PARTS` is a 400 rather than a silently ignored word.
  `GET .../{id}/plan` writes nothing at all: it is the apply sheet's
  numbers, counted against the open case, and a sheet whose numbers came
  from a route that could mutate would be a strange thing to put in front
  of a confirmation. Both 400 without a case, like every other per-case
  route. Import is `UploadFile`/multipart like the saved-filters import
  beside it; export sets `Content-Disposition` with the profile name
  slugged through a `[^A-Za-z0-9._-]` filter, since profile names are free
  text.
- **Every download's `Content-Disposition` comes from `_attachment()`**,
  never an f-string. Starlette encodes header values as Latin-1, so
  `filename="{name}"` was a 500 — and no file — for one en dash or
  Cyrillic letter, and the names that reach these headers are exactly the
  text that carries them: a SQL tab's name, an evidence file's own name,
  what the analyst typed in the save prompt. The helper writes the real
  name as RFC 6266 `filename*=UTF-8''…` beside an ASCII `filename="…"`
  fallback (non-ASCII, `"` and `\` replaced), and turns control
  characters into `_` in both, since a CR/LF would end the header line.
  `FileResponse` gets it through `headers=` rather than its own
  `filename=`, so there is one rule.
- `run_sql` (the SQL pane) allows arbitrary SELECT/EXPLAIN on purpose, but
  blacklists `ATTACH`/`DETACH`/`PRAGMA`/`VACUUM` as defense-in-depth — none of
  those serve a read-only ad-hoc query pane. CSV export runs every cell through
  `_csv_safe()` (OWASP formula-injection prefixing for `=+-@`/tab/CR-leading
  values) since exports are explicitly meant to be opened in Excel and handed
  to other analysts; this only touches the exported copy, never the stored
  case-file value. **The header row and the Tags column go through it too**
  (`_export_header`): column names are header text out of the evidence
  file and tag names arrive in adopted sessions, and openpyxl writes any
  string opening with `=` as a formula — a column called `=HYPERLINK(...)`
  was a live link in the hand-over workbook while every cell under it was
  neutralised. The SQL pane's CSV (`Store.sql_result_csv`) follows the
  same rules — column names through `_csv_safe`, text cells too, numbers
  left as numbers (`-5` is not a formula, and `'-5` would be text in the
  spreadsheet) — so its CSV and Excel buttons cannot disagree about one
  result. **Worksheet exports go
  through `_xlsx_safe()` instead**,
  which is `_csv_safe` plus the C0 control characters XML 1.0 cannot hold:
  ingest keeps those in cell values on purpose (names must be quotable,
  cells are evidence), openpyxl refuses them, and it refuses the whole
  workbook rather than the one cell — so one BEL in one command line used
  to take down the entire tagged export. They are escaped to `\x07` rather
  than dropped, because a silently shortened string in a hand-over artifact
  is worse than a visible escape. Length is the same rule: openpyxl cuts a
  value at Excel's 32,767-character cell limit without saying so, so
  `_xlsx_safe` cuts first and ends the cell with a marker naming the full
  length. Sheet names (`_xlsx_sheet_name`) are XML text too — a control
  character in a source name wrote a workbook nothing would reopen — and
  the apostrophe rule is applied after the 31-character cut, not before.
  A merged view's CSV carries a `Table` column after `Line` (the member's
  nickname or name): `Line` is a rid within one member, so without it two
  rows of a merge both read "Line 1" with nothing saying where either is.
  A single table's export keeps the old header.
- **The SQL pane's results are held server-side** (`winnow/sqlresults.py`,
  `POST /api/sql/run` and the `/api/sql/result/{id}/…` routes). The pane
  has no row cap, and "every row" as one JSON response was every row in
  Python, then in the response, then in the browser: no memory bound at
  all. A run now copies its rows in `BATCH`-sized `fetchmany`s into a
  scratch SQLite file (`winnow-views-sqlres-*`, in the platform tempdir
  rather than /dev/shm, since a result is every cell of every row; the
  views prefix means `sweep_orphan_views` collects one a hard kill left
  behind, and the same flock keeps a live one safe) and answers with the
  first page, the total and each column's widest value. The rest is paged by position,
  click-sorted by the server into a per-result order table (rows → rowid
  → `pos`, never `LIMIT/OFFSET`), and exported by streaming: CSV through
  a generator, XLSX from a temp file the response deletes after sending
  (a workbook can't be streamed before it is finished), both in the
  displayed sort, both spilling nothing into memory. Copy is `pick`, which
  answers 413 past `COPY_MAX_BYTES` rather than build a clipboard string
  of hundreds of MB. Things that bite: **the sort has to reproduce the
  one the browser used to do** (NULLs first, numbers — including numeric
  text — numerically, then text) or a click-sort and its export disagree
  — `WINNOW_SORT_NUM` is a UDF for that reason; **a result is a handle
  the client must let go of** (a re-run sends the old id in `release`,
  closing a tab releases, and the store keeps at most `KEEP` anyway); and
  a run **stops on a nearly full disk** (`MIN_FREE_BYTES`) with the rows
  it has, rather than filling the tempdir. `/api/sql` itself is
  unchanged for Python callers and `winnow.api.sql`: rows inline,
  `limit` 5,000 by default.
- **Every SQL pane cell is encoded at the JSON boundary**
  (`pane_cell_json`, in `winnow/sqlresults.py`), not in `Store.run_sql`,
  whose raw values plugins are owed: a non-UTF-8 BLOB becomes SQLite's
  `X'00FF'` (a UTF-8 one its text, as before), a non-finite REAL
  `Inf`/`-Inf`/`NaN`, and an integer past ±2^53 its decimal string. The
  first two were a 500 (JSON has neither); the third arrived rounded, so
  grid, Copy, CSV and Excel all disagreed with the case for a
  FILETIME-sized number. A held result's pages, picks and workbook go
  through it, `/api/sql`'s inline rows do, and its CSV and clipboard text
  (`js_str`) spell the same three the same way. "Save as table" stores
  the same spellings (`pane_cell_text`) — it stored Python's `b'…'` repr
  — and re-runs the query that produced the result on screen (`r.sql`,
  kept by `runSql`), not the editor's text by then.
- **A pane query has to be stoppable, because some of what gets typed
  never ends** — a recursive CTE with no stop condition, a cross join of
  two 2M-row tables. One used to run at 100% CPU through everything: the
  analyst had no Stop, a closed window or reload left it running, a case
  switch left it running, and **Shut down, Ctrl+C and SIGTERM left the
  process running** with the case lock held. That last is a uvicorn trap
  worth knowing for anything else that can run unbounded: graceful
  shutdown waits for every in-flight request *before* it runs the
  lifespan's shutdown half, so `_lifespan`'s `Store.close()` never got
  the chance, and the process cannot exit while a worker thread is inside
  `sqlite3_step` anyway. Every way in ends in `Connection.interrupt()` on
  the pane's own connection (`Store._pane_query`, which `run_sql`,
  `run_sql_result` and `sql_to_table` all run on):
  - **the pane's Stop, and the busy chip.** `runSql` sends an `op_token`
    with `/api/sql/run` and arms `armOpCancel` with it, like a view
    build; the running result shows "Running… N rows so far" (polled
    from `/api/sql/run/progress`) beside a Stop button that cancels the
    same token. Stopped after rows have landed, the result keeps them and
    says "stopped"; stopped before the first one, it is the 499, which
    paints as "Query cancelled". A run that is overtaken (Run again, its
    tab closed, another case opened) is cancelled the same way
    (`abandonSqlRuns`), so it stops rather than finishing for nobody.
  - **the client hanging up.** `/api/sql`, `/api/sql/run` and
    `/api/sql/to_table` are `async` for this alone (`_run_pane_query`): a
    sync route never reads its socket again, so it cannot notice. They
    wait on the worker thread and on the ASGI receive channel together —
    after the body, the only message left is `http.disconnect` — and turn
    a hang-up into `cancel_op`, with a made-up token when the client sent
    none. That also keeps a closed window's query from holding the idle
    shutdown off (it is an in-flight request).
  - **shutdown.** `main()` runs a `uvicorn.Server` subclass whose
    `handle_exit` — the signal handler, which is where Shut down's
    SIGINT, Ctrl+C and SIGTERM all land — calls `stop_pane_queries()`
    first, on a thread of its own (a handler can run inside
    `Store.close()` holding the lock that takes). That is why `main()`
    builds the server itself instead of calling `uvicorn.run`; it keeps
    `run`'s KeyboardInterrupt suppression and its exit code 3 when the
    server never started. **A test that drives `main()` up to the bind
    stands in for `uvicorn.Server.run`, not `uvicorn.run`** — patching
    the latter no longer stops anything, so the test starts a real server
    and hangs (tests/test_no_console_launch.py, test_not_a_case_file.py).
  No deadline: a GROUP BY over a few million rows legitimately takes
  minutes, and the analyst is better placed than a timer to say which
  query is runaway. `tests/test_sql_runaway.py` covers each way in.
- **`POST /api/shutdown`** (the home screen's ⏻ button and Session → "Shut
  down Winnow…") stops the server from the UI — the server usually lives
  in a forgotten terminal, and closing the browser tab leaving it running
  is how a case file stays flock'd all weekend. It answers 200 first, then
  `_trigger_shutdown` raises SIGINT ~0.3s later via
  `signal.raise_signal` (not `os.kill` — works on Windows too), which
  uvicorn treats exactly like Ctrl+C: graceful shutdown, `_lifespan` runs
  `Store.close()`, views scratch files are deleted. `_trigger_shutdown`
  is a separate function *so tests can monkeypatch it* — calling the real
  one under pytest kills pytest. The CSRF header gate is what stops a
  hostile page in another tab from turning the server off. A SQL pane
  query in flight is stopped by that SIGINT (`handle_exit` →
  `stop_pane_queries`, above), not by this route.
  **Open windows used to hold it up the same way a runaway query did.**
  Every page's `/api/presence` stream is a response that never finishes,
  and uvicorn's graceful shutdown waits for every open response — so Shut
  down answered 200, the page said "Winnow is off", and the process ran on
  with the case lock held until the last window closed (the window that
  pressed the button included; Ctrl+C and SIGTERM likewise). `main()`'s
  `_Server.shutdown` now calls `_end_presence_streams()` before uvicorn
  starts waiting: each stream sends `event: shutdown` (non-empty `data:`,
  or EventSource never dispatches it) and ends, so the other windows say
  "has shut down" instead of the lost-connection text, and the exit is
  under a second. Under a bare `uvicorn server:app` there is no
  `_Server`, so streams still hold it. Other in-flight requests (an
  export downloading) are still waited for — that is the graceful part.
  `_trigger_shutdown` raises its SIGINT **once**: uvicorn reads a second
  one as the impatient Ctrl+C and force-quits without the lifespan, which
  skips `Store.close()` (two windows pressing Shut down together).
  Before asking, the confirm names the other windows on the server
  (`GET /api/presence/others?page=`, the asking page excluded).
  `tests/test_shutdown.py`, `tests/ui/test_shutdown_and_log.py`.

- **Idle shutdown rides the presence stream, not request traffic.** An
  analyst reading the grid makes no requests for hours, so "last request
  time" would reap a live session; instead every page holds an EventSource
  open to `/api/presence` and the count of live streams is the signal.
  Three holds stop it destroying work: open streams, in-flight HTTP
  (counted by `_InflightCounter`, `/api/presence` itself excluded or it
  would read busy forever — a CSV download outliving its tab is exactly
  what this hold protects), and running/queued ingest jobs. The counter
  is **raw ASGI on purpose**: it used to live in a `@app.middleware`
  (BaseHTTPMiddleware), whose `call_next` returns when the headers exist
  and streams the body afterwards, so a download stopped counting the
  moment it started. Anything that has to span a whole response body
  can't be a BaseHTTPMiddleware. A server
  nothing ever connected to gets a 15-minute fuse rather than the 2-minute
  one — `--no-browser` plus a slow manual visit is legitimate, but an
  association-spawned server whose window failed to open still gets
  reaped. `--no-idle-shutdown` (or the `WINNOW_*_EXIT_S` env overrides the
  tests use) turns it off. Graces are compared on `time.monotonic()` — a
  suspended laptop must not wake up to an instant shutdown.

- **A quick-look case is temporary because of where it lives, not what it
  is.** `_is_temp_case` asks one question — is the file's parent directory
  named `quicklook/`? — so temp-ness survives restarts with no flag to
  persist, and `save_as` converts to a real case by *moving the file* out
  of that directory (close → `os.replace` → reopen; on a failed rename the
  old file reopens, never leaving the server storeless). Only the file
  moves and its `-wal` is deleted, so the close is `close(fold_wal=True)`:
  every write is checkpointed into the file first. A reader checked out by
  a request still in flight (a CSV export streaming, a search-all sweep)
  keeps the file open past the writer's close, so SQLite's own
  last-close checkpoint never runs, and with 64 KB pages the WAL only
  checkpoints itself at ~64 MB — on a quick look, usually the whole
  import and every tag. That reader only blocks the fold when its
  snapshot is older than the newest write; then save_as reopens the
  quick look and answers 409 rather than moving a file that is missing
  them. Quick-looks stay
  out of the case registry until saved, which is also why the startup
  `_sweep_quicklook` janitor is safe: it only eats files that are old,
  unlocked, and carry zero tags/notes/sessions — touched work is never
  reaped. The `--assoc` launcher resolves in a fixed order: a dropped
  *case file* (SQLite header + the three winnow tables) opens as itself; a
  data file goes to a just-started temp instance (<120s — multi-select
  spawns one server, not five), else an idle registered instance, else
  this process becomes the server on a free port. `/api/assoc/open` is
  loopback-only; its ingest tries PLUGIN formats first (the import
  modal's own precedence) so a registered plugin extension actually uses
  the plugin's parser instead of falling through to the CSV sniffer —
  plugin ingests are synchronous, so those entries carry a source_id,
  not a job_id. `/api/prefs` reports `first_run: false` inside a temp
  case — a fresh install's first double-click must not stack the
  cases-dir setup prompt on top of the file the analyst opened.

- **The default cases folder is the install's, never the working
  directory's.** `_default_cases_dir()` is `INSTALL_ROOT/cases` — what
  `_cases_dir()` falls back to, what `GET /api/prefs` reports as
  `default_cases_dir`, what the first-run prompt names and what its "Use
  the default" stores. It used to post `'cases'`, which `POST /api/prefs`
  made absolute against the server's cwd, so `python /opt/winnow/
  server.py` run from a home directory filed every case under ~/cases.
  A relative `cases_dir` is now resolved against `INSTALL_ROOT`, and New
  case falls back to `default_cases_dir` (not a relative `cases/`) on an
  install that has cases but never set a folder.

- **The quick-look sweep must recognise every kind of work.** It deletes
  abandoned temp cases after 7 days, and judged "no analysis" from tags,
  notes and sessions alone — so a quick-look whose content was a
  dashboard, case variables, a watchlist, a saved query or a PLUGIN's own
  table (an LLM chat transcript is often the only thing in one) was swept
  as empty. `_WORK_TABLES` + `_case_holds_work()` now cover everything a
  case holds except what it is BORN with (`open_tabs`, the seeded
  `tag_defs`) and incidental UI state. Add new case tables to that list
  when they can hold something an analyst would miss.
  Two more guards, same bias: the sweep only considers files
  `is_winnow_case_file` accepts (a History.db dropped in `quicklook/`
  has none of the work tables either, which read as "empty" and got it
  deleted), and a file it can't read is skipped rather than ending the
  sweep — a corrupt one used to raise `sqlite3.DatabaseError` past the
  per-file `except OSError` and strand every file listed after it.

- **A dropped presence stream is not a closed window.** Idle shutdown
  used to fire `IDLE_EXIT_S` after the last stream ended, which is right
  when the analyst closed the window and wrong every other way a stream
  can end: Edge and Chrome suspend background tabs, laptops sleep, VMs
  pause. Analysts hit exactly that — the server exited while the window
  was still sitting there, and the page's next click failed. Now
  `connection.js` POSTs `/api/goodbye` on `pagehide` (keepalive, so it
  outlives the unload; skipped when `persisted` says the page is only
  going into the bfcache), and the short fuse applies only when that
  arrived. A stream that merely stopped gets `SUSPENDED_EXIT_S` instead.
  **The goodbye has to be the LAST window's.** This note used to say a new
  stream clearing the flag was enough to keep closing one of two windows
  from putting the survivor on the short fuse; it was not — nothing new
  connects, the closed window's goodbye stood, and when the survivor later
  went quiet the server exited two minutes on. Clearing the flag whenever
  a stream ends with others still open covers a goodbye that arrives
  before its stream's end, but the two arrive in either order (the close
  is noticed on the loop the moment the socket drops; the goodbye is a
  separate request), and a goodbye landing after its own stream has gone
  looks just like the survivor's. So both carry a per-page-load id
  (`?page=` from `connection.js`), and `_presence_close` decides at the
  moment the count reaches zero, by whether the page that just went said
  goodbye; `api_goodbye` is `async` so the presence record is only touched
  from the event loop. A goodbye with no page id (an older client, the
  integration tests' raw requests) keeps the old meaning.
  `tests/test_idle_shutdown.py`.

- **Tests that spawn a real `server.py` must isolate it by env, not
  fixture.** The autouse `isolate_workspace` monkeypatch can't reach a
  subprocess, so a spawned server sees the real `INSTALL_ROOT` and will
  happily register throwaway cases in the developer's actual
  `workspace/cases.json` and drop quicklook files in the real `cases/` —
  state that outlives the test and collides on the next run (found as a
  UI test that passed exactly once). Every `Popen` of `server.py` in the
  suite sets `WINNOW_WORKSPACE_DIR` (read in `workspace.py` at import),
  `WINNOW_ENV_FILE` (the `WINNOW_*` token store `main()` loads and
  Settings → Environment writes — on Windows it is the real
  `HKCU\Environment` otherwise) and, where cases get written,
  `WINNOW_CASES_DIR`; do the same in any new one.

- **The association-default policy lives in the catalogue, and the API
  enforces it.** `winnow/assoc.py`'s `BUILTIN_TYPES` marks which
  extensions may become the DEFAULT app (`default_ok`) versus Open With
  handler only — .txt/.json/.db/.xlsx/.xlsm/.sqlite* have real owners
  (Excel, editors, DB tools) and stealing their double-click is how a
  tool gets uninstalled. Plugin extensions are default-ELIGIBLE but only
  ever via explicit consent — the new-extension launch prompt (fires
  once per extension the first boot after the catalogue grows, only on
  machines with at least one registered type; any answer including "Not
  now" is recorded in assoc_prompted_exts and only new extensions ask
  again) or the panel's own button. Nothing claims a default silently,
  which is what the old blanket handler-only rule stood in for. `/api/assoc/default` refuses
  non-default_ok types with a 400 — the UI not offering the button is
  not enforcement. All `/api/assoc/*` registration routes are
  loopback-only, like `/api/assoc/open`. Two platform honesty rules:
  Windows' hash-protected UserChoice key wins over anything we can
  write, so `make_default` reports the blocked extensions and the UI
  walks the analyst through Open With → Always instead of claiming
  success; on Linux a plugin extension resolves to no MIME type at all
  until our shared-mime-info package supplies the glob, so registering
  one writes `mime/packages/winnow.xml` and best-effort runs
  `update-mime-database`. The `assoc_background` machine pref launches
  associations through pythonw.exe (no console window) — ON by default
  (a console riding along with a double-click is the wrong first
  impression), OFF-able from Settings → Appearance for the day an
  association won't open and the hidden server log suddenly matters;
  False is stored explicitly, since with an on-default a deleted key
  would silently re-enable it. The
  toggle rewrites the ProgId's shared open command in place
  (`refresh_command`), never creating the ProgId on a machine where
  Winnow was never registered. Tests build both adapters against fake
  environments (a dict-backed winreg, tmp XDG dirs) — and any UI test
  touching the panel relies on conftest pointing the spawned server's
  `XDG_DATA_HOME`/`XDG_CONFIG_HOME` at tmp, or a green test would edit
  the developer's real ~/.config/mimeapps.list.

- **A Winnow case is `.db-winnow`, evidence to import is `.db`.** New
  case files (new-case, save-as, quick-looks) take `store.CASE_SUFFIX`
  (`.db-winnow`) so an OS association, a file manager and the launcher
  can tell "a case to OPEN" from "a SQLite database to INGEST as
  evidence" — a Chromium `History.db` and a Winnow case are both
  SQLite, and only the double-click target differs. It is still an
  ordinary SQLite file; the suffix is a label, not a format. The
  extension lives in ONE constant imported everywhere a case file is
  named. Backward compatibility is by design: historic `.db` cases open
  unchanged, because `is_winnow_case_file` and the open path sniff
  CONTENT (SQLite header + the sources/tag_defs/row_tags tables), never
  the extension — only NEW cases take the new suffix, nothing is
  migrated. The same sniff is the gate on every door that names an
  EXISTING path as a case — `/api/case/open`, `POST /api/cases`
  registration, `/api/case/copy_sources`' target, `--case` on the
  command line, and `_open_store` behind all of them — because opening a
  Store is a write (META_SCHEMA, WAL): before the gate covered all four,
  a Chrome `History` registered as a case and then picked in "Copy into
  this case" gained Winnow's tables and changed hash. A path that does
  not exist is a new case and is created as before
  (tests/test_not_a_case_file.py) — except from "Open existing case
  file…", which registers with `must_exist` and is refused ("No case
  file at …") instead: it used to create an empty case at a mistyped
  path and open it, which looks exactly like the analyst's case with
  its tables gone. Creating is + New case's job; `--case FILE` still
  creates a missing case, as documented. In the association catalogue `.db-winnow` is
  `default_ok`, alongside the formats nothing else contests — `.csv`,
  `.tsv`, `.jsonl`, `.ndjson`. What is particular about it is the
  ownership: nothing on the system claims `.db-winnow` at all, so Winnow
  can take its double-click without taking it from anybody, the way
  claiming `.db`/`.xlsx` would. Those, with `.txt`/`.json`/`.sqlite*`/
  `.xlsm`, stay handler-only. The brand icon rides the same association —
  DefaultIcon on the Windows ProgId, the `.desktop` Icon= and a
  hicolor `application-x-winnow-case` mimetype icon on Linux. **An icon
  replaced in place does not show up on its own**: Explorer caches
  association icons until SHCNE_ASSOCCHANGED (every registry mutation
  fires it, and startup re-stamps + pokes when the recorded IconHash
  differs from the .ico on disk), and the Linux theme copy is re-synced
  by content comparison, not only-when-missing — the only-if-missing
  version pinned the first-registered design forever.

- **`subset_rids` is deliberately not in `_WORK_TABLES`.** It only ever
  accompanies a `sources` row (Store.save_view_as_source writes both, and
  `drop_source` removes both), and `sources` is kept out of the sweep's
  definition of work on purpose: every quick-look has one. A subset table
  with nothing tagged or noted on it is a derived copy of rows the case
  already held, so a quick-look holding only that should still be
  reaped after 7 days; the moment the analyst tags or notes a row of it,
  `row_tags`/`row_notes` hold it like any other table's work. Listing the
  map would have made any quick-look that ever saved a subset immortal.

- **`_busy_reason` answers what is running, not just whether anything
  is** — a string an analyst would recognise, or None. Four kinds of
  background work count: a queued or running ingest job, a view-as-table
  copy (`Store.copies_in_flight` — the source it fills has a growing
  `row_count` and `columns='[]'` until it finishes, exactly the
  mid-import problem under another name), a watchlist scan (a writer:
  hits and auto-tags), and a background view build (the analyst's
  pending search). All four hold idle shutdown open — the monitor reads
  this as a boolean — and all four block the same two routes,
  `/api/case/copy_sources` and the quick-look `/api/case/save_as`, both
  of which would either snapshot a half-filled table or close the store
  out from under work that a close cancels.
  The reason it returns a string rather than a bool is the 409 those two
  raise: it used to say "Still importing" whatever was running, so a
  scan or a copy sent the analyst to a jobs panel with no import in it.
  The routes interpolate the cause and add their own advice ("copy again
  when it finishes"). Adding a fifth kind means adding its wording here,
  not just another `or`.

- **With no console, `sys.stdout` and `sys.stderr` are `None`, and uvicorn
  dereferences them before it binds.** Windows' `pythonw.exe` is what a
  file association launches once the background setting is on
  (`assoc.launch_command`, and that setting is the **default** — a console
  riding along with every double-clicked file is the wrong experience).
  It starts a process with no console at all, and CPython sets both
  streams to `None` rather than to something that discards. `print()`
  tolerates that, which is why nothing here noticed for so long;
  `uvicorn.run` does not. It calls `configure_logging()` from `Config`'s
  constructor, and uvicorn's default formatter asks
  `sys.stdout.isatty()` — so the server died with an `AttributeError` on
  the line *before* it would have started listening.

  The symptom was silence. `browser.open_when_ready` was already waiting
  on a daemon thread for a port that would never open, and there was no
  console for the traceback to land in either: double-click a file, and
  nothing happens. It reached us as "launching by file association never
  opens the browser", which is that crash seen from outside.

  `main()` therefore calls `_ensure_console_streams()` before anything
  that might log or print, binding whichever of the two is `None` to
  `os.devnull`. Rebinding the streams rather than handing uvicorn a log
  config that avoids stdout is deliberate: the config fix would leave
  every other stdout consumer — this module's own prints, any plugin's —
  one `None` dereference away from the same silent death.
  `tests/test_no_console_launch.py` pins it, and carries a test that the
  unfixed call really does raise, so a future uvicorn that stops asking
  cannot quietly turn the rest of the file into a tautology.

  **The stream also has to be able to spell what startup prints.** The
  first version opened the null device in the locale encoding — cp1252
  on a typical Windows box — and startup prints "Plugin disabled: …
  (toggle in Settings → Plugins)" for every bundled example, which all
  ship off. `→` is not in cp1252, so the same double-click died the same
  silent death one line later. The null stream is opened UTF-8 with
  `errors="replace"`, and a real stream that is strict in the locale
  encoding (`python server.py > winnow.log`, `PYTHONIOENCODING=cp1252`)
  is reconfigured to `errors="backslashreplace"` — encoding kept, since
  whatever reads it expects that one. That is what Python's own stderr
  does already, which is why log.record (stderr) only broke through the
  null stream. Anything new that opens a text stream to print through
  needs an explicit encoding for the same reason.

- **A console that stops taking output used to stop Winnow.** Reported as
  "launching in PowerShell with QuickEdit enabled can freeze it". With
  QuickEdit on (the Windows default), one click in the console window
  opens a selection, and until it ends (Esc, Enter, a right-click) the
  console accepts no output: every write to it blocks inside
  WriteConsole. Edit ▸ Mark, the Pause key, Ctrl+S in a Unix terminal and
  a pipe nobody reads do the same. Every `log.record` entry prints to
  stderr from whichever thread logged it, and some of those threads are
  the event loop (the catch-all exception handler, `_idle_monitor`,
  uvicorn's own warnings) — so the next line Winnow said froze whatever
  said it, and a frozen event loop is every request frozen. Measured with
  an unread pipe: the browser's own `/api/log/client` route hung on its
  ~130th line.

  `main()` calls `console.install()` straight after
  `_ensure_console_streams()`, which puts a queue in front of each std
  stream that can stall (`console.can_stall`: anything with a descriptor
  that is not a regular file — and not the null stream, which can't).
  One daemon thread does every real write, in order, flushing after each,
  so a stalled console stalls that thread and nothing else; the queue is
  bounded (`console.LIMIT`) and says how many lines it skipped once the
  console takes output again. Three things lean on the order of
  `main()`: install runs before `uvicorn.Config`, whose
  `configure_logging()` resolves `ext://sys.stderr` there (built the other
  way round, uvicorn's handlers hold the real stream); a handler that
  captured `sys.stderr` at import time would skip the queue the same way;
  and anything that writes straight to the descriptor (`os.write(2, …)`,
  `sys.stderr.buffer`) skips it by construction.

  Exit is the other half. The writer is a daemon thread, so `_at_exit`
  drains for `EXIT_GRACE_S` first — `main()` prints a refusal and calls
  `sys.exit(1)` straight after, and that line must not die in the queue.
  It leaves the queued streams installed even then: interpreter exit
  flushes `sys.stdout`, and with the real stream there that flush waits
  on the writer stuck inside it, so a console still stalled at exit
  would hang the process (`test_handing_the_real_streams_back_at_exit_would_hang_it`
  shows it). `tests/test_console_stall.py` covers both halves against a
  spawned server with `ServerLog.pause()` as the stalled console, and
  runs those with buffered streams (`PYTHONUNBUFFERED` empty), as a
  Windows console's are — unbuffered, the lock exit trips over isn't there.

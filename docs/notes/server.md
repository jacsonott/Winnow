# Server: HTTP layer, case lifecycle, safety rails

`server.py` — routes, middleware, and the handful of guards that exist
because this is a local single-analyst tool with no auth.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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
- There's no auth — this is a local, single-analyst tool. The one guard against
  a malicious page on another tab silently triggering side effects (e.g. via an
  unpreflighted `multipart/form-data` upload) is server.py's
  `require_client_header` middleware, which 403s any non-GET `/api/*` request
  missing an `X-Timeline-Lite-Client` header. `app.js`'s `api()` sets it on
  every non-GET call automatically — a raw `fetch()` that bypasses `api()`/
  `post()` won't have it and will get 403'd. GET stays exempt on purpose (the
  Export/session/filters download links are plain navigations, which can't set
  custom headers at all).
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
  hostile page in another tab from turning the server off.

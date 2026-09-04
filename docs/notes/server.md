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
- `api_view` maps only `ValueError`/`KeyError` to 400; everything else is a
  500. It used to catch bare `Exception`, so an internal defect surfaced as
  "Filter error: …" — blaming the analyst's filter for something they can't
  fix and hiding the traceback. `core.js`'s `api()` attaches `err.status` so
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

- **Idle shutdown rides the presence stream, not request traffic.** An
  analyst reading the grid makes no requests for hours, so "last request
  time" would reap a live session; instead every page holds an EventSource
  open to `/api/presence` and the count of live streams is the signal.
  Three holds stop it destroying work: open streams, in-flight HTTP
  (counted in the no-cache middleware, `/api/presence` itself excluded or
  it would read busy forever — a CSV download outliving its tab is exactly
  what this hold protects), and running/queued ingest jobs. A server
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
  old file reopens, never leaving the server storeless). Quick-looks stay
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
  `_presence_open()` clears the flag, so closing one of two windows
  cannot put the survivor on the short fuse.

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
  migrated. In the association catalogue `.db-winnow` is the one builtin
  type that is `default_ok`: nothing else owns it, so making Winnow its
  default can't steal a file from Excel or a DB browser the way claiming
  `.db`/`.xlsx` would. The brand icon rides the same association —
  DefaultIcon on the Windows ProgId, the `.desktop` Icon= and a
  hicolor `application-x-winnow-case` mimetype icon on Linux. **An icon
  replaced in place does not show up on its own**: Explorer caches
  association icons until SHCNE_ASSOCCHANGED (every registry mutation
  fires it, and startup re-stamps + pokes when the recorded IconHash
  differs from the .ico on disk), and the Linux theme copy is re-synced
  by content comparison, not only-when-missing — the only-if-missing
  version pinned the first-registered design forever.

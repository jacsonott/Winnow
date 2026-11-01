# The test suite: what it covers, and working on it

What each file under `tests/` covers and why its load-bearing tests are
the ones they are, then the traps that bite when working on the suite
itself, most of them learned working a batch of PRs at once. How the
suite is laid out, run and sharded is in CLAUDE.md's Testing section.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

## What the suite covers

Coverage is organized one file per concern (`test_ingest.py`,
`test_views.py`, `test_search.py`, `test_grouping.py`, ...) and is weighted
toward CLAUDE.md's invariants and the traps in [these notes](README.md) — numeric
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
`test_ingest_contracts.py` holds every reader (CSV, text, JSON, IIS,
key=value, XLSX, Plaso, SQLite) to the three promises ingest_csv set,
since each carries its own copy of the code that keeps them: every row
lands in order across batches (BATCH is shrunk so a 30-row fixture spans
five), a cancel leaves no table, and a failure raised from the reader
part-way keeps what was read, says how many, and leaves no empty table
when nothing was. Adding a reader means adding it to `READERS` there.
`test_group_export.py` holds the export of an expanded small group (its
own code path, `group_virtual`) to the rows the grid pages for it and,
byte for byte, to the same rows exported through a filter or a
materialised group. `test_merge_advanced_search.py` is Advanced search's
merge case (invariant 9), with members indexed, not, and mixed.
`test_wiki_shortcuts.py` holds the shortcut table in
docs/wiki/Settings-and-shortcuts.md to keymap.js's `DEFAULT_KEYMAP` and
`ACTION_LABELS`, read with esprima, so a relabelled action or a new
default key fails the backend job instead of leaving the manual
describing an older build (the table menu's label had drifted when it
was added). The same file holds the page's Fixed keys tables to
`FIXED_KEYS`, group by group and row by row, matched on the keys each row
names: Settings had been missing the Timeline's R / r, which the manual
already listed. `test_wiki_skins.py` does the same for the skin table, the
order and each description against settings.js's `STYLES` (the manual
had added "The default." to Harvest's card text). `test_updater.py` drives `update.py` itself as well as
updater.py: `--from` then `--rollback` as an analyst types them, the
answers to a refused or unusable archive, and `download()`'s promise
that a cut-off or Ctrl+C'd download leaves nothing at the target that
`--from` could later apply.
`test_derived.py` covers the column lifecycle and, more importantly, the
integration choices that are easy to reverse by accident: the
virtual-root EXPLAIN plan (invariant #2), that derived values are *not*
searchable, that merge eligibility is unaffected, and both cancel
semantics (driven through the backfill's own cancel hook, so the
cancellation point is deterministic rather than a race — same technique
as `test_ingest_jobs.py`).
`test_esxi_log_shapes.py` covers the esxi_logs plugin's parser one line
shape at a time (ESXi 5.x to 8.x hostd, vmkernel, vobd, shell, rhttpproxy,
Linux RFC 3164, RFC 3339 and RFC 5424), its iisread-style column rules (the
union of Originator keys, a key that names a fixed column suffixed,
columns only where a file has them), that its canonical timestamps sort in
time order, and the file handling it shares with iisread (NUL gaps, a `\r`
that can't forge a row, Windows-1252, a live file). Its load-bearing test
puts every combination of stamp, header and body it knows through both
the fast-path regexes and `_parse_line_slow` and requires one answer: the
fast paths are only a speed-up if they never disagree, and a disagreement
shows up nowhere else. `test_esxi_log_scan.py` covers the same plugin's
folder scan, continuation folding and syslog year.
`test_site_palette.py` holds the Pages site and Harvest to the same
colors: every value docs/site/assets/site.css copies from a Harvest token
(the ones commented `/* dark --panel */` and so on) must still equal that
token in static/style.css, and Harvest dark's greys must have no hue. The
site took the green cast out of Harvest's greys before the app did, and
the two drifted apart until the app read as green next to its own
website.
`test_plugin_registration_errors.py` gives each refusal in
register_ingest_format, register_tab, register_row_action and
register_toolbar_panel the plugin that earns it, and checks the exact
message Settings → Plugins shows. Each plugin registers one of every kind
first, so every case also proves the rollback: a register() that fails
keeps nothing. `test_case_open_failure.py` opens a case that will not open
while another is open. The open case must stay open and answer, and the
failed Store must give back its case lock, heartbeat thread, connection
and views file. Before that, the second try at the same file was refused
as "in use" by this same Winnow (docs/notes/store.md).

In the browser, `tests/ui/test_default_keys.py` presses every binding in
`DEFAULT_KEYMAP`. Its first test swaps each `ACTION_HANDLERS` entry for a
recorder and checks every key reaches its own action and no other. It reads
the keymap from the page, so a key added later is pressed without anyone
adding it, and nothing the keys would do happens. The rest press, for real,
the keys whose handlers have no button or menu entry (PageDown/PageUp, `0`,
`q` `w` `[` `]`, `f` `F`, the backtick, `x`, `L`, `Q`, `.`) and assert what
changes on screen. `test_plugin_install_dialog.py` drives Install a
plugin… through its two picker buttons and answers the install route itself
(a real install writes into the shared server's plugins folder, and nothing
takes a plugin back out). A routed request carries a file part's name but
not its bytes, so it asserts names and paths, not contents.
`test_row_action_menu.py` runs a plugin row action from the row menu (one
row, a range, a merge, `open_url`, a refusal) against a routed dispatch.
`test_detail_malformed_xml_ui.py` covers the detail pane's fallback for XML
that won't parse, through `renderDetailContent`: reflowed, highlighted,
nothing lost, no path offered, and the value never built as markup.
`test_popout.py` brings a pop-out back as a tab (and keeps what was typed
out there), and opens a pop-out with no case, and one of a page the case
doesn't have, through `window.open('')` so the window may close itself.
`test_skin_contrast.py` takes the WCAG contrast ratio of text on its
background in every skin and theme, resolved in the page (tokens can be
`color-mix()` or carry alpha). `--text` is held to 4.5:1 on every surface
a row or panel can have, and so is a primary button's label in the light
themes. The accent as text is held to 4:1 dark and 2.4:1 light (Blueprint
light's orange sets that; Phosphor light's green was 1.3:1), and each
default tag color to a floor on the surfaces a stripe and a chip sit on,
Suspicious's at 3:1. The other pairs (`--dim`, danger, diff and SQL
colors) are held to floors today's skins meet, some of them below AA in
light themes, so a skin can't get less readable without a test noticing.

`test_csv_round_trip.py` ingests a file of awkward cells, exports it over
`/api/export` and ingests the export: everything comes back except the
formula guard's apostrophe. Its last test forces `_CsvWriter`'s Python 3.10
path on every Python and, on 3.11+, checks it against what `csv` writes.
`test_firstlast_filters.py` runs each of First/Last's filter operators and
the grid's equivalent condition on the same table and on a merge, and
compares the counts per group, because the plugin compiles its own WHERE.
Its table has both '' and NULL, and a number column with a blank and a
non-number in it. `test_session_reimport.py` re-imports a table from a
session for every reader the session can name (IIS and key=value are in
their own files), with and without the reader recorded, and covers a
session with no path, a file gone from its path, and a file replaced while
it was read. Beside the `--from` tests above, `test_updater.py`'s
`ReleaseCli` runs `update.py` on the release path, with GitHub answered
through updater's own seams: `--check`, up to date, `--download-only`
then `--from` (the airgap trip), the apply prompt, `--dry-run`, and each
failure as exit 2.

The smaller UI paths that had never run in a browser have one file each.
`test_derived_unparsed_and_path_ui.py`: a derived column's "Show N
unparsed rows" (and an extracted field's "rows without this field") opens
exactly the failures, on a merge from every member, and "Change the field
path…" recomputes the column in place. `test_settings_density_cap.py`:
Row density through Settings keeps the top row in place, grouped or not,
and the autofit width limit clamps what is typed and stores "No limit" as
0. `test_saved_filters_rows.py` adds Rename… and Edit… on a saved filter.
`test_dashboard_manage_ui.py`: renaming a board from the sidebar or its
title, deleting it from the sidebar's ✕ (the next board, or the grid when
it was the last), and Save as profile…, which carries variable
definitions and never their values. `test_home_drop.py`: files dropped on
a case row go into that case and nowhere else, dropped off the rows they
start a quick look; each on its own server, since both switch the case.
`test_pivot_export_filter.py`: the pivot's Copy and CSV (formula guard
included) and its field filter. `test_firstlast_copy.py`: Copy result,
including the card it waits on when the analyst has left the tab, held
at `job/start` so the answer lands while the tab is hidden.

## Things that bite

- A note on the lock-scope test in `test_search.py`: it asserts *structurally*
  (a `_CountingLock` counting how many times the lock goes fully unheld)
  rather than by racing a competing thread, because a racing thread also
  passes against the broken implementation — it can grab the lock in the
  window before the sweep ever takes it. If you write another concurrency
  test here, check it actually fails against the shape it's meant to catch.

- **A browser test that only fails on a loaded runner may be a real race
  in the app.** test_view_state.py's scroll wait timed out now and then
  in CI for days and was waved through as pre-existing. It was a
  product bug: a second, stray view build landing over the restored one
  (see "Coming back to a table" in [ui.md](ui.md)). Before loosening a
  wait, reproduce the failure: run the test on repeat with every core
  busy (`python3 -c "while True: pass" &`, one per core), which turned
  a rare CI failure into roughly one run in eight, and record what each
  build and landing did in a `window` array to read back on failure.

- **Two imports queued together finish in either order**, so the tables'
  ids come out in either order too. A test that compares sorted pairs of
  `(source_id, rid)` against a list written in name order passes only
  when the first-named file wins (test_row_action_menu.py did, about half
  the time): sort the expected side as well, or key it by name.

- **A click that closes its own window can raise after it worked.** The
  window can be gone before Playwright hears the click land, and it
  reports a closed target as an error. Wrap that click in
  `expect_event("close")`, which is the real check, and swallow only a
  "closed" error from the click itself (`_dock` in test_popout.py;
  `test_a_pop_out_with_no_case_open_says_so` lacked it and failed now
  and then).

- **Home shows the case list it already has, then refreshes it.** The
  Home button paints the rows from the last fetch at once and replaces
  them when `/api/cases` answers, so rows are on screen before they are
  current. Waiting for `.home-row` waits for the old ones. Wait on
  `__winnow.S.cases` saying what the test needs (the case just left is
  `open`) before opening a row's menu (test_home_screen.py's delete test
  missed that note on a loaded runner).

- **`pytest … | tail` reports tail's exit status, not pytest's.** A
  pipeline's status is its last command's, so `pytest -q | tail -5 && git
  commit …` commits on a red suite, and a script that gates on it passes
  whatever pytest said. Write the output to a file and read the end of it
  after checking pytest's own status, or run under `set -o pipefail`.

- **With several worktrees open, `cd` explicitly in every command.** A
  shell that keeps its directory between commands, as an agent's does,
  runs the next `pytest` or `git commit` wherever the last command left
  it, which is easily another branch's worktree. The run is green for the
  wrong code, or the commit lands on the wrong branch. Name the directory
  in each command (`cd /path/to/wt && …`, `git -C /path/to/wt …`) rather
  than relying on where the shell happens to be.

- **A change two branches both need merges clean if the hunk is
  byte-identical.** When two PRs in a batch each need the same small
  addition (a shared CSS rule, a conftest fixture), make it the same
  bytes in both: git takes an identical change on both sides without a
  conflict, so whichever PR merges second still merges clean. A
  difference anywhere in the hunk, a comment or a blank line, turns it
  back into a conflict.

- **Every module in a browser shard shares one server and one case.**
  What a module leaves in the case (a table, a tag, a saved session, a
  plugin switched on) is the next module's starting state, and the
  failure shows up in whichever unrelated module the shard deals next,
  only in a full run (#227). `case_left_as_found` (tests/ui/conftest.py)
  fails the module that leaves the case changed, at its own teardown.
  Clean up in a `finally`, by id or name rather than by repeating the
  gesture under test, and let the `api` fixture raise on a non-2xx
  answer rather than catching it: a cleanup call that 404'd and was
  swallowed is how that issue went unnoticed.

- **A debounced save outlives the test that made it.** A module-scoped
  page lives across tests, so a write the app puts off (tabState's
  half-second debounce, a note's autosave) goes out in whichever test is
  running by then. Start fresh in test_firstlast_state.py leaves one,
  since the empty sheet it puts back saves itself as nothing, and the
  read-failure test after it wrote its row through the API before
  reloading. Depending on how long the tests between took, that save
  landed over the row or in the test's own watch for writes, failing
  the shard on develop now and then. Write the server-side state a test
  depends on after the reload, which kills a timer still waiting. And a
  check that a debounced write did NOT happen waits past the debounce:
  a fast round trip can end before it, so the check looks too early.

- **A click that lands while its target is being re-rendered fires
  nothing.** Not a refusal: the click hits a node that is being replaced,
  so no handler runs and no request goes out, and the test times out
  waiting for an answer to a question it never asked. It shows up on
  loaded CI runners. Where a gesture re-renders the control the next
  gesture clicks, retry the click until the request it should send is
  seen, inside `page.expect_response`, and assert on that response.
  `_click_add` in tests/ui/test_plugin_dashboard_offered.py is the
  pattern.

- **Polling a server that is starting on a listener it was handed can
  time out rather than be refused.** The socket is listening the whole
  time, so a request connects at once and waits in the backlog until the
  new process serves; a start slower than the request's timeout raises
  `TimeoutError`, which is neither a `URLError` nor a `ConnectionError`.
  test_restart.py's Windows-style relaunch caught only those two, and
  failed when a Windows runner took more than two seconds to start
  (#495's CI). A poll for "up yet?" treats a timeout as "not yet". A
  child that sleeps in a `sitecustomize.py` on its `PYTHONPATH` makes a
  slow start on demand.

- **A toast can be gone before the next frame.** `wait_for_function`
  looks once a frame, and the app replaces toasts freely: after "Change
  the field path…" the "Recomputing" toast lasted 12ms before the jobs
  poll announced the derive that had already finished, so a wait on the
  toast's text passed locally and failed in CI. Where another toast can
  follow, record them with a MutationObserver before the gesture and wait
  on the record (`RECORD_TOASTS` in
  tests/ui/test_derived_unparsed_and_path_ui.py).

- **`wait_for_function` does not await its predicate.** One that returns
  `fetch(…).then(…)` hands back a Promise, which is truthy, so the wait
  returns at once and the test races what it meant to wait for.
  tests/test_ui_test_hygiene.py fails on one; poll from Python with the
  `until` fixture instead, since `page.evaluate` does await.

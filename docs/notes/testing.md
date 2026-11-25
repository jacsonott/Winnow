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
`test_timestamp_sort.py` holds a recognized datetime column to time order
in each shape the import types as datetime (US dates, month names), to
the order its derived twin sorts in, and across a merge of an ISO table
and a US one, filtered and not and in the merge's SQL pane query; and
holds an ISO column to the plain text
sort its import index serves, since that is the fast path the change
must not cost.
`test_timestamp_separators.py` holds TS_NORMALIZE, DAY_BUCKET and
`parse_datetime_lenient` to the time after every separator a tool writes
between date and time ("@", "at", " - ", a comma, extra spaces, a
non-breaking space, a one-digit hour), and the near misses
("2026-06-23 -05:00") to their date alone; then sort, the timeframe
filter and the histogram on such a column. Its browser half,
`tests/ui/test_timestamp_time_of_day.py`, reads the painted cells
(00:00:00 on every row was the report) and runs one corpus through
`parseTimestamp` and `_ts_normalize` side by side.
`test_timestamp_dmy.py` does the same for the day-first dashed shape
(dd-mm-yyyy): import typing, TS_NORMALIZE and DAY_BUCKET, the near misses
(day 32, month 13, a month-first column written with dashes), sort,
timeframe, histogram, the `dmy_datetime` derived op, and that tsformat.js's
`TS_FORMATS` and `workspace.AppSettings.TS_FORMATS` list the same display
formats. `tests/ui/test_timestamp_dmy_ui.py` is its browser half: the grid's
first row, `parseTimestamp` against `_ts_normalize`, and the case set to
DD-MM-YYYY HH:MM:SS.
`test_number_columns.py` holds each shape of "imported numbers sort as
text" to a number column that sorts by value, through the reader it came
in by: a "-" or "N/A" among the numbers, a column empty for the first 500
rows (a FortiGate log's traffic keys, an IIS log's second `#Fields:`
line, a late JSON key), padded, signed and exponent numbers. Its
load-bearing tests are the ones that keep the fix from overreaching: a
column found to hold timestamps further in is NOT made a datetime column,
and the open-time pass over older tables leaves alone everything that was
declared text rather than inferred (the analyst's preview choice, a
key=value `tz`, a plugin's table, a column the old rule itself called a
number). Plus that every cell NUM_RE accepts is worth what CAST reads it
as, since `_numeric_expr` gates on it.
`test_maintenance.py` covers the things that only exist because everything
else only ever grows: the auto-created column indexes (listing/dropping),
`compact()`, and `sweep_orphan_views()` — whose load-bearing test is that a
*live* Store's views database survives a sweep, since that's the assumption
that makes deleting another process's file safe at all. Beside it, the
test that the lock is on the `-lock` sidecar and not the database: on
macOS a flock on the database stopped every case opening, which no Linux
run can reproduce, so on Linux the test can only check where the lock
sits (docs/notes/store.md). `test_ingest_jobs.py` covers the
background ingest jobs (lifecycle, byte-progress, the queued-cancel path,
cancel-drops-the-partial-source — driven through `ingest_csv`'s own
`cancel` hook so the cancellation point is deterministic, not a race —
multi-table sqlite jobs, spool deletion, close-with-running-job).
`test_disk_full.py` covers a full disk (winnow/diskspace.py): each
reader's job saying "Out of disk space" in one line (the emptiest drive
and what became of the import, rather than a line number), logging the
whole account (every drive, what to do, the raw error), and leaving no
partial table unless removing it failed too, which it then says; the 507 for an upload whose
temp folder filled while FastAPI was still parsing it (its load-bearing
test: that one used to be a bare "There was an error parsing the body",
logged nowhere); and that an HTTPException a route worded itself is left
alone. The disk is faked — a commit raising SQLite's own SQLITE_FULL
message, a write raising ENOSPC — because CI cannot mount a filesystem
small enough to fill; the messages asserted are the ones a real full
tmpfs produced. tests/ui/test_disk_full_ui.py holds the panel to keeping
a failed upload's row, the "out of space" badge, and the sidebar listing
the tables a failed import kept.
`test_import_into_folder.py` covers choosing a table folder at import
(`folder_id` on every route, `--folder`): its load-bearing tests are the
stale id refused before anything is read (no job, no table), and the
folder deleted mid-import leaving the import done and the mirrored
subfolder at the top level rather than under a parent nothing lists;
tests/ui/test_import_into_folder_ui.py drives both import windows.
`test_case_group_folders.py` covers a case group's folders: its
load-bearing tests are that a later case, or the first case leaving,
doesn't move the first case's folder, that removing every folder lets the
next case set one while removing the oldest case's derived one doesn't
bring it back, and that the registry's other writes keep the folders;
tests/ui/test_group_folder_ui.py drives + New case (the group's folders
offered under the path, and Add this folder to), the pinned folders in
Add from this machine…, and Add folder…/Start new cases here/Remove on
the home screen.
`test_cancel_op.py` covers cancellable builds, using a
catastrophic-backtracking regex filter as a reliably-slow build to
interrupt; its key regression assert is that the *previous* view still
pages after a cancelled rebuild. `test_concurrency.py` pins the
reader-pool split (invariant #4) structurally: each test holds the writer
lock for the *entire duration* of a read and asserts the read completes —
a deterministic deadlock against a single-connection implementation, not a
race — plus the dropped-view → KeyError mapping, pool recycling/drain on
close, and read-your-committed-writes across the connection split.
`test_column_values_spec.py` covers the value picker's This view list for
a column that has picks of its own (`column_values(spec=...)`, POST
/api/column_values). Its load-bearing test holds the counts to
`group_summary` over a view built from the same spec, for each kind of
narrowing: column filters, the filter tree and raw SQL in it, each search
mode, tags (any of, untagged, excluded), the timeframe, Hide empty rows,
and Hide duplicate rows with and without a tag filter after it, on a
table and on a merge. Each case also asserts that the list did narrow,
because a narrowing the store ignored would match the whole table on both
sides. `tests/ui/test_value_picker_other_filters.py` is its browser half:
the reported sequence (pick User, then Host, reopen User), the reverse,
the search box, ticking every value clearing the column's picks, and
Whole table one click away.
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
lateral_movement's edge aggregation, First/Last and Pivot ordering a
US-format timestamp column by time (`US_TIMES`: text order puts
`1/2/2026` before `12/30/2025` and `9:01 AM` after `10:05 AM`, with a
blank and a `-` that must win neither end), and claude_assistant against a fake
`anthropic` module injected into sys.modules (asserts the request shape —
model, fallbacks, the schema cache breakpoint — with no network). `test_sql_tabs.py` covers the SQL
pane's sub-tabs, including that they actually survive closing and
reopening the case file (the whole reason they live there rather than in
`localStorage`) and that `reorder` keeps tabs it wasn't given.
`test_sql_history.py` covers the SQL pane's query history the same way:
one row per distinct text with a re-run moved to the top and counted, a
failed run's error kept without counts, the cap dropping the oldest run,
ids never reused, surviving a reopen, and a case holding only history
counting as work for the quick-look sweep; `tests/ui/test_sql_history_ui.py`
holds the pane's half, including that a superseded run is not recorded,
that opening an entry over a draft asks first (with the focus on the safe
answer), that an entry's New tab button opens it in a tab of its own, and
that the panel spans the SQL page.
`test_timeparse.py` covers every timestamp operation as pure functions —
epoch auto-ranging boundaries, hex-vs-decimal FILETIME, the syslog year
rollover (including across a batch boundary and Feb 29 in a non-leap
year), offset-to-UTC conversion — plus the guarantee that every parser's
output is accepted by `_TS_ISO_RE`/`TS_NORMALIZE`/`DAY_BUCKET`, which is
what makes derived columns need no new regexes on either side. A mutation
pass found three gaps the happy paths left: all twelve month names are now
read by name through every month-name shape (March, May and August could
have said any number), the parts a value leaves out (seconds, a fraction
shorter than six digits, a bare date for the delta reader) are read as
zero, and `detect()` is checked against registry order with samples where
the two disagree.
`test_custom_timestamp_formats.py` covers custom timestamp patterns end
to end: how each letter is read (the casual "dd-mm-yyyy hh:mm:ss" as
day-month-year 24-hour, strftime, run-together digits, optional
fractions, zones), the values a pattern must refuse rather than read
wrong (a time after a date-only pattern, an AM/PM it doesn't read, digits
past its end), each compile error's message, saved formats in
`workspace/` (unique names, a hand-broken record skipped and kept),
detection ranking a saved format ahead of a built-in on a tie and
holding a digits-only pattern to plausible years, the column on a table,
a merge and through a session round trip, and the routes. Its browser
half, `tests/ui/test_custom_timestamp_formats_ui.py`, writes a pattern in
the dialog and saves it, sees detection pick the saved format on the
next open, and adds, edits and deletes one in Settings → Timestamps; it
deletes every saved format in a `finally`, because they live in the
shared server's workspace where `case_left_as_found` doesn't look, and a
leftover one would change every later module's format detection.
`test_tag_undo.py` covers the undo stack, and its load-bearing test is the
one that tags an overlapping selection and asserts the *pre-existing*
assignments survive the undo — a naive re-send-inverted implementation
passes every other test in that file and fails only that one.
`test_structparse.py` covers the JSON/XML path syntaxes and extraction as
pure functions (same shape as `test_timeparse.py`), including the round-trip
property that makes paths safe to write into a column definition, the
EVTX `[@Name='…']` predicate form, and the DOCTYPE refusal. Discovery's
two caps (`MAX_WALK_DEPTH`, `MAX_PATHS`) are tested at their boundaries, and
a raw line break inside a JSON string is tested on a document longer than
`LOOSE_MAX_CHARS`: a short one would be read by the loose parser anyway,
so only a long one shows `json.loads(strict=False)` doing its job.
`test_rxguard.py` holds the catastrophic-regex guard to a corpus of
verdicts plus a named case per fallback rule. Atomic groups and possessive
repeats (Python 3.11+) have cases of their own, skipped below 3.11 where
neither compiles: the guard has to look inside both, for a blow-up and for
the first character that tells alternatives apart.
`test_derived_extract.py` covers the store-level integration: that an
extracted column is an ordinary derived column everywhere, that discovery
only ever offers paths that actually extract, and that a batch is
all-or-nothing (a name collision in the fifth spec must not leave four
columns behind).
`test_ingest_contracts.py` holds every reader (CSV, text, JSON, IIS,
key=value, body file, secure log, XLSX, Plaso, SQLite) to the three promises ingest_csv set,
since each carries its own copy of the code that keeps them: every row
lands in order across batches (BATCH is shrunk so a 30-row fixture spans
five), a cancel leaves no table, and a failure raised from the reader
part-way keeps what was read, says how many, and leaves no empty table
when nothing was. Adding a reader means adding it to `READERS` there.
`test_group_export.py` holds the export of an expanded small group (its
own code path, `group_virtual`) to the rows the grid pages for it and,
byte for byte, to the same rows exported through a filter or a
materialised group. `test_export_formats.py` reads every format the
current view exports to (TSV, Excel, JSON Lines, JSON, Markdown, HTML)
back into rows and holds it, cell for cell, to the CSV of the same
filtered, sorted view, tagged-only too, through all three view readers
(root_virtual, group_virtual, materialised) and on a merge; then what each
format does with a value: the formula guard on exactly CSV, TSV and Excel,
JSON's deduplicated keys (a table's own Note is Note_1), Markdown's
escaping, HTML's escaping and its refusal of a tag colour that isn't hex,
and the route's media types, file names, 400/409 and log row counts.
The same file holds every format to a chosen column list (a hidden column
in, both Notes out, Line last), on a merge, on a file of Winnow's columns
alone through all three readers, and refuses a list it can't honor.
`tests/ui/test_export_view_formats_ui.py` presses each format's button on
a filtered, reverse-sorted table and reads the download; then unticks,
ticks, drags and ⤒s in the column list and reads that back, and checks
the grid's layout is untouched and the next dialog starts from it.
`test_merge_advanced_search.py` is Advanced search's
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
`test_derived_merge_partial.py` is the report that a coalesce on a merge
of EvtxECmd's TimeCreated and MFTECmd's Created0x10 was refused because
each column is on one table only: the coalesce, its preview reading
both tables, the column typed datetime and sorting, filtering and
charting by time on the merge; then that it was never about timestamps
(a text coalesce, a one-column op and the batch route over a column one
table has), re-derive on the merge and on a member, the "Show N unparsed
rows" fragment on a member that lacks an input, a session carrying the
inputs a table lacks (and still refusing one it does not vouch for), and
the canonical-timestamp typing rule case by case.
`test_derived_coalesce_timestamps.py` is the report that followed it:
the two timestamp columns showed alike in the grid but stored their times
in different shapes, and the coalesce came out text with both shapes as
stored. A merge of three shapes (EvtxECmd's, ISO with a Z, US with a
12-hour clock, whose 2023 stamp sorts last as text) sorted, filtered and
charted by time; the merge preview judging each table on its own inputs;
one table's two shapes, an offset applied; a value that isn't a time
passed over and a row with none counted; a text input keeping the copy; a
Parse-a-timestamp column counting as datetime; a re-derive rewriting an
old text column; the plain merge of same-named columns in two shapes,
which was always datetime; and `canonical_timestamp` shape by shape,
including what it refuses. `tests/ui/test_coalesce_timestamp_shapes_ui.py`
asserts the symptom in the grid: each row of the coalesced column shows
the text its own table's column shows, and sorting by it puts the US row
between the two ISO ones.
`test_merge_partial_derived.py` is the report that derived columns were
missing from a merge: a column derived on only some of a merge's tables,
with the FIRST member lacking it so nothing leans on members[0], run
through the column list (both modes, the merge builder's preview, a
column derived after the merge), cells, filters, a raw fragment, sort,
timeframe and histogram, grouping in both view shapes, the value list,
tagging, CSV export, the SQL pane, save-as-table, a derive on the merge
chained off it, re-derive, rename and remove on the merge, the unparsed
fragment (exact, and refused where it can't be), and that no index is
built on a table without the column.
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
`test_secure_log.py` covers the Linux secure / auth.log reader: the name
rule, header splitting (no host, ESXi's severity, RFC 3339 offsets to
UTC), `classify` one message shape at a time, and the year rule. Its
load-bearing tests are the attacker-chosen user names (a name holding
`from 10.0.0.1 port 22` must not move Source IP; sshd's tail decides)
and the year: a log across New Year dated from the file's mtime, a year
the analyst sets, and a session re-import that keeps the recorded year
after the file's mtime has moved. It also pins the plugin precedence
that sets this reader apart from the other built-in names: a folder scan
and an open-with both hand `secure` to a plugin that claims it.
`tests/ui/test_secure_queue.py` holds `secureLogNamed` to `name_says`
over one list, then drives the queue: a path item and an upload previewed
and imported in the year the pane shows (the upload dated from the
browser's `lastModified`, not the spool), and **Read as** → **Secure log**
on other text.
`test_systemd_journal.py` covers the systemd_journal plugin. Its fixtures
in `tests/data/journal/` were written by systemd's own tools, not by the
reader under test: `make_fixtures.py` feeds a fixed set of entries to
`systemd-journal-remote` in three file shapes (compact + zstd, regular +
LZ4, regular + XZ) and has `journalctl` print each one in every lossless
form, so the load-bearing tests compare the reader's rows for a journal
file against journalctl's own `-o export` of that file, and every output
form against the file. Regenerate them on a Linux box with systemd 252 or
later (compact files need it) and the `zstd` CLI; they are committed
xz-compressed, trimmed to their last object. The damage tests cut, punch
holes in and corrupt copies of those files in `tmp_path` and assert both
the rows that survive and the exact case-log sentence. The zstd tests run
the pure-Python decoder over streams the reference encoder wrote at
several levels, and against the standard library's on Python 3.14, which
is the only interpreter where that test runs rather than skips.
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
a row or panel can have, and so is a filled accent's label (`--accent-fg`
on `--accent-fill`) in every skin. The accent as text is held to 4.5:1
dark and 3.9:1 light (Blueprint light's orange was 2.4:1 and Phosphor
light's green 1.3:1), and each default tag color to a floor on the
surfaces a stripe and a chip sit on, TA's and Suspicious's at 3:1. The
other pairs (`--dim`, a pressed toggle's label on `--accent-dim`, danger,
diff and SQL colors) are held to floors today's skins meet, some of them
below AA, so a skin can't get less readable without a test noticing. One
check there reads `style.css` instead of the page: no rule paints a solid
`--accent` background, since a label on it would sit on a color the
ratios never measure in the two skins whose fill is not their text color.

`test_csv_round_trip.py` ingests a file of awkward cells, exports it over
`/api/export` and ingests the export: everything comes back except the
formula guard's apostrophe. Its last test forces `_CsvWriter`'s Python 3.10
path on every Python and, on 3.11+, checks it against what `csv` writes.
`test_firstlast_filters.py` runs each of First/Last's filter operators and
the grid's equivalent condition on the same table and on a merge, and
compares the counts per group, because the plugin compiles its own WHERE.
Its table has both '' and NULL, and a number column with a blank and a
non-number in it. `test_pivot_timeframe.py` does the same for the
Pivot's own timeframe: every range through the pivot and through
`build_view`'s `time_range`, on a table with two datetime columns (one
timestomped), the US shape, a 'T' with a zone, an empty cell and a bound
on a value, and on a merge with a member lacking the chosen column and one
with no datetime column at all; plus what it refuses and the dialog's
`/timeframe` route. `test_session_reimport.py` re-imports a table from a
session for every reader the session can name (IIS, key=value, login
records and body files are in their own files), with and without the reader recorded, and covers a
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
included) and its field filter. `test_pivot_reorder.py`: reordering the
pivot's areas with a real pointer drag (the slot line, drops between
chips), the ⠿ handle's menu and Alt+↑/↓; its drags end on two small moves
because the emulated drag sends dragover a move behind the pointer.
`test_pivot_timeframe_dialog.py`: the pivot's own timeframe — the dialog sends
the range as TS_NORMALIZE writes it, refuses text that is not a time with
the dialog still open, the chip turns off and back on and clears, the
drill-down carries the range, and it is saved and restored with the pivot
(its backend twin, tests/test_pivot_timeframe.py, counts every range
through the pivot and through build_view, merges included).
`test_pivot_to_grid.py` adds the hand-over: a cell counted under the
pivot's timeframe opens the grid with the tables' timeframe set to it.
`test_firstlast_copy.py`: Copy result,
including the card it waits on when the analyst has left the tab, held
at `job/start` so the answer lands while the tab is hidden.
`test_plugin_timestamp_format.py`: plugins show timestamps in the case's
format (the Pivot's labels in time order, its Min/Max, the First/Last
preview and copy, Top values under a column format of its own), a case
format change redraws a pivot without re-running it, in its own window
and in a pop-out, and the context's formatter is the grid's. Its table is
US-format with a year end in it, so stored text is neither the case's
format nor in time order; seeded through `/api/plugin_state`, so no drag
is in the way of what is asserted.

`tests/smoke_server.py` is not a test module (no `test_` prefix, so pytest
never collects it); CI runs it as a script. It starts `server.py --case
--open` the way an analyst does and waits for the CSV's rows on
`/api/sources` and for the page, `main.js` and `style.css` to be served.
The Windows backend job runs it on the checkout; the `shipped archive` job
runs it on `git archive` output installed from requirements.txt alone, so
a runtime import of a dev-only package, or an app file marked
export-ignore, fails there instead of on an analyst's machine. Run it by
hand with `--root <unpacked archive> --python <its venv's python>`.
`tests/nightly_upgrade.py` is the nightly workflow's other script: it
installs the last release from its `git archive`, adds an analyst's state
(a plugin folder, a case file, a session export, the workspace a first
start writes), runs that install's own `update.py --from` on a zip of
develop laid out like GitHub's, and checks every shipped file and every
byte of that state, then does the same after `update.py --rollback`. Run it
with `--from-ref v0.4.1` (any release tag); set `TMPDIR` to keep its
folders out of a shared `/tmp`. The state is what sits under `workspace/`,
`plugins/`, `sessions/` and `cases/` that neither release ships:
`plugins/README.md` lives under `plugins/` but ships
(`updater.PROTECTED_EXCEPTIONS`), so the updater rewriting it is held to
the shipped-file check, not the state one. Counting it as state failed
the nightly the first night develop's copy differed from the release's
(2026-11-20, the systemd_journal example's line in it), though the update
did exactly what it should.

`tests/ui/test_select_search.py` holds the searchable drop-down list
(static/js/selectsearch.js) and a drop-down menu's search box
(ui.js menuFinder) to the gestures an analyst makes: a press on the Add
derived column dialog's Parse column and Format lists, letters typed on a
focused select, Alt+↓, the arrows, Enter, Escape (which must close the
list and not the dialog), a second press on the select, a select inside
an open anchoredPanel (whose panel must survive a press in the list,
which is outside it), a list of five that keeps the browser's own, and a
menu of six with headers and a submenu. None of it uses `select_option`:
that sets the value without opening any list, so it passes whether the
search works or not.

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

- **A job's start answer can already say it is done.** The watchlist
  scan's start route starts the worker thread and then snapshots the job,
  so on a busy machine a small scan finishes in between and the answer
  reads `done`. A test that needs a scan still running when the answer
  lands rewrites the status on every start it relies on, not only the
  first (test_watchlist_immediate_add.py's second Add).

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
  swallowed is how that issue went unnoticed. A tag key is a toggle, so
  pressing it again on "the second row" untags only if the teardown
  finds the same row on the same table: `one_tagged_row` in
  test_timeline_tag_chips.py did that, and one nightly flake hunt it left
  ui.csv with a TA row, which four test_timeline_timeframe.py tests then
  counted. It now keeps the body `tag_written`'s response carries and
  posts it back with `on: false`.

- **A test sees a request before its fake has answered it.**
  `page.expect_request` and a "request" listener fire as the request
  leaves; the `page.route` handler that answers it runs after. A test that
  goes on from there and removes the route (a fixture's `unroute` in its
  teardown) hands the waiting request to the real server.
  test_custom_delimiter_ui.py's faked import ran for real that way, and
  its table was in the case for every later module in a nightly flake
  hunt. A loop that unrouted straight after `expect_request` leaked 148
  times in 150; after `expect_response`, none. Wait on the fake's answer
  with `expect_response` (its `.request.post_data_json` is what was
  sent), and fake a route from a fixture with `fake_route`
  (tests/ui/conftest.py), which leaves only once every request it caught
  has been answered. test_ui_test_hygiene.py fails an `expect_request` on
  a URL its module fakes. A request a route holds, to answer by hand
  later, has no response to wait for: a `# expect_request:` comment says
  so (test_derive_preview_scope_ui.py).

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

- **The shared context's seed runs on every navigation, reloads
  included.** `FIRST_RUN_INIT` is an init script, so it writes
  `winnow.sidebar` (`{ collapsed: false }`), `winnow.appearance` and
  `winnow.remotePrompt` again each time the page loads. A test of a
  preference stored under one of those keys that reloads to prove it was
  remembered reads back the seed instead, and fails for a reason that
  has nothing to do with the app. Carry the value over the reload in
  sessionStorage and put it back with an init script added after the
  seed (they run in the order they were added):
  `_reload_keeping_sidebar` in tests/ui/test_column_dock.py.

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
- **Playwright is pinned in requirements-dev.txt, because each release
  brings its own Chromium.** It used to be unpinned, so CI took whatever
  was newest. On 2026-11-10 that went from 1.64.1 (Chrome Headless Shell
  156) to 1.65.0 (158) between two runs, and
  `test_ctrl_f.py::test_the_placeholder_names_the_keys_without_clipping`
  failed on app code that had passed the night before, on both release
  PRs for 0.4.3 at once. Under 158 the search box's placeholder with its
  keys (`All columns  —  / or Ctrl+f`) no longer fits in Contains mode:
  the app's canvas measurement still said it did, so the hint was
  clipped, and measuring it in the page instead (#544) only turned that
  into the bare `All columns`, which the test also refuses. What changed
  in 158 (the face `ui-monospace` resolves to, the hint's width, or the
  fonts `playwright install --with-deps` brings) is not known yet;
  CI's trace artifacts could not be fetched from the sandbox that
  found it. A Playwright upgrade is a browser upgrade: make it in a PR
  of its own, so a failure like this is that PR's and not whatever else
  happened to run CI that day.

- **The clipboard on Windows reads back with CRLF line ends.** The app
  writes `\n`; Chromium on Windows stores clipboard text with `\r\n`, and
  `navigator.clipboard.readText()` returns it that way, so a test that
  splits a copy on `"\n"` finds a `"\r"` on every row there and nowhere
  else. The `copied` fixture folds `\r\n` back to `\n`, and so does the
  `clipboard` fixture for a test that reads what is there now: assert on
  `clip.text` or `clipboard(page)`, and don't read the clipboard by hand.
  Only the nightly's Windows browser job can see this one, and it took
  three nights to find every module that did (test_copy_unique_values.py,
  test_sql_result_uncapped.py).
- **A path from `tmp_path` has backslashes on Windows.** There
  `str(tmp_path / "custom-delim.log")` is `C:\Users\…\custom-delim.log`,
  so page JS that names a file with `p.split('/').pop()` gets the whole
  path back. test_custom_delimiter_ui.py queued its log that way: on
  Windows the table was named after the full path, the wait for
  `custom-delim.log` timed out, and the cleanup, which deletes by that
  name, left the table behind for `case_left_as_found` to fail the
  module's last test on. It failed both nights its shard ran on Windows
  (2026-11-16 and 11-18, #556) and passed everywhere else. The app was
  never wrong: importer.js splits on both separators. Take the name in
  Python (`p.name`) and hand the page `{ path, name }`, as the other
  import tests do; tests/test_ui_test_hygiene.py fails on
  `split('/').pop()` in tests/ui.
  The same backslash goes the other way: the page joins a folder and a
  file name with the separator the folder uses (home.js `joinPath`), and
  the server answers with `os.path` in the OS's shape. So a test that
  expects `f"{folder}/x.db-winnow"`, or takes a case's folder with
  `rsplit("/", 1)`, passes on Linux and fails on Windows.
  test_group_folder_ui.py did both and failed four tests the first night
  its shard ran there (2026-11-20). Build expected paths with pathlib
  (`folder / name`) or `os.path.join`, and take them apart with
  `os.path.dirname`/`basename` or `.parent`/`.name`;
  test_ui_test_hygiene.py's `test_no_path_joined_or_split_on_slash` fails
  an f-string that puts `/` after a `tmp_path`-derived path, and a
  `split("/")` of anything that says "path" and not "url".
- **An import lists its table before the table has columns.** The
  background import inserts the `sources` row as it starts, with
  `columns` = `[]`, and writes the typed columns as it ends
  (`Store.ingest_csv`). A test that polled `loadSources()` until the name
  appeared and then opened the table got `S.columns = []` whenever the
  import was still running: the histogram strip said "No datetime column"
  and never drew, there was no header to right-click, a merge had nothing
  to line up, and a table removed in cleanup failed its own import ("No
  table N"). Twelve modules waited that way; a fast runner always won the
  race and the nightly's Windows job and flake hunt lost it a different
  module each night. Import through the `imported` fixture, which waits
  for the job to say `done` and returns the new tables' ids, then
  `loadSources()`. To check a test against this, put a `time.sleep(2)` in
  `ingest_csv` after the `sources` insert: every module that polled by
  name failed with it, and passes with `imported`.
- **A test about one platform has to say which.** The server and
  Chromium both answer for the machine running the test, so an assertion
  written on Linux is a Linux-only assertion. test_assoc_manual_help.py
  expected the `xdg-mime` steps and failed on Windows, where the panel
  rightly shows the Open With ones, and the splash test "off Windows"
  emulated reduced motion but left `navigator.platform` to the machine, so
  on Windows the animation played. Branch on `sys.platform` for what the
  server decides, and pin `navigator.platform` and the user agent (as
  test_splash.py's `_reduced_motion_ctx` does) for what the page decides.
- **A sleep is a lower bound, not a measurement.** macOS coalesces
  timers: on the nightly's macOS runner `time.sleep(0.02)` measured 87ms.
  A test about what a timed window *includes* drives a fake
  `time.perf_counter` instead (test_bench_harness.py's hooks test), and
  one that has to wait for real bounds the wait from below only.

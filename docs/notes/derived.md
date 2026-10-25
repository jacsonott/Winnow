# Derived datetime columns

`timeparse.py`, the `derived_columns`/`drv_<id>` tables and `/api/derived/*`
— an analyst-added column computed from one the evidence file already had.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

- **The preview samples the rows the analyst is looking at, if they say
  so** (`preview_derived(..., view_id=)` → `_sample_column_in_view`).
  It used to take the first 200 non-empty values straight off the source
  table, which is the one region a triage filter exists to escape: the
  modal could report "All 200 sampled values parse" about rows that had
  been filtered away, and the analyst found out after a backfill over
  millions of rows. The scope control is the value picker's, verbatim
  ("This view" / "Whole table"), because it is the same question asked
  about a different thing; it defaults to the view when one is narrowed,
  and the verdict names which sample it is about. An expired view is a
  **409** the client retries against the table with a toast — a 404 would
  read as "no such column" — and that split is made on the exception
  TYPE (`ViewExpired`, a `KeyError` subclass), never on the message.
  Looking for "expired" in the text is what both this route and
  `/api/histogram` used to do, and the unknown-column `KeyError` carries
  the column NAME: a table with a column called `expired_at` answered
  "that view was rebuilt" to a question about a column, and the client
  retried a request whose real problem was never going to change
  (invariant #5 — column names are user data).
- **An outer `LIMIT` on a `UNION ALL` reads member 0 and stops.** The
  merge claim above was false for a year of row counts: `SELECT v FROM
  (a UNION ALL b) LIMIT 200` drains `a` before it touches `b`, so any
  merge whose first member held 200+ non-empty values was previewed
  against that member alone — exactly the habit the scoped path was
  written to fix. It passed review because the test used ten rows a
  member. The slice now happens INSIDE each arm (`per = ceil(want /
  members)`), so every member is read. Two consequences worth knowing:
  the total can land under `limit` when a member is short, and the
  reported figure is a per-member average rather than the view's true
  rate — ten bad rows beside five thousand good ones read as "10 of
  110", not "10 of 5010". That errs toward caution, which is the right
  direction for a pre-flight check. Making it exact needs a per-member
  `COUNT` *within the view*, which is a query per member for a number
  the analyst reads as "does any of this fail".
- **Each arm carries its own `ORDER BY` for the same reason
  `_sample_column` does** — `s.rid` on the direct branch, `vv.pos` on
  the view-join branch (the view's own order, and free: `pos` is the
  view table's rowid). Without it the scoped sample silently changed the
  moment a background column index appeared, while the source-scoped
  sibling stayed put.
- **The payload says which scope it actually used.** A multi-input
  operation reads several columns of one row, which the one-column
  sample cannot express, so it is previewed against the table even when
  a `view_id` is passed. `preview_derived` returns `scope: "view"|
  "table"` on every path and the modal writes its verdict from that
  field rather than from the pressed button — otherwise a whole-table
  answer was labelled with the view's name, which is the precise claim
  this feature exists to stop.
- **`_sample_column` orders by `rid`, and that is not tidiness.** Without
  it the statement is a bare `SCAN` and the head of the file is the head
  by accident — until a background column index exists on that column
  (`_ensure_column_index_building` builds one the moment an equals filter
  touches it), at which point the planner switches to a covering-index
  range scan and returns the lexicographically SMALLEST values instead.
  The sample an analyst previewed against would then change between one
  open of a case and the next with nothing to explain it. Verified at
  3,000 rows: the plan goes `SCAN` → `SEARCH … USING COVERING INDEX` and
  the values change with it; at 40 rows the planner keeps the scan, which
  is why a small test of this proves nothing.
- **Detection is not parsing: an op can carry a stricter `detect` than
  its `parse`.** Mac absolute time's epoch, 2001-01-01, is inside the
  1990–2100 plausibility window, so every small positive number parses —
  EvtxECmd's RecordNumber (1, 2, 3…) came back as "Mac absolute (Cocoa)
  time — 100% of sampled values parse", and so did EventId, a PID or a
  duration in seconds. Excel serial dates have the same trap one range
  up: every whole number from 32874 to 73050 is a date, which covers
  record ids and every ephemeral port. `timeparse.detect` scores through
  `_parse_mac_for_detect` (a floor of 1e8, March 2004 — by magnitude, not
  "is it whole", because durations carry fractions) and
  `_parse_excel_for_detect` (whole numbers are misses), and
  `detect_source_suggestions` inherits both. The parsers themselves are
  unchanged: an analyst who picks the operation for such a column gets
  every value converted, because that was a choice and the guess was
  not. `test_timeparse.py`'s counter test and `test_derived.py`'s
  EvtxECmd one hold both halves. The other half of the RecordNumber
  report was client-side: the add dialog opened from the Table dialog
  started on the first column whatever it was, and now starts on the
  first that looks like a timestamp (`defaultParseColumn` in derived.js).

- **The add dialog is a fixed frame; its content never sizes it**
  (`openDerivedColumnModal`, `.side-panes`). It was a column of rows
  in a centred card: picking a type rebuilt the operation list, an
  operation rebuilt its parameters, the preview emptied to "Checking…"
  and refilled, and each of those changed the card's height, so the card
  re-centred and its header slid (1,860px of header travel over one walk
  through the choices, in a replica built to measure it). Now the card is `.tall` (top
  anchored), the two panes are one fixed height, and every part of the
  right pane is fixed except `.derived-settings`, which takes what is
  left and scrolls. Three details hold that: the preview keeps its last
  answer on screen, dimmed (`.stale`, `aria-busy="true"`), instead of
  emptying; the verdict sits in a footer of fixed height; and the name
  row hides its input with `visibility` (`.by-group`) when a regex's
  named groups take over, so the row keeps its height. A new control in
  the dialog goes inside `.derived-settings`, or it brings the jump
  back. test_derived_op_groups.py measures the frame through every
  entry. Because the preview can now be stale, a test that waits for
  `.derived-preview-row` alone can read the previous answer's rows; wait
  on the `derive_preview` fixture (aria-busy false) instead. The frame,
  the side list and the preview box are shared with Import a folder
  (`sideRail` in ui.js, the `.side-*` rules in style.css), so a change
  to one is a change to both; see ui.md.

- **The timestamp formats are one side-list entry; detection only fills
  in its Format** (`deriveEntries`, `TIMESTAMP_ENTRY`). Detection runs on
  every column change and is cached per column, but it picks the format
  only while "Parse a timestamp" is chosen and the analyst hasn't picked
  one by hand since the column changed (`formatTouched`) — it used to be
  able to move the Type select out from under a half-built Combine.
  `data-detected` on the modal body is the test hook for "the answer is
  in". The side list's pins are `menuPins('derive')`, the row menu's
  store, so they are per browser like every other pin.

- **A regex derive addresses its group by NAME where the pattern names
  one.** `regex_extract` takes `group_name` alongside the numbered
  `group`, and the name wins. This is not a nicety: a pattern gets edited
  — a group added in the middle, an alternation widened — and every
  numbered definition after the edit quietly starts keeping a different
  field, with no error and no sign on screen. The name does not move.
  `(?P<name>…)` is Python's spelling, not JavaScript's, so the names are
  discovered server-side (`/api/derived/regex_groups`) rather than parsed
  out of the pattern in the browser, and the same route samples the
  column so each offered column shows what it would actually pull. The
  columns are created through the existing batch route — one scan, N
  columns, all-or-nothing — which is the same machinery the JSON flatten
  picker uses.
- **A regex that can backtrack for minutes is refused, not run.** Python's
  `re` has no timeout, a backfill polls its cancel flag only between
  batches, and the preview and `/api/derived/regex_groups` run inside the
  request — so `(\w+\s?)*` against one 30-character cell that almost
  matches held a worker (or the whole backfill) for minutes with nothing
  able to stop it. `structparse._compile_analyst_regex` is the one gate
  every derive pattern passes, and its check is `rxguard.regex_hazard`:
  the same automaton the regex filter, search and SQL-pane REGEXP use
  (docs/notes/store.md has how it decides). There used to be a second,
  shape-only check here, because structparse cannot import store; two
  checks drew the line in two places, so the guard moved to a module
  both import. The dotted and CSV shapes people write all the time —
  `(\.[a-z]+)*`, `([a-z0-9-]+\.)+`, `(?:[^,]*,)*`, `(\S+\s+){3}` — pass;
  `(\w+\s?)*`, `(\w+\.\w+)+` and `(\d+\d*,)+` do not. Bounding the
  outer repeat is NOT a way out: `(\w+\s?){0,50}` took a minute on 29
  characters. Overlaps are judged case-insensitively, as REGEXP runs, so
  for a derive pattern (case-sensitive unless it says `(?i)`) the check
  can only be stricter than it needs to be, never looser.
- **Every path the flatten picker offers has to survive `validate_params`
  and extract the sample it was offered with.** Because the batch is
  all-or-nothing, one offered path that fails to parse fails every column
  the analyst ticked — and the picker pre-ticks anything at 100%. Two
  shapes broke that: an EVTX `Data Name` holding an apostrophe was spliced
  into `'…'` bare (now `"O'Brien"`, or `'it''s "x"'` when it holds both,
  XPath 2.0's doubling — `parse_xml_path` reads all three, and its step
  splitting skips `/ ] @` inside the quotes), and a JSON key with edge
  whitespace went out in dot notation, which strips (now bracket-quoted,
  and quoted keys are no longer stripped). `test_structparse.py` checks
  the property itself on those shapes, not just the two fixes. The JSON
  `[Name='…']` predicate quotes its value the same way (one
  `_quote_predicate` for both syntaxes, `quotePredicate` in derived.js),
  and test_struct_name_value.py holds it to the same property.
- **A Name/Value list's columns are named by the Name, unlike every other
  JSON path.** suggest_name gives a JSON field its full dotted path so
  `target.ip` and `source.ip` stay apart, but in
  `ExtendedProperties[Name='UserAgent'].Value` the path is plumbing and
  the Name is the field name, which is what was asked for. The value key
  is dropped only when it is the plain one (`Value`, `#text`), so
  `[Name='X'].OldValue` and `.NewValue` stay two columns. Two lists in one
  document holding the same Name both suggest it; the picker's
  `uniqueColumnName` makes the second `UserAgent 2`. Detection is
  deliberately narrow (`_kv_shape`): a list of files with a `name` and a
  `size` must stay positional, since a column per file name would fill
  the picker with one-row fields.
- **URL decode keeps what it cannot decode, escape by escape**
  (`decode.py`, family `"decode"`, its own group in the modal's list). A run of
  `%XX` escapes is decoded as UTF-8 together, since one character spans
  several, and a byte that is not part of valid UTF-8 is written back as
  the escape the cell held, hex case included. Not U+FFFD: Latin-1's
  `caf%E9` and the overlong `%C0%AE` of a traversal are what an analyst
  decodes a column to find, and a replacement character makes them all
  the same mark. Not a NULL for the whole value either, which would hide
  the `%2F` decoded beside them. So the op returns NULL only for empty
  input and the column never has unparsed rows. `+` becomes a space
  before the escapes are decoded, so `%2B` survives as a plus;
  `plus_as_space` turns that off for paths. One pass only: double
  encoding is undone by deriving from the derived column.
- **Base64 decode follows the same rule, and "is it text" is what makes
  that safe.** A value that is not base64, or whose bytes do not decode
  strictly to text without C0/C1 control characters (`_NOT_TEXT`), is
  kept as written and is not a failure. Most ordinary values are not
  base64 at all, and most of those that are by accident decode to
  invalid UTF-8 or to controls (`AAAA` is three NULs); the ones that
  don't (`WS01` is `Y-5`) cannot be told from text, which is why the
  wiki says to use it on a column that holds base64. `encoding: auto`
  picks UTF-16LE on a BOM or when at least half the high bytes are zero
  (PowerShell's `-EncodedCommand`), else UTF-8. It does not fall back
  from a failed UTF-8 to UTF-16LE: nearly any even-length binary is
  "valid" UTF-16LE, so the fallback would decode noise into CJK; a
  non-Latin UTF-16LE column takes `encoding: utf-16le` instead.
- **load_json reads more than JSON, and json_field reads what it reads.**
  `strict=False`, then `load_loose` (Python's repr, trailing commas,
  unescaped backslashes), because the detail pane offers paths on those
  values and a path the backend won't extract fills the column with
  nothing. The loose parser only runs when json.loads has already
  refused, so a strict JSON column pays nothing for it. A Python-repr
  column parses in Python rather than C, about ten times slower (80µs for
  a 500-character Name/Value list), so it gets its own cap,
  `LOOSE_MAX_CHARS` (a million; the worst shape takes about a second
  there, where 8 MB took nine), and `_load_json_for_row` keeps the last
  row's document so a ten-column flatten parses each row once. It also
  widens sniff_kind, so a Python-repr column now gets the Flatten picker.
  The parser is a loop over one token regex, not the pane's recursion; a
  differential fuzz against a recursive version and against detail.js
  agreed on 200,000 and 20,000 values, and
  tests/ui/test_detail_name_value_paths_ui.py keeps the edge cases.

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
    and the session file's column list. Merge *eligibility* is base
    columns only, on the server (`create_merge`) and in the Merge tables
    dialog's grouping alike (`merge.js`'s `columnGroupKey`) — the dialog
    used to group by every column, so a table with a derived column sat
    in a group of its own and the dialog said no two tables matched for
    a merge the server accepts. Each member has its own `drv_` table and
    its own definitions; a merge exposes a derived column only where
    every member has one of that name (`_merge_columns`), since a UNION
    ALL across mismatched sets would need per-member NULL padding
    nothing does yet. Editing one is its own entry below.
  - **An op that reads several columns declares them as params and the
    store asks `timeparse.op_inputs(op_id, input_column, params)`** for
    the full ordered list — never `params["other_column"]` by name. The
    op implements `parse_multi(values, params, state)` (values in that
    order; an older `parse_pair(a, b, params)` is wrapped automatically).
    Validation, backfill (`slots`), preview, the remove/re-derive
    dependency checks and the session round-trip all go through that one
    helper, so a new `column`/`columns`-typed param is read everywhere
    the moment it's declared. `combine.py`'s coalesce (`family:
    "combine"`, `derived_kind: "combine"`) is the model.
    `row_json` (same family) is the second: the store puts the ordered
    input names in `state["inputs"]` before any `parse_multi` call —
    backfill and preview both — so an op whose output names its columns
    reads them there instead of growing a second calling shape. Its
    `columns` param carries two UI-only keys, `any_type` (offer every
    column, not just text) and `prefill: "all"` (start with every other
    column chosen); `validate_params` ignores both. `bool` is a param
    type now (a checkbox; the state holds a real boolean). A cell goes
    out as an unquoted JSON number only when the number json writes
    back is the cell's own text — "1.10", "-0" and
    "9999999999999999.99" stay strings, or the object would say 1.1, 0
    and 1e+16 about evidence that didn't — and the patterns are
    `fullmatch`ed, since `$` also matches before a trailing newline and
    wrote the cell "42\n" as 42.
  - **`_from_clause(src)` is the one place the join is spelled** —
    `LEFT JOIN drv_<id> USING(rid)`, so an unqualified `rid` stays legal
    on both sides. `drv`'s rid is an INTEGER PRIMARY KEY, so the join
    matches at most one row and can't change the row count or order: that
    is what keeps `_fetch_virtual_root_rows`' `pos = rid - 1` exact
    (invariant #2), pinned by an EXPLAIN QUERY PLAN test in
    `test_derived.py`. A source with no derived columns compiles
    byte-identical SQL, which is why `bench --vs-ref` is flat.
  - **An aggregate that reads no derived column leaves the join out.**
    SQLite drops an unused LEFT JOIN from a plain SELECT but not once it
    is flattened into a GROUP BY or a count(*), so adding one derived
    column made a Values list or a group count on a *base* column pay a
    sidecar lookup per row: Computer's values on a 2.1M-row EvtxECmd
    table went from 0.11 s to 14.3 s. `_from_clause`, `_member_from` and
    `_member_derived_join` take `uses` (every column the statement
    reads); `column_values`, `group_summary` (the grouped column plus the
    path's), `expand_group`, `time_histogram` (plus the split column) and
    the histogram probes pass it. Leaving a derived column out of `uses`
    is "no such column", never a wrong count, and omitting `uses` keeps
    the old always-join behaviour for every other reader.
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
  - **The grid on screen is refreshed when the backfill lands, by
    `derived.js`'s `refreshAfterDerive`, not by `loadSources`.** The
    modal re-opens the table straight after the POST, while the pass is
    still running, so the rows it caches are blank (or filled only as far
    as the pass had got) and the header says "(building…)". `pollJobs`
    refreshes the source list when the job finishes, but with
    `navigate:false`, which leaves a live grid alone on purpose — so for
    as long as that was the only refresh, a new column sat empty until
    the analyst switched tables and back. The refresh re-reads the
    column's status and drops the row caches in place, keeping the scroll
    and the picks — a re-open would reset them (and, from a page tab,
    drag the analyst back to the grid), and the analyst has had the whole
    pass to move on. A view sorted, filtered, grouped or timeframed by the
    column was ordered by the old (or missing) values and is rebuilt, and
    a cancelled or failed create's column, gone on the server, leaves
    `S.columns` and the column order; the row caches go in the same
    step, because a cached row's cells are positional to `S.columns`. A
    failed or cancelled job carries no table, so "is this the open table"
    is answered by comparing its derived columns with `S.columns` as well
    as by the done job's `source_ids`.
    `tests/ui/test_derived_fills_in_ui.py` runs on its own 150k-row
    server, because a pass over the shared fixture's 200 rows finishes
    before the grid can fetch a page and never shows the bug.
  - **A backfill that is refused lands the same way a cancelled one
    does, one step earlier.** `start_ingest_job` raises `OpCancelled`
    while the case is closing, and the definition rows are already
    committed by then: a create drops them
    (`_rollback_derive_definitions`), a re-derive restores the
    definition it had just overwritten (`_restore_derive_definition`),
    and a cascade marks the stale child `partial` and says so in the
    log. The state being avoided in all three is the same one — a
    column left `'building'` with no job in existence, which reads
    `(building…)` in the header after every reopen, makes
    `save_view_as_source` refuse the whole table, and blocks anything
    chaining off it, with no way back except deleting the column.
  - **One value is never the column's problem, and a failed pass never
    leaves `'building'`.** Parsers are meant to be total — return text or
    None for any input — and `test_timeparse.py` holds every registered op
    to that over a hostile corpus. The ones that weren't: an offset inside
    a value beyond ±14:00 (`_value_offset`; the analyst's own `utc_offset`
    param still fails loudly in `validate_params`), .NET's
    `1/1/0001 12:00:00 AM` under any offset (`_shift_naive`/`_fmt` give
    None at datetime's range ends), `"²"` and 5,000-digit strings as tick
    counts (`isdecimal` + a length cap before `int()`), and a JSON
    `"\ud83d"` whose lone surrogate SQLite cannot store (`_leaf_text`
    writes it back as the escape the cell held — `structparse.utf8_text`,
    the rule JSON import follows too, rather than U+FFFD, which would make
    two different half-characters one value and two keys one column
    name). Because the registry is open, the callers don't trust
    that either: the backfill wraps each value (a raise is NULL, counted
    with the other failures, logged once per column with the first row),
    retries a batch SQLite refused with `_storable_cell`, and detect /
    preview / suggestions go through `timeparse.parse_or_none` — one bad
    cell used to 500 those routes for the whole table. Anything else that
    stops a pass lands in `_fail_derive_backfill`: a create is removed
    (as on cancel); a re-derive that wrote nothing yet is put back exactly
    as it was (`previous`, the snapshot `rederive_column` passes); a
    cascade that wrote nothing is `partial`; and a re-derive that stopped
    after committing some batches is `'error'` with its values cleared and
    its own children `partial` — two runs' values side by side (the report
    had 20,000 rows at 11:22:01 and 20,009 at 13:22:01 for identical
    input) is the one result nobody can read correctly. `rederive_column`
    also runs the op's `check` first, so a lookup whose table is gone is
    refused with nothing changed.
  - **Canonical output is `YYYY-MM-DD HH:MM:SS[.ffffff]`**, sub-second only
    when the source has that resolution — four-digit year always, which is
    why `_fmt` writes the fields out itself and not with `strftime`: glibc's `%Y` writes
    year 999 as `999`, a shape nothing recognises and that sorts after
    2024 as text. That exact shape is why there are
    **no new regexes to hand-sync**: `_TS_ISO_RE`/`TS_NORMALIZE`/`DAY_BUCKET`
    and `tsformat.js`'s `parseTimestamp` already prefix-match it. Timezones:
    epoch-family values are UTC by definition, values carrying an explicit
    offset (ISO `Z`/`±HH:MM`, CLF, RFC 2822) are converted to UTC, and
    naive text is kept exactly as written unless the analyst sets the
    op's optional fixed `utc_offset` — never a TZ database, never DST
    inference. The ISO, `dd_mmm_yyyy`, `mmm_dd_yyyy` and US ops read whatever follows the
    time through one function, `timeparse._trailing_zone`: Z/UTC/GMT,
    `GMT+02:00`, `±HH:MM`/`±HHMM`/`±HH` (U+2212 as a minus too) are
    applied and win over `utc_offset`; a zone it *cannot* apply — an
    abbreviation like `EST`, an IANA name, an offset past ±14:00 — makes
    the value a parse failure rather than being dropped, because dropping
    it reads the wall-clock time as if it were UTC, hours off with
    nothing in the column to say so (that is what `+05`, a 10-digit
    fraction before the offset, `−05:00`, and every offset after a
    dd-Mmm or US time used to do). Trailing text that names no zone — a
    log message after the timestamp — is left alone, as before. RFC 2822
    has a zone grammar of its own and keeps the same rule without
    `_trailing_zone`: the zones the RFC defines (UT/UTC/GMT/Z, EST/EDT,
    CST/CDT, MST/MDT, PST/PDT, ±HHMM within ±14:00) are applied, and
    anything else in the zone slot — `CET`, `AST` (Atlantic or Arabia),
    a military letter (RFC 822 wrote their signs backwards), an IANA
    name — is a parse failure. `email.utils` dropped a zone it did not
    know without a word, so `15:04:05 CET` came out as 15:04:05 UTC
    while `15:04:05 EST` was converted. `-0000` is UTC by the RFC's own
    definition (the time IS in UT; only the sender's zone is unknown),
    so `utc_offset` does not shift it — `email.utils` returns it naive,
    like a value with no zone at all, which is why it is named
    explicitly. The zone token is found by mirroring email's own token
    selection (`_rfc2822_zone_token`: `date`'s layout puts the zone
    before the year) and cross-checked against what the stdlib parsed,
    so a future divergence fails values rather than misplacing them. The
    12-hour marker is `timeparse.MERIDIEM` everywhere a US or month-name
    time is read (`_US_RE`, `_MMM_DD_RE`, store.py's `_TS_US_RE`/`_TS_MONTH_RE` behind
    TS_NORMALIZE and DAY_BUCKET, tsformat.js's twins): `PM`, `Pm`,
    `p.m.`, after an optional fraction. `\s*(AM|PM|am|pm)?` straight after
    the seconds read `1:22:01.123 PM` as 01:22 — twelve hours early in
    the derived column, the Timeline and the timeframe filter at once.
    The month-name alternation and the day's ordinal suffix ("Jul 28th
    2026") are shared the same way: `timeparse.MONTH_NAME` and
    `timeparse.ORDINAL` build `_MMM_DD_RE` and store.py's `DATE_RE`/
    `_TS_MONTH_RE`, and tsformat.js's `TS_MONTH_RE` mirrors both by hand.
    The suffix isn't checked against the number — "21th" is still the
    21st, and refusing it would turn a typo into a missing timestamp.
    Unparseable input is NULL, not `''`: it drops out of
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
- **A lookup column names its table by a case-local id, so the id never
  travels on its own** (`enrich.py`; `_session_derived_def` /
  `_resolve_session_lookup` for sessions, `_repoint_copied_lookups` for
  "Copy into case"). In another case — or in this one once the table is
  dropped and its id reused — that number is some other table, and a
  session import used to fill the column from it with no warning (the
  owner column said mallory where the analyst had alice). A session now
  carries the table's identity beside the definition (`lookup_table`:
  file hash, name, base columns and row count — what a session source
  records) and the import resolves it the way session sources are
  matched by a load (`_match_session_sources`; a SQL-saved or
  plugin-made lookup table by its synthetic `made:` hash, so another
  case's table of the same name is not it); no such table, or more than
  one it cannot tell apart, is a warning and the column is not
  recreated. A session saved before the identity was recorded is
  refused the same way rather than trusted by id — and so is one that
  recorded a made table with no hash, which used to pair by name. A copy re-points the definition to its table when that came
  along, else to the target's one table with the same fingerprint, else
  to none — the copied values stand and a re-derive asks for the table.
  Blank keys (NULL or `''`, the one-column "had input" rule) match
  nothing on either side, and so are neither a hit nor a parse failure:
  an empty Host used to join the lookup table's own blank-key row.
- **A derived column on a merge is one definition per member, and an
  edit made on the merge goes to every one of them** (invariant #9).
  Creating on a merge fans out (`add_derived_column(s)` with a negative
  id: checked against every member, then made on each), but the merge's
  column list carries only the FIRST member's `derived_id` — so Remove,
  Re-derive and "Change the field path…", sent with that id alone,
  acted on the first member: a remove left every other member's copy
  behind, a re-derive recomputed one member of four. The client now
  sends the table the edit was made from (`source_id` on
  `DELETE /api/derived/{id}` and on `/rederive`), and `_derived_scope`
  turns a merge's id into each member's definition of that name
  (ASCII-folded, as `_merge_columns` matches them); no `source_id`, or a
  member's own, is the one definition, as before. All-or-nothing up
  front, the way create is: a remove checks every member for a column
  computed from it before removing any, and a re-derive validates the
  params against every member's definition before rewriting any —
  refusing outright when the members compute the column with different
  operations (made per table before the merge), since one set of params
  cannot mean the same to both. One job for the whole merge, as create
  starts (next entry); cancelled, the member it was on is left
  'partial' and the ones it had not reached keep their definitions.
  The merge's column therefore reports all of them: the least finished
  status (`_DERIVED_STATUS_RANK` — building, error, partial, ready) and
  the failures SUMMED, because the "Show N unparsed rows" filter it
  offers matches rows in every member; it used to count the first.
- **A merge's derive is ONE job that backfills the members in turn**
  (`_backfill_derive_parts`, options `parts`: each member's def ids,
  plus `previous` on a re-derive). Create, a batch (named groups, a
  flatten), re-derive and the cascade a re-derive sets off all start it
  that way. It used to be a job per member, and with
  `MAX_CONCURRENT_INGESTS = 2` two members' passes ran at once: two
  CPU-bound Python loops fighting over the GIL and the writer lock,
  each about three times slower than alone. A merge of four 250k-row
  tables took 7.5 s for a regex column where one 1M-row table took
  4.5 s; one member at a time takes what the single table does, and
  the analyst sees one row in the jobs panel (`User — 2/4: b.csv`, from
  `tables_done`/`tables_total`/`current_table`) instead of a column
  filling a table or two at a time. Don't split it back up to "use the
  second slot": the slot doesn't buy CPU, the GIL is the limit.
  Stopped part-way, the member it was on ends as
  `backfill_derived_columns` leaves any pass, and the members after it
  as a pass that wrote nothing (`_fail_derive_backfill(wrote=False)`):
  a create removes them, a re-derive restores their old definitions. A
  create that stops (cancel or error) also removes the members it had
  FINISHED — one left on some members is a column the merge doesn't
  show, and its name, taken there, refuses the retry on the merge. A
  re-derive keeps the members it finished. The job's `result` is
  the merge's column, not one entry per member (`_derive_job_result`:
  rows and failures summed), so the toast reads `"User": 1,000,000 rows
  read` rather than "4 columns".
- **"Change the field path…" is offered only for an op that has a
  `path` param** (`derived.js`'s `hasFieldPath` — JSON field and XML
  field), not for every `derived_kind: "text"` column. Regex capture
  and lookup are `"text"` too, and the prompt's `{path}` rederive is not
  a param either op takes, so it failed every time and their settings
  could not be changed at all. They take Re-derive…, which is the
  derive modal with the operation fixed and the column's current params
  filled in — pattern and group, or the lookup table and its columns —
  with the named-group "a column for each" offer suppressed, because a
  re-derive edits one column and never sends a batch.
- **A derived column's name is a reference, so a rename has to move
  every holder of it, in one transaction** (`rename_derived_column`).
  The holders in the case file: the `drv_<id>` column itself, every
  definition that reads it — its `input_column` and the `column`/
  `columns` params op_inputs names, on its own table, and a lookup's
  `lookup_column` params on ANY table whose `lookup_source` is this one
  (`_renamed_refs`) — and the saved layouts of its table and of each
  merge over it (only when every member showing the old name was
  renamed). One `BEGIN IMMEDIATE` around all of it, because a rename
  that moved the column and not the child computed from it leaves a
  child whose next re-derive reads a column that is not there. Refused
  while the column or anything reading it is 'building': a backfill
  binds the names it was started with into its SELECT and upsert.
  `RENAME COLUMN` is a schema edit, not a row rewrite, but SQLite
  refuses it (as it does DROP COLUMN) when any view anywhere in the
  file fails to parse; then the values are copied into a new column and
  the old one left blanked — the same orphan a refused DROP leaves,
  which add_derived_column already reuses. A change of capitals alone
  has no copy fallback (it would copy a column onto itself), so it is
  refused there with nothing changed. Saved filters, default column
  layouts and Timeline templates are NOT rewritten: they live in
  workspace/, keyed by header set and shared with every table in every
  case that has one, so following one table's rename there would break
  the rest.
- **The client moves its own state before it re-opens the table**
  (`derived.js` `renameColumnIn`, over `S` and the stashed view state of
  every table renamed on). openSource stashes what is on screen and
  puts it back, so a filter, sort, grouping or timeframe on the old name
  would otherwise come back naming a column the table no longer has.
  The layout is saved with `saveLayoutNow()` first rather than through
  the 400ms debounce: the re-open fetches the server's copy, and a
  width changed just before the rename was still waiting to be written,
  so it came back without it (tests/ui/test_derived_rename_ui.py).
- **A text-valued op's column is typed by its values, not by the op**
  (`derived_columns.inferred_type`, `_infer_derived_type`). A timestamp
  parse is datetime and a duration is a number whatever they hold, but
  the same regex capture is a host name in one column and a byte count
  in the next, so `value_type: "text"` used to make every regex, JSON,
  XML, decode, lookup and combine column text — and a size pulled out
  with a regex sorted 10 before 9 and compared `> 1000` as strings. The
  rule is infer_type's (every non-empty value matches NUM_RE), run in
  SQL with REGEXP over the whole `drv_` column rather than a 500-row
  sample, and NUM_RE exactly rather than a cheaper GLOB: `_numeric_expr`
  gates the sort and the comparisons on the same pattern, so a column
  this calls a number has no value they would turn into NULL. A text
  column answers at its first non-number; a number column is one scan
  (~0.7s for 1.3M values, against a pass of seconds per million rows).
  Read off the column as stored, never tallied by the pass, so one
  function covers every way values arrive. When it runs: at the end of
  every pass ('ready'); on a cancel ('partial' — half one pass and half
  another, so it is cleared and looked at again, at the next open if
  the cancel is the case closing); cleared with the values on 'error'.
  A re-derive keeps the old answer while it builds rather than clearing
  it up front, so the column does not flicker to text and back for the
  length of the pass. A case from before this has NULL everywhere and is
  typed once, after it opens (`_type_unlooked_derived_columns`, below);
  'empty' is stored rather than NULL so an empty column is not rescanned
  every open. A merge's twin is a number only when no member's copy holds
  text — an EMPTY member (a host whose log never has the field) says nothing,
  or one quiet host would put the whole merge back to sorting as text.
  `derived_kind` is untouched: the menus that key on "extracted" still
  see one.
- **That open-time typing runs on a thread, never inside `Store()`.**
  It shipped inline at the end of `__init__`, under the writer lock, and
  one number column is a full scan — so a case with a few million rows
  in a handful of regex columns took seconds to minutes to open, on a
  case switch under server.py's `_CASE_SWITCH` too. The home screen's
  Open did nothing visible for that long, and every click after it was
  dropped as a second open (home.js keeps one in flight). The worker
  scans on a pool reader with a progress handler that interrupts it
  once `_closing` is up, so close() (which joins it) is not held for a
  scan's length; a column it doesn't reach stays NULL for the next
  open. Its UPDATE is conditional on the column still being NULL and
  'ready'/'partial' under the same op, so a pass that rewrote the
  values meanwhile keeps its own answer rather than this one, read off
  the old values. Until the answer lands the column reads as the op's
  own type, which is what every derived column read before any of this
  (tests/test_derived_types.py holds `Store()` and /api/case/open to
  returning while it is held).

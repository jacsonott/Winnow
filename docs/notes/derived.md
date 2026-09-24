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
  read as "no such column". A view over a MERGE unions its members, so
  the scoped path also fixes the source-scoped one's habit of previewing
  member 0 only.
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
  - **An op that reads several columns declares them as params and the
    store asks `timeparse.op_inputs(op_id, input_column, params)`** for
    the full ordered list — never `params["other_column"]` by name. The
    op implements `parse_multi(values, params, state)` (values in that
    order; an older `parse_pair(a, b, params)` is wrapped automatically).
    Validation, backfill (`slots`), preview, the remove/re-derive
    dependency checks and the session round-trip all go through that one
    helper, so a new `column`/`columns`-typed param is read everywhere
    the moment it's declared. `combine.py`'s coalesce (`family:
    "combine"`, `derived_kind: "combine"` — not `"text"`, which the
    header menu treats as "has a field path to edit") is the model.
    `row_json` (same family) is the second: the store puts the ordered
    input names in `state["inputs"]` before any `parse_multi` call —
    backfill and preview both — so an op whose output names its columns
    reads them there instead of growing a second calling shape. Its
    `columns` param carries two UI-only keys, `any_type` (offer every
    column, not just text) and `prefill: "all"` (start with every other
    column chosen); `validate_params` ignores both. `bool` is a param
    type now (a checkbox; the state holds a real boolean).
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
  - **Canonical output is `YYYY-MM-DD HH:MM:SS[.ffffff]`**, sub-second only
    when the source has that resolution. That exact shape is why there are
    **no new regexes to hand-sync**: `_TS_ISO_RE`/`TS_NORMALIZE`/`DAY_BUCKET`
    and `tsformat.js`'s `parseTimestamp` already prefix-match it. Timezones:
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

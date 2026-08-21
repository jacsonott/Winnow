# Derived datetime columns

`timeparse.py`, the `derived_columns`/`drv_<id>` tables and `/api/derived/*`
— an analyst-added column computed from one the evidence file already had.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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

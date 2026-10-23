"""SQLite storage layer for Winnow.

Design notes
------------
* One case = one SQLite file. Each imported CSV becomes its own `src_<id>` table
  with an explicit `rid INTEGER PRIMARY KEY` so row identity is stable forever.
* Source tables are never mutated. Tags, notes and layouts live in sidecar tables
  keyed by (source_id, rid), so re-importing the same file keeps your work.
* Filtering/sorting is materialised once into a temp-attached `v.view_N` table of
  (pos, rid). The grid then pages with `WHERE pos BETWEEN ? AND ?`, which stays
  O(window) no matter how deep you scroll. Naive LIMIT/OFFSET does not.
"""

from __future__ import annotations

import calendar
import codecs
import contextlib
import copy
import csv
import datetime
import errno
import fnmatch
import functools
import getpass
import hashlib
import io
import itertools
import json
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import string
import sys
import tempfile
import threading
import time
from array import array
from typing import Any, Callable, Iterable, Iterator, Sequence

from openpyxl import Workbook

try:  # POSIX only — see sweep_orphan_views for why Windows needs no substitute
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from . import enrich  # noqa: F401 — registers the cross-table lookup op into timeparse.OPERATIONS
from . import combine  # noqa: F401 — registers the multi-column ops (coalesce) the same way
from . import decode  # noqa: F401 — registers the decode ops (URL decode) the same way
from . import structparse  # noqa: F401 — registers the JSON/XML extraction ops into timeparse.OPERATIONS
from . import timeparse
from . import iisread
from . import plasoread
from . import rxguard
from . import xlsxread
from . import log as wlog
from .paths import sqlite_header_is_wal, sqlite_peek_uri, sqlite_ro_uri, sqlite_rw_uri
from .sqlresults import SqlResults, js_str as _sql_js_str, pane_cell_json, pane_cell_text  # noqa: F401 — server.py imports pane_cell_json from here

BATCH = 20_000
# build_fts's commits are sized by the time they hold the writer lock, not
# by a row count: the trigram tokenizer indexes about 2-3 MB of text a
# second, so BATCH rows of a 1.4 KB-a-row EvtxECmd export held the lock
# for ten seconds a commit, and every tag, undo and group expand queued
# behind it for the whole build. Each commit aims at FTS_COMMIT_SECONDS,
# the next one's size scaled from how long the last one took. Shorter costs
# build time, since every commit flushes a segment FTS5 later merges: on
# 208k EvtxECmd rows 0.25 s made the build 21% slower (worst hold 0.73 s),
# 0.5 s 10% (1.15 s) and 1 s 6% (2.3 s).
FTS_COMMIT_SECONDS = 0.5
FTS_COMMIT_MIN_ROWS = 50
FTS_COMMIT_FIRST_ROWS = 500


def _next_fts_commit_rows(rows: int, held: float) -> int:
    """The next build_fts chunk's row count, from how long `rows` held the
    lock: scaled toward FTS_COMMIT_SECONDS, but never more than doubled in
    one step (a chunk of short rows followed by a run of long ones should
    not overshoot by the whole ratio) and kept between FTS_COMMIT_MIN_ROWS
    and BATCH."""
    want = rows * FTS_COMMIT_SECONDS / held if held > 0 else rows * 2
    return int(max(FTS_COMMIT_MIN_ROWS, min(BATCH, rows * 2, want)))


SAMPLE_ROWS = 500
# The csv module's own field limit is 131072 characters, and one field
# past it stopped the import at that line — an EvtxECmd payload carrying a
# PowerShell script block, or a base64 blob some tool put in a column, is
# routinely bigger. Raised rather than removed, because the limit is also
# what bounds a stray quote with no partner: csv folds the rest of the FILE
# into that one field, built in memory at four bytes a character, and on a
# multi-GB export "unlimited" means the analyst's machine runs out of
# memory instead of the import stopping with a line number. 64 Mi
# characters is far past any real field and caps that at a few hundred MB.
# A number rather than sys.maxsize: field_size_limit takes a C long, which
# is 32 bits on Windows, and sys.maxsize overflows it there. Set once, for
# the process (it is module state in _csv), so the import preview and the
# import itself read a file the same way. A swallow that stays under the
# cap no longer errors at all — suspect_quote_rows is what flags it now.
CSV_FIELD_LIMIT = 64 * 1024 * 1024
csv.field_size_limit(CSV_FIELD_LIMIT)
TRIGRAM_MIN_LEN = 3  # the trigram FTS5 tokenizer can't index anything shorter — see _fts_like_pattern
SARGABLE_OPS = {"equals", "in"}  # ops a plain single-column B-tree index actually accelerates (see _ensure_column_index_building)
# SQLite gained LIKE/GLOB pushdown onto fts5 trigram tables in 3.45.0 —
# the query form build_fts's detail=none index depends on. On an older
# runtime the same LIKE still returns correct rows (SQLite just evaluates
# it by scanning the fts table's docs — fallback speed), so nothing breaks;
# building an index that can't accelerate anything is just wasted disk,
# hence _ensure_fts_building gates on this.
TRIGRAM_LIKE_MIN_SQLITE = (3, 45, 0)
# search_all_sources stops counting a source's matches here and reports
# `capped` instead of an exact number. The modal it feeds only ranks
# "which tables hit, and roughly how hard" before you open one — an exact
# 480,113 is no more useful than "1,000+", and getting it costs a full
# count over every matching row on a source whose index isn't built yet.
SEARCH_ALL_COUNT_CAP = 1000
# How many terms a "paste a list" sweep will break down per source before it
# reports the union count alone. The breakdown costs one extra capped count
# per term on each source that matched at all (see
# _iter_search_all_sources), which is what makes an entry-per-term list
# possible; a list long enough to make that N+1 shape hurt is one where the
# per-term rows would be unreadable anyway. Well clear of a realistic IOC
# paste — it exists to bound an accident (a whole wordlist pasted in), not
# to ration normal use.
SEARCH_ALL_TERM_BREAKDOWN_MAX = 250
# The unified Timeline (and the Watchlist's Latest hits, which reads the
# same per-source config) joins a source's body columns with `a || ' | ' ||
# b || ...`, which SQLite parses as one left-deep tree two levels deeper per
# column — so a ~500-column table used to fail the whole query ("Expression
# tree is too large (maximum depth 1000)") and nothing rendered at all. Both
# now keep only as many body columns as fit (see Store._fit_body_cols). The
# depth limit is read off the linked SQLite; this is the fallback for
# Pythons before 3.11, whose Connection has no getlimit(). 1000 is SQLite's
# compiled-in default.
SQLITE_DEFAULT_EXPR_DEPTH = 1000
# Depth a timeline branch spends around the body chain itself (COALESCE
# and the column ref). Measured at limits 200 and 1000: N columns need
# 2*N + 2, so 499 builds and 500 fails at the default. The extra margin is
# for a SQLite that nests a little differently, or a later change that
# wraps the body in a function.
TIMELINE_BODY_DEPTH_HEADROOM = 10

# The extensions a directory import (scan_import_directory) recognizes by
# default — every format ingest_csv/ingest_json already know how to read.
# Deliberately mirrors app.js's file-picker <input accept> list rather than
# inventing a separate notion of "supported" — one format list, two places
# it has to be spelled out (a browser <input> can't read a Python constant).
DEFAULT_IMPORT_EXTENSIONS = {".csv", ".tsv", ".txt", ".psv", ".json", ".jsonl", ".ndjson"}
# The extensions that NAME their delimiter. A file called .tsv is a
# tab-separated file — that is what the suffix means, and it is better
# evidence than a content sniffer's tie-break, which is decided by
# CPython's `preferred` list and puts the comma first. One comma on every
# line (a column named "Last Modified (UTC, local)" is enough) makes the
# comma exactly as consistent as the tab, and a .tsv then imports as CSV
# with its header split down the middle.
# .txt is deliberately absent: it names no delimiter, so it is sniffed.
EXTENSION_DELIMITERS = {".tsv": "\t", ".tab": "\t", ".csv": ",", ".psv": "|"}
# The SQLite set is separate because these files can't be bulk-imported
# blind — which tables to pull out is a per-file choice (see
# preview_sqlite_tables) — so directory import ignores them while the
# server-disk file browser still lists them.
# Winnow's own case files. A distinct extension is what lets an OS
# association, a file manager, and the launcher tell "a case to open" from
# "a SQLite database to ingest as evidence" (a Chromium History.db and a
# Winnow case are both SQLite; only the association target differs). It
# still IS an ordinary SQLite file — a curious analyst can rename it to
# `.db` and open it in any SQLite tool — the suffix is a label, not a
# format. Deliberately NOT in SQLITE_IMPORT_EXTENSIONS: opening a case as
# if it were evidence to import is exactly the confusion this separates.
# Historic cases are `.db`; those still open (the launcher sniffs content,
# is_winnow_case_file), only NEW cases take this suffix. One definition,
# imported everywhere a case file is created or recognised.
CASE_SUFFIX = ".db-winnow"
SQLITE_IMPORT_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".db-wal"}
# Excel workbooks route the same way SQLite files do — which sheets to
# import is a per-file choice (see preview_xlsx_sheets), so directory
# import ignores them too. .xlsm is the same zip container with macros the
# reader never executes; legacy binary .xls is out of scope (xlsxread.py).
XLSX_IMPORT_EXTENSIONS = {".xlsx", ".xlsm"}

# Plaso storage files (log2timeline output) — one file, one flat timeline
# table, read by winnow/plasoread.py. Its own set (not SQLITE_...) because
# a .plaso must never fall into the generic table picker: its raw tables
# are serialized attribute containers, useless without the decode step.
PLASO_IMPORT_EXTENSIONS = {".plaso"}
# IIS web server logs are recognised by NAME (u_ex240501.log, nc2405.log —
# iisread.log_format), since their extension is the .log every other log
# has. In a folder scan's extension list this token stands for them the way
# "*" stands for other text files: a chip of its own, on by default.
IIS_IMPORT_TYPE = "iis"
# scan_import_directory stops walking once matched+excluded together hit
# this many entries — same "cap and say so" reasoning as
# SEARCH_ALL_COUNT_CAP, guarding against an analyst accidentally pointing
# the scan at a triage source volume instead of its (much smaller) output
# folder.
MAX_SCAN_RESULTS = 5000

# TA first: triage reaches for "this is the adversary" far more often than
# "this is fine", so the most-used tag sits on hotkey 1 under the left hand.
# Order here is display order everywhere tags render (ribbon, menus, undo).
DEFAULT_TAGS = [
    ("TA", "#c0392b", "1"),
    ("Suspicious", "#d68a2e", "2"),
    ("Benign", "#5d8a66", "3"),
]

# A plugin tab's saved UI state (plugin_ui_state) is a SPEC — which fields
# sit in which well, which sheet was open — never a result set. 64 KiB is
# far more than any such spec needs (First/Last's whole sheet list is under
# a kilobyte) and small enough that no plugin can quietly park a query
# result in the case file, where it would go stale against the evidence it
# claims to describe.
PLUGIN_UI_STATE_MAX_BYTES = 64 * 1024

META_SCHEMA = """
-- sources, tag_defs, merges and sql_tabs are AUTOINCREMENT: an id they
-- hand out is never handed out again. A plain INTEGER PRIMARY KEY gives
-- the newest row's id to the next insert once that row is deleted, and
-- every one of these ids is held somewhere that outlives the row — a view
-- handle or a tag list in another window, an undo entry, an indicator's
-- auto-tag, a widget's src_N — so a reused id turns "this no longer
-- exists" into "this is now a different table". Case files from before
-- this are rebuilt in place on open; see _migrate_ids_never_reused.
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    path        TEXT,
    table_name  TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    columns     TEXT NOT NULL,          -- json: [{name, type}]
    file_hash   TEXT,
    imported_at TEXT,
    has_fts     INTEGER NOT NULL DEFAULT 0,
    nickname    TEXT,                   -- analyst display name; `name` stays the imported file's identity
    origin      TEXT,                   -- NULL = an imported file; 'sql' = saved from the SQL pane; 'subset' = saved from a view/selection
    origin_meta TEXT,                   -- json, per origin: {sql} / {parent_source_id, parent_name, spec, ...} — see save_view_as_source; an imported file not read as plain UTF-8: {encoding, cp1252_bytes?} — see _encoding_report
    ingest      TEXT                    -- json: how the file was read, {kind, ...options} — what a session re-imports with
);
CREATE TABLE IF NOT EXISTS tag_defs (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    color  TEXT NOT NULL,
    hotkey TEXT
);
CREATE TABLE IF NOT EXISTS row_tags (
    source_id INTEGER NOT NULL,
    rid       INTEGER NOT NULL,
    tag_id    INTEGER NOT NULL,
    PRIMARY KEY (source_id, rid, tag_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_row_tags_tag ON row_tags(source_id, tag_id);
CREATE TABLE IF NOT EXISTS row_notes (
    source_id INTEGER NOT NULL,
    rid       INTEGER NOT NULL,
    note      TEXT NOT NULL,
    PRIMARY KEY (source_id, rid)
) WITHOUT ROWID;
-- Where a subset table's rows came from: one row per subset rid naming the
-- parent source and rid it was copied from — per ROW, so a subset of a merge
-- maps each row back to the member it belongs to. See save_view_as_source.
CREATE TABLE IF NOT EXISTS subset_rids (
    source_id        INTEGER NOT NULL,
    rid              INTEGER NOT NULL,
    parent_source_id INTEGER NOT NULL,
    parent_rid       INTEGER NOT NULL,
    PRIMARY KEY (source_id, rid)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS layouts (
    source_id INTEGER PRIMARY KEY,
    payload   TEXT NOT NULL
);
-- No saved_views table any more: it backed an API nothing in the app ever
-- called, and was removed with it. A case file made before then still has
-- one; nothing reads it, and it is left alone rather than dropped.
-- Legacy — presets are saved filters now (workspace-level, cross-case; see
-- Store.pop_legacy_presets and CLAUDE.md). Table kept only so a case file
-- from before that change has somewhere for its old presets to still be
-- read from and migrated out of on next open; nothing writes here anymore.
CREATE TABLE IF NOT EXISTS filter_presets (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    col_sig    TEXT NOT NULL,          -- sha256 of sorted, lowercased column names
    col_names  TEXT NOT NULL,          -- json array, original casing, for display/diffing
    payload    TEXT NOT NULL,          -- json: {filter_tree, sort, search, search_mode, search_terms}
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_filter_presets_sig ON filter_presets(col_sig);
CREATE TABLE IF NOT EXISTS merges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    source_ids    TEXT NOT NULL,       -- json array of real source ids, creation order
    created_at    TEXT,
    -- Which columns a merge of differently shaped tables exposes: 'all'
    -- (every member's columns, empty where a member lacks one) or
    -- 'shared' (only those every member has). Moot for same-shaped
    -- members, which is every merge made before mismatched ones existed.
    col_mode      TEXT NOT NULL DEFAULT 'all',
    -- 1 = expose MERGE_SOURCE_COLUMN, each member's label as a constant.
    source_column INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS open_tabs (
    source_id INTEGER PRIMARY KEY      -- signed, same convention as elsewhere: negative = merge id
);
-- The SQL pane's named sub-tabs. In the case file rather than workspace/ or
-- localStorage because a worked-out query is analysis *about this evidence*
-- ("the join that pulls 4624s against the RDP source"), not a UI preference
-- like tl.sidebar and not cross-case reusable like a saved filter — it
-- should travel with the case when it's handed to another analyst. Still
-- only ever SELECTs the analyst typed, so this stays within invariant #1:
-- no source table is touched.
CREATE TABLE IF NOT EXISTS sql_tabs (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sql  TEXT NOT NULL DEFAULT '',
    pos  INTEGER NOT NULL DEFAULT 0     -- left-to-right strip order; ties break by id
);
-- What a plugin tab (or panel) had on screen, per mount: the sheets
-- First/Last was showing, the fields a pivot had in its wells. In the case
-- file for the same reason as sql_tabs — a grouping somebody worked out is
-- analysis *about this evidence* — and per case, so a spec naming this
-- case's columns can never be restored over another case's tables.
--
-- Written only by the plugin, through winnow.tabState (static/js/plugins.js);
-- the host never writes here on a plugin's behalf. Nothing collects the
-- rows: a plugin toggled off for a while keeps its state for when it comes
-- back, and an uninstalled one leaves a row nobody reads. Both are
-- deliberate — the rows are bytes, and losing an analyst's sheets to a
-- checkbox would be the bug this table exists to fix.
--
-- SPEC ONLY, capped at PLUGIN_UI_STATE_MAX_BYTES: rows belong to the tables
-- they came from and are re-run on restore, never replayed from here.
CREATE TABLE IF NOT EXISTS plugin_ui_state (
    mount_key TEXT PRIMARY KEY,   -- 'tab:<plugin>.<id>', 'panel:…', 'page:…' (plugins.js mountKey)
    payload   TEXT NOT NULL,      -- a JSON document whose shape the plugin owns
    saved_at  TEXT NOT NULL
) WITHOUT ROWID;
-- Derived (computed) column definitions. The VALUES live in a per-source
-- sidecar table drv_<source_id> (rid INTEGER PRIMARY KEY, one TEXT column
-- per derived column) created lazily by add_derived_column — the
-- row_tags/row_notes sidecar pattern, so src_<id> is never mutated
-- (invariant #1). sources.columns stays base-only (it is the imported
-- file's identity — merge signatures and workspace header-set keys hang
-- off it); derived entries are merged into src["columns"] at read time.
CREATE TABLE IF NOT EXISTS derived_columns (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL,
    name           TEXT NOT NULL,      -- analyst-chosen; collision-checked vs base+derived
    input_column   TEXT NOT NULL,      -- a base column name
    op_id          TEXT NOT NULL,      -- timeparse.OPERATIONS key
    params         TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'building',  -- building | ready | partial | error
    parse_failures INTEGER,            -- non-empty inputs that produced NULL; set at backfill end
    created_at     TEXT,
    inferred_type  TEXT                -- number | text | empty, NULL until looked at; see below
);
-- inferred_type is what a text-valued op's values turned out to be, read
-- off the drv_ column when a backfill ends (_infer_derived_type). 'number'
-- is what types the column as one (_derived_col_entry), so sizes and
-- counts captured with a regex sort as numbers rather than as text.
-- No index on source_id on purpose: this table holds one row per derived
-- column in the whole case (tens at the very most), so a scan is one page
-- either way, and at the 64KB page size _tune sets, an index would cost
-- more file than it could ever save. The lookup IS on a hot path
-- (_source_lite_on, once per page fetch) — it's just a hot path over a
-- table that fits in a single page.
-- Named session snapshots, IN the case file rather than beside it.
--
-- A session is a point-in-time copy of the analysis layer — tags, notes,
-- layouts, derived-column definitions — for every source,
-- open in a tab or not. Keeping them here means work an analyst saved (or
-- received from someone else and loaded) travels with the case instead of
-- living in a sessions/ directory that gets left behind the moment the .db
-- is copied anywhere. A JSON file is now only produced when handing work to
-- another analyst, which is the one case that genuinely needs a separate
-- artifact.
--
-- Deliberately a SNAPSHOT table, not a session dimension on row_tags. Tags
-- stay single-valued per (source_id, rid, tag_id) — that is a WITHOUT
-- ROWID primary key, so adding a session column would rewrite the physical
-- layout of the hottest table in the case and every one of the ~70 places
-- that read it. Diffing does not need it: a snapshot already contains the
-- full tag set, so two of them can be compared by reading them.
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    saved_at TEXT NOT NULL,
    origin   TEXT,                     -- 'saved' here, or 'imported' from a file
    payload  TEXT NOT NULL             -- a winnow-case-session/1 document
);

-- Per-case settings (first key: ts_format, the case-level datetime display
-- default). In the case file, not workspace/, because "how this case's
-- timestamps read" should travel with the case when it's handed to
-- another analyst — same reasoning as sql_tabs.
CREATE TABLE IF NOT EXISTS case_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- How far a search index build (build_fts) has got: the first rid not yet
-- in fts_<source_id>, written in the same transaction as each chunk, and
-- the doc view the chunks were read through. A row here means a build was
-- started and not finished — the case closed under it — so the next build
-- carries on from next_rid instead of throwing away what is already
-- indexed. Deleted when the build finishes (has_fts=1).
CREATE TABLE IF NOT EXISTS fts_progress (
    source_id INTEGER PRIMARY KEY,
    next_rid  INTEGER NOT NULL,
    doc_sql   TEXT NOT NULL
);

-- Sidebar folders: a per-case tree an analyst sorts tables into, and which
-- a directory import reproduces from the on-disk folder structure. Case
-- data, not a UI preference — it organizes *this* evidence and should
-- travel with the .db, same reasoning as sql_tabs/sessions above (only the
-- collapsed/expanded state is a per-browser localStorage preference).
-- Purely organizational: source_folder_map keys by the SIGNED source id
-- (negative = merge, the convention open_tabs uses) so merges fold with no
-- extra machinery, and a table with no map row is simply "ungrouped" (root
-- level). Deleting a folder reparents its tables and never drops a source
-- (invariant #1 — the import record is untouched).
CREATE TABLE IF NOT EXISTS source_folders (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id INTEGER,                      -- NULL = top level
    pos       INTEGER NOT NULL DEFAULT 0    -- order among siblings
);
CREATE TABLE IF NOT EXISTS source_folder_map (
    source_id INTEGER PRIMARY KEY,          -- signed: negative = merge id
    folder_id INTEGER NOT NULL,
    pos       INTEGER NOT NULL DEFAULT 0    -- order within the folder
);

-- The case narrative: a single free-form Markdown scratchpad, distinct
-- from per-row notes. In the case file (not workspace/) because the story
-- of the investigation must travel with the .db to whoever receives it,
-- same portability argument as sessions.
CREATE TABLE IF NOT EXISTS case_notes (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    body       TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);

-- IOC watchlist: indicators the case is scanned for, and the materialised
-- hits (so they survive, are countable, and are taggable/exportable).
-- Evidence-adjacent findings, so they live in the case .db, not workspace/.
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY,
    value       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other',   -- hash|ip|domain|filename|other (advisory: sets colour)
    note        TEXT,
    auto_tag_id INTEGER,                          -- if set, matching rows get this tag on scan
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS watchlist_hits (
    watchlist_id INTEGER NOT NULL,
    source_id    INTEGER NOT NULL,
    rid          INTEGER NOT NULL,
    PRIMARY KEY (watchlist_id, source_id, rid)
) WITHOUT ROWID;
-- The PK leads with watchlist_id, which answers "this indicator's hits"
-- and nothing else. "Which rows in this table were flagged" and "which
-- indicators are on this row" are the two questions the latest-hits pane
-- and the overlap check ask, and both scanned the whole table to answer.
CREATE INDEX IF NOT EXISTS ix_watchlist_hits_row ON watchlist_hits(source_id, rid);
-- What a scan has actually looked at: one row per (indicator, table) unit
-- that completed, whether or not it matched. Without it a bare 0 means
-- both "scanned, found nothing" and "never scanned", which is the
-- difference between being able to write "not present" in a report and
-- not. Written in the same transaction as the unit's hits (_scan_unit),
-- so the record and the rows it describes can never disagree.
CREATE TABLE IF NOT EXISTS watchlist_scans (
    watchlist_id INTEGER NOT NULL,
    source_id    INTEGER NOT NULL,
    scanned_at   TEXT,
    PRIMARY KEY (watchlist_id, source_id)
) WITHOUT ROWID;

-- The case's dashboard: one JSON document (a list of widget definitions).
-- In the case .db so a dashboard travels with the case it summarises; a
-- PROFILE (workspace) is the reusable template a dashboard is applied from.
CREATE TABLE IF NOT EXISTS dashboard (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    widgets TEXT NOT NULL DEFAULT '[]'
);
-- Named dashboards: "Dashboard" was one page; now it's a function, and a
-- case can hold several named boards (a KAPE-triage board, a lateral-
-- movement board). The single-row `dashboard` above is kept only as a
-- one-way migration source for case files from before this — its contents
-- become one "Dashboard" entry here on next open (see _migrate_dashboards).
-- Case variables: named values a case carries for the plugins and people
-- working it — the engagement name, a backend API base URL, a link to the
-- scoping document. Case data (they travel with the .db), NOT secrets:
-- tokens belong in WINNOW_* environment variables, never here. A profile
-- can declare variables (some required) that seed on apply and that the
-- new-case dialog insists on.
CREATE TABLE IF NOT EXISTS case_variables (
    name        TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    description TEXT,
    required    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS dashboards (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    widgets TEXT NOT NULL DEFAULT '[]',
    pos     INTEGER NOT NULL DEFAULT 0,
    pinned  INTEGER NOT NULL DEFAULT 0,          -- 1: shown as a page tab in the top strip
    origin  TEXT                                 -- 'plugin:<fs_name>:<local_id>' for a copy of an offered board; NULL if the analyst built it
);

-- What each widget last returned. A board used to be a pure function
-- re-evaluated on every mount: the shipped KAPE board ran 26 queries on
-- every open, every card drag and every edit, and "open" mostly means
-- "reopen this case tomorrow morning". The last result is kept so a board
-- paints immediately and only the widgets marked `"live": true` re-run.
--
-- In the case file rather than in memory, deliberately, and against the
-- rule the SQL pane follows for its own results (sql.js: a result set is
-- "a snapshot of the data rather than the analysis"). The difference is
-- what the two are FOR. A SQL result is one step of a person's reasoning,
-- re-derivable by pressing Run, and the pane keeps the query. A board is
-- the standing summary of this evidence, re-derived by nobody, and the
-- cost it is avoiding is paid at exactly the moment an analyst opens the
-- case — which an in-memory cache never survives.
--
-- Keyed by the widget's own id (widgets grew one for this; see
-- _mint_widget_ids), so a reorder keeps every result. `fingerprint` is a
-- hash of the (source, query) the payload answers: change the question
-- and the old answer is not served at all. `generation` is what the
-- case's data looked like when it ran (Store.data_generation) — an
-- import or a tag write since then does not hide the number, it marks it
-- stale, and the board says so.
CREATE TABLE IF NOT EXISTS dashboard_widget_cache (
    dashboard_id INTEGER NOT NULL,
    widget_id    TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    generation   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    ran_at       TEXT NOT NULL,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dashboard_id, widget_id)
) WITHOUT ROWID;
"""

IDENT_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
# Anything that looks like a leading ISO-ish or US date; used only to pick a
# default sort column and to hint the UI, never to rewrite the stored value.
# Month-name timestamps ("JUN 23 2026 00:11:00", "23 Jun 2026", "June 23,
# 2026 5:11 PM", "Jul 28th 2026 01:02:22") — the third recognized family,
# name-first and day-first both, since exporters disagree, with the day's
# ordinal suffix optional. The alternation is timeparse.MONTH_NAME, strict
# (a weekday or a random word never matches), and _TS_MONTHS is the number
# lookup the parsers share; it's a superset of the alternation only by "sept".
_TS_MONTH_NAME = timeparse.MONTH_NAME
_TS_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]|^\d{1,2}/\d{1,2}/\d{4}"
    r"|^" + _TS_MONTH_NAME + r"\s+\d{1,2}" + timeparse.ORDINAL + r"\s*,?\s*\d{4}"
    r"|^\d{1,2}" + timeparse.ORDINAL + r"\s+" + _TS_MONTH_NAME + r"\s*,?\s*\d{4}",
    re.IGNORECASE)


def q(ident: str) -> str:
    """Quote an identifier for interpolation into SQL."""
    return '"' + ident.replace('"', '""') + '"'


# Longest run of one operator _sql_chain writes flat. A flat `a OP b OP c`
# parses into a left-deep tree one level per operator, and SQLite refuses
# a statement deeper than SQLITE_MAX_EXPR_DEPTH (1000 by default) — which
# a search over a 500-column table (flattened JSON gets there easily)
# used to hit. 64 keeps every table short of that width on exactly the
# SQL it always had.
_SQL_CHAIN_FLAT = 64


def _sql_chain(parts: list[str], op: str) -> str:
    """`parts` joined by `op` — which must be associative (OR, AND, the
    `|| ' ' ||` of _blob_expr) — as the flat chain it always was when it is
    short, and as a balanced tree of parenthesised halves when it is not,
    so depth grows with the log of the column count instead of linearly.
    Same value either way: only the grouping changes."""
    if len(parts) <= _SQL_CHAIN_FLAT:
        return op.join(parts)
    mid = len(parts) // 2
    return f"({_sql_chain(parts[:mid], op)}){op}({_sql_chain(parts[mid:], op)})"


def _blob_expr(cols: list[str]) -> str:
    """Every column concatenated into one search target — the same
    expression backs both sides of substring search: the src_<id>_doc view
    the trigram index is built over (build_fts), and every LIKE-scan
    fallback (Contains mode when no index is ready yet or never got one,
    Advanced mode's whole-query LIKE fallback, and a single Advanced-mode
    term _fts_like_pattern can't route through the index). Keeping them
    the same expression is what makes the indexed and fallback paths
    return identical results. Built by _sql_chain, so a wide table's
    search is not refused for depth."""
    return _sql_chain([f"COALESCE({q(c)},'')" for c in cols], " || ' ' || ")


def _fts_like_pattern(term: str) -> str | None:
    """The bare-LIKE pattern that routes a Contains/Advanced term through
    the trigram index (`doc LIKE ?` on the fts table), or None when this
    term has to take the blob-LIKE fallback scan instead. Two reasons a
    term can't use the index, both verified empirically:

    - Under TRIGRAM_MIN_LEN (3 chars) the trigram tokenizer has nothing to
      look up, so the "index-assisted" form degrades to scanning anyway.
    - SQLite only pushes a *bare* `LIKE ?` down onto a trigram table —
      adding an ESCAPE clause turns the plan back into a full scan. So the
      pattern here is deliberately unescaped, which is only safe when the
      term contains no LIKE wildcards: a `%`/`_` in the term would match
      as a wildcard instead of literally (a superset of right answers —
      silently wrong). Those terms go to the fallback, which escapes them.
      A backslash is fine unescaped — LIKE has no escape character at all
      unless an ESCAPE clause says so — which matters here because
      Windows paths make `\\` the single most-searched special character
      in this tool."""
    if len(term) < TRIGRAM_MIN_LEN or "%" in term or "_" in term:
        return None
    return f"%{term}%"


def _numeric_expr(sql_expr: str) -> str:
    """A REAL-valued SQL expression for a 'number'-typed column that is NULL
    for anything that doesn't actually look numeric (same pattern as
    infer_type/NUM_RE), instead of using bare CAST(... AS REAL).

    Columns are stored as TEXT (see CLAUDE.md — evidence fidelity beats sort
    elegance). SQLite's own CAST(text AS REAL) silently returns 0.0 for
    non-numeric text, so a stray "N/A"/blank/typo in an otherwise-numeric
    column is indistinguishable from a genuine 0 in a numeric sort or a
    `>`/`<` filter. Gating the cast behind the same regex ingest-time type
    inference uses means bad values come out NULL instead — SQLite sorts
    NULL to one edge rather than scattering it through real data, and a NULL
    comparison is never true, so garbage is visibly excluded rather than
    silently masquerading as zero."""
    return f"(CASE WHEN {sql_expr} REGEXP '{NUM_RE.pattern}' THEN CAST({sql_expr} AS REAL) ELSE NULL END)"


def _sqlite_has_json_each() -> bool:
    """JSON1 is built in from SQLite 3.38 and compiled into Python's own
    builds well before that, but a distro SQLite can still lack it."""
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as c:
            c.execute("SELECT value FROM json_each('[1]')").fetchall()
        return True
    except sqlite3.Error:
        return False


# An `in` filter binds its whole list as ONE JSON parameter and reads it
# back with json_each, rather than one `?` per item: a pasted list of a few
# thousand IOCs on a merge (the WHERE compiles once per member) is past
# stock SQLite's 32,766-variable limit, and a 999-line list already is on
# SQLite before 3.32. `x IN (SELECT value FROM json_each(?))` still uses a
# column index (EXPLAIN: SEARCH ... USING INDEX (x=?), LIST SUBQUERY), and
# applies the column's affinity to the values exactly as the list did.
_HAS_JSON_EACH = _sqlite_has_json_each()


# json_array takes at most SQLITE_MAX_FUNCTION_ARG arguments — 127 unless
# the build raised it — so a page's cells go to it this many at a time
# (Store.fetch_rows_json; a 420-column JSON import is four calls a row).
JSON_CELLS_PER_CALL = 100


def _cells_json_sql(cols: list[str]) -> str:
    """The SELECT list that writes a row's cells as JSON, one json_array
    per JSON_CELLS_PER_CALL columns; _cells_json joins what it returns.

    A cell is TEXT, NULL or a BLOB, never a number: every column a page
    reads is declared TEXT (every import path's CREATE TABLE, and each
    derived column's ADD COLUMN), and TEXT affinity stores a number as
    its text. That matters, because a REAL is the one value SQLite's JSON
    writes differently from json.dumps (15 significant digits, where
    Python writes the shortest that reads back exactly). Text it escapes
    exactly as json.dumps(ensure_ascii=False) does, which
    Store._sql_json_pages checks before trusting it; a BLOB it refuses,
    and the page falls back to json.dumps, which refuses it too."""
    return ", ".join(
        "json_array(" + ", ".join(q(c) for c in cols[i:i + JSON_CELLS_PER_CALL]) + ")"
        for i in range(0, len(cols), JSON_CELLS_PER_CALL))


def _cells_json(parts: Sequence[str]) -> str:
    """_cells_json_sql's arrays as one. Each holds at least one cell."""
    return parts[0] if len(parts) == 1 else "[" + ",".join(p[1:-1] for p in parts) + "]"


def _page_row_json(pos: int, source_id: int, rid: int, cells: str, tags: list[int], note: Any) -> str:
    """One row of a page as json.dumps writes fetch_rows' dict for it, key
    order included."""
    return (f'{{"pos":{pos},"source_id":{source_id},"rid":{rid},"cells":{cells},'
            f'"tags":[{",".join(map(str, tags))}],'
            f'"note":{json.dumps(note, ensure_ascii=False, allow_nan=False)}}}')


def _page_json(start: int, rows: Iterable[str]) -> bytes:
    return f'{{"start":{start},"rows":[{",".join(rows)}]}}'.encode("utf-8")


def _regexp(pattern: str, value: Any) -> bool:
    """SQLite's REGEXP. An unusable pattern RAISES (SQLite reports it as a
    failed statement): it used to swallow re.error per row, so `C:\\Users`
    in regex mode (\\U is a bad escape in Python re) answered 0 rows — "not
    in this table" — instead of saying the pattern was broken. The filter,
    search and raw-fragment paths check the pattern before the build
    (_compiled_regex) for a readable 400; this is the backstop for SQL the
    analyst typed into the SQL pane."""
    if value is None:
        return False
    return _compiled_regex(pattern).search(str(value)) is not None


def _check_regexp_literals(sql: str) -> None:
    """A REGEXP against a literal pattern — `"Cmd" REGEXP '…'` or
    regexp('…', "Cmd") — gets the check the regex filter and search get
    (_compiled_regex) before the statement runs: a broken pattern is a
    ValueError naming it rather than SQLite's bare "user-defined function
    raised exception", and a catastrophic one is refused before a crafted
    cell can freeze the process. A pattern that only exists at run time
    (a column, a concatenation) still meets _regexp's own check."""
    for m in re.finditer(r"\bREGEXP\s*\(?\s*'((?:[^']|'')*)'", sql, re.IGNORECASE):
        _compiled_regex(m.group(1).replace("''", "'"))


@functools.lru_cache(maxsize=256)
def _compiled_regex(pattern: str) -> re.Pattern:
    """`pattern` compiled the way REGEXP runs it (case-insensitive), or a
    ValueError saying why it will not be run: it does not compile, or it
    has a shape that can backtrack catastrophically (rxguard, whose
    comment says why that is refused rather than bounded). Cached —
    REGEXP asks once per row."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Not a valid regular expression ({e}): {pattern}") from None
    hazard = rxguard.regex_hazard(pattern)
    if hazard:
        raise ValueError(
            f"Not a usable regular expression: {pattern} has {hazard}, which can backtrack "
            "catastrophically — one crafted cell would freeze Winnow until it finished, and "
            "nothing can interrupt it. End each part of the repetition at a character it cannot "
            r"match ((\w+\.)+ or (\S+\s+)+ rather than (\w+\.?)+ or (\w+\s*)+), use a "
            "class such as [ab] rather than (a|b), or search with Contains.")
    return rx


# Same two shapes DATE_RE recognizes at ingest, capturing time-of-day too
# (all optional groups — a bare date still matches). Kept in sync with
# tsformat.js's TS_ISO_RE/TS_US_RE/TS_MONTH_RE (parseTimestamp) by hand —
# there's no shared module between the two, just the same format families.
# The 12-hour marker is timeparse.MERIDIEM, after optional fraction digits
# (not captured: TS_NORMALIZE is whole seconds) — see MERIDIEM for what
# `\s*(AM|PM|am|pm)?` straight after the seconds got wrong.
_TS_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")
_TS_US_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?"
                       r"\s*(?:" + timeparse.MERIDIEM + r")?)?")
_TS_MONTH_RE = re.compile(
    r"^(?:(" + _TS_MONTH_NAME + r")\s+(\d{1,2})" + timeparse.ORDINAL
    + r"|(\d{1,2})" + timeparse.ORDINAL + r"\s+(" + _TS_MONTH_NAME + r"))"
    r"\s*,?\s*(\d{4})"
    r"(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?\s*(?:" + timeparse.MERIDIEM + r")?)?",
    re.IGNORECASE)


def _month_parts(m):
    """(y, mo, d, hh, mi, ss) from a _TS_MONTH_RE match — the AM/PM rules
    identical to the US branch's."""
    mo = _TS_MONTHS[(m.group(1) or m.group(4)).lower()]
    d = int(m.group(2) or m.group(3))
    hh = timeparse.hour24(int(m.group(6) or 0), m.group(9))
    return int(m.group(5)), mo, d, hh, int(m.group(7) or 0), int(m.group(8) or 0)


def _day_bucket(raw: Any) -> str | None:
    """Registered as the SQL function DAY_BUCKET(x) — collapses a
    'datetime'-typed value to its calendar day ("YYYY-MM-DD"), so grouping
    by a timestamp column buckets by day instead of by exact timestamp
    (which, at second/millisecond precision, would put nearly every row in
    its own group of one). None for anything that doesn't match either
    recognized shape, same "leave it alone rather than guess" rule
    parseTimestamp follows client-side — those rows bucket together under
    a NULL group instead of being silently mis-grouped."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _TS_ISO_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _TS_US_RE.match(s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = _TS_MONTH_RE.match(s)
    if m:
        y, mo, d, _, _, _ = _month_parts(m)
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _ts_normalize(raw: Any) -> str | None:
    """Registered as the SQL function TS_NORMALIZE(x) — a zero-padded,
    lexicographically-sortable "YYYY-MM-DD HH:MM:SS" for a value in either
    shape _TS_ISO_RE/_TS_US_RE recognize, missing time-of-day treated as
    midnight. Powers the timeframe filter's range check (TS_NORMALIZE(col)
    BETWEEN ? AND ?, both bounds run through this same function) — a bare
    text/numeric comparison on the raw stored value would sort the US
    "M/D/YYYY" shape wrong (lexicographic order isn't chronological order
    for it) and mismatch on missing leading zeros either way. None for
    anything unparseable, same "leave it alone" rule as DAY_BUCKET/
    parseTimestamp — an unparseable value can't be judged in-range or not,
    so it's excluded rather than guessed at."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _TS_ISO_RE.match(s)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        return f"{y}-{mo}-{d} {h or '00'}:{mi or '00'}:{sec or '00'}"
    m = _TS_US_RE.match(s)
    if m:
        mo, d, y, h, mi, sec, ampm = m.groups()
        hh = timeparse.hour24(int(h) if h else 0, ampm)
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {hh:02d}:{int(mi or 0):02d}:{int(sec or 0):02d}"
    m = _TS_MONTH_RE.match(s)
    if m:
        y, mo, d, hh, mi, sec = _month_parts(m)
        return f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mi:02d}:{sec:02d}"
    return None


# The value _TS_ISO_RE reads with every part present, as a GLOB: four
# digits, two, two, a space or a T, then HH:MM:SS, and anything after.
_TS_ISO_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][ T][0-9][0-9]:[0-9][0-9]:[0-9][0-9]*"


# unixepoch() is SQLite 3.38's; on older builds the same number comes from
# strftime('%s') as text, cast. A third cheaper a row where it exists
# (`ts` is TEXT 'YYYY-MM-DD HH:MM:SS…' — whole seconds out of
# TS_NORMALIZE, which both read alike; tests/test_histogram.py).
_HAS_UNIXEPOCH = sqlite3.sqlite_version_info >= (3, 38, 0)


def epoch_sql(ts: str) -> str:
    """Epoch seconds of a normalized timestamp, as SQL."""
    return f"unixepoch({ts})" if _HAS_UNIXEPOCH else f"CAST(strftime('%s', {ts}) AS INTEGER)"


def raw_epoch_sql(expr: str) -> str:
    """epoch_sql(ts_normalize_sql(expr)), a step shorter: on the fast path
    the first 19 characters go to SQLite as they are, since it reads
    'YYYY-MM-DDTHH:MM:SS' exactly as it reads the same with a space, and
    building the spaced copy was a fifth of a histogram pass. `expr`
    appears several times; pass a column reference."""
    return (f"CASE WHEN typeof({expr}) = 'text' AND {expr} GLOB '{_TS_ISO_GLOB}' "
            f"THEN {epoch_sql(f'substr({expr}, 1, 19)')} ELSE {epoch_sql(f'TS_NORMALIZE({expr})')} END")


def day_bucket_sql(expr: str) -> str:
    """DAY_BUCKET(expr) as SQL, the common case done by SQLite — the same
    shortcut as ts_normalize_sql below, for the same reason: for a TEXT
    value matching _TS_ISO_GLOB, _day_bucket returns exactly its first ten
    characters (_TS_ISO_RE's date groups, at fixed offsets). Everything
    else still goes to DAY_BUCKET. `expr` appears several times in the
    result, so pass a column reference."""
    return (f"CASE WHEN typeof({expr}) = 'text' AND {expr} GLOB '{_TS_ISO_GLOB}' "
            f"THEN substr({expr}, 1, 10) ELSE DAY_BUCKET({expr}) END")


def ts_normalize_sql(expr: str) -> str:
    """TS_NORMALIZE(expr) as SQL, with the common case done by SQLite.

    TS_NORMALIZE is a Python function, so calling it is a trip through the
    interpreter per row, and on an aggregate over a whole view that trip
    is most of the cost. Most datetime columns hold the ISO shape with the
    time in full — EvtxECmd's and MFTECmd's timestamps, among others — and for a
    TEXT value matching _TS_ISO_GLOB, _ts_normalize returns exactly its
    first ten characters, a space, and characters 12–19: _TS_ISO_RE's
    groups sit at fixed offsets once every part is present, it ignores
    what follows the seconds (a fraction, a Z, an offset), and a value
    starting with a digit has no leading space for strip() to remove.
    Everything else — a US date, a month name, a missing seconds field,
    padding, a non-ASCII digit, a number or a BLOB — still goes to
    TS_NORMALIZE, so the two cannot disagree. typeof() keeps BLOBs out of
    the fast branch: GLOB and substr read a BLOB's bytes, where
    _ts_normalize sees `b'…'` and returns None. tests/test_histogram.py
    holds the two to the same answer.

    `expr` appears several times in the result, so pass a column
    reference, not something expensive."""
    return (f"CASE WHEN typeof({expr}) = 'text' AND {expr} GLOB '{_TS_ISO_GLOB}' "
            f"THEN substr({expr}, 1, 10) || ' ' || substr({expr}, 12, 8) "
            f"ELSE TS_NORMALIZE({expr}) END")


# The Timeline's summary line (see build_timeline). Both caps are about a
# single row of a fixed-height list: a lead long enough to fill the cell on
# its own pushes every detail off the right edge, where the analyst cannot
# see it and CSS cannot tell them it is there. The raw row stays one hover
# (or one click on "raw") away, so truncating here hides nothing.
TL_LEAD_MAX = 100
TL_DETAIL_MAX = 120
TL_DETAIL_SEP = " \u00b7 "


def _tl_squash(raw: Any) -> str:
    """One line of text out of whatever a cell holds. Forensic CSVs carry
    embedded newlines and runs of padding constantly (EvtxECmd's Payload,
    an ESXi log Message), and a raw newline in a row of a fixed-height
    virtualized list renders as a gap the row has no height for."""
    if raw is None:
        return ""
    return " ".join(str(raw).split())


def _tl_cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _tl_lead(*values: Any) -> str:
    """Registered as the SQL function TL_LEAD(v1, v2, ...) — the first of
    the summary template's lead columns that this row actually filled in.

    Not COALESCE: the columns that say what happened are text columns that
    are *present and empty* far more often than they are NULL (EvtxECmd
    writes an empty MapDescription for every event it has no map for), and
    COALESCE would hand back that empty string and stop."""
    for v in values:
        s = _tl_squash(v)
        if s:
            return _tl_cap(s, TL_LEAD_MAX)
    return ""


def _tl_details(lead: Any, *pairs: Any) -> str:
    """Registered as the SQL function TL_DETAILS(lead, label1, value1, ...)
    — the summary's trailing fields, blanks dropped, joined with a middle
    dot.

    Takes the lead value so it can drop a detail that merely repeats it:
    lead columns fall back (TL_LEAD above), so on a row where the first
    candidate was empty the lead IS one of the detail columns, and
    "KeyName  KeyName" is not a summary. Repeats among the details
    themselves go the same way — a jump list whose Path and LocalPath
    agree should say it once."""
    seen = {_tl_squash(lead)}
    out = []
    for i in range(0, len(pairs) - 1, 2):
        label, value = pairs[i], pairs[i + 1]
        s = _tl_squash(value)
        if not s or s in seen:
            continue
        seen.add(s)
        s = _tl_cap(s, TL_DETAIL_MAX)
        out.append(f"{label} {s}" if label else s)
    return TL_DETAIL_SEP.join(out)


# Microseconds between 1601-01-01 (the Windows/WebKit epoch) and 1970-01-01
# (the Unix epoch) — Chromium stores every *_time/*_utc column (History's
# urls.last_visit_time, visits.visit_time, Cookies' creation_utc/expires_utc,
# downloads' start_time/end_time, ...) as microseconds since 1601-01-01.
WEBKIT_EPOCH_OFFSET_US = 11_644_473_600_000_000


def _webkit_to_iso(value: Any) -> str | None:
    """Chrome/WebKit timestamp -> ISO 8601 UTC string, or None if `value`
    doesn't actually look like one in that format. Used two ways: to
    convert a column's values on SQLite-table ingest (opt-in, see
    ingest_sqlite_table), and — the same function, not just the same idea
    — to screen candidate columns for that option's default-checked state
    in preview_sqlite_tables (a column counts as "likely a WebKit
    timestamp" if most of its sampled values round-trip through here to a
    plausible date)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    unix_us = v - WEBKIT_EPOCH_OFFSET_US
    try:
        dt = datetime.datetime.fromtimestamp(unix_us / 1_000_000, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if dt.year < 1990 or dt.year > 2100:  # in range arithmetically but not a plausible date
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"



# The pseudo-column that means "group these rows by the tags on them" rather
# than by anything in the file (see group_summary). It's in the reserved set
# below so sanitize_columns renames a real header of this name at ingest,
# which is what makes the sentinel unambiguous with a column an analyst can
# actually see.
TAG_GROUP_COLUMN = "__tag__"

# The most distinct values distinct_values hands back — the grid's "Copy
# unique values" over a whole view. Generous enough for a case's hashes or
# image paths; past it the answer is a filter first, not a clipboard holding
# a few megabytes nobody will read. Over it, the call says so (truncated)
# rather than handing back a list that silently stops partway.
DISTINCT_VALUES_MAX = 100_000

# Column names that can't be used as-is: "rid" collides with every src_<id>
# table's own primary-key column, and "rank"/"rowid" are FTS5-reserved \u2014
# CREATE VIRTUAL TABLE ... USING fts5(...) rejects a column literally named
# either of those (case-insensitive) with "reserved fts5 column name", which
# previously surfaced as build_fts() raising well after the source table
# and its rows were already committed \u2014 a CSV with a plausible header like
# "Rank" (a real column name in e.g. some log formats) failed the *entire*
# import with a confusing error, even though the data itself was fine.
#
# "__line__" is the sort key the grid sends for file order (the gutter
# header's toggle) — a real header of that name was shadowed by it, so
# sorting that column returned the rows in file order.
LINE_SORT_COLUMN = "__line__"
RESERVED_COLUMN_NAMES = {"rid", "rank", "rowid", TAG_GROUP_COLUMN, LINE_SORT_COLUMN}

# A derived column on a merge is one definition per member; the merge
# reports the least finished of them (_merge_columns). Anything unknown
# outranks them all, so it is shown rather than hidden behind 'ready'.
_DERIVED_STATUS_RANK = {"ready": 0, "partial": 1, "error": 2, "building": 3}

# sources.origin values for tables that were not imported from a file.
# NULL is an import; these two are derived from what is already in the case.
SQL_ORIGIN = "sql"          # sql_to_table — origin_meta = {sql}
SUBSET_ORIGIN = "subset"    # save_view_as_source — origin_meta names the parent and the selection

# sources.file_hash for a table with no file behind it. The hash is what a
# session matches a table by (_match_session_sources), so a table without
# one was never given its tags back — and a replacing load cleared them. A
# subset's is derived from its parent and spec (save_view_as_source);
# anything else made in-process — a SQL-pane save, a plugin's ingest_rows
# with no file — gets MADE_HASH_PREFIX and random digits, deliberately not
# derived from what it holds: the same query over the same evidence in
# another case makes a table with the same name, columns and rows, and it
# is still not this table, so a session from there must not tag it. A copy
# (copy_sources_to) keeps the hash, so a copy does match. The prefixes keep
# a synthetic hash from ever reading as a _quick_hash of a file on disk.
MADE_HASH_PREFIX = "made:"
SUBSET_HASH_PREFIX = "subset:"


def _made_table_hash() -> str:
    return MADE_HASH_PREFIX + secrets.token_hex(16)


def _is_file_hash(h) -> bool:
    """True for a fingerprint of a file on disk; False for a table made
    inside Winnow (a synthetic hash) and for no hash at all — a session
    record from before made tables had one."""
    return bool(h) and not str(h).startswith((MADE_HASH_PREFIX, SUBSET_HASH_PREFIX))

# The one column a raw-text import has (ingest_text): every physical line
# of the file, verbatim, one row each. Not "Line" — the grid's own row-
# number header already reads that.
TEXT_COLUMN = "Message"


def looks_binary(path: str) -> bool:
    """Whether a file is plainly not text — a NUL in its first 8 KB. The
    guard in front of the raw-text importer, which would otherwise happily
    turn an .exe or an .evtx into a million rows of mojibake. A UTF-16 BOM
    is exempt: that encoding IS text with NULs in it (sniff_text_encoding
    reads it correctly). BOM-less UTF-16 reads as binary here, which is
    the same "left alone" call sniff_text_encoding makes."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False
    return b"\x00" in head


def _text_line(line: str) -> str:
    r"""One raw-text row from one line read with newline="\n": the \n
    and ONE \r before it (a CRLF file's) come off, nothing else does.

    Universal newlines — Python's default for text files, and what
    str.splitlines() does — also end a line at a lone \r. A log line
    carries attacker-chosen text (the username of a failed login), so a
    \r in it forged a row of its own: "Failed password for x\rAccepted
    password for root" imported as two lines, the second reading like a
    real entry, and every row id after it stopped being the line number.
    A \r that is not part of the line ending is the line's own text."""
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


# Bytes as Windows-1252 reads them, the five it leaves undefined (0x81,
# 0x8D, 0x8F, 0x90, 0x9D) as the Latin-1 control characters of the same
# value — so every byte maps to a character of its own and none is lost.
def _cp1252_char(b: int) -> str:
    try:
        return bytes([b]).decode("cp1252")
    except UnicodeDecodeError:
        return chr(b)


_CP1252_CHARS = "".join(_cp1252_char(b) for b in range(256))
_decode_fallback = threading.local()


def _cp1252_fallback(exc: UnicodeError) -> tuple[str, int]:
    """The `errors=` every text evidence read uses instead of "replace".
    Bytes that are not valid in the file's encoding are read as the
    Windows-1252 characters they would be, rather than as U+FFFD: a UTF-8
    log with one line pasted in from a cp1252 tool keeps that line's
    accents, and the byte is still there to see. Counted per thread
    (_cp1252_fallback_count), so an import can say how many it read this
    way — decoding happens on the thread iterating the file. UTF-16 errors
    (a truncated last code unit) are not cp1252 bytes and still read as
    U+FFFD."""
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    if exc.encoding.startswith("utf-16"):
        return "\ufffd", exc.end
    bad = exc.object[exc.start:exc.end]
    _decode_fallback.n = getattr(_decode_fallback, "n", 0) + len(bad)
    return "".join([_CP1252_CHARS[b] for b in bad]), exc.end


TEXT_DECODE_ERRORS = "winnow-cp1252"
codecs.register_error(TEXT_DECODE_ERRORS, _cp1252_fallback)


def _cp1252_fallback_count() -> int:
    return getattr(_decode_fallback, "n", 0)


# How much of a file sniff_text_encoding looks at to tell UTF-8 from
# Windows-1252. Cheap to read, and enough rows of an Excel export that
# a name with an accent in it has turned up; past it, _cp1252_fallback
# still reads a stray byte right and the count still reports it.
ENCODING_PROBE_BYTES = 1 << 20
# A well-formed multi-byte UTF-8 sequence. Windows-1252 text almost never
# contains one by accident (an accented capital followed by a symbol from
# 0x80-0xBF), so its presence is what says "UTF-8 with a few bad bytes".
_UTF8_MULTIBYTE = re.compile(rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}")
# The names an import reports, per codec it opened the file with.
ENCODING_LABELS = {"utf-8-sig": "utf-8", "utf-16": "utf-16", "cp1252": "cp1252"}


def _encoding_for_head(head: bytes, complete: bool) -> str:
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"   # codec reads the BOM itself and picks LE/BE
    try:
        # final=False unless the probe is the whole file: a multi-byte
        # character cut in half at the probe's edge is not an error.
        codecs.getincrementaldecoder("utf-8")().decode(head, final=complete)
        return "utf-8-sig"
    except UnicodeDecodeError:
        pass
    # Not valid UTF-8. A file that also holds real multi-byte UTF-8 is UTF-8
    # with a few bad bytes (a corrupt line, a paste from elsewhere): reading
    # it all as cp1252 would turn every genuine "é" in it into "Ã©", and the
    # fallback handles the bad bytes one by one. Only text with no UTF-8 in
    # it at all is a legacy 8-bit file.
    if _UTF8_MULTIBYTE.search(head):
        return "utf-8-sig"
    return "cp1252"


def sniff_text_encoding(path: str) -> str:
    """The encoding to open a text evidence file with — open it with
    errors=TEXT_DECODE_ERRORS. utf-8-sig unless a UTF-16 byte-order mark
    says otherwise, or the start of the file is not UTF-8 and has no
    UTF-8 in it at all, which is Windows-1252.

    Windows PowerShell 5.1's Out-File and `>` write UTF-16LE by default,
    so UTF-16 CSVs are routine DFIR inputs; decoded as UTF-8-with-
    replacement they turn into headers full of NULs (which used to kill
    CREATE TABLE with "the query contains a null character") and
    unreadable cells. BOM-less UTF-16 is left alone — guessing without a
    BOM mislabels legitimate binary-ish text. Excel on Windows saves
    "CSV" in the ANSI code page, Windows-1252 in the west, and so do older
    tools: read as UTF-8 every accented letter became U+FFFD with no
    warning ("José" -> "Jos\ufffd"), and the byte was gone. Windows-1252
    rather than Latin-1 because it is what those files are — the two
    differ only in 0x80-0x9F, where cp1252 has the euro sign, dashes and
    curly quotes and Latin-1 has control characters nobody writes."""
    try:
        with open(path, "rb") as f:
            head = f.read(ENCODING_PROBE_BYTES)
            complete = len(head) < ENCODING_PROBE_BYTES or not f.read(1)
    except OSError:
        return "utf-8-sig"
    return _encoding_for_head(head, complete)


def decode_text_sample(raw: bytes, complete: bool = False, encoding: str | None = None) -> tuple[str, str]:
    """The first bytes of a text file -> (text, encoding label), decoded
    exactly as an import of the whole file would decode them, because the
    preview an analyst approves has to be the table they get. `complete`
    is whether `raw` is the whole file (a character cut at the end of a
    sample is not an error). Pass `encoding` (sniff_text_encoding's
    answer) when the file itself is at hand: it probes more than a
    preview's sample holds."""
    enc = encoding or _encoding_for_head(raw[:ENCODING_PROBE_BYTES],
                                         complete and len(raw) <= ENCODING_PROBE_BYTES)
    text = codecs.getincrementaldecoder(enc)(errors=TEXT_DECODE_ERRORS).decode(raw, final=complete)
    return text, ENCODING_LABELS[enc]


def _encoding_report(enc: str, fallback_bytes: int) -> dict:
    """What an import says about how it read its bytes: `encoding` is the
    label for the codec, `cp1252_bytes` how many bytes of a UTF-8 file
    were not UTF-8 and were read as Windows-1252 instead. `meta` is what
    is kept on the source (sources.origin_meta, NULL for an import that
    needs no note) — the toast is gone in seconds, and how evidence bytes
    were interpreted belongs with the table they became."""
    label = ENCODING_LABELS[enc]
    n = fallback_bytes if label == "utf-8" else 0
    meta = None
    if label == "cp1252":
        meta = {"encoding": "cp1252"}
    elif n:
        meta = {"encoding": "utf-8", "cp1252_bytes": n}
    return {"encoding": label, "cp1252_bytes": n, "meta": meta}


def sanitize_columns(raw: list[str]) -> list[str]:
    """Make header names unique and safe to quote. Blank headers become col_N.

    Dedups against every name actually emitted so far, not just first-seen
    original names \u2014 a header that already contains the disambiguation
    pattern (e.g. ["Name", "Name", "Name_1"], plausible for CSVs that have
    already been through another tool's own dedup pass) used to collide:
    the second "Name" became "Name_1", which then collided outright with the
    third column's own literal "Name_1", and CREATE TABLE failed with
    "duplicate column name" \u2014 aborting an otherwise perfectly valid import."""
    out: list[str] = []
    seen: dict[str, int] = {}
    used: set[str] = set()
    for i, name in enumerate(raw):
        # Strip C0 control characters (NUL and friends): they reach here
        # from mis-decoded files, and a NUL in a header aborts CREATE TABLE
        # with an error no analyst can act on. Mis-decoded cells are data
        # and stay as-is; a *name* must be printable and quotable.
        name = "".join(ch for ch in (name or "") if ch >= " " or ch in "\t")
        clean = name.strip().lstrip("\ufeff") or f"col_{i + 1}"
        if clean.lower() in RESERVED_COLUMN_NAMES:
            clean = f"{clean}_1"
        base = clean
        while clean.lower() in used:
            seen[base.lower()] = seen.get(base.lower(), 0) + 1
            clean = f"{base}_{seen[base.lower()]}"
        used.add(clean.lower())
        out.append(clean)
    return out


_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _ascii_fold(name: str) -> str:
    """Case-fold the way SQLite resolves identifiers: A-Z only. str.lower
    folds far more ("Ö" -> "ö"), so two names it calls equal can be two
    different columns to SQLite — and quoting one against a table that
    only has the other is not an error but, through SQLite's
    double-quoted-string fallback, the column's NAME as a string literal."""
    return name.translate(_ASCII_FOLD)


def _member_column(member_src: dict, column: dict) -> str | None:
    """The member's own name for one of a merge's columns (matched by
    _col_key), or None when it has none. A file column and a derived one
    are never the same column, whatever their names: _merge_columns
    matches file columns across members and exposes a derived one only
    when every member derives it, so a member whose "Host" is derived,
    beside another member's "Host" read from its file, lacks the merge's
    "Host" — showing the two as one would put the analyst's own values
    in a column of evidence under the evidence's name (the same reason a
    rename on the merge refuses that name). Before this, the member was
    read as its bare table and every build of the merge failed with
    "no such column"."""
    key = _col_key(column["name"])
    for c in member_src["columns"]:
        if _col_key(c["name"]) == key:
            return c["name"] if bool(c.get("derived")) == bool(column.get("derived")) else None
    return None


def _rename_in_layout(payload: dict, olds: set[str], new: str, new_is_live: bool = False) -> bool:
    """A saved layout (columns.js saveLayout's payload) with a renamed
    column's entries moved to its new name: its per-column settings, its
    place in the order and any sort on it. `olds` are the old names
    _ascii_fold-ed. An entry already under the new name is a leftover of
    a column that is gone (the rename was checked against every column
    the table has), so the renamed column's own settings replace it.

    Except on a merge with a member the rename didn't touch that has a
    column of the new name (`new_is_live`): that column is still there,
    so its entries are the ones kept, and any the renamed column had go.
    True when anything changed."""
    def renamed(n):
        return isinstance(n, str) and _ascii_fold(n) in olds

    def is_new(n):
        return isinstance(n, str) and _ascii_fold(n) == _ascii_fold(new) and not renamed(n)

    def kept(names):
        # Which of two entries for the one column survives.
        if new_is_live and any(is_new(n) for n in names):
            return lambda n: not renamed(n)
        return lambda n: not is_new(n)

    before = json.dumps(payload, sort_keys=True)
    cols = payload.get("columns")
    if isinstance(cols, dict):
        keep = kept(cols)
        payload["columns"] = {(new if renamed(k) else k): v for k, v in cols.items() if keep(k)}
    order = payload.get("order")
    if isinstance(order, list):
        keep = kept(order)
        payload["order"] = [new if renamed(n) else n for n in order if keep(n)]
    sort = payload.get("sort")
    if isinstance(sort, list):
        if new_is_live and any(isinstance(x, dict) and is_new(x.get("column")) for x in sort):
            payload["sort"] = sort = [x for x in sort if not (isinstance(x, dict) and renamed(x.get("column")))]
        for x in sort:
            if isinstance(x, dict) and renamed(x.get("column")):
                x["column"] = new
    return json.dumps(payload, sort_keys=True) != before


def _no_dq_strings(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Turn off SQLite's double-quoted-string fallback on a connection
    that runs Winnow's own SQL, where it can only ever hide a bug: every
    column name reaches SQL through q(), so a quoted name that isn't a
    column is a wrong name, and with the fallback on it reads as the
    name's text — header names paged as cell values, a filter comparing
    against a constant — instead of failing. An extra layer, not the fix:
    Connection.setconfig is Python 3.12+, so on 3.10/3.11 the checks that
    keep a wrong name from getting this far (validate_where_fragment's
    quoted-name check, _merge_columns) are what hold. The SQL pane keeps
    the fallback — `WHERE Channel = "Security"` is an ordinary habit there,
    and the analyst sees what came back."""
    if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DQS_DML"):
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
    return conn


def _ingest_record(kind: str, **options) -> str:
    """sources.ingest for a table read from a file: which reader, and every
    option that decides which rows and columns it produced. It is what a
    session load re-imports a missing table with (import_case_session) —
    a session names rows by rid, so the re-import has to produce the same
    rows in the same order, and the defaults do not (a headerless CSV
    gains a header and every rid slides by one). `kind` names a Store
    reader: csv, text, json, sqlite, xlsx, plaso, iis — or rows, for a table
    produced in-process (a plugin's parser, a SQL save), which nothing
    here can re-run."""
    return json.dumps({"kind": kind, **{k: v for k, v in options.items() if v is not None}})


def _is_network_path(path: str) -> bool:
    r"""A UNC or device path (\\host\share, //host/share, \\?\UNC\...).
    On Windows, even asking whether one exists connects to the host and
    offers it the analyst's credentials — so a path that came out of a
    file someone else wrote is refused on its shape, before anything
    touches the filesystem. Windows treats / and \ alike, hence either."""
    return len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/"


def pair_tag_names(incoming: Iterable[str], local: Iterable[str]) -> dict[str, str]:
    """{incoming tag name -> the local tag name it is}, for every incoming
    name that has one: the one rule session load, copying tables between
    cases and the session diff all match tag names by.

    The exact name first. Failing that, the local tag whose name differs
    only in case — but only when that is unambiguous on BOTH sides: one
    local tag folds to it, and no other incoming tag does. tag_defs.name
    is not unique by case, so "Malware" and "malware" can be two tags an
    analyst keeps apart on purpose; matching by name.lower() alone (what
    session load did) collapsed them, so reloading your own session moved
    rows from one to the other, while copy and diff matched exactly and
    the three disagreed. An incoming name with no partner is absent from
    the result — the caller creates it, or reports it as different."""
    local = set(local)
    incoming = list(dict.fromkeys(incoming))
    in_folds: dict[str, int] = {}
    for n in incoming:
        in_folds[n.casefold()] = in_folds.get(n.casefold(), 0) + 1
    by_fold: dict[str, list[str]] = {}
    for n in local:
        by_fold.setdefault(n.casefold(), []).append(n)
    out = {}
    for n in incoming:
        if n in local:
            out[n] = n
            continue
        fold = n.casefold()
        if in_folds[fold] == 1 and len(by_fold.get(fold, ())) == 1:
            out[n] = by_fold[fold][0]
    return out


def column_signature(cols: list[str]) -> str:
    """Order/case-independent fingerprint of a column-name set — used to
    check two sources have "the same" columns for merge eligibility. Case
    folds as SQLite does (_ascii_fold), because the merge reads every
    member through the first member's names: members whose names differ
    only in non-ASCII case would read each other's header names as data."""
    return hashlib.sha256("\x1f".join(sorted(_ascii_fold(c.strip()) for c in cols)).encode()).hexdigest()


# Python 3.10's csv refuses any line holding a NUL ("line contains NUL");
# 3.11 reads one into its cell like any other character. NULs are ordinary
# in DFIR exports (UTF-16 debris, registry data), and 3.10 is the minimum
# the README promises, so there _csv_rows escapes them past the parser.
_CSV_READS_NUL = sys.version_info >= (3, 11)
_NUL_ESC, _NUL_MARK = "\uffff", "\ufffe"     # noncharacters: rare, never special to csv
_NUL_UNESCAPE = re.compile(f"{_NUL_ESC}[{_NUL_ESC}{_NUL_MARK}]")


def _nul_unescape(m: re.Match) -> str:
    return _NUL_ESC if m.group()[1] == _NUL_ESC else "\0"


def _csv_rows(lines: Iterable[str], delimiter: str, *, escape_nul: bool | None = None):
    """`(reader, rows)`: a csv.reader over `lines`, and what to iterate for
    its rows — the reader itself wherever csv reads NUL. The reader is
    returned for its line_num.

    Where it does not (3.10, or `escape_nul=True`, which is how the tests
    reach this path on every version), a line holding a NUL goes in with
    each NUL written as ESC MARK and each ESC already there doubled, and the
    row comes out with that undone: the same cells 3.11 reads. Exact, not
    "rare enough": anything that was an ESC in the file comes back as one.
    Only rows that took an escaped line pay for the unescape. That is
    sound because csv asks for exactly the lines its record needs and no
    more, so the flag a line sets belongs to the row read next, and a line
    that needed no escaping holds no NUL and no ESC, so unescaping a cell
    that spans both kinds of line touches only the escaped part. Every
    line still passes through one generator here, which is why 3.11+
    skips all of it."""
    if escape_nul is None:
        escape_nul = not _CSV_READS_NUL
    if not escape_nul:
        reader = csv.reader(lines, delimiter=delimiter)
        return reader, reader
    escaped = [False]

    def escape():
        for line in lines:
            if "\0" in line or _NUL_ESC in line:
                escaped[0] = True
                line = line.replace(_NUL_ESC, _NUL_ESC * 2).replace("\0", _NUL_ESC + _NUL_MARK)
            yield line

    reader = csv.reader(escape(), delimiter=delimiter)

    def rows():
        for row in reader:
            if escaped[0]:
                escaped[0] = False
                row = [_NUL_UNESCAPE.sub(_nul_unescape, c) for c in row]
            yield row

    return reader, rows()


def _first_nonblank_row(reader: Iterable[list[str]]) -> list[str]:
    """The first row with anything in it — the header, or (without one)
    the row that fixes the table's width. Leading blank rows are skipped:
    a blank line read as the header gave a table of ZERO columns, and
    `CREATE TABLE ... (rid INTEGER PRIMARY KEY, )` failed the import with
    a raw syntax error. Same rule as the Excel import's header (the first
    non-empty row). ValueError when there is no such row."""
    for row in reader:
        if any(c.strip() for c in row):
            return row
    raise ValueError("File is empty")


def _col_key(name: str) -> str:
    """A column's identity across a merge's members: its name, ignoring
    ASCII case — "EventId" in one export and "EventID" in another are one
    column. The same folding column_signature uses, and the one SQLite
    applies to quoted names, so "Größe" and "GRÖßE" stay two columns."""
    return _ascii_fold(name.strip())


def _sqlite_same_ident(a: str, b: str) -> bool:
    """Whether SQLite resolves quoted identifier `a` to a column named `b`.
    Stricter than _col_key, which also ignores surrounding whitespace: a
    member's " EventId" is the merge's "EventId" but needs an alias."""
    return _ascii_fold(a) == _ascii_fold(b)


def _sql_text_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# The merged view's optional "which table did this row come from" column.
# Not stored anywhere: every member projects its own label as a constant.
MERGE_SOURCE_COLUMN = "From table"
MERGE_COLUMN_MODES = ("all", "shared")


def infer_type(values: Iterable[str]) -> str:
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "text"
    if all(NUM_RE.match(v) for v in vals):
        return "number"
    if sum(1 for v in vals if DATE_RE.match(v)) >= max(1, int(len(vals) * 0.8)):
        return "datetime"
    return "text"


def _open_evidence_sqlite(path: str) -> tuple[sqlite3.Connection, Callable[[], None]]:
    """A read-only connection to an external SQLite file that writes
    nothing beside it. Returns (connection, cleanup): close the
    connection, then call cleanup.

    `mode=ro` alone is not that for a WAL-mode database: opening one still
    creates its `-shm` (and a `-wal` when there isn't one) next to the
    file — in the analyst's evidence folder for a path import, and as
    litter in the temp folder beside an upload's spool, which outlived the
    spool. Two cases:
    - no `-wal` beside it: there is nothing un-checkpointed to read, and
      no connection can be writing it (a live WAL database always has its
      -wal), so `immutable=1` reads the file as it is and creates nothing.
    - a `-wal` beside it: rows that exist only there are evidence, and
      reading them needs the -shm SQLite would create. So the pair is
      copied into a private temp directory and THAT is opened; cleanup
      removes the directory. The evidence folder is never written to,
      read-only media included.
    A rollback-journal database creates nothing under `mode=ro` and opens
    as it always did. Every case builds its URI with sqlite_ro_uri, like
    every other read-only open here, so a `#` or `%` in an evidence path
    and a file on a share open the same way they do everywhere else."""
    tmpdir = None
    target, extra = path, ""
    if sqlite_header_is_wal(path):
        if os.path.exists(path + "-wal"):
            tmpdir = tempfile.mkdtemp(prefix="winnow-sqlite-")
            target = os.path.join(tmpdir, "evidence.db")
            shutil.copyfile(path, target)
            shutil.copyfile(path + "-wal", target + "-wal")
        else:
            extra = "&immutable=1"

    def cleanup() -> None:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        conn = sqlite3.connect(sqlite_ro_uri(target) + extra, uri=True, check_same_thread=False)
    except BaseException:
        cleanup()
        raise
    return conn, cleanup


def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> list:
    """An external table's columns as `PRAGMA table_xinfo` sees them,
    GENERATED columns included. `table_info` leaves those out, so an
    import silently dropped every generated column (a computed `full_url`,
    a decoded timestamp the application derives) — data the evidence
    file shows its own users. Hidden columns of a virtual table (hidden
    = 1, e.g. an FTS table's own name column) are not data and stay out;
    2 and 3 are the virtual and stored generated kinds."""
    return [c for c in conn.execute(f"PRAGMA table_xinfo({q(table)})").fetchall()
            if c["hidden"] in (0, 2, 3)]


class _JsonNumber(str):
    """A JSON number, kept as the literal text the file wrote it as.

    json.loads turns numbers into floats, and the cell then said what
    float() made of them rather than what the evidence said: 1.10 became
    1.1, 1e5 became 100000.0, 1E400 became inf, and nineteen significant
    digits became seventeen. An integer is no safer — past Python's
    4,300-digit conversion limit json.loads raised, and the whole file
    failed. Every cell is TEXT anyway (the CSV convention), so the number
    is never converted at all: this is what parse_float/parse_int/
    parse_constant build, and it is a str, so a cell is just the text.
    The one place it must not act like one is inside a container written
    back out as JSON text — _json_text writes it unquoted."""
    __slots__ = ()


class _JsonObjectWithDuplicates(dict):
    """An object that names one key more than once. json.loads keeps the
    last value and the first vanishes without a trace — an evidence
    record that said user=alice and user=root showed only root. The dict
    holds what json.loads would have (anything reading it as a dict sees
    the usual thing); `pairs` keeps every (key, value) in document order,
    which is what _flatten_json and _json_text read."""
    __slots__ = ("pairs",)


def _json_object(pairs: list) -> dict:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        obj = _JsonObjectWithDuplicates(obj)
        obj.pairs = pairs
    return obj


# Every JSON evidence read goes through these two, never bare json.load(s).
_JSON_READ = {"parse_float": _JsonNumber, "parse_int": _JsonNumber,
              "parse_constant": _JsonNumber, "object_pairs_hook": _json_object}


# json's C scanner spends the interpreter's recursion limit on nesting
# before Python 3.12 (1,000 frames by default), so a document nested about
# 1,000 deep raised RecursionError on 3.10 and 3.11 where 3.12, whose C
# code has a budget of its own, reads at least 8,000. On those two a parse
# that runs out is tried once more under this limit. Measured, they spend
# about 150 bytes of C stack a level, so 3,000 levels is under half a
# megabyte, inside the 1 MB that is the smallest default thread stack on
# the platforms Winnow supports. 0 = no retry.
_JSON_DEEP_RECURSION_LIMIT = 3000 if sys.version_info < (3, 12) else 0
_recursion_lock = threading.Lock()
_recursion_raisers = 0
_recursion_saved = 0


@contextlib.contextmanager
def _deep_json_recursion_limit():
    """sys.setrecursionlimit is process-wide, so concurrent deep reads
    share one raise: the first saves the limit, the last puts it back."""
    global _recursion_raisers, _recursion_saved
    with _recursion_lock:
        if _recursion_raisers == 0:
            _recursion_saved = sys.getrecursionlimit()
            sys.setrecursionlimit(max(_recursion_saved, _JSON_DEEP_RECURSION_LIMIT))
        _recursion_raisers += 1
    try:
        yield
    finally:
        with _recursion_lock:
            _recursion_raisers -= 1
            if _recursion_raisers == 0:
                sys.setrecursionlimit(_recursion_saved)


def _json_loads(text: str) -> Any:
    """Past what the running Python can nest at all, a JSONDecodeError
    rather than the RecursionError json raises: every caller handles the
    first, and a JSONL import counts the line as bad and keeps the rest."""
    try:
        return json.loads(text, **_JSON_READ)
    except RecursionError:
        pass
    if _JSON_DEEP_RECURSION_LIMIT:
        try:
            with _deep_json_recursion_limit():
                return json.loads(text, **_JSON_READ)
        except RecursionError:
            pass
    raise json.JSONDecodeError("Nested too deeply to read", text, 0)


def _json_load(fh) -> Any:
    return _json_loads(fh.read())     # what json.load does, through the retry above


def _json_items(obj: dict):
    return obj.pairs if isinstance(obj, _JsonObjectWithDuplicates) else obj.items()


# What json.dumps(..., ensure_ascii=False) quotes a string with — the C
# function itself, without building a JSONEncoder per call.
_json_quote = json.encoder.encode_basestring
_JSON_SCALAR = {None: "null", True: "true", False: "false"}
# How deep _json_text recurses before handing the rest to the iterative
# writer: two frames a level (the comprehension is its own frame before
# 3.12), well inside the interpreter's limit from wherever ingest calls it.
_JSON_TEXT_RECURSE = 200


def _json_text(value: Any, depth: int = 0) -> str:
    """json.dumps(value, ensure_ascii=False), character for character,
    except that a _JsonNumber is written as its literal rather than quoted
    as the str it is, and a duplicated key is written as many times as the
    file had it. json.dumps has no hook for writing a value raw, hence a
    writer of our own — recursive because that measured twice as fast as
    an explicit stack (and faster than the json.dumps call it replaced,
    which built a JSONEncoder every time), and _json_text_deep below the
    first 200 levels, because what reaches here is whatever lies under
    _flatten_json's depth cap, and a document json accepted can be
    thousands of levels deep."""
    t = type(value)
    if t is str:
        return _json_quote(value)
    if t is _JsonNumber:
        return str.__str__(value)
    if depth >= _JSON_TEXT_RECURSE:
        return _json_text_deep(value)
    if t is list:
        return "[" + ", ".join([_json_text(v, depth + 1) for v in value]) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join([_json_quote(k if type(k) is str else str(k)) + ": " + _json_text(v, depth + 1)
                               for k, v in _json_items(value)]) + "}"
    if value is None or value is True or value is False:
        return _JSON_SCALAR[value]
    return json.dumps(value)   # a Python number handed in by a caller


_JSON_END = object()


def _json_text_deep(value: Any) -> str:
    """_json_text's output for a value nested too deep to recurse into —
    the same writer as an explicit stack."""
    parts: list[str] = []
    append = parts.append
    stack: list[list] = []

    def emit(v: Any) -> None:
        if isinstance(v, (dict, list)):
            if isinstance(v, dict):
                append("{")
                stack.append([iter(_json_items(v)), "}", True])
            else:
                append("[")
                stack.append([iter(v), "]", True])
        else:
            append(_json_text(v))   # a scalar: no recursion

    emit(value)
    while stack:
        frame = stack[-1]
        item = next(frame[0], _JSON_END)
        if item is _JSON_END:
            append(frame[1])
            stack.pop()
            continue
        if frame[2]:
            frame[2] = False
        else:
            append(", ")
        if frame[1] == "}":
            key, item = item
            append(_json_quote(key if type(key) is str else str(key)))
            append(": ")
        emit(item)
    return "".join(parts)


# JSON's "\udc80" (legal, and how Plaso's serializer writes an NTFS name
# holding an unpaired surrogate) decodes to a lone surrogate, which has no
# UTF-8 encoding: the batch insert raised "surrogates not allowed" and the
# import stopped there — in a key, before the first row. Written back as
# the six characters of the escape the file had: exactly what was in the
# evidence, and still valid inside JSON text. See structparse.utf8_text.
_utf8_text = structparse.utf8_text


def _lenient_text_factory(undecodable: list[int] | None = None):
    """sqlite3 text_factory for an EXTERNAL database's TEXT values. The
    default decodes strictly, and one stored value that isn't UTF-8 (a
    careless writer, a truncated multi-byte character) raised "Could not
    decode to UTF-8" mid-read: the batch it was in and every row after it
    were lost. Decoded with U+FFFD instead, the same rule CSV ingest's
    errors="replace" follows, and counted in `undecodable[0]` so the
    import can say how many values that touched. BLOBs never come
    through here — sqlite3 only calls a text_factory for TEXT."""
    def decode(b: bytes) -> str:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            if undecodable is not None:
                undecodable[0] += 1
            return b.decode("utf-8", "replace")
    return decode


def _json_leaf_text(value: Any) -> str:
    """A JSON value at a point flattening stops (see _flatten_json) ->
    TEXT, same all-values-are-TEXT convention as CSV ingest. An object or
    array is JSON-stringified whole rather than dropped or half-flattened —
    evidence fidelity over tidiness, same reasoning CLAUDE.md gives for not
    typing columns at ingest. A number is the text the file wrote
    (_JsonNumber)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return _utf8_text(_json_text(value))
    return _utf8_text(str(value))


def _storable_cell(value: Any) -> Any:
    """A derived value SQLite can bind: what it already takes as is, and
    anything else as text with any lone surrogate (half a UTF-16 pair,
    which UTF-8 cannot spell) written as its escape (_utf8_text — the
    same rule JSON import and structparse's extraction follow). Only
    reached when a batch's first write was refused — see
    backfill_derived_columns — so the common path never pays for it."""
    if value is None or isinstance(value, (int, float, bytes)):
        return value
    return _utf8_text(str(value))


def _flatten_json(obj: dict, max_depth: int | None, clashes: dict | None = None) -> dict[str, str]:
    """Flattens nested OBJECTS into dot-notation keys — {"user": {"name":
    "a"}} -> {"user.name": "a"} — but never arrays: a list value is always
    JSON-stringified as one column's text, at whatever depth it's found,
    per the ingest UI's own "flatten" explanation (an array's length can
    vary record to record, so index-expanding it would make the column set
    itself vary; a nested object's key set is comparatively stable).

    max_depth=0 flattens nothing — one column per top-level key, "none"
    mode, matching CSV's one-column-per-header shape exactly. max_depth=None
    flattens without limit ("full"). Any other int is how many levels of
    *object* nesting to unfold before stringifying whatever's left ("depth
    N") — e.g. max_depth=1 unfolds `user.name` but leaves `user.address`
    (a further-nested object) as one JSON-text column.

    Two values can want one column: {"process": {"name": "evil.exe"},
    "process.name": "svchost.exe"} flattens both to `process.name`, and so
    does a key the object names twice. The dict used to keep whichever came
    last, so whoever wrote the record chose which one the analyst saw. The
    first now keeps the name and each later one takes the next free
    `_1`, `_2`… (sanitize_columns' suffix for a duplicated CSV header);
    `clashes`, when given, counts the records it happened in."""
    out: dict[str, str] = {}
    next_suffix: dict[str, int] = {}
    clashed = False

    # 64: far past any real log format, far short of the interpreter's
    # recursion limit — a pathological 3,000-level document used to walk
    # off the stack as a RecursionError instead of ingesting. Below the
    # cap the remainder is stringified, same as "depth N" mode's leftovers.
    FULL_DEPTH_CAP = 64

    def put(key: str, text: str) -> None:
        nonlocal clashed
        if key in out:
            clashed = True
            # Remembered per key, so a record naming one key 100,000 times
            # costs 100,000 steps rather than a scan from _1 each time.
            n = next_suffix.get(key, 1)
            while f"{key}_{n}" in out:
                n += 1
            next_suffix[key] = n + 1
            key = f"{key}_{n}"
        out[key] = text

    def walk(value: Any, key_path: str, depth: int) -> None:
        if isinstance(value, dict) and (depth < max_depth if max_depth is not None
                                        else depth < FULL_DEPTH_CAP):
            if not value:
                put(key_path, "{}")
                return
            # _utf8_text on keys as _json_leaf_text does on values: a lone
            # surrogate in a key failed the import before the first row.
            for k, v in _json_items(value):
                walk(v, f"{key_path}.{_utf8_text(str(k))}" if key_path else _utf8_text(str(k)), depth + 1)
        else:
            put(key_path, _json_leaf_text(value))   # UTF-8-safe, as keys are above

    for k, v in _json_items(obj):
        walk(v, _utf8_text(str(k)), 0)
    if clashed and clashes is not None:
        clashes["count"] += 1
    return out


# SQLite refuses a table past 2,000 columns, and one record with more keys
# than that (attacker-chosen header names, an environment dump) used to
# fail the WHOLE import with "too many columns". A JSON file keeps its
# first JSON_MAX_COLUMNS - 1 keys (first-seen order) as columns and folds
# every key past them into one JSON-object cell per record, so no value is
# lost and the structparse ops can lift any of them out — the Plaso
# import's `Attributes (JSON)` shape. 1,000, not 1,999: a derived-column
# sidecar joins its own columns on, and a grid of a thousand columns is
# already past what anyone reads.
JSON_MAX_COLUMNS = 1000
JSON_OVERFLOW_COLUMN = "Other fields (JSON)"


def _json_column_plan(keys: list[str]) -> tuple[list[str], Callable[[dict], list[str]]]:
    """(column names, row_of) for the flattened keys a JSON file holds,
    where row_of(flat_record) is that record's cells in column order. One
    plan for preview and ingest, so the two show the same table."""
    if len(keys) <= JSON_MAX_COLUMNS:
        return sanitize_columns(keys), lambda flat: [flat.get(k, "") for k in keys]
    kept = keys[:JSON_MAX_COLUMNS - 1]
    kept_set = set(kept)

    def row_of(flat: dict) -> list[str]:
        rest = {k: v for k, v in flat.items() if k not in kept_set}
        return [flat.get(k, "") for k in kept] + [json.dumps(rest, ensure_ascii=False) if rest else ""]

    return sanitize_columns(kept + [JSON_OVERFLOW_COLUMN]), row_of


def _iter_json_records(path: str, bad: dict | None = None) -> Iterable[dict]:
    """Yields one dict per record. `.jsonl`/`.ndjson` (one JSON value per
    line — the genuinely streamable shape, never loaded fully into memory)
    vs a single `.json` document (a JSON array of records, a single record
    object, or — tolerated rather than rejected — a bare scalar/array of
    scalars, wrapped as {"value": ...} so it still lands as one column
    instead of erroring out). A whole `.json` document has to be parsed in
    one shot (no generic streaming parser in the standard library), so
    unlike CSV/JSONL ingest, memory use scales with file size for that
    shape — acceptable at the scale this tool already targets (CLAUDE.md's
    "Known limits" already documents ingest as not built for huge files
    without extra tooling)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jsonl", ".ndjson"):
        with open(path, "r", encoding=sniff_text_encoding(path), errors=TEXT_DECODE_ERRORS) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json_loads(line)
                except json.JSONDecodeError as e:
                    # Same philosophy as CSV's ragged rows: one broken line
                    # (a truncated write, a log rotation seam) must not cost
                    # the analyst the million good lines around it. Counted
                    # and reported, never silent — and if the CALLER passes
                    # no collector (bad=None), the old raise stands, with
                    # the FILE line number the analyst needs rather than
                    # json's useless "line 1" of the single line.
                    if bad is None:
                        raise ValueError(f"Line {lineno}: {e.msg}") from e
                    bad["count"] += 1
                    if bad["count"] == 1:
                        bad["first_line"], bad["first_error"] = lineno, e.msg
                    continue
                yield obj if isinstance(obj, dict) else {"value": obj}
        return
    with open(path, "r", encoding=sniff_text_encoding(path), errors=TEXT_DECODE_ERRORS) as f:
        data = _json_load(f)
    if isinstance(data, list):
        for obj in data:
            yield obj if isinstance(obj, dict) else {"value": obj}
    elif isinstance(data, dict):
        yield data
    else:
        yield {"value": data}


def _allow_everything(*_args) -> int:
    return sqlite3.SQLITE_OK


def _clear_authorizer(conn: sqlite3.Connection) -> None:
    """Take an authorizer back off `conn`. set_authorizer(None) only means
    "none" from Python 3.11; on 3.10 it installs None AS the callback, the
    call fails on every statement, and SQLite reads that as a denial — so
    one plugin table write left the writer connection refusing every
    write, view build and import ("not authorized") until Winnow
    restarted. 3.10 has no way to remove one, so it gets a callback that
    allows everything instead. SQLite consults it only while preparing a
    statement (once per table and column the statement names), never per
    row, and a statement served from sqlite3's cache is not prepared again."""
    if sys.version_info >= (3, 11):
        conn.set_authorizer(None)
    else:
        conn.set_authorizer(_allow_everything)


class IngestCancelled(Exception):
    """An ingest job was cancelled — raised out of the ingest_* method
    after the partial source has been dropped. Callers that don't pass a
    `cancel` callable never see it."""


class UnknownFilterColumn(ValueError):
    """A filter names a column this table does not have. Raised rather than
    dropped, because a dropped condition shows MORE rows under a filter
    that claims to be applied — see _compile_condition."""

    def __init__(self, column: str):
        self.column = column
        super().__init__(
            f'This filter uses a column this table does not have: "{column}". '
            "Edit the filter, or apply it to a table that has that column.")


class MissingTable(ValueError):
    """A dashboard widget names a table this case does not have — a
    `{{evtx}}`-style placeholder that binds to nothing, or a `src_N` that
    has been deleted since the widget was written.

    A ValueError subclass so every existing `except ValueError` around the
    widget paths (the preview route, the resolve route, `_cells_preview`)
    keeps behaving exactly as it did. What the subclass adds is the table's
    NAME as a separate field, because the client has to tell two empty
    cards apart and cannot do it by reading the sentence: a card whose
    query ran and matched nothing is a finding ("no Defender alerts in
    these logs"), and a card whose artefact was never collected is a gap
    in the collection that importing one file would fill. Sniffing the
    message text for "no ... table in this case yet" would have worked
    until the day somebody reworded it.
    """

    def __init__(self, table: str | None, message: str | None = None):
        # None when the reference named no artefact anyone could import —
        # a src_N that has been deleted. Importing another file would
        # create a new table with a new id, not bring that one back, so
        # the client offers editing the widget rather than an import.
        self.table = table
        super().__init__(message or f"No \u201c{table}\u201d table in this case yet")


class ViewExpired(KeyError):
    """The view a request named is no longer there — evicted by a newer
    build, or its table dropped under an in-flight read.

    A KeyError subclass so every `except KeyError` around a view read
    keeps behaving exactly as it did; what the subclass adds is a way to
    tell this apart from the OTHER KeyError those same methods raise, a
    missing column, whose payload is the column NAME. Column names are
    user data (invariant #5), and splitting the two by looking for
    "expired" in the message — which is what every view route did before
    this type existed — meant a table carrying a column called
    `expired_at` got a 409 "that view was rebuilt" and a client that
    dutifully retried a request whose real problem was the column.
    The type carries the distinction instead, where no import can spell
    it wrong.
    """


class RecordGone(LookupError):
    """A write named a table, tag or row that is not in this case (any
    more) — almost always a second window acting on something the first
    one deleted. server.py answers 404.

    Raised from inside the write's own transaction, before anything is
    written, so the request fails with nothing committed behind it. Not a
    KeyError: the view routes map KeyError to 409 "rebuild and retry",
    and no rebuild brings a deleted tag back."""


class OpCancelled(Exception):
    """A registered cancellable operation (view/timeline build, group
    summary) was interrupted via cancel_op. server.py maps it to HTTP 499 —
    not an error, not the analyst's fault, keep what you had."""


class OpPreempted(OpCancelled):
    """A view build was interrupted to let the table the analyst just
    switched to have the writer lock — see Store._yield_the_writer.

    A SUBCLASS of OpCancelled on purpose: every path that already knows
    how to unwind a cancelled build (roll back, keep the previous view,
    report 499) does the right thing with one of these untouched. Only
    the view-job worker looks for the distinction, and only so it can
    start the build again instead of telling the analyst their search was
    cancelled — which nobody asked for."""


def _union_scope(a: list[int] | None, b: list[int] | None) -> list[int] | None:
    """The scope covering both of two scan-job scopes, None meaning every
    table (or every indicator) — see Store.start_watchlist_scan_job."""
    if a is None or b is None:
        return None
    return sorted(set(a) | set(b))


class _JobRegistry:
    """Background jobs with one live job per slot — the bookkeeping the
    search-all job (`_search_job`) does inline, pulled out so the view job
    (and any later one shaped like it: a watchlist scan, say) is the same
    few lines. A slot is whatever "one at a time" is scoped to: a source
    id for view builds, a single constant for a case-wide job.

    Its own lock, never Store.lock — a worker recording "done" must not
    queue behind a multi-second build, which is the whole reason these
    jobs exist. Records are plain dicts (status running|done|error|
    cancelled plus whatever the owner adds); `start` registers one and
    hands back the record it displaced, so the owner can cancel that
    one's work. `get` answers only for a job that is still its slot's
    live one — a poller on a superseded id gets None, which server.py
    turns into a 404 ("stop polling"), never a stale result. `closing`
    is set by Store.close(); a worker checks it before touching the
    connection.

    `request_discard` + `finish` close the race a cancel has with the
    work finishing: an owner whose cancel can miss (cancel_op has nothing
    to interrupt while a build is between statements, committing, or
    already done) flags the job under this lock, and `finish` — under the
    same lock — records a "done" that arrives after the flag as
    "cancelled" and tells the worker so, which then drops the result
    itself. Whichever of the two runs first, the result is recorded
    exactly once or dropped exactly once, never leaked."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seq = 0
        self.by_slot: dict[Any, dict] = {}
        self.closing = False

    def start(self, slot: Any, record: dict) -> tuple[dict, dict | None]:
        with self.lock:
            self.seq += 1
            record.update(job_id=self.seq, slot=slot, status="running",
                          error=None, discard=False, started_at=time.time(),
                          finished_at=None, thread=None, done_event=threading.Event())
            old = self.by_slot.get(slot)
            self.by_slot[slot] = record
            return record, old

    def get(self, job_id: int) -> dict | None:
        with self.lock:
            for job in self.by_slot.values():
                if job["job_id"] == job_id:
                    return job
            return None

    def request_discard(self, job: dict) -> bool:
        """Flags a still-running job so that its result, should the work
        land anyway, is recorded cancelled and handed back to the worker
        to drop (see finish). Returns whether the job was running — False
        means it already finished, and the owner deals with the recorded
        result instead."""
        with self.lock:
            if job["status"] != "running":
                return False
            job["discard"] = True
            return True

    def finish(self, job: dict, status: str, *, elapsed_ms: int | None = None, **result: Any) -> str:
        """Records the outcome and wakes waiters. Returns the status
        recorded — "cancelled" in place of a "done" that arrives after
        request_discard, in which case `result` is NOT recorded: the
        worker still holds it and is the one to drop it."""
        with self.lock:
            if status == "done" and job.get("discard"):
                status = "cancelled"
                result = {}
            job.update(result)
            job["status"] = status
            job["elapsed_ms"] = elapsed_ms
            job["finished_at"] = time.time()
        job["done_event"].set()
        return status

    def live(self) -> list[dict]:
        with self.lock:
            return list(self.by_slot.values())

    def running(self) -> int:
        with self.lock:
            return sum(1 for j in self.by_slot.values() if j["status"] == "running")


# ------------------------------------------------- the views scratch database

# Naming for the per-Store views database (invariant #3). The prefix is the
# only thing identifying one of these files as ours once the process that
# made it is gone, so sweep_orphan_views matches on it — don't change it
# without leaving a sweep for the old shape behind.
VIEWS_PREFIX = "winnow-views-"
VIEWS_SUFFIX = ".db"
VIEWS_DB_SUFFIXES = ("", "-wal", "-shm")  # the three files a WAL database is


def _preferred_views_dir() -> str | None:
    """Where a *new* views database goes: /dev/shm when it's usable (tmpfs,
    Linux-only — see Store.__init__ for why that's worth 20-100%), else None
    for tempfile's own default (the platform tempdir: /tmp, or on Windows
    %TMP%/%TEMP%, which for an account with neither set is C:\\Windows\\Temp)."""
    if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        return "/dev/shm"
    return None


def _views_dirs() -> list[str]:
    """Every directory a views database could be sitting in — the preferred
    one *and* the platform tempdir, not just today's choice. A machine can
    have accumulated files in both (a /dev/shm that wasn't writable on some
    earlier run, a container where it appeared later), and the whole point of
    the sweep is to find files an earlier process left behind."""
    dirs = []
    preferred = _preferred_views_dir()
    if preferred:
        dirs.append(preferred)
    with contextlib.suppress(Exception):
        tmp = tempfile.gettempdir()
        if tmp not in dirs:
            dirs.append(tmp)
    return dirs


def _views_file_is_orphaned(path: str) -> bool:
    """Whether no live Store still owns `path`. Every Store holds an
    exclusive flock on its own views file for its entire life
    (Store.__init__), so a file this can lock has no owner left.

    Fail-safe in both directions, which is what makes it usable against a
    file another process might be mid-query on: an unreadable candidate, or
    one whose lock is held (or whose filesystem doesn't implement flock at
    all — the same OSError, and the same answer), is reported as still in
    use and therefore never deleted. On Windows there is no flock and none
    is needed: the OS refuses to unlink a file any process has open, so the
    delete in sweep_orphan_views is itself the liveness test there."""
    if fcntl is None:  # pragma: no cover - Windows
        return True
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)  # releases the probe's own lock immediately
    return True


def sweep_orphan_views() -> dict:
    """Delete views databases left behind by Winnow processes that are no
    longer running, and report `{"removed": n, "bytes_freed": n}`.

    Store.close() removes its own — but only a *clean* exit ever reaches it.
    A SIGKILL, a power loss, an OOM kill, or (before server.py grew its
    lifespan shutdown hook) simply closing the terminal did not, and every
    one of those stranded a `winnow-views-<random>.db` plus its `-wal`/`-shm`
    siblings that nothing would ever clean up again. On Linux they sit in
    /dev/shm and cost RAM until reboot; on macOS/Windows they sit in the
    platform tempdir — `C:\\Windows\\Temp` for an account with no TMP/TEMP
    set — and accumulate across reboots, one set per killed session, at
    whatever size that session's biggest materialised view reached.

    Deleting another process's file is only safe because views are
    re-derivable scratch that dies with the process anyway (invariant #3)
    *and* because _views_file_is_orphaned proves the owner is gone first —
    a second, live Winnow on the same machine keeps its own file. Called
    once at server startup; never fatal (any error just leaves the file)."""
    removed, bytes_freed = 0, 0
    for d in _views_dirs():
        try:
            entries = os.listdir(d)
        except OSError:
            continue  # tempdir unreadable or gone — nothing to sweep, not an error
        for name in entries:
            # -wal/-shm are removed with the .db they belong to, below.
            if not (name.startswith(VIEWS_PREFIX) and name.endswith(VIEWS_SUFFIX)):
                continue
            base = os.path.join(d, name)
            if not _views_file_is_orphaned(base):
                continue
            for suffix in VIEWS_DB_SUFFIXES:
                path = base + suffix
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue  # no -wal/-shm for this one; normal
                try:
                    os.remove(path)
                except OSError:
                    break  # can't remove the base => don't touch its siblings
                removed += 1
                bytes_freed += size
        for name in entries:
            # A -wal/-shm whose .db is already gone. Only a dead owner can
            # leave that shape (close() removes the .db first, and a live
            # Store always has its .db), so there's nothing to probe.
            if not name.startswith(VIEWS_PREFIX) or not name.endswith(("-wal", "-shm")):
                continue
            path = os.path.join(d, name)
            if os.path.exists(path[:-4]):  # both suffixes are 4 chars
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            removed += 1
            bytes_freed += size
    return {"removed": removed, "bytes_freed": bytes_freed}


# ----------------------------------------------------- the case "in use" lock

# A case file open in one Winnow is not safe to open in a second one: SQLite's
# own WAL locking keeps the *file* consistent, but nothing in this app
# invalidates a second process's caches, its frontend's row counts, or its
# idea of which tabs are open — and a long write (compact() is minutes by its
# own docstring) will simply fail the other process's writes once the 5s busy
# timeout runs out. On a network share it is worse than that: WAL needs a
# shared-memory -shm mapping and does not work over SMB/NFS at all, which is
# exactly the setup two analysts would use to collide.
#
# So each Store drops an advisory marker next to its case file and a second
# Winnow probes it before opening. Advisory is the operative word — this
# refuses nothing on its own. probe_case_lock() reports, server.py decides,
# and the analyst always has a way through.
CASE_LOCK_SUFFIX = ".winnow-lock"
CASE_LOCK_HEARTBEAT_SEC = 30
# Five missed beats. Generous on purpose, because the two wrong answers are
# not symmetric: calling a live lock stale silently permits the collision
# this whole mechanism exists to catch, while calling a dead lock live costs
# one extra click on the prompt's "Open anyway".
CASE_LOCK_STALE_AFTER_SEC = 150


def is_winnow_case_file(path: str) -> bool:
    """Distinguish a Winnow case from a generic SQLite file — double-
    clicking a case should OPEN it, not ingest it as data. Read-only,
    tolerant of anything unreadable.

    Also the gate in front of every Store opened on a path that already
    exists (server.py's _open_store, case registration, copy_sources_to):
    Store.__init__ runs META_SCHEMA against whatever it is handed and
    switches the file to WAL, so opening evidence AS a case writes into
    it. Lives here rather than in server.py for copy_sources_to's sake."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
        # sqlite_peek_uri: plain mode=ro on a case nothing has open left a
        # -wal/-shm pair beside it that no read-only connection can remove.
        conn = sqlite3.connect(sqlite_peek_uri(path), uri=True)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return {"sources", "tag_defs", "row_tags"} <= names
    except Exception:  # noqa: BLE001 — unreadable means "not a case"
        return False


def fold_leftover_wal(path: str) -> bool:
    """Fold a -wal left beside a case that nothing has open back into the
    case file, and let SQLite remove it and the -shm. True if they're gone.

    What leaves one: a Winnow that was killed before it closed the case
    (power cut, Task Manager, a second Ctrl+C), and — until
    sqlite_peek_uri — every read-only look at a closed case, which made an
    empty pair beside it that nothing could remove. Opening the case again
    folds a leftover in either way; this does it for a case listed on the
    home screen, so the pair doesn't sit there until then.

    Nothing here deletes a file. SQLite's own last close does: a
    connection that can write, closing while no other connection has the
    file open, checkpoints every frame into the case file first and only
    then removes the -wal and -shm, and if anything else does have it
    open it skips both and leaves the files exactly as they were. Deleting
    a -wal by hand is how committed rows get lost; this never does. A case
    a Winnow holds (probe_case_lock) is not opened at all, a file that
    isn't a case is never opened read-write (is_winnow_case_file — it
    could be evidence), and read-only media fail the open, or open
    read-only, and keep their files."""
    if not os.path.exists(path + "-wal") or probe_case_lock(path) is not None:
        return False
    if not is_winnow_case_file(path):
        return False
    try:
        # A short busy timeout: this runs inside a listing, and a file
        # someone is busy with is one to leave for next time.
        conn = sqlite3.connect(sqlite_rw_uri(path), uri=True, timeout=1.0)
        try:
            # Reading is what opens the WAL; a connection that never read
            # closes without checkpointing anything.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return not os.path.exists(path + "-wal")


def case_lock_path(case_path: str) -> str:
    # realpath, not abspath: the marker has to be ONE file per case however
    # the path to it was spelled. With abspath, a case opened through a
    # symlink (a case folder linked onto a share, say) got a marker of its
    # own beside the link, and a second Winnow opening the same file by its
    # real path probed a marker nobody held and walked straight in.
    return os.path.realpath(case_path) + CASE_LOCK_SUFFIX


def _read_lock_record(path: str) -> dict:
    """The marker's contents, or {} if there aren't any usable ones.

    A torn read is expected and benign: _CaseLock._write rewrites the file in
    place (it must — a write-to-temp-and-rename would move the flock onto an
    unlinked inode), so a probe can catch a half-written record. {} then means
    "no heartbeat signal", and probe_case_lock falls back to the flock."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _flock_state(path: str) -> str:
    """'held' | 'free' | 'unknown' — what flock says about an existing marker.

    'unknown' covers every way a filesystem can decline to answer (no fcntl
    at all on Windows, ENOLCK/EOPNOTSUPP on a share, an unreadable file).
    That's a third state rather than an error because the heartbeat is the
    other half of the answer and is checked independently."""
    if fcntl is None:  # pragma: no cover - Windows
        return "unknown"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return "unknown"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        return "held" if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK) else "unknown"
    finally:
        os.close(fd)  # releases the probe's own lock immediately
    return "free"


def _heartbeat_state(rec: dict) -> tuple[str, float | None]:
    """'live' | 'stale' | 'unknown', plus the beat's age in seconds.

    Wall-clock comparison across two machines, which is the point — it is the
    only signal that survives a filesystem without working flock. Clock skew
    is therefore real: a holder whose clock is far behind reads as stale. The
    skew would have to exceed CASE_LOCK_STALE_AFTER_SEC to matter, and the
    consequence is a missed prompt, not a wrong write."""
    hb = rec.get("heartbeat_at")
    if not isinstance(hb, (int, float)) or isinstance(hb, bool):
        return "unknown", None
    age = max(0.0, time.time() - float(hb))
    return ("live" if age <= CASE_LOCK_STALE_AFTER_SEC else "stale"), age


def probe_case_lock(case_path: str) -> dict | None:
    """Who, if anyone, already has this case open. None means free.

    Two independent signals, and either one alone is enough to report a
    conflict: the flock (exact, instant, local filesystems) and the heartbeat
    (works anywhere a file can be read, which is what covers the share). A
    marker whose flock is free *and* whose heartbeat has gone stale is the
    residue of a killed process and reports free — the next Store overwrites
    it in place.

    Deliberately biased toward reporting a conflict, the opposite of
    _views_file_is_orphaned's bias: there, a wrong answer deletes a live
    process's file, so it errs toward "in use"; here a wrong answer only
    raises a prompt the analyst can click through."""
    path = case_lock_path(case_path)
    if not os.path.exists(path):
        return None
    rec = _read_lock_record(path)
    flock_state = _flock_state(path)
    hb_state, age = _heartbeat_state(rec)
    if flock_state == "held":
        evidence = "flock"
    elif hb_state == "live":
        evidence = "heartbeat"
    elif flock_state == "unknown" and hb_state == "unknown":
        # The marker exists but says nothing and can't be locked — most
        # likely unreadable. Report it rather than assume it's junk.
        evidence = "unreadable"
    else:
        return None
    return {
        "host": rec.get("host"),
        "user": rec.get("user"),
        "pid": rec.get("pid"),
        "started_at": rec.get("started_at"),
        "heartbeat_age_sec": None if age is None else round(age, 1),
        "evidence": evidence,
        "lock_path": path,
    }


def describe_case_lock(holder: dict) -> str:
    """One line naming the holder, for the CLI refusal and the API detail."""
    who = holder.get("user") or "an unknown user"
    where = holder.get("host") or "an unknown host"
    bits = f"{who}@{where}"
    if holder.get("pid"):
        bits += f" (pid {holder['pid']})"
    if holder.get("started_at"):
        bits += f", open since {holder['started_at']}"
    if holder.get("evidence") == "unreadable":
        return f"A lock file exists at {holder['lock_path']} but can't be read"
    age = holder.get("heartbeat_age_sec")
    if age is not None:
        bits += f", last seen {int(age)}s ago"
    return bits


class _CaseLock:
    """The marker one Store holds for its case file, for its whole life.

    Modelled on the views file's flock (invariant #3) and shares its two
    liveness guarantees: server.py's _lifespan shutdown hook reaches
    Store.close() on every clean exit, and a hard kill leaves something the
    next process can recognise as dead — here the stale heartbeat, since a
    case marker can't be swept blindly the way a scratch views file can.

    Best-effort by construction. Every failure path leaves `held` False and
    lets the Store open anyway: an unwritable directory (evidence on
    read-only media), a filesystem with no flock, a marker someone else
    already holds. Refusing to open is server.py's decision, made from
    probe_case_lock *before* a Store is ever constructed — a Store that has
    got this far is one the analyst has already been asked about."""

    def __init__(self, case_path: str):
        self.path = case_lock_path(case_path)
        self.case_path = os.path.abspath(case_path)
        self.fd: int | None = None
        self.held = False
        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Held by someone else, or a filesystem without flock. Either
                # way this process doesn't own the file: don't write to it and
                # don't delete it on the way out.
                os.close(fd)
                return False
        self.fd = fd
        self.held = True
        try:
            self._write()
        except OSError:
            pass  # the flock still stands; only the heartbeat is missing
        self._thread = threading.Thread(target=self._beat, daemon=True, name="winnow-case-lock")
        self._thread.start()
        return True

    def _write(self) -> None:
        payload = json.dumps({
            "host": socket.gethostname(),
            "user": _current_user(),
            "pid": os.getpid(),
            "case_path": self.case_path,
            "started_at": self._started_at,
            "heartbeat_at": time.time(),
        }).encode("utf-8")
        # In place on the locked fd. A rename would put the flock on an
        # unlinked inode and silently un-hold the lock.
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, payload)
        with contextlib.suppress(OSError):
            os.fsync(self.fd)  # the point is that another *machine* can read it

    def _beat(self) -> None:
        while not self._stop.wait(CASE_LOCK_HEARTBEAT_SEC):
            try:
                self._write()
            except OSError:
                return  # marker gone or disk full — stop; the lock is advisory

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(5)
        self._thread = None
        if self.fd is None:
            return
        fd, self.fd, self.held = self.fd, None, False
        with contextlib.suppress(OSError):
            os.close(fd)  # drops the flock
        # Only unlink a marker that still names this process. On a
        # filesystem without flock a second Winnow the analyst opened anyway
        # will have overwritten the record with its own pid — deleting the
        # file then would leave that live process looking free to a third.
        rec = _read_lock_record(self.path)
        if rec.get("pid") == os.getpid() and rec.get("host") == socket.gethostname():
            with contextlib.suppress(OSError):
                os.remove(self.path)


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no pwd entry / no env vars
        return "unknown"


def _is_int64(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and -(1 << 63) <= v < (1 << 63)


def _list_of_dicts_problem(doc: dict, key: str, fields: dict[str, Any]) -> str | None:
    """`doc[key]`, when present, must be a list of objects whose `fields`
    (name -> type check) hold the right kind of value."""
    items = doc.get(key)
    if items is None:
        return None
    if not isinstance(items, list):
        return f"{key!r} should be a list"
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return f"{key}[{i}] should be an object"
        for name, ok in fields.items():
            if not ok(item.get(name)):
                return f"{key}[{i}].{name} is missing or the wrong type"
    return None


def session_document_problem(doc: Any) -> str | None:
    """What is wrong with the SHAPE of one table's session (the
    `winnow-session/1` export_session writes), or None if import_session
    can read it. A session file is another analyst's — foreign input — and
    a valid-JSON document of the wrong shape used to reach the importer
    and die there as a 500 ("'list' object has no attribute 'get'"),
    sometimes after writing part of itself. Checked before anything is
    written instead; only the fields the importer reads, so a newer
    exporter's extra keys still pass."""
    if not isinstance(doc, dict):
        return "a session should be a JSON object"
    if doc.get("source") is not None and not isinstance(doc["source"], dict):
        return "'source' should be an object"
    if doc.get("layout") is not None and not isinstance(doc["layout"], dict):
        return "'layout' should be an object"
    is_str = lambda v: isinstance(v, str)  # noqa: E731
    return (
        _list_of_dicts_problem(doc, "tag_defs", {"id": _is_int64, "name": is_str})
        or _list_of_dicts_problem(doc, "row_tags", {"rid": _is_int64, "tag_id": _is_int64})
        or _list_of_dicts_problem(doc, "row_notes", {"rid": _is_int64, "note": is_str})
        # A derived column's own fields are checked as it is created — a
        # bad one is a per-column warning there, never a failed import.
        or _list_of_dicts_problem(doc, "derived_columns", {})
    )


def case_session_document_problem(doc: Any) -> str | None:
    """The same check for a whole-case session (`winnow-case-session/1`):
    every table's session in it, before the first one is applied."""
    if not isinstance(doc, dict):
        return "a case session should be a JSON object"
    sources = doc.get("sources")
    if sources is None:
        return None
    if not isinstance(sources, list):
        return "'sources' should be a list"
    for i, s in enumerate(sources):
        problem = session_document_problem(s)
        if problem:
            return f"sources[{i}]: {problem}"
    return None


class Store:
    FTS_BUILD_CONCURRENCY = 2  # trigram builds are CPU/writer-bound; more just thrash

    def __init__(self, path: str, default_tags: list[tuple] | None = None):
        self.path = path
        self._default_tags = default_tags or DEFAULT_TAGS
        self.lock = threading.RLock()
        self.db = _no_dq_strings(sqlite3.connect(path, check_same_thread=False))
        self.db.row_factory = sqlite3.Row
        self.db.create_function("REGEXP", 2, _regexp, deterministic=True)
        self.db.create_function("DAY_BUCKET", 1, _day_bucket, deterministic=True)
        self.db.create_function("TS_NORMALIZE", 1, _ts_normalize, deterministic=True)
        # Timeline summaries — variadic. The INSERT that materialises a
        # timeline view runs them here on the writer; Configure tables…'
        # preview (timeline_preview) runs the same SELECT on the _reader()
        # pool, which registers them too.
        self.db.create_function("TL_LEAD", -1, _tl_lead, deterministic=True)
        self.db.create_function("TL_DETAILS", -1, _tl_details, deterministic=True)
        self._tune(self.db)
        # Materialised views live in their own on-disk database, named and
        # WAL-journalled (not the anonymous `ATTACH DATABASE ''` this used
        # to be) so that the read-only connections in _reader() can attach
        # it too — the anonymous temp attach only ever existed on the one
        # connection that ran it, which is what forced every read through
        # self.lock (old invariant #4). Still deleted on close(), same
        # lifecycle guarantee the anonymous attach gave for free.
        #
        # A *named* attach can't ride PRAGMA temp_store=MEMORY the way the
        # old anonymous one did — that pragma only covers SQLite's own TEMP
        # schema, not a file attached by path — so on plain disk this was
        # measured 20-100% slower on every views/paging/timeline benchmark
        # (bench --vs-ref). Two mitigations, both needed: /dev/shm (tmpfs,
        # Linux-only) keeps the file RAM-backed while still being a normal
        # path a second connection can ATTACH, and v.synchronous=OFF drops
        # the remaining fsyncs — safe here and only here, because views are
        # re-derivable scratch state that dies with the process anyway (a
        # crash costs a rebuild the frontend already does on any 409), never
        # evidence like the case file, where _ingest_synchronous_off
        # documents why OFF is off-limits. On macOS/Windows there's no
        # tmpfs path, so the tempdir file leans on synchronous=OFF plus the
        # OS page cache alone.
        fd, self._views_path = tempfile.mkstemp(
            suffix=VIEWS_SUFFIX, prefix=VIEWS_PREFIX, dir=_preferred_views_dir()
        )
        # The mkstemp fd is kept open and flocked for this Store's whole
        # life rather than closed straight away: it's the liveness signal
        # sweep_orphan_views() probes, so the startup janitor in a *second*
        # Winnow process can tell this file apart from one a killed process
        # abandoned. If the lock can't be taken (a filesystem without flock;
        # Windows, where fcntl doesn't exist at all) the fd is just closed —
        # the sweep's own probe fails the same way and errs toward keeping
        # the file, so the degraded mode is "not swept", never "swept while
        # in use". See _views_file_is_orphaned.
        self._views_fd: int | None = None
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._views_fd = fd
            except OSError:
                os.close(fd)
        else:  # pragma: no cover - Windows
            os.close(fd)
        # Bound, not pasted in: the tempdir is the analyst's profile on
        # Windows, and C:\Users\O'Brien\... would end the string literal.
        self.db.execute("ATTACH DATABASE ? AS v", (self._views_path,))
        # WAL on the views db is what lets _reader() connections read a
        # view concurrently with the writer materialising the next one.
        self.db.execute("PRAGMA v.journal_mode=WAL")
        self.db.execute("PRAGMA v.synchronous=OFF")
        # "This case is open" marker for a *second* Winnow to probe — see
        # _CaseLock. Taken after the database is open so a failed open can't
        # strand one, and never fatal: `held` False just means nothing will
        # warn the next process.
        self._case_lock = _CaseLock(self.path)
        self._case_lock.acquire()
        # Public because server.py has to distinguish "STORE points at this
        # case" from "STORE points at this case and can still answer" — the
        # module global outlives close() on both the case-switch path and
        # the legacy-preset migration path.
        self.closed = False
        # Read-only connection pool for the pure-read paths (paging,
        # grouping, exports, search counts — everything that goes through
        # _reader()). Guarded by its own small lock, never self.lock: the
        # entire point is that checking a reader out must not contend with
        # the writer. Connections are created lazily on first miss and
        # returned up to the cap; a burst beyond the cap opens transient
        # connections that close on return instead of pooling.
        self._reader_pool: list[sqlite3.Connection] = []
        self._reader_lock = threading.Lock()
        self._readers_closed = False
        # Readers checked out right now, so close() can wait for them: one
        # still open when the writer closes stops SQLite checkpointing the
        # -wal and removing it and the -shm (see close()).
        self._readers_out = 0
        self._readers_back = threading.Condition(self._reader_lock)
        with self.db:
            # Detect *before* CREATE TABLE IF NOT EXISTS runs, so a case file
            # that predates open_tabs gets every existing source/merge
            # backfilled as open (matching today's "everything's a tab"
            # behavior) exactly once. A case that already has the table —
            # even with zero rows, e.g. the analyst closed every tab on
            # purpose — is left alone; an empty table isn't ambiguous with
            # "never migrated" once we've checked this early.
            open_tabs_existed = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='open_tabs'"
            ).fetchone() is not None
            self.db.executescript(META_SCHEMA)
            # CREATE TABLE IF NOT EXISTS can't add a column to a case file
            # from before nicknames existed — patch those in place.
            if not any(r[1] == "nickname" for r in self.db.execute("PRAGMA table_info(sources)")):
                self.db.execute("ALTER TABLE sources ADD COLUMN nickname TEXT")
            # ...and sources.origin / origin_meta (where a derived table came
            # from — a SQL-pane save, or Save as table on a view or
            # selection). NULL reads as "an imported file", which every
            # pre-existing row is.
            # ...and sources.ingest (how the file was parsed; NULL on a
            # table from before it was recorded, which session re-import
            # treats as "guess, then check the shape").
            have = {r[1] for r in self.db.execute("PRAGMA table_info(sources)")}
            for col in ("origin", "origin_meta", "ingest"):
                if col not in have:
                    self.db.execute(f"ALTER TABLE sources ADD COLUMN {col} TEXT")
            # A table made inside Winnow before such tables were given an
            # identity (MADE_HASH_PREFIX) has a NULL hash, which no session
            # can match — so a replacing load cleared its tags and never
            # put them back. Each gets one now, once. randomblob() runs per
            # row, so no two share one, and nothing outside this case has
            # it. A session saved before this has no hash for such a table
            # and stays unmatched: import_case_session leaves the tables it
            # could be as they are rather than guess.
            self.db.execute(
                "UPDATE sources SET file_hash = ? || lower(hex(randomblob(16))) "
                "WHERE file_hash IS NULL OR file_hash = ''", (MADE_HASH_PREFIX,))
            # Same for dashboards.pinned (a board promoted into the page strip).
            if not any(r[1] == "pinned" for r in self.db.execute("PRAGMA table_info(dashboards)")):
                self.db.execute("ALTER TABLE dashboards ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            # ...and dashboards.origin. A case from before this column has
            # NULL everywhere, which reads as "the analyst built it" — so a
            # plugin re-offering a board still asks before replacing it.
            # That is the safe direction to be wrong in.
            if not any(r[1] == "origin" for r in self.db.execute("PRAGMA table_info(dashboards)")):
                self.db.execute("ALTER TABLE dashboards ADD COLUMN origin TEXT")
            # And merges' column mode / source column (mismatched merges).
            merge_cols = {r[1] for r in self.db.execute("PRAGMA table_info(merges)")}
            if "col_mode" not in merge_cols:
                self.db.execute("ALTER TABLE merges ADD COLUMN col_mode TEXT NOT NULL DEFAULT 'all'")
            if "source_column" not in merge_cols:
                self.db.execute("ALTER TABLE merges ADD COLUMN source_column INTEGER NOT NULL DEFAULT 0")
            # ...and derived_columns.inferred_type. NULL on every column a
            # case already has, which _type_unlooked_derived_columns fills
            # in on a thread of its own once the case is open.
            if not any(r[1] == "inferred_type" for r in self.db.execute("PRAGMA table_info(derived_columns)")):
                self.db.execute("ALTER TABLE derived_columns ADD COLUMN inferred_type TEXT")
            # The single-row `dashboard` becomes one named "Dashboard" entry
            # the first time a case with that older shape is opened.
            has_named = self.db.execute("SELECT 1 FROM dashboards LIMIT 1").fetchone()
            if not has_named:
                old = self.db.execute("SELECT widgets FROM dashboard WHERE id=1").fetchone()
                if old and old["widgets"] and old["widgets"] != "[]":
                    self.db.execute(
                        "INSERT INTO dashboards(name, widgets, pos) VALUES ('Dashboard', ?, 0)",
                        (old["widgets"],))
            if not open_tabs_existed:
                ids = [r[0] for r in self.db.execute("SELECT id FROM sources")]
                ids += [-r[0] for r in self.db.execute("SELECT id FROM merges")]
                if ids:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", [(i,) for i in ids]
                    )
        # Before _seed_tags, which is the first thing that can hand out an id.
        self._migrate_ids_never_reused()
        self._seed_tags()
        self._view_seq = 0
        # Its own lock for the same reason _search_job_lock exists: handing
        # out a view id must not queue behind a multi-second build. `+=` on
        # an attribute is load/add/store, so two requests arriving together
        # (two tabs, a grid rebuild racing a Timeline build) could compute
        # the same id — and then one CREATE TABLE v."view_9" raises "table
        # already exists" (a 500, since api_view maps only ValueError and
        # KeyError), or the loser wins the _views dict and its handle
        # describes a table it does not own.
        self._view_seq_lock = threading.Lock()
        self._views: dict[str, dict] = {}
        # time_histogram's bucket width per (view, column, max_buckets), so
        # a re-ask about the same view aggregates by bucket rather than by
        # second (see there). A hint, checked on every use, so its own lock
        # and no invalidation: reader threads write it, and it must not
        # queue behind the writer.
        self._histogram_widths: dict[tuple, int] = {}
        self._histogram_widths_lock = threading.Lock()
        # time_histogram's answers by the ROWS a view holds rather than by
        # view id, so a sort or a filter cleared back to the whole table
        # (a new view, the same rows) is answered without a pass. Exact,
        # unlike the width hint: see _histogram_rows_key for what may key
        # one. Under _histogram_widths_lock, newest last.
        self._histogram_results: dict[tuple, dict] = {}
        # fetch_rows_json's one question about this SQLite (_sql_json_pages).
        self._sql_json_ok: bool | None = None
        self._undo_seq = 0
        # Tag-change undo history. Entries are newest-last; each owns a
        # v.undo_<n> delta table listing the rows the op *actually*
        # changed. Scratch, not evidence — it lives in the views database
        # (invariant #3) and dies with the process, same as a view.
        self._undo: list[dict] = []
        self._maxlen_cache: dict[int, tuple[tuple, dict[str, int]]] = {}
        self._fts_threads: dict[int, threading.Thread] = {}
        self._fts_failures: set[tuple[int, str]] = set()   # logged once each (_build_fts_worker)
        self._index_threads: dict[tuple[int, str], threading.Thread] = {}
        # Caps how many trigram builds run at once. A broad action — a
        # search-all sweep over a case with many unindexed sources — calls
        # _ensure_fts_building for every one of them, and each build holds
        # the writer lock in chunks; unbounded, that swarm starves an
        # interactive build_view (a preset apply "not working at all" while
        # a sweep ran). Bounded like ingest (CPU-bound work; more just
        # thrashes), so at most FTS_BUILD_CONCURRENCY compete for the writer.
        self._fts_build_sem = threading.Semaphore(self.FTS_BUILD_CONCURRENCY)
        # Guards the two thread registries above — its own lock, not
        # self.lock, for the same reason as _search_job_lock: the ensure-*
        # helpers are called from read paths (group_summary, column_values,
        # the search sweep), and bookkeeping that waited on the connection
        # lock would make a "pure read" stall behind a long build_view
        # after all. Never held while acquiring any other lock.
        self._threads_lock = threading.Lock()
        self._fts_janitor: threading.Thread | None = None
        # The open-time typing pass over derived columns nothing has
        # typed yet (_type_unlooked_derived_columns). Joined by close().
        self._derived_typer: threading.Thread | None = None
        # Search-all job state. Its own lock, not self.lock: the worker
        # mutates the job record between per-source counts, and making that
        # wait on the connection lock would reintroduce exactly the
        # contention the per-source lock scoping exists to avoid.
        self._search_job_lock = threading.Lock()
        self._search_job: dict | None = None
        self._search_job_thread: threading.Thread | None = None
        self._search_job_seq = 0
        # Case files copy_sources_to is writing into right now. Its lock
        # covers the set's add/discard only, never the copy itself.
        self._copying_into: set[str] = set()
        self._copying_lock = threading.Lock()
        # View builds run in the background (start_view_job): one live job
        # per source, each building a *held* view that evicts nothing until
        # the client adopts it — see build_view(hold=True) / adopt_view.
        self._view_jobs = _JobRegistry()
        # The watchlist scan runs the same way (start_watchlist_scan_job):
        # one live job for the case, its writes one locked unit per
        # (indicator, source), cancelled between units.
        self._scan_jobs = _JobRegistry()

        # Ingest jobs — same fire-and-forget-with-a-registry shape as the
        # search-all job above, but plural: a directory import legitimately
        # starts many at once. The semaphore caps how many actually ingest
        # concurrently; the rest sit 'queued'. Each worker's writes still go
        # through self.lock per BATCH like any other ingest, so the cap is
        # about not stacking N ingest threads' parse work on the CPU, not
        # about connection safety.
        self._ingest_jobs: dict[int, dict] = {}
        self._ingest_jobs_lock = threading.Lock()
        self._ingest_job_seq = 0
        self._ingest_sem = threading.Semaphore(self.MAX_CONCURRENT_INGESTS)
        # Synchronous copies that build a source batch by batch on the
        # request's own thread (save_view_as_source), keyed by the new
        # source id: {"cancelled", "done" (an Event), "name"}, under the
        # jobs lock. Not _ingest_jobs — a job owns a thread close() can
        # join and a jobs-panel entry, and a copy has neither (its pool
        # thread outlives the request). close() flags each one cancelled,
        # then waits on `done`, so the partial source is dropped while the
        # writer is still open. `_closing` is set in the same locked step
        # as that snapshot, and a copy registering afterwards sees it and
        # aborts with nothing created — no gap for a copy to slip through.
        # `_closing` covers the job registry above on the same terms:
        # start_ingest_job refuses while it is set, because a derive
        # cascade asks for a job from inside a worker close() is draining
        # and would otherwise land one behind the snapshot.
        self._subset_copies: dict[int, dict] = {}
        self._closing = False
        # The SQL pane's runs in flight (run_sql_result), token →
        # {"rows", "done"}, under the jobs lock and on the copies' terms:
        # _sql_run refuses once `_closing` is set, and close() waits on
        # `done` before deleting the results file. Stopping one is
        # _pane_query's business, not this registry's — this is what the
        # pane's "Running… N rows" reads.
        self._sql_runs: dict[str, dict] = {}
        # The pane's results, held server-side and paged out
        # (winnow/sqlresults.py). Its scratch file is made on the first
        # run, not here — most sessions never open the pane.
        self.sql_results = SqlResults()

        # Cancellable-op registry (see _interruptible/cancel_op): token →
        # the connection running that op, so a cancel can interrupt() it.
        # Its own lock for the same reason _search_job_lock exists.
        self._op_lock = threading.Lock()
        self._op_conns: dict[str, sqlite3.Connection] = {}
        self._op_cancelled: set[str] = set()
        # The SQL pane's connections while each runs a query, and whether
        # this Store has stopped taking them (see _pane_query). Same lock:
        # stop_pane_queries interrupts under it, and a connection leaves
        # the set and closes under it, so an interrupt never lands on a
        # connection that is already closed. _pane_gone is how close()
        # waits for the interrupted ones to let go of the case file.
        self._pane_conns: set[sqlite3.Connection] = set()
        self._pane_stopped = False
        self._pane_gone = threading.Condition(self._op_lock)
        # Preemption (see _yield_the_writer): which build currently holds
        # the writer lock, which tokens were interrupted to make way for
        # another table rather than cancelled outright, and how many times
        # each job has been asked to stand aside. Same lock, because these
        # are read and written in the same steps as the two above.
        self._build_running: dict | None = None
        self._op_preempted: set[str] = set()
        self._build_yields: dict[str, int] = {}
        self._build_waiting = 0   # builds for the open table queued on the writer
        self._downgrade_legacy_fts()
        self._type_unlooked_derived_columns()

    def _downgrade_legacy_fts(self) -> None:
        """Sources whose fts_<id> predates the current index shape still
        have has_fts=1 pointing at a table the query code can't use — the
        original word-tokenized table (can't do substring search at all),
        or the first-generation trigram table (multi-column, detail=full:
        the new `doc LIKE ?` query form would be a SQL error against it,
        and it's ~6x the size the index needs to be — the reason the shape
        changed). Either way: treat it as not-ready (the lazy background
        rebuild in _ensure_fts_building kicks in on next use) rather than
        erroring or serving wrong results under a stale flag. No schema
        migration needed: the shape is read straight back out of
        sqlite_master's own CREATE VIRTUAL TABLE text ('detail=none' only
        appears in current-shape DDL), so this is a one-time, idempotent
        correction.

        The stale tables themselves get dropped on a background thread,
        one per transaction — for a fat-trigram case file that's most of
        the file's bulk (measured 892MB per 285MB source), and freeing it
        shouldn't block Store construction. Freed pages go to SQLite's
        freelist for reuse by this case file; the file itself only shrinks
        under a VACUUM, which nothing here runs automatically."""
        stale: list[int] = []
        with self.lock, self.db:
            rows = self.db.execute("SELECT id FROM sources WHERE has_fts=1").fetchall()
            for (source_id,) in rows:
                row = self.db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (f"fts_{source_id}",),
                ).fetchone()
                if not row or "detail=none" not in row[0]:
                    self.db.execute("UPDATE sources SET has_fts=0 WHERE id=?", (source_id,))
                    if row:
                        stale.append(source_id)
        if stale:
            t = threading.Thread(target=self._drop_stale_fts_worker, args=(stale,), daemon=True)
            self._fts_janitor = t
            t.start()

    def _type_unlooked_derived_columns(self) -> None:
        """Type the derived columns nothing has looked at yet
        (inferred_type NULL): every one in a case from before the column
        was recorded, and any whose pass ended without managing to (the
        case closing under it). Once each — the answer is stored, and a
        column is looked at again only when a pass rewrites its values.
        'building' and 'error' are left alone: one is mid-pass or was
        abandoned by a crash, the other's values were cleared. An op with
        a type of its own never needs looking at (_infer_derived_type), so
        it isn't queued.

        On a thread of its own, never here: a number column costs a full
        scan of its values, and this runs at the end of __init__, which is
        the case opening. Done inline, a case with a few million rows and
        a handful of derived columns took seconds to minutes to open, all
        of it under the writer lock and, on a case switch, server.py's
        _CASE_SWITCH — the home screen's Open did nothing visible for that
        long, and every click after it was dropped as a second open. Until
        a column's answer lands it reads as the op's own type ('text'),
        which is what every derived column read as before it was typed at
        all."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, source_id, name, op_id FROM derived_columns "
                "WHERE inferred_type IS NULL AND status IN ('ready', 'partial')").fetchall()
        todo = [dict(r) for r in rows
                if (timeparse.OPERATIONS.get(r["op_id"]) or {}).get("value_type") == "text"]
        if todo:
            t = threading.Thread(target=self._type_derived_worker, args=(todo,),
                                 name="winnow-derived-types", daemon=True)
            self._derived_typer = t
            t.start()

    def _type_derived_worker(self, todo: list[dict]) -> None:
        for d in todo:
            if self._closing:
                return
            # A drv_ table a damaged case lost is that column's problem
            # (it reads as text, as it did before), not the case's. So is a
            # scan the case closing interrupts: the column stays NULL, and
            # the next open looks again.
            with contextlib.suppress(Exception):
                with self._reader() as ro:
                    ro.set_progress_handler(lambda: 1 if self._closing else 0, 100_000)
                    try:
                        inferred = self._infer_derived_type(d, ro)
                    finally:
                        ro.set_progress_handler(None, 0)
                if not inferred:
                    continue
                with self.lock:
                    if self._closing:
                        return
                    # Only if nothing has typed it since the list was read: a
                    # pass that rewrote the values meanwhile (a re-derive, a
                    # re-parse) wrote its own answer, read off the new values,
                    # and this one was read off the old.
                    with self.db:
                        self.db.execute(
                            "UPDATE derived_columns SET inferred_type=? WHERE id=? AND op_id=?"
                            " AND inferred_type IS NULL AND status IN ('ready', 'partial')",
                            (inferred, d["id"], d["op_id"]))
            time.sleep(0.02)  # same anti-starvation yield build_fts uses

    def wait_for_derived_types(self, timeout: float | None = None) -> None:
        """Blocks until the open-time typing pass finishes (or `timeout`
        elapses). Used by tests."""
        t = self._derived_typer
        if t:
            t.join(timeout)

    def _drop_stale_fts_worker(self, source_ids: list[int]) -> None:
        for source_id in source_ids:
            name = f"fts_{source_id}"
            try:
                with self.lock, self.db:
                    # Re-check under the lock: a search may have triggered a
                    # rebuild since startup, and this name would now be the
                    # fresh index — only drop it if it's still the old shape.
                    row = self.db.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()
                    if row and "detail=none" not in row[0]:
                        self.db.execute(f"DROP TABLE IF EXISTS {q(name)}")
            except Exception:
                pass  # best-effort space reclamation — a survivor is dropped by its own rebuild
            time.sleep(0.02)

    def wait_for_fts_maintenance(self, timeout: float | None = None) -> None:
        """Blocks until the startup stale-index janitor finishes (or
        `timeout` elapses). Used by tests."""
        t = self._fts_janitor
        if t:
            t.join(timeout)

    def _ensure_fts_building(self, source_id: int) -> None:
        """Fire-and-forget: kicks off a background trigram-index build for
        this source if it doesn't already have one ready and isn't already
        building one. Callers (Contains/Advanced-mode search) call this and
        then immediately fall back to the LIKE-based path for the query
        they're already compiling — the next search against this source,
        once the build finishes, gets the fast indexed path instead.

        No-ops entirely on a pre-3.45 SQLite: the index is only ever
        queried through the trigram LIKE pushdown that version introduced,
        so on an older runtime it would cost disk and build time while
        every search still ran at fallback speed."""
        if sqlite3.sqlite_version_info < TRIGRAM_LIKE_MIN_SQLITE:
            return
        with self._threads_lock:
            existing = self._fts_threads.get(source_id)
            if existing and existing.is_alive():
                return
        # A _reader() for the has_fts check, not _source_lite/self.lock:
        # this helper is called from read paths mid-scan, and every call
        # site runs outside any writer transaction (ingest fires it after
        # its final commit), so committed state is exactly what to check.
        with self._reader() as ro:
            if self._source_lite_on(ro, source_id).get("has_fts"):
                return
        with self._threads_lock:
            existing = self._fts_threads.get(source_id)
            if existing and existing.is_alive():
                return
            t = threading.Thread(target=self._build_fts_worker, args=(source_id,), daemon=True)
            self._fts_threads[source_id] = t
        t.start()

    @staticmethod
    def search_index_supported() -> bool:
        """Whether this runtime's SQLite can use a search index at all — the
        trigram LIKE pushdown arrived in 3.45, and _ensure_fts_building
        builds nothing on anything older."""
        return sqlite3.sqlite_version_info >= TRIGRAM_LIKE_MIN_SQLITE

    def start_search_index(self, source_id: int) -> dict:
        """Start this table's search index now, in the background, rather
        than on the first Contains or Advanced search that wants it: the
        Tables manager's Build. Nothing happens to a table that has one or
        is building one (_ensure_fts_building's dedup). A merge has no index
        of its own (it searches through its tables'), so it is refused; no
        such table is a KeyError."""
        if source_id < 0:
            raise ValueError("A merge has no search index of its own — it uses its tables'")
        with self._reader() as ro:
            self._source_lite_on(ro, source_id)
        self._ensure_fts_building(source_id)
        with self._reader() as ro:
            ready = bool(self._source_lite_on(ro, source_id).get("has_fts"))
        return {"ready": ready, "building": self._is_fts_building(source_id),
                "supported": self.search_index_supported()}

    def _build_fts_worker(self, source_id: int) -> None:
        # The thread is registered (so _is_fts_building sees it and the
        # per-source dedup holds) before it waits here for a build slot —
        # bounding writer contention when many builds are requested at once
        # (the search-all sweep). The heavy, lock-holding build_fts runs
        # only once a slot is free.
        with self._fts_build_sem:
            try:
                self.build_fts(source_id)
            except Exception as e:  # noqa: BLE001
                # Best-effort background upgrade — the next search attempt
                # retries, and searches scan the table meanwhile. But said
                # once in the log: this used to be silent, which is how a
                # build that could never succeed (a table too wide for the
                # doc view's expression) went unnoticed.
                # Not when the case is closing under the build — that is
                # the build being stopped, not failing.
                key = (source_id, str(e))
                with self._threads_lock:
                    first = key not in self._fts_failures
                    self._fts_failures.add(key)
                if first and not self._closing:
                    wlog.record("warn", f"Search index for table {source_id} not built: {e} — "
                                        "searches of it scan the table instead")

    def _is_fts_building(self, source_id: int) -> bool:
        with self._threads_lock:
            t = self._fts_threads.get(source_id)
        return bool(t and t.is_alive())

    def wait_for_fts(self, source_id: int, timeout: float | None = None) -> bool:
        """Blocks until a background index build for this source finishes
        (or `timeout` elapses). Used by tests, and available for any caller
        that genuinely needs up-to-date has_fts before proceeding. Returns
        whether the index is ready by the time it returns."""
        with self._threads_lock:
            t = self._fts_threads.get(source_id)
        if t:
            t.join(timeout)
        return bool(self.get_source(source_id).get("has_fts"))

    @staticmethod
    def _column_index_name(table_name: str, column: str, purpose: str = "filter") -> str:
        # Column names are arbitrary user data (CSV headers) — hash rather
        # than interpolate one into the index name, so this never has to
        # worry about identifier-safety beyond what q() already guarantees
        # for the CREATE INDEX statement itself. purpose="filter" hashes
        # just the column, unchanged from before this had a purpose
        # argument, so index names already on disk in an existing case file
        # stay recognized; a distinct purpose (currently only "sort") salts
        # the hash so it never collides with the filter index's name — see
        # _ensure_sort_index_building for why they have to be two different
        # physical indexes.
        key = column if purpose == "filter" else f"{column}\x00{purpose}"
        h = hashlib.md5(key.encode()).hexdigest()[:12]
        return f"idx_{table_name}_{h}"

    def _column_index_exists(self, table_name: str, column: str, purpose: str = "filter") -> bool:
        name = self._column_index_name(table_name, column, purpose)
        # A _reader(), same reasoning as _ensure_fts_building's has_fts
        # check: called from read paths, always outside a writer
        # transaction, and an index only "exists" once its CREATE commits.
        with self._reader() as ro:
            row = ro.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
        return row is not None

    def _ensure_column_index_building(self, source_id: int, column: str, table_name: str | None = None) -> None:
        """Fire-and-forget: kicks off a background plain B-tree index build
        for (source_id, column) if one doesn't already exist and isn't
        already building — same pattern as _ensure_fts_building. Only
        called for SARGABLE_OPS ('equals'/'in'): those are the filter shapes
        a plain single-column index actually accelerates. 'contains'/
        'starts' are LIKE patterns (trigram FTS already covers substring
        search); numeric comparisons go through _numeric_expr, a functional
        expression a plain index on the raw column wouldn't match anyway.
        Callers keep using today's scan for the filter they're already
        compiling — the next application of this same filter, once the
        build finishes, gets the indexed path instead. Verified on a
        42 GB/11-member merged case: an EventId-equals filter went from
        6-8s per application (disk-bound full scan of every member, never
        staying cached — the working set is bigger than available RAM) to
        ~50ms once indexed.

        `table_name` lets a caller that already resolved it skip the
        get_source lookup. That isn't micro-optimisation: get_source runs a
        COUNT(DISTINCT rid) over row_tags, ~12ms on a heavily tagged source,
        and this is called on every filter compile and every group-by — once
        per member, so a merge multiplies it."""
        self._ensure_purpose_index_building(source_id, column, "filter", table_name)

    def _shape_index_columns(self, src: dict) -> list[str]:
        """The columns the shipped header sets this source matches ask to
        have indexed at import (defaults/headers.json `indexes`), as the
        table spells them. Matched as _timeline_summary_match matches a
        shape: the source CONTAINS the set's columns, case-insensitively,
        among its imported ones. Every matching set counts, not just the
        most specific — an index one shape wants is no less useful because
        a larger shape also fits."""
        from . import defaults

        head = defaults.headers()
        indexes = head.get("indexes") or {}
        if not indexes:
            return []
        have = {c["name"].strip().lower(): c["name"] for c in self._base_cols(src)}
        out: list[str] = []
        for name, cols in head["nicknames"]:
            want = indexes.get(name)
            if not want or not all(c.strip().lower() in have for c in cols):
                continue
            out += [have[c.strip().lower()] for c in want if have[c.strip().lower()] not in out]
        return out

    def _build_import_indexes(self, source_id: int) -> None:
        """The indexes an import builds as its last step, before the job
        reads as done: _shape_index_columns' filter indexes, and the sort
        index on the table's first datetime column. Each is the very index,
        by name, that first use would build in the background
        (_ensure_column_index_building, _ensure_sort_index_building), so
        either way there is one. Best-effort: the rows are in, and a table
        without an index is only slower.

        The shape indexes are up front not for the first filter's sake but
        for the planner's: once the grid has indexed a column (any sort,
        filter or Values list does), SQLite answers `WHERE EventId = '4624'
        GROUP BY RemoteHost` by walking the RemoteHost index with a table
        lookup per row, about 15x slower than a scan, and ANALYZE does not
        change its mind. With EventId indexed the WHERE is an index search
        instead. Measured on a 2.1M-row EvtxECmd table: the KAPE board
        45 s -> 10 s, Top remote hosts 10.3 s -> 0.55 s, the lateral
        movement preview 10.7 s -> 0.05 s, for 1.5 s of index build.

        The first datetime column is the one the Timeline and the histogram
        read a table by, and the first column most analysts sort it by. A
        first sort with no index sorts every row while the index builds
        beside it, for the writer lock: 5.6 s on that EvtxECmd table, 11 to
        14 s on a 4.3M-row MFTECmd one, against 0.8 s once it exists. On
        the EvtxECmd table it costs 2.4 s of import."""
        src = self._source_lite(source_id)
        table = src["table_name"]
        wanted = [(col, "filter") for col in self._shape_index_columns(src)]
        when = next((c["name"] for c in self._base_cols(src) if c["type"] == "datetime"), None)
        if when is not None:
            wanted.append((when, "sort"))
        for col, purpose in wanted:
            col = self._table_spelling(table, col)
            name = self._column_index_name(table, col, purpose)
            collate = " COLLATE NOCASE" if purpose == "sort" else ""
            try:
                with self.lock, self.db:
                    self.db.execute(f"CREATE INDEX IF NOT EXISTS {q(name)} ON {q(table)}({q(col)}{collate})")
            except sqlite3.Error as e:
                does = "filters on it scan" if purpose == "filter" else "a first sort by it sorts"
                wlog.record("warn", f"{src['name']}: no index on {col} ({e}) — {does} the table")

    def _ensure_sort_index_building(self, source_id: int, column: str, table_name: str | None = None) -> None:
        """Same fire-and-forget background build as
        _ensure_column_index_building, but for an ORDER BY column instead of
        a sargable filter — triggered from build_view for every non-numeric
        sort column, once per member for a merge.

        This can't reuse the filter index: _compile_order emits
        `col COLLATE NOCASE` for a non-numeric column, and SQLite will only
        use an index to serve a comparison whose collating sequence matches
        the index's own declared collation — verified with EXPLAIN QUERY
        PLAN both ways: a plain (BINARY) index does not serve a NOCASE
        ORDER BY (the temp B-tree stays), and a NOCASE index does not serve
        a bare `=` filter (which compares BINARY, the column's default
        collation) either. So a column that's both filtered-on and
        sorted-on ends up with two small physical indexes, not one — still
        far cheaper than unconditionally indexing every column both ways.

        Numeric columns are skipped entirely: a numeric sort goes through
        _numeric_expr, a functional expression no plain index (of either
        collation) can match — same exclusion the filter index already
        documents, enforced by build_view filtering sort_cols to
        colnames[col] != 'number' before calling this."""
        self._ensure_purpose_index_building(source_id, column, "sort", table_name)

    def _table_spelling(self, table_name: str, column: str) -> str:
        """`column` as `table_name` itself spells it. A merge reads every
        member through its first member's names, which SQLite resolves
        case-insensitively (A-Z), so a member's "HOST" is asked for as
        "Host". Anything named after the column has to use the member's
        own spelling — the index name hashes it, and list_column_indexes/
        drop_column_index look indexes up by the member's names."""
        own = self._table_own_spelling(table_name, column)
        return column if own is None else own

    def _table_own_spelling(self, table_name: str, column: str) -> str | None:
        """_table_spelling, or None when `table_name` has no such column —
        a member of a merge whose tables have different columns lacks some
        of the merge's. Matched as _col_key does, so a member's " Host" is
        the merge's "Host" too."""
        key = _col_key(column)
        with self._reader() as ro:
            names = [name for (name,) in ro.execute("SELECT name FROM pragma_table_info(?)", (table_name,))]
        if column in names:
            return column
        return next((name for name in names if _col_key(name) == key), None)

    def _ensure_purpose_index_building(self, source_id: int, column: str, purpose: str,
                                        table_name: str | None = None) -> None:
        table_name = table_name or self._source_lite(source_id)["table_name"]
        # Callers pass a merge's spelling of the column, once per member.
        # Index the member's own spelling (the name list_column_indexes
        # hashes), and nothing at all on a member that lacks the column:
        # a merge of tables with different columns asks every member for
        # every one of its columns.
        column = self._table_own_spelling(table_name, column)
        if column is None:
            return
        key = (source_id, column, purpose)
        with self._threads_lock:
            existing = self._index_threads.get(key)
            if existing and existing.is_alive():
                return
        if self._column_index_exists(table_name, column, purpose):
            return
        with self._threads_lock:
            existing = self._index_threads.get(key)
            if existing and existing.is_alive():
                return
            t = threading.Thread(target=self._build_column_index_worker,
                                 args=(column, table_name, purpose), daemon=True)
            self._index_threads[key] = t
        t.start()

    def _build_column_index_worker(self, column: str, table_name: str, purpose: str = "filter") -> None:
        try:
            name = self._column_index_name(table_name, column, purpose)
            collate = " COLLATE NOCASE" if purpose == "sort" else ""
            with self.lock, self.db:
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {q(name)} ON {q(table_name)}({q(column)}{collate})")
        except Exception:
            pass  # best-effort background upgrade — the next filter/sort application retries

    def wait_for_column_index(self, source_id: int, column: str, timeout: float | None = None,
                               purpose: str = "filter") -> bool:
        """Blocks until a background index build for (source_id, column,
        purpose) finishes (or `timeout` elapses). Used by tests. Returns
        whether the index exists by the time it returns."""
        src = self.get_source(source_id)
        table = self._index_table_for(src, column)
        column = self._table_spelling(table, column)
        with self._threads_lock:
            t = self._index_threads.get((source_id, column, purpose))
        if t:
            t.join(timeout)
        return self._column_index_exists(table, column, purpose)

    def list_column_indexes(self, source_id: int) -> list[dict]:
        """Which of this source's columns currently have one of the
        auto-created indexes (filter or sort — the Tables modal doesn't
        distinguish, both are "just an index" from the analyst's side), and
        whether one is being built right now.

        These get created behind the analyst's back (a sargable filter, a
        value dropdown, a group-by, or now a sort all trigger one) and never
        expire, so on a case where the same headers get imported and
        filtered/sorted over and over they accumulate silently and take
        real disk. This is what lets the Tables modal show them and offer a
        drop.

        The index *name* carries only an md5 of the column (see
        _column_index_name — a column name is arbitrary CSV-header text, so
        it's deliberately never interpolated into an identifier), which means
        there's no way to read a column back out of a name. Going the other
        direction works fine: hash each of the source's known columns (both
        purposes) and look for that name, which is what this does."""
        if source_id < 0:
            return []  # a merge has no table of its own; its members carry the indexes
        src = self.get_source(source_id)
        table = src["table_name"]
        with self._reader() as ro:
            existing = {
                r[0] for r in ro.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
                )
            }
        with self._threads_lock:
            building = {col for (sid, col, _purpose), t in self._index_threads.items()
                        if sid == source_id and t.is_alive()}
        out = []
        for c in src["columns"]:
            # A derived column's indexes live on the drv_<id> sidecar, where
            # its values are — see _index_table_for.
            ctable = self._index_table_for(src, c["name"], table)
            name = self._column_index_name(ctable, c["name"])
            sort_name = self._column_index_name(ctable, c["name"], "sort")
            if name in existing or sort_name in existing:
                out.append({"column": c["name"], "index_name": name if name in existing else sort_name, "building": False})
            elif c["name"] in building:
                out.append({"column": c["name"], "index_name": name, "building": True})
        return out

    def drop_column_index(self, source_id: int, column: str) -> None:
        """Drops every auto-created index (filter and/or sort — see
        _ensure_sort_index_building for why a column can have both) on this
        column. The pages go to the case file's freelist, not back to the
        OS — `compact()` is what returns them. Nothing breaks without the
        index: every query it accelerates still returns the same rows by
        scanning, and the next filter/sort/group-by on this column just
        builds it again."""
        src = self.get_source(source_id)
        names = {c["name"] for c in src["columns"]}
        if column not in names:
            raise KeyError(column)
        with self.lock, self.db:
            for purpose in ("filter", "sort"):
                name = self._column_index_name(self._index_table_for(src, column), column, purpose)
                self.db.execute(f"DROP INDEX IF EXISTS {q(name)}")

    # Slack VACUUM needs beyond a full second copy of the file, for the
    # journal/WAL it writes alongside. Deliberately generous — running out
    # of disk part-way through is the one failure mode worth refusing to
    # risk, and the analyst can always free space and retry.
    VACUUM_HEADROOM = 256 * 1024 * 1024

    def compact(self) -> dict:
        """VACUUM the case file, returning it to the size its live data
        actually needs.

        Nothing in normal operation shrinks a case file. Dropping a source,
        dropping a stale FTS index (the startup janitor frees whole
        hundreds of MB doing this), dropping a column index — all of it
        goes to SQLite's freelist for reuse by this same file, which is
        the right default (reuse is free, rewriting the file is not). But
        after a big cleanup that can be a large, permanently-parked chunk
        of disk, and VACUUM is the only thing that gives it back. It stays
        an explicit, confirmed action rather than anything automatic: it
        rewrites the entire file, so on a multi-GB case it's minutes of
        held lock, and it needs a full second copy of the file free on
        disk while it runs — checked up front here, because running out
        of space mid-VACUUM is a far worse outcome than declining.

        temp_store is forced back to FILE for the duration. _tune sets it
        to MEMORY, which is right for the sorts and temp b-trees ordinary
        queries build — but VACUUM's scratch copy of the whole database
        obeys the same pragma, and materialising a 42 GB case in RAM is
        not a thing to find out about the hard way.

        Sizes are measured as **main + -wal on both sides**, which is what
        makes the reclaim number honest without depending on a checkpoint
        succeeding: in WAL mode recently written pages live in the -wal
        until a checkpoint, so a case whose data has never been
        checkpointed reports a main-file size of one page and would show a
        nonsense negative reclaim. The WAL is still truncated on both
        sides — before so VACUUM starts from a folded-in file, after so
        the reclaimed bytes are returned to the OS rather than parked in a
        -wal — but neither checkpoint is allowed to fail the operation.
        A checkpoint that can't complete (something is holding an older
        snapshot) leaves bytes in the WAL for a later passive checkpoint
        to collect, and `wal_pending_bytes` says so; the accounting above
        already counts them as not-reclaimed, so the numbers stay true
        either way. Raising here instead would throw away a VACUUM that
        already succeeded — minutes of work on a large case."""
        with self.lock:
            self.db.commit()
            # Measured BEFORE any checkpoint, so this is the real on-disk
            # footprint whatever the checkpoint does next.
            before = os.path.getsize(self.path)
            wal_before = self._wal_size()
            # Idle pooled readers hold mmap'd views of the file; dropping
            # them keeps a shrinking file from being pinned by connections
            # nothing is using. It does NOT help with the checkpoint —
            # a reader that blocks one is mid-statement, i.e. checked out
            # of the pool and untouched by this (verified: an idle pooled
            # reader does not make TRUNCATE report busy). The retry inside
            # _checkpoint_truncate is what waits for those.
            self._drain_idle_readers()
            # One attempt, no waiting: folding the WAL in before the rewrite
            # is a nicety (VACUUM works either way, and the sizes above no
            # longer depend on it). Waiting the full budget here would just
            # double the lock-held time on the very path — a reader pinning
            # the snapshot — where the budget already gets spent below.
            self._checkpoint_truncate(timeout_s=0)
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            free = shutil.disk_usage(directory).free
            # main + wal: with the pre-checkpoint measured above, data still
            # in the -wal is data VACUUM has to write out too.
            needed = before + wal_before + self.VACUUM_HEADROOM
            if free < needed:
                raise ValueError(
                    f"Not enough free disk space to compact: VACUUM rewrites the whole file, "
                    f"so it needs about {needed // (1024 * 1024)} MB free and "
                    f"{free // (1024 * 1024)} MB is available"
                )
            self.db.execute("PRAGMA temp_store=FILE")
            try:
                self.db.execute("VACUUM")
                # VACUUM commits through the WAL, so without this the
                # rewritten (smaller) file is still sitting in the -wal and
                # nothing has been given back to the OS yet.
                checkpointed = self._checkpoint_truncate()
            finally:
                self.db.execute("PRAGMA temp_store=MEMORY")
            after = os.path.getsize(self.path)
            wal_after = self._wal_size()
        return {"before_bytes": before + wal_before, "after_bytes": after + wal_after,
                "main_after_bytes": after, "wal_pending_bytes": wal_after,
                "wal_checkpointed": checkpointed,
                "reclaimed_bytes": max(0, (before + wal_before) - (after + wal_after))}

    def _wal_size(self) -> int:
        try:
            return os.path.getsize(self.path + "-wal")
        except OSError:
            return 0

    CHECKPOINT_TIMEOUT_S = 10.0
    # close(fold_wal=True): long enough for a page fetch or a search's count
    # to finish, short enough that a long download only delays the 409.
    FOLD_TIMEOUT_S = 2.0

    def _checkpoint_truncate(self, timeout_s: float | None = None, *,
                             folded_is_enough: bool = False) -> bool:
        """wal_checkpoint(TRUNCATE) on the CASE FILE, retried until it
        really completes. True if the WAL was truncated, False if readers
        kept it pinned for the whole budget.

        Three things this gets right that the obvious one-liner doesn't:

        `main.` qualified, because an unqualified wal_checkpoint
        checkpoints **every attached database** and ORs their busy flags
        together — including the scratch views db (invariant #3), which
        every pooled reader attaches. A reader merely paging a view would
        otherwise report the case file's checkpoint as blocked (verified:
        (1,0,0) unqualified vs (0,0,0) for main. with only `v` pinned)
        and compact would refuse for a file it could have shrunk.

        The pragma reports failure in its RESULT ROW (busy=1) instead of
        raising, so a checkpoint blocked by an in-flight reader passes
        silently — the reason compact "didn't shrink" cases with live
        readers. A missing row counts as not-complete, not as success:
        a checkpoint we never saw confirmed is one we can't claim.

        And the busy timeout is scoped down for the attempts, because
        TRUNCATE invokes the connection's busy handler: with the default
        5s, one blocked attempt sits *inside* the pragma for 5s (measured
        5.03s), so a 10s budget really took ~15s and a 0.5s budget took
        5s — all of it holding self.lock, which stalls every writer.

        `folded_is_enough` also accepts a checkpoint that copied every frame
        back (log == checkpointed) but couldn't reset the WAL because a
        reader is still mid-statement: the case file then holds every write,
        which is all close(fold_wal=True) needs to know."""
        budget = self.CHECKPOINT_TIMEOUT_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget  # monotonic: an NTP step must not move the deadline
        prior = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
        # Per attempt, so the loop — not the busy handler — owns the budget.
        self.db.execute(f"PRAGMA busy_timeout={max(1, int(budget * 1000 / 4))}")
        try:
            while True:
                row = self.db.execute("PRAGMA main.wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None and row[0] == 0:
                    return True
                if folded_is_enough and row is not None and row[1] >= 0 and row[1] == row[2]:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        finally:
            self.db.execute(f"PRAGMA busy_timeout={int(prior)}")

    @staticmethod
    def _tune(conn: sqlite3.Connection) -> None:
        # page_size only takes effect on a database with no tables yet, and
        # only if set before the first write of any kind — including the
        # journal_mode=WAL switch below, which itself allocates page 1 and
        # silently locks in whatever page size was already in force
        # (verified: page_size after journal_mode=WAL is a no-op). On an
        # existing case file this pragma is a no-op regardless of order —
        # changing an established file's page size needs a VACUUM.
        conn.execute("PRAGMA page_size=65536")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-262144")  # ~256 MB page cache
        # 1 GB. mmap is file-backed and shared with the OS page cache, so
        # this costs address space (fine on 64-bit), not RAM — and it lets
        # the big sequential scans (view materialisation, LIKE fallback,
        # FTS builds) skip a read() syscall + memcpy per page on the
        # multi-GB cases this tool targets. SQLite silently clamps it to
        # its compile-time max (typically ~2 GB) — asking for more than an
        # old build allows degrades gracefully rather than erroring.
        conn.execute("PRAGMA mmap_size=1073741824")
        # Let the sorter use worker threads: a filtered/sorted view build
        # over millions of rows is one big external sort, and this is the
        # one knob that parallelises it (default is 0 = single-threaded).
        conn.execute("PRAGMA threads=4")

    # The tables whose ids must never come round again (see the comment at
    # the top of META_SCHEMA), each with every place in a case file that can
    # still hold one of its ids after the row itself is gone. A case file
    # from before AUTOINCREMENT only knows its CURRENT highest id, so the
    # migration seeds the counter past every id still mentioned anywhere —
    # an orphaned row_tags entry, a widget's `src_7`, a leftover drv_9 —
    # rather than past MAX(id) alone, which would hand a deleted newest
    # table's id straight back to the next import.
    #
    # Signed source ids (negative = merge) share open_tabs/layouts/
    # source_folder_map, so the positive half counts for
    # sources and the negated negative half for merges.
    _ID_HIGH_WATER_SQL = {
        "sources": [
            "SELECT MAX(id) FROM sources",
            *(f"SELECT MAX(source_id) FROM {t}" for t in (
                "row_tags", "row_notes", "layouts", "open_tabs",
                "derived_columns", "subset_rids", "source_folder_map",
                "watchlist_hits", "watchlist_scans")),
            "SELECT MAX(parent_source_id) FROM subset_rids",
        ],
        "tag_defs": [
            "SELECT MAX(id) FROM tag_defs",
            "SELECT MAX(tag_id) FROM row_tags",
            "SELECT MAX(auto_tag_id) FROM watchlist",
        ],
        "merges": [
            "SELECT MAX(id) FROM merges",
            *(f"SELECT -MIN(source_id) FROM {t}" for t in (
                "layouts", "open_tabs", "source_folder_map")),
        ],
        "sql_tabs": ["SELECT MAX(id) FROM sql_tabs"],
    }

    def _id_high_water(self, table: str) -> int:
        """The highest id `table` has ever been seen to hand out, as far as
        this case file can still tell (see _ID_HIGH_WATER_SQL)."""
        hw = 0
        for sql in self._ID_HIGH_WATER_SQL[table]:
            v = self.db.execute(sql).fetchone()[0]
            if isinstance(v, int):
                hw = max(hw, v)
        if table == "sources":
            # A source's own tables and views, and the text that names
            # them by id: merge member lists, widget queries, saved SQL.
            # A dropped table cleans up after itself, but a crash between
            # its statements, or a dashboard written against src_7 that
            # was deleted since, still names the id.
            ids = [int(m.group(1)) for (name,) in self.db.execute("SELECT name FROM sqlite_master")
                   if (m := re.match(r"(?:src|fts|drv)_(\d+)", name or ""))]
            for sql in ("SELECT widgets FROM dashboards", "SELECT sql FROM sql_tabs"):
                for (text,) in self.db.execute(sql):
                    ids += [int(n) for n in re.findall(r"\bsrc_(\d+)\b", text or "")]
            for (text,) in self.db.execute("SELECT source_ids FROM merges"):
                with contextlib.suppress(TypeError, ValueError):
                    ids += [n for n in json.loads(text) if isinstance(n, int)]
            hw = max([hw, *ids])
        return hw

    def _migrate_ids_never_reused(self) -> None:
        """Rebuild sources/tag_defs/merges/sql_tabs as AUTOINCREMENT in a
        case file created before they were, once.

        SQLite cannot add AUTOINCREMENT to an existing table, so each one
        is copied out, recreated from META_SCHEMA's definition, and copied
        back by column name with its ids intact. By name, and from the
        canonical DDL rather than the file's own: Store.__init__ has just
        ALTERed in any column an older file lacked, so the canonical
        column set is the one every file now has, in whatever order the
        ALTERs left it — and a column the canonical DDL does not know is
        carried across rather than dropped. Its sqlite_sequence row is
        then set to the high-water mark, never lower than what the
        copy-back itself recorded.

        The same pass clears what reuse already left behind in such a
        file, because the next id is no longer coming to claim it: tag
        assignments and notes for a table or tag that no longer exists
        (a stale window's write, an auto-tag scan after its tag was
        deleted), and an indicator's auto-tag naming a deleted tag. They
        are unreachable from anywhere in the app, but row_tags rows still
        count toward a table's tagged-row total.

        One transaction for the lot: a crash between a DROP and its
        copy-back would otherwise lose the case's table list."""
        todo = []
        for table in self._ID_HIGH_WATER_SQL:
            row = self.db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row and not re.search(r"\bAUTOINCREMENT\b", row[0], re.I):
                todo.append((table, row[0]))
        if not todo:
            return
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                marks = {table: self._id_high_water(table) for table, _ in todo}
                for table, _ in todo:
                    ddl = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", META_SCHEMA, re.S)
                    old_cols = [(r[1], r[2]) for r in self.db.execute(f"PRAGMA main.table_info({q(table)})")]
                    keep = f"_keep_{table}"
                    self.db.execute(f"CREATE TEMP TABLE {q(keep)} AS SELECT * FROM main.{q(table)}")
                    self.db.execute(f"DROP TABLE main.{q(table)}")
                    self.db.execute(ddl.group(0))
                    have = {r[1] for r in self.db.execute(f"PRAGMA main.table_info({q(table)})")}
                    for name, decl in old_cols:
                        if name not in have:
                            self.db.execute(f"ALTER TABLE main.{q(table)} ADD COLUMN {q(name)} {decl}")
                    cols = ", ".join(q(name) for name, _ in old_cols)
                    self.db.execute(f"INSERT INTO main.{q(table)} ({cols}) SELECT {cols} FROM temp.{q(keep)}")
                    self.db.execute(f"DROP TABLE temp.{q(keep)}")
                    seq = self.db.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                    if seq is None:
                        self.db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?,?)",
                                        (table, marks[table]))
                    elif seq[0] < marks[table]:
                        self.db.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?",
                                        (marks[table], table))
                if "tag_defs" in marks:
                    self.db.execute("DELETE FROM row_tags WHERE tag_id NOT IN (SELECT id FROM tag_defs)")
                    self.db.execute("UPDATE watchlist SET auto_tag_id=NULL "
                                    "WHERE auto_tag_id NOT IN (SELECT id FROM tag_defs)")
                if "sources" in marks:
                    for t in ("row_tags", "row_notes"):
                        self.db.execute(f"DELETE FROM {t} WHERE source_id NOT IN (SELECT id FROM sources)")
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()

    def _seed_tags(self) -> None:
        with self.lock, self.db:
            n = self.db.execute("SELECT count(*) FROM tag_defs").fetchone()[0]
            if not n:
                self.db.executemany(
                    "INSERT INTO tag_defs(name, color, hotkey) VALUES (?,?,?)",
                    self._default_tags,
                )

    def _drain_idle_readers(self, *, permanent: bool = False) -> None:
        """Close every reader sitting idle in the pool. `permanent` also
        latches the pool shut (close()); compact leaves it open, since the
        store keeps serving reads afterwards. Checked-out connections are
        never touched — they belong to a live read."""
        with self._reader_lock:
            if permanent:
                self._readers_closed = True
            idle, self._reader_pool = self._reader_pool, []
        for conn in idle:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def close(self, *, fold_wal: bool = False) -> bool:
        """Release the case file. Returns True, unless `fold_wal` asked for
        every committed write to be copied out of the -wal into the case
        file itself and that couldn't be done — see the end of this method.
        Only a caller about to move the file without its -wal (save_as)
        needs to ask. Every other close is the case's last connection when
        it can be, which is what has SQLite checkpoint the -wal and remove
        it and the -shm; when a long read is still open it folds the -wal
        in instead, and the files left hold nothing the case file lacks."""
        # SQL pane queries first: one can run for ever, and it has no job
        # record or thread of its own to stop cooperatively, so it is
        # interrupted outright — now, so it unwinds while the waits below
        # run. Waited for further down.
        self.stop_pane_queries()
        # Stop any running ingest job, and any view-as-table copy, before
        # the connection goes away — a worker mid-batch would otherwise die
        # on a closed database, and a copy would strand a half-filled
        # source nothing can open. Cancel is cooperative (checked per
        # BATCH), so the wait is bounded by one batch commit plus the
        # partial-source drop. A copy has no thread of its own to join (it
        # runs on the request's pool thread, which outlives it), so it is
        # waited on through its `done` event; `_closing` is raised in the
        # same locked step as the snapshot so a copy registering after it
        # aborts on its own (see save_view_as_source) — and so does a job
        # asked for after it (see start_ingest_job), which is how a derive
        # cascade running on one of the workers below cannot queue a
        # backfill behind this snapshot.
        with self._ingest_jobs_lock:
            self._closing = True
            jobs = list(self._ingest_jobs.values())
            for j in jobs:
                j["cancelled"] = True
            copies = list(self._subset_copies.values())
            for c in copies:
                c["cancelled"] = True
        for j in jobs:
            t = j.get("thread")
            if t and t is not threading.current_thread() and t.is_alive():
                t.join(15)
        # The open-time typing pass reads on a reader of its own and writes
        # one UPDATE per column; `_closing` interrupts its scan at the next
        # progress tick, and a column it hadn't reached stays NULL for the
        # next open (_type_unlooked_derived_columns).
        t = self._derived_typer
        if t and t is not threading.current_thread() and t.is_alive():
            t.join(15)
        # Same for view builds running in the background: a worker queued
        # on self.lock at this point would otherwise take it after the
        # close below and run its INSERT on a closed connection. `closing`
        # stops one that hasn't started; cancel_op interrupts one that has
        # (or fails it fast at registration, if it is still queued).
        self._view_jobs.closing = True
        view_jobs = self._view_jobs.live()
        for j in view_jobs:
            if j["status"] == "running":
                self.cancel_op(j["token"])
        for j in view_jobs:
            t = j.get("thread")
            if t and t is not threading.current_thread() and t.is_alive():
                t.join(15)
        # The watchlist scan is a writer too (hits, auto-tags). Its stop
        # is cooperative between (indicator, source) units and a unit's
        # write re-checks `closing` under the lock, so a worker the join
        # outwaits still never reaches the closed connection.
        self._scan_jobs.closing = True
        scan_jobs = self._scan_jobs.live()
        for j in scan_jobs:
            self._scan_jobs.request_discard(j)
        for j in scan_jobs:
            t = j.get("thread")
            if t and t is not threading.current_thread() and t.is_alive():
                t.join(15)
        for c in copies:
            c["done"].wait(15)
        # The pane queries interrupted at the top. Each holds the case file
        # open on a connection of its own, which is the trap the drain below
        # exists for: open at the writer's close, it stops the -wal being
        # checkpointed, and save_as then deletes the -wal. An interrupt
        # lands at the query's next VM step, so this is milliseconds.
        with self._pane_gone:
            self._pane_gone.wait_for(lambda: not self._pane_conns, timeout=15)
        # A pane run (run_sql_result) still writes to the results scratch
        # file for a moment after its connection goes — its last batch,
        # its summary. Wait for each to finish, then delete the file and
        # every result in it. No new run can start: _sql_run refuses once
        # `_closing` is set.
        with self._ingest_jobs_lock:
            sql_runs = list(self._sql_runs.values())
        for run in sql_runs:
            run["done"].wait(15)
        self.sql_results.close()
        # Immediately before the writer closes, and not before the cleanup
        # above: an idle reader still holding the case file open at that
        # moment stops SQLite checkpointing the -wal on the writer's close,
        # and save_as then renames the .db and deletes the -wal beside it —
        # the committed rows go with it. (Drained *here* rather than at the
        # top of close for the other half of the same problem: stopping the
        # jobs above means reading, and those reads are on the pool now.)
        self._drain_idle_readers(permanent=True)
        # A search-all sweep reads table by table on readers of its own; told
        # to stop, it ends after the count it is in (the latch above would
        # fail its next table anyway, as an error nobody is polling for).
        with self._search_job_lock:
            if self._search_job is not None:
                self._search_job["cancelled"] = True
        folded = True
        with self.lock:
            # The drain only reaches idle readers. One checked out by a
            # request or a worker still running — a page fetch, a CSV export
            # streaming, a search-all count, a search-index build reading its
            # table's row — keeps the case file open past the writer's close,
            # so SQLite skips the checkpoint it does on the last close: the
            # -wal and -shm stay beside the case, the -wal holding every
            # write since the last checkpoint.
            #
            # save_as can't have that at all, since it deletes the -wal —
            # doing so took every write since the last checkpoint with it
            # (with 64 KB pages that is ~64 MB of WAL: on a quick look,
            # usually the whole import and every tag). So it folds the -wal
            # into the case file here, under the lock, so nothing commits
            # after the check, and returns whether that worked. A reader
            # whose snapshot already has every write doesn't stop the frames
            # being copied back, only the WAL being reset, which is why
            # "folded" is enough and "truncated" is not needed.
            if fold_wal:
                folded = self._checkpoint_truncate(self.FOLD_TIMEOUT_S, folded_is_enough=True)
            else:
                # Any other close waits for those readers to come back, so
                # that it is the last close and SQLite tidies up after it. No
                # new one can be checked out now (the drain latched the pool),
                # and a page fetch or a count is over well inside the budget.
                # Under the lock, so a background writer (an index build)
                # can't meanwhile put frames into the -wal newer than an open
                # reader's snapshot, which would stop the fold below copying
                # them back.
                with self._readers_back:
                    self._readers_back.wait_for(lambda: not self._readers_out,
                                                timeout=self.FOLD_TIMEOUT_S)
                    readers_open = self._readers_out
                # A long download isn't back in time, and isn't cut off. Then
                # this close won't be the last, so fold what it can now: the
                # -wal left behind holds nothing the case file lacks, and goes
                # the next time the case is opened or listed on the home
                # screen (fold_leftover_wal). One attempt: frames newer than
                # an open reader's snapshot can't be copied until it ends,
                # and waiting out a download is what this just declined.
                if readers_open:
                    with contextlib.suppress(sqlite3.Error):
                        self._checkpoint_truncate(0, folded_is_enough=True)
            self.db.close()
        # Release the flock (see __init__) before unlinking, so that if the
        # remove below is the one that fails, what's left behind is an
        # unlocked file the next process's sweep_orphan_views can collect.
        if self._views_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._views_fd)
            self._views_fd = None
        # A reader still checked out by an in-flight request holds the
        # unlinked file open — fine on POSIX (the pages stay readable until
        # its close), and on Windows the remove just fails and is skipped,
        # leaving a stray temp file rather than an error. Either way it's no
        # longer permanent: the sweep at the next server start takes it.
        for suffix in VIEWS_DB_SUFFIXES:
            try:
                os.remove(self._views_path + suffix)
            except OSError:
                pass
        self._case_lock.release()
        self.closed = True
        return folded

    READER_POOL_CAP = 6  # matches app.js's PAGE_FETCH_CONCURRENCY — the burstiest client

    def _open_reader(self) -> sqlite3.Connection:
        """One read-only connection for _reader()'s pool. Never takes
        self.lock: WAL mode (_tune, and the `PRAGMA v.journal_mode=WAL` set
        on the writer at attach time) is what makes concurrent, unlocked
        reads against both the case file and the views database safe — a
        reader sees the latest *committed* data and is never blocked by, or
        blocks, a writer holding self.lock for a long
        build_view/ingest/search sweep. Same registered functions as the
        writer (a group_virtual WHERE can contain DAY_BUCKET(...); a raw
        filter fragment echoed into an export can contain REGEXP), so a
        query compiled against one connection never fails on the other."""
        ro = _no_dq_strings(sqlite3.connect(sqlite_ro_uri(self.path), uri=True,
                                            check_same_thread=False))
        ro.row_factory = sqlite3.Row
        ro.create_function("REGEXP", 2, _regexp, deterministic=True)
        ro.create_function("DAY_BUCKET", 1, _day_bucket, deterministic=True)
        ro.create_function("TS_NORMALIZE", 1, _ts_normalize, deterministic=True)
        # The Timeline's summary functions, for timeline_preview: the build
        # itself writes on self.db, but the preview of a few rows is a pure
        # read and must not queue behind a long build for the writer lock.
        ro.create_function("TL_LEAD", -1, _tl_lead, deterministic=True)
        ro.create_function("TL_DETAILS", -1, _tl_details, deterministic=True)
        # Same mmap reasoning as _tune (address space, not RAM — and shared
        # with the writer's mapping via the OS page cache). busy_timeout
        # covers the rare WAL edge (recovery, checkpoint restart) where
        # even a reader can see SQLITE_BUSY briefly.
        ro.execute("PRAGMA mmap_size=1073741824")
        ro.execute("PRAGMA busy_timeout=5000")
        ro.execute("ATTACH DATABASE ? AS v", (sqlite_ro_uri(self._views_path),))
        return ro

    @contextlib.contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """Checkout/return for the read-only pool. Every pure-read path
        (fetch_rows, tag_positions, group_summary, exports, search counts,
        ...) runs its queries on one of these instead of self.db, which is
        what makes a slow build_view or ingest invisible to paging.

        Two rules for using it:

        - Never inside an open writer transaction. A reader sees committed
          data only; code that has just INSERTed under `with self.db:` and
          needs to read it back must stay on self.db (e.g. ingest's
          get_source call). Every current _reader() caller is a top-level
          read-only Store method — keep it that way.
        - The connection is checked out by one thread at a time (that's
          what the pool provides; a shared sqlite3 connection would
          serialize its users at the C level and quietly reintroduce the
          contention this removes). Holding one across a generator's yields
          is fine — exports do — because the pool just opens another for
          whoever else asks.

        On any exception the connection is closed, not returned: the only
        realistic errors here are a view/source table dropped mid-read or
        the store closing, and a connection whose last statement died is
        cheaper to replace than to prove clean. The pool refills lazily.

        Checked-out connections are counted, so that close() can wait for
        them to come back: one still open when the writer closes keeps
        SQLite from tidying the -wal and -shm away (see close())."""
        with self._reader_lock:
            # A store whose case has been closed under a request in flight
            # (a case switch, with two browsers on one server) must fail
            # the way the writer does: server.py maps ProgrammingError
            # "closed database" to a clean 409. Opening a fresh reader
            # would instead fail on the deleted views file with an
            # OperationalError — a 500 and a traceback for a situation
            # that is neither. Checked inside the lock, so close() cannot
            # land between the check and the pop.
            if self._readers_closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            conn = self._reader_pool.pop() if self._reader_pool else None
            self._readers_out += 1
        pooled = False
        try:
            if conn is None:
                conn = self._open_reader()
            yield conn
            with self._reader_lock:
                if not self._readers_closed and len(self._reader_pool) < self.READER_POOL_CAP:
                    self._reader_pool.append(conn)
                    pooled = True
                    self._readers_out -= 1
                    self._readers_back.notify_all()
        finally:
            if not pooled:
                if conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        conn.close()
                # Counted back only once it is closed, so close() never sees
                # "none out" while one still has the case file open.
                with self._reader_lock:
                    self._readers_out -= 1
                    self._readers_back.notify_all()

    @staticmethod
    @contextlib.contextmanager
    def _dropped_view_is_expired() -> Iterator[None]:
        """Maps "no such table" during an unlocked view read to the same
        ViewExpired a missing handle raises, so server.py's existing
        KeyError → 409 mapping turns it into the "view expired" the
        frontend already rebuilds on. This race is new with _reader():
        under the old single-connection design the lock serialized a page
        fetch against the eviction that drops its view table; now a reader
        holding a handle can lose the table to _evict_root_views
        mid-request. (A *source* table dropped mid-read lands here too —
        same answer: the world changed under the request, rebuild.)"""
        try:
            yield
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                raise ViewExpired("View expired — rebuild it") from None
            raise

    # ------------------------------------------------------------------ ingest

    # ------------------------------------------------------- cancellable ops

    def cancel_op(self, token: str) -> bool:
        """Cancels the in-flight operation registered under `token` (a
        client-generated one-shot id riding on the request that started
        it — see _interruptible). Returns whether there was anything to
        interrupt. Marking the token cancelled *first* closes the start
        race: a cancel that lands before the operation registers still
        takes effect the moment it tries to."""
        with self._op_lock:
            self._op_cancelled.add(token)
            while len(self._op_cancelled) > 512:  # one-shot ids; keep it bounded
                self._op_cancelled.pop()
            conn = self._op_conns.get(token)
            if conn is not None:
                conn.interrupt()
                return True
        return False

    @contextlib.contextmanager
    def _interruptible(self, token: str | None, conn: sqlite3.Connection):
        """Registers `conn` as interruptible under `token` for the block,
        turning cancel_op(token) into OpCancelled out of this block.

        The discipline that makes interrupt() safe to expose at all: a
        writer caller enters this block while *holding* self.lock and
        unregisters before releasing it, so the only statements that can be
        running on the writer connection while its token is registered are
        the block's own. interrupt() aborts running statements only; a
        cancel arriving in the gap between two statements is a no-op (the
        flag clears when the statement count hits zero), which surfaces as
        "the cancel didn't land — click again", never as a mis-aimed kill
        of someone else's write. Reader-pool callers pass their checked-out
        reader (and stack this innermost): an interrupted reader raises out
        of _reader(), which already closes — not repools — a connection
        that died mid-statement."""
        if not token:
            yield
            return
        with self._op_lock:
            if token in self._op_cancelled:
                raise OpCancelled("Cancelled")
            self._op_conns[token] = conn
        try:
            yield
        except sqlite3.OperationalError as e:
            with self._op_lock:
                was_cancelled = token in self._op_cancelled
                was_preempted = token in self._op_preempted
                self._op_preempted.discard(token)
            if "interrupt" in str(e).lower():
                # A genuine cancel outranks a preemption that happened to
                # land in the same window: the analyst asked for one and
                # not the other, and OpPreempted would restart the build
                # they just stopped.
                if was_cancelled:
                    raise OpCancelled("Cancelled") from e
                if was_preempted:
                    raise OpPreempted("Yielded to another table") from e
            raise
        finally:
            with self._op_lock:
                self._op_conns.pop(token, None)
                self._op_preempted.discard(token)

    # A view build yields the writer lock to a build for a DIFFERENT table
    # at most this many times. Past it, the search finishes: the point is
    # that switching tables works while a long search runs, not that a
    # search can be starved forever by someone flicking between tabs.
    VIEW_BUILD_YIELD_MAX = 3

    def _yield_the_writer(self, source_id: int, token: str | None) -> None:
        """Interrupt the build holding the writer lock, if it is for
        another table, so this one can have it.

        build_view holds self.lock for the whole INSERT..SELECT, which is
        the right shape for a 1.2M-row materialise and the wrong one for
        an analyst who started a four-minute search and then clicked
        another tab: that table's own build queued behind the search and
        the grid sat on "Filtering…" for the rest of it. Reads stopped
        being the problem when they moved to the pool (invariant #4);
        this is the other half.

        The interrupted build rolls back — a held build evicts nothing, so
        there was never anything on screen depending on it — and its
        worker starts it again once the lock is free (_view_job_worker).
        Nothing is lost but the time already spent scanning, which is the
        trade: the table in front of the analyst wins.

        Only across tables. Two builds for the same table are the ordinary
        supersede, which cancel-on-rebuild already handles and which must
        stay a cancel: the newer spec replaces the older one rather than
        queueing behind it."""
        with self._op_lock:
            cur = self._build_running
            if cur is None or cur["source_id"] == source_id or cur["token"] == token:
                return
            if cur["token"] in self._op_preempted:
                return   # already asked, and unwinding — asking again is not another yield
            if self._build_yields.get(cur["token"], 0) >= self.VIEW_BUILD_YIELD_MAX:
                return
            self._build_yields[cur["token"]] = self._build_yields.get(cur["token"], 0) + 1
            self._op_preempted.add(cur["token"])
            conn = self._op_conns.get(cur["token"])
            if conn is not None:
                conn.interrupt()

    # How often a build waiting for the writer lock re-asks the holder to
    # stand aside. Only paid while actually waiting, and only by a build
    # for the table in front of the analyst. Short, because it is also the
    # granularity of the wait itself: the loop cannot notice the lock has
    # freed until its next attempt, and a build that answers inline has
    # VIEW_JOB_INLINE_WAIT_MS (250) to do everything in. An attempt is a
    # lock try and a dict lookup, so 100 a second while waiting is not a
    # cost worth trading latency for.
    YIELD_ASK_EVERY_S = 0.01
    # How long a restarted build will stand back for a waiting one before
    # going anyway. Generous: the thing it is waiting for is a build that
    # has already interrupted it and is about to take the lock.
    YIELD_STANDBACK_MAX_S = 10.0

    @contextlib.contextmanager
    def _writer_for_build(self, source_id: int, token: str | None, yield_others: bool):
        """The writer lock for a view build, asking whoever holds it to
        stand aside when it is building another table.

        Asked in a LOOP rather than once before blocking. The build to
        stand aside for may have taken the lock a moment ago and not yet
        registered — start a long search and switch tabs immediately and
        that is exactly the ordering — and a single ask would miss it and
        then wait out the whole search, which is the thing this exists to
        stop. Asking again is free: _yield_the_writer ignores a holder it
        has already asked, so the poll costs a lock and a dict lookup and
        cannot spend the yield budget."""
        if not yield_others:
            # A restart waits for the build that took the lock off it to
            # have the lock — otherwise the two race for it the moment it
            # is free, the restart can win, and the table in front of the
            # analyst is asked to stand aside a second time for a search
            # nobody is watching. Bounded: a waiter that never arrives (it
            # failed, or its request was dropped) must not park this
            # thread for the life of the process.
            deadline = time.time() + self.YIELD_STANDBACK_MAX_S
            while self._build_waiting and time.time() < deadline:
                time.sleep(self.YIELD_ASK_EVERY_S)
            with self.lock:
                yield
            return
        with self._op_lock:
            self._build_waiting += 1
        try:
            while True:
                self._yield_the_writer(source_id, token)
                if self.lock.acquire(timeout=self.YIELD_ASK_EVERY_S):
                    break
        finally:
            with self._op_lock:
                self._build_waiting -= 1
        try:
            yield
        finally:
            self.lock.release()

    @contextlib.contextmanager
    def _running_build(self, source_id: int, token: str | None):
        """Registers this build as the one holding the writer lock, for
        _yield_the_writer to find. Entered inside the lock and left before
        it is released, the same discipline _interruptible documents."""
        rec = {"source_id": source_id, "token": token}
        with self._op_lock:
            self._build_running = rec
        try:
            yield
        finally:
            with self._op_lock:
                if self._build_running is rec:
                    self._build_running = None

    @contextlib.contextmanager
    def _ingest_synchronous_off(self):
        """synchronous=OFF for the duration of one import's batch-commit
        loop, restored to NORMAL (the steady-state setting from _tune)
        whether the import finished, raised, or was interrupted. WAL stays
        on — only the fsync-on-commit behavior is relaxed.

        This is the shared connection (invariant #4), so the relaxed
        setting is in force for any other write that happens to land in the
        same window, not just this import's own — the risk that bounds is
        the same one ingest already accepts: a crash mid-import costs the
        in-flight import (and, in this narrow window, an unlucky concurrent
        write), and every batch before the crash is already durably
        committed to the WAL up to the last synchronous=NORMAL checkpoint.
        Never combine with journal_mode=OFF (that would risk case-file
        corruption, not just losing the in-flight write) — this case file
        is the durable artifact and holds tags/notes/sessions that aren't
        re-derivable from the source, unlike a throwaway temp database
        where that risk would be easy to accept."""
        with self.lock, self.db:
            self.db.execute("PRAGMA synchronous=OFF")
        try:
            yield
        finally:
            with self.lock, self.db:
                self.db.execute("PRAGMA synchronous=NORMAL")

    def ingest_csv(
        self,
        path: str,
        name: str | None = None,
        delimiter: str | None = None,
        build_fts: bool = True,
        has_header: bool = True,
        column_types: list[str] | None = None,
        progress=None,
        cancel=None,
    ) -> dict:
        """Stream a delimited file into its own table. Returns the source record.

        Commits in BATCH-sized chunks instead of one giant transaction, and
        only holds self.lock for the duration of each chunk's insert — not
        for the whole file. self.lock guards every Store method (it's the
        one thing serializing all access to the shared connection), so on a
        huge import (minutes, at the scale this tool targets) the old
        one-transaction-for-the-whole-file approach froze every other
        request — fetching rows, tagging, opening another already-loaded
        source — for the entire duration. It also meant one bad row near the
        end (a malformed line, a full disk) rolled back everything already
        inserted, discarding all of it.

        sources.row_count is updated after every chunk, so if something does
        go wrong partway through, whatever was already committed stays
        (and stays visible/queryable with an accurate count) instead of the
        whole import vanishing — the caller still sees the error and knows
        the source is incomplete, but doesn't lose everything that
        succeeded before it.
        """
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        file_hash = self._quick_hash(path)

        # 1 MB read buffer: the default 8 KB means a 20 GB file is ~2.5M
        # read() syscalls before the csv layer even sees a byte.
        encoding = sniff_text_encoding(path)
        fh = open(path, "r", encoding=encoding, errors=TEXT_DECODE_ERRORS,
                  newline="", buffering=1 << 20)
        head = fh.read(64 * 1024)
        fh.seek(0)
        fallback0 = _cp1252_fallback_count()   # after the seek: the head is read twice
        if delimiter is None:
            # `path`, not `name`: the display name is the caller's to
            # override and may carry no extension at all, and the
            # extension is the whole point here.
            delimiter = self._sniff(head, path)
        reader, rows = _csv_rows(fh, delimiter)

        # Everything up to the row loop's own try/finally closes the file
        # on the way out too — a failure here used to leak the handle.
        try:
            first_row = _first_nonblank_row(rows)
            if has_header:
                cols = sanitize_columns(first_row)
                ncols = len(cols)
                leftover_row = None
            else:
                ncols = len(first_row)
                cols = sanitize_columns([None] * ncols)
                leftover_row = first_row  # not a header — it's the first data row

            with self.lock, self.db:
                cur = self.db.execute(
                    "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name, os.path.abspath(path), "", "[]", file_hash,
                     time.strftime("%Y-%m-%dT%H:%M:%S"),
                     # The delimiter actually used, not the None that asked for
                     # a sniff: a re-import must split the same way.
                     _ingest_record("csv", delimiter=delimiter, has_header=has_header,
                                    column_types=column_types)),
                )
                source_id = cur.lastrowid
                self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
                table = f"src_{source_id}"
                coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
                self.db.execute(f"CREATE TABLE {q(table)} (rid INTEGER PRIMARY KEY, {coldefs})")
                self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (table, source_id))
        except BaseException:
            fh.close()
            raise

        placeholders = ",".join("?" * ncols)
        insert = f"INSERT INTO {q(table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

        sample: list[list[str]] = []
        batch: list[tuple] = []
        total = 0
        ragged = 0
        t0 = time.time()
        suspect_quotes = 0
        error: Exception | None = None

        # This per-row append loop is deliberate: a chunked
        # list(islice(reader, BATCH)) rewrite was measured *slower* (~7% on
        # a 200k-row file) — islice's per-item indirection costs more than
        # the tuple() copy it saves, and executemany binds tuples slightly
        # faster than lists. Don't "optimise" it back.
        rows_iter = ([leftover_row] if leftover_row is not None else [])
        with self._ingest_synchronous_off():
            try:
                prev_line = 0
                for row in itertools.chain(rows_iter, rows):
                    if len(row) != ncols:
                        # Ragged row: pad or trim rather than dropping evidence.
                        ragged += 1
                        row = (row + [""] * ncols)[:ncols]
                    # An unbalanced quote makes csv silently swallow every
                    # following line into one field until the next quote (or
                    # a hard error at CSV_FIELD_LIMIT) — dozens of
                    # evidence lines vanish from the grid with the ragged
                    # counter reading zero. The reader's line_num counts
                    # PHYSICAL lines consumed, so a row that swallowed its
                    # neighbours shows as a jump — one int compare per row
                    # (a per-cell newline scan here benchmarked ingest 11%
                    # slower). Legitimate quoted multi-line payloads (EVTX
                    # XML) sit under the threshold; counted, not judged.
                    line = reader.line_num
                    if line - prev_line >= 10:
                        suspect_quotes += 1
                    prev_line = line
                    batch.append(tuple(row))
                    if len(sample) < SAMPLE_ROWS:
                        sample.append(row)
                    if len(batch) >= BATCH:
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                        if progress:
                            # NOT fh.tell(): a text file being iterated (the
                            # csv reader drives the file iterator) raises
                            # "telling position disabled by next() call" on
                            # tell(). The underlying BufferedReader's byte
                            # position is legal to read and ahead of the
                            # decoded position by at most the 1 MB buffer.
                            progress(total, fh.buffer.tell(), size)
                if batch:
                    if cancel is not None and cancel():
                        raise IngestCancelled(f"Import of {name} cancelled")
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                    batch.clear()
                if progress:
                    progress(total, size, size)  # exact final tick — the loop's ticks stop at the last full batch
            except IngestCancelled:
                # An explicit cancel discards the partial import — unlike a
                # mid-file *error*, which keeps what already committed (see
                # the docstring). The analyst asked for this source not to
                # exist, and a keep-what-committed half-table would look
                # exactly like a complete import in every table list.
                self.drop_source(source_id)
                raise
            except Exception as e:
                # The rows in the pending batch parsed fine — the FAILURE is
                # the line after them. Losing them made "keep what
                # committed" quietly mean "lose up to a batch (20k rows) of
                # good data before the bad line". Best-effort: if the
                # failure was the commit itself (disk full), this second
                # attempt fails too and what committed still stands.
                if batch:
                    with contextlib.suppress(Exception):
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                line = getattr(reader, "line_num", 0)
                where = f"Line {line:,}: " if line else ""
                kept = (f" — import stopped there; the {total:,} rows before it were kept"
                        if total else "")
                error = ValueError(f"{where}{e}{kept}")
                error.__cause__ = e
            finally:
                fh.close()

        # Best-effort even on failure: whatever's in `sample` (up to
        # SAMPLE_ROWS, collected during parsing regardless of whether the
        # batch it was in ever committed) is enough to type the columns, so a
        # partial import is still typed/browsable rather than left with the
        # placeholder columns=[] forever.
        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        if column_types:
            types = [(column_types[i] if i < len(column_types) and column_types[i] in ("text", "number", "datetime") else t)
                     for i, t in enumerate(types)]
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        decoded = _encoding_report(encoding, _cp1252_fallback_count() - fallback0)
        self._finish_source_columns(source_id, colmeta, decoded, name)

        if error is not None:
            if total == 0:
                # Nothing ever committed: a zero-row source in the table
                # list is pure noise that looks like a real (empty) import.
                self.drop_source(source_id)
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            # Backgrounded, not built inline: the trigram index takes real
            # time to build (see build_fts) and ingest_csv should return as
            # soon as rows are typed/committed so the analyst can start
            # browsing immediately — has_fts is 0 in the returned record
            # until the background build finishes.
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["ragged_rows"] = ragged
        rec["suspect_quote_rows"] = suspect_quotes
        rec["encoding"], rec["cp1252_bytes"] = decoded["encoding"], decoded["cp1252_bytes"]
        return rec

    def ingest_text(self, path: str, name: str | None = None, build_fts: bool = True,
                    progress=None, cancel=None) -> dict:
        r"""Import a file as raw text: one row per physical line, the whole
        line in the one TEXT_COLUMN, nothing split, nothing treated as a
        header. For log files — syslog, hostd.log, auth.log.1, an
        extensionless dump — which the CSV path mangled: it sniffed a
        delimiter that was always one of four, took line 1 as the header,
        split lines on stray commas and truncated the wider ones, and let
        an unbalanced quote swallow the lines after it.

        Deliberately not the csv module: iterating lines has no field-size
        limit and no quoting rules, and the row id IS the line number
        (blank lines are kept as empty rows for exactly that reason — the
        grid has a hide-empty-rows option for the analyst who'd rather not
        see them). Same batching, progress, cancel and keep-what-committed
        contract as ingest_csv; same encoding sniff.

        A line ends at \n and nowhere else (_text_line has the why)."""
        name = name or os.path.basename(path)
        if looks_binary(path):
            raise ValueError(f"{name} looks like a binary file (NUL bytes in its first 8 KB) — not importable as text")
        size = os.path.getsize(path)
        file_hash = self._quick_hash(path)
        # newline="\n", not the default: universal newlines also end a line
        # at a lone \r. See _text_line.
        encoding = sniff_text_encoding(path)
        fh = open(path, "r", encoding=encoding, errors=TEXT_DECODE_ERRORS,
                  newline="\n", buffering=1 << 20)
        try:
            first = fh.readline()
        except Exception:
            fh.close()
            raise
        if first == "":
            fh.close()
            raise ValueError("File is empty")
        fh.seek(0)
        fallback0 = _cp1252_fallback_count()

        cols = [TEXT_COLUMN]
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                " VALUES (?,?,?,?,?,?,?)",
                (name, os.path.abspath(path), "", "[]", file_hash,
                 time.strftime("%Y-%m-%dT%H:%M:%S"),
                 _ingest_record("text")),
            )
            source_id = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
            table = f"src_{source_id}"
            self.db.execute(f"CREATE TABLE {q(table)} (rid INTEGER PRIMARY KEY, {q(TEXT_COLUMN)} TEXT)")
            self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (table, source_id))
        insert = f"INSERT INTO {q(table)} ({q(TEXT_COLUMN)}) VALUES (?)"

        batch: list[tuple] = []
        total = 0
        t0 = time.time()
        error: Exception | None = None
        with self._ingest_synchronous_off():
            try:
                for line in fh:
                    batch.append((_text_line(line),))
                    if len(batch) >= BATCH:
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                        if progress:
                            progress(total, fh.buffer.tell(), size)
                if batch:
                    if cancel is not None and cancel():
                        raise IngestCancelled(f"Import of {name} cancelled")
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                    batch.clear()
                if progress:
                    progress(total, size, size)
            except IngestCancelled:
                self.drop_source(source_id)   # as ingest_csv: a cancel discards the partial import
                raise
            except Exception as e:
                if batch:
                    with contextlib.suppress(Exception):
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                kept = f" — import stopped there; the {total:,} lines before it were kept" if total else ""
                error = ValueError(f"Line {total + 1:,}: {e}{kept}")
                error.__cause__ = e
            finally:
                fh.close()

        colmeta = [{"name": TEXT_COLUMN, "type": "text"}]
        decoded = _encoding_report(encoding, _cp1252_fallback_count() - fallback0)
        self._finish_source_columns(source_id, colmeta, decoded, name)
        if error is not None:
            if total == 0:
                self.drop_source(source_id)
            raise error
        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)
        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["ragged_rows"] = 0
        rec["suspect_quote_rows"] = 0
        rec["encoding"], rec["cp1252_bytes"] = decoded["encoding"], decoded["cp1252_bytes"]
        return rec

    @staticmethod
    def preview_text_lines(text: str, max_rows: int = 50) -> dict:
        r"""The raw-text importer's preview: the first lines, one per row, in
        the shape preview_csv_text returns so the import preview renders
        them with the same table. Split exactly as ingest_text splits —
        str.splitlines() breaks on \r, \v, \f, \x85 and U+2028 too, so
        the preview showed rows the import would not make."""
        if not text:
            raise ValueError("File is empty")
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()   # the last line's terminator, not a blank line after it
        lines = [_text_line(ln) for ln in lines[:max_rows]]
        return {"delimiter": None, "columns": [TEXT_COLUMN],
                "sample_rows": [[ln] for ln in lines], "inferred_types": ["text"]}

    def ingest_iis(self, path: str, name: str | None = None, build_fts: bool = True,
                   progress=None, cancel=None, log_format: str | None = None,
                   column_types: dict[str, str] | None = None) -> dict:
        """Import an IIS web server log (W3C extended, NCSA or IIS native)
        as one table — iisread.py has the formats and why W3C's columns
        come from its #Fields: lines. The file's name says which format it
        is; `log_format` overrides that (the import preview's pick, or a
        session re-import, which recorded it).

        `column_types` maps a column's name to the type the preview set
        for it. By name, not position, as ingest_csv's list is: the
        preview reads the start of the file, and a #Fields: line further
        in can add columns ahead of ones it saw (Timestamp, Unnamed
        values). A column it does not name keeps the inferred type.

        Two passes, as ingest_json: the first finds the columns (the union
        of every #Fields: line), the second inserts. The first runs before
        the source exists, so a cancel there leaves nothing to drop; the
        second reads exactly as many lines as the first did, so a log the
        server is still appending to can't hand it a field it never
        planned for. Otherwise ingest_text's contract: batched commits,
        byte progress, cancel drops the partial source, a mid-file error
        keeps what committed. No binary refusal — a log IIS was writing
        when the power went holds NUL runs, which are read as gaps."""
        name = name or os.path.basename(path)
        fmt = log_format or iisread.log_format(path)
        if log_format and fmt not in iisread.FORMATS:
            raise ValueError(f"Unknown IIS log format {log_format!r}")
        if fmt not in iisread.FORMATS:
            raise ValueError(f"{name} is not named like an IIS log (u_ex…, nc…, in….log)")
        if column_types is not None and (
                not isinstance(column_types, dict)
                or any(t not in ("text", "number", "datetime") for t in column_types.values())):
            raise ValueError("An IIS log's column types name their columns: {column: text|number|datetime}")
        size = os.path.getsize(path)
        file_hash = self._quick_hash(path)
        encoding = sniff_text_encoding(path)

        def open_log():
            # newline="\n": a lone \r is the line's own text (_text_line).
            return open(path, "r", encoding=encoding, errors=TEXT_DECODE_ERRORS,
                        newline="\n", buffering=1 << 20)

        counted = [0]

        def first_pass(lines):
            for line in lines:
                counted[0] += 1
                if counted[0] % BATCH == 0 and cancel is not None and cancel():
                    raise IngestCancelled(f"Import of {name} cancelled")
                yield line

        with open_log() as fh:
            if fh.read(1) == "":
                raise ValueError("File is empty")
            fh.seek(0)
            plan = iisread.plan(fmt, first_pass(iisread.logical_lines(fh, {})))
        if not plan["columns"]:
            raise ValueError(f"{name} has no {iisread.FORMATS[fmt]} log lines in it")

        cols = sanitize_columns(plan["columns"])
        ncols = len(cols)
        source_id, table = self._create_source_shell(
            name, cols, path=path, file_hash=file_hash,
            ingest=_ingest_record("iis", format=fmt, column_types=column_types or None))
        insert = f"INSERT INTO {q(table)} ({','.join(q(c) for c in cols)}) VALUES ({','.join('?' * ncols)})"

        stats: dict = {}
        sample: list[list[str]] = []
        batch: list[tuple] = []
        total = 0
        t0 = time.time()
        error: Exception | None = None
        fallback0 = _cp1252_fallback_count()   # pass two only, or the file counts twice
        fh = open_log()
        with self._ingest_synchronous_off():
            try:
                lines = itertools.islice(iisread.logical_lines(fh, stats), counted[0])
                for row in iisread.rows(lines, plan, stats):
                    batch.append(tuple(row))
                    if len(sample) < SAMPLE_ROWS:
                        sample.append(row)
                    if len(batch) >= BATCH:
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                        if progress:
                            progress(total, fh.buffer.tell(), size)
                if batch:
                    if cancel is not None and cancel():
                        raise IngestCancelled(f"Import of {name} cancelled")
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                    batch.clear()
                if progress:
                    progress(total, size, size)
            except IngestCancelled:
                self.drop_source(source_id)   # as ingest_csv: a cancel discards the partial import
                raise
            except Exception as e:
                if batch:
                    with contextlib.suppress(Exception):
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                kept = f" — import stopped there; the {total:,} rows before it were kept" if total else ""
                error = ValueError(f"Row {total + 1:,}: {e}{kept}")
                error.__cause__ = e
            finally:
                fh.close()

        types = [t or (infer_type([r[i] for r in sample]) if sample else "text")
                 for i, t in enumerate(plan["types"])]
        if column_types:
            types = [column_types.get(c, t) for c, t in zip(cols, types)]
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        decoded = _encoding_report(encoding, _cp1252_fallback_count() - fallback0)
        self._finish_source_columns(source_id, colmeta, decoded, name)
        if stats.get("nul_lines"):
            wlog.record("warn", f"{name}: {stats['nul_lines']:,} line(s) held runs of NUL bytes — the gap a "
                                "server leaves in its log when it stops without closing it (a crash or "
                                "power loss). The text either side of each run was imported.")
        if error is not None:
            if total == 0:
                self.drop_source(source_id)
            raise error
        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)
        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["ragged_rows"] = stats.get("ragged", 0)
        rec["suspect_quote_rows"] = 0
        rec["log_format"] = fmt
        rec["encoding"], rec["cp1252_bytes"] = decoded["encoding"], decoded["cp1252_bytes"]
        return rec

    @staticmethod
    def preview_iis_text(text: str, log_format: str, complete: bool = True, max_rows: int = 50) -> dict:
        """The first rows of an IIS log as ingest_iis would make them, in
        preview_csv_text's shape. The columns are the ones the sample
        names — a #Fields: line further into the file adds its own on
        import. `complete` is whether `text` is the whole file; when it
        isn't, its last line may be cut and is left out, and the answer
        says so, since then more columns may follow."""
        if not text:
            raise ValueError("File is empty")
        physical = text.split("\n")
        if not complete and len(physical) > 1:
            physical.pop()
        lines = list(iisread.logical_lines(physical, {}))
        plan = iisread.plan(log_format, lines)
        sample = list(itertools.islice(iisread.rows(lines, plan, {}), max_rows))
        types = [t or (infer_type([r[i] for r in sample]) if sample else "text")
                 for i, t in enumerate(plan["types"])]
        return {"delimiter": None, "columns": sanitize_columns(plan["columns"]),
                "sample_rows": sample, "inferred_types": types, "log_format": log_format,
                "complete": complete}

    def _finish_source_columns(self, source_id: int, colmeta: list[dict], decoded: dict,
                               name: str) -> None:
        """The last write of a text import: the typed columns, and — when
        the bytes were not plain UTF-8 — how they were read
        (_encoding_report), on the source where it outlives the toast.
        The log gets the same line: it is the record of what happened."""
        with self.lock, self.db:
            if decoded["meta"] is None:
                self.db.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(colmeta), source_id))
            else:
                self.db.execute("UPDATE sources SET columns=?, origin_meta=? WHERE id=?",
                                (json.dumps(colmeta), json.dumps(decoded["meta"]), source_id))
        if decoded["encoding"] == "cp1252":
            wlog.record("warn", f"{name} is not UTF-8 — read as Windows-1252")
        elif decoded["cp1252_bytes"]:
            wlog.record("warn", f"{name}: {decoded['cp1252_bytes']:,} byte(s) were not valid UTF-8 "
                                "and were read as Windows-1252")

    def _commit_ingest_batch(self, insert_sql: str, batch: list[tuple], source_id: int, total: int,
                             also: tuple[str, list[tuple]] | None = None,
                             op_token: str | None = None) -> int:
        """Insert+commit one ingest batch in its own short transaction,
        updating sources.row_count as we go. Only holds self.lock for this
        one batch — see ingest_csv. `also` is a second (sql, rows)
        executemany that lands in the SAME transaction — save_view_as_source's
        subset_rids map, which must commit with exactly the rows it
        describes. `op_token` makes the batch interruptible (cancel_op):
        registered while the lock is held, as the writer discipline in
        _interruptible requires."""
        with self.lock, self._interruptible(op_token, self.db), self.db:
            self.db.executemany(insert_sql, batch)
            if also and also[1]:
                self.db.executemany(also[0], also[1])
            total += len(batch)
            self.db.execute("UPDATE sources SET row_count=? WHERE id=?", (total, source_id))
        return total

    def preview_csv_text(
        self, text: str, delimiter: str | None = None, has_header: bool = True,
        max_rows: int = 50, name: str | None = None
    ) -> dict:
        """Read-only sample of a delimited file's first rows, for the import
        preview UI. Never touches the database — no source row, no table.

        `name` is the file's name, and it has to be passed: the preview and
        the ingest must reach the same delimiter or the analyst agrees to
        one table and gets another."""
        if delimiter is None:
            delimiter = self._sniff(text[:8192], name)
        _, rows = _csv_rows(io.StringIO(text), delimiter)
        first_row = _first_nonblank_row(rows)
        if has_header:
            cols = sanitize_columns(first_row)
            ncols = len(cols)
            leftover_row = None
        else:
            ncols = len(first_row)
            cols = sanitize_columns([None] * ncols)
            leftover_row = first_row

        sample: list[list[str]] = []
        for row in itertools.chain([leftover_row] if leftover_row is not None else [], rows):
            if len(sample) >= max_rows:
                break
            if len(row) != ncols:
                row = (row + [""] * ncols)[:ncols]
            sample.append(row)

        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        return {"delimiter": delimiter, "columns": cols, "sample_rows": sample, "inferred_types": types}

    def preview_sqlite_tables(self, path: str) -> dict:
        """Read-only look at every table in an uploaded/pointed-to SQLite
        file — Chromium's History/Cookies/Web Data/... or any other
        .db/.sqlite — for the import UI's table picker: names, row counts,
        columns, and which columns look like a WebKit/Chrome timestamp
        (see _webkit_to_iso) so that option can default to checked without
        the analyst needing to already know which columns are epoch-encoded.
        Never writes to the case db — a completely separate connection to
        a completely separate file — nor beside that file
        (_open_evidence_sqlite)."""
        ro, cleanup = _open_evidence_sqlite(path)
        ro.row_factory = sqlite3.Row
        ro.text_factory = _lenient_text_factory()   # the timestamp sniff reads values too
        try:
            table_names = [
                r["name"] for r in ro.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables = []
            for t in table_names:
                try:
                    cols = _sqlite_table_columns(ro, t)
                    row_count = ro.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchone()[0]
                    likely_ts = []
                    for c in cols:
                        if not re.search(r"time|utc|date", c["name"], re.IGNORECASE):
                            continue
                        sample = [
                            r[0] for r in ro.execute(
                                f"SELECT {q(c['name'])} FROM {q(t)} WHERE {q(c['name'])} IS NOT NULL LIMIT 20"
                            )
                        ]
                        if sample and sum(1 for v in sample if _webkit_to_iso(v)) >= max(1, int(len(sample) * 0.8)):
                            likely_ts.append(c["name"])
                except sqlite3.Error as e:
                    # One table this SQLite build can't read — a virtual
                    # table whose module it lacks (Thunderbird's message
                    # index uses its own FTS tokenizer) — used to fail the
                    # preview of the whole FILE, so not one of its tables
                    # could be imported. It is listed, marked unreadable
                    # (`error`, row_count None), and the rest are offered.
                    tables.append({"name": t, "row_count": None, "columns": [],
                                   "likely_timestamp_columns": [], "error": str(e)})
                    continue
                tables.append({
                    "name": t,
                    "row_count": row_count,
                    "columns": [{"name": c["name"], "type": c["type"] or "TEXT"} for c in cols],
                    "likely_timestamp_columns": likely_ts,
                })
            return {"tables": tables}
        finally:
            ro.close()
            cleanup()

    def ingest_sqlite_table(
        self,
        path: str,
        table_name: str,
        name: str | None = None,
        build_fts: bool = True,
        timestamp_columns: list[str] | None = None,
        progress=None,
        cancel=None,
    ) -> dict:
        """Imports one table from an external SQLite file as a new source —
        same TEXT-column, batched-commit convention as ingest_csv (see its
        docstring: self.lock held per BATCH-sized chunk, not for the whole
        import, so a large table doesn't freeze every other request for
        the whole read). `timestamp_columns` — a subset of the *source*
        table's own column names, typically pre-populated from
        preview_sqlite_tables' heuristic and confirmed/edited by the
        analyst — get converted from WebKit/Chrome epoch microseconds to
        an ISO datetime string as they're read, rather than importing the
        opaque integer. That's an explicit, analyst-confirmed conversion,
        not an automatic rewrite of the evidence: the source .db file
        itself is opened read-only and never touched."""
        name = name or f"{os.path.splitext(os.path.basename(path))[0]}.{table_name}"
        timestamp_columns = set(timestamp_columns or [])
        file_hash = self._quick_hash(path)

        ro, cleanup = _open_evidence_sqlite(path)
        ro.row_factory = sqlite3.Row
        undecodable = [0]
        ro.text_factory = _lenient_text_factory(undecodable)
        try:
            src_cols_info = _sqlite_table_columns(ro, table_name)
            if not src_cols_info:
                raise ValueError(f"No such table: {table_name}")
            src_colnames = [c["name"] for c in src_cols_info]
            cols = sanitize_columns(src_colnames)
            ncols = len(cols)
            ts_idx = {i for i, sc in enumerate(src_colnames) if sc in timestamp_columns}
            total_rows = ro.execute(f"SELECT COUNT(*) FROM {q(table_name)}").fetchone()[0]

            with self.lock, self.db:
                cur = self.db.execute(
                    "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name, os.path.abspath(path), "", "[]", file_hash, time.strftime("%Y-%m-%dT%H:%M:%S"),
                     _ingest_record("sqlite", table=table_name, timestamp_columns=sorted(timestamp_columns))),
                )
                source_id = cur.lastrowid
                self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
                dest_table = f"src_{source_id}"
                coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
                self.db.execute(f"CREATE TABLE {q(dest_table)} (rid INTEGER PRIMARY KEY, {coldefs})")
                self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (dest_table, source_id))

            placeholders = ",".join("?" * ncols)
            insert = f"INSERT INTO {q(dest_table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

            def to_text(i, v):
                if v is None:
                    return ""
                if i in ts_idx:
                    iso = _webkit_to_iso(v)
                    if iso:
                        return iso
                if isinstance(v, (bytes, bytearray)):
                    return f"<{len(v)} bytes>"
                return str(v)

            select_cols = ",".join(q(c) for c in src_colnames)
            src_cursor = ro.execute(f"SELECT {select_cols} FROM {q(table_name)}")

            sample: list[list[str]] = []
            total = 0
            t0 = time.time()
            error: Exception | None = None
            with self._ingest_synchronous_off():
                try:
                    while True:
                        rows = src_cursor.fetchmany(BATCH)
                        if not rows:
                            break
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        batch = [tuple(to_text(i, v) for i, v in enumerate(r)) for r in rows]
                        if len(sample) < SAMPLE_ROWS:
                            sample.extend(batch[: SAMPLE_ROWS - len(sample)])
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        if progress:
                            progress(total, total, total_rows)
                    if progress:
                        progress(total, total, total_rows)
                except IngestCancelled:
                    # Same cancel-discards-the-partial contract as ingest_csv.
                    self.drop_source(source_id)
                    raise
                except Exception as e:
                    error = e
        finally:
            ro.close()
            cleanup()

        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(colmeta), source_id))

        if error is not None:
            # Rows that committed before the failure stay, with an accurate
            # row_count (ingest_csv's partial-import contract). None did:
            # an empty table would look exactly like a real empty import.
            if not total:
                self.drop_source(source_id)
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["undecodable_values"] = undecodable[0]
        if undecodable[0]:
            wlog.record("warn", f"{name}: {undecodable[0]:,} text value"
                                f"{'s' if undecodable[0] != 1 else ''} not valid UTF-8 — imported with \ufffd")
        return rec

    def ingest_plaso(
        self,
        path: str,
        name: str | None = None,
        build_fts: bool = True,
        progress=None,
        cancel=None,
    ) -> dict:
        """Imports a Plaso storage file as one flat timeline table — the
        .plaso-shaped sibling of ingest_sqlite_table, same conventions:
        TEXT columns, self.lock held per BATCH-sized chunk, cancel drops
        the partial source, the source file opened read-only and never
        touched. Column shape and format handling live in plasoread.py;
        column types are declared, not sampled — the reader KNOWS its
        first column is a datetime (the ingest_rows docstring's argument,
        applied here)."""
        if not plasoread.is_plaso_storage(path):
            raise ValueError(f"{os.path.basename(path)} is not a Plaso storage file")
        name = name or os.path.basename(path)
        file_hash = self._quick_hash(path)
        total_rows = plasoread.plaso_summary(path)["event_count"]

        cols = sanitize_columns(list(plasoread.PLASO_COLUMNS))
        ncols = len(cols)
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                " VALUES (?,?,?,?,?,?,?)",
                (name, os.path.abspath(path), "", "[]", file_hash, time.strftime("%Y-%m-%dT%H:%M:%S"),
                 _ingest_record("plaso")),
            )
            source_id = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
            dest_table = f"src_{source_id}"
            coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
            self.db.execute(f"CREATE TABLE {q(dest_table)} (rid INTEGER PRIMARY KEY, {coldefs})")
            self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (dest_table, source_id))

        placeholders = ",".join("?" * ncols)
        insert = f"INSERT INTO {q(dest_table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

        total = 0
        t0 = time.time()
        error: Exception | None = None
        with self._ingest_synchronous_off():
            try:
                batch: list[tuple] = []
                for row in plasoread.iter_plaso_rows(path):
                    batch.append(tuple(row))
                    if len(batch) >= BATCH:
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch = []
                        if progress:
                            progress(total, total, total_rows)
                if batch:
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                if progress:
                    progress(total, total, total_rows)
            except IngestCancelled:
                # Same cancel-discards-the-partial contract as ingest_csv.
                self.drop_source(source_id)
                raise
            except Exception as e:
                error = e

        colmeta = [{"name": c, "type": t} for c, t in zip(cols, plasoread.PLASO_COLUMN_TYPES)]
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(colmeta), source_id))

        if error is not None:
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        return rec

    def preview_xlsx_sheets(self, path: str) -> dict:
        """Read-only look at every data sheet in an Excel workbook, in the
        exact shape preview_sqlite_tables returns ({"tables": [...]}) so
        the import UI's picker renders both from one code path — a sheet
        is to a workbook what a table is to a SQLite file: which ones to
        import is a real per-file choice. likely_timestamp_columns is
        always empty here because there is nothing to choose: date-styled
        cells convert to ISO text unconditionally (the day-serial Excel
        stores means nothing in a filter or an export — see xlsxread)."""
        sheets = xlsxread.workbook_sheets(path)
        return {"tables": [{
            "name": sh["name"],
            "row_count": sh["row_count"],
            "columns": [{"name": c, "type": "TEXT"} for c in sh["columns"]],
            "likely_timestamp_columns": [],
        } for sh in sheets]}

    def ingest_xlsx_sheet(
        self,
        path: str,
        sheet_name: str,
        name: str | None = None,
        build_fts: bool = True,
        progress=None,
        cancel=None,
    ) -> dict:
        """Imports one sheet of an Excel workbook as a new source — the
        sheet-shaped sibling of ingest_sqlite_table, on purpose: one
        spooled file, N picked units, the same TEXT-column and
        batched-commit conventions (self.lock held per BATCH-sized chunk,
        never for the whole sheet), cancel drops the partial source.

        The header is the sheet's first *non-empty* row (hand-made
        workbooks put titles and blank rows above it); the table is sized
        to the wider of that header and the sheet's declared width, so
        data extending past the named headers lands in col_N columns
        instead of being cut. Rows *shorter* than the header are the
        normal Excel shape (trailing empties simply aren't stored) and
        pad silently; only rows wider than the table count as ragged.
        Date-styled cells arrive as ISO text rather than the raw
        day-serial — an unconditional conversion, called out in the sheet
        picker, not an analyst toggle like SQLite's WebKit-epoch chips,
        because the serial has no readable meaning at all. The workbook
        is opened read-only and never written."""
        name = name or f"{os.path.splitext(os.path.basename(path))[0]}.{sheet_name}"
        file_hash = self._quick_hash(path)
        rows_hint, width_hint, rows_iter = xlsxread.sheet_reader(path, sheet_name)
        try:
            header = next((r for r in rows_iter if any(c.strip() for c in r)), None)
            if header is None:
                raise ValueError(f"Sheet {sheet_name!r} has no rows")
            width = max(len(header), width_hint)
            cols = sanitize_columns([c.strip() for c in header] + [""] * (width - len(header)))
            ncols = len(cols)
            total_rows = max(rows_hint - 1, 0)

            with self.lock, self.db:
                cur = self.db.execute(
                    "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name, os.path.abspath(path), "", "[]", file_hash, time.strftime("%Y-%m-%dT%H:%M:%S"),
                     _ingest_record("xlsx", sheet=sheet_name)),
                )
                source_id = cur.lastrowid
                self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
                dest_table = f"src_{source_id}"
                coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
                self.db.execute(f"CREATE TABLE {q(dest_table)} (rid INTEGER PRIMARY KEY, {coldefs})")
                self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (dest_table, source_id))

            placeholders = ",".join("?" * ncols)
            insert = f"INSERT INTO {q(dest_table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

            sample: list[list[str]] = []
            batch: list[tuple] = []
            total = 0
            ragged = 0
            t0 = time.time()
            error: Exception | None = None
            with self._ingest_synchronous_off():
                try:
                    for raw in rows_iter:
                        if len(raw) != ncols:
                            if len(raw) > ncols:
                                ragged += 1
                            raw = (raw + [""] * ncols)[:ncols]
                        batch.append(tuple(raw))
                        if len(sample) < SAMPLE_ROWS:
                            sample.append(raw)
                        if len(batch) >= BATCH:
                            if cancel is not None and cancel():
                                raise IngestCancelled(f"Import of {name} cancelled")
                            total = self._commit_ingest_batch(insert, batch, source_id, total)
                            batch.clear()
                            if progress:
                                progress(total, total, total_rows)
                    if batch:
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                    if progress:
                        progress(total, total, total_rows)
                except IngestCancelled:
                    # Same cancel-discards-the-partial contract as ingest_csv.
                    self.drop_source(source_id)
                    raise
                except Exception as e:
                    error = e
        finally:
            rows_iter.close()

        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(colmeta), source_id))

        if error is not None:
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["ragged_rows"] = ragged
        return rec

    @staticmethod
    def _json_flatten_depth(flatten_mode: str, flatten_depth: int) -> int | None:
        if flatten_mode == "full":
            return None
        if flatten_mode == "depth":
            return max(0, flatten_depth)
        return 0  # "none"

    def preview_json_file(
        self, path: str, flatten_mode: str = "none", flatten_depth: int = 0, max_rows: int = 50
    ) -> dict:
        """Read-only sample for the import preview UI — same shape as
        preview_csv_text (columns/sample_rows/inferred_types), plus
        record_count so the UI can show how many rows the real ingest will
        produce. Reads the whole file (see _iter_json_records — a .json
        document can't be safely byte-truncated the way CSV's sniff-first-
        chunk can) but writes nothing; column discovery here is the same
        full pass ingest_json's own first pass does, recomputed on every
        flatten-mode change in the UI since the two are independent calls."""
        max_depth = self._json_flatten_depth(flatten_mode, flatten_depth)
        seen_cols: list[str] = []
        seen_set: set[str] = set()
        bad_prev = {"count": 0, "first_line": 0, "first_error": ""}
        clashes = {"count": 0}
        sample_flat: list[dict[str, str]] = []
        record_count = 0
        fallback0 = _cp1252_fallback_count()
        for rec in _iter_json_records(path, bad=bad_prev):
            flat = _flatten_json(rec, max_depth, clashes)
            for k in flat:
                if k not in seen_set:
                    seen_set.add(k)
                    seen_cols.append(k)
            if len(sample_flat) < max_rows:
                sample_flat.append(flat)
            record_count += 1
        if record_count == 0:
            if bad_prev["count"]:
                raise ValueError(
                    f"File has no readable records — {bad_prev['count']} unreadable line(s), "
                    f"first at line {bad_prev['first_line']}: {bad_prev['first_error']}")
            raise ValueError("File has no records")
        cols, row_of = _json_column_plan(seen_cols)
        sample_rows = [row_of(flat) for flat in sample_flat]
        types = [infer_type([r[i] for r in sample_rows]) for i in range(len(cols))] if sample_rows else ["text"] * len(cols)
        return {"columns": cols, "sample_rows": sample_rows, "inferred_types": types,
                "record_count": record_count, "bad_records": bad_prev["count"],
                "first_bad_line": bad_prev["first_line"], "key_collisions": clashes["count"],
                **{k: v for k, v in _encoding_report(sniff_text_encoding(path),
                                                     _cp1252_fallback_count() - fallback0).items()
                   if k != "meta"}}

    def ingest_json(
        self,
        path: str,
        name: str | None = None,
        flatten_mode: str = "none",
        flatten_depth: int = 0,
        build_fts: bool = True,
        progress=None,
        cancel=None,
    ) -> dict:
        """Streams a .json/.jsonl file into its own table — same TEXT-
        column, batched-commit convention as ingest_csv (self.lock held per
        BATCH-sized chunk, not the whole import).

        Two full passes rather than one: JSON has no fixed header row the
        way a CSV's first line is one, so which columns exist at all can
        only be known by looking at every record first (same spirit as
        CSV's ragged-row tolerance, just discovered up front here instead
        of per-row) — pass 1 flattens every record only far enough to
        collect the union of column keys, pass 2 re-reads and inserts
        against that now-fixed column set, filling in "" for any key a
        given record doesn't have (same convention as a short CSV row
        getting padded)."""
        name = name or os.path.basename(path)
        file_hash = self._quick_hash(path)
        max_depth = self._json_flatten_depth(flatten_mode, flatten_depth)

        seen_cols: list[str] = []
        seen_set: set[str] = set()
        record_count = 0
        bad_scan = {"count": 0, "first_line": 0, "first_error": ""}
        for rec in _iter_json_records(path, bad=bad_scan):
            for k in _flatten_json(rec, max_depth):
                if k not in seen_set:
                    seen_set.add(k)
                    seen_cols.append(k)
            record_count += 1
            # Pass 1 runs before the source row exists, so a cancel here has
            # nothing to clean up — it just stops the scan.
            if cancel is not None and record_count % 50_000 == 0 and cancel():
                raise IngestCancelled(f"Import of {name or os.path.basename(path)} cancelled")
        if record_count == 0:
            if bad_scan["count"]:
                raise ValueError(
                    f"File has no readable records — {bad_scan['count']} unreadable line(s), "
                    f"first at line {bad_scan['first_line']}: {bad_scan['first_error']}")
            raise ValueError("File has no records")

        cols, row_of = _json_column_plan(seen_cols)
        ncols = len(cols)

        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at, ingest)"
                " VALUES (?,?,?,?,?,?,?)",
                (name, os.path.abspath(path), "", "[]", file_hash, time.strftime("%Y-%m-%dT%H:%M:%S"),
                 _ingest_record("json", flatten_mode=flatten_mode, flatten_depth=flatten_depth)),
            )
            source_id = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
            table = f"src_{source_id}"
            coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
            self.db.execute(f"CREATE TABLE {q(table)} (rid INTEGER PRIMARY KEY, {coldefs})")
            self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (table, source_id))

        placeholders = ",".join("?" * ncols)
        insert = f"INSERT INTO {q(table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

        sample: list[list[str]] = []
        batch: list[tuple] = []
        total = 0
        t0 = time.time()
        # Bad lines were already counted by the column scan; count again
        # here (fresh collector) because THIS pass is the one that decides
        # what actually lands in the table.
        bad = {"count": 0, "first_line": 0, "first_error": ""}
        clashes = {"count": 0}
        error: Exception | None = None
        # Pass 2 only: pass 1 decoded the same bytes once already.
        fallback0 = _cp1252_fallback_count()
        with self._ingest_synchronous_off():
            try:
                for rec in _iter_json_records(path, bad=bad):
                    row = row_of(_flatten_json(rec, max_depth, clashes))
                    batch.append(tuple(row))
                    if len(sample) < SAMPLE_ROWS:
                        sample.append(row)
                    if len(batch) >= BATCH:
                        if cancel is not None and cancel():
                            raise IngestCancelled(f"Import of {name} cancelled")
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                        if progress:
                            progress(total, total, record_count)
                if batch:
                    if cancel is not None and cancel():
                        raise IngestCancelled(f"Import of {name} cancelled")
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                    batch.clear()
                if progress:
                    progress(total, total, record_count)
            except IngestCancelled:
                # Same cancel-discards-the-partial contract as ingest_csv.
                self.drop_source(source_id)
                raise
            except Exception as e:
                # Keep the parsed-but-uncommitted batch, as in ingest_csv.
                if batch:
                    with contextlib.suppress(Exception):
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                kept = (f" — import stopped; the {total:,} records before the failure were kept"
                        if total else "")
                error = ValueError(f"{e}{kept}")
                error.__cause__ = e

        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        decoded = _encoding_report(sniff_text_encoding(path), _cp1252_fallback_count() - fallback0)
        self._finish_source_columns(source_id, colmeta, decoded, name)

        if error is not None:
            if total == 0:
                self.drop_source(source_id)   # same no-husk rule as ingest_csv
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["bad_records"] = bad["count"]
        rec["first_bad_line"] = bad["first_line"]
        rec["key_collisions"] = clashes["count"]
        rec["encoding"], rec["cp1252_bytes"] = decoded["encoding"], decoded["cp1252_bytes"]
        return rec

    def _create_source_shell(self, name: str, cols: list[str], *, path: str | None = None,
                             file_hash: str | None = None, origin: str | None = None,
                             origin_meta: dict | None = None,
                             ingest: str | None = None) -> tuple[int, str]:
        """The sources row, its open tab and an empty src_<id> for a table
        about to be filled — one short transaction, so the id and the table
        exist together or not at all. Shared by ingest_rows and
        save_view_as_source. `columns` lands as '[]' here and the caller
        writes it once it knows the types (ingest_rows samples, a subset
        copies its parent's); _require_columns reads that gap as "still
        importing". Returns (source_id, table_name)."""
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO sources(name, path, table_name, columns, file_hash, imported_at,"
                " origin, origin_meta, ingest) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, os.path.abspath(path) if path else None, "", "[]", file_hash,
                 time.strftime("%Y-%m-%dT%H:%M:%S"), origin,
                 json.dumps(origin_meta) if origin_meta is not None else None, ingest),
            )
            source_id = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
            table = f"src_{source_id}"
            coldefs = ", ".join(f"{q(c)} TEXT" for c in cols)
            self.db.execute(f"CREATE TABLE {q(table)} (rid INTEGER PRIMARY KEY, {coldefs})")
            self.db.execute("UPDATE sources SET table_name=? WHERE id=?", (table, source_id))
        return source_id, table

    def ingest_rows(
        self,
        columns: list[str],
        rows: Iterable,
        *,
        name: str,
        path: str | None = None,
        build_fts: bool = True,
        column_types: list[str] | None = None,
        progress=None,
        origin: str | None = None,
        origin_meta: dict | None = None,
    ) -> dict:
        """Generic ingest for rows produced in-process — the path plugin
        ingest formats feed (see plugin_api.py), and the natural seam for
        any future non-file producer. Same conventions as ingest_csv, on
        purpose: all-TEXT columns through sanitize_columns, contiguous rid
        from 1 (invariant #2's root_virtual carve-out depends on it),
        self.lock held per BATCH-sized chunk rather than for the whole
        iterable, ragged rows padded/trimmed and counted rather than
        dropped, types inferred from a SAMPLE_ROWS sample (overridable via
        column_types — a parser that *knows* a column is a datetime
        shouldn't be at the mercy of a 500-row sample), and the FTS build
        kicked off in the background.

        `rows` is any iterable of sequences aligned to `columns` — a
        generator keeps memory flat on multi-GB inputs. Cells may be
        str/int/None; everything is stringified (None -> "") because source
        tables are TEXT and evidence fidelity beats type elegance
        (CLAUDE.md). A mid-iteration parser exception is re-raised after
        the column metadata is written, same partial-import behavior as a
        malformed line late in a CSV: whatever committed stays, with an
        accurate row_count, and the caller sees the error."""
        if not columns:
            raise ValueError("No columns")
        cols = sanitize_columns(list(columns))
        ncols = len(cols)
        # No file to fingerprint (a SQL-pane save, a plugin's computed
        # table): an identity of its own, so a session can find it again.
        file_hash = self._quick_hash(path) if path and os.path.isfile(path) else _made_table_hash()

        source_id, table = self._create_source_shell(
            name, cols, path=path, file_hash=file_hash, origin=origin, origin_meta=origin_meta,
            ingest=_ingest_record("rows"))

        placeholders = ",".join("?" * ncols)
        insert = f"INSERT INTO {q(table)} ({','.join(q(c) for c in cols)}) VALUES ({placeholders})"

        sample: list[list[str]] = []
        batch: list[tuple] = []
        total = 0
        ragged = 0
        t0 = time.time()
        error: Exception | None = None
        with self._ingest_synchronous_off():
            try:
                for raw in rows:
                    row = ["" if v is None else v if isinstance(v, str) else str(v) for v in raw]
                    if len(row) != ncols:
                        ragged += 1
                        row = (row + [""] * ncols)[:ncols]
                    batch.append(tuple(row))
                    if len(sample) < SAMPLE_ROWS:
                        sample.append(row)
                    if len(batch) >= BATCH:
                        total = self._commit_ingest_batch(insert, batch, source_id, total)
                        batch.clear()
                        if progress:
                            progress(total, 0, 0)
                if batch:
                    total = self._commit_ingest_batch(insert, batch, source_id, total)
                    batch.clear()
            except Exception as e:
                error = e

        types = [infer_type([r[i] for r in sample]) for i in range(ncols)] if sample else ["text"] * ncols
        if column_types:
            types = [(column_types[i] if i < len(column_types) and column_types[i] in ("text", "number", "datetime") else t)
                     for i, t in enumerate(types)]
        colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(colmeta), source_id))

        if error is not None:
            raise error

        if total:
            self._build_import_indexes(source_id)
        if build_fts and total:
            self._ensure_fts_building(source_id)

        elapsed = time.time() - t0
        rec = self.get_source(source_id)
        rec["elapsed_sec"] = round(elapsed, 2)
        rec["rows_per_sec"] = int(total / elapsed) if elapsed > 0 else 0
        rec["ragged_rows"] = ragged
        return rec

    @staticmethod
    def _import_pattern_matches(pattern: str, filename: str, rel_path: str) -> bool:
        """A pattern containing '/' matches against the file's path relative
        to the scan root (posix-separated, so a Windows-collected triage's
        backslashes never matter); one without matches the bare filename
        anywhere in the tree. Lets `*_Amcache_UnassociatedFileEntries.csv`
        work as a simple filename glob while `RegistryHives/*` can still
        exclude a whole subfolder. Case-insensitive — KAPE/EZTools output
        casing isn't consistent enough to make analysts type it exactly."""
        pat = pattern.strip().lower()
        if not pat:
            return False
        target = rel_path.lower() if "/" in pat else filename.lower()
        return fnmatch.fnmatch(target, pat)

    def scan_import_directory(
        self, root: str, *, recursive: bool = True,
        extensions: list[str] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        filename_patterns: list[str] | None = None,
    ) -> dict:
        """Preview for directory import (server.py's /api/ingest/dir/scan):
        walks `root`, buckets every file it finds into "would import" or
        "excluded, and why" — no ingestion happens here. Pure filesystem +
        fnmatch, no self.lock held beyond one cheap read of already-imported
        paths, so this is safe to call live as the analyst edits patterns.

        A file is excluded for exactly one of: its extension isn't
        recognized ("extension"; "IIS logs not selected" for a file named
        like an IIS log when `extensions` leaves out IIS_IMPORT_TYPE),
        include_patterns is non-empty and nothing
        in it matched ("no include pattern matched"), a pattern in
        exclude_patterns matched ("excluded by pattern: <pattern>"), it
        couldn't be stat'd ("unreadable" — a broken symlink, a permission
        error mid-walk), or it is a FIFO, socket or device node ("not a
        regular file" — never opened, since opening a FIFO blocks).
        Surfacing the reason is the point: tuning a profile's patterns is a
        visible feedback loop instead of guessing whether "*Amcache*"
        actually matched anything.

        `already_imported` flags a matched file whose absolute path already
        equals some source's stored path — same abspath-equality convention
        workspace.CaseRegistry.find_by_path already uses, no extra
        normalization added. This never blocks re-import (no hard skip
        here); it's the frontend's default-uncheck signal so re-running the
        same import doesn't silently duplicate every table.

        `filename_patterns` (from plugin-registered ingest formats — see
        plugin_api.py) is a second way past the extension gate, not a
        second include filter: a file whose extension isn't recognized
        still matches when its bare name fnmatches one of these. It exists
        because the files plugins are built for are exactly the ones an
        extension list can never name — "$MFT", "$J" — and it deliberately
        reuses the bare-filename half of _import_pattern_matches' rules so
        "matches" means one thing in this scan. include/exclude patterns
        still apply to these files afterward, unchanged."""
        root_abs = os.path.abspath(root)
        # "*" in the extension list means "and any other text file" — the
        # raw-text importer's catch-all (ingest_text). Off unless asked: a
        # triage tree is full of READMEs, hashes and scripts nobody wants
        # as tables. A file that gets in this way is checked for NULs
        # first; a binary is excluded with its own reason.
        other_text = any(e in ("*", ".*") for e in (extensions or []))
        # IIS logs: admitted by name, on unless the list leaves the token
        # out (a profile saved before the chip existed does).
        iis_logs = extensions is None or IIS_IMPORT_TYPE in (e.lower() for e in extensions)
        # None means "the defaults"; an empty list means exactly that — the
        # analyst turned every chip off. `or` conflated the two, so a scan
        # with nothing selected matched the default set.
        exts = {
            (e if e.startswith(".") else "." + e).lower()
            for e in (DEFAULT_IMPORT_EXTENSIONS if extensions is None else extensions)
            if e not in ("*", ".*") and e.lower() != IIS_IMPORT_TYPE
        }
        includes = [p for p in (include_patterns or []) if p.strip()]
        excludes = [p for p in (exclude_patterns or []) if p.strip()]
        fname_pats = [p for p in (filename_patterns or []) if p.strip()]

        with self.lock:
            existing_paths = {
                os.path.abspath(r[0]) for r in self.db.execute("SELECT path FROM sources WHERE path IS NOT NULL")
            }

        walker = os.walk(root_abs) if recursive else itertools.islice(os.walk(root_abs), 1)
        matched: list[dict] = []
        excluded: list[dict] = []
        truncated = False
        for dirpath, _dirnames, filenames in walker:
            for fname in sorted(filenames):
                if len(matched) + len(excluded) >= MAX_SCAN_RESULTS:
                    truncated = True
                    break
                fpath = os.path.join(dirpath, fname)
                rel = os.path.relpath(fpath, root_abs).replace(os.sep, "/")
                try:
                    st = os.stat(fpath)
                except OSError:
                    excluded.append({"path": fpath, "rel_path": rel, "reason": "unreadable"})
                    continue
                # Only a regular file is a candidate, decided from the stat
                # already in hand and before anything opens it. os.walk lists
                # FIFOs, sockets and device nodes as files, and a mounted
                # *nix image is full of them (/run/systemd/initctl). open() on
                # a FIFO blocks until a writer appears, which on an image is
                # never: the binary probe hung the scan, and a FIFO named
                # *.csv was matched and hung the import. os.stat follows
                # symlinks, so a link to a real file still imports and one
                # to a FIFO does not.
                if not stat.S_ISREG(st.st_mode):
                    excluded.append({"path": fpath, "rel_path": rel, "reason": "not a regular file"})
                    continue
                entry = {"path": fpath, "rel_path": rel, "size_bytes": st.st_size}
                ext = os.path.splitext(fname)[1].lower()
                is_iis = iisread.log_format(fname) is not None
                by_filename_pattern = any(fnmatch.fnmatch(fname.lower(), p.lower()) for p in fname_pats)
                by_ext = ext in exts
                unclaimed = not by_ext and not by_filename_pattern and not (is_iis and iis_logs)
                if unclaimed and not other_text:
                    excluded.append({**entry, "reason": "IIS logs not selected" if is_iis else "extension"})
                    continue
                if includes and not any(self._import_pattern_matches(p, fname, rel) for p in includes):
                    excluded.append({**entry, "reason": "no include pattern matched"})
                    continue
                hit = next((p for p in excludes if self._import_pattern_matches(p, fname, rel)), None)
                if hit:
                    excluded.append({**entry, "reason": f"excluded by pattern: {hit}"})
                    continue
                # The binary probe reads the file, so it runs last: only a
                # file the name gates let through gets opened — a tree of
                # excluded build output is never sniffed.
                if unclaimed and not is_iis and looks_binary(fpath):
                    excluded.append({**entry, "reason": "binary"})
                    continue
                # kind routes the frontend's per-file import call: the two
                # built-in parsers by their own extensions, everything else
                # ("plugin") resolved client-side against the loaded plugin
                # formats — the scan doesn't know which format claimed an
                # extension/pattern, and doesn't need to. An IIS log is
                # read as one however it got in — a plugin claiming .log
                # (esxi_logs) or "other text files" included: built-in
                # recognition wins, as built-in extensions do.
                if is_iis:
                    kind = "iis"
                elif ext in (".json", ".jsonl", ".ndjson"):
                    kind = "json"
                elif ext in PLASO_IMPORT_EXTENSIONS:
                    kind = "plaso"
                elif ext in DEFAULT_IMPORT_EXTENSIONS:
                    kind = "csv"
                elif by_ext or by_filename_pattern:
                    kind = "plugin"
                else:
                    kind = "text"
                matched.append({
                    **entry,
                    "kind": kind,
                    "already_imported": fpath in existing_paths,
                })
            if truncated:
                break

        matched.sort(key=lambda e: e["rel_path"])
        excluded.sort(key=lambda e: e["rel_path"])
        return {"root": root_abs, "matched": matched, "excluded": excluded, "truncated": truncated}

    # ------------------------------------------------------------ ingest jobs

    MAX_CONCURRENT_INGESTS = 2  # parse work is CPU-bound; more just thrash
    INGEST_JOB_KEEP = 20        # finished jobs kept for the UI panel

    def start_ingest_job(self, kind: str, path: str, *, name: str | None = None,
                         options: dict | None = None, delete_after: bool = False,
                         spool_dir: str | None = None, recorded_path: str | None = None) -> dict:
        """Runs an ingest on a background daemon thread and returns its job
        record immediately — the exact reason start_search_all_job exists,
        applied to the operation that takes the longest of anything in the
        app. A 50 GB import used to be one multi-minute POST the analyst
        sat behind with no progress, no way to tell "working" from
        "crashed", and a browser tab they couldn't use meanwhile.

        `kind` is 'csv' | 'text' | 'json' | 'sqlite' | 'xlsx' | 'plaso' | 'iis'. For 'sqlite',
        options['tables'] is [{table, name?, timestamp_columns?}, ...] —
        one uploaded file, one job, N sources, so the file is spooled and
        read once rather than re-uploaded per table. `delete_after` removes
        `path` when the job ends (the upload route's tempfile — which the
        old synchronous upload endpoints leaked on disk, tens of GB at
        this tool's file sizes), and `spool_dir` after it: the private
        directory the upload was spooled into (server._upload_spool).

        `recorded_path` is what the job's tables say they were imported
        from when that is not `path` — an upload's spool is deleted when
        the job ends, so the name the browser sent is recorded instead
        (set_source_path's reason; record_upload_path does it).

        Progress lands in the job record from the per-BATCH `progress`
        callback the ingest paths already had (rows plus a unit/units_total
        pair: bytes for CSV — a percentage without pre-scanning the file —
        records for JSON, rows for SQLite). Cancellation is cooperative per
        BATCH; a cancelled ingest *drops* its partial source (see
        ingest_csv).

        'derive' is the odd one out: no file at all (`path` is ''), it
        backfills one derived column's values from a column already in the
        case. It rides this machinery rather than growing a second job
        system because it wants exactly what this one provides — a progress
        bar over a multi-million-row pass, per-BATCH cancellation, the jobs
        panel, and close()'s cancel-and-join.

        A job asked for while the case is closing is refused with
        OpCancelled (499 on a route, the same answer save_view_as_source
        gives) rather than started. Registration reads `_closing` inside
        the one locked step that registers, so either the job is in the
        snapshot close() took — and gets cancelled and joined — or it
        never exists: no thread left running a backfill into a connection
        close() is about to shut. The caller this is really for is
        _cascade_dependent_derives, which runs on a worker close() is
        *already draining* and would otherwise queue its child job behind
        the snapshot; it marks the child 'partial' and logs why. The derive
        paths that commit a definition BEFORE asking for the job
        (add_derived_column, add_derived_columns, rederive_column) undo
        that definition on the refusal — a column whose only job was never
        started would otherwise be 'building' for good."""
        if kind not in ("csv", "text", "json", "sqlite", "xlsx", "plaso", "iis", "derive"):
            raise ValueError(f"Unknown ingest kind: {kind}")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        with self._ingest_jobs_lock:
            if self._closing:
                noun = self._job_noun({"kind": kind}).lower()
                raise OpCancelled(f"The case is closing — the {noun} was not started")
            self._ingest_job_seq += 1
            job = {
                "job_id": self._ingest_job_seq,
                "kind": kind,
                "name": name or os.path.basename(path),
                "path": path,
                "status": "queued",
                "rows_done": 0,
                "units_done": 0,
                "units_total": size if kind in ("csv", "text") else ((options or {}).get("units_total") or 0),
                "unit": "bytes" if kind in ("csv", "text") else ("records" if kind == "json" else "rows"),
                "tables_done": 0,
                "tables_total": len((options or {}).get("tables") or []) if kind in ("sqlite", "xlsx") else 0,
                "current_table": None,
                "source_ids": [],
                "result": None,
                "error": None,
                "cancelled": False,
                "started_at": time.time(),
                "finished_at": None,
                "options": options or {},
                "delete_after": delete_after,
                "spool_dir": spool_dir,
                "recorded_path": recorded_path,
                "thread": None,
            }
            self._ingest_jobs[job["job_id"]] = job
            self._prune_ingest_jobs_locked()
        t = threading.Thread(target=self._ingest_job_worker, args=(job,), daemon=True)
        with self._ingest_jobs_lock:
            job["thread"] = t
        wlog.record("info", f"{self._job_noun(job)} queued: {job['name']} ({job['kind']}) — job #{job['job_id']}")
        t.start()
        return self._ingest_job_snapshot(job)

    @staticmethod
    def _job_elapsed(job: dict) -> float:
        """Seconds since the job started RUNNING — not since it was queued,
        or a 20 ms import that waited ten minutes behind a 50 GB one would
        log as a ten-minute import."""
        started = job.get("run_started_at") or job.get("started_at")
        return max(0.0, time.time() - started) if started else 0.0

    @staticmethod
    def _job_noun(job: dict) -> str:
        """A derive is a job too, but it isn't an import; the log says which."""
        return "Derive" if job.get("kind") == "derive" else "Import"

    def _ingest_job_worker(self, job: dict) -> None:
        acquired = False
        try:
            self._ingest_sem.acquire()
            acquired = True
            with self._ingest_jobs_lock:
                if job["cancelled"]:
                    job["status"] = "cancelled"
                    wlog.record("info", f"{self._job_noun(job)} cancelled: {job['name']} ({job['kind']}) before it started")
                    return
                job["status"] = "running"
                job["run_started_at"] = time.time()

            def cancel() -> bool:
                return job["cancelled"]  # plain bool read; set under the jobs lock

            def progress(rows: int, units: int, units_total: int) -> None:
                with self._ingest_jobs_lock:
                    job["rows_done"] = rows
                    job["units_done"] = units
                    job["units_total"] = units_total

            opts = job["options"]
            if job["kind"] == "csv":
                results = [self.ingest_csv(
                    job["path"], name=job["name"],
                    delimiter=opts.get("delimiter"), build_fts=opts.get("build_fts", True),
                    has_header=opts.get("has_header", True), column_types=opts.get("column_types"),
                    progress=progress, cancel=cancel,
                )]
            elif job["kind"] == "derive":
                # options carries def_ids (a flatten adding several columns
                # in one pass) or def_id (the single-column create and
                # re-derive paths) — one job either way. A merge's carries
                # `parts`, one per member, which this one job walks in turn
                # (_backfill_derive_parts).
                if opts.get("parts"):
                    out = self._backfill_derive_parts(
                        job, opts["parts"], progress=progress, cancel=cancel,
                        drop_on_cancel=opts.get("drop_on_cancel", False),
                    )
                else:
                    out = [self.backfill_derived_columns(
                        opts.get("def_ids") or [opts["def_id"]], progress=progress, cancel=cancel,
                        drop_on_cancel=opts.get("drop_on_cancel", False),
                        previous=opts.get("previous"),
                    )]
                rows = sum(res["rows"] for res in out)
                with self._ingest_jobs_lock:
                    job["status"] = "done"
                    job["rows_done"] = rows
                    job["source_ids"] = [res["source_id"] for res in out]
                    job["result"] = self._derive_job_result(out)
                # Chained columns: anything derived FROM a column this job
                # just recomputed is now stale — re-derive it, which
                # cascades down the chain one job at a time (each named
                # for why it's running). A fresh create has no dependents
                # (its name is new), so this only fires on re-derives.
                self._cascade_dependent_derives(
                    [(res["source_id"], [c["name"] for c in res["columns"]]) for res in out])
                tables = ", ".join(str(res["source_id"]) for res in out)
                wlog.record("info", f"Derive finished: {job['name']} — {rows:,} rows in "
                                    f"{self._job_elapsed(job):.1f}s "
                                    f"(table{'s' if len(out) != 1 else ''} {tables})")
                return
            elif job["kind"] == "text":
                results = [self.ingest_text(
                    job["path"], name=job["name"], build_fts=opts.get("build_fts", True),
                    progress=progress, cancel=cancel,
                )]
            elif job["kind"] == "plaso":
                results = [self.ingest_plaso(
                    job["path"], name=job["name"],
                    build_fts=opts.get("build_fts", True),
                    progress=progress, cancel=cancel,
                )]
            elif job["kind"] == "iis":
                results = [self.ingest_iis(
                    job["path"], name=job["name"], build_fts=opts.get("build_fts", True),
                    progress=progress, cancel=cancel, log_format=opts.get("log_format"),
                    column_types=opts.get("column_types"),
                )]
            elif job["kind"] == "json":
                results = [self.ingest_json(
                    job["path"], name=job["name"],
                    flatten_mode=opts.get("flatten_mode", "none"),
                    flatten_depth=opts.get("flatten_depth", 0),
                    build_fts=opts.get("build_fts", True),
                    progress=progress, cancel=cancel,
                )]
            else:  # sqlite/xlsx: one job, N tables (sheets) out of the one spooled file
                results = []
                for i, t in enumerate(opts.get("tables") or []):
                    if job["cancelled"]:
                        raise IngestCancelled("cancelled between tables")
                    with self._ingest_jobs_lock:
                        job["current_table"] = t["table"]
                        job["tables_done"] = i
                    if job["kind"] == "sqlite":
                        results.append(self.ingest_sqlite_table(
                            job["path"], t["table"], name=t.get("name"),
                            build_fts=opts.get("build_fts", True),
                            timestamp_columns=t.get("timestamp_columns"),
                            progress=progress, cancel=cancel,
                        ))
                    else:
                        results.append(self.ingest_xlsx_sheet(
                            job["path"], t["table"], name=t.get("name"),
                            build_fts=opts.get("build_fts", True),
                            progress=progress, cancel=cancel,
                        ))
                    with self._ingest_jobs_lock:
                        job["tables_done"] = i + 1
            # Before the job reads as done, so the source list the client
            # fetches on that never shows the spool's path.
            if job["recorded_path"] is not None:
                self.record_upload_path(job["path"], job["recorded_path"])
            # Directory import: drop each new table into the folder that
            # mirrors its on-disk path (created once, shared across the
            # tree), and bring it in CLOSED — a folder import can be dozens
            # of files, and opening a tab per file buries the working set.
            # They're one click away in the folder tree. folder_path is ""
            # for a file at the scan root, which stays open like a single
            # import.
            if opts.get("folder_path"):
                fid = self.ensure_folder_path(opts["folder_path"])
                for r in results:
                    if fid is not None:
                        self.set_source_folder(r["id"], fid)
                    self.set_tab_open(r["id"], False)
            with self._ingest_jobs_lock:
                job["status"] = "done"
                job["rows_done"] = sum(r.get("row_count") or 0 for r in results)
                job["source_ids"] = [r["id"] for r in results]
                job["result"] = [
                    {k: r.get(k) for k in ("id", "name", "row_count", "elapsed_sec", "rows_per_sec",
                                           "ragged_rows", "suspect_quote_rows", "bad_records",
                                           "first_bad_line", "key_collisions", "encoding",
                                           "cp1252_bytes", "undecodable_values")}
                    for r in results
                ]
            wlog.record("info", f"Import finished: {job['name']} ({job['kind']}) — "
                                f"{job['rows_done']:,} rows in {self._job_elapsed(job):.1f}s"
                                f" (table{'s' if len(job['source_ids']) != 1 else ''} "
                                f"{', '.join(str(i) for i in job['source_ids'])})")
        except IngestCancelled:
            # The tables a multi-table job finished before the cancel stay.
            self._record_upload_path_quietly(job)
            with self._ingest_jobs_lock:
                job["status"] = "cancelled"
            wlog.record("info", f"{self._job_noun(job)} cancelled: {job['name']} ({job['kind']}) "
                                f"after {self._job_elapsed(job):.1f}s")
        except Exception as e:  # noqa: BLE001 — surfaced to the UI as job.error
            # And so does what a failed one committed before the error.
            self._record_upload_path_quietly(job)
            with self._ingest_jobs_lock:
                job["status"] = "error"
                job["error"] = str(e)
            # The toast this becomes is gone in seconds; this is the record.
            wlog.record("error", f"{self._job_noun(job)} failed: {job['name']} ({job['kind']}) — "
                                 f"{type(e).__name__}: {e}")
        finally:
            with self._ingest_jobs_lock:
                job["finished_at"] = time.time()
            if acquired:
                self._ingest_sem.release()
            if job["delete_after"]:
                with contextlib.suppress(OSError):
                    os.remove(job["path"])
                if job["spool_dir"]:
                    with contextlib.suppress(OSError):
                        os.rmdir(job["spool_dir"])

    def record_upload_path(self, spool_path: str, recorded_path: str) -> None:
        """Every table read from the upload spool `spool_path` records
        `recorded_path` — the name the browser sent — as where it came
        from, rather than the spool, a temp file deleted as soon as the
        import ends (set_source_path's reason). A session saved against
        such a table looked for its file there and reported it missing.

        Keyed on the spool's path rather than on ids because an upload's
        spool is unique to it (a private directory, server._upload_spool),
        and because the tables that need it include ones the caller never
        got an id for: the rows a failed import committed before its
        error, and the tables a multi-table job finished before one
        failed."""
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET path=? WHERE path=?",
                            (recorded_path, os.path.abspath(spool_path)))

    def _record_upload_path_quietly(self, job: dict) -> None:
        # The job's own failure is the error worth reporting; one from
        # this (the failure may have been the database itself) is not.
        if job.get("recorded_path") is not None:
            with contextlib.suppress(sqlite3.Error):
                self.record_upload_path(job["path"], job["recorded_path"])

    def _ingest_job_snapshot(self, job: dict) -> dict:
        keys = ("job_id", "kind", "name", "status", "rows_done", "units_done",
                "units_total", "unit", "tables_done", "tables_total", "current_table",
                "source_ids", "result", "error", "cancelled", "started_at", "finished_at")
        with self._ingest_jobs_lock:
            return {k: job[k] for k in keys}

    def _prune_ingest_jobs_locked(self) -> None:
        done = [j for j in self._ingest_jobs.values()
                if j["status"] in ("done", "error", "cancelled")]
        excess = len(done) - self.INGEST_JOB_KEEP
        if excess > 0:
            done.sort(key=lambda j: j["finished_at"] or 0)
            for j in done[:excess]:
                self._ingest_jobs.pop(j["job_id"], None)

    def list_ingest_jobs(self) -> list[dict]:
        with self._ingest_jobs_lock:
            ids = sorted(self._ingest_jobs, reverse=True)
            jobs = [self._ingest_jobs[i] for i in ids]
        return [self._ingest_job_snapshot(j) for j in jobs]

    def cancel_ingest_job(self, job_id: int) -> bool:
        with self._ingest_jobs_lock:
            job = self._ingest_jobs.get(job_id)
            if job is None or job["status"] in ("done", "error", "cancelled"):
                return False
            job["cancelled"] = True
            return True

    def wait_for_ingest_job(self, job_id: int | None = None, timeout: float | None = None) -> dict | None:
        """Blocks until the given (or every) ingest job finishes. Tests
        only; nothing in the request path waits on a job."""
        with self._ingest_jobs_lock:
            jobs = [j for j in self._ingest_jobs.values()
                    if job_id is None or j["job_id"] == job_id]
        snap = None
        for j in jobs:
            t = j.get("thread")
            if t and t.is_alive():
                t.join(timeout)
            snap = self._ingest_job_snapshot(j)
        return snap

    def copies_in_flight(self) -> int:
        """How many save_view_as_source copies are mid-way right now. A
        source mid-copy has a growing row_count and columns='[]', so the
        routes that refuse while an import is running (copy_sources,
        save-as) read this too — server.py's _busy_reason."""
        with self._ingest_jobs_lock:
            return len(self._subset_copies)

    def build_fts(self, source_id: int) -> None:
        """(Re)builds the trigram substring index for a source: a single
        `doc` column (every column concatenated, via a `src_<id>_doc` view
        over `_blob_expr`) indexed with `tokenize='trigram', detail=none,
        columnsize=0`, queried with a bare `doc LIKE ?` (never MATCH — see
        below). Trigram indexing 3-character sequences is what makes
        Contains-mode search both correct *and* fast for a fragment buried
        mid-token (e.g. "jacso" inside `C:\\users\\jacso\\desktop\\file.txt`)
        — a real index lookup instead of a full-table LIKE scan.

        Why this exact shape — each of these was measured, not assumed
        (332K-row/285MB EvtxECmd source):

        - `detail=none, columnsize=0` drops the per-occurrence position
          lists (which nothing here uses — no ranking, no highlighting),
          cutting the index from 892MB to 143MB (–84%). The cost is that
          FTS5 must verify each candidate against the real text, so query
          time scales with *result count*: identical for rare/medium terms
          (the IOC-hunting case), ~0.8s worst-case for a term matching 94%
          of all rows — a degenerate result set anyway.
        - Under detail=none a multi-trigram MATCH is a *phrase* query and
          errors outright ("phrase queries are not supported"); the query
          form is SQLite 3.45+'s LIKE pushdown instead. That pushdown is
          per-column — a table-level `fts LIKE ?` runs against the hidden
          table-name column (NULL outside MATCH) and silently returns 0
          rows — which is why the index is one concatenated `doc` column
          rather than mirroring the source's columns.
        - content= points at the src_<id>_doc *view*, so candidate
          verification recomputes the concat on demand — no second stored
          copy of the data.

        Batches its own commits (DROP/CREATE once, then INSERT chunks keyed
        by rid range) instead of one multi-second transaction, so a build
        never freezes every other request for its duration — same reasoning
        as ingest_csv's own chunked commits. Not the same size, though:
        ingest's BATCH rows are cheap to write and expensive to tokenize,
        and at the trigram tokenizer's 2-3 MB/s a BATCH of wide EvtxECmd
        rows held the lock for ten seconds a commit, for the whole of a
        25-minute build that every import starts. So each chunk is sized to
        hold the lock for about FTS_COMMIT_SECONDS, scaled from the one
        before it. Measured while a 4.3M-row MFTECmd table built, a tag
        click every half second waited 1.7 s at the median and 3.3 s at
        worst before, 0.21 s and 0.89 s after; on EvtxECmd rows the
        longest single commit had been 10.7 s.

        Resumable: each chunk records the next rid to index in
        fts_progress, in its own transaction, so a build the case closed
        under picks up where it stopped on the next call instead of
        dropping an index that may be most of the way there. Only when the
        progress row, the index's shape and the doc view all still match —
        anything else rebuilds from scratch, as before. Called
        synchronously here; _ensure_fts_building is what runs this on a
        background thread so ingest/search never block waiting for it.

        Each chunk also yields the thread briefly (time.sleep) right after
        releasing the lock, not just released-and-immediately-reacquired:
        measured directly, a tight release/reacquire loop with no yield
        starves a foreground request waiting on the same lock for several
        chunks in a row before the OS scheduler gives it a turn — one
        request stalled >13s despite every individual chunk holding the
        lock for under a second. A ~20ms yield dropped that worst case to
        ~1.4s (one chunk's worth, as intended) for a ~5% longer total
        build.

        Deliberately *not* followed by an FTS5 'optimize' (or an incremental
        'merge' loop), despite that being the usual advice after a chunked
        build. Measured on a 400K-row index built in BATCH-sized chunks:
        optimize does consolidate segments (the structure record shrank
        51->24 bytes) but query time was unchanged on every shape tried —
        rare term 4.6ms -> 4.0ms, a term matching every row 205ms -> 208ms,
        a miss 0.1ms -> 0.0ms, all inside run-to-run noise — while the case
        file grew ~7% (48.8MB -> 52.2MB), since the pre-merge pages are
        freed to the freelist and nothing here VACUUMs. Two reasons it
        doesn't pay: FTS5's own automerge already consolidates as the chunks
        go in, and under detail=none query cost is dominated by verifying
        candidates against the content view, not by walking segments."""
        src = self._source_lite(source_id)
        # Base columns only. A derived column is computed from a column
        # that's already in the doc blob, so indexing it would only make
        # the same evidence match twice — and keeping the doc view over the
        # source table alone means the index needs no rebuild when a
        # derived column is added or removed.
        table, cols = src["table_name"], [c["name"] for c in self._base_cols(src)]
        fts = f"fts_{source_id}"
        doc_view = f"{table}_doc"
        doc_sql = f"CREATE VIEW {q(doc_view)} AS SELECT rid, {_blob_expr(cols)} AS doc FROM {q(table)}"
        with self.lock, self.db:
            start = self._fts_resume_point(source_id, fts, doc_view, doc_sql)
            if start is None:
                self.db.execute(f"DROP TABLE IF EXISTS {q(fts)}")
                self.db.execute(f"DROP VIEW IF EXISTS {q(doc_view)}")
                self.db.execute(doc_sql)
                self.db.execute(
                    f"CREATE VIRTUAL TABLE {q(fts)} USING fts5(doc, "
                    f"content={q(doc_view)}, content_rowid='rid', tokenize='trigram', "
                    f"detail=none, columnsize=0)"
                )
                start = 1
                self.db.execute(
                    "INSERT INTO fts_progress(source_id, next_rid, doc_sql) VALUES (?,?,?)"
                    " ON CONFLICT(source_id) DO UPDATE SET next_rid=excluded.next_rid,"
                    " doc_sql=excluded.doc_sql", (source_id, start, doc_sql))
            max_rid = self.db.execute(f"SELECT COALESCE(MAX(rid), 0) FROM {q(table)}").fetchone()[0]
        rows = FTS_COMMIT_FIRST_ROWS
        while start <= max_rid:
            if self._closing:
                return   # fts_progress already says where the next build starts
            with self.lock:
                t0 = time.perf_counter()
                with self.db:
                    self.db.execute(
                        f"INSERT INTO {q(fts)}(rowid, doc) "
                        f"SELECT rid, doc FROM {q(doc_view)} WHERE rid >= ? AND rid < ?",
                        (start, start + rows),
                    )
                    self.db.execute("UPDATE fts_progress SET next_rid=? WHERE source_id=?",
                                    (start + rows, source_id))
                held = time.perf_counter() - t0
            start += rows
            rows = _next_fts_commit_rows(rows, held)
            time.sleep(0.02)
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET has_fts=1 WHERE id=?", (source_id,))
            self.db.execute("DELETE FROM fts_progress WHERE source_id=?", (source_id,))

    def _fts_resume_point(self, source_id: int, fts: str, doc_view: str, doc_sql: str) -> int | None:
        """The rid an interrupted build_fts should carry on from, or None
        to build from scratch. Under the caller's writer transaction.

        Resumes only what this code itself left half done: a progress row
        read through the same doc view (so the same columns, concatenated
        the same way), an fts_ table of the current shape, and that view
        still in place. A missing or different piece — a case from before
        progress was recorded, a legacy-shape index, a rebuild asked for
        on a table that already has one — is a fresh build, which is what
        every build used to be."""
        row = self.db.execute(
            "SELECT next_rid, doc_sql FROM fts_progress WHERE source_id=?", (source_id,)).fetchone()
        if not row or row["doc_sql"] != doc_sql:
            return None
        found = {r[0]: r[1] for r in self.db.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?)", (fts, doc_view))}
        if "detail=none" not in (found.get(fts) or "") or found.get(doc_view) != doc_sql:
            return None
        return int(row["next_rid"])

    @staticmethod
    def _looks_delimited_by(head: str, ch: str, sample: int = 20) -> bool:
        """Whether `ch` actually splits this text, so a file's extension is
        only believed when the bytes agree with it.

        Every sampled line has to contain it at least once — a character
        the file does not split on at all is incidental, whatever the name
        says — and most of them have to agree on how many. The 10% slack
        is for ragged rows, which this app pads rather than refuses
        (docs/notes/ingest.md), and for the occasional quoted field
        carrying a literal delimiter."""
        rows = [ln for ln in head.splitlines() if ln.strip()][:sample]
        if not rows:
            return False
        counts = [ln.count(ch) for ln in rows]
        if min(counts) < 1:
            return False
        modal = max(set(counts), key=counts.count)
        return counts.count(modal) / len(counts) >= 0.9

    @classmethod
    def _sniff(cls, head: str, name: str | None = None) -> str:
        """The delimiter, from the file's name first and its content second.

        `name` is the file name when one is known (it is, on every path
        that reaches here). An extension in EXTENSION_DELIMITERS names a
        delimiter outright, and that beats sniffing the bytes — but only
        when the bytes agree, so a mislabelled file still falls through to
        the sniffer rather than being read down the wrong column.

        Content-only sniffing stays underneath, unchanged, for .txt, for
        extensionless dumps, and for the named file whose named delimiter
        is not in it."""
        ext = os.path.splitext(name or "")[1].lower()
        named = EXTENSION_DELIMITERS.get(ext)
        if named and cls._looks_delimited_by(head, named):
            return named
        try:
            return csv.Sniffer().sniff(head[:8192], delimiters=",\t;|").delimiter
        except csv.Error:
            first = head.splitlines()[0] if head else ""
            return max(",\t;|", key=first.count)

    @staticmethod
    def _quick_hash(path: str) -> str:
        """Size + head/tail digest. Fast on multi-GB files and stable enough to
        recognise the same evidence file across imports."""
        h = hashlib.sha256()
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(1 << 20))
            if size > (1 << 21):
                f.seek(-(1 << 20), os.SEEK_END)
                h.update(f.read())
        return h.hexdigest()[:32]

    # ----------------------------------------------------------------- sources

    # ------------------------------------------------------------ folders
    #
    # A per-case tree an analyst sorts tables into (and a directory import
    # reproduces from disk). Folders are organizational only: nothing here
    # ever touches a source table, and delete_folder reparents rather than
    # drops (invariant #1). Membership is keyed by the SIGNED source id so a
    # merge folds like any other table.

    def list_folders(self) -> list[dict]:
        with self._reader() as ro:
            rows = ro.execute(
                "SELECT id, name, parent_id, pos FROM source_folders "
                "ORDER BY (parent_id IS NULL) DESC, parent_id, pos, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def _source_folder(self, source_id: int) -> tuple[int | None, int]:
        """(folder_id, pos) for one signed source id — (None, 0) if it sits
        at the root (no map row)."""
        with self._reader() as ro:
            r = ro.execute(
                "SELECT folder_id, pos FROM source_folder_map WHERE source_id=?",
                (source_id,)).fetchone()
        return (r["folder_id"], r["pos"]) if r else (None, 0)

    def _folder_exists(self, folder_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM source_folders WHERE id=?", (folder_id,)).fetchone() is not None

    def create_folder(self, name: str, parent_id: int | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("A folder needs a name")
        if len(name) > 200:
            raise ValueError("That folder name is too long")
        with self.lock, self.db:
            if parent_id is not None and not self._folder_exists(parent_id):
                raise ValueError("That parent folder no longer exists")
            pos = (self.db.execute(
                "SELECT COALESCE(MAX(pos), -1) + 1 FROM source_folders WHERE parent_id IS ?",
                (parent_id,)).fetchone()[0])
            cur = self.db.execute(
                "INSERT INTO source_folders(name, parent_id, pos) VALUES (?,?,?)",
                (name, parent_id, pos))
            fid = cur.lastrowid
        return {"id": fid, "name": name, "parent_id": parent_id, "pos": pos}

    def rename_folder(self, folder_id: int, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("A folder needs a name")
        if len(name) > 200:
            raise ValueError("That folder name is too long")
        with self.lock, self.db:
            if not self._folder_exists(folder_id):
                raise KeyError(f"No folder {folder_id}")
            self.db.execute("UPDATE source_folders SET name=? WHERE id=?", (name, folder_id))

    def _descendant_ids(self, folder_id: int) -> set[int]:
        """folder_id and every folder beneath it — used to reject a move
        that would make a folder its own ancestor."""
        seen = {folder_id}
        frontier = [folder_id]
        while frontier:
            kids = self.db.execute(
                "SELECT id FROM source_folders WHERE parent_id=?", (frontier.pop(),)).fetchall()
            for (kid,) in kids:
                if kid not in seen:
                    seen.add(kid)
                    frontier.append(kid)
        return seen

    def move_folder(self, folder_id: int, parent_id: int | None, pos: int | None = None) -> None:
        """Reparent and/or reorder a folder. A move into the folder's own
        subtree (including itself) is refused — that would orphan a cycle."""
        with self.lock, self.db:
            if not self._folder_exists(folder_id):
                raise KeyError(f"No folder {folder_id}")
            if parent_id is not None:
                if not self._folder_exists(parent_id):
                    raise ValueError("That parent folder no longer exists")
                if parent_id in self._descendant_ids(folder_id):
                    raise ValueError("A folder can't be moved inside itself")
            if pos is None:
                pos = (self.db.execute(
                    "SELECT COALESCE(MAX(pos), -1) + 1 FROM source_folders WHERE parent_id IS ?",
                    (parent_id,)).fetchone()[0])
            self.db.execute(
                "UPDATE source_folders SET parent_id=?, pos=? WHERE id=?",
                (parent_id, pos, folder_id))

    def reorder_folders(self, parent_id: int | None, ordered_ids: list[int]) -> None:
        """Set sibling order under one parent from a full id list (index =
        pos). Ids not actually children of parent_id are ignored."""
        with self.lock, self.db:
            kids = {r[0] for r in self.db.execute(
                "SELECT id FROM source_folders WHERE parent_id IS ?", (parent_id,))}
            for i, fid in enumerate([f for f in ordered_ids if f in kids]):
                self.db.execute("UPDATE source_folders SET pos=? WHERE id=?", (i, fid))

    def delete_folder(self, folder_id: int) -> None:
        """Delete a folder, reparenting its child folders and member tables
        to its own parent (root when it was top-level). Never deletes a
        source — a folder is a label, not a container that owns evidence."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT parent_id FROM source_folders WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                raise KeyError(f"No folder {folder_id}")
            parent = row["parent_id"]
            self.db.execute(
                "UPDATE source_folders SET parent_id=? WHERE parent_id=?", (parent, folder_id))
            if parent is None:
                # tables land at the root — the root is "no map row" at all
                self.db.execute(
                    "DELETE FROM source_folder_map WHERE folder_id=?", (folder_id,))
            else:
                self.db.execute(
                    "UPDATE source_folder_map SET folder_id=? WHERE folder_id=?",
                    (parent, folder_id))
            self.db.execute("DELETE FROM source_folders WHERE id=?", (folder_id,))

    def set_source_folder(self, source_id: int, folder_id: int | None, pos: int | None = None) -> None:
        """Put a table in a folder (or, with folder_id=None, back at the
        root — which is simply the absence of a map row)."""
        with self.lock, self.db:
            if folder_id is None:
                self.db.execute("DELETE FROM source_folder_map WHERE source_id=?", (source_id,))
                return
            if not self._folder_exists(folder_id):
                raise ValueError("That folder no longer exists")
            if pos is None:
                pos = (self.db.execute(
                    "SELECT COALESCE(MAX(pos), -1) + 1 FROM source_folder_map WHERE folder_id=?",
                    (folder_id,)).fetchone()[0])
            self.db.execute(
                "INSERT INTO source_folder_map(source_id, folder_id, pos) VALUES (?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET folder_id=excluded.folder_id, pos=excluded.pos",
                (source_id, folder_id, pos))

    def ensure_folder_path(self, path: str, parent_id: int | None = None) -> int | None:
        """Find-or-create a nested folder chain from a posix path
        ("RegistryHives/Users"), returning the leaf folder's id. Empty path
        (a file at the scan root) returns None — the caller leaves the
        source ungrouped. Reused across a directory import so a tree of
        files lands in one shared folder structure, not a folder per file."""
        parts = [p.strip() for p in (path or "").replace("\\", "/").split("/")]
        parts = [p for p in parts if p and p not in (".", "..")]
        if not parts:
            return parent_id
        cur = parent_id
        # One transaction for the whole chain: two directory-import jobs
        # (MAX_CONCURRENT_INGESTS=2) that share a folder would otherwise both
        # miss the find and both insert a duplicate.
        with self.lock, self.db:
            for part in parts:
                row = self.db.execute(
                    "SELECT id FROM source_folders WHERE parent_id IS ? AND name=? COLLATE NOCASE",
                    (cur, part)).fetchone()
                if row:
                    cur = row["id"]
                else:
                    pos = (self.db.execute(
                        "SELECT COALESCE(MAX(pos), -1) + 1 FROM source_folders WHERE parent_id IS ?",
                        (cur,)).fetchone()[0])
                    cur = self.db.execute(
                        "INSERT INTO source_folders(name, parent_id, pos) VALUES (?,?,?)",
                        (part, cur, pos)).lastrowid
        return cur

    def list_sources(self) -> list[dict]:
        """Bulk per-source annotation (is_open / tagged_row_count /
        note_count) in one locked pass — a per-source query here would be an
        N+1 against row_tags/row_notes/open_tabs on every case with several
        tables open, which is exactly the "8+ tables" case this exists to
        support."""
        with self._reader() as ro:
            rows = ro.execute("SELECT * FROM sources ORDER BY id").fetchall()
            open_ids = {r[0] for r in ro.execute("SELECT source_id FROM open_tabs")}
            tagged = dict(ro.execute("SELECT source_id, COUNT(DISTINCT rid) FROM row_tags GROUP BY source_id"))
            notes = dict(ro.execute("SELECT source_id, COUNT(*) FROM row_notes GROUP BY source_id"))
            derived = ro.execute(
                "SELECT * FROM derived_columns ORDER BY source_id, id"
            ).fetchall()
            folder = {r[0]: (r[1], r[2]) for r in ro.execute(
                "SELECT source_id, folder_id, pos FROM source_folder_map")}
        by_src: dict[int, list] = {}
        for r in derived:
            by_src.setdefault(r["source_id"], []).append(r)
        out = []
        for r in rows:
            d = self._src_dict(r)
            for dr in by_src.get(d["id"], []):
                d["columns"].append(self._derived_col_entry(dr))
                d["has_derived"] = True
            d["is_open"] = d["id"] in open_ids
            d["folder_id"], d["folder_pos"] = folder.get(d["id"], (None, 0))
            d["tagged_row_count"] = tagged.get(d["id"], 0)
            d["note_count"] = notes.get(d["id"], 0)
            d["fts_building"] = self._is_fts_building(d["id"])
            out.append(d)
        return out

    def _tab_meta(self, source_id: int) -> dict:
        """is_open for a single signed source_id (real or merge). Not
        tagged_row_count/note_count for merges — row_tags/row_notes are
        never keyed by a negative id, so callers building a merge's dict
        must sum its members' counts instead (see _merge_source_dict)."""
        with self._reader() as ro:
            is_open = ro.execute("SELECT 1 FROM open_tabs WHERE source_id=?", (source_id,)).fetchone() is not None
        return {"is_open": is_open}

    def set_tab_open(self, source_id: int, open_: bool) -> None:
        with self.lock, self.db:
            if open_:
                self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (source_id,))
            else:
                self.db.execute("DELETE FROM open_tabs WHERE source_id=?", (source_id,))

    def open_tabs(self, source_ids: Sequence[int]) -> int:
        """Open a tab for each of these sources (or merges, by negative id)
        in one transaction, and say how many were not already open.

        The bulk counterpart of close_all_tabs, and for the same reason:
        the sidebar's "open all" on a directory import of thirty files was
        thirty round trips, each taking the writer lock in turn, with the
        source list refetched at the end anyway. Which tables belong in the
        list is the caller's decision — "every table" and "every table with
        tagged rows" differ over merges, whose tags live on their members
        — so this validates the ids and opens exactly them."""
        ids = [int(i) for i in source_ids]
        if not ids:
            return 0
        with self.lock, self.db:
            known = {r[0] for r in self.db.execute("SELECT id FROM sources")}
            known |= {-r[0] for r in self.db.execute("SELECT id FROM merges")}
            missing = [i for i in ids if i not in known]
            if missing:
                raise KeyError(f"No such table: {missing[0]}")
            before = self.db.execute("SELECT COUNT(*) FROM open_tabs").fetchone()[0]
            self.db.executemany("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)",
                                [(i,) for i in ids])
            after = self.db.execute("SELECT COUNT(*) FROM open_tabs").fetchone()[0]
        return after - before

    def close_all_tabs(self) -> None:
        """Close every open tab in one shot — the tables stay in the case
        (and the folder tree); only their open flag clears. Cheaper and
        atomic vs one round trip per tab from the client."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM open_tabs")

    def set_source_nickname(self, source_id: int, nickname: str | None) -> dict:
        """A display name the analyst chooses ("DC01 security log") over the
        imported file's own name ("20240611_EvtxECmd_Output.csv"). `name`
        is deliberately left untouched — it's the file's identity (session
        matching, the record of what was imported), where a nickname is
        presentation. Empty/whitespace clears it. A merge has no file
        behind it, so its name *is* its display name — nicknaming one is
        just a rename of merges.name."""
        nickname = (nickname or "").strip() or None
        if nickname and len(nickname) > 200:
            raise ValueError("That nickname is too long")
        if source_id < 0:
            if not nickname:
                raise ValueError("A merge needs a name — give it one rather than clearing it")
            # Renaming is the second door into merges.name; the uniqueness
            # rule means nothing if it only guards the first.
            if self._merge_name_taken(nickname, exclude_id=-source_id):
                raise ValueError(f'A merge named "{nickname}" already exists')
            with self.lock, self.db:
                cur = self.db.execute("UPDATE merges SET name=? WHERE id=?", (nickname, -source_id))
                if cur.rowcount == 0:
                    raise KeyError(f"No merge {-source_id}")
        else:
            with self.lock, self.db:
                cur = self.db.execute("UPDATE sources SET nickname=? WHERE id=?", (nickname, source_id))
                if cur.rowcount == 0:
                    raise KeyError(f"No table {source_id}")
        return self.get_source(source_id)

    def set_source_path(self, source_id: int, path: str | None) -> None:
        """Where a table says it was imported from, when that is not the
        file it was read from: an upload is parsed (and hashed) from a
        spool deleted the moment the import returns, so the name the
        browser sent is what gets recorded, not a temp path that points at
        nothing — or, worse, at a leftover copy of the evidence."""
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET path=? WHERE id=?", (path, source_id))

    def get_source(self, source_id: int) -> dict:
        """A merge is addressed as a negative source_id (merges.id=m ->
        source_id=-m) so it can flow through every existing source_id: int
        endpoint unchanged. Real sources.id is always >= 1, so this can't
        collide."""
        if source_id < 0:
            return self._merge_source_dict(-source_id)
        with self._reader() as ro:
            row = ro.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise KeyError(f"No table {source_id}")
            tagged = ro.execute("SELECT COUNT(DISTINCT rid) FROM row_tags WHERE source_id=?", (source_id,)).fetchone()[0]
            note_count = ro.execute("SELECT COUNT(*) FROM row_notes WHERE source_id=?", (source_id,)).fetchone()[0]
            derived = ro.execute(
                "SELECT * FROM derived_columns WHERE source_id=? ORDER BY id", (source_id,)
            ).fetchall()
        d = self._src_dict(row)
        for dr in derived:
            d["columns"].append(self._derived_col_entry(dr))
            d["has_derived"] = True
        d.update(self._tab_meta(source_id))
        d["tagged_row_count"] = tagged
        d["note_count"] = note_count
        d["fts_building"] = self._is_fts_building(source_id)
        return d

    @staticmethod
    def _src_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["columns"] = json.loads(d["columns"])
        # origin_meta is JSON text in the row; every consumer (the UI's
        # tab titles, copy_sources_to's remap) wants the object. Tolerant
        # of a row from a case whose migration somehow left it unparseable —
        # provenance is a label, never worth failing a source listing over.
        for key in ("origin_meta", "ingest"):
            raw = d.get(key)
            try:
                d[key] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                d[key] = None
        d.setdefault("origin", None)
        return d

    @staticmethod
    def _derived_col_entry(r: sqlite3.Row) -> dict:
        """A derived_columns row -> the column-list entry merged into
        src["columns"]. Carries everything the UI needs to mark, manage and
        type the column; the `derived` flag is what the handful of
        base-only sites (_base_cols) filter on."""
        op = timeparse.OPERATIONS.get(r["op_id"]) or {}
        # A text-valued op's column is typed by what it holds, as an
        # imported column is: sizes or counts pulled out with a regex are
        # a number column, and sort, compare and group as numbers. The op
        # can't say so up front — the same regex capture is a host name
        # in one column and a byte count in the next.
        typ = op.get("value_type", "datetime")
        if typ == "text" and r["inferred_type"] == "number":
            typ = "number"
        return {
            "name": r["name"],
            "type": typ,
            "inferred_type": r["inferred_type"],
            "derived": True,
            "derived_id": r["id"],
            "derived_from": r["input_column"],
            "derived_op": r["op_id"],
            "derived_kind": op.get("derived_kind", "datetime"),
            "derived_status": r["status"],
            "parse_failures": r["parse_failures"],
            # The params come along so the UI can show what the column is
            # defined by without a second round trip — for an extracted
            # column that's the field path, which is the whole definition.
            "derived_params": json.loads(r["params"] or "{}"),
        }

    def _source_lite(self, source_id: int) -> dict:
        """Cheap source metadata for internal hot paths: just the sources
        row (columns parsed), or a merge synthesized from its members'
        rows. None of get_source's per-call annotations — tagged_row_count
        is a COUNT(DISTINCT rid) over row_tags (~12ms on a heavily tagged
        source), note_count/is_open are two more locked queries — and
        fetch_rows used to pay all of that at least twice per page, on
        every scroll. Anything user-facing that actually shows those
        counts keeps using get_source; everything that only needs
        table_name/columns/row_count/has_fts to run a query uses this."""
        with self._reader() as ro:
            return self._source_lite_on(ro, source_id)

    def _source_lite_on(self, conn: sqlite3.Connection, source_id: int) -> dict:
        """The query logic behind _source_lite, parameterised on the
        connection: writer-side callers hold self.lock and pass self.db,
        _reader() paths pass their checked-out read-only connection and
        never touch the lock at all. A merge (negative id) synthesizes the
        same dict _merge_source_dict builds minus the per-member
        tag/note/tab counts, with the same live-members contract (a dropped
        member is a KeyError, not a stale merge)."""
        if source_id < 0:
            merge_id = -source_id
            row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
            if not row:
                raise KeyError(f"No merge {merge_id}")
            member_ids = json.loads(row["source_ids"])
            members = [self._source_lite_on(conn, sid) for sid in member_ids]
            return {
                "id": source_id,
                "name": row["name"],
                "path": None,
                "table_name": None,
                "row_count": sum(m["row_count"] for m in members),
                "columns": self._merge_columns(row, members),
                "file_hash": None,
                "imported_at": row["created_at"],
                "has_fts": 1 if all(m["has_fts"] for m in members) else 0,
                "is_merge": True,
                "member_source_ids": member_ids,
                "col_mode": row["col_mode"],
                "source_column": bool(row["source_column"]),
            }
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise KeyError(f"No table {source_id}")
        d = self._src_dict(row)
        for dr in conn.execute(
            "SELECT * FROM derived_columns WHERE source_id=? ORDER BY id", (source_id,)
        ):
            d["columns"].append(self._derived_col_entry(dr))
            d["has_derived"] = True
        return d

    def _merge_row(self, merge_id: int) -> sqlite3.Row:
        with self._reader() as ro:
            row = ro.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
        if not row:
            raise KeyError(f"No merge {merge_id}")
        return row

    def _merge_source_dict(self, merge_id: int) -> dict:
        """Synthesizes a get_source()-shaped dict for a merge, computed live
        from its members' CURRENT columns/row_count/has_fts each time it's
        opened — a member dropped after the merge was created surfaces as a
        clear KeyError here rather than a silently stale/broken merge."""
        row = self._merge_row(merge_id)
        member_ids = json.loads(row["source_ids"])
        members = [self.get_source(sid) for sid in member_ids]
        return {
            "id": -merge_id,
            "name": row["name"],
            "path": None,
            "table_name": None,
            "row_count": sum(m["row_count"] for m in members),
            "columns": self._merge_columns(row, members),
            "col_mode": row["col_mode"],
            "source_column": bool(row["source_column"]),
            # Per member, which merge columns it lacks — what the builder's
            # Edit view and the grid's "only in some tables" markers read.
            "members": [
                {"id": m["id"], "label": m.get("nickname") or m["name"], "name": m["name"],
                 "row_count": m["row_count"],
                 "missing": self._member_missing_columns(row, members, m)}
                for m in members
            ],
            "file_hash": None,
            "imported_at": row["created_at"],
            "has_fts": 1 if all(m["has_fts"] for m in members) else 0,
            "fts_building": any(m["fts_building"] for m in members),
            "is_merge": True,
            "member_source_ids": member_ids,
            "is_open": self._tab_meta(-merge_id)["is_open"],
            "folder_id": self._source_folder(-merge_id)[0],
            "folder_pos": self._source_folder(-merge_id)[1],
            "tagged_row_count": sum(m["tagged_row_count"] for m in members),
            "note_count": sum(m["note_count"] for m in members),
        }

    @staticmethod
    def _merge_columns(row: sqlite3.Row | dict, members: list[dict]) -> list[dict]:
        """A merge's column list, computed live from its members' current
        columns. Base columns are matched by _col_key across members:
        members[0]'s in its order, then each column a later member adds, in
        that member's order. The first member to have a column decides its
        spelling and type — for same-shaped members (every merge from
        before mismatched ones) that is exactly members[0]'s list, as it
        always was. col_mode 'shared' keeps only the columns every member
        has; a member lacking an exposed column reads it as NULL (see
        _member_projection).

        "The same column" ignores ASCII case only, as SQLite does when it
        reads a quoted name: "Größe" and "GRÖßE" are two columns, each empty
        on the member that lacks it, never one member's header read as the
        other's data.

        Derived columns keep their older, stricter rule: exposed only when
        present on ALL members under the same name, each read from its own
        drv_ sidecar. The source column, when on, comes first."""
        seen: dict[str, dict] = {}
        counts: dict[str, int] = {}
        for m in members:
            for c in m["columns"]:
                if c.get("derived"):
                    continue
                k = _col_key(c["name"])
                if k not in seen:
                    seen[k] = c
                counts[k] = counts.get(k, 0) + 1
        keys = list(seen)
        if row["col_mode"] == "shared":
            keys = [k for k in keys if counts[k] == len(members)]
        cols = [seen[k] for k in keys]
        taken = set(keys)
        for c in members[0]["columns"]:
            if not c.get("derived") or _col_key(c["name"]) in taken:
                continue
            twins = [next((mc for mc in m["columns"] if mc.get("derived")
                           and _col_key(mc["name"]) == _col_key(c["name"])), None)
                     for m in members[1:]]
            if any(t is None for t in twins):
                continue
            # One column over every member, so it says what all of them
            # hold: still building if any member is (a re-derive on the
            # merge runs a job per member), and the failures summed — the
            # "Show N unparsed rows" filter it offers matches rows in every
            # member. The id stays the first member's; the remove and
            # re-derive routes take the merge's id beside it and act on
            # every member's definition (_derived_scope).
            every = [c] + twins
            failures = [x.get("parse_failures") for x in every]
            # Typed by every member's values, not the first member's: a
            # number on the merge only when no member holds text. A
            # member with nothing in the column (a host whose log never
            # has the field) says nothing either way, or one quiet host
            # would put the whole merge back to sorting as text.
            held = {x.get("inferred_type") for x in every} - {None, "empty"}
            typ = c["type"]
            if held and all(timeparse.OPERATIONS.get(x.get("derived_op"), {}).get("value_type") == "text"
                            for x in every):
                typ = "number" if held == {"number"} else "text"
            cols.append({**c,
                         "type": typ,
                         "derived_status": max((x.get("derived_status") for x in every),
                                               key=lambda s: _DERIVED_STATUS_RANK.get(s, 4)),
                         "parse_failures": None if None in failures else sum(failures)})
        if row["source_column"]:
            name = MERGE_SOURCE_COLUMN
            n = 2
            while any(_col_key(c["name"]) == _col_key(name) for c in cols):
                name = f"{MERGE_SOURCE_COLUMN} {n}"
                n += 1
            cols.insert(0, {"name": name, "type": "text", "source_column": True})
        return cols

    def _member_missing_columns(self, row: sqlite3.Row, members: list[dict], member: dict) -> list[str]:
        return [c["name"] for c in self._merge_columns(row, members)
                if not c.get("source_column") and _member_column(member, c) is None]

    @staticmethod
    def _member_projection(merge_src: dict, msrc: dict) -> list[str] | None:
        """How one member of a merge must be read so that it exposes the
        merge's columns under the merge's spelling: a SELECT list over the
        member's own FROM, or None when the member's table already does
        (every merge column present under a name SQLite resolves to it) —
        which keeps same-shaped merges on exactly the SQL they always ran.

        A column the member lacks is NULL; one spelled differently beyond
        what SQLite's ASCII-only case folding bridges is aliased; the source
        column is the member's label as a constant. The member's own
        columns the merge doesn't expose (col_mode 'shared') are kept too,
        so the search blob — always the member's own base columns — still
        resolves inside the projection.

        A member's derived column is not its copy of a file column another
        member has under the same name (_member_column): that member lacks
        the merge's column like any other, and reads it as NULL."""
        exprs: list[str] = []
        plain = True
        used: set[str] = set()
        for c in merge_src["columns"]:
            name = c["name"]
            if c.get("source_column"):
                exprs.append(f"{_sql_text_literal(msrc.get('nickname') or msrc['name'])} AS {q(name)}")
                plain = False
                continue
            actual = _member_column(msrc, c)
            if actual is None:
                exprs.append(f"NULL AS {q(name)}")
                plain = False
            elif actual == name:
                exprs.append(q(name))
            else:
                exprs.append(f"{q(actual)} AS {q(name)}")
                if not _sqlite_same_ident(actual, name):
                    plain = False
            used.add(_col_key(name))
        if plain:
            return None
        for c in msrc["columns"]:
            if not c.get("derived") and _col_key(c["name"]) not in used:
                exprs.append(q(c["name"]))
        return exprs

    def _member_read_src(self, merge_src: dict, msrc: dict) -> dict:
        """The src dict a merge member's own WHERE (filters, search, tags,
        timeframe) compiles against: the member's own columns, renamed to
        the merge's spelling, plus has_fts/id for its own FTS index. Every
        name in it resolves inside _member_from's projection."""
        # merge_id: raw filter fragments are validated once against the
        # merge (identifiers against its columns, a dry run per member's
        # projection), not per member against that member's own columns —
        # where a column only some members have would be "Unknown".
        if self._member_projection(merge_src, msrc) is None:
            return {**msrc, "merge_id": merge_src["id"]}
        rename = {_col_key(c["name"]): c for c in merge_src["columns"] if not c.get("source_column")}
        columns = []
        for c in msrc["columns"]:
            mc = rename.get(_col_key(c["name"]))
            if mc is None:
                columns.append(c)
            elif _member_column(msrc, mc) is not None:
                columns.append({**c, "name": mc["name"]})
            # Otherwise a derived column under the name of the merge's file
            # column, which is not the member's copy of it (_member_column):
            # left in, the timeframe would take the member as having that
            # column and read the projection's NULL.
        return {**msrc, "merge_id": merge_src["id"], "columns": columns}

    def _member_ref_src(self, merge_src: dict, msrc: dict) -> dict:
        """The src dict to resolve a merge's column references against for
        one member, in the query shapes that join a member's table by
        alias (`s."col"`, derived values via `d."col"`). A projected member
        exposes every column — derived ones included — as a plain column of
        the projection, so its references all go through the table alias
        and no separate sidecar join is chained (_member_derived_join on
        this src emits nothing)."""
        if self._member_projection(merge_src, msrc) is None:
            return merge_src
        return {**merge_src, "columns": [{k: v for k, v in c.items() if k != "derived"}
                                         for c in merge_src["columns"]]}

    def _member_join_target(self, merge_src: dict, m: dict) -> str:
        """The table text a view-join shape puts after `JOIN` for one
        member: its table, or its projection (see _member_projection) when
        the member doesn't carry the merge's columns as-is."""
        if not merge_src.get("is_merge"):
            return q(m["table_name"])
        msrc = m.get("src") or self._source_lite(m["source_id"])
        exprs = self._member_projection(merge_src, msrc)
        if exprs is None:
            return q(m["table_name"])
        return f"(SELECT rid, {', '.join(exprs)} FROM {self._from_clause(msrc)})"

    def _member_rows_from(self, src: dict, member_src: dict) -> str:
        """FROM text for reading cells of one member's rows by rid (the
        page/export readers): the member's own FROM, or its projection when
        `src` is a merge the member doesn't carry the columns of as-is."""
        if src.get("is_merge") and self._member_projection(src, member_src) is not None:
            return self._member_join_target(src, {"source_id": member_src["id"], "src": member_src,
                                                  "table_name": member_src["table_name"]})
        return self._from_clause(member_src)

    def _ref_src_for(self, src: dict, m: dict) -> dict:
        """_member_ref_src for member `m` of `src`, or `src` itself when it
        isn't a merge."""
        if not src.get("is_merge"):
            return src
        return self._member_ref_src(src, m.get("src") or self._source_lite(m["source_id"]))

    def _resolve_members(self, source_id: int) -> list[dict]:
        """Real source -> single identity-mapped element. Merge -> one
        element per member, carrying the member's own src dict. Members no
        longer share the merge's columns, so nothing may write a merge
        column name straight into a member's table: read a member through
        _member_from / _member_join_target / merge_select_list, which pad
        and rename it (see _member_projection)."""
        with self._reader() as ro:
            return self._resolve_members_on(ro, source_id)

    def _resolve_members_on(self, conn: sqlite3.Connection, source_id: int) -> list[dict]:
        """_resolve_members parameterised on the connection, same split as
        _source_lite/_source_lite_on."""
        if source_id > 0:
            src = self._source_lite_on(conn, source_id)
            return [{"source_id": source_id, "table_name": src["table_name"]}]
        merge = self._source_lite_on(conn, source_id)
        out = []
        for sid in merge["member_source_ids"]:
            msrc = self._source_lite_on(conn, sid)
            out.append({"source_id": sid, "table_name": msrc["table_name"], "src": msrc})
        return out

    def _merge_name_taken(self, name: str, exclude_id: int | None = None) -> bool:
        """Case-insensitive: two merges named "EVTX" and "evtx" are one
        typo'd reference apart in every list they'd both appear in, which is
        the confusion a uniqueness rule exists to prevent. NOCASE is ASCII-
        only, matching how header sets are already keyed (str.lower)."""
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM merges WHERE name = ? COLLATE NOCASE AND id != ?",
                (name, exclude_id or 0),
            ).fetchone()
        return row is not None

    def _check_merge_members(self, source_ids: list[int], col_mode: str) -> None:
        """What a merge's member list must satisfy, for create and edit
        alike. Columns no longer have to match: a member lacking one of the
        merge's columns reads it as empty (see _member_projection). Only
        'shared' mode needs something in common, or it exposes nothing."""
        if col_mode not in MERGE_COLUMN_MODES:
            raise ValueError(f"Unknown column mode {col_mode!r}")
        ids = [int(i) for i in source_ids]
        if len(ids) < 2:
            raise ValueError("A merge needs at least 2 sources")
        if len(set(ids)) != len(ids):
            raise ValueError("A table can only be in a merge once")
        if any(i <= 0 for i in ids):
            raise ValueError("A merge can only include tables, not other merges")
        sources = [self.get_source(sid) for sid in ids]
        for s in sources:
            self._require_columns(s)
        if col_mode == "shared":
            # Base columns only: adding a derived column to one member must
            # not change what a merge of two same-format files can do.
            common = set.intersection(*({_col_key(c["name"]) for c in self._base_cols(s)} for s in sources))
            if not common:
                raise ValueError("These tables have no columns in common — keep every column instead")

    def create_merge(self, name: str, source_ids: list[int], col_mode: str = "all",
                     source_column: bool = False) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("A merge needs a name")
        if self._merge_name_taken(name):
            raise ValueError(f'A merge named "{name}" already exists')
        self._check_merge_members(source_ids, col_mode)
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO merges(name, source_ids, created_at, col_mode, source_column) VALUES (?,?,?,?,?)",
                (name, json.dumps([int(i) for i in source_ids]), time.strftime("%Y-%m-%dT%H:%M:%S"),
                 col_mode, 1 if source_column else 0),
            )
            merge_id = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (-merge_id,))
        return self.get_source(-merge_id)

    def update_merge(self, merge_id: int, name: str | None = None, source_ids: list[int] | None = None,
                     col_mode: str | None = None, source_column: bool | None = None) -> dict:
        """Edit a merge in place — its tables, column mode, source column
        or name. In place matters: everything keyed by the merge's negative
        id (its tab, layout, folder) stays with it, which
        deleting and recreating would lose. Tags and notes live on member
        rows and never depended on the merge at all.

        Live views of the merge are dropped: their rows were drawn from the
        old member list, and paging one now would mix the two."""
        row = self._merge_row(merge_id)
        new_name = row["name"] if name is None else (name or "").strip()
        if not new_name:
            raise ValueError("A merge needs a name")
        if new_name != row["name"] and self._merge_name_taken(new_name, exclude_id=merge_id):
            raise ValueError(f'A merge named "{new_name}" already exists')
        ids = json.loads(row["source_ids"]) if source_ids is None else [int(i) for i in source_ids]
        mode = row["col_mode"] if col_mode is None else col_mode
        src_col = bool(row["source_column"]) if source_column is None else bool(source_column)
        self._check_merge_members(ids, mode)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE merges SET name=?, source_ids=?, col_mode=?, source_column=? WHERE id=?",
                (new_name, json.dumps(ids), mode, 1 if src_col else 0, merge_id),
            )
            self._evict_root_views(-merge_id)
        self._maxlen_cache.pop(-merge_id, None)
        return self.get_source(-merge_id)

    def merge_preview(self, source_ids: list[int], col_mode: str = "all",
                      source_column: bool = False, sample_rows: int = 12) -> dict:
        """What create_merge would make of these tables, without making it:
        the builder's preview. The column list is _merge_columns itself, so
        the preview can't disagree with the merge it previews; each column
        also says which tables have it, under what name and type. A few
        rows are read from each table (the first ones, split evenly, at
        least one per table while they last) through merge_select_list, so
        empty cells show exactly where the merge would have them.

        'shared' with nothing in common previews as no columns rather than
        an error: the builder says why in words, and create_merge refuses."""
        if col_mode not in MERGE_COLUMN_MODES:
            raise ValueError(f"Unknown column mode {col_mode!r}")
        ids = list(dict.fromkeys(int(i) for i in source_ids))
        if any(i <= 0 for i in ids):
            raise ValueError("A merge can only include tables, not other merges")
        members = [self._source_lite(sid) for sid in ids]
        if not members:
            return {"columns": [], "members": [], "rows": []}
        cols = self._merge_columns({"col_mode": col_mode, "source_column": source_column}, members)
        own = [{_col_key(c["name"]): c for c in m["columns"]} for m in members]
        merge_src = {"id": 0, "is_merge": True, "columns": cols}
        per = max(1, min(4, sample_rows // len(members)))
        rows: list[dict] = []
        with self._reader() as ro:
            for m in members:
                if len(rows) >= sample_rows:
                    break
                frm = f"{q(m['table_name'])} s"
                if any(c.get("derived") for c in m["columns"]):
                    frm += f" LEFT JOIN {q(self._derived_table(m['id']))} d ON d.rid = s.rid"
                sql = (f"SELECT {self.merge_select_list(merge_src, m) if cols else 's.rid'} FROM {frm} "
                       f"ORDER BY s.rid LIMIT {min(per, sample_rows - len(rows))}")
                rows += [{"source_id": m["id"], "cells": list(r) if cols else []} for r in ro.execute(sql)]
        return {
            "columns": [{**c, "in": {} if c.get("source_column") else {
                str(m["id"]): {"name": o[_col_key(c["name"])]["name"], "type": o[_col_key(c["name"])]["type"]}
                for m, o in zip(members, own) if _col_key(c["name"]) in o}} for c in cols],
            "members": [{"id": m["id"], "label": m.get("nickname") or m["name"], "name": m["name"],
                         "row_count": m["row_count"]} for m in members],
            "rows": rows,
        }

    def list_merges(self) -> list[dict]:
        with self._reader() as ro:
            rows = ro.execute("SELECT * FROM merges ORDER BY id").fetchall()
        out = []
        for r in rows:
            try:
                out.append(self.get_source(-r["id"]))
            except KeyError as e:
                # a member was dropped since the merge was created — surface
                # it as a broken entry the UI can offer to delete, not a
                # silent omission from the list.
                out.append({
                    "id": -r["id"], "name": r["name"], "is_merge": True,
                    "error": str(e), "member_source_ids": json.loads(r["source_ids"]),
                })
        return out

    def delete_merge(self, merge_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM merges WHERE id=?", (merge_id,))
            # Everything keyed by the merge's NEGATIVE source id. Tags and
            # notes aren't here because a merge has none of its own — they
            # live on the member rows (invariant #9) and survive on purpose.
            for t in ("open_tabs", "layouts"):
                self.db.execute(f"DELETE FROM {t} WHERE source_id=?", (-merge_id,))
            # Its views too, for drop_source's reason: a window still
            # holding one would otherwise page and tag a merge nobody has.
            self._evict_views_of(-merge_id)

    def merges_using(self, source_id: int) -> list[dict]:
        """The merges this table is a member of, as {id, name}.

        `merges.source_ids` is a JSON array of real source ids, so it is a
        holder of source ids exactly like row_tags or layouts — but unlike
        those, drop_source does not clean it, and deliberately: a member
        that disappears surfaces as a KeyError when the merge is opened
        rather than as a merge that silently changed shape
        (_merge_source_dict). That is the right call for a merge whose
        member went missing; it is the wrong thing to let an analyst DO by
        accident, which is why the drop is refused instead.
        """
        if source_id < 0:
            return []
        out = []
        with self._reader() as ro:
            for r in ro.execute("SELECT id, name, source_ids FROM merges ORDER BY id"):
                try:
                    ids = json.loads(r["source_ids"] or "[]")
                except (TypeError, ValueError):
                    continue
                if source_id in ids:
                    out.append({"id": r["id"], "name": r["name"]})
        return out

    def drop_source(self, source_id: int) -> None:
        """Delete a table and every sidecar keyed by its id.

        Refuses a table that a merge is built on: the merge would be left
        naming a member that is not there, and it is the analyst who should
        decide what becomes of it. Deleting the merge first is one click,
        and it keeps the other tables. (Source ids used to be REUSED by the
        next import, which made this worse — the next file imported took
        the freed id and silently became part of a merge nobody added it
        to. Ids are AUTOINCREMENT now, and the refusal stands on its own.)

        The internal callers are all ingest cancels dropping a partial
        import created seconds earlier, which cannot be a member of
        anything; if one ever is, refusing is still the right answer.
        """
        used_by = self.merges_using(source_id)
        if used_by:
            names = ", ".join(f'"{m["name"]}"' for m in used_by)
            raise ValueError(
                f"This table is merged into {names}. Delete "
                + ("that merge" if len(used_by) == 1 else "those merges")
                + " first — a merge is only a view over its tables, so deleting it keeps them.")
        src = self.get_source(source_id)
        with self.lock, self.db:
            # Its views go first. A view id outlives the table in whatever
            # window built it, and paging or tagging through one after the
            # drop reads rows of a table that is gone — or, back when ids
            # were reused, of the next import. Evicted, it answers 409 and
            # the window rebuilds against what is actually there.
            self._evict_views_of(source_id)
            self.db.execute(f"DROP TABLE IF EXISTS {q(src['table_name'])}")
            self.db.execute(f"DROP TABLE IF EXISTS {q('fts_' + str(source_id))}")
            self.db.execute("DELETE FROM fts_progress WHERE source_id=?", (source_id,))
            self.db.execute(f"DROP VIEW IF EXISTS {q(src['table_name'] + '_doc')}")
            # The watchlist's two per-table sidecars too: a hit left keyed
            # by the id would be listed under a table that is not there,
            # and a scan record left behind would say it was scanned clean.
            for t in ("row_tags", "row_notes", "watchlist_hits", "watchlist_scans"):
                self.db.execute(f"DELETE FROM {t} WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM layouts WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM open_tabs WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            self.db.execute(f"DROP TABLE IF EXISTS {q(self._derived_table(source_id))}")
            self.db.execute("DELETE FROM derived_columns WHERE source_id=?", (source_id,))
            # The subset provenance map, in BOTH directions: this table's
            # own rows, and every subset that named it as the parent — a
            # map left behind would point a subset's rows at a table that
            # is not there.
            self.db.execute("DELETE FROM subset_rids WHERE source_id=? OR parent_source_id=?",
                            (source_id, source_id))
            # Undo entries with rows in it: replaying one would write tags
            # for rows that no longer exist.
            self._drop_undo_for_source(source_id)
        self._maxlen_cache.pop(source_id, None)

    def _evict_views_of(self, source_id: int) -> None:
        """Evict every view built over this (signed) source id — root and
        virtual, held or adopted, with their group sub-views. Caller holds
        self.lock inside a self.db transaction, as for
        _evict_view_and_children."""
        for vid in list(self._views):
            handle = self._views.get(vid)
            if handle and handle.get("source_id") == source_id:
                self._evict_view_and_children(vid)

    # -------------------------------------------------------- derived columns

    @staticmethod
    def _derived_table(source_id: int) -> str:
        return f"drv_{source_id}"

    @staticmethod
    def _base_cols(src: dict) -> list[dict]:
        """The source's own columns, as imported. Used by everything that
        must see the *evidence file's* shape rather than the analyst's
        additions: the FTS doc view and its LIKE-fallback twin (a derived
        value is computed from data that's already searchable), merge
        eligibility, and the session file's column list."""
        return [c for c in src["columns"] if not c.get("derived")]

    def _from_clause(self, src: dict, alias: str | None = None, uses: Iterable[str] | None = None) -> str:
        """FROM-clause text for reading a source's rows: the source table
        alone, or LEFT JOIN'd to its derived-value sidecar.

        `uses`, when a caller passes it, is every column the statement
        reads; one that reads no derived column gets the table alone. That
        is not something SQLite works out for itself in the aggregates that
        pass it: it drops an unused LEFT JOIN from a plain SELECT, but not
        once the SELECT is flattened into a GROUP BY or a count(*), so a
        Values list or a group count on a base column paid a sidecar lookup
        per row for a column it never read — Computer's values on a 2.1M-row
        EvtxECmd table took 14.3 s instead of 0.11 s with one derived column
        added. Naming a derived column the caller left out of `uses` is "no
        such column", an error, never a wrong answer.

        `USING(rid)` rather than `ON a.rid = b.rid` so an unqualified `rid`
        stays legal on both sides (every read path selects it that way).
        drv_<id> has rid as its INTEGER PRIMARY KEY, so the join matches at
        most one row and can never change the row count or their order —
        which is what keeps _fetch_virtual_root_rows' `pos = rid - 1`
        exact (invariant #2). A source with no derived columns compiles
        byte-identical SQL to before this feature existed."""
        t = q(src["table_name"])
        if alias:
            t = f"{t} {alias}"
        if not any(c.get("derived") for c in src["columns"]):
            return t
        if uses is not None and not any(self._is_derived(src, c) for c in uses):
            return t
        return f"{t} LEFT JOIN {q(self._derived_table(src['id']))} USING(rid)"

    def _col_ref(self, src: dict, column: str, alias: str | None = None,
                 derived_alias: str | None = None) -> str:
        """SQL reference to one of a source's columns. Base columns take the
        caller's table alias (`s."Time"`); a derived column lives in the
        joined sidecar instead, so it's referenced bare under _from_clause's
        USING(rid) shape, or by `derived_alias` in the chained-join shapes
        (group_summary's view join) that can't use USING. Unambiguous either
        way because add_derived_column refuses a name that collides with a
        base column, case-insensitively — how SQLite resolves quoted
        identifiers."""
        for c in src["columns"]:
            if c.get("derived") and c["name"].lower() == column.lower():
                return f"{derived_alias}.{q(column)}" if derived_alias else q(column)
        return f"{alias}.{q(column)}" if alias else q(column)

    def _is_derived(self, src: dict, column: str) -> bool:
        return any(c.get("derived") and c["name"].lower() == str(column).lower()
                   for c in src["columns"])

    def _member_from(self, src: dict, m: dict, alias: str | None = None,
                     uses: Iterable[str] | None = None) -> str:
        """FROM text for one member table of a (possibly merged) view. A
        plain source picks up its own sidecar join; a merge that exposes
        derived columns (present on every member) reads each member through
        that member's OWN sidecar — same USING(rid) shape, so bare column
        and rid references stay legal. `uses` as in _from_clause."""
        if not src.get("is_merge") and m["source_id"] == src["id"]:
            return self._from_clause(src, alias, uses)
        if src.get("is_merge"):
            msrc = m.get("src") or self._source_lite(m["source_id"])
            if self._member_projection(src, msrc) is not None:
                t = self._member_join_target(src, {**m, "src": msrc})
                return f"{t} {alias}" if alias else t
            if any(c.get("derived") for c in src["columns"]):
                return self._from_clause(msrc, alias, uses)
        t = q(m["table_name"])
        return f"{t} {alias}" if alias else t

    def _derived_join(self, src: dict, on_alias: str, alias: str = "d") -> str:
        """`LEFT JOIN drv_<id> d ON d.rid = <on_alias>.rid`, for the query
        shapes that already chain a JOIN ... ON (group_summary's view join)
        and so can't use _from_clause's USING(rid) form — an ON clause after
        a USING join would bind to the wrong join."""
        if src.get("is_merge") or not any(c.get("derived") for c in src["columns"]):
            return ""
        return f" LEFT JOIN {q(self._derived_table(src['id']))} {alias} ON {alias}.rid = {on_alias}.rid"

    def _member_derived_join(self, src: dict, m: dict, on_alias: str,
                             uses: Iterable[str] | None = None) -> str:
        """_derived_join's per-member counterpart for the view-join shapes:
        a merge's rows resolve derived values through each member's own
        sidecar. For a plain source it emits exactly what _derived_join
        would. `uses` as in _from_clause: nothing when no column it names
        is derived."""
        if not any(c.get("derived") for c in src["columns"]):
            return ""
        if uses is not None and not any(self._is_derived(src, c) for c in uses):
            return ""
        mid = m["source_id"]
        msrc = src if (not src.get("is_merge") and mid == src["id"]) else self._source_lite(mid)
        if not any(c.get("derived") for c in msrc["columns"]):
            return ""
        return f" LEFT JOIN {q(self._derived_table(mid))} d ON d.rid = {on_alias}.rid"

    @staticmethod
    def _derived_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        return d

    def list_derived_ops(self) -> list[dict]:
        """The parsing operations the UI offers, straight from the
        timeparse registry (id/label/description/param schema)."""
        return timeparse.list_ops()

    def list_derived_columns(self, source_id: int) -> list[dict]:
        if source_id < 0:
            # A merge's derived columns are the ones its column list exposes
            # (present on every member) — represented by the first member's
            # definitions, since that's the canonical column source.
            src = self._source_lite(source_id)
            exposed = {c["name"].lower() for c in src["columns"] if c.get("derived")}
            first = self._resolve_members(source_id)[0]["source_id"]
            return [d for d in self.list_derived_columns(first) if d["name"].lower() in exposed]
        with self._reader() as ro:
            rows = ro.execute(
                "SELECT * FROM derived_columns WHERE source_id=? ORDER BY id", (source_id,)
            ).fetchall()
        return [self._derived_dict(r) for r in rows]

    def get_derived_column(self, def_id: int) -> dict:
        with self._reader() as ro:
            row = ro.execute("SELECT * FROM derived_columns WHERE id=?", (def_id,)).fetchone()
        if not row:
            raise KeyError(f"No derived column {def_id}")
        return self._derived_dict(row)

    def _find_column(self, src: dict, name: str) -> dict | None:
        for c in src["columns"]:
            if c["name"].lower() == str(name).lower():
                return c
        return None

    def add_derived_column(self, source_id: int, name: str, input_column: str,
                           op_id: str, params: dict | None = None) -> dict:
        """Define a derived column and kick off its backfill job.

        The values are materialised into the drv_<source_id> sidecar rather
        than computed per query: they have to be sortable, filterable,
        groupable and exportable, all of which are server-side SQL over a
        column that either exists or doesn't. The source table itself is
        never touched (invariant #1).

        The definition and the job are one unit as far as the analyst is
        concerned: if the job cannot be started (OpCancelled, the case
        closing), the definition is rolled back and the refusal raised —
        the same cancel-drops-the-partial-column contract this job carries
        in `drop_on_cancel`."""
        if source_id < 0:
            # A merge has no table of its own — creating "on the merge"
            # means creating the same column on every member, after which
            # _source_lite's intersection rule exposes it on the merge.
            # All-or-nothing: validate against every member before touching
            # any, so a name collision on member 3 doesn't leave members
            # 1-2 with a column the merge never grows.
            members = self._resolve_members(source_id)
            for m in members:
                msrc = self._source_lite(m["source_id"])
                if self._find_column(msrc, (name or "").strip()):
                    raise ValueError(
                        f"Member table {msrc['name']!r} already has a column called {(name or '').strip()!r}")
                for col in timeparse.op_inputs(op_id, input_column, params):
                    if self._find_column(msrc, col) is None:
                        raise ValueError(f"Member table {msrc['name']!r} has no column {col!r}")
            def_ids: list[int] = []
            try:
                for m in members:
                    def_ids.append(self._define_derived_column(
                        m["source_id"], name, input_column, op_id, params))
            except Exception:
                # A check only a member's own definition makes (an input
                # still building there) — the members before it go too.
                self._rollback_derive_definitions(def_ids)
                raise
            definitions = [self.get_derived_column(d) for d in def_ids]
            job = self._start_merge_derive_job(definitions[0]["name"], [[d] for d in def_ids], members)
            return {"definition": definitions[0],
                    "member_definitions": definitions,
                    "job_id": job["job_id"],
                    "job_ids": [job["job_id"]]}
        def_id = self._define_derived_column(source_id, name, input_column, op_id, params)
        # Read back BEFORE the job starts: a backfill that fails at once
        # removes the column (_fail_derive_backfill), and on a busy machine
        # it can do that before this thread gets to the return below.
        definition = self.get_derived_column(def_id)
        try:
            job = self.start_ingest_job(
                "derive", "", name=definition["name"],
                options={"def_id": def_id, "drop_on_cancel": True,
                         "units_total": self._source_lite(source_id)["row_count"]},
            )
        except OpCancelled:
            # The definition is committed and the backfill was refused (the
            # case is closing). `drop_on_cancel` above is this path's own
            # promise that a cancelled create leaves no half-column behind,
            # and a job that never started is that same cancel one moment
            # earlier — so drop the definition and let the refusal out.
            # Left standing it survives the close as a column stuck
            # 'building' with nothing to finish it: "(building…)" in the
            # header on every reopen, save_view_as_source refusing the
            # whole table, and nothing able to chain off it.
            self._rollback_derive_definitions([def_id])
            raise
        return {"definition": definition, "job_id": job["job_id"]}

    def _start_merge_derive_job(self, name: str, parts: list[list[int]], members: list[dict]) -> dict:
        """The one backfill job for columns just created on a merge:
        `parts` is each member's definition ids, in member order. A job
        refused because the case is closing takes every member's
        definitions with it, as add_derived_column's own refusal does."""
        units = sum(self._source_lite(m["source_id"])["row_count"] for m in members)
        try:
            return self.start_ingest_job(
                "derive", "", name=name,
                options={"parts": [{"def_ids": ids} for ids in parts], "drop_on_cancel": True,
                         "units_total": units},
            )
        except OpCancelled:
            self._rollback_derive_definitions([d for ids in parts for d in ids])
            raise

    def _define_derived_column(self, source_id: int, name: str, input_column: str,
                               op_id: str, params: dict | None) -> int:
        """add_derived_column's checks and its definition row (status
        'building'), on one real table, without the job — so a merge can
        make every member's before starting the one job that fills them."""
        op = timeparse.OPERATIONS.get(op_id)
        if op is None:
            raise ValueError(f"Unknown operation: {op_id}")
        params = timeparse.validate_params(op_id, params)
        src = self._source_lite(source_id)

        name = (name or "").strip()
        if not name:
            raise ValueError("The new column needs a name")
        if len(name) > 200:
            raise ValueError("Column name is too long")
        if name.lower() in RESERVED_COLUMN_NAMES:
            raise ValueError(f"{name!r} is a reserved column name")
        if self._find_column(src, name):
            raise ValueError(f"This table already has a column called {name!r}")

        # Inputs: a parse operation reads one of the file's own columns; a
        # two-input operation (duration) can also read an already-built
        # derived datetime column, so deltas can chain off parsed values.
        def _check_input(col: str, label: str) -> dict:
            entry = self._find_column(src, col)
            if entry is None:
                raise ValueError(f"No column called {col!r} to use as the {label}")
            # A derived input is fine — chains are how a JSON field that
            # holds XML gets taken apart in two steps, and the backfill
            # already reads inputs through _col_ref/_from_clause, which
            # resolve the sidecar. It just has to be FINISHED: a
            # still-building parent has incomplete values, and jobs are
            # thread-per-job with no ordering to lean on.
            if entry.get("derived") and entry.get("derived_status") != "ready":
                raise ValueError(f"{col!r} is still building — wait for it to finish first")
            return entry

        _check_input(input_column, "input column")
        for col in timeparse.op_inputs(op_id, input_column, params)[1:]:
            _check_input(col, "other column")
        if op.get("check"):
            op["check"](self, params)  # ops that reference the case (lookup) validate NOW, not mid-backfill

        drv = self._derived_table(source_id)
        with self.lock, self.db:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {q(drv)} (rid INTEGER PRIMARY KEY)")
            existing = {r[1].lower() for r in self.db.execute(f"PRAGMA table_info({q(drv)})")}
            if name.lower() in existing:
                # An orphan physical column left behind by a remove on a
                # SQLite too old for DROP COLUMN (< 3.35). Definitions drive
                # visibility, so it was invisible; reuse it, blanked.
                self.db.execute(f"UPDATE {q(drv)} SET {q(name)}=NULL")
            else:
                self.db.execute(f"ALTER TABLE {q(drv)} ADD COLUMN {q(name)} TEXT")
            cur = self.db.execute(
                "INSERT INTO derived_columns(source_id, name, input_column, op_id, params, status, created_at) "
                "VALUES (?,?,?,?,?,'building',?)",
                (source_id, name, input_column, op_id, json.dumps(params),
                 time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            return cur.lastrowid

    STRUCT_SAMPLE = 200

    def detect_struct_paths(self, source_id: int, column: str) -> dict:
        """What's inside a column that holds JSON or XML documents.

        Samples the column, decides which of the two it is (or neither),
        and enumerates every field found with how many of the sampled rows
        carried it plus one example value. That coverage number is the
        point: it's what turns "here are 60 paths" into "here are the six
        worth making columns of", and it's why this samples rather than
        scanning — the answer only has to be good enough to tick
        checkboxes against, and a full scan of a 1.2M-row column to
        populate a picker would cost more than building the columns."""
        src = self._source_lite(source_id)
        if src.get("is_merge"):
            # A merge has no table of its own to sample (_sample_column
            # read `"None"` and died on it). The header menu never offers
            # Flatten on one; this is the same answer for every other way
            # of asking, the detail pane's "Flatten this document" included.
            raise ValueError("Flatten works on one table at a time — open a "
                             "member table of this merge to flatten its documents")
        if not self._find_column(src, column):
            raise KeyError(f"No column called {column!r}")
        values = self._sample_column(src, column, limit=self.STRUCT_SAMPLE)
        kind = structparse.sniff_kind(values)
        if kind is None:
            return {"kind": None, "paths": [], "sampled": len(values)}
        return {
            "kind": kind,
            "sampled": len(values),
            "paths": structparse.discover_paths(values, kind),
        }

    def add_derived_columns(self, source_id: int, specs: list[dict]) -> dict:
        """Define several derived columns over one source and backfill them
        in a single pass.

        This exists for flattening: picking eight fields out of a JSON
        column is one intent, and running it as eight `add_derived_column`
        calls would mean eight full scans of the source table, eight jobs
        in the panel, and eight chances to end up half-done. One job, one
        scan, N columns.

        Definitions are created up front and all-or-nothing — a name
        collision in the fifth spec fails before the first column exists,
        rather than leaving four behind for the analyst to clean up. That
        holds through the job start too: a backfill refused because the
        case is closing takes every definition in the batch with it."""
        if not specs:
            raise ValueError("Nothing to add")
        if source_id < 0:
            # Same fan-out contract as add_derived_column (invariant #9):
            # validate every spec against every member first, then create
            # the whole batch on each member — a collision anywhere fails
            # before anything exists anywhere.
            members = self._resolve_members(source_id)
            for m in members:
                msrc = self._source_lite(m["source_id"])
                for spec in specs:
                    nm = (spec.get("name") or "").strip()
                    if self._find_column(msrc, nm):
                        raise ValueError(
                            f"Member table {msrc['name']!r} already has a column called {nm!r}")
                    if self._find_column(msrc, spec.get("input_column")) is None:
                        raise ValueError(
                            f"Member table {msrc['name']!r} has no column {spec.get('input_column')!r}")
            parts: list[list[int]] = []
            try:
                for m in members:
                    parts.append(self._define_derived_columns(m["source_id"], specs)[0])
            except Exception:
                self._rollback_derive_definitions([d for ids in parts for d in ids])
                raise
            label = self._derive_batch_label(specs)
            job = self._start_merge_derive_job(label, parts, members)
            results = [{"definitions": [self.get_derived_column(d) for d in ids], "job_id": job["job_id"]}
                       for ids in parts]
            return {**results[0], "member_results": results, "job_ids": [job["job_id"]]}
        def_ids, label = self._define_derived_columns(source_id, specs)
        # Before the job, for add_derived_column's reason.
        definitions = [self.get_derived_column(d) for d in def_ids]
        try:
            job = self.start_ingest_job(
                "derive", "", name=label,
                options={"def_ids": def_ids, "drop_on_cancel": True,
                         "units_total": self._source_lite(source_id)["row_count"]},
            )
        except OpCancelled:
            # All-or-nothing all the way to the job: this batch is
            # created in one step and cancelled in one step (the backfill
            # drops every column it was filling), so a refused job takes
            # the whole batch with it rather than leaving eight columns
            # stuck 'building' — see add_derived_column.
            self._rollback_derive_definitions(def_ids)
            raise
        return {"definitions": definitions, "job_id": job["job_id"]}

    @staticmethod
    def _derive_batch_label(specs: list[dict]) -> str:
        return (specs[0].get("name") or "").strip() if len(specs) == 1 else f"{len(specs)} columns"

    def _define_derived_columns(self, source_id: int, specs: list[dict]) -> tuple[list[int], str]:
        """add_derived_columns' checks and definition rows on one real
        table, without the job: (the new definition ids, the job's
        label). See _define_derived_column."""
        src = self._source_lite(source_id)
        drv = self._derived_table(source_id)

        # Validated against each other as well as against the table: two
        # specs asking for the same name is the realistic mistake here
        # (two paths whose last component is "Name"), and _find_column
        # can't see a column that doesn't exist yet.
        taken = {c["name"].lower() for c in src["columns"]}
        prepared = []
        for spec in specs:
            name = (spec.get("name") or "").strip()
            op_id = spec.get("op_id")
            input_column = spec.get("input_column")
            op = timeparse.OPERATIONS.get(op_id)
            if op is None:
                raise ValueError(f"Unknown operation: {op_id}")
            if op["multi_input"]:
                raise ValueError(f"{op['label']!r} reads several columns and can't be added in a batch")
            if not name:
                raise ValueError("Every new column needs a name")
            if len(name) > 200:
                raise ValueError(f"Column name {name!r} is too long")
            if name.lower() in RESERVED_COLUMN_NAMES:
                raise ValueError(f"{name!r} is a reserved column name")
            if name.lower() in taken:
                raise ValueError(f"This table already has a column called {name!r}")
            entry = self._find_column(src, input_column)
            if entry is None:
                raise ValueError(f"No column called {input_column!r} to read")
            # Same chain policy as add_derived_column: a derived input is
            # fine (flattening a derived JSON column is the natural second
            # step of a chain) — it just has to be finished.
            if entry.get("derived") and entry.get("derived_status") != "ready":
                raise ValueError(f"{input_column!r} is still building — wait for it to finish first")
            taken.add(name.lower())
            prepared.append({
                "name": name, "input_column": input_column, "op_id": op_id,
                "params": timeparse.validate_params(op_id, spec.get("params")),
            })

        def_ids = []
        with self.lock, self.db:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {q(drv)} (rid INTEGER PRIMARY KEY)")
            existing = {r[1].lower() for r in self.db.execute(f"PRAGMA table_info({q(drv)})")}
            for pspec in prepared:
                name = pspec["name"]
                if name.lower() in existing:
                    # Orphan physical column from a remove on SQLite < 3.35
                    # (no DROP COLUMN) — same reuse-it-blanked path
                    # add_derived_column takes.
                    self.db.execute(f"UPDATE {q(drv)} SET {q(name)}=NULL")
                else:
                    self.db.execute(f"ALTER TABLE {q(drv)} ADD COLUMN {q(name)} TEXT")
                cur = self.db.execute(
                    "INSERT INTO derived_columns(source_id, name, input_column, op_id, params, status, created_at) "
                    "VALUES (?,?,?,?,?,'building',?)",
                    (source_id, name, pspec["input_column"], pspec["op_id"],
                     json.dumps(pspec["params"]), time.strftime("%Y-%m-%dT%H:%M:%S")),
                )
                def_ids.append(cur.lastrowid)
        return def_ids, self._derive_batch_label(specs)

    def _rollback_derive_definitions(self, def_ids: list[int]) -> None:
        """Undo the definition rows a create has just committed, for a
        backfill job that was refused rather than started (OpCancelled —
        the case is closing). The caller re-raises; this is what makes the
        refusal true of the case FILE as well, so nothing comes back from
        the reopen as a column stuck 'building' with no job to finish it.

        A rollback that fails is swallowed deliberately. The only way it
        can is by losing the race to close()'s `self.db.close()`, and the
        OpCancelled underneath is the answer the caller has to act on
        either way — a ProgrammingError raised over it would report a
        broken case where there is a closing one."""
        for def_id in reversed(list(def_ids)):
            with contextlib.suppress(Exception):
                self.remove_derived_column(def_id)

    def _restore_derive_definition(self, before: dict) -> None:
        """Put one definition back as `before` (a get_derived_column
        snapshot taken ahead of the UPDATE) found it — the re-derive half
        of _rollback_derive_definitions, where the column already existed
        and still holds the values it always had, so the row is restored
        rather than removed. Same reason for swallowing a failure."""
        with contextlib.suppress(Exception):
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE derived_columns SET params=?, status=?, parse_failures=? WHERE id=?",
                    (json.dumps(before["params"]), before["status"],
                     before["parse_failures"], before["id"]),
                )

    def _derived_scope(self, def_id: int, source_id: int | None = None) -> list[dict]:
        """The definitions one action on a derived column applies to.

        On a table that is `def_id` alone. On a merge (`source_id`
        negative) it is every member's definition of that column, matched
        by name the way _merge_columns matches them: creating on a merge
        made one definition per member, and the merge's column list
        carries only the FIRST member's id, so remove, re-derive and a
        changed field path sent with that id alone used to act on that
        member and leave the others as they were (invariant #9). Members
        that do not carry the column are left out, which is also how a
        column the merge does not expose (it is missing on some member)
        is removed from the ones it is on."""
        d = self.get_derived_column(def_id)
        if source_id is None or source_id == d["source_id"]:
            return [d]
        if source_id > 0:
            raise KeyError(f"No derived column {def_id} on table {source_id}")
        members = [m["source_id"] for m in self._resolve_members(source_id)]
        if d["source_id"] not in members:
            raise KeyError(f"Derived column {def_id} is not on a table of merge {-source_id}")
        key = _ascii_fold(d["name"])
        out = []
        with self._reader() as ro:
            for sid in members:
                for r in ro.execute("SELECT * FROM derived_columns WHERE source_id=? ORDER BY id", (sid,)):
                    if _ascii_fold(r["name"]) == key:
                        out.append(self._derived_dict(r))
                        break
        return out

    def _member_label(self, d: dict, scope: list[dict]) -> str:
        """" on table 'x'" when an action spans several members, so a
        refusal says which member it came from; nothing otherwise."""
        if len(scope) < 2:
            return ""
        return f" on table {self._source_lite(d['source_id'])['name']!r}"

    def remove_derived_column(self, def_id: int, source_id: int | None = None) -> None:
        """Remove a derived column — from every member that carries it when
        `source_id` is the merge it was removed from (_derived_scope).

        All-or-nothing on a merge, as creating on one is: every member is
        checked for a column computed from this one before any is
        removed, so a dependant on the second member refuses the whole
        removal instead of leaving the first without the column."""
        scope = self._derived_scope(def_id, source_id)
        for d in scope:
            with self.lock:
                others = self.db.execute(
                    "SELECT * FROM derived_columns WHERE source_id=? AND id<>?",
                    (d["source_id"], d["id"]),
                ).fetchall()
            for o in others:
                o_params = json.loads(o["params"] or "{}")
                reads = {c.lower() for c in timeparse.op_inputs(o["op_id"], o["input_column"], o_params)}
                if d["name"].lower() in reads:
                    raise ValueError(f"{o['name']!r}{self._member_label(d, scope)} is computed from "
                                     f"this column — remove that one first")
        for d in scope:
            self._drop_derived_definition(d)

    def _drop_derived_definition(self, d: dict) -> None:
        drv = self._derived_table(d["source_id"])
        with self.lock, self.db:
            self.db.execute("DELETE FROM derived_columns WHERE id=?", (d["id"],))
            try:
                self.db.execute(f"ALTER TABLE {q(drv)} DROP COLUMN {q(d['name'])}")
            except sqlite3.OperationalError:
                # SQLite < 3.35 has no DROP COLUMN. The definition is gone,
                # so the column is invisible everywhere; add_derived_column
                # reuses the orphan if the same name comes back.
                pass

    def rename_derived_column(self, def_id: int, name: str,
                              source_id: int | None = None) -> dict:
        """Give a derived column a new name — on every member that carries
        it when `source_id` is the merge it was renamed from
        (_derived_scope), as remove and re-derive do.

        The file's own columns are evidence and keep the names they came
        with; a derived column's name is the analyst's, so it is the one
        that can change. Everything in the case file that names it follows
        in the same transaction: the drv_<id> column itself, every derived
        column computed from it (its input, a `column`/`columns` param, or
        a lookup on any table reading it as the key or the value), and the
        saved layout of its table and of each merge over it, so it keeps
        its place, width, format and sort. What lives outside the case file
        is left alone — saved filters, default layouts and Timeline
        settings are keyed by header set and shared with every table, in
        every case, that has one.

        On a merge the new name is checked against every member, as adding
        a column there is: a member without the column that already has
        one of the new name would otherwise end up showing two columns as
        one. Refused while the column, or anything computed from it, is
        building, since a backfill reads and writes its columns by the
        names they had when it started."""
        new = (name or "").strip()
        if not new:
            raise ValueError("The column needs a name")
        if len(new) > 200:
            raise ValueError("Column name is too long")
        if new.lower() in RESERVED_COLUMN_NAMES:
            raise ValueError(f"{new!r} is a reserved column name")
        scope = self._derived_scope(def_id, source_id)
        ids = {d["id"] for d in scope}
        on_merge = source_id is not None and source_id < 0
        tables = ([m["source_id"] for m in self._resolve_members(source_id)] if on_merge
                  else [scope[0]["source_id"]])
        for sid in tables:
            src = self._source_lite(sid)
            taken = self._find_column(src, new)
            if taken and taken.get("derived_id") not in ids:
                where = f"Member table {src['name']!r}" if on_merge else "This table"
                raise ValueError(f"{where} already has a column called {new!r}")
        if all(d["name"] == new for d in scope):
            return {"definition": self.get_derived_column(def_id)}

        # Layouts name columns as the grid shows them, which on a merge is
        # the merge's own spelling — the members' names, matched the way
        # _derived_scope matched them. A merge's layout follows only when
        # every member showing the old name was renamed: renamed on one
        # table, a merge over it and another table that keeps the old name
        # still has a column of that name for its layout to describe.
        olds = {_ascii_fold(d["name"]) for d in scope}
        renamed_on = {d["source_id"] for d in scope}
        layout_ids = set(renamed_on)
        live_new: set[int] = set()
        for mid in {m["id"] for d in scope for m in self.merges_using(d["source_id"])}:
            try:
                members = [m["source_id"] for m in self._resolve_members(-mid)]
            except KeyError:
                continue   # a merge missing a member opens as an error, layout or not
            if all(sid in renamed_on or not any(_ascii_fold(c["name"]) in olds
                                                for c in self._source_lite(sid)["columns"])
                   for sid in members):
                layout_ids.add(-mid)
                if any(sid not in renamed_on and any(_ascii_fold(c["name"]) == _ascii_fold(new)
                                                     for c in self._source_lite(sid)["columns"])
                       for sid in members):
                    live_new.add(-mid)
        labels = {d["id"]: self._member_label(d, scope) for d in scope}
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Read under the lock, in the transaction that writes: a
                # column started (or made) from this one since the checks
                # above is either seen here or made after the rename.
                every = [self._derived_dict(r) for r in
                         self.db.execute("SELECT * FROM derived_columns ORDER BY id")]
                now = {r["id"]: r for r in every}
                rewrites: dict[int, dict] = {}
                for d in scope:
                    if now[d["id"]]["status"] == "building":
                        raise ValueError(f"{d['name']!r}{labels[d['id']]} is still building "
                                         f"— wait for it to finish first")
                    for r in every:
                        if r["id"] in ids:
                            continue
                        changed = self._renamed_refs(rewrites.get(r["id"], r), d["source_id"], d["name"], new)
                        if changed is None:
                            continue
                        if r["status"] == "building":
                            raise ValueError(f"{r['name']!r} is being computed from {d['name']!r} "
                                             f"— wait for it to finish first")
                        rewrites[r["id"]] = changed
                for d in scope:
                    if d["name"] != new:
                        self._rename_drv_column(self._derived_table(d["source_id"]), d["name"], new)
                        self.db.execute("UPDATE derived_columns SET name=? WHERE id=?", (new, d["id"]))
                for rid, r in rewrites.items():
                    self.db.execute("UPDATE derived_columns SET input_column=?, params=? WHERE id=?",
                                    (r["input_column"], json.dumps(r["params"]), rid))
                for sid in sorted(layout_ids):
                    row = self.db.execute("SELECT payload FROM layouts WHERE source_id=?", (sid,)).fetchone()
                    if not row:
                        continue
                    payload = json.loads(row[0])
                    if _rename_in_layout(payload, olds, new, sid in live_new):
                        self.db.execute("UPDATE layouts SET payload=? WHERE source_id=?",
                                        (json.dumps(payload), sid))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        out = {"definition": self.get_derived_column(def_id)}
        if len(scope) > 1:
            out["member_definitions"] = [self.get_derived_column(d["id"]) for d in scope]
        return out

    @staticmethod
    def _renamed_refs(reader: dict, source_id: int, old: str, new: str) -> dict | None:
        """`reader` (a derived_columns definition) with every reference it
        makes to column `old` of table `source_id` changed to `new`, or
        None when it makes none. Which params name columns comes from the
        op's own param schema, as op_inputs reads it: `column`/`columns`
        name the reader's own table's columns, and `lookup_column` names a
        column of the table its `lookup_source` param points at."""
        op = timeparse.OPERATIONS.get(reader["op_id"]) or {"params": []}
        key = old.lower()
        hit = False

        def swap(v):
            nonlocal hit
            if isinstance(v, str) and v.strip().lower() == key:
                hit = True
                return new
            return v

        input_column = reader["input_column"]
        params = dict(reader["params"] or {})
        if reader["source_id"] == source_id:
            input_column = swap(input_column)
            for spec in op["params"]:
                v = params.get(spec["name"])
                if spec.get("type") == "column":
                    params[spec["name"]] = swap(v)
                elif spec.get("type") == "columns" and v:
                    items = v.split(",") if isinstance(v, str) else list(v)
                    params[spec["name"]] = [swap(str(x).strip()) for x in items]
        looked_in = next((params.get(s["name"]) for s in op["params"] if s.get("type") == "lookup_source"), None)
        try:
            looked_in = int(looked_in)
        except (TypeError, ValueError):
            looked_in = None
        if looked_in == source_id:
            for spec in op["params"]:
                if spec.get("type") == "lookup_column":
                    params[spec["name"]] = swap(params.get(spec["name"]))
        if not hit:
            return None
        return {**reader, "input_column": input_column, "params": params}

    def _rename_drv_column(self, drv: str, old: str, new: str) -> None:
        """Rename one column of a drv_<id> sidecar, inside the caller's
        transaction (writer lock held).

        RENAME COLUMN is a schema edit, not a rewrite of the rows, so it
        costs the same on a 2M-row table as on ten. SQLite refuses it when
        anything else in the file's schema no longer parses (and before
        3.25 has no RENAME COLUMN at all); then the values are copied into
        a new column and the old one is left behind blanked — the same
        invisible orphan _drop_derived_definition leaves where DROP COLUMN
        is refused, since definitions, not the sidecar, decide what is
        shown. An orphan already holding the new name is dropped first, or
        reused when it cannot be."""
        phys = [r[1] for r in self.db.execute(f"PRAGMA table_info({q(drv)})")]
        same = _ascii_fold(old) == _ascii_fold(new)
        orphan = next((p for p in phys if not same and _ascii_fold(p) == _ascii_fold(new)), None)
        if orphan is not None:
            try:
                self.db.execute(f"ALTER TABLE {q(drv)} DROP COLUMN {q(orphan)}")
                orphan = None
            except sqlite3.OperationalError:
                pass
        if orphan is None:
            try:
                self.db.execute(f"ALTER TABLE {q(drv)} RENAME COLUMN {q(old)} TO {q(new)}")
                return
            except sqlite3.OperationalError as e:
                if same:
                    # Copying a column onto itself would blank it; a change
                    # of capitals alone has no fallback.
                    raise ValueError(f"Could not rename {old!r}: {e}") from e
                self.db.execute(f"ALTER TABLE {q(drv)} ADD COLUMN {q(new)} TEXT")
        self.db.execute(f"UPDATE {q(drv)} SET {q(new)}={q(old)}, {q(old)}=NULL")

    def dependent_derived_columns(self, source_id: int, names: list[str]) -> list[dict]:
        """Definitions on this source computed from any of `names` — the
        children a re-derive of those columns leaves stale."""
        lower = {n.lower() for n in names}
        out = []
        for d in self.list_derived_columns(source_id):
            reads = {c.lower() for c in timeparse.op_inputs(d["op_id"], d["input_column"], d.get("params"))}
            if reads & lower:
                out.append(d)
        return out

    def _cascade_dependent_derives(self, changed: list[tuple[int, list[str]]]) -> None:
        """Re-derive what was computed from the columns a derive job just
        wrote: `changed` holds (source_id, names) for each table it wrote.
        A merge's job wrote one per member, and the same child on every
        member is then recomputed by one job walking them in turn, for
        the reason the parent was (_backfill_derive_parts)."""
        groups: dict[str, list[dict]] = {}
        for source_id, names in changed:
            for d in self.dependent_derived_columns(source_id, names):
                key = _ascii_fold(d["name"]) if len(changed) > 1 else str(d["id"])
                groups.setdefault(key, []).append(dict(d, parents=names))
        for defs in groups.values():
            d, names = defs[0], defs[0]["parents"]
            try:
                with self.lock, self.db:
                    self.db.executemany(
                        "UPDATE derived_columns SET status='building', parse_failures=NULL WHERE id=?",
                        [(x["id"],) for x in defs],
                    )
                units = sum(self._source_lite(x["source_id"])["row_count"] for x in defs)
                which = ({"def_id": d["id"]} if len(defs) == 1
                         else {"parts": [{"def_ids": [x["id"]]} for x in defs]})
                self.start_ingest_job(
                    "derive", "", name=f"{d['name']} (recomputed — its input changed)",
                    options={**which, "drop_on_cancel": False, "units_total": units},
                )
            except Exception as e:  # noqa: BLE001 — recorded below, then on to the next child
                # One broken child shouldn't stop its siblings — but it must
                # not be left stuck 'building' with no job either: a
                # permanent 'building' blocks the whole chain below it.
                # 'partial' is what the column actually is. Its values are
                # real but were computed from the parent's data as it stood
                # BEFORE the re-derive, and 'ready' says "finished" about
                # them — columns.js renders 'partial' as "incomplete —
                # re-derive to finish", which is the one hint that survives
                # a reopen, and the same status a cancelled re-derive
                # leaves. The case closing arrives here too: this runs on a
                # worker close() is draining and start_ingest_job refuses
                # once `_closing` is set. That refusal is a failure like any
                # other, including being written down — every other derive
                # outcome reaches the log, and a cascade that silently did
                # not happen is exactly the quietly-partial result the log
                # exists for. This column's OWN children are left as they
                # are: re-deriving it cascades to them in turn, which is
                # the recovery the message asks for.
                wlog.record("warn", f"Derive not recomputed: {d['name']} still holds values from before "
                                    f"{', '.join(names)} changed — re-derive it "
                                    f"({type(e).__name__}: {e})")
                try:
                    with self.lock, self.db:
                        self.db.executemany(
                            "UPDATE derived_columns SET status='partial' WHERE id=? AND status='building'",
                            [(x["id"],) for x in defs],
                        )
                except Exception:  # noqa: BLE001 — a closing connection has nothing left to record on
                    pass
                continue

    def rederive_column(self, def_id: int, params: dict | None = None,
                        source_id: int | None = None) -> dict:
        """Recompute a derived column in place — the path for "I set the
        wrong syslog year" or "these were local time, not UTC".

        The new params are written before the job starts, so a job that is
        refused (OpCancelled, the case closing) restores the definition
        exactly as it was rather than leaving it 'building' under params
        nothing computed.

        On a merge (`source_id` negative) every member that carries the
        column is re-derived with the same params (_derived_scope), by one
        job that walks the members in turn — the shape creating on a merge
        has (_backfill_derive_parts). Cancelled part-way, the member it
        was on keeps its values marked 'partial', which the merge's column
        then reports (_merge_columns); the members it had not reached keep
        the column they had. All-or-nothing up to the job, as create is:
        every member's definition is checked against the new params
        before any is rewritten, so params one member cannot take refuse
        the whole re-derive instead of leaving the merge's one column
        computed two ways."""
        scope = self._derived_scope(def_id, source_id)
        if params is not None and len({d["op_id"] for d in scope}) > 1:
            ways = ", ".join(
                f"{self._source_lite(d['source_id'])['name']}: "
                f"{timeparse.OPERATIONS.get(d['op_id'], {}).get('label', d['op_id'])}" for d in scope)
            raise ValueError(f"{scope[0]['name']!r} is not computed the same way on every table in "
                             f"this merge ({ways}) — re-derive it on each table on its own")
        prepared = []
        for d in scope:
            new_params = timeparse.validate_params(d["op_id"], params if params is not None else d["params"])
            op = timeparse.OPERATIONS[d["op_id"]]
            if op.get("check"):
                # Same as add_derived_column: an op that references the case
                # (lookup) is refused HERE, with nothing changed, when what it
                # references has gone — not after the column was put in
                # 'building' and the job got as far as loading it.
                op["check"](self, new_params)
            prepared.append((d, new_params))
        with self.lock, self.db:
            self.db.executemany(
                "UPDATE derived_columns SET params=?, status='building', parse_failures=NULL WHERE id=?",
                [(json.dumps(new_params), d["id"]) for d, new_params in prepared],
            )
        # `previous` lets a backfill that fails before writing a value put
        # the definition back as it was (_fail_derive_backfill).
        if len(scope) == 1:
            which = {"def_id": scope[0]["id"], "previous": [scope[0]]}
        else:
            which = {"parts": [{"def_ids": [d["id"]], "previous": [d]} for d in scope]}
        units = sum(self._source_lite(d["source_id"])["row_count"] for d in scope)
        try:
            job = self.start_ingest_job(
                "derive", "", name=scope[0]["name"],
                options={**which, "drop_on_cancel": False, "units_total": units},
            )
        except OpCancelled:
            # Refused because the case is closing. The rows above now say
            # 'building' under the new params and no job will ever make
            # that true — the column would come back from the reopen
            # reading "(building…)" over the values it had all along. A
            # re-derive is not a delete (the cancelled-mid-backfill case
            # keeps the column and marks it 'partial'), so put the
            # definitions back exactly as they were: this call raised, so
            # nothing about it took effect.
            for d in scope:
                self._restore_derive_definition(d)
            raise
        definitions = [self.get_derived_column(d["id"]) for d in scope]
        cascades, seen = [], set()
        for d in scope:
            for c in self.dependent_derived_columns(d["source_id"], [d["name"]]):
                if _ascii_fold(c["name"]) not in seen:
                    seen.add(_ascii_fold(c["name"]))
                    cascades.append(c["name"])
        out = {"definition": definitions[0], "job_id": job["job_id"], "cascades_to": cascades}
        if len(scope) > 1:
            out.update(member_definitions=definitions, job_ids=[job["job_id"]])
        return out

    def backfill_derived_column(self, def_id: int, progress=None, cancel=None,
                                drop_on_cancel: bool = False) -> dict:
        """Compute every row's value for one derived column. The one-column
        case of backfill_derived_columns, kept as its own name because
        that's what the single-column create/re-derive paths mean."""
        out = self.backfill_derived_columns(
            [def_id], progress=progress, cancel=cancel, drop_on_cancel=drop_on_cancel,
        )
        return out["columns"][0] | {"rows": out["rows"]}

    def _backfill_derive_parts(self, job: dict, parts: list[dict], progress=None, cancel=None,
                               drop_on_cancel: bool = False) -> list[dict]:
        """A merge's derive in one job: each member's definitions (`parts`,
        in member order, each {"def_ids", "previous"?}) backfilled one
        member after another. Returns backfill_derived_columns' result for
        each member.

        Creating on a merge used to start a job per member, and those ran
        two at a time (MAX_CONCURRENT_INGESTS) — two CPU-bound passes
        fighting over the GIL and the writer lock. A merge of four
        250k-row tables took twice as long as the same four passes run one
        after another, which is also what the same rows take in one table;
        and the analyst watched the column fill in a table or two at a
        time, behind one job per table. One job is one row in the panel,
        one progress bar over the merge's rows and one Cancel.

        A cancel or a failure stops at the member it reached, which
        backfill_derived_columns settles as it always has. The members it
        had not reached are settled as a pass that wrote nothing
        (_fail_derive_backfill with wrote=False): a create removes them, a
        re-derive puts their definitions back as they were. A create also
        removes the members it had finished, since a create that does not
        finish is removed (drop_on_cancel) and here that is the merge's
        column: left on some members it is one the merge doesn't show
        (_merge_columns), and its name, taken on those members, would
        refuse the analyst's retry on the merge."""
        grand = (job.get("options") or {}).get("units_total") or 0
        done = 0
        out: list[dict] = []
        for i, part in enumerate(parts):
            try:
                name = self._source_lite(self.get_derived_column(part["def_ids"][0])["source_id"])
                name = name.get("nickname") or name["name"]
            except Exception:  # noqa: BLE001 — the label only; the backfill below says what is wrong
                name = None
            with self._ingest_jobs_lock:
                job["tables_total"] = len(parts)
                job["tables_done"] = i
                job["current_table"] = name

            def part_progress(rows: int, units: int, units_total: int, base: int = done) -> None:
                if progress:
                    progress(base + rows, base + units, max(grand, base + units_total))

            try:
                res = self.backfill_derived_columns(
                    part["def_ids"], progress=part_progress, cancel=cancel,
                    drop_on_cancel=drop_on_cancel, previous=part.get("previous"),
                )
            except Exception:   # IngestCancelled included
                self._settle_unrun_derive_parts(parts[i + 1:], drop=drop_on_cancel)
                if drop_on_cancel:
                    self._rollback_derive_definitions([d for p in parts[:i] for d in p["def_ids"]])
                raise
            out.append(res)
            done += res["rows"]
        with self._ingest_jobs_lock:
            job["tables_done"] = len(parts)
        return out

    def _settle_unrun_derive_parts(self, parts: list[dict], *, drop: bool) -> None:
        """The members a merge's derive job stopped before reaching: each
        is a backfill that wrote nothing. Best-effort, as
        _fail_derive_backfill is — the cancel or error being raised is the
        answer."""
        for part in parts:
            defs = []
            for def_id in part["def_ids"]:
                with contextlib.suppress(Exception):
                    defs.append(self.get_derived_column(def_id))
            if defs:
                with contextlib.suppress(Exception):
                    self._fail_derive_backfill(defs, wrote=False, drop=drop, previous=part.get("previous"))

    @staticmethod
    def _derive_job_result(out: list[dict]) -> list[dict]:
        """A finished derive job's `result`: one entry per column, with
        `rows` the length of the pass. A merge's job wrote each column on
        every member; it reports the merge's column — the rows of every
        member, and their failures summed, as _merge_columns counts them."""
        rows = sum(res["rows"] for res in out)
        merged: dict[str, dict] = {}
        for res in out:
            for c in res["columns"]:
                key = _ascii_fold(c["name"])
                if key not in merged:
                    merged[key] = dict(c, rows=rows)
                    continue
                m = merged[key]
                m["parse_failures"] = (None if m["parse_failures"] is None or c["parse_failures"] is None
                                       else m["parse_failures"] + c["parse_failures"])
        return list(merged.values())

    def backfill_derived_columns(self, def_ids: list[int], progress=None, cancel=None,
                                 drop_on_cancel: bool = False,
                                 previous: list[dict] | None = None) -> dict:
        """Compute every row's values for one or more derived columns of the
        same source, in a single pass.

        Batched exactly like ingest_csv/build_fts: read a BATCH-sized window
        on a pooled reader (committed data — the source table hasn't changed
        since ingest, invariant #1), parse in Python, then take the writer
        lock for just that batch's upsert (invariant #4). Rows are walked in
        rid order because the stateful operations need it — BSD syslog's
        year rollover is decided by comparing each line's month against the
        previous line's, so the order rows are parsed in is part of the
        answer, not an implementation detail. Each definition gets its own
        `state` dict for exactly that reason: two columns derived in the
        same pass must not share a rollover cursor.

        Several columns at once is what flattening a JSON blob into its
        fields needs (see add_derived_columns) — one scan and one progress
        bar instead of N of each. The SELECT reads each distinct input
        column once however many definitions want it.

        One value is never the column's problem: a parser that raises on a
        cell makes that cell NULL, which the failure count then includes
        like any other value that did not parse (see _backfill_value_errors
        for the record of it). Anything else that stops the pass — a
        lookup table gone since the column was made, a full disk — leaves
        every column in a state that says what it holds rather than
        'building' (see _fail_derive_backfill); `previous` is the
        definition as a re-derive found it, so a pass that fails before
        writing anything can put it back. Then it raises, and the job
        reports it."""
        if not def_ids:
            raise ValueError("Nothing to backfill")
        defs = [self.get_derived_column(d) for d in def_ids]
        source_id = defs[0]["source_id"]
        if any(d["source_id"] != source_id for d in defs):
            raise ValueError("All columns in one backfill must belong to the same table")
        rows_done, last_rid = 0, 0
        # Everything from here is inside the try, setup included: the
        # definitions are already 'building', so whatever the setup raises
        # (an op id the registry no longer has, say) must land like any
        # other failure rather than leave them so.
        try:
            src = self._source_lite(source_id)
            drv = self._derived_table(source_id)
            frm = self._from_clause(src)

            # One SELECT slot per distinct input column, shared by every
            # definition reading it.
            slots: dict[str, int] = {}

            def slot(col: str) -> int:
                key = col.lower()
                if key not in slots:
                    slots[key] = len(slots)
                return slots[key]

            plans = []
            for d in defs:
                op = timeparse.OPERATIONS[d["op_id"]]
                inputs = timeparse.op_inputs(d["op_id"], d["input_column"], d["params"])
                # state["inputs"]: the ordered column names behind the values
                # parse_multi receives — an op that needs them (row_json's keys)
                # reads them here rather than growing a second calling shape.
                # Multi-input ops only: a stateful single-input op (syslog's
                # year rollover) takes an empty dict as "fresh" and seeds it.
                state = {"inputs": list(inputs)} if op["multi_input"] else {}
                plans.append({"d": d, "op": op, "state": state, "slots": [slot(c) for c in inputs],
                              "raised": 0, "first_error": None})
            cols = list(slots.keys())
            # slot() keys case-insensitively but _col_ref needs the real name.
            real = {}
            for d in defs:
                for c in timeparse.op_inputs(d["op_id"], d["input_column"], d["params"]):
                    real[c.lower()] = c
            sel = ", ".join(self._col_ref(src, real[c]) for c in cols)

            names = [d["name"] for d in defs]
            assigns = ", ".join(f"{q(n)}=excluded.{q(n)}" for n in names)
            upsert = (f"INSERT INTO {q(drv)}(rid, {', '.join(q(n) for n in names)}) "
                      f"VALUES({','.join('?' * (len(names) + 1))}) "
                      f"ON CONFLICT(rid) DO UPDATE SET {assigns}")

            total = src["row_count"]
            for plan in plans:
                if plan["op"].get("prepare"):
                    # e.g. lookup loading its whole mapping once — per
                    # definition, before the scan, so the scan stays dict
                    # hits. Inside the try: a lookup table deleted since the
                    # column was made fails HERE, and has to land the way
                    # any other failure does.
                    plan["op"]["prepare"](self, plan["d"]["params"], plan["state"])
            while True:
                with self._reader() as ro:
                    rows = ro.execute(
                        f"SELECT rid, {sel} FROM {frm} WHERE rid > ? ORDER BY rid LIMIT {BATCH}",
                        (last_rid,),
                    ).fetchall()
                if not rows:
                    break
                vals = []
                for r in rows:
                    t = tuple(r)
                    out = [t[0]]
                    for plan in plans:
                        op = plan["op"]
                        # Per value, not per column: a parser that raises
                        # on one cell (an offset no clock has, a "²" where
                        # a tick count should be) used to fail the whole
                        # backfill — and, on a re-derive, leave the
                        # batches before it under the new params and the
                        # rest under the old. The cell is NULL instead,
                        # which _count_parse_failures counts with the rest.
                        try:
                            if op["multi_input"]:
                                v = op["parse_multi"]([t[1 + i] for i in plan["slots"]],
                                                      plan["d"]["params"], plan["state"])
                            else:
                                v = op["parse"](t[1 + plan["slots"][0]], plan["d"]["params"], plan["state"])
                        except Exception as e:  # noqa: BLE001 — one value, see above
                            v = None
                            plan["raised"] += 1
                            if plan["first_error"] is None:
                                plan["first_error"] = f"row {t[0]}: {type(e).__name__}: {e}"
                        out.append(v)
                    vals.append(tuple(out))
                last_rid = vals[-1][0]
                with self.lock, self.db:
                    if cancel and cancel():
                        raise IngestCancelled("cancelled during derive")
                    try:
                        self.db.executemany(upsert, vals)
                    except (UnicodeEncodeError, sqlite3.InterfaceError, sqlite3.ProgrammingError):
                        # A value SQLite cannot bind: a str holding a lone
                        # surrogate, or something that is not a str at all
                        # from a plugin's op. Same transaction, so the rows
                        # the first attempt got through are simply written
                        # again — the upsert is idempotent. A closed
                        # connection raises again here and goes on up.
                        self.db.executemany(upsert, [tuple(_storable_cell(c) for c in row)
                                                     for row in vals])
                rows_done += len(vals)
                if progress:
                    progress(rows_done, rows_done, total)
                time.sleep(0.02)  # same anti-starvation yield build_fts uses
        except IngestCancelled:
            if drop_on_cancel:
                # Mirror of cancel-drops-the-partial-source: the analyst
                # asked for these columns not to exist, and a half-filled one
                # looks exactly like a complete one in the grid.
                for d in defs:
                    self.remove_derived_column(d["id"])
            else:
                with self.lock, self.db:
                    self.db.executemany(
                        "UPDATE derived_columns SET status='partial', inferred_type=NULL WHERE id=?",
                        [(d["id"],) for d in defs],
                    )
                # Half this pass's values and half the last one's, so its
                # type is looked at again: now, or at the next open when
                # the cancel is the case closing (no scan to hold that up).
                if not self._closing:
                    for d in defs:
                        with contextlib.suppress(Exception):
                            inferred = self._infer_derived_type(d)
                            with self.lock, self.db:
                                self.db.execute(
                                    "UPDATE derived_columns SET inferred_type=? WHERE id=?",
                                    (inferred, d["id"]))
            raise
        except Exception:
            self._fail_derive_backfill(defs, wrote=rows_done > 0, drop=drop_on_cancel,
                                       previous=previous)
            raise

        self._backfill_value_errors(plans)
        out_cols = []
        for d in defs:
            # Every value is written by now, so this column is finished
            # whatever the count does: a count that fails (the case closing
            # under it) is an unknown number of failures, not a reason to
            # leave the column 'building'.
            try:
                failures = self._count_parse_failures(src, d)
            except Exception:  # noqa: BLE001 — see above
                failures = None
            # The same for its type: unknown (NULL) keeps the op's own until
            # the next open looks again (_type_unlooked_derived_columns).
            try:
                inferred = self._infer_derived_type(d)
            except Exception:  # noqa: BLE001 — see above
                inferred = None
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE derived_columns SET status='ready', parse_failures=?, inferred_type=? WHERE id=?",
                    (failures, inferred, d["id"]),
                )
            out_cols.append({"def_id": d["id"], "source_id": source_id, "name": d["name"],
                             "parse_failures": failures})
        return {"source_id": source_id, "rows": rows_done, "columns": out_cols}

    def _backfill_value_errors(self, plans: list[dict]) -> None:
        """Write down that a parser raised, once per column per pass. The
        cells are already NULL and counted as failures, so the analyst's
        number is right without this; the log is for whoever wonders WHY a
        value that looks parseable came out empty — it names the first
        row and the exception."""
        for plan in plans:
            if plan["raised"]:
                n = plan["raised"]
                wlog.record("warn", f"Derive {plan['d']['name']!r}: {plan['d']['op_id']} raised on "
                                    f"{n:,} value{'s' if n != 1 else ''}, left empty and counted as "
                                    f"unparsed (first: {plan['first_error']})")

    def _fail_derive_backfill(self, defs: list[dict], *, wrote: bool, drop: bool,
                              previous: list[dict] | None) -> None:
        """Where a backfill that stopped on an error (not a cancel) leaves
        its columns. The state being avoided is the one it used to leave:
        'building' with no job left to finish it, which survives every
        reopen, blocks anything chaining off the column and makes
        save_view_as_source refuse the table — and, on a re-derive, values
        computed under two different sets of params with nothing to say so.

        - A create (`drop`): removed, as a cancel removes it. The column
          never had values worth keeping, and the job's error says why it
          is not there.
        - A re-derive that failed before writing anything (the lookup
          table it reads is gone): the definition goes back to exactly
          what it was (`previous`). Its values were never touched, so it
          IS still that column. A cascade has no `previous` — its params
          did not change, its parent's data did — so it is marked
          'partial', which is what a cascade that could not start leaves
          (_cascade_dependent_derives).
        - A re-derive that wrote some batches first: 'error', and the
          values cleared. Half old params and half new is the one answer
          that cannot be read correctly, so none is left to read; the
          header says to re-derive. Status first, so that if the clearing
          fails too (the disk that filled up), the column still says it is
          not to be trusted. The columns computed FROM it held values from
          before and are now 'partial' for the same reason a cascade
          that could not start leaves them so.

        Every step is best-effort: the error being raised is the answer
        (the job records it, and the log line names it), and a second
        failure here — the case closing under it — must not replace it."""
        if drop:
            self._rollback_derive_definitions([d["id"] for d in defs])
            return
        before = {b["id"]: b for b in (previous or [])}
        if not wrote:
            for d in defs:
                if d["id"] in before:
                    self._restore_derive_definition(before[d["id"]])
                else:
                    with contextlib.suppress(Exception):
                        with self.lock, self.db:
                            self.db.execute(
                                "UPDATE derived_columns SET status='partial' WHERE id=? AND status='building'",
                                (d["id"],),
                            )
            return
        names = [d["name"] for d in defs]
        wlog.record("warn", f"Derive stopped part-way: {', '.join(names)} — the values it had "
                            f"written were cleared rather than left mixed with the old ones; "
                            f"re-derive to retry")
        with contextlib.suppress(Exception):
            with self.lock, self.db:
                self.db.executemany(
                    "UPDATE derived_columns SET status='error', parse_failures=NULL, inferred_type=NULL WHERE id=?",
                    [(d["id"],) for d in defs],
                )
        source_id = defs[0]["source_id"]
        drv = self._derived_table(source_id)
        cleared = ", ".join(f"{q(d['name'])}=NULL" for d in defs)
        with contextlib.suppress(Exception):
            last = 0
            while True:
                # By rid range, a BATCH at a time, for invariant #4's
                # reason: one UPDATE over a multi-million-row column would
                # hold the writer for as long as the backfill's whole pass.
                with self.lock, self.db:
                    hi = self.db.execute(
                        f"SELECT MAX(rid) FROM (SELECT rid FROM {q(drv)} WHERE rid > ? "
                        f"ORDER BY rid LIMIT {BATCH})", (last,),
                    ).fetchone()[0]
                    if hi is None:
                        break
                    self.db.execute(f"UPDATE {q(drv)} SET {cleared} WHERE rid > ? AND rid <= ?",
                                    (last, hi))
                last = hi
        with contextlib.suppress(Exception):
            stale = [c["id"] for c in self.dependent_derived_columns(source_id, names)]
            with self.lock, self.db:
                self.db.executemany(
                    "UPDATE derived_columns SET status='partial' WHERE id=? AND status='ready'",
                    [(i,) for i in stale],
                )

    def _count_parse_failures(self, src: dict, d: dict) -> int:
        """Rows whose input had something in it but produced no datetime —
        the number the analyst needs to decide whether they picked the wrong
        format. An empty input cell isn't a failure, it's an empty cell."""
        drv = self._derived_table(d["source_id"])
        with self._reader() as ro:
            return ro.execute(
                f"SELECT COUNT(*) FROM {self._from_clause(src, 's')} "
                f"WHERE {self._had_input_sql(src, d, 's')} AND {q(drv)}.{q(d['name'])} IS NULL"
            ).fetchone()[0]

    def _infer_derived_type(self, d: dict, conn: sqlite3.Connection | None = None) -> str | None:
        """What a text-valued derived column holds, by the rule an imported
        column is typed by (infer_type): 'number' when every non-empty
        value matches NUM_RE, 'text' when one doesn't, 'empty' when there
        are none. None for an op that has a type of its own (a timestamp
        parse, a duration), which never needs looking at.

        Read off the drv_ column as stored rather than tallied by the pass
        that wrote it, so it says the same thing however the values got
        there: a finished pass, a cancelled one, a copy from another case,
        a case from before this was recorded. NUM_RE itself, not something
        cheaper that approximates it, because _numeric_expr gates the sort
        and the comparisons on the same pattern — a column this calls
        'number' holds no value those would turn into NULL. A text column
        answers at its first non-numeric value; a number column costs one
        scan (~0.7s for 1.3M values), beside a pass that took seconds per
        million rows."""
        op = timeparse.OPERATIONS.get(d["op_id"]) or {}
        if op.get("value_type") != "text":
            return None
        drv, col = q(self._derived_table(d["source_id"])), q(d["name"])

        def ask(c: sqlite3.Connection) -> str:
            if c.execute(f"SELECT 1 FROM {drv} WHERE {col} <> '' AND NOT ({col} REGEXP ?) LIMIT 1",
                         (NUM_RE.pattern,)).fetchone():
                return "text"
            return "number" if c.execute(f"SELECT 1 FROM {drv} WHERE {col} <> '' LIMIT 1").fetchone() else "empty"

        if conn is not None:
            return ask(conn)
        with self._reader() as ro:
            return ask(ro)

    def _had_input_sql(self, src: dict, d: dict, alias: str | None = None) -> str:
        """The row had something to read. One column, non-empty, for the
        ordinary op; for a multi-column op (coalesce) ANY of its inputs
        non-blank — the op's own contract is that whitespace is blank, so
        TRIM there, and a row where every listed column is empty is an
        empty row, not a failed one (the preview counts the same way)."""
        inputs = timeparse.op_inputs(d["op_id"], d["input_column"], d.get("params"))
        refs = [self._col_ref(src, c, alias) if alias else self._col_ref(src, c) for c in inputs]
        if len(refs) == 1:
            return f"({refs[0]} IS NOT NULL AND {refs[0]} <> '')"
        # TRIM's default strips spaces only; the op's blank is Python's
        # str.strip(), so name the whitespace explicitly.
        return "(" + " OR ".join(f"({r} IS NOT NULL AND TRIM({r}, ' \t\r\n') <> '')" for r in refs) + ")"

    def unparsed_where_fragment(self, def_id: int) -> str:
        """An advanced-filter fragment selecting exactly the rows that
        failed to parse, so "12 failures" is one click from "show me
        which 12". Built here rather than in the frontend because it has
        to quote two analyst-named columns into SQL (invariant #5)."""
        d = self.get_derived_column(def_id)
        inputs = timeparse.op_inputs(d["op_id"], d["input_column"], d.get("params"))
        if len(inputs) == 1:
            return f"{q(d['name'])} IS NULL AND {q(d['input_column'])} <> ''"
        had = " OR ".join(f"TRIM(COALESCE({q(c)}, ''), ' \t\r\n') <> ''" for c in inputs)
        return f"{q(d['name'])} IS NULL AND ({had})"

    DETECT_SAMPLE = 200

    def _sample_column(self, src: dict, column: str, limit: int | None = None) -> list:
        """The first N non-empty values of a column, in FILE order.

        `ORDER BY rid` is load-bearing, not tidiness. Without it this is a
        bare SCAN and the first N rows arrive in file order by accident —
        until a background column index exists on that column
        (`_ensure_column_index_building` builds one the moment an
        equals/in filter touches it), at which point the planner switches
        to a covering-index range scan and returns the N lexicographically
        SMALLEST values instead. The sample an analyst previewed a derived
        column against would then differ between one open of a case and
        the next, with nothing on screen to explain why. `rid` is the
        INTEGER PRIMARY KEY, so ordering by it costs no temp b-tree —
        the same fact invariant #2's carve-out rests on.
        """
        with self._reader() as ro:
            return [r[0] for r in ro.execute(
                f"SELECT {self._col_ref(src, column)} FROM {self._from_clause(src)} "
                f"WHERE {self._col_ref(src, column)} IS NOT NULL "
                f"AND {self._col_ref(src, column)} <> '' ORDER BY rid LIMIT ?",
                (limit or self.DETECT_SAMPLE,),
            )]

    def _sample_column_in_view(self, view_id: str, column: str,
                               limit: int | None = None) -> list:
        """The same sample, taken from the rows the VIEW holds.

        The head of a file is exactly the region a triage filter exists to
        escape, so a preview sampled from it can report that every value
        parses while the rows the analyst is actually looking at do not.
        Scoping the sample to the view is the analyst saying which rows
        they mean.

        Shape copied from time_histogram's `branch()`: an unfiltered,
        unsorted view (`root_virtual`) has no `v.view_N` table at all and
        reads its member tables directly; a materialised one joins the
        view table per member. A merge reads every member, because a
        merged view's rows carry their own source_id.

        Each member is sliced FIRST and the slices are combined after,
        rather than capping one big union: UNION ALL is evaluated in
        order, so a LIMIT outside it drains member 0 before it ever looks
        at member 1, and a merge whose members are each bigger than
        DETECT_SAMPLE would be sampled entirely from its first table —
        the head-of-file bias this method exists to remove, arrived at by
        a different road. Every member gets an equal share of the budget
        instead. A member with fewer rows than its share spends less and
        the total lands under `limit`, which is the trade worth making:
        the verdict is a proportion, and a merge's members are different
        tools' output, so reading none of one is the failure that matters.

        `ORDER BY` inside each slice is load-bearing for the same reason
        it is in _sample_column, not tidiness. Unordered, the direct
        branch is a bare SCAN whose first N rows are in file order only by
        accident — until a background column index exists on that column,
        at which point the planner switches to a covering-index range scan
        and the sample silently becomes the N lexicographically smallest
        values instead. Both keys are free: the view table's `pos` and a
        source table's `rid` are each an INTEGER PRIMARY KEY, so neither
        ordering costs a temp b-tree, and each slice can stop as soon as
        it has its share.
        """
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        with self._reader() as ro:
            src = self._source_lite_on(ro, handle["source_id"])
            members = self._resolve_members_on(ro, handle["source_id"])
        if self._find_column(src, column) is None:
            raise KeyError(column)
        if handle.get("kind") == "root_virtual":
            # No filter and no sort: the view IS the source, and the
            # cheaper head-of-file path says the same thing.
            return self._sample_column(src, column, limit)

        direct = self._grouping_covers_whole_source(handle, src)
        want = limit or self.DETECT_SAMPLE
        # An even split of the budget, ceil so a remainder does not starve
        # the last member. Deliberately even and NOT weighted by member
        # size: the failure this exists to stop is a member going unread,
        # and the weights that would make the proportion exact are
        # per-member counts WITHIN the view, which is a COUNT per member
        # for an answer the analyst reads as "does any of this fail".
        # The cost is that the reported proportion is a per-member average
        # rather than a whole-view rate — ten bad rows beside five
        # thousand good ones read as 10 of 110, not 10 of 5010. It errs
        # toward caution, which is the right direction for a pre-flight
        # check, but it is not the view's true rate. See
        # docs/notes/derived.md.
        per = max(1, -(-want // len(members)))

        def branch(m: dict) -> str:
            # A member read through its projection (a merge of tables with
            # different columns) has the column, padded or renamed, as a
            # plain column of `s`.
            rsrc = self._ref_src_for(src, m)
            ref = self._col_ref(rsrc, column, "s", None if direct else "d")
            if direct:
                scope = f"FROM {self._member_from(src, m, 's', (column,))}"
                order = "s.rid"
            else:
                scope = (f"FROM v.{q(view_id)} vv "
                         f"JOIN {self._member_join_target(src, m)} s ON s.rid = vv.rid "
                         f"AND vv.source_id = {int(m['source_id'])}"
                         f"{self._member_derived_join(rsrc, m, 's', (column,))}")
                # The view's own order, which is what the analyst is
                # looking at, and free: pos is the view table's rowid.
                order = "vv.pos"
            return (f"SELECT v FROM (SELECT {ref} AS v {scope} "
                    f"WHERE {ref} IS NOT NULL AND {ref} <> '' "
                    f"ORDER BY {order} LIMIT {int(per)})")

        union = " UNION ALL ".join(branch(m) for m in members)
        with self._reader() as ro, self._dropped_view_is_expired():
            return [r[0] for r in ro.execute(f"SELECT v FROM ({union}) LIMIT ?", (want,))]

    def _derive_preview_member(self, merge_id: int, columns: list[str]) -> int:
        """The member a derive preview on a merge samples: its first. A
        column only some members have can't be derived on the merge —
        add_derived_column refuses it, member by member — so the preview
        refuses it the same way, rather than as an unknown column."""
        members = self._resolve_members(merge_id)
        for m in members:
            for col in columns:
                if self._find_column(m["src"], col) is None:
                    raise ValueError(f"Member table {m['src']['name']!r} has no column {col!r}")
        return members[0]["source_id"]

    def detect_timestamp_format(self, source_id: int, column: str) -> list[dict]:
        """Which operations can read this column, best first. Same
        sample-and-round-trip shape preview_sqlite_tables uses to pre-check
        likely WebKit columns: try every parser against real values and
        rank by how many it reads. Ambiguous numeric ranges (Mac absolute
        vs unix seconds) legitimately return several — the caller's preview
        of actual converted values is what settles it."""
        if source_id < 0:  # a merge samples its first (canonical) member
            source_id = self._derive_preview_member(source_id, [column])
        src = self._source_lite(source_id)
        if self._find_column(src, column) is None:
            raise KeyError(column)
        samples = self._sample_column(src, column)
        out = []
        for r in timeparse.detect(samples):
            out.append({**r, "preview": self._preview_rows(samples[:10], r["op_id"], r["params"])})
        return out

    def detect_source_suggestions(self, source_id: int, min_confidence: float = 0.9) -> list[dict]:
        """Columns that look like timestamps the app can't already read —
        the basis for the post-import "this looks like a Unix epoch, want a
        datetime column?" hint. Columns already typed `datetime` at ingest
        are skipped: those already sort and filter correctly, so converting
        them would just duplicate data."""
        src = self._source_lite(source_id)
        if src.get("is_merge"):
            # The hint follows an import, and a merge is never imported —
            # its members already had their turn. It also has no table of
            # its own to sample.
            return []
        # Nothing to suggest for a column the analyst has already converted.
        converted = {d["input_column"].lower() for d in self.list_derived_columns(source_id)}
        out = []
        for c in self._base_cols(src):
            if c["type"] == "datetime" or c["name"].lower() in converted:
                continue
            samples = self._sample_column(src, c["name"])
            if not samples:
                continue
            ranked = timeparse.detect(samples)
            if ranked and ranked[0]["confidence"] >= min_confidence:
                best = ranked[0]
                out.append({
                    "column": c["name"], "op_id": best["op_id"], "label": best["label"],
                    "confidence": best["confidence"], "params": best["params"],
                    "preview": self._preview_rows(samples[:5], best["op_id"], best["params"]),
                })
        return out

    def _preview_state(self, op_id: str, params: dict) -> dict:
        """The same hooks the real backfill runs: a lookup previewed without
        its mapping loaded would show every value as a miss."""
        op = timeparse.OPERATIONS[op_id]
        state: dict = {}
        if op.get("check"):
            op["check"](self, params)
        if op.get("prepare"):
            op["prepare"](self, params, state)
        return state

    def _preview_rows(self, values: list, op_id: str, params: dict,
                      state: dict | None = None) -> list[dict]:
        op = timeparse.OPERATIONS[op_id]
        if state is None:
            state = self._preview_state(op_id, params)
        # parse_or_none: a value that raises previews as a failure, as it
        # would backfill as one — not as a 500 for the whole modal.
        return [{"input": v, "output": timeparse.parse_or_none(op, v, params, state)} for v in values]

    def preview_derived(self, source_id: int, column: str, op_id: str,
                        params: dict | None = None, limit: int = 10,
                        view_id: str | None = None) -> dict:
        """A handful of real input→output pairs plus how many of the
        sampled values this operation can't read, so the analyst sees what
        they're about to create before committing to a full backfill.

        `view_id` scopes the sample to the rows the analyst is looking at.
        Without it the sample is the head of the file, which is the one
        region a triage filter exists to escape — so a preview could
        report that all 200 sampled values parse while every row on screen
        failed. It also gets a merge right: a view over one reads every
        member, where the source-scoped path only ever read member 0.

        The returned `scope` says which sample was actually taken, "view"
        or "table", and is not simply an echo of the argument: a
        multi-input operation cannot be scoped yet (see below) and answers
        "table" even when a view_id was passed. The client renders the
        verdict's "in this view" / "in the whole table" from this field
        rather than from the button it pressed, so the sentence can never
        claim a narrowing the numbers underneath it do not have.
        """
        op = timeparse.OPERATIONS.get(op_id)
        if op is None:
            raise ValueError(f"Unknown operation: {op_id}")
        params = timeparse.validate_params(op_id, params)
        if view_id and not op["multi_input"]:
            if source_id < 0:
                self._derive_preview_member(source_id, [column])   # refuses a partial column
            samples = self._sample_column_in_view(view_id, column)
            return self._derived_preview_from(samples, op_id, params, limit, scope="view")
        # A multi-input operation reads several columns of the SAME row, so
        # the one-column sample above cannot serve it: scoping it needs a
        # row-wise read of the view, a second query shape built for the one
        # operation family that has this problem. Between the two honest
        # options — build that read, or admit the scope was not applied —
        # this takes the second, because the harm is not that the numbers
        # come off the table's head, it is a verdict labelled "in this
        # view" over rows the analyst filtered away, and a field in the
        # payload closes that while a new read path is a feature. So the
        # answer here is the table's, and it says "table".
        if source_id < 0:
            # Preview against the first member — representative, and the
            # only side with a real table to sample.
            source_id = self._derive_preview_member(
                source_id, timeparse.op_inputs(op_id, column, params) if op["multi_input"] else [column])
        src = self._source_lite(source_id)
        if self._find_column(src, column) is None:
            raise KeyError(column)
        if op["multi_input"]:
            inputs = timeparse.op_inputs(op_id, column, params)
            for other in inputs[1:]:
                if self._find_column(src, other) is None:
                    raise KeyError(other)
            state: dict = {"inputs": list(inputs)}   # see backfill_derived_columns
            if op.get("prepare"):
                op["prepare"](self, params, state)
            sel = ", ".join(self._col_ref(src, c) for c in inputs)
            with self._reader() as ro:
                rows = ro.execute(
                    f"SELECT {sel} FROM {self._from_clause(src)} LIMIT ?", (limit,),
                ).fetchall()
            preview = [{"input": r[0], "output": timeparse.parse_multi_or_none(op, list(r), params, state)}
                       for r in rows]
            # A failure is a NULL from a row that HAD something to read —
            # the same rule _count_parse_failures applies after the
            # backfill, so the modal's verdict and the column's count agree.
            failures = sum(1 for r, p in zip(rows, preview)
                           if p["output"] is None and any(v is not None and str(v).strip() for v in r))
            return {"preview": preview, "sampled": len(preview), "failures": failures,
                    "scope": "table"}
        return self._derived_preview_from(self._sample_column(src, column), op_id, params, limit,
                                          scope="table")

    def _derived_preview_from(self, samples: list, op_id: str, params: dict, limit: int,
                              scope: str) -> dict:
        """Pairs, count and failure tally for an already-taken sample —
        shared so the view-scoped and source-scoped paths cannot drift into
        reporting the same numbers two different ways. `scope` travels with
        them for the same reason: the caller that took the sample is the
        only one that knows which rows it came from, and the verdict the
        client writes is a claim about exactly that."""
        op = timeparse.OPERATIONS[op_id]
        # One prepare for the pairs and the count both: a lookup's prepare
        # loads its whole table (up to MAX_LOOKUP_ROWS) inside the request.
        state = self._preview_state(op_id, params)
        preview = self._preview_rows(samples[:limit], op_id, params, state)
        failures = sum(1 for v in samples if timeparse.parse_or_none(op, v, params, state) is None)
        return {"preview": preview, "sampled": len(samples), "failures": failures, "scope": scope}

    def preview_regex_groups(self, source_id: int, column: str, pattern: str,
                             limit: int = 3) -> dict:
        """The named groups a pattern declares, each with a few of the
        values it would pull out of this column.

        One pattern over one column is usually N columns of intent — an
        IIS line is a method, a path, a status and a time — and deriving
        them one at a time meant writing the same regex four times, each
        with a different group number, and four full scans of the table.
        This is what the "a column per named group" offer is built from:
        the names in the order they open, the sample values under each, and
        how much of the sample the pattern matched at all, so the analyst
        can see the pattern is right before committing to a backfill.

        Sampling is the same `_sample_column` every other preview uses (so
        a merge previews against its first member, as they all do), and the
        work is pure Python over those values — no new query shape."""
        names = structparse.regex_group_names(pattern)
        if source_id < 0:
            source_id = self._derive_preview_member(source_id, [column])
        src = self._source_lite(source_id)
        if self._find_column(src, column) is None:
            raise KeyError(column)
        samples = self._sample_column(src, column)
        rx = re.compile(pattern)
        matched = 0
        found: dict[str, list[str]] = {n: [] for n in names}
        for value in samples:
            m = rx.search(str(value))
            if m is None:
                continue
            matched += 1
            for n in names:
                got = m.group(n)
                if got is not None and len(found[n]) < limit:
                    found[n].append(got)
        return {
            "groups": [{"name": n, "samples": found[n]} for n in names],
            "sampled": len(samples),
            "matched": matched,
        }

    # ------------------------------------------------------------ case settings

    def get_case_settings(self) -> dict:
        with self._reader() as ro:
            return {r["key"]: r["value"] for r in ro.execute("SELECT key, value FROM case_settings")}

    def set_case_setting(self, key: str, value: str | None) -> None:
        with self.lock, self.db:
            if value is None or value == "":
                self.db.execute("DELETE FROM case_settings WHERE key=?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO case_settings(key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    # ------------------------------------------------------------------- views

    def build_view(self, source_id: int, spec: dict, *, hold: bool = False,
                   yield_others: bool = False) -> dict:
        """Materialise (pos, source_id, rid) for a filter+sort spec. Returns
        view handle. source_id is a constant column here (every row comes
        from the same source) — kept as a real column rather than a
        handle-level constant so fetch_rows/tag_positions/tag_view/export
        share one code path with merged views, whose rows carry their own
        (differing) source_id per row.

        CLAUDE.md invariant #2's carve-out: an unfiltered, unsorted view of
        a single (non-merge) source needs no materialisation at all — its
        "sort" is just rid order, which INTEGER PRIMARY KEY already gives
        for free (verified: `EXPLAIN QUERY PLAN SELECT rid FROM t ORDER BY
        rid ASC` is a bare `SCAN`, no temp b-tree). See
        _build_virtual_root_view.

        `hold`: build a *pending* view — one that leaves the source's live
        view alone instead of evicting it, so the grid keeps paging the
        rows it has while a long search runs in the background
        (start_view_job). The handle carries `pending: True` until
        adopt_view installs it, which is when the others go. A held build
        still evicts an EARLIER pending view of the same source (one
        pending per source, or every abandoned search would leave a table
        behind in the views db), and a normal build evicts pending views
        along with everything else — the newer intent wins. Threaded
        through both the materialised block and the virtual-root path: a
        cleared search box lands in the latter, which evicts under the
        lock before it has a handle to mint."""
        src = self._source_lite(source_id)
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        order = self._compile_order(spec, colnames)
        # Only non-numeric sort columns: _compile_order emits a plain
        # `col COLLATE NOCASE` for those, which a background index can
        # serve; a numeric sort goes through _numeric_expr, a functional
        # expression a plain index can't match (see
        # _ensure_sort_index_building).
        sort_cols = [
            s.get("column") for s in (spec.get("sort") or [])
            if s.get("column") in colnames and colnames[s.get("column")] != "number"
        ]
        # __line__ asc IS the virtual root's order, so it deliberately does
        # NOT count as a sort (keeps the no-materialize fast path); only the
        # descending direction needs a real ORDER BY.
        has_sort = any(
            s.get("column") in colnames
            or (s.get("column") == "__line__" and str(s.get("dir", "asc")).lower() == "desc")
            for s in (spec.get("sort") or [])
        )

        if src.get("is_merge"):
            for frag in self._raw_fragments(spec.get("filter_tree")):
                self.validate_where_fragment(source_id, frag)
            sid_col = q(self._member_id_alias(colnames))
            # (member id, FROM ... WHERE ...) per member; the columns that go
            # between the two depend on the sort shape chosen below.
            parts = []
            filtered = False
            params: list[Any] = []
            for m in self._resolve_members(source_id):
                msrc = m["src"]
                where, p = self._compile_where(m["source_id"], self._member_read_src(src, msrc), spec, colnames)
                for col in sort_cols:
                    self._ensure_sort_index_building(
                        m["source_id"], col,
                        self._index_table_for(src, col, msrc["table_name"], m["source_id"]))
                parts.append((int(m["source_id"]),
                              f" FROM {self._member_from(src, m)}" + (f" WHERE {where}" if where else "")))
                filtered = filtered or bool(where)
                params.extend(p)

            def union(cols: str) -> str:
                return " UNION ALL ".join(f"SELECT {mid} AS {sid_col}, rid{cols}{rest}" for mid, rest in parts)

            vid = self._next_view_id()
            # Each member SELECTs its id, rid and what the sort needs, and
            # nothing else. This used to project every column, so SQLite
            # read every column of every row of every member — the whole
            # table, whatever indexes it had — and dragged the lot through
            # the sort: clearing the filters on a 500k-row, five-member
            # merge read all 739 MB of its tables, each time, where a
            # single table reads its sort index.
            if filtered:
                # Filtered: one sort over what the members matched, as
                # before. The compound's own ORDER BY (below) would have
                # SQLite walk each member's sort index and look every row
                # up to test the filter, measured slower than scanning.
                need = dict.fromkeys(s.get("column") for s in (spec.get("sort") or [])
                                     if s.get("column") in colnames)
                select = f"SELECT {sid_col}, rid FROM ({union(''.join(f', {q(c)}' for c in need))}) {order}"
            else:
                # Unfiltered (Clear filters, a sort change): the ORDER BY is
                # the compound's own, so SQLite streams each member in its
                # sort index's order and merges them (MERGE (UNION ALL) in
                # the plan) — 18 MB of index for that merge, and no sort.
                # The outer SELECT has no ORDER BY of its own, which is what
                # keeps SQLite from dropping the inner one (its OmitOrderBy
                # rule), so the INSERT still assigns pos in sorted order.
                # With no sort keys (rid order, or the Line column) the
                # compound is already (member id, rid) and goes in as is.
                keys, merge_order = self._compile_merge_order(spec, colnames)
                union_sql = union("".join(f", {k}" for k in keys))
                select = (f"SELECT {sid_col}, rid FROM ({union_sql} {merge_order})" if keys
                          else f"{union_sql} {merge_order}")
            sql = f"INSERT INTO v.{q(vid)}(source_id, rid) {select}"
        else:
            table = src["table_name"]
            where, params = self._compile_where(source_id, src, spec, colnames)
            derived_names = {c["name"] for c in src["columns"] if c.get("derived")}
            for col in sort_cols:
                # A derived column's values live in the sidecar, so its sort
                # index belongs on that table, not on src_<id>.
                self._ensure_sort_index_building(
                    source_id, col,
                    self._derived_table(source_id) if col in derived_names else table,
                )
            if not where and not has_sort:
                return self._build_virtual_root_view(source_id, src, hold=hold)
            vid = self._next_view_id()
            sql = (
                f"INSERT INTO v.{q(vid)}(source_id, rid) "
                f"SELECT {int(source_id)}, rid FROM {self._from_clause(src)}"
                + (f" WHERE {where}" if where else "")
                + f" {order}"
            )
        t0 = time.time()
        # `yield_others`: this build is for the table the analyst is
        # looking at now, so a build for a DIFFERENT table holding the
        # writer lock stands aside for it — asked while waiting for the
        # lock, not once before blocking on it (_writer_for_build).
        with self._writer_for_build(source_id, spec.get("op_token"), yield_others), \
                self._running_build(source_id, spec.get("op_token")):
            with self._interruptible(spec.get("op_token"), self.db), self.db:
                # pos declared INTEGER PRIMARY KEY makes it *be* the rowid, so
                # this alone gives the same unique/indexed-by-pos lookup the old
                # CTAS + separate CREATE UNIQUE INDEX did, for one less full
                # sort-and-write of every row (see CLAUDE.md's "Performance"
                # section, change 3). pos is deliberately absent from the
                # INSERT's column list: rowids are assigned 1..N in insertion
                # order, and an INSERT..SELECT with ORDER BY inserts in sorted
                # order — measured ~35% faster than numbering the same sort
                # with ROW_NUMBER() OVER, for byte-identical view content.
                # (The ORDER BY always ends in `rid ASC`, so the order — and
                # therefore every pos — is fully determined, not sorter-
                # dependent.) cursor.rowcount then gives the view's row count
                # without re-scanning the table we just wrote.
                self.db.execute(f"CREATE TABLE v.{q(vid)} (pos INTEGER PRIMARY KEY, source_id INTEGER, rid INTEGER)")
                n = self.db.execute(sql, params).rowcount
                # Evicted *after* the new view is built, not before (it used to
                # be first): the whole block is one transaction, so a cancelled
                # (interrupted) build rolls back to a world where the previous
                # view still exists — the frontend keeps its rows instead of
                # every open handle 409ing. The new view's handle isn't in
                # self._views yet, so the eviction loop can't touch it. A held
                # build evicts only an earlier pending view (see the docstring).
                self._evict_root_views(source_id, pending_only=hold)

            # Registered after the commit (a rolled-back build never gets a
            # handle) but BEFORE the writer lock is released. Registering
            # after it left a gap in which the next build for this source
            # could take the lock, find no handle to evict, and register
            # its own — leaving this one live beside it: two root views for
            # one source, a table nothing would drop, and an older held
            # view adopt_view would install over the newer build instead of
            # answering 409.
            handle = {
                "view_id": vid,
                "source_id": source_id,
                "row_count": n,
                "kind": "root",
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
            if hold:
                handle["pending"] = True
            self._views[vid] = handle
        return handle

    def _evict_root_views(self, source_id: int, *, keep: str | None = None,
                          pending_only: bool = False) -> None:
        """Evicts every existing root (materialised or virtual) view for
        this source, so opening a new one leaves only the new view live.
        Caller must hold self.lock (and be inside a self.db transaction —
        eviction of a materialised view drops its backing table).

        `keep` spares one view id (adopt_view: the view being installed is
        already registered); `pending_only` evicts only held views that
        were never adopted (a held build superseding an earlier one).

        Only root views for this source are eligible here. Group sub-views
        (kind='group'/'group_virtual') are never evicted by this loop
        directly — they cascade-drop with their parent via
        _evict_view_and_children, so expanding one group never evicts a
        sibling or the outer root view. `old` may already be gone by the
        time we reach it (a previous iteration's cascade can remove a later
        entry in this same snapshot) — .get() and skip, don't [ ]."""
        for old in list(self._views):
            if old == keep:
                continue
            handle = self._views.get(old)
            if not handle or handle.get("kind") not in ("root", "root_virtual") or handle["source_id"] != source_id:
                continue
            if pending_only and not handle.get("pending"):
                continue
            self._evict_view_and_children(old)

    def adopt_view(self, view_id: str) -> dict:
        """Installs a held view (build_view(hold=True)) as the source's live
        one: clears `pending` and evicts every other root view for that
        source — the same world a normal build leaves behind, arrived at a
        step later. Returns the handle, the same payload build_view does.
        KeyError when the view is gone: a normal build, a newer held build
        or a case switch evicted it in the meantime, and server.py answers
        409 "expired" so the client runs the search again instead."""
        with self.lock, self.db:
            handle = self._views.get(view_id)
            if handle is None or handle.get("kind") not in ("root", "root_virtual"):
                raise ViewExpired("View expired — rebuild it")
            handle["pending"] = False
            self._evict_root_views(handle["source_id"], keep=view_id)
        return handle

    def _next_view_id(self) -> str:
        """The one place a view id is minted. See _view_seq_lock."""
        with self._view_seq_lock:
            self._view_seq += 1
            return f"view_{self._view_seq}"

    def _build_virtual_root_view(self, source_id: int, src: dict, *, hold: bool = False) -> dict:
        """No filters, no sort: the view IS the source table in its natural
        rid order, which INTEGER PRIMARY KEY already gives for free — no
        v.view_N to build. row_count is read straight off `src` (already
        the source's own row_count) rather than a fresh COUNT(*).

        fetch_rows/tag_positions/find_position/tag_view/export all handle
        kind='root_virtual' by paging or computing directly against the
        source table instead of a backing view table — every one of them
        can use the exact `pos = rid - 1` shortcut (never a stub) because
        ingest assigns rid contiguously from 1 for every ingest path
        (verified: CSV/SQLite-table/JSON ingest all create `rid INTEGER
        PRIMARY KEY` without ever specifying a value, insert in sequential
        BATCH-sized transactions that either fully commit or fully roll
        back, and no source table is ever mutated after ingest —
        invariant #1) and this view is, by construction, every row of the
        source.

        Deliberately narrower than "sort is absent OR served by an
        existing index" — a virtual view with a sorted (even indexed)
        order is not implemented: tag_positions would need each tagged
        row's rank in that order, which isn't a `pos = rid - 1` shortcut
        and would cost one query per tagged row (up to 20,000) rather than
        one materialise. Change 2 already makes that case's materialise
        cheap (index supplies the order, no temp b-tree); this virtual
        path targets the single biggest and most common case instead:
        opening a table.

        `hold` as in build_view: a held virtual view evicts only an earlier
        pending view. The eviction here happens before the handle exists,
        which is exactly why hold has to reach this path too — a search box
        cleared while a search is pending lands here."""
        with self.lock, self.db:
            self._evict_root_views(source_id, pending_only=hold)
            vid = self._next_view_id()
            handle = {
                "view_id": vid,
                "source_id": source_id,
                "row_count": src["row_count"],
                "kind": "root_virtual",
                "elapsed_ms": 0,
            }
            if hold:
                handle["pending"] = True
            self._views[vid] = handle
        return handle

    def _evict_view_and_children(self, vid: str) -> None:
        """Drops a view (and its backing table, unless it's a virtual
        no-table handle) plus every group sub-view whose parent_view_id is
        this one — recursively, so a chain of expansions cleans up fully.
        Caller must hold self.lock (and be inside a self.db transaction if
        dropping tables)."""
        handle = self._views.pop(vid, None)
        if not handle:
            return
        if handle.get("kind") not in ("group_virtual", "root_virtual"):
            self.db.execute(f"DROP TABLE IF EXISTS v.{q(vid)}")
        for child_vid, child in list(self._views.items()):
            if child.get("parent_view_id") == vid:
                self._evict_view_and_children(child_vid)

    def close_view(self, view_id: str) -> None:
        """Explicit free for a group sub-view (called on UI collapse) rather
        than waiting for its parent root view to be rebuilt."""
        with self.lock, self.db:
            self._evict_view_and_children(view_id)

    # -------------------------------------------------------------- timeline

    def _timeline_summary_match(self, src: dict) -> tuple[str, dict] | None:
        """(shape name, summary template) from defaults/headers.json for
        this source's artefact shape, or None for a CSV no shipped shape
        describes. The name is the shipped one ("Event logs (EvtxECmd)"),
        which Configure tables… quotes when it says what the Body falls
        back to.

        Matched the way every other header-set binding in here is matched
        (_sources_for_header_set): a source qualifies when it CONTAINS the
        set's columns, case-insensitively, against its own imported
        columns only — a derived column the analyst added must not stop
        an EvtxECmd export from being recognised as one. The most specific
        match wins when several sets fit, so a shape that is another one
        plus a few columns is summarised as itself rather than as whichever
        smaller set happened to be listed first."""
        from . import defaults

        head = defaults.headers()
        have = {c["name"].strip().lower() for c in self._base_cols(src)}
        best: tuple[int, str, dict] | None = None
        for name, cols in head["nicknames"]:
            spec = head["summaries"].get(name)
            if not spec or not all(c.strip().lower() in have for c in cols):
                continue
            if best is None or len(cols) > best[0]:
                best = (len(cols), name, spec)
        return (best[1], best[2]) if best else None

    def _timeline_summary_template(self, src: dict) -> dict | None:
        """The template half of _timeline_summary_match."""
        match = self._timeline_summary_match(src)
        return match[1] if match else None

    def _timeline_summary_exprs(self, src: dict, ts_col: str | None) -> tuple[str, str, list] | None:
        """(lead SQL, details SQL, params) for this source's summary, or
        None when nothing shipped describes it — in which case the row
        keeps today's pipe-joined body and nothing regresses.

        `ts_col` is dropped from both halves wherever it appears: the
        timestamp is already the column beside the body, and a summary
        that opens by repeating it is exactly the row the Timeline had
        before. That is enforced here, against the timestamp this source
        is ACTUALLY being read with — which can be a derived column or an
        analyst's override, neither of which the shipped template can
        know about."""
        spec = self._timeline_summary_template(src)
        if not spec:
            return None
        have = {c["name"].strip().lower(): c["name"] for c in self._base_cols(src)}
        skip = (ts_col or "").strip().lower()

        def resolve(name: str) -> str | None:
            key = name.strip().lower()
            return None if key == skip else have.get(key)

        lead_cols = [c for c in (resolve(n) for n in spec["lead"]) if c]
        details = [(label, col) for label, col in
                   ((label, resolve(name)) for name, label in spec["details"]) if col]
        if not lead_cols and not details:
            return None
        lead_sql = ("TL_LEAD(" + ", ".join(self._col_ref(src, c, "s") for c in lead_cols) + ")"
                    if lead_cols else "''")
        params: list[Any] = []
        if not details:
            return lead_sql, "''", params
        # The lead expression is evaluated a second time inside TL_DETAILS
        # (it carries no parameters, so repeating its text is safe) because
        # dropping a detail that repeats the lead needs the lead's VALUE,
        # which is only known per row.
        args = [lead_sql]
        for label, col in details:
            args.append("?")
            params.append(label)
            args.append(self._col_ref(src, col, "s"))
        return lead_sql, f"TL_DETAILS({', '.join(args)})", params

    def _timeline_parts(self, src: dict, cfg: dict) -> dict:
        """How one source reads on the Timeline under `cfg` (a resolved
        timeline template, or {} for none): the SQL for its timestamp,
        body and summary columns against alias `s`, the summary's bound
        parameters, and the type label — plus what was chosen, for the
        callers that have to say so.

        The one place these rules live. build_timeline unions a branch of
        this per source, and timeline_preview (Configure tables…) runs the
        very same expressions over a few rows, so the dialog's preview
        cannot drift from what the Timeline then shows.

        - The timestamp is `cfg`'s column when the source has it, else
          its first datetime column, else none.
        - The body is `cfg`'s columns (the ones the source has), else every
          column, cut to what one expression can hold (_fit_body_cols).
        - The summary only applies when nothing was picked: an analyst who
          picked body columns said what they want this source to read as,
          so the shipped summary stays out of it; otherwise the artefact
          shape gets its summary, and a shape nothing describes keeps the
          join.
        - An empty type label is no label: the source's own name."""
        col_names = {c["name"] for c in src["columns"]}
        dt_cols = [c["name"] for c in src["columns"] if c["type"] == "datetime"]

        ts_col = cfg.get("timestamp_column")
        if ts_col not in col_names:
            ts_col = dt_cols[0] if dt_cols else None
        # A derived column is already canonical "YYYY-MM-DD HH:MM:SS",
        # so it's used as-is: TS_NORMALIZE would only truncate the
        # sub-second part that makes two same-second events sort in the
        # order they happened. Everything else still normalises, which
        # is what lets sources in different formats interleave.
        ts_expr = "NULL"
        if ts_col:
            ref = self._col_ref(src, ts_col, "s")
            ts_expr = ref if self._is_derived(src, ts_col) else ts_normalize_sql(ref)

        chosen_body = [c for c in (cfg.get("body_columns") or []) if c in col_names]
        body_cols = chosen_body or [c["name"] for c in src["columns"]]
        shown = self._fit_body_cols(body_cols)
        body_expr = " || ' | ' || ".join(
            f"COALESCE({self._col_ref(src, c, 's')}, '')" for c in shown
        )

        summary = None if chosen_body else self._timeline_summary_exprs(src, ts_col)
        lead_expr, detail_expr, summary_params = summary or ("''", "''", [])
        shape = None
        if summary:
            match = self._timeline_summary_match(src)
            shape = match[0] if match else None
        return {
            "ts_col": ts_col, "ts_expr": ts_expr, "body_expr": body_expr,
            "lead_expr": lead_expr, "detail_expr": detail_expr, "summary_params": summary_params,
            "type_label": cfg.get("type_label") or src["name"],
            "body_columns": chosen_body, "body_shown": len(shown), "body_total": len(body_cols),
            "summary_shape": shape,
        }

    TIMELINE_PREVIEW_ROWS = 3
    TIMELINE_PREVIEW_MAX = 10

    def timeline_preview(self, source_id: int, cfg: dict | None = None, limit: int | None = None) -> dict:
        """A few rows of one table as the Timeline would show them under
        `cfg` — the not-yet-saved settings in Configure tables…, so the
        dialog can show the result of a pick before it is saved.

        Built by _timeline_parts, the same expressions build_timeline
        materialises, so a preview and the Timeline cannot disagree about
        what a row reads as (the summary, its timestamp-column drop, the
        495-column cap, the derived-column timestamp). Which rows: the
        table's first TAGGED rows, since those are the ones the Timeline
        holds, then its first rows when fewer are tagged, so a table nobody
        has tagged yet still previews. In Timeline order among themselves.

        `body` says what the Body is made of: "columns" (the ones picked),
        "summary" (the shipped one for `summary_shape`) or "all" (every
        column joined, for a shape nothing describes). `shape` names the
        shipped shape the table matches whether or not its summary is in
        use, so the dialog can say what picking columns replaces.
        `timestamp_column` is the column actually read — the fallback
        resolved, so the dialog can name it.

        A pure read on the reader pool (invariant #4); a merge is refused,
        since the Timeline is configured per real table (invariant #9:
        its rows are read through their members)."""
        if source_id < 0:
            raise ValueError("A merged table isn't configured for the Timeline itself — "
                             "its rows appear under the tables it merges.")
        src = self.get_source(source_id)
        self._require_columns(src)
        limit = self.TIMELINE_PREVIEW_ROWS if limit is None else int(limit)
        limit = max(1, min(self.TIMELINE_PREVIEW_MAX, limit))
        p = self._timeline_parts(src, cfg or {})
        select = (
            f"SELECT s.rid AS rid, {p['ts_expr']} AS ts, ({p['body_expr']}) AS body, "
            f"{p['lead_expr']} AS summary_lead, {p['detail_expr']} AS summary_detail, "
            f"(SELECT GROUP_CONCAT(tag_id) FROM row_tags WHERE source_id={int(source_id)} AND rid=s.rid) AS tag_ids "
            f"FROM {self._from_clause(src, 's')} "
        )
        with self._reader() as ro:
            # rid order over row_tags' (source_id, rid, tag_id) key, and
            # over the table's own rid key: two short range reads, never a
            # scan of a large table.
            tagged = [r[0] for r in ro.execute(
                "SELECT DISTINCT rid FROM row_tags WHERE source_id=? ORDER BY rid LIMIT ?",
                (source_id, limit))]
            rows = []
            if tagged:
                rows += ro.execute(
                    select + f"WHERE s.rid IN ({','.join('?' * len(tagged))})",
                    [*p["summary_params"], *tagged]).fetchall()
            if len(rows) < limit:
                skip = f"WHERE s.rid NOT IN ({','.join('?' * len(tagged))}) " if tagged else ""
                rows += ro.execute(
                    select + skip + "ORDER BY s.rid LIMIT ?",
                    [*p["summary_params"], *tagged, limit - len(rows)]).fetchall()
        match = self._timeline_summary_match(src)
        # Each row in fetch_timeline_rows' shape, so the dialog paints it
        # with the Timeline's own row code — tag dots included.
        out = [{
            "source_id": source_id, "rid": r["rid"], "ts": r["ts"], "body": r["body"],
            "lead": r["summary_lead"] or "", "detail": r["summary_detail"] or "",
            "type_label": p["type_label"], "source_name": src["name"],
            "tags": [int(x) for x in r["tag_ids"].split(",")] if r["tag_ids"] else [],
        } for r in rows]
        for r in out:
            r["tagged"] = bool(r["tags"])
        # The Timeline's own order: timestamped rows first, oldest first.
        out.sort(key=lambda r: (r["ts"] is None, r["ts"] or "", r["rid"]))
        return {
            "source_id": source_id, "source_name": src["name"], "rows": out,
            "timestamp_column": p["ts_col"],
            "body": "columns" if p["body_columns"] else ("summary" if p["summary_shape"] else "all"),
            "summary_shape": p["summary_shape"], "shape": match[0] if match else None,
            "body_shown": p["body_shown"], "body_total": p["body_total"],
        }

    def build_timeline(self, configs: dict[int, dict] | None = None, tag_ids: list[int] | None = None,
                       op_token: str | None = None) -> dict:
        """Materialises one row per *tagged* row across every real source in
        the case (open or closed — this is "every finding in the case", not
        "every finding in a currently-open tab"), each contributing its own
        timestamp/body/type-label per `configs.get(source_id)`. `configs` is
        resolved by the caller (server.py's _resolve_timeline_configs,
        matching each source's header set against workspace.timeline_
        templates) rather than looked up in here — store.py can't import
        workspace.py (workspace.py already imports from store.py; see
        pop_legacy_presets for the same constraint) — a source missing from
        `configs`, or with an override that doesn't actually apply to it
        (a timestamp_column that isn't one of its own columns), falls back
        to its first datetime column, every column, and its own name.

        Each row also carries a **summary** — a lead ("Special privileges
        assigned") and a few identifying details ("svc_backup · WKSTN-4471
        · id 4672") — built here, beside the body, from the artefact
        shape's template in defaults/headers.json. Built here rather than
        client-side for the reason the body is: the client is a virtualized
        list that already has the row, and re-deriving a summary there
        would mean shipping it the source columns it deliberately never
        fetches. A shape nothing describes, or a source whose body columns
        the analyst chose by hand, gets an empty summary and reads exactly
        as it did before.

        `tag_ids` narrows to rows carrying at least one of those tags;
        None/empty means every tagged row regardless of which tag(s).

        A source whose body would join more columns than SQLite's expression
        depth allows (_fit_body_cols — 495 at the default limit) keeps only
        the first that many, in body order, rather than failing the whole
        build. Each one trimmed that way is listed in the result's
        `body_trimmed` so the tab can say so.

        Same materialize-into-v.-then-page-by-pos pattern as build_view
        (CLAUDE.md invariant #2) — a heavily-tagged case can still be
        thousands of rows, so this isn't exempt from the no-LIMIT/OFFSET
        rule just because it's a cross-table view rather than a single-
        source one. Only one timeline view is ever alive at a time (an
        analyst has one Timeline tab, not several) — building a new one
        evicts whatever timeline view existed before, same as build_view
        evicts the previous root view for a source."""
        configs = configs or {}
        tag_clause = f" AND tag_id IN ({','.join('?' * len(tag_ids))})" if tag_ids else ""

        branches = []
        params: list[Any] = []
        body_trimmed: list[dict] = []
        for src in self.list_sources():
            if not src["tagged_row_count"]:
                continue
            source_id = src["id"]
            p = self._timeline_parts(src, configs.get(source_id, {}))
            if p["body_shown"] < p["body_total"]:
                body_trimmed.append({"source_id": source_id, "source_name": src["name"],
                                     "shown": p["body_shown"], "total": p["body_total"]})
            branches.append(
                f"SELECT {int(source_id)} AS source_id, s.rid AS rid, {p['ts_expr']} AS ts, "
                f"({p['body_expr']}) AS body, "
                f"{p['lead_expr']} AS summary_lead, {p['detail_expr']} AS summary_detail, "
                f"? AS type_label, ? AS source_name, "
                f"(SELECT GROUP_CONCAT(tag_id) FROM row_tags WHERE source_id={int(source_id)} AND rid=s.rid) AS tag_ids "
                f"FROM {self._from_clause(src, 's')} "
                f"WHERE s.rid IN (SELECT rid FROM row_tags WHERE source_id={int(source_id)}{tag_clause})"
            )
            # Parameter order follows the order the placeholders appear in
            # the SELECT above, since SQLite binds by ordinal across the
            # whole UNION: the summary's detail labels, then the two label
            # columns, then this branch's tag ids.
            params.extend(p["summary_params"])
            params.append(p["type_label"])
            params.append(src["name"])
            if tag_ids:
                params.extend(tag_ids)

        vid = self._next_view_id()
        with self.lock, self._interruptible(op_token, self.db), self.db:
            self.db.execute(
                f"CREATE TABLE v.{q(vid)} (pos INTEGER PRIMARY KEY, source_id INTEGER, rid INTEGER, "
                f"ts TEXT, body TEXT, summary_lead TEXT, summary_detail TEXT, "
                f"type_label TEXT, source_name TEXT, tag_ids TEXT)"
            )
            n = 0
            if branches:
                union_sql = " UNION ALL ".join(branches)
                # ORDER BY-fed insert, same as build_view: pos auto-assigns
                # in sorted order ((source_id, rid) makes the order fully
                # determined), rowcount replaces a count(*) re-scan.
                n = self.db.execute(
                    f"INSERT INTO v.{q(vid)}(source_id, rid, ts, body, summary_lead, summary_detail, "
                    f"type_label, source_name, tag_ids) "
                    f"SELECT source_id, rid, ts, body, summary_lead, summary_detail, "
                    f"type_label, source_name, tag_ids FROM ({union_sql}) "
                    f"ORDER BY (ts IS NULL) ASC, ts ASC, source_id, rid",
                    params,
                ).rowcount
            # Same evict-after-build ordering (and reasoning) as build_view.
            for old in list(self._views):
                h = self._views.get(old)
                if h and h.get("kind") == "timeline":
                    self._evict_view_and_children(old)

        self._views[vid] = {"view_id": vid, "kind": "timeline", "row_count": n}
        return {"view_id": vid, "row_count": n, "body_trimmed": body_trimmed}

    def _fit_body_cols(self, cols: list[str]) -> list[str]:
        """The first of `cols` that one ' | '-joined body can hold before
        SQLite rejects the expression as too deep: two levels per column
        (the value and its separator), less TIMELINE_BODY_DEPTH_HEADROOM —
        495 at the default limit of 1000. All of them when this SQLite was
        built with no depth limit at all (getlimit reports 0)."""
        getlimit = getattr(self.db, "getlimit", None)
        depth = getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH) if getlimit else SQLITE_DEFAULT_EXPR_DEPTH
        if depth <= 0:
            return cols
        return cols[:max(1, (depth - TIMELINE_BODY_DEPTH_HEADROOM) // 2)]

    def fetch_timeline_rows(self, view_id: str, start: int, count: int) -> dict:
        handle = self._views.get(view_id)
        if not handle or handle.get("kind") != "timeline":
            raise ViewExpired("View expired — rebuild it")
        with self._reader() as ro, self._dropped_view_is_expired():
            rows = ro.execute(
                f"SELECT pos, source_id, rid, ts, body, summary_lead, summary_detail, "
                f"type_label, source_name, tag_ids "
                f"FROM v.{q(view_id)} WHERE pos >= ? AND pos < ? ORDER BY pos",
                (start + 1, start + 1 + count),
            ).fetchall()
        # `body` stays the raw row it has always been and `lead`/`detail`
        # ride along beside it, rather than the summary replacing the body:
        # the row has to be able to show both (the client's "raw" toggle),
        # and a client that re-fetches after a rebuild must not depend on
        # which shape the row happened to get.
        out = [
            {
                "pos": r["pos"] - 1,
                "source_id": r["source_id"],
                "rid": r["rid"],
                "ts": r["ts"],
                "body": r["body"],
                "lead": r["summary_lead"] or "",
                "detail": r["summary_detail"] or "",
                "type_label": r["type_label"],
                "source_name": r["source_name"],
                "tags": [int(x) for x in r["tag_ids"].split(",")] if r["tag_ids"] else [],
            }
            for r in rows
        ]
        return {"start": start, "rows": out}

    # ---------------------------------------------------------------- group-by

    GROUP_MATERIALIZE_THRESHOLD = 5000  # rows; above this (or for a merge) a group gets a real indexed sub-view

    @staticmethod
    def _eq_condition(col_ident: str, value: Any, is_datetime: bool = False) -> tuple[str, list]:
        """`is_datetime` must match how group_summary grouped this column
        (DAY_BUCKET'd or not) — otherwise a group's day-bucket value like
        "2026-01-01" would get compared against the raw full-timestamp
        column and never match any row, breaking expand/tag/export for
        every datetime group. The bucket is day_bucket_sql's, which only
        calls DAY_BUCKET for a value not in the ISO shape, so `col_ident`
        must be a column reference: it appears several times."""
        expr = day_bucket_sql(col_ident) if is_datetime else col_ident
        if value is None:
            return f"{expr} IS ?", [None]
        return f"{expr} = ?", [value]

    @staticmethod
    def _tag_condition(alias: str, source_id: int, value: Any) -> tuple[str, list]:
        """"Rows carrying tag `value`", or "rows carrying no tag at all"
        when `value` is None — the tag pseudo-column's answer to
        _eq_condition, and the reason grouping by tag needs its own
        condition builder rather than a clever column expression: the thing
        being compared lives in the row_tags sidecar, keyed by
        (source_id, rid), not in the source table at all.

        `value` is a tag *id*, never a name: tag_defs has no unique
        constraint on name, so grouping by name would silently merge two
        tags an analyst deliberately kept apart. The frontend renders the
        name it already has in S.tags."""
        prefix = f"{alias}." if alias else ""
        if value is None:
            return f"{prefix}rid NOT IN (SELECT rid FROM row_tags WHERE source_id = ?)", [source_id]
        return (
            f"{prefix}rid IN (SELECT rid FROM row_tags WHERE source_id = ? AND tag_id = ?)",
            [source_id, int(value)],
        )

    def _path_where(self, path: list[dict] | None, colnames: dict, alias: str = "",
                    src: dict | None = None, derived_alias: str | None = None,
                    member_sid: int | None = None) -> tuple[list[str], list]:
        """Compile a group-by path (the outer levels' already-fixed values,
        for nested multi-column grouping) into per-column condition
        fragments. `alias` (e.g. "s"), when given, is baked into the
        identifier _eq_condition builds on — NOT prefixed onto the
        returned fragment by the caller afterward, since a datetime
        column's fragment is a CASE over the column (day_bucket_sql), and
        "s.CASE ..." isn't valid SQL the way "s.\"col\"" is.

        `src` routes each column through _col_ref, so a level grouped on a
        derived column resolves against the sidecar rather than taking the
        source table's alias (which would be "no such column")."""
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        for p in path or []:
            col = p.get("column")
            if col == TAG_GROUP_COLUMN:
                # A tag level's predicate is per *member* (row_tags is keyed
                # by source_id), which is why member_sid is threaded down
                # here rather than the whole path being compiled once and
                # reused across a merge's members.
                if member_sid is None:
                    raise ValueError("grouping by tag needs a member source id")
                c, v = self._tag_condition(alias, member_sid, p.get("value"))
                clauses.append(c)
                params.extend(v)
                continue
            if col not in colnames:
                raise KeyError(col)
            ident = (self._col_ref(src, col, alias or None, derived_alias) if src is not None
                     else f"{prefix}{q(col)}")
            c, v = self._eq_condition(ident, p.get("value"), colnames[col] == "datetime")
            clauses.append(c)
            params.extend(v)
        return clauses, params

    @staticmethod
    def _grouping_covers_whole_source(handle: dict, src: dict) -> bool:
        """True when this view provably contains every row of every member
        table, so an aggregate over the view and one over the tables
        themselves are the same question.

        Row counts are enough to prove it, not just a heuristic: a view's
        rows are always distinct (source_id, rid) pairs drawn from its
        members (build_view materialises one row per matching source row),
        so a view holding as many rows as the source holds can only be all
        of them. A filter that happens to match every row is
        indistinguishable from no filter here — and that's fine, because the
        answer is genuinely identical either way. `src` covers a merge
        without special-casing: _merge_source_dict's row_count is already
        the sum of its members'.

        Takes the source dict the caller already has rather than looking it
        up: get_source runs a COUNT(DISTINCT rid) over row_tags, which on a
        heavily tagged source is ~12ms — enough that adding a lookup here
        would have eaten the entire saving this check exists to unlock.

        Restricted to root views: a 'group'/'group_virtual' sub-view is a
        subset by construction, and its row_count comparison would be
        meaningless anyway. A root_virtual view is always this case by
        construction (build_view only goes virtual when unfiltered — see
        _build_virtual_root_view) without needing the row_count comparison,
        which matters because it has no v.view_N to have been counted from
        in the first place."""
        if handle.get("kind") == "root_virtual":
            return True
        if handle.get("kind", "root") != "root":
            return False
        return bool(src["row_count"]) and handle["row_count"] == src["row_count"]

    def group_summary(self, view_id: str, column: str, order: str = "count", direction: str | None = None,
                       limit: int = 1000, path: list[dict] | None = None,
                       op_token: str | None = None, bucket_datetime: bool = True) -> dict:
        """One aggregate pass over the already-filtered view — SELECT val,
        count(*) per member, unioned and re-summed. Not a paging operation,
        so no O(window) concern here; capped at `limit` distinct groups.

        `path` narrows to one nested group's rows before aggregating the
        next column — [{"column": ..., "value": ...}, ...] for every outer
        level already chosen, e.g. computing the User breakdown *within*
        the Process="svchost.exe" group of a Process-then-User grouping.

        `column` may be TAG_GROUP_COLUMN, which groups by the tags on the
        rows rather than by anything in the file: one group per tag (its
        `value` is the tag *id* — tag names aren't unique) plus one for the
        untagged remainder (`value` None). It's the one grouping whose
        counts can sum to more than the view holds, because a row carrying
        two tags belongs to both groups; that's what makes it answer "how
        much of this have I marked, and as what", which a partition into
        tag-set combinations would not. It nests in either direction like
        any other level. See _tag_group_branches for the query shapes and
        why their join order is pinned.

        A 'datetime' column groups by calendar day (DAY_BUCKET) rather than
        exact timestamp — grouping by the raw value would put nearly every
        row in a group of its own at second/millisecond precision, which
        isn't useful for anything. `expand_group`/`_virtual_group_where`
        match this same DAY_BUCKET'ing (via _eq_condition's is_datetime
        flag) so a click on a day group's row actually finds its rows.

        `bucket_datetime=False` turns that off, and exists for exactly one
        caller: the header value-picker dropdown, which lists a column's
        distinct values so the analyst can tick the ones to filter to. What
        it writes is an ordinary `=`/`in` filter on the *stored* value, so
        every value it shows has to be one such a filter can match — a
        DAY_BUCKET'ed one never is. Nothing about the grouping contract
        above is relaxed: this path returns values, never groups anything
        gets expanded against, so there is no `_eq_condition` on the other
        side to keep in step. Raw datetime values also make the column
        index worth building (a DAY_BUCKET is a functional expression a
        plain index can't serve), so the whole_source gate below admits
        them.

        `direction` defaults per `order` if not given explicitly (matches
        the behavior before either was ever configurable): "desc" for
        count (most-common-first), "asc" for value (alphabetical / chrono
        ascending) — both are swappable independent of that default.

        An unfiltered root view skips the join and aggregates the member
        tables directly — see _grouping_covers_whole_source. That's not a
        micro-optimisation: the joined shape can't use a column index at
        all (verified with EXPLAIN QUERY PLAN — adding one leaves the plan
        byte-identical: SCAN vv, SEARCH s USING INTEGER PRIMARY KEY, USE
        TEMP B-TREE FOR GROUP BY, because the plan is driven from the view
        table and reaches the source by rid), whereas the direct shape gets
        the same covering-index scan column_values does. Group-by-a-column
        on a table you just opened is the common case, and on a big merge
        it's the one that hurts."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        by_tag = column == TAG_GROUP_COLUMN
        with self._reader() as ro:
            src = self._source_lite_on(ro, handle["source_id"])
            members = self._resolve_members_on(ro, handle["source_id"])
            # Only grouping by tag needs these, and only for its cheapest
            # shape (below): "how many rows carry no tag" over a whole
            # member table is that table's row count minus the number of
            # distinct tagged rids, both of which come out of metadata and
            # an index rather than a scan.
            member_rows = (
                {m["source_id"]: self._source_lite_on(ro, m["source_id"])["row_count"] for m in members}
                if by_tag else {}
            )
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        if not by_tag and column not in colnames:
            raise KeyError(column)
        is_datetime = not by_tag and colnames[column] == "datetime" and bucket_datetime

        # The two branches below reach the derived sidecar differently — the
        # direct shape joins it with USING(rid) (bare column refs), the view
        # shape chains a second ON join (refs go through the `d` alias) — so
        # the value expression and the path conditions are built per branch.
        def _val_expr(m: dict, derived_alias: str | None) -> str:
            ref = self._col_ref(self._ref_src_for(src, m), column, "s", derived_alias)
            return day_bucket_sql(ref) if is_datetime else ref

        def _path_for(m: dict, derived_alias: str | None) -> tuple[str, list]:
            clauses, p = self._path_where(path, colnames, "s", self._ref_src_for(src, m),
                                          derived_alias, m["source_id"])
            return "".join(f" AND {c}" for c in clauses), p

        has_path = bool(path)
        # The only columns a branch reads: the one grouped and the path's.
        uses = [column, *(p.get("column") for p in path or [] if isinstance(p, dict))]
        whole_source = not has_path and self._grouping_covers_whole_source(handle, src)
        # A root_virtual view has no v.view_N to join at all (it was never
        # materialised — see _build_virtual_root_view), path or no path, so
        # it always takes the direct-against-member-tables shape; a real
        # 'root' view only gets to skip the join when it's provably
        # unfiltered (whole_source, no path).
        direct = whole_source or handle.get("kind") == "root_virtual"
        if direct and not by_tag:
            # A datetime column groups by DAY_BUCKET(col), a functional
            # expression a plain index on the raw column can't serve — same
            # exclusion _ensure_column_index_building already documents for
            # _numeric_expr'd numeric comparisons. Only triggered for the
            # true whole_source (unfiltered, no path) case, same scope as
            # before this had to also cover root_virtual.
            if whole_source and not is_datetime:
                for m in members:
                    self._ensure_column_index_building(
                        m["source_id"], column, self._index_table_for(src, column, m["table_name"], m["source_id"])
                    )

        branches: list[str] = []
        params: list[Any] = []
        if by_tag:
            branches, params = self._tag_group_branches(view_id, src, members, direct, member_rows, _path_for,
                                                        uses)
        else:
            for m in members:
                derived_alias = None if direct else "d"
                extra_where, path_params = _path_for(m, derived_alias)
                if direct:
                    scope = f"FROM {self._member_from(src, m, 's', uses)} WHERE 1=1{extra_where}"
                else:
                    scope = (
                        f"FROM v.{q(view_id)} vv "
                        f"JOIN {self._member_join_target(src, m)} s ON s.rid = vv.rid AND vv.source_id = {int(m['source_id'])}"
                        f"{self._member_derived_join(self._ref_src_for(src, m), m, 's', uses)} "
                        f"WHERE 1=1{extra_where}"
                    )
                branches.append(f"SELECT {_val_expr(m, derived_alias)} AS val, count(*) AS n {scope} GROUP BY 1")
                params.extend(path_params)
        union_sql = " UNION ALL ".join(branches)
        if direction is None:
            direction = "asc" if order == "value" else "desc"
        dir_sql = "DESC" if direction == "desc" else "ASC"
        if order == "value":
            if by_tag:
                # "By value" for tags means by tag name — the label the
                # analyst actually sees — not by the id the group carries.
                order_sql = (
                    f"(SELECT name FROM tag_defs WHERE id = val) COLLATE NOCASE {dir_sql}"
                )
            elif colnames[column] == "number":
                order_sql = f"{_numeric_expr('val')} {dir_sql}"
            else:
                order_sql = f"val COLLATE NOCASE {dir_sql}"
        else:
            order_sql = f"n {dir_sql}"
        # _interruptible innermost: an interrupt becomes OpCancelled before
        # _dropped_view_is_expired can see the OperationalError, and _reader
        # then closes (not repools) the interrupted connection on the way out.
        with self._reader() as ro, self._dropped_view_is_expired(), self._interruptible(op_token, ro):
            # HAVING is a no-op for a column grouping (every branch row is
            # a real group of at least one row) and load-bearing for a tag
            # one: the untagged branch is a bare aggregate, so it emits a
            # zero on a source where every row is tagged.
            rows = ro.execute(
                f"SELECT val, SUM(n) AS n FROM ({union_sql}) GROUP BY val HAVING SUM(n) > 0 "
                f"ORDER BY {order_sql} LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {"groups": [{"value": r["val"], "count": r["n"]} for r in rows], "truncated": truncated}

    def distinct_values(self, view_id: str, column: str, limit: int = DISTINCT_VALUES_MAX,
                        op_token: str | None = None) -> dict:
        """Every distinct non-empty value of one column across the view, in
        the column's own sort order — what "Copy unique values" puts on the
        clipboard when no rows are picked.

        It is the header value picker's call to group_summary: the STORED
        value (bucket_datetime=False), because the list is meant to be
        pasted somewhere else and a day bucket is not a value the column
        holds, and order="value" so a number column sorts as numbers and a
        timestamp column chronologically. Merges and derived columns come
        with it, since group_summary already resolves both per member.

        NULL and '' are dropped — "(empty)" is not something anyone pastes
        into a lookup. They are separate groups to SQLite, so the aggregate
        asks for two past the cap to keep a list that is exactly `limit`
        long once they're gone from reading as truncated. `truncated` is
        the caller's cue to refuse rather than copy a list that stops
        partway."""
        if column == TAG_GROUP_COLUMN:
            # Grouping's sentinel, not a column — its "values" are tag ids.
            raise KeyError(column)
        res = self.group_summary(view_id, column, order="value", direction="asc", limit=limit + 2,
                                 op_token=op_token, bucket_datetime=False)
        values = [g["value"] for g in res["groups"] if g["value"] is not None and g["value"] != ""]
        return {"values": values[:limit], "truncated": res["truncated"] or len(values) > limit}

    # Bucket widths a histogram may pick from, seconds — chosen so the
    # view's span fits in max_buckets bars. Human-shaped (minute, hour,
    # day, week…) rather than a raw division, so bars line up with clock
    # boundaries and a brush lands on times an analyst can read.
    HISTOGRAM_BUCKETS = (1, 5, 10, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600,
                         12 * 3600, 86400, 7 * 86400, 30 * 86400, 365 * 86400)

    # How many values a value split may color: the categorical palette's
    # eight slots. A ninth would be a generated hue, and it belongs in
    # "Other" instead.
    HISTOGRAM_SPLIT_MAX = 8

    def time_histogram(self, view_id: str, column: str, max_buckets: int = 160,
                       op_token: str | None = None, stack: str | None = None,
                       split_column: str | None = None, split_values: list | None = None) -> dict:
        """Time buckets over the rows of the CURRENT view — filtered, searched,
        timeframe'd, exactly what the grid shows — for a datetime column.
        One aggregate pass on the reader pool (invariant #4), shaped like
        group_summary's column branch: a root_virtual/unfiltered view reads
        the member tables directly, a materialised one joins v.view_N; a
        merge unions its members.

        TS_NORMALIZE turns whatever the column stores into a sortable
        'YYYY-MM-DD HH:MM:SS…' (a derived datetime column already is one),
        and strftime('%s') turns that into epoch seconds — rows the
        normaliser can't parse have no epoch and fall out of the histogram
        rather than piling into bucket zero.

        **Each row's epoch is worked out once a pass, and in SQL.** Every
        SQLite statement here groups by something, and grouping sorts every
        row, so what a pass costs is mostly that sort; what decides how many
        passes there are is the bar width, which depends on the span. The
        width comes first, as cheaply as it can be had: the width this view
        was last drawn at (_histogram_widths — the strip asks about one view
        many times: the plain chart as it lands, then Tags, each Value,
        Groups, First N), or, on a whole table, one from the column's raw
        MIN/MAX (_histogram_guess_width). Then ONE pass groups by bar with
        MIN/MAX of the epoch alongside, and if the span it found calls for a
        different width — a remembered width after a derived column filled
        in, a guess the text order misled — one more pass draws the right
        bars. A filtered view with no width to try takes a MIN/MAX pass
        first. raw_epoch_sql reads the common ISO shape without TS_NORMALIZE
        (a Python call per row); a column whose first rows do not have that
        shape (_histogram_parsed_in_sql) would pay that call in every pass,
        so it takes one pass by exact second instead and the bars are folded
        here, in Python. On a 500k-row view: about 0.6 s against 1.15 s
        when every chart was the pass by second, the same answer
        (tests/test_histogram_value_split.py's pass-shape tests).

        The Tags view on a whole table with few rows tagged counts the
        tagged rows apart (see the comment at tags_apart): the first-tag
        lookup per row was a third of its pass.

        `stack` splits each bar. "tags" by the first tag a row carries
        (_histogram_tag_exprs). "value" and "groups" by which of
        `split_values` the row holds in `split_column` — up to
        HISTOGRAM_SPLIT_MAX of them, everything else in slot 0 — which is
        the same one-row-one-segment shape, since a row holds one value per
        column. The two differ only in how a datetime column is compared:
        "groups" names the group-by strip's values, which are DAY_BUCKETs
        there (group_summary), and "value" a cell's stored value. The day
        is worked out once a row, in the union, and the slot from it
        outside: a CASE naming DAY_BUCKET in each WHEN called it once per
        group, and grouping by a date took four times as long as by a
        name."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        with self._reader() as ro:
            src = self._source_lite_on(ro, handle["source_id"])
            members = self._resolve_members_on(ro, handle["source_id"])
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        if column not in colnames:
            raise KeyError(column)
        if colnames[column] != "datetime":
            raise ValueError(f"{column} is not a datetime column")
        direct = self._grouping_covers_whole_source(handle, src) or handle.get("kind") == "root_virtual"
        is_derived = self._is_derived(src, column)
        by_values = stack in ("value", "groups")
        stacked = stack == "tags" or by_values
        split_day = False
        if by_values:
            if split_column == TAG_GROUP_COLUMN:
                raise ValueError("Grouped by tag — the Tags view colors these")
            if split_column not in colnames:
                raise KeyError(split_column)
            split_values = list(split_values or [])
            if not 1 <= len(split_values) <= self.HISTOGRAM_SPLIT_MAX:
                raise ValueError(f"A value split takes 1 to {self.HISTOGRAM_SPLIT_MAX} values")
            split_day = stack == "groups" and colnames[split_column] == "datetime"
        uses = (column, split_column) if by_values else (column,)

        def slot_of(expr: str) -> str:
            # Slot 1..N for the value a row holds, 0 for anything else. IS,
            # not =, so the "(empty)" group's None finds its NULL rows.
            whens = " ".join(f"WHEN {expr} IS ? THEN {i + 1}" for i in range(len(split_values)))
            return f"CASE {whens} ELSE 0 END"

        def split_expr(m: dict) -> str:
            ref = self._col_ref(self._ref_src_for(src, m), split_column, "s", None if direct else "d")
            # A date's day as the union's own column (`sv`), compared
            # outside; anything else is a column read, cheap to name N times.
            return day_bucket_sql(ref) if split_day else slot_of(ref)

        def branch(m: dict, select: str, tagged: bool = False) -> str:
            derived_alias = None if direct else "d"
            rsrc = self._ref_src_for(src, m)
            ref = self._col_ref(rsrc, column, "s", derived_alias)
            epoch = epoch_sql(ref) if is_derived else raw_epoch_sql(ref)
            if direct and tagged:
                # Each tagged row once, as `t.tag` under its first tag (the
                # lowest id, as in _histogram_tag_exprs), then the row by
                # rid. CROSS JOIN keeps row_tags outside; `trid` keeps a
                # USING(rid) sidecar join in _member_from unambiguous.
                scope = (f"FROM (SELECT rid AS trid, MIN(tag_id) AS tag FROM row_tags "
                         f"WHERE source_id = {int(m['source_id'])} GROUP BY rid) t "
                         f"CROSS JOIN {self._member_from(src, m, 's', uses)} WHERE s.rid = t.trid")
            elif direct:
                scope = f"FROM {self._member_from(src, m, 's', uses)}"
            else:
                scope = (f"FROM v.{q(view_id)} vv "
                         f"JOIN {self._member_join_target(src, m)} s ON s.rid = vv.rid AND vv.source_id = {int(m['source_id'])}"
                         f"{self._member_derived_join(rsrc, m, 's', uses)}")
            return f"SELECT {select.replace('EPOCH', epoch)} {scope}"

        # Distinct seconds in order, as flat int arrays rather than a list
        # of row tuples: a view whose every row is its own second holds as
        # many of these as it has rows. Untagged is 0 — tag ids start at 1.
        secs, tags, counts = array("q"), array("q"), array("q")
        width_key = (view_id, column, max_buckets)
        with self._histogram_widths_lock:
            hint = self._histogram_widths.get(width_key)
        with self._reader() as ro, self._dropped_view_is_expired(), self._interruptible(op_token, ro):
            # The same rows asked the same question have the same answer: a
            # sort, or a filter cleared back to the whole table, is a new
            # view of rows already counted, and was a whole pass again
            # (2.2 s on 2.1M rows, each time). Not for a derived column,
            # whose values can still be filling in; the tag split also
            # keys on the tag generation, read before the pass so a write
            # that lands during it can only make the answer newer than its
            # key.
            answer_key = None
            if not is_derived and not (by_values and self._is_derived(src, split_column)):
                rows_key = self._histogram_rows_key(ro, view_id, handle, src, members, direct)
                if rows_key is not None:
                    answer_key = (rows_key, column, max_buckets, stack,
                                  split_column if by_values else None,
                                  json.dumps(split_values) if by_values else None,
                                  self._state_generation_on(ro) if stack == "tags" else None)
                    with self._histogram_widths_lock:
                        cached = self._histogram_results.pop(answer_key, None)
                        if cached is not None:
                            self._histogram_results[answer_key] = cached
                    if cached is not None:
                        return copy.deepcopy(cached)
            # Tags over a whole table where few of its rows carry one: the
            # bars without the split, then the tagged rows by themselves,
            # reached from row_tags, and untagged is what each bar has left.
            # Looking up every row's first tag was a third of the pass, and
            # a table holds far fewer findings than rows. Past a third of
            # the rows tagged, the lookup in the one pass is cheaper again.
            # Not for a derived column: one still filling in can change
            # between the two passes, and the tagged rows' bars must be
            # bars the first pass found.
            tagged_n = (self._histogram_tagged_count(ro, members, src["row_count"] // 3 + 1)
                        if stack == "tags" and direct and not is_derived else None)
            tags_apart = tagged_n is not None and tagged_n <= src["row_count"] // 3
            split_in_pass = stacked and not tags_apart
            if by_values:
                tag_of = {m["source_id"]: split_expr(m) for m in members}
                # The day's slot is worked out outside the union, so its
                # values are bound once rather than once per member.
                params = list(split_values) if split_day else [v for _ in members for v in split_values]
            else:
                tag_of = self._histogram_tag_exprs(ro, members) if split_in_pass else {}
                params = []
            union = " UNION ALL ".join(
                branch(m, "EPOCH AS e" + (f", {tag_of[m['source_id']]} AS {'sv' if split_day else 'tag'}"
                                          if split_in_pass else ""))
                for m in members)
            tag = slot_of("sv") if by_values and split_day else "tag"
            # LIMIT -1 limits nothing; it is what keeps EPOCH to one call
            # per row. Without it SQLite flattens a one-member subquery
            # into this query, or pushes the IS NOT NULL down into each
            # branch of a merge's union, and either way copies the
            # expression to every place `e` is named — two or three
            # TS_NORMALIZE calls a row, counted. Neither is allowed past a
            # LIMIT, since moving a WHERE across one changes what it means.
            rows_from = f"FROM ({union} LIMIT -1) WHERE e IS NOT NULL"
            split = f", {tag}" if split_in_pass else ""
            keys = "1, 2" if split_in_pass else "1"

            def take(rows, n_at: int) -> None:
                for row in rows:
                    secs.append(row[0])
                    counts.append(row[n_at])
                    if split_in_pass:
                        tags.append(row[1] or 0)

            def by_bar(width: int, span: bool = False, split: str = split, keys: str = keys,
                       rows_from: str = rows_from) -> list:
                # Floored like the fold below, not truncated: SQLite's %
                # keeps the sign of a pre-1970 epoch.
                return ro.execute(f"SELECT e - ((e % {width}) + {width}) % {width}{split}, COUNT(*)"
                                  f"{', MIN(e), MAX(e)' if span else ''} {rows_from} "
                                  f"GROUP BY {keys} ORDER BY {keys}", params).fetchall()

            in_sql = is_derived or self._histogram_parsed_in_sql(ro, src, members, column)
            # A width to try: this view's last one, or for a whole table
            # read in SQL, one from the column's raw MIN/MAX — a scan of
            # one column with nothing parsed, a fifteenth of a pass.
            width = hint or (self._histogram_guess_width(ro, src, members, column, max_buckets)
                             if in_sql and direct else None)
            if width:
                # One pass by bar, with the true span alongside to check
                # the width against: right, it is the answer; wrong, the
                # span it found is what the second pass needs.
                rows = by_bar(width, span=True)
                if rows:
                    lo, hi = min(r[-2] for r in rows), max(r[-1] for r in rows)
                    right = self._histogram_width(hi - lo, max_buckets)
                    if right == width:
                        take(rows, -3)
                    else:
                        take(by_bar(right), -1)
            elif in_sql:
                # The span first, then the bars: two scans of SQL arithmetic
                # and a few hundred rows back, where one pass by second
                # sends Python every distinct second to fold.
                spans = " UNION ALL ".join(branch(m, "EPOCH AS e") for m in members)
                lo, hi = ro.execute(f"SELECT MIN(e), MAX(e) FROM ({spans} LIMIT -1)").fetchone()
                if lo is not None:
                    take(by_bar(self._histogram_width(hi - lo, max_buckets)), -1)
            else:
                # Parsed in Python, a row is parsed once: one pass by second.
                take(ro.execute(f"SELECT e{split}, COUNT(*) {rows_from} GROUP BY {keys} ORDER BY {keys}", params), -1)
                if secs:
                    lo, hi = secs[0], secs[-1]
            if not secs:
                return {"column": column, "bucket_seconds": 0, "start": None, "end": None,
                        "total": 0, "buckets": []}
            bucket = self._histogram_width(hi - lo, max_buckets)
            apart = []
            if tags_apart and tagged_n:
                tagged = " UNION ALL ".join(branch(m, "EPOCH AS e, t.tag AS tag", tagged=True) for m in members)
                apart = by_bar(bucket, split=", tag", keys="1, 2",
                               rows_from=f"FROM ({tagged} LIMIT -1) WHERE e IS NOT NULL")
            # The labels come from SQLite too, not time.gmtime: on Windows
            # that raises OSError for anything before 1970 (a 500 for a
            # column holding FILETIME zero), and SQLite formats every year
            # its own strftime('%s') could have produced.
            lo_text, hi_text = ro.execute(
                "SELECT datetime(?, 'unixepoch'), datetime(?, 'unixepoch')", (lo, hi)).fetchone()
        with self._histogram_widths_lock:
            self._histogram_widths[width_key] = bucket
            while len(self._histogram_widths) > 64:
                del self._histogram_widths[next(iter(self._histogram_widths))]
        # A bucket starts at floor(e / bucket) * bucket, which is what the
        # strip assumes when it finds a row's bar (b <= t < b + bucket).
        # Python's // floors; SQLite's integer division truncates toward
        # zero, which bucketed a pre-1970 (negative) epoch one bar late
        # while this was done in SQL — and 1601, FILETIME zero, is a value
        # real artefacts carry.
        per_bucket: dict[int, int] = {}
        for e, n in zip(secs, counts):
            b = e // bucket * bucket
            per_bucket[b] = per_bucket.get(b, 0) + n
        out = {"column": column, "bucket_seconds": bucket,
               "start": lo_text, "end": hi_text, "total": sum(counts),
               "buckets": [[b, per_bucket[b]] for b in sorted(per_bucket)]}
        if by_values:
            out["stack"] = {"kind": stack, "column": split_column, "values": split_values,
                            "buckets": self._histogram_slot_buckets(
                                secs, tags, counts, bucket, len(split_values) + 1)}
        elif stacked:
            if tags_apart:
                secs, tags, counts = self._histogram_untagged_rest(per_bucket, apart)
            out["stack"] = self._histogram_tag_stack(secs, tags, counts, bucket)
        if answer_key is not None:
            with self._histogram_widths_lock:
                self._histogram_results[answer_key] = copy.deepcopy(out)
                while len(self._histogram_results) > self.HISTOGRAM_ANSWERS_KEPT:
                    del self._histogram_results[next(iter(self._histogram_results))]
        return out

    # How many time_histogram answers _histogram_results keeps. One is a
    # few KB (at most 160 bars, nine segments each), and the strip asks a
    # handful per view: the plain chart, Tags, a Value or two.
    HISTOGRAM_ANSWERS_KEPT = 32

    def _histogram_rows_key(self, ro: sqlite3.Connection, view_id: str, handle: dict, src: dict,
                            members: list[dict], direct: bool) -> tuple | None:
        """Which rows a view holds, as a key two views holding the same
        rows share: what _histogram_results files time_histogram's answers
        under, so a new view of rows already counted needs no pass.

        A view over every row of its source (`direct`) is that source's
        members at their row count. Nothing else can change which rows
        that is: a table's rows never change once imported (invariant #1), ids
        are never reused (AUTOINCREMENT), and a merge edited to other
        members names other ids. A root view holding some of the rows is
        fingerprinted from v.view_N instead — its row count and two sums of
        a hash of (source_id, rid), a 64-bit chance of two row sets
        agreeing by accident — once, since a built view's rows never
        change; 0.23 s on 2.1M rows, a tenth of the pass it can save. A
        group sub-view or the timeline is not cached (None)."""
        if direct:
            return ("all", handle["source_id"], tuple(m["source_id"] for m in members), src["row_count"])
        if handle.get("kind") != "root":
            return None
        key = handle.get("rows_key")
        if key is None:
            n, a, b = ro.execute(
                "SELECT COUNT(*), SUM((rid * 2147483629 + source_id * 1500450271) % 4294967291), "
                f"SUM((rid * 1500450271 + source_id * 2147483629) % 4294967279) FROM v.{q(view_id)}"
            ).fetchone()
            key = handle["rows_key"] = ("some", handle["source_id"], n, a, b)
        return key

    def _state_generation_on(self, ro: sqlite3.Connection) -> str:
        """STATE_GENERATION_KEY as `ro` sees it: the counter every tag
        write bumps (_bump_state_generation)."""
        row = ro.execute("SELECT value FROM case_settings WHERE key=?",
                         (self.STATE_GENERATION_KEY,)).fetchone()
        return row["value"] if row else "0"

    def _histogram_parsed_in_sql(self, ro: sqlite3.Connection, src: dict, members: list[dict],
                                 column: str) -> bool:
        """Whether SQLite reads this column's timestamps itself
        (ts_normalize_sql's fast path) rather than handing them to
        TS_NORMALIZE, judged on the first rows of each member. A guess
        about cost only: a column that strays later is still parsed right,
        just in Python, twice."""
        for m in members:
            ref = self._col_ref(self._ref_src_for(src, m), column, "s", None)
            stray = ro.execute(
                f"SELECT 1 FROM (SELECT {ref} AS v FROM {self._member_from(src, m, 's', (column,))} LIMIT 256) "
                f"WHERE v IS NOT NULL AND NOT (typeof(v) = 'text' AND v GLOB '{_TS_ISO_GLOB}') LIMIT 1").fetchone()
            if stray:
                return False
        return True

    def _histogram_guess_width(self, ro: sqlite3.Connection, src: dict, members: list[dict],
                               column: str, max_buckets: int) -> int | None:
        """A bar width from the column's raw MIN/MAX — text order, which is
        time order for the ISO values _histogram_parsed_in_sql found — or
        None when either end does not read as a timestamp. A guess:
        time_histogram checks it against the span its pass finds."""
        ends = []
        for m in members:
            ref = self._col_ref(self._ref_src_for(src, m), column, "s", None)
            ends += ro.execute(
                f"SELECT MIN({ref}), MAX({ref}) FROM {self._member_from(src, m, 's', (column,))}").fetchone()
        epochs = []
        for v in ends:
            ts = _ts_normalize(v) if v is not None else None
            try:
                epochs.append(calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S")))
            except (TypeError, ValueError, OverflowError):
                return None
        return self._histogram_width(max(epochs) - min(epochs), max_buckets)

    def _histogram_width(self, span: int, max_buckets: int) -> int:
        """The bucket width for a span: the narrowest that fits it in
        max_buckets bars."""
        return next((b for b in self.HISTOGRAM_BUCKETS if span / b <= max_buckets),
                    self.HISTOGRAM_BUCKETS[-1])

    @staticmethod
    def _histogram_tagged_count(ro: sqlite3.Connection, members: list[dict], cap: int) -> int:
        """How many tags the members' rows carry, counted no further than
        `cap` — a bound on the tagged rows, which is all time_histogram
        needs to know, without counting every tag on a table full of
        them."""
        ids = ",".join(str(int(m["source_id"])) for m in members)
        return ro.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM row_tags WHERE source_id IN ({ids}) "
                          f"LIMIT {int(cap)})").fetchone()[0]

    @staticmethod
    def _histogram_untagged_rest(per_bucket: dict[int, int], tagged: list) -> tuple[array, array, array]:
        """The plain bars and the tagged rows' (bar, tag, count) as the
        (bar, tag, count) rows _histogram_tag_stack folds, the untagged
        segment being what each bar has left once its tagged rows are
        taken out."""
        secs, tags, counts = array("q"), array("q"), array("q")
        rest = dict(per_bucket)
        for b, t, n in tagged:
            secs.append(b)
            tags.append(t)
            counts.append(n)
            rest[b] -= n
        for b, n in rest.items():
            secs.append(b)
            tags.append(0)
            counts.append(n)
        return secs, tags, counts

    def _histogram_tag_exprs(self, ro: sqlite3.Connection, members: list[dict]) -> dict[int, str]:
        """Per member, the SQL for the tag a row is counted under in the
        histogram's tag split — the FIRST tag it carries in ribbon order,
        which is the lowest tag id, since the ribbon renders tag_defs in id
        order; NULL for an untagged row.

        **One row, one segment.** Untagged rows make the base segment, so a
        stacked bar is exactly as tall as the plain one and the stacked
        chart and the flat chart are the same chart, read two ways.
        Counting a two-tag row under both segments (which is what grouping
        by tag does, deliberately — see _tag_group_branches) would make bars
        taller than the rows they describe, and a height that stops meaning
        "rows" is not worth the extra truth about one tag's total: the
        ribbon already carries those numbers exactly.

        The per-row lookup is a correlated MIN over row_tags' primary key
        (source_id, rid, tag_id) — a prefix probe per row, not a join the
        planner can turn inside out. A member with no tags at all gets a
        bare NULL instead, the same short-circuit tag_counts_in_view makes,
        because that is the common case and it costs nothing to notice."""
        out = {}
        for m in members:
            sid = int(m["source_id"])
            has_tags = ro.execute("SELECT 1 FROM row_tags WHERE source_id=? LIMIT 1", (sid,)).fetchone()
            out[sid] = (f"(SELECT MIN(rt.tag_id) FROM row_tags rt WHERE rt.source_id = {sid} AND rt.rid = s.rid)"
                        if has_tags else "NULL")
        return out

    @staticmethod
    def _histogram_tag_stack(secs: array, tags: array, counts: array, bucket: int) -> dict:
        """time_histogram's (second, tag, count) rows folded into the same
        buckets as the plain chart, split by tag: {tags: [id…], buckets:
        [[epoch, [untagged, n_first_tag, …]], …]}, the tags in id order."""
        ids = sorted({t for t in tags if t})
        slot = {tid: i + 1 for i, tid in enumerate(ids)}
        by_bucket: dict[int, list[int]] = {}
        for e, t, n in zip(secs, tags, counts):
            split = by_bucket.setdefault(e // bucket * bucket, [0] * (len(ids) + 1))
            split[slot[t] if t else 0] += n
        return {"tags": ids,
                "buckets": [[b, by_bucket[b]] for b in sorted(by_bucket)]}

    @staticmethod
    def _histogram_slot_buckets(secs: array, slots: array, counts: array, bucket: int,
                                width: int) -> list:
        """A value split's (second, slot, count) rows in the plain chart's
        buckets: [[epoch, [other, n_value_1, …]], …]. The slot is already
        the position — the CASE that made it numbered the values in the
        order they were asked for."""
        by_bucket: dict[int, list[int]] = {}
        for e, s, n in zip(secs, slots, counts):
            by_bucket.setdefault(e // bucket * bucket, [0] * width)[s] += n
        return [[b, by_bucket[b]] for b in sorted(by_bucket)]

    def _tag_group_branches(self, view_id: str, src: dict, members: list[dict], direct: bool,
                            member_rows: dict[int, int], path_for,
                            uses: Iterable[str] | None = None) -> tuple[list[str], list]:
        """The UNION ALL branches behind grouping by tag: one group per tag,
        plus one for the untagged remainder. A row carrying two tags is
        counted under both — that's what makes "group by tag" answer the
        question analysts actually ask, and it's why these counts can sum to
        more than the view holds.

        Every shape here is written to pin the join order, because the
        obvious spellings do not survive the query planner. `v.view_N` is
        indexed on `pos` (its INTEGER PRIMARY KEY) and on nothing else, so
        reaching a view row by `rid` is a full scan of it. Given a
        `WHERE vv.source_id = ?` to work with, SQLite happily drives from
        row_tags' covering index and scans the entire view once per tagged
        row — measured at 150k x 300k row visits, minutes, on a case where
        the correct plan takes 200ms. So:

        - The view-scoped branches use `CROSS JOIN`, which SQLite documents
          as suppressing join reordering, to force the view to be the outer
          loop and row_tags to be probed by its (source_id, rid, tag_id)
          primary key.
        - They also drop the per-member `vv.source_id = ?` restriction
          entirely when there's no nested path: the ON clause already
          matches each view row against its own source's tags, so one
          view-wide pair of branches is both correct for a merge and
          cheaper than one pair per member.
        - The whole-table case (an unfiltered view, no path) never touches
          the source table at all: per-tag counts come from row_tags'
          own aggregate, and the untagged remainder is the member's row
          count minus its distinct tagged rids.
        - The source table, when a path needs it, is reached through a
          self-contained EXISTS rather than another join — nothing to
          reorder, and it works against _from_clause's USING(rid) shape
          without the alias juggling _derived_join exists for.
        """
        branches: list[str] = []
        params: list[Any] = []
        # Path conditions are compiled against _from_clause's USING(rid)
        # shape (derived_alias None) since the source table only ever
        # appears inside a standalone EXISTS here.
        per_member = [(m, *path_for(m, None)) for m in members]
        any_path = any(clause for _, clause, _ in per_member)

        if not direct and not any_path:
            branches.append(
                f"SELECT rt.tag_id AS val, count(*) AS n FROM v.{q(view_id)} vv "
                f"CROSS JOIN row_tags rt ON rt.source_id = vv.source_id AND rt.rid = vv.rid GROUP BY 1"
            )
            branches.append(
                f"SELECT NULL AS val, count(*) AS n FROM v.{q(view_id)} vv "
                f"WHERE NOT EXISTS (SELECT 1 FROM row_tags rt2 "
                f"WHERE rt2.source_id = vv.source_id AND rt2.rid = vv.rid)"
            )
            return branches, params

        for m, path_clause, path_params in per_member:
            sid = int(m["source_id"])
            member_from = self._member_from(src, m, "s", uses)
            if direct and not path_clause:
                branches.append(
                    f"SELECT tag_id AS val, count(*) AS n FROM row_tags WHERE source_id = {sid} GROUP BY 1"
                )
                branches.append(
                    f"SELECT NULL AS val, {int(member_rows.get(sid, 0))} - "
                    f"(SELECT count(DISTINCT rid) FROM row_tags WHERE source_id = {sid}) AS n"
                )
            elif direct:
                branches.append(
                    f"SELECT rt.tag_id AS val, count(*) AS n FROM row_tags rt WHERE rt.source_id = {sid} "
                    f"AND EXISTS (SELECT 1 FROM {member_from} WHERE s.rid = rt.rid{path_clause}) GROUP BY 1"
                )
                params.extend(path_params)
                branches.append(
                    f"SELECT NULL AS val, count(*) AS n FROM {member_from} WHERE 1=1{path_clause} "
                    f"AND NOT EXISTS (SELECT 1 FROM row_tags rt2 "
                    f"WHERE rt2.source_id = {sid} AND rt2.rid = s.rid)"
                )
                params.extend(path_params)
            else:
                in_scope = (
                    f"EXISTS (SELECT 1 FROM {member_from} "
                    f"WHERE s.rid = vv.rid AND vv.source_id = {sid}{path_clause})"
                )
                branches.append(
                    f"SELECT rt.tag_id AS val, count(*) AS n FROM v.{q(view_id)} vv "
                    f"CROSS JOIN row_tags rt ON rt.source_id = vv.source_id AND rt.rid = vv.rid "
                    f"WHERE {in_scope} GROUP BY 1"
                )
                params.extend(path_params)
                branches.append(
                    f"SELECT NULL AS val, count(*) AS n FROM v.{q(view_id)} vv WHERE {in_scope} "
                    f"AND NOT EXISTS (SELECT 1 FROM row_tags rt2 "
                    f"WHERE rt2.source_id = vv.source_id AND rt2.rid = vv.rid)"
                )
                params.extend(path_params)
        return branches, params

    def expand_group(self, view_id: str, column: str, value: Any, path: list[dict] | None = None) -> dict:
        """Measures the group's real size within the current filtered root
        view (never trusts a client-cached count — the outer filter may have
        changed), then picks a strategy: small single-source groups of an
        *unfiltered* parent page directly (no v.view_N churn); large groups,
        groups of a filtered parent, and any group on a merge get a real
        indexed sub-view so paging stays O(window) even for a single
        dominant value across hundreds of thousands of rows.

        A root_virtual parent (see _build_virtual_root_view) has no
        v.view_N to join — it's always a single, unfiltered source, so
        every query here goes straight at the member table instead, and a
        materialised sub-view's root_pos is just the member table's own
        rid (root_virtual's outer order is rid order by construction)."""
        root = self._views.get(view_id)
        if not root:
            raise ViewExpired("View expired — rebuild it")
        src = self._source_lite(root["source_id"])
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        by_tag = column == TAG_GROUP_COLUMN
        if not by_tag and column not in colnames:
            raise KeyError(column)

        members = self._resolve_members(root["source_id"])
        is_root_virtual = root.get("kind") == "root_virtual"
        covers_source = self._grouping_covers_whole_source(root, src)
        # A parent holding every row (root_virtual, or a root view that is
        # only sorted) is the member tables themselves as far as which rows
        # are in the group goes, so they are read directly, in their own
        # order. Through the view it was one rid lookup per row in the
        # view's order — random access over the whole table on any sort
        # but rid's: 30 s to expand a day on a 4.3M-row MFTECmd table
        # sorted by a timestamp, 2 s directly.
        direct = is_root_virtual or covers_source
        # Same split group_summary makes: the direct-against-the-table shape
        # picks the derived sidecar up through _from_clause's USING(rid)
        # (bare refs), the view-join shape chains a second ON join and
        # refers to it by alias.
        d_alias = None if direct else "d"
        uses = [column, *(p.get("column") for p in path or [] if isinstance(p, dict))]

        def _conds(m: dict) -> tuple[str, list]:
            """This group's predicate for one member. Per member because
            both halves can be: a tag test is keyed by source_id (row_tags
            is), and so is any tag level in the nested path."""
            rsrc = self._ref_src_for(src, m)
            if by_tag:
                c, v = self._tag_condition("s", m["source_id"], value)
            else:
                c, v = self._eq_condition(
                    self._col_ref(rsrc, column, "s", d_alias), value, colnames[column] == "datetime"
                )
            pc, pp = self._path_where(path, colnames, "s", rsrc, d_alias, m["source_id"])
            return f"{c}" + "".join(f" AND {x}" for x in pc), [*v, *pp]

        # Both locked blocks below read v.<parent view>, whose handle was
        # taken unlocked above — a rebuild of the same source can evict it in
        # either window. Same contract as every other view read: expired, so
        # a 409 the frontend rebuilds on, not a 500.
        with self.lock, self._dropped_view_is_expired():
            if direct and not is_root_virtual:
                # Counted without reading the parent, which still has to
                # exist for the group to mean anything.
                self.db.execute(f"SELECT 1 FROM v.{q(view_id)} LIMIT 0")
            total = 0
            for m in members:
                where_sql, where_params = _conds(m)
                if direct:
                    n = self.db.execute(
                        f"SELECT count(*) FROM {self._member_from(src, m, 's', uses)} WHERE {where_sql}",
                        where_params,
                    ).fetchone()[0]
                else:
                    n = self.db.execute(
                        f"SELECT count(*) FROM v.{q(view_id)} vv JOIN {self._member_join_target(src, m)} s "
                        f"ON s.rid = vv.rid AND vv.source_id = ?{self._member_derived_join(self._ref_src_for(src, m), m, 's', uses)} WHERE {where_sql}",
                        [m["source_id"], *where_params],
                    ).fetchone()[0]
                total += n

        # The virtual fast path reads straight off the member table with
        # nothing but column=value (+ the nested path) — _virtual_group_where
        # has no view to join and so no way to express the parent's own
        # filters/search/timeframe. It is therefore only correct when the
        # parent provably holds every row of the source; a filtered parent
        # has to materialise. The failure it prevents is quiet, because the
        # counts come from the other side: group_summary and `total` above
        # both join the view and stay right, so the grid asks for row_count
        # rows and gets the first row_count of a *longer*, unfiltered list —
        # rows the filter excluded, rendered under a correct count. Tag and
        # export on the group read the same way, which makes it an
        # over-tagging bug too, not just a display one.
        is_merge = bool(src.get("is_merge"))
        # A tag level is the other thing the fast path can't hold still.
        # Its predicate is re-run against row_tags on every page fetch,
        # while row_count was fixed here — so tagging a row inside the
        # expanded "untagged" group shrank the membership under the next
        # page's offset and the grid skipped a row it never showed. Column
        # values are source data and never change (invariant #1); tags do,
        # constantly, and in exactly this view. Materialising snapshots
        # membership the way a tag *filter* already does.
        by_tag_anywhere = by_tag or any(
            isinstance(p, dict) and p.get("column") == TAG_GROUP_COLUMN for p in (path or []))
        if (is_merge or not covers_source or by_tag_anywhere
                or total > self.GROUP_MATERIALIZE_THRESHOLD):
            vid = self._next_view_id()
            branches = []
            params: list[Any] = []
            for m in members:
                where_sql, where_params = _conds(m)
                if is_root_virtual:
                    branches.append(
                        f"SELECT s.rid AS root_pos, {int(m['source_id'])} AS source_id, s.rid AS rid "
                        f"FROM {self._member_from(src, m, 's', uses)} WHERE {where_sql}"
                    )
                    params.extend(where_params)
                elif direct:
                    # The parent's order still comes from the view, which
                    # is read in that order once, against the group's rids
                    # gathered from the member (an uncorrelated IN, so one
                    # pass over each).
                    branches.append(
                        f"SELECT vv.pos AS root_pos, vv.source_id, vv.rid FROM v.{q(view_id)} vv "
                        f"WHERE vv.source_id = ? AND vv.rid IN "
                        f"(SELECT s.rid FROM {self._member_from(src, m, 's', uses)} WHERE {where_sql})"
                    )
                    params.extend([m["source_id"], *where_params])
                else:
                    branches.append(
                        f"SELECT vv.pos AS root_pos, vv.source_id, vv.rid FROM v.{q(view_id)} vv "
                        f"JOIN {self._member_join_target(src, m)} s ON s.rid = vv.rid AND vv.source_id = ?{self._member_derived_join(self._ref_src_for(src, m), m, 's', uses)} "
                        f"WHERE {where_sql}"
                    )
                    params.extend([m["source_id"], *where_params])
            union_sql = " UNION ALL ".join(branches)
            with self.lock:
                with self._dropped_view_is_expired(), self.db:
                    # The parent can have been rebuilt away since its
                    # handle was read; a root_virtual one has no table
                    # whose absence would say so, so ask the registry.
                    if view_id not in self._views:
                        raise ViewExpired("View expired — rebuild it")
                    # Same ORDER BY-fed insert as build_view: pos auto-assigns
                    # 1..N in insertion (= sorted) order, rowcount replaces a
                    # count(*) re-scan. root_pos is unique across the union
                    # (vv.pos from one view, or s.rid from one member table),
                    # so the order is fully determined.
                    self.db.execute(f"CREATE TABLE v.{q(vid)} (pos INTEGER PRIMARY KEY, source_id INTEGER, rid INTEGER)")
                    n = self.db.execute(
                        f"INSERT INTO v.{q(vid)}(source_id, rid) "
                        f"SELECT source_id, rid FROM ({union_sql}) ORDER BY root_pos",
                        params,
                    ).rowcount
                # Registered before the lock goes, like build_view's handle:
                # a rebuild of the parent that took the lock first would
                # have cascaded over a child it couldn't see yet, leaving a
                # group (and its table) under a parent that no longer exists.
                self._views[vid] = {
                    "view_id": vid, "source_id": root["source_id"], "row_count": n,
                    "kind": "group", "parent_view_id": view_id,
                }
            return {"view_id": vid, "row_count": n, "elapsed_ms": 0}

        # Same rule for the virtual group, which has no table but does have
        # a parent: registered under the lock, and only while that parent
        # is still the registered view it was counted against.
        with self.lock:
            if view_id not in self._views:
                raise ViewExpired("View expired — rebuild it")
            vid = self._next_view_id()
            self._views[vid] = {
                "view_id": vid, "source_id": root["source_id"], "row_count": total,
                "kind": "group_virtual", "parent_view_id": view_id,
                "column": column, "value": value, "members": members, "path": path,
            }
        return {"view_id": vid, "row_count": total, "elapsed_ms": 0}

    def _virtual_group_where(self, handle: dict, conn: sqlite3.Connection | None = None) -> tuple[str, list]:
        """WHERE fragment (unqualified column names, against the member
        table directly) for a group_virtual handle's own column=value plus
        every outer path level already fixed by nested grouping — shared by
        every place that reads straight off the backing member table for a
        small, ungathered group. `conn` lets a _reader() caller resolve the
        member's column types on its own connection; writer-side callers
        (tag/export on a group) omit it and go through the lock as before."""
        column, value = handle["column"], handle["value"]
        member_sid = handle["members"][0]["source_id"]
        src = self._source_lite_on(conn, member_sid) if conn is not None else self._source_lite(member_sid)
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        # Unqualified names throughout: every caller runs this against the
        # member table joined to its sidecar with USING(rid), where a base
        # and a derived column are both addressable bare.
        if column == TAG_GROUP_COLUMN:
            cond_sql, cond_val = self._tag_condition("", member_sid, value)
        else:
            cond_sql, cond_val = self._eq_condition(q(column), value, colnames[column] == "datetime")
        path_clauses, path_params = self._path_where(handle.get("path"), colnames, "", src, None, member_sid)
        extra = "".join(f" AND {c}" for c in path_clauses)
        return f"{cond_sql}{extra}", [*cond_val, *path_params]

    @staticmethod
    def _compile_condition(col: str, op: str, val: Any, colnames: dict,
                           strict: bool = False) -> tuple[str, list]:
        """Compile one column/op/value condition into a parameterized clause.
        Shared by the flat quick-filter list and the guided filter-tree's
        'cond' leaves. Returns ("", []) for an unknown op, or an empty value
        on ops that require one — an empty filter box means "no filter".

        A column this table does not have is a different thing entirely, and
        `strict` says so. Dropping such a condition silently BROADENS the
        result: the analyst is shown more rows than they asked for, under a
        filter chip saying the filter is on. Saved filters are offered
        across cases on a column-overlap heuristic, so this is reachable by
        design, and in a triage tool "more rows than you asked for, with no
        warning" is the worst possible failure."""
        if col in Store.PHYSICAL_COLUMNS:
            colnames = {**colnames, col: Store.PHYSICAL_COLUMNS[col]}
        if col not in colnames:
            if strict and col:
                raise UnknownFilterColumn(col)
            return "", []
        c = q(col)
        numeric = colnames[col] == "number"
        if op == "contains":
            if val == "":
                return "", []
            return f"{c} LIKE ? ESCAPE '\\'", [f"%{_esc_like(val)}%"]
        if op == "not_contains":
            if val == "":
                return "", []
            return f"({c} NOT LIKE ? ESCAPE '\\' OR {c} IS NULL)", [f"%{_esc_like(val)}%"]
        if op == "equals":
            if val == "":
                return "", []
            return f"{c} = ?", [val]
        if op == "not_equals":
            if val == "":
                return "", []
            return f"({c} <> ? OR {c} IS NULL)", [val]
        if op == "starts":
            if val == "":
                return "", []
            return f"{c} LIKE ? ESCAPE '\\'", [f"{_esc_like(val)}%"]
        if op == "regex":
            if val == "":
                return "", []
            _compiled_regex(val)   # refuse an unusable pattern here, by name — see _regexp
            return f"{c} REGEXP ?", [val]
        if op in (">", ">=", "<", "<="):
            if val == "":
                return "", []
            lhs = _numeric_expr(c) if numeric else c
            return f"{lhs} {op} ?", [float(val) if numeric else val]
        if op == "empty":
            return f"({c} IS NULL OR {c} = '')", []
        if op == "not_empty":
            return f"({c} IS NOT NULL AND {c} <> '')", []
        if op == "in":
            # An empty string in a LIST is the empty cell — the box's
            # `a|""`, the value picker's "(empty)" ticked beside real
            # values — and means what `empty` means, NULL included, which
            # `IN` alone can never match. It used to be dropped, so the
            # rows it named silently fell out of the filter. A
            # newline-separated string still drops its blank lines: there
            # a blank is a stray newline, not a value anyone picked.
            with_empty = isinstance(val, list) and "" in val
            items = [s for s in (val if isinstance(val, list) else str(val).split("\n")) if s != ""]
            empty_sql = f"{c} IS NULL OR {c} = ''"
            if not items:
                return (f"({empty_sql})", []) if with_empty else ("", [])
            if col in Store.PHYSICAL_COLUMNS:
                # Row ids arrive as strings (the tree is JSON from a picker);
                # bind them as the integers the column holds.
                items = [int(s) if str(s).strip().lstrip("-").isdigit() else s for s in items]
            if _HAS_JSON_EACH:   # one parameter however long the list — see _HAS_JSON_EACH
                sql, params = f"{c} IN (SELECT value FROM json_each(?))", [json.dumps(items)]
            else:
                sql, params = f"{c} IN ({','.join('?' * len(items))})", items
            return (f"({sql} OR {empty_sql})", params) if with_empty else (sql, params)
        return "", []

    @staticmethod
    def _raw_fragments(node: dict | None) -> list[str]:
        if not node:
            return []
        if node.get("type") == "raw":
            return [node.get("sql", "")]
        if node.get("type") == "group":
            return [f for c in node.get("children", []) for f in Store._raw_fragments(c)]
        return []

    @staticmethod
    def _tree_has_raw(node: dict | None) -> bool:
        """True if any node in the filter tree is a raw-SQL fragment. Raw
        nodes assume a single table_name for their EXPLAIN QUERY PLAN
        validation, so they're rejected for merges rather than extended to
        dry-run per member."""
        if not node:
            return False
        if node.get("type") == "raw":
            return True
        if node.get("type") == "group":
            return any(Store._tree_has_raw(c) for c in node.get("children", []))
        return False

    def _index_table_for(self, src: dict, column: str, table_name: str | None = None,
                         member_id: int | None = None) -> str:
        """Which physical table an auto-created index for this column
        belongs on — a derived column's values live in the drv_<id>
        sidecar, not in src_<id>. `table_name` is the caller's already
        resolved member table (a merge's src has none of its own), and
        `member_id` names the member whose sidecar carries a merge's
        derived values (drv_<-3> doesn't exist)."""
        for c in src["columns"]:
            if c.get("derived") and c["name"].lower() == str(column).lower():
                return self._derived_table(member_id if member_id is not None else src["id"])
        return table_name or src["table_name"]

    def _compile_tree(self, node: dict | None, colnames: dict, source_id: int, src: dict) -> tuple[str, list]:
        """Compile a guided filter-builder tree (group/cond/raw nodes) into a
        parameterized WHERE fragment."""
        if not node:
            return "", []
        kind = node.get("type")
        if kind == "group":
            parts: list[str] = []
            params: list[Any] = []
            for child in node.get("children", []):
                c, p = self._compile_tree(child, colnames, source_id, src)
                if c:
                    parts.append(c)
                    params.extend(p)
            if not parts:
                return "", []
            joiner = " OR " if node.get("op") == "OR" else " AND "
            return "(" + joiner.join(parts) + ")", params
        if kind == "raw":
            sql = node.get("sql", "")
            # Re-validated on every compile, not just at authoring time — a
            # stored preset may be replayed later against a different schema.
            # A merge member's compile skips it: build_view/spec_sql validate
            # each fragment once against the merge itself (_member_read_src).
            if "merge_id" not in src:
                self.validate_where_fragment(source_id, sql)
            return f"({sql})", []
        if kind == "cond":
            col, op = node.get("column"), node.get("op")
            c, p = self._compile_condition(col, op, node.get("value", ""), colnames, strict=True)
            if c and op in SARGABLE_OPS and col in colnames:
                self._ensure_column_index_building(source_id, col, self._index_table_for(src, col))
            return c, p
        return "", []

    FORBIDDEN_SQL_RE = re.compile(
        r"\b(select|insert|update|delete|drop|alter|create|replace|reindex|"
        r"union|attach|detach|pragma|vacuum|exec)\b",
        re.IGNORECASE,
    )
    ALLOWED_SQL_WORDS = {
        "AND", "OR", "NOT", "IS", "NULL", "IN", "BETWEEN", "LIKE", "GLOB", "REGEXP", "ESCAPE",
        "CAST", "REAL", "TEXT", "INTEGER", "COALESCE", "LENGTH", "LOWER", "UPPER", "SUBSTR",
        "TRIM", "TRUE", "FALSE",
    }

    # Columns every source table has beyond its own column list — the row
    # id. One definition, read by the raw-fragment validator and the
    # guided condition compiler alike, so `rid` is a column everywhere or
    # nowhere (the session diff opens "these rows" as a rid IN condition).
    PHYSICAL_COLUMNS = {"rid": "number"}

    def validate_where_fragment(self, source_id: int, fragment: str) -> None:
        """Raise ValueError with a human-readable reason for anything that is
        not a safe, side-effect-free boolean expression over this source's
        own columns.

        The actual safety boundary is that this fragment is only ever spliced
        into `WHERE (<fragment>)` against a FROM clause the caller cannot
        influence (always this source's own table) — user text never
        controls FROM/JOIN. Blacklisting SELECT closes the one way that
        boundary could be defeated (a subquery reading row_tags,
        sqlite_master, or another source's table). The identifier allowlist
        and the EXPLAIN QUERY PLAN dry-run below are good defense-in-depth
        and produce better error messages, but are not by themselves a
        sufficient boundary — regex-based SQL sanitization has known edge
        cases, so raw mode's blast radius is kept small by construction
        (no SELECT at all, ever) rather than by trying to perfect this list.
        """
        frag = (fragment or "").strip()
        if not frag:
            raise ValueError("Empty expression")
        if frag.count("(") != frag.count(")"):
            raise ValueError("Unbalanced parentheses")
        for qc in ("'", '"'):
            if (frag.count(qc) - 2 * frag.count(qc * 2)) % 2 != 0:
                raise ValueError("Unbalanced quotes")

        # Blank out quoted-literal contents before keyword/identifier scanning
        # so a forensic value like CommandLine = 'SELECT * FROM users' isn't
        # mistaken for a SELECT statement, and a value like 'Sysmon' isn't
        # mistaken for a bare identifier.
        # NOT comment-stripped: this validator REJECTS comments outright a
        # line below, and stripping them first would blank the very markers
        # it looks for. (The SQL pane, where comments are legal, strips them
        # before its keyword scan instead — see run_sql.)
        structural = _blank_string_literals(frag)
        if ";" in structural or "--" in structural or "/*" in structural or "*/" in structural:
            raise ValueError("Statement separators and comments are not allowed")
        if self.FORBIDDEN_SQL_RE.search(structural):
            raise ValueError("Only a boolean filter expression is allowed — no SELECT/PRAGMA/ATTACH/etc.")

        src = self.get_source(source_id)
        colnames = {c["name"] for c in src["columns"]} | set(self.PHYSICAL_COLUMNS)
        for ident in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", structural):
            if ident.upper() in self.ALLOWED_SQL_WORDS or ident in colnames or NUM_RE.match(ident):
                continue
            raise ValueError(f"Unknown identifier: {ident}")
        # A double-quoted name has to be a column too. SQLite reads one that
        # isn't as a string literal (its double-quoted-string fallback), and
        # EXPLAIN below accepts that happily: `"Event Id " <> '4624'` — one
        # trailing space — matched every row, `"EventID" = '4624'` none,
        # and neither said why. Case folds as SQLite folds it, ASCII only.
        folded = {_ascii_fold(c) for c in colnames}
        for name in _double_quoted_names(frag):
            if _ascii_fold(name) not in folded:
                raise ValueError(
                    f'Unknown column "{name}". A double-quoted name must be one of this '
                    "table's columns; a text value takes single quotes.")
        # EXPLAIN below never calls REGEXP, so it can't catch a bad pattern.
        _check_regexp_literals(frag)

        # A merge has no table of its own: the fragment is later spliced
        # into each member's branch, so the dry-run below must pass against
        # every member (invariant #9 — its identifier check above already
        # ran against the merge's exposed column set).
        froms = ([self._member_from(src, m) for m in self._resolve_members(source_id)]
                 if source_id < 0 else [self._from_clause(src)])
        ro = _no_dq_strings(sqlite3.connect(sqlite_ro_uri(self.path), uri=True,
                                            check_same_thread=False))
        ro.row_factory = sqlite3.Row
        ro.create_function("REGEXP", 2, _regexp, deterministic=True)
        try:
            for f_clause in froms:
                ro.execute(f"EXPLAIN QUERY PLAN SELECT 1 FROM {f_clause} WHERE ({frag}) LIMIT 0")
        except sqlite3.Error as e:
            raise ValueError(f"Not a valid filter expression: {e}")
        finally:
            ro.close()

    def _compile_where(self, source_id, src, spec, colnames) -> tuple[str, list]:
        clauses: list[str] = []
        params: list[Any] = []

        for f in spec.get("filters", []):
            col, op = f.get("column"), f.get("op", "contains")
            c, p = self._compile_condition(col, op, f.get("value", ""), colnames)
            if c:
                clauses.append(c)
                params.extend(p)
                if op in SARGABLE_OPS and col in colnames:
                    self._ensure_column_index_building(source_id, col, self._index_table_for(src, col))

        tree = spec.get("filter_tree")
        if tree:
            c, p = self._compile_tree(tree, colnames, source_id, src)
            if c:
                clauses.append(c)
                params.extend(p)

        search_mode = spec.get("search_mode", "contains")
        search = (spec.get("search") or "").strip()
        if search or (search_mode == "advanced" and spec.get("search_terms")):
            # Every search mode reads all of the table's columns, and a
            # table still importing has none yet: `_blob_expr([])` compiled
            # `() LIKE ?`, a syntax error the route could only call a 500.
            self._require_columns(src)
        if search_mode == "regex" and search:
            # Base columns only, matching build_fts's doc view exactly — the
            # indexed and fallback paths have to search the same text, and
            # derived values are computed from data that's already in it.
            _compiled_regex(search)   # refuse an unusable pattern here, by name — see _regexp
            blob = _blob_expr([c["name"] for c in self._base_cols(src)])
            clauses.append(f"({blob}) REGEXP ?")
            params.append(search)
        elif search_mode == "advanced":
            terms = spec.get("search_terms") or []
            if terms:
                cols = [c["name"] for c in self._base_cols(src)]
                if not src["has_fts"]:
                    self._ensure_fts_building(source_id)
                if src["has_fts"]:
                    clause, p = _advanced_fts_clause(q("fts_" + str(source_id)), _blob_expr(cols), terms)
                else:
                    clause, p = _advanced_like_clause(cols, terms)
                if clause:
                    clauses.append(clause)
                    params.extend(p)
        elif search:
            # A true substring match either way — the trigram index (a bare
            # `doc LIKE ?` against the fts table, pushed down onto the
            # trigram tokenizer; see build_fts/_fts_like_pattern for why
            # not MATCH) makes it fast once built. Falls back to a blob
            # LIKE scan — same results, just slower — when the index isn't
            # ready yet (kicked off here in the background) or the term
            # can't route through it (under 3 chars, or contains a LIKE
            # wildcard the unescaped pushdown form would misinterpret).
            if not src["has_fts"]:
                self._ensure_fts_building(source_id)
            pattern = _fts_like_pattern(search)
            if src["has_fts"] and pattern is not None:
                fts_ident = q("fts_" + str(source_id))
                clauses.append(f"rid IN (SELECT rowid FROM {fts_ident} WHERE doc LIKE ?)")
                params.append(pattern)
            else:
                blob = _blob_expr([c["name"] for c in self._base_cols(src)])
                clauses.append(f"({blob}) LIKE ? ESCAPE '\\'")
                params.append(f"%{_esc_like(search)}%")

        # The tag filter is a UNION of whatever it names: any of these tags,
        # or untagged, or both. OR rather than AND because that is what the
        # ribbon's chips mean — ticking Malware and Lateral movement asks
        # for rows carrying either, the way ticking two values in the value
        # picker does. (An intersection is expressible in the filter
        # builder, which is where a query that needs one belongs.)
        #
        # It used to compare the whole list against ["__any__"] and
        # ["__none__"] as exact matches, so a list holding a sentinel
        # alongside ids fell through to the ids branch and dropped the
        # sentinel silently. Each part is compiled on its own now and the
        # parts are OR'd, which also makes "untagged, plus the ones I
        # marked" a filter the ribbon can express.
        #
        # A "!<id>" entry is a tag filtered OUT (the chip's right-click
        # menu), and it is the one part that is ANDed rather than OR'd:
        # "Suspicious, but not the ones I marked Benign" has to drop a row
        # carrying both, and a union with "not Benign" would bring back
        # every untagged row instead. Exclusions ride in the same list
        # rather than a field of their own so that everything that already
        # carries, stashes or clears the tag filter — a table switch, a
        # background search, Clear filters, a dashboard drill — carries
        # them too without being told.
        tag_filter = spec.get("tags") or []
        excluded = [int(t[1:]) for t in tag_filter
                    if isinstance(t, str) and t.startswith("!") and t[1:].isdecimal()]
        if excluded:
            clauses.append(
                f"rid NOT IN (SELECT rid FROM row_tags WHERE source_id=? AND tag_id IN ({','.join('?' * len(excluded))}))"
            )
            params.append(source_id)
            params.extend(excluded)
        if tag_filter:
            parts: list[str] = []
            if "__any__" in tag_filter:
                parts.append("rid IN (SELECT rid FROM row_tags WHERE source_id=?)")
                params.append(source_id)
            if "__none__" in tag_filter:
                parts.append("rid NOT IN (SELECT rid FROM row_tags WHERE source_id=?)")
                params.append(source_id)
            ids = [int(t) for t in tag_filter if str(t).isdigit()]
            if ids:
                parts.append(
                    f"rid IN (SELECT rid FROM row_tags WHERE source_id=? AND tag_id IN ({','.join('?' * len(ids))}))"
                )
                params.append(source_id)
                params.extend(ids)
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")

        time_range = spec.get("time_range")
        if time_range and time_range.get("enabled") and (time_range.get("start") or time_range.get("end")):
            start_norm = _ts_normalize(time_range["start"]) if time_range.get("start") else None
            end_norm = _ts_normalize(time_range["end"]) if time_range.get("end") else None
            col = time_range.get("column")
            # An explicit column only applies here if this member actually
            # has it as a datetime column — otherwise (blank/"all columns"
            # mode, or a merge member that doesn't share this column) fall
            # back to every datetime column on this table, OR'd together:
            # a row counts as in-range if *any* of its timestamps is —
            # the "timestomped creation date, but a real modified date"
            # case the timeframe filter exists for in the first place.
            # `src` is this member's own (merge-spelled) column list, so a
            # merge column the member lacks never counts as "has it".
            present = {_col_key(c["name"]) for c in src["columns"]}
            if col and colnames.get(col) == "datetime" and _col_key(col) in present:
                ts_cols = [col]
            else:
                ts_cols = [c for c, t in colnames.items() if t == "datetime" and _col_key(c) in present]
            if ts_cols and (start_norm or end_norm):
                parts = []
                for c in ts_cols:
                    # ts_normalize_sql, not a TS_NORMALIZE call per row: a
                    # drag across the histogram builds this filter, and on
                    # the usual ISO column the Python call was most of the
                    # build (tests/test_histogram.py holds the two alike).
                    expr = ts_normalize_sql(q(c))
                    if start_norm is not None and end_norm is not None:
                        parts.append(f"{expr} BETWEEN ? AND ?")
                        params.extend([start_norm, end_norm])
                    elif start_norm is not None:
                        parts.append(f"{expr} >= ?")
                        params.append(start_norm)
                    else:
                        parts.append(f"{expr} <= ?")
                        params.append(end_norm)
                clauses.append("(" + _sql_chain(parts, " OR ") + ")")

        # "Hide empty rows" (the table menu): drop a row whose EVERY column
        # is empty — NULL or '', the same "empty" the per-column filter op
        # and "Hide empty columns" use. Here, rather than appended to the
        # SQL later, on purpose: a non-empty `where` is what keeps an
        # otherwise unfiltered view off the root_virtual carve-out
        # (invariant #2), whose positions are only exact when the view is
        # every row of the source. colnames is the merge's own list when
        # this compiles per member, so the clause is legal in each branch.
        if spec.get("hide_empty_rows") and colnames:
            parts = []
            for c in colnames:
                clause, p = self._compile_condition(c, "not_empty", "", colnames)
                parts.append(clause)
                params.extend(p)
            # _sql_chain: one OR per column is past SQLite's expression
            # depth limit on a table about 1,000 columns wide. Halves keep
            # their order, so the params still line up.
            clauses.append("(" + _sql_chain(parts, " OR ") + ")")

        return " AND ".join(clauses), params

    @staticmethod
    def _member_id_alias(colnames) -> str:
        """What a merge's UNION calls the member id it projects beside the
        data columns: `source_id`, unless the file has a column of that
        name (any ASCII case) — then ORDER BY "source_id" resolved to the
        first of the two, the member id, and sorting the merge on the
        file's own column sorted it by member. Underscores are prepended
        until the name is free, since any name a file could also use."""
        taken = {_ascii_fold(c) for c in colnames}
        alias = "source_id"
        while alias in taken:
            alias = "_" + alias
        return alias

    @staticmethod
    def _compile_order(spec, colnames) -> str:
        sort = spec.get("sort") or []
        parts = []
        for s in sort:
            col = s.get("column")
            direction = "DESC" if str(s.get("dir", "asc")).lower() == "desc" else "ASC"
            if col == "__line__":
                # The gutter's Line header: original file order, both ways.
                # rid IS the line order, so ascending is the no-sort default
                # — this exists for DESC, and so the client can state line
                # order explicitly.
                parts.append(f"rid {direction}")
                continue
            if col not in colnames:
                continue
            if colnames[col] == "number":
                parts.append(f"{_numeric_expr(q(col))} {direction}")
            else:
                parts.append(f"{q(col)} COLLATE NOCASE {direction}")
        parts.append("rid ASC")
        return "ORDER BY " + ", ".join(parts)

    @staticmethod
    def _compile_merge_order(spec, colnames) -> tuple[list[str], str]:
        """_compile_order for a merge's UNION ALL, which build_view sorts as
        a compound rather than as a subquery (see the comment there).
        Returns the sort keys each member SELECTs after `<member id>, rid`,
        and an ORDER BY naming them by position, since a compound's ORDER
        BY can only name its own result columns. Term for term the same
        order _compile_order gives, with the member id added last: rows
        that tie on every key and on rid keep member order by construction,
        which is the order the old temp B-tree sort gave them."""
        keys: list[str] = []
        terms: list[str] = []
        for s in spec.get("sort") or []:
            col = s.get("column")
            direction = "DESC" if str(s.get("dir", "asc")).lower() == "desc" else "ASC"
            if col == "__line__":
                terms.append(f"2 {direction}")
                continue
            if col not in colnames:
                continue
            keys.append(_numeric_expr(q(col)) if colnames[col] == "number" else f"{q(col)} COLLATE NOCASE")
            terms.append(f"{len(keys) + 2} {direction}")
        terms += ["2 ASC", "1 ASC"]
        return keys, "ORDER BY " + ", ".join(terms)

    @staticmethod
    def _inline_sql_params(sql: str, params: list) -> str:
        """Substitute ? placeholders with safely-quoted literals, producing
        SQL fit to hand the analyst in the SQL pane. Skips any ? that is not
        a placeholder, by walking the text the way SQLite's tokenizer does:
        inside a string literal (a raw filter fragment the analyst typed can
        legitimately contain one), inside a quoted identifier ("In Use?" is
        a header q() quotes — and also `x` and [x], which a typed fragment
        may use), or inside a comment. Quotes escape by doubling, as in
        SQLite; brackets don't escape at all."""
        out: list[str] = []
        it = iter(params)
        closers = {"'": "'", '"': '"', "`": "`", "[": "]"}
        n = len(sql)
        i = 0
        while i < n:
            ch = sql[i]
            if ch in closers:
                close = closers[ch]
                j = i + 1
                while j < n:
                    if sql[j] == close:
                        if close != "]" and j + 1 < n and sql[j + 1] == close:
                            j += 2
                            continue
                        break
                    j += 1
                out.append(sql[i:j + 1])
                i = j + 1
                continue
            if sql.startswith("--", i):
                j = sql.find("\n", i)
                j = n if j < 0 else j
                out.append(sql[i:j])
                i = j
                continue
            if sql.startswith("/*", i):
                j = sql.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append(sql[i:j])
                i = j
                continue
            if ch == "?":
                try:
                    v = next(it)
                except StopIteration:
                    raise ValueError("Filter could not be rendered as SQL (placeholder mismatch)")
                if v is None:
                    out.append("NULL")
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.append(repr(v))
                else:
                    out.append("'" + str(v).replace("'", "''") + "'")
            else:
                out.append(ch)
            i += 1
        return "".join(out)

    def spec_sql(self, source_id: int, spec: dict) -> str:
        """The current filter/sort/search spec rendered as a runnable
        SELECT — the "open this filter in the SQL pane" action. Compiled by
        the exact same _compile_where/_compile_order the view build uses,
        so what the analyst gets is the query their grid is actually
        showing, not a hand-maintained approximation; the only difference
        is that bound parameters are inlined as literals so the text stands
        alone. run_sql's own connection registers REGEXP/TS_NORMALIZE/
        DAY_BUCKET, so every compiled shape runs there unchanged.

        Table references are the PANE's names, deliberately bare: run_sql
        shadows src_<id> with a derived-including TEMP view when the
        source has derived columns, so `FROM "src_1"` with bare column
        references is both correct and exactly what an analyst would
        write. Spelling the sidecar join here instead would collide with
        that shadow ("ambiguous column name") — the pane's names do the
        joining now."""
        src = self._source_lite(source_id)
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        order = self._compile_order(spec, colnames)
        if src.get("is_merge"):
            for frag in self._raw_fragments(spec.get("filter_tree")):
                self.validate_where_fragment(source_id, frag)
            collist = ", ".join(q(c) for c in colnames)
            sid_col = q(self._member_id_alias(colnames))
            branches = []
            params: list[Any] = []
            for m in self._resolve_members(source_id):
                msrc = m["src"]
                where, p = self._compile_where(m["source_id"], self._member_read_src(src, msrc), spec, colnames)
                # The pane's own src_<id> name, projected when the member
                # doesn't carry the merge's columns as-is — run_sql's shadow
                # view supplies any derived columns the projection names.
                exprs = self._member_projection(src, msrc)
                from_sql = (q(m["table_name"]) if exprs is None
                            else f"(SELECT rid, {', '.join(exprs)} FROM {q(m['table_name'])})")
                branches.append(
                    f"SELECT {int(m['source_id'])} AS {sid_col}, rid, {collist}\n"
                    f"FROM {from_sql}"
                    + (f"\nWHERE {where}" if where else "")
                )
                params.extend(p)
            sql = "\nUNION ALL\n".join(branches) + f"\n{order}"
        else:
            where, params = self._compile_where(source_id, src, spec, colnames)
            collist = ", ".join(q(c) for c in colnames)
            sql = f"SELECT rid, {collist}\nFROM {q(src['table_name'])}"
            if where:
                sql += f"\nWHERE {where}"
            sql += f"\n{order}"
        return self._inline_sql_params(sql, params)

    @staticmethod
    def _require_columns(src: dict) -> None:
        """A source whose column list is still empty is not readable yet.

        Every ingest path inserts the `sources` row with `columns='[]'` and
        only fills it in at the end, while `open_tabs` is written straight
        away — so a directory import gives the tab strip tabs for files
        that are still importing. Reading one built `SELECT rid,  FROM
        "src_5"`, whose OperationalError is not "no such table" and so
        surfaced as a 500. A `kill -9` mid-import leaves the row that way
        permanently, and the tab 500s forever.

        A ValueError instead: the routes already turn it into a 400 the UI
        shows, and "still importing" is the true answer."""
        if not src.get("columns"):
            raise ValueError(
                f"{src.get('name') or 'This table'} is still importing — its columns "
                "aren't known yet. It will open when the import finishes.")

    def fetch_rows(self, view_id: str, start: int, count: int) -> dict:
        """Pages v.view_N by pos, then resolves each row's cells/tags/notes
        against ITS OWN source_id — not a single handle-level constant. For
        an ordinary view every row shares one source_id (one query, same
        cost as before); for a merged view rows are grouped by their real
        distinct member source_id (bounded by window size × member count)
        and resolved per member. Either way this stays O(window)."""
        return self._fetch_page(view_id, start, count, as_json=False)

    def fetch_rows_json(self, view_id: str, start: int, count: int) -> bytes:
        """fetch_rows' answer as the bytes JSONResponse would send for it,
        with the cells written by SQLite (`json_array`) rather than read
        into Python one value at a time and written again by json.dumps.
        GET /api/rows serves this, on every scroll.

        The cells were most of a page's cost: a 5,000-row page took
        500 ms on a 420-column JSON import and 110 ms this way, 80 ms and
        40 ms on a 27-column EvtxECmd table (8 MB). Same bytes, which tests/test_rows_json.py holds over every
        kind of view and of text (_cells_json_sql says why that holds).
        Anything SQLite's JSON refuses — a BLOB, a build without JSON
        functions or with a lower argument cap (_sql_json_pages) — is
        answered by the fetch_rows path instead, so it fails, or not, as
        it always did."""
        if self._sql_json_pages():
            try:
                return self._fetch_page(view_id, start, count, as_json=True)
            except sqlite3.OperationalError:
                pass
        return json.dumps(self.fetch_rows(view_id, start, count), ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")

    # What _sql_json_pages has SQLite write: NULL, empty, every control
    # character, the characters json.dumps escapes, the ones it leaves
    # alone that a JSON writer might not (DEL, the line and paragraph
    # separators, an astral character), and enough more to fill one
    # json_array call.
    _SQL_JSON_PROBE = ([None, "", "".join(map(chr, range(1, 32))), '"\\/', "\x7f\u2028\u2029\U0001F600"]
                       + [str(i) for i in range(JSON_CELLS_PER_CALL - 5)])

    def _sql_json_pages(self) -> bool:
        """Whether this SQLite writes a page's cells as json.dumps would:
        JSON functions present (a build option before 3.38), _SQL_JSON_PROBE
        written alike, and room for JSON_CELLS_PER_CALL arguments. Asked
        once per Store."""
        if self._sql_json_ok is None:
            probe = self._SQL_JSON_PROBE
            try:
                with self._reader() as ro:
                    got = ro.execute(
                        "SELECT json_array(" + ", ".join(f"v{i}" for i in range(len(probe)))
                        + ") FROM (SELECT " + ", ".join(f"? AS v{i}" for i in range(len(probe))) + ")",
                        probe).fetchone()[0]
                self._sql_json_ok = got == json.dumps(probe, ensure_ascii=False, separators=(",", ":"))
            except sqlite3.Error:
                self._sql_json_ok = False
        return self._sql_json_ok

    def _fetch_page(self, view_id: str, start: int, count: int, *, as_json: bool) -> dict | bytes:
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "group_virtual":
            return self._fetch_virtual_group_rows(handle, start, count, as_json)
        if handle.get("kind") == "root_virtual":
            return self._fetch_virtual_root_rows(handle, start, count, as_json)

        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, handle["source_id"])
            self._require_columns(src)
            cols = [c["name"] for c in src["columns"]]

            vrows = ro.execute(
                f"SELECT pos, source_id, rid FROM v.{q(view_id)} "
                f"WHERE pos >= ? AND pos < ? ORDER BY pos",
                (start + 1, start + 1 + count),
            ).fetchall()

            by_source: dict[int, list[int]] = {}
            for r in vrows:
                by_source.setdefault(r["source_id"], []).append(r["rid"])

            cellmap: dict[tuple[int, int], Any] = {}
            tags: dict[tuple[int, int], list[int]] = {}
            notes: dict[tuple[int, int], str] = {}
            sel = _cells_json_sql(cols) if as_json else ", ".join(q(c) for c in cols)
            for sid, rids in by_source.items():
                member_src = self._source_lite_on(ro, sid)
                ph = ",".join("?" * len(rids))
                for row in ro.execute(
                    f"SELECT rid, {sel} FROM {self._member_rows_from(src, member_src)} WHERE rid IN ({ph})", rids
                ):
                    # positional: the SELECT puts rid first, then cols in
                    # order — tuple(row) is one C-level copy, vs a
                    # per-cell name lookup for row[c]
                    t = tuple(row)
                    cellmap[(sid, t[0])] = _cells_json(t[1:]) if as_json else t[1:]
                for rid, tid in ro.execute(
                    f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})",
                    [sid, *rids],
                ):
                    tags.setdefault((sid, rid), []).append(tid)
                for rid, note in ro.execute(
                    f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})",
                    [sid, *rids],
                ):
                    notes[(sid, rid)] = note

        if as_json:
            missing = "[" + ",".join(['""'] * len(cols)) + "]"
            return _page_json(start, (
                _page_row_json(r["pos"] - 1, r["source_id"], r["rid"],
                               cellmap.get((r["source_id"], r["rid"]), missing),
                               tags.get((r["source_id"], r["rid"]), []), notes.get((r["source_id"], r["rid"])))
                for r in vrows))
        out = []
        for r in vrows:
            key = (r["source_id"], r["rid"])
            out.append({
                "pos": r["pos"] - 1,
                "source_id": r["source_id"],
                "rid": r["rid"],
                "cells": list(cellmap.get(key, ("",) * len(cols))),
                "tags": tags.get(key, []),
                "note": notes.get(key),
            })
        return {"start": start, "rows": out}

    def _fetch_virtual_group_rows(self, handle: dict, start: int, count: int,
                                  as_json: bool = False) -> dict | bytes:
        """Pages a small, ungathered group directly with LIMIT/OFFSET —
        expand_group only chooses this path for a single-source (non-merge)
        group under GROUP_MATERIALIZE_THRESHOLD, so `handle['members']`
        always has exactly one entry here. Ordered by rid (stable insertion
        order), not the outer view's actual sort — an accepted simplification
        for this fast/small-group path; a group above the threshold (or on a
        merge) always gets the materialized path instead, which does
        preserve the outer sort via root_pos."""
        member = handle["members"][0]
        sid = member["source_id"]
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, sid)
            self._require_columns(src)
            cols = [c["name"] for c in src["columns"]]
            where_sql, where_params = self._virtual_group_where(handle, ro)
            sel = _cells_json_sql(cols) if as_json else ", ".join(q(c) for c in cols)

            rows = ro.execute(
                f"SELECT rid, {sel} FROM {self._from_clause(src)} WHERE {where_sql} "
                f"ORDER BY rid LIMIT ? OFFSET ?",
                [*where_params, count, start],
            ).fetchall()
            rids = [r["rid"] for r in rows]
            tags: dict[int, list[int]] = {}
            notes: dict[int, str] = {}
            if rids:
                ph = ",".join("?" * len(rids))
                for rid, tid in ro.execute(
                    f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    tags.setdefault(rid, []).append(tid)
                for rid, note in ro.execute(
                    f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    notes[rid] = note

        if as_json:
            return _page_json(start, (
                _page_row_json(start + i, sid, r[0], _cells_json(tuple(r)[1:]), tags.get(r[0], []), notes.get(r[0]))
                for i, r in enumerate(rows)))
        out = []
        for i, r in enumerate(rows):
            t = tuple(r)
            out.append({
                "pos": start + i,
                "source_id": sid,
                "rid": t[0],
                "cells": list(t[1:]),
                "tags": tags.get(t[0], []),
                "note": notes.get(t[0]),
            })
        return {"start": start, "rows": out}

    def _fetch_virtual_root_rows(self, handle: dict, start: int, count: int,
                                 as_json: bool = False) -> dict | bytes:
        """Pages the source table directly by rid *range* — no v.view_N to
        build, since an unfiltered/unsorted root_virtual view is, by
        construction, every row of the source in its natural (already
        free) order, and rid is contiguous from 1 (see
        _build_virtual_root_view), so the window at `start` is exactly
        rids start+1 .. start+count. A rid-range seek is O(log n) at any
        depth, where the LIMIT/OFFSET shape _fetch_virtual_group_rows
        uses (fine for its ≤GROUP_MATERIALIZE_THRESHOLD rows) walks and
        discards `start` rows first — measured 26ms vs 0.1ms at the far
        end of 2M rows, and that gap grows linearly with depth."""
        sid = handle["source_id"]
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, sid)
            # Source-column order (same as the materialised fetch_rows path),
            # NOT _export_columns: the client maps each cell by its column's
            # index in S.columns (source order), so paging in the saved
            # layout's display order — with hidden columns dropped — scrambles
            # values under headers whenever a custom layout reorders/hides a
            # column. _export_columns is for CSV/XLSX, which emit the on-screen
            # arrangement on purpose; the grid does its own ordering client-side.
            self._require_columns(src)
            cols = [c["name"] for c in src["columns"]]
            sel = _cells_json_sql(cols) if as_json else ", ".join(q(c) for c in cols)

            # _from_clause adds the derived-value sidecar join only when the
            # source has derived columns, and that join is on drv's INTEGER
            # PRIMARY KEY — at most one match per rid, so the rid window
            # still yields exactly its rows in rid order and `pos = rid - 1`
            # below stays exact (invariant #2).
            rows = ro.execute(
                f"SELECT rid, {sel} FROM {self._from_clause(src)} WHERE rid >= ? AND rid < ? ORDER BY rid",
                (start + 1, start + 1 + count),
            ).fetchall()
            rids = [r["rid"] for r in rows]
            tags: dict[int, list[int]] = {}
            notes: dict[int, str] = {}
            if rids:
                ph = ",".join("?" * len(rids))
                for rid, tid in ro.execute(
                    f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    tags.setdefault(rid, []).append(tid)
                for rid, note in ro.execute(
                    f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    notes[rid] = note

        if as_json:
            # rid - 1: contiguous rids make this exact
            return _page_json(start, (
                _page_row_json(r[0] - 1, sid, r[0], _cells_json(tuple(r)[1:]), tags.get(r[0], []), notes.get(r[0]))
                for r in rows))
        out = []
        for i, r in enumerate(rows):
            t = tuple(r)
            out.append({
                "pos": t[0] - 1,  # rid - 1: contiguous rids make this exact
                "source_id": sid,
                "rid": t[0],
                "cells": list(t[1:]),
                "tags": tags.get(t[0], []),
                "note": notes.get(t[0]),
            })
        return {"start": start, "rows": out}

    def tag_positions(self, view_id: str, limit: int = 20_000,
                      height: int | None = None) -> list[list[int]]:
        """Positions of tagged rows inside a view, for the scrollbar rail:
        [pos, tag_id] per tagged row, in view order, at most `limit`.

        With `height` (the rail's height in pixels) it is one entry per
        pixel row and tag instead, [y, tag_id, first pos, last pos], where
        y is the rail's own round(pos / row_count * height). Every tagged
        row counts: the flat list stopped at the 20,000th, so on a view
        with more tagged rows than that the rail showed nothing below it,
        after sending 20,000 pairs to draw a strip a few hundred pixels
        tall. Ordered by last pos, so drawing the entries in order paints
        each pixel the colour drawing every row would have."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "group_virtual":
            return []  # no pos-ordered backing table for a small ungathered group — documented limitation
        with self._reader() as ro, self._dropped_view_is_expired():
            if handle.get("kind") == "root_virtual":
                # pos = rid - 1 directly: an unfiltered root_virtual view is
                # every row of the source in rid order, so a tagged row's rank
                # IS its rid - no join against a backing view table needed, and
                # (unlike group_virtual) never a stub — see _build_virtual_root_view.
                tagged = "SELECT rid - 1 AS p, tag_id FROM row_tags WHERE source_id=?"
                params: list = [handle["source_id"]]
            else:
                # The join below has no better plan than scanning the whole view
                # probing row_tags per row (the view has no index on rid — measured
                # ~115ms per 2M view rows even with row_tags empty). An untagged
                # source is the common case and this runs after every view build,
                # so check for any tag at all first — one indexed probe per member.
                if "source_id" in handle:
                    any_tags = any(
                        ro.execute(
                            "SELECT 1 FROM row_tags WHERE source_id=? LIMIT 1", (m["source_id"],)
                        ).fetchone()
                        for m in self._resolve_members_on(ro, handle["source_id"])
                    )
                    if not any_tags:
                        return []
                tagged = (f"SELECT vv.pos - 1 AS p, rt.tag_id FROM v.{q(view_id)} vv "
                          f"JOIN row_tags rt ON rt.rid = vv.rid AND rt.source_id = vv.source_id")
                params = []
            if height is None:
                order = "rid" if handle.get("kind") == "root_virtual" else "vv.pos"
                rows = ro.execute(f"{tagged} ORDER BY {order} LIMIT ?", [*params, limit]).fetchall()
                return [[r["p"], r["tag_id"]] for r in rows]
            n = max(int(handle.get("row_count") or 0), 1)
            rows = ro.execute(
                f"SELECT CAST(ROUND(p * 1.0 / ? * ?) AS INTEGER) AS y, tag_id, MIN(p) AS lo, MAX(p) AS hi "
                f"FROM ({tagged}) GROUP BY y, tag_id ORDER BY hi",
                [n, int(height), *params],
            ).fetchall()
        return [[r["y"], r["tag_id"], r["lo"], r["hi"]] for r in rows]

    def _positions_of(self, ro: sqlite3.Connection, view_id: str, handle: dict,
                      source_id: int, rids: list[int]) -> list[int]:
        """0-indexed positions of one source's rids inside a view, in the
        order found — however the view keeps its rows. A root_virtual view
        is the source itself, so pos = rid - 1 (invariant #2) and only the
        rid's existence is checked; anything else has a pos column to ask.
        Rids the view doesn't have are simply absent. The one lookup behind
        find_position (one row) and view_positions (a selection's worth);
        caller holds the reader and _dropped_view_is_expired."""
        out: list[int] = []
        if handle.get("kind") == "root_virtual":
            if source_id != handle["source_id"]:
                return out
            table = q(self._source_lite_on(ro, source_id)["table_name"])
            for i in range(0, len(rids), 500):
                chunk = rids[i:i + 500]
                rows = ro.execute(f"SELECT rid FROM {table} WHERE rid IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                out.extend(r["rid"] - 1 for r in rows)
            return out
        for i in range(0, len(rids), 500):
            chunk = rids[i:i + 500]
            rows = ro.execute(
                f"SELECT pos FROM v.{q(view_id)} WHERE source_id=? AND rid IN ({','.join('?' * len(chunk))})",
                [source_id, *chunk]).fetchall()
            out.extend(r["pos"] - 1 for r in rows)
        return out

    def find_position(self, view_id: str, source_id: int, rid: int) -> int | None:
        """0-indexed position of one (source_id, rid) row inside a view, or
        None if it's not in this view. Positions are view-specific and get
        wiped on every rebuild (see CLAUDE.md); this is how the frontend
        re-centers the grid on a previously-selected row after its view is
        rebuilt out from under it, e.g. clearing filters."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "group_virtual":
            return None  # no pos-ordered backing table for a small ungathered group
        with self._reader() as ro, self._dropped_view_is_expired():
            found = self._positions_of(ro, view_id, handle, source_id, [int(rid)])
        return found[0] if found else None

    SELECTION_REMAP_MAX = 20000

    def view_keys(self, view_id: str, positions: list[int]) -> list[list[int]]:
        """The (source_id, rid) behind each 0-indexed position of a view —
        what a selection IS once the view that gave those positions meaning
        is about to be rebuilt. Pairs with view_positions: the grid asks for
        the keys before a rebuild and for their new positions after it, so
        a sort or a filter keeps the rows the analyst picked. Positions the
        view doesn't have are skipped; capped like the remap itself."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "group_virtual":
            return []
        wanted = sorted({int(p) for p in positions})[:self.SELECTION_REMAP_MAX]
        if not wanted:
            return []
        out: list[list[int]] = []
        with self._reader() as ro, self._dropped_view_is_expired():
            if handle.get("kind") == "root_virtual":
                src = self._source_lite_on(ro, handle["source_id"])
                table = q(src["table_name"])
                for i in range(0, len(wanted), 500):
                    chunk = [p + 1 for p in wanted[i:i + 500]]   # pos = rid - 1, exact (invariant #2)
                    rows = ro.execute(f"SELECT rid FROM {table} WHERE rid IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                    out.extend([handle["source_id"], r["rid"]] for r in rows)
                return out
            for i in range(0, len(wanted), 500):
                chunk = [p + 1 for p in wanted[i:i + 500]]
                rows = ro.execute(
                    f"SELECT source_id, rid FROM v.{q(view_id)} WHERE pos IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                out.extend([r["source_id"], r["rid"]] for r in rows)
        return out

    def view_positions(self, view_id: str, keys: list) -> dict:
        """The 0-indexed positions a list of (source_id, rid) keys hold in a
        view, plus how many of them the view does not contain — the rows a
        filter took out from under a selection. See view_keys."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        pairs: list[tuple[int, int]] = []
        for k in list(keys)[:self.SELECTION_REMAP_MAX]:
            try:
                pairs.append((int(k[0]), int(k[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not pairs or handle.get("kind") == "group_virtual":
            return {"positions": [], "missing": len(pairs)}
        found: list[int] = []
        by_source: dict[int, list[int]] = {}
        for sid, rid in pairs:
            by_source.setdefault(sid, []).append(rid)
        with self._reader() as ro, self._dropped_view_is_expired():
            for sid, rids in by_source.items():
                found.extend(self._positions_of(ro, view_id, handle, sid, rids))
        found.sort()
        return {"positions": found, "missing": len(pairs) - len(found)}

    def find_nearest_timestamp(self, view_id: str, value: str, column: str | None = None) -> dict | None:
        """Position (0-indexed, within this view) of the row whose timestamp
        is closest in time to `value` — "jump to timestamp". `column` picks
        which datetime column measures closeness; None means the nearest
        across every datetime column, same spirit as the timeframe filter's
        all-columns mode.

        Closeness is |julianday(a) - julianday(b)| over TS_NORMALIZE'd
        values — string comparison can order timestamps but can't measure
        between them. That makes this a scan of the view, the same cost
        shape as group_summary's whole-source aggregate: fine for a
        user-initiated, occasional action, and it runs on a pooled reader
        so it never blocks anything. The ISO shape is read by SQLite
        (ts_normalize_sql) and a view holding every row reads its table in
        the table's own order (_nearest_in_covering_view), which between
        them took a 4.3M-row MFTECmd table sorted by a timestamp from 41 s
        to 5 s. Rows whose timestamp doesn't parse simply can't win.
        Returns None when no row has a usable timestamp."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        norm = _ts_normalize(value)
        if norm is None:
            raise ValueError("Not a recognized timestamp — try YYYY-MM-DD HH:MM:SS")
        best: tuple | None = None  # (diff, source_id, rid, pos_or_None, ts)
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, handle["source_id"])
            colnames = {c["name"]: c["type"] for c in src["columns"]}
            if column:
                if colnames.get(column) != "datetime":
                    raise ValueError(f"{column!r} is not a datetime column")
                ts_cols = [column]
            else:
                ts_cols = [c for c, t in colnames.items() if t == "datetime"]
            if not ts_cols:
                raise ValueError("This table has no datetime columns")
            kind = handle.get("kind")
            covers = kind == "root" and self._grouping_covers_whole_source(handle, src)
            members = self._resolve_members_on(ro, handle["source_id"])
            for m in members:
                msrc = self._source_lite_on(ro, m["source_id"])
                for want in ts_cols:
                    # The member's own spelling — a merge matches columns
                    # ignoring case, so "TIME" on one member is "Time" here.
                    c = next((cc["name"] for cc in msrc["columns"]
                              if _col_key(cc["name"]) == _col_key(want)), None)
                    if c is None:
                        continue  # merge member without this column
                    # The timestamp is worked out once a row, inside
                    # (LIMIT -1 keeps that SELECT from being flattened into
                    # this one, which would repeat it for the filter, the
                    # distance and the answer), and by SQLite itself for
                    # the ISO shape (ts_normalize_sql). A row whose
                    # distance is NULL (a timestamp julianday can't read,
                    # like 0000-00-00) can't win: ORDER BY puts NULL first.
                    # A tie goes to the row first in the view's order, said
                    # outright (pos, or rid for the kinds that page in rid
                    # order): the scan's own order is the planner's choice,
                    # and an index on the column makes it the column's.
                    if covers:
                        row = self._nearest_in_covering_view(ro, view_id, msrc, m["source_id"], c, norm)
                        if row is not False:
                            if row and (best is None or row["d"] < best[0]):
                                best = (row["d"], m["source_id"], row["rid"], row["pos"], row["ts"])
                            continue
                    if kind == "root_virtual":
                        inner = (f"SELECT rid, NULL AS pos, {ts_normalize_sql(q(c))} AS ts "
                                 f"FROM {self._from_clause(msrc)}")
                        params = (norm,)
                    elif kind == "group_virtual":
                        where_sql, where_params = self._virtual_group_where(handle, ro)
                        inner = (f"SELECT rid, NULL AS pos, {ts_normalize_sql(q(c))} AS ts "
                                 f"FROM {self._member_from(msrc, m)} WHERE ({where_sql})")
                        params = (norm, *where_params)
                    else:
                        inner = (f"SELECT vv.rid AS rid, vv.pos AS pos, "
                                 f"{ts_normalize_sql(self._col_ref(msrc, c, 's', 'd'))} AS ts "
                                 f"FROM v.{q(view_id)} vv "
                                 f"JOIN {q(m['table_name'])} s ON s.rid = vv.rid AND vv.source_id = ?"
                                 f"{self._derived_join(msrc, 's')}")
                        params = (norm, m["source_id"])
                    row = ro.execute(
                        f"SELECT rid, pos, ts, d FROM (SELECT rid, pos, ts, "
                        f"ABS(julianday(ts) - julianday(?)) AS d FROM ({inner} LIMIT -1)) "
                        f"WHERE d IS NOT NULL ORDER BY d, pos, rid LIMIT 1",
                        params,
                    ).fetchone()
                    if row and row["d"] is not None and (best is None or row["d"] < best[0]):
                        best = (row["d"], m["source_id"], row["rid"], row["pos"], row["ts"])
            if best is None:
                return None
            d, sid, rid, pos, ts = best
            if kind == "root_virtual":
                pos = rid - 1  # exact — see _fetch_virtual_root_rows / invariant #2
            elif kind == "group_virtual":
                # pos within a virtual group = how many of its rows page in
                # before this rid (they page in rid order, see
                # _fetch_virtual_group_rows).
                where_sql, where_params = self._virtual_group_where(handle, ro)
                member = handle["members"][0]
                msrc = self._source_lite_on(ro, member["source_id"])
                pos = ro.execute(
                    f"SELECT COUNT(*) FROM {self._member_from(msrc, member)} "
                    f"WHERE ({where_sql}) AND rid < ?",
                    (*where_params, rid),
                ).fetchone()[0]
            else:
                pos = pos - 1  # v.view_N pos is 1-based
        return {"pos": pos, "source_id": sid, "rid": rid, "ts": ts}

    # How many rows find_nearest_timestamp's covering-view shortcut takes
    # as tied for closest before it reads the view instead.
    NEAREST_TIES = 500

    def _nearest_in_covering_view(self, ro: sqlite3.Connection, view_id: str, msrc: dict,
                                  source_id: int, column: str, norm: str) -> dict | None | bool:
        """find_nearest_timestamp for a root view holding every row of its
        source (_grouping_covers_whole_source), one member: the closest
        row, None when no row has a usable timestamp, or False when more
        than NEAREST_TIES rows tie for closest and the caller should read
        through the view after all.

        Reading through the view visits the member's rows in the view's
        order, one rid lookup each: on any sort but rid's that is random
        access over the whole table (30 s on a 4.3M-row MFTECmd table
        sorted by a timestamp, against 5 s in rid order). Since the view
        holds every row, the member table is read in its own order
        instead, and the view is only asked where the closest rows sit. Of
        rows tied for closest, the one first in the view's order wins,
        which is the row reading the view in order would have kept."""
        cand = ro.execute(
            f"SELECT rid, ts, d FROM (SELECT rid, ts, ABS(julianday(ts) - julianday(?)) AS d "
            f"FROM (SELECT rid, {ts_normalize_sql(q(column))} AS ts FROM {self._from_clause(msrc)} LIMIT -1)) "
            f"WHERE d IS NOT NULL ORDER BY d LIMIT ?",
            (norm, self.NEAREST_TIES + 1),
        ).fetchall()
        if not cand:
            return None
        tied = {r["rid"]: r for r in cand if r["d"] == cand[0]["d"]}
        if len(tied) > self.NEAREST_TIES:
            return False
        hit = ro.execute(
            f"SELECT pos, rid FROM v.{q(view_id)} WHERE source_id = ? "
            f"AND rid IN ({','.join('?' * len(tied))}) ORDER BY pos LIMIT 1",
            (source_id, *tied),
        ).fetchone()
        if hit is None:
            return None
        r = tied[hit["rid"]]
        return {"rid": r["rid"], "pos": hit["pos"], "ts": r["ts"], "d": r["d"]}

    def column_values(self, source_id: int, column: str, limit: int = 200) -> list[dict]:
        """Distinct values + counts for one column — the filter box's
        value-picker dropdown.

        Kicks off the same lazy B-tree index _compile_where's sargable
        filters do (see _ensure_column_index_building), because this exact
        shape is what a plain single-column index is *best* at: verified
        with EXPLAIN QUERY PLAN, `SELECT col, count(*) FROM src GROUP BY 1`
        goes from `SCAN src` + `USE TEMP B-TREE FOR GROUP BY` to `SCAN src
        USING COVERING INDEX` — never touching the table pages, and with the
        grouping sort gone entirely. Fire-and-forget as everywhere else: this
        call still runs today's scan, the next open of the dropdown gets the
        index. It's also the same index an equals/in filter on this column
        wants, which is the overwhelmingly likely next action after picking
        a value out of this list."""
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, source_id)
            names = {c["name"] for c in src["columns"]}
            if column not in names:
                raise KeyError(column)
            members = self._resolve_members_on(ro, source_id)
            for m in members:
                self._ensure_column_index_building(
                    m["source_id"], column, self._index_table_for(src, column, m["table_name"], m["source_id"])
                )
            union_sql = " UNION ALL ".join(
                f"SELECT {q(column)} AS val FROM {self._member_from(src, m, uses=(column,))}" for m in members
            )
            rows = ro.execute(
                f"SELECT val, count(*) AS n FROM ({union_sql}) GROUP BY 1 ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"value": r["val"], "count": r["n"]} for r in rows]

    def column_max_lengths(self, source_id: int, columns: list[str] | None = None) -> dict[str, int]:
        """MAX(LENGTH(col)) per column, across all member tables — backs the
        UI's autofit-column-width shortcut. One sequential scan per member
        table, every asked column computed in a single pass.

        `columns` narrows it to the ones being fitted: double-clicking one
        column's edge used to measure every column of the table to answer
        for one — 4.7 s on a 2.1M-row EvtxECmd export where the column
        alone takes 0.1 to 1.4 s, and much less when it is indexed, since
        then the scan reads the index instead of the table. None is every
        column, as before; names the source does not have are left out.

        Cached per source and per column — a full scan over every column
        of a 10M-row source isn't cheap, and autofit is a button a user can
        click repeatedly (once per column double-click, plus a "fit all"
        action) without the underlying data changing at all between
        clicks. Keyed on the row count AND the column list, derived
        columns' id and status included: base columns never change after
        ingest (invariant #1), but a derived column comes and goes, and
        fills in behind a backfill, without the row count moving — keyed
        on rows alone, the answer after one was added simply lacked it."""
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, source_id)
            key = (src["row_count"],
                   tuple((c["name"], c.get("derived_id"), c.get("derived_status"))
                         for c in src["columns"]))
            self._require_columns(src)
            cached = self._maxlen_cache.get(source_id)
            known = dict(cached[1]) if cached and cached[0] == key else {}
            cols = [c["name"] for c in src["columns"]]
            if columns is not None:
                asked = set(columns)
                cols = [c for c in cols if c in asked]
            missing = [c for c in cols if c not in known]
            if missing:
                members = self._resolve_members_on(ro, source_id)
                sel = ", ".join(f"MAX(LENGTH({q(c)}))" for c in missing)
                maxes = [0] * len(missing)
                for m in members:
                    row = ro.execute(f"SELECT {sel} FROM {self._member_from(src, m)}").fetchone()
                    for i, v in enumerate(row):
                        if v and v > maxes[i]:
                            maxes[i] = v
                known.update(zip(missing, maxes))
                self._maxlen_cache[source_id] = (key, known)
        return {c: known[c] for c in cols}

    # -------------------------------------------------------------------- tags

    def tag_time_bounds(self, source_id: int, tag_ids: list[int] | None = None,
                        column: str | None = None) -> dict:
        """Earliest and latest timestamp among this source's tagged rows —
        what "set the timeframe filter from my findings" needs. `tag_ids`
        empty/None means any tag; `column` narrows which datetime column
        counts, else every datetime column does (same all-columns semantics
        as the timeframe filter itself, so the range this fills is
        guaranteed to cover the rows it was derived from). Values are
        TS_NORMALIZE'd, i.e. exactly the shape the timeframe filter
        compares through."""
        src = self._source_lite(source_id)
        colnames = {c["name"]: c["type"] for c in src["columns"]}
        if column and colnames.get(column) == "datetime":
            ts_cols = [column]
        else:
            ts_cols = [c for c, t in colnames.items() if t == "datetime"]
        if not ts_cols:
            return {"start": None, "end": None}
        ids = [int(t) for t in (tag_ids or [])]
        tag_clause = f" AND tag_id IN ({','.join('?' * len(ids))})" if ids else ""
        lo = hi = None
        with self._reader() as ro:
            for m in self._resolve_members_on(ro, source_id):
                msrc = self._source_lite_on(ro, m["source_id"])
                member_names = {_col_key(c["name"]): c["name"] for c in msrc["columns"]}
                for want in ts_cols:
                    c = member_names.get(_col_key(want))  # the member's own spelling
                    if c is None:
                        continue
                    # LIMIT -1 keeps the inner SELECT from being
                    # flattened into the aggregate, which would work the
                    # timestamp out once for MIN and again for MAX.
                    row = ro.execute(
                        f"SELECT MIN(ts), MAX(ts) FROM (SELECT {ts_normalize_sql(q(c))} AS ts "
                        f"FROM {self._from_clause(msrc)} "
                        f"WHERE rid IN (SELECT rid FROM row_tags WHERE source_id=?{tag_clause}) LIMIT -1)",
                        [m["source_id"], *ids],
                    ).fetchone()
                    if row[0] is not None and (lo is None or row[0] < lo):
                        lo = row[0]
                    if row[1] is not None and (hi is None or row[1] > hi):
                        hi = row[1]
        return {"start": lo, "end": hi}

    def list_tags(self) -> list[dict]:
        with self._reader() as ro:
            return [dict(r) for r in ro.execute("SELECT * FROM tag_defs ORDER BY id")]

    def upsert_tag(self, tag_id: int | None, name: str, color: str, hotkey: str | None) -> dict:
        with self.lock, self.db:
            # A tags widget lists one row PER TAG DEFINITION, labelled with
            # its name, so a new tag or a rename changes its answer without
            # touching row_tags. A recolour or a rehotkey does not, and a
            # board that reddened every time somebody adjusted the palette
            # would be crying wolf.
            if tag_id:
                was = self.db.execute("SELECT name FROM tag_defs WHERE id=?", (tag_id,)).fetchone()
                if was is None:
                    # Edited from a window that still lists a tag another
                    # one deleted. Creating it again under the old id is
                    # not an option (ids are never reused), and a new id
                    # would be a tag nobody asked for.
                    raise RecordGone(f"Tag {tag_id} no longer exists in this case")
                self.db.execute(
                    "UPDATE tag_defs SET name=?, color=?, hotkey=? WHERE id=?",
                    (name, color, hotkey, tag_id),
                )
                if was["name"] != name:
                    self._bump_state_generation()
            else:
                # A new tag never takes a name another tag already has: the
                # first of "New tag", "New tag 2", "New tag 3"… that is free.
                # Tags are matched by NAME wherever two cases meet (session
                # load, the diff, copying tables), so two tags of one name
                # are two tags nothing outside this case can tell apart —
                # and Add tag sends "New tag" every time, which made that
                # the ordinary state of a case two clicks in. Exact names,
                # as pair_tag_names matches first: "Malware" and "malware"
                # can be two tags on purpose. A rename is left alone — it
                # is the analyst's own word for the tag.
                taken = {r[0] for r in self.db.execute("SELECT name FROM tag_defs")}
                base, n = name, 2
                while name in taken:
                    name = f"{base} {n}"
                    n += 1
                cur = self.db.execute(
                    "INSERT INTO tag_defs(name, color, hotkey) VALUES (?,?,?)",
                    (name, color, hotkey),
                )
                tag_id = cur.lastrowid
                self._bump_state_generation()
            row = self.db.execute("SELECT * FROM tag_defs WHERE id=?", (tag_id,)).fetchone()
        return dict(row)

    def delete_tag(self, tag_id: int) -> None:
        """Drops every assignment of the tag with it — a direct row_tags
        write rather than a recorded delta (there is nothing to undo once
        the tag itself is gone), so it bumps the state generation itself.

        An indicator that auto-tags with it stops auto-tagging rather than
        keep the id: the next scan would otherwise write assignments for a
        tag that does not exist, invisible in every list and still counted
        in a table's tagged-row total."""
        with self.lock, self.db:
            self._drop_undo_for_tag(tag_id)
            self.db.execute("DELETE FROM row_tags WHERE tag_id=?", (tag_id,))
            self.db.execute("UPDATE watchlist SET auto_tag_id=NULL WHERE auto_tag_id=?", (tag_id,))
            self.db.execute("DELETE FROM tag_defs WHERE id=?", (tag_id,))
            self._bump_state_generation()

    # ------------------------------------------------------------------ undo

    # How many tag changes deep Ctrl+Z goes, and the ceiling on how many
    # delta rows the whole history is allowed to retain. The row budget is
    # the one that actually bites: "tag every row in this 1.2M-row view"
    # records 1.2M pairs, and a 25-deep stack of those would put half a
    # gigabyte of scratch in /dev/shm. Whichever limit trips first evicts
    # from the oldest end.
    UNDO_LIMIT = 25
    UNDO_ROW_BUDGET = 5_000_000

    # Bumped by every write that changes what an analyst has CONCLUDED —
    # a tag on a row, a tag definition, a watchlist indicator or its hits —
    # so a dashboard can tell that its cached "Tagged findings: 0" was
    # counted before the analyst spent the morning tagging. The evidence
    # half of that question (rows imported, tables added or dropped) is
    # read off the `sources` table instead; the two together are
    # data_generation, which says why.
    #
    # A counter rather than a COUNT(*): row_tags and watchlist_hits are
    # both WITHOUT ROWID and can hold millions of rows, so counting them on
    # every board open would cost more than the widget being guarded. The
    # price of a counter is that every writer has to remember to bump it,
    # which is why the bump sits inside each write's own transaction and
    # why _bump_state_generation lists who calls it.
    STATE_GENERATION_KEY = "state_generation"

    def _apply_tag_change(self, *, tag_id: int, on: bool, source_id: int, scope: str,
                          target_sql: str | None = None, target_params: Sequence = (),
                          pairs: Sequence[Sequence[int]] | None = None,
                          count_ids: Sequence[int] | None = None,
                          record: bool = True) -> int:
        """The single write path behind every tag apply/remove.

        The target — the rows the analyst asked to change — arrives either
        as a SELECT yielding (source_id, rid) (`target_sql`, the whole-view
        paths) or as an explicit pair list (`pairs`, the selection paths).
        What actually *changes* is a subset either way: tagging skips rows
        that already carry the tag, untagging skips rows that don't.

        Recording that delta rather than the target is the whole reason
        undo can be correct. Undoing "tag these 200 rows" by deleting the
        tag from all 200 would strip it off the rows that already had it
        beforehand — a silent, unnoticeable corruption of the analyst's own
        triage state, which is the worst failure mode this tool has (the
        same trap `tag_view`'s `exclude` argument exists to avoid, one
        level up).

        The delta lands in a `v.undo_<n>` table and the change is then
        applied *from* that table, so the rows recorded and the rows
        written are the same set by construction rather than by two queries
        agreeing. Returns how many rows actually changed.

        CLAUDE.md invariant #7 — every tag write goes through here; nothing
        INSERTs or DELETEs row_tags directly.

        `record=False` is for writes the analyst did not make — a watchlist
        scan's auto-tags (`_scan_unit`). Those still move exactly the delta,
        but leave the analyst's Ctrl+Z stack alone: one entry per scanned
        (indicator, table) unit evicted the analyst's whole history the
        moment a scan had UNDO_LIMIT of them, and Ctrl+Z then stripped
        auto-tags off rows whose hits were still listed. An unrecorded
        write also CLAIMS its target rows out of any recorded entry that
        moved them the same way (`_claim_from_undo`): rows an analyst
        tagged that a scan then matched are the scan's as well, and undoing
        the analyst's change must not untag them — invariant #7's overlap
        rule, with the scan as the other writer. Pairs only; nothing
        unrecorded writes a whole view.

        Caller must hold self.lock and be inside a self.db transaction.

        Both ends of the write are checked first, in that same transaction:
        the tag, and every real table the rows belong to (the pairs' own
        ids, or `count_ids` — the members — on the whole-view paths). A
        window can still be showing a table or a tag another window has
        deleted, and its write used to land anyway: committed rows for an
        id nothing names any more, behind a request that then failed on
        the way out. RecordGone here means nothing was written."""
        if not record and pairs is None:
            raise ValueError("an unrecorded tag change takes explicit pairs")
        if not self.db.execute("SELECT 1 FROM tag_defs WHERE id=?", (tag_id,)).fetchone():
            raise RecordGone(f"Tag {tag_id} no longer exists in this case")
        self._require_sources({int(sid) for sid, _ in pairs} if pairs is not None
                              else {int(i) for i in (count_ids or ())})
        self._undo_seq += 1
        table = f"undo_{self._undo_seq}"
        if pairs is not None:
            # Explicit pairs go in through executemany and are then filtered
            # down in place, rather than being inlined as a VALUES list: a
            # select-all-then-tag can carry tens of thousands of rows, and
            # two bound parameters apiece blows past SQLITE_MAX_VARIABLE_NUMBER
            # long before it blows past anything else.
            self.db.execute(
                f"CREATE TABLE v.{q(table)} (source_id INTEGER NOT NULL, rid INTEGER NOT NULL, "
                f"PRIMARY KEY (source_id, rid)) WITHOUT ROWID"
            )
            self.db.executemany(
                f"INSERT OR IGNORE INTO v.{q(table)}(source_id, rid) VALUES (?,?)",
                [(int(sid), int(rid)) for sid, rid in pairs],
            )
            if not record:
                self._claim_from_undo(table, tag_id, on)
            # Delete the rows this change *wouldn't* move: already tagged on
            # the way in, not tagged on the way out.
            self.db.execute(
                f"DELETE FROM v.{q(table)} WHERE {'EXISTS' if on else 'NOT EXISTS'} "
                f"(SELECT 1 FROM row_tags rt WHERE rt.source_id=v.{q(table)}.source_id "
                f"AND rt.rid=v.{q(table)}.rid AND rt.tag_id=?)",
                (tag_id,),
            )
        else:
            # Keep the rows it *would* move — the mirror of the clause above.
            self.db.execute(
                f"CREATE TABLE v.{q(table)} AS "
                f"SELECT DISTINCT t.source_id AS source_id, t.rid AS rid FROM ({target_sql}) t "
                f"WHERE {'NOT EXISTS' if on else 'EXISTS'} "
                f"(SELECT 1 FROM row_tags rt WHERE rt.source_id=t.source_id "
                f"AND rt.rid=t.rid AND rt.tag_id=?)",
                (*target_params, tag_id),
            )
        changed = self.db.execute(f"SELECT count(*) FROM v.{q(table)}").fetchone()[0]
        self._write_tag_delta(table, tag_id, on)
        if not record:
            self.db.execute(f"DROP TABLE IF EXISTS v.{q(table)}")
        elif changed:
            self._undo.append({
                "undo_id": self._undo_seq, "table": table, "tag_id": tag_id,
                "on": on, "count": changed, "source_id": source_id, "scope": scope,
                "count_ids": list(count_ids) if count_ids is not None else None,
                "at": time.time(),
            })
            self._trim_undo()
        else:
            # Nothing moved (re-tagging already-tagged rows). An entry here
            # would make Ctrl+Z a no-op that still consumed a press, which
            # reads as "undo is broken" rather than "there was nothing to
            # undo" — so drop it rather than record it.
            self.db.execute(f"DROP TABLE IF EXISTS v.{q(table)}")
        return changed

    def _require_sources(self, source_ids: Iterable[int]) -> None:
        """RecordGone unless every id names a real table in this case. On
        whichever connection the caller is writing with (self.db), so it
        sees the same state the write that follows will."""
        ids = sorted(set(source_ids))
        if not ids:
            return
        have = {r[0] for r in self.db.execute(
            f"SELECT id FROM sources WHERE id IN ({','.join('?' * len(ids))})", ids)}
        missing = [i for i in ids if i not in have]
        if missing:
            raise RecordGone(f"Table {missing[0]} no longer exists in this case")

    def _write_tag_delta(self, table: str, tag_id: int, on: bool) -> None:
        """Apply (or re-apply) one delta table's rows in the given
        direction. Both the original write and its undo go through here —
        undo is the same operation with `on` flipped, not a second
        implementation of it.

        The state generation is bumped here (see STATE_GENERATION_KEY) so
        that a cached tag count goes stale on an undo exactly as it does on
        the write. This is the DELTA path, not the only path: the writes
        that replace tag state wholesale rather than by a recorded delta
        (delete_tag, import_session, start_new_session) bypass it by
        design and bump for themselves."""
        if on:
            cur = self.db.execute(
                f"INSERT OR IGNORE INTO row_tags(source_id, rid, tag_id) "
                f"SELECT source_id, rid, ? FROM v.{q(table)}",
                (tag_id,),
            )
        else:
            cur = self.db.execute(
                f"DELETE FROM row_tags WHERE tag_id=? AND (source_id, rid) IN "
                f"(SELECT source_id, rid FROM v.{q(table)})",
                (tag_id,),
            )
        if cur.rowcount > 0:
            self._bump_state_generation()

    def _bump_state_generation(self) -> None:
        """Record that this case's conclusions have moved on, for
        data_generation and the dashboard cache that reads it.

        Caller holds self.lock and is inside a self.db transaction — the
        bump commits with the rows it describes, so a reader never sees a
        generation that promises a write that rolled back.

        Called by every writer of the tables data_generation cannot afford
        to count: _write_tag_delta (so every tag apply and its undo),
        delete_tag and upsert_tag, import_session and start_new_session
        (which replace tag state wholesale, outside the delta path),
        add_indicator and delete_indicator, and _scan_unit when a scan
        actually changes an indicator's hits. Adding a writer of row_tags,
        tag_defs, watchlist or watchlist_hits means adding a bump; a board
        that shows a number from one of them is otherwise told it is
        current when it is not."""
        self.db.execute(
            "INSERT INTO case_settings(key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (self.STATE_GENERATION_KEY,))

    def _trim_undo(self) -> None:
        """Evict from the oldest end until both limits hold. Caller must
        hold self.lock and be inside a self.db transaction."""
        total = sum(e["count"] for e in self._undo)
        while self._undo and (len(self._undo) > self.UNDO_LIMIT or total > self.UNDO_ROW_BUDGET):
            dead = self._undo.pop(0)
            total -= dead["count"]
            self.db.execute(f"DROP TABLE IF EXISTS v.{q(dead['table'])}")

    def _claim_from_undo(self, target: str, tag_id: int, on: bool) -> None:
        """Take the rows in `target` (a v. table of (source_id, rid)) out of
        every recorded entry that moved `tag_id` in the same direction, for
        an unrecorded write that is putting those rows into that state too
        (see _apply_tag_change's `record`). An entry left with no rows is
        dropped — a Ctrl+Z that changes nothing reads as a broken undo.
        Caller holds self.lock, inside a transaction."""
        keep = []
        for e in self._undo:
            if e["tag_id"] == tag_id and e["on"] == on:
                self.db.execute(
                    f"DELETE FROM v.{q(e['table'])} WHERE (source_id, rid) IN "
                    f"(SELECT source_id, rid FROM v.{q(target)})")
                e["count"] = self.db.execute(f"SELECT count(*) FROM v.{q(e['table'])}").fetchone()[0]
                if not e["count"]:
                    self.db.execute(f"DROP TABLE IF EXISTS v.{q(e['table'])}")
                    continue
            keep.append(e)
        self._undo = keep

    def _drop_undo_for_tag(self, tag_id: int) -> None:
        """Deleting a tag definition takes its assignments with it, so every
        history entry naming that tag is now describing a change to
        something that no longer exists. Undoing one would resurrect rows
        pointing at a dead tag_id. Caller holds self.lock, inside a
        transaction."""
        keep = []
        for e in self._undo:
            if e["tag_id"] == tag_id:
                self.db.execute(f"DROP TABLE IF EXISTS v.{q(e['table'])}")
            else:
                keep.append(e)
        self._undo = keep

    def _undo_entry_label(self, entry: dict) -> str:
        row = self.db.execute("SELECT name FROM tag_defs WHERE id=?", (entry["tag_id"],)).fetchone()
        name = row["name"] if row else "deleted tag"
        n = entry["count"]
        verb = "Tag" if entry["on"] else "Untag"
        return f"{verb} {n:,} row{'' if n == 1 else 's'} · {name}"

    def undo_peek(self) -> dict:
        """What Ctrl+Z would undo, for the menu label and enabled state."""
        with self.lock:
            if not self._undo:
                return {"available": False, "depth": 0}
            e = self._undo[-1]
            return {
                "available": True, "depth": len(self._undo),
                "label": self._undo_entry_label(e), "tag_id": e["tag_id"],
                "on": e["on"], "count": e["count"], "source_id": e["source_id"],
            }

    def undo_last_tag_change(self) -> dict:
        """Reverse the most recent tag change, exactly and only over the
        rows it changed. Returns the source it touched (so the frontend
        knows whether the visible table even needs repainting) alongside
        the usual fresh counts."""
        with self.lock, self.db:
            if not self._undo:
                raise ValueError("Nothing to undo")
            e = self._undo[-1]
            label = self._undo_entry_label(e)
            self._write_tag_delta(e["table"], e["tag_id"], not e["on"])
            self._undo.pop()
            self.db.execute(f"DROP TABLE IF EXISTS v.{q(e['table'])}")
            ids = e["count_ids"]
            if ids is None:
                ids = [m["source_id"] for m in self._resolve_members_on(self.db, e["source_id"])]
            out = self._tag_counts_for_ids(ids)
        out.update({
            "undone": label, "affected": e["count"], "tag_id": e["tag_id"],
            "source_id": e["source_id"], "was_on": e["on"],
        })
        out.update({"next": self.undo_peek()})
        return out

    def set_tags(self, source_id: int, rids: list[int], tag_id: int, on: bool) -> dict:
        if not rids:
            return self.tag_counts(source_id)
        with self.lock, self.db:
            self._apply_tag_change(
                tag_id=tag_id, on=on, source_id=source_id, scope="rows",
                pairs=[(source_id, r) for r in rids],
            )
        return self.tag_counts(source_id)

    def set_tags_pairs(self, pairs: list[list[int]], tag_id: int, on: bool,
                       source_id: int | None = None) -> dict:
        """Same as set_tags, but for a merged view where selected rows can
        belong to different real source_ids — each pair is that row's own
        (source_id, rid), never the merge's synthetic negative id.

        `source_id` is the merge the selection was made in, when there is
        one, and it decides what the returned counts cover: the client
        stores them as that merge's whole-table counts (S.tagCountsAll),
        so they have to count EVERY member — counting only the members the
        selection happened to touch made a ribbon of 3 read 1. The same
        member list goes to undo, which recounts against it."""
        if not pairs:
            return {"counts": {}}
        count_ids = sorted({int(sid) for sid, _ in pairs})
        if source_id is not None and int(source_id) < 0:
            count_ids = [m["source_id"] for m in self._resolve_members(int(source_id))]
        with self.lock, self.db:
            self._apply_tag_change(
                tag_id=tag_id, on=on, source_id=int(pairs[0][0]), scope="rows",
                pairs=pairs, count_ids=count_ids,
            )
            return self._tag_counts_for_ids(count_ids)

    def tag_view(self, view_id: str, tag_id: int, on: bool, exclude: list[Iterable[int]] | None = None) -> dict:
        """Tag every row in a materialised view — the 'filter to it, then mark
        the lot' move that filtering exists for in the first place. Uses
        each row's own source_id from the view now, so this works unchanged
        for merged views (a single INSERT/DELETE, no per-member looping).

        `exclude` is a list of (source_id, rid) pairs to leave alone. It's
        what backs "select all, then uncheck a few, then tag": the frontend
        models a select-all as a flag plus an exclusion set rather than a
        materialised Set of every position, and without this it would have
        to fall back to sending millions of individual rids (or, worse, tag
        everything and then untag the exclusions — which would strip the tag
        off an excluded row that legitimately already had it). Excluded
        rows are by construction rows the analyst had on screen to uncheck,
        so this list is small; `affected` accounts for them without a
        second count, since they came out of this same view."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "group_virtual":
            return self._tag_virtual_group(handle, tag_id, on, exclude)
        if handle.get("kind") == "root_virtual":
            return self._tag_virtual_root(handle, tag_id, on, exclude)
        skip_sql, skip_params = self._exclude_clause(exclude, "source_id", "rid")
        # The handle was read unlocked, and a concurrent rebuild of the same
        # source evicts v.view_N in the window before the lock. Without this
        # the write raises "no such table" — an OperationalError, which
        # server.py maps to a 500 rather than the 409 the frontend rebuilds
        # on, so a bulk tag is lost instead of retried.
        with self.lock, self._dropped_view_is_expired(), self.db:
            changed = self._apply_tag_change(
                tag_id=tag_id, on=on, source_id=handle["source_id"], scope="view",
                target_sql=f"SELECT source_id, rid FROM v.{q(view_id)}{skip_sql}",
                target_params=skip_params,
                count_ids=[m["source_id"] for m in self._resolve_members_on(self.db, handle["source_id"])],
            )
        out = self.tag_counts(handle["source_id"])
        out["affected"] = max(0, handle["row_count"] - len(exclude or []))
        out["changed"] = changed
        return out

    @staticmethod
    def _exclude_clause(exclude: list[Iterable[int]] | None, sid_col: str, rid_col: str) -> tuple[str, list[int]]:
        """` WHERE (sid, rid) NOT IN ((?,?), ...)` for a small exclusion list,
        or ('', []) when there's nothing to exclude."""
        pairs = [(int(sid), int(rid)) for sid, rid in (exclude or [])]
        if not pairs:
            return "", []
        tuples = ",".join(["(?,?)"] * len(pairs))
        params: list[int] = []
        for sid, rid in pairs:
            params.extend((sid, rid))
        return f" WHERE ({sid_col}, {rid_col}) NOT IN ({tuples})", params

    def _tag_virtual_group(self, handle: dict, tag_id: int, on: bool,
                           exclude: list[Iterable[int]] | None = None) -> dict:
        member = handle["members"][0]
        sid = member["source_id"]
        src = self._source_lite(sid)
        member_from = self._member_from(src, member)
        where_sql, where_params = self._virtual_group_where(handle)
        # A virtual group is always one real source, so only that source's
        # rids in the exclusion list can possibly be in it.
        skip_rids = [int(rid) for s, rid in (exclude or []) if int(s) == sid]
        if skip_rids:
            where_sql += f" AND rid NOT IN ({','.join('?' * len(skip_rids))})"
            where_params = [*where_params, *skip_rids]
        with self.lock, self.db:
            changed = self._apply_tag_change(
                tag_id=tag_id, on=on, source_id=sid, scope="view",
                target_sql=f"SELECT ? AS source_id, rid FROM {member_from} WHERE {where_sql}",
                target_params=[sid, *where_params],
                count_ids=[m["source_id"] for m in self._resolve_members_on(self.db, sid)],
            )
        out = self.tag_counts(sid)
        out["affected"] = max(0, handle["row_count"] - len(skip_rids))
        out["changed"] = changed
        return out

    def _tag_virtual_root(self, handle: dict, tag_id: int, on: bool,
                          exclude: list[Iterable[int]] | None = None) -> dict:
        """Tagging the whole view is tagging the whole (unfiltered) source
        table directly — same shape as _tag_virtual_group, but with no
        column=value condition at all since a root_virtual view is every
        row. No materialise needed regardless of view size: ordering never
        enters into "tag every row"."""
        sid = handle["source_id"]
        table = self._source_lite(sid)["table_name"]
        skip_rids = [int(rid) for s, rid in (exclude or []) if int(s) == sid]
        skip_sql = f" WHERE rid NOT IN ({','.join('?' * len(skip_rids))})" if skip_rids else ""
        with self.lock, self.db:
            changed = self._apply_tag_change(
                tag_id=tag_id, on=on, source_id=sid, scope="view",
                target_sql=f"SELECT ? AS source_id, rid FROM {q(table)}{skip_sql}",
                target_params=[sid, *skip_rids],
                count_ids=[m["source_id"] for m in self._resolve_members_on(self.db, sid)],
            )
        out = self.tag_counts(sid)
        out["affected"] = max(0, handle["row_count"] - len(skip_rids))
        out["changed"] = changed
        return out

    def tag_counts(self, source_id: int) -> dict:
        member_ids = [m["source_id"] for m in self._resolve_members(source_id)]
        with self._reader() as ro:
            return self._tag_counts_for_ids(member_ids, ro)

    def _tag_counts_for_ids(self, member_ids: Sequence[int],
                            conn: sqlite3.Connection | None = None) -> dict:
        """Counts over an explicit member list. Split out of tag_counts
        because undo has to recount from inside its own open transaction
        (self.lock is not re-entrant) and over the exact member set the
        original change reported against — a merged view's ribbon counts
        every member, not just the one the changed rows happened to
        belong to.

        `conn` is which connection asks. tag_counts hands it a pooled
        reader — it is a top-level read and has no business on the writer
        (invariant #4) — while undo, which is mid-transaction and has to
        see its own uncommitted delete, keeps the default of self.db."""
        ids = [int(i) for i in member_ids]
        if not ids:
            return {"counts": {}}
        ph = ",".join("?" * len(ids))
        rows = (conn or self.db).execute(
            f"SELECT tag_id, count(*) n FROM row_tags WHERE source_id IN ({ph}) GROUP BY 1", ids,
        ).fetchall()
        return {"counts": {str(r["tag_id"]): r["n"] for r in rows}}

    def tag_counts_in_view(self, view_id: str) -> dict:
        """The same shape tag_counts returns, but counting only the rows
        inside one view — so the tag ribbon can answer "how many of these
        are tagged X" rather than "how many rows in the whole table are
        tagged X", which is a different and usually less interesting
        question once a filter or a search is on.

        Scope is the view exactly as built, tag filter included: a ribbon
        that quietly dropped one of the filters in play would be reporting a
        count for a view nobody is looking at. The frontend keeps the
        whole-table counts alongside these and shows both, rather than
        picking one and hoping the analyst infers which it meant.

        Cost is the same join tag_positions makes, with the same
        untagged-source short-circuit in front of it — one indexed probe per
        member — because this runs after every view build and an untagged
        source is the common case. An unfiltered root_virtual view is every
        row of the source by construction, so it skips straight to the
        plain per-source counts with no join at all."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        if handle.get("kind") == "root_virtual":
            return self.tag_counts(handle["source_id"])
        with self._reader() as ro, self._dropped_view_is_expired():
            if handle.get("kind") == "group_virtual":
                member = handle["members"][0]
                sid = member["source_id"]
                msrc = self._source_lite_on(ro, sid)
                where_sql, where_params = self._virtual_group_where(handle, ro)
                # `rid IN (subquery)` rather than a join: _virtual_group_where
                # builds unqualified column references (it has to — a derived
                # column reaches its sidecar through USING(rid)), and row_tags
                # has a `rid` of its own that would make them ambiguous.
                rows = ro.execute(
                    "SELECT tag_id, count(*) n FROM row_tags WHERE source_id=? AND rid IN "
                    f"(SELECT rid FROM {self._member_from(msrc, member)} WHERE {where_sql}) GROUP BY 1",
                    [sid, *where_params],
                ).fetchall()
                return {"counts": {str(r["tag_id"]): r["n"] for r in rows}}
            any_tags = any(
                ro.execute("SELECT 1 FROM row_tags WHERE source_id=? LIMIT 1", (m["source_id"],)).fetchone()
                for m in self._resolve_members_on(ro, handle["source_id"])
            )
            if not any_tags:
                return {"counts": {}}
            rows = ro.execute(
                f"SELECT rt.tag_id, count(*) n FROM v.{q(view_id)} vv "
                f"JOIN row_tags rt ON rt.rid = vv.rid AND rt.source_id = vv.source_id GROUP BY 1"
            ).fetchall()
        return {"counts": {str(r["tag_id"]): r["n"] for r in rows}}

    def tag_coverage_in_view(self, view_id: str, tag_id: int) -> dict:
        """How much of a view one tag already covers: {rows, tagged}.

        What the whole-view tag hotkey asks before it acts. "Tag everything
        in this view" only makes sense as a one-way action while something
        in the view is untagged; once the lot carries the tag, pressing the
        same keys again can only sensibly mean take it off. Answering that
        needs the count and the view's size together, and the caller needs
        both numbers anyway — a confirm that says "remove it from 1,204
        rows" is a different sentence from "tag 1,204 rows", and the
        analyst is entitled to read which one they are agreeing to before
        anything is written.

        Deliberately the same aggregate the ribbon already runs after every
        build (tag_counts_in_view), so this adds a shape of query, not a
        cost."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        counts = self.tag_counts_in_view(view_id)["counts"]
        return {"rows": int(handle["row_count"]),
                "tagged": int(counts.get(str(int(tag_id)), 0))}

    def set_note(self, source_id: int, rid: int, note: str) -> None:
        with self.lock, self.db:
            # The row has to be there, for the reason _apply_tag_change
            # checks its targets: a note posted from a window still showing
            # a deleted table was committed against an id nothing names.
            # A merge's notes live on its member rows (invariant #9), so
            # only a real table is a place a note can go.
            src = self.db.execute("SELECT table_name FROM sources WHERE id=?", (source_id,)).fetchone()
            if src is None:
                raise RecordGone(f"Table {source_id} no longer exists in this case")
            if not self.db.execute(f"SELECT 1 FROM {q(src['table_name'])} WHERE rid=?", (rid,)).fetchone():
                raise RecordGone(f"Table {source_id} has no row {rid}")
            if note.strip():
                self.db.execute(
                    "INSERT INTO row_notes(source_id, rid, note) VALUES (?,?,?) "
                    "ON CONFLICT(source_id, rid) DO UPDATE SET note=excluded.note",
                    (source_id, rid, note),
                )
            else:
                self.db.execute(
                    "DELETE FROM row_notes WHERE source_id=? AND rid=?", (source_id, rid)
                )

    def list_row_notes(self, limit: int = 500) -> dict:
        """Every row note in the case, with the table each one sits on — what
        the Notes page lists under the case narrative, so the rows an analyst
        annotated during triage are findable from the page they write the
        report on rather than only from the detail pane of the row itself.

        Joined to `sources` rather than read bare, because the label is the
        whole point of the listing ("Security.csv · Line 412"), and the join
        also drops notes whose table is gone — `drop_source` deletes them, so
        one surviving here would be a bug, but listing an unnameable row would
        be worse than omitting it.

        Merge parity (invariant #9) needs nothing here: a note taken on a row
        of a merged table is written against the member it came from, exactly
        like a tag, so it is already listed under that member's name and its
        (source_id, rid) opens the real table. The watchlist hits pane resolves
        the same way.

        `total` is the count before `limit`, so a case with more notes than the
        page shows can say so instead of quietly truncating. Ordered by table
        then line, which is the order the tables are listed in and the order
        the rows are read in."""
        limit = max(0, int(limit))
        with self._reader() as ro:
            total = ro.execute(
                "SELECT COUNT(*) FROM row_notes n JOIN sources s ON s.id = n.source_id"
            ).fetchone()[0]
            rows = ro.execute(
                "SELECT n.source_id, n.rid, n.note, s.name, s.nickname "
                "FROM row_notes n JOIN sources s ON s.id = n.source_id "
                "ORDER BY n.source_id, n.rid LIMIT ?",
                (limit,),
            ).fetchall()
        return {"total": int(total),
                "notes": [{"source_id": r["source_id"], "rid": r["rid"], "note": r["note"],
                           "source_name": r["nickname"] or r["name"]} for r in rows]}

    # ------------------------------------------------------------ layout/views

    def save_layout(self, source_id: int, payload: dict) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO layouts(source_id, payload) VALUES (?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload",
                (source_id, json.dumps(payload)),
            )

    def get_layout(self, source_id: int) -> dict | None:
        with self._reader() as ro:
            row = ro.execute(
                "SELECT payload FROM layouts WHERE source_id=?", (source_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    # ------------------------------------------------------- sql pane sub-tabs

    def list_sql_tabs(self) -> list[dict]:
        with self._reader() as ro:
            rows = ro.execute(
                "SELECT id, name, sql, pos FROM sql_tabs ORDER BY pos, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_sql_tab(self, name: str, sql: str = "") -> dict:
        """New tab lands at the right end of the strip (max(pos)+1)."""
        with self.lock, self.db:
            pos = (self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM sql_tabs").fetchone()[0])
            cur = self.db.execute(
                "INSERT INTO sql_tabs(name, sql, pos) VALUES (?,?,?)", (name, sql, pos),
            )
        return {"id": cur.lastrowid, "name": name, "sql": sql, "pos": pos}

    def update_sql_tab(self, tab_id: int, *, name: str | None = None, sql: str | None = None) -> dict:
        """Partial, same None-means-leave-alone convention as
        workspace.SavedFilters.update — a rename sends name, the editor's
        debounced autosave sends sql."""
        with self.lock, self.db:
            row = self.db.execute("SELECT id, name, sql, pos FROM sql_tabs WHERE id=?", (tab_id,)).fetchone()
            if row is None:
                raise KeyError(f"No SQL tab {tab_id}")
            rec = dict(row)
            if name is not None:
                rec["name"] = name
            if sql is not None:
                rec["sql"] = sql
            self.db.execute("UPDATE sql_tabs SET name=?, sql=? WHERE id=?", (rec["name"], rec["sql"], tab_id))
        return rec

    def delete_sql_tab(self, tab_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sql_tabs WHERE id=?", (tab_id,))

    def reorder_sql_tabs(self, ordered_ids: list[int]) -> list[dict]:
        """Renumbers pos to match the given id order. Unlike
        workspace.SavedFilters.reorder (which has to preserve the relative
        order of filters *outside* the group being moved), there's only one
        flat strip here, so any id not listed just sorts after the listed
        ones, keeping its own relative order."""
        with self.lock, self.db:
            known = {r["id"] for r in self.db.execute("SELECT id FROM sql_tabs").fetchall()}
            seq = [i for i in ordered_ids if i in known]
            self.db.executemany(
                "UPDATE sql_tabs SET pos=? WHERE id=?", [(p, i) for p, i in enumerate(seq)],
            )
            # Anything unlisted keeps its order but sits after the listed run.
            rest = [i for i in sorted(known - set(seq))]
            self.db.executemany(
                "UPDATE sql_tabs SET pos=? WHERE id=?", [(len(seq) + p, i) for p, i in enumerate(rest)],
            )
        return self.list_sql_tabs()

    # -------------------------------------------- plugin tab/panel UI state

    def get_plugin_ui_state(self, mount_key: str) -> dict | None:
        """What a plugin mount saved here, or None if it never has.

        A payload that no longer parses (a hand-edited case file, a
        truncated copy) reads as nothing saved rather than raising: every
        caller's fallback is "start fresh", which is the right answer to a
        payload nobody can read anyway."""
        with self._reader() as ro:
            row = ro.execute(
                "SELECT payload, saved_at FROM plugin_ui_state WHERE mount_key=?",
                (mount_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        return {"mount_key": mount_key, "payload": payload, "saved_at": row["saved_at"]}

    def set_plugin_ui_state(self, mount_key: str, payload) -> dict:
        """Replace a mount's saved state. Last write wins — two Winnows on
        one case file do not merge, and a UI spec is not something that
        could be merged meaningfully anyway.

        Refuses anything past PLUGIN_UI_STATE_MAX_BYTES: the cap is what
        keeps "save the definition" from drifting into "save the result"."""
        if not mount_key:
            raise ValueError("A plugin state key is required")
        blob = json.dumps(payload)
        size = len(blob.encode("utf-8"))
        if size > PLUGIN_UI_STATE_MAX_BYTES:
            raise ValueError(
                f"Plugin tab state is {size} bytes, over the "
                f"{PLUGIN_UI_STATE_MAX_BYTES}-byte cap — save the definition, not the rows")
        saved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO plugin_ui_state(mount_key, payload, saved_at) VALUES (?,?,?) "
                "ON CONFLICT(mount_key) DO UPDATE SET payload=excluded.payload, "
                "saved_at=excluded.saved_at",
                (mount_key, blob, saved_at),
            )
        return {"mount_key": mount_key, "payload": payload, "saved_at": saved_at}

    def clear_plugin_ui_state(self, mount_key: str) -> None:
        """The "start fresh" path. Deleting a row nobody has is fine."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM plugin_ui_state WHERE mount_key=?", (mount_key,))

    # ------------------------------------------ legacy filter-preset migration

    def get_case_notes(self) -> dict:
        """The case narrative Markdown. One row, read whole."""
        with self._reader() as ro:
            row = ro.execute("SELECT body, updated_at FROM case_notes WHERE id=1").fetchone()
        return {"body": row["body"] if row else "", "updated_at": row["updated_at"] if row else None}

    def set_case_notes(self, body: str) -> dict:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO case_notes(id, body, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
                (body or "", ts))
        return {"body": body or "", "updated_at": ts}

    # ------------------------------------------------------------ watchlist

    #: The columns the Watchlist's Export… writes (watchlist.js), in order.
    WATCHLIST_EXPORT_COLUMNS = ("value", "kind", "note", "hits")

    @classmethod
    def parse_indicator_list(cls, text: str, default_kind: str = "other") -> list[dict]:
        """The indicators in a file given to the Watchlist's Import…, as
        `{value, kind, note}` in file order, duplicates included (the
        import dedupes against the list, and against itself).

        A plain list is one indicator per line: blanks and #-comments
        dropped, an optional `value,kind` carrying its own type.

        The Watchlist's own Export… is not one: it is a CSV under a header
        row, `value,kind,note,hits`. Read as a plain list, that header came
        back as an indicator called "value", every kind as the rest of its
        line ("filename,,3") and a quoted value split at its comma. So a
        first line made of the export's column names, `value` first and at
        least two of them, is read as that header and everything after it
        as CSV — each row keeps its kind (an empty one takes
        `default_kind`) and its note, and `hits` is left to the scan to
        recount. Only that header switches: a plain list whose first
        indicator is the word "value" is one cell, not a header, and stays
        an indicator. static/js/watchlist.js mirrors this to count and
        preview the file; it never decides."""
        text = (text or "").lstrip("\ufeff")
        lines = text.splitlines(keepends=True)
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                header = [c.strip().lower() for c in next(csv.reader([line]), [])]
            except csv.Error:
                header = []
            if (len(header) >= 2 and header[0] == "value" and len(set(header)) == len(header)
                    and set(header) <= set(cls.WATCHLIST_EXPORT_COLUMNS)):
                out = []
                try:
                    for rec in csv.reader(io.StringIO("".join(lines[i + 1:]), newline="")):
                        row = dict(zip(header, rec))
                        value = (row.get("value") or "").strip()
                        if value:
                            out.append({"value": value,
                                        "kind": (row.get("kind") or "").strip() or default_kind,
                                        "note": row.get("note") or None})
                except csv.Error as e:
                    raise ValueError(f"Could not read the watchlist export: {e}") from None
                return out
            break
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            value, _, kind = line.partition(",")
            value = value.strip()
            if value:
                out.append({"value": value, "kind": kind.strip() or default_kind, "note": None})
        return out

    def add_indicator(self, value: str, kind: str = "other",
                      note: str | None = None, auto_tag_id: int | None = None) -> dict:
        value = (value or "").strip()
        if not value:
            raise ValueError("An indicator needs a value")
        with self.lock, self.db:
            # An exact duplicate is refused, not added twice. The imports
            # already dedupe by value; a second Add of the same value — the
            # retype after "nothing happened" — made a second row carrying
            # the same hits, and nothing ever told the analyst.
            if self.db.execute("SELECT 1 FROM watchlist WHERE value=?", (value,)).fetchone():
                raise ValueError(f"{value} is already on the watchlist")
            # Checked under the same lock as the insert. An id from a tag
            # list another case painted (or a tag deleted a moment ago) is
            # otherwise stored as-is, and every scan writes assignments
            # for it that no list shows.
            if auto_tag_id is not None and not self.db.execute(
                    "SELECT 1 FROM tag_defs WHERE id=?", (auto_tag_id,)).fetchone():
                raise ValueError(f"Auto-tag {auto_tag_id} is not a tag in this case")
            cur = self.db.execute(
                "INSERT INTO watchlist(value, kind, note, auto_tag_id, created_at)"
                " VALUES (?,?,?,?,?)",
                (value, kind or "other", note, auto_tag_id, time.strftime("%Y-%m-%dT%H:%M:%S")))
            # A watchlist widget lists one row per indicator, so the board's
            # answer changed the moment this one was added — before any
            # scan gives it hits.
            self._bump_state_generation()
        return self._indicator(cur.lastrowid)

    _INDICATOR_SQL = (
        "SELECT w.id, w.value, w.kind, w.note, w.auto_tag_id, COUNT(h.rid) AS hit_count"
        " FROM watchlist w LEFT JOIN watchlist_hits h ON h.watchlist_id = w.id"
    )

    @staticmethod
    def _indicator_dict(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "value": r["value"], "kind": r["kind"], "note": r["note"],
                "auto_tag_id": r["auto_tag_id"], "hit_count": r["hit_count"]}

    def _indicator(self, wid: int) -> dict:
        with self._reader() as ro:
            r = ro.execute(self._INDICATOR_SQL + " WHERE w.id=? GROUP BY w.id", (wid,)).fetchone()
        if r is None:
            raise KeyError(f"No indicator {wid}")
        return self._indicator_dict(r)

    def list_indicators(self) -> list[dict]:
        """Every indicator with its hit count: one LEFT JOIN … GROUP BY on
        the reader pool. This is behind /api/watchlist and the tab-badge
        poll, and as N+1 queries under self.lock it queued behind every
        scan and view build in progress (invariant #4)."""
        with self._reader() as ro:
            rows = ro.execute(self._INDICATOR_SQL + " GROUP BY w.id ORDER BY w.id").fetchall()
        return [self._indicator_dict(r) for r in rows]

    def delete_indicator(self, wid: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM watchlist WHERE id=?", (wid,))
            self.db.execute("DELETE FROM watchlist_hits WHERE watchlist_id=?", (wid,))
            # The scan record goes with it: watchlist.id is not
            # AUTOINCREMENT, so a row left behind would tell the next
            # indicator to take this id that it had already been scanned.
            self.db.execute("DELETE FROM watchlist_scans WHERE watchlist_id=?", (wid,))
            self._bump_state_generation()

    def _watchlist_match_sql(self, src: dict, cols: list[str], value: str) -> tuple[str, tuple]:
        """The rid SELECT for one indicator against one source: the two
        WHERE shapes _search_all_count_sql uses — the trigram index's bare
        `doc LIKE ?` when the source has one built and _fts_like_pattern
        accepts the value, the escaped blob LIKE otherwise — without its
        LIMIT. A scan is not a count: every matching rid is written to
        watchlist_hits and, for an auto-tag indicator, tagged, so it has to
        be complete — a capped scan would tag part of the matches and say
        nothing, the silent-partial-triage failure invariant #7 exists to
        prevent. Both shapes fold case for ASCII only, exactly as the
        `instr(lower(blob), lower(?))` scan this replaced did, so the hits
        are the same rows by construction."""
        table = q(src["table_name"])
        pattern = _fts_like_pattern(value)
        if src.get("has_fts") and pattern is not None:
            fts_ident = q("fts_" + str(src["id"]))
            return (f"SELECT rid FROM {table} WHERE rid IN "
                    f"(SELECT rowid FROM {fts_ident} WHERE doc LIKE ?) ORDER BY rid", (pattern,))
        return (f"SELECT rid FROM {table} WHERE ({_blob_expr(cols)}) LIKE ? ESCAPE '\\' ORDER BY rid",
                (f"%{_esc_like(value)}%",))

    def _scan_unit(self, src: dict, cols: list[str], ind: dict) -> tuple[int, bool] | None:
        """One (indicator, source) unit of a scan: the match on a reader
        connection, then one locked transaction that replaces that unit's
        slice of watchlist_hits and auto-tags the matches. Returns
        (match count, whether an auto-tag was written), or None when the
        unit wrote nothing: the indicator was deleted while the match ran
        — or its id was reused by another value, watchlist.id not being
        AUTOINCREMENT — or the source was dropped, or the store is closing.
        A dropped trigram index is not a skip: the match retries on the
        LIKE path. Any other database error is the caller's to surface.

        The match never holds self.lock. It is a full LIKE scan on a table
        with no index yet, and under the lock (where it used to run, once
        per indicator per table) it stalled every locked read — the tab
        strip, the ribbon counts, tagging — for the length of the whole
        scan. The write is one unit of committed work (invariant #4). It
        re-reads the indicator under the lock because the match's copy is
        stale by the time the write lands: hits written for a dead or
        reused id would attach to the wrong indicator. The auto-tag goes
        through _apply_tag_change inside the same transaction (invariant
        #7), so a unit's hits and its tags land together or not at all."""
        source_id, wid, value = src["id"], ind["id"], ind["value"]
        while True:
            sql, params = self._watchlist_match_sql(src, cols, value)
            try:
                with self._reader() as ro:
                    rids = [r[0] for r in ro.execute(sql, params)]
                break
            except sqlite3.OperationalError as e:
                # "no such table" is the one error a unit absorbs, and it
                # means one of two things. The trigram table: an index
                # rebuild dropped it between listing the source and now,
                # and the escaped LIKE gives the same rows without it. The
                # source's own table: the source was dropped mid-scan, and
                # there is nothing left to record the unit against. Anything
                # else — an I/O error, a corrupt index — surfaces as the
                # job's error: a unit quietly missing from the totals, with
                # the table's old hits left in place, would say "done" over
                # a scan that was not.
                if "no such table" not in str(e).lower():
                    raise
                if src.get("has_fts") and ("fts_" + str(source_id)) in str(e):
                    src = dict(src, has_fts=0)
                    continue
                return None
        with self.lock:
            # close() joins the scan worker before closing the connection,
            # but the join is bounded; a worker that outlived it must not
            # touch a connection that is going or gone.
            if self._scan_jobs.closing:
                return None
            with self.db:
                # The source too can have gone between the match and here:
                # drop_source deletes its hits, so a write for it now would
                # file this value's rows under a table that is not there.
                if not self.db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                    return None
                # The auto-tag through a join, so an indicator still naming
                # a tag that is gone (delete_tag clears it; this is the
                # belt to that brace) scans without tagging rather than
                # failing the unit.
                row = self.db.execute(
                    "SELECT w.value, t.id AS auto_tag_id FROM watchlist w "
                    "LEFT JOIN tag_defs t ON t.id = w.auto_tag_id WHERE w.id=?", (wid,)).fetchone()
                if row is None or row["value"] != value:
                    return None
                # What this unit is replacing, for the state generation
                # below. A range scan of this indicator's own hits in this
                # one source — the leading columns of the hits PK — not a
                # count of the table.
                before = self.db.execute(
                    "SELECT COUNT(*) FROM watchlist_hits WHERE watchlist_id=? AND source_id=?",
                    (wid, source_id)).fetchone()[0]
                self.db.execute("DELETE FROM watchlist_hits WHERE watchlist_id=? AND source_id=?",
                                (wid, source_id))
                # This unit looked at this table, and that is a different
                # fact from how many rows it matched. It lands in the same
                # transaction as the hits so "scanned" can never outlive
                # the rows it is a claim about (a record written after the
                # commit would survive a crash the hits did not).
                self.db.execute(
                    "INSERT INTO watchlist_scans(watchlist_id, source_id, scanned_at) VALUES (?,?,?)"
                    " ON CONFLICT(watchlist_id, source_id) DO UPDATE SET scanned_at=excluded.scanned_at",
                    (wid, source_id, time.strftime("%Y-%m-%dT%H:%M:%S")))
                tagged = False
                if rids:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO watchlist_hits(watchlist_id, source_id, rid) VALUES (?,?,?)",
                        [(wid, source_id, rid) for rid in rids])
                    if row["auto_tag_id"]:
                        # Unrecorded: the scan is not the analyst, and its
                        # tags stand while its hits do — see `record`.
                        self._apply_tag_change(tag_id=int(row["auto_tag_id"]), on=True, source_id=source_id,
                                               scope="rows", pairs=[(source_id, rid) for rid in rids],
                                               record=False)
                        tagged = True
                # A watchlist widget's number is the sum of the hit counts,
                # so a scan that finds different hits moves it with nothing
                # in `sources` changing. Bumped on a CHANGE rather than on
                # every unit: re-scanning an unchanged table finds the same
                # rids, and marking every board stale for that would teach
                # the analyst to ignore the mark. (Same count, different
                # rows means the table itself changed, and row_count has
                # already said so.)
                if before != len(rids):
                    self._bump_state_generation()
        return len(rids), tagged

    def watchlist_scan_sources(self) -> list[dict]:
        """The tables a scan would actually read — every real, finished,
        error-free source.

        One predicate, named, because two callers now depend on the same
        answer: the scan itself, and the profile-apply sheet, which tells
        an analyst how many tables seeding a watchlist is about to scan
        before they agree to it. A count that disagreed with the scan
        would be worse than no count.

        Merges are absent by construction (CLAUDE.md invariant #9 lists
        this exception): a merge has no src_N of its own, its rows belong
        to its members and are scanned there, so counting it would
        promise the analyst a table that is really two of the ones
        already in the list. A source still filling (`columns` is `'[]'`)
        is absent for the reason _iter_watchlist_scan gives below."""
        return [s for s in self.list_sources()
                if not s.get("is_merge") and not s.get("error") and s.get("columns")]

    def _iter_watchlist_scan(
        self, source_ids: list[int] | None = None, watchlist_ids: list[int] | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[int, int, dict | None]]:
        """The scan itself — every indicator against every real source, or
        the given subsets. Merges are skipped by design: a merge has no
        src_N of its own, its rows are its members' and are scanned there
        (CLAUDE.md invariant #9 lists this); errored sources too. Yields
        `(0, total, None)` once up front, then `(scanned, total, info)` after
        each source with info = {source_id, matched: {watchlist_id: count},
        auto_tagged: bool} for the units that wrote (see _scan_unit), so a
        caller can report progress. `stop` is asked between (indicator,
        source) units and ends the scan early. One implementation behind
        the synchronous scan_source/scan_all (profile apply, tests) and the
        background job the UI runs, like _iter_search_all_sources.

        A source whose columns are still `'[]'` is skipped as well, both
        here and through scan_source: that is the shell every fill leaves
        standing while it works — `_create_source_shell` writes it up
        front and the caller fills `columns` in at the end, so an ingest
        job and a save-view-as-table copy each keep one alive for their
        whole run — and `_require_columns` already reads that gap as
        "still importing". A scan cannot read it either: `_blob_expr([])`
        is empty, so the unit would compile `WHERE () LIKE ?`, whose
        syntax error is not the "no such table" `_scan_unit` absorbs and
        so would end the whole job in error with every later table
        unscanned. Skipping costs this run nothing — those rows aren't
        committed yet — but what picks them up afterwards depends on which
        fill it was. An import's tables are scanned as soon as the job
        reports done (jobs.js starts that scan); a save-view-as-table copy
        has no post-copy scan, so a scan that overlapped one leaves that
        table with no hits of its own until the next scan someone starts.
        Nothing is missed case-wide either way — a subset's rows are
        copies of the parent's, which the same run did scan — and giving
        the copy a scan of its own is a change of its own: a subset table
        has never been scanned on creation, overlapping scan or not."""
        sources = self.watchlist_scan_sources()
        if source_ids is not None:
            wanted = set(source_ids)
            sources = [s for s in sources if s["id"] in wanted]
        indicators = self.list_indicators()
        if watchlist_ids is not None:
            wanted = set(watchlist_ids)
            indicators = [i for i in indicators if i["id"] in wanted]
        total = len(sources)
        yield 0, total, None
        for scanned, src in enumerate(sources, 1):
            if scanned > 1:
                # The same scheduling courtesy the search-all sweep takes
                # between tables: back-to-back full scans must not pin a
                # core against interactive requests.
                time.sleep(0.02)
            matched: dict[int, int] = {}
            auto_tagged = False
            if indicators:
                cols = [c["name"] for c in self._base_cols(src)]
                if not src.get("has_fts"):
                    self._ensure_fts_building(src["id"])   # this scan takes the LIKE path; the next gets the index
                for ind in indicators:
                    if stop is not None and stop():
                        return
                    unit = self._scan_unit(src, cols, ind)
                    if unit is not None:
                        matched[ind["id"]] = unit[0]
                        auto_tagged = auto_tagged or unit[1]
            yield scanned, total, {"source_id": src["id"], "matched": matched, "auto_tagged": auto_tagged}

    def scan_source(self, source_id: int, watchlist_ids: list[int] | None = None) -> dict:
        """Synchronous scan of one source for every indicator (or the given
        ones): the profile-apply path and tests; the UI runs the job below.
        A merge (negative id) has no src_N of its own and scans as nothing
        — its rows are its members', scanned there. So does a table that
        is still filling (columns `'[]'`; see _iter_watchlist_scan).
        Idempotent: a source's hits for an indicator are replaced each
        scan. Returns per-indicator match counts."""
        matched: dict[int, int] = {}
        for _scanned, _total, info in self._iter_watchlist_scan([source_id], watchlist_ids):
            if info:
                matched.update(info["matched"])
        return {"source_id": source_id, "matched": matched}

    def scan_all(self, watchlist_ids: list[int] | None = None) -> dict:
        total: dict[int, int] = {}
        for _scanned, _total, info in self._iter_watchlist_scan(None, watchlist_ids):
            for wid, n in (info["matched"] if info else {}).items():
                total[wid] = total.get(wid, 0) + n
        return {"matched": total}

    # ----------------------------------------------- the scan as a job

    WATCHLIST_SCAN_SLOT = "watchlist"   # one scan at a time for the whole case

    _EMPTY_SEED = {"sources": [], "indicators": [], "pairs": 0}

    def seed_watchlist_from_search_all(self, job_id: int,
                                       watchlist_ids: list[int] | None = None) -> dict:
        """What a Search-all sweep already proved, written as scan records
        so the scan behind "Add to watchlist" does not read those tables a
        second time.

        The sweep and the scan ask a table the same question. A sweep
        counts `(blob) LIKE '%term%'` — or the trigram index's `doc LIKE`
        — per source and stops at a cap; a scan runs those same two shapes
        uncapped and writes the rids (_watchlist_match_sql's docstring
        names the shapes it shares with _search_all_count_sql, and
        _search_all_term_counts reaches that function through its
        single-term path, so a per-term count and a scan compile the same
        WHERE). A sweep that READ a table and found no row holding a term
        has therefore established the whole of what a scan of that
        (indicator, table) pair would: nothing matches, and the pair has
        been looked at. What this writes is the write _scan_unit makes for
        a clean pair — the pair's hits replaced by none, a watchlist_scans
        upsert in the same transaction — which is what makes this a
        shortcut rather than a second kind of answer.

        What it refuses to trust, each for its own reason:

        - **A sweep that is not a plain OR of positive terms**
          (_swept_or_terms). Under mixed AND/NOT the terms constrain each
          other, so "no row matched the query" says nothing about whether
          a row holds one of its terms.
        - **A source the sweep did not read end to end.** `read` is only
          recorded when the count returned (see _iter_search_all_sources):
          a table dropped mid-sweep, an index swapped under it, or a
          source still filling — whose empty column list will not compile
          a blob expression — leaves no record, and absence is not proof.
        - **A source whose row count has moved since.** Source tables are
          never mutated after ingest (invariant #1), so this should be
          unreachable. It is here because "clean" is a sentence an analyst
          puts in a report, and the cost of being sure is one integer
          comparison.
        - **A source that matched with no per-term breakdown** — the sweep
          knows something in it matched and not which term, which is the
          one shape that proves nothing per term.
        - **An indicator whose value is not one of the swept terms.** Exact
          strings, both already stripped; an analyst who edited the box
          after the sweep gets a real scan rather than a guess.
        - **Any source not covered for EVERY named indicator.** A table
          holding a hit for one of them is read in full regardless, and
          that read rewrites every pair in it — so seeding there would be
          work rather than work saved. Which is also where the saving
          stops: it is per TABLE, not per pair. A table where 49 of 50
          indicators are clean is still read for all 50, because skipping
          pairs inside a table a scan is already reading would mean
          teaching the scan loop about exceptions, for a table it has to
          open anyway.

        Returns `{"sources": [ids answered without a read], "indicators":
        [ids covered], "pairs": n}`, counted from what was actually
        written — `sources` is what start_watchlist_scan_job then takes
        out of its own scope.
        """
        with self._search_job_lock:
            job = self._search_job
            # An errored sweep is refused whole. Its per-source records are
            # individually sound (each one is a count that returned), but
            # the exception came from somewhere this does not model, and
            # the safe direction to be wrong in is "scan the table".
            if job is None or job["job_id"] != job_id or job["error"]:
                return dict(self._EMPTY_SEED)
            swept = _swept_or_terms(job["query"], job["terms"])
            # Copied under the lock: a sweep still running appends to this
            # as it goes, and what it has read so far is proof already.
            read = {sid: dict(r) for sid, r in job["read"].items()}
        if not swept or not read:
            return dict(self._EMPTY_SEED)
        proven = set(swept)
        indicators = self.list_indicators()
        if watchlist_ids is not None:
            wanted = set(watchlist_ids)
            indicators = [i for i in indicators if i["id"] in wanted]
        values = [(i["id"], (i["value"] or "").strip()) for i in indicators]
        if not values or not all(v in proven for _wid, v in values):
            return dict(self._EMPTY_SEED)

        scannable = {s["id"]: s for s in self.watchlist_scan_sources()}
        covered: list[int] = []
        for sid, r in read.items():
            src = scannable.get(sid)
            # Not a table this scan would read at all (a merge, an errored
            # or still-filling source) — there is no pair to record.
            if src is None or src["row_count"] != r["row_count"]:
                continue
            matched = r["matched_terms"]
            if matched is None or any(v in matched for _wid, v in values):
                continue
            covered.append(sid)
        if not covered:
            return dict(self._EMPTY_SEED)

        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        done: list[int] = []
        pairs = 0
        emptied = 0
        for sid in sorted(covered):
            # One source is one unit of committed work, the rule every
            # long-running writer here follows (invariant #4) — not the
            # whole loop under one lock.
            with self.lock, self.db:
                if not self.db.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone():
                    continue           # dropped between the sweep and here
                wrote = 0
                for wid, value in values:
                    # Re-read under the lock, for _scan_unit's reason:
                    # watchlist.id is not AUTOINCREMENT, so an id deleted
                    # and reused since the listing above would file this
                    # value's answer under whatever value took the id.
                    row = self.db.execute("SELECT value FROM watchlist WHERE id=?", (wid,)).fetchone()
                    if row is None or row["value"] != value:
                        continue
                    cur = self.db.execute(
                        "DELETE FROM watchlist_hits WHERE watchlist_id=? AND source_id=?", (wid, sid))
                    emptied += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    self.db.execute(
                        "INSERT INTO watchlist_scans(watchlist_id, source_id, scanned_at) VALUES (?,?,?)"
                        " ON CONFLICT(watchlist_id, source_id) DO UPDATE SET scanned_at=excluded.scanned_at",
                        (wid, sid, stamp))
                    wrote += 1
                if wrote:
                    done.append(sid)
                    pairs += wrote
        if emptied:
            # Hits went away, so a watchlist widget's number moved — the
            # same condition _scan_unit bumps on, and for the same reason.
            with self.lock, self.db:
                self._bump_state_generation()
        return {"sources": done, "indicators": [wid for wid, _v in values], "pairs": pairs}

    def start_watchlist_scan_job(self, source_ids: list[int] | None = None,
                                 watchlist_ids: list[int] | None = None,
                                 from_search_all: int | None = None) -> dict:
        """Runs the scan on a daemon thread and answers at once with the
        job's snapshot — the Add button used to sit in one POST for the
        length of a scan of every table, with the new row invisible until
        it ended. One live job per case (_JobRegistry): starting another
        asks the running one to stop, folds its remaining scope into the
        new job (below) and makes its id unpollable, so a poller on the
        old id gets None → 404 → stops. The record carries
        progress (scanned/total), per-indicator and per-table match totals
        as they land, and which tables an auto-tag was written to — the
        client invalidates its row caches for the open one.

        `from_search_all` is a search-all job id, and the one thing an
        "Add to watchlist" from a finished sweep has that nothing else
        does: the sweep just asked every table the same question this scan
        is about to. seed_watchlist_from_search_all writes the pairs it
        proved clean and names the tables that answers whole, and those
        come out of this job's scope — so adding fifty IOCs off a sweep of
        twenty tables reads the two or three the sweep found something in,
        not all twenty again. `seeded` rides in the record so the UI can
        say how much did not have to be re-read.

        The scope narrowing is the first thing a displaced scan undoes: the
        fold below widens this job to cover whatever the scan it replaced
        still owed, and that scan was not promised anything by a sweep. The
        seeded records stay true either way — they are facts about pairs,
        not about this job — so the worst case is a scan that re-derives
        some of them."""
        seeded = self.seed_watchlist_from_search_all(from_search_all, watchlist_ids) \
            if from_search_all is not None else dict(self._EMPTY_SEED)
        if seeded["pairs"]:
            answered = set(seeded["sources"])
            rest = [s["id"] for s in self.watchlist_scan_sources() if s["id"] not in answered]
            if source_ids is not None:
                wanted = set(source_ids)
                rest = [sid for sid in rest if sid in wanted]
            source_ids = rest
        job, old = self._scan_jobs.start(self.WATCHLIST_SCAN_SLOT, {
            "source_ids": None if source_ids is None else [int(s) for s in source_ids],
            "watchlist_ids": None if watchlist_ids is None else [int(w) for w in watchlist_ids],
            "scanned": 0, "total": 0, "matched": {}, "by_source": {}, "auto_tagged": [],
            "seeded": {"tables": len(seeded["sources"]), "pairs": seeded["pairs"]},
            "elapsed_ms": None,
        })
        if old is not None and self._scan_jobs.request_discard(old):
            # The scan this one displaces was still running: it stops at
            # its next unit, and its remaining scope is this job's. Without
            # that, a scoped scan's work would be left half done — an Add's
            # indicator scanned against three tables of twelve, its count
            # and its auto-tags partial with nothing to say so — every time
            # another Add, an import landing or Search-all's "Add to
            # watchlist" started a scan behind it. Re-doing the units it
            # finished is safe (a unit replaces its slice). None wins on
            # either axis: the union of a source-scoped scan and an
            # indicator-scoped one is the whole case, a superset cheaper
            # than a second job shape. A scan that already finished widens
            # nothing — its work is complete. The job's scope is read by
            # the worker and by snapshots, both of which begin below.
            job["source_ids"] = _union_scope(job["source_ids"], old["source_ids"])
            job["watchlist_ids"] = _union_scope(job["watchlist_ids"], old["watchlist_ids"])
        t = threading.Thread(target=self._watchlist_scan_worker, args=(job,), daemon=True)
        job["thread"] = t
        t.start()
        return self._scan_job_snapshot(job)

    def _watchlist_scan_worker(self, job: dict) -> None:
        """Cancellation is cooperative and checked between (indicator,
        source) units — the registry's discard flag doubles as the stop
        flag, since the scan has no result to drop — and close() sets the
        registry closing before it joins this thread."""
        t0 = time.time()
        reg = self._scan_jobs

        def stop() -> bool:
            with reg.lock:
                return reg.closing or bool(job["discard"])

        try:
            for scanned, total, info in self._iter_watchlist_scan(job["source_ids"], job["watchlist_ids"], stop):
                with reg.lock:
                    job["scanned"] = scanned
                    job["total"] = total
                    if info:
                        for wid, n in info["matched"].items():
                            job["matched"][wid] = job["matched"].get(wid, 0) + n
                        job["by_source"][info["source_id"]] = sum(info["matched"].values())
                        if info["auto_tagged"]:
                            job["auto_tagged"].append(info["source_id"])
        except Exception as e:  # noqa: BLE001 — surfaced to the UI as job.error
            reg.finish(job, "error", error=str(e), elapsed_ms=int((time.time() - t0) * 1000))
            return
        reg.finish(job, "cancelled" if stop() else "done", elapsed_ms=int((time.time() - t0) * 1000))

    def _scan_job_snapshot(self, job: dict) -> dict:
        with self._scan_jobs.lock:
            return {
                "job_id": job["job_id"], "status": job["status"],
                "scanned": job["scanned"], "total": job["total"],
                "matched": dict(job["matched"]), "by_source": dict(job["by_source"]),
                "auto_tagged": list(job["auto_tagged"]),
                # Tables a Search-all sweep answered for this scan, so they
                # were left out of its scope (see from_search_all above).
                "seeded": dict(job.get("seeded") or {"tables": 0, "pairs": 0}),
                "error": job["error"], "elapsed_ms": job["elapsed_ms"],
                "source_ids": job["source_ids"], "watchlist_ids": job["watchlist_ids"],
            }

    def get_watchlist_scan_job(self, job_id: int) -> dict | None:
        """Snapshot of the live job, or None once a newer scan has replaced
        it (server.py: 404, the poller stops)."""
        job = self._scan_jobs.get(job_id)
        return None if job is None else self._scan_job_snapshot(job)

    def cancel_watchlist_scan_job(self, job_id: int) -> bool:
        """Asks a running scan to stop at its next unit. A finished or
        unknown job is a miss (False), not an error."""
        job = self._scan_jobs.get(job_id)
        return job is not None and self._scan_jobs.request_discard(job)

    def running_watchlist_scan_jobs(self) -> int:
        """How many scans are running — server.py's idle-shutdown hold
        counts them with the ingest and view jobs."""
        return self._scan_jobs.running()

    def wait_for_watchlist_scan_job(self, job_id: int, timeout: float | None = None) -> dict | None:
        """Blocks until that job finishes. Tests only."""
        job = self._scan_jobs.get(job_id)
        if job is None:
            return None
        job["done_event"].wait(timeout)
        return self._scan_job_snapshot(job)

    def import_watchlist_from_case(self, path: str) -> dict:
        """Copy another case file's watchlist into this one — the standing
        indicator set an analyst carries between engagements usually lives
        in whichever case they worked last. Read-only against the other
        .db (never opened as a Store — no lock contention, no migration of
        someone else's file); deduped by exact value against what's
        already here; kind and note carried, auto_tag_id deliberately NOT
        (it names a tag id in the OTHER case's tag_defs — meaningless
        here). A case file from before the watchlist existed simply has
        no table and imports zero."""
        if os.path.abspath(path) == os.path.abspath(self.path):
            raise ValueError("That is the currently open case")
        try:
            ro = sqlite3.connect(sqlite_peek_uri(path), uri=True)
        except sqlite3.Error as e:
            raise ValueError(f"Could not open case file: {e}")
        ro.row_factory = sqlite3.Row
        try:
            if not ro.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone():
                return {"added": 0, "skipped": 0}
            rows = ro.execute("SELECT value, kind, note FROM watchlist ORDER BY id").fetchall()
        except sqlite3.Error as e:
            raise ValueError(f"Could not read that case's watchlist: {e}")
        finally:
            ro.close()
        have = {i["value"] for i in self.list_indicators()}
        added = 0
        for r in rows:
            value = (r["value"] or "").strip()
            if not value or value in have:
                continue
            self.add_indicator(value, r["kind"] or "other", r["note"], None)
            have.add(value)
            added += 1
        return {"added": added, "skipped": len(rows) - added}

    WATCHLIST_PREVIEW_CHARS = 240
    WATCHLIST_HITS_PER_SOURCE = 200

    def indicator_hits(self, wid: int, per_source_limit: int | None = None) -> dict:
        """An indicator's hits grouped by the table they landed in, with the
        context a hit list needs: which column held the indicator and that
        cell's value, plus the row as one line. Returns
        `{sources: [{source_id, source_name, count, shown}], hits: [...]}`:
        one entry per table with its EXACT hit count (a GROUP BY over
        watchlist_hits, never derived from the rows returned), tables in
        count-desc-then-id order, and for each the first `per_source_limit`
        hits in rid order — so a hot indicator's later tables are listed
        with their counts rather than vanishing past a global cap, and the
        pane can say "…and N more" per table. `hits` come in that same
        table order.

        The column is found by re-reading the hit rows — first column in
        table order whose text contains the value, case-insensitively.
        Nothing is stored, so old cases get the context too. The scan
        matched the row's columns joined into one blob, so a value that
        straddles two cells is a real hit no single column holds: `column`
        is None then, and `value` is the stretch of the joined row around
        the match. A row that no longer contains the value at all (an
        indicator edited since) gets neither.

        A pure read, on the reader pool (invariant #4): the hit list is
        opened while imports run, and the writer lock is theirs."""
        limit = self.WATCHLIST_HITS_PER_SOURCE if per_source_limit is None else max(1, int(per_source_limit))
        context: dict[tuple[int, int], dict] = {}
        with self._reader() as ro:
            names = {r["id"]: r["name"] for r in ro.execute("SELECT id, name FROM sources")}
            ind = ro.execute("SELECT value FROM watchlist WHERE id=?", (wid,)).fetchone()
            groups = ro.execute(
                "SELECT source_id, COUNT(*) AS c FROM watchlist_hits WHERE watchlist_id=?"
                " GROUP BY source_id ORDER BY c DESC, source_id", (wid,)).fetchall()
            rows: list[sqlite3.Row] = []
            for g in groups:
                rows.extend(ro.execute(
                    "SELECT source_id, rid FROM watchlist_hits WHERE watchlist_id=? AND source_id=?"
                    " ORDER BY rid LIMIT ?", (wid, g["source_id"], limit)).fetchall())
            needle = (ind["value"] if ind else "").lower()
            by_source: dict[int, list[int]] = {}
            for r in rows:
                by_source.setdefault(r["source_id"], []).append(r["rid"])
            for sid, rids in by_source.items():
                try:
                    src = self._source_lite_on(ro, sid)
                except (KeyError, ValueError):
                    continue
                cols = [c["name"] for c in self._base_cols(src)]
                if not cols or not src.get("table_name"):
                    continue
                sel = ", ".join(q(c) for c in cols)
                blob = _blob_expr(cols)
                table = q(src["table_name"])
                for i in range(0, len(rids), 500):
                    chunk = rids[i:i + 500]
                    found = ro.execute(
                        f"SELECT rid, {sel}, lower({blob}) AS blob_ FROM {table} "
                        f"WHERE rid IN ({','.join('?' * len(chunk))})",
                        chunk).fetchall()
                    for row in found:
                        cells = [(c, row[k + 1]) for k, c in enumerate(cols)]
                        col = val = None
                        for c, v in cells:
                            if v is not None and needle and needle in str(v).lower():
                                col, val = c, str(v)
                                break
                        if col is None and needle:
                            # The scan's own test, on the scan's own blob.
                            at = (row["blob_"] or "").find(needle)
                            if at >= 0:
                                lo = max(0, at - 40)
                                val = (row["blob_"] or "")[lo:at + len(needle) + 40]
                        preview = " | ".join(str(v) for _, v in cells if v is not None and str(v).strip())
                        context[(sid, row["rid"])] = {
                            "column": col,
                            "value": val[:self.WATCHLIST_PREVIEW_CHARS] if val is not None else None,
                            "preview": preview[:self.WATCHLIST_PREVIEW_CHARS],
                        }
        sources = [{
            "source_id": g["source_id"],
            "source_name": names.get(g["source_id"], f"source {g['source_id']}"),
            "count": g["c"],
            "shown": min(g["c"], limit),
        } for g in groups]
        hits = []
        for r in rows:
            key = (r["source_id"], r["rid"])
            hits.append({"source_id": r["source_id"], "rid": r["rid"],
                         "source_name": names.get(r["source_id"], f"source {r['source_id']}"),
                         **context.get(key, {"column": None, "value": None, "preview": ""})})
        return {"sources": sources, "hits": hits}

    # ------------------------------------- what the counts cannot say alone

    #: Hits above which the overlap pass is skipped rather than run. It is
    #: two linear scans of watchlist_hits plus a dict keyed by every
    #: distinct flagged row, which is milliseconds and a few MB at the
    #: sizes a watchlist reaches in practice (a 9k-hit case scans in ~10 ms)
    #: and neither at a million. Skipping says so — `overlap_checked` False
    #: — rather than reporting "no duplicates", which would be a claim the
    #: pass never made.
    WATCHLIST_OVERLAP_MAX_HITS = 250_000

    def watchlist_overlaps(self) -> dict:
        """Which indicators are flagging the same rows as each other, and
        how many rows the case is actually holding.

        Two indicators can cover identical row sets — `mimikatz` and
        `mimikatz.exe` match the same 2,323 rows — and nothing in a list of
        per-indicator counts says so: the summary adds them and reports
        twice the findings the case has. Returns

            {total_hits, distinct_rows, overlap_checked, relations}

        where `total_hits` is the sum of the per-indicator counts (what the
        list adds up to), `distinct_rows` the number of distinct
        `(source_id, rid)` rows behind them — the honest count of findings —
        and `relations` one entry per ordered pair that overlaps
        completely: `{id, other_id, relation, shared}` with `relation` in
        `same` (identical row sets), `subset` (every row of `id` is also
        `other_id`'s) or `superset` (the other way). Both directions of
        every pair are emitted, so a renderer can look an indicator up
        rather than search.

        Complete overlap only, deliberately. A partial one — two indicators
        sharing some rows — is ordinary and says nothing worth acting on;
        an identical or contained set is the case where one of the two
        entries is buying the analyst nothing, which is the thing worth a
        line on the row and a way to merge them.

        An indicator with no hits never appears here. `{}` is a subset of
        everything, so an unscanned entry would be reported as redundant
        against every other one — a claim about rows nobody has looked at.

        Linear, not pairwise: the candidate supersets of an indicator are
        the intersection, over its own rows, of the sets of indicators on
        each row, so one pass over `watchlist_hits` answers for every
        indicator at once. The pairwise self-join it replaces is
        O(hits x indicators-per-row) rows out of SQLite and needs a sort on
        (source_id, rid) per side.

        A pure read on the reader pool (invariant #4) — the watchlist tab
        opens while imports run."""
        with self._reader() as ro:
            total = ro.execute("SELECT COUNT(*) FROM watchlist_hits").fetchone()[0]
            if total > self.WATCHLIST_OVERLAP_MAX_HITS:
                return {"total_hits": total, "distinct_rows": None,
                        "overlap_checked": False, "relations": []}
            on_row: dict[tuple[int, int], list[int]] = {}
            counts: dict[int, int] = {}
            for r in ro.execute("SELECT watchlist_id, source_id, rid FROM watchlist_hits"):
                wid = r["watchlist_id"]
                on_row.setdefault((r["source_id"], r["rid"]), []).append(wid)
                counts[wid] = counts.get(wid, 0) + 1

            relations: list[dict] = []

            def flush(wid: int | None, cand: set[int] | None) -> None:
                # `cand` is every indicator that appeared on EVERY one of
                # wid's rows — i.e. every superset of wid, itself included.
                if wid is None or not cand:
                    return
                for other in sorted(cand):
                    if other == wid:
                        continue
                    if counts[other] == counts[wid]:
                        # Identical sets. The other one's own pass emits
                        # the mirror entry, so only this direction here.
                        relations.append({"id": wid, "other_id": other,
                                          "relation": "same", "shared": counts[wid]})
                    else:
                        # wid is strictly inside other, which other's own
                        # pass can never discover (other is not a subset of
                        # wid), so both directions are written here.
                        relations.append({"id": wid, "other_id": other,
                                          "relation": "subset", "shared": counts[wid]})
                        relations.append({"id": other, "other_id": wid,
                                          "relation": "superset", "shared": counts[wid]})

            current: int | None = None
            cand: set[int] | None = None
            for r in ro.execute(
                    "SELECT watchlist_id, source_id, rid FROM watchlist_hits ORDER BY watchlist_id"):
                wid = r["watchlist_id"]
                if wid != current:
                    flush(current, cand)
                    current, cand = wid, set(on_row[(r["source_id"], r["rid"])])
                elif cand:
                    # Already empty stays empty; the rest of this
                    # indicator's rows cannot put a candidate back.
                    cand.intersection_update(on_row[(r["source_id"], r["rid"])])
            flush(current, cand)
        return {"total_hits": total, "distinct_rows": len(on_row),
                "overlap_checked": True, "relations": relations}

    def watchlist_overview(self) -> dict:
        """Everything the watchlist tab needs to describe its own state:
        the indicators, how much of the case each has actually been scanned
        against, and the overlap picture above.

        Each indicator carries `scanned_sources` — how many of the tables a
        scan would read (`watchlist_scan_sources`, so merges and
        still-filling imports are excluded from both halves of the
        fraction) have a completed unit on record for it. With
        `scan_targets` beside it that is the difference between "scanned,
        found nothing" and "never scanned", which a `0` cannot express and
        an analyst needs before writing "not present" in a report. A scan
        record naming a table that is no longer scannable is not counted:
        the fraction has to be readable against today's case."""
        targets = {s["id"] for s in self.watchlist_scan_sources()}
        scanned: dict[int, int] = {}
        with self._reader() as ro:
            for r in ro.execute("SELECT watchlist_id, source_id FROM watchlist_scans"):
                if r["source_id"] in targets:
                    scanned[r["watchlist_id"]] = scanned.get(r["watchlist_id"], 0) + 1
        indicators = self.list_indicators()
        for ind in indicators:
            ind["scanned_sources"] = scanned.get(ind["id"], 0)
        return {"indicators": indicators, "scan_targets": len(targets),
                **self.watchlist_overlaps()}

    #: Rows the latest-hits pane opens on. Enough to fill the pane and see
    #: what the case is about; the per-indicator pane is where paging
    #: through one indicator's hits happens.
    LATEST_HITS = 60

    def latest_hits(self, configs: dict[int, dict] | None = None,
                    limit: int | None = None) -> dict:
        """The most recent flagged rows across every indicator, newest
        first — what the watchlist has found, before anything is selected.

        One row per flagged ROW, not per hit: `watchlist_ids` lists every
        indicator that matched it, so two indicators covering the same rows
        read as one finding here rather than filling the pane twice.

        `configs` is the same per-source timeline configuration
        `build_timeline` takes, resolved by server.py against the analyst's
        timeline templates, so "when" and "what the row is" are the columns
        they already chose for this kind of artefact; a source without one
        falls back to its first datetime column and every column, exactly
        as the timeline does. A source with no datetime column at all sorts
        last (SQL NULLs are smallest, so DESC puts them there) rather than
        being dropped — its rows are findings too, they just cannot be
        placed in time.

        Capped per source before the union as well as after it, so the sort
        that orders the pane is over (sources x limit) rows rather than
        every hit in the case.

        Merges are absent for the reason invariant #9 gives: a merge has no
        `src_N`, its rows are its members' and were scanned and flagged
        there, so they appear here under the member that holds them.

        A pure read on the reader pool (invariant #4)."""
        limit = self.LATEST_HITS if limit is None else max(1, int(limit))
        configs = configs or {}
        with self._reader() as ro:
            flagged = {r["source_id"] for r in ro.execute(
                "SELECT DISTINCT source_id FROM watchlist_hits")}
        if not flagged:
            return {"rows": [], "limit": limit}

        branches: list[str] = []
        params: list[Any] = []
        for src in self.list_sources():
            sid = src["id"]
            if sid not in flagged or src.get("is_merge") or not src.get("table_name") or not src.get("columns"):
                continue
            col_names = {c["name"] for c in src["columns"]}
            dt_cols = [c["name"] for c in src["columns"] if c["type"] == "datetime"]
            cfg = configs.get(sid, {})
            ts_col = cfg.get("timestamp_column")
            if ts_col not in col_names:
                ts_col = dt_cols[0] if dt_cols else None
            # A derived column is already canonical, so it is used as-is —
            # the same split build_timeline makes, for the same reason.
            ts_expr = "NULL"
            if ts_col:
                ref = self._col_ref(src, ts_col, "s")
                ts_expr = ref if self._is_derived(src, ts_col) else ts_normalize_sql(ref)
            body_cols = [c for c in (cfg.get("body_columns") or []) if c in col_names]
            if not body_cols:
                # Every column EXCEPT the one already shown as "when" —
                # unlike the timeline, whose body is the only text on the
                # line, this pane has a timestamp column of its own, and
                # repeating it ate the width the rest of the row needed.
                # A template that names its body columns explicitly is
                # taken at its word, timestamp included.
                body_cols = [c["name"] for c in src["columns"] if c["name"] != ts_col]
            # Trimmed silently, unlike the Timeline: the preview is cut to
            # WATCHLIST_PREVIEW_CHARS long before the 495th column.
            body_expr = " || ' | ' || ".join(
                f"COALESCE({self._col_ref(src, c, 's')}, '')"
                for c in self._fit_body_cols(body_cols)) or "''"
            branches.append(
                "SELECT * FROM ("
                f"SELECT ? AS source_name, {int(sid)} AS source_id, s.rid AS rid, "
                f"{ts_expr} AS ts, ({body_expr}) AS body, "
                f"(SELECT GROUP_CONCAT(watchlist_id) FROM watchlist_hits "
                f" WHERE source_id={int(sid)} AND rid=s.rid) AS wids "
                f"FROM {self._from_clause(src, 's')} "
                f"WHERE s.rid IN (SELECT rid FROM watchlist_hits WHERE source_id={int(sid)}) "
                # Ordinals, not the `ts` alias: a source is free to have a
                # column of its own called ts, and the ORDER BY has to mean
                # the expression above it either way.
                "ORDER BY 4 DESC, 3 DESC LIMIT ?)")
            params.append(src["name"])
            params.append(limit)
        if not branches:
            return {"rows": [], "limit": limit}
        sql = " UNION ALL ".join(branches) + " ORDER BY 4 DESC, 2, 3 DESC LIMIT ?"
        params.append(limit)
        with self._reader() as ro:
            rows = ro.execute(sql, params).fetchall()
        return {"rows": [{
            "source_id": r["source_id"],
            "source_name": r["source_name"],
            "rid": r["rid"],
            "ts": r["ts"],
            "body": (r["body"] or "")[:self.WATCHLIST_PREVIEW_CHARS],
            "watchlist_ids": [int(x) for x in (r["wids"] or "").split(",") if x],
        } for r in rows], "limit": limit}

    def merge_indicators(self, keep_id: int, drop_id: int) -> dict:
        """Fold one indicator into another that already covers every row it
        found, and delete it.

        There is nothing else to merge: two indicators are a value, a note
        and an auto-tag, and the rows are already the keeper's. So the only
        thing that can be lost by removing the redundant entry is its
        auto-tag, and that moves across when the keeper has none — the rows
        it named are tagged already, and the next scan re-tags the same rows
        through the keeper rather than quietly stopping.

        Refuses unless every one of the dropped indicator's hits is also
        the keeper's, and says how many rows would have gone with it. The
        check is against the hits on disk, not against a cached overlap
        report: the report can be a scan old by the time the analyst acts
        on it, and dropping rows nothing else matches is exactly the
        silent-loss failure this codebase refuses elsewhere."""
        keep_id, drop_id = int(keep_id), int(drop_id)
        if keep_id == drop_id:
            raise ValueError("An indicator cannot be merged into itself")
        with self.lock, self.db:
            keep = self.db.execute("SELECT id, value, auto_tag_id FROM watchlist WHERE id=?",
                                   (keep_id,)).fetchone()
            drop = self.db.execute("SELECT id, value, auto_tag_id FROM watchlist WHERE id=?",
                                   (drop_id,)).fetchone()
            if keep is None or drop is None:
                raise KeyError("No such indicator")
            only_drop = self.db.execute(
                "SELECT COUNT(*) FROM watchlist_hits d WHERE d.watchlist_id=?"
                " AND NOT EXISTS (SELECT 1 FROM watchlist_hits k WHERE k.watchlist_id=?"
                "                 AND k.source_id=d.source_id AND k.rid=d.rid)",
                (drop_id, keep_id)).fetchone()[0]
            if only_drop:
                raise ValueError(
                    f"“{keep['value']}” does not cover {only_drop:,} row"
                    f"{'' if only_drop == 1 else 's'} “{drop['value']}” found — "
                    "removing it would lose them")
            moved = drop["auto_tag_id"] is not None and keep["auto_tag_id"] is None
            if moved:
                self.db.execute("UPDATE watchlist SET auto_tag_id=? WHERE id=?",
                                (drop["auto_tag_id"], keep_id))
            self.db.execute("DELETE FROM watchlist WHERE id=?", (drop_id,))
            self.db.execute("DELETE FROM watchlist_hits WHERE watchlist_id=?", (drop_id,))
            self.db.execute("DELETE FROM watchlist_scans WHERE watchlist_id=?", (drop_id,))
            self._bump_state_generation()
        # Outside the write transaction: _indicator reads through the pool,
        # and a reader inside an open writer transaction sees the old row
        # (invariant #4).
        return {"kept": self._indicator(keep_id), "dropped": drop_id,
                "dropped_value": drop["value"], "auto_tag_moved": moved}

    # --------------------------------------------------------- entity pivot

    def entity_pivot(self, value: str, limit: int = 60) -> dict:
        """Everywhere a value appears across every table: per-source match
        counts, which columns it landed in, and a merged, time-bucketed
        evidence stream. The counted scan is the same all-columns blob
        search-all uses; the timeline reuses TS_NORMALIZE so it lines up
        with the grid's own datetime handling — the same normalization a
        super-timeline would share. See docs/design/analysis-suite.md."""
        value = (value or "").strip()
        if not value:
            raise ValueError("Pivot needs a value")
        sources: list[dict] = []
        rows: list[dict] = []
        buckets: dict[str, int] = {}
        for meta in self.list_sources():
            if meta.get("is_merge") or meta.get("error"):
                continue
            src = self.get_source(meta["id"])
            base = self._base_cols(src)
            names = [c["name"] for c in base]
            blob = _blob_expr(names)
            table = q(src["table_name"])
            match = f"instr(lower({blob}), lower(?)) > 0"
            with self.lock:
                n = self.db.execute(
                    f"SELECT COUNT(*) c FROM {table} WHERE {match}", (value,)).fetchone()["c"]
            if not n:
                continue
            # Which columns held it — a general stand-in for "seen as
            # source / destination" that needs no per-tool hardcoding.
            in_cols = []
            for c in names:
                with self.lock:
                    hit = self.db.execute(
                        f"SELECT 1 FROM {table} WHERE instr(lower({q(c)}), lower(?)) > 0 LIMIT 1",
                        (value,)).fetchone()
                if hit:
                    in_cols.append(c)
            tcol = next((c["name"] for c in base if c.get("type") == "datetime"), None)
            sources.append({"source_id": src["id"], "source_name": src["name"],
                            "count": n, "columns": in_cols, "time_col": tcol})
            # A capped, time-ordered sample per source, merged client-side.
            ts_expr = f"TS_NORMALIZE({q(tcol)})" if tcol else "NULL"
            with self.lock:
                sample = self.db.execute(
                    f"SELECT rid, {ts_expr} AS ts, substr({blob}, 1, 200) AS preview"
                    f" FROM {table} WHERE {match} ORDER BY ts LIMIT ?", (value, limit)).fetchall()
            for r in sample:
                rows.append({"source_id": src["id"], "source_name": src["name"],
                             "rid": r["rid"], "ts": r["ts"], "preview": r["preview"]})
                if r["ts"]:
                    day = str(r["ts"])[:10]
                    buckets[day] = buckets.get(day, 0) + 1
        rows.sort(key=lambda r: (r["ts"] or "￿"))
        return {"value": value, "sources": sources,
                "rows": rows[:limit],
                "buckets": sorted(buckets.items())}

    # ------------------------------------------------------------ dashboard

    # ---- named dashboards (a case holds several) ----

    @staticmethod
    def _loads_widgets(text) -> list:
        try:
            w = json.loads(text)
            return w if isinstance(w, list) else []
        except (TypeError, ValueError):
            return []

    # ---- widget identity ----
    #
    # A widget was identified by its position in the JSON list, which a
    # drag-reorder rewrites — fine while a board was stateless, useless the
    # moment anything (a cached result, a per-widget setting) has to be
    # attached to one card rather than to "whatever is third". So every
    # widget carries an `id`: minted on write, and back-filled
    # DETERMINISTICALLY on read for the boards that already exist, so a
    # case nobody has edited since the upgrade still gets cache hits
    # instead of a new key every open.

    @staticmethod
    def _widget_fingerprint(w: dict) -> str:
        """A hash of the question a widget asks — its source and its query,
        which are exactly the arguments dashboard_widget_preview takes.

        Not the whole widget dict: renaming a card or making it two columns
        wide does not change a single number in the answer, and throwing
        away a result that took twelve seconds because someone fixed a typo
        in a title is a cost with nothing on the other side of it. Changing
        the SQL, the placeholder or the source does change the answer, and
        that is what this catches."""
        if not isinstance(w, dict):
            return "0"
        key = {"source": w.get("source") or "sql", "query": w.get("query") or {}}
        # A `signals` card asks its questions in `cells`, not in `query` —
        # so that is where its answer can go out of date. Added only when
        # present, or every board in every existing case would fingerprint
        # differently on first read and throw its whole cache away.
        # The label rides along, unlike a card's title: a cell's label is
        # IN the answer (the payload is label/value pairs, and the card
        # reads each cell's drill back off it by label), so renaming one
        # does change the result in a way a repaint would get wrong.
        if w.get("cells"):
            key["cells"] = [{"label": c.get("label") or "", "source": c.get("source") or "sql",
                             "query": c.get("query") or {}}
                            for c in w["cells"] if isinstance(c, dict)]
        return hashlib.sha1(
            json.dumps(key, sort_keys=True, default=str).encode("utf-8", "replace")).hexdigest()[:16]

    @classmethod
    def _mint_widget_ids(cls, widgets: list) -> list:
        """Give every widget on a board an id it keeps, in place.

        Ids already present are left alone — a board round-trips through
        the client on every edit, so re-minting would orphan the cache of
        every widget on it each time a card moved. A duplicate (two copies
        of one widget pasted into a profile, say) is re-minted: two cards
        sharing an id would share a cached result, and marking one "live"
        would quietly refresh the other.

        Returns a new list of new dicts rather than stamping the caller's:
        the widgets arriving here are often a plugin's registered board or
        a profile's, and those are shared objects that get copied into
        every case that asks for them."""
        out, seen = [], set()
        for i, w in enumerate(widgets):
            if not isinstance(w, dict):
                out.append(w)
                continue
            w = dict(w)
            wid = w.get("id")
            if not isinstance(wid, str) or not wid or wid in seen:
                wid = cls._backfill_widget_id(w, i)
                while wid in seen:
                    wid = "w" + os.urandom(6).hex()
                w["id"] = wid
            seen.add(wid)
            out.append(w)
        return out

    @classmethod
    def _backfill_widget_id(cls, w: dict, index: int) -> str:
        """The id an existing board's widget gets on read: derived from the
        widget and its position, so two reads of an unchanged board agree
        and the cache written under one open is found by the next. It stops
        being derived the first time the board is written — from then on it
        is just the id the widget carries."""
        return f"w{index}-{cls._widget_fingerprint(w)[:10]}"

    @classmethod
    def _with_widget_ids(cls, widgets: list) -> list:
        for i, w in enumerate(widgets):
            if isinstance(w, dict) and not (isinstance(w.get("id"), str) and w.get("id")):
                w["id"] = cls._backfill_widget_id(w, i)
        return widgets

    # ------------------------------------------------------- case variables

    VARIABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

    def list_variables(self) -> list[dict]:
        with self._reader() as ro:
            rows = ro.execute(
                "SELECT name, value, description, required FROM case_variables ORDER BY required DESC, name").fetchall()
        return [{"name": r["name"], "value": r["value"], "description": r["description"] or "",
                 "required": bool(r["required"])} for r in rows]

    def get_variables(self) -> dict:
        """{name: value} — the shape a plugin reads (req.variables)."""
        return {v["name"]: v["value"] for v in self.list_variables()}

    def set_variable(self, name: str, value: str | None = None, description: str | None = None,
                     required: bool | None = None) -> dict:
        """Upsert. Omitted fields keep their stored value, so a plugin
        setting a value never clears the description a profile gave it."""
        name = (name or "").strip()
        if not self.VARIABLE_NAME_RE.match(name):
            raise ValueError("A variable name is a letter followed by letters, digits, _ . or - (max 64)")
        if value is not None and len(str(value)) > 4000:
            raise ValueError("Variable values are capped at 4000 characters")
        with self.lock, self.db:
            row = self.db.execute("SELECT value, description, required FROM case_variables WHERE name=?",
                                  (name,)).fetchone()
            self.db.execute(
                "INSERT OR REPLACE INTO case_variables(name, value, description, required, updated_at)"
                " VALUES (?,?,?,?,?)",
                (name,
                 str(value) if value is not None else (row["value"] if row else ""),
                 description if description is not None else (row["description"] if row else None),
                 int(bool(required)) if required is not None else (row["required"] if row else 0),
                 time.strftime("%Y-%m-%dT%H:%M:%S")))
        return next(v for v in self.list_variables() if v["name"] == name)

    def delete_variable(self, name: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM case_variables WHERE name=?", (name,))

    def seed_variables(self, defs: list[dict]) -> list[str]:
        """A profile's declared variables land as rows — created with the
        declared default when absent, description/required refreshed when
        present, an existing VALUE never overwritten (re-applying a profile
        must not wipe what the analyst typed). Returns the names that are
        required and still empty, for the caller to prompt for."""
        for d in defs or []:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "").strip()
            if not self.VARIABLE_NAME_RE.match(name):
                continue
            with self.lock:
                exists = self.db.execute("SELECT 1 FROM case_variables WHERE name=?", (name,)).fetchone()
            self.set_variable(name, None if exists else (d.get("default") or ""),
                              description=d.get("description") or d.get("label") or "",
                              required=bool(d.get("required")))
        return [v["name"] for v in self.list_variables() if v["required"] and not v["value"]]

    # ------------------------------------------------------- plugin tables
    #
    # A plugin's own tables, inside the case file. Not a source: they carry
    # no `sources` row, so the grid, the sidebar and merges never see them —
    # they are a plugin's private store for things that belong to THIS case
    # and should travel with it when the .db is handed over (an LLM chat
    # transcript that has to render offline is the case that prompted them).
    #
    # Machine-level plugin state stays in `req.storage` (workspace JSON);
    # analysis output an analyst should browse still goes through
    # `ingest_rows`, which makes a real source. This is the third thing:
    # case-scoped, plugin-private, queryable.
    #
    # NAMED `plugin:<fs_name>:<table>`, with a separator neither half can
    # contain. `plugin_<fs>_<name>` was ambiguous exactly where this
    # codebase lives: plugin `mft_usn` table `cache` and plugin `mft` table
    # `usn_cache` both produced `plugin_mft_usn_cache`, so one plugin could
    # read and drop another's table by accident. Identifiers are always
    # quoted, so a colon costs nothing.
    #
    # This keeps two plugins from colliding. It is NOT a security boundary:
    # a plugin is arbitrary Python holding `req.store`, and can do anything
    # to the case file that Winnow can (docs/writing-plugins.md §12).

    PLUGIN_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

    @staticmethod
    def _plugin_ns(fs_name: str) -> str:
        """The plugin half of the name. Deliberately permissive: fs_name is
        whatever the analyst named the folder or file, and a plugin called
        `chat-gpt` loads and serves routes perfectly well — it must not then
        get a permanent 400 from req.table(). Only what breaks the naming
        scheme is refused."""
        fs = (fs_name or "").strip().lower()
        if not fs:
            raise ValueError("No plugin name")
        if ":" in fs or '"' in fs or len(fs) > 64:
            raise ValueError(f"Bad plugin name {fs!r} — no ':' or '\"', max 64 characters")
        return fs

    def _plugin_table(self, fs_name: str, name: str) -> str:
        """The real table name. The table half is the author's own choice,
        so it is held to a strict shape; both halves reach SQL as an
        identifier (invariant #5) and are quoted at every use."""
        nm = (name or "").strip().lower()
        if not self.PLUGIN_TABLE_RE.match(nm):
            raise ValueError(
                f"Bad table name {nm!r} — a letter, then letters, digits or _ (max 41)")
        return f"plugin:{self._plugin_ns(fs_name)}:{nm}"

    @staticmethod
    def _sub_table(sql: str, table: str) -> str:
        """Replace `{table}` with the quoted table name, but never inside a
        string literal: `UPDATE {table} SET meta = '{table}'` has to store
        the two braces, not the table's name. Single-quote literals are the
        only string form SQLite standardises, and '' is its escape — which
        this handles by simply toggling on every quote."""
        out = []
        i = 0
        in_str = False
        quoted = q(table)
        while i < len(sql):
            ch = sql[i]
            if ch == "'":
                in_str = not in_str
                out.append(ch)
                i += 1
                continue
            if not in_str and sql.startswith("{table}", i):
                out.append(quoted)
                i += len("{table}")
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def plugin_table_create(self, fs_name: str, name: str, columns: str) -> str:
        """CREATE TABLE IF NOT EXISTS with the given column definitions.

        Checks first on the reader pool, and only takes the writer lock when
        there is something to create — this is documented as the call every
        request makes, and taking the writer lock unconditionally would put
        a routine read behind whatever holds it (a long import, compact()'s
        VACUUM)."""
        table = self._plugin_table(fs_name, name)
        if not isinstance(columns, str) or not columns.strip():
            raise ValueError("Give the columns, e.g. 'id INTEGER PRIMARY KEY, role TEXT'")
        if self._plugin_table_exists(table):
            return table
        with self.lock, self.db:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {q(table)} ({columns})")
        return table

    def _plugin_table_exists(self, table: str) -> bool:
        with self._reader() as db:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone() is not None

    def plugin_table_exists(self, fs_name: str, name: str) -> bool:
        return self._plugin_table_exists(self._plugin_table(fs_name, name))

    def plugin_tables(self, fs_name: str) -> list[str]:
        """This plugin's table names, without the namespace prefix."""
        prefix = f"plugin:{self._plugin_ns(fs_name)}:"
        like = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%"
        with self._reader() as db:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\' "
                "ORDER BY name", (like,)).fetchall()
        return [r["name"][len(prefix):] for r in rows]

    def plugin_table_write(self, fs_name: str, name: str, sql: str, params: Sequence = ()) -> int:
        """One statement on the writer connection, with `{table}` standing
        in for this plugin's table so an author never quotes an identifier
        by hand. Returns the number of rows changed (0 for statements that
        change none, DDL included — sqlite3 reports -1 there).

        The substitution is a convenience, not a sandbox: SQL naming
        another PLUGIN's table runs. Isolation between plugins is by NAMING
        (they cannot collide by accident), and nothing more — see the block
        comment above.

        Evidence is the exception, and not a stylistic one. Invariant #1
        says a source table is never mutated, and invariant #2's virtual
        paging carve-out is only exact BECAUSE of it: `pos = rid - 1` holds
        while rids stay contiguous, so a plugin deleting rows from a src_
        table would silently page and tag the wrong rows everywhere, with no
        error. A plugin has plenty of legitimate reasons to write; none of
        them is into the evidence."""
        table = self._plugin_table(fs_name, name)
        with self.lock, self.db:
            # Scoped to this one statement, and to the writer connection the
            # lock we hold makes ours alone for its duration.
            self.db.set_authorizer(self._refuse_evidence_writes)
            try:
                cur = self.db.execute(self._sub_table(str(sql), table), tuple(params or ()))
            except sqlite3.DatabaseError as e:
                if "not authorized" in str(e).lower():
                    raise ValueError(
                        "A plugin may not write to source or analyst tables (invariant #1). "
                        "Use your own plugin table — the {table} placeholder.") from e
                raise
            finally:
                _clear_authorizer(self.db)
            return max(0, cur.rowcount if cur.rowcount is not None else 0)

    # src_<id>, drv_<id> and the tables analyst work lives in. A plugin
    # reads these freely (plugin_query); writing to them is what invariants
    # #1 and #2 forbid.
    _EVIDENCE_TABLE_RE = re.compile(
        r"^(src_\d+|drv_\d+|fts_\d+|row_tags|row_notes|sources|tag_defs)$", re.IGNORECASE)
    _WRITE_ACTIONS = frozenset({
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_INDEX,
    })

    @classmethod
    def _refuse_evidence_writes(cls, action, arg1, _arg2, _db, _trigger):
        """Authorizer for a plugin's own statement. Reads the parsed action
        and the table it names, so no spelling — a comment, a case change,
        a quoted alias — changes the answer, which a text scan cannot say."""
        if action in cls._WRITE_ACTIONS and arg1 and cls._EVIDENCE_TABLE_RE.match(str(arg1)):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def plugin_table_insert(self, fs_name: str, name: str, rows: "list[dict] | dict") -> int:
        """Insert one row or many. Column names come from the dicts, so they
        are quoted (invariant #5) and the values are always bound. Rows may
        list their columns in any order, as long as it is the same set."""
        table = self._plugin_table(fs_name, name)
        items = [rows] if isinstance(rows, dict) else list(rows or [])
        if not items:
            return 0
        cols = list(items[0].keys())
        if not cols:
            raise ValueError("A row needs at least one column")
        wanted = set(cols)
        for r in items:
            if set(r.keys()) != wanted:
                raise ValueError("Every row in one insert needs the same columns")
        sql = (f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        with self.lock, self.db:
            self.db.executemany(sql, [tuple(r[c] for c in cols) for r in items])
        return len(items)

    def plugin_table_rows(self, fs_name: str, name: str, where: str = "", params: Sequence = (),
                          limit: int | None = 5000) -> list[dict]:
        """Rows as dicts, on the reader pool (invariant #4) — reading a chat
        transcript must not queue behind an import.

        `where` is the tail of the query (WHERE / ORDER BY / LIMIT), with
        values bound through `params`. A LIMIT in the tail is the author's,
        and is left alone; otherwise `limit` caps the result, and
        `limit=None` means no cap."""
        table = self._plugin_table(fs_name, name)
        tail = (where or "").strip()
        args = list(params or ())
        sql = f"SELECT * FROM {q(table)}" + (f" {tail}" if tail else "")
        if limit is not None and not re.search(r"(?i)\blimit\b", self._strip_literals(tail)):
            sql += " LIMIT ?"
            args.append(max(1, int(limit)))
        with self._reader() as db:
            return [dict(r) for r in db.execute(sql, tuple(args)).fetchall()]

    @staticmethod
    def _strip_literals(sql: str) -> str:
        """The statement with single-quoted literals blanked — so a LIMIT
        inside a string doesn't read as a LIMIT clause."""
        out, in_str = [], False
        for ch in sql:
            if ch == "'":
                in_str = not in_str
                out.append(" ")
            else:
                out.append(" " if in_str else ch)
        return "".join(out)

    def plugin_table_drop(self, fs_name: str, name: str) -> None:
        table = self._plugin_table(fs_name, name)
        with self.lock, self.db:
            self.db.execute(f"DROP TABLE IF EXISTS {q(table)}")

    def list_dashboards(self) -> list[dict]:
        """Each named dashboard with its widget count — the sidebar's
        Dashboards section. Widgets themselves are fetched per-board.

        On the reader pool (invariant #4): opening a board while 30 files
        are importing must not queue behind the ingest holding the writer
        lock."""
        with self._reader() as db:
            rows = db.execute(
                "SELECT id, name, widgets, pos, pinned FROM dashboards ORDER BY pos, id").fetchall()
        return [{"id": r["id"], "name": r["name"], "pos": r["pos"], "pinned": bool(r["pinned"]),
                 "widget_count": len(self._loads_widgets(r["widgets"]))} for r in rows]

    def set_dashboard_pinned(self, dashboard_id: int, pinned: bool) -> None:
        """Pinned = shown as a page tab in the top strip (next to SQL,
        Timeline…) as well as under the sidebar's Dashboards section."""
        with self.lock, self.db:
            if self.db.execute("UPDATE dashboards SET pinned=? WHERE id=?",
                               (1 if pinned else 0, dashboard_id)).rowcount == 0:
                raise KeyError(f"No dashboard {dashboard_id}")

    def get_dashboard(self, dashboard_id: int) -> list:
        """One board's widgets. Reader pool, same reason as list_dashboards.

        Widgets written before ids existed are given one here rather than
        on the next write — a board nobody has edited would otherwise never
        get a cache hit, which is precisely the board this change is for.
        The back-filled id is derived, so two reads agree (see
        _backfill_widget_id); the next write makes it permanent."""
        with self._reader() as db:
            row = db.execute("SELECT widgets FROM dashboards WHERE id=?", (dashboard_id,)).fetchone()
        if row is None:
            raise KeyError(f"No dashboard {dashboard_id}")
        return self._with_widget_ids(self._loads_widgets(row["widgets"]))

    def set_dashboard_widgets(self, dashboard_id: int, widgets: list) -> list:
        """Edited by hand, which also clears `origin`.

        `origin` means "these widgets are exactly what whoever stamped it
        wrote" — a plugin's add (`plugin:<fs_name>:<id>`) or a profile's
        apply (`profile:<name>`); see upsert_dashboard_by_name. That is
        the whole basis for re-adding or re-applying a board refreshing it
        without asking. The moment the analyst changes a card, there IS
        work of theirs to lose, so the board stops being anyone else's
        copy and both of those go back to asking."""
        if not isinstance(widgets, list):
            raise ValueError("A dashboard is a list of widgets")
        widgets = self._mint_widget_ids(widgets)
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE dashboards SET widgets=?, origin=NULL WHERE id=?",
                (json.dumps(widgets), dashboard_id))
            if cur.rowcount == 0:
                raise KeyError(f"No dashboard {dashboard_id}")
            self._prune_widget_cache(dashboard_id, widgets)
        return widgets

    def create_dashboard(self, name: str, widgets: list | None = None,
                         origin: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("A dashboard needs a name")
        if len(name) > 200:
            raise ValueError("That dashboard name is too long")
        widgets = self._mint_widget_ids(widgets or [])
        with self.lock, self.db:
            pos = self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM dashboards").fetchone()[0]
            cur = self.db.execute(
                "INSERT INTO dashboards(name, widgets, pos, origin) VALUES (?,?,?,?)",
                (name, json.dumps(widgets), pos, origin))
            did = cur.lastrowid
        return {"id": did, "name": name, "pos": pos, "widget_count": len(widgets)}

    def rename_dashboard(self, dashboard_id: int, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("A dashboard needs a name")
        if len(name) > 200:
            raise ValueError("That dashboard name is too long")
        # A rename is a hand edit like changing a card: the board stops
        # being the plugin's copy, so a later add of the offered board asks
        # before replacing it rather than refreshing silently.
        with self.lock, self.db:
            if self.db.execute("UPDATE dashboards SET name=?, origin=NULL WHERE id=?",
                               (name, dashboard_id)).rowcount == 0:
                raise KeyError(f"No dashboard {dashboard_id}")

    def delete_dashboard(self, dashboard_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM dashboards WHERE id=?", (dashboard_id,))
            # The board's cached results go with it. They are keyed by its
            # id, which SQLite hands out again after a delete — leaving them
            # would show the previous board's numbers on the next one.
            self.db.execute("DELETE FROM dashboard_widget_cache WHERE dashboard_id=?", (dashboard_id,))

    def reorder_dashboards(self, ordered_ids: list[int]) -> None:
        with self.lock, self.db:
            known = {r[0] for r in self.db.execute("SELECT id FROM dashboards")}
            for i, did in enumerate([d for d in ordered_ids if d in known]):
                self.db.execute("UPDATE dashboards SET pos=? WHERE id=?", (i, did))

    def find_dashboard_by_name(self, name: str) -> dict | None:
        """The board that name would land on, if any — so a caller can ask
        before replacing one it did not create (see the plugin-board add
        route). NOCASE, matching upsert_dashboard_by_name's own lookup.

        `origin` is what makes that question answerable: a board stamped
        with the same plugin board this caller is about to write is that
        caller's own earlier copy, and refreshing it discards nothing the
        analyst wrote."""
        with self._reader() as ro:
            row = ro.execute(
                "SELECT id, name, widgets, origin FROM dashboards WHERE name=? COLLATE NOCASE",
                (name,)).fetchone()
        if not row:
            return None
        try:
            n = len(json.loads(row["widgets"]) or [])
        except (TypeError, ValueError):
            n = 0
        return {"id": row["id"], "name": row["name"], "widget_count": n,
                "origin": row["origin"]}

    def upsert_dashboard_by_name(self, name: str, widgets: list,
                                 origin: str | None = None) -> dict:
        """Create-or-replace a dashboard by name — how a profile applies its
        board (a second apply of the same profile refreshes rather than
        duplicates).

        `origin` says whose widgets these now are, and is written every
        time — including as NULL. Whoever last wrote the board owns it: a
        plugin add stamps `plugin:<fs_name>:<id>`, a profile apply stamps
        `profile:<profile name>`, and a hand edit clears it to NULL
        (set_dashboard_widgets). A caller re-writing its OWN stamp is
        refreshing its own copy and says nothing; any other stamp, or
        none, means there is someone else's work here and the caller asks
        first. One rule, read from both ends, and it errs toward
        asking."""
        widgets = self._mint_widget_ids(widgets)
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT id FROM dashboards WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            if row:
                self.db.execute("UPDATE dashboards SET widgets=?, origin=? WHERE id=?",
                               (json.dumps(widgets), origin, row["id"]))
                self._prune_widget_cache(row["id"], widgets)
                return {"id": row["id"], "name": name}
        return self.create_dashboard(name, widgets, origin=origin)

    # ---- cached widget results ----

    def data_generation(self) -> str:
        """A cheap marker for "something a widget could have counted has
        changed". Not a checksum of the evidence — how many sources there
        are, how many rows they hold between them, the highest source id,
        and STATE_GENERATION_KEY, the counter the tag and watchlist writes
        bump.

        Between them those cover what moves a dashboard's numbers under it:
        an import finishing (row_count climbs), a table arriving or being
        dropped, the analyst tagging rows or clearing them for a second
        pass, a tag renamed or deleted, and an indicator or its hits
        changing. The evidence half is deliberately DERIVED from the
        `sources` table rather than bumped by each ingest path, because a
        derived marker cannot be forgotten by the next import path somebody
        writes; the conclusions half has to be a counter because its tables
        are too big to count on every board open, and it is bumped inside
        each write's own transaction (_bump_state_generation).

        What it is not: exact. A widget reading something else about the
        case — case notes, a derived column's values — can
        still go quietly out of date, and a marker that moved for those
        would mark every board stale every time anyone did anything. Its
        job is to let a stale number be SHOWN AND LABELLED, not to be
        cryptographically complete."""
        with self._reader() as db:
            n, rows, top = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(row_count), 0), COALESCE(MAX(id), 0) FROM sources"
            ).fetchone()
            tag = db.execute(
                "SELECT value FROM case_settings WHERE key=?", (self.STATE_GENERATION_KEY,)).fetchone()
        return f"{n}.{rows}.{top}.{tag['value'] if tag else '0'}"

    def _prune_widget_cache(self, dashboard_id: int, widgets: list) -> None:
        """Drop cached results the board no longer has a use for: a widget
        that was removed, and a widget whose question changed.

        Caller holds self.lock and is inside a self.db transaction — this
        runs in the same committed unit of work as the widget write, so a
        board and its cache are never briefly out of step."""
        keep = {(w.get("id"), self._widget_fingerprint(w))
                for w in widgets if isinstance(w, dict) and w.get("id")}
        rows = self.db.execute(
            "SELECT widget_id, fingerprint FROM dashboard_widget_cache WHERE dashboard_id=?",
            (dashboard_id,)).fetchall()
        dead = [(dashboard_id, r["widget_id"]) for r in rows
                if (r["widget_id"], r["fingerprint"]) not in keep]
        if dead:
            self.db.executemany(
                "DELETE FROM dashboard_widget_cache WHERE dashboard_id=? AND widget_id=?", dead)

    def get_dashboard_cache(self, dashboard_id: int, widgets: list | None = None) -> dict:
        """Every usable cached result for a board, keyed by widget id:
        {payload, ran_at, elapsed_ms, stale}.

        "Usable" excludes a result whose widget has changed since — the
        fingerprint check is the same one _prune_widget_cache applies on
        write, repeated here so a payload is never served for a question
        nobody asked, whichever door the widgets came in through.

        A result the case has moved on from IS returned, flagged
        `stale`. Hiding it would trade a number that was true an hour ago
        for no number at all; the board's job is to say which it is."""
        if widgets is None:
            widgets = self.get_dashboard(dashboard_id)
        want = {w["id"]: self._widget_fingerprint(w)
                for w in widgets if isinstance(w, dict) and w.get("id")}
        gen = self.data_generation()
        with self._reader() as db:
            rows = db.execute(
                "SELECT widget_id, fingerprint, generation, payload, ran_at, elapsed_ms"
                " FROM dashboard_widget_cache WHERE dashboard_id=?", (dashboard_id,)).fetchall()
        out = {}
        for r in rows:
            if want.get(r["widget_id"]) != r["fingerprint"]:
                continue
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                continue
            out[r["widget_id"]] = {"payload": payload, "ran_at": r["ran_at"],
                                   "elapsed_ms": r["elapsed_ms"],
                                   "stale": r["generation"] != gen}
        return out

    @staticmethod
    def _answered_in_full(payload) -> bool:
        """False when a `cells` payload carries a cell that did not answer.

        A widget that fails is reported and NOT cached, so the next open
        retries it rather than painting an error forever. A cell IS a
        widget — its own source, its own query, its own drill — so a card
        where one cell of ten errored is that same failure in a smaller
        box, and filing it would leave that cell reading "—" until
        somebody pressed ↻ Refresh. One rule for both: a partial answer
        is not a cached answer.

        The other half of the trade is real and deliberate. A card that
        can never answer in full — the five registry cells of Logging
        posture on a case with no RECmd batch — re-runs on every open
        instead of painting from the cache. That is exactly what a whole
        widget in the same state already costs, and it is the price of a
        transient failure healing itself the next time the board opens."""
        if not isinstance(payload, dict):
            return True
        return not any(e for e in (payload.get("cell_errors") or []))

    def cache_widget_result(self, dashboard_id: int, widget_id: str, payload: dict,
                            elapsed_ms: int = 0) -> dict | None:
        """Record what a widget just returned, so the next open paints it
        instead of running it again.

        The widget is looked up on the board by id: a run of something that
        is not on this board (the editor previewing an unsaved draft, a
        widget removed while its query was in flight) is not cached, rather
        than cached under a key nothing will ever match. Nor is a signals
        card one of whose cells failed (`_answered_in_full`). Returns the
        stamp the client shows, or None when nothing was written."""
        widgets = self.get_dashboard(dashboard_id)
        w = next((x for x in widgets if isinstance(x, dict) and x.get("id") == widget_id), None)
        if w is None or not self._answered_in_full(payload):
            return None
        # default=str for the same reason _widget_fingerprint uses it: a
        # hand-written widget can SELECT a BLOB (unhex, randomblob, CAST),
        # and json.dumps raises TypeError on the bytes rather than
        # returning them. The run already succeeded and the analyst already
        # has the number; refusing to FILE it would be answering a
        # successful query with a 500.
        rec = {"fingerprint": self._widget_fingerprint(w), "generation": self.data_generation(),
               "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "elapsed_ms": int(elapsed_ms or 0)}
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO dashboard_widget_cache"
                "(dashboard_id, widget_id, fingerprint, generation, payload, ran_at, elapsed_ms)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(dashboard_id, widget_id) DO UPDATE SET"
                " fingerprint=excluded.fingerprint, generation=excluded.generation,"
                " payload=excluded.payload, ran_at=excluded.ran_at, elapsed_ms=excluded.elapsed_ms",
                (dashboard_id, widget_id, rec["fingerprint"], rec["generation"],
                 json.dumps(payload, default=str), rec["ran_at"], rec["elapsed_ms"]))
        return {"ran_at": rec["ran_at"], "elapsed_ms": rec["elapsed_ms"], "stale": False}

    def refresh_dashboard(self, dashboard_id: int, widget_id: str | None = None) -> dict:
        """Re-run a board's widgets and cache what they return — the ↻ a
        cached number needs to be answerable for.

        Every widget, or the one named. A widget that fails is reported and
        NOT cached: the next open retries it rather than painting an error
        forever — and a signals card whose cells did not all answer is the
        same failure by the cell (`_answered_in_full`), so it is returned
        with the numbers it did get and no `ran_at`, which is how this and
        the preview route both say "nothing was filed". Runs come first
        and the cache write comes last, in one transaction, because a
        widget run reads through the reader pool and invariant #4 forbids
        that inside an open writer transaction."""
        widgets = self.get_dashboard(dashboard_id)
        if widget_id is not None:
            widgets = [w for w in widgets if isinstance(w, dict) and w.get("id") == widget_id]
            if not widgets:
                raise KeyError(f"No widget {widget_id} on dashboard {dashboard_id}")
        gen = self.data_generation()
        ran_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        results, writes = {}, []
        for w in widgets:
            if not isinstance(w, dict):
                continue
            t0 = time.time()
            try:
                payload = self.dashboard_widget_preview(w.get("source") or "sql", w.get("query") or {},
                                                       cells=w.get("cells"))
                ms = int((time.time() - t0) * 1000)
                # Encoded inside the same try, and with default=str: one
                # widget nobody can serialise must cost its own cache row,
                # not the whole board's — the widgets that ran before it
                # would otherwise have their results discarded with it.
                blob = json.dumps(payload, default=str)
            except (ValueError, KeyError, TypeError, sqlite3.Error) as e:
                results[w.get("id")] = {"error": str(e)}
                continue
            if not self._answered_in_full(payload):
                # The numbers that came back, none of them filed.
                results[w["id"]] = {"payload": payload, "elapsed_ms": ms}
                continue
            writes.append((dashboard_id, w["id"], self._widget_fingerprint(w), gen,
                           blob, ran_at, ms))
            results[w["id"]] = {"payload": payload, "ran_at": ran_at, "elapsed_ms": ms, "stale": False}
        if writes:
            with self.lock, self.db:
                self.db.executemany(
                    "INSERT INTO dashboard_widget_cache"
                    "(dashboard_id, widget_id, fingerprint, generation, payload, ran_at, elapsed_ms)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(dashboard_id, widget_id) DO UPDATE SET"
                    " fingerprint=excluded.fingerprint, generation=excluded.generation,"
                    " payload=excluded.payload, ran_at=excluded.ran_at, elapsed_ms=excluded.elapsed_ms",
                    writes)
        return {"ran_at": ran_at, "results": results}

    # Shorthands a SHIPPED dashboard uses so its SQL is portable across
    # cases — the table's src_<id> varies, but its header set doesn't.
    _TABLE_SHORTHANDS = {
        "evtx": "Event logs (EvtxECmd)",
        "mft": "NTFS $MFT (MFTECmd)",
        "usn": "USN journal $J (MFTECmd)",
        "registry": "Registry (RECmd batch)",
        "amcache": "Amcache — program entries",
        "prefetch": "Prefetch (PECmd)",
    }

    def _sources_for_header_set(self, hs_name: str) -> list:
        """Every non-merge source whose columns carry the named header set —
        the list form of _source_for_header_set, for the {{all:...}} union
        placeholder. A support bundle's logs arrive as many files (plus
        rotated copies) sharing one schema, so an overview query has to
        span them, not bind to whichever imported first."""
        from . import defaults
        want = dict(defaults.headers()["nicknames"]).get(hs_name)
        if not want:
            return []
        out = []
        for meta in self.list_sources():
            # A subset is a copy of rows another table already contributes
            # to the union — counting it too would count those rows twice.
            # (build_timeline and export_tagged_xlsx do NOT skip subsets: a
            # tag put on one is real work, which is why the copy starts
            # untagged — see save_view_as_source.)
            if meta.get("is_merge") or meta.get("error") or meta.get("origin") == SUBSET_ORIGIN:
                continue
            cols = {c["name"] for c in self._base_cols(self.get_source(meta["id"]))}
            if all(c in cols for c in want):
                out.append(meta)
        return out

    def _source_for_header_set(self, hs_name: str):
        """The first non-merge source whose columns carry every column the
        named header set defines — the same content binding lateral
        movement's defaults use. None if this case has no such table."""
        from . import defaults
        sets = dict(defaults.headers()["nicknames"])
        want = sets.get(hs_name)
        if not want:
            return None
        for meta in self.list_sources():
            if meta.get("is_merge") or meta.get("error"):
                continue
            cols = {c["name"] for c in self._base_cols(self.get_source(meta["id"]))}
            if all(c in cols for c in want):
                return meta
        return None

    def _resolve_table_placeholders(self, sql: str) -> str:
        """Substitute {{evtx}} / {{header_set:Full Name}} in a widget's SQL
        with the matching source's src_<id> table. A placeholder for a
        table this case doesn't have raises a friendly error the widget
        renders as an empty state — better than a SQL error."""
        import re

        def _hs_of(key: str) -> str:
            if key.lower().startswith("header_set:"):
                return key[len("header_set:"):].strip()
            return self._TABLE_SHORTHANDS.get(key.lower(), key)

        def repl(m):
            key = m.group(1).strip()
            # {{all:...}} -> a UNION ALL over every source matching the set,
            # parenthesised so `FROM {{all:...}} x` and bare-column refs work.
            #
            # Each branch NAMES the header set's columns rather than
            # SELECT *. The matcher accepts any source that CONTAINS the
            # set (order-insensitive, superset-tolerant), so `SELECT *`
            # lined the branches up by position on an assumption the
            # matcher never made: a table carrying the same nine columns in
            # a different order unioned silently and charted values from
            # the wrong column, and one with an extra column made the
            # counts differ and took the whole query down with it.
            if key.lower().startswith("all:"):
                hs = _hs_of(key[len("all:"):].strip())
                srcs = self._sources_for_header_set(hs)
                if not srcs:
                    raise MissingTable(hs)
                from . import defaults
                want = dict(defaults.headers()["nicknames"]).get(hs) or []
                cols = ", ".join(q(c) for c in want)
                union = " UNION ALL ".join(
                    f"SELECT {cols} FROM {q(s['table_name'])}" for s in srcs)
                return f"({union})"
            hs = _hs_of(key)
            src = self._source_for_header_set(hs)
            if not src:
                raise MissingTable(hs)
            return q(src["table_name"])

        return re.sub(r"\{\{([^}]+)\}\}", repl, sql)

    def resolve_table_sources(self, table: str) -> list[int]:
        """Every source a widget's table names in this case: one for src_N or
        a plain placeholder, all of them for {{all:…}} — whose SQL unions
        every match, so a drill has to say which one it is opening."""
        key = (table or "").strip()
        m = re.fullmatch(r"\{\{\s*all:([^}]+)\}\}", key)
        if not m:
            return [self.resolve_table_source(key)]
        inner = m.group(1).strip()
        hs = inner[len("header_set:"):].strip() if inner.lower().startswith("header_set:") \
            else self._TABLE_SHORTHANDS.get(inner.lower(), inner)
        srcs = self._sources_for_header_set(hs)
        if not srcs:
            raise MissingTable(hs)
        return [int(s["id"]) for s in srcs]

    def resolve_table_source(self, table: str) -> int:
        """The source id a widget's table names in this case — `src_N`
        as-is (if it exists), a `{{evtx}}` / `{{header_set:…}}` placeholder
        by the same first-match rule the SQL resolver uses. `{{all:…}}`
        binds to the first of its sources: a drilldown opens ONE table.
        ValueError, worded like the widget's empty state, when there's
        none."""
        import re
        key = (table or "").strip()
        m = re.fullmatch(r"src_(\d+)", key)
        if m:
            sid = int(m.group(1))
            if any(s["id"] == sid for s in self.list_sources()):
                return sid
            raise MissingTable(None, "That table is no longer in this case")
        m = re.fullmatch(r"\{\{([^}]+)\}\}", key)
        if not m:
            raise ValueError(f"Not a table reference: {table!r}")
        inner = m.group(1).strip()
        if inner.lower().startswith("all:"):
            inner = inner[4:].strip()
        if inner.lower().startswith("header_set:"):
            hs = inner[len("header_set:"):].strip()
        else:
            hs = self._TABLE_SHORTHANDS.get(inner.lower(), inner)
        src = self._source_for_header_set(hs)
        if not src:
            raise MissingTable(hs)
        return int(src["id"])

    def dashboard_widget_preview(self, source: str, query: dict, limit: int = 200,
                                 cells: list | None = None) -> dict:
        """Run one widget's data source and return normalized tabular data
        the client renders per the widget's kind. SQL rides the read-only
        run_sql path (own connection, statement checks) — so a dashboard is
        data, not code. watchlist / tags read the case's own state.

        `source: "cells"` is the grid of labelled numbers the `signals`
        render kind draws: `cells` is a list of small widgets, each with
        its own source, query and drill, and this runs them all and
        answers with one label/value row per cell."""
        query = query or {}
        if not isinstance(query, dict):
            # A cell's query arrives unchecked inside `cells` (the route's
            # model only types the top-level one), and `.get` on a string
            # was a 500 — or, per cell, an AttributeError past the cell's
            # own error catch.
            raise ValueError("A widget's query must be an object")
        if source == "cells":
            return self._cells_preview(cells or [], limit)
        if source == "sql":
            sql = (query.get("sql") or "").strip()
            if not sql:
                raise ValueError("SQL widget needs a query")
            sql = self._resolve_table_placeholders(sql)   # {{evtx}} -> src_<id>
            res = self.run_sql(sql, limit=min(int(query.get("limit") or limit), 1000))
            return {"columns": res["columns"], "rows": res["rows"], "truncated": res.get("truncated", False)}
        if source == "watchlist":
            inds = self.list_indicators()
            return {"columns": ["indicator", "hits"],
                    "rows": [[i["value"], i["hit_count"]] for i in inds],
                    "total": sum(i["hit_count"] for i in inds)}
        if source == "tags":
            with self.lock:
                rows = self.db.execute(
                    "SELECT td.name AS name, COUNT(rt.rid) AS n FROM tag_defs td"
                    " LEFT JOIN row_tags rt ON rt.tag_id = td.id"
                    " GROUP BY td.id ORDER BY n DESC").fetchall()
            return {"columns": ["tag", "count"], "rows": [[r["name"], r["n"]] for r in rows],
                    "total": sum(r["n"] for r in rows)}
        raise ValueError(f"Unknown widget source {source!r}")

    @staticmethod
    def _cell_value(payload: dict):
        """One number out of a widget payload — the same rule the client's
        `stat` render applies: the watchlist/tags total when there is one,
        otherwise the last column of the first row."""
        if payload.get("total") is not None:
            return payload["total"]
        rows = payload.get("rows") or []
        return rows[0][-1] if rows and rows[0] else None

    def _cells_preview(self, cells: list, limit: int = 200) -> dict:
        """Every cell of a `signals` widget, in order: one label/value row
        each, and `cell_errors` parallel to them.

        A cell fails ALONE. Five host-fact cards on a case with no RECmd
        batch used to be five cards reading "No Registry (RECmd batch)
        table in this case yet"; folded into one card with one query they
        would have been one error where six numbers used to be, and the
        evtx cells beside them would have gone down with the registry
        ones. So each cell is asked separately and an error is reported in
        the cell's own place, next to the neighbours that did answer.

        One request, N answers, which is the other half of why the board
        got shorter: eleven cards were eleven round trips."""
        rows, errors = [], []
        for c in cells:
            if not isinstance(c, dict):
                continue
            label = "" if c.get("label") is None else str(c["label"])
            src = c.get("source") or "sql"
            if src == "cells":
                # A cell of cells has no meaning and would recurse; say so
                # rather than answering something.
                rows.append([label, None])
                errors.append("A signals cell cannot itself be a grid of signals")
                continue
            try:
                payload = self.dashboard_widget_preview(src, c.get("query") or {}, limit)
            except (ValueError, KeyError, TypeError, sqlite3.Error) as e:
                rows.append([label, None])
                errors.append(str(e))
                continue
            rows.append([label, self._cell_value(payload)])
            errors.append(None)
        return {"columns": ["label", "value"], "rows": rows, "cell_errors": errors}

    def pop_legacy_presets(self) -> list[dict]:
        """filter_presets used to be this case's own SQLite-backed table of
        saved filters, scoped to just this case file. Presets are saved
        filters now — one workspace-level, cross-case mechanism (see
        CLAUDE.md and workspace.SavedFilters) instead of two overlapping
        ones. Reads out whatever's left in a case file created before this
        change and clears the table, so this is a one-time migration on
        first open rather than an ongoing read on every open (the table's
        empty for good afterward — including for a case file that's always
        been on the new scheme, where this is just a cheap empty SELECT).
        The caller (server.py, on case open) folds the result into
        workspace.filters."""
        with self.lock, self.db:
            rows = self.db.execute("SELECT * FROM filter_presets").fetchall()
            if rows:
                self.db.execute("DELETE FROM filter_presets")
        return [
            {
                "name": r["name"],
                "col_names": json.loads(r["col_names"]),
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- sessions

    def export_session(self, source_id: int) -> dict:
        src = self.get_source(source_id)
        with self.lock:
            tags = [dict(r) for r in self.db.execute("SELECT * FROM tag_defs ORDER BY id")]
            rt = [
                {"rid": r["rid"], "tag_id": r["tag_id"]}
                for r in self.db.execute(
                    "SELECT rid, tag_id FROM row_tags WHERE source_id=? ORDER BY rid", (source_id,)
                )
            ]
            notes = [
                {"rid": r["rid"], "note": r["note"]}
                for r in self.db.execute(
                    "SELECT rid, note FROM row_notes WHERE source_id=? ORDER BY rid", (source_id,)
                )
            ]
        return {
            "format": "winnow-session/1",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {
                "name": src["name"],
                "path": src["path"],
                "file_hash": src["file_hash"],
                "row_count": src["row_count"],
                # The imported file's own columns — this is what
                # import_session matches against, and a derived column the
                # exporting analyst happened to add isn't part of the
                # evidence's shape.
                "columns": self._base_cols(src),
                # How it was read, so a case without it open can re-import
                # it into the same rids (import_case_session). Absent from
                # sessions saved before it was recorded.
                "ingest": src.get("ingest"),
            },
            "tag_defs": tags,
            "row_tags": rt,
            "row_notes": notes,
            "layout": self.get_layout(source_id),
            # Definitions, not values: a derived column is a deterministic
            # function of data the receiving case already has, so importing
            # re-runs the backfill rather than shipping a second copy of
            # every timestamp.
            "derived_columns": [self._session_derived_def(d)
                                for d in self.list_derived_columns(source_id)],
        }

    def _session_derived_def(self, d: dict) -> dict:
        """One derived column's definition as a session carries it. A
        lookup's params name its table by THIS case's source id, which means
        nothing in another case — or in this one, once the table is dropped
        and the id reused — so the table's identity travels beside it as
        `lookup_table` (the evidence's fingerprint and name, the keys session
        sources are matched by), and the receiving side resolves that
        instead (_resolve_session_lookup)."""
        out = {k: d[k] for k in ("name", "input_column", "op_id", "params")}
        if d["op_id"] == "lookup":
            try:
                other = self._source_lite(int((d.get("params") or {}).get("other_source_id")))
            except (KeyError, TypeError, ValueError):
                other = None
            if other is not None and other["id"] >= 0:
                out["lookup_table"] = {
                    "file_hash": other.get("file_hash"), "name": other.get("name"),
                    "columns": [c["name"] for c in self._base_cols(other)],
                    "row_count": other.get("row_count")}
        return out

    def _resolve_session_lookup(self, d: dict) -> int:
        """The live source a session's lookup column should read, from the
        `lookup_table` identity _session_derived_def wrote, matched the way
        a load matches session sources (_match_session_sources: the hash,
        then which table of that file). A SQL-saved or plugin-made lookup
        table — common here — matches by its synthetic hash like any other
        (MADE_HASH_PREFIX), so another case's table of the same name is not
        it. One recorded with no hash at all (a session from before made
        tables had one) used to pair by name; nothing shows that table is
        this one, so it is a warning now, like a load. Raises ValueError — a
        warning, the column not recreated — when no table matches or
        several could. The session's id is never used: binding by it filled
        a column from whichever table happened to hold that number, with
        nothing to say so."""
        ident = d.get("lookup_table")
        if not isinstance(ident, dict):
            raise ValueError("the session does not say which table it looks values up in "
                             "(it was saved before that was recorded); recreate it from the column menu")
        name = ident.get("name")
        h = ident.get("file_hash")
        if not h:
            raise ValueError(f"its lookup table {name!r} was made inside Winnow and the session was "
                             "saved before such tables could be told apart, so which one it reads "
                             "can't be confirmed; recreate it from the column menu")
        real = [s for s in self.list_sources() if s["id"] >= 0]
        pairs = self._match_session_sources([ident], real)
        if 0 in pairs:
            return real[pairs[0]]["id"]
        same = [s for s in real if s.get("file_hash") == h]
        if not same and not _is_file_hash(h) and any(s["name"] == name for s in real):
            raise ValueError(f"its lookup table {name!r} was made inside Winnow, and this case's "
                             f"{name!r} is a different table made separately")
        if not same:
            raise ValueError(f"its lookup table {name!r} is not in this case")
        raise ValueError(f"{len(same)} tables here could be its lookup table {name!r} and none "
                         "of them is unambiguously it; recreate it from the column menu")

    def import_session(self, source_id: int, session: dict, merge: bool = True) -> dict:
        """Apply a one-table session to `source_id`. merge=False replaces
        that table's tags and notes — only that table's, since a one-table
        session says nothing about the others."""
        src = self.get_source(source_id)
        # A one-table session only ever arrives as a file, so nothing says
        # its tag ids are this case's: names only, and a name two tags
        # share is refused (_session_tag_doubts).
        self._session_tag_doubts([session], same_case=False)
        warnings = self._session_hash_warnings(src, session)
        idmap = self._session_tag_idmap(session)

        with self.lock, self.db:
            if not merge:
                self.db.execute("DELETE FROM row_tags WHERE source_id=?", (source_id,))
                self.db.execute("DELETE FROM row_notes WHERE source_id=?", (source_id,))
            self._write_session_rows(source_id, session, idmap)
            # Tags arrived wholesale rather than through the delta path, so
            # the bump it would have done is owed here: a dashboard cached
            # against the pre-load state is now counting somebody else's
            # pass over this evidence.
            self._bump_state_generation()
        # Tag state was just replaced wholesale, not by a recorded delta, so
        # every existing undo entry now describes a world that is gone —
        # replaying one would strip tags off rows the loaded session owns.
        self._discard_undo()
        derived_added, job_ids = self._restore_session_reading(source_id, session, warnings)
        return {"warnings": warnings, "tags_applied": len(session.get("row_tags", [])),
                "derived_columns_added": derived_added,
                "derived_job_ids": job_ids}

    @staticmethod
    def _session_hash_warnings(src: dict, session: dict) -> list[str]:
        s_src = session.get("source", {})
        if s_src.get("file_hash") and s_src["file_hash"] != src["file_hash"]:
            what = ("file" if _is_file_hash(s_src["file_hash"]) and _is_file_hash(src["file_hash"])
                    else "table")
            return [f"Session was recorded against a different {what}. Row numbers may not line up."]
        return []

    def _session_tag_doubts(self, docs: list[dict], *, same_case: bool) -> set[str]:
        """The tag names a session's rows carry that name more than one tag
        — two of the session's own tags that carry rows, or two of this
        case's that the name pairs with (pair_tag_names) — and so can't
        be matched by name. `docs` are the session's per-table documents;
        a case session's tags count across all of its tables, since every
        one of them lists the same tag palette.

        Matching such a name by name put every row of both tags on one of
        them — the last id per name won — and said nothing: two "New tag"s
        made by Add tag became one on the first reload of the analyst's own
        session, and the diff, comparing names, called the two the same.

        For a session saved in this case (`same_case`) the tag ids are this
        case's own, so these names are returned for _session_tag_idmap to
        match by id. From anywhere else an id means nothing here (invariant
        #10) and there is no right answer to "which of the two", so the
        load is refused, naming them — the precedent _match_session_sources
        set for a table it can't tell from its sibling. Nothing has been
        written or re-imported when this raises."""
        used: dict[str, set[int]] = {}
        for doc in docs:
            names = {t["id"]: t["name"] for t in doc.get("tag_defs") or []}
            for rt in doc.get("row_tags") or []:
                if rt.get("tag_id") in names:
                    used.setdefault(names[rt["tag_id"]], set()).add(rt["tag_id"])
        if not used:
            return set()
        live: dict[str, int] = {}
        for t in self.list_tags():
            live[t["name"]] = live.get(t["name"], 0) + 1
        paired = pair_tag_names(used, live)
        doubt = {n: (len(ids), live.get(paired.get(n), 0)) for n, ids in used.items()}
        doubt = {n: c for n, c in doubt.items() if c[0] > 1 or c[1] > 1}
        if same_case or not doubt:
            return set(doubt)
        said = []
        for n, (theirs, ours) in sorted(doubt.items()):
            where = [f"{theirs} tags in the session"] if theirs > 1 else []
            if ours > 1:
                where.append(f"{ours} {'' if where else 'tags '}in this case")
            said.append(f'"{n}" names {" and ".join(where)}')
        fix = []
        if any(ours > 1 for _, ours in doubt.values()):
            fix.append("Give each tag here its own name (Edit tags) and load again.")
        if any(theirs > 1 for theirs, _ in doubt.values()):
            fix.append("A name the session repeats has to be changed in the case that saved it, "
                       "and the session saved again.")
        raise ValueError(
            f"Not loaded: {'; '.join(said)}. Tags in a session from outside this case are "
            "matched by name, so the rows of tags that share one can't be told apart. "
            + " ".join(fix))

    def _session_tag_idmap(self, session: dict, by_id: set[str] = frozenset(),
                           made: dict | None = None, warnings: list[str] | None = None) -> dict:
        """{session tag id -> local tag id}, matched by name with
        pair_tag_names — the rule copy and diff use too — creating what's
        missing.

        Except the names in `by_id` (_session_tag_doubts: names that more
        than one tag carries, in a session saved in this case). Each tag of
        one of those that carries rows of this table goes back to the tag
        with the session's own id, while that tag still has the name;
        otherwise it gets a tag of its own, created under a free name and
        reported in `warnings` — never a namesake, which is what moved its
        rows before. One that carries none is left out: it has nothing to
        place, and no name to place it by. `made` carries the created ones
        across a case session's tables, so one session tag is one new tag."""
        tags = self.list_tags()
        local = {t["name"]: t["id"] for t in tags}
        name_of = {t["id"]: t["name"] for t in tags}
        defs = session.get("tag_defs", [])
        carried = {rt.get("tag_id") for rt in session.get("row_tags") or []}
        paired = pair_tag_names([t["name"] for t in defs], local)
        created: dict[str, int] = {}
        made = {} if made is None else made
        idmap = {}
        for t in defs:
            if t["name"] in by_id:
                if t["id"] not in carried:
                    continue
                if paired.get(t["name"]) is not None and name_of.get(t["id"]) == paired[t["name"]]:
                    idmap[t["id"]] = t["id"]
                    continue
                key = (t["id"], t["name"])
                if key not in made:
                    new = self.upsert_tag(None, t["name"], t.get("color", "#888888"), t.get("hotkey"))
                    made[key] = new["id"]
                    if warnings is not None:
                        warnings.append(
                            f'One of the session\'s tags named "{t["name"]}" is no longer in this '
                            f'case under that name, so its rows are on a new tag, "{new["name"]}"')
                idmap[t["id"]] = made[key]
                continue
            if t["name"] in paired:
                idmap[t["id"]] = local[paired[t["name"]]]
                continue
            if t["name"] not in created:
                created[t["name"]] = self.upsert_tag(
                    None, t["name"], t.get("color", "#888888"), t.get("hotkey"))["id"]
            idmap[t["id"]] = created[t["name"]]
        return idmap

    def _write_session_rows(self, source_id: int, session: dict, idmap: dict) -> None:
        """One table's tags and notes out of a session document. Caller
        holds self.lock inside a self.db transaction, and owns the clearing
        a replace needs — which tables that covers is the caller's to say."""
        self.db.executemany(
            "INSERT OR IGNORE INTO row_tags(source_id, rid, tag_id) VALUES (?,?,?)",
            [
                (source_id, r["rid"], idmap.get(r["tag_id"], r["tag_id"]))
                for r in session.get("row_tags", [])
            ],
        )
        self.db.executemany(
            "INSERT INTO row_notes(source_id, rid, note) VALUES (?,?,?) "
            "ON CONFLICT(source_id, rid) DO UPDATE SET note=excluded.note",
            [(source_id, r["rid"], r["note"]) for r in session.get("row_notes", [])],
        )

    def _restore_session_reading(self, source_id: int, session: dict,
                                 warnings: list[str]) -> tuple[int, list[str]]:
        """The half of a session that is how the evidence is READ — layout
        and derived columns — rather than what was concluded about
        it. Returns (derived columns added, their backfill job ids)."""
        if session.get("layout"):
            self.save_layout(source_id, session["layout"])
        # A session saved before saved views were removed can still carry a
        # `saved_views` list; it is ignored, since nothing could open one.

        # Derived columns arrive as definitions and are recomputed here —
        # each starts its own backfill job. A name that's already taken
        # (the analyst built the same column locally, or a base column has
        # that name) is a warning, never a failure: the tags and notes are
        # the part of a session that can't be reconstructed.
        derived_added = 0
        pending_jobs: dict[str, str] = {}  # column name -> its backfill job, for chains
        for d in session.get("derived_columns", []):
            try:
                params = d.get("params")
                if d["op_id"] == "lookup":
                    params = dict(params or {}, other_source_id=self._resolve_session_lookup(d))
                # A chained column's parent was created a moment ago and its
                # backfill is an async job — wait for that link, or the
                # "still building" guard (correctly) refuses the child.
                for inp in timeparse.op_inputs(d["op_id"], d["input_column"], params):
                    jid = pending_jobs.get(inp.lower())
                    if jid:
                        self.wait_for_ingest_job(jid, timeout=600)
                res = self.add_derived_column(source_id, d["name"], d["input_column"],
                                              d["op_id"], params)
                pending_jobs[d["name"].lower()] = res["job_id"]
                derived_added += 1
            except (ValueError, KeyError) as e:
                warnings.append(f"Derived column {d.get('name')!r} not recreated: {e}")
        # The backfills are still running when this returns — only the ones a
        # LATER column depended on were waited for above. Without the job ids
        # a caller has nothing to wait on, so "derived_columns_added: 2" reads
        # as done while the columns are still empty; anything reading values
        # straight afterwards gets NULLs. Hand them back so the caller can
        # decide whether to wait.
        return derived_added, list(pending_jobs.values())

    # ---------------------------------------------------------- case sessions

    def export_case_session(self) -> dict:
        """Whole-case session: every source's tags/notes/layout — open in a
        tab or not — in one file. Merges have no entry: their tags live on
        their members. Builds directly on export_session — no new
        per-source shape."""
        return {
            "format": "winnow-case-session/1",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sources": [self.export_session(s["id"]) for s in self.list_sources()],
        }

    def import_case_session(self, session: dict, merge: bool = True, *, same_case: bool = False) -> dict:
        """Reattaches each source session to the live table it describes
        (_match_session_sources: the file's hash, then which table of that
        file); if a source's file isn't in the case at all but still exists
        at the recorded path, re-imports it first — the way it was imported
        (_reimport_session_source). Local single-user tool with full
        filesystem access already (same trust level as --open), so this is
        the one action that can fully reconstruct a case from a session.

        A source whose FILE is in the case but which matches no single
        table of it — the case lacks that table, or holds two copies
        nothing tells apart — is skipped with a warning naming it. Never
        re-imported (the file is here; another copy of it is not this
        table) and never put on a sibling: that was the old hash-only
        match, which moved one table's tags onto another's rows.

        And a session's tags name rows by rid, so even a matched table only
        gets them if it holds the rows the session was recorded against:
        the row count and columns the session recorded
        (_session_shape_mismatch). Anything else is skipped with a warning
        rather than tagged — the same file imported with other options has
        every rid on a different row, and nothing about the tags would
        show it.

        A table made inside Winnow (a SQL-pane save, a plugin's computed
        table, a subset) has a synthetic hash — MADE_HASH_PREFIX — and
        matches by it like a file; only a copy of that table carries it, so
        another case's table made the same way, with the same name and
        rows, never takes these tags. With no table of that identity here
        there is nothing to re-import either, and the warning says so
        (_made_table_unmatched_warning). A session saved before made
        tables had an identity records none for them: such an entry is
        never matched (a same-named table here may have been made over
        again since), and a replace leaves the tables it could be as they
        are (_legacy_made_table_candidates) rather than clearing them for
        nothing.

        Tags are matched by name, except a name more than one tag carries
        (_session_tag_doubts): by the session's own ids when it was saved in
        this case (`same_case`, load_session's call for a session whose
        origin is 'saved'), and otherwise the whole load is refused — before
        anything is re-imported or written."""
        warnings: list[str] = []
        tags_applied = 0
        sources_restored = 0
        sources_reimported = 0
        entries = session.get("sources", [])
        by_id = self._session_tag_doubts(entries, same_case=same_case)
        made_tags: dict = {}
        live = self.list_sources()
        metas = [e.get("source", {}) for e in entries]
        pairs = self._match_session_sources(metas, live)
        live_hashes = {s["file_hash"] for s in live if s.get("file_hash")}
        resolved: list[tuple[int, dict]] = []
        # Open tables a skipped source may be: a replace leaves these as
        # they are (below), since the session's version of them was not
        # applied either.
        unplaced: set[int] = set()

        for i, src_session in enumerate(entries):
            s_meta = metas[i]
            label = s_meta.get("name") or s_meta.get("path") or "unknown"
            source_id = live[pairs[i]]["id"] if i in pairs else None
            reimported = False
            if source_id is None and s_meta.get("file_hash") in live_hashes:
                same = [s["id"] for s in live if s.get("file_hash") == s_meta["file_hash"]]
                unplaced.update(same)
                what = (f"{len(same)} table(s) from the same file" if _is_file_hash(s_meta["file_hash"])
                        else f"{len(same)} copies of this table")
                warnings.append(
                    f"'{s_meta.get('name', 'unknown')}' was not restored: this case has "
                    f"{what} and none of them is "
                    "unambiguously this one (matched on name, columns and row count) "
                    "— skipped rather than guessed")
                continue
            if source_id is None and not _is_file_hash(s_meta.get("file_hash")):
                # Made inside Winnow, and this case has no table with its
                # identity — or a record from before made tables had one.
                # There is no file to re-import it from either way.
                could_be = self._legacy_made_table_candidates(s_meta, live)
                unplaced.update(could_be)
                warnings.append(self._made_table_unmatched_warning(s_meta, live, could_be))
                continue
            if source_id is None:
                source_id, why = self._reimport_session_source(s_meta)
                if source_id is None:
                    warnings.append(why)
                    continue
                reimported = True
            mismatch = self._session_shape_mismatch(self.get_source(source_id), s_meta)
            if mismatch:
                if reimported:
                    # Read the right file the wrong way (a session from
                    # before the reader was recorded, guessed from the
                    # extension): the copy is no use to anyone.
                    self.drop_source(source_id)
                    warnings.append(
                        f"'{label}' was re-imported from {s_meta.get('path')} but came out "
                        f"different from the session's copy ({mismatch}) — removed again, "
                        "its tags and notes not applied")
                else:
                    unplaced.add(source_id)
                    warnings.append(
                        f"'{label}' is in this case, but not as the session's copy was imported "
                        f"({mismatch}) — its tags and notes would land on other rows, so "
                        "they were not applied")
                continue
            if reimported:
                sources_reimported += 1
            resolved.append((source_id, src_session))

        # Replace means the CASE's tags and notes, every table of it — what
        # the Load dialog says. Clearing only the tables the snapshot names
        # left a table imported after it was taken holding its second-pass
        # tags: the case was then neither the session nor the live state but
        # a mix, and a diff against the session called it clean. The clear
        # and every table's writes are one transaction, so a failure part
        # way leaves the case as it was rather than half-replaced.
        #
        # Except the open tables a skipped source may be (`unplaced`): the
        # session's tags for them were refused because they would land on
        # other rows, and clearing the tags those tables do have would lose
        # the analyst's work there for nothing in return.
        unplaced -= {sid for sid, _ in resolved}
        if unplaced and not merge:
            names = sorted(s["name"] for s in live if s["id"] in unplaced)
            warnings.append(
                f"Left the tags and notes on {', '.join(repr(n) for n in names)} as they were: "
                "the session's copy could not be applied there, so replacing would only have "
                "cleared them")
        idmaps = []
        for source_id, src_session in resolved:
            warnings.extend(self._session_hash_warnings(self._source_lite(source_id), src_session))
            idmaps.append(self._session_tag_idmap(src_session, by_id, made_tags, warnings))
        with self.lock, self.db:
            if not merge:
                keep = sorted(unplaced)
                spare = f" WHERE source_id NOT IN ({','.join('?' * len(keep))})" if keep else ""
                self.db.execute(f"DELETE FROM row_tags{spare}", keep)
                self.db.execute(f"DELETE FROM row_notes{spare}", keep)
            for (source_id, src_session), idmap in zip(resolved, idmaps):
                self._write_session_rows(source_id, src_session, idmap)
                tags_applied += len(src_session.get("row_tags", []))
                sources_restored += 1
            # Wholesale, outside the delta path — see import_session.
            self._bump_state_generation()
        self._discard_undo()
        for source_id, src_session in resolved:
            self._restore_session_reading(source_id, src_session, warnings)

        return {
            "warnings": warnings,
            "tags_applied": tags_applied,
            "sources_restored": sources_restored,
            "sources_reimported": sources_reimported,
        }

    @classmethod
    def _legacy_made_table_candidates(cls, s_meta: dict, live: list[dict]) -> list[int]:
        """The live tables a session entry with NO hash could describe — a
        table made inside Winnow, recorded before such tables were given an
        identity. Nothing proves which (if any) it is, so none is tagged,
        but a replace must not clear these either. A made table's name,
        columns and rows never change (invariant #1; a rename is a
        nickname), so the table it was is one with no file behind it that
        still agrees on all three — whatever of them the entry recorded.
        An entry that has a hash has an identity, and no candidates."""
        if s_meta.get("file_hash"):
            return []
        name = s_meta.get("name") or ""
        cols = [c.get("name") if isinstance(c, dict) else c for c in s_meta.get("columns") or []]
        rows = s_meta.get("row_count")
        out = []
        for s in live:
            if _is_file_hash(s.get("file_hash")) or s.get("name") != name:
                continue
            if rows is not None and s.get("row_count") != rows:
                continue
            if cols and [c["name"] for c in cls._base_cols(s)] != cols:
                continue
            out.append(s["id"])
        return out

    @staticmethod
    def _made_table_unmatched_warning(s_meta: dict, live: list[dict], could_be: list[int]) -> str:
        """What to tell the analyst about a made-inside-Winnow entry that
        matched no table here. It used to fall through to re-import, which
        said the table "isn't open" — while it was open, and its tags had
        just been cleared."""
        label = s_meta.get("name") or "unknown"
        if not s_meta.get("file_hash"):
            if could_be:
                return (f"'{label}' was not restored: the session was saved before Winnow could tell "
                        "apart tables made inside it (SQL pane saves, plugin output), so nothing "
                        f"shows this case's '{label}' is the same table — its tags and notes were "
                        "not applied")
            return (f"'{label}' was not restored: it is a table made inside Winnow, saved before "
                    "such tables could be told apart, and this case has no table it could be")
        if any(s.get("name") == label for s in live):
            return (f"'{label}' was not restored: it was made inside Winnow (not imported from a "
                    f"file), and this case's '{label}' is a different table made separately — the "
                    "session's tags and notes were not put on it")
        return (f"'{label}' was not restored: it was made inside Winnow (not imported from a file), "
                "so there is no file to re-import it from, and this case does not have it or a "
                "copy of it")

    _SESSION_READERS = ("csv", "text", "json", "sqlite", "xlsx", "plaso", "iis")

    def _reimport_session_source(self, s_meta: dict) -> tuple[int | None, str | None]:
        """(source_id, None) after re-importing a session source from its
        recorded path, or (None, why it was not).

        The path came out of a session document, which is often a file
        another analyst sent — so it is treated as a claim to check, not a
        place to read from: a network path is refused before anything
        touches it (_is_network_path), only a regular file at an absolute
        path is considered, and the file is fingerprinted and compared with
        the session's file_hash BEFORE it is ingested. Nothing but the
        evidence the session was recorded against is ever read into the
        case.

        The reader and its options are the ones the table was imported
        with (sources.ingest, carried in the session). A session saved
        before that was recorded gets the reader the import queue would
        pick for the extension — a guess, which the caller checks against
        the shape the session did record."""
        label = s_meta.get("name") or s_meta.get("path") or "unknown"
        path = s_meta.get("path")
        if not path:
            return None, f"'{label}' isn't in this case and the session doesn't say which file it came from — skipped"
        if _is_network_path(path) or not os.path.isabs(path):
            return None, (f"'{label}' isn't in this case, and the session names {path} for it — not a local "
                          "file path, so Winnow did not look at it. Open the file yourself, then load "
                          "the session again.")
        if not os.path.isfile(path):
            return None, f"'{label}' isn't in this case and its original file wasn't found at {path} — skipped"
        want = s_meta.get("file_hash")
        how = s_meta.get("ingest") or self._guess_session_reader(path)
        kind = how.get("kind") if isinstance(how, dict) else None
        if kind not in self._SESSION_READERS:
            # "rows" with a file behind it is a plugin's importer: a table
            # made with no file never gets here (import_case_session).
            why = ("was read by a plugin's importer"
                   if kind == "rows" else "has no record of how it was read")
            return None, (f"'{label}' isn't in this case, and it {why}, so Winnow can't re-import it "
                          f"from {path}. Import it yourself, then load the session again.")
        try:
            have = self._quick_hash(path)
        except OSError as e:
            return None, f"Could not re-import '{label}': {e}"
        if not want or have != want:
            return None, (f"'{label}' isn't in this case, and {path} is not the file the session was "
                          "recorded against (it has changed, or it is a different file) — not "
                          "imported, its tags and notes not applied")
        name = s_meta.get("name")
        try:
            if kind == "csv":
                rec = self.ingest_csv(path, name=name, delimiter=how.get("delimiter"),
                                      has_header=how.get("has_header", True),
                                      column_types=how.get("column_types"))
            elif kind == "text":
                rec = self.ingest_text(path, name=name)
            elif kind == "json":
                rec = self.ingest_json(path, name=name, flatten_mode=how.get("flatten_mode", "none"),
                                       flatten_depth=how.get("flatten_depth", 0))
            elif kind == "sqlite":
                rec = self.ingest_sqlite_table(path, how["table"], name=name,
                                               timestamp_columns=how.get("timestamp_columns"))
            elif kind == "xlsx":
                rec = self.ingest_xlsx_sheet(path, how["sheet"], name=name)
            elif kind == "iis":
                rec = self.ingest_iis(path, name=name, log_format=how.get("format"),
                                      column_types=how.get("column_types"))
            else:
                rec = self.ingest_plaso(path, name=name)
        except Exception as e:
            return None, f"Could not re-import '{label}': {e}"
        if rec.get("file_hash") != want:
            # Changed between the check and the read.
            self.drop_source(rec["id"])
            return None, f"'{label}' changed on disk while it was being re-imported — removed again"
        return rec["id"], None

    @staticmethod
    def _guess_session_reader(path: str) -> dict:
        """The reader the import queue would choose for this extension
        (server._ingest_kind_for_path, minus plugins), for a session saved
        before sources.ingest existed. A SQLite file or workbook holds
        several tables and the old session didn't say which, so those get
        no guess at all."""
        ext = os.path.splitext(path)[1].lower()
        if ext in SQLITE_IMPORT_EXTENSIONS or ext in XLSX_IMPORT_EXTENSIONS:
            return {}
        if ext in PLASO_IMPORT_EXTENSIONS:
            return {"kind": "plaso"}
        if iisread.log_format(path):
            return {"kind": "iis"}
        if ext in (".json", ".jsonl", ".ndjson"):
            return {"kind": "json"}
        return {"kind": "csv" if ext in DEFAULT_IMPORT_EXTENSIONS else "text"}

    @classmethod
    def _session_shape_mismatch(cls, src: dict, s_meta: dict) -> str | None:
        """Why `src` does not hold the rows a session source was recorded
        against, or None if it does — the same number of rows under the
        same imported columns. A session from before either was recorded
        can't be checked on that count and passes it."""
        want_rows = s_meta.get("row_count")
        if want_rows is not None and want_rows != src["row_count"]:
            return f"{want_rows:,} rows in the session, {src['row_count']:,} here"
        want_cols = [c.get("name") for c in s_meta.get("columns") or []]
        have_cols = [c["name"] for c in cls._base_cols(src)]
        if want_cols and want_cols != have_cols:
            return "different columns"
        return None

    _SESSION_NAME_RE = re.compile(r"[^A-Za-z0-9_ -]")

    def _sessions_dir(self) -> str:
        """Where the file-based named sessions (/api/sessions) of THIS case
        live: sessions/<case file name>/ beside it. It was sessions/ alone,
        which every case in the folder shared, so listing showed another
        case's sessions, and loading one brought that case's tags and notes
        into this one (onto any table here with the same file hash) and
        re-imported its tables from their recorded paths. Files an older
        build left in sessions/ itself stay there and are no longer listed.
        The sessions the UI saves are in the case file (invariant #10),
        which has no such problem; only scripts use these."""
        d = os.path.join(os.path.dirname(os.path.abspath(self.path)), "sessions",
                         os.path.basename(self.path))
        os.makedirs(d, exist_ok=True)
        return d

    def _session_path(self, name: str) -> str:
        safe = self._SESSION_NAME_RE.sub("_", name).strip() or "session"
        return os.path.join(self._sessions_dir(), f"{safe}.winnow_case.json")

    # ------------------------------------------------ copying into another case

    def copy_sources_to(self, target_path: str, source_ids: list[int]) -> dict:
        """Copy sources — rows, tags, notes, layout, derived
        columns and their values — from this case into another case file.

        The flow this serves: a file opened by double-click lands in a
        temporary quick-look case, the analyst tags a few rows, then wants
        that work in the real investigation's case. No re-parse: the rows
        are already TEXT in src_N, so ATTACH + INSERT..SELECT moves them at
        SQLite speed, rid-for-rid — and rid preservation is what keeps the
        copied tags and notes pointing at the same evidence rows
        (invariant #1's contiguity guarantee survives because the table is
        copied whole).

        The mechanics run THROUGH THE TARGET, not the source: the target is
        opened as a full Store — which is what runs its schema migrations,
        so a case created by an older Winnow gains any missing tables
        before anything writes — and this case's file is ATTACHed to the
        target's writer connection read-only-by-discipline (no statement
        here ever writes through the attachment; WAL makes the concurrent
        read of our live file safe). Opening the target as a Store also
        takes its case lock, which is the exclusivity that makes writing
        into it correct at all — and why a target that is OPEN IN ANOTHER
        WINNOW is refused up front with the holder named, rather than
        risking two servers' in-memory state disagreeing about one file.

        Tags are matched into the target BY NAME, never id — the same rule
        session import and the QC diff use, for the same reason: two cases
        number their tags independently. Missing names are created with
        the source's colour and hotkey (hotkey dropped on collision).

        FTS is deliberately not copied; has_fts lands 0 and the target
        rebuilds in the background when opened. Merges are refused: their
        member ids are meaningless in the target."""
        target_path = os.path.abspath(target_path)
        if target_path == os.path.abspath(self.path):
            raise ValueError("That is this case — pick a different one to copy into")
        if not os.path.isfile(target_path):
            raise ValueError(f"No case file at {target_path}")
        # Before Store(target_path), which would write its schema into
        # whatever is there: the case registry can hold a path that is
        # not a case (registered before registration checked, or a case
        # file since replaced), and the one the analyst picks here may be
        # a browser history database they meant to IMPORT.
        if not is_winnow_case_file(target_path):
            raise ValueError(
                f"{target_path} is not a Winnow case file — copying into it would write into it")
        for sid in source_ids:
            if sid < 0:
                raise ValueError("Merges can't be copied — their member tables can be, individually")
        # A copy already running into the same case holds that case's lock
        # from THIS process, so the probe below reported it as "open in
        # another Winnow", naming our own pid — what a double-clicked "Copy
        # into this case" told the analyst. Two copies racing past the probe
        # before either had opened the target would also both copy.
        with self._copying_lock:
            if target_path in self._copying_into:
                raise ValueError("Already copying into that case — wait for that copy to finish")
            self._copying_into.add(target_path)
        try:
            holder = probe_case_lock(target_path)
            if holder:
                raise ValueError(
                    f"That case is open in another Winnow — {describe_case_lock(holder)}. "
                    "Close it there first.")

            t = Store(target_path)
            try:
                return self._copy_sources_into(t, source_ids)
            finally:
                t.close()
        finally:
            with self._copying_lock:
                self._copying_into.discard(target_path)

    def _copy_sources_into(self, t: "Store", source_ids: list[int]) -> dict:
        src_rows = {}
        with self.lock:
            self.db.commit()   # everything tagged a moment ago is visible to the attach
            for sid in source_ids:
                row = self.db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
                if not row:
                    raise KeyError(f"No table {sid}")
                src_rows[sid] = dict(row)
            tag_defs = [dict(r) for r in self.db.execute("SELECT * FROM tag_defs ORDER BY id")]

        copied = []
        id_map: dict[int, int] = {}   # this case's source id -> its id in the target
        with t.lock, t.db:
            t.db.execute("ATTACH ? AS srccase", (self.path,))
            try:
                tag_map = t._map_tags_by_name(tag_defs)
                shared_cols = [c for c in ("name", "path", "table_name", "row_count",
                                           "columns", "file_hash", "imported_at", "nickname",
                                           "origin", "origin_meta", "ingest")]
                for sid in source_ids:
                    src = src_rows[sid]
                    cur = t.db.execute(
                        f"INSERT INTO sources({','.join(shared_cols)}, has_fts) "
                        f"VALUES ({','.join('?' * len(shared_cols))}, 0)",
                        [src.get(c) for c in shared_cols])
                    new_id = cur.lastrowid
                    new_table = f"src_{new_id}"
                    # Recreate the table from the source's own DDL rather than
                    # rebuilding from the column list — preserves exact quoting
                    # and column order for any header sanitize_columns produced.
                    ddl = t.db.execute(
                        "SELECT sql FROM srccase.sqlite_master WHERE type='table' AND name=?",
                        (src["table_name"],)).fetchone()[0]
                    t.db.execute(ddl.replace(f'"{src["table_name"]}"', f'"{new_table}"', 1)
                                 .replace(src["table_name"], new_table, 1))
                    t.db.execute("UPDATE sources SET table_name=? WHERE id=?", (new_table, new_id))
                    t.db.execute(f"INSERT INTO {q(new_table)} SELECT * FROM srccase.{q(src['table_name'])}")

                    stats = {"tags": 0, "notes": 0, "derived": 0}
                    for old_tag, new_tag in tag_map.items():
                        stats["tags"] += t.db.execute(
                            "INSERT OR IGNORE INTO row_tags(source_id, rid, tag_id) "
                            "SELECT ?, rid, ? FROM srccase.row_tags WHERE source_id=? AND tag_id=?",
                            (new_id, new_tag, sid, old_tag)).rowcount
                    stats["notes"] = t.db.execute(
                        "INSERT OR IGNORE INTO row_notes(source_id, rid, note) "
                        "SELECT ?, rid, note FROM srccase.row_notes WHERE source_id=?",
                        (new_id, sid)).rowcount
                    t.db.execute(
                        "INSERT OR REPLACE INTO layouts(source_id, payload) "
                        "SELECT ?, payload FROM srccase.layouts WHERE source_id=?",
                        (new_id, sid))

                    # Derived columns: definitions re-pointed, values table
                    # copied whole. Same DDL-rewrite trick as the source table.
                    drv_old, drv_new = f"drv_{sid}", f"drv_{new_id}"
                    drv_ddl = t.db.execute(
                        "SELECT sql FROM srccase.sqlite_master WHERE type='table' AND name=?",
                        (drv_old,)).fetchone()
                    if drv_ddl:
                        t.db.execute(drv_ddl[0].replace(f'"{drv_old}"', f'"{drv_new}"', 1)
                                     .replace(drv_old, drv_new, 1))
                        t.db.execute(f"INSERT INTO {q(drv_new)} SELECT * FROM srccase.{q(drv_old)}")
                    dcols = t.db.execute(
                        "SELECT * FROM srccase.derived_columns WHERE source_id=? ORDER BY id",
                        (sid,)).fetchall()
                    for d in dcols:
                        dd = dict(d)
                        t.db.execute(
                            "INSERT INTO derived_columns(source_id, name, input_column, op_id, "
                            " params, status, parse_failures, created_at, inferred_type) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (new_id, dd["name"], dd["input_column"], dd["op_id"], dd["params"],
                             dd["status"], dd.get("parse_failures"), dd.get("created_at"),
                             dd.get("inferred_type")))
                    stats["derived"] = len(dcols)

                    t.db.execute("INSERT OR IGNORE INTO open_tabs(source_id) VALUES (?)", (new_id,))
                    id_map[sid] = new_id
                    copied.append({"id": new_id, "name": src["name"],
                                   "rows": src["row_count"], **stats})

                # A lookup column's definition names its table by THIS
                # case's id, which in the target is whatever table took that
                # number there: a re-derive (or a cascade from its input)
                # would fill the column from it without a word. It follows
                # its table when that came along in this copy, else the
                # target's table with the same fingerprint when there is
                # exactly one, else it names none — the values stay as
                # copied and a re-derive asks for the table.
                self._repoint_copied_lookups(t, id_map)
                # Subset provenance travels only as far as its parent does:
                # parent_source_id is THIS case's numbering, and a later
                # import into the target could land on that id. Map rows
                # whose parent came along in this same copy are re-pointed;
                # the rest stay behind, and origin_meta drops the id while
                # keeping the parent's name for display. The table itself
                # copied whole above either way — the map is bookkeeping,
                # not evidence.
                for sid, new_id in id_map.items():
                    for old_parent, new_parent in id_map.items():
                        t.db.execute(
                            "INSERT OR IGNORE INTO subset_rids(source_id, rid, parent_source_id, parent_rid) "
                            "SELECT ?, rid, ?, parent_rid FROM srccase.subset_rids "
                            "WHERE source_id=? AND parent_source_id=?",
                            (new_id, new_parent, sid, old_parent))
                    raw = src_rows[sid].get("origin_meta")
                    if not raw:
                        continue
                    try:
                        meta = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(meta, dict) and "parent_source_id" in meta:
                        meta["parent_source_id"] = id_map.get(meta["parent_source_id"])
                        t.db.execute("UPDATE sources SET origin_meta=? WHERE id=?",
                                     (json.dumps(meta), new_id))
            finally:
                with contextlib.suppress(sqlite3.Error):
                    t.db.execute("DETACH srccase")
        return {"target": t.path, "copied": copied}

    @staticmethod
    def _repoint_copied_lookups(t: "Store", id_map: dict[int, int]) -> None:
        """See the call in _copy_sources_into. Runs on the target's writer,
        inside the copy's transaction, with this case attached as srccase."""
        for d in t.db.execute(
                "SELECT id, name, params FROM derived_columns WHERE op_id='lookup' AND source_id IN "
                f"({','.join('?' * len(id_map))})", list(id_map.values())).fetchall():
            params = json.loads(d["params"] or "{}")
            try:
                old = int(params.get("other_source_id"))
            except (TypeError, ValueError):
                continue
            new = id_map.get(old)
            if new is None:
                was = t.db.execute("SELECT file_hash, name FROM srccase.sources WHERE id=?",
                                   (old,)).fetchone()
                found = t.db.execute(
                    "SELECT id, name FROM sources WHERE file_hash=?",
                    (was["file_hash"],)).fetchall() if was and was["file_hash"] else []
                if len(found) > 1:
                    found = [f for f in found if f["name"] == was["name"]]
                new = found[0]["id"] if len(found) == 1 else None
            params["other_source_id"] = new
            t.db.execute("UPDATE derived_columns SET params=? WHERE id=?",
                         (json.dumps(params), d["id"]))

    def _map_tags_by_name(self, incoming_defs: list[dict]) -> dict:
        """{incoming tag id -> this case's tag id}, creating what's missing.
        Names pair by pair_tag_names, the rule session load uses. Caller
        holds self.lock inside a transaction."""
        mine = {r["name"]: r["id"] for r in self.db.execute("SELECT id, name FROM tag_defs")}
        paired = pair_tag_names([d["name"] for d in incoming_defs], mine)
        taken_keys = {r[0] for r in self.db.execute(
            "SELECT hotkey FROM tag_defs WHERE hotkey IS NOT NULL")}
        out = {}
        for d in incoming_defs:
            if d["name"] in paired:
                out[d["id"]] = mine[paired[d["name"]]]
                continue
            if d["name"] in mine:          # created a moment ago for an earlier def of the same name
                out[d["id"]] = mine[d["name"]]
                continue
            hotkey = d.get("hotkey") if d.get("hotkey") not in taken_keys else None
            if hotkey:
                taken_keys.add(hotkey)
            cur = self.db.execute(
                "INSERT INTO tag_defs(name, color, hotkey) VALUES (?,?,?)",
                (d["name"], d.get("color") or "#888888", hotkey))
            mine[d["name"]] = out[d["id"]] = cur.lastrowid
        return out

    # ------------------------------------------------- sessions in the case

    @staticmethod
    def _session_name(name: str | None) -> str:
        """The name a session is stored under, or ValueError saying why it
        can't be. Any text goes — the routes carry it as {name:path}, so a
        "/" (a date: "before 12/05") round-trips — except "." and "..",
        which no URL path can carry however they are escaped: a browser
        resolves them, %2E spellings included, before the request leaves,
        so a session saved under one could never be reached again."""
        name = (name or "").strip()
        if not name:
            raise ValueError("A session needs a name")
        if name in (".", ".."):
            raise ValueError(f"A session can't be called {name!r} — pick a name with a letter in it")
        return name

    def save_session(self, name: str) -> dict:
        """Snapshot the current analysis layer under `name`, in the case
        file. Saving over an existing name replaces it — the alternative is
        a pile of "triage-2-final-v3" and no way to tell them apart."""
        name = self._session_name(name)
        data = self.export_case_session()
        return self._write_session(name, data, origin="saved")

    def _write_session(self, name: str, data: dict, origin: str) -> dict:
        blob = json.dumps(data)
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO sessions(name, saved_at, origin, payload) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET saved_at=excluded.saved_at, "
                "origin=excluded.origin, payload=excluded.payload",
                (name, data.get("exported_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
                 origin, blob))
        return self._session_summary(name)

    def _session_summary(self, name: str) -> dict:
        with self._reader() as ro:
            row = ro.execute(
                "SELECT id, name, saved_at, origin, payload FROM sessions WHERE name=?",
                (name,)).fetchone()
        if not row:
            raise KeyError(f"No session named {name!r}")
        data = json.loads(row["payload"])
        srcs = data.get("sources", [])
        return {
            "id": row["id"], "name": row["name"], "saved_at": row["saved_at"],
            "origin": row["origin"], "source_count": len(srcs),
            # Rows, not tag assignments: a row carrying two tags is one
            # tagged row — what the panel's "N tagged" says, and what the
            # live table list counts (COUNT(DISTINCT rid)). Summing
            # row_tags read a row with three tags as three.
            "tagged_rows": sum(len({t.get("rid") for t in s.get("row_tags") or []}) for s in srcs),
            "notes": sum(len(s.get("row_notes") or []) for s in srcs),
        }

    def list_sessions(self) -> list[dict]:
        """Newest first — a session list is read to find recent work, not
        to browse alphabetically."""
        with self._reader() as ro:
            names = [r["name"] for r in ro.execute(
                "SELECT name FROM sessions ORDER BY saved_at DESC, id DESC")]
        return [self._session_summary(n) for n in names]

    def get_session(self, name: str) -> dict:
        return self._stored_session(name)[0]

    def _stored_session(self, name: str) -> tuple[dict, bool]:
        """(document, saved here). A session saved in this case — origin
        'saved', which only save_session writes — names tags by this case's
        own ids: tag_defs is AUTOINCREMENT, so an id is never handed to
        another tag, and a copy of the case file carries the same ids. One
        received from a file ('imported') was numbered by someone else's
        case. _session_tag_doubts is what this is asked for."""
        with self._reader() as ro:
            row = ro.execute("SELECT origin, payload FROM sessions WHERE name=?", (name,)).fetchone()
        if not row:
            raise KeyError(f"No session named {name!r}")
        return json.loads(row["payload"]), row["origin"] == "saved"

    def load_session(self, name: str, merge: bool = True) -> dict:
        """Apply a stored session to the live case. merge=False replaces the
        current tags and notes; merge=True adds to them."""
        doc, saved_here = self._stored_session(name)
        return self.import_case_session(doc, merge=merge, same_case=saved_here)

    def delete_session(self, name: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE name=?", (name,))

    def rename_session(self, name: str, new_name: str) -> dict:
        new_name = self._session_name(new_name)
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM sessions WHERE name=?", (new_name,)).fetchone():
                raise ValueError(f"A session named {new_name!r} already exists")
            cur = self.db.execute("UPDATE sessions SET name=? WHERE name=?", (new_name, name))
            if not cur.rowcount:
                raise KeyError(f"No session named {name!r}")
        return self._session_summary(new_name)

    def adopt_session(self, name: str, data: dict) -> dict:
        """Record a session that arrived as a FILE from another analyst, so
        it travels with the case from now on. Applying it is a separate
        step — receiving someone's work and merging it into your own are
        different decisions."""
        if data.get("format") != "winnow-case-session/1":
            raise ValueError("That file isn't a Winnow case session")
        return self._write_session(self._session_name(name), data, origin="imported")

    LIVE_SESSION = "__live__"

    def start_new_session(self, save_as: str | None = None) -> dict:
        """Clear the live tags and notes for a fresh pass over the same
        evidence, optionally saving what's there first.

        Clears conclusions only. Layouts, derived columns, SQL
        tabs and the tag palette all stay — they are how the evidence is
        READ, and starting a second pass doesn't mean re-deriving timestamps
        or re-hiding columns.

        Not routed through _apply_tag_change (invariant #7) on purpose: the
        undo journal is bounded by UNDO_ROW_BUDGET and a case with a million
        tagged rows would blow straight through it. Saving first is the
        undo, and it is a better one — a named session you can diff against
        rather than a single step you can lose. Bypassing the delta path
        means bypassing its state-generation bump too, so this does it
        itself — a dashboard whose tag counts were worked out during the
        last pass is wrong about this one, not merely old."""
        # One hold of the (re-entrant) writer lock from the snapshot to the
        # clear. Saved and cleared under separate holds, a tag written in
        # between — a watchlist scan's auto-tag, a second window — landed
        # after the snapshot and before the DELETE, and so was in neither:
        # gone, with the session reading as complete. The same hold keeps
        # the undo discard from taking an entry recorded after the clear.
        with self.lock:
            saved = self.save_session(save_as) if save_as else None
            with self.db:
                tags = self.db.execute("SELECT COUNT(*) FROM row_tags").fetchone()[0]
                notes = self.db.execute("SELECT COUNT(*) FROM row_notes").fetchone()[0]
                self.db.execute("DELETE FROM row_tags")
                self.db.execute("DELETE FROM row_notes")
                self._bump_state_generation()
            # The undo entries reference rows that no longer carry those tags;
            # replaying one would reinsert assignments this deliberately cleared.
            self._discard_undo()
        return {"saved": saved, "tags_cleared": tags, "notes_cleared": notes}

    def _drop_undo_for_source(self, source_id: int) -> None:
        """Undo entries with any row in a source that no longer exists.
        Mirrors _drop_undo_for_tag.

        By the rows the entry recorded, not by the source it was made
        from: an entry made on a merge (or from a Timeline selection) is
        filed under the merge's id but holds member rows, so dropping a
        member after deleting the merge used to leave it behind — and with
        ids reused, Ctrl+Z then tagged rows of the next file imported.
        `count_ids` (the real tables a change could touch) rules most
        entries out without reading them."""
        with self.lock:
            keep = []
            for entry in self._undo:
                ids = entry.get("count_ids")
                if ids is None:
                    ids = [entry.get("source_id")]
                touches = entry.get("source_id") == source_id
                if not touches and source_id in ids:
                    try:
                        touches = self.db.execute(
                            f"SELECT 1 FROM v.{q(entry['table'])} WHERE source_id=? LIMIT 1",
                            (source_id,)).fetchone() is not None
                    except sqlite3.Error:
                        touches = True   # can't tell: drop it rather than risk replaying it
                if touches:
                    with contextlib.suppress(sqlite3.Error):
                        self.db.execute(f"DROP TABLE IF EXISTS v.{q(entry['table'])}")
                    continue
                keep.append(entry)
            self._undo[:] = keep

    def _discard_undo(self) -> None:
        """Drop the whole undo journal and its delta tables.

        Called by anything that replaces tag state wholesale rather than by
        a recorded delta (invariant #7). An entry names rows by (source_id,
        rid) and a direction; once the state it was recorded against is
        gone, replaying it writes tags onto rows nobody chose — which is
        exactly the silent corruption the invariant exists to prevent, and
        the undo label still reads as the analyst's own last action."""
        with self.lock:
            for entry in self._undo:
                with contextlib.suppress(sqlite3.Error):
                    self.db.execute(f"DROP TABLE IF EXISTS v.{q(entry['table'])}")
            self._undo.clear()

    def _session_tag_map(self, data: dict, keys: list[str],
                         split: set[str] = frozenset()) -> tuple[dict, dict, dict]:
        """(tags_by_row, notes_by_row, source_labels) for a session document.

        Keyed by (source key, rid) rather than source_id, because two cases
        number their sources differently — a QC review is usually the
        reviewer's case against the analyst's file. `keys` names each of the
        document's sources, in order; diff_sessions derives them from the
        evidence's own fingerprint (_diff_source_keys), which is what makes
        that work at all.

        A tag is its name, except a name in `split` — one several tags
        carry, in two documents numbered by the same case
        (_diff_tag_doubts) — which is told apart by its id: "New tag #4".
        """
        tags: dict = {}
        notes: dict = {}
        labels: dict = {}
        for src, key in zip(data.get("sources", []), keys):
            meta = src.get("source") or {}
            labels[key] = meta.get("name") or key
            names = {t["id"]: (f"{t['name']} #{t['id']}" if t["name"] in split else t["name"])
                     for t in (src.get("tag_defs") or [])}
            for rt in src.get("row_tags") or []:
                tags.setdefault((key, rt["rid"]), set()).add(
                    names.get(rt["tag_id"], f"tag:{rt['tag_id']}"))
            for rn in src.get("row_notes") or []:
                notes[(key, rn["rid"])] = rn.get("note") or ""
        return tags, notes, labels

    @staticmethod
    def _diff_tag_doubts(ldoc: dict, rdoc: dict, *, same_case: bool) -> tuple[set[str], list[str]]:
        """(names to tell apart by id, names that can't be) for a diff: the
        names that more than one tag carries on rows of either document.
        Compared as names, a row moved from one "New tag" to the other was
        no change at all, and nor was the load that collapsed them.

        When both documents were numbered by this case (a session saved
        here, or the live case — _stored_session), the ids settle it, as
        they do for a load: every such name is split by id on both sides,
        a name two tags carry between the two sides included (one "New
        tag" on each, different ids, is a row that moved). From another
        case ids mean nothing (invariant #10), so the names a side repeats
        are returned to be reported instead, sorted."""
        def used(doc: dict) -> dict[str, set[int]]:
            out: dict[str, set[int]] = {}
            for src in doc.get("sources", []):
                names = {t["id"]: t["name"] for t in src.get("tag_defs") or []}
                for rt in src.get("row_tags") or []:
                    if rt.get("tag_id") in names:
                        out.setdefault(names[rt["tag_id"]], set()).add(rt["tag_id"])
            return out

        lu, ru = used(ldoc), used(rdoc)
        if same_case:
            return {n for n in lu.keys() | ru.keys() if len(lu.get(n, set()) | ru.get(n, set())) > 1}, []
        return set(), sorted({n for side in (lu, ru) for n, ids in side.items() if len(ids) > 1})

    @classmethod
    def _session_identity(cls, meta: dict) -> tuple:
        """Everything a session records about a source, as one comparable
        value: (file_hash, name, base column names, row_count). A live
        source (list_sources) reads the same way, minus its derived
        columns — the session's column list is the evidence's own shape."""
        cols = meta.get("columns") or []
        if any(isinstance(c, dict) and c.get("derived") for c in cols):
            cols = cls._base_cols(meta)
        names = tuple(c.get("name") if isinstance(c, dict) else c for c in cols)
        return (meta.get("file_hash") or None, meta.get("name") or "", names, meta.get("row_count"))

    @classmethod
    def _match_session_sources(cls, entries: list[dict], candidates: list[dict],
                               *, hashless: bool = False) -> dict[int, int]:
        """Pair session source records with the sources they describe:
        {entry index: candidate index}, one-to-one. The candidates are the
        live case's sources, or (for the diff) the other session's.

        The file hash says which FILE; it cannot say which table of it.
        Every table imported out of one SQLite database, every sheet of one
        workbook and a file imported twice share it, and matching on it
        alone (the old {hash: id} dict, last one wins) put one table's tags
        on another's rows. So the hash is required and the rest of what a
        session records picks the table, coarsest last:

        1. hash, name, columns and row count all agree. Records equal in
           all four are indistinguishable, so n of them pair with n
           candidates in order — document order against id order, which is
           import order on both sides of a session restored into its own
           case (export_case_session lists by id).
        2. hash and name — the same table with a different shape (imported
           again with other options).
        3. hash, columns and row count — the same table under another name,
           which is the ordinary QC case: the reviewer named their tables.
        4. hash alone, when it is the only record and the only candidate of
           that file left — the single-table file, as it always matched.

        Below tier 1 only one-to-one pairs: two records that agree on a
        coarse key but not a finer one are different tables, and pairing
        them in order would be a guess. Whatever is left unpaired is
        reported by the caller, never placed. Never matched on ids: two
        cases number their sources independently (invariant #10).

        A table made inside Winnow has a synthetic hash (MADE_HASH_PREFIX,
        SUBSET_HASH_PREFIX) and pairs by it exactly like a file. A record
        with NO hash is one a session wrote before made tables had one:
        `hashless` lets two such records pair by tiers 1 and 2 — name
        required — which is what the diff does between two documents.
        Never against a live table: load, the lookup resolver and the
        diff's pivot leave it unmatched, since a same-named table here may
        have been made over again since (import_case_session)."""
        ie = [cls._session_identity(m) for m in entries]
        ic = [cls._session_identity(m) for m in candidates]
        free_e = [i for i, x in enumerate(ie) if x[0] or hashless]
        free_c = [j for j, x in enumerate(ic) if x[0] or hashless]
        tiers = ((lambda x: x, True),
                 (lambda x: x[:2], False),
                 (lambda x: (x[0], x[2], x[3]), False),
                 (lambda x: x[0], False))
        pairs: dict[int, int] = {}
        for tier, (key, n_to_n) in enumerate(tiers):
            by_e: dict = {}
            by_c: dict = {}
            for i in free_e:
                if tier < 2 or ie[i][0]:
                    by_e.setdefault(key(ie[i]), []).append(i)
            for j in free_c:
                if tier < 2 or ic[j][0]:
                    by_c.setdefault(key(ic[j]), []).append(j)
            for k, es in by_e.items():
                cs = by_c.get(k, [])
                if not cs or len(es) != len(cs) or (len(es) > 1 and not n_to_n):
                    continue
                pairs.update(zip(es, cs))
            free_e = [i for i in free_e if i not in pairs]
            taken = set(pairs.values())
            free_c = [j for j in free_c if j not in taken]
        return pairs

    @classmethod
    def _diff_source_keys(cls, left: list[dict], right: list[dict]) -> tuple[list[str], list[str]]:
        """One key per source on each side of a diff, equal exactly when
        the two records describe the same table (_match_session_sources).
        The file hash where that alone is unambiguous, which keeps the
        common single-table shape what it always was; hash plus name, and
        then a counter, where several tables share it."""
        pairs = cls._match_session_sources(left, right, hashless=True)
        units = [(i, pairs.get(i)) for i in range(len(left))]
        paired_right = set(pairs.values())
        units += [(None, j) for j in range(len(right)) if j not in paired_right]
        metas = [left[i] if i is not None else right[j] for i, j in units]
        bases = [m.get("file_hash") or m.get("name") or "" for m in metas]
        counts: dict = {}
        for b in bases:
            counts[b] = counts.get(b, 0) + 1
        keys, seen = [], {}
        for b, m in zip(bases, metas):
            k = b if counts[b] == 1 else f"{b}|{m.get('name') or ''}"
            seen[k] = seen.get(k, 0) + 1
            keys.append(k if seen[k] == 1 else f"{k}|{seen[k]}")
        lk: list = [None] * len(left)
        rk: list = [None] * len(right)
        for (i, j), k in zip(units, keys):
            if i is not None:
                lk[i] = k
            if j is not None:
                rk[j] = k
        return lk, rk

    def diff_sessions(self, left: str, right: str, limit: int = 2000) -> dict:
        """What changed between two sessions — the QC question: "what did
        the reviewer tag that I didn't, and what did I tag that they
        removed?"

        Either side may be LIVE_SESSION to compare against the case's
        current state, which is the common review shape: a stored session
        from the first analyst against what the reviewer has now.

        Tags are compared BY NAME, not by tag_id: two analysts' cases
        number their tags independently, so "TA-1" in one is rarely id 3 in
        the other. Comparing ids would report every row as different.
        A name more than one tag carries is the exception (_diff_tag_doubts):
        told apart by id when both sides were numbered by this case, and
        otherwise listed in `duplicate_tag_names`, since a row moved
        between two tags of one name is no difference by name.
        """
        def _load(which):
            if which == self.LIVE_SESSION:
                return self.export_case_session(), True
            return self._stored_session(which)

        (ldoc, lhere), (rdoc, rhere) = _load(left), _load(right)
        lmeta = [s.get("source") or {} for s in ldoc.get("sources", [])]
        rmeta = [s.get("source") or {} for s in rdoc.get("sources", [])]
        lkeys, rkeys = self._diff_source_keys(lmeta, rmeta)
        split, dup_names = self._diff_tag_doubts(ldoc, rdoc, same_case=lhere and rhere)
        lt, ln, llab = self._session_tag_map(ldoc, lkeys, split)
        rt, rn, rlab = self._session_tag_map(rdoc, rkeys, split)
        labels = {**rlab, **llab}
        # The right side's names in the left side's spelling where
        # pair_tag_names pairs them — the rule loading either session
        # would apply — so the diff reports what a load would change.
        paired = pair_tag_names({n for v in rt.values() for n in v}, {n for v in lt.values() for n in v})
        rt = {k: {paired.get(n, n) for n in v} for k, v in rt.items()}

        added, removed, changed, note_changes = [], [], [], []
        for key in sorted(set(lt) | set(rt), key=lambda k: (str(k[0]), k[1])):
            a, b = lt.get(key, set()), rt.get(key, set())
            if a == b:
                continue
            row = {"source": labels.get(key[0], key[0]), "key": key[0], "rid": key[1],
                   "left": sorted(a), "right": sorted(b)}
            if not a:
                added.append(row)
            elif not b:
                removed.append(row)
            else:
                changed.append(row)
        for key in sorted(set(ln) | set(rn), key=lambda k: (str(k[0]), k[1])):
            a, b = ln.get(key), rn.get(key)
            if a != b:
                note_changes.append({"source": labels.get(key[0], key[0]), "key": key[0], "rid": key[1],
                                     "left": a, "right": b})

        groups = {"added": added, "removed": removed, "changed": changed, "note_changes": note_changes}
        counts = {k: len(v) for k, v in groups.items()}
        # Per table, uncapped: what the panel shows as its counts. The row
        # lists below are capped, so a table past the cap still reports
        # how many rows differ even when not all of them can be opened.
        # Each key resolves to the live table its record matches — by the
        # same rule a load uses (no `hashless`: a record with no hash is
        # never placed on a live table), so a diff never pivots into
        # evidence a load would not have put those tags on.
        live = self.list_sources()
        live_id: dict = {}
        for metas, keys in ((rmeta, rkeys), (lmeta, lkeys)):   # left wins
            for i, j in self._match_session_sources(metas, live).items():
                live_id[keys[i]] = live[j]["id"]
        resolve = live_id.get
        per_source: dict = {}
        for kind, rows in groups.items():
            for r in rows:
                ps = per_source.setdefault(r["key"], {
                    "source": r["source"], "source_id": resolve(r["key"]),
                    "counts": {k: 0 for k in groups}})
                ps["counts"][kind] += 1
        # Each listed row names its live source, so the panel's counts can
        # pivot to the table showing exactly those rows.
        capped = {k: v[:limit] for k, v in groups.items()}
        for rows in capped.values():
            for r in rows:
                r["source_id"] = resolve(r.pop("key"))

        return {
            "left": left, "right": right,
            # "only in right" reads as added when right is the later pass,
            # which is how a review is run: left = what was handed over.
            **capped,
            "counts": counts,
            "sources": [per_source[k] for k in sorted(per_source, key=str)],
            "truncated": max(counts.values(), default=0) > limit,
            # A shared source is one both sides have evidence for; anything
            # else means the two sessions are describing different cases and
            # the numbers above are not a like-for-like comparison.
            "shared_sources": sorted(set(llab) & set(rlab)),
            "only_left_sources": sorted(set(llab) - set(rlab)),
            "only_right_sources": sorted(set(rlab) - set(llab)),
            # Names that more than one tag on one side carries, where the
            # two sides weren't numbered by one case and so can't be told
            # apart: rows moved between those tags are in no count above.
            "duplicate_tag_names": dup_names,
        }

    def export_session_file(self, name: str, path: str) -> dict:
        """Write a stored session out as JSON — the hand-off to another
        analyst, and now the only reason a session file exists at all."""
        data = self.get_session(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return {"name": name, "path": os.path.abspath(path),
                "bytes": os.path.getsize(path)}

    def save_named_session(self, name: str) -> dict:
        data = self.export_case_session()
        with open(self._session_path(name), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return {"name": name, "source_count": len(data["sources"]), "saved_at": data["exported_at"]}

    def list_named_sessions(self) -> list[dict]:
        out = []
        d = self._sessions_dir()
        for fn in os.listdir(d):
            if not fn.endswith(".winnow_case.json"):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({
                    "name": fn[: -len(".winnow_case.json")],
                    "saved_at": data.get("exported_at"),
                    "source_count": len(data.get("sources", [])),
                })
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda s: s.get("saved_at") or "", reverse=True)
        return out

    def load_named_session(self, name: str, merge: bool = True) -> dict:
        path = self._session_path(name)
        if not os.path.isfile(path):
            raise KeyError(f"No saved session named {name!r}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return self.import_case_session(data, merge=merge)

    def delete_named_session(self, name: str) -> None:
        path = self._session_path(name)
        if os.path.isfile(path):
            os.remove(path)

    # --------------------------------------------------------------- exporting

    def export_view_csv(self, view_id: str, tagged_only: bool = False):
        """Validates the view eagerly, then returns a generator for the CSV
        body. Splitting this out matters: a generator function's body
        (including a KeyError check) doesn't run until first iterated, which
        would happen inside StreamingResponse — too late for the route's
        try/except to turn it into a 409."""
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        # Covers all three export shapes at once, and eagerly: the body
        # below is a generator, so an error raised inside it would surface
        # from StreamingResponse rather than from the route's try/except.
        # On a pooled reader, never _source_lite — that takes the writer
        # lock, and an export must not block behind a build (invariant #4;
        # test_concurrency.py fails the moment it does).
        with self._reader() as ro:
            self._require_columns(self._source_lite_on(ro, handle["source_id"]))
        if handle.get("kind") == "group_virtual":
            return self._export_virtual_group_csv_rows(handle, tagged_only)
        if handle.get("kind") == "root_virtual":
            return self._export_virtual_root_csv_rows(handle, tagged_only)
        return self._export_view_csv_rows(view_id, handle, tagged_only)

    def _export_columns(self, conn: sqlite3.Connection, src: dict) -> list[str]:
        """The columns an export should emit: the analyst's saved layout —
        their column ORDER, with hidden columns excluded — rather than raw
        storage order. What lands in the CSV/XLSX is the table as they
        arranged it on screen. Columns the layout has never seen (a derived
        column added since the last save) append at the end; a layout that
        somehow hides everything falls back to all columns, because an
        empty export helps nobody."""
        all_names = [c["name"] for c in src["columns"]]
        # Read on the caller's pooled connection, NEVER via get_layout: the
        # export paths run without touching Store.lock (invariant #4 — the
        # concurrency suite fails the moment an export blocks on a writer).
        try:
            row = conn.execute("SELECT payload FROM layouts WHERE source_id=?", (src["id"],)).fetchone()
            layout = json.loads(row["payload"]) if row else {}
        except sqlite3.Error:
            layout = {}
        per_col = layout.get("columns") or {}
        known = set(all_names)
        ordered = [n for n in (layout.get("order") or []) if n in known]
        # Mirrors sources.js's placement of a column the layout never saw:
        # a derived column lands right after its input (after siblings
        # already there), anything else at the end — so an export before
        # the next layout save agrees with the screen.
        from_by_name = {c["name"]: c.get("derived_from") for c in src["columns"]}
        for name in all_names:
            if name in ordered:
                continue
            src_col = from_by_name.get(name)
            if not src_col or src_col not in ordered:
                ordered.append(name)
                continue
            at = ordered.index(src_col)
            while at + 1 < len(ordered) and from_by_name.get(ordered[at + 1]) == src_col:
                at += 1
            ordered.insert(at + 1, name)
        visible = [n for n in ordered if not (per_col.get(n) or {}).get("hidden")]
        return visible or all_names

    def _export_virtual_group_csv_rows(self, handle: dict, tagged_only: bool):
        member = handle["members"][0]
        sid = member["source_id"]
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, sid)
            # The analyst's layout, like every other export path. This one
            # used raw storage order, so the same rows exported from an
            # expanded group carried columns they had taken off screen,
            # while exporting them from a filtered parent did not.
            cols = self._export_columns(ro, src)
            where_sql, where_params = self._virtual_group_where(handle, ro)
            sel = ", ".join(q(c) for c in cols)

            tagnames = {r["id"]: r["name"] for r in ro.execute("SELECT id, name FROM tag_defs")}
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(_export_header(cols, _csv_safe))
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)

            rows = ro.execute(
                f"SELECT rid, {sel} FROM {self._member_from(src, member)} WHERE {where_sql} ORDER BY rid",
                where_params,
            ).fetchall()
            rids = [r["rid"] for r in rows]
            tmap: dict[int, list[str]] = {}
            nmap: dict[int, str] = {}
            if rids:
                ph = ",".join("?" * len(rids))
                for rid, tid in ro.execute(
                    f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    tmap.setdefault(rid, []).append(tagnames.get(tid, str(tid)))
                for rid, note in ro.execute(
                    f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    nmap[rid] = note

        for r in rows:
            if tagged_only and r["rid"] not in tmap:
                continue
            w.writerow([r["rid"], _csv_safe("; ".join(tmap.get(r["rid"], []))), _csv_safe(nmap.get(r["rid"], "")), *[_csv_safe(r[c]) for c in cols]])
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)

    def _export_virtual_root_csv_rows(self, handle: dict, tagged_only: bool):
        """Streams the whole source table in rid order, chunked the same
        keyset way _export_view_csv_rows pages v.view_N (`rid > ?` here
        instead of `pos > ?`) — a root_virtual view can be the entire
        source, potentially millions of rows, unlike the small bounded
        group _export_virtual_group_csv_rows fetches in one shot. Runs
        entirely on one checked-out _reader() connection, held across
        yields (safe — see _reader's docstring; an abandoned generator's
        GeneratorExit closes it), so a multi-minute export never touches
        self.lock at all."""
        sid = handle["source_id"]
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, sid)
            table = src["table_name"]
            cols = self._export_columns(ro, src)
            sel = ", ".join(q(c) for c in cols)

            tagnames = {r["id"]: r["name"] for r in ro.execute("SELECT id, name FROM tag_defs")}
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(_export_header(cols, _csv_safe))
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)

            last_rid = 0
            while True:
                chunk = ro.execute(
                    f"SELECT rid, {sel} FROM {self._from_clause(src)} WHERE rid > ? ORDER BY rid LIMIT 5000",
                    (last_rid,),
                ).fetchall()
                if not chunk:
                    break
                last_rid = chunk[-1]["rid"]
                rids = [r["rid"] for r in chunk]
                ph = ",".join("?" * len(rids))
                tmap: dict[int, list[str]] = {}
                nmap: dict[int, str] = {}
                if tagged_only:
                    tagged_set = {
                        row[0] for row in ro.execute(
                            f"SELECT DISTINCT rid FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                        )
                    }
                else:
                    tagged_set = None
                for rid, tid in ro.execute(
                    f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    tmap.setdefault(rid, []).append(tagnames.get(tid, str(tid)))
                for rid, note in ro.execute(
                    f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                ):
                    nmap[rid] = note

                for r in chunk:
                    if tagged_only and r["rid"] not in tagged_set:
                        continue
                    w.writerow([r["rid"], _csv_safe("; ".join(tmap.get(r["rid"], []))), _csv_safe(nmap.get(r["rid"], "")),
                               *[_csv_safe(r[c]) for c in cols]])
                yield buf.getvalue()
                buf.seek(0), buf.truncate(0)

    def _export_view_csv_rows(self, view_id: str, handle: dict, tagged_only: bool):
        """Streams chunks of v.view_N grouped by each row's own source_id —
        same per-member resolution pattern as fetch_rows, so this works for
        merged views without a single cross-table JOIN.

        Runs entirely on one checked-out _reader() connection, held across
        yields — safe, unlike the RLock this used to have to release before
        every yield (a generator can be resumed or abandoned from a
        different thread than the one that acquired the lock; a connection
        is just an object, and GeneratorExit on early client disconnect
        closes it via _reader's exception path). Keyset pagination
        (`pos > ?`) over v.view_N's unique index avoids the
        OFFSET-rescans-from-zero cost that would otherwise reintroduce.
        """
        with self._reader() as ro, self._dropped_view_is_expired():
            src = self._source_lite_on(ro, handle["source_id"])
            cols = self._export_columns(ro, src)
            sel = ", ".join(q(c) for c in cols)
            # A merge's rows come from several tables, and Line is the rid
            # within ONE of them — so without saying which, two rows read
            # "Line 1" and nothing in the file tells the reader where either
            # is. A merge export carries the member's name (as the sidebar
            # shows it) after Line; a single table's stays as it was.
            member_names: dict[int, str] | None = None
            if src.get("is_merge"):
                ids = [int(i) for i in src["member_source_ids"]]
                member_names = {
                    r["id"]: r["nickname"] or r["name"] for r in ro.execute(
                        f"SELECT id, name, nickname FROM sources WHERE id IN ({','.join('?' * len(ids))})",
                        ids)}

            tagnames = {r["id"]: r["name"] for r in ro.execute("SELECT id, name FROM tag_defs")}
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            head = _export_header(cols, _csv_safe)
            if member_names is not None:
                head.insert(1, "Table")
            w.writerow(head)
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)

            last_pos = 0
            while True:
                chunk = ro.execute(
                    f"SELECT pos, source_id, rid FROM v.{q(view_id)} WHERE pos > ? ORDER BY pos LIMIT 5000",
                    (last_pos,),
                ).fetchall()
                if not chunk:
                    break
                last_pos = chunk[-1]["pos"]

                by_source: dict[int, list[int]] = {}
                for r in chunk:
                    by_source.setdefault(r["source_id"], []).append(r["rid"])

                cellmap: dict[tuple[int, int], tuple] = {}
                tmap: dict[tuple[int, int], list[str]] = {}
                nmap: dict[tuple[int, int], str] = {}
                for sid, rids in by_source.items():
                    if tagged_only:
                        ph0 = ",".join("?" * len(rids))
                        tagged_set = {
                            row[0] for row in ro.execute(
                                f"SELECT DISTINCT rid FROM row_tags WHERE source_id=? AND rid IN ({ph0})",
                                [sid, *rids],
                            )
                        }
                        rids = [r for r in rids if r in tagged_set]
                        if not rids:
                            continue
                    member_src = self._source_lite_on(ro, sid)
                    ph = ",".join("?" * len(rids))
                    for row in ro.execute(
                        f"SELECT rid, {sel} FROM {self._member_rows_from(src, member_src)} WHERE rid IN ({ph})", rids
                    ):
                        cellmap[(sid, row["rid"])] = tuple(row[c] for c in cols)
                    for rid, tid in ro.execute(
                        f"SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                    ):
                        tmap.setdefault((sid, rid), []).append(tagnames.get(tid, str(tid)))
                    for rid, note in ro.execute(
                        f"SELECT rid, note FROM row_notes WHERE source_id=? AND rid IN ({ph})", [sid, *rids]
                    ):
                        nmap[(sid, rid)] = note

                for r in chunk:
                    key = (r["source_id"], r["rid"])
                    if key not in cellmap:
                        continue  # tagged_only filtered this row out
                    w.writerow([
                        r["rid"],
                        *([_csv_safe(member_names.get(r["source_id"], f"table {r['source_id']}"))]
                          if member_names is not None else []),
                        _csv_safe("; ".join(tmap.get(key, []))),
                        _csv_safe(nmap.get(key, "")),
                        *[_csv_safe(v) for v in cellmap[key]],
                    ])
                yield buf.getvalue()
                buf.seek(0), buf.truncate(0)

    def export_tagged_xlsx(self) -> io.BytesIO:
        """One worksheet per real source that has at least one tagged row —
        merges are skipped since their rows already belong to a real source
        sheet, and sources with zero tagged rows are skipped so the workbook
        doesn't fill up with empty tabs. No view/filter/sort involved (only
        "tagged or not"), so unlike _export_view_csv_rows this doesn't need
        v.view_N at all — just row_tags per source_id. Reads run on a
        _reader() connection checked out per source, so a big multi-sheet
        export never touches self.lock.

        The tagged rows are read in keyset pages, cells joined to the page
        of row_tags they came from and tags/notes fetched by the page's rid
        RANGE — never a list of rids. It used to bind every tagged rid of a
        source into one `IN (...)`, which past 32,766 tagged rows is "too
        many SQL variables" on a stock SQLite build (python.org's Windows
        Python), so the export failed outright for exactly the cases with
        the most work in them. A source with more tagged rows than one
        worksheet holds continues on numbered sheets (_xlsx_write_sheets,
        the all-tables rule); write-only, so no cell objects pile up."""
        tagnames = {t["id"]: t["name"] for t in self.list_tags()}
        wb = Workbook(write_only=True)
        used_names: set[str] = set()
        sheets = 0

        for src in self.list_sources():
            if not src.get("tagged_row_count"):
                continue
            source_id = src["id"]
            with self._reader() as ro, self._dropped_view_is_expired():
                cols = self._export_columns(ro, src)
                sel = ", ".join(self._col_ref(src, c, "s") for c in cols)

                def tagged_rows(ro=ro, sel=sel, source_id=source_id, src=src):
                    last = 0
                    while True:
                        page = ro.execute(
                            f"SELECT s.rid, {sel} FROM {self._from_clause(src, 's')} WHERE s.rid IN "
                            f"(SELECT DISTINCT rid FROM row_tags WHERE source_id=? AND rid > ? "
                            f"ORDER BY rid LIMIT {BATCH}) ORDER BY s.rid", (source_id, last),
                        ).fetchall()
                        if not page:
                            return
                        lo, hi = page[0][0], page[-1][0]
                        tmap: dict[int, list[str]] = {}
                        for rid, tid in ro.execute(
                            "SELECT rid, tag_id FROM row_tags WHERE source_id=? AND rid BETWEEN ? AND ?",
                            (source_id, lo, hi),
                        ):
                            tmap.setdefault(rid, []).append(tagnames.get(tid, str(tid)))
                        nmap = {rid: note for rid, note in ro.execute(
                            "SELECT rid, note FROM row_notes WHERE source_id=? AND rid BETWEEN ? AND ?",
                            (source_id, lo, hi))}
                        for row in page:
                            rid = row[0]
                            yield [
                                rid,
                                _xlsx_safe("; ".join(tmap.get(rid, []))),
                                _xlsx_safe(nmap.get(rid, "")),
                                *[_xlsx_safe(v) for v in row[1:]],
                            ]
                        last = hi

                n_sheets, _ = _xlsx_write_sheets(wb, used_names, src["name"],
                                                 _export_header(cols, _xlsx_safe), tagged_rows())
                sheets += n_sheets

        if not sheets:
            wb.create_sheet("No tagged rows").append(["No rows are tagged in this case yet."])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    def export_all_xlsx_plan(self) -> dict:
        """What "export all tables" would write, before writing it: every
        real table with its row count, how many worksheets it needs under
        Excel's per-sheet cap, and which ones overflow — so the UI can say
        so and ask before a multi-minute export starts. Merges are skipped
        (their rows already belong to a member's sheet), as are sources
        that failed to import."""
        per = XLSX_MAX_ROWS - 1   # one row is the header
        tables = []
        for src in self.list_sources():
            if src.get("is_merge") or src.get("error"):
                continue
            rows = src.get("row_count") or 0
            tables.append({"id": src["id"], "name": src["name"], "rows": rows,
                           "sheets": max(1, -(-rows // per)), "over_cap": rows > per})
        return {"tables": tables, "rows_per_sheet": per,
                "over_cap": [t["name"] for t in tables if t["over_cap"]]}

    def export_all_xlsx(self, path: str) -> dict:
        """Every real table, every row, one worksheet per table — the
        whole case as a workbook. Merges are not tables of their own here:
        every one of a merge's rows is a member's row and lands on that
        member's sheet (a merge-level derived column is the one thing
        that doesn't travel — recorded as an export exception under
        invariant #9). Same header convention as the tagged
        export (Line, Tags, Note, then the analyst's layout columns), same
        formula guard, same write-only workbook and the same continuation
        sheets (_xlsx_write_sheets). Differences from export_tagged_xlsx,
        each because "all rows" is a different size of thing:

        - Saved to `path` (a temp file the route hands to FileResponse)
          rather than an in-memory BytesIO — even compressed, every row of
          the case is far more than its tagged subset.
        - Rows are read in keyset chunks (`WHERE rid > ? ORDER BY rid`)
          on one _reader() checkout per source; never self.lock
          (invariant #4). Tags and notes are preloaded per source with
          two whole-source queries — bounded by tagged/noted rows, no
          `IN (...)` list to grow.
        - A table past Excel's sheet cap continues on "Name (2)", "Name
          (3)"…, header re-emitted, so every row lands. The plan above is
          how the analyst hears about it first.

        Returns {"sheets": n, "rows": n} for the log line."""
        tagnames = {t["id"]: t["name"] for t in self.list_tags()}
        wb = Workbook(write_only=True)
        used_names: set[str] = set()
        sheets = total_rows = 0

        for src in self.list_sources():
            if src.get("is_merge") or src.get("error"):
                continue
            source_id = src["id"]
            with self._reader() as ro, self._dropped_view_is_expired():
                cols = self._export_columns(ro, src)
                sel = ", ".join(self._col_ref(src, c) for c in cols)
                tmap: dict[int, list[str]] = {}
                for rid, tid in ro.execute(
                    "SELECT rid, tag_id FROM row_tags WHERE source_id=? ORDER BY rid", (source_id,)
                ):
                    tmap.setdefault(rid, []).append(tagnames.get(tid, str(tid)))
                nmap = {rid: note for rid, note in ro.execute(
                    "SELECT rid, note FROM row_notes WHERE source_id=?", (source_id,))}

                def all_rows(ro=ro, sel=sel, src=src, tmap=tmap, nmap=nmap):
                    last_rid = 0
                    while True:
                        rows = ro.execute(
                            f"SELECT rid, {sel} FROM {self._from_clause(src)} "
                            f"WHERE rid > ? ORDER BY rid LIMIT {BATCH}", (last_rid,),
                        ).fetchall()
                        if not rows:
                            return
                        for row in rows:
                            rid = row[0]
                            yield [
                                rid,
                                _xlsx_safe("; ".join(tmap.get(rid, []))),
                                _xlsx_safe(nmap.get(rid, "")),
                                *[_xlsx_safe(v) for v in row[1:]],
                            ]
                        last_rid = rows[-1][0]

                n_sheets, n_rows = _xlsx_write_sheets(wb, used_names, src["name"],
                                                      _export_header(cols, _xlsx_safe), all_rows())
                sheets += n_sheets
                total_rows += n_rows

        if not sheets:
            wb.create_sheet("No tables").append(["This case has no tables to export."])
            sheets = 1
        wb.save(path)
        return {"sheets": sheets, "rows": total_rows}

    def resolve_search_all_scope(self, source_ids: list[int] | None) -> dict:
        """The sweep's scope, turned into real source ids it can actually
        scan: `{"source_ids": [...] | None, "merges": [...]}`.

        None in, None out — the whole case, which is what the sweep has
        always done. Otherwise every id is resolved, in the order asked,
        deduped, and **a merge is expanded to its member_source_ids**: a
        merge has no `src_N` of its own (CLAUDE.md invariant #9's
        exceptions), so "search this table" on one can only mean searching
        the tables its rows actually live in. The expansion is reported in
        `merges` — `{id, name, member_source_ids}` per merge asked for — so
        the caller can say so rather than quietly answering a different
        question than the one asked.

        An id naming nothing raises KeyError (the routes turn that into a
        400): a typo'd id that silently scanned nothing would come back as
        "no matches", which is the wrong answer rather than an error. An
        empty list is a scope of no tables, and scans nothing.

        Resolution happens once, at the point the scope is accepted — the
        sweep itself never re-resolves. A table dropped between the scope
        being chosen and the sweep reaching it simply isn't in the
        `list_sources()` snapshot _iter_search_all_sources filters, so it
        costs that table its scan rather than ending the run, the way the
        unscoped sweep has always absorbed a source removed mid-sweep."""
        if source_ids is None:
            return {"source_ids": None, "merges": []}
        out: list[int] = []
        merges: list[dict] = []
        for sid in source_ids:
            src = self._source_lite(sid)          # KeyError: no such source/merge
            members = list(src["member_source_ids"]) if src.get("is_merge") else [sid]
            if src.get("is_merge"):
                merges.append({"id": sid, "name": src["name"], "member_source_ids": members})
            for m in members:
                if m not in out:
                    out.append(m)
        return {"source_ids": out, "merges": merges}

    def search_all_sources(self, query: str = "", terms: list[dict] | None = None,
                           source_ids: list[int] | None = None) -> list[dict]:
        """Every source's match count, sorted heaviest-first — the whole
        sweep, run to completion. See _iter_search_all_sources for the
        actual scan and its lock discipline; this is the collect-it-all
        wrapper, used by the synchronous endpoint and the tests.

        `source_ids` scopes it (resolve_search_all_scope); None is every
        real source in the case. It is resolved HERE, before the scan, so
        a caller naming a table this case doesn't have gets the KeyError
        its route turns into a 400 rather than an empty answer that reads
        like "no matches".

        A table the sweep could not count is in the list too, as
        `{source_id, name, error}` with no match_count — after the hits,
        since it has none — rather than left out, which reads as "no
        matches".

        start_search_all_job is the same sweep run on a background thread
        with incremental results, which is what the UI uses."""
        scope = self.resolve_search_all_scope(source_ids)["source_ids"]
        out = [hit for _, _, hit, _read in self._iter_search_all_sources(query, terms, scope) if hit]
        out.sort(key=lambda d: (bool(d.get("error")), -d.get("match_count", 0)))
        return out

    def _iter_search_all_sources(
        self, query: str = "", terms: list[dict] | None = None,
        source_ids: list[int] | None = None,
    ) -> Iterator[tuple[int, int, dict | None, dict | None]]:
        """Per-source match counts for a search across every real source in
        the case (merges excluded — their rows already belong to a real
        source), or across the subset `source_ids` names, no filter/sort/
        view materialisation involved. `source_ids` is REAL source ids,
        already resolved: both entry points come through
        resolve_search_all_scope, which is where a merge becomes its
        members and an unknown id becomes a KeyError.

        Yields `(scanned, total, hit_or_None, read_or_None)` after each
        source so a caller can report progress and surface partial results
        while the sweep is still running (start_search_all_job) — a source
        with no matches yields a None hit rather than being skipped
        silently, so `scanned` always advances. A source whose count
        raised yields `{source_id, name, error}` as its hit: it used to be
        recorded as no match, so a table the sweep could not read at all
        (one too wide for the search expression, before _sql_chain) was
        reported as "No matches." — the one answer an analyst moves on
        from. The exception is a table dropped since the sweep began,
        which has nothing left to report and still yields None. Unsorted, in source order:
        sorting is the collecting caller's job, since a running sweep has
        nothing stable to sort yet.

        `read` is the fourth element and a different kind of fact from the
        hit: what this sweep PROVED about a source, for
        seed_watchlist_from_search_all to hand to a watchlist scan instead
        of making it read the table again (the two use the same predicate —
        see _watchlist_match_sql). `{source_id, row_count, matched_terms}`,
        and it is None unless the source was read end to end: a count that
        raised mid-way is recorded as no match by the `except` below, which
        is the right answer for a progress report and would be a lie here —
        "clean" is a sentence an analyst puts in a report, and a table
        nobody could read has not earned it. `matched_terms` is the terms
        that did match, `[]` when the union count was zero (nothing matched,
        so no term did), and None when the source matched without a
        per-term breakdown — "something here matched, which of them is
        unknown", the one shape that proves nothing per term.

        Same indexed-when-ready-else-LIKE reasoning as _compile_where's
        Contains/Advanced branches for both `query` and `terms` — including
        triggering a background index build for any source that doesn't
        have one yet, and falling back to a blob LIKE for a term
        _fts_like_pattern can't route through the trigram index.

        Two things keep this from monopolising the case for its duration:

        - The lock is taken per source's count, not once around the whole
          loop. This is a read-only sweep over every table in the case, and
          on a case whose indexes aren't built yet that's N full LIKE scans
          back to back — minutes on a 42 GB merge. Holding self.lock across
          all of them freezes every other request (paging, tagging, view
          builds) for the entire sweep, which is exactly what ingest_csv and
          build_fts already go out of their way to avoid: hold the lock for
          one unit of work, not for a whole loop.
        - Each count stops at SEARCH_ALL_COUNT_CAP rows rather than counting
          every match, bounding the worst case per source (a term matching
          most of a huge unindexed table). A capped result reports
          match_count == SEARCH_ALL_COUNT_CAP with capped=True so the UI can
          say "1,000+" rather than imply a precise number it didn't compute.
        """
        query = (query or "").strip()
        terms = [t for t in (terms or []) if (t.get("term") or "").strip()]
        if not query and not terms:
            return
        # A pasted list of IOCs is a plain OR of positive terms, and there
        # the analyst wants an entry per term per table, not one row per
        # table with everything summed into it. Anything else (mixed
        # AND/NOT from the Advanced builder, a bare `query`) keeps the
        # single union count — see _or_of_positive_terms.
        breakdown = _or_of_positive_terms(terms) if terms else None
        if breakdown and len(breakdown) > SEARCH_ALL_TERM_BREAKDOWN_MAX:
            breakdown = None
        # Snapshot the source list up front (list_sources takes the lock
        # itself); the per-source work below re-acquires it one count at a
        # time. A source removed mid-sweep just yields a SQL error we skip.
        sources = [s for s in self.list_sources() if not s.get("error")]
        # Scope filtered straight onto that snapshot, the shape
        # _iter_watchlist_scan uses: an id whose table has been dropped
        # since the scope was chosen is not in the snapshot, so it costs
        # that table its scan and nothing else. `total` therefore counts
        # the tables this run will really scan — it is the progress
        # denominator the modal shows.
        if source_ids is not None:
            wanted = set(source_ids)
            sources = [s for s in sources if s["id"] in wanted]
        total = len(sources)
        scanned = 0
        for src in sources:
            source_id = src["id"]
            table = src["table_name"]
            # Base columns only — the search-all sweep has to agree with the
            # per-source search paths, which scan build_fts's doc view.
            cols = [c["name"] for c in self._base_cols(src)]
            if not src.get("has_fts"):
                self._ensure_fts_building(source_id)
            inner, params = self._search_all_count_sql(src, table, cols, query, terms)
            n = 0
            per_term: list[dict] = []
            read: dict | None = None
            if inner is not None:
                # One _reader() checkout per source's count — this loop
                # never touches self.lock at all (a scoped run pays one
                # locked resolve before it starts, not one per source), so
                # even its worst
                # case (N full LIKE scans on an unindexed 42 GB merge)
                # can't stall a single paging/tagging/view-build request.
                # Checked out per count rather than once for the sweep so
                # the connection goes back in the pool between sources.
                try:
                    with self._reader() as ro:
                        n = ro.execute(f"SELECT COUNT(*) FROM ({inner})", params).fetchone()[0]
                        # The per-term breakdown, and why it's here rather
                        # than folded into the query above: counting each
                        # term separately is the only way to say which of a
                        # pasted IOC list actually hit, and a single query
                        # with one SUM(CASE ...) per term would have to
                        # evaluate *every* term against every candidate row
                        # — losing the OR's short-circuit — on every source,
                        # including the ones that don't match at all. Gated
                        # on n instead, the sources that miss cost exactly
                        # what they cost before this existed, and only a
                        # source that already matched pays per term.
                        if n and breakdown:
                            per_term = self._search_all_term_counts(ro, src, table, cols, breakdown)
                        # Last, and inside the try on purpose: `read` is the
                        # claim that this source was read to the end, and a
                        # breakdown that raised after the union count landed
                        # must not leave one behind.
                        read = {"source_id": source_id, "row_count": src["row_count"],
                                "matched_terms": [] if not n
                                                 else [t["term"] for t in per_term] if breakdown
                                                 else None}
                except sqlite3.Error as e:
                    n = 0
                    per_term = []
                    read = None
                    with self._reader() as ro:
                        still_here = ro.execute(
                            "SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone() is not None
                    if still_here:
                        failed = {"source_id": source_id, "name": src["name"], "error": str(e)}
                        scanned += 1
                        yield scanned, total, failed, None
                        time.sleep(0.02)
                        continue
            hit = None
            if n:
                hit = {
                    "source_id": source_id,
                    "name": src["name"],
                    "match_count": min(n, SEARCH_ALL_COUNT_CAP),
                    "capped": n > SEARCH_ALL_COUNT_CAP,
                    # One entry per term that actually matched this source,
                    # heaviest first. Empty when the query isn't a plain OR
                    # of terms (see _or_of_positive_terms) — the modal falls
                    # back to the single row per table it always showed.
                    # Terms can overlap on a row, so these don't have to sum
                    # to match_count.
                    "terms": per_term,
                }
            scanned += 1
            # Every source yields, matches or not, so `scanned` tracks real
            # progress through the sweep rather than only counting hits.
            yield scanned, total, hit, read
            # No lock to starve anyone on anymore — this is now just a
            # scheduling courtesy so a long sweep's back-to-back scans
            # don't pin a core against interactive requests, and the gap
            # cancellation checks land in.
            time.sleep(0.02)

    # ------------------------------------------------ search-all as a job

    def _search_all_count_sql(
        self, src: dict, table: str, cols: list[str], query: str, terms: list[dict] | None
    ) -> tuple[str | None, tuple]:
        """The capped `SELECT 1 FROM ... WHERE <match> LIMIT cap+1` a
        search-all count wraps in COUNT(*), for either a term list or a bare
        substring query. Returns (None, ()) when there's nothing to match on.

        Extracted so the per-term breakdown counts a term through exactly
        the same shapes the whole-query count uses — indexed doc-LIKE when
        the source has a usable trigram index, escaped blob LIKE otherwise
        (see _fts_like_pattern). A single positive term is the same question
        as a bare Contains query, which is why the breakdown reaches this
        with `terms=None` and one string rather than a one-element chip
        list."""
        source_id = src["id"]
        if terms:
            if src.get("has_fts"):
                clause, params = _advanced_fts_clause(q("fts_" + str(source_id)), _blob_expr(cols), terms)
            else:
                clause, params = _advanced_like_clause(cols, terms)
            if not clause:
                return None, ()
            return f"SELECT 1 FROM {q(table)} WHERE {clause} LIMIT {SEARCH_ALL_COUNT_CAP + 1}", tuple(params)
        if not query:
            return None, ()
        pattern = _fts_like_pattern(query)
        if src.get("has_fts") and pattern is not None:
            fts_ident = q("fts_" + str(source_id))
            return (
                f"SELECT 1 FROM {q(table)} WHERE rid IN "
                f"(SELECT rowid FROM {fts_ident} WHERE doc LIKE ?) "
                f"LIMIT {SEARCH_ALL_COUNT_CAP + 1}",
                (pattern,),
            )
        blob = _blob_expr(cols)
        return (
            f"SELECT 1 FROM {q(table)} WHERE ({blob}) LIKE ? ESCAPE '\\' "
            f"LIMIT {SEARCH_ALL_COUNT_CAP + 1}",
            (f"%{_esc_like(query)}%",),
        )

    def _search_all_term_counts(
        self, ro: sqlite3.Connection, src: dict, table: str, cols: list[str], terms: list[str]
    ) -> list[dict]:
        """One capped count per term against one source, heaviest first,
        omitting the terms that didn't match it at all — the entry-per-term
        half of a search-all hit.

        Runs on the caller's already-checked-out reader so the whole source
        (union count + breakdown) is one pool checkout, not N+1. Each count
        is capped independently, so a term's own `capped` says "1,000+ of
        *this* term", not "the source was busy" — and because a row can
        match several terms, these deliberately don't have to sum to the
        source's match_count."""
        out = []
        for term in terms:
            inner, params = self._search_all_count_sql(src, table, cols, term, None)
            if inner is None:
                continue
            n = ro.execute(f"SELECT COUNT(*) FROM ({inner})", params).fetchone()[0]
            if n:
                out.append({
                    "term": term,
                    "match_count": min(n, SEARCH_ALL_COUNT_CAP),
                    "capped": n > SEARCH_ALL_COUNT_CAP,
                })
        out.sort(key=lambda d: -d["match_count"])
        return out

    def start_search_all_job(self, query: str = "", terms: list[dict] | None = None,
                             source_ids: list[int] | None = None) -> dict:
        """Runs a search_all sweep on a background daemon thread and returns
        its job record immediately, so the HTTP request that started it
        doesn't sit open for the length of the sweep.

        `source_ids` scopes the sweep (None = every real table). It is
        resolved BEFORE the thread starts, so an unknown id is a KeyError
        the caller's request carries back rather than an error buried in a
        job record nobody is polling yet; the resolved scope — including
        any merge expanded to its members — rides along in the record, so a
        poller can say which tables the results in front of it came from.

        The sweep was already careful not to hold self.lock across the whole
        loop, so other requests were never actually blocked at the *server* —
        but the old single POST still took as long as the sweep did, which
        meant the analyst had one open modal, no results until the very end,
        and nothing to come back to if they closed it. Progress
        (scanned/total) and hits accumulate into the record as each source
        finishes, so a poller sees partial results while it's still running.

        Only one job runs at a time: starting a new one marks any live job
        cancelled (the worker checks between sources) and replaces it. There
        is no queue — a second concurrent sweep would just contend for the
        same lock with the first and make both slower.

        Same fire-and-forget daemon-thread pattern as _ensure_fts_building.
        Cancellation is cooperative and only checked between sources, so a
        cancel during one huge table's count still waits out that count."""
        scope = self.resolve_search_all_scope(source_ids)
        with self._search_job_lock:
            if self._search_job is not None:
                self._search_job["cancelled"] = True
            self._search_job_seq += 1
            job = {
                "job_id": self._search_job_seq,
                "query": query,
                "terms": terms or [],
                "scope": {"requested": list(source_ids) if source_ids is not None else None,
                          "source_ids": scope["source_ids"],
                          "merges": scope["merges"]},
                "scanned": 0,
                "total": 0,
                "hits": [],
                # Tables the sweep could not count ({source_id, name,
                # error}), kept apart from `hits` so that list still means
                # "tables that matched" to everything reading it.
                "failed": [],
                # What the sweep proved, per source it read end to end
                # (_iter_search_all_sources' `read`), for
                # seed_watchlist_from_search_all. Deliberately NOT in the
                # snapshot get_search_all_job answers: the modal polls that
                # every 400 ms and has no use for it, and the handoff reads
                # the record here by job id.
                "read": {},
                "done": False,
                "error": None,
                "cancelled": False,
                "started_at": time.time(),
            }
            self._search_job = job
        t = threading.Thread(target=self._search_all_worker, args=(job,), daemon=True)
        with self._search_job_lock:
            self._search_job_thread = t
        t.start()
        return self.get_search_all_job(job["job_id"])

    def _search_all_worker(self, job: dict) -> None:
        try:
            for scanned, total, hit, read in self._iter_search_all_sources(
                    job["query"], job["terms"], job["scope"]["source_ids"]):
                with self._search_job_lock:
                    if job["cancelled"]:
                        break
                    job["scanned"] = scanned
                    job["total"] = total
                    if hit and hit.get("error"):
                        job["failed"].append(hit)
                    elif hit:
                        job["hits"].append(hit)
                    if read:
                        job["read"][read["source_id"]] = read
        except Exception as e:  # noqa: BLE001 — surfaced to the UI as job.error
            with self._search_job_lock:
                job["error"] = str(e)
        finally:
            with self._search_job_lock:
                job["done"] = True

    def get_search_all_job(self, job_id: int | None = None) -> dict | None:
        """Snapshot of the current (or specifically requested) job. Hits come
        back sorted heaviest-first the same way search_all_sources returns
        them, re-sorted on each poll since the running sweep appends in
        source order. Returns None when there's no such job — a poller whose
        job got superseded should stop rather than keep asking."""
        with self._search_job_lock:
            job = self._search_job
            if job is None or (job_id is not None and job["job_id"] != job_id):
                return None
            return {
                "job_id": job["job_id"],
                # The scope the results in this snapshot came from, not
                # whatever the modal has since been switched to — same rule
                # the frontend's st.terms follows.
                "scope": job["scope"],
                "scanned": job["scanned"],
                "total": job["total"],
                "hits": sorted(job["hits"], key=lambda d: -d["match_count"]),
                "failed": list(job["failed"]),
                "done": job["done"],
                "error": job["error"],
                "cancelled": job["cancelled"],
            }

    def cancel_search_all_job(self, job_id: int | None = None) -> bool:
        with self._search_job_lock:
            job = self._search_job
            if job is None or (job_id is not None and job["job_id"] != job_id):
                return False
            job["cancelled"] = True
            return True

    def wait_for_search_all_job(self, timeout: float | None = None) -> dict | None:
        """Blocks until the running sweep finishes. Used by tests; nothing in
        the request path waits on a job."""
        with self._search_job_lock:
            t = self._search_job_thread
        if t:
            t.join(timeout)
        return self.get_search_all_job()

    # ------------------------------------------------------------- view jobs

    VIEW_JOB_INLINE_WAIT_MS = 250
    # The inline wait parks the calling thread — over HTTP one of the
    # threadpool's shared workers — for its whole length, so a caller's
    # own figure is bounded: the client never sends one, and a stuck
    # retry loop asking for minutes must not park every worker.
    VIEW_JOB_INLINE_WAIT_MAX_MS = 5000

    def start_view_job(self, source_id: int, spec: dict, wait_ms: int | None = None) -> dict:
        """Runs build_view(hold=True) on a daemon thread and returns the
        job's snapshot — after waiting up to `wait_ms` (clamped to
        VIEW_JOB_INLINE_WAIT_MAX_MS) for it, so a search that finishes at
        once comes back with its view inline and costs the client no
        poll. The grid's live view is untouched throughout (held builds
        evict nothing); the client adopts the result when it wants it
        (adopt_view) or leaves it, and a later build evicts it.

        One live job per source: starting another cancels the previous
        one's build through its op_token (cancel_op — effective whether
        the build is running, still queued on the writer lock, or not yet
        registered) and makes its id unpollable. The spec's op_token is
        what the cancel routes act on; one is minted here when the client
        sent none."""
        spec = dict(spec)
        if not spec.get("op_token"):
            spec["op_token"] = f"vj_{time.time_ns():x}"
        job, old = self._view_jobs.start(source_id, {
            "source_id": source_id, "spec": spec, "token": spec["op_token"],
            "view": None, "error_status": None, "elapsed_ms": None,
        })
        if old is not None and old["status"] == "running":
            self.cancel_op(old["token"])
        t = threading.Thread(target=self._view_job_worker, args=(job,), daemon=True)
        job["thread"] = t
        t.start()
        wait = (self.VIEW_JOB_INLINE_WAIT_MS if wait_ms is None
                else min(max(0, wait_ms), self.VIEW_JOB_INLINE_WAIT_MAX_MS))
        job["done_event"].wait(wait / 1000)
        return self._view_job_snapshot(job)

    def _view_job_worker(self, job: dict) -> None:
        t0 = time.time()
        try:
            try:
                view = self._build_for_job(job)
            finally:
                self._forget_build_yields(job["token"])
        except OpCancelled:
            self._view_jobs.finish(job, "cancelled", elapsed_ms=int((time.time() - t0) * 1000))
        except (ValueError, KeyError) as e:
            # The analyst-fixable failures api_view answers 400 with — a bad
            # filter, an unknown column, a source that's gone.
            self._view_jobs.finish(job, "error", error=str(e), error_status=400,
                                   elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001 — surfaced to the UI as job.error
            self._view_jobs.finish(job, "error", error=str(e), error_status=500,
                                   elapsed_ms=int((time.time() - t0) * 1000))
        else:
            # A cancel that arrived while the build was committing — or
            # between the client's read of "running" and its cancel — had
            # no statement to interrupt; the registry recorded its flag
            # instead, and the view the build landed anyway is dropped
            # here rather than left pending for nobody (cancel_view_job).
            if self._view_jobs.finish(job, "done", view=view,
                                      elapsed_ms=int((time.time() - t0) * 1000)) == "cancelled":
                self._drop_pending_view(view["view_id"])

    def _build_for_job(self, job: dict) -> dict:
        """The job's build, started again if it was asked to stand aside.

        The first attempt is the one that may take the lock off a build
        for another table (_yield_the_writer) — the analyst is looking at
        this table, so it goes first. A RESTART never does: it has already
        had its turn, and two builds that each take the lock off the other
        would run forever without finishing either. So a retry simply
        queues, which is what makes this terminate.

        Bounded the other way too, by VIEW_BUILD_YIELD_MAX in the yielding
        half: a search on one table cannot be restarted forever by someone
        flicking between tabs. Past the cap it keeps the lock and
        finishes."""
        attempt = 0
        while True:
            # close() sets this before it cancels and joins; a worker that
            # was queued behind that must not touch the connection at all.
            if self._view_jobs.closing:
                raise OpCancelled("Cancelled")
            try:
                return self.build_view(job["source_id"], job["spec"], hold=True,
                                       yield_others=attempt == 0)
            except OpPreempted:
                # Not the analyst's cancel: the build rolled back whole and
                # nothing on screen depended on it (a held build evicts
                # nothing), so the honest thing is to run it again rather
                # than report a cancellation nobody asked for.
                attempt += 1
                if attempt > self.VIEW_BUILD_YIELD_MAX:
                    raise OpCancelled("Cancelled") from None

    def _forget_build_yields(self, token: str | None) -> None:
        """One entry per view job, dropped when the job settles — the
        registry keeps one live job per source, but tokens are minted per
        build and this dict would otherwise be a slow leak."""
        if not token:
            return
        with self._op_lock:
            self._build_yields.pop(token, None)

    def _view_job_snapshot(self, job: dict) -> dict:
        with self._view_jobs.lock:
            return {k: job[k] for k in ("job_id", "source_id", "status", "view", "error",
                                        "error_status", "elapsed_ms", "started_at")}

    def get_view_job(self, job_id: int) -> dict | None:
        """Snapshot of a live job, or None once a newer job for the same
        source has replaced it (server.py: 404, the poller stops)."""
        job = self._view_jobs.get(job_id)
        return None if job is None else self._view_job_snapshot(job)

    def cancel_view_job(self, job_id: int) -> bool:
        """Ends a job the client no longer wants. Running: cancel_op on its
        token — the build rolls back and, being held, evicted nothing.
        Finished with a view still pending: that view is dropped (the
        client's Discard). Returns whether there was anything to do.

        The running case is decided under the registry lock (request
        discard), not by trusting cancel_op: a build that is between
        statements, committing, or finished since the status was read
        gives cancel_op nothing to interrupt, and the client — told True
        — has already dropped its record. The flag makes the worker
        record such a build cancelled and drop its view (see
        _view_job_worker), so no held table outlives its job unreached.
        A build still queued on the writer lock is covered the old way:
        the marked token fails it fast at registration."""
        job = self._view_jobs.get(job_id)
        if job is None:
            return False
        if self._view_jobs.request_discard(job):
            self.cancel_op(job["token"])
            return True
        view = job.get("view")
        if job["status"] == "done" and view is not None and self._drop_pending_view(view["view_id"]):
            with self._view_jobs.lock:
                job["status"] = "cancelled"
                job["view"] = None
            return True
        return False

    def _drop_pending_view(self, view_id: str) -> bool:
        """Drops a held view that was never adopted — a Discard, or a
        cancel that landed as its build committed. A view already adopted
        (pending cleared) or already evicted is left alone; returns
        whether there was one to drop."""
        with self.lock, self.db:
            handle = self._views.get(view_id)
            if not handle or not handle.get("pending"):
                return False
            self._evict_view_and_children(view_id)
            return True

    def running_view_jobs(self) -> int:
        """How many view builds are running in the background — server.py's
        idle-shutdown hold counts them alongside ingest jobs."""
        return self._view_jobs.running()

    def wait_for_view_job(self, job_id: int, timeout: float | None = None) -> dict | None:
        """Blocks until that job finishes. Tests only."""
        job = self._view_jobs.get(job_id)
        if job is None:
            return None
        job["done_event"].wait(timeout)
        return self._view_job_snapshot(job)

    # -------------------------------------------------------------- sql window

    SQL_PANE_FORBIDDEN_RE = re.compile(r"\b(attach|detach|pragma|vacuum)\b", re.IGNORECASE)

    def merge_select_list(self, merge_src: dict, member_src: dict) -> str:
        """One member's SELECT list in a hand-built UNION ALL over a merge:
        every merge column, in the merge's order, `AS` the merge's name —
        read as `s."col"` (`d."col"` for a derived column) over
        `src_<id> s LEFT JOIN drv_<id> d ON d.rid = s.rid`. The union is
        positional, so a member lacking a column pads it with NULL, and the
        source column is the member's label. Public: the bundled plugins
        build their merge scopes with it."""
        def _cell(c: dict) -> str:
            if c.get("source_column"):
                return f"{_sql_text_literal(member_src.get('nickname') or member_src['name'])} AS {q(c['name'])}"
            actual = _member_column(member_src, c)
            if actual is None:
                return f"NULL AS {q(c['name'])}"
            return f"{self._col_ref(member_src, actual, 's', 'd')} AS {q(c['name'])}"

        return ", ".join(_cell(c) for c in merge_src["columns"])

    def _merge_union_sql(self, conn: sqlite3.Connection, merge_id: int) -> str:
        """SELECT text behind the pane's merge_<id> TEMP VIEW: a UNION ALL
        of the members carrying source_id, rid and every exposed column
        (derived included, each member reading its own sidecar). Every
        table reference is main.-qualified: the pane also creates TEMP
        views SHADOWING src_<id> (see _source_view_sql), and an unqualified
        src_N inside this view would bind to that shadow — double-joining
        the sidecar and making every derived name ambiguous."""
        msrc = self._source_lite_on(conn, -merge_id)
        cols = [c["name"] for c in msrc["columns"]]
        branches = []
        for mid in msrc["member_source_ids"]:
            m = self._source_lite_on(conn, mid)
            has_drv = any(c.get("derived") for c in m["columns"])
            frm = f"main.{q(m['table_name'])} s"
            if has_drv:
                frm += f" LEFT JOIN main.{q(self._derived_table(mid))} d ON d.rid = s.rid"
            sel = self.merge_select_list(msrc, m)
            taken = {c.lower() for c in cols}
            branches.append(
                f"SELECT {int(mid)} AS source_id, s.rid AS rid, {sel}, "
                f"{self._annotation_cols(mid, taken)} FROM {frm}")
        return " UNION ALL ".join(branches)

    @staticmethod
    def _annotation_cols(source_id: int, taken: set[str], alias: str = "s",
                         sid_expr: str | None = None) -> str:
        """The Tags and Note columns every pane view carries: tag NAMES
        comma-joined, and the row's note — correlated subselects, so they
        cost only for the rows a query actually emits (a LIMIT stays
        cheap). Names step aside if the file genuinely has a Tags/Note
        column — the file's own data always wins the plain name."""
        sid = sid_expr if sid_expr is not None else str(int(source_id))
        tags_name = "Tags" if "tags" not in taken else "Winnow Tags"
        note_name = "Note" if "note" not in taken else "Winnow Note"
        return (
            f"(SELECT GROUP_CONCAT(t.name, ', ') FROM row_tags rt "
            f"JOIN tag_defs t ON t.id = rt.tag_id "
            f"WHERE rt.source_id = {sid} AND rt.rid = {alias}.rid) AS {q(tags_name)}, "
            f"(SELECT rn.note FROM row_notes rn "
            f"WHERE rn.source_id = {sid} AND rn.rid = {alias}.rid) AS {q(note_name)}"
        )

    def _source_view_sql(self, conn: sqlite3.Connection, source_id: int) -> str:
        """SELECT text behind the pane's TEMP VIEW shadowing src_<id>: the
        table as the analyst sees it in the grid — rid, base columns,
        derived columns (each sidecar joined on its PRIMARY KEY, so SQLite
        drops the join entirely for queries that never touch one), plus
        Tags and Note. main.src_<id> stays reachable as the byte-faithful
        raw import.

        The trailing `rowid` alias is deliberate: a view has no real rowid
        (SQLite 3.45 returns NULL for it — a join on it silently matches
        nothing; 3.46+ errors), and pre-existing pane queries written
        against the raw table legitimately used rowid, where it equals
        rid. Last, so the grid-shaped columns come first."""
        src = self._source_lite_on(conn, source_id)
        has_drv = any(c.get("derived") for c in src["columns"])
        sel = ", ".join(f"{self._col_ref(src, c['name'], 's', 'd')} AS {q(c['name'])}"
                        for c in src["columns"])
        taken = {c["name"].lower() for c in src["columns"]}
        frm = f"main.{q(src['table_name'])} s"
        if has_drv:
            frm += f" LEFT JOIN main.{q(self._derived_table(source_id))} d ON d.rid = s.rid"
        return (f"SELECT s.rid AS rid, {sel}, "
                f"{self._annotation_cols(source_id, taken)}, s.rid AS rowid FROM {frm}")

    def _pane_connection(self) -> sqlite3.Connection:
        """The SQL pane's private read-only connection: the app functions
        registered (REGEXP/DAY_BUCKET/TS_NORMALIZE — a filter opened in the
        pane can legitimately contain them), plus the pane's TEMP views —
        merge_<id> for every merge (invariant #9: no src_N of their own)
        and a view SHADOWING every src_<id> (temp schema wins resolution),
        so derived columns resolve and Tags/Note are ordinary queryable
        columns. TEMP lives on this connection only: the case file is
        never written, and mode=ro stays honest. Callers own the close."""
        ro = sqlite3.connect(sqlite_ro_uri(self.path), uri=True, check_same_thread=False)
        ro.row_factory = sqlite3.Row
        ro.create_function("REGEXP", 2, _regexp, deterministic=True)
        ro.create_function("DAY_BUCKET", 1, _day_bucket, deterministic=True)
        ro.create_function("TS_NORMALIZE", 1, _ts_normalize, deterministic=True)
        try:
            for row in ro.execute("SELECT id FROM merges ORDER BY id"):
                try:
                    ro.execute(f"CREATE TEMP VIEW {q('merge_' + str(row['id']))} AS "
                               + self._merge_union_sql(ro, row["id"]))
                except (sqlite3.Error, KeyError):
                    continue  # one broken merge shouldn't take the pane down
        except sqlite3.Error:
            pass  # a case predating merges has no merges table to read
        try:
            for row in ro.execute("SELECT id FROM sources ORDER BY id"):
                try:
                    ro.execute(f"CREATE TEMP VIEW {q('src_' + str(row[0]))} AS "
                               + self._source_view_sql(ro, row[0]))
                except (sqlite3.Error, KeyError):
                    continue
        except sqlite3.Error:
            pass
        # Last, so the TEMP views above are still allowed to be created:
        # from here on this connection can only read.
        ro.set_authorizer(read_only_authorizer)
        return ro

    @contextlib.contextmanager
    def _pane_query(self, op_token: str | None = None):
        """A _pane_connection to run one of the analyst's queries on, that
        something can STOP: cancel_op(op_token) — the busy chip's Cancel,
        or server.py noticing the request's client has gone — and
        stop_pane_queries(), which close() and the server's shutdown call.

        The pane runs whatever was typed, and some of it never ends: a
        recursive CTE with no stop condition, a cross join of two 2M-row
        tables. Nothing could stop one. It ran at 100% CPU through a closed
        window, a case switch, Shut down and SIGTERM — the process cannot
        exit while a worker thread is inside sqlite3_step — holding the
        case lock until someone found the process and killed it.

        No deadline (a progress handler could impose one): a GROUP BY over
        a few million rows legitimately takes minutes, and the analyst is
        better placed than a timer to say which query is runaway. Every way
        they can say so is covered above.

        An interrupt from stop_pane_queries surfaces as OpCancelled, as a
        cancel does — a 499 nobody reads, since the case is going away."""
        if self._pane_stopped:   # don't even open the file; re-checked below
            raise OpCancelled("The case is closing")
        ro = self._pane_connection()
        with self._op_lock:
            stopped = self._pane_stopped
            if not stopped:
                self._pane_conns.add(ro)
        if stopped:
            ro.close()
            raise OpCancelled("The case is closing")
        try:
            with self._interruptible(op_token, ro):
                yield ro
        except sqlite3.OperationalError as e:
            # _interruptible has already turned a cancel into OpCancelled;
            # an interrupt it did not ask for came from stop_pane_queries.
            if "interrupt" in str(e).lower() and self._pane_stopped:
                raise OpCancelled("The case is closing") from e
            raise
        finally:
            with self._pane_gone:
                self._pane_conns.discard(ro)
                ro.close()
                self._pane_gone.notify_all()

    def stop_pane_queries(self) -> int:
        """Interrupt every running SQL pane query (_pane_query) and refuse
        new ones from here on: close() and the server's shutdown both mean
        this Store is finished. Returns how many were running. Safe from
        any thread, never waits on a query."""
        with self._op_lock:
            self._pane_stopped = True
            for conn in self._pane_conns:
                conn.interrupt()
            return len(self._pane_conns)

    SQL_TO_TABLE_SOFT_CAP = 500_000

    def sql_to_table(self, sql: str, name: str, force: bool = False, *,
                     op_token: str | None = None) -> dict:
        """Land a pane query's FULL result as a new source — an ordinary
        table (taggable, timeline-able, exportable), via the same ingest
        conventions everything else uses. Past SQL_TO_TABLE_SOFT_CAP rows
        it stops and reports the count for a you-sure confirmation unless
        `force` — a warning, not a wall, per the design discussion."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Name the new table")
        structural = _blank_string_literals(_strip_sql_comments(sql))
        if self.SQL_PANE_FORBIDDEN_RE.search(structural):
            raise ValueError("ATTACH, DETACH, PRAGMA and VACUUM aren't allowed in the SQL pane")
        _check_regexp_literals(sql)
        with self._pane_query(op_token) as ro:
            if not force:
                # Exact count when the query wraps cleanly; a query that
                # defeats the wrap falls back to stream-and-stop below.
                try:
                    n = ro.execute(
                        f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')})").fetchone()[0]
                    if n > self.SQL_TO_TABLE_SOFT_CAP:
                        return {"needs_confirm": True, "rows": n}
                except sqlite3.Error as e:
                    # Stopped, not a query that defeats the wrap: falling
                    # through would start the same runaway again.
                    if "interrupt" in str(e).lower():
                        raise
            cur = ro.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            if not cols:
                raise ValueError("That statement returns no rows to save")
            rows: list[list] = []
            while True:
                chunk = cur.fetchmany(50_000)
                if not chunk:
                    break
                rows.extend([[pane_cell_text(v) for v in r] for r in chunk])
                if not force and len(rows) > self.SQL_TO_TABLE_SOFT_CAP:
                    return {"needs_confirm": True, "rows": None}  # count unknowable cheaply
        # Provenance: the query that produced it, so the table reads as
        # derived (the SQL pane's own "Saved from…" badge) rather than as a
        # file nobody can find on disk.
        rec = self.ingest_rows(cols, rows, name=name, origin=SQL_ORIGIN, origin_meta={"sql": sql})
        return {"source": {"id": rec["id"], "name": rec["name"], "row_count": rec["row_count"]}}

    # ------------------------------------------- saving a view as a table

    def save_view_as_source(self, view_id: str, name: str, *, keys: list | None = None,
                            exclude: list | None = None, spec: dict | None = None,
                            copy_tags: bool = False, force: bool = False,
                            op_token: str | None = None, build_fts: bool = True) -> dict:
        """Copy the rows a view shows — or an explicit pick of them — into a
        new, ordinary source of this case, badged as a subset of the view's
        table (sources.origin = 'subset'; origin_meta names the parent, the
        spec that produced the view and the selection shape). The move it
        serves: 2M rows filtered down to the 300 that matter, wanted as a
        table of their own — to tag and note separately, to hand over via
        copy_sources_to, to group or join in the SQL pane. Nothing about it
        is temporary: it is deleted like any table and nothing auto-deletes
        it.

        Rows land in VIEW order with rid contiguous from 1 (invariant #2's
        root_virtual carve-out needs that of every source), as plain TEXT
        columns: the parent's base columns AND its derived columns, the
        derived ones copied as VALUES rather than re-derived — instant, no
        backfill to wait on. Column types come from the parent's metadata,
        not a fresh sample: a pick whose numeric column happens to be blank
        would otherwise re-type it as text. rid/source_id/Tags/Note are
        never data columns here (that is the SQL pane's SELECT * leak,
        which this route exists to avoid).

        The copy streams. A pooled reader walks the view's keys BATCH at a
        time (the same four kinds fetch_rows pages — root: v.view_N by
        pos; root_virtual: the source by rid range; group: its own
        v.view_N; group_virtual: the member under _virtual_group_where),
        _subset_cells pulls each batch's cells through ITS member's
        _from_clause (the fetch_rows shape — which is what makes a subset
        of a MERGE resolve every row through its own member with the
        merge's canonical columns, invariant #9), and _commit_ingest_batch
        commits under the writer lock per batch. Never a Python list of
        the whole result (sql_to_table's rows.extend is gigabytes on a
        2M x 27 view) and never the lock for the whole loop (invariant #4;
        _ingest_synchronous_off is deliberately NOT taken — a subset is
        usually small, and the relaxed-fsync window would cover every
        concurrent tag write). subset_rids records each new rid's
        (parent_source_id, parent_rid) in the SAME transaction as the rows
        it describes, so the map can never outrun or lag the table.

        `keys` is an explicit [[source_id, rid], ...] pick — capped at
        SELECTION_REMAP_MAX like a selection remap; rows the view doesn't
        hold are skipped, the rest land in view order. `exclude` is the
        select-all-minus-a-few shape tag_view takes. Past
        SQL_TO_TABLE_SOFT_CAP rows the call returns {needs_confirm, rows}
        unless `force` — sql_to_table's contract.

        Any failure mid-copy — a cancel through `op_token`, the view
        evicted under the reader (KeyError → 409, every view read's
        contract), an error — DROPS the partial source: a half-table looks
        exactly like a whole one in every list (the cancel-drops-partial
        rule in docs/notes/ingest.md).

        Afterwards the parent's layout is copied so the subset opens
        looking the same (its sort included, so it opens materialised
        rather than root_virtual — fine). The subset starts UNTAGGED:
        `copy_tags` defaults off, and the route and the UI leave it off.
        build_timeline and export_tagged_xlsx walk every source with a
        tagged row — a subset included, on purpose, since a tag put on a
        subset is real work — so seeding the parent's tags would list each
        finding twice in the Timeline and give it a second worksheet in
        the hand-over workbook. With `copy_tags=True` (a script that wants
        a tagged copy) the parent rows' tags and notes are seeded onto the
        new rows by one INSERT..SELECT over subset_rids: a documented
        exception to invariant #7 (every tag write through
        _apply_tag_change) — a direct row_tags write, the
        _copy_sources_into precedent, a brand-new table nobody could have
        tagged yet — and deliberately NOT an undo entry (undoing "create
        table" is deleting it). Either way, tags applied to the subset
        later do not write back to the parent; the map is stored so that
        can come.

        Refused up front (ValueError → 400) while any derived column the
        copy would take is not 'ready': the values are copied as they
        stand, so a backfill mid-way would land '' for every row it had
        not reached, in a plain column nothing can re-derive — the rule
        add_derived_column applies to its own inputs. On a merge that
        means every member's copy of a canonical derived column.

        The copy sits in _subset_copies from the shell to the metadata
        write that makes the table openable, so close() (a case switch, a
        shutdown) flags it cancelled and waits for it: the flag is read
        once per batch, surfaces as OpCancelled, and the partial source is
        dropped while the writer is still open rather than stranded — the
        cancel-and-drain contract close() gives ingest jobs, minus their
        thread and their panel entry. The shell and its registration
        happen under one hold of self.lock against close()'s flag-and-
        snapshot step, so a copy that loses that race drops its shell and
        gives up rather than running on into a closed writer.

        file_hash gets a synthetic 'subset:<sha256>': a subset has no file
        on disk, and a NULL hash is exactly what makes import_case_session
        skip a source, so a saved session would never give a subset its
        tags back (SQL-saved and plugin-made tables get 'made:<random>' in
        ingest_rows for the same reason; see MADE_HASH_PREFIX). The prefix
        keeps it from ever reading as a _quick_hash of something on disk."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Name the new table")
        handle = self._views.get(view_id)
        if not handle:
            raise ViewExpired("View expired — rebuild it")
        parent_id = handle["source_id"]
        parent = self._source_lite(parent_id)
        self._require_columns(parent)
        cols = [c["name"] for c in parent["columns"]]
        types = [c.get("type") if c.get("type") in ("text", "number", "datetime") else "text"
                 for c in parent["columns"]]
        # A derived column mid-backfill copies as blanks nothing can
        # re-derive — refuse until it is ready. A merge exposes a derived
        # column only when every member has it, and each member's status is
        # its own, so every member's copy is checked (invariant #9).
        derived = {c["name"].lower() for c in parent["columns"] if c.get("derived")}
        if derived:
            members = [parent] if parent_id >= 0 else [self._source_lite(m) for m in parent["member_source_ids"]]
            for m in members:
                for c in m["columns"]:
                    if c.get("derived") and c["name"].lower() in derived and c.get("derived_status") != "ready":
                        raise ValueError(f"{c['name']!r} is still building — wait for it to finish, "
                                         "then save the view as a table")

        skip: set[tuple[int, int]] = set()
        for pair in exclude or []:
            try:
                skip.add((int(pair[0]), int(pair[1])))
            except (TypeError, ValueError, IndexError):
                continue
        picks: list[tuple[int, int]] | None = None
        if keys is not None:
            picks = []
            seen: set[tuple[int, int]] = set()
            for k in keys:
                try:
                    pair = (int(k[0]), int(k[1]))
                except (TypeError, ValueError, IndexError):
                    continue
                if pair in skip or pair in seen:
                    continue
                seen.add(pair)
                picks.append(pair)
            if len(picks) > self.SELECTION_REMAP_MAX:
                raise ValueError(
                    f"At most {self.SELECTION_REMAP_MAX:,} picked rows can be saved at once — "
                    "filter the view down to them and save the view instead")
            expected = len(picks)
        else:
            expected = max(0, int(handle["row_count"]) - len(skip))
        if not force and expected > self.SQL_TO_TABLE_SOFT_CAP:
            return {"needs_confirm": True, "rows": expected}

        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta = {
            "parent_source_id": parent_id,
            "parent_name": parent["name"],
            "parent_row_count": parent["row_count"],
            "spec": spec,
            "selection": "picks" if picks is not None else "view",
            "excluded": len(skip),
            "rows": 0,
            "created_at": created_at,
        }
        copy = {"source_id": None, "name": name, "cancelled": False, "done": threading.Event()}
        # One hold of the (re-entrant) writer lock across the shell and its
        # registration: close() sets _closing and snapshots the registry in
        # a single locked step, so either this copy is in that snapshot and
        # gets waited for, or it sees _closing here and backs out with the
        # shell dropped — never a registered-too-late copy running its
        # batches into a closed writer.
        with self.lock:
            new_id, table = self._create_source_shell(name, cols, origin=SUBSET_ORIGIN, origin_meta=meta)
            copy["source_id"] = new_id
            with self._ingest_jobs_lock:
                closing = self._closing
                if not closing:
                    self._subset_copies[new_id] = copy
            if closing:
                self.drop_source(new_id)
                raise OpCancelled("The case is closing — nothing was saved")
        insert = (f"INSERT INTO {q(table)} ({','.join(q(c) for c in cols)}) "
                  f"VALUES ({','.join('?' * len(cols))})")
        map_sql = "INSERT INTO subset_rids(source_id, rid, parent_source_id, parent_rid) VALUES (?,?,?,?)"
        total = 0
        try:
            try:
                with self._reader() as ro, self._dropped_view_is_expired():
                    for chunk in self._iter_subset_keys(ro, view_id, handle, picks, skip, op_token):
                        if copy["cancelled"]:   # close() — read once per batch, ingest_csv's cadence
                            raise OpCancelled("The case is closing — the copy was cancelled")
                        cells = self._subset_cells(ro, cols, chunk, op_token, parent)
                        rows: list[tuple] = []
                        mapping: list[tuple] = []
                        for pair in chunk:
                            vals = cells.get(pair)
                            if vals is None:
                                continue   # not in its source table — never invent a row for it
                            rows.append(vals)
                            # rid is assigned 1..N in insertion order on the one
                            # writer (invariant #2), so the new rid is known
                            # before the INSERT runs — the map rides the same
                            # transaction as the rows it describes.
                            mapping.append((new_id, total + len(rows), pair[0], pair[1]))
                        if rows:
                            total = self._commit_ingest_batch(insert, rows, new_id, total,
                                                              also=(map_sql, mapping), op_token=op_token)

                # The metadata write is what makes the table openable
                # (_require_columns reads columns='[]' as "still importing"),
                # so it stays inside the drop-on-failure frame and the
                # registry: a close() landing between the last batch and
                # this write still waits for it.
                meta["rows"] = total
                colmeta = [{"name": c, "type": t} for c, t in zip(cols, types)]
                digest = hashlib.sha256(
                    f"{parent.get('file_hash') or parent['name']}|{json.dumps(spec, sort_keys=True, default=str)}"
                    f"|{created_at}|{new_id}".encode("utf-8")).hexdigest()
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE sources SET columns=?, origin_meta=?, file_hash=? WHERE id=?",
                        (json.dumps(colmeta), json.dumps(meta), SUBSET_HASH_PREFIX + digest, new_id))
                    if copy_tags:
                        self.db.execute(
                            "INSERT OR IGNORE INTO row_tags(source_id, rid, tag_id) "
                            "SELECT m.source_id, m.rid, rt.tag_id FROM subset_rids m "
                            "JOIN row_tags rt ON rt.source_id = m.parent_source_id AND rt.rid = m.parent_rid "
                            "WHERE m.source_id=?", (new_id,))
                        self.db.execute(
                            "INSERT OR IGNORE INTO row_notes(source_id, rid, note) "
                            "SELECT m.source_id, m.rid, rn.note FROM subset_rids m "
                            "JOIN row_notes rn ON rn.source_id = m.parent_source_id AND rn.rid = m.parent_rid "
                            "WHERE m.source_id=?", (new_id,))
            except BaseException:
                with contextlib.suppress(Exception):
                    self.drop_source(new_id)
                raise
        finally:
            with self._ingest_jobs_lock:
                self._subset_copies.pop(new_id, None)
            copy["done"].set()
        layout = self.get_layout(parent_id)
        if layout:
            self.save_layout(new_id, layout)
        if total:
            self._build_import_indexes(new_id)
        if build_fts and total:
            self._ensure_fts_building(new_id)
        return {"source": {"id": new_id, "name": name, "row_count": total}}

    def _iter_subset_keys(self, ro: sqlite3.Connection, view_id: str, handle: dict,
                          picks: list[tuple[int, int]] | None, skip: set,
                          op_token: str | None) -> Iterator[list[tuple[int, int]]]:
        """(source_id, rid) pairs of a view in its pos order, BATCH at a
        time, on the caller's reader — one walk per view kind, the same
        four fetch_rows pages. Keyset-paged (`> last`), never LIMIT/OFFSET
        (invariant #2's reasoning applies to a full walk too). `picks`
        narrows to those rows, view order kept and absent ones skipped;
        `skip` drops select-all exclusions. Each statement runs inside
        _interruptible, innermost, so a cancel surfaces as OpCancelled
        before the caller's _dropped_view_is_expired can misread it."""
        kind = handle.get("kind", "root")
        sid = handle["source_id"]
        if picks is not None:
            by_source: dict[int, list[int]] = {}
            for s, r in picks:
                by_source.setdefault(s, []).append(r)
            ordered: list[tuple[int, int, int]] = []   # (order key, source_id, rid)
            with self._interruptible(op_token, ro):
                if kind == "group_virtual":
                    # One member, rows in rid order: keep the picks the
                    # group's own predicate admits.
                    msid = handle["members"][0]["source_id"]
                    rids = by_source.get(msid, [])
                    if rids:
                        msrc = self._source_lite_on(ro, msid)
                        where_sql, where_params = self._virtual_group_where(handle, ro)
                        for i in range(0, len(rids), 500):
                            chunk = rids[i:i + 500]
                            for r in ro.execute(
                                    f"SELECT rid FROM {self._from_clause(msrc)} WHERE {where_sql} "
                                    f"AND rid IN ({','.join('?' * len(chunk))})", [*where_params, *chunk]):
                                ordered.append((r["rid"], msid, r["rid"]))
                elif kind == "root_virtual":
                    rids = by_source.get(sid, [])
                    table = q(self._source_lite_on(ro, sid)["table_name"])
                    for i in range(0, len(rids), 500):
                        chunk = rids[i:i + 500]
                        for r in ro.execute(
                                f"SELECT rid FROM {table} WHERE rid IN ({','.join('?' * len(chunk))})", chunk):
                            ordered.append((r["rid"], sid, r["rid"]))   # pos = rid - 1, exact
                else:
                    for s, rids in by_source.items():
                        for i in range(0, len(rids), 500):
                            chunk = rids[i:i + 500]
                            for r in ro.execute(
                                    f"SELECT pos, rid FROM v.{q(view_id)} WHERE source_id=? "
                                    f"AND rid IN ({','.join('?' * len(chunk))})", [s, *chunk]):
                                ordered.append((r["pos"], s, r["rid"]))
            ordered.sort()
            for i in range(0, len(ordered), BATCH):
                yield [(s, r) for _, s, r in ordered[i:i + BATCH]]
            return

        if kind == "root_virtual":
            table = q(self._source_lite_on(ro, sid)["table_name"])
            last = 0
            while True:
                with self._interruptible(op_token, ro):
                    rows = ro.execute(f"SELECT rid FROM {table} WHERE rid > ? ORDER BY rid LIMIT ?",
                                      (last, BATCH)).fetchall()
                if not rows:
                    return
                last = rows[-1]["rid"]
                yield [(sid, r["rid"]) for r in rows if (sid, r["rid"]) not in skip]
        elif kind == "group_virtual":
            msid = handle["members"][0]["source_id"]
            msrc = self._source_lite_on(ro, msid)
            where_sql, where_params = self._virtual_group_where(handle, ro)
            last = 0
            while True:
                with self._interruptible(op_token, ro):
                    rows = ro.execute(
                        f"SELECT rid FROM {self._from_clause(msrc)} WHERE {where_sql} AND rid > ? "
                        f"ORDER BY rid LIMIT ?", [*where_params, last, BATCH]).fetchall()
                if not rows:
                    return
                last = rows[-1]["rid"]
                yield [(msid, r["rid"]) for r in rows if (msid, r["rid"]) not in skip]
        else:
            last = 0
            while True:
                with self._interruptible(op_token, ro):
                    rows = ro.execute(
                        f"SELECT pos, source_id, rid FROM v.{q(view_id)} WHERE pos > ? ORDER BY pos LIMIT ?",
                        (last, BATCH)).fetchall()
                if not rows:
                    return
                last = rows[-1]["pos"]
                yield [(r["source_id"], r["rid"]) for r in rows if (r["source_id"], r["rid"]) not in skip]

    def _subset_cells(self, ro: sqlite3.Connection, cols: list[str], chunk: list[tuple[int, int]],
                      op_token: str | None, parent: dict | None = None) -> dict[tuple[int, int], tuple]:
        """{(source_id, rid): cells} for one batch of keys, each row read
        through ITS member's _from_clause — derived sidecar joined
        USING(rid), so base and derived columns are both addressable bare
        — the fetch_rows shape. A member of a merge `parent` that doesn't
        carry the merge's columns as-is is read through its projection
        (_member_rows_from), which pads and renames it to that column
        list. NULL → '' as ingest_rows stores it."""
        by_source: dict[int, list[int]] = {}
        for s, r in chunk:
            by_source.setdefault(s, []).append(r)
        sel = ", ".join(q(c) for c in cols)
        out: dict[tuple[int, int], tuple] = {}
        with self._interruptible(op_token, ro):
            for s, rids in by_source.items():
                msrc = self._source_lite_on(ro, s)
                frm = (self._member_rows_from(parent, msrc) if parent and parent.get("is_merge")
                       else self._from_clause(msrc))
                for i in range(0, len(rids), 900):
                    part = rids[i:i + 900]
                    for row in ro.execute(
                            f"SELECT rid, {sel} FROM {frm} WHERE rid IN ({','.join('?' * len(part))})", part):
                        t = tuple(row)
                        out[(s, t[0])] = tuple("" if v is None else v if isinstance(v, str) else str(v)
                                               for v in t[1:])
        return out

    def run_sql(self, sql: str, limit: int = 5000, *, op_token: str | None = None) -> dict:
        """Read-only ad-hoc query against the case file, its rows inline:
        the shape plugins (winnow.api.sql) and dashboards draw from, each
        passing a limit sized to what it draws. The SQL pane's Run does not
        come through here — run_sql_result holds its rows server-side,
        because the pane has no cap and an uncapped answer in one response
        is gigabytes.

        This intentionally allows arbitrary SELECT/EXPLAIN — that's the
        point of the pane — unlike validate_where_fragment's tight allowlist
        for filter fragments. But ATTACH/DETACH/PRAGMA/VACUUM serve no
        purpose for a read-only query pane and are worth blocking anyway as
        defense-in-depth: the `mode=ro` connection only restricts writes to
        *this* database file, and ATTACH opens a second, independent
        connection to whatever path it's given (modern SQLite inherits the
        read-only restriction onto attached databases too, but that's a
        SQLite-version-dependent guarantee this code shouldn't have to lean
        on). Stacked statements aren't a separate concern here: Python's
        sqlite3 already refuses to execute more than one statement per call.

        Runs on a _pane_query connection, so a query that never ends can
        still be stopped — `op_token` through cancel_op, every query by
        stop_pane_queries (close(), and the server's shutdown).
        """
        structural = _blank_string_literals(_strip_sql_comments(sql))
        if self.SQL_PANE_FORBIDDEN_RE.search(structural):
            raise ValueError("ATTACH, DETACH, PRAGMA and VACUUM aren't allowed in the SQL pane")
        _check_regexp_literals(sql)
        with self._pane_query(op_token) as ro:
            t0 = time.time()
            cur = ro.execute(sql)
            rows = cur.fetchmany(limit + 1)   # one past the limit: exactly `limit` rows is not truncated
            cols = [d[0] for d in cur.description] if cur.description else []
            return {
                "columns": cols,
                "rows": [[r[i] for i in range(len(cols))] for r in rows[:limit]],
                "truncated": len(rows) > limit,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

    def run_sql_result(self, sql: str, op_token: str | None = None, release=()) -> dict:
        """The SQL pane's Run: every row of the answer, held server-side.

        The query runs on a _pane_query connection exactly as run_sql's
        does (same forbidden-statement check, same authorizer, stopped by
        the same things); its cursor is handed to SqlResults.materialise,
        which copies the rows a batch at a time into the scratch database
        and returns the result's id, count and column widths. The response
        carries the first SqlResults.PREVIEW_ROWS rows; the pane pages the
        rest by id, sorts through it, and exports from it — see
        winnow/sqlresults.py for why nothing holds the whole answer in
        memory any more.

        `op_token` is the pane's Stop (cancel_op): stopped mid-fetch, the
        rows so far are kept and `stopped` says "cancelled"; stopped
        before the first row, it is OpCancelled (499). stop_pane_queries
        (close(), shutdown) ends it the same way. `release` names the
        result(s) this run replaces — the tab's last one — so a re-run
        frees its scratch space before taking more."""
        structural = _blank_string_literals(_strip_sql_comments(sql))
        if self.SQL_PANE_FORBIDDEN_RE.search(structural):
            raise ValueError("ATTACH, DETACH, PRAGMA and VACUUM aren't allowed in the SQL pane")
        _check_regexp_literals(sql)
        if release:
            self.sql_results.release(release)
        token = op_token or f"sqlr_{time.time_ns():x}"
        with self._sql_run(token) as run:
            with self._pane_query(token) as ro:
                t0 = time.time()
                cur = ro.cursor()
                cur.row_factory = None
                cur.execute(sql)
                meta = self.sql_results.materialise(
                    cur, cancelled=lambda: self._op_was_cancelled(token),
                    progress=lambda n: run.__setitem__("rows", n))
                elapsed = int((time.time() - t0) * 1000)
            out = self.sql_results.summary(meta["id"])
        out["elapsed_ms"] = elapsed
        return out

    @contextlib.contextmanager
    def _sql_run(self, token: str) -> Iterator[dict]:
        """Registers a pane run as in flight for the block — what
        sql_run_progress reads, and what close() waits on before it
        deletes the results file. Refused once close() has begun."""
        run = {"rows": 0, "done": threading.Event()}
        with self._ingest_jobs_lock:
            if self._closing:
                raise OpCancelled("The case is closing")
            self._sql_runs[token] = run
        try:
            yield run
        finally:
            with self._ingest_jobs_lock:
                self._sql_runs.pop(token, None)
            run["done"].set()

    def _op_was_cancelled(self, token: str) -> bool:
        with self._op_lock:
            return token in self._op_cancelled

    def sql_run_progress(self, token: str) -> dict:
        """Rows landed so far by the run under `token` — the pane's
        "Running… N rows" while it waits."""
        with self._ingest_jobs_lock:
            run = self._sql_runs.get(token)
            return {"running": run is not None, "rows": run["rows"] if run else 0}

    def sql_result_csv(self, result_id: str, sort: int | None = None, order: str = "asc") -> Iterator[str]:
        """A held result as CSV text chunks, in displayed order, streamed
        off the scratch table. Values as the pane shows them (js_str);
        text — the column names included, which are just as much the
        query's output — through _csv_safe like every other export of case
        data. A number stays a number: -5 is not a formula, and an
        apostrophe in front of it would make it text in the spreadsheet."""
        cols, rows = self.sql_results.iter_rows(result_id, sort, order)

        def cell(v):
            text = _sql_js_str(v)
            return text if isinstance(v, (int, float)) else _csv_safe(text)

        def gen():
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow([_csv_safe(c) for c in cols])
            n = 0
            for row in rows:
                w.writerow([cell(v) for v in row])
                n += 1
                if n % 2000 == 0:
                    yield buf.getvalue()
                    buf.seek(0), buf.truncate(0)
            yield buf.getvalue()
        return gen()

    def sql_result_xlsx(self, result_id: str, path: str, sort: int | None = None, order: str = "asc",
                        sheet: str = "Query") -> dict:
        """A held result as a workbook written to `path`, in displayed
        order, continuing past one worksheet the way rows_to_xlsx does.
        Numbers stay numbers; text goes through _xlsx_safe."""
        cols, rows = self.sql_results.iter_rows(result_id, sort, order)
        return write_rows_xlsx(path, cols, rows, sheet)

_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(v):
    """OWASP CSV/formula-injection mitigation: a cell that opens with
    =, +, -, @, tab or CR can execute as a formula when the export is later
    opened in Excel/Sheets. Forensic values (command lines, filenames,
    subject lines, notes) are attacker- or user-influenced text by
    definition, and this export is explicitly meant to be handed to other
    analysts/reports — exactly the CSV-injection threat model. Only the
    exported *copy* is affected; the stored case-file value is never
    touched, so evidence fidelity in the case itself is unchanged."""
    if isinstance(v, str) and v and v[0] in _CSV_FORMULA_LEAD:
        return "'" + v
    return v


# The C0 control characters XML 1.0 cannot encode. openpyxl refuses
# them — and refuses the whole workbook, with an IllegalCharacterError
# naming the value but not the row it came from. Tab, newline and
# carriage return are legal XML and stay as they are.
_XLSX_ILLEGAL = re.compile(r"[\000-\010\013\014\016-\037]")


# Excel's per-cell text limit. openpyxl enforces it by cutting the string
# at 32,767 characters and saying nothing.
XLSX_CELL_MAX = 32_767


def _xlsx_safe(v):
    """_csv_safe, plus the characters a worksheet cannot hold at all, and
    a visible mark on a value too long for one cell.

    Winnow keeps control characters in cell VALUES on purpose — ingest
    strips them from column names only, because a name has to be
    quotable while "cells are data and stay as-is". That fidelity is
    right for the case file and fatal for a worksheet: one BEL inside
    one command line raised IllegalCharacterError and took the entire
    export down, which an analyst reads as the feature being broken.

    Escaped rather than dropped. These turn up in exactly the fields
    worth reading closely — command lines, paths, payloads — and a
    silently shortened string in an exported artifact is worse than a
    visible \\x07: the escape says what was there and is reversible,
    while a strip quietly rewrites evidence.

    Length is the same argument. Past XLSX_CELL_MAX openpyxl truncates
    without a word, and an encoded PowerShell payload or a long script
    block is exactly the value that runs past it — the reader of the
    workbook then has a cut-off command line that looks complete. Cut
    here instead, with a marker that says it was cut and how long the
    value is; the case file still holds all of it.
    """
    full = len(v) if isinstance(v, str) else 0
    if isinstance(v, str) and _XLSX_ILLEGAL.search(v):
        v = _XLSX_ILLEGAL.sub(lambda m: "\\x%02x" % ord(m.group()), v)
    v = _csv_safe(v)
    if isinstance(v, str) and len(v) > XLSX_CELL_MAX:
        mark = f" …[cut: the value is {full:,} characters, a cell holds {XLSX_CELL_MAX:,} — all of it is in the case]"
        v = v[: XLSX_CELL_MAX - len(mark)] + mark
    return v


def _xlsx_write_sheets(wb, used_names: set[str], name: str, header: list,
                       rows: Iterable[list]) -> tuple[int, int]:
    """Write one table's `rows` to a write-only workbook under its own
    sheet name, continuing on "Name (2)", "Name (3)"… with the header
    re-emitted once a sheet holds XLSX_MAX_ROWS - 1 rows, so every row
    lands. Returns (sheets written, rows written). Both workbook exports
    use it — the tagged-rows one had no continuation and wrote a sheet
    Excel refuses to open.

    Continuations derive from the name the FIRST sheet actually got — two
    tables both called Security.evtx.csv become "Security.evtx.csv" and
    "Security.evtx.csv_1", and the second one's overflow must read
    "…_1 (2)", not "… (2)", which would look like the first table
    continuing."""
    per = XLSX_MAX_ROWS - 1   # one row is the header
    first = _xlsx_sheet_name(name, used_names)
    part = 1
    ws = wb.create_sheet(first)
    ws.append(header)
    sheets, written, total = 1, 0, 0
    for row in rows:
        if written == per:
            part += 1
            suffix = f" ({part})"
            ws = wb.create_sheet(_xlsx_sheet_name(first[: 31 - len(suffix)] + suffix, used_names))
            ws.append(header)
            sheets += 1
            written = 0
        ws.append(row)
        written += 1
        total += 1
    return sheets, total


def _export_header(cols: list[str], safe) -> list:
    """An export's header row through the same formula guard as its cells
    (`safe` is _csv_safe or _xlsx_safe). Column names are header text out
    of the evidence file — attacker-influenced exactly as the cells are —
    and openpyxl writes any string opening with '=' as a formula, so a
    column called =HYPERLINK(...) was a live link in the hand-over
    workbook's first row while every cell under it was neutralised."""
    return ["Line", "Tags", "Note", *[safe(c) for c in cols]]


def _esc_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# What a worksheet NAME cannot hold: Excel's own \ / ? * [ ] : plus every C0
# control character and DEL — a name is XML text in workbook.xml, and
# openpyxl writes a BEL there without complaint, producing a workbook
# that neither Excel nor openpyxl will open again.
_XLSX_SHEET_INVALID = re.compile(r"[\\/?*\[\]:\x00-\x1f\x7f]")


# Excel's hard per-worksheet row limit (1,048,576 including the header).
# Module-level so a test can shrink it to prove the continuation-sheet
# path without writing a million rows; read at call time for that reason.
XLSX_MAX_ROWS = 1_048_576


def rows_to_xlsx(columns: list, rows: Iterable, sheet_name: str = "Query") -> io.BytesIO:
    """A result set as a workbook, in memory — write_rows_xlsx into a
    BytesIO. A module function rather than a Store method because it
    touches no database, which is also what lets its tests run without a
    case file."""
    buf = io.BytesIO()
    write_rows_xlsx(buf, columns, rows, sheet_name)
    buf.seek(0)
    return buf


def write_rows_xlsx(target, columns: list, rows: Iterable, sheet_name: str = "Query") -> dict:
    """Rows (any iterable — the SQL pane's export streams them off its
    scratch table) as a workbook saved to `target`, a path or a file
    object; returns {"rows", "sheets"}.

    The pane has no row cap, so a result can be past Excel's per-sheet
    limit: it continues on "Query (2)", "Query (3)"…, header re-emitted,
    the way export_all_xlsx does — every row lands rather than the export
    being refused. Write-only for the same reason that one is: a million
    rows as openpyxl cell objects is gigabytes.
    """
    per = XLSX_MAX_ROWS - 1   # one row is the header
    header = [_xlsx_safe(c) for c in columns]
    used: set[str] = set()
    first = _xlsx_sheet_name(sheet_name, used)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(first)
    ws.append(header)
    sheets, on_sheet, total = 1, 0, 0
    for row in rows:
        if on_sheet == per:
            sheets += 1
            suffix = f" ({sheets})"
            ws = wb.create_sheet(_xlsx_sheet_name(first[: 31 - len(suffix)] + suffix, used))
            ws.append(header)
            on_sheet = 0
        ws.append([_xlsx_safe(pane_cell_json(v)) for v in row])
        on_sheet += 1
        total += 1
    wb.save(target)
    return {"rows": total, "sheets": sheets}


def _xlsx_sheet_name(name: str, used: set[str]) -> str:
    """Excel worksheet names: no \\/?*[]: (nor control characters), max 31
    chars, can't be blank or start/end with an apostrophe, can't be
    "History" (Excel reserves it), and must be unique within the workbook.
    Dedup mirrors sanitize_columns' approach (store.py:137) — append a
    numeric suffix, truncating the base to make room for it. The
    apostrophe rule is applied AFTER the cut to 31: "AAAA…A's log" cut
    there ends in the apostrophe the full name had in its middle."""
    clean = _XLSX_SHEET_INVALID.sub("_", (name or "Sheet").strip()).strip("'") or "Sheet"
    clean = clean[:31].rstrip("'")
    base = clean
    n = 1
    while clean.lower() in used or clean.lower() == "history":
        suffix = f"_{n}"
        clean = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


# The only SQLite actions a read-only query needs. Everything else — the
# CREATE_TABLE/INSERT/ATTACH that `VACUUM INTO` performs, a bare ATTACH,
# a PRAGMA, any write — is refused by the authorizer below.
#
# This exists because the keyword blacklist it backs up was bypassable:
# `_blank_string_literals` has no notion of comments, so `/* ' */ VACUUM
# INTO 'x'` shifted quote parity and blanked the keyword out of the string
# the blacklist scanned. Text scanning is the friendly error; the
# authorizer is the guarantee, and it cannot be fooled by spelling.
_READ_ONLY_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE,
})


def read_only_authorizer(action, _arg1, _arg2, _db, _trigger):
    return sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY


def _strip_sql_comments(frag: str) -> str:
    """Blank `--` and `/* */` comments, preserving length. Runs before
    literal blanking: a quote inside a comment is not a quote, and letting
    it shift parity is what made the keyword scan bypassable."""
    out: list[str] = []
    i, n = 0, len(frag)
    while i < n:
        two = frag[i:i + 2]
        if two == "--":
            while i < n and frag[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            depth_end = frag.find("*/", i + 2)
            stop = n if depth_end < 0 else depth_end + 2
            out.append(" " * (stop - i))
            i = stop
        elif frag[i] in ("'", '"'):
            # A literal: copy it verbatim here so a comment marker inside a
            # string is not mistaken for a comment. Blanking of the literal
            # itself is _blank_string_literals' job, which runs next.
            ch = frag[i]
            out.append(ch)
            i += 1
            while i < n:
                out.append(frag[i])
                if frag[i] == ch:
                    if i + 1 < n and frag[i + 1] == ch:
                        out.append(frag[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(frag[i])
            i += 1
    return "".join(out)


def _double_quoted_names(frag: str) -> list[str]:
    """The double-quoted spans in `frag`, unescaped ("" -> "), skipping
    single-quoted literals (a '"' inside one is text, not a quote) — the
    same walk _blank_string_literals does, keeping what it blanks."""
    out: list[str] = []
    i, n = 0, len(frag)
    while i < n:
        ch = frag[i]
        if ch not in ("'", '"'):
            i += 1
            continue
        j, buf = i + 1, []
        while j < n:
            if frag[j] == ch:
                if j + 1 < n and frag[j + 1] == ch:
                    buf.append(ch)
                    j += 2
                    continue
                break
            buf.append(frag[j])
            j += 1
        if ch == '"':
            out.append("".join(buf))
        i = j + 1
    return out


def _blank_string_literals(frag: str) -> str:
    """Replace the contents of single/double-quoted literals with spaces,
    preserving length and quote delimiters, so keyword/identifier scanning
    over the result only sees SQL structure, never literal payload text."""
    out: list[str] = []
    i, n = 0, len(frag)
    while i < n:
        ch = frag[i]
        if ch in ("'", '"'):
            out.append(ch)
            i += 1
            while i < n:
                if frag[i] == ch:
                    if i + 1 < n and frag[i + 1] == ch:  # escaped '' or "" inside the literal
                        out.append("  ")
                        i += 2
                        continue
                    out.append(ch)
                    i += 1
                    break
                out.append(" ")
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _advanced_fts_clause(fts_ident: str, blob_expr: str, terms: list[dict]) -> tuple[str, list]:
    """Compile Advanced-mode term chips into a WHERE fragment against the
    trigram FTS5 index — one `rid IN/NOT IN (SELECT rowid FROM fts WHERE
    doc LIKE ?)` per term, combined with AND/OR at the SQL level.

    The doc-LIKE form (not MATCH) is required by the index's detail=none
    shape — a multi-trigram MATCH is a phrase query, which detail=none
    refuses outright; see build_fts. Applying NOT as SQL-level `NOT IN`
    (rather than any in-query negation) keeps NOT working in any position,
    including as the first term — an earlier MATCH-string version silently
    dropped a leading term's exclude flag because FTS5's NOT is a binary
    "and not" with no unary form.

    Per-term, not just per-query: a term _fts_like_pattern can't route
    through the index (under 3 characters, or containing a LIKE wildcard
    the unescaped pushdown form would misinterpret) gets its own escaped
    `(blob_expr) LIKE ?` atom instead, joined into the exact same AND/OR
    chain as every indexed term — such a term never silently drops out of
    the query, and never changes meaning."""
    fts_in = f"rid IN (SELECT rowid FROM {fts_ident} WHERE doc LIKE ?)"
    fts_not_in = f"rid NOT IN (SELECT rowid FROM {fts_ident} WHERE doc LIKE ?)"
    like_atom = f"({blob_expr}) LIKE ? ESCAPE '\\'"
    parts: list[str] = []
    params: list[Any] = []
    for t in terms:
        term = (t.get("term") or "").strip()
        if not term:
            continue
        exclude = t.get("exclude")
        pattern = _fts_like_pattern(term)
        if pattern is not None:
            atom = fts_not_in if exclude else fts_in
            params.append(pattern)
        else:
            atom = f"NOT {like_atom}" if exclude else like_atom
            params.append(f"%{_esc_like(term)}%")
        if not parts:
            parts.append(atom)
        else:
            connector = "OR" if str(t.get("connector", "AND")).upper() == "OR" else "AND"
            parts.append(f"{connector} {atom}")
    if not parts:
        return "", []
    return "(" + " ".join(parts) + ")", params


def _or_of_positive_terms(terms: list[dict]) -> list[str] | None:
    """The term strings, when `terms` is a plain OR of positive terms —
    otherwise None.

    That shape is exactly what the Search-all modal's "Paste a list" mode
    produces (one term per line, OR'd, nothing excluded), and it's the only
    shape where a per-term count means anything on its own: the terms are
    independent alternatives, so "which of my IOCs hit this table, and how
    hard" is a real question with a real answer. Under mixed AND/NOT the
    terms constrain each other — a count for one of them in isolation
    describes a query the analyst never asked — so those keep the single
    union count they've always had.

    Two terms or more: a one-term list's breakdown is its own total."""
    strings = [(t.get("term") or "").strip() for t in terms]
    if len(strings) < 2 or not all(strings):
        return None
    for i, t in enumerate(terms):
        if t.get("exclude"):
            return None
        if i and str(t.get("connector", "AND")).upper() != "OR":
            return None
    return strings


def _swept_or_terms(query: str, terms: list[dict] | None) -> list[str] | None:
    """The term strings a search-all sweep proves INDEPENDENTLY of each
    other, or None when it proves nothing per term.

    Wider than _or_of_positive_terms in two directions, both still a plain
    OR. A single positive term: that function answers None because a
    one-term breakdown is its own total, which is true of a breakdown and
    beside the point here — "this source matched" and "this term matched
    here" are the same fact when there is one term. And a bare `query`,
    which is one positive term typed into the box instead of chipped.

    Mixed AND/NOT is None, and has to be: under those the terms constrain
    each other, so "no row matched the query" says nothing about whether
    some row holds one of its terms. See
    Store.seed_watchlist_from_search_all, the only caller.
    """
    if terms:
        strings = [(t.get("term") or "").strip() for t in terms]
        if not all(strings):
            return None
        for i, t in enumerate(terms):
            if t.get("exclude"):
                return None
            if i and str(t.get("connector", "AND")).upper() != "OR":
                return None
        return strings
    query = (query or "").strip()
    return [query] if query else None


def _advanced_like_clause(cols: list[str], terms: list[dict]) -> tuple[str, list]:
    """LIKE-chain fallback for sources without an FTS index. Each term is its
    own '(blob) LIKE ?' atom, optionally NOT-prefixed, joined by AND/OR per
    its stated connector — standard SQL boolean precedence applies (NOT >
    AND > OR), same as a hand-typed FTS AND/OR/NOT query would behave."""
    blob = _blob_expr(cols)
    parts: list[str] = []
    params: list[str] = []
    for t in terms:
        term = (t.get("term") or "").strip()
        if not term:
            continue
        atom = f"({blob}) LIKE ? ESCAPE '\\'"
        if t.get("exclude"):
            atom = f"NOT {atom}"
        params.append(f"%{_esc_like(term)}%")
        if not parts:
            parts.append(atom)
        else:
            connector = "OR" if str(t.get("connector", "AND")).upper() == "OR" else "AND"
            parts.append(f"{connector} {atom}")
    if not parts:
        return "", []
    return "(" + " ".join(parts) + ")", params

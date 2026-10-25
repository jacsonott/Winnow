"""Reading OTHER cases, read-only, while one case stays open for writing.

An intrusion spans hosts, and a Winnow case is one host's evidence. The
questions an analyst actually has — "where else did this IOC land", "show
me these three machines on one timeline", "query two collections at once"
— all need several case files at once, and all of them are READS.

The design that follows from that: **one writable case, N read-only
readers.** The open case keeps its exclusive lock, its writer connection,
its views database and every invariant in CLAUDE.md exactly as written;
the others are opened `mode=ro`, with no lock, no views database and no
heartbeat, and nothing here can write to any of them.

That is not a limitation to work around later. Tags, notes and sessions
live in the case file that owns the rows, so writing across cases means
opening them read-write and fighting the one-Winnow-per-case lock. The
supported move is "jump to that row in its own case", which is a click.

Verified rather than assumed: a case that another Winnow has open and is
actively writing (WAL) reads fine through `mode=ro`, and SQLite attaches
at most 10 databases at once (`MAX_ATTACHED=10`) — which is why the
cross-case query path attaches in batches and everything else uses one
connection per case.

Opened through `sqlite_peek_uri`, not plain `mode=ro`: on a case nothing
has open, `mode=ro` creates a -wal/-shm pair beside it that a read-only
connection can never remove, so every cross-case search left one beside
every case it read. A case another Winnow has open has its -wal, and still
reads through plain `mode=ro`, as above.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import json
import os
import re
import sqlite3
from typing import Iterator

from winnow.paths import sqlite_peek_uri
from winnow.store import _esc_like, _ts_normalize, q, read_only_authorizer

# `v` is already attached to the writable Store's connection; leave room.
MAX_ATTACHED = 10
ATTACH_BUDGET = 8

# A table name out of a case's own `sources` row. Anchored because it is
# interpolated into SQL — the rows come from a file on disk, and a case
# file is data, not code (invariant #5's reasoning, applied across cases).
_TABLE_RE = re.compile(r"^src_\d+$")


class NotACaseFile(ValueError):
    """The path is readable but isn't a Winnow case."""


class CaseReader:
    """One other case, read-only. Close it, or use it as a context manager.

    Deliberately not a `Store`: Store takes the case lock, creates a views
    database and starts a heartbeat, all of which are exactly what a
    read-only visitor must not do. It is also a much smaller surface —
    everything here is a SELECT.
    """

    def __init__(self, path: str, name: str | None = None):
        self.path = str(path)
        self.name = name or os.path.splitext(os.path.basename(self.path))[0]
        if not os.path.isfile(self.path):
            raise NotACaseFile(f"No case file at {self.path}")
        self.db = sqlite3.connect(sqlite_peek_uri(self.path), uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # The timeline orders and windows by this, as the case's own
        # timeframe filter and Timeline do. A function, not a write.
        self.db.create_function("TS_NORMALIZE", 1, _ts_normalize, deterministic=True)
        try:
            names = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:      # not a database at all
            self.close()
            raise NotACaseFile(f"{self.path} is not readable as a case: {e}")
        if not {"sources", "row_tags"} <= names:
            self.close()
            raise NotACaseFile(f"{self.path} has no Winnow case tables")

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    def __enter__(self) -> "CaseReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- reads

    def sources(self) -> list[dict]:
        """Every real table in this case, with its columns parsed."""
        out = []
        for r in self.db.execute(
                "SELECT id, name, table_name, row_count, columns, nickname "
                "FROM sources ORDER BY id"):
            if not _TABLE_RE.match(r["table_name"] or ""):
                continue          # a case file naming something else is not ours to trust
            try:
                cols = json.loads(r["columns"] or "[]")
            except json.JSONDecodeError:
                cols = []
            out.append({"id": r["id"], "name": r["name"], "table_name": r["table_name"],
                        "row_count": r["row_count"], "nickname": r["nickname"],
                        "columns": [c["name"] for c in cols],
                        "column_types": {c["name"]: c.get("type") for c in cols}})
        return out

    def datetime_columns(self, source: dict) -> list[str]:
        return [c for c, t in source["column_types"].items() if t == "datetime"]

    def schema_text(self, alias: str) -> str:
        """CREATE TABLE-ish text for the SQL pane, qualified by the alias
        the caller attached this case under."""
        lines = [f"-- {self.name} ({os.path.basename(self.path)}) attached as {alias}"]
        for s in self.sources():
            cols = ",\n  ".join(f"{q(c)} TEXT" for c in s["columns"])
            lines.append(f'CREATE TABLE {alias}."{s["table_name"]}" (  -- {s["name"]}'
                         f' · {s["row_count"]:,} rows\n  {cols}\n);')
        return "\n".join(lines)

    def watchlist_values(self) -> list[str]:
        if not self._has_table("watchlist"):
            return []
        return [r[0] for r in self.db.execute(
            "SELECT value FROM watchlist WHERE value <> '' ORDER BY value")]

    def _has_table(self, name: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    # ------------------------------------------------------------ searching

    def find_values(self, values: list[str], *, limit_per_case: int = 200,
                    columns_per_row: int = 0) -> list[dict]:
        """Rows in this case containing any of `values`, as a substring, in
        any column. One hit per (source, rid, value) — the point is "where
        did this land", not a full row dump; `columns_per_row` > 0 asks for
        that many cells of context.

        LIKE, not the FTS index: a visiting reader must not depend on
        whether the other case ever built one, and must never build it.
        Escaped, because these are literal values: an IOC is very often an
        account or a file name with `_` in it, and unescaped `_` is LIKE's
        any-character — `svc_backup` reported a host whose only account
        was `svcXbackup`.
        """
        wanted = [v for v in (values or []) if str(v).strip()]
        if not wanted:
            return []
        hits: list[dict] = []
        for src in self.sources():
            if len(hits) >= limit_per_case:
                break
            cols = src["columns"]
            if not cols:
                continue
            blob = " || '\\u0001' || ".join(f"COALESCE({q(c)}, '')" for c in cols)
            where = " OR ".join(f"{blob} LIKE ? ESCAPE '\\'" for _ in wanted)
            params = [f"%{_esc_like(str(v))}%" for v in wanted]
            sel = "rid"
            if columns_per_row:
                sel += ", " + ", ".join(q(c) for c in cols[:columns_per_row])
            try:
                rows = self.db.execute(
                    f"SELECT {sel} FROM {q(src['table_name'])} WHERE {where} "
                    f"LIMIT {int(limit_per_case - len(hits))}", params).fetchall()
            except sqlite3.Error:
                continue          # a malformed source must not sink the sweep
            for r in rows:
                matched = [v for v in wanted
                           if any(v.lower() in str(x).lower() for x in tuple(r)[1:] or ())] if columns_per_row else []
                hits.append({
                    "case": self.name, "path": self.path,
                    "source_id": src["id"], "source": src["name"], "rid": r["rid"],
                    "values": matched or None,
                    "cells": {c: r[c] for c in cols[:columns_per_row]} if columns_per_row else {},
                })
        return hits

    # ------------------------------------------------------------- timeline

    def timeline_batches(self, *, ts_column: dict[int, str] | None = None,
                         start: str = "", end: str = "", limit: int = 5000) -> Iterator[list[dict]]:
        """Per source, its first `limit + 1` rows in time order (see
        `_timeline_key`) — the most any one source can contribute to a
        timeline of `limit` rows, plus one to tell whether it ran out.
        `ts_column` picks the timestamp column per source id; without one,
        the first datetime column is used and a source with none is
        skipped.

        Every source gets the full allowance. Each used to be read with
        `LIMIT limit - rows so far` in table order, so the first table
        could spend the whole limit — on its LATEST rows, if another
        table's were earlier — and the rest were never read.

        Times are compared and ordered as TS_NORMALIZE sees them, which is
        what the case's own timeframe filter and Timeline do: as raw text,
        `1/5/2024` sorted before `12/31/2023` and an ISO window compared
        against `M/D/YYYY` kept and dropped the wrong rows. A value that
        doesn't normalise can't be placed in time, so it isn't."""
        start_norm = _ts_normalize(start) if start else None
        end_norm = _ts_normalize(end) if end else None
        for src in self.sources():
            col = (ts_column or {}).get(src["id"]) or next(iter(self.datetime_columns(src)), None)
            if not col or col not in src["columns"]:
                continue
            others = [c for c in src["columns"] if c != col][:4]
            sel = ", ".join(q(c) for c in [col] + others)
            norm = f"TS_NORMALIZE({q(col)})"
            where, params = [f"{norm} IS NOT NULL"], []
            if start_norm is not None:
                where.append(f"{norm} >= ?"); params.append(start_norm)
            if end_norm is not None:
                where.append(f"{norm} <= ?"); params.append(end_norm)
            try:
                rows = self.db.execute(
                    f"SELECT rid, {norm} AS _ts, {sel} FROM {q(src['table_name'])} "
                    f"WHERE {' AND '.join(where)} "
                    f"ORDER BY _ts, {q(col)}, rid LIMIT {int(limit) + 1}", params).fetchall()
            except sqlite3.Error:
                continue
            batch = [{
                "when": r[col], "case": self.name, "path": self.path,
                "source_id": src["id"], "source": src["name"], "rid": r["rid"],
                "column": col,
                "summary": " · ".join(str(r[c]) for c in others if r[c] not in (None, "")),
                "_ts": r["_ts"],
            } for r in rows]
            batch.sort(key=_timeline_key)     # already is; the merge below relies on it
            yield batch

    def timeline_rows(self, *, ts_column: dict[int, str] | None = None,
                      start: str = "", end: str = "", limit: int = 5000) -> list[dict]:
        """This case's first `limit` rows in time order, across every
        source: (when, case, source, rid, summary)."""
        top: list[dict] = []
        for batch in self.timeline_batches(ts_column=ts_column, start=start, end=end, limit=limit):
            top = _first_in_time(top, batch, limit)
        return [_public(r) for r in top]


def _timeline_key(row: dict) -> tuple:
    """Time order: the normalised instant, then the raw text — which keeps
    two events inside one second in their sub-second order, since
    TS_NORMALIZE drops the fraction — then a fixed tie-break."""
    return (row["_ts"], row["when"] or "", row["case"], row["source_id"], row["rid"])


def _first_in_time(top: list[dict], batch: list[dict], n: int) -> list[dict]:
    """The first `n` of two time-ordered lists. Merged as they arrive so a
    timeline over many sources holds about `n` rows, not every source's."""
    return list(itertools.islice(heapq.merge(top, batch, key=_timeline_key), n))


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_ts"}


@contextlib.contextmanager
def open_cases(paths: list[str], names: dict[str, str] | None = None) -> Iterator[list[CaseReader]]:
    """Open several cases read-only, closing every one of them even if a
    later open fails."""
    readers: list[CaseReader] = []
    try:
        for p in paths:
            readers.append(CaseReader(p, (names or {}).get(p)))
        yield readers
    finally:
        for r in readers:
            r.close()


# ------------------------------------------------------------- cross-case SQL

_FORBIDDEN_RE = re.compile(r"\b(attach|detach|pragma|vacuum)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Blank `--` and `/* */` comments. Without this a quote inside a
    comment shifts the parity that _blank_string_literals depends on, and
    the keyword scan below reads over the statement it is meant to catch
    (`/* ' */ ATTACH ...`). Same fix as Store's pane guard."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            end = sql.find("*/", i + 2)
            stop = n if end < 0 else end + 2
            out.append(" " * (stop - i))
            i = stop
        elif sql[i] == "'":
            out.append(sql[i])
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _blank_string_literals(sql: str) -> str:
    """Single-quoted literals blanked, so a keyword inside a string is not
    read as a keyword. Same idea as Store's SQL-pane guard."""
    out, in_str = [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(" ")
        else:
            out.append(" " if in_str else ch)
    return "".join(out)


def query_across(paths: list[str], sql: str, *, limit: int = 5000,
                 names: dict[str, str] | None = None) -> dict:
    """Run ONE read-only SELECT across several cases, each attached under
    `c1`, `c2`, … in the order given.

    A fresh in-memory connection with the cases attached read-only
    (sqlite_peek_uri), so the query cannot reach the open case's writer
    connection, cannot write anywhere, and disappears with the request.
    ATTACH/DETACH/PRAGMA/VACUUM are refused for the same reason
    Store.run_sql refuses them: the caller picks which cases are visible,
    not the SQL.
    """
    if not paths:
        raise ValueError("Pick at least one case")
    if len(paths) > ATTACH_BUDGET:
        raise ValueError(
            f"SQLite attaches at most {MAX_ATTACHED} databases at once — "
            f"pick {ATTACH_BUDGET} cases or fewer")
    structural = _blank_string_literals(_strip_sql_comments(sql or ""))
    if not structural.strip():
        raise ValueError("Write a query")
    if _FORBIDDEN_RE.search(structural):
        raise ValueError("ATTACH, DETACH, PRAGMA and VACUUM aren't allowed here")

    with open_cases(paths, names) as readers:      # validates each is a case first
        aliases = [f"c{i + 1}" for i in range(len(readers))]
        # uri=True although the main database is no file at all: it is
        # the connection's flag that makes SQLite read an ATTACHed name as
        # a URI. Whether that is also on by default is a compile option
        # (SQLITE_USE_URI) — the Linux SQLite CI runs on has it, the one a
        # Windows Python bundles need not — and without it the ATTACH
        # below looks for a file literally named "file:...?mode=ro".
        conn = sqlite3.connect(":memory:", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            for alias, r in zip(aliases, readers):
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (sqlite_peek_uri(r.path),))
            # After the attaches, so they are still allowed: from here the
            # connection can only read. The main database here is :memory:
            # and therefore writable, so the keyword scan above was the only
            # thing standing between a cross-case query and a file write.
            conn.set_authorizer(read_only_authorizer)
            import time as _time
            t0 = _time.time()
            cur = conn.execute(sql)
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
            return {
                "columns": cols,
                "rows": [[r[i] for i in range(len(cols))] for r in rows],
                "truncated": len(rows) == limit,
                "elapsed_ms": int((_time.time() - t0) * 1000),
                "cases": [{"alias": a, "name": r.name, "path": r.path}
                          for a, r in zip(aliases, readers)],
            }
        finally:
            conn.close()


def schema_across(paths: list[str], names: dict[str, str] | None = None) -> str:
    """The attached cases' schema, aliased the way query_across attaches
    them — what the SQL pane shows and what an LLM would be handed."""
    with open_cases(paths, names) as readers:
        return "\n\n".join(r.schema_text(f"c{i + 1}") for i, r in enumerate(readers))


def sweep_values(paths: list[str], values: list[str], *, limit_per_case: int = 200,
                 columns_per_row: int = 4, names: dict[str, str] | None = None) -> list[dict]:
    """IOC sweep: which of these cases hold any of these values, and where.

    One reader at a time rather than attaching, so the number of cases is
    not capped at SQLite's attach limit — an analyst with thirty hosts is
    the case this exists for. A case that fails to open is reported, not
    fatal: a sweep that stops at the first unreadable file is useless on a
    share where one case is mid-copy.
    """
    out = []
    for p in paths:
        try:
            with CaseReader(p, (names or {}).get(p)) as r:
                hits = r.find_values(values, limit_per_case=limit_per_case,
                                     columns_per_row=columns_per_row)
                out.append({"case": r.name, "path": p, "hits": hits,
                            "truncated": len(hits) >= limit_per_case, "error": None})
        except (NotACaseFile, sqlite3.Error) as e:
            out.append({"case": os.path.splitext(os.path.basename(p))[0], "path": p,
                        "hits": [], "truncated": False, "error": str(e)})
    return out


def timeline_across(paths: list[str], *, start: str = "", end: str = "",
                    limit: int = 5000, ts_columns: dict[str, dict[int, str]] | None = None,
                    names: dict[str, str] | None = None) -> dict:
    """One timeline over several cases, in time order.

    Merged in Python over each source's already-ordered rows rather than
    in SQL, so the case count is not capped by SQLite's attach limit and
    one reader is open at a time. Every source offers its own first
    `limit + 1` rows and the merge keeps the first `limit + 1` of all of
    them, so the rows are the true first `limit` and `truncated` is true
    exactly when there were more. `total_seen` counts the rows read to get
    there: a lower bound on the total, which is how the pane shows it
    ("N of M+").

    Read-only by construction, and that is the design: tags and notes live
    in the case that owns the row, so the way to act on a hit is to open it
    in its own case.
    """
    top: list[dict] = []
    seen = 0
    errors = []
    for p in paths:
        try:
            with CaseReader(p, (names or {}).get(p)) as r:
                for batch in r.timeline_batches(ts_column=(ts_columns or {}).get(p),
                                                start=start, end=end, limit=limit):
                    seen += len(batch)
                    top = _first_in_time(top, batch, limit + 1)
        except (NotACaseFile, sqlite3.Error) as e:
            errors.append({"path": p, "error": str(e)})
    return {"rows": [_public(r) for r in top[:limit]], "truncated": len(top) > limit,
            "total_seen": seen, "errors": errors}

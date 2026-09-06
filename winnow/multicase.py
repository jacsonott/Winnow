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
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from typing import Any, Iterator

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
        self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
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
            cols = ",\n  ".join(f'"{c}" TEXT' for c in s["columns"])
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
            blob = " || '\\u0001' || ".join(f'COALESCE("{c}", \'\')' for c in cols)
            where = " OR ".join(f"{blob} LIKE ?" for _ in wanted)
            params = [f"%{v}%" for v in wanted]
            sel = "rid"
            if columns_per_row:
                sel += ", " + ", ".join(f'"{c}"' for c in cols[:columns_per_row])
            try:
                rows = self.db.execute(
                    f'SELECT {sel} FROM "{src["table_name"]}" WHERE {where} '
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

    def timeline_rows(self, *, ts_column: dict[int, str] | None = None,
                      start: str = "", end: str = "", limit: int = 5000) -> list[dict]:
        """Rows from this case as timeline entries: (when, case, source,
        rid, summary). `ts_column` picks the timestamp column per source id;
        without one, the first datetime column is used and a source with
        none is skipped."""
        out: list[dict] = []
        for src in self.sources():
            if len(out) >= limit:
                break
            col = (ts_column or {}).get(src["id"]) or next(iter(self.datetime_columns(src)), None)
            if not col or col not in src["columns"]:
                continue
            others = [c for c in src["columns"] if c != col][:4]
            sel = ", ".join(f'"{c}"' for c in [col] + others)
            where, params = [f'"{col}" <> \'\''], []
            if start:
                where.append(f'"{col}" >= ?'); params.append(start)
            if end:
                where.append(f'"{col}" <= ?'); params.append(end)
            try:
                rows = self.db.execute(
                    f'SELECT rid, {sel} FROM "{src["table_name"]}" WHERE {" AND ".join(where)} '
                    f'ORDER BY "{col}" LIMIT {int(limit - len(out))}', params).fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                out.append({
                    "when": r[col], "case": self.name, "path": self.path,
                    "source_id": src["id"], "source": src["name"], "rid": r["rid"],
                    "column": col,
                    "summary": " · ".join(str(r[c]) for c in others if r[c] not in (None, "")),
                })
        return out


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

    A fresh in-memory connection with the cases attached `mode=ro`, so the
    query cannot reach the open case's writer connection, cannot write
    anywhere, and disappears with the request. ATTACH/DETACH/PRAGMA/VACUUM
    are refused for the same reason Store.run_sql refuses them: the caller
    picks which cases are visible, not the SQL.
    """
    if not paths:
        raise ValueError("Pick at least one case")
    if len(paths) > ATTACH_BUDGET:
        raise ValueError(
            f"SQLite attaches at most {MAX_ATTACHED} databases at once — "
            f"pick {ATTACH_BUDGET} cases or fewer")
    structural = _blank_string_literals(sql or "")
    if not structural.strip():
        raise ValueError("Write a query")
    if _FORBIDDEN_RE.search(structural):
        raise ValueError("ATTACH, DETACH, PRAGMA and VACUUM aren't allowed here")

    with open_cases(paths, names) as readers:      # validates each is a case first
        aliases = [f"c{i + 1}" for i in range(len(readers))]
        conn = sqlite3.connect(":memory:")
        try:
            conn.row_factory = sqlite3.Row
            for alias, r in zip(aliases, readers):
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{r.path}?mode=ro",))
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
    """One timeline over several cases, sorted by the timestamp text.

    Sorted in Python over each case's already-ordered rows rather than in
    SQL: the values are the canonical "YYYY-MM-DD HH:MM:SS[.ffffff]" shape
    (timeparse's whole point), so lexicographic order IS chronological, and
    this way the case count is not capped by SQLite's attach limit.

    Read-only by construction, and that is the design: tags and notes live
    in the case that owns the row, so the way to act on a hit is to open it
    in its own case.
    """
    rows: list[dict] = []
    errors = []
    for p in paths:
        try:
            with CaseReader(p, (names or {}).get(p)) as r:
                rows.extend(r.timeline_rows(
                    ts_column=(ts_columns or {}).get(p), start=start, end=end, limit=limit))
        except (NotACaseFile, sqlite3.Error) as e:
            errors.append({"path": p, "error": str(e)})
    rows.sort(key=lambda x: (x["when"] or "", x["case"], x["source_id"], x["rid"]))
    return {"rows": rows[:limit], "truncated": len(rows) > limit,
            "total_seen": len(rows), "errors": errors}

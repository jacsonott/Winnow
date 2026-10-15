"""The SQL pane's results, held server-side and paged out.

The pane has no row cap: an analyst asked for every row of an answer on
screen and in every export. Holding every row in ONE response was the
first way that was built, and it does not survive a real table —
`SELECT *` over 2M rows of a 10-column export was 516 MB of JSON and
3.4 GB of server memory, a few more columns went past the browser's
string limit, and a recursive CTE missing its stop condition grew until
the OS killed Winnow. So a run copies its rows, a batch at a time, into
a table in a scratch database of its own, and the browser pages that
table the way the grid pages a view (invariant #2): by position, never
LIMIT/OFFSET. Copy, CSV and Excel are read from the same table, in the
order the analyst sorted it, so nothing ever has to hold the whole
answer in memory — not the server, not the tab.

What this module owns, and what it leaves to Store:

* The scratch file. One per Store, created on the first run (most
  sessions never open the SQL pane), in the platform tempdir rather than
  /dev/shm where the views database lives: views are (pos, rid) pairs,
  a result is every cell of every row, and on Linux /dev/shm is RAM.
  Named under the views prefix and flock'd for the Store's life, so
  store.sweep_orphan_views collects one a killed process stranded with
  no new janitor; deleted in close(). auto_vacuum=INCREMENTAL, so a
  released result gives its pages back rather than leaving the file at
  its high-water mark.
* Result tables `r_N` (columns c0..cN — the query's own names can repeat
  and are kept in the metadata) with rowid = position + 1, and sort
  tables `o_N_<col>_<a|d>` of (pos, rowid) built on first request — the
  views' own materialise-then-page shape. Sorting reproduces what the
  pane's client-side sort did: NULLs first, values that read as numbers
  numerically (text such as '4624' included — ingest stores text), the
  rest case-insensitively, ties in query order; descending reverses all
  of it but the tie-break.
* At most KEEP results; the oldest goes first. The client releases a
  tab's previous result when it re-runs and when the tab closes, and a
  page request for one that is gone is ResultExpired (the route's 404).

The query itself never runs here. Store runs it on the pane's read-only
connection (the read_only_authorizer, untouched) and hands this module
the cursor: the analyst's SQL never shares a connection with anything
that can write.
"""

from __future__ import annotations

import contextlib
import decimal
import math
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX only — see store._views_file_is_orphaned
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# Must start with store.VIEWS_PREFIX (tests/test_sql_results.py pins it):
# that prefix and the flock are the whole of how sweep_orphan_views tells
# an abandoned scratch file from a live one.
FILE_PREFIX = "winnow-views-sqlres-"
FILE_SUFFIX = ".db"
FILE_SUFFIXES = ("", "-wal", "-shm")


class ResultExpired(Exception):
    """The result was released, evicted, or belongs to a case that has
    since closed. The route answers 404 and the pane says to run the
    query again — the rows were never anywhere else."""


class TooBig(Exception):
    """A copy that would put more on the clipboard than a browser tab
    can hold. The route answers 413 with this message; CSV… streams the
    same rows to disk instead."""


def _sort_num(v):
    """The numeric reading of a value for sorting, or None for "not a
    number" — what the pane's client-side sort tested with Number(),
    minus Number()'s accidents: blank is not 0, Infinity and NaN are
    not numbers, and an int keeps its precision (FILETIMEs and other
    18-digit values would tie as floats)."""
    if v is None or isinstance(v, bytes):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    s = str(v).strip()
    if not s or "_" in s:   # float('1_000') is 1000.0 to Python, text to an analyst
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


# The largest integer a JavaScript number holds exactly.
_JS_MAX_SAFE_INT = 2 ** 53 - 1


def _blob_text(b: bytes) -> str:
    """A BLOB as the SQL pane shows it: its text when it is UTF-8 (what
    CAST('abc' AS BLOB) holds, and what the grid already showed), else
    SQLite's own literal for it, X'00FF' — what quote() prints."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return "X'" + b.hex().upper() + "'"


def _nonfinite_text(f: float) -> str:
    """Inf/-Inf/NaN spelled the way SQLite's CAST(… AS TEXT) spells them."""
    return "NaN" if f != f else ("Inf" if f > 0 else "-Inf")


def pane_cell_json(v: Any) -> Any:
    """One SQL pane cell as JSON can carry it to the browser without
    changing what it says. A non-UTF-8 BLOB (SELECT x'00ff') and a
    non-finite REAL (SELECT 1e999) both made /api/sql a 500 — JSON has no
    bytes and no Infinity — and an integer past 2^53 arrived rounded, so
    the grid, Copy, CSV and Excel all showed a different number from the
    one in the case (a FILETIME or a 64-bit id is exactly that size).
    Those three become text; everything else passes through as it was.

    Every way a held result leaves the server goes through here — a page,
    a pick, the workbook — and /api/sql's inline rows do too, so the pane
    says the same thing whichever route a value took."""
    if isinstance(v, bytes):
        return _blob_text(v)
    if isinstance(v, float) and not math.isfinite(v):
        return _nonfinite_text(v)
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > _JS_MAX_SAFE_INT:
        return str(v)
    return v


def pane_cell_text(v: Any) -> str:
    """The same cell as the TEXT "Save as table" stores — the same spelling
    the pane showed, not Python's repr (a BLOB used to land as "b'abc'")."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return _blob_text(v)
    if isinstance(v, float) and not math.isfinite(v):
        return _nonfinite_text(v)
    return str(v)


def js_str(v) -> str:
    """A value as the pane SHOWS it — JavaScript's String() of the JSON
    number, not Python's str() — so a copy or a CSV says what the screen
    said: 1.0 is "1", 1e-07 is "1e-7", 0.00001 is "0.00001"."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return _nonfinite_text(v)
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        s = repr(v)
        if "e" in s:
            if 1e-6 <= abs(v) < 1e21:
                return format(decimal.Decimal(s), "f")
            mant, exp = s.split("e")
            n = int(exp)
            return f"{mant}e{'+' if n >= 0 else '-'}{abs(n)}"
        return s
    if isinstance(v, bytes):
        return _blob_text(v)
    return str(v)


def _int_expr(col: str) -> str:
    """SQL for `col` read as an integer key, NULL when it isn't one — the
    server side of the client's Number.isInteger test in sqlRowKey, so a
    rid the query cast to text still joins its tags."""
    return (f"(CASE WHEN typeof({col}) = 'integer' THEN {col} "
            f"WHEN typeof({col}) = 'text' AND trim({col}) <> '' AND trim({col}) <> '-' "
            f"AND ltrim(trim({col}), '-') NOT GLOB '*[^0-9]*' THEN CAST(trim({col}) AS INTEGER) "
            f"WHEN typeof({col}) = 'real' AND {col} = CAST({col} AS INTEGER) THEN CAST({col} AS INTEGER) END)")


class Ref:
    """Which result columns name a real row: `rid_col`, plus either a
    `sid_col` (a merge_N, or a join that kept source_id) or the one
    source `sid` the query reads. The client works it out (sqlRowRef)
    from the columns and the SQL text; this only checks it."""

    def __init__(self, ncols: int, rid_col, sid_col=None, sid=None):
        try:
            self.rid_col = int(rid_col)
            self.sid_col = None if sid_col is None else int(sid_col)
            self.sid = None if sid is None else int(sid)
        except (TypeError, ValueError):
            raise ValueError("A row reference names columns by index") from None
        if not 0 <= self.rid_col < ncols or (self.sid_col is not None and not 0 <= self.sid_col < ncols):
            raise ValueError("That column isn't in the result")
        if (self.sid_col is None) == (self.sid is None):
            raise ValueError("Give either the source_id column or the one source")

    def sid_sql(self) -> str:
        return _int_expr(f"c{self.sid_col}") if self.sid_col is not None else str(self.sid)

    def rid_sql(self) -> str:
        return _int_expr(f"c{self.rid_col}")


class SqlResults:
    KEEP = 12                  # results held at once, oldest evicted first
    ORDERS_PER_RESULT = 2      # sort tables kept per result (the current one and the last)
    BATCH = 5_000              # rows per committed batch while a run lands
    PREVIEW_ROWS = 5_000       # rows the run response carries: the old cap, which is what winnow.sqlPage promised
    PAGE_ROWS_MAX = 5_000
    PAGE_BYTES = 8 << 20       # a page stops early past this much text, so one wide page can't be the next 500 MB response
    COPY_MAX_BYTES = 64 << 20  # past this, Copy says to use CSV…
    MIN_FREE_BYTES = 1 << 30   # a run stops, keeping its rows, when the scratch disk has less than this left
    DISK_CHECK_EVERY = 40      # batches between free-space checks

    def __init__(self, directory: str | None = None):
        self._dir = directory
        # Two locks, and _reg is only ever taken INSIDE _lock or alone,
        # never the other way round. _lock is the writer connection: held
        # for one batch, one sort build, one drop. _reg is the registry,
        # held for a dict lookup — so paging one result never waits behind
        # a multi-second sort build for another.
        self._lock = threading.RLock()
        self._reg = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._path: str | None = None
        self._fd: int | None = None
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._seq = 0
        self._closed = False

    # ------------------------------------------------------------ the file

    @property
    def path(self) -> str | None:
        return self._path

    def _writer(self) -> sqlite3.Connection:
        """The scratch database's one writer, opened on first use. Call
        with self._lock held."""
        if self._closed:
            raise ResultExpired("The case is closing")
        if self._db is not None:
            return self._db
        fd, path = tempfile.mkstemp(prefix=FILE_PREFIX, suffix=FILE_SUFFIX, dir=self._dir)
        # Held for the Store's life, like the views file's: the sweep's
        # liveness probe. Unlockable (no flock, Windows) just means "never
        # swept while open", which is the safe direction.
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
            except OSError:
                os.close(fd)
        else:  # pragma: no cover - Windows
            os.close(fd)
        db = sqlite3.connect(path, check_same_thread=False)
        # Before any table exists, or it can't be switched on.
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA journal_mode=WAL")
        # Scratch that dies with the process, like the views database —
        # never evidence, so no fsync to pay for.
        db.execute("PRAGMA synchronous=OFF")
        db.create_function("WINNOW_SORT_NUM", 1, _sort_num, deterministic=True)
        self._db, self._path = db, path
        return db

    @contextlib.contextmanager
    def _reader(self, attach_case: str | None = None) -> Iterator[sqlite3.Connection]:
        """A read-only connection of its own per call, so paging one
        result never waits on a batch landing for another (WAL: readers
        see what is committed). `attach_case` adds the case file, read
        only, as `casedb` — the tags join."""
        path = self._path
        if self._closed or path is None:
            raise ResultExpired("This result is no longer held — run the query again")
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True,
                               check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if attach_case:
                # as_uri percent-encodes the path, so a '#' or '?' in a
                # case's folder name can't cut the filename short.
                conn.execute("ATTACH DATABASE ? AS casedb",
                             (Path(attach_case).absolute().as_uri() + "?mode=ro",))
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Drop every result and delete the file. Store.close() calls this
        after the runs it cancelled have finished landing."""
        with self._lock:
            self._closed = True
            with self._reg:
                self._results.clear()
            if self._db is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._db.close()
                self._db = None
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None
            if self._path:
                for suffix in FILE_SUFFIXES:
                    with contextlib.suppress(OSError):
                        os.remove(self._path + suffix)

    # ------------------------------------------------------------ results

    def _meta(self, result_id: str) -> dict:
        with self._reg:
            meta = self._results.get(result_id)
            if meta is None or self._closed:
                raise ResultExpired("This result is no longer held — run the query again")
            self._results.move_to_end(result_id)
            return meta

    def _drop_locked(self, meta: dict) -> None:
        db = self._db
        if db is None:
            return
        for name in [*meta["orders"].values(), meta["table"]]:
            with contextlib.suppress(sqlite3.Error):
                db.execute(f"DROP TABLE IF EXISTS {name}")
        meta["orders"].clear()

    def release(self, ids) -> int:
        """Forget these results (unknown ids are fine — a re-run sends its
        tab's last one whether or not it is still held)."""
        with self._reg:
            gone = [m for m in (self._results.pop(str(rid), None) for rid in ids or ()) if m is not None]
        n = len(gone)
        with self._lock:
            for meta in gone:
                self._drop_locked(meta)
            if n and self._db is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._db.commit()
                    self._db.execute("PRAGMA incremental_vacuum")
        return n

    def _disk_low(self) -> bool:
        try:
            return shutil.disk_usage(os.path.dirname(self._path or ".") or ".").free < self.MIN_FREE_BYTES
        except OSError:
            return False

    def materialise(self, cur: sqlite3.Cursor, *, cancelled: Callable[[], bool] = lambda: False,
                    progress: Callable[[int], None] = lambda n: None) -> dict:
        """Copy an executed cursor's rows into a new result table, BATCH at
        a time, committing each — never the whole answer in memory, and
        the writer lock held for one batch, not the run (invariant #4's
        pattern). Returns the result's metadata.

        Stops early, keeping what landed, when `cancelled()` (the analyst's
        Stop, or the case closing — Store interrupts the pane connection,
        which surfaces here as an "interrupted" error from the next fetch)
        or when the scratch disk is nearly full: `stopped` says which, and
        the pane says so beside the row count. Any other error drops the
        partial table and raises — a half-result that looked whole is the
        thing never to hand back."""
        names = [d[0] for d in cur.description] if cur.description else []
        n = len(names)
        with self._lock:
            db = self._writer()
            self._seq += 1
            seq = self._seq
            table = f"r_{seq}"
            db.execute(f"CREATE TABLE {table} ({', '.join(f'c{i}' for i in range(max(n, 1)))})")
            db.commit()
            meta = {"id": secrets.token_hex(12), "seq": seq, "table": table, "columns": names,
                    "total": 0, "widths": [0] * n, "orders": OrderedDict(), "stopped": None}
            with self._reg:
                self._results[meta["id"]] = meta
                evicted = []
                while len(self._results) > self.KEEP:
                    evicted.append(self._results.popitem(last=False)[1])
            for old in evicted:
                self._drop_locked(old)
            db.commit()
        insert = f"INSERT INTO {table} VALUES ({', '.join('?' * n)})"
        total, batches, stopped = 0, 0, None
        try:
            while n:
                if cancelled():
                    stopped = "cancelled"
                    break
                try:
                    rows = cur.fetchmany(self.BATCH)
                except sqlite3.OperationalError as e:
                    if "interrupt" in str(e).lower() and cancelled():
                        stopped = "cancelled"
                        break
                    raise
                if not rows:
                    break
                with self._lock:
                    db = self._writer()
                    with db:
                        db.executemany(insert, rows)
                total += len(rows)
                progress(total)
                batches += 1
                if batches % self.DISK_CHECK_EVERY == 0 and self._disk_low():
                    stopped = "disk"
                    break
            widths = [0] * n
            if n and total:
                with self._lock:
                    got = self._writer().execute(
                        f"SELECT {', '.join(f'max(length(c{i}))' for i in range(n))} FROM {table}").fetchone()
                widths = [int(w or 0) for w in got]
        except BaseException:
            self.release([meta["id"]])
            raise
        meta.update(total=total, widths=widths, stopped=stopped)
        return meta

    def summary(self, result_id: str, preview: int | None = None) -> dict:
        """What the run response carries: the shape, the count, the widths
        and the first `preview` rows in query order (byte-budgeted like a
        page)."""
        meta = self._meta(result_id)
        out = {"id": meta["id"], "columns": meta["columns"], "total": meta["total"],
               "widths": meta["widths"], "stopped": meta["stopped"]}
        n = self.PREVIEW_ROWS if preview is None else preview
        out["rows"] = self.page(result_id, 0, n)["rows"] if n and meta["total"] else []
        return out

    # ------------------------------------------------------------ order

    def _order_table(self, meta: dict, sort) -> str | None:
        """The (pos, r) table for a sort, built on first request and kept
        (the latest ORDERS_PER_RESULT per result). None for query order."""
        if sort is None:
            return None
        idx, desc = sort
        if not 0 <= idx < len(meta["columns"]):
            raise ValueError("That column isn't in the result")
        key = (idx, bool(desc))
        with self._lock:
            with self._reg:
                held = meta["id"] in self._results
            if not held:
                raise ResultExpired("This result is no longer held — run the query again")
            name = meta["orders"].get(key)
            if name:
                meta["orders"].move_to_end(key)
                return name
            db = self._writer()
            name = f"o_{meta['seq']}_{idx}_{'d' if desc else 'a'}"
            c, k = f"c{idx}", f"WINNOW_SORT_NUM(c{idx})"
            d = "DESC" if desc else "ASC"
            # Ascending: NULLs, then numbers by value, then text without
            # case and with it. Descending flips each of those; the rowid
            # tie-break stays ascending either way, which is what makes
            # this the client's stable Array.sort.
            order = (f"({c} IS NULL) {'ASC' if desc else 'DESC'}, "
                     f"({k} IS NULL) {'DESC' if desc else 'ASC'}, {k} {d}, "
                     f"CAST({c} AS TEXT) COLLATE NOCASE {d}, CAST({c} AS TEXT) {d}, rowid ASC")
            with db:
                db.execute(f"DROP TABLE IF EXISTS {name}")
                db.execute(f"CREATE TABLE {name} (pos INTEGER PRIMARY KEY, r INTEGER NOT NULL)")
                db.execute(f"INSERT INTO {name}(r) SELECT rowid FROM {meta['table']} ORDER BY {order}")
            meta["orders"][key] = name
            while len(meta["orders"]) > self.ORDERS_PER_RESULT:
                _, old = meta["orders"].popitem(last=False)
                with contextlib.suppress(sqlite3.Error), db:
                    db.execute(f"DROP TABLE IF EXISTS {old}")
            return name

    def _ordered(self, conn: sqlite3.Connection, meta: dict, sort, select: str = "r.*",
                 start: int = 0, stop: int | None = None, where: str = "", params=()) -> sqlite3.Cursor:
        """Rows [start, stop) in displayed order. Position ranges, never
        LIMIT/OFFSET: rowid (query order) or the sort table's pos, both
        position + 1, are a seek however deep the window."""
        stop = meta["total"] if stop is None else stop
        ot = self._order_table(meta, sort)
        cond = f" AND ({where})" if where else ""
        if ot:
            return conn.execute(
                f"SELECT {select} FROM {ot} o JOIN {meta['table']} r ON r.rowid = o.r "
                f"WHERE o.pos > ? AND o.pos <= ?{cond} ORDER BY o.pos", (start, stop, *params))
        return conn.execute(
            f"SELECT {select} FROM {meta['table']} r WHERE r.rowid > ? AND r.rowid <= ?{cond} "
            f"ORDER BY r.rowid", (start, stop, *params))

    @staticmethod
    def _sort(sort, order) -> tuple[int, bool] | None:
        if sort is None:
            return None
        return int(sort), str(order).lower() == "desc"

    # ------------------------------------------------------------ reads

    def page(self, result_id: str, start: int, count: int, sort=None, order: str = "asc") -> dict:
        """Up to `count` rows from position `start` — fewer when they pass
        PAGE_BYTES (at least one, however wide). The client asks again for
        whatever it is still missing."""
        meta = self._meta(result_id)
        start = max(0, int(start))
        count = max(0, min(int(count), self.PAGE_ROWS_MAX))
        rows: list[list] = []
        if count and start < meta["total"]:
            budget = 0
            with self._reader() as conn:
                cur = self._ordered(conn, meta, self._sort(sort, order), start=start,
                                    stop=min(meta["total"], start + count))
                for row in cur:
                    out = [pane_cell_json(v) for v in row]
                    rows.append(out)
                    budget += sum(len(v) if isinstance(v, str) else 8 for v in out) + 4 * len(out)
                    if budget > self.PAGE_BYTES:
                        break
        return {"start": start, "rows": rows, "total": meta["total"]}

    def iter_rows(self, result_id: str, sort=None, order: str = "asc") -> tuple[list[str], Iterator[tuple]]:
        """Every row in displayed order, streamed off one read connection
        held for the walk — the exports. Raw stored values: the caller
        formats them for its file."""
        meta = self._meta(result_id)
        srt = self._sort(sort, order)
        self._order_table(meta, srt)   # built now, so a bad column fails before the response starts

        def walk():
            with self._reader() as conn:
                yield from self._ordered(conn, meta, srt)
        return meta["columns"], walk()

    def keys(self, result_id: str, start: int, count: int, ref: dict, sort=None, order: str = "asc") -> list:
        """[sid, rid] (or None) for positions [start, start+count) — a
        shift-click range over rows the browser never loaded."""
        meta = self._meta(result_id)
        r = Ref(len(meta["columns"]), **ref)
        start = max(0, int(start))
        stop = min(meta["total"], start + max(0, int(count)))
        with self._reader() as conn:
            return [[s, k] if s is not None and k is not None else None
                    for s, k in self._ordered(conn, meta, self._sort(sort, order),
                                              select=f"{r.sid_sql()}, {r.rid_sql()}", start=start, stop=stop)]

    def tags(self, result_id: str, ref: dict, case_path: str) -> dict:
        """{"map": {"sid:rid": [tag ids]}, "sids": [...]} for the rows the
        result holds that carry a tag. Bounded by how many rows are
        TAGGED, not by how many the result has: row_tags is walked for
        the result's source(s) and each hit checked against the result's
        own keys."""
        meta = self._meta(result_id)
        r = Ref(len(meta["columns"]), **ref)
        t = meta["table"]
        out: dict[str, list[int]] = {}
        with self._reader(attach_case=case_path) as conn:
            if r.sid_col is not None:
                hits = conn.execute(
                    f"SELECT t.source_id, t.rid, group_concat(t.tag_id) FROM casedb.row_tags t "
                    f"WHERE (t.source_id, t.rid) IN (SELECT {r.sid_sql()}, {r.rid_sql()} FROM {t}) "
                    f"GROUP BY 1, 2")
                sids = [row[0] for row in conn.execute(
                    f"SELECT DISTINCT {r.sid_sql()} AS s FROM {t} WHERE s IS NOT NULL LIMIT 1000")]
            else:
                hits = conn.execute(
                    f"SELECT t.source_id, t.rid, group_concat(t.tag_id) FROM casedb.row_tags t "
                    f"WHERE t.source_id = ? AND t.rid IN (SELECT {r.rid_sql()} FROM {t}) GROUP BY 1, 2",
                    (r.sid,))
                sids = [r.sid]
            for sid, rid, ids in hits:
                out[f"{sid}:{rid}"] = [int(x) for x in str(ids or "").split(",") if x]
        return {"map": out, "sids": sids}

    def pick(self, result_id: str, sort=None, order: str = "asc", keys=None, ref: dict | None = None,
             max_bytes: int | None = None, as_text: bool = True) -> dict:
        """The rows to copy, in displayed order: every row, or the ones
        `keys` ([[sid, rid], ...], the pane's selection) name. as_text
        gives the TSV the Copy button puts on the clipboard (header row
        first, values as the screen shows them); otherwise the rows, for
        winnow.sqlPage.selectedRows. Past `max_bytes` it raises TooBig
        rather than build a string the tab would fall over on."""
        meta = self._meta(result_id)
        srt = self._sort(sort, order)
        limit = self.COPY_MAX_BYTES if max_bytes is None else max_bytes
        where, params = "", ()
        with self._reader() as conn:
            if keys is not None:
                if ref is None:
                    raise ValueError("A selection needs the row reference it was made with")
                r = Ref(len(meta["columns"]), **ref)
                conn.execute("CREATE TEMP TABLE pick_keys (s INTEGER, r INTEGER, PRIMARY KEY (s, r)) WITHOUT ROWID")
                pairs = []
                for k in keys:
                    try:
                        pairs.append((int(k[0]), int(k[1])))
                    except (TypeError, ValueError, IndexError):
                        continue
                conn.executemany("INSERT OR IGNORE INTO temp.pick_keys VALUES (?, ?)", pairs)
                where = f"({r.sid_sql()}, {r.rid_sql()}) IN (SELECT s, r FROM temp.pick_keys)"
            cur = self._ordered(conn, meta, srt, where=where, params=params)
            if as_text:
                parts = ["\t".join(str(c) for c in meta["columns"])]
                size = len(parts[0])
                for row in cur:
                    line = "\t".join(js_str(v) for v in row)
                    size += len(line) + 1
                    if size > limit:
                        raise TooBig(
                            f"Too much to copy ({meta['total']:,} rows) — use CSV… to save it to a file instead")
                    parts.append(line)
                return {"text": "\n".join(parts), "rows": len(parts) - 1}
            rows, size = [], 0
            for row in cur:
                size += sum(len(v) if isinstance(v, str) else 8 for v in row)
                if size > limit:
                    raise TooBig(f"Too many rows selected to hand over ({meta['total']:,} in the result)")
                rows.append([pane_cell_json(v) for v in row])
            return {"columns": meta["columns"], "rows": rows}

"""Saying "the disk is full" when the disk is full.

A full disk reaches Winnow as whichever error the layer that hit it first
raises, and none of them say it plainly: SQLite's "database or disk is
full" (prefixed, by a CSV import, with the line its reader had got to, as
if that line were the problem), the OS's "[Errno 28] No space left on
device" from an upload being copied to the temp folder, and — when the
temp folder fills while the upload is still arriving — FastAPI's "There
was an error parsing the body", which drops the cause altogether. An
analyst whose import stopped got one of those in a toast that was gone in
seconds, or a partial table that looked complete; the report that started
this module was "the import failed silently".

So every place an import can fail asks is_disk_full() about the exception
it caught, chain and all, and when the answer is yes says so in two
lengths. brief() is the one line the jobs panel and the 507 carry: the
drive with the least room and what happened to the import ("Out of disk
space on D: (0 B free). 40,000 rows were kept as a partial table.").
message() is the log's: every drive Winnow writes to with the folders on
it, and what to do once there is room. The first version put all of that
in the panel and the toast, and it was too much to read in a corner.

Stdlib only, imports nothing from the app.
"""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
from collections.abc import Iterator

# SQLite's primary result code for "database or disk is full". An extended
# code carries it in the low byte.
SQLITE_FULL = 13

# Out of space, or out of the user's share of it: the analyst does the
# same thing about either. EDQUOT is absent on some platforms.
_FULL_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})

# ERROR_HANDLE_DISK_FULL and ERROR_DISK_FULL, for an OSError on Windows
# whose winerror the C runtime did not map to ENOSPC.
_FULL_WINERRORS = frozenset({39, 112})


class DiskFullError(ValueError):
    """An import reader that stopped because the disk filled, and how many
    `unit`s (rows, lines, records) it had committed — which it keeps, as a
    partial table. A ValueError, like every other import failure a reader
    raises, so the routes that turn those into a 400 still do; the
    handlers in server.py then find this in the chain and answer with
    what it knows instead."""

    def __init__(self, kept: int = 0, unit: str = "rows"):
        self.kept = kept
        self.unit = unit
        super().__init__(f"Out of disk space. {self.outcome}.")

    @property
    def outcome(self) -> str:
        if not self.kept:
            return "Nothing was imported"
        return f"{_count(self.kept, self.unit)} {_were(self.kept)} kept as a partial table"

    @property
    def advice(self) -> str:
        return "delete that table and import the file again" if self.kept else "import the file again"


def _count(n: int, unit: str) -> str:
    return f"{n:,} {unit if n != 1 else unit[:-1]}"


def _were(n: int) -> str:
    return "were" if n != 1 else "was"


def chain(exc: BaseException | None) -> Iterator[BaseException]:
    """`exc`, then what caused it, then what that was raised while
    handling: the explicit `raise ... from e` first, else the implicit
    context unless it was suppressed (`from None`). The readers wrap their
    failures (`ValueError("Line 60,001: ...")` from the sqlite3 error),
    and FastAPI wraps a failed form parse, so the disk-full error is
    rarely the one that reaches the handler."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        if exc.__cause__ is not None:
            exc = exc.__cause__
        elif not exc.__suppress_context__:
            exc = exc.__context__
        else:
            exc = None


def _says_full(e: BaseException) -> bool:
    if isinstance(e, DiskFullError):
        return True
    if isinstance(e, OSError):
        return e.errno in _FULL_ERRNOS or getattr(e, "winerror", None) in _FULL_WINERRORS
    if isinstance(e, sqlite3.Error):
        code = getattr(e, "sqlite_errorcode", None)   # Python 3.11+
        if code is not None:
            return code & 0xFF == SQLITE_FULL
        return "database or disk is full" in str(e)
    return False


def is_disk_full(exc: BaseException | None) -> bool:
    """Whether `exc`, or anything in its chain, is the disk filling up.

    Only the errors that say so: ENOSPC/EDQUOT (and Windows' own two
    codes) from the OS, SQLITE_FULL from SQLite. Not a "disk I/O error",
    which a full disk can cause but so can a failing one, and telling an
    analyst to free space on a dying drive sends them the wrong way."""
    return any(_says_full(e) for e in chain(exc))


def root_cause(exc: BaseException) -> BaseException:
    """The error in the chain that said the disk was full (the last one,
    the lowest layer), for the log: the message the analyst reads is
    written for them, and whoever reads the log next wants the original."""
    found = exc
    for e in chain(exc):
        if _says_full(e) and not isinstance(e, DiskFullError):
            found = e
    return found


def find_disk_full_error(exc: BaseException | None) -> DiskFullError | None:
    """The DiskFullError in the chain, if a reader raised one: it knows
    what the import kept, which nothing above it does."""
    return next((e for e in chain(exc) if isinstance(e, DiskFullError)), None)


def stopped_error(e: BaseException, total: int, unit: str = "rows") -> DiskFullError | None:
    """What an import reader raises when it stops part way because the
    disk filled: a DiskFullError saying how many `unit`s it kept, with `e`
    as its cause. None when `e` is anything else, so the reader raises its
    usual error. "Line 60,001: database or disk is full" read as if that
    line were the problem, and the line is only how far the reader had got
    when a commit, up to a batch behind it, failed."""
    if not is_disk_full(e):
        return None
    error = DiskFullError(total, unit)
    error.__cause__ = e
    return error


def format_size(n: int) -> str:
    """Bytes as an analyst reads them: 0 B, 512 KB, 37.2 MB, 1.4 GB."""
    size = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}" if size < 100 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} TB"  # unreachable


def _existing(path: str) -> str:
    """The nearest folder at or above `path` that exists: a case folder
    or temp folder that has gone missing still has a drive."""
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _drive_key(path: str):
    # A drive letter or share on Windows, where st_dev is not always
    # filled in for a folder; the device number everywhere else, which is
    # what tells /tmp on its own tmpfs apart from the case's disk.
    if os.name == "nt":
        return os.path.splitdrive(path)[0].upper() or path
    try:
        return os.stat(path).st_dev
    except OSError:
        return path


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def drive_name(path: str) -> str:
    """What an analyst calls the drive `path` is on: its letter (or
    \\\\server\\share) on Windows, its mount point elsewhere."""
    path = _existing(path)
    if os.name == "nt":
        return os.path.splitdrive(path)[0] or path
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _drives(places: list[tuple[str, str]]) -> list[dict]:
    """The drives `places` ([(label, folder)]) are on, emptiest first:
    {"free", "names" (each "label (folder)"), "probe" (a folder on it that
    exists)}. A folder named twice is counted once; one that cannot be
    measured is left out."""
    drives: dict = {}
    seen: set[str] = set()
    for label, folder in places:
        if not folder:
            continue
        folder = os.path.abspath(folder)
        if os.path.normcase(folder) in seen:
            continue
        seen.add(os.path.normcase(folder))
        probe = _existing(folder)
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            continue
        entry = drives.setdefault(_drive_key(probe), {"free": free, "names": [], "probe": probe})
        entry["free"] = min(entry["free"], free)
        entry["names"].append(f"{label} ({folder})")
    return sorted(drives.values(), key=lambda d: d["free"])


def free_space(places: list[tuple[str, str]]) -> str:
    """One sentence on how much room is left, per drive, emptiest first:
    "The drive holding the case (D:\\Cases) has 0 B free; the one holding
    the temp folder (C:\\Users\\a\\AppData\\Local\\Temp) has 12.4 GB."
    `places` is [(label, folder)]; places on one drive share a clause.
    Empty when no place can be measured. For the log (message())."""
    drives = _drives(places)
    if not drives:
        return ""
    first, rest = drives[0], drives[1:]
    clauses = [f"The drive holding {_and(first['names'])} has {format_size(first['free'])} free"]
    clauses += [f"the one holding {_and(d['names'])} has {format_size(d['free'])}" for d in rest]
    return "; ".join(clauses) + "."


def brief(outcome: str | None, places: list[tuple[str, str]]) -> str:
    """The one line the jobs panel, the 507 and a toast carry: the drive
    with the least room of those `places` are on, and `outcome` (a
    sentence, without its full stop) when there is one to tell.

    Out of disk space on D: (0 B free). 40,000 rows were kept as a partial table.

    One drive, the emptiest, because that is the one to clear; the others
    and what to do once there is room are in the log (message())."""
    drives = _drives(places)
    head = "Out of disk space"
    if drives:
        head += f" on {drive_name(drives[0]['probe'])} ({format_size(drives[0]['free'])} free)"
    return f"{head}." + (f" {outcome}." if outcome else "")


def message(outcome: str | None, advice: str, places: list[tuple[str, str]]) -> str:
    """The whole account, for the log, where whoever reads it next has the
    time: what happened, the free space on every drive with the folders on
    it, and what to do.

    Out of disk space. 40,000 rows were kept as a partial table. The drive
    holding the case (D:\\Cases) has 0 B free; the one holding the temp
    folder (C:\\...\\Temp) has 12.4 GB. Free up space, then delete that
    table and import the file again."""
    where = free_space(places)
    return ("Out of disk space." + (f" {outcome}." if outcome else "")
            + (f" {where}" if where else "") + f" Free up space, then {advice}.")

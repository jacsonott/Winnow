r"""Reading body files for import: the pipe-delimited file listing that
The Sleuth Kit's `fls -m` and `ils -m` write and `mactime` turns into a
timeline. UAC collects one as `bodyfile/bodyfile.txt`, MFTECmd writes one
with `--body`, and Volatility's timeliner and mftparser write one with
`--output=body`. Each line is one file system entry with eleven fields, in
the layout TSK has used since 3.0:

    MD5|name|inode|mode_as_string|UID|GID|size|atime|mtime|ctime|crtime

    0|/etc/passwd|1835063|r/rrw-r--r--|0|0|2913|1714557598|1714473600|1714473600|0

Stdlib only. Imports iisread for FileChanged and nothing else from the
app: store.py reads the file with iisread's logical lines and hands them
here.

**One row per line: the fields as columns, the four times converted.**
The columns are

    Modified (UTC)  Accessed (UTC)  Changed (UTC)  Born (UTC)
    Name  Size  Mode  UID  GID  Inode  MD5

The times lead, in mactime's MACB order, so the first datetime column
(the one the Timeframe filter, the histogram and the Timeline reach for)
is the modification time rather than atime, which noatime and relatime
mounts barely keep. Changed is ctime: the inode's change time on a Unix
file system, the MFT entry's on NTFS. Born is crtime. The other fields
keep the file's own order. Size, UID and GID are numbers; Inode stays text,
since NTFS writes it as entry-type-id (`1234-128-1`).

**Times are seconds since 1970, UTC.** TSK writes whole seconds; `find
-printf %T@` (one of the ways UAC builds its body file) writes a ten-digit
fraction. Both become timeparse's canonical `YYYY-MM-DD HH:MM:SS`, with
the fraction truncated to six digits when there is one, so they sort and
filter like every other datetime column. 0 means the file system did not
record that time (ext3 keeps no crtime, and neither do FAT's atimes) and
is left empty, as mactime leaves it off the timeline; so is `-`, which
some `stat` builds print for an unknown birth time. A number that is no
real time (before year 1 or after 9999) is kept exactly as written rather
than dropped.

**A name can hold a pipe.** fls writes a name as the file system has it,
and Linux allows `|` in a file name. So the MD5 is the first field, the
nine after the name are the last nine, and the name is everything in
between. A fixed split would shift every field after a `|` in a name.

**A line that isn't a body file line is kept, not dropped.** It goes whole
into `Unparsed line`, a column only a file that has such a line gets, with
the eleven columns left empty, and it is counted (`stats["unparsed"]`).
The TSK 2.x layout (sixteen fields: device, the mode as a number and as a
string, link count, rdev and block counts, and no crtime) is one of those
lines. Read as 3.x, its fields would land one or two columns off, and
every one of them is a number that passes for the field it lands in, so a
line in that shape is refused (`_TSK2`) rather than misread.

**Recognized by name and by content.** `name_says` takes `bodyfile`,
UAC's `bodyfile.txt` and anything ending in `.body`, the names the tools
above leave (and an archive expansion's second copy, `bodyfile (2).txt`).
`looks_like` reads the first non-blank line and asks whether it parses,
for a body file under any other name. Like kvread.looks_like, it is only
asked about a file no reader claims by name (store.looks_like_bodyfile's
callers). merge.js has the twins of both for the import queue
(tests/ui/test_bodyfile_queue.py holds the two sides to one list).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Iterable, Iterator

from .iisread import FileChanged

__all__ = ["COLUMNS", "COLUMN_TYPES", "UNPARSED", "name_says", "looks_like",
           "is_line", "split_line", "epoch_text", "plan", "rows"]

COLUMNS = [
    "Modified (UTC)", "Accessed (UTC)", "Changed (UTC)", "Born (UTC)",
    "Name", "Size", "Mode", "UID", "GID", "Inode", "MD5",
]
COLUMN_TYPES = [
    "datetime", "datetime", "datetime", "datetime",
    "text", "number", "text", "number", "number", "text", "text",
]
UNPARSED = "Unparsed line"

# The names the tools leave: fls's usual `> bodyfile`, UAC's
# bodyfile/bodyfile.txt, Volatility's `--output-file=x.body`. " (2)" is
# the second copy an archive expansion keeps. Kept in step with
# BODYFILE_NAME in merge.js.
_NAME = re.compile(r"(?:bodyfile(?: \(\d+\))?(?:\.txt)?|.+\.body)\Z", re.I | re.A)

# One TSK 3.x line, as merge.js's twin (isBodyfileLine) reads it: the MD5
# (hex, often just "0"), the name, which may hold pipes, then nine fields
# none of which can. A time is whole seconds or seconds with a fraction,
# signed (a date before 1970 is a real, if odd, value), "-" or empty.
_HEX = re.compile(r"[0-9A-Fa-f]*")
_T = r"(?:-?\d+(?:\.\d+)?|-|)"
# The nine after the name: inode, mode, UID, GID, size, then the four times.
_TAIL = re.compile(rf"[^|]*\|[^|]*\|-?\d*\|-?\d*\|-?\d*\|{_T}\|{_T}\|{_T}\|{_T}", re.A)
# TSK 2.x: MD5|name|device|inode|mode_as_value|mode_as_string|num_of_links|
# UID|GID|rdev|size|atime|mtime|ctime|block_size|num_of_blocks. Only asked
# about a line that read as 3.x with a pipe in its name, which is where a
# 2.x line's extra fields end up. Kept in step with BODYFILE_TSK2 in merge.js.
_TSK2 = re.compile(r"[0-9A-Fa-f]*\|[^|]*\|[^|]*\|[^|]*\|\d+\|[^|]*[-rwxsStT]{9}\|\d+(?:\|-?\d+){9}", re.A)

_EPOCH = datetime(1970, 1, 1)
# time.gmtime is the fast path, up to the year 3000 that Windows' C
# library stops at; past it, and before 1970, datetime does the work.
_GMTIME_MAX = 32_503_680_000
# How many converted times split_line's cache holds before it starts over.
_TIME_CACHE = 1 << 16


def name_says(filename: str) -> bool:
    """Whether a file's name says it is a body file: bodyfile,
    bodyfile.txt, anything.body. Takes a bare name or a path."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return _NAME.match(base) is not None


def _fields(line: str) -> list[str] | None:
    """[md5, name, inode, mode, uid, gid, size, atime, mtime, ctime,
    crtime] for a TSK 3.x line, or None. Split rather than one regex: a
    pattern with the name as `(.*)` backtracks over every name, and a body
    file of a whole disk runs to millions of lines."""
    md5, sep, rest = line.partition("|")
    if not sep or (md5 != "0" and not _HEX.fullmatch(md5)):
        return None
    parts = rest.rsplit("|", 9)
    if len(parts) != 10 or not _TAIL.fullmatch(rest, len(parts[0]) + 1):
        return None
    if "|" in parts[0] and _TSK2.fullmatch(line):
        return None
    parts.insert(0, md5)
    return parts


def is_line(line: str) -> bool:
    """Whether one line (no line ending) is a TSK 3.x body file line."""
    return _fields(line) is not None


def looks_like(text: str) -> bool:
    """Whether the first non-blank line of `text` (the start of a file,
    decoded) is a body file line."""
    for line in text.split("\n", 64)[:64]:
        line = line.strip().lstrip("﻿")
        if line:
            return is_line(line)
    return False


def epoch_text(value: str) -> str:
    """One body file time as the canonical UTC text: "" for an unrecorded
    time (0, "-", empty), the value as written for one past datetime's
    range, else `YYYY-MM-DD HH:MM:SS`, plus `.ffffff` when the value had a
    fraction (truncated, never rounded, so it can't roll into the next
    second)."""
    if not value or value == "-":
        return ""
    neg = value[0] == "-"
    whole, _, frac = (value[1:] if neg else value).partition(".")
    sec = int(whole)
    usec = int((frac + "00000")[:6]) if frac else 0
    if sec == 0 and usec == 0 and not frac.strip("0"):
        return ""
    if not neg and sec < _GMTIME_MAX:
        t = time.gmtime(sec)
        out = (f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d} "
               f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}")
    else:
        total = sec * 1_000_000 + usec
        try:
            dt = _EPOCH + timedelta(microseconds=-total if neg else total)
        except OverflowError:
            return value
        if neg and usec:
            # -1.25 s is 1969-12-31 23:59:58.750000: the whole and the
            # fraction are both behind 1970, so the fraction printed is
            # what is left of the second, which datetime already worked out.
            usec = dt.microsecond
        out = (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
               f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return f"{out}.{usec:06d}" if frac else out


def split_line(line: str, times: dict | None = None) -> list[str] | None:
    """One body file line's values in COLUMNS order, or None when the line
    is not one. `times`, when given, caches epoch_text's answers: the
    files on one disk share a handful of install and update times, so most
    of a body file's times are ones it has already converted."""
    f = _fields(line)
    if f is None:
        return None
    md5, name, inode, mode, uid, gid, size, atime, mtime, ctime, crtime = f
    if times is None:
        return [epoch_text(mtime), epoch_text(atime), epoch_text(ctime), epoch_text(crtime),
                name, size, mode, uid, gid, inode, md5]
    out = []
    for v in (mtime, atime, ctime, crtime):
        t = times.get(v)
        if t is None:
            if len(times) >= _TIME_CACHE:
                times.clear()
            t = times[v] = epoch_text(v)
        out.append(t)
    out += (name, size, mode, uid, gid, inode, md5)
    return out


def plan(lines: Iterable[str]) -> dict:
    """Pass one (store._ingest_planned_lines): the columns are fixed, so
    all this pass decides is whether the file needs the Unparsed line
    column, and whether it holds a body file line at all. A file with none
    plans no columns, which the store refuses with a clear error."""
    good = bad = 0
    for line in lines:
        if is_line(line):
            good += 1
        else:
            bad += 1
    if not good:
        return {"columns": [], "types": [], "unparsed": bad}
    if bad:
        return {"columns": COLUMNS + [UNPARSED], "types": COLUMN_TYPES + ["text"], "unparsed": bad}
    return {"columns": list(COLUMNS), "types": list(COLUMN_TYPES), "unparsed": 0}


def rows(lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    """Pass two: a row per line, aligned to `plan`'s columns. A line that
    isn't a body file line is the Unparsed line column's, counted in
    `stats["unparsed"]`. One showing up in a file pass one found none in
    means the file changed between the passes (see iisread.FileChanged)."""
    keep = UNPARSED in plan["columns"]
    blank = [""] * len(COLUMNS)
    times: dict[str, str] = {}
    for line in lines:
        values = split_line(line, times)
        if values is None:
            if not keep:
                raise FileChanged()
            stats["unparsed"] = stats.get("unparsed", 0) + 1
            yield blank + [line]
        elif keep:
            values.append("")
            yield values
        else:
            yield values

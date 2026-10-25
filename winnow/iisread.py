r"""Reading IIS web server logs for import — recognised by file NAME, the
way IIS itself names them, and read into one table per file. Stdlib only;
imports timeparse and nothing else from the app.

IIS names a site's log after its format and its rollover period, and puts
it under inetpub\logs\LogFiles\W3SVC<site id>\ (FTPSVC<n> for FTP):

    u_ex240501.log     W3C extended, one file a day (u_: written as UTF-8)
    ex24050113.log     ... an hour (YYMMDDHH)
    ex240502.log       ... a week (YYMMWW, the week of the month)
    ex2405.log         ... a month (YYMM)
    u_extend1.log      ... rolled over by size, or never (a sequence number)
    nc240501.log       NCSA common log format (ncsa1.log by size)
    in240501.log       IIS native format (inetsv1.log by size)

Nothing else in a triage tree is named like that, so the name is what
routes a file here (server._ingest_kind_for_path, importKindFor in
merge.js, the folder scan) — a .log no reader claims is raw text, and an
IIS log read that way is one opaque Message column. The format comes from
the name too: the content of a W3C file says W3C, but an NCSA or native
line has no marker that a sniffer could tell from any other access log.

**W3C is read from its #Fields: directive, never a fixed column list.**
Which fields a site logs is its own configuration, and the directive can
change partway through a file: IIS writes a fresh header block every time
the site restarts, and an administrator who adds a field gets the new set
from that point on. So the reader works like JSON ingest's two passes —
`plan` walks the file once collecting every field any #Fields: line names
(the union, first-seen order, matched case-insensitively), and `rows`
reads each data line against whichever directive is in effect where the
line sits, leaving the fields that header doesn't name blank. A data line
with more values than its header names (or one before any header at all)
keeps the surplus in an "Unnamed values" column, which exists only when a
file needs it; it and a short line are counted as ragged.

Values are kept exactly as logged: IIS writes "-" for an empty field and
"+" for a space inside one (cs(User-Agent)), and both stay — the file
cannot tell a literal "-" or "+" from the substitution either, so decoding
them would only invent certainty. One column is added in front, the
timestamp an analyst sorts and filters by (W3C's date and time are always
UTC; NCSA's offset is applied; native format is server local time and is
labelled so). The raw date and time columns stay beside it.

**A run of NUL bytes is a gap, not a line.** A server that loses power
mid-write leaves its log padded with zeros where the unflushed tail was,
and IIS carries on writing after them when it comes back — so a NUL run
can sit at the start of a line or in the middle of one, glued to the
header block of the restart. The text either side of a run is read as
separate lines and the run itself is dropped (and counted: an unclean
stop is worth knowing about).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Iterator

from . import timeparse

FORMATS = {"w3c": "W3C extended", "ncsa": "NCSA common", "native": "IIS native"}

# u_ is IIS's "this log is UTF-8" prefix. The dated stems are YYMM (monthly),
# YYMMDD (daily, and weekly's YYMMWW), YYMMDDHH (hourly); the by-size ones a
# sequence number. " (2)" is archive.py's name for a second member with the
# same name — still the same kind of file.
_NAME_RE = re.compile(
    r"^(?:u_)?(?:(?P<dated>ex|nc|in)(?P<stamp>\d{4}|\d{6}|\d{8})|(?P<sized>extend|ncsa|inetsv)\d+)"
    r"(?: \(\d+\))?\.log$",
    re.IGNORECASE,
)
_FORMAT_OF = {"ex": "w3c", "extend": "w3c", "nc": "ncsa", "ncsa": "ncsa", "in": "native", "inetsv": "native"}

TIMESTAMP_UTC = "Timestamp (UTC)"
TIMESTAMP_LOCAL = "Timestamp (local)"
UNNAMED_VALUES = "Unnamed values"
UNPARSED_LINE = "Unparsed line"

# NCSA common log format as IIS writes it — no referer or user agent (that
# is the "combined" format, which IIS does not write).
NCSA_COLUMNS = ["Remote host", "Remote log name", "User name", "Timestamp (as logged)",
                "Request", "Status", "Bytes"]
# The fifteen fields of IIS native format, named as Microsoft's docs name them.
NATIVE_COLUMNS = ["Client IP address", "User name", "Date", "Time", "Service and instance",
                  "Server name", "Server IP address", "Time taken", "Client bytes sent",
                  "Server bytes sent", "Service status code", "Windows status code",
                  "Request type", "Target of operation", "Parameters"]


def log_format(path: str) -> str | None:
    """"w3c", "ncsa" or "native" when the file is named like an IIS log,
    else None. Only the name is read: this runs on every file a folder
    scan walks. A dated stem has to be a date (month 01-12, day 01-31,
    hour 00-23), so "in1234.log" is not mistaken for one."""
    base = re.split(r"[\\/]", path)[-1]
    m = _NAME_RE.match(base)
    if not m:
        return None
    stamp = m.group("stamp")
    if stamp and not _plausible_stamp(stamp):
        return None
    return _FORMAT_OF[(m.group("dated") or m.group("sized")).lower()]


def _plausible_stamp(stamp: str) -> bool:
    month = int(stamp[2:4])
    if not 1 <= month <= 12:
        return False
    if len(stamp) >= 6 and not 1 <= int(stamp[4:6]) <= 31:
        return False
    return len(stamp) < 8 or int(stamp[6:8]) <= 23


_NUL_RUN = re.compile("\x00+")


def logical_lines(lines: Iterable[str], stats: dict) -> Iterator[str]:
    r"""The lines of a log read with newline="\n", without their line
    ending (one \r before the \n, as store._text_line), split at NUL runs
    (module docstring), blank ones dropped. `stats["nul_lines"]` counts
    the physical lines that held NULs."""
    for raw in lines:
        line = raw[:-1] if raw.endswith("\n") else raw
        if line.endswith("\r"):
            line = line[:-1]
        if "\x00" in line:
            stats["nul_lines"] = stats.get("nul_lines", 0) + 1
            for piece in _NUL_RUN.split(line):
                if piece and not piece.isspace():
                    yield piece
        elif line and not line.isspace():
            yield line


class FileChanged(ValueError):
    """Pass two met a line pass one did not plan for. The caller reads the
    same number of lines both times, so only a file still being written
    (a live server's current log, imported in place) gets here — through
    its last line, which grew between the passes."""

    def __init__(self):
        super().__init__("the file changed while it was being read (is the server still writing "
                         "it?) — import a copy, or import it again once it has rolled over")


# ---------------------------------------------------------------- W3C

def _directive_fields(line: str) -> list[str] | None:
    """The field names a "#Fields:" directive declares, or None for any
    other directive (#Software, #Version, #Date, #Remark)."""
    if line[:8].lower() != "#fields:":
        return None
    return line[8:].split()


def _field_keys(fields: list[str]) -> list[str]:
    """Case-insensitive keys for one directive's fields. A name repeated
    within the one directive gets _1, _2 (sanitize_columns' suffix), so two
    values never land in one column."""
    keys: list[str] = []
    for f in fields:
        key, n = f.lower(), 0
        while key in keys:
            n += 1
            key = f"{f.lower()}_{n}"
        keys.append(key)
    return keys


def _w3c_plan(lines: Iterable[str]) -> dict:
    names: dict[str, str] = {}        # key -> the spelling first seen, in first-seen order
    timestamp = overflow = False
    active: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            fields = _directive_fields(line)
            if fields is not None:
                active = fields
                keys = _field_keys(fields)
                for key, f in zip(keys, fields):
                    names.setdefault(key, f if key == f.lower() else key)
                timestamp = timestamp or ("date" in keys and "time" in keys)
            continue
        if active is None or line.count(" ") >= len(active):
            overflow = True
    columns = ([TIMESTAMP_UTC] if timestamp else []) + list(names.values())
    types: list[str | None] = (["datetime"] if timestamp else []) + [None] * len(names)
    if overflow:
        columns.append(UNNAMED_VALUES)
        types.append("text")
    return {"format": "w3c", "columns": columns, "types": types, "keys": list(names),
            "timestamp": timestamp, "overflow": overflow}


_W3C_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?")


def _w3c_timestamp(date: str, clock: str) -> str:
    """date + time -> the canonical "YYYY-MM-DD HH:MM:SS[.ffffff]"
    (timeparse's shape), or "" when either is not what IIS writes."""
    stamp = f"{date} {clock}"
    m = _W3C_STAMP.fullmatch(stamp)
    if not m:
        return ""
    frac = m.group(1)
    return stamp if not frac or len(frac) == 6 else stamp[:20] + frac.ljust(6, "0")


def _w3c_rows(lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    offset = 1 if plan["timestamp"] else 0
    position = {key: offset + i for i, key in enumerate(plan["keys"])}
    width = len(plan["columns"])
    unnamed = width - 1 if plan["overflow"] else None
    slots: list[int] = []
    # For a line with exactly the header's count of values — nearly every
    # line — the row is built in one step: `gather` holds, per column, the
    # value's index in the line (-1 for a column this header lacks), and
    # `direct` is the common case where that is simply the line in order.
    gather: list[int] = []
    direct = False
    date_i = time_i = None
    for line in lines:
        if line.startswith("#"):
            fields = _directive_fields(line)
            if fields is not None:
                keys = _field_keys(fields)
                if any(k not in position for k in keys):
                    raise FileChanged()
                slots = [position[k] for k in keys]
                where = {slot: i for i, slot in enumerate(slots)}
                gather = [where.get(c, -1) for c in range(width)]
                direct = slots == list(range(offset, offset + len(slots)))
                date_i = keys.index("date") if "date" in keys else None
                time_i = keys.index("time") if "time" in keys else None
            continue
        values = line.split(" ")
        n = len(values)
        if n == len(slots):
            if direct:
                row = ([""] if offset else []) + values + [""] * (width - offset - n)
            else:
                row = [values[g] if g >= 0 else "" for g in gather]
            if offset and date_i is not None and time_i is not None:
                row[0] = _w3c_timestamp(values[date_i], values[time_i])
            yield row
            continue
        # More values than the header names, or fewer: ragged.
        stats["ragged"] = stats.get("ragged", 0) + 1
        row = [""] * width
        for slot, value in zip(slots, values):
            row[slot] = value
        if n > len(slots):
            if unnamed is None:
                raise FileChanged()
            row[unnamed] = " ".join(values[len(slots):])
        if offset and date_i is not None and time_i is not None and max(date_i, time_i) < n:
            row[0] = _w3c_timestamp(values[date_i], values[time_i])
        yield row


# --------------------------------------------- NCSA and native: fixed layouts

_NCSA_RE = re.compile(r'^(\S+) (\S+) (.*?) \[([^\]]*)\] "(.*)" (\S+) (\S+)$')
_CLF = timeparse.OPERATIONS["apache_clf"]


def _ncsa_values(line: str) -> list[str] | None:
    m = _NCSA_RE.match(line)
    return list(m.groups()) if m else None


def _ncsa_timestamp(values: list[str]) -> str:
    return timeparse.parse_or_none(_CLF, values[3], {}, {}) or ""


def _native_values(line: str) -> list[str] | None:
    """The fifteen comma-space separated fields, the trailing comma IIS
    ends every line with dropped. Split from the left for the first
    thirteen and from the right for the last, so a ", " inside the
    target of the operation (a URL may hold one) keeps it in one piece."""
    s = line.rstrip()
    if s.endswith(","):
        s = s[:-1]
    head = s.split(", ", 13)
    if len(head) != 14:
        return None
    target, sep, params = head[13].rpartition(", ")
    if not sep:
        return None
    return head[:13] + [target, params]


_NATIVE_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_NATIVE_TIME = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")


def _native_timestamp(values: list[str]) -> str:
    """M/D/YY(YY) and H:MM:SS, as the server's clock read them — no
    offset is logged, so the value is left in local time, not guessed
    into UTC (timeparse's no-TZ-math rule)."""
    d, t = _NATIVE_DATE.match(values[2]), _NATIVE_TIME.match(values[3])
    if not d or not t:
        return ""
    year = int(d.group(3))
    if year < 100:
        year += 1900 if year >= 70 else 2000
    try:
        dt = datetime(year, int(d.group(1)), int(d.group(2)),
                      int(t.group(1)), int(t.group(2)), int(t.group(3)))
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_FIXED = {
    # format: (timestamp column, field columns, split one line, its timestamp)
    "ncsa": (TIMESTAMP_UTC, NCSA_COLUMNS, _ncsa_values, _ncsa_timestamp),
    "native": (TIMESTAMP_LOCAL, NATIVE_COLUMNS, _native_values, _native_timestamp),
}


def _fixed_plan(fmt: str, lines: Iterable[str]) -> dict:
    ts_col, cols, split, _ = _FIXED[fmt]
    overflow = False
    for line in lines:   # every line read, not any(): the caller counts them
        if not overflow and split(line) is None:
            overflow = True
    columns = [ts_col, *cols] + ([UNPARSED_LINE] if overflow else [])
    types: list[str | None] = ["datetime"] + [None] * len(cols) + (["text"] if overflow else [])
    if fmt == "native":
        # The raw date ("3/20/2024") would otherwise be typed datetime too,
        # and offered beside the real timestamp everywhere one is picked.
        types[1 + cols.index("Date")] = "text"
    return {"format": fmt, "columns": columns, "types": types, "overflow": overflow}


def _fixed_rows(fmt: str, lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    _, cols, split, stamp = _FIXED[fmt]
    width = len(plan["columns"])
    for line in lines:
        values = split(line)
        if values is None:
            # A line that isn't in the format is kept whole rather than
            # forced into the wrong columns.
            if not plan["overflow"]:
                raise FileChanged()
            stats["ragged"] = stats.get("ragged", 0) + 1
            row = [""] * width
            row[-1] = line
            yield row
            continue
        row = [stamp(values), *values]
        if plan["overflow"]:
            row.append("")
        yield row


# ---------------------------------------------------------------- the API

def plan(fmt: str, lines: Iterable[str]) -> dict:
    """Pass one over a file's logical lines: the table it makes. `columns`
    (and `types` beside them — None means "infer from the rows") is the
    column list for every row `rows` will yield."""
    if fmt == "w3c":
        return _w3c_plan(lines)
    if fmt in _FIXED:
        return _fixed_plan(fmt, lines)
    raise ValueError(f"Unknown IIS log format: {fmt}")


def rows(lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    """Pass two: one row per data line, aligned to plan["columns"].
    Directive lines make no rows. `stats["ragged"]` counts the lines whose
    values did not match the fields in effect."""
    if plan["format"] == "w3c":
        return _w3c_rows(lines, plan, stats)
    return _fixed_rows(plan["format"], lines, plan, stats)

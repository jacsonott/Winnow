r"""Reading key=value logs for import — the format every Fortinet product
writes (FortiGate, FortiAnalyzer, FortiMail, FortiWeb), one event a line,
each field a name=value pair:

    date=2026-10-23 time=05:39:00 devname="Main-Firewall" devid="FG6H1E..."
    logid="0000000013" type="traffic" subtype="forward" srcip=192.168.1.50
    srcport=54705 srcintf="internal" dstip=8.8.8.8 dstport=53 action="accept"

(one line in the file). Stdlib only; imports timeparse, and iisread for
its line splitting, and nothing else from the app.

**Recognised by content, not by name.** A FortiGate log has no name of its
own — `disk-traffic-forward-2026_10_23.log` from the GUI, whatever a
syslog server called it, a `.txt` someone saved — so `looks_like` reads
the first line: its pairs start date=YYYY-MM-DD, then time=HH:MM:SS, then
another pair. That is the one thing every Fortinet log line does, and no
other text file does by accident. It only runs on a file nothing else
claims by name (store.looks_like_kv_log's callers), and it reads the
first line only, so a log whose first line is something else is read
this way by picking it (Read as → Key=value).

**The columns are the keys, the union over the whole file.** A traffic
line, an event line and a UTM line carry different fields, so like JSON
ingest and IIS's W3C there are two passes: `plan` collects every key in
first-seen order, `rows` puts each line's values under its own keys and
leaves the rest blank. Keys match case-insensitively (SQLite's column
names do), and a key repeated within one line gets _1, _2, so two values
never land in one column. Past MAX_COLUMNS keys the rest fold into one
JSON cell per row, as JSON ingest does.

**Quoting.** A value in double quotes can hold spaces, "=" and commas; the
quotes come off. A quote inside one is written \" (and read as a quote),
or left bare, so a quote only ends a value where what follows is another
key= or the end of the line: msg="said "hi" twice" is one value. A line
that reads cleanly as pairs, no quote inside any quoted value — nearly
every line — is read in two regex passes; only one that doesn't goes
through the careful reading. FortiOS can also separate the pairs with
commas (`set format csv`); a line whose first two pairs are joined by a
comma is read that way. Values are otherwise kept as logged.

**What isn't a pair is kept, not dropped.** Text before the first pair — a
syslog server's own header, `<189>` or `Oct 23 05:39:00 10.0.0.1` — goes
to a `Line prefix` column. A word after the first pair that isn't one,
or a line with no pairs at all, goes to `Unparsed text`, and the line
counts as ragged (so does a quote that never closes, whose value runs to
the end of the line). Both columns exist only when a file needs them.

**The timestamp.** date and time are joined into the first column, so the
Timeframe filter, Histogram and Timeline work straight away; both stay as
columns too. FortiOS 6.2 and later also log tz="+0200": when every line
with a date and time carries one, the column is `Timestamp (UTC)`, the
offset applied. When not, it is `Timestamp (local)`, the device's clock as
logged and not guessed into UTC (timeparse's no-TZ-math rule) — one column,
one meaning, so a file with tz on only some lines is local throughout.
eventtime stays as logged (an epoch whose unit has changed between FortiOS
releases); a derived Unix epoch column reads it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Iterable, Iterator

from . import timeparse
from .iisread import FileChanged, logical_lines

__all__ = ["FileChanged", "logical_lines", "looks_like", "split_line", "timestamp", "plan", "rows"]

TIMESTAMP_UTC = "Timestamp (UTC)"
TIMESTAMP_LOCAL = "Timestamp (local)"
LINE_PREFIX = "Line prefix"
UNPARSED_TEXT = "Unparsed text"
OTHER_FIELDS = "Other fields (JSON)"
# store.JSON_MAX_COLUMNS' reasoning: SQLite refuses a table past 2,000
# columns, and a derived-column sidecar joins its own onto these.
MAX_COLUMNS = 1000
# Values that read as numbers and are not quantities: "-0500" is an offset.
TEXT_KEYS = {"tz"}

# The first line of a Fortinet log, after an optional syslog priority and
# any words before the pairs. Kept in step with KV_LOG_LINE in merge.js,
# which the import queue tests an uploaded file's first line with
# (tests/ui/test_kv_queue.py runs one list of lines through both).
_RECOGNISE = re.compile(
    r'(?:<\d{1,3}>)?(?:[^\s=]+\s+)*?date="?\d{4}-\d{2}-\d{2}"?[\s,]+'
    r'time="?\d{2}:\d{2}:\d{2}(?:\.\d+)?"?[\s,]+[^\s=",]+=')

_KEY = r'[^\s=",]+'
_PRIORITY = re.compile(r"<\d{1,3}>")
# Where the pairs start: the first word that is a key=, at the start of
# the line, after whitespace, or straight after a syslog <priority>.
_FIRST_KEY = re.compile(r'(?:^|(?<=[\s>]))' + _KEY + '=')
_COMMA_JOINED = re.compile(_KEY + r'=(?:"[^"]*"|[^\s",]*)\s*,\s*' + _KEY + '=')

# The clean line: every pair key=value or key="value", no quote inside a
# quoted value. Each alternative starts with a different character and a
# key cannot hold "=", so a line that doesn't match fails in linear time.
_SPACE_LINE = re.compile(r'(?:' + _KEY + r'=(?:"[^"]*"|[^\s"]*)(?:\s+|$))+')
_SPACE_PAIR = re.compile(r'(' + _KEY + r')=(?:"([^"]*)"|([^\s"]*))')
_COMMA_LINE = re.compile(r'(?:' + _KEY + r'=(?:"[^"]*"|[^\s",]*)\s*(?:,\s*|$))+')
_COMMA_PAIR = re.compile(r'(' + _KEY + r')=(?:"([^"]*)"|([^\s",]*))')

# The careful reading's pieces.
_KEY_AT = re.compile(r'(' + _KEY + r')=')
_BARE = {False: re.compile(r'\S*'), True: re.compile(r'[^\s,]*')}
_WORD = {False: re.compile(r'\S+'), True: re.compile(r'[^\s,]+')}
# A quote closes its value where the line ends or the next pair starts.
_CLOSES = {False: re.compile(r'\s*$|\s+' + _KEY + '='),
           True: re.compile(r'\s*,?\s*$|\s*,\s*' + _KEY + '=')}


def looks_like(text: str) -> bool:
    """Whether the first non-blank line of `text` (the start of a file,
    decoded) reads as a Fortinet key=value log line."""
    for line in text.split("\n", 64)[:64]:
        line = line.strip()
        if line:
            return _RECOGNISE.match(line) is not None
    return False


def _close_quote(line: str, opening: int, comma: bool, escapes: bool) -> int | None:
    """The index of the quote that closes the value opened at `opening`,
    or None. With `escapes`, a quote after an odd run of backslashes is
    part of the value."""
    closes = _CLOSES[comma]
    j = opening + 1
    while True:
        q = line.find('"', j)
        if q < 0:
            return None
        j = q + 1
        if escapes:
            k = q - 1
            while k > opening and line[k] == "\\":
                k -= 1
            if (q - 1 - k) % 2:
                continue
        if closes.match(line, j):
            return q


def _careful(line: str, i: int, comma: bool) -> tuple[list[str], list[str], list[str], bool]:
    """(keys, values, stray words, unterminated) for a line the clean
    regex refused: a quote inside a quoted value, a word that isn't a
    pair, a quote that never closes."""
    n = len(line)
    bare, word = _BARE[comma], _WORD[comma]
    keys: list[str] = []
    values: list[str] = []
    stray: list[str] = []
    unterminated = False
    while True:
        while i < n and (line[i].isspace() or (comma and line[i] == ",")):
            i += 1
        if i >= n:
            break
        m = _KEY_AT.match(line, i)
        if m is None:
            w = word.match(line, i)
            stray.append(w.group())
            i = w.end()
            continue
        keys.append(m.group(1))
        i = m.end()
        if i < n and line[i] == '"':
            end = _close_quote(line, i, comma, escapes=True)
            if end is not None:
                value = line[i + 1:end]
                if '\\"' in value:
                    value = value.replace('\\"', '"')
            else:
                # Read without escapes: "C:\dir\" ends in a backslash
                # that escapes nothing.
                end = _close_quote(line, i, comma, escapes=False)
                if end is not None:
                    value = line[i + 1:end]
                else:
                    value, end, unterminated = line[i + 1:], n - 1, True
            values.append(value)
            i = end + 1
        else:
            b = bare.match(line, i)
            values.append(b.group())
            i = b.end()
    return keys, values, stray, unterminated


def split_line(line: str) -> tuple[str, tuple[str, ...], list[str], list[str], bool]:
    """One logical line -> (prefix, keys, values, stray words, ragged).
    `prefix` is the text before the first pair; a line with no pairs is
    all stray. `ragged` is whether anything on it wasn't a clean pair."""
    pri = _PRIORITY.match(line)
    m = _FIRST_KEY.search(line, pri.end() if pri else 0)
    if m is None:
        return "", (), [], [line.strip()], True
    start = m.start()
    prefix = line[:start].strip()
    comma = _COMMA_JOINED.match(line, start) is not None
    if (_COMMA_LINE if comma else _SPACE_LINE).fullmatch(line, start):
        found = (_COMMA_PAIR if comma else _SPACE_PAIR).findall(line, start)
        return prefix, tuple([f[0] for f in found]), [f[1] or f[2] for f in found], [], False
    keys, values, stray, unterminated = _careful(line, start, comma)
    return prefix, tuple(keys), values, stray, bool(stray) or unterminated


class _Splitter:
    """split_line for a whole pass, faster on the lines it has met before.

    A Fortinet file repeats a few dozen key sequences (one per log type,
    plus the optional fields), and a regex spelling out one sequence's
    keys reads a line in one match, where the clean-line check and the
    pair scan take two and cost three times as much. So each clean,
    space-separated line split_line reads teaches one: its keys, literal,
    and each value quoted or not as it was there. Kept under (count of
    "=", count of quotes) in the line, which every line of that shape
    shares unless a value holds one, and tried before split_line. A
    match is exactly the line split_line would read the same way, so the
    answer is the same either way; a line that matches none is read by
    split_line. Bounded, so a file whose every line differs costs a
    lookup, not a compile, per line."""

    PER_KEY = 4        # sequences tried for one (=, ") count
    LEARN = 512        # sequences compiled in a pass

    def __init__(self):
        self.known: dict[tuple[int, int], list[tuple[tuple[str, ...], re.Pattern]]] = {}
        self.learned = 0

    def __call__(self, line: str) -> tuple[str, tuple[str, ...], list[str], list[str], bool]:
        pri = _PRIORITY.match(line)
        m = _FIRST_KEY.search(line, pri.end() if pri else 0)
        if m is None or _COMMA_JOINED.match(line, m.start()) is not None:
            return split_line(line)
        start = m.start()
        sig = (line.count("="), line.count('"'))
        tried = self.known.get(sig)
        if tried:
            for i, (keys, rx) in enumerate(tried):
                hit = rx.fullmatch(line, start)
                if hit is not None:
                    if i:
                        tried.insert(0, tried.pop(i))
                    return line[:start].strip(), keys, list(hit.groups()), [], False
        split = split_line(line)
        if not split[4] and self.learned < self.LEARN:
            self._learn(line, start, sig, split[1])
        return split

    def _learn(self, line: str, start: int, sig: tuple[int, int], keys: tuple[str, ...]) -> None:
        found = list(_SPACE_PAIR.finditer(line, start))
        if tuple(m.group(1) for m in found) != keys:
            return      # split_line read it the careful way: not a shape to keep
        parts = [re.escape(k) + ('="([^"]*)"' if m.start(2) >= 0 else r'=([^\s"]*)') for k, m in zip(keys, found)]
        rx = re.compile(r"\s+".join(parts) + r"\s*")
        tried = self.known.setdefault(sig, [])
        tried.insert(0, (keys, rx))
        del tried[self.PER_KEY:]
        self.learned += 1


def _column_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    """Case-insensitive column keys for one line's keys, a repeat within
    the line suffixed _1, _2 (sanitize_columns' suffix)."""
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        low = key = k.lower()
        n = 0
        while key in seen:
            n += 1
            key = f"{low}_{n}"
        seen.add(key)
        out.append(key)
    return tuple(out)


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CLOCK = re.compile(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?")
_TZ = re.compile(r"[+-]\d{2}:?\d{2}")
_ISO = timeparse.OPERATIONS["iso8601"]


def timestamp(date: str, clock: str, tz: str | None = None) -> str:
    """date + time (+ tz) -> the canonical "YYYY-MM-DD HH:MM:SS[.ffffff]",
    in UTC when `tz` is given, or "" when they aren't a real date and time
    in the form Fortinet writes."""
    if not (_DATE.fullmatch(date) and _CLOCK.fullmatch(clock)):
        return ""
    if tz is not None and not _TZ.fullmatch(tz):
        return ""
    if len(clock) == 8:
        # Whole seconds, which is every Fortinet line: read here rather
        # than through timeparse's general ISO parse, a third of the
        # import's time on a log with no two lines in one second. The
        # answer is the same (tests/test_kv_logs.py holds the two to it).
        try:
            dt = datetime(int(date[:4]), int(date[5:7]), int(date[8:]),
                          int(clock[:2]), int(clock[3:5]), int(clock[6:]))
            if tz:
                hours, minutes = int(tz[1:3]), int(tz[-2:])
                if hours > 14 or minutes > 59:
                    return ""
                shift = timedelta(hours=hours, minutes=minutes)
                dt = dt - shift if tz[0] == "+" else dt + shift
        except (ValueError, OverflowError):
            return ""
        return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
                f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return timeparse.parse_or_none(_ISO, f"{date} {clock}{tz or ''}", {}, {}) or ""


# Distinct key sequences remembered per pass. A Fortinet file has a few
# dozen (one per log type and subtype); this only bounds a pathological one.
_SHAPE_CACHE = 4096


def _shape(keys: tuple[str, ...]) -> tuple[tuple[str, ...], int | None, int | None, int | None]:
    """(column keys, index of date, of time, of tz) for one line's keys."""
    ck = _column_keys(keys)
    where = {c: i for i, c in enumerate(ck)}
    return ck, where.get("date"), where.get("time"), where.get("tz")


def plan(lines: Iterable[str]) -> dict:
    """Pass one over a file's logical lines: the table it makes. `columns`
    (and `types` beside them — None means "infer from the rows") is the
    column list for every row `rows` will yield, and is empty when no line
    holds a pair."""
    names: dict[str, str] = {}          # column key -> the spelling first seen
    shapes: dict[tuple, tuple] = {}
    prefix = unparsed = dated = False
    zoned = True
    split = _Splitter()
    for line in lines:
        pre, keys, values, stray, _ = split(line)
        prefix = prefix or bool(pre)
        unparsed = unparsed or bool(stray)
        if not keys:
            continue
        shape = shapes.get(keys)
        if shape is None:
            if len(shapes) >= _SHAPE_CACHE:
                shapes.clear()
            shape = shapes[keys] = _shape(keys)
            for c, k in zip(shape[0], keys):
                names.setdefault(c, k if c == k.lower() else c)
        _, di, ti, zi = shape
        if di is not None and ti is not None and _DATE.fullmatch(values[di]) and _CLOCK.fullmatch(values[ti]):
            dated = True
            if zoned and (zi is None or not _TZ.fullmatch(values[zi])):
                zoned = False
    if not names:     # not one pair: not a key=value log, however much prose
        prefix = unparsed = False
    stamp = [TIMESTAMP_UTC if zoned else TIMESTAMP_LOCAL] if dated else []
    extra = ([LINE_PREFIX] if prefix else []) + ([UNPARSED_TEXT] if unparsed else [])
    keys = list(names)
    room = MAX_COLUMNS - len(stamp) - len(extra)
    overflow = len(keys) > room
    if overflow:
        keys = keys[:room - 1]
    columns = stamp + [names[k] for k in keys] + extra + ([OTHER_FIELDS] if overflow else [])
    types: list[str | None] = (["datetime"] * len(stamp) + [("text" if k in TEXT_KEYS else None) for k in keys]
                               + ["text"] * (len(extra) + overflow))
    return {"columns": columns, "types": types, "keys": keys, "timestamp": dated,
            "utc": dated and zoned, "prefix": prefix, "unparsed": unparsed, "overflow": overflow}


def rows(lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    """Pass two: one row per logical line, aligned to plan["columns"].
    `stats["ragged"]` counts the lines that weren't all clean pairs."""
    offset = 1 if plan["timestamp"] else 0
    position = {k: offset + i for i, k in enumerate(plan["keys"])}
    width = len(plan["columns"])
    at = offset + len(plan["keys"])
    prefix_at = at if plan["prefix"] else None
    at += plan["prefix"]
    unparsed_at = at if plan["unparsed"] else None
    other_at = width - 1 if plan["overflow"] else None
    utc = plan["utc"]
    shapes: dict[tuple, tuple] = {}
    last_stamp: tuple = ()
    split = _Splitter()
    for line in lines:
        pre, keys, values, stray, ragged = split(line)
        row = [""] * width
        if ragged:
            stats["ragged"] = stats.get("ragged", 0) + 1
        if pre:
            if prefix_at is None:
                raise FileChanged()
            row[prefix_at] = pre
        if stray:
            if unparsed_at is None:
                raise FileChanged()
            row[unparsed_at] = " ".join(stray)
        if keys:
            shape = shapes.get(keys)
            if shape is None:
                if len(shapes) >= _SHAPE_CACHE:
                    shapes.clear()
                ck, di, ti, zi = _shape(keys)
                slots = tuple(position.get(c) for c in ck)
                if other_at is None and None in slots:
                    raise FileChanged()
                shape = shapes[keys] = (slots, di, ti, zi)
            slots, di, ti, zi = shape
            other = None
            for slot, k, v in zip(slots, keys, values):
                if slot is not None:
                    row[slot] = v
                else:
                    other = other or {}
                    other[k if k not in other else f"{k}_{len(other)}"] = v
            if other:
                row[other_at] = json.dumps(other, ensure_ascii=False)
            if offset and di is not None and ti is not None:
                # Lines a second apart share their date and time; parse once.
                # A UTC column never takes a local time: no tz is no value.
                zone = (values[zi] if zi is not None else "") if utc else None
                when = (values[di], values[ti], zone)
                if when != last_stamp[:3]:
                    last_stamp = (*when, timestamp(*when))
                row[0] = last_stamp[3]
        yield row

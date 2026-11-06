"""Reading Linux login records — wtmp, btmp and utmp — for import.
Stdlib only (struct, ipaddress, datetime); imports nothing from the app,
so the airgap rule holds and store.py can import it without a cycle.

The three files share one binary format, glibc's `struct utmp`: fixed-size
records written back to back, with no header, no magic number and no
record count. What differs is what writes them:

- **wtmp** (`/var/log/wtmp`, read by `last`): every login and logout, boot,
  shutdown, runlevel change and clock change, appended forever.
- **btmp** (`/var/log/btmp`, read by `lastb`): failed logins. The user name
  in a record here is whatever the other side typed — attacker-chosen text.
- **utmp** (`/var/run/utmp`, read by `who`): who is logged in now, one slot
  per terminal, reused in place.

**Which layout a file is in is read off its bytes, never assumed.** The
struct is the same everywhere except two things a reader has to get right:

- *Byte order* — the machine's. x86, ARM and ppc64le files are little-endian;
  PowerPC, SPARC, MIPS and s390x big-endian.
- *Record size* — glibc's `__WORDSIZE_TIME64_COMPAT32`. Where it is 1 (x86-64,
  ppc64, sparc64, mips64, RISC-V, and every 32-bit target) `ut_session` and
  `ut_tv` are 32-bit and a record is 384 bytes, so a file reads the same on a
  32- and a 64-bit machine. Where it is 0 (64-bit ARM, s390x) they are a
  `long` and a `struct timeval`: 64-bit, and a record is 400 bytes. A reader
  that knows only 384 reads an ARM64 server's wtmp as garbage from the
  second record on.

`detect` tries all four (two sizes x two byte orders) over the file's first
records and keeps the one under which they read as login records: a type
glibc defines, a PID the kernel could hand out, a time between 1980 and
2100 with fewer than a million microseconds. A misread record fails those
almost at once — a little-endian USER_PROCESS (07 00) read big-endian is
type 1792 — so the scores are far apart in practice.

Every record becomes a row, EMPTY slots included, so rid N is record N and
`Offset` is where it starts in the file. A tail too short for a whole
record (a write cut off by a crash) is left out and counted, never padded
into a row that would read as a real one. Text fields are C strings cut at
their first NUL; a byte that isn't UTF-8 is kept as a \\xNN escape, since a
btmp user name is whatever bytes the remote side sent.
"""

from __future__ import annotations

import datetime
import io
import ipaddress
import re
import struct
from typing import BinaryIO, Iterator, NamedTuple

COLUMNS = [
    "Timestamp (UTC)", "Type", "User", "Terminal", "Host", "IP address",
    "PID", "ID", "Session", "Exit status", "Termination status", "Offset",
]
COLUMN_TYPES = [
    "datetime", "text", "text", "text", "text", "text",
    "number", "text", "number", "number", "number", "number",
]

# ut_type, as glibc's <bits/utmp.h> names them — the names utmpdump and
# every write-up use, so an analyst can filter on what they already know.
TYPE_NAMES = {
    0: "EMPTY", 1: "RUN_LVL", 2: "BOOT_TIME", 3: "NEW_TIME", 4: "OLD_TIME",
    5: "INIT_PROCESS", 6: "LOGIN_PROCESS", 7: "USER_PROCESS", 8: "DEAD_PROCESS",
    9: "ACCOUNTING",
}


class Layout(NamedTuple):
    key: str        # recorded in the ingest record, so a session re-import reads the same way
    size: int       # bytes per record
    order: str      # struct byte-order character
    wide: bool      # 64-bit ut_session and ut_tv
    label: str      # what the import pane and the table's hover text say


# struct utmp, field by field: ut_type (h) and its 2 bytes of padding,
# ut_pid (i), ut_line[32], ut_id[4], ut_user[32], ut_host[256], ut_exit
# (e_termination, e_exit: h h), then ut_session and ut_tv — 32-bit (i, I
# unsigned seconds as glibc now declares them, i) or 64-bit (q q q) — then
# ut_addr_v6 as 16 raw bytes (network order whatever the machine's) and
# 20 unused bytes; the 400-byte form pads to its 8-byte alignment.
_NARROW = "hxxi32s4s32s256shhiIi16s20x"
_WIDE = "hxxi32s4s32s256shhqqq16s20x4x"

# Most common first: the order breaks a tie (an all-EMPTY file says nothing).
LAYOUTS = (
    Layout("384-le", 384, "<", False,
           "384-byte records, little-endian (x86, x86-64, 32-bit ARM, RISC-V, ppc64le)"),
    Layout("400-le", 400, "<", True, "400-byte records, little-endian (64-bit ARM)"),
    Layout("384-be", 384, ">", False, "384-byte records, big-endian (PowerPC, SPARC, MIPS)"),
    Layout("400-be", 400, ">", True, "400-byte records, big-endian (s390x)"),
)
LAYOUTS_BY_KEY = {lay.key: lay for lay in LAYOUTS}
_STRUCTS = {lay.key: struct.Struct(lay.order + (_WIDE if lay.wide else _NARROW)) for lay in LAYOUTS}
for _lay in LAYOUTS:
    assert _STRUCTS[_lay.key].size == _lay.size, _lay

MIN_RECORD = min(lay.size for lay in LAYOUTS)
# How many records detect() judges. A layout is settled within a record or
# two; 64 is headroom for a file whose first few slots are EMPTY.
SNIFF_RECORDS = 64
SNIFF_BYTES = SNIFF_RECORDS * max(lay.size for lay in LAYOUTS)
# What looks_like reads — the import queue's own probe (importer.js reads
# the first 8 KB of a browser-picked file), so both sides judge the same
# bytes and say the same thing.
PROBE_BYTES = 8192

_PID_MAX = 1 << 22                 # the kernel's PID_MAX_LIMIT
_TIME_MIN = 315_532_800            # 1980-01-01
_TIME_MAX = 4_102_444_800          # 2100-01-01
_EPOCH = datetime.datetime(1970, 1, 1)
_DATETIME_MIN = -62_135_596_800    # 0001-01-01
_DATETIME_MAX = 253_402_300_799    # 9999-12-31 23:59:59

# By name: what logrotate leaves (wtmp.1, btmp-20240501, wtmp.1.old) and an
# archive expansion's " (2)" for a second file of one name, which lands
# before the rotation suffix (free_path keeps the extension). Nothing after
# the name but a digit-led or .old suffix, so wtmpdb's wtmp.db — SQLite —
# stays the SQLite reader's.
_NAME = re.compile(r"^([bwu]tmp)(?: \(\d+\))?(?:[.-](?:\d+|old))*(?: \(\d+\))?$", re.IGNORECASE)


def log_kind(name: str) -> str | None:
    """'wtmp', 'btmp' or 'utmp' for a file named like one (any directory in
    front, either separator), else None. merge.js keeps a twin of this
    (utmpFileKind) and tests/ui/test_utmp_queue.py holds the two together."""
    base = re.split(r"[\\/]", name)[-1]
    m = _NAME.match(base)
    return m.group(1).lower() if m else None


def _text(raw: bytes) -> str:
    """A C string field: up to its first NUL (glibc pads with NULs, but a
    field that fills its width has none), UTF-8 with any other byte kept
    as a visible \\xNN escape."""
    cut = raw.find(b"\0")
    if cut >= 0:
        raw = raw[:cut]
    return raw.decode("utf-8", "backslashreplace")


def _padded(raw: bytes) -> bool:
    """Nothing but NULs after the string's end — how every writer leaves
    one (they zero the record first, or strncpy, which pads). Random bytes
    almost never pass this over three fields at once, which is what makes
    it the strict half of looks_like."""
    cut = raw.find(b"\0")
    return cut < 0 or not raw[cut:].strip(b"\0")


def _ip(raw: bytes) -> str:
    """ut_addr_v6: an IPv4 address uses the first four bytes only."""
    if not any(raw):
        return ""
    if not any(raw[4:]):
        return str(ipaddress.IPv4Address(raw[:4]))
    return str(ipaddress.IPv6Address(raw))


def _timestamp(sec: int, usec: int) -> str:
    """ut_tv as the 'YYYY-MM-DD HH:MM:SS.ffffff' store.py's timestamp
    handling already reads. Zero is "never set" and stays empty; a time no
    datetime can hold, or microseconds past a second, is shown as the raw
    numbers rather than dropped — a broken time is itself a finding."""
    if sec == 0 and usec == 0:
        return ""
    if not (0 <= usec < 1_000_000 and _DATETIME_MIN <= sec <= _DATETIME_MAX):
        return f"<invalid time: {sec} s, {usec} us>"
    return (_EPOCH + datetime.timedelta(seconds=sec, microseconds=usec)).strftime("%Y-%m-%d %H:%M:%S.%f")


def _judge(lay: Layout, raw: bytes, strict: bool) -> int:
    """+1 a record that reads as a login record under this layout, -1 one
    that doesn't, 0 one that says nothing (all zeros, or an EMPTY slot)."""
    if not any(raw):
        return 0
    (typ, pid, line, _id, user, host, _term, _exit,
     _session, sec, usec, _addr) = _STRUCTS[lay.key].unpack(raw)
    if not 0 <= typ <= 9 or not 0 <= pid < _PID_MAX:
        return -1
    if typ == 0:
        return -1 if strict else 0
    if not (_TIME_MIN <= sec < _TIME_MAX and 0 <= usec < 1_000_000):
        return -1
    if strict and not (_padded(line) and _padded(user) and _padded(host)):
        return -1
    return 1


def _score(lay: Layout, head: bytes, strict: bool) -> tuple[int, int]:
    good = bad = 0
    for k in range(min(len(head) // lay.size, SNIFF_RECORDS)):
        verdict = _judge(lay, head[k * lay.size:(k + 1) * lay.size], strict)
        if verdict > 0:
            good += 1
        elif verdict < 0:
            bad += 1
    return good, bad


def detect(head: bytes, size: int | None = None) -> Layout | None:
    """The layout the first records of a file read best under, or None
    when none of the four reads them as login records. `size` (the whole
    file's) breaks a tie in favor of a layout it is a multiple of. A file
    with nothing to judge — all EMPTY, or empty — gets the most common
    layout that fits it, since there is no record it could misread."""
    best = None
    for rank, lay in enumerate(LAYOUTS):
        if len(head) < lay.size:
            continue
        good, bad = _score(lay, head, strict=False)
        whole = size is not None and size % lay.size == 0
        key = (good - 4 * bad, whole, -rank)
        if best is None or key > best[0]:
            best = (key, lay, good, bad)
    if best is None:
        return None
    _key, lay, good, bad = best
    if good > bad:
        return lay
    if good == 0 and bad == 0:
        fits = [lay for lay in LAYOUTS if size is not None and size % lay.size == 0]
        return (fits or [LAYOUTS[0]])[0]
    return None


def looks_like(head: bytes, size: int) -> bool:
    """Whether a file named nothing in particular holds login records — the
    content half of recognition, for wtmp copied out as evidence.bin. Held
    to more than detect is: the file a whole number of records, every one
    judged reading cleanly (EMPTY slots and unpadded strings count
    against), at least one real record. static/js/merge.js's looksLikeUtmp
    is the twin, over the same PROBE_BYTES."""
    head = head[:PROBE_BYTES]
    for lay in LAYOUTS:
        if size < lay.size or size % lay.size:
            continue
        good, bad = _score(lay, head, strict=True)
        if good and not bad:
            return True
    return False


def choose_layout(head: bytes, size: int, name: str) -> Layout:
    """detect, with the reason when there is no answer — what an import of
    a file that isn't login records says."""
    if size == 0:
        return LAYOUTS[0]   # an empty btmp: no records, nothing to misread
    if size < MIN_RECORD:
        raise ValueError(f"{name} is {size:,} bytes — shorter than one login record ({MIN_RECORD} bytes)")
    lay = detect(head, size)
    if lay is None:
        raise ValueError(f"{name} does not hold Linux login records (wtmp, btmp or utmp) in any layout "
                         "Winnow reads — its first records are not valid under any of them")
    return lay


def row(rec: tuple, offset: int) -> list[str]:
    (typ, pid, line, ut_id, user, host, term, exit_,
     session, sec, usec, addr) = rec
    return [
        _timestamp(sec, usec),
        TYPE_NAMES.get(typ, f"UNKNOWN ({typ})"),
        _text(user),
        _text(line),
        _text(host),
        _ip(addr),
        str(pid),
        _text(ut_id),
        str(session),
        str(exit_),
        str(term),
        str(offset),
    ]


def iter_rows(fh: BinaryIO, lay: Layout, stats: dict, limit: int | None = None) -> Iterator[list[str]]:
    """Every whole record from the file's current position as a row; a
    torn tail's byte count goes in stats['torn_bytes']. `limit` stops
    after that many rows (the preview)."""
    unpack = _STRUCTS[lay.key].unpack_from
    size = lay.size
    offset = 0
    count = 0
    stats.setdefault("torn_bytes", 0)
    while True:
        buf = fh.read(size * 4096)
        if not buf:
            return
        whole = len(buf) - len(buf) % size
        for pos in range(0, whole, size):
            yield row(unpack(buf, pos), offset + pos)
            count += 1
            if limit is not None and count >= limit:
                return
        offset += whole
        if whole < len(buf):
            # A regular file reads short only at its end: this is the tail.
            stats["torn_bytes"] = len(buf) - whole
            return


def preview(head: bytes, size: int | None, name: str = "this file", max_rows: int = 50) -> dict:
    """The first rows as the import will make them, from the file's head."""
    lay = choose_layout(head, size if size is not None else len(head), name)
    sample = list(iter_rows(io.BytesIO(head), lay, {}, limit=max_rows))
    out = {"columns": list(COLUMNS), "sample_rows": sample, "inferred_types": list(COLUMN_TYPES),
           "layout": lay.key, "layout_label": lay.label, "record_size": lay.size}
    if size is not None:
        out["record_count"] = size // lay.size
        out["torn_bytes"] = size % lay.size
    return out

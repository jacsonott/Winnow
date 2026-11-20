"""The one column layout every journal table gets, whichever form it came in.

A journal entry is a bag of FIELD=value pairs, and which fields an entry
carries depends on who logged it: a kernel message has _KERNEL_DEVICE, a
service's stdout has _STREAM_ID, sudo adds COMMAND. So a table is:

- six columns read the way `journalctl` prints an entry, always first:
  Timestamp (UTC), Hostname, Identifier, PID, Priority, Message;
- then a column per field the file's entries carry, named as logged
  (_SYSTEMD_UNIT, _CMDLINE, SYSLOG_FACILITY …) — the well-known ones in
  a fixed order, the rest alphabetically, so tables from two journals of
  the same host line up column for column;
- then the entry's own metadata: __REALTIME_TIMESTAMP,
  __MONOTONIC_TIMESTAMP, __SEQNUM, __SEQNUM_ID and __CURSOR, and for a
  journal FILE the byte Offset of the entry, for a hex editor.

The lead columns follow journalctl's own rules: Timestamp is the field
_SOURCE_REALTIME_TIMESTAMP when the entry has it (when the message was
generated, which journalctl shows) and the entry's realtime (when journald
received it) otherwise; Identifier is SYSLOG_IDENTIFIER, else _COMM; PID
is _PID, else SYSLOG_PID. MESSAGE, _HOSTNAME and PRIORITY get no column
of their own, because Message, Hostname and Priority hold them unchanged
(Priority by name: "err", not "3"). Every other field keeps its column,
fallbacks included, so nothing an entry carries is lost.

_BOOT_ID is always a column, as it is always printed by journalctl export
and JSON: the boot an entry belongs to is the first thing a Linux
timeline is cut by.

Values are text. Bytes that aren't UTF-8 (journald stores any bytes) are
kept as \\xNN escapes, a NUL as \\x00; a field that appears more than once
in one entry keeps every value, one per line. A value over FIELD_MAX
bytes keeps its first FIELD_MAX and says it was cut: that is a core dump
stored in the journal, not something to read in a grid (and a compressed
one is only decompressed that far).
"""

from __future__ import annotations

from datetime import datetime, timedelta

LEAD = ["Timestamp (UTC)", "Hostname", "Identifier", "PID", "Priority", "Message"]
LEAD_TYPES = ["datetime", "text", "text", "number", "text", "text"]

# Fields whose value a lead column carries unchanged.
LIFTED = {"MESSAGE", "_HOSTNAME", "PRIORITY"}

META = ["__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP", "__SEQNUM", "__SEQNUM_ID", "__CURSOR"]
META_TYPES = ["number", "number", "number", "text", "text"]
OFFSET = "Offset"

PRIORITY_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]

# The fields an analyst reaches for first, in the order they read best
# beside Message (systemd.journal-fields(7) names them all). Anything not
# listed follows alphabetically.
FIELD_ORDER = [
    "_BOOT_ID", "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "UNIT", "USER_UNIT", "_TRANSPORT",
    "_UID", "_GID", "_AUDIT_LOGINUID", "_AUDIT_SESSION", "_SYSTEMD_SESSION", "_SYSTEMD_OWNER_UID",
    "_COMM", "_EXE", "_CMDLINE", "SYSLOG_IDENTIFIER", "SYSLOG_PID", "SYSLOG_FACILITY", "_PID", "TID",
    "MESSAGE_ID", "_CAP_EFFECTIVE", "_SELINUX_CONTEXT",
    "_SYSTEMD_CGROUP", "_SYSTEMD_SLICE", "_SYSTEMD_USER_SLICE", "_SYSTEMD_INVOCATION_ID",
    "INVOCATION_ID", "USER_INVOCATION_ID",
    "_SOURCE_REALTIME_TIMESTAMP", "_SOURCE_MONOTONIC_TIMESTAMP", "SYSLOG_TIMESTAMP", "SYSLOG_RAW",
    "_MACHINE_ID", "_STREAM_ID", "_LINE_BREAK", "_NAMESPACE", "_RUNTIME_SCOPE",
    "ERRNO", "CODE_FILE", "CODE_LINE", "CODE_FUNC", "DOCUMENTATION",
    "_KERNEL_DEVICE", "_KERNEL_SUBSYSTEM", "_UDEV_SYSNAME", "_UDEV_DEVNODE", "_UDEV_DEVLINK",
]
_FIELD_RANK = {name: i for i, name in enumerate(FIELD_ORDER)}

NUMBER_FIELDS = {
    "_PID", "_UID", "_GID", "SYSLOG_PID", "TID", "_AUDIT_SESSION", "_AUDIT_LOGINUID",
    "_SYSTEMD_OWNER_UID", "ERRNO", "CODE_LINE", "_SOURCE_REALTIME_TIMESTAMP",
    "_SOURCE_MONOTONIC_TIMESTAMP", "SYSLOG_FACILITY",
}

FIELD_MAX = 1 << 20

_EPOCH = datetime(1970, 1, 1)
_USEC_MAX = int((datetime(9999, 12, 31, 23, 59, 59) - _EPOCH).total_seconds()) * 1_000_000


def usec_to_text(usec) -> str:
    """Microseconds since the epoch, UTC, in timeparse's canonical shape.
    0 is journald's "not set"; a value out of range or not a number gives
    '' rather than a wrong time (the raw number is in its own column)."""
    try:
        us = int(usec)
    except (TypeError, ValueError):
        return ""
    if us <= 0 or us > _USEC_MAX:
        return ""
    return (_EPOCH + timedelta(microseconds=us)).strftime("%Y-%m-%d %H:%M:%S.%f")


def value_text(raw: bytes, counts: dict | None = None) -> str:
    """A field's bytes as cell text: UTF-8, with anything else kept as a
    visible \\xNN escape (NUL included, so a cell never holds a raw NUL)."""
    if len(raw) > FIELD_MAX:
        if counts is not None:
            counts["truncated"] = counts.get("truncated", 0) + 1
        head = bytes(raw[:FIELD_MAX]).decode("utf-8", "backslashreplace").replace("\x00", "\\x00")
        return f"{head}… [over {FIELD_MAX:,} bytes; only the first {FIELD_MAX:,} were imported]"
    text = bytes(raw).decode("utf-8", "backslashreplace")
    if "\x00" in text:
        text = text.replace("\x00", "\\x00")
    return text


def order_fields(names) -> list[str]:
    """The as-logged columns for a set of field names: the lifted three
    out, _BOOT_ID always in, well-known first, the rest alphabetically."""
    out = {n for n in names if n and n not in LIFTED and n not in META}
    out.add("_BOOT_ID")
    return sorted(out, key=lambda n: (_FIELD_RANK.get(n, len(FIELD_ORDER)), n))


class Schema:
    """Columns and types for one table, and the row an entry becomes."""

    def __init__(self, field_names, with_offset: bool = False):
        self.fields = order_fields(field_names)
        self.columns = LEAD + self.fields + META + ([OFFSET] if with_offset else [])
        self.types = (LEAD_TYPES
                      + [("number" if f in NUMBER_FIELDS else "text") for f in self.fields]
                      + META_TYPES + (["number"] if with_offset else []))
        self.index = {name: i for i, name in enumerate(self.columns)}
        self.width = len(self.columns)
        self._lead = len(LEAD)
        self._meta = len(LEAD) + len(self.fields)

    def row(self, fields: dict, meta: tuple, offset=None) -> list:
        """`fields`: name -> text (several values already joined).
        `meta`: (realtime, monotonic, seqnum, seqnum_id, cursor) as text."""
        row = [""] * self.width
        idx = self.index
        for name, text in fields.items():
            i = idx.get(name)
            if i is not None:
                row[i] = text
        realtime = meta[0]
        src = fields.get("_SOURCE_REALTIME_TIMESTAMP")
        row[0] = (usec_to_text(src) if src else "") or usec_to_text(realtime)
        row[1] = fields.get("_HOSTNAME", "")
        row[2] = fields.get("SYSLOG_IDENTIFIER") or fields.get("_COMM", "")
        row[3] = fields.get("_PID") or fields.get("SYSLOG_PID", "")
        prio = fields.get("PRIORITY", "")
        row[4] = PRIORITY_NAMES[int(prio)] if len(prio) == 1 and "0" <= prio <= "7" else prio
        row[5] = fields.get("MESSAGE", "")
        m = self._meta
        row[m:m + 5] = meta
        if offset is not None:
            row[-1] = offset
        return row

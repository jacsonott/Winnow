"""systemd journal ingest for Winnow: Linux journal files and journalctl's
lossless output, read with no journalctl (and no Linux) in the loop.

What it reads, told apart by content rather than by name:

- **Journal files** — `/var/log/journal/<machine-id>/system.journal`,
  `user-1000.journal`, the archived `system@….journal`, and the
  `system@….journal~` journald leaves when it finds a file it can't trust
  (a crash, a full disk). Both layouts (regular, and the compact one
  systemd 252+ writes), every compression a field can carry (XZ, LZ4,
  zstd), and damaged files: entries are found by walking the file's
  objects, so a crash that cut the indexes short costs nothing that is
  still on disk (journalfile.py).
- **`journalctl -o export`** — the binary-safe stream systemd-journal-remote
  reads (exported.py).
- **`journalctl -o json`** — and json-pretty, json-seq, json-sse
  (exported.py).

All three land in the same columns (common.py): Timestamp (UTC), Hostname,
Identifier, PID, Priority and Message as journalctl prints an entry, then
every field the entries carry under its journal name (_SYSTEMD_UNIT,
_CMDLINE, _BOOT_ID …), then the entry's own metadata and, for a file, its
byte offset. So the journal files collected from a host, and an export a
responder ran on it, line up column for column and merge.

journalctl's text output (the default `short`, `short-iso`, `verbose` …)
is not read here: it has already thrown fields away, and the default one
has no year. Winnow imports it as text; ask for `-o export` or the files.

Pure stdlib Python: zstd is decoded by the standard library on Python
3.14+ and by zstd.py before that, so it runs on the same airgapped box
Winnow does. It ships switched off in examples/plugins/; turn it on in
Settings → Plugins → Manage plugins….
"""

import os

from winnow import log as wlog

from . import exported, journalfile

PLUGIN = {
    "name": "systemd-journal",
    "version": "1.0.0",
    "description": ("Read Linux systemd journal files (.journal, .journal~) and journalctl "
                    "export or JSON output, with no journalctl needed."),
}

WINNOW_API_VERSION = 11   # the board's Signals card: the signals render and its cells source

HEADER_SET = "systemd journal"
_ALL = "{{all:header_set:" + HEADER_SET + "}}"
_TS = '"Timestamp (UTC)"'


def parse(path, options):
    name = os.path.basename(path)
    with open(path, "rb") as f:
        head = f.read(8192)
    if not head:
        raise ValueError(f"{name} is empty")
    counts: dict = {}
    if journalfile.is_journal(head):
        result = journalfile.parse(path, counts)
        form = "journal file"
    else:
        kind = exported.sniff(head)
        if kind is None:
            raise ValueError(
                f"{name} is not a systemd journal file or journalctl export or JSON output. "
                "journalctl's text output (short, short-iso, verbose …) has already lost fields; "
                "import it as text, or ask for the journal files or `journalctl -o export`.")
        result = exported.parse(path, kind, counts)
        form = f"journalctl {'JSON' if kind == 'json' else 'export'}"
    rows = result["rows"]

    def read():
        yield from rows
        _report(name, form, counts)

    result["rows"] = read()
    result["name"] = name
    return result


def _report(name: str, form: str, c: dict) -> None:
    """What the analyst should know about how the file read, in the case
    log: the import toast only says how many rows."""
    notes = []
    info = []
    if form == "journal file":
        found, header = c.get("entries", 0), c.get("header_entries", 0)
        if c.get("state") == 1:
            # Online: journald had it open. Normal for the live system.journal
            # of a running host; on an archived copy it means a crash.
            if found > header:
                info.append(f"it was still open for writing (online) when it was collected; "
                            f"{found - header:,} entries written after its header was last updated "
                            "were read too")
            elif found == header:
                info.append("it was still open for writing (online) when it was collected; every "
                            "entry its header counts was read")
        if found < header:
            notes.append(f"its header counts {header:,} entries and {found:,} could be read")
        if c.get("unknown_flags"):
            notes.append(f"it sets format flags this reader doesn't know (0x{c['unknown_flags']:x}), "
                         "from a systemd newer than it; check the rows against journalctl")
        if c.get("skipped_bytes"):
            notes.append(f"{c['skipped_bytes']:,} damaged bytes from offset {c.get('damaged_at', 0):,} "
                         "were skipped and the entries after them were read")
        if c.get("damaged_tail_bytes"):
            notes.append(f"it can't be read from offset {c.get('damaged_at', 0):,} to its end "
                         f"({c['damaged_tail_bytes']:,} bytes): the copy is cut short there, or the "
                         "file is damaged; every entry before that was read")
        if c.get("bad_items"):
            notes.append(f"{c['bad_items']:,} field(s) pointed at damaged data and were left out of "
                         "their entries")
        if c.get("undecodable"):
            notes.append(f"{c['undecodable']:,} compressed field(s) could not be decompressed and were "
                         "left out of their entries")
    if c.get("unparsed_lines"):
        notes.append(f"{c['unparsed_lines']:,} line(s) were not {form} output and were skipped")
    if c.get("truncated_fields"):
        notes.append("it ends in the middle of a field, which was imported as far as it goes")
    if c.get("truncated"):
        notes.append(f"{c['truncated']:,} value(s) over 1 MiB (core dumps, usually) were cut to "
                     "their first 1 MiB")
    if notes:
        wlog.record("warn", f"{name} ({form}): " + "; ".join(info + notes) + ".")
    elif info:
        wlog.record("info", f"{name} ({form}): " + "; ".join(info) + ".")


def _where(column, op, value):
    return {"column": column, "op": op, "value": value}


def register(api):
    api.register_ingest_format(
        id="journal",
        label="systemd journal",
        extensions=[".journal", ".journal~", ".export"],
        filename_patterns=["*.journal", "*.journal~", "*.export"],
        description=(
            "Linux systemd journal files (system.journal, user-1000.journal, archived "
            "system@….journal and damaged .journal~ files; regular or compact; XZ, LZ4 or "
            "zstd fields) and journalctl's export or JSON output, recognized by content. "
            "A row per entry: Timestamp (UTC), Hostname, Identifier, PID, Priority and "
            "Message as journalctl shows them, then every field the entries carry "
            "(_SYSTEMD_UNIT, _CMDLINE, _BOOT_ID …). For a .json file, pick this format "
            "here; Winnow's own JSON import claims that extension."
        ),
        parse=parse,
    )

    # Offered under Dashboards ▸ Library. Written against the header set, so
    # it spans every journal table in the case: a host's journal arrives as
    # a directory of files (one per rotation, one per user).
    api.register_dashboard(
        id="overview",
        label="Linux journal overview",
        description="What a host's journal holds: boots, logins, sudo, errors and when it was busy.",
        widgets=[
            {"title": "Journal entries", "source": "sql", "render": "stat", "span": 1,
             "query": {"sql": f"SELECT COUNT(*) FROM {_ALL}"},
             "sub": "across every journal table",
             "drill": {"table": _ALL, "where": []}},
            {"title": "Signals", "source": "cells", "render": "signals", "span": 3,
             "sub": "sshd, sudo and the priorities journald marks as errors",
             "cells": [
                 {"label": "SSH logins accepted", "source": "sql",
                  "query": {"sql": f"SELECT COUNT(*) FROM {_ALL} WHERE Identifier = 'sshd'"
                                   " AND Message LIKE '%Accepted %'"},
                  "drill": {"table": _ALL, "where": [
                      _where("Identifier", "equals", "sshd"), _where("Message", "contains", "Accepted ")]}},
                 {"label": "Failed SSH logins", "tone": "warn", "source": "sql",
                  "query": {"sql": f"SELECT COUNT(*) FROM {_ALL} WHERE Identifier = 'sshd' AND"
                                   " (Message LIKE '%Failed password%' OR Message LIKE '%Invalid user%')"},
                  "drill": {"table": _ALL, "tree": {"type": "group", "op": "AND", "children": [
                      {"type": "cond", "column": "Identifier", "op": "equals", "value": "sshd"},
                      {"type": "group", "op": "OR", "children": [
                          {"type": "cond", "column": "Message", "op": "contains", "value": "Failed password"},
                          {"type": "cond", "column": "Message", "op": "contains", "value": "Invalid user"}]}]}}},
                 {"label": "sudo commands", "source": "sql",
                  "query": {"sql": f"SELECT COUNT(*) FROM {_ALL} WHERE Identifier = 'sudo'"
                                   " AND Message LIKE '%COMMAND=%'"},
                  "drill": {"table": _ALL, "where": [
                      _where("Identifier", "equals", "sudo"), _where("Message", "contains", "COMMAND=")]}},
                 {"label": "Errors or worse", "tone": "warn", "source": "sql",
                  "query": {"sql": f"SELECT COUNT(*) FROM {_ALL}"
                                   " WHERE Priority IN ('emerg', 'alert', 'crit', 'err')"},
                  "drill": {"table": _ALL, "where": [
                      _where("Priority", "in", ["emerg", "alert", "crit", "err"])]}},
                 {"label": "Kernel messages", "source": "sql",
                  "query": {"sql": f"SELECT COUNT(*) FROM {_ALL} WHERE Identifier = 'kernel'"},
                  "drill": {"table": _ALL, "where": [_where("Identifier", "equals", "kernel")]}},
             ]},
            {"title": "Boots, oldest first", "source": "sql", "render": "list", "span": 2,
             "query": {"sql": f"SELECT _BOOT_ID, COUNT(*) c FROM {_ALL} GROUP BY _BOOT_ID"
                              f" ORDER BY MIN(NULLIF({_TS}, '')) LIMIT 12"},
             "drill": {"table": _ALL, "column": "_BOOT_ID"}},
            {"title": "Priority", "source": "sql", "render": "bar", "span": 2,
             "query": {"sql": f"SELECT Priority AS label, COUNT(*) c FROM {_ALL}"
                              " GROUP BY Priority ORDER BY c DESC"},
             "drill": {"table": _ALL, "column": "Priority"}},
            {"title": "Top identifiers", "source": "sql", "render": "bar", "span": 2,
             "query": {"sql": f"SELECT Identifier AS label, COUNT(*) c FROM {_ALL}"
                              " GROUP BY Identifier ORDER BY c DESC LIMIT 15"},
             "drill": {"table": _ALL, "column": "Identifier"}},
            {"title": "Activity over time", "source": "sql", "render": "histogram", "span": 2,
             "query": {"sql": f"SELECT strftime('%Y-%m-%d %H', {_TS}) AS hr, COUNT(*) c FROM {_ALL}"
                              f" WHERE {_TS} <> '' GROUP BY hr ORDER BY hr"},
             "drill": {"table": _ALL, "column": "Timestamp (UTC)", "bucket": "hour",
                       "where": [_where("Timestamp (UTC)", "not_empty", "")]}},
        ],
    )

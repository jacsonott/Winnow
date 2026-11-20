"""journalctl's two lossless outputs: `-o export` and `-o json`.

Both print every field of every entry, plus the entry's metadata
(__CURSOR, __REALTIME_TIMESTAMP, __MONOTONIC_TIMESTAMP, __SEQNUM,
__SEQNUM_ID on newer systemd, _BOOT_ID), so they read into the same
columns a journal file does (common.py) — a host's journal exported by a
responder and the same host's journal files line up.

**Export** (`journalctl -o export`, what systemd-journal-remote reads):
an entry is lines of FIELD=value and ends at an empty line. A value that
isn't text — it holds a newline, a control character or bytes that
aren't UTF-8 — is written as the field name alone on its line, then its
length as a little-endian 64-bit number, the bytes, and a newline.

**JSON** (`-o json`, and the `json-pretty`, `json-seq` and `json-sse`
variants of it): an object per entry. A string is a value; a value that
isn't valid UTF-8 is an array of byte numbers; a field logged more than
once is an array of values; and a value journalctl left out for being
over 4096 bytes (it does unless run with --all) is null, which is said
in the cell rather than left blank.

Two passes over the file, as every Winnow text reader makes: the first
collects the field names and counts the entries, the second reads exactly
that many, so an export still being written is read as far as the first
pass saw.
"""

from __future__ import annotations

import io
import json
import os

from .common import META, Schema, value_text

JSON_OMITTED = "[left out by journalctl: over 4096 bytes; export with --all to keep it]"

_MAX_PENDING = 16 << 20


# ----------------------------------------------------------------- sniffing

def sniff(head: bytes) -> str | None:
    """'export', 'json' or None, from a file's first bytes."""
    text = head.lstrip(b"\xef\xbb\xbf").lstrip(b" \t\r\n\x1e")
    if text.startswith(b"{") or text.startswith(b"data: {") or text.startswith(b"data:{"):
        return "json"
    first = text.split(b"\n", 1)[0].rstrip(b"\r")
    name, eq, _ = first.partition(b"=")
    if _field_name(name) and (eq or len(text) > len(first) + 9):
        return "export"
    return None


def _field_name(name: bytes) -> bool:
    if not name or len(name) > 256 or name[:1].isdigit():
        return False
    return all(c == 0x5F or 0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A for c in name)


# ------------------------------------------------------------------- export

def _export_entries(fh, counts: dict):
    """Each entry as a list of (name, bytes)."""
    entry: list = []
    crlf = None
    while True:
        line = fh.readline()
        if not line:
            if entry:
                yield entry
            return
        if crlf is None:
            crlf = line.endswith(b"\r\n")
        if line.endswith(b"\n"):
            line = line[:-1]
            if crlf and line.endswith(b"\r"):
                line = line[:-1]
        if not line:
            if entry:
                yield entry
                entry = []
            continue
        eq = line.find(b"=")
        if eq > 0:
            entry.append((line[:eq].decode("ascii", "backslashreplace"), line[eq + 1:]))
            continue
        if not _field_name(line):
            counts["unparsed_lines"] = counts.get("unparsed_lines", 0) + 1
            continue
        # A binary field: the name, then le64 length, the bytes, "\n".
        size = fh.read(8)
        if len(size) < 8:
            counts["truncated_fields"] = counts.get("truncated_fields", 0) + 1
            break
        n = int.from_bytes(size, "little")
        data = fh.read(n)
        if len(data) < n:
            counts["truncated_fields"] = counts.get("truncated_fields", 0) + 1
            entry.append((line.decode("ascii"), data))
            break
        entry.append((line.decode("ascii"), data))
        if fh.read(1) not in (b"\n", b""):
            counts["unparsed_lines"] = counts.get("unparsed_lines", 0) + 1
    if entry:
        yield entry


def _export_fields(entry, counts: dict):
    fields: dict = {}
    for name, raw in entry:
        text = value_text(raw, counts)
        prev = fields.get(name)
        fields[name] = text if prev is None else f"{prev}\n{text}"
    return fields


# --------------------------------------------------------------------- json

def _json_records(fh, counts: dict):
    """Each JSON object in a file of them: one per line (json), spread over
    lines (json-pretty), behind an RS byte (json-seq) or after "data:"
    (json-sse)."""
    pending: list[str] = []
    size = 0
    for line in fh:
        s = line.strip()
        if not pending:
            s = s.lstrip("\x1e").strip()
            if s.startswith("data:"):
                s = s[5:].strip()
            if not s:
                continue
            if s[0] != "{":
                counts["unparsed_lines"] = counts.get("unparsed_lines", 0) + 1
                continue
            if s[-1] == "}":
                try:
                    obj = json.loads(s)
                except ValueError:
                    pass
                else:
                    if isinstance(obj, dict):
                        yield obj
                    continue
        pending.append(s)
        size += len(s)
        if s.endswith("}"):
            try:
                obj = json.loads("\n".join(pending))
            except ValueError:
                if size > _MAX_PENDING:
                    raise ValueError("this doesn't read as journalctl JSON output: an object "
                                     f"ran on for over {_MAX_PENDING >> 20} MB without closing")
                continue
            pending, size = [], 0
            if isinstance(obj, dict):
                yield obj
    if pending:
        counts["unparsed_lines"] = counts.get("unparsed_lines", 0) + len(pending)


def _json_value(v, counts: dict) -> str:
    if v is None:
        return JSON_OMITTED
    if isinstance(v, str):
        return v.replace("\x00", "\\x00") if "\x00" in v else v
    if isinstance(v, list):
        if v and all(isinstance(x, int) for x in v):
            try:
                return value_text(bytes(v), counts)
            except ValueError:
                pass
        if not v:
            return ""
        return "\n".join(_json_value(x, counts) for x in v)
    return json.dumps(v)


def _json_fields(obj: dict, counts: dict) -> dict:
    return {str(k): _json_value(v, counts) for k, v in obj.items()}


# ------------------------------------------------------------------- shared

def _meta(fields: dict) -> tuple:
    return tuple(fields.get(k, "") for k in META)


def parse(path: str, kind: str, counts: dict) -> dict:
    if kind == "export":
        def opened():
            return open(path, "rb", buffering=1 << 20)

        def records(fh, tally):
            for entry in _export_entries(fh, tally):
                yield _export_fields(entry, tally)

        def names_of(fh, tally):
            # Pass one needs only each entry's field names, not its values.
            for entry in _export_entries(fh, tally):
                yield [name for name, _ in entry]
    else:
        def opened():
            return io.open(path, "r", encoding="utf-8-sig", errors="backslashreplace",
                           newline=None, buffering=1 << 20)

        def records(fh, tally):
            for obj in _json_records(fh, tally):
                yield _json_fields(obj, tally)

        def names_of(fh, tally):
            for obj in _json_records(fh, tally):
                yield [str(k) for k in obj]

    names: set[str] = set()
    total = 0
    with opened() as fh:
        # Its own tally: the second pass counts what the analyst is told.
        for keys in names_of(fh, {}):
            names.update(keys)
            total += 1
    if not total:
        raise ValueError(f"{os.path.basename(path)} holds no journal entries: it doesn't read as "
                         f"journalctl {'JSON' if kind == 'json' else 'export'} output")
    counts["entries"] = total
    schema = Schema(names)

    def rows():
        with opened() as fh:
            for i, fields in enumerate(records(fh, counts)):
                if i >= total:
                    break
                yield schema.row(fields, _meta(fields))

    return {"columns": schema.columns, "column_types": schema.types, "rows": rows()}

"""Raw $MFT parser: one row per FILE record, with reconstructed paths.

Two passes over the file, for the same reason store.py's JSON ingest makes
two (a fixed answer needs the whole file first): pass 1 collects each
record's (sequence, parent reference, best name) into a small in-memory
map, pass 2 re-reads the records and emits rows with full paths resolved
against that map — so memory scales with the number of records (a few
dozen bytes each), never with row width, and rows stream straight into
Store.ingest_rows' batched inserts.

Timestamps from $STANDARD_INFORMATION and the preferred $FILE_NAME
attribute are emitted side by side (the 0x10/0x30 column-name convention
analysts know from MFTECmd), because disagreement between them is the
point: $SI is what timestomping tools rewrite, $FN is what they usually
can't. Deleted records are rows like any other — the record being dead is
a column (InUse), not a reason to drop evidence.
"""

from __future__ import annotations

import struct

from .common import extension_of, filetime_to_iso, split_file_reference

MAGIC = b"FILE"
ROOT_ENTRY = 5
RECORD_SIZES = (1024, 4096)  # 4096 on 4Kn-sector volumes
SECTOR = 512

ATTR_SI = 0x10
ATTR_FN = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

NAMESPACES = {0: "POSIX", 1: "Win32", 2: "DOS", 3: "Win32&DOS"}

COLUMNS = [
    "EntryNumber", "SequenceNumber", "ParentEntryNumber", "InUse", "IsDirectory",
    "FullPath", "FileName", "Extension", "NameType", "FileSize",
    "Created0x10", "LastModified0x10", "RecordModified0x10", "LastAccess0x10",
    "Created0x30", "LastModified0x30", "RecordModified0x30", "LastAccess0x30",
    "SI<FN Created",
]
COLUMN_TYPES = [
    "number", "number", "number", "text", "text",
    "text", "text", "text", "text", "number",
    "datetime", "datetime", "datetime", "datetime",
    "datetime", "datetime", "datetime", "datetime",
    "text",
]


def parse(path: str, options: dict) -> dict:
    which = (options or {}).get("records", "all")
    record_size = _detect_record_size(path)
    names: dict[int, tuple[int, int, int, str]] = {}  # entry -> (seq, parent_entry, parent_seq, name)
    for entry, rec in _iter_records(path, record_size):
        parsed = _parse_record(rec, entry)
        if parsed and parsed["fn"]:
            pe, ps = split_file_reference(parsed["fn"]["parent_ref"])
            names[parsed["entry"]] = (parsed["seq"], pe, ps, parsed["fn"]["name"])
    return {
        "columns": COLUMNS,
        "rows": _rows(path, record_size, names, which),
        "column_types": COLUMN_TYPES,
    }


def _rows(path: str, record_size: int, names: dict, which: str):
    paths: dict[int, str] = {}
    for entry, rec in _iter_records(path, record_size):
        parsed = _parse_record(rec, entry)
        if parsed is None or (parsed["fn"] is None and parsed["si"] is None):
            continue  # zeroed slot, torn record, or an attribute-extension record — no row to make
        if which == "in-use" and not parsed["in_use"]:
            continue
        if which == "deleted" and parsed["in_use"]:
            continue
        fn = parsed["fn"] or {}
        name = fn.get("name", "")
        si = parsed["si"] or (0, 0, 0, 0)
        fn_times = fn.get("times", (0, 0, 0, 0))
        if fn:
            pe, ps = split_file_reference(fn["parent_ref"])
            full = _dir_path(pe, ps, names, paths) + "\\" + name
        else:
            pe, full = "", ""
        si_lt_fn = "Y" if si[0] and fn_times[0] and si[0] < fn_times[0] else ""
        yield [
            parsed["entry"], parsed["seq"], pe,
            "true" if parsed["in_use"] else "false",
            "true" if parsed["is_dir"] else "false",
            full, name, extension_of(name), fn.get("namespace", ""), parsed["size"],
            filetime_to_iso(si[0]), filetime_to_iso(si[1]), filetime_to_iso(si[2]), filetime_to_iso(si[3]),
            filetime_to_iso(fn_times[0]), filetime_to_iso(fn_times[1]),
            filetime_to_iso(fn_times[2]), filetime_to_iso(fn_times[3]),
            si_lt_fn,
        ]


def _detect_record_size(path: str) -> int:
    """Bytes 28..32 of a FILE record hold its allocated size — read it off
    the first record rather than assuming 1024 (4Kn volumes use 4096)."""
    with open(path, "rb") as f:
        head = f.read(32)
    if len(head) >= 32 and head[:4] == MAGIC:
        alloc = struct.unpack_from("<I", head, 28)[0]
        if alloc in RECORD_SIZES:
            return alloc
    return 1024


def _iter_records(path: str, record_size: int):
    """(entry_number_by_position, raw_record) for every slot in the file.
    The positional entry number is the fallback identity for pre-3.1
    records whose header doesn't carry one."""
    per_read = max(1, (8 << 20) // record_size)
    with open(path, "rb", buffering=1 << 20) as f:
        entry = 0
        while True:
            block = f.read(per_read * record_size)
            if not block:
                break
            for off in range(0, len(block) - record_size + 1, record_size):
                yield entry, block[off: off + record_size]
                entry += 1


def _parse_record(rec: bytes, position_entry: int) -> dict | None:
    if rec[:4] != MAGIC:  # zeroed slot, or "BAAD" (failed multi-sector write)
        return None
    usa_ofs, usa_count = struct.unpack_from("<HH", rec, 4)
    rec = _apply_fixups(rec, usa_ofs, usa_count)
    if rec is None:
        return None
    seq, = struct.unpack_from("<H", rec, 16)
    first_attr, flags = struct.unpack_from("<HH", rec, 20)
    # The record's own entry number was added to the header (offset 44) in
    # NTFS 3.1, pushing the update sequence array from 42 to 48 — usa_ofs
    # is how you tell whether offset 44 is real or part of the USA.
    entry = struct.unpack_from("<I", rec, 44)[0] if usa_ofs >= 48 else position_entry

    si = None          # (created, modified, record_modified, accessed) FILETIMEs
    fn = None          # dict: parent_ref, times, name, namespace
    fn_rank = -1
    size = 0
    off = first_attr
    while 16 <= off <= len(rec) - 16:
        atype, alen = struct.unpack_from("<II", rec, off)
        if atype == ATTR_END:
            break
        if alen < 16 or off + alen > len(rec):
            break  # torn attribute list — keep what parsed so far
        non_resident = rec[off + 8]
        attr_name_len = rec[off + 9]
        content = b""
        if not non_resident and off + 24 <= len(rec):
            csize, cofs = struct.unpack_from("<IH", rec, off + 16)
            if cofs + csize <= alen:
                content = rec[off + cofs: off + cofs + csize]
        if atype == ATTR_SI and len(content) >= 32:
            si = struct.unpack_from("<4Q", content, 0)
        elif atype == ATTR_FN and len(content) >= 66:
            name_len, namespace = content[64], content[65]
            if 66 + 2 * name_len <= len(content):
                # A record commonly has two $FILE_NAMEs (Win32 + DOS 8.3);
                # prefer the human one: Win32&DOS > Win32 > POSIX > DOS.
                rank = {3: 3, 1: 2, 0: 1, 2: 0}.get(namespace, 1)
                if rank > fn_rank:
                    fn_rank = rank
                    fn = {
                        "parent_ref": struct.unpack_from("<Q", content, 0)[0],
                        "times": struct.unpack_from("<4Q", content, 8),
                        "name": content[66: 66 + 2 * name_len].decode("utf-16-le", errors="replace"),
                        "namespace": NAMESPACES.get(namespace, str(namespace)),
                    }
        elif atype == ATTR_DATA and attr_name_len == 0:
            # Unnamed $DATA = the file's main content; named ones are ADSes.
            if non_resident and off + 56 <= len(rec) and alen >= 56:
                size = struct.unpack_from("<Q", rec, off + 48)[0]
            elif not non_resident:
                size = len(content)
        off += alen

    return {
        "entry": entry, "seq": seq,
        "in_use": bool(flags & 0x1), "is_dir": bool(flags & 0x2),
        "si": si, "fn": fn, "size": size,
    }


def _apply_fixups(rec: bytes, usa_ofs: int, usa_count: int) -> bytes | None:
    """Undo NTFS's multi-sector write protection: the last two bytes of
    every 512-byte sector were swapped out for the update sequence number
    (USA slot 0) at write time, with the real bytes parked in the USA.
    Failing to put them back corrupts exactly the bytes most likely to sit
    mid-attribute.

    Three cases, told apart by which sector tails carry the USN stamp:
    every tail stamped -> a raw record (KAPE/icat/RawCopy extraction),
    un-stamp it; no tail stamped -> the extraction tool already applied
    the fixups itself (ntfscat does — verified against a real mkntfs
    volume, whose records come back with corrected tails), parse as-is;
    a mix -> a genuinely torn multi-sector write, and half known-bad
    bytes are worse than no row, so skip the record."""
    if usa_count < 2 or usa_ofs + usa_count * 2 > len(rec) or (usa_count - 1) * SECTOR > len(rec):
        return rec  # no usable USA — parse the bytes we have
    usn = rec[usa_ofs: usa_ofs + 2]
    stamped = [rec[i * SECTOR - 2: i * SECTOR] == usn for i in range(1, usa_count)]
    if not any(stamped):
        return rec  # already fixed up by whatever extracted it
    if not all(stamped):
        return None  # torn write
    fixed = bytearray(rec)
    for i in range(1, usa_count):
        end = i * SECTOR
        src = usa_ofs + i * 2
        fixed[end - 2: end] = rec[src: src + 2]
    return bytes(fixed)


def _dir_path(entry: int, expect_seq: int, names: dict, paths: dict) -> str:
    """Resolve a directory's full path by walking parent references,
    memoized in `paths`. The sequence check is what makes deleted files
    honest: a dead record's parent entry may have been reused for some
    unrelated directory, and stitching the new tenant's path onto an old
    file would invent a location it never had — that's an <orphan>."""
    rec = names.get(entry)
    if entry == ROOT_ENTRY:
        return "."
    if rec is None or rec[0] != expect_seq:
        return ".\\<orphan>"

    chain: list[int] = []
    on_chain: set[int] = set()
    e = entry
    base = ".\\<orphan>"
    while True:
        if e == ROOT_ENTRY:
            base = "."
            break
        if e in paths:
            base = paths[e]
            break
        r = names.get(e)
        if r is None:
            base = ".\\<orphan>"
            break
        if e in on_chain:
            base = ".\\<cycle>"
            break
        chain.append(e)
        on_chain.add(e)
        seq, pe, ps, _name = r
        parent = names.get(pe)
        if pe != ROOT_ENTRY and (parent is None or parent[0] != ps):
            base = ".\\<orphan>"
            break
        e = pe
    for e2 in reversed(chain):
        base = base + "\\" + names[e2][3]
        paths[e2] = base
    return paths.get(entry, base)

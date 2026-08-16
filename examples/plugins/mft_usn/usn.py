"""Raw $UsnJrnl:$J (USN change journal) parser.

The $J stream is a sequence of USN_RECORD structures, 8-byte aligned,
usually preceded by a very large sparse run of zeros (the journal is a
circular buffer; extraction tools materialize the holes as zero bytes).
Records are parsed leniently, carving-style: a slot that doesn't look like
a valid record advances 8 bytes and tries again rather than aborting the
whole ingest — a torn record in a multi-GB journal shouldn't cost the
other few million.

Handles USN_RECORD_V2 (the overwhelmingly common shape) and V3 (128-bit
file references, ReFS/newer Windows). V4 range-tracking records carry no
timestamp or name and are skipped via their declared length.
"""

from __future__ import annotations

import struct

from .common import decode_flags, extension_of, filetime_to_iso, split_file_reference

CHUNK = 8 << 20          # read granularity; also the upper bound on carry size
MAX_RECORD_LEN = 0x1000  # real records are ≤ ~570 bytes (60 + 255 UTF-16 chars)
MIN_RECORD_LEN = 60      # V2 header size

REASON_FLAGS = [
    (0x00000001, "DATA_OVERWRITE"), (0x00000002, "DATA_EXTEND"),
    (0x00000004, "DATA_TRUNCATION"), (0x00000010, "NAMED_DATA_OVERWRITE"),
    (0x00000020, "NAMED_DATA_EXTEND"), (0x00000040, "NAMED_DATA_TRUNCATION"),
    (0x00000100, "FILE_CREATE"), (0x00000200, "FILE_DELETE"),
    (0x00000400, "EA_CHANGE"), (0x00000800, "SECURITY_CHANGE"),
    (0x00001000, "RENAME_OLD_NAME"), (0x00002000, "RENAME_NEW_NAME"),
    (0x00004000, "INDEXABLE_CHANGE"), (0x00008000, "BASIC_INFO_CHANGE"),
    (0x00010000, "HARD_LINK_CHANGE"), (0x00020000, "COMPRESSION_CHANGE"),
    (0x00040000, "ENCRYPTION_CHANGE"), (0x00080000, "OBJECT_ID_CHANGE"),
    (0x00100000, "REPARSE_POINT_CHANGE"), (0x00200000, "STREAM_CHANGE"),
    (0x00400000, "TRANSACTED_CHANGE"), (0x00800000, "INTEGRITY_CHANGE"),
    (0x80000000, "CLOSE"),
]

SOURCE_FLAGS = [
    (0x1, "DATA_MANAGEMENT"), (0x2, "AUXILIARY_DATA"),
    (0x4, "REPLICATION_MANAGEMENT"), (0x8, "CLIENT_REPLICATION_MANAGEMENT"),
]

FILE_ATTR_FLAGS = [
    (0x1, "READONLY"), (0x2, "HIDDEN"), (0x4, "SYSTEM"), (0x10, "DIRECTORY"),
    (0x20, "ARCHIVE"), (0x40, "DEVICE"), (0x80, "NORMAL"), (0x100, "TEMPORARY"),
    (0x200, "SPARSE"), (0x400, "REPARSE_POINT"), (0x800, "COMPRESSED"),
    (0x1000, "OFFLINE"), (0x2000, "NOT_INDEXED"), (0x4000, "ENCRYPTED"),
]

COLUMNS = [
    "Timestamp", "FileName", "Extension", "Reason",
    "EntryNumber", "SequenceNumber", "ParentEntryNumber", "ParentSequenceNumber",
    "USN", "FileAttributes", "SourceInfo",
]
COLUMN_TYPES = [
    "datetime", "text", "text", "text",
    "number", "number", "number", "number",
    "number", "text", "text",
]


def parse(path: str, options: dict) -> dict:
    return {"columns": COLUMNS, "rows": _rows(path), "column_types": COLUMN_TYPES}


def _rows(path: str):
    with open(path, "rb") as f:
        carry = b""
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            data = carry + chunk if carry else chunk
            carry = b""
            pos, n = 0, len(data)
            while pos + MIN_RECORD_LEN <= n:
                (length,) = struct.unpack_from("<I", data, pos)
                if length == 0:
                    # Sparse padding. lstrip runs at C speed, so a
                    # gigabytes-long zero run costs one scan per chunk, not
                    # a Python-level loop over every byte.
                    nz = n - len(data[pos:].lstrip(b"\x00"))
                    if nz >= n:
                        pos = n
                        break
                    pos = max(pos + 8, nz & ~7)  # records are 8-aligned; max() guarantees progress
                    continue
                if length < MIN_RECORD_LEN or length > MAX_RECORD_LEN or length % 8:
                    pos += 8  # doesn't look like a record — carve forward
                    continue
                if pos + length > n:
                    break  # straddles the chunk boundary — finish in the next pass
                row = _record(data, pos, length)
                if row is not None:
                    yield row
                pos += length
            if pos < n:
                carry = data[pos:]


def _record(data: bytes, pos: int, length: int):
    major, _minor = struct.unpack_from("<HH", data, pos + 4)
    if major == 2:
        frn, parent_frn, usn_val, ts, reason, source, _sec, fattr, name_len, name_ofs = \
            struct.unpack_from("<QQQQIIIIHH", data, pos + 8)
        entry, seq = split_file_reference(frn)
        parent_entry, parent_seq = split_file_reference(parent_frn)
    elif major == 3:
        # 128-bit file references; NTFS zero-extends the classic 64-bit
        # reference, so the low quadword splits the same way.
        frn_lo, _frn_hi, par_lo, _par_hi, usn_val, ts, reason, source, _sec, fattr, name_len, name_ofs = \
            struct.unpack_from("<QQQQQQIIIIHH", data, pos + 8)
        entry, seq = split_file_reference(frn_lo)
        parent_entry, parent_seq = split_file_reference(par_lo)
    else:
        return None  # V4 range-tracking (or unknown) — no timestamp/name to ingest
    if name_ofs + name_len > length:
        return None
    name = data[pos + name_ofs: pos + name_ofs + name_len].decode("utf-16-le", errors="replace")
    return [
        filetime_to_iso(ts), name, extension_of(name), decode_flags(reason, REASON_FLAGS),
        entry, seq, parent_entry, parent_seq,
        usn_val, decode_flags(fattr, FILE_ATTR_FLAGS), decode_flags(source, SOURCE_FLAGS),
    ]

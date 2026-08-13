"""Shared NTFS decoding helpers for the mft/usn parsers."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

_FILETIME_EPOCH = datetime(1601, 1, 1)
# FILETIMEs past the year 9999 overflow datetime — seen in the wild in
# timestomped/corrupt records. Render those as raw hex rather than dying
# mid-ingest or silently blanking a value that's itself a finding.
_FILETIME_MAX = (datetime(9999, 12, 28) - _FILETIME_EPOCH).total_seconds() * 10_000_000


def filetime_to_iso(ft: int) -> str:
    """Windows FILETIME (100ns ticks since 1601-01-01 UTC) -> the zero-padded
    'YYYY-MM-DD HH:MM:SS.ffffff' shape store.py's TS_NORMALIZE/DAY_BUCKET
    already recognize. 0 means "never set" and becomes ''. Microsecond
    precision is kept on purpose: a timestomped $SI time with .000000
    subseconds next to a populated $FN one is a classic tell."""
    if not ft:
        return ""
    if ft < 0 or ft > _FILETIME_MAX:
        return f"<invalid filetime 0x{ft & 0xFFFFFFFFFFFFFFFF:016x}>"
    return (_FILETIME_EPOCH + timedelta(microseconds=ft / 10)).strftime("%Y-%m-%d %H:%M:%S.%f")


def split_file_reference(ref: int) -> tuple[int, int]:
    """An NTFS file reference packs a 48-bit MFT entry number with a 16-bit
    sequence number. Splitting them is what lets a USN row join against an
    ingested $MFT on (EntryNumber, SequenceNumber)."""
    return ref & 0xFFFF_FFFF_FFFF, ref >> 48


def extension_of(name: str) -> str:
    """Lowercased extension without the dot — its own column because 'every
    .exe/.ps1 the journal saw' is a first-reflex filter."""
    return os.path.splitext(name)[1].lstrip(".").lower()


def decode_flags(value: int, table: list[tuple[int, str]]) -> str:
    """Bitmask -> 'A|B|C' using the given (bit, name) table; unknown leftover
    bits are kept as hex rather than dropped."""
    names = [name for bit, name in table if value & bit]
    known = 0
    for bit, _ in table:
        known |= bit
    rest = value & ~known
    if rest:
        names.append(f"0x{rest:x}")
    return "|".join(names)

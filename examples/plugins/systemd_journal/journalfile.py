"""Binary systemd journal files (system.journal, user-1000.journal, the
archived system@….journal and a dirty system@….journal~).

The format is systemd's "Journal File Format" (docs/JOURNAL_FILE_FORMAT.md
in the systemd tree): a header, then an arena of 8-byte-aligned objects,
each a 16-byte header (type, flags, size) and a payload. An ENTRY object
is one log entry: its sequence number, realtime and monotonic clocks, boot
ID, and a list of items pointing at DATA objects; a DATA object holds one
"FIELD=value" payload, shared by every entry that carries the same pair
(so a host's _HOSTNAME is stored once), and compressed with XZ, LZ4 or
zstd past a size threshold.

**Entries are found by walking the objects in file order, not by following
the entry arrays.** journalctl walks the arrays, which is right for a
healthy file and loses everything past the first damage in a broken one.
Objects are only ever appended, so file order is write order, an entry's
DATA objects always sit before it, and a walk reads every entry the file
still holds, including ones a crash left out of the arrays (and out of
the header's counts) in an online file or a .journal~. Where an object
header is damaged, the walk carves forward to the next object that reads
as one and says how many bytes it skipped.

Two layouts are read, told apart by the header's incompatible flags: the
regular one, and the "compact" one systemd 252 introduced (32-bit item
offsets, a longer DATA header), which is the default for new files since.
The keyed-hash flag changes only the hash tables, which this never needs.

Two passes, as every Winnow reader that needs its columns first makes:
the first walks the objects and collects the entries and the field names
(from the DATA objects themselves, so the columns are exactly what the
entries hold); the second reads the entries in that order. DATA objects
are cached by offset between entries, which is most of them: a service's
_CMDLINE or _SYSTEMD_UNIT is one object referenced thousands of times.
"""

from __future__ import annotations

import mmap
import os
import re
import struct

from .common import FIELD_MAX, Schema, value_text
from .decompress import COMPRESSION_MASK, DecompressError, decompress

SIGNATURE = b"LPKSHHRH"

OBJECT_DATA = 1
OBJECT_FIELD = 2
OBJECT_ENTRY = 3
OBJECT_DATA_HASH_TABLE = 4
OBJECT_FIELD_HASH_TABLE = 5
OBJECT_ENTRY_ARRAY = 6
OBJECT_TAG = 7

INCOMPATIBLE_COMPRESSED_XZ = 1 << 0
INCOMPATIBLE_COMPRESSED_LZ4 = 1 << 1
INCOMPATIBLE_KEYED_HASH = 1 << 2
INCOMPATIBLE_COMPRESSED_ZSTD = 1 << 3
INCOMPATIBLE_COMPACT = 1 << 4
INCOMPATIBLE_KNOWN = (INCOMPATIBLE_COMPRESSED_XZ | INCOMPATIBLE_COMPRESSED_LZ4 | INCOMPATIBLE_KEYED_HASH
                      | INCOMPATIBLE_COMPRESSED_ZSTD | INCOMPATIBLE_COMPACT)

STATES = {0: "offline", 1: "online", 2: "archived"}

# The smallest each object type can be: its fixed header (DATA's is longer
# in a compact file). A size under it is not that object.
_MIN_SIZE = {OBJECT_DATA: 64, OBJECT_FIELD: 40, OBJECT_ENTRY: 64, OBJECT_DATA_HASH_TABLE: 16,
             OBJECT_FIELD_HASH_TABLE: 16, OBJECT_ENTRY_ARRAY: 24, OBJECT_TAG: 64}
# Oldest header systemd ever wrote (v187 added n_data/n_fields after it).
_MIN_HEADER = 208
# No real object is this big (journald caps a field at 768 MiB).
_MAX_OBJECT = 1 << 31

_NONZERO = re.compile(rb"[^\x00]")

# Data objects kept decoded between entries: by count and by bytes.
_CACHE_ITEMS = 200_000
_CACHE_BYTES = 64 << 20
_CACHE_VALUE_MAX = 64 << 10


def is_journal(head: bytes) -> bool:
    return head[:8] == SIGNATURE


class JournalFile:
    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "rb")
        try:
            size = os.fstat(self._fh.fileno()).st_size
            if size < _MIN_HEADER:
                raise ValueError(f"{os.path.basename(path)} is too short to be a systemd journal file")
            self.buf = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
            self._read_header(size)
        except BaseException:
            self.close()
            raise
        self._cache: dict = {}
        self._cache_bytes = 0
        self.counts: dict = {}

    def close(self) -> None:
        buf = getattr(self, "buf", None)
        if buf is not None:
            buf.close()
            self.buf = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # ------------------------------------------------------------ header

    def _read_header(self, size: int) -> None:
        b = self.buf
        if b[:8] != SIGNATURE:
            raise ValueError(f"{os.path.basename(self.path)} is not a systemd journal file "
                             "(it doesn't start with the LPKSHHRH signature)")
        self.compatible, self.incompatible = struct.unpack_from("<II", b, 8)
        self.state = b[16]
        self.file_id = b[24:40].hex()
        self.machine_id = b[40:56].hex()
        self.seqnum_id = b[72:88].hex()
        (self.header_size, self.arena_size, _dht_off, _dht_size, _fht_off, _fht_size,
         self.tail_object_offset, self.n_objects, self.n_entries) = struct.unpack_from("<9Q", b, 88)
        if self.header_size < _MIN_HEADER or self.header_size > min(size, 1 << 16) or self.header_size % 8:
            raise ValueError(f"{os.path.basename(self.path)}: the journal header gives an impossible "
                             f"header size ({self.header_size})")
        self.compact = bool(self.incompatible & INCOMPATIBLE_COMPACT)
        self.unknown_flags = self.incompatible & ~INCOMPATIBLE_KNOWN
        self.data_payload = 72 if self.compact else 64
        self._min_size = dict(_MIN_SIZE)
        self._min_size[OBJECT_DATA] = self.data_payload
        self.item_size = 4 if self.compact else 16
        self.file_size = size
        # The arena's declared end, or the file's if it is shorter (a copy
        # cut short, a carved file): read what is there.
        arena_end = self.header_size + self.arena_size
        self.end = min(size, arena_end) if arena_end > self.header_size else size

    def describe(self) -> str:
        layout = "compact" if self.compact else "regular"
        return f"{STATES.get(self.state, f'state {self.state}')}, {layout} layout"

    # ------------------------------------------------------------- objects

    def _object_at(self, off: int):
        """(type, flags, size) if a plausible object header sits at `off`."""
        if off % 8 or off < self.header_size or off + 16 > self.end:
            return None
        b = self.buf
        otype, flags = b[off], b[off + 1]
        (size,) = struct.unpack_from("<Q", b, off + 8)
        if not OBJECT_DATA <= otype <= OBJECT_TAG or size > _MAX_OBJECT or off + size > self.end:
            return None
        if size < self._min_size[otype]:
            return None
        if flags and (otype != OBJECT_DATA or flags & ~COMPRESSION_MASK or flags & (flags - 1)):
            return None
        if otype == OBJECT_ENTRY and (size - 64) % self.item_size:
            return None
        return otype, flags, size

    def scan(self):
        """Walk every object. Returns (entry offsets in file order, field
        names). Damage is counted in self.counts, never raised."""
        entries: list[int] = []
        names: set[str] = set()
        b = self.buf
        off = self.header_size
        end = self.end
        # Where the header says the last object starts. Zeros before it are
        # damage; zeros after it are the unused tail of the arena.
        declared_tail = self.tail_object_offset if self.header_size <= self.tail_object_offset < end else 0
        while off + 16 <= end:
            obj = self._object_at(off)
            if obj is None:
                if not any(b[off:off + 16]) and off > declared_tail:
                    break  # the arena's unused tail
                nxt = self._resync(off + 8)
                if nxt is None:
                    self._count("damaged_tail_bytes", end - off)
                    self.counts.setdefault("damaged_at", off)
                    break
                self._count("skipped_bytes", nxt - off)
                self.counts.setdefault("damaged_at", off)
                off = nxt
                continue
            otype, flags, size = obj
            if otype == OBJECT_ENTRY:
                entries.append(off)
            elif otype == OBJECT_DATA:
                name = self._data_name(off, flags, size)
                if name:
                    names.add(name)
            off += (size + 7) & ~7
        return entries, names

    def _resync(self, start: int):
        """The next offset from `start` where an object header reads as one
        and the object after it does too (or the arena ends there)."""
        b = self.buf
        off = (start + 7) & ~7
        end = self.end
        while off + 16 <= end:
            if not any(b[off:off + 8]):
                m = _NONZERO.search(b, off, end)
                if m is None:
                    return None
                off = max(off + 8, m.start() & ~7)
                continue
            obj = self._object_at(off)
            if obj is not None:
                after = off + ((obj[2] + 7) & ~7)
                if after + 16 > end or self._object_at(after) is not None or not any(b[after:after + 16]):
                    return off
            off += 8
        return None

    def _count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _data_payload(self, off: int, flags: int, size: int, limit: int | None = None) -> bytes:
        payload = self.buf[off + self.data_payload:off + size]
        if flags:
            payload = decompress(flags, payload, limit)
        return payload

    def _data_name(self, off: int, flags: int, size: int) -> str | None:
        try:
            if flags:
                payload = self._data(off)
                return payload[0] if payload else None
            head = self.buf[off + self.data_payload:off + min(size, self.data_payload + 256)]
        except DecompressError:
            return None
        eq = head.find(b"=")
        if eq <= 0:
            return None
        return head[:eq].decode("ascii", "backslashreplace")

    def _data(self, off: int):
        """(name, text) for the DATA object at `off`, or None if there is no
        readable one there. Cached."""
        hit = self._cache.get(off)
        if hit is not None:
            return hit
        obj = self._object_at(off)
        if obj is None or obj[0] != OBJECT_DATA:
            self._count("bad_items")
            return None
        _, flags, size = obj
        try:
            # A compressed field is decompressed only as far as a cell keeps
            # (value_text cuts it): a core dump can be hundreds of MB.
            payload = self._data_payload(off, flags, size, FIELD_MAX + 4096 if flags else None)
        except DecompressError:
            self._count("undecodable")
            return None
        eq = payload.find(b"=")
        if eq <= 0:
            self._count("bad_items")
            return None
        name = bytes(payload[:eq]).decode("ascii", "backslashreplace")
        text = value_text(payload[eq + 1:], self.counts)
        hit = (name, text)
        if len(text) <= _CACHE_VALUE_MAX:
            if len(self._cache) >= _CACHE_ITEMS or self._cache_bytes > _CACHE_BYTES:
                self._cache.clear()
                self._cache_bytes = 0
            self._cache[off] = hit
            self._cache_bytes += len(text) + 64
        return hit

    # ------------------------------------------------------------- entries

    def entry_fields(self, off: int):
        """(fields dict, meta tuple) for the ENTRY object at `off`."""
        b = self.buf
        (size,) = struct.unpack_from("<Q", b, off + 8)
        seqnum, realtime, monotonic = struct.unpack_from("<QQQ", b, off + 16)
        boot_id = b[off + 40:off + 56].hex()
        (xor_hash,) = struct.unpack_from("<Q", b, off + 56)
        n = (size - 64) // self.item_size
        if self.compact:
            items = struct.unpack_from(f"<{n}I", b, off + 64)
        else:
            items = struct.unpack_from(f"<{2 * n}Q", b, off + 64)[::2]
        fields: dict = {}
        data = self._data
        for item in items:
            nv = data(item)
            if nv is None:
                continue
            name, text = nv
            if name == "_BOOT_ID":
                continue  # the entry's own boot ID is the column, as journalctl export prints it
            prev = fields.get(name)
            fields[name] = text if prev is None else f"{prev}\n{text}"
        fields["_BOOT_ID"] = boot_id
        cursor = (f"s={self.seqnum_id};i={seqnum:x};b={boot_id};m={monotonic:x};"
                  f"t={realtime:x};x={xor_hash:x}")
        meta = (str(realtime), str(monotonic), str(seqnum), self.seqnum_id, cursor)
        return fields, meta


def parse(path: str, counts: dict) -> dict:
    jf = JournalFile(path)
    try:
        entries, names = jf.scan()
    except BaseException:
        jf.close()
        raise
    counts["entries"] = len(entries)
    counts["describe"] = jf.describe()
    counts["state"] = jf.state
    counts["unknown_flags"] = jf.unknown_flags
    counts["header_entries"] = jf.n_entries
    schema = Schema(names, with_offset=True)

    def rows():
        try:
            for off in entries:
                fields, meta = jf.entry_fields(off)
                yield schema.row(fields, meta, off)
        finally:
            counts.update(jf.counts)
            jf.close()

    return {"columns": schema.columns, "column_types": schema.types, "rows": rows()}

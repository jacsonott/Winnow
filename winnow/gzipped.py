"""Gzip-compressed single files (file.json.gz, events.tsv.gz,
auth.log.1.gz) for import — read as the file inside, by the reader that
file would have had.

Two rules, kept apart on purpose:

- **Routing is by name.** ``x.json.gz`` is routed as ``x.json`` would
  be (``inner_name``): JSON, delimited, an IIS log, a body file, a plugin
  format, whatever the inner name says. ``x.tar.gz`` (and ``.tgz``) is a
  tarball, an archive of many files, and stays archive.py's. The import
  queue does the same in the browser (merge.js's ``gzipInner``).
- **Decompressing is by content.** A file whose first two bytes are the
  gzip magic is decompressed before a reader sees it, whatever it is
  called; one that is not is read as it is, ``.gz`` or not.

Every reader in the app wants a real file on disk — two passes over JSON,
encoding sniffs that seek, SQLite and workbooks that must be files, plugin
parsers that take a path — so the import decompresses into a private
temporary directory (``decompressed``) under the inner file's name (the
name is meaning: an IIS log's format, a plugin's log type, a SQLite
table's ``<stem>.<table>`` name) and reads that. The copy keeps the date
gzip stored for the original (its MTIME header) or else the ``.gz``'s
own, as archive.py's expansion does, because a secure log's year-less
stamps are dated from it. Nothing is written beside the evidence, and the
``.gz`` itself is never touched.

Untrusted input, so: the decompressed size is counted as it is written
and stops at ``MAX_BYTES`` (archive.py's cap — a bomb's size is found out
by decompressing it, never read from a header), a stream that is corrupt
or cut short is a ``GzipError`` (a ValueError, so a route answers 400 and
a job reports it), and a partial copy never survives a failure: a
truncated file would import as if it were whole. A full disk is let out
as the OSError it is, so diskspace.py says so.

Previews read only a head (``head``, ``inflate_head``): a bounded number
of decompressed bytes, tolerant of a stream that stops early, which is
what the browser sends for an upload's preview (the first megabyte of the
compressed file).

Stdlib only (gzip/zlib), per the airgap rule.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import os
import shutil
import struct
import tempfile
import zlib
from typing import Callable, Iterator

from winnow import archive
from winnow import diskspace

MAGIC = b"\x1f\x8b"
SUFFIX = ".gz"

# The most a single file may decompress to: archive.py's cap for a whole
# bundle, so a single file can never be the cheaper way to fill a disk.
MAX_BYTES = archive.MAX_TOTAL_BYTES
_CHUNK = 4 << 20
# How much of a head is decompressed at a time; small, so a stream that
# stops early still gives back nearly everything before the break.
_HEAD_PIECE = 64 << 10

# What reading a damaged stream raises: zlib.error for corrupt deflate
# data, EOFError for a stream cut short, gzip.BadGzipFile (an OSError) for
# a bad header or CRC.
_DAMAGE = (zlib.error, EOFError, gzip.BadGzipFile)


class GzipError(ValueError):
    pass


def inner_name(name: str) -> str | None:
    """`name` without its ``.gz`` when the name says one gzipped file —
    ``/x/events.json.gz`` -> ``/x/events.json``, ``auth.log.1.gz`` ->
    ``auth.log.1`` — else None: a name that doesn't end in .gz, a bare
    ``.gz``, or a tarball (``x.tar.gz``), which is many files."""
    if not name or not name.lower().endswith(SUFFIX):
        return None
    inner = name[: -len(SUFFIX)]
    base = os.path.basename(inner.replace("\\", "/"))
    if not base or base.lower().endswith(".tar"):
        return None
    return inner


def routing_name(name: str) -> str:
    """The name a file is routed by: the inner name of a gzipped single
    file, else the name itself."""
    return inner_name(name) or name


def is_gzip_bytes(head: bytes) -> bool:
    return head[:2] == MAGIC


def is_gzip(path: str) -> bool:
    """Whether the file is gzip-compressed — its first two bytes, never
    its name. False for anything that can't be read as a regular file."""
    try:
        with open(path, "rb") as f:
            return is_gzip_bytes(f.read(2))
    except OSError:
        return False


def header_mtime(head: bytes) -> int | None:
    """The MTIME a gzip header stores (gzip sets it to the original's
    when it compresses a file; logrotate's pipe leaves 0), or None."""
    if len(head) < 8 or not is_gzip_bytes(head):
        return None
    stored = struct.unpack("<I", head[4:8])[0]
    return stored or None


def mtime(path: str) -> float | None:
    """The date to put on a decompressed copy: the gzip header's MTIME,
    else the .gz's own modified time (archive._gz_mtime's rule)."""
    try:
        with open(path, "rb") as f:
            stored = header_mtime(f.read(8))
        return float(stored) if stored else os.path.getmtime(path)
    except OSError:
        return None


def uncompressed_size(path: str) -> int | None:
    """The size gzip's trailer records for the file inside (ISIZE: the
    last member's, modulo 4 GiB) — an estimate for a probe that wants a
    size before anything is decompressed (login records are a whole
    number of records), never a limit. None for a file that is not
    gzip, or whose trailer can't be read."""
    try:
        with open(path, "rb") as f:
            if not is_gzip_bytes(f.read(2)):
                return None
            f.seek(-4, os.SEEK_END)
            return struct.unpack("<I", f.read(4))[0] or None
    except (OSError, struct.error):
        return None


def _read_head(stream, n: int) -> tuple[bytes, bool]:
    """Up to `n` decompressed bytes from a GzipFile, and whether the
    stream ended within them.

    `read1`, not `read`: a buffered `read(n)` keeps reading until it has
    all n bytes, so a stream that is cut short raises with everything it
    had decompressed still inside the buffer, and a preview of a damaged
    file showed nothing at all. `read1` hands back each piece as it comes,
    and the break then arrives on the call after the last good one."""
    read1 = getattr(stream, "read1", None) or stream.read
    out = bytearray()
    ended = False
    try:
        while len(out) < n:
            piece = read1(min(_HEAD_PIECE, n - len(out)))
            if not piece:
                ended = True
                break
            out += piece
        if not ended and len(out) >= n:
            ended = not read1(1)
    except _DAMAGE:
        pass
    return bytes(out[:n]), ended


def inflate_head(data: bytes, n: int) -> tuple[bytes, bool]:
    """The first `n` decompressed bytes of a gzip stream's first bytes
    (an upload preview's head), and whether the stream ended in them.
    Bytes that aren't gzip come back as they are."""
    if not is_gzip_bytes(data):
        return data[:n], len(data) <= n
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as g:
        return _read_head(g, n)


def head(path: str, n: int) -> bytes:
    """A file's first `n` bytes as a reader will see them: decompressed
    when the file is gzip, as they are otherwise. For the probes that say
    what a file is (looks_like_kv_log and the rest) and the previews."""
    return head_and_end(path, n)[0]


def head_and_end(path: str, n: int) -> tuple[bytes, bool]:
    """`head`, and whether the file (decompressed, for a gzip) ended
    within those bytes."""
    with open(path, "rb") as raw:
        if not is_gzip_bytes(raw.read(2)):
            raw.seek(0)
            data = raw.read(n)
            return data, not raw.read(1)
        raw.seek(0)
        with gzip.GzipFile(fileobj=raw, mode="rb") as g:
            return _read_head(g, n)


def holds_tar(path: str) -> bool:
    """Whether a gzip file is a tarball whatever it is called (bundle.gz):
    an archive for archive.py, not one file to read."""
    return is_gzip(path) and archive._is_tar(path)


def decompress(path: str, dest: str, *, max_bytes: int | None = None,
               on_chunk: Callable[[int, int], None] | None = None) -> int:
    """Decompress `path` into `dest` (a new file) and return its size.

    `on_chunk(compressed_bytes_read, compressed_size)` is called as it
    goes — progress, and a cancel point: whatever it raises stops the copy
    and goes on to the caller. The copy is removed on every failure, and
    a stream that is damaged, or larger than `max_bytes` (MAX_BYTES by
    default — read here rather than bound as a default argument, so the
    cap can be lowered for a test instead of writing 32 GB), is a
    GzipError. The finished copy keeps the date the original had
    (``mtime``)."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    label = os.path.basename(path)
    total = os.path.getsize(path)
    written = 0
    try:
        with open(path, "rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as src, \
                open(dest, "xb") as out:
            while True:
                try:
                    chunk = src.read(_CHUNK)
                except _DAMAGE as e:
                    raise GzipError(f"{label} is not a complete gzip file — it is damaged or cut short "
                                    f"({e}); nothing was imported") from e
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise GzipError(f"{label} decompresses to more than "
                                    f"{diskspace.format_size(max_bytes)}, the limit that stops a "
                                    "decompression bomb; decompress it yourself and import what is "
                                    "inside it")
                out.write(chunk)
                if on_chunk is not None:
                    on_chunk(raw.tell(), total)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    stamp = mtime(path)
    if stamp:
        with contextlib.suppress(OSError, OverflowError, ValueError):
            os.utime(dest, (stamp, stamp))
    return written


def scratch_name(path: str, name: str | None = None) -> str:
    """The name a decompressed copy is written under: the inner name of
    `name` (the name the file arrived with — an upload's) or of `path`."""
    given = os.path.basename((name or path).replace("\\", "/"))
    return os.path.basename(inner_name(given) or given) or "decompressed"


@contextlib.contextmanager
def decompressed(path: str, name: str | None = None, *,
                 on_chunk: Callable[[int, int], None] | None = None) -> Iterator[str]:
    """The path to read `path` from: a decompressed copy, in a private
    temporary directory removed on the way out, when it is gzip — else
    `path` itself, untouched. `name` is the name it arrived with, when
    that is not the path's (an upload spooled under a temporary name)."""
    if not is_gzip(path):
        yield path
        return
    tmpdir = tempfile.mkdtemp(prefix="winnow-gunzip-")
    try:
        dest = os.path.join(tmpdir, scratch_name(path, name))
        decompress(path, dest, on_chunk=on_chunk)
        yield dest
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

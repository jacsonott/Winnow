"""Expanding evidence archives (.zip / .tar[.gz|.bz2|.xz] / .tgz / bare
.gz) for import — the front door for ESXi support bundles, UAC
collections, and any zipped log directory.

The shape of those inputs drives the design: a support bundle is an
archive of directories *containing more archives* (per-subsystem tgz
files, gzipped rotated logs like ``hostd.0.gz``), so a single-level
unzip yields a tree that still isn't importable. ``expand_archive``
therefore extracts recursively — inner archives expand in place (into a
directory named after them) and the inner archive file itself is removed
so the finished tree holds only real files; the ORIGINAL archive is
never touched. The result lands in a fresh ``<name>-extracted[-N]``
directory beside the archive (or under ``dest_root``), and the caller
hands that root to the existing directory-import flow, which already
does everything else: extension/pattern gates, include/exclude, folder
mirroring in the sidebar, closed-tab convention.

Untrusted-input rules, all enforced here rather than assumed:

- **Zip-slip**: every member path is normalized and must resolve inside
  the destination; absolute paths, drive letters and ``..`` escapes are
  skipped and reported, not extracted.
- **Bombs**: bytes are counted as they are COPIED (never trusted from
  headers) against a total budget, along with file-count and
  nesting-depth caps. Hitting a cap stops cleanly with what was
  extracted so far reported as partial.
- **Tar specials**: only regular files extract — symlinks, hardlinks,
  devices and FIFOs are skipped and reported (a hostile tarball's
  symlink-then-write dance never gets its first step).
- **Encrypted zip members** are skipped and reported by name.
- **Damage is per member.** A member that cannot be read (corrupt or
  truncated data, a compression method zipfile lacks — Deflate64, which
  Windows Explorer writes) or written (a name too long, a file where a
  directory must go) is skipped and reported, and the members after it
  still extract; a partial member never stays in the tree. An archive
  whose own structure ends early keeps what it held. Only an archive
  that yields nothing at all is an error.
- **Nothing is overwritten.** Two members with one name, or ``messages``
  beside ``messages.gz``, would otherwise leave whichever came last; the
  later one takes ``name (2).ext`` and is reported under ``renamed``.
- **Names fit the OS they land on**: on Windows a ``:`` in a member name
  would write an NTFS alternate data stream of the file before it, and
  ``?*<>|"`` raise — so each path component is sanitised there
  (``_member_name_for_os``).

Stdlib only (zipfile/tarfile/gzip/shutil), per the airgap rule.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import lzma
import os
import shutil
import tarfile
import zipfile
import zlib

# Extensions the import UI routes here. ``.gz`` covers both ``.tar.gz``
# (content-sniffed to tar below) and the bare gzipped rotated logs
# (auth.log.1.gz) that Linux/ESXi rotation produces.
ARCHIVE_IMPORT_EXTENSIONS = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".tbz2", ".txz"}

MAX_TOTAL_BYTES = 32 * 1024 * 1024 * 1024   # 32 GB extracted, across all nesting
MAX_TOTAL_FILES = 200_000
MAX_DEPTH = 6                               # archives-within-archives
_COPY_CHUNK = 4 << 20

_TAR_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".tbz2", ".txz")


class ArchiveError(ValueError):
    pass


# What reading a damaged or unusual archive raises — none of it a ValueError
# or an OSError the route would turn into a 400, so each used to escape as a
# 500 with half the tree on disk: a truncated stream (EOFError), corrupt
# deflate data (zlib.error), a bad header or CRC (BadZipFile, TarError), an
# .xz that isn't (LZMAError), a compression method zipfile cannot read
# (NotImplementedError).
_DAMAGE = (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error, lzma.LZMAError,
           NotImplementedError)
# ...and what one member can raise, which adds failing to WRITE it.
_MEMBER_ERRORS = _DAMAGE + (OSError,)
_DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
# zipfile's number for Deflate64 — named in the skip reason, because the
# way round it (extract with Explorer or 7-Zip, import the folder) is not
# something an analyst would guess from "compression method not supported".
_ZIP_DEFLATE64 = 9


def _raise_if_disk_full(e: BaseException) -> None:
    # Every later member would fail the same way; stop rather than list them.
    if isinstance(e, OSError) and e.errno in _DISK_FULL:
        raise e


def _member_reason(e: BaseException, info: zipfile.ZipInfo | None = None) -> str:
    if isinstance(e, NotImplementedError) and info is not None:
        if info.compress_type == _ZIP_DEFLATE64:
            return ("compressed with Deflate64, which Python cannot read (Windows Explorer writes it for "
                    "large files) — extract the archive with Explorer or 7-Zip and import the folder")
        return f"compression method {info.compress_type} is not supported"
    if isinstance(e, OSError) and e.filename:
        # Opening or creating a path: the write side. A decompressor's own
        # OSError (bz2's "Invalid data stream") names no file — that is
        # damage, below.
        return f"could not be written: {e.strerror or e}"
    return f"corrupt or truncated data: {e}"


def looks_like_archive(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(s) for s in _TAR_SUFFIXES) or n.endswith(".zip") or n.endswith(".gz")


def _kind(path: str) -> str | None:
    """'zip' | 'tar' | 'gz' | None — by content first (a .tgz IS a gz
    stream, so suffix order matters: tar before bare gz)."""
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        try:
            # is_tarfile only catches TarError; a corrupt gzip stream it
            # reads into raises zlib.error or EOFError straight out of it.
            if tarfile.is_tarfile(path):
                return "tar"
        except _DAMAGE:
            pass
        with open(path, "rb") as f:
            head = f.read(4)
        if head[:2] == b"\x1f\x8b":
            return "gz"
        # Python 3.14's is_zipfile reads the central directory as well as
        # the end record, so a zip whose directory is damaged stopped being
        # a zip there: an inner one stayed in the tree unreported, and an
        # outer one was refused as "not a zip, tar or gzip archive". Its
        # first local header still says what it is; opening it then fails,
        # and that is reported as damage, as on every earlier Python.
        if head == b"PK\x03\x04":
            return "zip"
    except OSError:
        return None
    return None


def _strip_archive_suffixes(name: str) -> str:
    base = os.path.basename(name)
    low = base.lower()
    for s in _TAR_SUFFIXES + (".zip", ".gz"):
        if low.endswith(s):
            return base[: -len(s)] or base
    return base


def _fresh_dest(archive_path: str, dest_root: str | None) -> str:
    parent = dest_root or os.path.dirname(os.path.abspath(archive_path))
    stem = _strip_archive_suffixes(archive_path) + "-extracted"
    cand = os.path.join(parent, stem)
    n = 1
    while os.path.exists(cand):
        n += 1
        cand = os.path.join(parent, f"{stem}-{n}")
    os.makedirs(cand)
    return cand


class _Budget:
    def __init__(self, root: str):
        self.root = root
        self.bytes = 0
        self.files = 0
        self.archives = 0
        self.skipped: list[dict] = []
        self.renamed: list[dict] = []
        self.truncated = False
        self.failure: str | None = None    # the first member/archive that could not be read
        self._next_copy: dict[str, int] = {}

    def skip(self, path: str, reason: str):
        if len(self.skipped) < 200:
            self.skipped.append({"path": path, "reason": reason})

    def fail(self, path: str, reason: str):
        """A skip because something was damaged or unwritable, as opposed
        to refused (zip-slip, a symlink): an archive where that is all that
        happened produced nothing, and that is an error."""
        self.failure = self.failure or f"{path}: {reason}"
        self.skip(path, reason)

    def label(self, path: str) -> str:
        """How a path is reported: relative to the extraction root, as the
        analyst will see it in the tree — or the archive's own name."""
        rel = os.path.relpath(path, self.root)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return os.path.basename(path)
        return rel.replace(os.sep, "/")

    def free_path(self, path: str) -> str:
        """`path`, or `name (2).ext`, `name (3).ext`… if something already
        has that name. The next number is remembered per path, so a
        hostile archive repeating one name 200,000 times costs 200,000
        steps, not a scan from (2) every time."""
        if not os.path.lexists(path):
            return path
        root, ext = os.path.splitext(path)
        n = self._next_copy.get(path, 2)
        while os.path.lexists(f"{root} ({n}){ext}"):
            n += 1
        self._next_copy[path] = n + 1
        return f"{root} ({n}){ext}"


# Characters Windows refuses in a file name (and ':' which it doesn't
# refuse but reads as "alternate data stream of the file before it"),
# control characters included; and the device names that are reserved
# with or without an extension (`aux.log` is the AUX device).
_WINDOWS_BAD_CHARS = set('<>:"|?*') | {chr(i) for i in range(32)}
_WINDOWS_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                     | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def _member_name_for_os(rel: str, windows: bool) -> str:
    """`rel` (a member's normalised relative path, '/'-separated) made
    writable as itself on this OS. A no-op on POSIX, where every one of
    these is a legal name. On Windows, each component: bad characters
    become '_'; a trailing dot or space (which Windows silently drops, so
    `a.` and `a` would collide) becomes '_'; a reserved device name gets a
    '_' prefix. Two names that come out the same are kept apart by
    _Budget.free_path, like any other clash. Taking the parameter rather
    than reading os.name is what lets the POSIX suite check the Windows
    rules."""
    if not windows:
        return rel
    out = []
    for comp in rel.split("/"):
        comp = "".join("_" if ch in _WINDOWS_BAD_CHARS else ch for ch in comp)
        kept = comp.rstrip(". ")
        comp = kept + "_" * (len(comp) - len(kept))
        if comp.split(".")[0].upper() in _WINDOWS_RESERVED:
            comp = "_" + comp
        out.append(comp)
    return "/".join(out)


def _safe_target(dest: str, member_name: str, budget: _Budget) -> str | None:
    # Zips written by Windows tools sometimes carry backslash separators.
    name = member_name.replace("\\", "/").lstrip("/")
    if not name:
        return None
    norm = os.path.normpath(name)
    if norm.startswith("..") or os.path.isabs(norm) or (len(norm) > 1 and norm[1] == ":"):
        budget.skip(member_name, "path escapes the extraction directory")
        return None
    return os.path.join(dest, _member_name_for_os(norm.replace(os.sep, "/"), windows=os.name == "nt"))


def _copy_stream(src, dst_path: str, budget: _Budget, source: str) -> bool:
    """Copy with the byte budget enforced DURING the copy — a bomb's real
    size is discovered as it decompresses, not read from its headers.

    Never over an existing file (see _Budget.free_path; `source` is how
    the rename is reported), and never leaving part of one: a member whose
    data turns out corrupt halfway is removed before the error goes on to
    the caller, which skips it — a truncated file in the tree would import
    as if it were whole."""
    wanted = dst_path
    dst_path = budget.free_path(dst_path)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    try:
        with open(dst_path, "wb") as out:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                budget.bytes += len(chunk)
                if budget.bytes > MAX_TOTAL_BYTES:
                    out.close()
                    os.unlink(dst_path)
                    budget.truncated = True
                    return False
                out.write(chunk)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dst_path)
        raise
    if dst_path != wanted and len(budget.renamed) < 200:
        budget.renamed.append({"path": source, "as": budget.label(dst_path)})
    return True


def _expand_zip(path: str, dest: str, budget: _Budget) -> bool:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if budget.files >= MAX_TOTAL_FILES:
                budget.truncated = True
                return False
            if info.flag_bits & 0x1:
                budget.skip(info.filename, "encrypted zip member")
                continue
            target = _safe_target(dest, info.filename, budget)
            if not target:
                continue
            label = budget.label(target)
            try:
                with zf.open(info) as src:
                    if not _copy_stream(src, target, budget, label):
                        return False
            except _MEMBER_ERRORS as e:
                _raise_if_disk_full(e)
                budget.fail(label, _member_reason(e, info))
                continue
            budget.files += 1
    return True


def _expand_tar(path: str, dest: str, budget: _Budget) -> bool:
    with tarfile.open(path, "r:*") as tf:
        members = iter(tf)
        while True:
            try:
                member = next(members, None)
            except _DAMAGE as e:
                # The archive itself stops here (a cut-off download, a
                # damaged block): nothing after this point can be found,
                # and everything before it is whole and stays.
                budget.fail(budget.label(path), f"archive ends early — truncated or corrupt ({e}); "
                                                "what came before that point was extracted")
                return True
            if member is None:
                return True
            if not member.isreg():
                if not member.isdir():
                    budget.skip(member.name, f"tar member is not a regular file ({member.type!r})")
                continue
            if budget.files >= MAX_TOTAL_FILES:
                budget.truncated = True
                return False
            target = _safe_target(dest, member.name, budget)
            if not target:
                continue
            label = budget.label(target)
            try:
                src = tf.extractfile(member)
                if src is None:
                    budget.skip(member.name, "unreadable tar member")
                    continue
                with src:
                    if not _copy_stream(src, target, budget, label):
                        return False
            except _MEMBER_ERRORS as e:
                _raise_if_disk_full(e)
                budget.fail(label, _member_reason(e))
                continue
            budget.files += 1


def _expand_gz(path: str, dest_file: str, budget: _Budget) -> bool:
    label = budget.label(path)
    try:
        with gzip.open(path, "rb") as src:
            ok = _copy_stream(src, dest_file, budget, label)
    except _MEMBER_ERRORS as e:   # zlib.error is not an OSError — it used to escape
        _raise_if_disk_full(e)
        budget.fail(label, f"corrupt gzip stream: {e}")
        return True
    if ok:
        budget.files += 1
    return ok


def _is_tar(path: str) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except _DAMAGE:
        return False


def _expand_one(path: str, dest: str, budget: _Budget) -> bool:
    kind = _kind(path)
    if kind == "zip":
        return _expand_zip(path, dest, budget)
    if kind == "tar":
        return _expand_tar(path, dest, budget)
    if kind == "gz":
        # A bare gzipped file (rotated log): dest is a DIRECTORY here; the
        # payload keeps its name minus the .gz.
        inner = _strip_archive_suffixes(path)
        return _expand_gz(path, os.path.join(dest, inner), budget)
    budget.skip(path, "not a recognized archive")
    return True


def expand_archive(path: str, dest_root: str | None = None) -> dict:
    """Extract `path` (and every archive found inside it, recursively) into
    a fresh directory. Returns {root, files, bytes, archives, skipped,
    truncated} — `truncated` means a size/count/depth cap stopped the
    expansion with a partial (but valid) tree."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ArchiveError(f"No file at {path}")
    if _kind(path) is None:
        raise ArchiveError(f"{os.path.basename(path)} is not a zip, tar or gzip archive")

    try:
        dest = _fresh_dest(path, dest_root)
    except OSError as e:
        # A read-only mount, or "<name>-extracted" past the filesystem's
        # name limit: nothing was written, and the route can say why
        # (this ran outside every handler, so it was a 500).
        raise ArchiveError(f"Could not create a folder to expand "
                           f"{os.path.basename(path)} into: {e}") from e
    budget = _Budget(dest)
    budget.archives += 1
    try:
        cont = _expand_one(path, dest, budget)

        # Nested pass: expand archives the previous pass produced, until a
        # sweep finds none (or the depth cap says a bundle this deep is
        # not a bundle). Inner archives expand into a directory named
        # after them and are then removed — the finished tree holds only
        # real files. One that cannot even be opened stays in the tree as
        # it was (the only copy of those bytes outside the original), is
        # reported, and is not tried again by later sweeps; the archives
        # after it still expand.
        depth = 0
        unopenable: set[str] = set()
        while cont and depth < MAX_DEPTH:
            found = []
            for dirpath, _dirs, files in os.walk(dest):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    if fp not in unopenable and looks_like_archive(f) and _kind(fp) is not None:
                        found.append(fp)
            if not found:
                break
            depth += 1
            for fp in sorted(found):
                inner_kind = _kind(fp)
                if inner_kind == "gz" and not _is_tar(fp):
                    inner_dest = os.path.dirname(fp)     # foo.log.gz -> foo.log beside it
                else:
                    inner_dest = os.path.join(os.path.dirname(fp), _strip_archive_suffixes(fp))
                    if os.path.exists(inner_dest):
                        inner_dest += ".extracted"
                budget.archives += 1
                try:
                    cont = _expand_one(fp, inner_dest, budget)
                except _MEMBER_ERRORS as e:
                    _raise_if_disk_full(e)
                    budget.fail(budget.label(fp), f"could not be opened: {e}")
                    unopenable.add(fp)
                    continue
                os.unlink(fp)
                if not cont:
                    break
        if depth >= MAX_DEPTH:
            budget.truncated = True
    except _MEMBER_ERRORS as e:
        # A corrupt outer archive with nothing extracted is an error; a
        # corrupt inner piece just marks the result partial.
        if budget.files == 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ArchiveError(f"Could not expand {os.path.basename(path)}: {e}")
        budget.skip(path, f"expansion stopped early: {e}")
        budget.truncated = True

    if budget.files == 0 and budget.failure:
        # Damage and nothing else: an empty tree would read as an empty
        # archive. Say what was wrong instead.
        shutil.rmtree(dest, ignore_errors=True)
        raise ArchiveError(f"Could not expand {os.path.basename(path)}: {budget.failure}")

    return {"root": dest, "files": budget.files, "bytes": budget.bytes,
            "archives": budget.archives, "skipped": budget.skipped,
            "renamed": budget.renamed, "truncated": budget.truncated}

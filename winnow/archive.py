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

Stdlib only (zipfile/tarfile/gzip/shutil), per the airgap rule.
"""

from __future__ import annotations

import gzip
import os
import shutil
import tarfile
import zipfile

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


def looks_like_archive(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(s) for s in _TAR_SUFFIXES) or n.endswith(".zip") or n.endswith(".gz")


def _kind(path: str) -> str | None:
    """'zip' | 'tar' | 'gz' | None — by content first (a .tgz IS a gz
    stream, so suffix order matters: tar before bare gz)."""
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        if tarfile.is_tarfile(path):
            return "tar"
        with open(path, "rb") as f:
            if f.read(2) == b"\x1f\x8b":
                return "gz"
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
    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.archives = 0
        self.skipped: list[dict] = []
        self.truncated = False

    def skip(self, path: str, reason: str):
        if len(self.skipped) < 200:
            self.skipped.append({"path": path, "reason": reason})


def _safe_target(dest: str, member_name: str, budget: _Budget) -> str | None:
    # Zips written by Windows tools sometimes carry backslash separators.
    name = member_name.replace("\\", "/").lstrip("/")
    if not name:
        return None
    norm = os.path.normpath(name)
    if norm.startswith("..") or os.path.isabs(norm) or (len(norm) > 1 and norm[1] == ":"):
        budget.skip(member_name, "path escapes the extraction directory")
        return None
    return os.path.join(dest, norm)


def _copy_stream(src, dst_path: str, budget: _Budget) -> bool:
    """Copy with the byte budget enforced DURING the copy — a bomb's real
    size is discovered as it decompresses, not read from its headers."""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "wb") as out:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                return True
            budget.bytes += len(chunk)
            if budget.bytes > MAX_TOTAL_BYTES:
                out.close()
                os.unlink(dst_path)
                budget.truncated = True
                return False
            out.write(chunk)


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
            with zf.open(info) as src:
                if not _copy_stream(src, target, budget):
                    return False
            budget.files += 1
    return True


def _expand_tar(path: str, dest: str, budget: _Budget) -> bool:
    with tarfile.open(path, "r:*") as tf:
        for member in tf:
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
            src = tf.extractfile(member)
            if src is None:
                budget.skip(member.name, "unreadable tar member")
                continue
            with src:
                if not _copy_stream(src, target, budget):
                    return False
            budget.files += 1
    return True


def _expand_gz(path: str, dest_file: str, budget: _Budget) -> bool:
    with gzip.open(path, "rb") as src:
        try:
            ok = _copy_stream(src, dest_file, budget)
        except (OSError, EOFError) as e:
            budget.skip(path, f"corrupt gzip stream: {e}")
            return True
        if ok:
            budget.files += 1
        return ok


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

    budget = _Budget()
    dest = _fresh_dest(path, dest_root)
    budget.archives += 1
    try:
        cont = _expand_one(path, dest, budget)

        # Nested pass: expand archives the previous pass produced, until a
        # sweep finds none (or the depth cap says a bundle this deep is
        # not a bundle). Inner archives expand into a directory named
        # after them and are then removed — the finished tree holds only
        # real files.
        depth = 0
        while cont and depth < MAX_DEPTH:
            found = []
            for dirpath, _dirs, files in os.walk(dest):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    if looks_like_archive(f) and _kind(fp) is not None:
                        found.append(fp)
            if not found:
                break
            depth += 1
            for fp in sorted(found):
                inner_kind = _kind(fp)
                if inner_kind == "gz" and not tarfile.is_tarfile(fp):
                    inner_dest = os.path.dirname(fp)     # foo.log.gz -> foo.log beside it
                else:
                    inner_dest = os.path.join(os.path.dirname(fp), _strip_archive_suffixes(fp))
                    if os.path.exists(inner_dest):
                        inner_dest += ".extracted"
                budget.archives += 1
                cont = _expand_one(fp, inner_dest, budget)
                os.unlink(fp)
                if not cont:
                    break
        if depth >= MAX_DEPTH:
            budget.truncated = True
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        # A corrupt outer archive with nothing extracted is an error; a
        # corrupt inner piece just marks the result partial.
        if budget.files == 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ArchiveError(f"Could not expand {os.path.basename(path)}: {e}")
        budget.skip(path, f"expansion stopped early: {e}")
        budget.truncated = True

    return {"root": dest, "files": budget.files, "bytes": budget.bytes,
            "archives": budget.archives, "skipped": budget.skipped,
            "truncated": budget.truncated}

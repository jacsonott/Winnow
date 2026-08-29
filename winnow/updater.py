"""Updating Winnow in place without losing the analyst's work.

The whole problem in one sentence: **every piece of analyst state lives
inside the install directory.** `workspace/` (case registry, saved
filters, tag template, cases_dir, plugin enablement, column layouts,
header nicknames) is `Path(__file__).parent / "workspace"`; installed
plugins are `HERE / "plugins"`; session exports are `sessions/`; and
unless the analyst pointed `cases_dir` elsewhere on first run, their case
files are in `cases/` right there too. "Download the new version and
replace the folder" destroys all of it, which is exactly why this module
exists rather than a line in the README telling people to do that.

So the rule everything here is built on:

    An update replaces the files Winnow ships. It never reads, writes or
    deletes anything else in the install directory.

`PROTECTED` below is that rule made explicit, and it deliberately mirrors
.gitignore — which is already a precise, maintained manifest of "user
state, not source". Two independent guards enforce it: nothing outside
the archive is ever written, and nothing under a protected path is ever
removed, even if a malformed archive contains one.

Design notes worth keeping:

- **Never checks on its own.** No startup ping, no background poll. A
  forensic tool that phones home unasked is a problem independent of the
  airgap rule in CLAUDE.md, and an analysis box may have no network at
  all. Every check here is something the analyst clicked or typed.
- **The offline path is first-class**, not a fallback: `--download-only`
  on a connected machine, `--from` on the airgapped one. Same verify,
  same backup, same apply.
- **Integrity comes from the zip's own CRCs** (`testzip()`), which costs
  nothing and catches the realistic failure — a bundle truncated or
  corrupted crossing a USB stick — without a signing story this tool has
  no way to anchor yet.
- **Backups are cheap** (the shipped tree is a few MB) so there is no
  reason not to take one every time. `rollback()` restores the newest.
  Case files are deliberately NOT part of it: a case opened by the newer
  version has already migrated itself, and silently reverting evidence
  state is far worse than the version mismatch it would paper over.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import paths

# The install this updater belongs to — see paths.py for why this is
# not computed from THIS file's location.
HERE = paths.INSTALL_ROOT

REPO = "jacsonott/Winnow"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"

# The branch beta testers and developers track. main is release-only —
# one squashed snapshot per release — so "the latest changes" means
# develop, not main. See CLAUDE.md's branch model.
DEV_BRANCH = "develop"


def branch_api(branch: str = DEV_BRANCH) -> str:
    return f"https://api.github.com/repos/{REPO}/commits/{branch}"


def branch_zip(branch: str = DEV_BRANCH) -> str:
    return f"https://github.com/{REPO}/archive/refs/heads/{branch}.zip"

# Where the updater keeps its own bookkeeping, inside the install.
BACKUP_DIR = ".winnow-backup"
MANIFEST = ".winnow-install.json"
KEEP_BACKUPS = 3

# Analyst state, never touched by an update. Mirrors .gitignore's
# user-state section — if you add one there, add it here.
PROTECTED = (
    "workspace/",     # case registry, saved filters, tag template, prefs.json
    "plugins/",       # installed plugins (plugins/README.md is shipped; see _is_protected)
    # Named session exports. Note these normally live beside the CASE file
    # (Store._sessions_dir), not here — this entry only matters when the
    # analyst kept their cases inside the install directory, which the
    # first-run cases_dir prompt steers away from. Harmless either way.
    "sessions/",
    "cases/",         # the default home for case files
    ".venv/", "venv/",
    "bench/.cache/", "bench/baselines/",
    "__pycache__/",
    BACKUP_DIR + "/",
)
PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".winnow-lock", ".winnow_case.json")

# plugins/README.md is source (it documents the folder), so the archive
# carries it and writing it back is correct. Nothing else under plugins/
# is ever written or removed.
PROTECTED_EXCEPTIONS = ("plugins/README.md",)


# Modules that used to live in the install root and now live in winnow/.
# An update only removes files a PREVIOUS update recorded, so an install
# with no manifest keeps its old top-level copies alongside the new
# package. They are inert for the app — nothing imports the bare names any
# more — but a plugin or dev script doing `import store` would silently
# load the stale one, which is a hard-to-see way to run half of an old
# version.
#
# Note what this can and cannot do. The updater that runs an upgrade is
# the one ALREADY INSTALLED, so the upgrade that first delivers the
# package layout is carried out by the old, pre-package updater and this
# sweep does not exist yet during it. The stale files therefore survive
# that one upgrade and are cleared by the next one, when this code is the
# code running. That is a deliberate accepted delay rather than an
# oversight: the alternative is deleting files from the install root at
# server startup, which is a much larger blast radius for a problem whose
# worst case is an inert duplicate.
#
# Spelled out rather than globbed — removing an entry is safe, adding a
# wrong one is not.
MOVED_INTO_PACKAGE = (
    # filter_defaults.py and header_defaults.py became defaults/*.json and
    # are gone entirely, so they need sweeping wherever they still sit.
    "enrich.py", "filter_defaults.py", "header_defaults.py", "make_fixture.py",
    "paths.py", "plugin_api.py", "store.py", "structparse.py", "timeparse.py",
    "updater.py", "version.py", "workspace.py", "xlsxread.py",
)


class UpdateError(Exception):
    """Anything the analyst can act on — no network, a corrupt bundle, a
    backup that isn't there. Surfaces as a message, not a traceback."""


def installed_version(root: Path | None = None) -> str:
    """The version of the install at `root` (default: this one), read from
    version.py as text rather than imported — `root` may be an archive we
    have not installed and must not execute."""
    root = Path(root or HERE)
    # winnow/version.py since the package move; the bare root path is the
    # pre-move layout, which an install being updated FROM still has.
    for rel in ("winnow/version.py", "version.py"):
        try:
            text = (root / rel).read_text(encoding="utf-8")
            break
        except OSError:
            continue
    else:
        return "unknown"
    for line in text.splitlines():
        if line.startswith("VERSION"):
            _, _, rhs = line.partition("=")
            return rhs.strip().strip('"').strip("'")
    return "unknown"


def _version_key(v: str) -> tuple:
    """Sortable form of a dotted version, so 1.10.0 > 1.9.0. Anything
    non-numeric sorts as 0 rather than raising — a tag like '1.2.0-rc1'
    should still compare sanely against 1.1.0."""
    parts = []
    for chunk in v.lstrip("vV").replace("-", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _version_key(candidate) > _version_key(current)


# ------------------------------------------------------------------ check

def check_for_update(timeout: float = 10.0, current: str | None = None,
                     _fetch=None) -> dict:
    """Ask GitHub for the latest release. Returns
    {current, latest, available, notes, url, published_at}.

    `_fetch` is the seam the tests use — the suite must never touch the
    network, and neither should CI."""
    current = current or installed_version()
    fetch = _fetch or _fetch_json
    try:
        rel = fetch(RELEASES_API, timeout)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        # Reaching GitHub and being told "no releases" is a different thing
        # from having no network, and telling an analyst to go find a
        # sneakernet bundle that doesn't exist would send them in circles.
        if e.code == 404:
            raise UpdateError(
                "No releases have been published for Winnow yet, so there is "
                "nothing to update to.") from e
        raise UpdateError(f"GitHub refused the update check ({e}).") from e
    except Exception as e:  # noqa: BLE001 — anything else here is "couldn't ask"
        raise UpdateError(
            f"Could not reach GitHub to check for updates ({e}). "
            "If this machine has no network, download the release on one that "
            "does and apply it with: python update.py --from <file>.zip"
        ) from e
    latest = (rel.get("tag_name") or "").lstrip("vV")
    if not latest:
        raise UpdateError("GitHub returned no release tag — nothing to compare against")
    return {
        "current": current,
        "latest": latest,
        "available": is_newer(latest, current),
        "notes": rel.get("body") or "",
        "url": rel.get("zipball_url") or "",
        "published_at": rel.get("published_at") or "",
        "html_url": rel.get("html_url") or "",
    }


def _fetch_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Winnow/{installed_version()}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_branch(branch: str = DEV_BRANCH, timeout: float = 10.0, _fetch=None) -> dict:
    """The tip of a branch, for the development path (`update.py --dev`).

    Deliberately NOT part of check_for_update: a branch is not a version,
    so there is nothing to compare against and no "are you up to date"
    answer to give — you either want that branch's current state or you
    don't. Returns {branch, sha, short, message, committed_at, url} so the
    sync can say what it landed on; a branch has no tag to name it by."""
    fetch = _fetch or _fetch_json
    try:
        c = fetch(branch_api(branch), timeout)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                f"There is no {branch!r} branch in {REPO}. If the repository has "
                "not been restructured yet, use a release instead: python update.py"
            ) from e
        raise UpdateError(f"GitHub refused the branch lookup ({e}).") from e
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Could not reach GitHub to read {branch} ({e}).") from e
    sha = c.get("sha") or ""
    commit = c.get("commit") or {}
    return {
        "branch": branch,
        "sha": sha,
        "short": sha[:7],
        "message": (commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
        "committed_at": ((commit.get("committer") or {}).get("date") or ""),
        "url": branch_zip(branch),
    }


def download(url: str, dest: Path, timeout: float = 120.0, _open=None) -> Path:
    """Fetch a release archive to `dest` (a file path or a directory).
    Downloads beside the target and renames on success, so an interrupted
    download can't leave a half-file that looks applicable."""
    opener = _open or (lambda u, t: urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": f"Winnow/{installed_version()}"}), timeout=t))
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / "winnow-update.zip"
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener(url, timeout) as r, open(tmp, "wb") as out:
            shutil.copyfileobj(r, out)
    except Exception as e:  # noqa: BLE001
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise UpdateError(f"Download failed: {e}") from e
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------ apply

def _is_protected(rel_path: str) -> bool:
    rel = rel_path.replace(os.sep, "/").lstrip("./")
    if rel in PROTECTED_EXCEPTIONS:
        return False
    if rel.endswith(PROTECTED_SUFFIXES):
        return True
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in PROTECTED)


# zipfile raises a whole family of things on a file that isn't a usable zip
# — BadZipFile, but also NotImplementedError for features it won't decode,
# EOFError/OSError on truncation, ValueError on a closed handle. An analyst
# who pointed --from at the wrong file, or carried a bundle across on a
# flaky USB stick, should get a sentence about it rather than a traceback.
_ZIP_ERRORS = (zipfile.BadZipFile, NotImplementedError, EOFError, OSError, ValueError)


def _unusable(archive: Path, e: Exception) -> UpdateError:
    return UpdateError(
        f"Could not read {archive.name} as a release archive ({e}) — "
        "it may be damaged or not a Winnow release. Download it again.")


def _archive_members(zf: zipfile.ZipFile) -> tuple[str, list[str]]:
    """(prefix, [paths relative to it]). GitHub's generated archives nest
    everything under a single `Winnow-<sha>/` directory; a hand-rolled zip
    may not. Detect rather than assume, so both apply identically."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise UpdateError("That archive is empty")
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        prefix = tops.pop() + "/"
        return prefix, [n[len(prefix):] for n in names]
    return "", names


def _read_manifest(root: Path) -> list[str]:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        return list(data.get("files") or [])
    except (OSError, ValueError):
        return []


def _write_manifest(root: Path, version: str, files: list[str], source: str = "release") -> None:
    (root / MANIFEST).write_text(json.dumps({
        "version": version,
        "source": source,   # "release", or "main@<sha>" for a dev sync
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": sorted(files),
    }, indent=1), encoding="utf-8")


def installed_source(root: Path | None = None) -> str:
    """Where this install's files came from — "release" or "main@<sha>".
    Worth surfacing: a box synced to main is running code no release has
    been cut from, which is a thing to know before trusting a version
    number in a report."""
    try:
        data = json.loads((Path(root or HERE) / MANIFEST).read_text(encoding="utf-8"))
        return data.get("source") or "release"
    except (OSError, ValueError):
        return "release"


def plan_update(archive: Path, root: Path | None = None) -> dict:
    """What applying `archive` would do, without doing any of it — the
    dry-run behind `--dry-run` and the UI's "what changes on disk"."""
    root = Path(root or HERE)
    archive = Path(archive)
    try:
        with zipfile.ZipFile(archive) as zf:
            prefix, members = _archive_members(zf)
            incoming = [m for m in members if not _is_protected(m)]
            new_version = _version_from_archive(zf, prefix)
    except UpdateError:
        raise
    except _ZIP_ERRORS as e:
        raise _unusable(archive, e) from e
    known = _read_manifest(root)
    added, changed = [], []
    for m in incoming:
        (changed if (root / m).exists() else added).append(m)
    # Only files a previous update recorded are eligible for removal —
    # never anything we can't prove Winnow put there.
    removed = [f for f in known if f not in set(incoming) and not _is_protected(f)]
    return {
        "version": new_version,
        "added": sorted(added),
        "changed": sorted(changed),
        "removed": sorted(removed),
        "protected": [p for p in PROTECTED if (root / p.rstrip("/")).exists()],
        "first_update": not known,
    }


def _version_from_archive(zf: zipfile.ZipFile, prefix: str) -> str:
    for rel in ("winnow/version.py", "version.py"):
        try:
            text = zf.read(prefix + rel).decode("utf-8")
            break
        except KeyError:
            continue
    else:
        return "unknown"
    for line in text.splitlines():
        if line.startswith("VERSION"):
            return line.partition("=")[2].strip().strip('"').strip("'")
    return "unknown"


def apply_update(archive: Path, root: Path | None = None, source: str = "release") -> dict:
    """Install `archive` over the install at `root`, after backing up every
    shipped file it will touch. Returns the plan that was carried out plus
    the backup path, so a caller can tell the analyst how to undo it."""
    root = Path(root or HERE)
    archive = Path(archive)
    if not archive.is_file():
        raise UpdateError(f"No such file: {archive}")

    try:
        zf = zipfile.ZipFile(archive)
    except _ZIP_ERRORS as e:
        raise _unusable(archive, e) from e
    with zf:
        try:
            bad = zf.testzip()  # CRC check — catches a bundle damaged in transit
        except _ZIP_ERRORS as e:
            raise _unusable(archive, e) from e
        if bad is not None:
            raise UpdateError(f"That archive is damaged (bad entry: {bad}) — download it again")
        prefix, members = _archive_members(zf)
        incoming = [m for m in members if not _is_protected(m)]
        if not any(m == "server.py" for m in incoming):
            raise UpdateError(
                "That doesn't look like a Winnow release (no server.py inside) — "
                "check you downloaded the source archive")
        plan = plan_update(archive, root)
        before = installed_version(root)
        backup = _backup(root, plan, before)
        try:
            for m in incoming:
                target = root / m
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(prefix + m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            for m in plan["removed"]:
                with contextlib.suppress(OSError):
                    (root / m).unlink()
            _sweep_moved_modules(root, incoming)
            _write_manifest(root, plan["version"], incoming, source)
        except Exception as e:  # noqa: BLE001 — a half-applied install is the one thing we can't leave behind
            restore(backup, root)
            raise UpdateError(
                f"Update failed and was rolled back ({e}). Winnow is still on {before}.") from e
    _prune_backups(root)
    return {**plan, "previous_version": before, "backup": str(backup)}


def _sweep_moved_modules(root: Path, incoming: list[str]) -> list[str]:
    """Delete the pre-package top-level modules, once the archive proves it
    carries the package form. Gated on winnow/store.py actually being in
    the incoming file list so this can never fire while updating between
    two pre-package versions."""
    if not any(m == "winnow/store.py" for m in incoming):
        return []
    swept = []
    for name in MOVED_INTO_PACKAGE:
        stale = root / name
        # Never touch something the new version still ships at that path.
        if name in incoming or not stale.is_file():
            continue
        with contextlib.suppress(OSError):
            stale.unlink()
            swept.append(name)
    return swept


def _backup(root: Path, plan: dict, version: str) -> Path:
    """Copy every file the update will overwrite or delete. Only those —
    the point is to be able to put the old version back, not to snapshot
    the analyst's cases (which can be enormous, and which an update never
    touches anyway)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = root / BACKUP_DIR / f"{version}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for rel in list(plan["changed"]) + list(plan["removed"]):
        src = root / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        saved.append(rel)
    # `added` files did not exist before, so restoring means removing them.
    (dest / "restore.json").write_text(json.dumps({
        "version": version,
        "restore": sorted(saved),
        "remove": sorted(plan["added"]),
    }, indent=1), encoding="utf-8")
    return dest


def list_backups(root: Path | None = None) -> list[Path]:
    root = Path(root or HERE)
    d = root / BACKUP_DIR
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if (p / "restore.json").is_file()),
                  key=lambda p: p.name)


def _prune_backups(root: Path) -> None:
    for old in list_backups(root)[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


def restore(backup: Path, root: Path | None = None) -> dict:
    """Put a backup back: rewrite what it saved, remove what the update
    added. Never touches protected paths, same as applying."""
    root = Path(root or HERE)
    backup = Path(backup)
    meta_path = backup / "restore.json"
    if not meta_path.is_file():
        raise UpdateError(f"Not a Winnow backup: {backup}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for rel in meta.get("restore", []):
        if _is_protected(rel):
            continue
        src = backup / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel)
    for rel in meta.get("remove", []):
        if _is_protected(rel):
            continue
        with contextlib.suppress(OSError):
            (root / rel).unlink()
    return {"version": meta.get("version", "unknown"), "backup": str(backup)}


def rollback(root: Path | None = None) -> dict:
    """Undo the most recent update."""
    backups = list_backups(root)
    if not backups:
        raise UpdateError("No backup to roll back to — nothing has been updated yet")
    return restore(backups[-1], root)

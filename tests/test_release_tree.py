"""What ships, checked against the REAL repository.

tests/test_release_script.py proves the release *mechanism* on a throwaway
repo. Nothing checked the mechanism against this tree, and that is exactly
where it went wrong: a 1.7 MB case file and two directories of development
material rode into the release archive across 224 changed files with a
green suite, because no test ever asked "is every top-level entry either
shipped on purpose or excluded on purpose?".

So this asks that question. A new top-level entry fails until it is put in
one list or the other — which is the point. Adding a directory the app
needs is a one-line edit here; adding one it doesn't is caught before an
analyst downloads it.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Everything the running application reads, plus the material a downloaded
# tree needs to explain itself (docs/, README, LICENSE — deliberately kept,
# see .gitattributes).
SHIPPED = {
    "server.py", "update.py", "winnow", "static", "examples", "plugins",
    "launch", "scripts", "docs", "README.md", "LICENSE", "requirements.txt",
    "pytest.ini", ".github", ".gitignore", ".gitattributes",
}

# Development-only: present in the branch, kept out of `git archive` by
# .gitattributes (and, for the ones main doesn't need either, out of main
# by scripts/release.py's DEV_ONLY).
DEV_ONLY = {
    "tests", "bench", "CLAUDE.md", ".claude", "requirements-dev.txt",
    "screenshots", "design",
}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout


def _tracked_top_level() -> set[str]:
    return {line.split("/", 1)[0] for line in _git("ls-files").splitlines() if line}


def _export_ignore_patterns() -> set[str]:
    """The export-ignore rules, read from .gitattributes itself.

    Not via `git check-attr`: that reports "unspecified" for both a
    directory and the files under it even where `git archive` prunes the
    whole subtree, so a test built on it passes for anything. The rules
    file is the mechanism, so read the mechanism.
    """
    out = set()
    for raw in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "export-ignore" not in line:
            continue
        out.add(line.split()[0].rstrip("/"))
    return out


def _worktree_is_clean() -> bool:
    return not _git("status", "--porcelain").strip()


def test_every_top_level_entry_is_classified():
    """A new directory has to be a deliberate ship-or-drop decision."""
    unknown = _tracked_top_level() - SHIPPED - DEV_ONLY
    assert not unknown, (
        f"top-level entries in neither list: {sorted(unknown)} — add each to "
        "SHIPPED (the app reads it) or DEV_ONLY (+ .gitattributes export-ignore)")


def test_dev_only_material_has_an_export_ignore_rule():
    rules = _export_ignore_patterns()
    for unwanted in sorted(DEV_ONLY):
        assert unwanted in rules, (
            f"{unwanted} rides into the release archive — it needs an "
            "export-ignore line in .gitattributes")
    shipped_but_excluded = sorted(SHIPPED & rules)
    assert not shipped_but_excluded, (
        f"the app reads {shipped_but_excluded}, but .gitattributes drops them "
        "from the download")


def test_the_built_archive_agrees_with_those_rules():
    """The end-to-end version, against HEAD — which is what CI and a real
    `git archive` see. Skipped on a dirty tree, where HEAD is not yet the
    thing being described."""
    if not _worktree_is_clean():
        pytest.skip("uncommitted changes — HEAD is not what this tree would ship")
    blob = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT,
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        entries = {n.split("/", 1)[0] for n in tf.getnames() if n}
    for want in ("server.py", "winnow", "static", "examples", "plugins", "requirements.txt"):
        assert want in entries, f"{want} is missing from the release archive"
    for unwanted in sorted(DEV_ONLY):
        assert unwanted not in entries, f"{unwanted} rides into the release archive"


def test_no_case_file_is_tracked():
    """`.gitignore` used to say `*.db`, which never matched `.db-winnow`."""
    from winnow.store import CASE_SUFFIX

    stray = [p for p in _git("ls-files").splitlines() if p.endswith(CASE_SUFFIX)]
    assert not stray, f"case files committed to the repo: {stray}"


@pytest.mark.parametrize("name", ["a.db-winnow", "a.db-winnow-wal", "a.db-winnow-shm",
                                 "cases/x.db-winnow"])
def test_a_case_file_anywhere_in_the_tree_is_ignored(name):
    """Opening a case inside a checkout must not dirty it: scripts/release.py
    refuses a dirty tree, and -wal/-shm siblings are not committable."""
    ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0
    assert ignored, f"{name} is not ignored — .gitignore does not cover the case suffix"


def test_the_declared_version_is_not_the_one_main_already_ships():
    """The release script reads version.py off origin/develop and refuses a
    version that disagrees with the tag; this catches the forgotten bump
    while it is still cheap."""
    from winnow import version

    tags = _git("tag", "-l").split()
    assert f"v{version.VERSION}" not in tags, (
        f"winnow/version.py still says {version.VERSION}, which is already tagged — "
        "bump it in the commit you want released")

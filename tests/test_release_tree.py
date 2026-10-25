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
import re
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
    "ruff.toml", "screenshots", "design",
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
        out.add(line.split()[0].strip("/"))   # "/tests/" is the rule for tests
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


def test_the_docs_ship_whole():
    """Everything under docs/, and everything README.md links to or shows,
    is in the archive — the "deliberately NOT applied to docs/" promise in
    .gitattributes, checked against what `git archive` actually writes.

    An export-ignore rule without a leading "/" matches its name at any
    depth: `screenshots/` (meant for the top-level folder) also dropped
    docs/screenshots/, so every image on the README was a broken link in
    the zip an analyst downloads, with the rule-reading tests above green.
    """
    if not _worktree_is_clean():
        pytest.skip("uncommitted changes — HEAD is not what this tree would ship")
    blob = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT,
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        shipped = {n for n in tf.getnames() if n}
    docs = {p for p in _git("ls-files", "docs").splitlines() if p}
    assert docs, "no tracked files under docs/ — the check below would pass vacuously"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    linked = {
        target.split("#", 1)[0].rstrip("/")
        for target in re.findall(r"\]\(([^)\s]+)\)", readme)
        if "://" not in target and not target.startswith(("#", "mailto:"))
    } - {""}
    missing = sorted(p for p in docs | linked if p not in shipped)
    assert not missing, f"left out of the release archive: {missing}"


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


def test_a_tagged_commit_declares_the_version_it_is_tagged_with():
    """The check worth having, in the direction that stays quiet.

    It used to assert the declared version was not already tagged, to
    catch a forgotten bump. That fires on every commit after a release —
    `develop` legitimately sits at the shipped version until someone opens
    the next cycle — so it would have failed CI on every PR from the
    moment 0.3.0 shipped until the next bump. A required check that is red
    for reasons nobody can act on is the kind that teaches people to
    re-run until green.

    The forgotten bump is already refused where it matters and cannot be
    skipped: `scripts/release.py` reads version.py off the source branch
    and exits rather than tag a version that disagrees. What that guard
    does NOT cover is a tag and a tree that disagree after the fact — a
    hand-moved tag, a cherry-pick onto a release commit — so that is what
    this asserts, and on an untagged commit it has nothing to say.
    """
    from winnow import version

    tags = [t for t in _git("tag", "--points-at", "HEAD").split() if t.startswith("v")]
    if not tags:
        return          # an ordinary commit declares whatever it likes
    assert f"v{version.VERSION}" in tags, (
        f"this commit is tagged {tags} but winnow/version.py says {version.VERSION} — "
        "the app would report a version its own tag contradicts")

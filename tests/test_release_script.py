"""The release script: snapshot develop onto main, minus the dev files.

Everything here runs against a throwaway repository built in tmp_path, so
nothing can touch the real one. The case that matters most is the SECOND
release: main is orphaned at the first, so it shares no ancestor with
develop, and any merge-based release would three-way merge against an
empty base and conflict on every file in the tree. Replacing the tree has
no merge base to disagree with — this proves that stays true."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.py"


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=check).stdout.strip()


def _release(repo, *args):
    return subprocess.run([sys.executable, str(repo / "scripts" / "release.py"), *args],
                          cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A miniature Winnow: app files, dev files, and a develop branch."""
    r = tmp_path / "repo"
    (r / "winnow").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "bench").mkdir()
    (r / "scripts").mkdir()
    (r / "winnow" / "version.py").write_text('VERSION = "0.1.0"\n')
    (r / "winnow" / "store.py").write_text("# app\n")
    (r / "server.py").write_text("# server\n")
    (r / "update.py").write_text("# update\n")
    (r / "requirements.txt").write_text("fastapi\n")
    (r / "tests" / "test_a.py").write_text("# test\n")
    (r / "bench" / "run.py").write_text("# bench\n")
    (r / "CLAUDE.md").write_text("# agent notes\n")
    (r / "requirements-dev.txt").write_text("pytest\n")
    (r / "scripts" / "release.py").write_text(SCRIPT.read_text())
    _git(r.parent, "init", "-qb", "main", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "commit", "-q", "--allow-empty", "-m", "a feature")
    _git(r, "branch", "-f", "develop")
    _git(r, "update-ref", "refs/remotes/origin/develop", "refs/heads/develop")
    return r


def _tracked(repo, ref):
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def test_first_release_orphans_main_and_drops_dev_files(repo):
    res = _release(repo, "0.1.0", "--write", "--orphan")
    assert res.returncode == 0, res.stderr

    files = _tracked(repo, "main")
    assert "server.py" in files and "winnow/store.py" in files
    for dev in ("tests/test_a.py", "bench/run.py", "CLAUDE.md", "requirements-dev.txt"):
        assert dev not in files, f"{dev} should not be on the release branch"
    # One commit, no history carried over — the "fresh start".
    assert _git(repo, "rev-list", "--count", "main") == "1"
    assert _git(repo, "tag", "-l") == "v0.1.0"
    # develop keeps everything, including its history.
    assert "tests/test_a.py" in _tracked(repo, "develop")
    assert int(_git(repo, "rev-list", "--count", "develop")) >= 2


def test_a_second_release_needs_no_merge_and_cannot_conflict(repo):
    """The load-bearing test. main and develop share no ancestor after the
    orphan, so `git merge develop` would refuse or conflict on every file.
    Replacing the tree has to keep working release after release."""
    assert _release(repo, "0.1.0", "--write", "--orphan").returncode == 0

    # Back to develop first: the release left the working tree on main,
    # where tests/ does not exist.
    _git(repo, "checkout", "-q", "develop")
    (repo / "winnow" / "newthing.py").write_text("# new\n")
    (repo / "winnow" / "version.py").write_text('VERSION = "0.2.0"\n')
    (repo / "tests" / "test_b.py").write_text("# more tests\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second feature")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "refs/heads/develop")

    res = _release(repo, "0.2.0", "--write")
    assert res.returncode == 0, res.stderr

    files = _tracked(repo, "main")
    assert "winnow/newthing.py" in files, "the new feature has to reach the release"
    assert "tests/test_b.py" not in files, "the new tests must not"
    assert 'VERSION = "0.2.0"' in _git(repo, "show", "main:winnow/version.py")
    # One commit per release, no merge commits, and the second one is an
    # ordinary descendant — so pushing it needs no force.
    assert _git(repo, "rev-list", "--count", "main") == "2"
    assert _git(repo, "rev-list", "--count", "--merges", "main") == "0"


def test_dry_run_changes_nothing(repo):
    before = _git(repo, "rev-parse", "HEAD")
    res = _release(repo, "0.1.0")
    assert res.returncode == 0
    assert "Dry run" in res.stdout
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "tag", "-l") == ""


def test_it_refuses_a_version_the_source_does_not_declare(repo):
    """Releasing 0.9.0 from a tree that says 0.1.0 would ship an app
    reporting a different version than its own tag."""
    res = _release(repo, "0.9.0", "--write")
    assert res.returncode == 2
    assert "says '0.1.0'" in res.stderr
    assert _git(repo, "tag", "-l") == ""


def test_it_refuses_a_dirty_tree(repo):
    (repo / "scratch.txt").write_text("uncommitted\n")
    res = _release(repo, "0.1.0", "--write")
    assert res.returncode == 2 and "dirty" in res.stderr


def test_it_refuses_to_reuse_a_tag(repo):
    assert _release(repo, "0.1.0", "--write", "--orphan").returncode == 0
    res = _release(repo, "0.1.0", "--write")
    assert res.returncode == 2 and "already exists" in res.stderr


def test_it_refuses_a_missing_source_branch(repo):
    res = _release(repo, "0.1.0", "--write", "--source", "nope")
    assert res.returncode == 2 and "no origin/nope" in res.stderr


def test_a_failure_puts_the_branch_back(repo, monkeypatch):
    """A half-done release that left the repo on a scratch branch with a
    mangled tree would be a bad afternoon."""
    before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    res = _release(repo, "0.1.0", "--write", "--source", "nope")
    assert res.returncode == 2
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""

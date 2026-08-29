#!/usr/bin/env python3
"""Cut a release: snapshot develop onto main, tagged.

    python scripts/release.py 0.2.0              # what it would do
    python scripts/release.py 0.2.0 --write      # do it (local only)
    python scripts/release.py 0.1.0 --write --orphan   # the first one

**main is not merged into, it is replaced.** Each release commit is a
snapshot of develop's tree minus the development-only files, so main's
history is one commit per release and nothing else.

That is not a stylistic choice, it is the only shape that works. main is
orphaned at the first release, so it shares no ancestor with develop —
`git merge develop` would refuse outright, and with
--allow-unrelated-histories it three-way merges against an empty base and
conflicts on every file in the repository. Every release. Replacing the
tree has no merge base to disagree with, so it cannot conflict, and it is
what makes "main holds fewer files than develop" free rather than a
permanent re-deletion chore.

Nothing here pushes. It prints the commands, because force-pushing main
should be a thing a person types.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Material dropped from the release branch.
#
# THE PAIRING RULE: anything dropped here must take its tests with it.
# main runs the same pytest checks as develop, so a test left behind that
# imports or reads a dropped path fails the release branch's own required
# check. This has bitten twice — bench/ and this script — so when adding
# an entry, grep tests/ for it first.
#
# Shorter than you might expect, and the reason is a chain: main runs the
# same required status checks as develop, those checks are `pytest`, and
# pytest on a tree with no tests/ exits 5 ("no tests collected") and fails
# the job — so main keeps tests/. Tests cannot run without their
# dependency list either (CI does `pip install -r requirements-dev.txt`),
# so that stays too, and .github/ stays so the checks exist at all.
#
# What users download is kept lean by .gitattributes instead: release
# archives are `git archive` output, which honours export-ignore, so
# tests/ is present in the branch for CI and absent from the zip an
# analyst installs. The branch and the artifact have different jobs.
DEV_ONLY = (
    ".claude",          # editor/agent launch config
    "bench",
    # Goes with bench/ — it imports it, so leaving it behind breaks pytest
    # COLLECTION on the release branch, which fails the very check keeping
    # tests/ there in the first place. Anything dropped here has to take
    # its tests with it.
    "tests/test_bench_harness.py",
    "CLAUDE.md",
    # Cuts releases FROM a develop checkout; a release tree is the wrong
    # place to run it from. Its test reads the script off disk, so — same
    # pairing rule as bench above — it has to go too.
    "scripts/release.py",
    "tests/test_release_script.py",
)

VERSION_FILE = Path("winnow/version.py")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class GitError(RuntimeError):
    pass


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}\n{(r.stderr or r.stdout).strip()}")
    return (r.stdout or "").strip()


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def preflight(version: str, source: str) -> str:
    """Everything that must be true before a release is worth making.
    Returns the source commit sha."""
    if not SEMVER.match(version):
        _fail(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    if git("status", "--porcelain"):
        _fail("working tree is dirty — commit or stash first")

    try:
        sha = git("rev-parse", f"origin/{source}")
    except GitError:
        _fail(f"no origin/{source} — fetch first, or pass --source")

    # The version in the tree being released has to be the version being
    # released, or the app reports one number while the tag says another.
    try:
        declared = git("show", f"origin/{source}:{VERSION_FILE.as_posix()}")
    except GitError:
        _fail(f"{VERSION_FILE} not found on {source}")
    found = next((ln.split("=")[1].strip().strip('"\'')
                  for ln in declared.splitlines() if ln.startswith("VERSION")), None)
    if found != version:
        _fail(f"{VERSION_FILE} on {source} says {found!r}, not {version!r} — "
              f"bump it on {source} first, in the commit you want released")

    if git("tag", "-l", f"v{version}"):
        _fail(f"tag v{version} already exists")
    return sha


def build_tree(source: str) -> list[str]:
    """Replace the index and working tree with `source`'s, minus DEV_ONLY.
    Returns the paths dropped, for the report."""
    # -f because after `checkout --orphan` every path is staged-as-new
    # against an empty tree, and git rm refuses those without it.
    git("rm", "-rqf", "--ignore-unmatch", ".")
    git("checkout", f"origin/{source}", "--", ".")
    dropped = []
    for rel in DEV_ONLY:
        if (ROOT / rel).exists():
            git("rm", "-rq", "--cached", "--ignore-unmatch", rel)
            subprocess.run(["rm", "-rf", rel], cwd=ROOT, check=True)
            dropped.append(rel)
    return dropped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="release.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", help="the version being released, e.g. 0.2.0")
    p.add_argument("--source", default="develop", help="branch to snapshot (default: develop)")
    p.add_argument("--target", default="main", help="release branch (default: main)")
    p.add_argument("--write", action="store_true",
                   help="actually create the commit and tag (still does not push)")
    p.add_argument("--orphan", action="store_true",
                   help="start the release branch fresh with no prior history. "
                        "Used once, for the first release")
    args = p.parse_args(argv)

    sha = preflight(args.version, args.source)
    print(f"Release {args.version}")
    print(f"  from:    origin/{args.source} @ {sha[:7]}")
    print(f"  onto:    {args.target}" + ("  (fresh, no prior history)" if args.orphan else ""))
    print(f"  dropped: {', '.join(DEV_ONLY)}")

    if not args.write:
        print("\nDry run — nothing changed. Re-run with --write to create the "
              "commit and tag.")
        return 0

    started_on = git("rev-parse", "--abbrev-ref", "HEAD")
    try:
        if args.orphan:
            git("checkout", "-q", "--orphan", f"release-{args.version}")
        else:
            git("checkout", "-q", args.target)
        dropped = build_tree(args.source)
        git("add", "-A")
        git("commit", "-q", "-m", f"Winnow {args.version}")
        git("tag", "-a", f"v{args.version}", "-m", f"Winnow {args.version}")
        if args.orphan:
            git("branch", "-M", args.target)
    except GitError as e:
        git("checkout", "-q", "--force", started_on)
        _fail(f"release aborted, returned to {started_on}:\n{e}")

    new = git("rev-parse", "HEAD")
    # Back to where you were. The commit and tag are refs; you do not have
    # to be standing on them to push. Staying on the release branch would
    # leave you in a tree with no bench/, no CLAUDE.md and — since it is
    # dropped too — no scripts/release.py, which is a confusing place to
    # be handed back control.
    git("checkout", "-q", started_on)
    print(f"\nCreated {args.target} {new[:7]} and tag v{args.version} "
          f"(dropped: {', '.join(dropped) or 'nothing'}).")
    print(f"You are back on {started_on}; the release is on {args.target}.")
    print("\nReview it, then push:")
    force = " --force-with-lease" if args.orphan else ""
    print(f"  git push{force} origin {args.target}")
    print(f"  git push origin v{args.version}")
    print(f"  gh release create v{args.version} --title 'Winnow {args.version}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

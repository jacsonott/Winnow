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

**Nothing here touches your checkout either.** The snapshot is built in a
throwaway index (`git read-tree` / `git rm --cached` / `git write-tree` /
`git commit-tree`) and the branch and tag are moved as refs, so your
branch, index and working tree are exactly as they were — including what
is gitignored in them. It used to check main out, rebuild the tree there
and `rm -rf bench .claude` to keep them out of `git add -A`, which also
deleted the maintainer's bench/baselines/, bench/.cache/ and
.claude/settings.local.json: per-machine files no commit holds.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
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


def git(*args: str, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, env=env)
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


def build_snapshot(source: str, parent: str | None, version: str) -> tuple[str, list[str]]:
    """Commit `source`'s tree minus DEV_ONLY, on top of `parent` (None for
    the orphan first release). Returns (commit sha, the DEV_ONLY paths
    that were there to drop).

    Built in a private index file, so neither the real index nor the
    working tree is read or written: what lands is exactly `source`'s
    blobs, and nothing in the checkout — tracked, untracked or ignored —
    can be added to the snapshot or removed from the disk."""
    with tempfile.TemporaryDirectory(prefix="winnow-release-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", f"origin/{source}", env=env)
        dropped = [rel for rel in DEV_ONLY if git("ls-files", "--", rel, env=env)]
        if dropped:
            # -f: the private index matches neither HEAD nor the working
            # tree by design, which plain `rm --cached` refuses.
            git("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", *dropped, env=env)
        tree = git("write-tree", env=env)
    parents = ["-p", parent] if parent else []
    return git("commit-tree", tree, *parents, "-m", f"Winnow {version}"), dropped


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
    ref = f"refs/heads/{args.target}"
    if started_on == args.target:
        # Moving the branch under a checkout would leave its index and
        # working tree describing the OLD release, as if every change in
        # this one were staged in reverse.
        _fail(f"you are on {args.target}, the branch this replaces — run it from "
              f"{args.source} (or any other branch)")
    try:
        old = git("rev-parse", "--verify", "-q", ref)
    except GitError:
        old = None
    parent = None
    if not args.orphan:
        # No local branch yet: build on origin's, as `git checkout main`
        # would have by creating one from it.
        try:
            parent = old or git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{args.target}")
        except GitError:
            _fail(f"no {args.target} branch here or on origin — pass --orphan for the "
                  "very first release")

    tag = f"v{args.version}"
    tagged = False
    try:
        new, dropped = build_snapshot(args.source, parent, args.version)
        git("tag", "-a", tag, "-m", f"Winnow {args.version}", new)
        tagged = True
        # Last, and compare-and-swap: if anything moved the branch since it
        # was read (or made it, where there was none), this fails rather
        # than drops that commit. --orphan replaces it outright, which is
        # what the first release means.
        if args.orphan:
            git("update-ref", ref, new)
        else:
            git("update-ref", ref, new, old or "")
    except BaseException as e:  # noqa: BLE001 — undone whatever it was, then reported
        # Only refs move, so undoing is deleting the tag if it was made;
        # the commit object is unreachable, and gc collects it.
        if tagged:
            try:
                git("tag", "-d", tag)
            except Exception:  # noqa: BLE001
                print(f"warning: could not remove tag {tag}; delete it by hand", file=sys.stderr)
        if not isinstance(e, Exception):
            raise
        what = str(e) if isinstance(e, GitError) else f"{type(e).__name__}: {e}"
        _fail(f"release aborted; {args.target} and your checkout are as they were "
              f"(you are on {started_on}):\n{what}")

    # The commit and tag are refs; you do not have to be standing on them
    # to push — and the release tree has no bench/, no CLAUDE.md and no
    # scripts/release.py, which is a confusing place to be handed control.
    print(f"\nCreated {args.target} {new[:7]} and tag v{args.version} "
          f"(dropped: {', '.join(dropped) or 'nothing'}).")
    print(f"You are still on {started_on}, untouched; the release is on {args.target}.")
    print("\nReview it, then ship (main takes releases via PR — its rulesets")
    print("require one, and CI then validates the snapshot itself):")
    print(f"  git push origin v{args.version}")
    print(f"  gh release create v{args.version} --title 'Winnow {args.version}'")
    print(f"  git branch -f release/v{args.version} {new[:7]}")
    print(f"  git push origin release/v{args.version}")
    print(f"  gh pr create --base {args.target} --head release/v{args.version}"
          f" --title 'Release {args.version}'")
    print("Merge that PR with a MERGE COMMIT only — squash/rebase rewrite the")
    print("SHA and orphan the tag. Delete release/* after; the tag keeps the")
    print("commit. (A direct `git push origin main` needs the rulesets off.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Upgrade an install of the last release to this tree, then roll it back.

    python tests/nightly_upgrade.py --from-ref v0.4.1 [--to-ref HEAD]

What an analyst does, end to end, with the updater they already have:

1. Installs --from-ref (its `git archive`, which is what a release zip
   is) into a fresh folder with its own virtualenv, starts it once with
   a case so it has a workspace, and adds the state an analyst's install
   carries: a plugin, a case file, a session export.
2. Runs THAT install's `update.py --from` on a zip of --to-ref, laid out
   the way GitHub's archives are (one top-level folder).
3. Checks that every file the new version ships is there as shipped, that
   none of the analyst's state changed by a byte, and that the upgraded
   install starts.
4. Runs `update.py --rollback` and checks the old version's files are back,
   the new version's additions are gone, and the state is still untouched.

The updater's own tests (tests/test_updater.py) cover each rule on a
synthetic tree. This covers the thing those can't: last release's updater
reading this release's real archive, which is the pairing every analyst's
next update is.

Not a test module (no test_ prefix); the nightly workflow runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STATE_DIRS = ("workspace", "plugins", "sessions", "cases")


def _unpack(ref: str, dest: Path) -> None:
    tar = subprocess.run(["git", "archive", "--format=tar", ref], cwd=REPO, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tf.extractall(dest)


def _files(root: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _state(install: Path) -> dict[str, str]:
    out = {}
    for d in STATE_DIRS:
        if (install / d).is_dir():
            out.update({f"{d}/{k}": v for k, v in _files(install / d).items()})
    return out


def _bin(env: Path, name: str) -> Path:
    return env / ("Scripts" if os.name == "nt" else "bin") / (name + (".exe" if os.name == "nt" else ""))


def _run(*cmd, cwd: Path) -> None:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def _matches(install: Path, tree: dict[str, str]) -> list[str]:
    """Files of `tree` that the install doesn't hold byte for byte."""
    return [rel for rel, digest in tree.items()
            if not (install / rel).is_file() or hashlib.sha256((install / rel).read_bytes()).hexdigest() != digest]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-ref", required=True, help="the release an analyst has installed (a tag)")
    ap.add_argument("--to-ref", default="HEAD", help="what they update to")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="winnow-upgrade-"))
    old, new, install = tmp / "old", tmp / "new", tmp / "install"
    _unpack(args.from_ref, old)
    _unpack(args.to_ref, new)
    old_tree, new_tree = _files(old), _files(new)
    shutil.copytree(old, install)

    # A zip laid out like GitHub's: everything under one top-level folder.
    archive = tmp / "winnow-next.zip"
    subprocess.run(["git", "archive", "--format=zip", "--prefix=Winnow-next/", "-o", str(archive), args.to_ref],
                   cwd=REPO, check=True)

    env = install / ".venv"   # protected by the updater, as an analyst's would be
    venv.create(env, with_pip=True)
    py = _bin(env, "python")
    _run(py, "-m", "pip", "install", "-q", "-r", "requirements.txt", cwd=install)
    smoke = [sys.executable, HERE / "smoke_server.py", "--root", install, "--python", py]
    _run(*smoke, cwd=install)

    (install / "plugins" / "analyst_plugin").mkdir(parents=True, exist_ok=True)
    (install / "plugins" / "analyst_plugin" / "notes.txt").write_text("an analyst's own plugin folder\n")
    (install / "cases").mkdir(exist_ok=True)
    (install / "cases" / "intrusion.db-winnow").write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
    (install / "sessions").mkdir(exist_ok=True)
    (install / "sessions" / "qc-pass.json").write_text('{"format": "winnow-case-session/1"}\n')
    state = _state(install)
    assert any(k.startswith("workspace/") for k in state), "starting the old install wrote no workspace"

    problems: list[str] = []
    _run(py, "update.py", "--from", archive, "-y", cwd=install)
    if missing := _matches(install, new_tree):
        problems.append(f"after the update, {len(missing)} shipped files are not as shipped: {missing[:10]}")
    if _state(install) != state:
        problems.append("the update changed the analyst's state: "
                        f"{sorted(set(_state(install).items()) ^ set(state.items()))[:10]}")
    _run(py, "-m", "pip", "install", "-q", "-r", "requirements.txt", cwd=install)
    _run(*smoke, cwd=install)

    # Starting it again registered another case in workspace/: that is
    # the server's write, not the updater's, so the rollback is held to
    # the state as it is now.
    state = _state(install)
    _run(py, "update.py", "--rollback", cwd=install)
    if missing := _matches(install, old_tree):
        problems.append(f"after the rollback, {len(missing)} files of the old version are not back: {missing[:10]}")
    added = sorted(set(new_tree) - set(old_tree))
    if left := [rel for rel in added if (install / rel).exists()]:
        problems.append(f"after the rollback, files only the new version ships are still there: {left[:10]}")
    if _state(install) != state:
        problems.append("the rollback changed the analyst's state")
    _run(*smoke, cwd=install)

    for p in problems:
        print("::error::" + p if os.environ.get("GITHUB_ACTIONS") else "FAIL: " + p, file=sys.stderr)
    if not problems:
        print(f"upgrade {args.from_ref} -> {args.to_ref} and rollback: OK")
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Where the install root is, and that everything agrees on it.

This file exists to make one specific mistake impossible: moving a module
into a subdirectory without noticing that it was the thing deciding where
the install lives. Two of those answers are expensive to get wrong —
workspace/ is the analyst's saved filters and case registry, and the
updater's root is what it backs up, replaces and protects — and both fail
*silently*, by looking in a directory that simply has nothing in it.

So these assertions are deliberately about identity, not about any
particular path string: whatever INSTALL_ROOT is, it has to be the
directory Winnow was installed into, and the workspace and the updater
have to be looking at that same directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from winnow import paths
import server
from winnow import updater
from winnow import workspace as WS


def test_install_root_is_the_directory_winnow_was_installed_into():
    root = paths.INSTALL_ROOT
    # The three things every install has at its top level. If INSTALL_ROOT
    # ever points a directory too deep (or too shallow), these are gone.
    assert (root / "server.py").is_file()
    assert (root / "update.py").is_file()
    assert (root / "static" / "index.html").is_file()


def test_install_root_is_resolved_and_absolute():
    assert paths.INSTALL_ROOT.is_absolute()
    assert paths.INSTALL_ROOT == paths.INSTALL_ROOT.resolve()


def test_the_workspace_lives_directly_under_the_install_root():
    """Asserted in a SUBPROCESS, because conftest's autouse
    isolate_workspace fixture repoints WS.WORKSPACE_DIR at a tmp dir for
    every test in the suite — reading it in-process would only prove the
    fixture works. What matters is the value a real install computes."""
    out = subprocess.run(
        [sys.executable, "-c",
         "from winnow import paths, workspace; "
         "print(workspace.WORKSPACE_DIR); print(paths.INSTALL_ROOT)"],
        cwd=str(paths.INSTALL_ROOT), capture_output=True, text=True, check=True).stdout.split()
    workspace_dir, install_root = Path(out[0]), Path(out[1])
    assert workspace_dir.parent == install_root
    assert workspace_dir.name == "workspace"


def test_the_updater_targets_the_install_root_not_its_own_directory():
    """updater.HERE is what apply_update rewrites, what _backup copies and
    what PROTECTED is relative to. If it ever becomes the directory the
    updater module sits in, an update protects nothing and replaces the
    wrong tree."""
    assert updater.HERE == paths.INSTALL_ROOT
    # And the paths it refuses to touch have to be meaningful from there.
    for guarded in ("workspace/", "plugins/", "sessions/", "cases/"):
        assert updater._is_protected(guarded + "anything.json")


def test_the_server_serves_static_from_the_install_root():
    assert server.HERE == paths.INSTALL_ROOT
    assert (server.HERE / "static" / "js" / "main.js").is_file()


def test_paths_module_imports_nothing_from_the_app():
    """It has to be safe for anything to depend on, including modules that
    would otherwise be a cycle."""
    src = Path(paths.__file__).read_text(encoding="utf-8")
    app_modules = ("store", "server", "workspace", "plugin_api", "updater", "timeparse")
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):
            assert not any(line.startswith(f"{kw} {m}") for kw in ("import", "from")
                           for m in app_modules), f"paths.py must not import app code: {line}"

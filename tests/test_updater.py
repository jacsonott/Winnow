"""Updating in place without losing the analyst's work.

The load-bearing test here is the one that builds a realistic install —
workspace/ with a case registry and prefs, an installed plugin, a session
export, a case file — updates it, and asserts every one of those is
byte-for-byte untouched. That is the entire promise of the feature; a
green suite that didn't check it would be worthless.

Nothing in this module touches the network: check_for_update takes a
`_fetch` seam and every apply works from a zip built in tmp_path.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from winnow import updater
from winnow.updater import UpdateError

# A minimal but honest shipped tree — apply_update refuses anything without
# server.py, which is the guard against pointing it at an unrelated zip.
SHIPPED = {
    "server.py": "print('server v1')\n",
    "store.py": "# store v1\n",
    "version.py": 'VERSION = "1.0.0"\n',
    "static/js/main.js": "// main v1\n",
    "plugins/README.md": "# drop plugins here (v1)\n",
}


def _make_install(root: Path, files: dict[str, str] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or SHIPPED).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _make_release(path: Path, version: str, files: dict[str, str],
                  prefix: str = "Winnow-abc123/") -> Path:
    """A release archive shaped like GitHub's generated one: everything
    nested under a single top-level directory."""
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(prefix + rel, text)
    return path


def _v2_files() -> dict[str, str]:
    f = dict(SHIPPED)
    f["version.py"] = 'VERSION = "1.1.0"\n'
    f["server.py"] = "print('server v2')\n"
    f["xlsxread.py"] = "# new in v2\n"   # added
    del f["store.py"]                    # removed in v2
    return f


@pytest.fixture
def install(tmp_path) -> Path:
    return _make_install(tmp_path / "winnow")


def _seed_user_state(root: Path) -> dict[str, str]:
    """Everything an analyst accumulates *inside* the install directory."""
    state = {
        "workspace/cases.json": json.dumps({"cases": [{"path": "/evidence/case1.db"}]}),
        "workspace/prefs.json": json.dumps({"cases_dir": "/mnt/evidence"}),
        "workspace/filters.json": json.dumps({"filters": [{"name": "My RDP sweep"}]}),
        "workspace/tags.json": json.dumps({"tags": [{"name": "TA-1"}]}),
        "plugins/my_parser.py": "# an analyst's own plugin\n",
        "plugins/vendor_tool/__init__.py": "# a plugin package\n",
        "sessions/case1-2026-08-01.json": json.dumps({"tags": []}),
        "cases/case1.db": "SQLite format 3\x00(not really)",
        "beside_the_server.db": "SQLite format 3\x00(a case file in the root)",
    }
    for rel, text in state.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return state


def test_update_never_touches_analyst_state(install, tmp_path):
    """The whole point of the feature."""
    state = _seed_user_state(install)
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())

    updater.apply_update(archive, install)

    for rel, text in state.items():
        assert (install / rel).read_text(encoding="utf-8") == text, f"{rel} was modified"
    # And the code really did update.
    assert updater.installed_version(install) == "1.1.0"
    assert (install / "server.py").read_text() == "print('server v2')\n"


def test_shipped_files_are_added_changed_and_removed(install, tmp_path):
    # A first update can't know what the old version shipped, so it removes
    # nothing; the second one can, because the first recorded a manifest.
    first = _make_release(tmp_path / "v1.zip", "1.0.0", SHIPPED)
    updater.apply_update(first, install)
    assert (install / "store.py").exists()

    second = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())
    plan = updater.plan_update(second, install)
    assert "xlsxread.py" in plan["added"]
    assert "server.py" in plan["changed"]
    assert plan["removed"] == ["store.py"]
    assert plan["first_update"] is False

    updater.apply_update(second, install)
    assert (install / "xlsxread.py").read_text() == "# new in v2\n"
    assert not (install / "store.py").exists()


def test_first_update_removes_nothing(install, tmp_path):
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())
    plan = updater.plan_update(archive, install)
    assert plan["first_update"] is True
    assert plan["removed"] == []
    updater.apply_update(archive, install)
    # store.py is gone from v2 but stays on disk — unremovable, not unsafe.
    assert (install / "store.py").exists()


def test_rollback_restores_the_previous_version(install, tmp_path):
    state = _seed_user_state(install)
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())
    updater.apply_update(archive, install)
    assert updater.installed_version(install) == "1.1.0"
    assert (install / "xlsxread.py").exists()

    res = updater.rollback(install)

    assert res["version"] == "1.0.0"
    assert updater.installed_version(install) == "1.0.0"
    assert (install / "server.py").read_text() == "print('server v1')\n"
    assert not (install / "xlsxread.py").exists(), "a file the update added must go back too"
    # Rollback is not allowed to disturb analyst state either.
    for rel, text in state.items():
        assert (install / rel).read_text(encoding="utf-8") == text


def test_an_archive_carrying_user_paths_cannot_overwrite_them(install, tmp_path):
    """Defense in depth: even a malformed or hostile archive containing
    workspace/ or plugins/ entries must not be able to write them."""
    _seed_user_state(install)
    hostile = dict(SHIPPED)
    hostile["workspace/prefs.json"] = '{"cases_dir": "/tmp/attacker"}'
    hostile["plugins/my_parser.py"] = "# replaced\n"
    hostile["cases/case1.db"] = "clobbered"
    archive = _make_release(tmp_path / "bad.zip", "1.1.0", hostile)

    plan = updater.plan_update(archive, install)
    assert not any(p.startswith(("workspace/", "cases/")) for p in plan["added"] + plan["changed"])
    assert "plugins/my_parser.py" not in plan["added"] + plan["changed"]

    updater.apply_update(archive, install)
    assert json.loads((install / "workspace/prefs.json").read_text())["cases_dir"] == "/mnt/evidence"
    assert (install / "plugins/my_parser.py").read_text() == "# an analyst's own plugin\n"
    assert (install / "cases/case1.db").read_text().startswith("SQLite format 3")
    # plugins/README.md IS shipped, so that one does get written.
    assert (install / "plugins/README.md").read_text() == "# drop plugins here (v1)\n"


def test_a_zip_slip_archive_is_refused_before_anything_is_written(install, tmp_path):
    """A member path that climbs out of the install root with `..` is a
    zip-slip: `root / member` would write wherever the traversal points.
    An archive is untrusted input (a downloaded release, or something
    handed to `update.py --from`), so the whole archive is refused and
    nothing outside the root is ever created."""
    import zipfile as _zip
    outside = tmp_path / "OUTSIDE_THE_ROOT.txt"
    assert not outside.exists()
    archive = tmp_path / "slip.zip"
    with _zip.ZipFile(archive, "w") as zf:
        for rel, text in SHIPPED.items():
            zf.writestr("Winnow-abc/" + rel, text)
        # After the "Winnow-abc/" prefix is stripped this climbs back out
        # to tmp_path/OUTSIDE_THE_ROOT.txt — a sibling of the install.
        zf.writestr("Winnow-abc/x/../../OUTSIDE_THE_ROOT.txt", "pwned")

    with pytest.raises(UpdateError, match="unsafe path"):
        updater.apply_update(archive, install)
    assert not outside.exists()
    assert updater.installed_version(install) == "1.0.0"

    # An ABSOLUTE member is refused the same way.
    archive2 = tmp_path / "abs.zip"
    with _zip.ZipFile(archive2, "w") as zf:
        for rel, text in SHIPPED.items():
            zf.writestr("Winnow-abc/" + rel, text)
        zf.writestr("Winnow-abc//etc/winnow_pwned", "pwned")
    with pytest.raises(UpdateError, match="unsafe path"):
        updater.plan_update(archive2, install)


def test_a_damaged_archive_is_refused_before_anything_is_written(install, tmp_path):
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())
    raw = bytearray(archive.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    archive.write_bytes(raw)
    # Whichever way zipfile objects — bad CRC, bad header, an encoding it
    # won't decode — the analyst gets a sentence, never a traceback.
    with pytest.raises(UpdateError):
        updater.apply_update(archive, install)
    assert updater.installed_version(install) == "1.0.0"
    assert (install / "server.py").read_text() == "print('server v1')\n"


def test_a_truncated_archive_is_refused(install, tmp_path):
    """The realistic sneakernet failure: the copy to the USB stick was cut
    short, so the zip's central directory never made it."""
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", _v2_files())
    archive.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])
    with pytest.raises(UpdateError):
        updater.apply_update(archive, install)
    assert updater.installed_version(install) == "1.0.0"


def test_an_unrelated_zip_is_refused(install, tmp_path):
    archive = _make_release(tmp_path / "nope.zip", "9.9.9", {"README.md": "some other project\n"})
    with pytest.raises(UpdateError, match="doesn't look like a Winnow release"):
        updater.apply_update(archive, install)
    assert updater.installed_version(install) == "1.0.0"


def test_a_flat_archive_applies_too(install, tmp_path):
    """A hand-rolled bundle without GitHub's wrapping directory."""
    archive = _make_release(tmp_path / "flat.zip", "1.1.0", _v2_files(), prefix="")
    updater.apply_update(archive, install)
    assert updater.installed_version(install) == "1.1.0"


def test_version_comparison_orders_numerically():
    assert updater.is_newer("1.10.0", "1.9.0")      # not string order
    assert updater.is_newer("1.1.0", "1.0.9")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("0.9.0", "1.0.0")
    assert updater.is_newer("v1.2.0", "1.1.0")      # tag names carry a leading v


def test_a_pre_release_sorts_before_its_release():
    """The key read "-rc1" as a fourth number, so 1.2.0-rc1 counted as NEWER
    than 1.2.0 (this file used to assert exactly that), and 0.3.0 newer
    than 0.3. Semver and PEP 440 agree on both."""
    assert updater.is_newer("1.2.0", "1.2.0-rc1")
    assert not updater.is_newer("1.2.0-rc1", "1.2.0")
    assert updater.is_newer("1.2.0-rc1", "1.1.9")
    assert updater.is_newer("1.2.0-rc10", "1.2.0-rc9")
    assert updater.is_newer("1.2.0-beta1", "1.2.0-alpha3")
    assert updater.is_newer("1.2.0", "1.2.0rc1")       # PEP 440 spelling
    assert not updater.is_newer("0.3.0", "0.3")
    assert not updater.is_newer("0.3", "0.3.0")
    assert not updater.is_newer("1.2.0+build7", "1.2.0")
    assert updater.is_newer("0.0.1", "unknown")


def test_check_reports_availability_without_touching_the_network():
    rel = {"tag_name": "v1.4.0", "body": "notes here",
           "zipball_url": "https://example/z", "published_at": "2026-08-01T00:00:00Z"}
    info = updater.check_for_update(current="1.3.0", _fetch=lambda u, t: rel)
    assert info["available"] is True and info["latest"] == "1.4.0"
    same = updater.check_for_update(current="1.4.0", _fetch=lambda u, t: rel)
    assert same["available"] is False


def test_a_check_with_no_network_says_what_to_do_instead():
    def boom(url, timeout):
        raise OSError("Network is unreachable")

    with pytest.raises(UpdateError, match="--from"):
        updater.check_for_update(current="1.0.0", _fetch=boom)


def test_no_releases_yet_is_not_reported_as_a_network_failure():
    """Today's actual state of the repo: reachable, but nothing published.
    Telling the analyst to go fetch a bundle offline would send them
    looking for a file that doesn't exist."""
    import urllib.error

    def not_found(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(UpdateError, match="No releases have been published"):
        updater.check_for_update(current="1.0.0", _fetch=not_found)


def test_check_branch_reports_the_tip_without_a_version_comparison(install):
    """A branch isn't a version, so there is no available/up-to-date answer
    to give — just what the tip currently is."""
    commit = {"sha": "abc1234def5678", "commit": {
        "message": "Resizable sidebar\n\nlonger body", "committer": {"date": "2026-08-29T10:00:00Z"}}}
    tip = updater.check_branch(_fetch=lambda u, t: commit)
    assert tip["branch"] == "develop"      # the channel beta testers track
    assert tip["short"] == "abc1234"
    assert tip["message"] == "Resizable sidebar"   # subject only
    assert tip["url"].endswith("refs/heads/develop.zip")
    assert "available" not in tip


def test_a_missing_dev_branch_says_so_rather_than_blaming_the_network(install):
    """Until the repository is restructured there is no develop branch,
    and 'could not reach GitHub' would send someone debugging their VPN."""
    import urllib.error

    def not_found(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(UpdateError, match="no 'develop' branch"):
        updater.check_branch(_fetch=not_found)


@pytest.mark.parametrize("argv, branch", [(["--dev"], "develop"),
                                          (["--dev", "--branch", "feat/x"], "feat/x")])
def test_a_dev_sync_names_the_branch_it_downloads(install, tmp_path, monkeypatch, capsys, argv, branch):
    """`update.py --dev` printed "Downloading main…" while it fetched
    develop (or --branch NAME)."""
    import update as update_cli
    monkeypatch.setattr(update_cli, "HERE", install)
    commit = {"sha": "abc1234def5678", "commit": {"message": "x", "committer": {"date": ""}}}
    real = updater.check_branch
    monkeypatch.setattr(updater, "check_branch", lambda b: real(b, _fetch=lambda u, t: commit))
    monkeypatch.setattr(updater, "download",
                        lambda url, dest: _make_release(tmp_path / "tip.zip", "1.1.0", _v2_files()))

    assert update_cli.main(argv + ["--dry-run", "--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"Downloading {branch}@abc1234" in out
    assert "Downloading main" not in out


def test_a_main_sync_is_recorded_as_such(install, tmp_path):
    """A box synced to main is running code no release was cut from —
    worth being able to tell after the fact."""
    assert updater.installed_source(install) == "release"
    archive = _make_release(tmp_path / "main.zip", "1.1.0", _v2_files(), prefix="Winnow-main/")
    updater.apply_update(archive, install, source="main@abc1234def")
    assert updater.installed_source(install) == "main@abc1234def"
    # And it is an ordinary update in every other respect.
    assert updater.installed_version(install) == "1.1.0"
    assert updater.list_backups(install)


def test_a_main_sync_still_protects_analyst_state(install, tmp_path):
    state = _seed_user_state(install)
    archive = _make_release(tmp_path / "main.zip", "1.1.0", _v2_files(), prefix="Winnow-main/")
    updater.apply_update(archive, install, source="main@deadbeef")
    for rel, text in state.items():
        assert (install / rel).read_text(encoding="utf-8") == text, f"{rel} was modified"
    # ...and rolling back a main sync works the same way.
    updater.rollback(install)
    assert updater.installed_version(install) == "1.0.0"


def test_upgrading_across_the_package_move_sweeps_the_old_modules(install, tmp_path):
    """An install that predates winnow/ has store.py etc. at the top level.
    The normal removal path can't clear them — it only removes what a
    previous update recorded, and a first update records nothing — so they
    would sit there shadowing `import store` for plugins and dev scripts."""
    (install / "store.py").write_text("# the old top-level module\n")
    (install / "timeparse.py").write_text("# ditto\n")
    (install / "make_fixture.py").write_text("# dev script that also moved\n")
    _seed_user_state(install)

    packaged = {
        "server.py": "print('server v2')\n",
        "update.py": "print('update')\n",
        "winnow/__init__.py": "",
        "winnow/store.py": "# store, now packaged\n",
        "winnow/timeparse.py": "# packaged\n",
        "winnow/version.py": 'VERSION = "2.0.0"\n',
        "version.py": 'VERSION = "2.0.0"\n',
        "plugins/README.md": "# drop plugins here\n",
    }
    updater.apply_update(_make_release(tmp_path / "pkg.zip", "2.0.0", packaged), install)

    assert not (install / "store.py").exists()
    assert not (install / "timeparse.py").exists()
    assert not (install / "make_fixture.py").exists()
    assert (install / "winnow" / "store.py").is_file()
    # The sweep is not allowed to touch analyst state, same as everything else.
    assert (install / "plugins" / "my_parser.py").is_file()
    assert (install / "workspace" / "prefs.json").is_file()


def test_the_sweep_never_fires_between_two_pre_package_versions(install, tmp_path):
    """Gated on the incoming archive actually carrying winnow/store.py —
    otherwise a routine 1.0 -> 1.1 update would delete the app."""
    (install / "store.py").write_text("# still top-level here\n")
    updater.apply_update(_make_release(tmp_path / "v2.zip", "1.1.0", _v2_files()), install)
    assert (install / "store.py").read_text() == "# still top-level here\n"


def test_the_sweep_keeps_a_module_the_new_version_still_ships_at_top_level(install, tmp_path):
    (install / "version.py").write_text('VERSION = "1.0.0"\n')
    packaged = {"server.py": "x\n", "winnow/__init__.py": "",
                "winnow/store.py": "# packaged\n", "version.py": 'VERSION = "2.0.0"\n'}
    updater.apply_update(_make_release(tmp_path / "pkg.zip", "2.0.0", packaged), install)
    # It is in the incoming list, so it was updated rather than swept.
    assert (install / "version.py").read_text() == 'VERSION = "2.0.0"\n'


def test_backups_are_pruned_but_the_newest_survive(install, tmp_path):
    for i in range(updater.KEEP_BACKUPS + 2):
        files = dict(SHIPPED)
        files["version.py"] = f'VERSION = "1.{i + 1}.0"\n'
        files["server.py"] = f"print('server v{i + 2}')\n"
        updater.apply_update(_make_release(tmp_path / f"v{i}.zip", "x", files), install)
    assert len(updater.list_backups(install)) == updater.KEEP_BACKUPS
    # The newest backup still rolls back one version.
    updater.rollback(install)
    assert updater.installed_version(install) == f"1.{updater.KEEP_BACKUPS + 1}.0"


def test_rollback_puts_the_install_manifest_back(install, tmp_path):
    """The manifest is what the next update decides removals from. Rollback
    left the newer one in place, so a file the old version shipped — put
    back by the rollback — was unknown to every later update, and
    installed_source() still named the build rolled away from."""
    updater.apply_update(_make_release(tmp_path / "v1.zip", "1.0.0", SHIPPED), install)
    before = (install / updater.MANIFEST).read_text()
    updater.apply_update(_make_release(tmp_path / "v2.zip", "1.1.0", _v2_files()), install,
                         source="develop@abc1234")
    assert updater.installed_source(install) == "develop@abc1234"

    updater.rollback(install)

    assert (install / updater.MANIFEST).read_text() == before
    assert updater.installed_source(install) == "release"
    # And the restored store.py is removable by a later update again.
    plan = updater.plan_update(_make_release(tmp_path / "v3.zip", "1.2.0", _v2_files()), install)
    assert "store.py" in plan["removed"]


def test_rollback_of_a_first_update_removes_the_manifest_it_wrote(install, tmp_path):
    updater.apply_update(_make_release(tmp_path / "v2.zip", "1.1.0", _v2_files()), install)
    updater.rollback(install)
    assert not (install / updater.MANIFEST).exists()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX file modes")
def test_a_file_new_to_the_install_keeps_its_executable_bit(install, tmp_path):
    """open(..., "wb") creates a new file at the umask default, so the first
    update that brought launch/winnow.sh left it not executable. git
    archive records the mode; it has to survive the trip."""
    import os
    import stat
    archive = tmp_path / "v2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for rel, text in _v2_files().items():
            zf.writestr("Winnow-abc123/" + rel, text)
        zi = zipfile.ZipInfo("Winnow-abc123/launch/winnow.sh")
        zi.create_system = 3
        zi.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(zi, "#!/bin/sh\n")
    updater.apply_update(archive, install)
    assert os.access(install / "launch/winnow.sh", os.X_OK)
    assert not os.access(install / "server.py", os.X_OK)


def test_a_shipped_file_can_become_a_directory_and_back(install, tmp_path):
    """Writes ran before removals, so a release turning a single-file
    plugin into a package (examples/plugins/geoip -> geoip/__init__.py)
    could never apply: creating geoip/ failed on the file still there, and
    every attempt rolled back. The reverse is the same problem."""
    as_file = {**SHIPPED, "examples/plugins/geoip": "single file\n"}
    as_dir = {**SHIPPED, "examples/plugins/geoip/__init__.py": "package\n",
              "version.py": 'VERSION = "1.2.0"\n'}
    updater.apply_update(_make_release(tmp_path / "a.zip", "1.1.0", as_file), install)
    updater.apply_update(_make_release(tmp_path / "b.zip", "1.2.0", as_dir), install)
    assert (install / "examples/plugins/geoip/__init__.py").read_text() == "package\n"

    updater.rollback(install)          # and undoing it turns it back into the file
    assert (install / "examples/plugins/geoip").read_text() == "single file\n"

    back = {**as_file, "version.py": 'VERSION = "1.3.0"\n'}
    updater.apply_update(_make_release(tmp_path / "b2.zip", "1.2.0", as_dir), install)
    updater.apply_update(_make_release(tmp_path / "c.zip", "1.3.0", back), install)
    assert (install / "examples/plugins/geoip").read_text() == "single file\n"


def test_rollback_without_a_backup_is_a_clear_message(install):
    with pytest.raises(UpdateError, match="No backup"):
        updater.rollback(install)


# ------------------------------------------------- what PROTECTED covers

ROOT = Path(__file__).resolve().parent.parent

# One sample per user-state pattern in .gitignore, at the depth .gitignore
# matches it: a pattern with no slash but a trailing one (`cases/`,
# `__pycache__/`) matches a directory of that name ANYWHERE, so nested
# samples are the ones that catch a root-only implementation.
USER_STATE_SAMPLES = [
    "case.db-winnow", "case.db-winnow-wal", "case.db-winnow-shm",
    "cases/nested/x.db-winnow", "evidence/host1.db-winnow-wal",
    "case.db", "case.db-wal", "case.db-shm", "case.db-winnow.winnow-lock",
    "evidence.csv", "triage/out.tsv", "notes/x.winnow_case.json",
    "winnow/__pycache__/store.cpython-313.pyc", "winnow/stray.pyc",
    "examples/cases/x.json", "examples/sessions/s.json", "tools/workspace/prefs.json",
    "venv/lib/x.py", ".venv/bin/python", "sub/.venv/x",
    "workspace/cases.json", "plugins/my_parser.py", "plugins/pkg/__init__.py",
    "sessions/a.json", "bench/.cache/fixture.csv", "bench/baselines/b.json",
    ".winnow-backup/0.3.0-x/restore.json", ".winnow-install.json",
]


@pytest.mark.parametrize("rel", USER_STATE_SAMPLES)
def test_protected_mirrors_gitignore(rel):
    """CLAUDE.md: PROTECTED mirrors .gitignore's user-state section. It
    didn't — case files are `.db-winnow`, which the `.db` suffix never
    matched, so a malformed archive carrying `case.db-winnow` emptied the
    analyst's case. Asked of git itself, so a sample that stops being user
    state per .gitignore fails here rather than silently going unchecked."""
    import subprocess
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel],
                             cwd=ROOT).returncode == 0
    assert ignored, f"{rel} is not user state per .gitignore — fix the sample"
    assert updater._is_protected(rel), f"{rel} is ignored by git but an update may write it"


def test_no_shipped_file_is_protected_or_refused():
    """The other direction: anything PROTECTED covers, an update never
    delivers. A shipped file caught by a pattern would stay at whatever
    version first installed it, forever, with no error anywhere — and one
    _safe_member refuses (a `:` in its name) would refuse every release."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    caught = [f for f in tracked if updater._is_protected(f)]
    assert caught == [], f"shipped files an update could never write: {caught}"
    refused = [f for f in tracked if not updater._safe_member(f)]
    assert refused == [], f"files that would make every release archive refused: {refused}"


@pytest.mark.parametrize("rel", [
    "Workspace/cases.json", "PLUGINS/foo/__init__.py", "CASE.DB", "Case.DB-Winnow",
    "Cases/x.json", "Evidence.CSV", ".Winnow-Backup/x/restore.json",
])
def test_protection_ignores_case(rel):
    """Windows and macOS file systems are case-insensitive, so an archive's
    `Workspace/cases.json` IS the analyst's workspace/cases.json there. The
    file system decides that, not the OS (an exFAT stick on Linux folds
    case too), so the check folds case everywhere."""
    assert updater._is_protected(rel)


@pytest.mark.parametrize("rel", ["./workspace/x.json", "./.winnow-backup/x/restore.json",
                                 "winnow//../workspace/x", "./case.db-winnow"])
def test_a_leading_dot_slash_does_not_slip_past_protection(rel):
    """`.lstrip("./")` strips characters, not a prefix: it turned
    `.winnow-backup/` into `winnow-backup/`, which no rule matched."""
    assert updater._is_protected(rel)


def test_an_archive_cannot_overwrite_a_case_file(install, tmp_path):
    (install / "case.db-winnow").write_text("ANALYST CASE DATA")
    (install / "case.db-winnow-wal").write_text("WAL")
    files = dict(SHIPPED)
    files["case.db-winnow"] = ""          # an empty placeholder shipped by mistake
    files["case.db-winnow-wal"] = ""
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", files)
    updater.apply_update(archive, install)
    assert (install / "case.db-winnow").read_text() == "ANALYST CASE DATA"
    assert (install / "case.db-winnow-wal").read_text() == "WAL"


def test_an_archive_cannot_plant_a_backup_for_rollback_to_restore(tmp_path):
    """The chain the reviewer built: an archive ships
    `.winnow-backup/zzz/restore.json` (unprotected, because of the lstrip),
    `zzz` sorts last so --rollback picks it, and restore() wrote its
    `../../payload.txt` entry outside the install."""
    install = _make_install(tmp_path / "a" / "winnow")
    files = dict(SHIPPED)
    files["payload.txt"] = "planted by the archive\n"
    files[".winnow-backup/zzz/restore.json"] = json.dumps(
        {"version": "1.0.0", "restore": ["../../payload.txt"], "remove": []})
    archive = _make_release(tmp_path / "v2.zip", "1.1.0", files)
    updater.apply_update(archive, install)

    assert not (install / updater.BACKUP_DIR / "zzz").exists()
    assert "zzz" not in [p.name for p in updater.list_backups(install)]
    updater.rollback(install)
    assert not (tmp_path / "payload.txt").exists()
    assert updater.installed_version(install) == "1.0.0"


@pytest.mark.parametrize("entry", ["../../escaped.txt", "/etc/escaped.txt", "D:escaped.txt",
                                   "..\\..\\escaped.txt"])
def test_rollback_refuses_a_backup_that_names_a_path_outside_the_install(tmp_path, entry):
    """restore() trusted restore.json. It is written by this module, but it
    sits in a directory anything can write to, and --rollback runs it
    with the analyst's privileges — so it gets the archive's checks."""
    install = _make_install(tmp_path / "a" / "winnow")
    bk = install / updater.BACKUP_DIR / "1.0.0-20260101-000000"
    bk.mkdir(parents=True)
    (bk / "server.py").write_text("print('server v0')\n")
    (bk / "restore.json").write_text(json.dumps(
        {"version": "1.0.0", "restore": ["server.py", entry], "remove": [entry]}))
    (tmp_path / "escaped.txt").write_text("not the updater's to touch")

    with pytest.raises(UpdateError, match="unsafe path"):
        updater.rollback(install)
    assert (tmp_path / "escaped.txt").read_text() == "not the updater's to touch"
    # Refused whole, like an archive: the safe entry was not applied either.
    assert (install / "server.py").read_text() == SHIPPED["server.py"]


@pytest.mark.parametrize("member", ["D:evil.py", "sub/D:evil.py", "server.py:stream", "C:x/y.py"])
def test_drive_relative_and_stream_members_are_refused(install, tmp_path, member):
    """On Windows `root / "D:evil.py"` discards the install root (a
    drive-relative path), and `sub/x:y` writes an NTFS alternate data
    stream. Only a two-character `X:` first component was refused."""
    assert not updater._safe_member(member)
    files = dict(SHIPPED)
    files[member] = "print('pwned')\n"
    with pytest.raises(UpdateError, match="unsafe path"):
        updater.apply_update(_make_release(tmp_path / "bad.zip", "1.1.0", files), install)


# ---------------------------------------------------- which backup is newest

def _release_at(tmp_path: Path, version: str) -> Path:
    files = dict(SHIPPED)
    files["version.py"] = f'VERSION = "{version}"\n'
    files["server.py"] = f"print('server {version}')\n"
    return _make_release(tmp_path / f"{version}.zip", version, files)


def test_rollback_restores_the_newest_backup_not_the_last_by_name(tmp_path):
    """Backups were sorted by directory name, which starts with the
    version: `0.10.0-…` sorts before `0.9.0-…`, so after 0.9 -> 0.10 ->
    0.11 a rollback restored 0.9.0, skipping a version."""
    install = _make_install(tmp_path / "winnow", {**SHIPPED, "version.py": 'VERSION = "0.9.0"\n'})
    updater.apply_update(_release_at(tmp_path, "0.10.0"), install)
    updater.apply_update(_release_at(tmp_path, "0.11.0"), install)

    res = updater.rollback(install)
    assert res["version"] == "0.10.0"
    assert updater.installed_version(install) == "0.10.0"


def test_pruning_keeps_the_newest_backups_not_the_last_by_name(tmp_path):
    install = _make_install(tmp_path / "winnow", {**SHIPPED, "version.py": 'VERSION = "0.8.0"\n'})
    for v in ("0.9.0", "0.10.0", "0.11.0", "0.12.0"):
        updater.apply_update(_release_at(tmp_path, v), install)
    kept = [json.loads((p / "restore.json").read_text())["version"]
            for p in updater.list_backups(install)]
    assert kept == ["0.9.0", "0.10.0", "0.11.0"]


def test_backups_from_before_the_timestamp_still_sort_by_when_they_were_taken(install):
    """Backups an older updater wrote carry no creation time in
    restore.json; the stamp in their directory name is the fallback."""
    for name, version in (("0.10.0-20260301-120000", "0.10.0"),
                          ("0.9.0-20260201-120000", "0.9.0")):
        d = install / updater.BACKUP_DIR / name
        d.mkdir(parents=True)
        (d / "restore.json").write_text(json.dumps({"version": version, "restore": [], "remove": []}))
    assert [p.name for p in updater.list_backups(install)] == [
        "0.9.0-20260201-120000", "0.10.0-20260301-120000"]
    # ...and one this version writes is newer than both.
    updater.apply_update(_release_at(install.parent, "1.1.0"), install)
    assert json.loads((updater.list_backups(install)[-1] / "restore.json").read_text())["version"] == "1.0.0"


# ----------------------------------------------------------- interrupted

def _ten_module_install(root: Path) -> tuple[Path, dict[str, str]]:
    files = dict(SHIPPED)
    files.update({f"winnow/m{i}.py": f"old {i}\n" * 50 for i in range(10)})
    return _make_install(root, files), files


def _ten_module_release(path: Path) -> Path:
    files = dict(SHIPPED)
    files["version.py"] = 'VERSION = "1.1.0"\n'
    files.update({f"winnow/m{i}.py": f"NEW {i}\n" * 50 for i in range(10)})
    return _make_release(path, "1.1.0", files)


def _interrupt_the_nth_copy(monkeypatch, n: int, partial: bool = False):
    """Ctrl+C arriving while apply_update copies its n-th file. `partial`
    writes some of the file first, the way a real interrupt mid-copy does."""
    import shutil
    real = shutil.copyfileobj
    count = {"k": 0}

    def flaky(src, dst, *a, **kw):
        count["k"] += 1
        if count["k"] == n:
            if partial:
                dst.write(src.read(7))
            raise KeyboardInterrupt
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copyfileobj", flaky)


def test_ctrl_c_mid_update_puts_the_previous_version_back(tmp_path, monkeypatch):
    """apply_update restored on `except Exception`, which KeyboardInterrupt
    is not: Ctrl+C on the sixth file left five modules new, one EMPTY and
    four old — an install that is neither version and won't start — and
    no manifest saying so."""
    install, files = _ten_module_install(tmp_path / "winnow")
    archive = _ten_module_release(tmp_path / "v2.zip")
    _interrupt_the_nth_copy(monkeypatch, 6)

    with pytest.raises(KeyboardInterrupt):     # still means "stop" to a caller
        updater.apply_update(archive, install)

    for rel, text in files.items():
        assert (install / rel).read_text(encoding="utf-8") == text, f"{rel} was left changed"
    assert updater.installed_version(install) == "1.0.0"
    assert not (install / updater.MANIFEST).exists()
    leftovers = [p.name for p in install.rglob("*") if ".winnow-update" in p.name]
    assert not leftovers, f"temporary files left in the install: {leftovers}"


def test_a_file_interrupted_mid_write_is_never_left_half_written(tmp_path, monkeypatch):
    """Even when putting the old version back fails too (a second Ctrl+C,
    a full disk), no shipped file may be left truncated: each one is
    either the old version or the new one, because a file is written
    beside its target and renamed over it."""
    install, files = _ten_module_install(tmp_path / "winnow")
    archive = _ten_module_release(tmp_path / "v2.zip")

    def restore_fails(*a, **k):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        _interrupt_the_nth_copy(m, 6, partial=True)
        m.setattr(updater, "restore", restore_fails)
        with pytest.raises(UpdateError) as info:
            updater.apply_update(archive, install)
    # The message says what state the install is in and how to fix it.
    msg = str(info.value)
    assert "--rollback" in msg and "1.0.0" in msg

    new = {rel: text.replace("old", "NEW") for rel, text in files.items()}
    for rel in files:
        if rel.startswith("winnow/m"):
            got = (install / rel).read_text(encoding="utf-8")
            assert got in (files[rel], new[rel]), f"{rel} is half-written: {got[:30]!r}"
    leftovers = [p.name for p in install.rglob("*") if ".winnow-update" in p.name]
    assert not leftovers, f"temporary files left in the install: {leftovers}"
    # And the backup that message points at really does put it back.
    updater.rollback(install)
    for rel, text in files.items():
        assert (install / rel).read_text(encoding="utf-8") == text


def test_the_cli_says_the_update_was_rolled_back_not_cancelled(tmp_path, monkeypatch, capsys):
    """update.py printed "Cancelled." for a Ctrl+C mid-apply, which reads
    as "nothing happened" about an install that had been half-replaced."""
    import update as update_cli
    install, _files = _ten_module_install(tmp_path / "winnow")
    archive = _ten_module_release(tmp_path / "v2.zip")
    monkeypatch.setattr(update_cli, "HERE", install)
    _interrupt_the_nth_copy(monkeypatch, 6)

    rc = update_cli.main(["--from", str(archive), "-y"])

    assert rc == 130
    err = capsys.readouterr().err
    assert "rolled back" in err and "1.0.0" in err
    assert "nothing was changed" not in err


def test_the_cli_still_says_cancelled_when_nothing_was_touched(tmp_path, monkeypatch, capsys):
    import update as update_cli
    install, files = _ten_module_install(tmp_path / "winnow")
    archive = _ten_module_release(tmp_path / "v2.zip")
    monkeypatch.setattr(update_cli, "HERE", install)

    def ctrl_c(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", ctrl_c)      # Ctrl+C at "Apply it? [y/N]"

    assert update_cli.main(["--from", str(archive)]) == 130
    assert "nothing was changed" in capsys.readouterr().err
    for rel, text in files.items():
        assert (install / rel).read_text(encoding="utf-8") == text


def test_ctrl_c_while_backing_up_leaves_no_half_backup_behind(tmp_path, monkeypatch):
    """A backup with no restore.json is never listed, so it is never
    restored (right) and never pruned either (wrong): one Ctrl+C during
    the backup left a directory of copied modules in the install for
    good, uncounted by KEEP_BACKUPS."""
    import shutil
    install, files = _ten_module_install(tmp_path / "winnow")
    archive = _ten_module_release(tmp_path / "v2.zip")
    real = shutil.copy2
    count = {"k": 0}

    def flaky(src, dst, *a, **kw):
        count["k"] += 1
        if count["k"] == 4:
            raise KeyboardInterrupt
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(shutil, "copy2", flaky)

    with pytest.raises(updater.UpdateInterrupted, match="before anything was changed"):
        updater.apply_update(archive, install)

    for rel, text in files.items():
        assert (install / rel).read_text(encoding="utf-8") == text
    backups = install / updater.BACKUP_DIR
    assert not backups.exists() or not any(backups.iterdir()), \
        f"partial backup left behind: {[p.name for p in backups.iterdir()]}"


# --------------------------------------------------------------- HTTP layer

def _client(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "STORE", store)
    return server, TestClient(server.app)


HEADERS = {"X-Timeline-Lite-Client": "1"}


def test_version_route_reports_the_installed_version(store, monkeypatch):
    from winnow import version as version_module
    server, client = _client(store, monkeypatch)
    res = client.get("/api/version", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == version_module.VERSION
    assert body["is_release"] is True   # no manifest in a dev checkout


def test_version_route_distinguishes_a_develop_build(store, monkeypatch):
    """version.py on develop still reads as the last released number, so
    the number alone describes a build nobody can reproduce."""
    server, client = _client(store, monkeypatch)
    monkeypatch.setattr(server.updater, "installed_source", lambda *a: "develop@abc1234")
    body = client.get("/api/version", headers=HEADERS).json()
    assert body["source"] == "develop@abc1234"
    assert body["is_release"] is False


def test_check_route_surfaces_an_offline_box_as_a_400(store, monkeypatch):
    server, client = _client(store, monkeypatch)

    def boom(**kwargs):
        raise UpdateError("Could not reach GitHub ... python update.py --from <file>.zip")

    monkeypatch.setattr(server.updater, "check_for_update", boom)
    res = client.post("/api/updates/check", json={}, headers=HEADERS)
    assert res.status_code == 400
    assert "--from" in res.json()["detail"]


@pytest.mark.parametrize("body", [{"confirm": False}, {"confirm": False, "dev": True}])
def test_apply_route_refuses_without_confirmation(store, monkeypatch, body):
    server, client = _client(store, monkeypatch)
    called = []
    monkeypatch.setattr(server.updater, "apply_update",
                        lambda *a, **k: called.append(a) or {})
    res = client.post("/api/updates/apply", json=body, headers=HEADERS)
    assert res.status_code == 400
    assert not called, "a POST without confirm must not install anything"


_TIP = {"branch": "develop", "sha": "abc1234def5678", "short": "abc1234",
        "message": "Resizable sidebar", "committed_at": "2026-08-29T10:00:00Z",
        "url": "https://github.com/jacsonott/Winnow/archive/refs/heads/develop.zip"}


def _no_release_lookup(**kwargs):
    raise AssertionError("the develop path must not ask for the latest release")


def test_check_route_reads_the_develop_tip_when_asked(store, monkeypatch):
    """Settings → Updates' Alt+right-click: `update.py --dev --check`."""
    server, client = _client(store, monkeypatch)
    monkeypatch.setattr(server.updater, "check_branch", lambda b: {**_TIP, "branch": b})
    monkeypatch.setattr(server.updater, "check_for_update", _no_release_lookup)
    res = client.post("/api/updates/check", json={"dev": True}, headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["branch"] == "develop"
    assert res.json()["short"] == "abc1234"


def test_check_route_with_no_body_is_still_the_release_check(store, monkeypatch):
    server, client = _client(store, monkeypatch)
    monkeypatch.setattr(server.updater, "check_for_update",
                        lambda **k: {"available": False, "current": "1.0.0", "latest": "1.0.0"})
    monkeypatch.setattr(server.updater, "check_branch",
                        lambda *a: pytest.fail("a plain check must not read a branch"))
    res = client.post("/api/updates/check", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["latest"] == "1.0.0"


def test_apply_route_dev_records_the_develop_commit(store, monkeypatch):
    """The manifest has to say develop@<sha>: version.py on develop still
    reads as the last release, so without it /api/version would describe
    a build nobody can reproduce."""
    server, client = _client(store, monkeypatch)
    monkeypatch.setattr(server.updater, "check_branch", lambda b: {**_TIP, "branch": b})
    monkeypatch.setattr(server.updater, "check_for_update", _no_release_lookup)
    fetched = []
    monkeypatch.setattr(server.updater, "download",
                        lambda url, dest: fetched.append((url, dest)) or Path(dest))
    applied = []
    monkeypatch.setattr(server.updater, "apply_update",
                        lambda archive, root, source="release": applied.append(source) or {
                            "version": "1.0.0", "previous_version": "1.0.0", "added": [],
                            "changed": [], "removed": [], "protected": [],
                            "first_update": False, "backup": "/tmp/b"})
    res = client.post("/api/updates/apply", json={"confirm": True, "dev": True}, headers=HEADERS)
    assert res.status_code == 200
    assert fetched[0][0] == _TIP["url"]
    assert Path(fetched[0][1]).name == "winnow-develop-abc1234.zip"
    assert applied == ["develop@abc1234def5678"]
    body = res.json()
    assert (body["branch"], body["short"], body["restart_required"]) == ("develop", "abc1234", True)


def test_apply_route_dev_surfaces_an_unreachable_github_as_a_400(store, monkeypatch):
    server, client = _client(store, monkeypatch)

    def boom(branch):
        raise UpdateError("Could not reach GitHub to read develop (no route to host).")

    monkeypatch.setattr(server.updater, "check_branch", boom)
    monkeypatch.setattr(server.updater, "apply_update",
                        lambda *a, **k: pytest.fail("nothing to install"))
    res = client.post("/api/updates/apply", json={"confirm": True, "dev": True}, headers=HEADERS)
    assert res.status_code == 400
    assert "develop" in res.json()["detail"]


def test_apply_route_reports_the_restart_requirement(store, monkeypatch):
    server, client = _client(store, monkeypatch)
    monkeypatch.setattr(server.updater, "check_for_update",
                        lambda **k: {"available": True, "latest": "9.9.9", "url": "u",
                                     "current": "1.0.0", "notes": "", "published_at": "",
                                     "html_url": ""})
    monkeypatch.setattr(server.updater, "download", lambda *a, **k: Path("/tmp/x.zip"))
    monkeypatch.setattr(server.updater, "apply_update",
                        lambda *a, **k: {"version": "9.9.9", "previous_version": "1.0.0",
                                         "added": [], "changed": [], "removed": [],
                                         "protected": [], "first_update": False,
                                         "backup": "/tmp/b"})
    res = client.post("/api/updates/apply", json={"confirm": True}, headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    # The server is still running the OLD code — saying otherwise would be a lie.
    assert body["restart_required"] is True
    assert body["version"] == "9.9.9"

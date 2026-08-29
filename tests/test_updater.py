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
    assert not updater.is_newer("1.2.0", "1.2.0-rc1")


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


def test_check_main_reports_the_tip_without_a_version_comparison(install):
    """main isn't a version, so there is no available/up-to-date answer to
    give — just what the tip currently is."""
    commit = {"sha": "abc1234def5678", "commit": {
        "message": "Resizable sidebar\n\nlonger body", "committer": {"date": "2026-08-29T10:00:00Z"}}}
    tip = updater.check_main(_fetch=lambda u, t: commit)
    assert tip["short"] == "abc1234"
    assert tip["message"] == "Resizable sidebar"   # subject only
    assert tip["url"].endswith("refs/heads/main.zip")
    assert "available" not in tip


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


def test_rollback_without_a_backup_is_a_clear_message(install):
    with pytest.raises(UpdateError, match="No backup"):
        updater.rollback(install)


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
    assert res.json() == {"version": version_module.VERSION}


def test_check_route_surfaces_an_offline_box_as_a_400(store, monkeypatch):
    server, client = _client(store, monkeypatch)

    def boom(**kwargs):
        raise UpdateError("Could not reach GitHub ... python update.py --from <file>.zip")

    monkeypatch.setattr(server.updater, "check_for_update", boom)
    res = client.post("/api/updates/check", json={}, headers=HEADERS)
    assert res.status_code == 400
    assert "--from" in res.json()["detail"]


def test_apply_route_refuses_without_confirmation(store, monkeypatch):
    server, client = _client(store, monkeypatch)
    called = []
    monkeypatch.setattr(server.updater, "apply_update",
                        lambda *a, **k: called.append(a) or {})
    res = client.post("/api/updates/apply", json={"confirm": False}, headers=HEADERS)
    assert res.status_code == 400
    assert not called, "a POST without confirm must not install anything"


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

"""/api/browse_dir in files mode reports each file's mtime, so the picker
can sort by it."""

from __future__ import annotations

import os


def test_file_entries_carry_an_integer_mtime(client, tmp_path):
    folder = tmp_path / "files"          # not tmp_path itself: the case file lives there
    folder.mkdir()
    (folder / "old.csv").write_text("a\n")
    (folder / "new.csv").write_text("b\n")
    os.utime(folder / "old.csv", (1_600_000_000, 1_600_000_000))
    os.utime(folder / "new.csv", (1_700_000_000, 1_700_000_000))
    res = client.get(f"/api/browse_dir?path={folder}&files=true")
    assert res.status_code == 200, res.text
    files = {f["name"]: f for f in res.json()["files"]}
    assert files["old.csv"]["mtime"] == 1_600_000_000
    assert files["new.csv"]["mtime"] == 1_700_000_000
    assert isinstance(files["old.csv"]["size"], int)
    # Still name-sorted on the wire; the picker does its own ordering.
    assert [f["name"] for f in res.json()["files"]] == ["new.csv", "old.csv"]


def test_folder_mode_is_unchanged(client, tmp_path):
    folder = tmp_path / "files"
    (folder / "sub").mkdir(parents=True)
    res = client.get(f"/api/browse_dir?path={folder}")
    assert res.status_code == 200
    assert "files" not in res.json() and res.json()["dirs"] == ["sub"]

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
    # Name-sorted by default; the picker's sort is taken server-side so
    # the 2000-entry cap is cut in the order being shown.
    assert [f["name"] for f in res.json()["files"]] == ["new.csv", "old.csv"]
    res = client.get(f"/api/browse_dir?path={folder}&files=true&sort=mtime&dir=desc")
    assert [f["name"] for f in res.json()["files"]] == ["new.csv", "old.csv"]
    res = client.get(f"/api/browse_dir?path={folder}&files=true&sort=mtime&dir=asc")
    assert [f["name"] for f in res.json()["files"]] == ["old.csv", "new.csv"]
    (folder / "big.csv").write_text("x" * 500)
    res = client.get(f"/api/browse_dir?path={folder}&files=true&sort=size&dir=desc")
    assert [f["name"] for f in res.json()["files"]][0] == "big.csv"


def test_folder_mode_is_unchanged(client, tmp_path):
    folder = tmp_path / "files"
    (folder / "sub").mkdir(parents=True)
    res = client.get(f"/api/browse_dir?path={folder}")
    assert res.status_code == 200
    assert "files" not in res.json() and res.json()["dirs"] == ["sub"]


def test_the_cap_is_cut_in_the_requested_order(client, tmp_path, monkeypatch):
    """With a cap of 2 and three files, Modified-descending must return the
    two NEWEST, not the newest two of the alphabetically-first two."""
    import server as srv
    monkeypatch.setattr(srv, "BROWSE_LIST_CAP", 2)
    folder = tmp_path / "files"
    folder.mkdir()
    for name, mt in [("a_old.csv", 1_600_000_000), ("b_mid.csv", 1_650_000_000), ("z_new.csv", 1_700_000_000)]:
        (folder / name).write_text("x")
        os.utime(folder / name, (mt, mt))
    res = client.get(f"/api/browse_dir?path={folder}&files=true&sort=mtime&dir=desc").json()
    assert res["truncated"] is True
    assert [f["name"] for f in res["files"]] == ["z_new.csv", "b_mid.csv"]

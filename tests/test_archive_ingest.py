"""Archive expansion for evidence bundles: recursive (archives inside
archives, gzipped rotated logs), safe against zip-slip / non-regular tar
members / bombs, and wired through the expand route."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import zipfile

import pytest

from winnow import archive


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _write_tgz(path, entries):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            b = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(b)
            tf.addfile(info, io.BytesIO(b))


def _tree(root):
    out = []
    for dirpath, _d, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(out)


def test_nested_bundle_expands_to_a_flat_tree(tmp_path):
    """A support-bundle shape: a zip holding a directory tree, a nested
    tgz, and a gzipped rotated log — one call yields only real files."""
    inner_tgz = tmp_path / "esx-inner.tgz"
    _write_tgz(inner_tgz, {"var/log/hostd.log": "2024-01-05T13:22:01Z Hi\n"})
    rotated = gzip.compress(b"old auth line\n")
    bundle = tmp_path / "esx-support.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("var/log/auth.log", "2024-01-05T13:22:01Z sshd[1]: ok\n")
        zf.writestr("var/log/auth.log.1.gz", rotated)
        zf.write(inner_tgz, "commands/esx-inner.tgz")

    rep = archive.expand_archive(str(bundle))
    assert rep["truncated"] is False
    assert rep["archives"] == 3           # zip + tgz + gz
    assert _tree(rep["root"]) == [
        "commands/esx-inner/var/log/hostd.log",
        "var/log/auth.log",
        "var/log/auth.log.1",
    ]
    assert open(os.path.join(rep["root"], "var/log/auth.log.1")).read() == "old auth line\n"
    # inner archives were removed; the ORIGINAL archive was not
    assert bundle.exists()
    # a second expansion never collides with the first
    rep2 = archive.expand_archive(str(bundle))
    assert rep2["root"] != rep["root"]


def test_zip_slip_is_skipped_and_absolute_paths_are_defanged(tmp_path):
    z = tmp_path / "evil.zip"
    _write_zip(z, {"../escape.txt": "nope", "/abs.txt": "was absolute", "ok.txt": "fine"})
    rep = archive.expand_archive(str(z))
    # ../ escapes are refused outright; an absolute member extracts with
    # the leading slash stripped (what tar/unzip do — evidence preserved,
    # filesystem untouched).
    assert _tree(rep["root"]) == ["abs.txt", "ok.txt"]
    reasons = " ".join(sk["reason"] for sk in rep["skipped"])
    assert "escapes" in reasons
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not os.path.exists("/abs.txt")


def test_tar_symlinks_are_skipped(tmp_path):
    t = tmp_path / "links.tar"
    with tarfile.open(t, "w") as tf:
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
        data = b"real\n"
        info = tarfile.TarInfo("real.log")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    rep = archive.expand_archive(str(t))
    assert _tree(rep["root"]) == ["real.log"]
    assert any("not a regular file" in sk["reason"] for sk in rep["skipped"])


def test_byte_budget_stops_a_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1024)
    z = tmp_path / "bomb.zip"
    _write_zip(z, {"a.bin": b"\x00" * 4096})
    rep = archive.expand_archive(str(z))
    assert rep["truncated"] is True
    assert rep["files"] == 0


def test_depth_cap_marks_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_DEPTH", 1)
    innermost = tmp_path / "in.zip"
    _write_zip(innermost, {"leaf.txt": "x"})
    mid = tmp_path / "mid.zip"
    with zipfile.ZipFile(mid, "w") as zf:
        zf.write(innermost, "in.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(mid, "mid.zip")
    rep = archive.expand_archive(str(outer))
    assert rep["truncated"] is True


def test_not_an_archive_is_a_clean_error(tmp_path):
    f = tmp_path / "plain.zip"
    f.write_text("just text")
    with pytest.raises(archive.ArchiveError, match="not a zip"):
        archive.expand_archive(str(f))


def test_expand_route(client, tmp_path):
    z = tmp_path / "logs.zip"
    _write_zip(z, {"var/log/vobd.log": "line\n"})
    r = client.post("/api/ingest/archive/expand", json={"path": str(z)})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["files"] == 1 and os.path.isdir(rep["root"])
    r = client.post("/api/ingest/archive/expand", json={"path": str(tmp_path / "missing.zip")})
    assert r.status_code == 400


def test_upload_route_extracts_beside_the_case(client, store, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("log/shell.log", "2024-01-05T13:22:01Z shell[1]: [root]: ls\n")
    r = client.post("/api/ingest/archive/upload",
                    files={"file": ("uac-host.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert os.path.dirname(rep["root"]) == os.path.dirname(os.path.abspath(store.path))
    assert os.path.basename(rep["root"]).startswith("uac-host-extracted")
    assert _tree(rep["root"]) == ["log/shell.log"]

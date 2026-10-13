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


# ------------------------------------------- damaged archives, name clashes
#
# Every one of these used to escape expand_archive as a raw EOFError /
# zlib.error / NotImplementedError — a 500 on the route — after part of
# the tree was already on disk, with the members after the bad one never
# extracted. And a name two members shared was silently overwritten.


def _deflate64_zip(path):
    """A zip whose middle member claims compression method 9 (Deflate64),
    which Windows Explorer writes for large members and zipfile cannot
    read. Built stored, then relabelled in both headers."""
    import struct
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("C/small.csv", "a,b\n1,2\n")
        zf.writestr("C/big.csv", "x" * 1000)
        zf.writestr("C/after.csv", "c,d\n3,4\n")
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        offset = zf.infolist()[1].header_offset
    struct.pack_into("<H", raw, offset + 8, 9)
    cd, n = raw.find(b"PK\x01\x02"), 0
    while cd != -1:
        if n == 1:
            struct.pack_into("<H", raw, cd + 10, 9)
        n += 1
        cd = raw.find(b"PK\x01\x02", cd + 4)
    path.write_bytes(bytes(raw))


def test_an_unsupported_zip_member_is_skipped_and_the_rest_extract(tmp_path):
    z = tmp_path / "kape_output.zip"
    _deflate64_zip(z)
    rep = archive.expand_archive(str(z))
    assert _tree(rep["root"]) == ["C/after.csv", "C/small.csv"]
    assert [sk["path"] for sk in rep["skipped"]] == ["C/big.csv"]
    assert "Deflate64" in rep["skipped"][0]["reason"]


def test_a_corrupt_inner_gz_is_skipped_and_the_rest_extract(tmp_path):
    bad = bytearray(gzip.compress(b"line\n" * 10000))
    bad[30:60] = b"\xff" * 30
    z = tmp_path / "b.zip"
    _write_zip(z, {"a.log": "keep me\n", "rot/hostd.0.gz": bytes(bad), "z.log": "keep me too\n"})
    rep = archive.expand_archive(str(z))
    assert _tree(rep["root"]) == ["a.log", "z.log"]     # no half-written hostd.0
    assert any(sk["path"] == "rot/hostd.0.gz" and "gzip" in sk["reason"] for sk in rep["skipped"])


def test_a_truncated_tgz_keeps_what_it_held(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(50):
            data = os.urandom(20000)
            info = tarfile.TarInfo(f"logs/f{i:02}.log")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    t = tmp_path / "bundle.tgz"
    t.write_bytes(raw[: len(raw) // 2])
    rep = archive.expand_archive(str(t))
    files = _tree(rep["root"])
    assert 0 < len(files) < 50
    # Only whole members are in the tree: the one the cut fell in is gone.
    assert all(os.path.getsize(os.path.join(rep["root"], f)) == 20000 for f in files)
    assert any("ends early" in sk["reason"] for sk in rep["skipped"])


def test_a_damaged_archive_that_yields_nothing_is_a_clean_error(tmp_path):
    t = tmp_path / "cut.gz"
    t.write_bytes(gzip.compress(os.urandom(200_000))[:5000])
    with pytest.raises(archive.ArchiveError, match="cut.gz"):
        archive.expand_archive(str(t))
    assert not [p for p in os.listdir(tmp_path) if "extracted" in p]


def test_a_later_inner_archive_still_expands_after_a_broken_one(tmp_path):
    """An inner zip whose central directory is damaged (its end record
    intact, so it still reads as a zip) fails to open; that used to end
    the whole nested pass, so every inner archive sorted after it stayed
    packed. It is skipped, kept in the tree as it was, and the rest expand."""
    broken = tmp_path / "broken.zip"
    _write_zip(broken, {"x.log": "x\n"})
    raw = broken.read_bytes()
    broken.write_bytes(raw.replace(b"PK\x01\x02", b"XX\x01\x02"))
    good = tmp_path / "good.zip"
    _write_zip(good, {"inner.log": "fine\n"})
    z = tmp_path / "outer.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(broken, "a/broken.zip")
        zf.write(good, "b/good.zip")
    rep = archive.expand_archive(str(z))
    assert _tree(rep["root"]) == ["a/broken.zip", "b/good/inner.log"]
    assert [sk["path"] for sk in rep["skipped"]] == ["a/broken.zip"]


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_member_names_both_survive(tmp_path):
    z = tmp_path / "dup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("auth.log", "FIRST copy - attacker activity\n")
        zf.writestr("auth.log", "second copy\n")
    rep = archive.expand_archive(str(z))
    assert _tree(rep["root"]) == ["auth (2).log", "auth.log"]
    assert open(os.path.join(rep["root"], "auth.log")).read() == "FIRST copy - attacker activity\n"
    assert rep["renamed"] == [{"path": "auth.log", "as": "auth (2).log"}]


def test_a_gz_never_overwrites_the_live_log_beside_it(tmp_path):
    z = tmp_path / "s.zip"
    _write_zip(z, {"var/log/messages": "CURRENT messages\n",
                   "var/log/messages.gz": gzip.compress(b"OLD rotated messages\n")})
    rep = archive.expand_archive(str(z))
    root = rep["root"]
    assert open(os.path.join(root, "var/log/messages")).read() == "CURRENT messages\n"
    assert open(os.path.join(root, "var/log/messages (2)")).read() == "OLD rotated messages\n"
    assert rep["renamed"] == [{"path": "var/log/messages.gz", "as": "var/log/messages (2)"}]


def test_one_unwritable_member_does_not_stop_the_rest(tmp_path):
    """A member named past the filesystem's limit, or one that needs a
    directory where a file already stands, is one member — not the end of
    the expansion."""
    z = tmp_path / "f.zip"
    _write_zip(z, {"first.log": "1\n", "x": "x is a file\n", "x/y.log": "under x\n",
                   "n" * 300 + ".log": "long\n", "later.log": "kept\n"})
    rep = archive.expand_archive(str(z))
    assert _tree(rep["root"]) == ["first.log", "later.log", "x"]
    assert {sk["path"] for sk in rep["skipped"]} == {"x/y.log", "n" * 300 + ".log"}
    assert rep["truncated"] is False


def test_expand_route_answers_400_not_500_for_a_damaged_archive(client, tmp_path):
    t = tmp_path / "cut.tgz"
    t.write_bytes(b"\x1f\x8b\x08\x00" + os.urandom(64))
    r = client.post("/api/ingest/archive/expand", json={"path": str(t)})
    assert r.status_code == 400, r.text

"""The plugin system end to end: the loader (plugin_api.py), the generic
Store.ingest_rows path plugins feed, the HTTP routes, directory-scan
integration, and the example mft_usn plugin parsed against synthetic NTFS
fixtures built byte-by-byte here (no real evidence files in the repo)."""

from __future__ import annotations

import json
import struct
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

import plugin_api
from plugin_api import PluginRegistry

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "plugins"


# ------------------------------------------------------------------ loader

def _write_plugin(dirpath: Path, name: str, body: str) -> Path:
    p = dirpath / f"{name}.py"
    p.write_text(textwrap.dedent(body))
    return p


GOOD_PLUGIN = """
    PLUGIN = {"name": "demo", "version": "0.1", "description": "demo plugin"}

    def _parse(path, options):
        prefix = "!" if options.get("shout") else ""
        rows = ([prefix + line.rstrip("\\n"), len(line.rstrip("\\n"))]
                for line in open(path, encoding="utf-8"))
        return {"columns": ["Line", "Length"], "rows": rows,
                "column_types": ["text", "number"]}

    def _boom(path, options):
        raise ValueError("boom: unparseable")

    def register(api):
        api.register_ingest_format(
            id="lines", label="Line file", extensions=[".lines"],
            filename_patterns=["$LINES"],
            options=[{"name": "shout", "label": "Shout", "type": "bool", "default": False},
                     {"name": "mode", "label": "Mode", "type": "choice",
                      "choices": ["a", "b"], "default": "a"}],
            parse=_parse,
        )
        api.register_ingest_format(id="boom", label="Always fails", extensions=[".boom"], parse=_boom)
"""


@pytest.fixture
def plug_dir(tmp_path) -> Path:
    d = tmp_path / "plugs"
    d.mkdir()
    return d


@pytest.fixture
def registry(plug_dir) -> PluginRegistry:
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    reg.load([plug_dir])
    return reg


def test_loads_single_file_plugin(registry):
    (rec,) = registry.describe()
    assert rec["name"] == "demo" and rec["version"] == "0.1" and rec["error"] is None
    assert rec["formats"] == ["demo.boom", "demo.lines"]
    fmt = registry.get_format("demo.lines")
    assert fmt.label == "Line file" and fmt.extensions == [".lines"]


def test_package_plugin_with_relative_import(plug_dir):
    pkg = plug_dir / "pkg_plug"
    pkg.mkdir()
    (pkg / "helper.py").write_text("COLS = ['A']\n")
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from . import helper

        def register(api):
            api.register_ingest_format(
                id="pkg", label="Pkg", extensions=[".pkg"],
                parse=lambda path, options: {"columns": helper.COLS, "rows": [["x"]]},
            )
    """))
    reg = PluginRegistry()
    reg.load([plug_dir])
    rec = next(p for p in reg.describe() if p["name"] == "pkg_plug")
    assert rec["error"] is None and rec["formats"] == ["pkg_plug.pkg"]


def test_broken_plugin_never_takes_down_the_rest(plug_dir):
    _write_plugin(plug_dir, "aaa_broken", "def register(api:\n")  # syntax error
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    _write_plugin(plug_dir, "noreg", "X = 1\n")  # imports fine, no register()
    reg = PluginRegistry()
    reg.load([plug_dir])
    by_name = {p["name"]: p for p in reg.describe()}
    assert "SyntaxError" in by_name["aaa_broken"]["error"]
    assert "register" in by_name["noreg"]["error"]
    assert by_name["demo"]["error"] is None
    assert "demo.lines" in {f["id"] for f in reg.list_formats()}


def test_api_version_gate(plug_dir):
    _write_plugin(plug_dir, "future", """
        WINNOW_API_VERSION = 999

        def register(api):
            pass
    """)
    reg = PluginRegistry()
    reg.load([plug_dir])
    (rec,) = reg.describe()
    assert "update Winnow" in rec["error"]


def test_duplicate_format_id_is_a_plugin_error(plug_dir):
    _write_plugin(plug_dir, "dupe", """
        def register(api):
            p = lambda path, options: {"columns": ["A"], "rows": []}
            api.register_ingest_format(id="x", label="X", extensions=[".x"], parse=p)
            api.register_ingest_format(id="x", label="X again", extensions=[".y"], parse=p)
    """)
    reg = PluginRegistry()
    reg.load([plug_dir])
    (rec,) = reg.describe()
    assert "Duplicate" in rec["error"]
    # The first registration survives; the plugin just can't add more.
    assert reg.get_format("dupe.x").label == "X"


def test_underscore_and_hidden_entries_are_skipped(plug_dir):
    _write_plugin(plug_dir, "_disabled", "raise RuntimeError('should never import')")
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    reg.load([plug_dir])
    assert [p["name"] for p in reg.describe()] == ["demo"]


def test_format_matching(registry):
    fmt = registry.get_format("demo.lines")
    assert fmt.matches("notes.lines")
    assert fmt.matches("NOTES.LINES")          # case-insensitive extension
    assert fmt.matches(r"C:\evidence\$LINES")  # bare-name pattern, extension-less
    assert fmt.matches("$lines")
    assert not fmt.matches("notes.csv")
    assert registry.format_for_filename("a.lines").id == "demo.lines"
    assert registry.format_for_filename("a.nope") is None


def test_resolve_options(registry):
    fmt = registry.get_format("demo.lines")
    assert fmt.resolve_options(None) == {"shout": False, "mode": "a"}
    assert fmt.resolve_options({"shout": 1, "mode": "b", "sneaky": "x"}) == {"shout": True, "mode": "b"}
    with pytest.raises(ValueError):
        fmt.resolve_options({"mode": "nope"})


def test_disabled_plugin_is_discovered_but_never_imported(plug_dir):
    # A disabled plugin whose module body would blow up on import proves
    # the point: discovery must not execute its code.
    _write_plugin(plug_dir, "landmine", "raise RuntimeError('imported a disabled plugin')")
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    reg.load([plug_dir], disabled={"landmine"})
    by_fs = {p["fs_name"]: p for p in reg.describe()}
    mine = by_fs["landmine"]
    assert mine["enabled"] is False and mine["error"] is None and mine["formats"] == []
    assert by_fs["demo"]["enabled"] is True
    # Re-enabling is just another load without the name in the set.
    reg.load([plug_dir], disabled=set())
    mine = next(p for p in reg.describe() if p["fs_name"] == "landmine")
    assert "imported a disabled plugin" in mine["error"]  # now it ran, and failed loudly


# ------------------------------------------------------------- ingest_rows

def test_ingest_rows_basic(store):
    rows = ([f"row{i}", i] for i in range(10))
    rec = store.ingest_rows(["Name", "N"], rows, name="gen source")
    assert rec["row_count"] == 10 and rec["ragged_rows"] == 0
    assert [c["name"] for c in rec["columns"]] == ["Name", "N"]
    got = store.db.execute(f"SELECT rid, Name, N FROM {rec['table_name']} ORDER BY rid").fetchall()
    assert [r[0] for r in got] == list(range(1, 11))  # contiguous rid from 1
    assert got[3][1] == "row3" and got[3][2] == "3"   # ints stringified: TEXT columns


def test_ingest_rows_ragged_and_none(store):
    rows = [["a", "b"], ["only-one"], ["x", None, "extra"]]
    rec = store.ingest_rows(["C1", "C2"], rows, name="ragged")
    assert rec["row_count"] == 3 and rec["ragged_rows"] == 2
    got = store.db.execute(f"SELECT C1, C2 FROM {rec['table_name']} ORDER BY rid").fetchall()
    assert [tuple(r) for r in got] == [("a", "b"), ("only-one", ""), ("x", "")]


def test_ingest_rows_column_types_override(store):
    rec = store.ingest_rows(
        ["When", "What"], [["2024-01-01 10:00:00", "x"]], name="typed",
        column_types=["datetime", "bogus-type"],
    )
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["When"] == "datetime"
    assert types["What"] == "text"  # unknown declared type falls back to inference


def test_ingest_rows_requires_columns(store):
    with pytest.raises(ValueError):
        store.ingest_rows([], [], name="empty")


def test_ingest_rows_error_mid_iteration_keeps_committed_work(store):
    def rows():
        yield ["ok"]
        raise RuntimeError("parser died")

    with pytest.raises(RuntimeError, match="parser died"):
        store.ingest_rows(["A"], rows(), name="partial")
    # Same convention as a malformed line late in a CSV: the source record
    # survives with an accurate count and typed columns.
    src = store.list_sources()[-1]
    assert src["name"] == "partial"
    assert [c["name"] for c in src["columns"]] == ["A"]


# --------------------------------------------------- directory-scan routing

def test_scan_filename_patterns_pass_the_extension_gate(store, tmp_path):
    root = tmp_path / "triage"
    root.mkdir()
    (root / "$MFT").write_bytes(b"\x00" * 16)
    (root / "events.csv").write_text("a,b\n1,2\n")
    (root / "raw.bin").write_bytes(b"\x00")

    out = store.scan_import_directory(str(root), filename_patterns=["$MFT"])
    kinds = {m["rel_path"]: m["kind"] for m in out["matched"]}
    assert kinds == {"$MFT": "plugin", "events.csv": "csv"}
    assert [e["rel_path"] for e in out["excluded"]] == ["raw.bin"]

    # A plugin extension handed in via `extensions` also routes as plugin.
    out = store.scan_import_directory(str(root), extensions=[".csv", ".bin"])
    kinds = {m["rel_path"]: m["kind"] for m in out["matched"]}
    assert kinds == {"events.csv": "csv", "raw.bin": "plugin"}


# ------------------------------------------------------------- HTTP routes

@pytest.fixture
def plugin_client(client, registry, plug_dir, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", registry)
    # Toggle reloads from, and install writes into, PLUGIN_DIRS — pointing
    # it at the test's tmp dir keeps the repo's real plugins/ untouched.
    monkeypatch.setattr(server, "PLUGIN_DIRS", [plug_dir])
    return client


def test_api_plugins_lists_everything(plugin_client):
    r = plugin_client.get("/api/plugins").json()
    assert r["api_version"] == plugin_api.PLUGIN_API_VERSION
    assert r["plugins"][0]["name"] == "demo"
    fmt = next(f for f in r["formats"] if f["id"] == "demo.lines")
    assert fmt["filename_patterns"] == ["$LINES"]
    assert fmt["options"][0]["name"] == "shout"


def test_ingest_plugin_path(plugin_client, store, tmp_path):
    f = tmp_path / "notes.lines"
    f.write_text("alpha\nbeta\n")
    r = plugin_client.post("/api/ingest/plugin/path", json={
        "path": str(f), "format_id": "demo.lines", "options": {"shout": True},
    })
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["row_count"] == 2 and rec["name"] == "notes.lines"
    got = store.db.execute(f"SELECT Line, Length FROM {rec['table_name']} ORDER BY rid").fetchall()
    assert [tuple(r) for r in got] == [("!alpha", "5"), ("!beta", "4")]
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types == {"Line": "text", "Length": "number"}


def test_ingest_plugin_upload(plugin_client, store):
    r = plugin_client.post(
        "/api/ingest/plugin/upload",
        files={"file": ("dropped.lines", b"one\ntwo\nthree\n")},
        data={"format_id": "demo.lines", "options": json.dumps({"shout": False})},
    )
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["row_count"] == 3 and rec["name"] == "dropped.lines"


def test_ingest_plugin_errors_are_400s(plugin_client, tmp_path):
    f = tmp_path / "x.boom"
    f.write_text("data")
    r = plugin_client.post("/api/ingest/plugin/path", json={"path": str(f), "format_id": "demo.boom"})
    assert r.status_code == 400 and "boom: unparseable" in r.json()["detail"]
    r = plugin_client.post("/api/ingest/plugin/path", json={"path": str(f), "format_id": "no.such"})
    assert r.status_code == 400
    r = plugin_client.post("/api/ingest/plugin/path", json={"path": str(tmp_path / "missing"), "format_id": "demo.lines"})
    assert r.status_code == 400


def test_workspace_plugin_prefs_roundtrip():
    import workspace as WS

    assert WS.plugin_prefs.disabled() == set()
    WS.plugin_prefs.set_enabled("x", False)
    WS.plugin_prefs.set_enabled("y", False)
    assert WS.plugin_prefs.disabled() == {"x", "y"}
    WS.plugin_prefs.set_enabled("x", True)
    assert WS.plugin_prefs.disabled() == {"y"}


def test_toggle_route(plugin_client, tmp_path):
    f = tmp_path / "notes.lines"
    f.write_text("alpha\n")

    r = plugin_client.post("/api/plugins/toggle", json={"fs_name": "demo", "enabled": False})
    assert r.status_code == 200
    out = r.json()
    rec = next(p for p in out["plugins"] if p["fs_name"] == "demo")
    assert rec["enabled"] is False and rec["formats"] == []
    assert out["formats"] == []  # nothing registered => nothing routes

    # A disabled plugin's formats no longer ingest — the format id is gone.
    r = plugin_client.post("/api/ingest/plugin/path", json={"path": str(f), "format_id": "demo.lines"})
    assert r.status_code == 400 and "demo.lines" in r.json()["detail"]

    r = plugin_client.post("/api/plugins/toggle", json={"fs_name": "demo", "enabled": True})
    out = r.json()
    assert next(p for p in out["plugins"] if p["fs_name"] == "demo")["enabled"] is True
    assert {fm["id"] for fm in out["formats"]} == {"demo.boom", "demo.lines"}
    r = plugin_client.post("/api/ingest/plugin/path", json={"path": str(f), "format_id": "demo.lines"})
    assert r.status_code == 200

    assert plugin_client.post("/api/plugins/toggle", json={"fs_name": "nope", "enabled": False}).status_code == 404


INSTALLABLE = textwrap.dedent("""
    def register(api):
        api.register_ingest_format(
            id="k", label="K", extensions=[".k"],
            parse=lambda path, options: {"columns": ["A"], "rows": [["1"]]},
        )
""")


def test_install_single_py(plugin_client, plug_dir):
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("kplug.py", INSTALLABLE.encode()))])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["installed"] == "kplug" and out["error"] is None
    assert (plug_dir / "kplug.py").is_file()
    assert "kplug.k" in {fm["id"] for fm in out["formats"]}


def test_install_folder_with_junk_filtered(plugin_client, plug_dir):
    init = ('from . import helper\n\n'
            'def register(api):\n'
            '    api.register_ingest_format(id="p", label="P", extensions=[".p"], parse=helper.parse)\n')
    helper = 'def parse(path, options):\n    return {"columns": ["A"], "rows": [["x"]]}\n'
    r = plugin_client.post(
        "/api/plugins/install",
        files=[("files", ("__init__.py", init.encode())),
               ("files", ("helper.py", helper.encode())),
               ("files", ("junk.pyc", b"\x00"))],
        data={"paths": json.dumps([
            "folderplug/__init__.py", "folderplug/helper.py", "folderplug/__pycache__/junk.pyc",
        ])},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["installed"] == "folderplug" and out["error"] is None
    assert (plug_dir / "folderplug" / "helper.py").is_file()
    # The uploaded junk wasn't copied. (A __pycache__ dir may exist anyway —
    # importing the installed package just created a fresh one.)
    assert not (plug_dir / "folderplug" / "__pycache__" / "junk.pyc").exists()
    assert "folderplug.p" in {fm["id"] for fm in out["formats"]}


def test_install_rejects_traversal_and_absolute_paths(plugin_client, plug_dir):
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("evil.py", b"x = 1"))],
                           data={"paths": json.dumps(["../evil.py"])})
    assert r.status_code == 400
    assert not (plug_dir.parent / "evil.py").exists()
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("evil.py", b"x = 1"))],
                           data={"paths": json.dumps(["/tmp/evil.py"])})
    assert r.status_code == 400


def test_install_folder_requires_init(plugin_client):
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("helper.py", b"x = 1"))],
                           data={"paths": json.dumps(["someplug/helper.py"])})
    assert r.status_code == 400 and "__init__.py" in r.json()["detail"]


def test_install_overwrite_flow(plugin_client, plug_dir):
    first = plugin_client.post("/api/plugins/install",
                               files=[("files", ("kplug.py", INSTALLABLE.encode()))])
    assert first.status_code == 200
    again = plugin_client.post("/api/plugins/install",
                               files=[("files", ("kplug.py", b"def register(api):\n    pass\n"))])
    assert again.status_code == 409  # taken — needs explicit consent to replace
    forced = plugin_client.post("/api/plugins/install",
                                files=[("files", ("kplug.py", b"def register(api):\n    pass\n"))],
                                data={"overwrite": "true"})
    assert forced.status_code == 200
    out = forced.json()
    # The replacement registers nothing, so the format from the first
    # install must be gone after the reload.
    assert "kplug.k" not in {fm["id"] for fm in out["formats"]}


def test_install_broken_plugin_reports_load_error(plugin_client, plug_dir):
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("busted.py", b"def register(api:\n"))])
    assert r.status_code == 200  # the *install* succeeded; the load didn't
    out = r.json()
    assert out["installed"] == "busted" and "SyntaxError" in out["error"]
    assert (plug_dir / "busted.py").is_file()  # kept for the analyst to fix or remove


# ============================================================ mft_usn plugin
#
# Synthetic fixtures built from the on-disk structures' documented layouts —
# the tests own every byte, so assertions are exact.

EPOCH = datetime(1601, 1, 1)


def ft(dt: datetime) -> int:
    """datetime -> FILETIME with integer math (float seconds lose the
    sub-second digits at 2024-scale magnitudes)."""
    d = dt - EPOCH
    return (d.days * 86400 + d.seconds) * 10_000_000 + d.microseconds * 10


@pytest.fixture(scope="module")
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    rec = next(p for p in reg.describe() if p["name"] == "mft-usn")
    assert rec["error"] is None, rec["error"]
    return reg


def test_example_plugin_loads_and_matches(example_registry):
    mft = example_registry.get_format("mft-usn.mft")
    usn = example_registry.get_format("mft-usn.usn")
    assert mft.matches("$MFT") and mft.matches("c.mft") and not mft.matches("x.csv")
    assert usn.matches("$J") and usn.matches("20240101_$UsnJrnl_$J.bin") and usn.matches("vol.usn")


# ----------------------------------------------------------- USN fixtures

def usn_v2(ts: int, name: str, *, entry=100, seq=1, parent=5, parent_seq=5,
           usn=4096, reason=0x100 | 0x80000000, fattr=0x20, source=0) -> bytes:
    nb = name.encode("utf-16-le")
    length = (60 + len(nb) + 7) & ~7
    buf = bytearray(length)
    struct.pack_into("<IHH", buf, 0, length, 2, 0)
    struct.pack_into("<QQQQIIIIHH", buf, 8,
                     entry | (seq << 48), parent | (parent_seq << 48),
                     usn, ts, reason, source, 0, fattr, len(nb), 60)
    buf[60:60 + len(nb)] = nb
    return bytes(buf)


def test_usn_parse(example_registry, tmp_path):
    t1 = ft(datetime(2024, 3, 1, 10, 30, 0, 123456))
    t2 = ft(datetime(2024, 3, 1, 10, 31, 5))
    blob = (
        b"\x00" * 65536                                   # sparse lead-in
        + usn_v2(t1, "evil.exe", entry=105, seq=2, usn=1000)
        + b"\xde\xad\xbe\xef\xde\xad\xbe\xef"             # 8 bytes of garbage to carve past
        + usn_v2(t2, "evil.exe", entry=105, seq=2, usn=1100,
                 reason=0x200 | 0x80000000)               # FILE_DELETE|CLOSE
    )
    j = tmp_path / "$J"
    j.write_bytes(blob)

    fmt = example_registry.get_format("mft-usn.usn")
    out = fmt.parse(str(j), {})
    rows = list(out["rows"])
    cols = out["columns"]
    assert len(rows) == 2
    r1 = dict(zip(cols, rows[0]))
    assert r1["Timestamp"] == "2024-03-01 10:30:00.123456"
    assert r1["FileName"] == "evil.exe" and r1["Extension"] == "exe"
    assert r1["Reason"] == "FILE_CREATE|CLOSE"
    assert (r1["EntryNumber"], r1["SequenceNumber"]) == (105, 2)
    assert (r1["ParentEntryNumber"], r1["ParentSequenceNumber"]) == (5, 5)
    assert r1["FileAttributes"] == "ARCHIVE"
    r2 = dict(zip(cols, rows[1]))
    assert r2["Reason"] == "FILE_DELETE|CLOSE" and r2["USN"] == 1100


def test_usn_record_straddling_chunk_boundary(example_registry, tmp_path, monkeypatch):
    """A record split across two read chunks must be carried, not lost or
    half-parsed. Shrinking CHUNK makes every record straddle."""
    import sys

    fmt = example_registry.get_format("mft-usn.usn")
    usn_mod = sys.modules[fmt.parse.__module__]
    monkeypatch.setattr(usn_mod, "CHUNK", 64)

    t = ft(datetime(2024, 5, 5, 5, 5, 5))
    blob = b"".join(usn_v2(t, f"file{i:02d}.txt", entry=200 + i, usn=i) for i in range(20))
    j = tmp_path / "straddle.usn"
    j.write_bytes(blob)
    rows = list(fmt.parse(str(j), {})["rows"])
    assert [r[1] for r in rows] == [f"file{i:02d}.txt" for i in range(20)]


# ----------------------------------------------------------- MFT fixtures

def _attr(atype: int, content: bytes) -> bytes:
    hdr = 24
    total = (hdr + len(content) + 7) & ~7
    buf = bytearray(total)
    struct.pack_into("<II", buf, 0, atype, total)
    buf[8] = 0   # resident
    buf[9] = 0   # no attribute name
    struct.pack_into("<IH", buf, 16, len(content), hdr)
    buf[hdr:hdr + len(content)] = content
    return bytes(buf)


def _si(times=(0, 0, 0, 0)) -> bytes:
    c = bytearray(48)
    struct.pack_into("<4Q", c, 0, *times)
    return _attr(0x10, bytes(c))


def _fn(parent: int, parent_seq: int, name: str, *, namespace=1, times=(0, 0, 0, 0), size=0) -> bytes:
    nb = name.encode("utf-16-le")
    c = bytearray(66 + len(nb))
    struct.pack_into("<Q", c, 0, parent | (parent_seq << 48))
    struct.pack_into("<4Q", c, 8, *times)
    struct.pack_into("<QQ", c, 40, size, size)
    c[64], c[65] = len(name), namespace
    c[66:] = nb
    return _attr(0x30, bytes(c))


def _data_nonres(size: int) -> bytes:
    """Non-resident unnamed $DATA — how a file of any real size is stored
    (a 1337-byte payload doesn't fit residently in a 1 KB record)."""
    buf = bytearray(72)
    struct.pack_into("<II", buf, 0, 0x80, 72)
    buf[8] = 1  # non-resident
    struct.pack_into("<H", buf, 32, 64)  # runlist offset (empty runlist)
    struct.pack_into("<QQQ", buf, 40, (size + 4095) & ~4095, size, size)  # allocated, real, initialized
    return bytes(buf)


def mft_record(entry: int, seq: int, attrs: list[bytes], *, in_use=True, is_dir=False,
               stamped=True) -> bytes:
    rec = bytearray(1024)
    rec[0:4] = b"FILE"
    struct.pack_into("<HH", rec, 4, 48, 3)  # usa_ofs=48 (NTFS 3.1 layout), usa_count=3
    struct.pack_into("<H", rec, 16, seq)
    struct.pack_into("<HH", rec, 20, 56, (1 if in_use else 0) | (2 if is_dir else 0))
    struct.pack_into("<I", rec, 28, 1024)   # allocated record size
    struct.pack_into("<I", rec, 44, entry)
    off = 56
    for a in attrs:
        rec[off:off + len(a)] = a
        off += len(a)
    struct.pack_into("<I", rec, off, 0xFFFFFFFF)
    # Multi-sector write protection, exactly as NTFS applies it: park each
    # sector's true last two bytes in the USA, stamp the shared USN over
    # them. The parser has to reverse this or those bytes read corrupted.
    # stamped=False models an extraction tool (ntfscat among them) that
    # already applied the fixups itself — the USA is populated but the
    # sector tails hold the true bytes.
    usn = b"\x99\x99"
    rec[48:50] = usn
    for i in (1, 2):
        end = i * 512
        rec[48 + 2 * i: 50 + 2 * i] = rec[end - 2:end]
        if stamped:
            rec[end - 2:end] = usn
    return bytes(rec)


@pytest.fixture
def mft_file(tmp_path) -> Path:
    si_old = ft(datetime(2020, 1, 1, 0, 0, 0))           # timestomped-looking $SI
    fn_real = ft(datetime(2024, 6, 1, 12, 0, 0, 500000))  # genuine $FN creation
    normal = ft(datetime(2024, 6, 2, 8, 30, 0))
    # A name long enough that its UTF-16 bytes cross the 510-byte sector
    # boundary — the assertion on it only passes if fixups were applied.
    long_name = "L" * 200 + ".dat"
    records = [
        mft_record(5, 5, [_si(), _fn(5, 5, ".", namespace=3)], is_dir=True),
        mft_record(40, 1, [_si((normal,) * 4), _fn(5, 5, "Users", namespace=3)], is_dir=True),
        mft_record(41, 1, [_si((normal,) * 4), _fn(40, 1, "bob", namespace=3)], is_dir=True),
        # Two $FILE_NAMEs: the DOS 8.3 one must lose to Win32.
        mft_record(42, 7, [
            _si((si_old, normal, normal, normal)),
            _fn(41, 1, "SECRET~1.TXT", namespace=2, times=(fn_real,) * 4),
            _fn(41, 1, "secret.txt", namespace=1, times=(fn_real,) * 4, size=1337),
            _data_nonres(1337),
        ]),
        # Deleted, and its parent entry 99 isn't in the file at all -> orphan.
        mft_record(43, 2, [_si((normal,) * 4), _fn(99, 1, "gone.exe", times=(normal,) * 4)], in_use=False),
        mft_record(44, 1, [_si((normal,) * 4), _fn(41, 1, long_name, times=(normal,) * 4)]),
        b"\x00" * 1024,  # an unused, zeroed slot — skipped, not a row
    ]
    p = tmp_path / "$MFT"
    p.write_bytes(b"".join(records))
    return p


def _mft_rows(example_registry, mft_file, options=None):
    fmt = example_registry.get_format("mft-usn.mft")
    out = fmt.parse(str(mft_file), fmt.resolve_options(options))
    return [dict(zip(out["columns"], r)) for r in out["rows"]]


def test_mft_parse_paths_names_and_times(example_registry, mft_file):
    rows = _mft_rows(example_registry, mft_file)
    by_entry = {r["EntryNumber"]: r for r in rows}
    assert set(by_entry) == {5, 40, 41, 42, 43, 44}

    secret = by_entry[42]
    assert secret["FileName"] == "secret.txt"          # Win32 beat the DOS 8.3 name
    assert secret["NameType"] == "Win32"
    assert secret["FullPath"] == ".\\Users\\bob\\secret.txt"
    assert secret["Extension"] == "txt"
    assert secret["FileSize"] == 1337
    assert secret["InUse"] == "true" and secret["IsDirectory"] == "false"
    assert secret["SequenceNumber"] == 7 and secret["ParentEntryNumber"] == 41
    assert secret["Created0x10"] == "2020-01-01 00:00:00.000000"
    assert secret["Created0x30"] == "2024-06-01 12:00:00.500000"
    assert secret["SI<FN Created"] == "Y"              # the timestomp tell

    assert by_entry[41]["FullPath"] == ".\\Users\\bob"
    assert by_entry[41]["IsDirectory"] == "true"
    assert by_entry[40]["SI<FN Created"] == ""


def test_mft_deleted_and_orphans(example_registry, mft_file):
    rows = _mft_rows(example_registry, mft_file)
    gone = next(r for r in rows if r["EntryNumber"] == 43)
    assert gone["InUse"] == "false"
    assert gone["FullPath"] == ".\\<orphan>\\gone.exe"  # parent entry missing — no invented location

    deleted_only = _mft_rows(example_registry, mft_file, {"records": "deleted"})
    assert [r["EntryNumber"] for r in deleted_only] == [43]
    in_use_only = _mft_rows(example_registry, mft_file, {"records": "in-use"})
    assert 43 not in [r["EntryNumber"] for r in in_use_only]


def test_mft_fixups_restore_sector_boundary_bytes(example_registry, mft_file):
    rows = _mft_rows(example_registry, mft_file)
    long_row = next(r for r in rows if r["EntryNumber"] == 44)
    # This name's UTF-16 bytes cross offset 510 of the record; it only
    # round-trips intact if the parser undid the fixup stamping.
    assert long_row["FileName"] == "L" * 200 + ".dat"
    assert long_row["FullPath"] == ".\\Users\\bob\\" + "L" * 200 + ".dat"


def test_mft_already_fixed_up_extraction(example_registry, tmp_path):
    """ntfscat-style extractions hand over records whose fixups are already
    un-applied (verified against a real mkntfs volume) — the parser must
    take those as-is, not reject every record as torn."""
    long_name = "M" * 200 + ".bin"
    normal = ft(datetime(2024, 7, 1))
    records = [
        mft_record(5, 5, [_si(), _fn(5, 5, ".", namespace=3)], is_dir=True, stamped=False),
        mft_record(50, 1, [_si((normal,) * 4), _fn(5, 5, long_name, times=(normal,) * 4)], stamped=False),
    ]
    p = tmp_path / "corrected.mft"
    p.write_bytes(b"".join(records))
    rows = _mft_rows(example_registry, p)
    by_entry = {r["EntryNumber"]: r for r in rows}
    assert by_entry[50]["FileName"] == long_name
    assert by_entry[50]["FullPath"] == ".\\" + long_name


def test_mft_end_to_end_through_the_api(client, store, example_registry, mft_file, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    r = client.post("/api/ingest/plugin/path", json={
        "path": str(mft_file), "format_id": "mft-usn.mft", "name": "$MFT",
    })
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["name"] == "$MFT" and rec["row_count"] == 6
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["Created0x10"] == "datetime"  # declared, not left to sampling
    assert types["EntryNumber"] == "number"
    got = store.db.execute(
        f"SELECT FullPath FROM {rec['table_name']} WHERE FileName='secret.txt'"
    ).fetchone()
    assert got[0] == ".\\Users\\bob\\secret.txt"

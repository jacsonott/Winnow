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


# ====================================================== tabs / assets / APIs

UI_PLUGIN_INIT = textwrap.dedent("""
    def _echo(req):
        return {"method": req.method, "route": req.route, "query": req.query,
                "body": req.body, "has_store": req.store is not None}

    def _boom(req):
        raise ValueError("kaboom: bad input")

    def register(api):
        api.register_tab(id="view", label="My View", entry="ui/tab.js", description="demo tab")
        api.register_api("echo", _echo, methods=["GET", "POST"])
        api.register_api("boom", _boom, methods=["GET"])
""")


@pytest.fixture
def ui_plug_dir(tmp_path) -> Path:
    d = tmp_path / "uiplugs"
    (d / "uiplug" / "ui").mkdir(parents=True)
    (d / "uiplug" / "__init__.py").write_text(UI_PLUGIN_INIT)
    (d / "uiplug" / "ui" / "tab.js").write_text("export default function mount(c, w) {}\n")
    (d / "secret.txt").write_text("outside the plugin folder")
    return d


@pytest.fixture
def ui_registry(ui_plug_dir) -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([ui_plug_dir])
    rec = reg.describe()[0]
    assert rec["error"] is None, rec["error"]
    return reg


@pytest.fixture
def ui_client(client, ui_registry, ui_plug_dir, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", ui_registry)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [ui_plug_dir])
    return client


def test_register_tab_and_api_are_listed(ui_registry):
    rec = ui_registry.describe()[0]
    assert rec["tabs"] == ["uiplug.view"]
    (tab,) = ui_registry.list_tabs()
    assert tab["label"] == "My View" and tab["entry"] == "ui/tab.js"
    assert tab["plugin_fs"] == "uiplug" and tab["gen"] > 0
    assert ui_registry.get_api("uiplug", "echo")["methods"] == {"GET", "POST"}
    assert ui_registry.get_api("uiplug", "nope") is None


def test_tab_gen_changes_on_reload(ui_plug_dir, ui_registry):
    """gen is the frontend's import() cache-buster — a reload must change it
    or a toggled plugin's updated JS would never be re-fetched."""
    (old,) = ui_registry.list_tabs()
    ui_registry.load([ui_plug_dir])
    (new,) = ui_registry.list_tabs()
    assert new["gen"] != old["gen"]


def test_register_tab_validation(plug_dir):
    _write_plugin(plug_dir, "badtab", """
        def register(api):
            api.register_tab(id="x", label="X", entry="ui/tab.js")
    """)  # single-file plugin — no folder to serve assets from
    pkg = plug_dir / "missing_entry"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        def register(api):
            api.register_tab(id="x", label="X", entry="does/not/exist.js")
    """))
    reg = PluginRegistry()
    reg.load([plug_dir])
    by_name = {p["fs_name"]: p for p in reg.describe()}
    assert "folder plugins" in by_name["badtab"]["error"]
    assert "inside the plugin folder" in by_name["missing_entry"]["error"]


def test_register_api_validation(plug_dir):
    _write_plugin(plug_dir, "badroute", """
        def register(api):
            api.register_api("Bad Route!", lambda req: {}, methods=["GET"])
    """)
    _write_plugin(plug_dir, "badmethod", """
        def register(api):
            api.register_api("ok", lambda req: {}, methods=["YEET"])
    """)
    _write_plugin(plug_dir, "duperoute", """
        def register(api):
            api.register_api("ok", lambda req: {})
            api.register_api("ok", lambda req: {})
    """)
    reg = PluginRegistry()
    reg.load([plug_dir])
    by_name = {p["fs_name"]: p for p in reg.describe()}
    assert "Route" in by_name["badroute"]["error"]
    assert "methods" in by_name["badmethod"]["error"]
    assert "Duplicate" in by_name["duperoute"]["error"]


def test_plugin_listing_includes_tabs(ui_client):
    r = ui_client.get("/api/plugins").json()
    assert [t["id"] for t in r["tabs"]] == ["uiplug.view"]


def test_plugin_assets_served_and_contained(ui_client, ui_plug_dir):
    r = ui_client.get("/plugin_assets/uiplug/ui/tab.js")
    assert r.status_code == 200 and "export default" in r.text
    # Traversal can't escape the plugin folder (encoded, so the client
    # doesn't normalize it away before it reaches the route).
    r = ui_client.get("/plugin_assets/uiplug/%2e%2e/secret.txt")
    assert r.status_code == 404
    r = ui_client.get("/plugin_assets/nope/ui/tab.js")
    assert r.status_code == 404


def test_plugin_api_dispatch(ui_client):
    r = ui_client.get("/api/plugin/uiplug/echo?x=1&y=z")
    assert r.status_code == 200
    out = r.json()
    assert out["method"] == "GET" and out["route"] == "echo"
    assert out["query"] == {"x": "1", "y": "z"} and out["body"] is None
    assert out["has_store"] is True  # the client fixture's open case

    r = ui_client.post("/api/plugin/uiplug/echo", json={"a": [1, 2]})
    assert r.json()["body"] == {"a": [1, 2]}

    assert ui_client.put("/api/plugin/uiplug/echo", json={}).status_code == 405
    assert ui_client.get("/api/plugin/uiplug/nope").status_code == 404
    assert ui_client.get("/api/plugin/ghost/echo").status_code == 404

    r = ui_client.get("/api/plugin/uiplug/boom")
    assert r.status_code == 400 and "kaboom: bad input" in r.json()["detail"]


def test_disabled_plugin_loses_assets_tabs_and_routes(ui_client):
    r = ui_client.post("/api/plugins/toggle", json={"fs_name": "uiplug", "enabled": False})
    assert r.status_code == 200 and r.json()["tabs"] == []
    assert ui_client.get("/plugin_assets/uiplug/ui/tab.js").status_code == 404
    assert ui_client.get("/api/plugin/uiplug/echo").status_code == 404


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


# ================================================== lateral_movement plugin

LOGON_ROWS = [
    ["SourceHost", "DestHost", "User"],
    ["WS1", "SRV1", "alice"],
    ["WS1", "SRV1", "bob"],
    ["WS2", "SRV1", "alice"],
    ["WS1", "WS1", "alice"],   # self-loop — filtered
    ["-", "SRV2", "carol"],    # EVTX "not present" spelling — filtered
]


@pytest.fixture
def lateral_client(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(LOGON_ROWS, "logons.csv"), name="logons")
    return client, rec["id"]


def test_lateral_movement_edges(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "source_id": source_id, "src_col": "SourceHost", "dst_col": "DestHost",
        "label_col": "User",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["truncated"] is False
    assert out["edges"] == [
        {"src": "WS1", "dst": "SRV1", "n": 2, "labels": 2},
        {"src": "WS2", "dst": "SRV1", "n": 1, "labels": 1},
    ]


def test_lateral_movement_validation(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "source_id": source_id, "src_col": "SourceHost", "dst_col": "Nope",
    })
    assert r.status_code == 400 and "Nope" in r.json()["detail"]
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "source_id": -1, "src_col": "a", "dst_col": "b",
    })
    assert r.status_code == 400 and "merge" in r.json()["detail"].lower()
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "source_id": source_id, "src_col": "SourceHost", "dst_col": "SourceHost",
    })
    assert r.status_code == 400


def test_lateral_movement_tab_asset(lateral_client):
    client, _ = lateral_client
    r = client.get("/plugin_assets/lateral_movement/ui/tab.js")
    assert r.status_code == 200 and "export default" in r.text


# ================================================== claude_assistant plugin
#
# The handler imports `anthropic` lazily, so the tests inject a fake module
# into sys.modules — no network, no real SDK needed. The fake records the
# exact request kwargs so the tests can assert on the API shape (model,
# fallbacks, cache breakpoint) rather than just the happy path.

import sys
import types


def _fake_anthropic(record: dict, msg) -> types.ModuleType:
    mod = types.ModuleType("anthropic")
    mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mod.APIConnectionError = type("APIConnectionError", (Exception,), {})
    mod.APIStatusError = type("APIStatusError", (Exception,), {"status_code": 500, "message": "boom"})

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return msg

    class _Messages:
        def stream(self, **kw):
            record.update(kw)
            return _Stream()

    class _Client:
        def __init__(self, *a, **kw):
            self.beta = types.SimpleNamespace(messages=_Messages())

    mod.Anthropic = _Client
    return mod


def _claude_msg(stop_reason="end_turn", text="SELECT 1;"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=42, cache_read_input_tokens=90),
        stop_details=types.SimpleNamespace(explanation=None, category="cyber"),
    )


@pytest.fixture
def claude_client(client, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    return client


def test_claude_ask_request_shape(claude_client, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg()))
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={
        "question": "Which src_ table has the 4624s?",
        "schema": "CREATE TABLE src_1 (...);",
        "history": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "sneaky"},   # not a chat role — dropped
        ],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["answer"] == "SELECT 1;" and out["model"] == "claude-opus-5"
    assert out["usage"]["cache_read_input_tokens"] == 90

    assert record["model"] == "claude-opus-5"
    assert record["betas"] == ["server-side-fallback-2026-07-01"]
    assert record["fallbacks"] == "default"
    assert "thinking" not in record  # omitted on purpose — adaptive by default on this model
    # Schema rides in system with the cache breakpoint on it.
    assert record["system"][1]["text"].endswith("CREATE TABLE src_1 (...);")
    assert record["system"][1]["cache_control"] == {"type": "ephemeral"}
    # History sanitized; the new question is the last user turn.
    assert [m["role"] for m in record["messages"]] == ["user", "assistant", "user"]
    assert record["messages"][-1]["content"] == "Which src_ table has the 4624s?"


def test_claude_refusal_is_a_400_with_category(claude_client, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg(stop_reason="refusal")))
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})
    assert r.status_code == 400 and "cyber" in r.json()["detail"]


def test_claude_missing_sdk_is_actionable(claude_client, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})
    assert r.status_code == 400 and "pip install" in r.json()["detail"]


def test_claude_empty_question_rejected(claude_client):
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "   "})
    assert r.status_code == 400


# ============================================================ pivot plugin

PIVOT_ROWS = [
    ["Host", "User", "Channel", "Bytes"],
    ["SRV1", "alice", "Security", "100"],
    ["SRV1", "alice", "System", "200"],
    ["SRV1", "bob", "Security", ""],        # blank — not counted by COUNTA, not summed
    ["SRV2", "alice", "Security", "N/A"],   # junk — must not aggregate as 0.0
    ["SRV2", "carol", "System", "40"],
]


@pytest.fixture
def pivot_client(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(PIVOT_ROWS, "pivot.csv"), name="pivot")
    return client, rec["id"]


def _agg(client, source_id, **body):
    r = client.post("/api/plugin/pivot/aggregate", json={"source_id": source_id, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_pivot_distinct_count_is_right_at_every_level(pivot_client):
    """The reason subtotals are queried per level instead of summed from the
    cells above them: alice appears under both hosts, so adding the per-host
    distinct counts (1 + 1 + 1) would report 3 distinct users where there are
    3 — but the *Security* column has alice twice and bob once, and summing
    its children would say 3 where the answer is 2."""
    client, source_id = pivot_client
    out = _agg(client, source_id,
               values=[{"agg": "count_distinct", "column": "User"}],
               group_sets=[["Host"], ["Channel"], []])
    per_host = dict((r[0], r[1]) for r in out["sets"][0]["rows"])
    per_channel = dict((r[0], r[1]) for r in out["sets"][1]["rows"])
    assert per_host == {"SRV1": 2, "SRV2": 2}          # alice+bob, alice+carol
    assert per_channel == {"Security": 2, "System": 2}  # alice+bob, alice+carol
    assert out["sets"][2]["rows"][0][0] == 3            # alice, bob, carol — not 4


def test_pivot_count_counts_non_blank_values_like_excel(pivot_client):
    """Count with a field behind it is COUNTA: rows that have a value in that
    field. Bare Count (no field) is the row count. If those two came out the
    same, putting a specific field in Values would be pointless."""
    client, source_id = pivot_client
    out = _agg(client, source_id,
               values=[{"agg": "count"}, {"agg": "count", "column": "Bytes"}],
               group_sets=[[]])
    rows_total, bytes_present = out["sets"][0]["rows"][0]
    assert rows_total == 5
    assert bytes_present == 4      # one row's Bytes is blank; "N/A" is still a value


def test_pivot_numeric_aggregates_skip_junk_rather_than_zeroing_it(pivot_client):
    """A bare CAST would read "N/A" as 0.0 and drag the average down; the
    guarded cast makes it NULL, which drops out of both SUM and AVG."""
    client, source_id = pivot_client
    out = _agg(client, source_id,
               values=[{"agg": "sum", "column": "Bytes"}, {"agg": "avg", "column": "Bytes"}],
               group_sets=[[]])
    total, mean = out["sets"][0]["rows"][0]
    assert total == 340                      # 100 + 200 + 40
    assert mean == pytest.approx(340 / 3)    # three numeric values, not five rows


def test_pivot_filters_compile_and_survive_the_regex_in_the_numeric_guard(pivot_client):
    """A Sum plus a filter is the shape that broke first: the numeric guard
    embeds a regex containing `?`, and the parameter inliner used to read
    those as placeholders and shift every bound value one slot along."""
    client, source_id = pivot_client
    out = _agg(client, source_id,
               values=[{"agg": "sum", "column": "Bytes"}, {"agg": "count"}],
               group_sets=[[]],
               filters=[{"column": "Host", "op": "in", "values": ["SRV1"]}])
    total, n = out["sets"][0]["rows"][0]
    assert (total, n) == (300, 3)


def test_pivot_not_in_keeps_rows_with_no_value(pivot_client):
    """`NOT IN` never matches NULL, so excluding one value would silently
    drop every blank cell too if the clause didn't say otherwise."""
    client, source_id = pivot_client
    out = _agg(client, source_id, values=[{"agg": "count"}], group_sets=[[]],
               filters=[{"column": "Bytes", "op": "not_in", "values": ["100"]}])
    assert out["sets"][0]["rows"][0][0] == 4


def test_pivot_empty_filter_selection_filters_nothing(pivot_client):
    """An untouched filter field is Excel's "(All)", not "nothing"."""
    client, source_id = pivot_client
    out = _agg(client, source_id, values=[{"agg": "count"}], group_sets=[[]],
               filters=[{"column": "Host", "op": "in", "values": []}])
    assert out["sets"][0]["rows"][0][0] == 5


def test_pivot_detail_returns_the_rows_behind_a_cell(pivot_client):
    client, source_id = pivot_client
    r = client.post("/api/plugin/pivot/detail", json={
        "source_id": source_id,
        "cell": [{"column": "Host", "value": "SRV1"}, {"column": "Channel", "value": "Security"}],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["columns"][0] == "Line"
    assert [row[1:3] for row in out["rows"]] == [["SRV1", "alice"], ["SRV1", "bob"]]


def test_pivot_detail_on_a_blank_cell_means_blank_not_empty_string(pivot_client):
    """A cell keyed on (blank) has to find the rows with no value there —
    which in an ingested table can be '' or NULL depending on how the row
    was padded."""
    client, source_id = pivot_client
    r = client.post("/api/plugin/pivot/detail", json={
        "source_id": source_id, "cell": [{"column": "Bytes", "value": ""}],
    })
    assert r.status_code == 200, r.text
    assert [row[1:3] for row in r.json()["rows"]] == [["SRV1", "bob"]]


# `source_id` is filled in from the fixture unless the case is about it
# being wrong, which is what the third element says.
@pytest.mark.parametrize("body, fragment, own_source_id", [
    ({"values": [{"agg": "count"}], "group_sets": [[]], "source_id": -1}, "merge", True),
    ({"values": [{"agg": "count"}], "group_sets": [[]]}, "source_id", True),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "source_id": 999}, "No source", True),
    ({"values": [{"agg": "count"}], "group_sets": [["Nope"]]}, "No column", False),
    ({"values": [{"agg": "nope", "column": "Host"}], "group_sets": [[]]}, "Unknown aggregation", False),
    ({"values": [{"agg": "sum"}], "group_sets": [[]]}, "needs a column", False),
    ({"values": [{"agg": "count"}], "group_sets": [["Host"]] * 40}, "Too many", False),
    ({"values": [{"agg": "count"}], "group_sets": []}, "group_sets is required", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]],
      "filters": [{"column": "Host", "op": "wat", "values": ["x"]}]}, "Unknown filter operator", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]],
      "filters": [{"column": "Bytes", "op": "gt", "value": "abc"}]}, "needs a number", False),
])
def test_pivot_validation_rejects_bad_requests(pivot_client, body, fragment, own_source_id):
    """Everything the analyst can cause is a 400 with a message they can act
    on; nothing reaches SQL that wasn't checked against the real column list."""
    client, source_id = pivot_client
    if not own_source_id:
        body = dict(body, source_id=source_id)
    r = client.post("/api/plugin/pivot/aggregate", json=body)
    assert r.status_code == 400, r.text
    assert fragment.lower() in r.json()["detail"].lower()


def test_pivot_meta_lists_the_vocabulary_the_ui_builds_from(pivot_client):
    client, _ = pivot_client
    out = client.get("/api/plugin/pivot/meta").json()
    assert {a["id"] for a in out["aggregations"]} >= {"count", "count_distinct", "sum", "avg", "min", "max"}
    assert {o["id"] for o in out["operators"]} >= {"in", "not_in", "empty", "contains"}
    assert out["limits"]["group_sets"] >= 4


def test_pivot_groups_by_a_derived_column(client, store, write_csv, example_registry, monkeypatch):
    """Derived columns are merged into src["columns"] but materialised in the
    `drv_<id>` sidecar, so SQL that names one has to join it. Without the
    join, SQLite's double-quoted-string fallback turns `"Day"` into the
    literal 'Day' and every row lands in one group named after the column —
    a wrong answer that looks like a real one."""
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rows = [["Host", "Epoch"],
            ["SRV1", "1700000000"],   # 2023-11-14
            ["SRV1", "1700086400"],   # 2023-11-15
            ["SRV2", "1700000050"]]
    sid = store.ingest_csv(write_csv(rows, "drv.csv"), name="drv", build_fts=False)["id"]
    res = store.add_derived_column(sid, "When", "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)

    out = _agg(client, sid, values=[{"agg": "count"}], group_sets=[["When"]])
    days = sorted(r[0][:10] for r in out["sets"][0]["rows"])
    assert days == ["2023-11-14", "2023-11-14", "2023-11-15"], days

    # and as a measure, and as a filter
    out = _agg(client, sid, values=[{"agg": "count_distinct", "column": "When"}],
               group_sets=[["Host"]],
               filters=[{"column": "When", "op": "not_empty"}])
    assert dict((r[0], r[1]) for r in out["sets"][0]["rows"]) == {"SRV1": 2, "SRV2": 1}


def test_pivot_detail_shows_derived_values_not_the_column_name(client, store, write_csv,
                                                               example_registry, monkeypatch):
    """The drill-down selects every column the source reports, which includes
    the derived ones — from the sidecar, or each row shows the string 'When'
    where its timestamp should be."""
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    sid = store.ingest_csv(write_csv([["Host", "Epoch"], ["SRV1", "1700000000"]], "drv2.csv"),
                           name="drv2", build_fts=False)["id"]
    res = store.add_derived_column(sid, "When", "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)

    r = client.post("/api/plugin/pivot/detail", json={
        "source_id": sid, "cell": [{"column": "Host", "value": "SRV1"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    when = out["rows"][0][out["columns"].index("When")]
    assert when.startswith("2023-11-14"), when


def test_pivot_handles_a_question_mark_in_a_column_name(client, store, write_csv,
                                                        example_registry, monkeypatch):
    """`Elevated?` is an ordinary CSV header, and q() quotes it into the SQL
    text — where a parameter inliner that only skips *string* literals reads
    the `?` as a placeholder and shifts every bound value one slot along."""
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rows = [["Host", "Elevated?", "Bytes"],
            ["SRV1", "yes", "10"],
            ["SRV1", "no", "20"],
            ["SRV2", "yes", "30"]]
    sid = store.ingest_csv(write_csv(rows, "qm.csv"), name="qm", build_fts=False)["id"]

    out = _agg(client, sid, values=[{"agg": "sum", "column": "Bytes"}],
               group_sets=[["Elevated?"]],
               filters=[{"column": "Host", "op": "in", "values": ["SRV1"]}])
    assert dict((r[0], r[1]) for r in out["sets"][0]["rows"]) == {"yes": 10, "no": 20}

    r = client.post("/api/plugin/pivot/detail", json={
        "source_id": sid, "cell": [{"column": "Elevated?", "value": "yes"}]})
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 2


def test_pivot_like_filters_escape_their_own_wildcards(client, store, write_csv,
                                                       example_registry, monkeypatch):
    """`_` and `%` are LIKE wildcards; an analyst typing SRV_1 means the
    underscore. Matching SRVX1 too is a silently wider search."""
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rows = [["Host"], ["SRV_1"], ["SRVX1"], ["OTHER"]]
    sid = store.ingest_csv(write_csv(rows, "like.csv"), name="like", build_fts=False)["id"]

    out = _agg(client, sid, values=[{"agg": "count"}], group_sets=[[]],
               filters=[{"column": "Host", "op": "contains", "value": "SRV_1"}])
    assert out["sets"][0]["rows"][0][0] == 1

    out = _agg(client, sid, values=[{"agg": "count"}], group_sets=[[]],
               filters=[{"column": "Host", "op": "starts", "value": "SRV%"}])
    assert out["sets"][0]["rows"][0][0] == 0


def test_pivot_distinct_count_ignores_blanks_like_count_does(pivot_client):
    """Blank is the absence of a value, not one more distinct value. Counting
    '' as a category makes Distinct count exceed Count on the same column."""
    client, source_id = pivot_client
    out = _agg(client, source_id,
               values=[{"agg": "count", "column": "Bytes"},
                       {"agg": "count_distinct", "column": "Bytes"}],
               group_sets=[[]])
    present, distinct = out["sets"][0]["rows"][0]
    assert present == 4                  # 100, 200, N/A, 40
    assert distinct == 4 and distinct <= present

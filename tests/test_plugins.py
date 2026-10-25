"""The plugin system end to end: the loader (plugin_api.py), the generic
Store.ingest_rows path plugins feed, the HTTP routes, directory-scan
integration, and the example mft_usn plugin parsed against synthetic NTFS
fixtures built byte-by-byte here (no real evidence files in the repo)."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import struct
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from spawn import running_server
from winnow import plugin_api
from winnow.plugin_api import PluginRegistry

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
    # Not even the first registration survives: a plugin listed as failed
    # must not leave a working format behind (see the partial-register
    # test below — this assertion used to pin the opposite).
    with pytest.raises(KeyError):
        reg.get_format("dupe.x")


def test_a_register_that_raises_partway_keeps_nothing_it_registered(plug_dir):
    """register() writes straight into the live tables, so a plugin that
    failed on its fourth call kept the first three — a format offered in
    the import dialog, a route answering, a row action in the menu — while
    the panel listed it as failed. A plugin loaded after it is unaffected."""
    _write_plugin(plug_dir, "aaa_half", """
        def register(api):
            api.register_ingest_format(id="half", label="Half", extensions=[".half"],
                                       parse=lambda path, options: {"columns": ["A"], "rows": []})
            api.register_api("ping", lambda req: {"pong": True})
            api.register_row_action(id="look", label="Look", handler=lambda req: {})
            raise RuntimeError("config file missing")
    """)
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    reg.load([plug_dir])
    by_name = {p["fs_name"]: p for p in reg.describe()}
    assert "config file missing" in by_name["aaa_half"]["error"]
    assert by_name["aaa_half"]["formats"] == []
    with pytest.raises(KeyError):
        reg.get_format("aaa_half.half")
    assert reg.get_api("aaa_half", "ping") is None
    assert not [a for a in reg.list_row_actions() if a["plugin_fs"] == "aaa_half"]
    assert by_name["demo"]["error"] is None and reg.get_format("demo.lines")


def test_rollback_covers_every_registration_table():
    """_snapshot/_rollback work from a list of table names, so a new
    register_* hook whose table isn't on it would keep its registrations
    after a failed register() again. Every dict a load fills is on it."""
    from winnow.plugin_api import _Loaded
    tables = {k for k, v in vars(_Loaded()).items() if isinstance(v, dict)}
    assert tables == set(_Loaded._TABLES)


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
    from winnow import workspace as WS

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


def test_a_plugin_that_exits_at_import_is_a_load_error_not_an_exit(plug_dir):
    """`sys.exit("needs Winnow 2.0")` is an ordinary way for a plugin to
    bail on a version check, and SystemExit is not an Exception — so it
    used to walk straight out of the registry. At startup that killed the
    import of server.py itself: install such a plugin and Winnow would
    not come back up until it was deleted by hand."""
    _write_plugin(plug_dir, "bail",
                  'import sys\nsys.exit("needs a newer Winnow")\n\ndef register(api):\n    pass\n')
    reg = PluginRegistry()
    reg.load([plug_dir])          # used to raise SystemExit
    rec = next(p for p in reg.describe() if p["fs_name"] == "bail")
    assert rec["error"] == "SystemExit: needs a newer Winnow"
    assert rec["enabled"] is True and rec["formats"] == []


def test_a_plugin_that_raises_keyboardinterrupt_is_caught_too(plug_dir):
    _write_plugin(plug_dir, "ki", "raise KeyboardInterrupt()\n\ndef register(api):\n    pass\n")
    reg = PluginRegistry()
    reg.load([plug_dir])
    rec = next(p for p in reg.describe() if p["fs_name"] == "ki")
    assert rec["error"].startswith("KeyboardInterrupt")


def test_one_plugin_exiting_does_not_take_the_others_with_it(plug_dir):
    """The loop has to keep going. A single bad plugin taking out every
    other plugin's tabs and formats is the failure this registry exists
    to prevent, and BaseException was a hole straight through it."""
    _write_plugin(plug_dir, "aaa_bail", 'import sys\nsys.exit(3)\n\ndef register(api):\n    pass\n')
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    reg.load([plug_dir])
    by_fs = {p["fs_name"]: p for p in reg.describe()}
    assert by_fs["aaa_bail"]["error"].startswith("SystemExit")
    assert by_fs["demo"]["error"] is None
    assert reg.get_format("demo.lines") is not None


# ------------------------------------------------------- reloads and races

class _ImportGate:
    """Lets a test hold a plugin's import open mid-reload, so "during a
    reload" is a state the test is IN rather than a race it hopes to hit.
    The plugin finds it in sys.modules — its source can't import the test."""
    NAME = "_winnow_test_import_gate"

    def __init__(self):
        import threading
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.imports = 0


GATED_PLUGIN = """
    import sys
    gate = sys.modules["_winnow_test_import_gate"]
    gate.imports += 1
    if gate.block:
        gate.entered.set()
        gate.release.wait(10)   # an import that takes a while: pandas, an SDK

    def register(api):
        api.register_api("ping", lambda req: "{name}", methods=["GET"])
"""


@pytest.fixture
def gated(plug_dir, monkeypatch):
    import sys
    gate = _ImportGate()
    monkeypatch.setitem(sys.modules, _ImportGate.NAME, gate)
    # a_slow sorts first, so a reload held inside it has registered
    # nothing new yet — the moment the old code had nothing at all.
    for name in ("a_slow", "b_ok", "c_ok"):
        _write_plugin(plug_dir, name, GATED_PLUGIN.replace("{name}", name))
    reg = PluginRegistry()
    reg.load([plug_dir])
    yield gate, reg
    gate.release.set()      # never leave a reload thread parked


def test_plugin_routes_keep_answering_while_a_reload_runs(gated, plug_dir):
    """load() cleared the live registry and refilled it as each plugin
    imported, so every plugin route 404'd (and the listing was empty) for
    as long as a reload took — and Settings toggles, installs and every
    case open reload."""
    import threading
    gate, reg = gated
    gate.block = True
    t = threading.Thread(target=reg.load, args=([plug_dir],), daemon=True)
    t.start()
    assert gate.entered.wait(10)
    try:
        assert reg.get_api("c_ok", "ping") is not None
        assert [p["fs_name"] for p in reg.describe()] == ["a_slow", "b_ok", "c_ok"]
    finally:
        gate.release.set()
        t.join(10)
    assert reg.get_api("c_ok", "ping") is not None


def test_overlapping_reloads_are_serialised(gated, plug_dir):
    """Two reloads at once (a toggle in one window while a case opens in
    another; both are threadpooled routes) filled the same dicts: each
    plugin's register() collided with its own registration from the
    other load, so healthy plugins were listed twice and marked failed
    with "Duplicate API route"."""
    import threading
    import time
    gate, reg = gated
    gate.block = True
    imports_before = gate.imports
    first = threading.Thread(target=reg.load, args=([plug_dir],), daemon=True)
    first.start()
    assert gate.entered.wait(10)
    second = threading.Thread(target=reg.load, args=([plug_dir],), daemon=True)
    second.start()
    time.sleep(0.3)
    # The second load is waiting its turn, not importing alongside.
    assert gate.imports == imports_before + 1
    gate.block = False
    gate.release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()

    listing = reg.describe()
    assert [p["fs_name"] for p in listing] == ["a_slow", "b_ok", "c_ok"]
    assert [p["error"] for p in listing] == [None, None, None]
    assert all(reg.get_api(fs, "ping") for fs in ("a_slow", "b_ok", "c_ok"))


def test_a_reload_that_fails_leaves_the_previous_set_running(gated, plug_dir):
    gate, reg = gated

    def policy_that_breaks(fs_name, directory):
        if fs_name == "b_ok":
            raise OSError("plugins.json unreadable")
        return True

    with pytest.raises(OSError):
        reg.load([plug_dir], enabled_for=policy_that_breaks)
    assert [p["fs_name"] for p in reg.describe()] == ["a_slow", "b_ok", "c_ok"]
    assert all(reg.get_api(fs, "ping") for fs in ("a_slow", "b_ok", "c_ok"))


def test_installing_a_plugin_that_exits_answers_instead_of_hanging(plugin_client, plug_dir):
    """The reported symptom. The install route reloads the registry in a
    worker thread; a BaseException escaping that thread means the request
    never returns and Winnow simply stops answering."""
    init = b'import sys\nsys.exit("needs a newer Winnow")\n\ndef register(api):\n    pass\n'
    r = plugin_client.post(
        "/api/plugins/install",
        files=[("files", ("__init__.py", init))],
        data={"paths": json.dumps(["bailfolder/__init__.py"])},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    # Installed — the files are on disk — and reported as failed to load,
    # exactly like a plugin with a syntax error.
    assert out["installed"] == "bailfolder"
    assert out["error"] == "SystemExit: needs a newer Winnow"
    assert (plug_dir / "bailfolder" / "__init__.py").is_file()


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


# What the check has to refuse on Windows, where `\\x` and `C:x` are not
# absolute to pathlib and have no "..": a root, a drive, a drive-relative
# name, a UNC path, an NTFS alternate stream. Checked on every platform,
# so a Linux CI run proves what a Windows install relies on.
WINDOWS_ESCAPES = [r"\Windows\Tasks\x.py", r"\__init__.py", "C:__init__.py",
                   r"C:Users\Public\x.py", r"C:\evil.py", "c:/evil.py",
                   r"\\server\share\x.py", "plug.py:hidden", "pkg/__init__.py:ads",
                   "D:pkg/__init__.py"]


@pytest.mark.parametrize("rel", WINDOWS_ESCAPES)
def test_install_path_check_refuses_windows_escapes(rel):
    import server
    assert server._plugin_upload_parts(rel) is None


@pytest.mark.parametrize("rel,parts", [("kplug.py", ("kplug.py",)),
                                        ("pkg/__init__.py", ("pkg", "__init__.py")),
                                        (r"pkg\sub\x.py", ("pkg", "sub", "x.py"))])
def test_install_path_check_keeps_ordinary_paths(rel, parts):
    import server
    assert server._plugin_upload_parts(rel) == parts


@pytest.mark.parametrize("name,paths", [
    ("C:__init__.py", None),                          # single file: fs_name "C:__init__"
    ("__init__.py", ["C:pkg/__init__.py"]),           # folder: fs_name "C:pkg"
    ("x.py", ["plug.py:hidden"]),
])
def test_install_refuses_drive_relative_and_stream_names(plugin_client, plug_dir, name, paths):
    """Through the route: these were ACCEPTED (on Linux too — a colon is an
    ordinary character there), and on Windows each one writes somewhere
    other than plugins/."""
    before = sorted(p.name for p in plug_dir.iterdir())
    data = {"paths": json.dumps(paths)} if paths else None
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", (name, INSTALLABLE.encode()))], data=data)
    assert r.status_code == 400, r.text
    assert sorted(p.name for p in plug_dir.iterdir()) == before


def test_install_refuses_a_symlink_out_of_plugins(plugin_client, plug_dir, tmp_path):
    """The resolved destination must stay under plugins/ — a link already
    in there pointing elsewhere is a way out that no string check sees."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (plug_dir / "linked").symlink_to(outside, target_is_directory=True)
    r = plugin_client.post("/api/plugins/install",
                           files=[("files", ("__init__.py", INSTALLABLE.encode()))],
                           data={"paths": json.dumps(["linked/__init__.py"]), "overwrite": "true"})
    assert r.status_code == 400
    assert outside.is_dir() and list(outside.iterdir()) == []


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


@pytest.mark.parametrize("first,second", [("folder", "file"), ("file", "folder")])
def test_install_over_the_other_form_of_the_same_plugin(plugin_client, plug_dir, first, second):
    """foo.py and foo/ are one plugin to the loader (the folder sorts first
    and wins). The collision check looked at the destination PATH, so
    installing foo.py beside foo/ answered "Installed foo" and the new
    file never ran. Same name is taken, whichever form; overwrite replaces
    both, so what was installed is what loads."""
    def install(form, version, **data):
        body = (f'PLUGIN = {{"name": "foo", "version": "{version}"}}\n'
                'def register(api):\n    pass\n').encode()
        if form == "file":
            return plugin_client.post("/api/plugins/install",
                                      files=[("files", ("foo.py", body))], data=data or None)
        return plugin_client.post("/api/plugins/install", files=[("files", ("__init__.py", body))],
                                  data={"paths": json.dumps(["foo/__init__.py"]), **data})

    assert install(first, "1.0").status_code == 200
    assert install(second, "2.0").status_code == 409
    r = install(second, "2.0", overwrite="true")
    assert r.status_code == 200, r.text
    rec = next(p for p in r.json()["plugins"] if p["fs_name"] == "foo")
    assert rec["version"] == "2.0"
    assert (plug_dir / "foo.py").exists() == (second == "file")
    assert (plug_dir / "foo").exists() == (second == "folder")


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


@pytest.fixture
def example_registry() -> PluginRegistry:
    """Function-scoped, and that is load-bearing: server._reload_plugins()
    calls PLUGINS.load(...) which mutates the registry IN PLACE. A test that
    monkeypatches server.PLUGINS to this object and then does anything that
    reloads (applying a profile, toggling a plugin, switching case) rewrites
    it against the real plugin directories — where the bundled examples are
    default-OFF. Shared across a module, that silently removed the example
    plugins' routes from every test that ran afterwards."""
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    rec = next(p for p in reg.describe() if p["name"] == "mft-usn")
    assert rec["error"] is None, rec["error"]
    return reg


def test_filetime_keeps_every_microsecond_and_never_rounds_up(tmp_path):
    """filetime_to_iso divided ticks by 10 as a float. At 2024 magnitudes
    that is past a float's exact range, and timedelta rounds the fraction
    it is handed — so 01.9999999 printed as 02.000000, manufacturing the
    zeroed subseconds a timestomping check looks for."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mft_usn_common_under_test", EXAMPLES / "mft_usn" / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    base = ft(datetime(2024, 1, 5, 13, 22, 1, 123456))
    assert common.filetime_to_iso(base + 7) == "2024-01-05 13:22:01.123456"
    almost = ft(datetime(2024, 1, 5, 13, 22, 1, 999999)) + 9
    assert common.filetime_to_iso(almost) == "2024-01-05 13:22:01.999999"


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


def test_usn_short_v3_slot_is_skipped_not_raised(example_registry, tmp_path, monkeypatch):
    """A slot claiming V3 with a 64-byte length passed the 60-byte minimum,
    but a V3 header is 76 bytes: _record unpacked past the slot's end, and
    at the end of the file (or of a read chunk) struct.error escaped the
    generator and failed the whole ingest over one torn slot. Mid-file it
    read the next record's bytes as its own. A slot shorter than its own
    version's header is carved past like any other non-record."""
    import sys

    fmt = example_registry.get_format("mft-usn.usn")
    t = ft(datetime(2024, 6, 1, 12, 0, 0))
    good = [usn_v2(t, f"f{i}.txt", entry=300 + i, usn=i) for i in range(3)]
    short_v3 = struct.pack("<IHH", 64, 3, 0) + b"\x01" * 56

    tail = tmp_path / "tail.usn"
    tail.write_bytes(b"".join(good) + short_v3)
    assert [r[1] for r in fmt.parse(str(tail), {})["rows"]] == ["f0.txt", "f1.txt", "f2.txt"]

    mid = tmp_path / "mid.usn"
    mid.write_bytes(good[0] + short_v3 + good[1] + short_v3 + good[2])
    assert [r[1] for r in fmt.parse(str(mid), {})["rows"]] == ["f0.txt", "f1.txt", "f2.txt"]

    # The same slot ending exactly on a read chunk's edge, mid-file.
    usn_mod = sys.modules[fmt.parse.__module__]
    monkeypatch.setattr(usn_mod, "CHUNK", len(good[0]) + len(short_v3))
    assert [r[1] for r in fmt.parse(str(mid), {})["rows"]] == ["f0.txt", "f1.txt", "f2.txt"]


def test_usn_garbage_is_scanned_in_linear_time(example_registry, tmp_path):
    """Every 8-byte slot whose first u32 is zero was treated as the start
    of a sparse run, and measuring the run sliced off the rest of the
    chunk: garbage whose slots start with a zero u32 without being zero
    runs scanned the chunk once per slot. 1 MiB took ~5 s, 2 MiB ~20 s,
    and one 8 MiB chunk would take minutes. Linear, 2 MiB is ~0.1 s; the
    bound leaves an order of magnitude for a slow runner on either side.
    The record after the garbage proves the skip lands on it, not past it."""
    import time

    fmt = example_registry.get_format("mft-usn.usn")
    t = ft(datetime(2024, 6, 1, 12, 0, 0))
    j = tmp_path / "garbage.usn"
    j.write_bytes(b"\x00\x00\x00\x00\x01\x00\x00\x00" * (2 * 1024 * 1024 // 8)
                  + usn_v2(t, "after.txt"))
    t0 = time.monotonic()
    rows = list(fmt.parse(str(j), {})["rows"])
    elapsed = time.monotonic() - t0
    assert [r[1] for r in rows] == ["after.txt"]
    assert elapsed < 4, f"2 MiB of garbage took {elapsed:.1f}s — the zero-run skip is quadratic again"


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


def _sel(source_id, **kw):
    return {"source_id": source_id, "src_col": "SourceHost", "dst_col": "DestHost", **kw}


def test_lateral_movement_edges(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [_sel(source_id, label_col="User")],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["truncated"] is False
    # k tags the selection; no time_col here, so t is None.
    assert out["edges"] == [
        {"k": 0, "src": "WS1", "dst": "SRV1", "t": None, "n": 2, "labels": 2},
        {"k": 0, "src": "WS2", "dst": "SRV1", "t": None, "n": 1, "labels": 1},
    ]


def test_lateral_movement_multiple_selections_carry_their_index(lateral_client):
    """Two events out of one table land on one graph, each edge tagged with
    the selection (k) that produced it — that's how the UI colors them."""
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [
            _sel(source_id),
            _sel(source_id, conditions=[{"column": "User", "op": "equals", "value": "alice"}]),
        ],
    })
    assert r.status_code == 200, r.text
    edges = r.json()["edges"]
    assert {e["k"] for e in edges} == {0, 1}
    # selection 1 is alice-only: WS1->SRV1 once, WS2->SRV1 once.
    k1 = {(e["src"], e["dst"]): e["n"] for e in edges if e["k"] == 1}
    assert k1 == {("WS1", "SRV1"): 1, ("WS2", "SRV1"): 1}


def test_lateral_movement_conditions_filter(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [_sel(source_id, conditions=[
            {"column": "User", "op": "in", "value": "bob,carol"}])],
    })
    assert r.status_code == 200, r.text
    # only bob's WS1->SRV1 survives (carol's row is a filtered '-' source)
    assert r.json()["edges"] == [
        {"k": 0, "src": "WS1", "dst": "SRV1", "t": None, "n": 1, "labels": None}]


def test_lateral_movement_validation(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/edges", json={"selections": []})
    assert r.status_code == 400 and "at least one" in r.json()["detail"].lower()
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [_sel(source_id, dst_col="Nope")]})
    assert r.status_code == 400 and "Nope" in r.json()["detail"]
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [_sel(-1)]})
    # "table", not "source": the message reaches the analyst through the
    # plugin's error toast, so it uses the word the rest of the UI uses.
    assert r.status_code == 400 and "no table" in r.json()["detail"].lower()
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [_sel(source_id, dst_col="SourceHost")]})
    assert r.status_code == 400


def test_lateral_movement_edges_over_a_merge(lateral_client, store, write_csv):
    """Invariant #9: edge counts aggregate across every member of a merge."""
    client, source_id = lateral_client
    src = store.get_source(source_id)
    header = [c["name"] for c in src["columns"] if not c.get("derived")]
    row = {c: "" for c in header}
    row.update({"SourceHost": "WKS1", "DestHost": "SRV1"})
    other = store.ingest_csv(write_csv([header, [row[c] for c in header]], "lm2.csv"),
                             name="lm2", build_fts=False)["id"]
    mid = store.create_merge("lmmerge", [source_id, other])["id"]

    def n_for(sid):
        r = client.post("/api/plugin/lateral_movement/edges", json={
            "selections": [{"source_id": sid, "src_col": "SourceHost", "dst_col": "DestHost"}]})
        assert r.status_code == 200, r.text
        return {(e["src"], e["dst"]): e["n"] for e in r.json()["edges"]}

    single = n_for(source_id)
    merged = n_for(mid)
    assert merged[("WKS1", "SRV1")] == single.get(("WKS1", "SRV1"), 0) + 1


def test_lateral_movement_preview_counts_what_a_graph_would_draw(lateral_client):
    """The event builder's preview: rows the conditions match, then the
    links and hosts those rows draw once empty, '-' and self endpoints
    drop out — the same rows Build would turn into edges."""
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/preview", json={
        "selection": _sel(source_id, label_col="User")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["matched"] == 5          # every row; no conditions
    assert out["hops"] == 3             # the self-loop and the '-' source draw nothing
    assert (out["links"], out["hosts"]) == (2, 3)
    assert out["top"] == [{"src": "WS1", "dst": "SRV1", "n": 2, "labels": 2},
                          {"src": "WS2", "dst": "SRV1", "n": 1, "labels": 1}]
    assert out["first"] is None and out["links_capped"] is False

    sel = _sel(source_id, conditions=[{"column": "User", "op": "equals", "value": "alice"}])
    out = client.post("/api/plugin/lateral_movement/preview", json={"selection": sel}).json()
    assert (out["matched"], out["hops"]) == (3, 2)   # alice's self-loop matches, draws nothing
    edges = client.post("/api/plugin/lateral_movement/edges", json={"selections": [sel]}).json()["edges"]
    assert out["hops"] == sum(e["n"] for e in edges)


def test_lateral_movement_preview_refuses_what_build_would(lateral_client):
    client, source_id = lateral_client
    for sel in (_sel(source_id, dst_col="SourceHost"),
                _sel(source_id, dst_col="Nope"),
                _sel(source_id, conditions=[{"column": "User", "op": "in", "value": " , "}])):
        r = client.post("/api/plugin/lateral_movement/preview", json={"selection": sel})
        assert r.status_code == 400, sel


def test_lateral_movement_preview_time_span(timed_client):
    client, sid = timed_client
    r = client.post("/api/plugin/lateral_movement/preview", json={"selection": {
        "source_id": sid, "src_col": "SourceHost", "dst_col": "DestHost", "time_col": "When"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["first"].startswith("2026-03-14 08:00") and out["last"].startswith("2026-03-15 09:00")


def test_lateral_movement_preview_over_a_merge(lateral_client, store, write_csv):
    client, source_id = lateral_client
    src = store.get_source(source_id)
    header = [c["name"] for c in src["columns"] if not c.get("derived")]
    row = {c: "" for c in header}
    row.update({"SourceHost": "WKS1", "DestHost": "SRV1"})
    other = store.ingest_csv(write_csv([header, [row[c] for c in header]], "lm3.csv"),
                             name="lm3", build_fts=False)["id"]
    mid = store.create_merge("lmmerge2", [source_id, other])["id"]
    r = client.post("/api/plugin/lateral_movement/preview", json={"selection": _sel(mid)})
    assert r.status_code == 200, r.text
    assert (r.json()["matched"], r.json()["hops"]) == (6, 4)


def test_lateral_movement_values_suggest_the_commonest(lateral_client):
    client, source_id = lateral_client
    r = client.post("/api/plugin/lateral_movement/values",
                    json={"source_id": source_id, "column": "User"})
    assert r.status_code == 200, r.text
    assert r.json()["values"] == [{"value": "alice", "n": 3}, {"value": "bob", "n": 1},
                                  {"value": "carol", "n": 1}]
    bad = client.post("/api/plugin/lateral_movement/values",
                      json={"source_id": source_id, "column": "Nope"})
    assert bad.status_code == 400 and "Nope" in bad.json()["detail"]


def test_lateral_movement_tab_asset(lateral_client):
    client, _ = lateral_client
    r = client.get("/plugin_assets/lateral_movement/ui/tab.js")
    assert r.status_code == 200 and "export default" in r.text


TIMED_ROWS = [
    ["SourceHost", "DestHost", "User", "When"],
    ["WS1", "SRV1", "alice", "2026-03-14 08:00:00"],
    ["WS1", "SRV1", "bob", "2026-03-14 08:30:00"],
    ["WS2", "SRV1", "alice", "2026-03-15 09:00:00"],
]


@pytest.fixture
def timed_client(client, store, write_csv, example_registry, monkeypatch):
    import server
    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(TIMED_ROWS, "timed.csv"), name="timed")
    return client, rec["id"]


def test_lateral_movement_time_bucketing_and_filter(timed_client):
    client, sid = timed_client
    sel = {"source_id": sid, "src_col": "SourceHost", "dst_col": "DestHost",
           "time_col": "When", "label_col": "User"}
    # Full range: every edge carries a bucket key, chosen from the span.
    r = client.post("/api/plugin/lateral_movement/edges", json={"selections": [sel]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["bucket"] in ("minute", "hour", "day")
    assert all(e["t"] for e in out["edges"])

    # The case timeframe passes straight through and cuts the 15th.
    r = client.post("/api/plugin/lateral_movement/edges", json={
        "selections": [sel], "time": {"start": "2026-03-14 00:00:00", "end": "2026-03-14 23:59:59"}})
    assert r.status_code == 200, r.text
    edges = r.json()["edges"]
    assert {(e["src"], e["dst"]) for e in edges} == {("WS1", "SRV1")}


@pytest.mark.parametrize("start,end", [
    ("2026-03-14 00:00:00", "2026-03-14 23:59:59"),
    ("2026-03-14T00:00:00", "2026-03-14T23:59:59"),
    ("3/14/2026 0:00", "3/14/2026 23:59"),
    ("2026-03-14", None),          # an open end, in the date-only spelling
])
def test_lateral_movement_timeframe_matches_the_grid_in_any_spelling(timed_client, start, end):
    """The bounds were compared to TS_NORMALIZE(col) verbatim, while the
    core normalises both sides — so any spelling the Timeframe dialog
    accepts other than the canonical one cut the graph to a different row
    set from the grid (US dates: nothing at all) under "timeframe
    applied". The graph's rows must be the grid's rows."""
    client, sid = timed_client
    time = {"start": start, "end": end}
    view = client.post("/api/view", json={"source_id": sid, "time_range": {
        "enabled": True, "column": "When", **time}})
    assert view.status_code == 200, view.text
    r = client.post("/api/plugin/lateral_movement/edges", json={"selections": [
        {"source_id": sid, "src_col": "SourceHost", "dst_col": "DestHost", "time_col": "When"}],
        "time": time})
    assert r.status_code == 200, r.text
    assert sum(e["n"] for e in r.json()["edges"]) == view.json()["row_count"]


def test_lateral_movement_defaults_ship_tied_to_a_header_set(lateral_client):
    """The shipped KAPE defaults are served, each tied to a header set
    (the app's own EvtxECmd nickname) so it only offers itself on a real
    EVTX table — same binding a filter default uses."""
    client, _ = lateral_client
    r = client.get("/api/plugin/lateral_movement/defs")
    assert r.status_code == 200, r.text
    body = r.json()
    shipped = body["shipped"]
    assert any("4624" in d["name"] for d in shipped)
    for d in shipped:
        assert d["src_col"] and d["dst_col"]
        assert d["header_set"] == "Event logs (EvtxECmd)"
    # The header sets are served so the UI can offer and resolve them.
    assert "Event logs (EvtxECmd)" in body["header_sets"]
    evtx = body["header_sets"]["Event logs (EvtxECmd)"]
    assert "EventId" in evtx and "RemoteHost" in evtx


def test_lateral_movement_saved_defs_persist_machine_side(lateral_client):
    client, _ = lateral_client
    mine = {"name": "WinRM", "src_col": "SourceHost", "dst_col": "DestHost",
            "conditions": [{"column": "User", "op": "equals", "value": "svc"}]}
    r = client.post("/api/plugin/lateral_movement/defs", json={"saved": [mine]})
    assert r.status_code == 200, r.text
    assert [d["name"] for d in r.json()["saved"]] == ["WinRM"]
    # The tab replaces its copy with this reply; without the header sets
    # in it, every header-set binding resolved against nothing until the
    # next GET.
    assert "Event logs (EvtxECmd)" in r.json()["header_sets"]
    # A fresh GET (new request, same machine storage) still has it.
    assert client.get("/api/plugin/lateral_movement/defs").json()["saved"][0]["name"] == "WinRM"
    # A malformed definition is refused as a 400, not stored.
    bad = client.post("/api/plugin/lateral_movement/defs", json={"saved": [{"name": "x"}]})
    assert bad.status_code == 400
    # A definition bound to a header set that doesn't exist is refused —
    # it would otherwise never appear on any table, silently.
    ghost = client.post("/api/plugin/lateral_movement/defs", json={"saved": [
        {"name": "Ghost", "src_col": "a", "dst_col": "b", "header_set": "No such set"}]})
    assert ghost.status_code == 400 and "header set" in ghost.json()["detail"].lower()
    # A valid header set is accepted.
    ok = client.post("/api/plugin/lateral_movement/defs", json={"saved": [
        {"name": "Real", "src_col": "a", "dst_col": "b", "header_set": "Event logs (EvtxECmd)"}]})
    assert ok.status_code == 200


# ================================================== claude_assistant plugin
#
# The handler imports `anthropic` lazily, so the tests inject a fake module
# into sys.modules — no network, no real SDK needed. The fake records the
# exact request kwargs so the tests can assert on the API shape (model,
# fallbacks, cache breakpoint) rather than just the happy path.

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
            record["api_key"] = kw.get("api_key")     # None when the SDK resolves it itself
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
    # Prior turns come from the case's own transcript, not the browser.
    claude_client.post("/api/plugin/claude_assistant/clear")
    r0 = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "hi"})
    assert r0.status_code == 200, r0.text
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={
        "question": "Which src_ table has the 4624s?",
        "schema": "CREATE TABLE src_1 (...);",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["answer"] == "SELECT 1;" and out["model"] == "claude-opus-5"
    assert out["usage"]["cache_read_input_tokens"] == 90

    assert record["model"] == "claude-opus-5"
    assert record["betas"] == ["server-side-fallback-2026-07-01"]
    assert record["fallbacks"] == "default"
    assert "thinking" not in record  # omitted on purpose — adaptive by default on this model
    # Schema rides in system with a cache breakpoint on it.
    assert record["system"][1]["text"].endswith("CREATE TABLE src_1 (...);")
    assert record["system"][1]["cache_control"] == {"type": "ephemeral"}
    # The stored turns replay in order; the new question is the last one.
    assert [m["role"] for m in record["messages"]] == ["user", "assistant", "user"]
    assert record["messages"][0]["content"] == "hi"
    assert record["messages"][-1]["content"] == "Which src_ table has the 4624s?"
    # The transcript gets its own breakpoint, on the last turn that came out
    # of the case's table — otherwise every replayed turn is reprocessed at
    # full price behind the cached schema. It must land there and not on the
    # question we appended, which is the one turn unique to this request:
    # an entry written on the tail is bytes no later request sends again.
    # (In the Copilot the tail is further from the stored text still — it
    # carries the editor's SQL folded in. This request is the chat tab, so
    # the two happen to match, which is why the assertion above reads the
    # plain question back.)
    assert record["messages"][-2]["content"] == [
        {"type": "text", "text": "SELECT 1;", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in str(record["messages"][-1]["content"])
    assert "cache_control" not in record  # no top-level automatic caching


def test_claude_copilot_mode_has_its_own_prompt_transcript_and_context(claude_client, monkeypatch):
    """mode "sql" is the Copilot: the narrower system prompt, the editor's
    current query folded into the turn (not into the stored transcript),
    and a transcript of its own so the tab's chat and the Copilot's never
    interleave."""
    record = {}
    reply = "```sql\nSELECT 1;\n```\nOne row."   # the fake answers every call with this
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg(text=reply)))
    claude_client.post("/api/plugin/claude_assistant/clear")
    claude_client.post("/api/plugin/claude_assistant/clear", json={"mode": "sql"})
    # A tab turn first, so we can prove the Copilot does not see it
    assert claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "tab question"}).status_code == 200
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={
        "question": "make this faster", "mode": "sql",
        "schema": "CREATE TABLE src_1 (...);", "current_sql": "SELECT * FROM src_1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["answer"].startswith("```sql")
    assert "copilot" in record["system"][0]["text"].lower()
    assert "fenced" in record["system"][0]["text"]
    assert record["system"][1]["cache_control"] == {"type": "ephemeral"}   # schema breakpoint, as in chat mode
    # Only the Copilot's own turns replay, and the editor text rides in the turn
    assert [m["role"] for m in record["messages"]] == ["user"]
    assert record["messages"][0]["content"].startswith("make this faster")
    assert "SELECT * FROM src_1" in record["messages"][0]["content"]
    # …but the stored question is the analyst's words alone
    sql_turns = claude_client.get("/api/plugin/claude_assistant/history?mode=sql").json()["turns"]
    assert [t["content"] for t in sql_turns] == ["make this faster", reply]
    chat_turns = claude_client.get("/api/plugin/claude_assistant/history").json()["turns"]
    assert [t["content"] for t in chat_turns] == ["tab question", reply]
    # Clearing one conversation leaves the other
    assert claude_client.post("/api/plugin/claude_assistant/clear", json={"mode": "sql"}).json() == {"ok": True}
    assert claude_client.get("/api/plugin/claude_assistant/history?mode=sql").json()["turns"] == []
    assert len(claude_client.get("/api/plugin/claude_assistant/history").json()["turns"]) == 2
    # An unknown mode is a 400, not a third table
    assert claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q", "mode": "notes"}).status_code == 400
    assert claude_client.get("/api/plugin/claude_assistant/history?mode=notes").status_code == 400


def test_claude_registers_the_copilot_page_panel(example_registry):
    (panel,) = [p for p in example_registry.list_page_panels() if p["plugin_fs"] == "claude_assistant"]
    assert panel["page"] == "sql" and panel["id"] == "claude-assistant.copilot" and panel["entry"] == "ui/copilot.js"


def test_claude_refusal_is_a_400_with_category(claude_client, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg(stop_reason="refusal")))
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})
    assert r.status_code == 400 and "cyber" in r.json()["detail"]


def _fake_anthropic_raising(exc: Exception, at: str) -> types.ModuleType:
    """The fake SDK, refusing with `exc` either when the client is built or
    when the request is made."""
    mod = _fake_anthropic({}, _claude_msg())
    real = mod.Anthropic

    def refuse(*a, **kw):
        raise exc

    def client(*a, **kw):
        if at == "construct":
            refuse()
        c = real(*a, **kw)
        c.beta.messages.stream = refuse
        return c

    mod.Anthropic = client
    return mod


@pytest.mark.parametrize("at", ["construct", "request"])
def test_claude_with_no_credentials_at_all_says_where_to_put_a_key(claude_client, monkeypatch, at):
    """No key anywhere is not a 401: the SDK refuses before sending, with a
    TypeError from its header validation. That fell past the handler's
    AuthenticationError mapping and reached the analyst as a 500 — the one
    setup mistake the friendly message was written for."""
    refusal = TypeError('"Could not resolve authentication method. Expected either api_key '
                        'or auth_token to be set. Or for one of the `X-Api-Key` or '
                        '`Authorization` headers to be explicitly omitted"')
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_raising(refusal, at))
    monkeypatch.delenv("WINNOW_ANTHROPIC_API_KEY", raising=False)
    r = claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})
    assert r.status_code == 400 and "WINNOW_ANTHROPIC_API_KEY" in r.json()["detail"]


def test_claude_an_unrelated_type_error_is_not_called_missing_credentials(claude_client, monkeypatch):
    """The mapping above is keyed on what the SDK says, not on TypeError —
    a bug in the plugin must still surface as one."""
    bug = TypeError("stream() got an unexpected keyword argument 'fallbacks'")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_raising(bug, "request"))
    with pytest.raises(TypeError, match="fallbacks"):
        claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})


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
    ({"values": [{"agg": "count"}], "group_sets": [[]], "source_id": -1}, "no table", True),  # merges work now (invariant #9); -1 simply doesn't exist here
    ({"values": [{"agg": "count"}], "group_sets": [[]]}, "source_id", True),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "source_id": 999}, "No table", True),
    ({"values": [{"agg": "count"}], "group_sets": [["Nope"]]}, "No column", False),
    ({"values": [{"agg": "nope", "column": "Host"}], "group_sets": [[]]}, "Unknown aggregation", False),
    ({"values": [{"agg": "sum"}], "group_sets": [[]]}, "needs a column", False),
    ({"values": [{"agg": "count"}], "group_sets": [["Host"]] * 40}, "Too many", False),
    ({"values": [{"agg": "count"}], "group_sets": []}, "group_sets is required", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]],
      "filters": [{"column": "Host", "op": "wat", "values": ["x"]}]}, "Unknown filter operator", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]],
      "filters": [{"column": "Bytes", "op": "gt", "value": "abc"}]}, "needs a number", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "tags": ["wat"]}, "Unknown tag filter", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "tags": [True]}, "Unknown tag filter", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "tags": "1"}, "must be a list", False),
    # A deleted tag: filtered to it, the pivot would say "no rows" with confidence.
    ({"values": [{"agg": "count"}], "group_sets": [[]], "tags": [999]}, "No tag 999", False),
    ({"values": [{"agg": "count"}], "group_sets": [[]], "tags": ["!999"]}, "No tag 999", False),
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


PIVOT_EVERY_AGG = [{"agg": "count"}, {"agg": "count", "column": "Bytes"},
                   {"agg": "count_distinct", "column": "User"}, {"agg": "sum", "column": "Bytes"},
                   {"agg": "avg", "column": "Bytes"}, {"agg": "max", "column": "User"}]


def test_pivot_rolled_up_levels_match_each_level_aggregated_alone(pivot_client):
    """Every level comes out of one base grouped by all of the keys, so each
    subtotal is assembled from the base's partials. That is only right if
    every aggregation assembles to what aggregating the level straight from
    the rows gives — worked out by hand below from PIVOT_ROWS. The one that
    can't be assembled is Distinct count: Security holds alice under both
    hosts, so the per-(Host, Channel) counts add up to 3 where the answer is
    2. An average is its sum over its count, not the average of averages."""
    client, source_id = pivot_client
    out = _agg(client, source_id, values=PIVOT_EVERY_AGG,
               group_sets=[["Host", "Channel"], ["Host"], ["Channel"], []])
    got = [{tuple(r[:len(s["keys"])]): r[len(s["keys"]):] for r in s["rows"]} for s in out["sets"]]
    #        rows, COUNTA Bytes, distinct users, sum, avg, max user
    assert got[0] == {("SRV1", "Security"): [2, 1, 2, 100, 100, "bob"],
                      ("SRV1", "System"): [1, 1, 1, 200, 200, "alice"],
                      ("SRV2", "Security"): [1, 1, 1, None, None, "alice"],   # N/A: a value, not a number
                      ("SRV2", "System"): [1, 1, 1, 40, 40, "carol"]}
    assert got[1] == {("SRV1",): [3, 2, 2, 300, 150, "bob"], ("SRV2",): [2, 2, 2, 40, 40, "carol"]}
    assert got[2] == {("Security",): [3, 2, 2, 100, 100, "bob"], ("System",): [2, 2, 2, 240, 120, "carol"]}
    total = got[3][()]
    assert total[:4] + total[5:] == [5, 4, 3, 340, "carol"]
    assert total[4] == pytest.approx(340 / 3)


def test_pivot_levels_over_no_rows_answer_as_before(pivot_client):
    """Filtered down to nothing, the grouped levels are empty and the grand
    total is still one row: a COUNT(*) of 0, not the NULL a SUM over an
    empty base would give, and a distinct count of 0."""
    client, source_id = pivot_client
    out = _agg(client, source_id, values=PIVOT_EVERY_AGG, group_sets=[["Host"], []],
               filters=[{"column": "Host", "op": "in", "values": ["nope"]}])
    assert out["sets"][0]["rows"] == []
    assert out["sets"][1]["rows"] == [[0, None, 0, None, None, None]]


def test_pivot_reads_the_table_once_however_many_levels(pivot_client, store, monkeypatch):
    """What made a pivot several times slower than the same GROUP BY typed
    into the SQL pane: a full scan of the table per grouping level — four
    for one row field and one column field, six with a nested row. Now one
    statement, whose plan scans the source once and reads every level from
    the materialised base."""
    client, source_id = pivot_client
    ran = []
    real = store.run_sql
    monkeypatch.setattr(store, "run_sql", lambda sql, **kw: ran.append(sql) or real(sql, **kw))
    _agg(client, source_id, values=[{"agg": "sum", "column": "Bytes"}],
         group_sets=[["Host", "Channel"], ["Host"], ["Channel"], []],
         filters=[{"column": "User", "op": "not_in", "values": ["bob"]}])
    assert len(ran) == 1, ran
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("SQLite before 3.35 inlines a CTE into every arm rather than materialising it")
    plan = [r[3] for r in real("EXPLAIN QUERY PLAN " + ran[0])["rows"]]
    source_scans = [d for d in plan if d.startswith("SCAN s")]
    assert len(source_scans) == 1, plan
    assert any("MATERIALIZE pivot_base" in d for d in plan), plan


def test_pivot_truncates_each_level_on_its_own(pivot_client, monkeypatch):
    """One statement answers every level, but the group cap is still per
    level: a level with too many groups says so without crowding out the
    others, and exactly the cap is not truncated."""
    import server

    client, source_id = pivot_client
    mod = server.PLUGINS.get_api("pivot", "aggregate")["handler"].__globals__
    monkeypatch.setitem(mod, "MAX_GROUPS", 2)
    out = _agg(client, source_id, values=[{"agg": "count"}], group_sets=[["User"], ["Host"], []])
    users, hosts, total = out["sets"]
    assert (len(users["rows"]), users["truncated"]) == (2, True)     # alice, bob, carol
    assert (len(hosts["rows"]), hosts["truncated"]) == (2, False)    # SRV1, SRV2: exactly the cap
    assert (total["rows"], total["truncated"]) == ([[5]], False)


# The pivot's tag chips, in the grid's own spelling. PIVOT_ROWS are rids 1-5:
# Suspicious on 1, 2 and 4; Benign on 2 (so 2 carries both); TA on 3; 5 is
# untagged.
@pytest.fixture
def tagged_pivot(pivot_client, store):
    client, source_id = pivot_client
    tags = {t["name"]: t["id"] for t in store.list_tags()}
    store.set_tags(source_id, [1, 2, 4], tags["Suspicious"], True)
    store.set_tags(source_id, [2], tags["Benign"], True)
    store.set_tags(source_id, [3], tags["TA"], True)
    return client, source_id, tags


def _tag_keys(tags, *names):
    """"Suspicious" -> its id, "!Benign" -> "!<its id>", sentinels as-is."""
    out = []
    for n in names:
        if n.startswith("__"):
            out.append(n)
        elif n.startswith("!"):
            out.append(f"!{tags[n[1:]]}")
        else:
            out.append(tags[n])
    return out


def _count(client, source_id, **body):
    return _agg(client, source_id, values=[{"agg": "count"}], group_sets=[[]], **body)["sets"][0]["rows"][0][0]


TAG_FILTER_CASES = [
    (["Suspicious"], 3),
    (["Suspicious", "TA"], 4),               # two chips: rows carrying either
    (["Suspicious", "!Benign"], 2),          # 2 carries both, so it goes
    (["!Benign"], 4),                        # an exclusion alone keeps untagged rows
    (["__any__"], 4),
    (["__none__"], 1),
    (["__none__", "TA"], 2),                 # untagged, plus TA's
    (["__any__", "!Benign"], 3),
]


@pytest.mark.parametrize("names, expected", TAG_FILTER_CASES)
def test_pivot_tag_filter_counts_what_its_chips_say(tagged_pivot, names, expected):
    client, source_id, tags = tagged_pivot
    assert _count(client, source_id, tags=_tag_keys(tags, *names)) == expected


@pytest.mark.parametrize("names, _", TAG_FILTER_CASES)
def test_pivot_tag_filter_keeps_the_rows_the_grid_keeps(tagged_pivot, store, names, _):
    """The chips are the grid ribbon's, so they have to select the grid's
    rows: the same keys through Store.build_view and through the pivot."""
    client, source_id, tags = tagged_pivot
    keys = _tag_keys(tags, *names)
    assert _count(client, source_id, tags=keys) == store.build_view(source_id, {"tags": keys})["row_count"]


def test_pivot_tag_filter_groups_and_combines_with_field_filters(tagged_pivot):
    """Tag clauses go in after the field filters' bound values, and the Sum's
    numeric guard carries `?`s of its own — so this is the shape that would
    shift a parameter if the tag SQL ever grew a placeholder."""
    client, source_id, tags = tagged_pivot
    out = _agg(client, source_id,
               values=[{"agg": "sum", "column": "Bytes"}, {"agg": "count"}],
               group_sets=[["Host"]],
               filters=[{"column": "Channel", "op": "in", "values": ["Security"]}],
               tags=_tag_keys(tags, "Suspicious", "!Benign"))
    assert {r[0]: r[1:] for r in out["sets"][0]["rows"]} == {"SRV1": [100, 1], "SRV2": [None, 1]}


def test_pivot_detail_keeps_the_tag_filter(tagged_pivot):
    """A cell counted over Suspicious-but-not-Benign rows opens on those
    rows, not on every row behind the cell."""
    client, source_id, tags = tagged_pivot
    r = client.post("/api/plugin/pivot/detail", json={
        "source_id": source_id, "cell": [{"column": "Host", "value": "SRV1"}],
        "tags": _tag_keys(tags, "Suspicious", "!Benign"),
    })
    assert r.status_code == 200, r.text
    assert [row[0] for row in r.json()["rows"]] == [1]


def test_pivot_tag_filter_on_a_merge_matches_each_row_through_its_own_member(tagged_pivot, store, write_csv):
    """Invariant #9. A merge's tags live on its members, keyed by the
    member's own rid — and rids collide across members, so matching on rid
    alone would count the second table's rows 1, 2 and 4 as Suspicious
    too."""
    client, source_id, tags = tagged_pivot
    other = store.ingest_csv(write_csv(PIVOT_ROWS, "pivot2.csv"), name="pivot2", build_fts=False)["id"]
    store.set_tags(other, [5], tags["Suspicious"], True)
    mid = store.create_merge("pivtags", [source_id, other])["id"]
    for names, expected in [(["Suspicious"], 4), (["__none__"], 5), (["Suspicious", "!Benign"], 3),
                            (["!Suspicious"], 6)]:
        keys = _tag_keys(tags, *names)
        assert _count(client, mid, tags=keys) == expected, names
        assert expected == store.build_view(mid, {"tags": keys})["row_count"], names
    r = client.post("/api/plugin/pivot/detail", json={"source_id": mid, "cell": [],
                                                      "tags": _tag_keys(tags, "Suspicious")})
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 4


# ------------------------------------------------- bundled dir + scopes


@pytest.fixture
def scoped_client(client, plug_dir, tmp_path, monkeypatch):
    """A registry with an installed plugin ("demo") and a bundled example
    ("shipped"), loaded through server._reload_plugins so the effective-
    state policy under test is the real one, not a re-implementation."""
    import server

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN)
    _write_plugin(bundled, "shipped", GOOD_PLUGIN.replace('"demo"', '"shipped"'))
    reg = PluginRegistry()
    monkeypatch.setattr(server, "PLUGINS", reg)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [plug_dir, bundled])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", bundled)
    server._reload_plugins()
    return client


def _states(client):
    r = client.get("/api/plugins").json()
    return {p["fs_name"]: p for p in r["plugins"]}, r


def test_bundled_examples_are_listed_but_off_by_default(scoped_client):
    """Presence-means-enabled is the right default for something the analyst
    placed by hand, and the wrong one for what we shipped: an example runs
    only once asked for (claude_assistant wants network + a key)."""
    states, r = _states(scoped_client)
    assert states["demo"]["enabled"] and not states["demo"]["bundled"]
    assert states["shipped"]["bundled"] and not states["shipped"]["enabled"]
    assert states["shipped"]["machine_enabled"] is False

    scoped_client.post("/api/plugins/toggle", json={"fs_name": "shipped", "scope": "on_all"})
    states, _ = _states(scoped_client)
    assert states["shipped"]["enabled"] and states["shipped"]["machine_enabled"]


def test_installed_copy_shadows_bundled_same_name(plug_dir, tmp_path, monkeypatch, client):
    """plugins/ comes first and load() is first-directory-wins, so an
    analyst's (possibly edited) install of an example beats the shipped
    copy instead of both loading and fighting over tab ids."""
    import server

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_plugin(plug_dir, "demo", GOOD_PLUGIN.replace('"0.1"', '"9.9"'))
    _write_plugin(bundled, "demo", GOOD_PLUGIN)
    reg = PluginRegistry()
    monkeypatch.setattr(server, "PLUGINS", reg)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [plug_dir, bundled])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", bundled)
    server._reload_plugins()
    states, _ = _states(client)
    assert len(states) == 1
    assert states["demo"]["version"] == "9.9" and not states["demo"]["bundled"]


def test_case_scope_overrides_travel_with_the_case(scoped_client, store):
    """"Off in this case" lives in the case file: it must beat the machine
    default while this case is open, survive a close/reopen of the same
    case, and vanish from the effective state under a different case."""

    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "off_case"})
    assert r.status_code == 200
    states, listing = _states(scoped_client)
    assert listing["case_open"] is True
    assert states["demo"]["case_override"] is False
    assert states["demo"]["enabled"] is False          # override beats machine-on
    assert states["demo"]["machine_enabled"] is True   # ...without touching the default

    # Recorded in the case file itself.
    import json as _json
    assert _json.loads(store.get_case_settings()["plugin_overrides"]) == {"demo": False}

    # The "everywhere" scope clears the override — no silent exemption left.
    scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "on_all"})
    states, _ = _states(scoped_client)
    assert states["demo"]["case_override"] is None and states["demo"]["enabled"]
    assert "plugin_overrides" not in store.get_case_settings()


def test_follow_case_drops_the_override_without_touching_the_default(scoped_client, store):
    """Going back to "this case follows the machine" was, until follow_case,
    only reachable by re-picking an everywhere scope — which clears the
    override as a side effect. That is fine when the machine setting you
    want is the one already set, and a silent rewrite of every OTHER
    case's behaviour when it isn't. Removing one case's override must not
    be able to do that."""

    # Machine default is on; this case says off.
    scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "off_case"})
    states, _ = _states(scoped_client)
    assert states["demo"]["case_override"] is False and states["demo"]["machine_enabled"] is True

    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "follow_case"})
    assert r.status_code == 200
    states, _ = _states(scoped_client)
    assert states["demo"]["case_override"] is None      # the override is gone
    assert states["demo"]["machine_enabled"] is True    # ...and the default is untouched
    assert states["demo"]["enabled"] is True            # so the case follows it again
    # No empty dict left behind in the case file either.
    assert "plugin_overrides" not in store.get_case_settings()


def test_follow_case_leaves_other_plugins_overrides_alone(scoped_client, store):
    import json as _json

    scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "off_case"})
    scoped_client.post("/api/plugins/toggle", json={"fs_name": "shipped", "scope": "on_case"})
    scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "follow_case"})
    assert _json.loads(store.get_case_settings()["plugin_overrides"]) == {"shipped": True}


def test_follow_case_on_a_plugin_with_no_override_is_a_no_op(scoped_client, store):
    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "follow_case"})
    assert r.status_code == 200
    states, _ = _states(scoped_client)
    assert states["demo"]["case_override"] is None
    assert "plugin_overrides" not in store.get_case_settings()


def test_follow_case_requires_an_open_case(scoped_client, monkeypatch):
    import server

    monkeypatch.setattr(server, "STORE", None)
    server._reload_plugins()
    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "follow_case"})
    assert r.status_code == 400


def test_unknown_scope_is_refused(scoped_client):
    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "follow_all"})
    assert r.status_code == 400
    assert "follow_all" in r.json()["detail"]


def test_case_scope_requires_an_open_case(scoped_client, monkeypatch):
    import server

    monkeypatch.setattr(server, "STORE", None)
    server._reload_plugins()
    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "scope": "on_case"})
    assert r.status_code == 400
    assert "case" in r.json()["detail"].lower()
    states, listing = _states(scoped_client)
    assert listing["case_open"] is False


def test_legacy_toggle_body_still_means_everywhere(scoped_client):
    r = scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo", "enabled": False})
    assert r.status_code == 200
    states, _ = _states(scoped_client)
    assert states["demo"]["enabled"] is False and states["demo"]["machine_enabled"] is False
    assert scoped_client.post("/api/plugins/toggle", json={"fs_name": "demo"}).status_code == 400


# A plugin that says so the moment any of its code runs: "off in this case"
# promises the module is never imported at all, not merely unlisted after.
MARKING_PLUGIN = """
    import os, pathlib
    pathlib.Path(os.environ["PLUGIN_MARK_DIR"], "NAME").touch()
    PLUGIN = {"name": "NAME"}

    def register(api):
        api.register_api("ping", lambda req: {"alive": True}, methods=["GET"])
"""


@contextlib.contextmanager
def _serve(tmp_path, case_path, plug_dir):
    """`python server.py --case` for real. What is under test is the state
    main() leaves the registry in, and no in-process client goes through
    main()."""
    with running_server(tmp_path, "--case", case_path, "--no-fts", "--no-idle-shutdown",
                        env={"WINNOW_PLUGINS_DIR": str(plug_dir),
                             "PLUGIN_MARK_DIR": str(tmp_path / "marks")},
                        cwd=tmp_path, timeout=60) as srv:
        def get(route):
            """(status, json body or None)"""
            try:
                with urllib.request.urlopen(srv.base + route, timeout=15) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        yield get


def test_a_case_opened_at_startup_gets_its_own_plugin_set(tmp_path, plug_dir):
    """`--case` (and double-clicking a case file, which is the same
    path) loaded plugins before any case was open and never again: a
    plugin the case switched off ran and served its routes, and one it
    switched on stayed off, until the analyst happened to open some other
    case."""
    from winnow.store import Store

    (tmp_path / "marks").mkdir()
    for name in ("probe", "wanted"):
        _write_plugin(plug_dir, name, MARKING_PLUGIN.replace("NAME", name))
    (tmp_path / "ws").mkdir()
    # Machine default: probe on (installed), wanted off.
    (tmp_path / "ws" / "plugins.json").write_text(json.dumps({"disabled": ["wanted"]}))
    case = str(tmp_path / "case.db-winnow")
    s = Store(case)
    s.set_case_setting("plugin_overrides", json.dumps({"probe": False, "wanted": True}))
    s.close()

    with _serve(tmp_path, case, plug_dir) as get:
        states = {p["fs_name"]: p for p in get("/api/plugins")[1]["plugins"]}
        assert (states["probe"]["case_override"], states["probe"]["enabled"]) == (False, False)
        assert (states["wanted"]["case_override"], states["wanted"]["enabled"]) == (True, True)
        assert get("/api/plugin/probe/ping")[0] == 404
        assert get("/api/plugin/wanted/ping") == (200, {"alive": True})
        # Not imported and then unloaded: never imported.
        assert sorted(os.listdir(tmp_path / "marks")) == ["wanted"]


def test_reopening_the_open_case_reapplies_its_plugin_settings(scoped_client, store):
    """Re-opening the case that is already open skips the store swap, and
    it skipped the plugin reload along with it — so a registry that did not
    match the case stayed that way however often the analyst re-opened it.
    The client resets its plugin tabs on that request and refetches the
    list on the strength of the server having reloaded."""
    import server

    # Written behind the registry's back, the way --case startup used to
    # leave it: the case says off, the loaded set says on.
    store.set_case_setting("plugin_overrides", json.dumps({"demo": False}))
    assert _states(scoped_client)[0]["demo"]["enabled"] is True

    r = scoped_client.post("/api/case/open", json={"path": store.path})
    assert r.status_code == 200
    assert server.STORE is store          # still the fast path, not a reopen
    assert _states(scoped_client)[0]["demo"]["enabled"] is False


def test_discarding_a_quicklook_drops_the_plugins_it_switched_on(scoped_client, tmp_path, monkeypatch):
    """Discard leaves no case open, so the machine defaults apply again —
    but the registry was not reloaded, and a plugin switched on only for
    the quick look stayed imported, routed and listed as on."""
    import server
    from winnow.store import Store

    _write_plugin(server.BUNDLED_PLUGIN_DIR, "looker", """
        PLUGIN = {"name": "looker"}

        def register(api):
            api.register_api("ping", lambda req: {"alive": True}, methods=["GET"])
    """)
    qdir = tmp_path / server.QUICKLOOK_DIRNAME
    qdir.mkdir()
    monkeypatch.setattr(server, "STORE", Store(str(qdir / f"quicklook{server.CASE_SUFFIX}")))
    server._reload_plugins()
    scoped_client.post("/api/plugins/toggle", json={"fs_name": "looker", "scope": "on_case"})
    assert scoped_client.get("/api/plugin/looker/ping").status_code == 200

    assert scoped_client.post("/api/case/discard").status_code == 200
    states, listing = _states(scoped_client)
    assert listing["case_open"] is False
    assert states["looker"]["enabled"] is False and states["looker"]["machine_enabled"] is False
    assert scoped_client.get("/api/plugin/looker/ping").status_code == 404


def test_pivot_aggregates_over_a_merge(pivot_client, store, write_csv):
    """Invariant #9: the pivot reads a merge through a UNION ALL of its
    members — counts must cover every member."""
    client, source_id = pivot_client
    src = store.get_source(source_id)
    header = [c["name"] for c in src["columns"] if not c.get("derived")]
    other = store.ingest_csv(write_csv(
        [header, ["mh", "mu", "7", "2024-02-01 10:00:00"][:len(header)]],
        "pm2.csv"), name="pm2", build_fts=False)["id"]
    mid = store.create_merge("pivmerge", [source_id, other])["id"]
    single = _agg(client, source_id, group_sets=[[]], aggs=[{"fn": "count"}])
    merged = _agg(client, mid, group_sets=[[]], aggs=[{"fn": "count"}])
    total_single = single["sets"][0]["rows"][0][-1]
    total_merged = merged["sets"][0]["rows"][0][-1]
    assert total_merged == total_single + 1  # every member counted


def test_pivot_detail_on_a_merge_is_deterministic(pivot_client, store, write_csv):
    client, source_id = pivot_client
    src = store.get_source(source_id)
    header = [c["name"] for c in src["columns"] if not c.get("derived")]
    other = store.ingest_csv(write_csv([header] + [["dh", "du", "3", "2024-03-01 09:00:00"][:len(header)]],
                                       "pm3.csv"), name="pm3", build_fts=False)["id"]
    mid = store.create_merge("pivdetail", [source_id, other])["id"]
    r = client.post("/api/plugin/pivot/detail", json={"source_id": mid, "cell": [], "filters": []})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["columns"][0] == "Line"
    assert len(out["rows"]) == store.get_source(mid)["row_count"]


def test_pivot_over_a_merge_with_mismatched_columns(pivot_client, store, write_csv):
    """A member without a merge column reads it as blank — so it counts
    toward bare Count but not Count-of-that-field, and groups under the
    blank row label rather than under the column's name."""
    client, source_id = pivot_client
    other = store.ingest_csv(write_csv([["HOST", "User"], ["SRV3", "dave"], ["SRV3", "erin"]],
                                       "pmm.csv"), name="pmm", build_fts=False)["id"]
    mid = store.create_merge("pivmismatch", [source_id, other])["id"]
    out = _agg(client, mid, values=[{"agg": "count"}, {"agg": "count", "column": "Channel"}],
               group_sets=[["Host"], ["Channel"]])
    per_host = {r[0]: r[1:] for r in out["sets"][0]["rows"]}
    assert per_host["SRV3"] == [2, 0]
    per_channel = {r[0]: r[1] for r in out["sets"][1]["rows"]}
    assert "Channel" not in per_channel
    assert per_channel[None] == 2


# ============================================================ first_last plugin

FL_ROWS = [
    ["When", "Host", "User", "EventId"],
    ["2026-03-14 08:00:00", "SRV1", "alice", "4624"],
    ["2026-03-14 09:00:00", "SRV1", "alice", "4688"],
    ["2026-03-14 17:30:00", "SRV1", "alice", "4634"],
    ["2026-03-14 10:00:00", "SRV2", "bob", "4624"],     # single-event group
    ["2026-03-14 11:00:00", "SRV1", "carol", "4625"],
    ["2026-03-14 11:00:00", "SRV1", "carol", "4624"],   # timestamp tie — rid breaks it
]


@pytest.fixture
def fl_client(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(FL_ROWS, "fl.csv"), name="fl", build_fts=False)
    return client, rec["id"]


def _fl(client, route, **body):
    r = client.post(f"/api/plugin/first_last/{route}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_first_last_bookends_each_group_with_rendered_descriptions(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["User", "EventId"],
              template="{which} of {count} | {Host} | user: {User}")
    assert out["columns"] == ["When", "User", "EventId", "Description"]
    assert out["total_groups"] == 3
    descs = [r[-1] for r in out["rows"]]
    # alice: 3 events -> two bookends; the description reads each row's OWN
    # values ({which}/{count} substituted, EventId from that end's row).
    assert "First of 3 | SRV1 | user: alice" in descs
    assert "Last of 3 | SRV1 | user: alice" in descs
    first_alice = next(r for r in out["rows"] if r[-1].startswith("First of 3"))
    last_alice = next(r for r in out["rows"] if r[-1].startswith("Last of 3"))
    assert first_alice[0] == "2026-03-14 08:00:00" and first_alice[2] == "4624"
    assert last_alice[0] == "2026-03-14 17:30:00" and last_alice[2] == "4634"


def test_first_last_single_row_group_emits_one_row(fl_client):
    """A one-event group is its own first and last — one row, labelled
    Only, not a duplicated pair saying nothing twice."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["User"], template="{which} of {count}")
    bob = [r for r in out["rows"] if r[1] == "bob"]
    assert len(bob) == 1 and bob[0][-1] == "Only of 1"


def test_first_last_breaks_timestamp_ties_by_rid(fl_client):
    """carol's two events share a timestamp — rid order (file order) decides
    deterministically, so re-running never swaps first and last."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["User"],
              sort_column="When", columns=["User", "EventId"], template="{which}")
    assert out["columns"] == ["When", "User", "EventId", "Description"]
    carol = {r[-1]: r[2] for r in out["rows"] if r[1] == "carol"}
    assert carol == {"First": "4625", "Last": "4624"}  # file order: the 4625 row came first


def test_first_last_filters_scope_the_grouping(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"],
              sort_column="When", columns=[],
              template="{which} of {count}",
              filters=[{"column": "EventId", "op": "in", "values": ["4624"]}])
    assert out["total_groups"] == 2  # SRV1 (2x 4624), SRV2 (1x)
    assert {r[-1] for r in out["rows"]} == {"First of 2", "Last of 2", "Only of 1"}


def test_first_last_create_lands_a_real_source(fl_client, store):
    client, sid = fl_client
    out = _fl(client, "create", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["User"],
              template="{which} of {count} on {Host}", name="Sessions")
    rec = out["source"]
    assert rec["name"] == "Sessions" and rec["row_count"] == 5  # 2+1+2 bookends
    src = store.get_source(rec["id"])
    assert [c["name"] for c in src["columns"]] == ["When", "User", "Description"]
    # and it's an ordinary source: build a view over it
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    assert view["row_count"] == 5


def test_first_last_template_typo_is_a_400_naming_the_placeholder(fl_client):
    client, sid = fl_client
    r = client.post("/api/plugin/first_last/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When",
        "columns": [], "template": "{which} of {Usre}"})
    assert r.status_code == 400
    assert "{Usre}" in r.json()["detail"]


def test_first_last_template_names_a_column_it_does_not_carry(fl_client, store, write_csv):
    """{Column} resolves for ANY column of the table, not only one that is
    grouped, ordered or carried: the template's columns ride the windowed
    pass for the renderer alone and never become output columns. A typo is
    still refused by name. Same over a merge (invariant #9), where the
    column arrives through the union."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=[], template="{which} of {count} — {EventId}")
    assert out["columns"] == ["When", "Description"]
    descs = {r[-1] for r in out["rows"]}
    assert "First of 3 — 4624" in descs and "Last of 3 — 4634" in descs
    r = client.post("/api/plugin/first_last/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When",
        "columns": [], "template": "{EventId} {Evnt}"})
    assert r.status_code == 400 and "{Evnt}" in r.json()["detail"]

    rows2 = [["When", "Host", "User", "EventId"],
             ["2026-03-15 08:00:00", "SRV9", "erin", "7001"],
             ["2026-03-15 17:00:00", "SRV9", "erin", "7002"]]
    sid2 = store.ingest_csv(write_csv(rows2, "fltc2.csv"), name="fltc2", build_fts=False)["id"]
    mid = store.create_merge("fltcm", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["User"], sort_column="When",
              columns=[], template="{which} {EventId} on {Host}")
    assert out["columns"] == ["When", "Description"]
    descs = {r[-1] for r in out["rows"]}
    assert "First 7001 on SRV9" in descs and "Last 7002 on SRV9" in descs


def test_first_last_values_are_most_common_first(fl_client, store, write_csv):
    """The filter editor's value list: most common first, and the cap
    reported as `truncated` so the tab can say the list is partial. Over a
    merge the counts come through the union (invariant #9)."""
    client, sid = fl_client
    out = _fl(client, "values", source_id=sid, column="Host")
    assert [(v["value"], v["count"]) for v in out["values"]] == [("SRV1", 5), ("SRV2", 1)]
    assert out["truncated"] is False
    out = _fl(client, "values", source_id=sid, column="Host", limit=1)
    assert [v["value"] for v in out["values"]] == ["SRV1"] and out["truncated"] is True
    r = client.post("/api/plugin/first_last/values", json={"source_id": sid, "column": "Nope"})
    assert r.status_code == 400

    rows2 = [["When", "Host", "User", "EventId"],
             ["2026-03-15 08:00:00", "SRV9", "erin", "4624"],
             ["2026-03-15 17:00:00", "SRV9", "erin", "4634"]]
    sid2 = store.ingest_csv(write_csv(rows2, "flv2.csv"), name="flv2", build_fts=False)["id"]
    mid = store.create_merge("flvm", [sid, sid2])["id"]
    out = _fl(client, "values", source_id=mid, column="Host")
    assert [(v["value"], v["count"]) for v in out["values"]] == [("SRV1", 5), ("SRV9", 2), ("SRV2", 1)]
    assert out["truncated"] is False


FL_ALIAS_ROWS = [
    # Headers named like the pass's own window aliases, with values that
    # could never be a group size or a total, so a leak is unmistakable.
    ["When", "Host", "Bytes", "group_n", "Sum of Bytes"],
    ["2026-03-14 08:00:00", "SRV1", "10", "999", "-1"],
    ["2026-03-14 09:00:00", "SRV1", "20", "998", "-1"],
    ["2026-03-14 10:00:00", "SRV2", "5", "997", "-1"],
]

FL_ALIAS_TEMPLATE = "{which} of {count} ({group_n}) {sum:Bytes}|{Sum of Bytes}"


def _alias_preview(client, sid):
    """The preview over FL_ALIAS_ROWS-shaped data: {desc: (Sum column,
    plain {Sum of Bytes} placeholder)} keyed by the part before the bar."""
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], sum_columns=["Bytes"], template=FL_ALIAS_TEMPLATE)
    assert out["columns"] == ["When", "Sum of Bytes", "Description"]
    return {r[-1].split("|")[0]: (r[1], float(r[-1].split("|")[1])) for r in out["rows"]}


def test_first_last_template_cannot_shadow_the_pass_own_aliases(fl_client, store, write_csv):
    """A source column named like one of the pass's own outputs (group_n,
    rn_first/rn_last, a Sum/Min/Max-of alias) is not projected on the
    template's behalf. Projected, the inner SELECT would carry the name
    twice — the file's, then the window's — the outer SELECT * would label
    the window's copy `group_n:1`, and {count} and the Sum column would
    silently read the file's values instead of the group's. The reserved
    names are skipped, so {count} is the group size, the Sum column is the
    total, and the plain {group_n}/{Sum of Bytes} placeholders read the
    pass's own numbers, never the file's. Same over a merge (invariant #9),
    where the columns arrive through the union."""
    client, _ = fl_client
    sid = store.ingest_csv(write_csv(FL_ALIAS_ROWS, "flal.csv"), name="flal", build_fts=False)["id"]
    got = _alias_preview(client, sid)
    assert got == {"First of 2 (2) 30": ("30", 30.0), "Last of 2 (2) 30": ("30", 30.0),
                   "Only of 1 (1) 5": ("5", 5.0)}

    rows2 = [FL_ALIAS_ROWS[0],
             ["2026-03-15 08:00:00", "SRV9", "1", "996", "-1"],
             ["2026-03-15 17:00:00", "SRV9", "2", "995", "-1"]]
    sid2 = store.ingest_csv(write_csv(rows2, "flal2.csv"), name="flal2", build_fts=False)["id"]
    mid = store.create_merge("flalm", [sid, sid2])["id"]
    got = _alias_preview(client, mid)
    assert got == {"First of 2 (2) 30": ("30", 30.0), "Last of 2 (2) 30": ("30", 30.0),
                   "Only of 1 (1) 5": ("5", 5.0),
                   "First of 2 (2) 3": ("3", 3.0), "Last of 2 (2) 3": ("3", 3.0)}


@pytest.mark.parametrize("body, fragment", [
    ({"group_by": [], "sort_column": "When"}, "at least one column"),
    ({"group_by": ["Host"]}, "orders each group"),
    ({"group_by": ["Nope"], "sort_column": "When"}, "No column"),
    ({"group_by": ["Host"] * 9, "sort_column": "When"}, "Too many"),
])
def test_first_last_validation(fl_client, body, fragment):
    client, sid = fl_client
    r = client.post("/api/plugin/first_last/preview", json={"source_id": sid, **body})
    assert r.status_code == 400, r.text
    assert fragment.lower() in r.json()["detail"].lower()


def test_first_last_tag_filter_scopes_the_grouping(fl_client, store):
    """Tags aren't a column — they live in the row_tags sidecar — so the
    plugin filters on membership: specific tags, any tag, or untagged."""
    client, sid = fl_client
    tag = store.list_tags()[0]
    rids = [r[0] for r in store.run_sql(
        f'SELECT rid FROM "src_{sid}" WHERE "User" = \'alice\'')["rows"]]
    store.set_tags(sid, rids, tag["id"], True)

    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which} of {count}",
              tags={"mode": "ids", "ids": [tag["id"]]})
    assert out["total_groups"] == 1
    assert {r[-1] for r in out["rows"]} == {"First of 3", "Last of 3"}

    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which}", tags={"mode": "any"})
    assert out["total_groups"] == 1
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which}", tags={"mode": "none"})
    assert out["total_groups"] == 2  # bob and carol never got the tag

    # ...and it ANDs with the column filters rather than replacing them.
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which} of {count}",
              tags={"mode": "ids", "ids": [tag["id"]]},
              filters=[{"column": "EventId", "op": "in", "values": ["4624"]}])
    assert out["total_groups"] == 1
    assert [r[-1] for r in out["rows"]] == ["Only of 1"]


def test_first_last_rejects_a_bad_tag_filter(fl_client):
    client, sid = fl_client
    for tags in ({"mode": "wat"}, {"mode": "ids", "ids": ["TA"]}):
        r = client.post("/api/plugin/first_last/preview", json=dict(
            source_id=sid, group_by=["User"], sort_column="When",
            columns=[], template="{which}", tags=tags))
        assert r.status_code == 400, r.text


def test_first_last_carry_order_is_the_output_order(fl_client):
    """The order of `columns` is the created table's column order — it's
    what the tab's drag-to-reorder chips are editing, so it has to hold."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"],
              sort_column="When", columns=["EventId", "User"], template="{which}")
    assert out["columns"] == ["When", "EventId", "User", "Description"]
    out = _fl(client, "preview", source_id=sid, group_by=["Host"],
              sort_column="When", columns=["User", "EventId"], template="{which}")
    assert out["columns"] == ["When", "User", "EventId", "Description"]


def test_pivot_groups_on_a_derived_column(pivot_client, store):
    """Item from analyst feedback: custom columns (timestamp/regex derived)
    must be usable in the pivot — the plugin's _from_clause joins the
    drv_ sidecar, and /api/sources feeds its UI the derived names."""
    client, source_id = pivot_client
    res = store.add_derived_column(source_id, "HostRx", "Host", "regex_extract",
                                    {"pattern": r"^(\w+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    out = _agg(client, source_id, group_sets=[["HostRx"]], aggs=[{"fn": "count"}])
    assert out["sets"][0]["rows"], "no groups came back for the derived column"


def test_first_last_carries_a_derived_column(fl_client, store):
    client, sid = fl_client
    res = store.add_derived_column(sid, "UserRx", "User", "regex_extract",
                                    {"pattern": r"^(\w)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    out = _fl(client, "preview", source_id=sid, group_by=["Host"],
              sort_column="When", columns=["UserRx"], template="{which} | {UserRx}")
    assert out["columns"] == ["When", "UserRx", "Description"]
    firsts = [r for r in out["rows"] if r[-1].startswith("First")]
    assert firsts and all(r[1] for r in firsts), "derived values missing from the carried column"


def test_first_last_row_json_cell_carries_the_entire_row(fl_client):
    import json as _json

    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["User"], template="{which}", row_json=True)
    # The description reads before the JSON: it is the line, the JSON is
    # the long cell opened when needed.
    assert out["columns"] == ["When", "User", "Description", "Row (JSON)"]
    first = next(r for r in out["rows"] if r[2] == "First" and r[1] == "alice")
    row = _json.loads(r_json := first[3])
    assert row == {"When": "2026-03-14 08:00:00", "Host": "SRV1",
                   "User": "alice", "EventId": "4624"}
    # the synthetic window columns never leak into the JSON
    assert "rn_first" not in r_json and "group_n" not in r_json


def test_first_last_row_json_lands_in_the_created_table(fl_client, store):
    client, sid = fl_client
    out = _fl(client, "create", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=[], template="{which}", row_json=True,
              name="bookends-json")
    src = store.get_source(out["source"]["id"])
    assert "Row (JSON)" in [c["name"] for c in src["columns"]]


def test_first_last_orders_a_number_column_by_value(fl_client, store):
    """Cells are TEXT, so ordering a number column by its text put "10"
    before "9": a group of record ids came back First=10, Last=9, while
    the grid sorted the same column numerically. A number column orders by
    value, and a cell that isn't a number (blank, "n/a") wins neither end.
    The result's own row order follows the same rule. A merge orders the
    same way (invariant #9)."""
    client, _ = fl_client
    rows = [["WS1", "9"], ["WS1", "10"], ["WS1", "100"], ["WS1", "55"], ["WS1", ""], ["WS1", "n/a"],
            ["WS2", "n/a"], ["WS2", "-3"], ["WS2", "20"],
            ["WS3", ""], ["WS3", "x"]]                  # nothing numeric: file order decides
    sid = store.ingest_rows(["Host", "RecordId"], rows[:6], name="evts",
                            column_types=["text", "number"], build_fts=False)["id"]
    sid2 = store.ingest_rows(["Host", "RecordId"], rows[6:], name="evts2",
                             column_types=["text", "number"], build_fts=False)["id"]
    mid = store.create_merge("evtsm", [sid, sid2])["id"]

    def bookends(source_id):
        out = _fl(client, "preview", source_id=source_id, group_by=["Host"],
                  sort_column="RecordId", columns=["Host"], template="{which}")
        return [(r[1], r[2], r[0]) for r in out["rows"]]

    assert bookends(sid) == [("WS1", "First", "9"), ("WS1", "Last", "100")]
    assert bookends(mid) == [("WS2", "First", "-3"), ("WS1", "First", "9"), ("WS2", "Last", "20"),
                             ("WS1", "Last", "100"), ("WS3", "First", ""), ("WS3", "Last", "x")]
    # A text column is still ordered as text.
    store.ingest_rows(["Host", "Label"], [["A", "10"], ["A", "9"]], name="txt",
                      column_types=["text", "text"], build_fts=False)
    tid = next(x["id"] for x in store.list_sources() if x["name"] == "txt")
    out = _fl(client, "preview", source_id=tid, group_by=["Host"], sort_column="Label",
              columns=[], template="{which}")
    assert [(r[0], r[-1]) for r in out["rows"]] == [("10", "First"), ("9", "Last")]


def test_first_last_a_value_sorts_before_the_longer_values_it_begins(fl_client, store):
    """The bookends are MIN and MAX of a text key that spells the value in
    hex with file order on the end. The separator between the two sorts
    below every hex digit, which is what keeps "10:00" before "10:00:05"
    as ORDER BY has it; one that sorted above would swap them."""
    client, _ = fl_client
    sid = store.ingest_rows(["Host", "When"], [["A", "2026-03-14 10:00:05"], ["A", "2026-03-14 10:00"],
                                               ["A", "2026-03-14 10:00:0"]],
                            name="prefixes", column_types=["text", "text"], build_fts=False)["id"]
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], template="{which}")
    assert [(r[0], r[-1]) for r in out["rows"]] == [("2026-03-14 10:00", "First"),
                                                    ("2026-03-14 10:00:05", "Last")]


def test_first_last_a_cut_result_keeps_the_bookends_first_in_order(fl_client, store, monkeypatch):
    """The bookends are put in order and cut to the limit before any of
    their rows is read, so the cut has to keep the ones first in order: by
    value, then a group's first row before another group's last, then file
    order. Y's first row shares its time with A's and B's last rows and
    comes after both in the file, and still makes the cut ahead of them."""
    import server

    client, _ = fl_client
    mod = server.PLUGINS.get_api("first_last", "rows")["handler"].__globals__
    monkeypatch.setitem(mod, "MAX_COPY_ROWS", 3)
    sid = store.ingest_rows(["G", "T"], [["A", "01"], ["A", "05"], ["B", "02"], ["B", "05"],
                                         ["Y", "05"], ["Y", "09"]],
                            name="cut", column_types=["text", "text"], build_fts=False)["id"]
    out = _fl(client, "rows", source_id=sid, group_by=["G"], sort_column="T",
              columns=["G"], template="{which}")
    assert [(r[1], r[-1]) for r in out["rows"]] == [("A", "First"), ("B", "First"), ("Y", "First")]
    assert out["truncated"] is True


@pytest.mark.parametrize("sort_column", ["When", "EventId"])
def test_first_last_reads_the_table_once(fl_client, store, monkeypatch, sort_column):
    """What made First/Last several times slower than the same GROUP BY
    typed into the SQL pane: window functions, each a sort of the whole
    table carrying every column the result names, and a second statement
    to count the groups. Now one statement, whose plan scans the source
    once and reads each bookend it lists by rid. A number column takes two
    passes over the rows it materialised, still one scan of the table."""
    client, sid = fl_client
    store.ingest_rows(["Host", "EventId"], [["SRV1", "10"], ["SRV1", "9"]], name="nums",
                      column_types=["text", "number"], build_fts=False)
    if sort_column == "EventId":
        sid = next(x["id"] for x in store.list_sources() if x["name"] == "nums")
    ran = []
    real = store.run_sql
    monkeypatch.setattr(store, "run_sql", lambda sql, **kw: ran.append(sql) or real(sql, **kw))
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column=sort_column,
              columns=["Host"], template="{which} of {count}")
    assert out["total_groups"] >= 1
    assert len(ran) == 1, ran
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("SQLite before 3.35 inlines a CTE into each place it is read")
    plan = [r[3] for r in real("EXPLAIN QUERY PLAN " + ran[0])["rows"]]
    assert len([d for d in plan if d.startswith("SCAN s")]) == 1, plan
    assert any(d.startswith("SEARCH s USING INTEGER PRIMARY KEY") for d in plan), plan


def test_first_last_works_on_a_merge(fl_client, store, write_csv):
    """Invariant #9: bookends over a merged table — one windowed pass over
    a UNION ALL of the members, ties broken by (source_id, rid)."""
    client, sid = fl_client
    rows2 = [["When", "Host", "User", "EventId"],
             ["2026-03-15 08:00:00", "SRV9", "erin", "4624"],
             ["2026-03-15 17:00:00", "SRV9", "erin", "4634"]]
    sid2 = store.ingest_csv(write_csv(rows2, "fl2.csv"), name="fl2", build_fts=False)["id"]
    mid = store.create_merge("flm", [sid, sid2])["id"]

    out = _fl(client, "preview", source_id=mid, group_by=["Host", "User"],
              sort_column="When", columns=["User"], template="{which} of {count}")
    assert out["total_groups"] == 4  # alice, bob, carol (+SRV1 variants) from fl, erin from fl2
    erin = [r for r in out["rows"] if r[1] == "erin"]
    assert {r[-1] for r in erin} == {"First of 2", "Last of 2"}

    # filters and the tag filter run against the union
    tag = store.list_tags()[0]
    rids = [r[0] for r in store.run_sql(
        f'SELECT rid FROM "src_{sid2}"')["rows"]]
    store.set_tags(sid2, rids, tag["id"], True)
    out = _fl(client, "preview", source_id=mid, group_by=["User"], sort_column="When",
              columns=[], template="{which} of {count}",
              tags={"mode": "ids", "ids": [tag["id"]]})
    assert out["total_groups"] == 1  # only erin's rows carry the tag

    # ...and create lands a real table from the merge
    made = _fl(client, "create", source_id=mid, group_by=["User"], sort_column="When",
               columns=["Host"], template="{which}", name="merge-bookends")
    assert store.get_source(made["source"]["id"])["row_count"] > 0


def test_first_last_merge_tie_break_is_deterministic(fl_client, store, write_csv):
    """Identical timestamps across members: (source_id, rid) decides, so
    reruns never swap first and last."""
    client, sid = fl_client
    rows2 = [["When", "Host", "User", "EventId"],
             ["2026-03-14 11:00:00", "SRV1", "carol", "9999"]]  # ties carol's pair in member 1
    sid2 = store.ingest_csv(write_csv(rows2, "tie2.csv"), name="tie2", build_fts=False)["id"]
    mid = store.create_merge("tiem", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["User"],
              sort_column="When", columns=["User", "EventId"], template="{which}")
    carol = [r for r in out["rows"] if r[1] == "carol"]
    first = next(r for r in carol if r[-1] == "First")
    last = next(r for r in carol if r[-1] == "Last")
    # all three carol rows tie at 11:00 — member 1 (lower source_id) wins
    # the front (its first tied row is 4625), member 2 takes the back.
    assert first[2] == "4625"
    assert last[2] == "9999"


def test_first_last_merge_row_json_carries_the_exposed_columns(fl_client, store, write_csv):
    import json as _json

    client, sid = fl_client
    sid2 = store.ingest_csv(write_csv(
        [["When", "Host", "User", "EventId"], ["2026-03-16 08:00:00", "X", "zed", "1"]],
        "fl3.csv"), name="fl3", build_fts=False)["id"]
    mid = store.create_merge("flj", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["User"], sort_column="When",
              columns=[], template="{which}", row_json=True)
    zed = next(r for r in out["rows"] if '"zed"' in r[-1])
    assert set(_json.loads(zed[-1]).keys()) == {"When", "Host", "User", "EventId"}


def test_first_last_over_a_merge_with_mismatched_columns(fl_client, store, write_csv):
    """A member missing a carried column contributes its rows with that
    column blank; one spelling the header differently still lines up."""
    client, sid = fl_client
    sid2 = store.ingest_csv(write_csv(
        [["WHEN", "Host", "User"], ["2026-03-16 08:00:00", "SRV7", "fay"],
         ["2026-03-16 09:00:00", "SRV7", "fay"]], "flmm.csv"), name="flmm", build_fts=False)["id"]
    mid = store.create_merge("flmm", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["User"], sort_column="When",
              columns=["User", "EventId"], template="{which} {EventId}")
    fay = [r for r in out["rows"] if r[1] == "fay"]
    assert [r[0] for r in fay] == ["2026-03-16 08:00:00", "2026-03-16 09:00:00"]
    assert {r[2] for r in fay} == {""}   # the plugin renders a blank as ""
    assert "EventId" not in {r[2] for r in out["rows"]}


# ============================================= shipped KAPE profile

def test_kape_profile_ships_and_applies(client, store, write_csv, example_registry, monkeypatch):
    """The KAPE-triage default profile is listed (read-only), and applying
    it sets the dashboard, seeds the watchlist, and its SQL widgets resolve
    the {{evtx}} placeholder against a real EvtxECmd table."""
    import server
    from winnow import defaults
    monkeypatch.setattr(server, "PLUGINS", example_registry)

    evtx_cols = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]
    def row(**kw):
        d = {c: "" for c in evtx_cols}; d.update(kw); return [d[c] for c in evtx_cols]
    rows = [evtx_cols,
            row(EventId="4624", Channel="Security", RemoteHost="WKS07", UserName="alice"),
            row(EventId="4624", Channel="Security", RemoteHost="WKS01", UserName="bob"),
            row(EventId="4625", Channel="Security", RemoteHost="WKS07", UserName="admin")]
    store.ingest_csv(write_csv(rows, "evtx.csv"), name="evtx", build_fts=False)

    listed = {b["name"]: b for b in client.get("/api/plugin_bundles").json()}
    assert "KAPE triage" in listed
    kape = listed["KAPE triage"]
    assert kape["id"] < 0 and kape["shipped"] is True and kape["dashboard"]

    ap = client.post(f"/api/plugin_bundles/{kape['id']}/apply")
    assert ap.status_code == 200
    body = ap.json()
    assert body["dashboard_applied"] is True and body["watchlist_seeded"] == 5

    boards = {b["name"]: b for b in client.get("/api/dashboards").json()}
    assert "KAPE triage" in boards
    widgets = client.get(f"/api/dashboards/{boards['KAPE triage']['id']}").json()["widgets"]
    assert len(widgets) == len(next(p for p in defaults.profiles() if p["name"] == "KAPE triage")["dashboard"])
    # The {{evtx}} placeholder resolves for a whole card of cells in ONE
    # request — the numbers that used to be ten separate widgets.
    signals = next(w for w in widgets if w["title"] == "Triage signals")
    sv = client.post("/api/dashboard/widget/preview",
                     json={"source": "cells", "query": {}, "cells": signals["cells"]})
    assert sv.status_code == 200
    by = dict(sv.json()["rows"])
    assert by["Remote logon peers"] == 2          # WKS07, WKS01
    assert by["Distinct accounts"] >= 1

    # And the registry cell riding in that same card resolves its
    # {{registry}} placeholder to a friendly message in its own place —
    # this case has EVTX only, no RECmd batch — while the nine evtx cells
    # beside it still answer. That is the whole point of shipping a board
    # a case may only half fill.
    errs = dict(zip([r[0] for r in sv.json()["rows"]], sv.json()["cell_errors"]))
    assert by["Run / service entries"] is None
    assert "table" in errs["Run / service entries"].lower()
    assert errs["Remote logon peers"] is None

    # a placeholder with no matching table gives a friendly 400, not a SQL
    # error — and names the table separately from the sentence, which is
    # how the board tells "not collected" from "matched nothing"
    miss = client.post("/api/dashboard/widget/preview",
                       json={"source": "sql", "query": {"sql": "SELECT * FROM {{mft}}"}})
    assert miss.status_code == 400
    detail = miss.json()["detail"]
    assert "table" in detail["message"].lower()
    assert detail["missing_table"] == "NTFS $MFT (MFTECmd)"


def test_claude_transcript_lives_in_the_case_and_renders_without_the_service(claude_client, monkeypatch, store):
    """The point of the example: the tab can be reloaded, or the service can
    be unreachable, and the conversation is still there."""
    record = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg()))
    claude_client.post("/api/plugin/claude_assistant/clear")
    claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "what is 4624?"})

    # No SDK at all now — the tab still renders the history.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    got = claude_client.get("/api/plugin/claude_assistant/history").json()
    assert got["persisted"] is True
    assert [t["role"] for t in got["turns"]] == ["user", "assistant"]
    assert got["turns"][0]["content"] == "what is 4624?"
    assert got["turns"][1]["content"] == "SELECT 1;"
    assert got["turns"][0]["at"]

    # It is in the case file, in this plugin's own table — never a source.
    assert store.plugin_tables("claude_assistant") == ["history"]
    assert store.list_sources() == [] or all(
        not s["name"].startswith("plugin") for s in store.list_sources())

    # Clearing forgets this case's chat.
    assert claude_client.post("/api/plugin/claude_assistant/clear").json() == {"ok": True}
    assert claude_client.get("/api/plugin/claude_assistant/history").json()["turns"] == []


def test_claude_a_failed_question_does_not_become_context(claude_client, monkeypatch):
    """Both turns are written only after the call succeeds, so a refusal or
    a network error never poisons the next question."""
    record = {}
    claude_client.post("/api/plugin/claude_assistant/clear")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg(stop_reason="refusal")))
    assert claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "bad"}).status_code == 400
    assert claude_client.get("/api/plugin/claude_assistant/history").json()["turns"] == []


def test_claude_prefers_the_winnow_named_key(claude_client, monkeypatch):
    """The guide tells authors to name a WINNOW_* variable; the bundled
    example has to follow its own advice."""
    record = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(record, _claude_msg()))
    monkeypatch.setenv("WINNOW_ANTHROPIC_API_KEY", "sk-from-settings")
    claude_client.post("/api/plugin/claude_assistant/ask", json={"question": "q"})
    assert record["api_key"] == "sk-from-settings"


def test_plugin_upload_spool_is_removed_and_named_like_the_upload(plugin_client, store, registry,
                                                                  tmp_path, monkeypatch):
    """The upload route spooled to a mkstemp file and never deleted it:
    every uploaded $MFT stayed in the temp folder, a full copy each time,
    and the table's recorded path pointed at it. And a parser that reads
    meaning from the file NAME (esxi_logs' log type, $MFT/$J matching)
    was handed tmpk3j2_x.log. The spool now carries the upload's own name
    inside a private directory that is gone when the route returns —
    failed or not — and the table records the name the browser sent."""
    import tempfile
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    seen = []

    def parse(path, options):
        seen.append(os.path.basename(path))
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return {"columns": ["Line"], "rows": [[ln] for ln in lines]}

    fmt = plugin_api.IngestFormat(plugin="demo", local_id="named", label="Named", parse=parse)
    registry._add_format(fmt)
    r = plugin_client.post("/api/ingest/plugin/upload", files={"file": ("hostd.log", b"a\nb\n")},
                           data={"format_id": "demo.named", "build_fts": "false"})
    assert r.status_code == 200, r.text
    assert seen == ["hostd.log"]
    assert os.listdir(systmp) == []
    assert store.get_source(r.json()["id"])["path"] == "hostd.log"
    assert store.get_source(r.json()["id"])["file_hash"]     # still hashed from the bytes
    # A parse that fails leaves nothing behind either.
    r = plugin_client.post("/api/ingest/plugin/upload", files={"file": ("x.boom", b"data")},
                           data={"format_id": "demo.boom"})
    assert r.status_code == 400
    assert os.listdir(systmp) == []

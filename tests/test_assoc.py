"""OS file-association registration (winnow/assoc.py + the /api/assoc/*
registration routes). The adapters take their environment as arguments —
XDG paths, a registry object — so both platforms' key/file layouts are
pinned here on any OS; only the two-line platform defaults go untested.

The policy tests matter most: which types may become the DEFAULT app is
the decision from the feature review (handler for everything, default
only where hijacking double-click is harmless), and it must hold at the
API even if a UI stops enforcing it."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from winnow import assoc

HEADERS = {"X-Timeline-Lite-Client": "1"}


# ------------------------------------------------------------- catalogue


def test_the_default_policy_is_in_the_catalogue():
    types = {t["ext"]: t for t in assoc.supported_types()}
    # Types with real owners stay handler-only, forever.
    for ext in (".txt", ".json", ".db", ".xlsx", ".xlsm", ".sqlite", ".sqlite3"):
        assert types[ext]["default_ok"] is False, ext
    for ext in (".csv", ".tsv"):
        assert types[ext]["default_ok"] is True, ext


def test_plugin_extensions_join_the_catalogue_handler_only():
    fmts = [{"plugin": "mft_usn", "label": "NTFS $MFT", "extensions": [".mft", ".MFT"]},
            {"plugin": "weird", "label": "No ext", "extensions": [], "filename_patterns": ["$J"]},
            {"plugin": "greedy", "label": "CSV again", "extensions": [".csv"]}]
    types = {t["ext"]: t for t in assoc.supported_types(fmts)}
    assert types[".mft"]["source"] == "mft_usn"
    assert types[".mft"]["default_ok"] is False       # unvetted, never default
    assert types[".csv"]["source"] == "builtin"        # builtin wins a clash
    # Pattern-only formats ($MFT, $J) contribute nothing — extensionless
    # files can't be associated on either OS.
    assert all(t["ext"].startswith(".") for t in types.values())


def test_launch_command_points_at_this_install():
    cmd = assoc.launch_command()
    assert cmd[1].endswith("server.py")
    assert cmd[-1] == "--assoc"


# ----------------------------------------------------------------- linux


@pytest.fixture
def lin(tmp_path):
    return assoc.LinuxAssoc(data_home=str(tmp_path / "share"),
                            config_home=str(tmp_path / "config"))


def _cat(*exts):
    types = assoc.supported_types(
        [{"plugin": "mft_usn", "label": "NTFS $MFT", "extensions": [".mft"]}])
    by = {t["ext"]: t for t in types}
    return [by[e] for e in exts], types


def test_linux_register_writes_desktop_and_added_association(lin, tmp_path):
    picked, cat = _cat(".csv")
    lin.register(picked, cat)
    desktop = (tmp_path / "share/applications/winnow.desktop").read_text()
    assert "text/csv" in desktop
    assert "--assoc" in desktop and "%F" in desktop
    assert "NoDisplay=true" in desktop
    apps = (tmp_path / "config/mimeapps.list").read_text()
    assert "text/csv=winnow.desktop;" in apps
    st = lin.status(cat)
    assert st[".csv"] == {"registered": True, "default": False}
    assert st[".tsv"]["registered"] is False


def test_linux_default_prepends_and_status_reports_it(lin):
    picked, cat = _cat(".csv")
    lin.make_default(picked, cat)
    st = lin.status(cat)
    assert st[".csv"] == {"registered": True, "default": True}


def test_linux_touches_nothing_it_does_not_own(lin, tmp_path):
    """A foreign mimeapps.list survives byte-meaningfully: other apps'
    entries stay, and unregistering only removes winnow's token."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "mimeapps.list").write_text(
        "[Default Applications]\ntext/csv=libreoffice-calc.desktop;\n"
        "[Added Associations]\ntext/csv=libreoffice-calc.desktop;code.desktop;\n")
    picked, cat = _cat(".csv")
    lin.register(picked, cat)
    apps = (cfg / "mimeapps.list").read_text()
    assert "libreoffice-calc.desktop" in apps
    assert "code.desktop" in apps
    assert "winnow.desktop" in apps
    # And Calc keeps the double-click: register is handler-ship only.
    assert lin.status(cat)[".csv"]["default"] is False
    lin.unregister(picked, cat)
    apps = (cfg / "mimeapps.list").read_text()
    assert "winnow.desktop" not in apps
    assert "libreoffice-calc.desktop" in apps


def test_linux_reregister_clears_a_past_manual_removal(lin, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "mimeapps.list").write_text(
        "[Removed Associations]\ntext/csv=winnow.desktop;\n")
    picked, cat = _cat(".csv")
    lin.register(picked, cat)
    apps = (cfg / "mimeapps.list").read_text()
    assert "winnow.desktop" in apps.split("[Added Associations]")[1]
    removed = apps.split("[Removed Associations]")[1].split("[")[0]
    assert "winnow.desktop" not in removed


def test_linux_plugin_type_gets_a_mime_package(lin, tmp_path, monkeypatch):
    monkeypatch.setattr(assoc.shutil, "which", lambda n: None)  # no db refreshers
    picked, cat = _cat(".mft")
    lin.register(picked, cat)
    pkg = (tmp_path / "share/mime/packages/winnow.xml").read_text()
    assert '<glob pattern="*.mft"/>' in pkg
    assert "application/x-winnow-mft" in pkg
    lin.unregister(picked, cat)
    assert not (tmp_path / "share/mime/packages/winnow.xml").exists()


def test_linux_unregister_last_type_removes_the_desktop_file(lin, tmp_path):
    picked, cat = _cat(".csv")
    lin.register(picked, cat)
    lin.unregister(picked, cat)
    assert not (tmp_path / "share/applications/winnow.desktop").exists()


def test_desktop_exec_quoting_handles_spaces():
    assert assoc._desktop_quote("/opt/win now/python") == '"/opt/win now/python"'
    assert assoc._desktop_quote("plain") == "plain"


# --------------------------------------------------------------- windows


class FakeReg:
    """Just enough of winreg's shape for WindowsAssoc: a dict of
    key-path → {value_name: value}, with the constants and the handle
    dance collapsed away."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = KEY_SET_VALUE = 0
    REG_SZ = 1

    def __init__(self):
        self.keys: dict[str, dict] = {}

    class _H:
        def __init__(self, path): self.path = path

    def CreateKeyEx(self, root, path, _res, _access):
        # Real CreateKeyEx creates every intermediate key too — without
        # this, _delete_tree spins forever on a parent it can list but
        # never open.
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]), {})
        return self._H(path)

    def OpenKey(self, root, path, _res, _access):
        if path not in self.keys:
            raise OSError(2, "no such key")
        return self._H(path)

    def CloseKey(self, h): pass

    def SetValueEx(self, h, name, _res, _type, value):
        self.keys[h.path][name] = value

    def QueryValueEx(self, h, name):
        if name not in self.keys[h.path]:
            raise OSError(2, "no such value")
        return (self.keys[h.path][name], self.REG_SZ)

    def DeleteValue(self, h, name):
        if name not in self.keys[h.path]:
            raise OSError(2, "no such value")
        del self.keys[h.path][name]

    def DeleteKey(self, root, path):
        if path not in self.keys or any(k.startswith(path + "\\") for k in self.keys):
            raise OSError(2, "no such key or not empty")
        del self.keys[path]

    def EnumKey(self, h, i):
        subs = sorted({k[len(h.path) + 1:].split("\\")[0]
                       for k in self.keys if k.startswith(h.path + "\\")})
        if i >= len(subs):
            raise OSError(22, "no more data")
        return subs[i]


@pytest.fixture
def win():
    return assoc.WindowsAssoc(reg=FakeReg())


def test_windows_register_writes_progid_and_openwith(win):
    picked, cat = _cat(".csv")
    win.register(picked, cat)
    keys = win.reg.keys
    cmd = keys["Software\\Classes\\Winnow.File\\shell\\open\\command"][None]
    assert "--assoc" in cmd and cmd.endswith('"%1"')
    assert "Winnow.File" in keys["Software\\Classes\\.csv\\OpenWithProgids"]
    st = win.status(cat)
    assert st[".csv"] == {"registered": True, "default": False, "windows_userchoice": False}


def test_windows_default_reports_a_userchoice_block(win):
    win.reg.keys["Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"
                 "\\FileExts\\.csv\\UserChoice"] = {"ProgId": "Excel.CSV"}
    picked, cat = _cat(".csv")
    out = win.make_default(picked, cat)
    # The classic association IS written (it's what pre-8 semantics and
    # some shells still read)…
    assert win.reg.keys["Software\\Classes\\.csv"][None] == "Winnow.File"
    # …but the caller is told Explorer will overrule it.
    assert out["userchoice"] == [".csv"]
    assert win.status(cat)[".csv"]["windows_userchoice"] is True


def test_windows_unregister_cleans_up_fully(win):
    picked, cat = _cat(".csv")
    win.make_default(picked, cat)
    win.unregister(picked, cat)
    keys = win.reg.keys
    assert "Winnow.File" not in keys.get("Software\\Classes\\.csv\\OpenWithProgids", {})
    assert keys.get("Software\\Classes\\.csv", {}).get(None) != "Winnow.File"
    # Last type gone → the ProgId tree goes too, no orphan registry litter.
    assert not any(k.startswith("Software\\Classes\\Winnow.File") for k in keys)


def test_windows_unregister_keeps_progid_while_other_types_remain(win):
    both, cat = _cat(".csv", ".tsv")
    win.register(both, cat)
    win.unregister([both[0]], cat)
    assert any(k.startswith("Software\\Classes\\Winnow.File") for k in win.reg.keys)
    st = win.status(cat)
    assert st[".csv"]["registered"] is False and st[".tsv"]["registered"] is True


# ---------------------------------------------------------------- routes


@pytest.fixture
def client(store, monkeypatch, tmp_path):
    import server
    fake = assoc.LinuxAssoc(data_home=str(tmp_path / "share"),
                            config_home=str(tmp_path / "config"))
    monkeypatch.setattr(server.file_assoc, "adapter", lambda background=False: fake)
    monkeypatch.setattr(server.file_assoc, "platform_name", lambda: "linux")
    return server, TestClient(server.app)


def test_types_route_reports_status_and_asked(client):
    server, c = client
    r = c.get("/api/assoc/types", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "linux"
    csv = next(t for t in body["types"] if t["ext"] == ".csv")
    assert csv["registered"] is False and csv["asked"] is False

    assert c.post("/api/assoc/register", json={"exts": [".csv"]}, headers=HEADERS).status_code == 200
    csv = next(t for t in c.get("/api/assoc/types", headers=HEADERS).json()["types"]
               if t["ext"] == ".csv")
    # An explicit register answers the one-time offer too.
    assert csv["registered"] is True and csv["asked"] is True


def test_default_route_enforces_the_handler_only_policy(client):
    server, c = client
    r = c.post("/api/assoc/default", json={"exts": [".xlsx"]}, headers=HEADERS)
    assert r.status_code == 400
    assert "handler" in r.json()["detail"]
    assert c.post("/api/assoc/default", json={"exts": [".csv"]}, headers=HEADERS).status_code == 200


def test_unknown_extension_is_a_400_not_a_registration(client):
    server, c = client
    r = c.post("/api/assoc/register", json={"exts": [".exe"]}, headers=HEADERS)
    assert r.status_code == 400


def test_asked_route_remembers_a_no(client):
    server, c = client
    assert c.post("/api/assoc/asked", json={"exts": [".xlsx"]}, headers=HEADERS).status_code == 200
    x = next(t for t in c.get("/api/assoc/types", headers=HEADERS).json()["types"]
             if t["ext"] == ".xlsx")
    assert x["asked"] is True and x["registered"] is False


def test_registration_routes_are_loopback_only(client, monkeypatch):
    server, c = client
    monkeypatch.setattr(server, "_is_loopback", lambda req: False)
    for route in ("/api/assoc/register", "/api/assoc/unregister",
                  "/api/assoc/default", "/api/assoc/asked"):
        assert c.post(route, json={"exts": [".csv"]}, headers=HEADERS).status_code == 403
    assert c.get("/api/assoc/types", headers=HEADERS).status_code == 403


def test_a_malformed_mimeapps_degrades_instead_of_raising(lin, tmp_path):
    """mimeapps.list is hand-editable and shared by every desktop app. A
    stray line must not take the Settings panel down (status reads as
    nothing-registered) — and register must REFUSE with the fix named,
    never parse-as-empty and write back, which would erase every other
    application's associations."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "mimeapps.list").write_text("no section header here\nkey=val\n")
    picked, cat = _cat(".csv")
    st = lin.status(cat)
    assert st[".csv"] == {"registered": False, "default": False}
    with pytest.raises(ValueError, match="malformed"):
        lin.register(picked, cat)
    # Untouched: the refusal is what protects the other apps' entries.
    assert "no section header here" in (cfg / "mimeapps.list").read_text()

    (cfg / "mimeapps.list").write_bytes(b"\xff\x8f" + b"\x00" * 40)
    assert lin.status(cat)[".csv"]["registered"] is False
    with pytest.raises(ValueError, match="malformed"):
        lin.register(picked, cat)


def test_a_corrupt_desktop_file_reads_as_no_mimes(lin, tmp_path):
    apps = tmp_path / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "winnow.desktop").write_bytes(b"\xff\xfegarbage\x00")
    picked, cat = _cat(".csv")
    assert lin.status(cat)[".csv"]["registered"] is False
    # And registering rewrites our file wholesale, recovering it.
    lin.register(picked, cat)
    assert lin.status(cat)[".csv"]["registered"] is True


def test_malformed_mimeapps_maps_to_400_not_500(client, tmp_path, monkeypatch):
    server, c = client
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "mimeapps.list").write_text("garbage\n")
    r = c.get("/api/assoc/types", headers=HEADERS)
    assert r.status_code == 200          # status degrades, panel stays up
    r = c.post("/api/assoc/register", json={"exts": [".csv"]}, headers=HEADERS)
    assert r.status_code == 400
    assert "malformed" in r.json()["detail"]


def test_the_winnow_case_type_is_in_the_catalogue_and_default_ok():
    """The one builtin that may be the double-click DEFAULT: nothing else
    owns .db-winnow, so claiming it can't steal a file from another app."""
    types = {t["ext"]: t for t in assoc.supported_types()}
    case = types[".db-winnow"]
    assert case["default_ok"] is True
    assert case["mime"] == "application/x-winnow-case"
    # And the generic .db is now handler-only evidence, not "Winnow case".
    assert types[".db"]["default_ok"] is False
    assert "case" not in types[".db"]["label"].lower()


def test_icon_files_exist_for_each_platform_flavour():
    for kind in ("ico", "png", "svg"):
        assert Path(assoc.icon_file(kind)).is_file(), kind


def test_linux_register_installs_the_desktop_and_mime_icon(lin, tmp_path, monkeypatch):
    monkeypatch.setattr(assoc.shutil, "which", lambda n: None)  # no theme-cache refresher
    case, cat = _cat(".db-winnow")
    lin.register(case, cat)
    desktop = (tmp_path / "share/applications/winnow.desktop").read_text()
    assert "Icon=" in desktop and "winnow-icon-256.png" in desktop
    icon = tmp_path / "share/icons/hicolor/scalable/mimetypes/application-x-winnow-case.svg"
    assert icon.is_file(), "the case mime should get a file-manager icon"
    lin.unregister(case, cat)
    assert not icon.exists(), "the mime icon goes when the type is unregistered"


def test_windows_register_sets_a_default_icon(win):
    case, cat = _cat(".db-winnow")
    win.register(case, cat)
    di = win.reg.keys["Software\\Classes\\Winnow.File\\DefaultIcon"][None]
    assert di.endswith("winnow.ico,0")


# ------------------------------------------------------- background launch


def test_hidden_interpreter_maps_to_pythonw_when_it_exists(tmp_path):
    """The whole Windows story as a pure sibling-file check — testable on
    any OS: python.exe → pythonw.exe iff pythonw.exe is really there."""
    py = tmp_path / "python.exe"
    py.write_text("")
    assert assoc._hidden_interpreter(str(py)) == str(py)   # no pythonw yet
    pyw = tmp_path / "pythonw.exe"
    pyw.write_text("")
    assert assoc._hidden_interpreter(str(py)) == str(pyw)
    # Already the hidden one, or not a python at all: untouched.
    assert assoc._hidden_interpreter(str(pyw)) == str(pyw)
    other = tmp_path / "ruby.exe"; other.write_text("")
    assert assoc._hidden_interpreter(str(other)) == str(other)


def test_background_register_writes_the_consoleless_command(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"; py.write_text("")
    pyw = tmp_path / "pythonw.exe"; pyw.write_text("")
    monkeypatch.setattr(assoc.sys, "executable", str(py))
    picked, cat = _cat(".csv")

    w = assoc.WindowsAssoc(reg=FakeReg(), background=True)
    w.register(picked, cat)
    cmd = w.reg.keys["Software\\Classes\\Winnow.File\\shell\\open\\command"][None]
    assert "pythonw.exe" in cmd

    w2 = assoc.WindowsAssoc(reg=FakeReg(), background=False)
    w2.register(picked, cat)
    cmd2 = w2.reg.keys["Software\\Classes\\Winnow.File\\shell\\open\\command"][None]
    assert "pythonw.exe" not in cmd2 and "python.exe" in cmd2


def test_refresh_command_rewrites_only_an_existing_progid(tmp_path, monkeypatch):
    """A settings toggle must update every registered type at once (they
    share the one ProgId command) — and must NOT create the ProgId as a
    side effect on a machine where Winnow was never registered."""
    py = tmp_path / "python.exe"; py.write_text("")
    pyw = tmp_path / "pythonw.exe"; pyw.write_text("")
    monkeypatch.setattr(assoc.sys, "executable", str(py))
    picked, cat = _cat(".csv")

    reg = FakeReg()
    assert assoc.WindowsAssoc(reg=reg, background=True).refresh_command() is False
    assert "Software\\Classes\\Winnow.File" not in reg.keys

    assoc.WindowsAssoc(reg=reg, background=False).register(picked, cat)
    assert assoc.WindowsAssoc(reg=reg, background=True).refresh_command() is True
    cmd = reg.keys["Software\\Classes\\Winnow.File\\shell\\open\\command"][None]
    assert "pythonw.exe" in cmd


def test_background_route_stores_and_reports_the_setting(client):
    server, c = client
    # ON by default — no console window riding along with a double-click.
    assert c.get("/api/assoc/types", headers=HEADERS).json()["background"] is True
    r = c.post("/api/assoc/background", json={"enabled": False}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["background"] is False
    # False is STORED, not dropped: with the default on, deleting the key
    # would silently re-enable the setting.
    assert c.get("/api/assoc/types", headers=HEADERS).json()["background"] is False
    assert c.post("/api/assoc/background", json={"enabled": True},
                  headers=HEADERS).json()["background"] is True


def test_background_route_is_loopback_only(client, monkeypatch):
    server, c = client
    monkeypatch.setattr(server, "_is_loopback", lambda req: False)
    assert c.post("/api/assoc/background", json={"enabled": True},
                  headers=HEADERS).status_code == 403


# --------------------------------------------------------- icon refresh


def test_linux_mime_icon_follows_an_icon_update(lin, tmp_path, monkeypatch):
    """The theme copy used to be written only when missing — an update
    that replaced the committed icon in place left every file manager
    showing the old design forever (the reported bug)."""
    monkeypatch.setattr(assoc.shutil, "which", lambda n: None)
    src = tmp_path / "mark.svg"
    src.write_text("<svg>v1</svg>")
    monkeypatch.setattr(assoc, "icon_file", lambda kind="png": str(src))
    case, cat = _cat(".db-winnow")
    lin.register(case, cat)
    dest = tmp_path / "share/icons/hicolor/scalable/mimetypes/application-x-winnow-case.svg"
    assert dest.read_text() == "<svg>v1</svg>"

    src.write_text("<svg>v2</svg>")             # the update replaced the icon
    assert lin.refresh_icons(cat) is True
    assert dest.read_text() == "<svg>v2</svg>"
    assert lin.refresh_icons(cat) is False      # settled: nothing to do


def test_linux_refresh_is_a_noop_when_never_registered(lin, tmp_path):
    _, cat = _cat(".db-winnow")
    assert lin.refresh_icons(cat) is False
    assert not (tmp_path / "share/icons").exists()


def test_windows_registration_pokes_explorers_icon_cache(tmp_path, monkeypatch):
    """Explorer caches association icons; SHCNE_ASSOCCHANGED is the only
    thing that makes it look again. Every mutation must fire it."""
    pokes = []
    w = assoc.WindowsAssoc(reg=FakeReg(), notify=lambda: pokes.append(1))
    picked, cat = _cat(".csv")
    w.register(picked, cat)
    assert pokes, "register must notify"
    n = len(pokes)
    w.make_default(picked, cat)
    assert len(pokes) > n, "make_default must notify"
    n = len(pokes)
    w.unregister(picked, cat)
    assert len(pokes) > n, "unregister must notify"


def test_windows_icon_refresh_fires_only_on_a_changed_icon(tmp_path, monkeypatch):
    ico = tmp_path / "winnow.ico"
    ico.write_bytes(b"v1-icon-bytes")
    monkeypatch.setattr(assoc, "icon_file", lambda kind="png": str(ico))
    pokes = []
    reg = FakeReg()
    w = assoc.WindowsAssoc(reg=reg, notify=lambda: pokes.append(1))
    picked, cat = _cat(".csv")

    # Never registered: refresh must not create keys or poke anything.
    assert w.refresh_icons(cat) is False
    assert "Software\\Classes\\Winnow.File" not in reg.keys

    w.register(picked, cat)
    pokes.clear()
    assert w.refresh_icons(cat) is False        # same icon: quiet
    assert pokes == []

    ico.write_bytes(b"v2-icon-bytes")           # the update replaced the icon
    assert w.refresh_icons(cat) is True
    assert pokes, "a changed icon must poke Explorer"
    assert w.refresh_icons(cat) is False        # hash re-recorded: settled

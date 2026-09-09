"""OS file associations — putting Winnow in the Open With menu.

Everything here writes PER-USER state only: HKCU on Windows,
~/.local/share + ~/.config on Linux. No admin rights, no installer, no
network — the same constraints as the rest of the tool. macOS is out of
scope (a real association there needs a .app bundle, which a
run-from-source Python tool is not).

Two decisions carried over from the feature review, encoded in the
catalogue rather than left to the UI:

- Winnow registers as a HANDLER (an Open With entry) for everything it
  can ingest, but offers "make default" only for types where hijacking
  the double-click can't cost the analyst anything (`default_ok`).
  .txt/.json/.db/.xlsx have real owners — Notepad, editors, DB Browser,
  Excel — and a forensic tool that steals .xlsx from Excel is the kind
  of thing that gets it deleted. Plugin extensions are handler-only
  too: they aren't vetted, and a plugin is free to claim .zip.

- On Windows, "make default" writes the classic HKCU\\Software\\Classes
  association and then tells the truth: since Windows 8 Explorer's
  UserChoice key (hash-protected, deliberately unwritable) wins when
  present, so the caller gets `windows_userchoice: True` back and the
  UI walks the analyst through the one supported path — the system
  "Open With → Always" dialog. Pretending the write worked would be
  worse than not offering the button.

The adapters take their environment as constructor arguments (a
registry object, XDG paths) so every path/format decision in here is
testable on any platform; only the two-line defaults touch the real OS.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
from configparser import ConfigParser, Error
from pathlib import Path

from . import paths

# ---------------------------------------------------------------- catalogue

# ext → (label, linux mime, default_ok). The mime is the type a Linux
# desktop actually RESOLVES for such a file (shared-mime-info sniffs
# SQLite by magic, so extensionless-looking .db files still land on
# vnd.sqlite3); an entry under a mime nothing resolves to would be dead.
# The Winnow case type gets its own mime so a Linux desktop can carry a
# distinct file icon for it (application/x-winnow-case, glob *.db-winnow,
# supplied by our shared-mime-info package). It is the one builtin type
# that IS default_ok: nothing else on the system owns .db-winnow, so
# making Winnow its default double-click can't steal a file from Excel,
# an editor or a DB browser the way claiming .db/.xlsx would.
CASE_TYPE: dict = {"ext": ".db-winnow", "label": "Winnow case",
                   "mime": "application/x-winnow-case", "default_ok": True, "is_case": True}

BUILTIN_TYPES: list[dict] = [
    CASE_TYPE,
    {"ext": ".csv", "label": "Comma-separated values", "mime": "text/csv", "default_ok": True},
    {"ext": ".tsv", "label": "Tab-separated values", "mime": "text/tab-separated-values", "default_ok": True},
    {"ext": ".txt", "label": "Delimited text", "mime": "text/plain", "default_ok": False},
    {"ext": ".json", "label": "JSON table", "mime": "application/json", "default_ok": False},
    {"ext": ".jsonl", "label": "JSON lines", "mime": "application/json", "default_ok": True},
    {"ext": ".ndjson", "label": "Newline-delimited JSON", "mime": "application/json", "default_ok": True},
    # SQLite databases the analyst IMPORTS as evidence (Chromium History.db
    # and the like). Handler-only — a DB browser or the OS may own these,
    # and a Winnow case now has its own extension above, so this no longer
    # doubles as "Winnow case".
    {"ext": ".db", "label": "SQLite database", "mime": "application/vnd.sqlite3", "default_ok": False},
    {"ext": ".sqlite", "label": "SQLite database", "mime": "application/vnd.sqlite3", "default_ok": False},
    {"ext": ".sqlite3", "label": "SQLite database", "mime": "application/vnd.sqlite3", "default_ok": False},
    {"ext": ".xlsx", "label": "Excel workbook", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "default_ok": False},
    {"ext": ".xlsm", "label": "Excel macro workbook", "mime": "application/vnd.ms-excel.sheet.macroEnabled.12", "default_ok": False},
]


def supported_types(plugin_formats: list[dict] | None = None) -> list[dict]:
    """The full association catalogue: builtins plus every extension a
    loaded plugin ingest format claims. Plugin filename PATTERNS ($MFT,
    $J) are absent by construction — an extensionless file cannot be
    associated on either OS, which docs/notes/plugins.md already warns
    plugin authors about."""
    out = [dict(t, source="builtin") for t in BUILTIN_TYPES]
    seen = {t["ext"] for t in out}
    for fmt in plugin_formats or []:
        for ext in fmt.get("extensions", []):
            ext = ext.lower()
            if not ext.startswith(".") or ext in seen:
                continue
            seen.add(ext)
            out.append({
                "ext": ext,
                "label": fmt.get("label", ext),
                # No standard mime resolves for a novel extension, so
                # Linux needs the glob from our mime package (below).
                "mime": f"application/x-winnow{ext.replace('.', '-')}",
                # Default-ELIGIBLE, but never default silently: a plugin
                # extension only becomes the double-click default through
                # an explicit analyst choice (the new-extension launch
                # prompt, or the panel's button). That consent is what the
                # old blanket handler-only rule stood in for.
                "default_ok": True,
                "source": fmt.get("plugin", "plugin"),
            })
    return out


def _hidden_interpreter(executable: str) -> str:
    """The console-less flavour of an interpreter, when one exists beside
    it. CPython on Windows ships pythonw.exe next to python.exe: same
    interpreter, no console window — which is the whole difference
    between "double-click a file, Winnow opens" and "double-click a
    file, a PowerShell-looking box also opens and sits there for the
    server's lifetime". Purely a sibling-file check, no platform sniff,
    so it's testable anywhere and a no-op on Linux (no pythonw exists)."""
    p = Path(executable)
    name = p.name.lower()
    if name.startswith("python") and not name.startswith("pythonw"):
        w = p.with_name("pythonw" + p.name[len("python"):])
        if w.is_file():
            return str(w)
    return executable


def launch_command(background: bool = False) -> list[str]:
    """What the OS should run to open files with Winnow — this install's
    interpreter against this install's server.py, resolved now so a
    moved install re-registers rather than silently pointing at air.
    `background` swaps in the console-less interpreter where one exists
    (see _hidden_interpreter) — an analyst-facing setting, because the
    hidden flavour also hides the server log, which is exactly what you
    do NOT want while troubleshooting an association that won't open."""
    exe = _hidden_interpreter(sys.executable) if background else sys.executable
    return [exe, str(paths.INSTALL_ROOT / "server.py"), "--assoc"]


_ICON_DIR = paths.INSTALL_ROOT / "static" / "icons"


def _icon_hash() -> str:
    """A fingerprint of the committed .ico, recorded at registration so a
    later start can tell 'same path, different icon' — the case Explorer's
    cache can't see on its own."""
    try:
        with open(icon_file("ico"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def icon_file(kind: str = "png") -> str:
    """The committed brand icon, by flavour. `ico` for Windows (the
    multi-resolution file Explorer and the Open With menu want), `svg`
    for the scalable Linux mime-type icon, `png` for a `.desktop`
    Icon= that even an older file manager renders."""
    return str(_ICON_DIR / {
        "ico": "winnow.ico",
        "svg": "winnow-mark.svg",
    }.get(kind, "winnow-icon-256.png"))


# ------------------------------------------------------------------- linux


class LinuxAssoc:
    """Per-user Linux registration: a .desktop entry naming the mimes we
    handle, [Added Associations] lines in mimeapps.list for handler-ship,
    [Default Applications] lines for defaults, and — only for plugin
    extensions no desktop knows — a shared-mime-info package supplying
    the glob. All plain files under $XDG_DATA_HOME/$XDG_CONFIG_HOME."""

    DESKTOP_ID = "winnow.desktop"

    def __init__(self, data_home: str | None = None, config_home: str | None = None):
        home = Path.home()
        self.data = Path(data_home or os.environ.get("XDG_DATA_HOME") or home / ".local/share")
        self.config = Path(config_home or os.environ.get("XDG_CONFIG_HOME") or home / ".config")

    # -- files

    def _desktop_path(self) -> Path:
        return self.data / "applications" / self.DESKTOP_ID

    def _mimeapps_path(self) -> Path:
        return self.config / "mimeapps.list"

    def _mime_pkg_path(self) -> Path:
        return self.data / "mime" / "packages" / "winnow.xml"

    def _read_mimeapps(self) -> ConfigParser:
        cp = ConfigParser(delimiters=("=",), strict=False, interpolation=None)
        cp.optionxform = str  # mime types are case-sensitive keys
        p = self._mimeapps_path()
        if p.exists():
            # mimeapps.list is a hand-editable file shared by every desktop
            # app; a stray line or a corrupt write must surface as a
            # fixable message, not a 500 — and register() must NOT press on
            # with an empty parse, or writing back would erase every other
            # application's associations.
            try:
                cp.read(p, encoding="utf-8")
            except (UnicodeDecodeError, Error) as e:
                raise ValueError(
                    f"{p} is malformed ({e.__class__.__name__}) — fix or remove it, "
                    "then try again") from e
        for sec in ("Default Applications", "Added Associations", "Removed Associations"):
            if not cp.has_section(sec):
                cp.add_section(sec)
        return cp

    def _write_mimeapps(self, cp: ConfigParser) -> None:
        p = self._mimeapps_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=False)

    @staticmethod
    def _add_entry(cp: ConfigParser, section: str, mime: str, desktop: str, *, front: bool) -> None:
        cur = [x for x in (cp.get(section, mime, fallback="") or "").split(";") if x]
        if desktop in cur:
            cur.remove(desktop)
        cur.insert(0 if front else len(cur), desktop)
        cp.set(section, mime, ";".join(cur) + ";")

    @staticmethod
    def _drop_entry(cp: ConfigParser, section: str, mime: str, desktop: str) -> None:
        cur = [x for x in (cp.get(section, mime, fallback="") or "").split(";") if x]
        if desktop in cur:
            cur.remove(desktop)
            if cur:
                cp.set(section, mime, ";".join(cur) + ";")
            else:
                cp.remove_option(section, mime)

    def _write_desktop(self, mimes: list[str]) -> None:
        cmd = launch_command()
        exe = " ".join(_desktop_quote(a) for a in cmd)
        body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Winnow\n"
            "Comment=Read and tag large timelines\n"
            f"Exec={exe} %F\n"
            "Terminal=false\n"
            "NoDisplay=true\n"          # a file handler, not a launcher-menu app
            f"Icon={icon_file('png')}\n"  # absolute path: no install-into-theme step needed for the app icon
            f"MimeType={';'.join(sorted(mimes))};\n"
            "Categories=Utility;\n"
        )
        p = self._desktop_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    def _desktop_mimes(self) -> set[str]:
        p = self._desktop_path()
        if not p.exists():
            return set()
        # OUR file: if something corrupted it, "no mimes" is the honest
        # reading, and the next register() rewrites it wholesale anyway.
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MimeType="):
                return {m for m in line[len("MimeType="):].split(";") if m}
        return set()

    def _write_mime_package(self, glob_types: list[dict]) -> None:
        """shared-mime-info package for extensions no desktop resolves —
        plugin types only. Removed (and the db refreshed) when empty."""
        p = self._mime_pkg_path()
        if not glob_types:
            if p.exists():
                p.unlink()
                self._refresh_mime_db()
            return
        rows = "".join(
            f'  <mime-type type="{t["mime"]}">\n'
            f'    <comment>{t["label"]}</comment>\n'
            f'    <glob pattern="*{t["ext"]}"/>\n'
            f'  </mime-type>\n' for t in sorted(glob_types, key=lambda t: t["ext"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
            f"{rows}</mime-info>\n", encoding="utf-8")
        self._refresh_mime_db()

    def _refresh_mime_db(self) -> None:
        exe = shutil.which("update-mime-database")
        if exe:
            try:
                subprocess.run([exe, str(self.data / "mime")], capture_output=True, timeout=30)
            except OSError:
                pass

    def _refresh_desktop_db(self) -> None:
        exe = shutil.which("update-desktop-database")
        if exe:
            try:
                subprocess.run([exe, str(self.data / "applications")], capture_output=True, timeout=30)
            except OSError:
                pass

    # -- operations. `types` are catalogue entries.

    def register(self, types: list[dict], catalogue: list[dict]) -> None:
        cp = self._read_mimeapps()
        mimes = self._desktop_mimes()
        for t in types:
            mimes.add(t["mime"])
            self._add_entry(cp, "Added Associations", t["mime"], self.DESKTOP_ID, front=False)
            # A past manual removal would silently defeat re-registration.
            self._drop_entry(cp, "Removed Associations", t["mime"], self.DESKTOP_ID)
        self._write_desktop(sorted(mimes))
        self._write_mimeapps(cp)
        self._sync_mime_package(mimes, catalogue)
        self._refresh_desktop_db()

    def unregister(self, types: list[dict], catalogue: list[dict]) -> None:
        cp = self._read_mimeapps()
        mimes = self._desktop_mimes()
        for t in types:
            mimes.discard(t["mime"])
            self._drop_entry(cp, "Added Associations", t["mime"], self.DESKTOP_ID)
            self._drop_entry(cp, "Default Applications", t["mime"], self.DESKTOP_ID)
        if mimes:
            self._write_desktop(sorted(mimes))
        elif self._desktop_path().exists():
            self._desktop_path().unlink()
        self._write_mimeapps(cp)
        self._sync_mime_package(mimes, catalogue)
        self._refresh_desktop_db()

    def make_default(self, types: list[dict], catalogue: list[dict]) -> None:
        self.register(types, catalogue)   # a default that isn't a handler is nonsense
        cp = self._read_mimeapps()
        for t in types:
            self._add_entry(cp, "Default Applications", t["mime"], self.DESKTOP_ID, front=True)
        self._write_mimeapps(cp)

    def _sync_mime_package(self, live_mimes: set[str], catalogue: list[dict]) -> None:
        ours = [t for t in catalogue
                if t["mime"].startswith("application/x-winnow") and t["mime"] in live_mimes]
        self._write_mime_package(ours)
        self._sync_mime_icons(ours)

    def _mime_icon_path(self, mime: str) -> Path:
        # freedesktop convention: a mimetype icon is named after the mime
        # with the slash turned into a dash, under the theme's mimetypes/.
        return (self.data / "icons" / "hicolor" / "scalable" / "mimetypes"
                / f"{mime.replace('/', '-')}.svg")

    def _sync_mime_icons(self, our_types: list[dict]) -> bool:
        """Give our own mime types a file icon in the icon theme, so a file
        manager shows the brand mark on a .db-winnow case rather than a
        generic page. Only our x-winnow mimes get one — a standard mime
        (text/csv) already has a themed icon we must not shadow.

        Copies when the COPY differs, not just when it's missing: the
        source icon is a committed file an update replaces in place, and
        the only-if-missing version pinned the theme copy to whatever the
        icon looked like on first registration, forever (reported as
        associated files keeping the old icon after an update)."""
        wanted = {self._mime_icon_path(t["mime"]) for t in our_types}
        src = Path(icon_file("svg"))
        src_bytes = src.read_bytes() if src.is_file() else None
        refresh = False
        for dest in wanted:
            if src_bytes is None:
                continue
            if not dest.exists() or dest.read_bytes() != src_bytes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                refresh = True
        # Drop icons for mimes we no longer register (e.g. after unregister).
        icon_dir = self.data / "icons" / "hicolor" / "scalable" / "mimetypes"
        if icon_dir.is_dir():
            for f in icon_dir.glob("application-x-winnow*.svg"):
                if f not in wanted:
                    f.unlink()
                    refresh = True
        if refresh:
            exe = shutil.which("gtk-update-icon-cache")
            if exe:
                with contextlib.suppress(OSError):
                    subprocess.run([exe, "-f", "-t", str(self.data / "icons" / "hicolor")],
                                   capture_output=True, timeout=30)
        return refresh

    def refresh_icons(self, catalogue: list[dict]) -> bool:
        """Re-sync the theme copies against the committed icon — called at
        server startup so an update that changed the icon propagates
        without the analyst re-registering anything. A no-op on a machine
        where Winnow was never registered (no .desktop entry)."""
        if not self._desktop_path().exists():
            return False
        return self._sync_mime_icons(
            [t for t in catalogue
             if t["mime"].startswith("application/x-winnow") and t["mime"] in self._desktop_mimes()])

    def status(self, catalogue: list[dict]) -> dict[str, dict]:
        try:
            cp = self._read_mimeapps()
        except ValueError:
            # Reporting must never take the Settings panel down; a
            # malformed mimeapps.list reads as "nothing registered", and
            # the actionable message arrives when a CHANGE is attempted.
            return {t["ext"]: {"registered": False, "default": False} for t in catalogue}
        mimes = self._desktop_mimes()

        def _in(section, mime):
            return self.DESKTOP_ID in (cp.get(section, mime, fallback="") or "").split(";")

        out = {}
        for t in catalogue:
            registered = t["mime"] in mimes and (
                _in("Added Associations", t["mime"]) or _in("Default Applications", t["mime"]))
            default = _in("Default Applications", t["mime"]) and (
                cp.get("Default Applications", t["mime"], fallback="").split(";")[0] == self.DESKTOP_ID)
            out[t["ext"]] = {"registered": registered, "default": default}
        return out


def _desktop_quote(arg: str) -> str:
    # Desktop-entry Exec quoting: double quotes with backslash-escaping —
    # NOT shell quoting. Paths with spaces are the norm on analyst boxes.
    if not any(c in arg for c in ' "\\'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------- windows


def _shell_assoc_changed() -> None:  # pragma: no cover - windows only
    """SHChangeNotify(SHCNE_ASSOCCHANGED): tell Explorer the association
    world changed, which is also what flushes its ICON cache for
    associated types. Without it, an icon replaced in place at the same
    path (exactly what an update does to winnow.ico) keeps showing its
    old cached bitmap indefinitely."""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:  # noqa: BLE001 — cosmetic refresh, never worth failing over
        pass


def hkcu_set(reg, path: str, name: str | None, value: str) -> None:
    """Write one REG_SZ value under HKEY_CURRENT_USER, creating the key.
    Shared by the file-association and user-environment stores so the
    open/set/close dance lives once."""
    key = reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE)
    try:
        reg.SetValueEx(key, name, 0, reg.REG_SZ, value)
    finally:
        reg.CloseKey(key)


def hkcu_delete_value(reg, path: str, name: str | None) -> bool:
    """Delete one value under HKEY_CURRENT_USER; True if something was
    removed, False when the key or value wasn't there."""
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        reg.DeleteValue(key, name)
        return True
    except OSError:
        return False
    finally:
        reg.CloseKey(key)


class WindowsAssoc:
    """Per-user Windows registration under HKCU\\Software\\Classes: one
    ProgId holding the open command, OpenWithProgids per extension for
    handler-ship, the classic default value per extension for "make
    default" — with the UserChoice caveat reported, not papered over.
    Takes a `reg` object (the real `winreg` module in production, a fake
    in tests) so the key/value layout is testable off-Windows."""

    PROGID = "Winnow.File"

    def __init__(self, reg=None, background: bool = False, notify=None):
        if reg is None:
            import winreg as reg  # pragma: no cover - windows only
        self.reg = reg
        self.background = background
        # Injectable for tests; the real one pokes Explorer's icon cache.
        self._notify = _shell_assoc_changed if notify is None else notify

    def _hkcu(self):
        return self.reg.HKEY_CURRENT_USER

    def _set(self, path: str, name: str | None, value: str) -> None:
        hkcu_set(self.reg, path, name, value)

    def _get(self, path: str, name: str | None) -> str | None:
        try:
            key = self.reg.OpenKey(self._hkcu(), path, 0, self.reg.KEY_READ)
        except OSError:
            return None
        try:
            return self.reg.QueryValueEx(key, name)[0]
        except OSError:
            return None
        finally:
            self.reg.CloseKey(key)

    def _delete_value(self, path: str, name: str | None) -> None:
        hkcu_delete_value(self.reg, path, name)

    def _delete_tree(self, path: str) -> None:
        try:
            while True:
                key = self.reg.OpenKey(self._hkcu(), path, 0, self.reg.KEY_READ)
                try:
                    sub = self.reg.EnumKey(key, 0)
                finally:
                    self.reg.CloseKey(key)
                self._delete_tree(f"{path}\\{sub}")
        except OSError:
            pass
        try:
            self.reg.DeleteKey(self._hkcu(), path)
        except OSError:
            pass

    def _ensure_progid(self) -> None:
        base = f"Software\\Classes\\{self.PROGID}"
        self._set(base, None, "Winnow")
        # DefaultIcon is what puts the brand mark on the Open With → Winnow
        # menu entry and on any file type Winnow is the default for (a
        # .db-winnow case, above all). ",0" = the first icon in the .ico.
        self._set(f"{base}\\DefaultIcon", None, f"{icon_file('ico')},0")
        self._set(base, "IconHash", _icon_hash())
        self._set(f"{base}\\shell\\open\\command", None,
                  subprocess.list2cmdline(launch_command(self.background)) + ' "%1"')

    def refresh_icons(self, catalogue: list[dict]) -> bool:
        """Explorer caches association icons by path, so replacing
        winnow.ico in place (an update) changes nothing on screen until
        something says SHCNE_ASSOCCHANGED. Compare the recorded hash of
        the icon we registered with what's on disk now; on a change,
        re-stamp and poke Explorer. No-op where Winnow was never
        registered — a startup must not create registry keys."""
        base = f"Software\\Classes\\{self.PROGID}"
        if self._get(base, None) is None:
            return False
        current = _icon_hash()
        if self._get(base, "IconHash") == current:
            return False
        self._set(f"{base}\\DefaultIcon", None, f"{icon_file('ico')},0")
        self._set(base, "IconHash", current)
        self._notify()
        return True

    def refresh_command(self) -> bool:
        """Rewrite the ProgId's open command to match the current
        background setting — the one registry value every registered
        extension shares, so a toggle takes effect immediately without
        re-registering each type. No-op (False) when Winnow was never
        registered: creating the ProgId as a side effect of a settings
        toggle would be registration by surprise."""
        if self._get(f"Software\\Classes\\{self.PROGID}", None) is None:
            return False
        self._ensure_progid()
        self._notify()
        return True

    def _userchoice_present(self, ext: str) -> bool:
        path = ("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"
                f"\\FileExts\\{ext}\\UserChoice")
        try:
            key = self.reg.OpenKey(self._hkcu(), path, 0, self.reg.KEY_READ)
        except OSError:
            return False
        self.reg.CloseKey(key)
        return True

    def register(self, types: list[dict], catalogue: list[dict]) -> None:
        self._ensure_progid()
        for t in types:
            self._set(f"Software\\Classes\\{t['ext']}\\OpenWithProgids", self.PROGID, "")
        self._notify()

    def unregister(self, types: list[dict], catalogue: list[dict]) -> None:
        for t in types:
            ext = t["ext"]
            self._delete_value(f"Software\\Classes\\{ext}\\OpenWithProgids", self.PROGID)
            if self._get(f"Software\\Classes\\{ext}", None) == self.PROGID:
                self._delete_value(f"Software\\Classes\\{ext}", None)
        still = any(v["registered"] for v in self.status(catalogue).values())
        if not still:
            self._delete_tree(f"Software\\Classes\\{self.PROGID}")
        self._notify()

    def make_default(self, types: list[dict], catalogue: list[dict]) -> dict:
        self.register(types, catalogue)
        blocked = []
        for t in types:
            self._set(f"Software\\Classes\\{t['ext']}", None, self.PROGID)
            if self._userchoice_present(t["ext"]):
                blocked.append(t["ext"])
        self._notify()
        return {"userchoice": blocked}

    def status(self, catalogue: list[dict]) -> dict[str, dict]:
        out = {}
        for t in catalogue:
            ext = t["ext"]
            registered = self._get(
                f"Software\\Classes\\{ext}\\OpenWithProgids", self.PROGID) is not None
            default = self._get(f"Software\\Classes\\{ext}", None) == self.PROGID
            out[ext] = {"registered": registered, "default": default,
                        "windows_userchoice": self._userchoice_present(ext) if default else False}
        return out


# --------------------------------------------------------------- dispatch


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def adapter(background: bool = False):
    name = platform_name()
    if name == "windows":
        return WindowsAssoc(background=background)
    if name == "linux":
        # Terminal=false in the .desktop entry already means no console on
        # Linux; the flag has nothing to change there.
        return LinuxAssoc()
    return None

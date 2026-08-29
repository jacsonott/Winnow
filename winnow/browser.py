"""Opening Winnow in a browser window that looks like an application.

`python server.py` used to call `webbrowser.open()`, which hands the URL
to whatever the default browser is and gets a tab: address bar, bookmarks
bar, the analyst's other twenty tabs, and nothing to distinguish Winnow
from a website. Chromium-family browsers have a flag for exactly this —
`--app=<url>` opens a chromeless window with its own taskbar entry — so
that is what we use when we can find one, falling back to the old
behaviour when we cannot.

Two decisions worth knowing about:

**No `--user-data-dir`.** A dedicated profile would give Winnow taskbar
identity and isolation from the analyst's extensions, and it is the first
thing every "chrome app mode" recipe suggests. It is wrong here: Winnow's
appearance, keybindings, sidebar width, detail-pane layout and remote-mode
choice all live in localStorage, which is per-profile. Passing a fresh
profile directory would silently reset every one of those on upgrade, and
"my theme and hotkeys are gone" is a far worse first impression than a
shared profile. `--browser-profile` is there for anyone who wants the
isolation and is willing to re-set their preferences once.

**Edge first on Windows.** It is present on every supported Windows
install, so it is the one that reliably exists; Chrome only wins if the
analyst installed it. Everywhere else Chrome leads, for the same
"what is actually here" reason. Firefox has no app-mode equivalent
(`--kiosk` is fullscreen-with-no-exit, which is not this), so it is only
ever reached through the fallback.
"""

from __future__ import annotations

import ntpath
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import webbrowser

# Ordered by how likely the browser is to be present, per platform — the
# first one found wins. Windows leads with Edge because it ships with the
# OS; the others are only there if someone installed them.
_WINDOWS_CANDIDATES = (
    (r"Microsoft\Edge\Application\msedge.exe", ("ProgramFiles(x86)", "ProgramFiles")),
    (r"Google\Chrome\Application\chrome.exe", ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")),
    (r"BraveSoftware\Brave-Browser\Application\brave.exe", ("ProgramFiles", "ProgramFiles(x86)")),
    (r"Chromium\Application\chrome.exe", ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")),
)

_MACOS_CANDIDATES = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
    "Chromium.app/Contents/MacOS/Chromium",
)

_LINUX_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable", "brave-browser",
)


def find_chromium(*, system=None, which=None, isfile=None, environ=None) -> str | None:
    """Path to a Chromium-family browser, or None. The keyword arguments
    exist so the tests can ask "what would this do on Windows" without
    being on Windows or having any of these installed."""
    system = system or platform.system()
    which = which or shutil.which
    isfile = isfile or os.path.isfile
    environ = environ if environ is not None else os.environ

    if system == "Windows":
        for suffix, roots in _WINDOWS_CANDIDATES:
            for root in roots:
                base = environ.get(root)
                if not base:
                    continue
                # ntpath, not os.path: on Windows they are the same module,
                # and off Windows this still builds a real Windows path, so
                # the tests can drive this branch from any platform.
                path = ntpath.join(base, suffix)
                if isfile(path):
                    return path
        return None

    if system == "Darwin":
        for suffix in _MACOS_CANDIDATES:
            for base in ("/Applications", os.path.expanduser("~/Applications")):
                path = os.path.join(base, suffix)
                if isfile(path):
                    return path
        return None

    for name in _LINUX_CANDIDATES:
        found = which(name)
        if found:
            return found
    return None


def app_command(url: str, *, profile_dir: str | None = None, **kw) -> list[str] | None:
    """argv that opens `url` as a chromeless app window, or None when no
    Chromium-family browser is around to do it."""
    exe = find_chromium(**kw)
    if not exe:
        return None
    # --app must carry a full URL with scheme; a bare host:port is treated
    # as a search term and lands the analyst on a search results page.
    argv = [exe, f"--app={url}"]
    if profile_dir:
        argv.append(f"--user-data-dir={profile_dir}")
    return argv


def _port_is_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_when_ready(url: str, host: str, port: int, *, app_mode: bool = True,
                    profile_dir: str | None = None, wait_s: float = 15.0) -> threading.Thread:
    """Open the UI once the server is actually accepting connections.

    The old code opened the browser immediately and then started uvicorn,
    which races: the browser can reach the port before anything is
    listening. A tab that lands on a connection error is merely annoying;
    an app window is worse, since there is no address bar to retry from.
    So this waits for the port and runs on a daemon thread — uvicorn.run()
    blocks the main one.
    """

    def run():
        deadline = time.monotonic() + wait_s
        # 127.0.0.1 rather than whatever --host says: 0.0.0.0 is a bind
        # address, not somewhere you can connect to.
        probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        while time.monotonic() < deadline:
            if _port_is_open(probe, port):
                break
            time.sleep(0.1)
        argv = app_command(url, profile_dir=profile_dir) if app_mode else None
        if argv:
            try:
                launch_detached(argv)
                return
            except OSError:
                pass  # fall through to the default browser
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — never let opening a window stop the server
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def launch_detached(argv: list[str]) -> None:
    """Start the browser in its own session so Ctrl+C on the server does
    not also kill the window the analyst is reading."""
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, **kwargs)

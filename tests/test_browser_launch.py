"""Opening the UI as an app window instead of a browser tab.

Browser discovery is the part worth testing and the part that can't be
tested by running it: the machine doing so has one platform and whatever
browsers happen to be installed. So find_chromium takes its platform and
its filesystem lookups as arguments, and these drive it through all three
platforms with nothing actually present."""

from __future__ import annotations

import pytest

from winnow import browser

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME_WIN = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIN_ENV = {"ProgramFiles": r"C:\Program Files",
           "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\a\AppData\Local"}


def _win(present):
    return browser.find_chromium(system="Windows", environ=WIN_ENV,
                                 isfile=lambda p: p in present, which=lambda n: None)


def test_windows_prefers_edge_when_both_are_installed():
    """Edge ships with Windows, so it is the one that reliably exists."""
    assert _win({EDGE, CHROME_WIN}) == EDGE


def test_windows_falls_back_to_chrome_when_edge_is_absent():
    assert _win({CHROME_WIN}) == CHROME_WIN


def test_windows_with_no_chromium_browser_finds_nothing():
    assert _win(set()) is None


def test_macos_looks_inside_the_app_bundle():
    found = browser.find_chromium(
        system="Darwin", which=lambda n: None,
        isfile=lambda p: p == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    assert found.endswith("Google Chrome")


def test_linux_resolves_through_the_path():
    found = browser.find_chromium(system="Linux", isfile=lambda p: False,
                                  which=lambda n: "/usr/bin/chromium" if n == "chromium" else None)
    assert found == "/usr/bin/chromium"


def test_the_command_carries_a_full_url():
    """A bare host:port is treated as a search term by Chromium and lands
    the analyst on a search results page."""
    argv = browser.app_command("http://127.0.0.1:8777", system="Linux", isfile=lambda p: False,
                               which=lambda n: "/usr/bin/chromium" if n == "chromium" else None)
    assert argv[1] == "--app=http://127.0.0.1:8777"
    assert argv[1].startswith("--app=http")


def test_no_profile_directory_is_passed_by_default():
    """Appearance, keybindings and panel sizes live in the profile's
    localStorage — handing Winnow a fresh one would silently reset every
    analyst's settings on upgrade."""
    argv = browser.app_command("http://127.0.0.1:8777", system="Linux", isfile=lambda p: False,
                               which=lambda n: "/usr/bin/chromium")
    assert not any(a.startswith("--user-data-dir") for a in argv)


def test_a_profile_directory_is_passed_when_asked_for():
    argv = browser.app_command("http://127.0.0.1:8777", profile_dir="/tmp/p", system="Linux",
                               isfile=lambda p: False, which=lambda n: "/usr/bin/chromium")
    assert "--user-data-dir=/tmp/p" in argv


def test_no_browser_found_means_no_command():
    assert browser.app_command("http://x", system="Linux",
                               isfile=lambda p: False, which=lambda n: None) is None


def test_it_waits_for_the_port_then_opens(monkeypatch):
    """The old code opened the browser before uvicorn started listening."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    launched = []
    monkeypatch.setattr(browser, "app_command", lambda *a, **k: ["fake-browser"])
    monkeypatch.setattr(browser, "launch_detached", lambda argv: launched.append(argv))

    t = browser.open_when_ready(f"http://127.0.0.1:{port}", "127.0.0.1", port, wait_s=5)
    threading.Timer(0.3, srv.listen).start()   # server starts accepting late
    t.join(timeout=8)
    srv.close()
    assert launched == [["fake-browser"]]


def test_it_falls_back_to_the_default_browser_without_a_chromium(monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "app_command", lambda *a, **k: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda u: opened.append(u))
    browser.open_when_ready("http://127.0.0.1:1", "127.0.0.1", 1, wait_s=0.2).join(timeout=5)
    assert opened == ["http://127.0.0.1:1"]


def test_a_browser_that_will_not_start_still_falls_back(monkeypatch):
    """Never let opening a window be the thing that stops Winnow."""
    opened = []
    monkeypatch.setattr(browser, "app_command", lambda *a, **k: ["/nope/browser"])
    monkeypatch.setattr(browser, "launch_detached",
                        lambda argv: (_ for _ in ()).throw(OSError("no such file")))
    monkeypatch.setattr(browser.webbrowser, "open", lambda u: opened.append(u))
    browser.open_when_ready("http://127.0.0.1:1", "127.0.0.1", 1, wait_s=0.2).join(timeout=5)
    assert opened == ["http://127.0.0.1:1"]


def test_binding_all_interfaces_still_probes_loopback(monkeypatch):
    """0.0.0.0 is a bind address, not somewhere you can connect to."""
    probed = []
    monkeypatch.setattr(browser, "_port_is_open",
                        lambda h, p, timeout=0.2: probed.append(h) or True)
    monkeypatch.setattr(browser, "app_command", lambda *a, **k: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda u: None)
    browser.open_when_ready("http://x", "0.0.0.0", 8777, wait_s=1).join(timeout=5)
    assert probed and probed[0] == "127.0.0.1"
